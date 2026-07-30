"""Persistent Jupyter kernels — one lazy kernel per (session, language).

Ported from the opencodon donor (``execution/kernel_client.py`` +
``execution/manager.py``), simplified for opencodon:

- **KernelSession** is a thin synchronous wrapper over one Jupyter kernel.
  Every execute request is correlated by its Jupyter ``msg_id`` — never a
  process-global "current cell" — so interleaved iopub traffic cannot
  misattribute output.
- **SessionKernelManager** owns at most one live kernel per
  ``(session_id, language)``. Cells serialize on a per-key lock. A timeout or
  kernel death *taints* the kernel: it is shut down and the next run_code
  lazily starts a fresh one (interruption is not a rollback).
- **Where** a kernel runs is a KernelProvisioner decision. Everything above
  that seam is transport-agnostic — msg_id correlation, taint/restart, the
  execution_log and lineage writes — so a remote backend supplies a
  provisioner and nothing else. LocalProvisioner is the default.

The donor's epoch/policy record machinery is deliberately dropped: opencodon
records each cell in ``execution_log`` (science/store.py) and a restart shows
up as a new ``kernel_id`` there, which is the invariant that matters for
reproducibility.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import logging
import platform
import queue
import re
import shutil
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from tools.ansi_strip import strip_ansi

logger = logging.getLogger(__name__)

READY_TIMEOUT_S = 30.0
DEFAULT_CELL_TIMEOUT_S = 60.0
MAX_STREAM_CHARS = 200_000


def kernels_installed() -> bool:
    """True when the jupyter kernel stack is importable right now."""
    try:
        import ipykernel  # noqa: F401
        import jupyter_client  # noqa: F401
        return True
    except Exception:
        return False


def kernels_available() -> bool:
    """The ``run_code`` / ``reproduce_artifact`` check_fn gate.

    Deliberately broader than :func:`kernels_installed`. The science surface
    is on by default (see ``_OPENCODON_CORE_TOOLS``), so an install that
    merely *lacks* the kernel stack must not permanently hide the agent's
    headline tools — it just needs to fetch them on first use. When the deps
    are missing but lazy installs are permitted, the tools stay in the schema
    and :func:`ensure_kernels` does the install at kernel-start time.

    Never installs anything itself: this runs during schema assembly, on
    every tool-definition build, in non-interactive contexts (gateway, cron).
    A pip invocation there would be a latency and correctness hazard.
    """
    if kernels_installed():
        return True
    try:
        from tools.lazy_deps import _allow_lazy_installs

        return _allow_lazy_installs()
    except Exception:
        return False


def ensure_kernels() -> None:
    """Install the jupyter kernel stack if missing. Raises on failure.

    Called from the kernel-start path — i.e. only when a cell is actually
    being run, never during schema assembly. ``prompt=False`` because this
    can be reached from the gateway/cron with no TTY to answer.
    """
    if kernels_installed():
        return
    from tools.lazy_deps import ensure

    ensure("tool.science", prompt=False)


# ── Environment resolution ──────────────────────────────────────────


@dataclass(frozen=True)
class EnvironmentSpec:
    """A resolved kernel launch environment and its observed identity."""

    language: str
    interpreter_path: str
    argv: Tuple[str, ...]
    runtime_identity: str
    env_name: Optional[str] = None
    env_overrides: Dict[str, str] = field(default_factory=dict)


def _installed_distributions() -> List[str]:
    try:
        from importlib import metadata

        return sorted(
            f"{d.metadata['Name']}=={d.version}"
            for d in metadata.distributions()
            if d.metadata and d.metadata.get("Name")
        )
    except Exception:
        return []


def python_env_snapshot() -> str:
    """Observed runtime snapshot (pip-freeze-style) for execution_log rows.

    This is an *observation* of the realized environment, not a recreate
    recipe — reproduce() grades accordingly (best-effort, never verified,
    unless a lockfile identity is introduced later).
    """
    return json.dumps(
        {
            "language": "python",
            "python_version": platform.python_version(),
            "platform": platform.platform(),
            "distributions": _installed_distributions(),
        },
        sort_keys=True,
    )


def env_snapshot_hash(snapshot: Optional[str]) -> Optional[str]:
    if not snapshot:
        return None
    return hashlib.sha256(snapshot.encode("utf-8")).hexdigest()


class PythonEnvResolver:
    """Bind the running opencodon interpreter as the local python kernel."""

    def __init__(self, interpreter_path: str = None):
        self._interpreter = interpreter_path or sys.executable

    def available(self) -> bool:
        # Resolver availability means "usable now", so it tracks
        # kernels_installed, not the schema-gate variant that also returns
        # True when the stack is merely installable.
        return kernels_installed()

    def resolve(self) -> EnvironmentSpec:
        return EnvironmentSpec(
            language="python",
            interpreter_path=self._interpreter,
            argv=(
                self._interpreter,
                "-m",
                "ipykernel_launcher",
                "-f",
                "{connection_file}",
            ),
            runtime_identity=(
                f"cpython-{platform.python_version()}-{platform.machine()}"
                f"-{platform.system().lower()}"
            ),
        )

    def snapshot(self) -> str:
        return python_env_snapshot()


class MicromambaEnvResolver:
    """Bind a durable micromamba environment as the kernel for a session.

    Unlike :class:`PythonEnvResolver`, whose snapshot is an *observation* of
    whatever happened to be installed, this one's snapshot carries a lockfile
    identity — which is what lets reproduce() grade a replay as verified
    rather than merely byte-identical.
    """

    def __init__(self, env_name: str):
        self.env_name = env_name

    def available(self) -> bool:
        try:
            from science import envmanager

            return kernels_installed() and envmanager.exists(self.env_name)
        except Exception:
            return False

    def resolve(self) -> EnvironmentSpec:
        from science import envmanager

        if not envmanager.exists(self.env_name):
            raise RuntimeError(
                f"environment {self.env_name!r} does not exist; create it first"
            )
        interpreter = envmanager.env_prefix(self.env_name) / "bin" / "python"
        return EnvironmentSpec(
            language="python",
            interpreter_path=str(interpreter),
            argv=(str(interpreter), "-m", "ipykernel_launcher", "-f", "{connection_file}"),
            runtime_identity=f"micromamba:{self.env_name}",
            env_name=self.env_name,
        )

    def snapshot(self) -> str:
        from science import envmanager

        return envmanager.env_snapshot(self.env_name)


class RKernelResolver:
    """Bind a system R + IRkernel as the R kernel (cross-language via files).

    R and Python share the workspace and the artifact store — never memory.
    Availability requires an ``R`` binary with the IRkernel package installed.
    """

    def __init__(self, r_path: str = None):
        self._r = r_path or shutil.which("R")

    def available(self) -> bool:
        if not self._r:
            return False
        import subprocess

        try:
            probe = subprocess.run(
                [self._r, "--slave", "-e",
                 "if (!requireNamespace('IRkernel', quietly=TRUE)) quit(status=1)"],
                capture_output=True,
                timeout=20,
            )
            return probe.returncode == 0
        except Exception:
            return False

    def resolve(self) -> EnvironmentSpec:
        if not self._r:
            raise RuntimeError("no R interpreter found on PATH")
        return EnvironmentSpec(
            language="r",
            interpreter_path=self._r,
            argv=(
                self._r,
                "--slave",
                "-e",
                "IRkernel::main()",
                "--args",
                "{connection_file}",
            ),
            runtime_identity=f"R-irkernel-{platform.machine()}",
        )

    def snapshot(self) -> str:
        # The R session snapshot is captured in-kernel at bootstrap
        # (sessionInfo()); host-side we record the interpreter identity only.
        return json.dumps(
            {"language": "r", "interpreter": self._r,
             "machine": platform.machine()},
            sort_keys=True,
        )


# ── Kernel session (ported from donor kernel_client.py) ─────────────


@dataclass
class ExecutionOutputs:
    """Captured protocol evidence for one execute request."""

    status: str  # "ok" | "error" | "timeout" | "aborted"
    execution_count: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    display: List[Dict[str, Any]] = field(default_factory=list)
    results: List[Dict[str, Any]] = field(default_factory=list)
    error_name: str = ""
    error_value: str = ""
    traceback: Tuple[str, ...] = ()
    timed_out: bool = False

    @property
    def is_error(self) -> bool:
        return self.status in ("error", "timeout", "aborted")


class KernelStartError(RuntimeError):
    """The kernel process failed to start or become ready."""


@dataclass
class ProvisionedKernel:
    """A started kernel and the client already connected to it.

    ``location`` names where it runs — ``local``, ``ssh:<host>``,
    ``modal:<app>`` — and is recorded per cell. It is the difference between
    "this number was computed" and "this number was computed on a GPU", which
    a reader of the provenance record cannot otherwise recover.
    """

    manager: Any
    client: Any
    location: str
    # Where the workspace lives *from the kernel's point of view*. Local
    # kernels share the host path; a remote one has its own copy that the
    # provisioner syncs, and the injected SDK must be told about it or every
    # save_artifact() writes into a directory nobody reads.
    remote_workspace: Optional[str] = None
    # Backend-owned state (sandbox handle, forwarders) — opaque here, handed
    # back to the provisioner for liveness, sync and teardown.
    handle: Any = None


class KernelProvisioner:
    """Starts a kernel somewhere and hands back a connected client.

    The seam that lets a kernel live off this machine. Everything above it —
    msg_id correlation, taint-and-restart, the execution_log and lineage
    writes — is transport-agnostic already, so a provisioner is the whole of
    what a remote backend has to supply.

    Implementations must either return a live :class:`ProvisionedKernel` or
    raise :class:`KernelStartError`; a half-started kernel must be cleaned up
    before raising, since the manager will treat the failure as a taint and
    immediately try again.
    """

    name = "abstract"

    def provision(self, spec: EnvironmentSpec, workdir: Path) -> ProvisionedKernel:
        raise NotImplementedError

    def describe_target(self) -> str:
        """Human-readable target, used in errors and in kernel_location."""
        return self.name

    def is_alive(self, provisioned: ProvisionedKernel) -> bool:
        """Whether the kernel is still usable.

        Not delegated to jupyter_client, because its liveness check reads the
        heartbeat channel — and a heartbeat does not survive every transport.
        A remote kernel that answers ``kernel_info`` perfectly can report
        ``hb_channel.is_beating() == False``, which would make the manager tear
        down and restart a healthy kernel on every cell, destroying exactly the
        session state persistent kernels exist to keep.
        """
        return bool(provisioned.manager and provisioned.manager.is_alive())

    def shutdown(self, provisioned: ProvisionedKernel) -> None:
        try:
            provisioned.manager.shutdown_kernel(now=True)
        except Exception:
            pass

    def sync_in(self, provisioned: ProvisionedKernel, workdir: Path) -> None:
        """Push host workspace state to the kernel before a cell runs.

        A no-op when the kernel shares the host filesystem.
        """

    def sync_out(self, provisioned: ProvisionedKernel, workdir: Path) -> None:
        """Pull kernel-side workspace changes back after a cell runs."""


class LocalProvisioner(KernelProvisioner):
    """Start the kernel as a child process on this machine.

    The default, and the only one that needs no configuration.
    """

    name = "local"

    def provision(self, spec: EnvironmentSpec, workdir: Path) -> ProvisionedKernel:
        import os

        # First actual use — fetch the kernel stack if this install doesn't
        # carry it (lean install, broken [all] resolve). Raises
        # FeatureUnavailable with a remediation hint if that's not possible.
        ensure_kernels()

        from jupyter_client.kernelspec import KernelSpec
        from jupyter_client.manager import KernelManager

        workdir.mkdir(parents=True, exist_ok=True)
        km = KernelManager(kernel_name="python3")
        km._kernel_spec = KernelSpec(
            argv=list(spec.argv),
            display_name="opencodon-science-kernel",
            language=spec.language,
        )
        try:
            if spec.env_overrides:
                km.start_kernel(
                    cwd=str(workdir), env={**os.environ, **spec.env_overrides}
                )
            else:
                km.start_kernel(cwd=str(workdir))
            kc = km.client()
            kc.start_channels()
            kc.wait_for_ready(timeout=READY_TIMEOUT_S)
        except Exception as exc:
            try:
                km.shutdown_kernel(now=True)
            except Exception:
                pass
            raise KernelStartError(
                f"kernel failed to start on {self.describe_target()}: {exc}"
            ) from exc
        return ProvisionedKernel(manager=km, client=kc, location=self.name)

    # sync_in/sync_out stay no-ops: the kernel *is* on this filesystem, so
    # there is nothing to copy — the workspace it writes is the one we read.


class KernelSession:
    """One live kernel bound to a workspace directory."""

    def __init__(
        self,
        spec: EnvironmentSpec,
        *,
        workdir: Path,
        provisioner: Optional[KernelProvisioner] = None,
    ):
        self._spec = spec
        self._workdir = Path(workdir)
        self._provisioner = provisioner or LocalProvisioner()
        self._provisioned: Optional[ProvisionedKernel] = None
        self._km: Any = None
        self._kc: Any = None
        self.location = self._provisioner.describe_target()
        self.kernel_id = f"krn-{uuid.uuid4().hex[:16]}"

    @property
    def workdir(self) -> Path:
        return self._workdir

    @property
    def spec(self) -> EnvironmentSpec:
        return self._spec

    def start(self) -> None:
        provisioned = self._provisioner.provision(self._spec, self._workdir)
        self._provisioned = provisioned
        self._km = provisioned.manager
        self._kc = provisioned.client
        self.location = provisioned.location

    @property
    def kernel_workspace(self) -> str:
        """Workspace path as the *kernel* sees it — remote copy or host path."""
        if self._provisioned and self._provisioned.remote_workspace:
            return self._provisioned.remote_workspace
        return str(self._workdir)

    def is_alive(self) -> bool:
        if self._provisioned is None:
            return False
        return self._provisioner.is_alive(self._provisioned)

    def execute(self, source: str, *, timeout: float) -> ExecutionOutputs:
        """Run one cell, syncing the workspace around it.

        The sync is bracketed in ``finally`` so a cell that times out or dies
        still has its partial writes pulled back — a failed cell's artifacts
        are evidence too, and a timeout is where you most want to see what the
        kernel had managed to produce.
        """
        if self._kc is None:
            raise RuntimeError("kernel session is not started")
        # Push host-side cell setup (cell.json, materialized inputs) to the
        # kernel before it looks for them; pull its writes back afterwards.
        # Both are no-ops when the kernel shares this filesystem.
        self._provisioner.sync_in(self._provisioned, self._workdir)
        try:
            return self._execute(source, timeout=timeout)
        finally:
            self._provisioner.sync_out(self._provisioned, self._workdir)

    def _execute(self, source: str, *, timeout: float) -> ExecutionOutputs:
        kc = self._kc
        # Drain any stale iopub so a prior cell's late traffic is not
        # attributed to this request (msg_id filtering below is the real
        # guard).
        _drain(kc)
        msg_id = kc.execute(source, allow_stdin=False, store_history=True)
        out = ExecutionOutputs(status="ok")
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self._on_timeout(msg_id, out)
            try:
                msg = kc.get_iopub_msg(timeout=min(remaining, 1.0))
            except queue.Empty:
                if not self.is_alive():
                    out.status = "error"
                    out.error_name = out.error_name or "KernelDied"
                    out.error_value = out.error_value or "kernel process exited"
                    return out
                continue
            if msg.get("parent_header", {}).get("msg_id") != msg_id:
                continue
            if self._absorb(msg, out):
                break
        self._reconcile_shell_reply(msg_id, out, deadline)
        return out

    def _absorb(self, msg: dict, out: ExecutionOutputs) -> bool:
        """Fold one iopub message; return True when the request goes idle."""
        msg_type = msg["msg_type"]
        content = msg["content"]
        if msg_type == "stream":
            text = content.get("text", "")
            if content.get("name") == "stderr":
                out.stderr = _bounded(out.stderr + text)
            else:
                out.stdout = _bounded(out.stdout + text)
        elif msg_type in ("display_data", "update_display_data"):
            out.display.append(
                {"data": content.get("data", {}),
                 "metadata": content.get("metadata", {})}
            )
        elif msg_type == "execute_result":
            out.execution_count = content.get("execution_count")
            out.results.append(
                {"data": content.get("data", {}),
                 "execution_count": content.get("execution_count")}
            )
        elif msg_type == "error":
            out.status = "error"
            out.error_name = content.get("ename", "")
            out.error_value = content.get("evalue", "")
            out.traceback = tuple(content.get("traceback", ()))
        elif msg_type == "status" and content.get("execution_state") == "idle":
            return True
        return False

    def _reconcile_shell_reply(self, msg_id, out, deadline) -> None:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            try:
                reply = self._kc.get_shell_msg(timeout=min(remaining, 1.0))
            except queue.Empty:
                return
            if reply.get("parent_header", {}).get("msg_id") != msg_id:
                continue
            content = reply["content"]
            if content.get("execution_count") is not None:
                out.execution_count = content["execution_count"]
            status = content.get("status")
            if status == "error" and out.status == "ok":
                out.status = "error"
                out.error_name = content.get("ename", out.error_name)
                out.error_value = content.get("evalue", out.error_value)
                out.traceback = tuple(content.get("traceback", out.traceback))
            elif status == "abort":
                out.status = "aborted"
            return

    def _on_timeout(self, msg_id, out: ExecutionOutputs) -> ExecutionOutputs:
        out.status = "timeout"
        out.timed_out = True
        out.error_name = out.error_name or "Timeout"
        out.error_value = out.error_value or "cell exceeded its wall-clock budget"
        self.interrupt()
        # Briefly gather terminal evidence produced by the interrupt.
        grace = time.monotonic() + 2.0
        while time.monotonic() < grace:
            try:
                msg = self._kc.get_iopub_msg(timeout=0.3)
            except queue.Empty:
                continue
            if msg.get("parent_header", {}).get("msg_id") != msg_id:
                continue
            if msg["msg_type"] == "error":
                out.error_name = msg["content"].get("ename", out.error_name)
                out.error_value = msg["content"].get("evalue", out.error_value)
                out.traceback = tuple(
                    msg["content"].get("traceback", out.traceback)
                )
            if (
                msg["msg_type"] == "status"
                and msg["content"].get("execution_state") == "idle"
            ):
                break
        return out

    def interrupt(self) -> None:
        if self._km is not None:
            try:
                self._km.interrupt_kernel()
            except Exception:
                pass

    def shutdown(self) -> None:
        if self._kc is not None:
            try:
                self._kc.stop_channels()
            except Exception:
                pass
            self._kc = None
        if self._provisioned is not None:
            self._provisioner.shutdown(self._provisioned)
            self._provisioned = None
        self._km = None


def _drain(kc) -> None:
    while True:
        try:
            kc.get_iopub_msg(timeout=0)
        except queue.Empty:
            return
        except Exception:
            return


def _bounded(text: str) -> str:
    if len(text) <= MAX_STREAM_CHARS:
        return text
    return text[:MAX_STREAM_CHARS] + "\n…[stream truncated]"


# Frames that belong to the *submitted cell* rather than to library code.
# IPython has named cells three ways across versions, so all three are
# matched; anything else in the traceback is a library frame and ignored.
_CELL_FRAME_RE = re.compile(
    r"Cell In\[\d+\],\s*line\s+(\d+)"                                  # IPython 8+
    r"|File\s+\"?<ipython-input-[^>\"]*>\"?,\s*line\s+(\d+)"           # legacy
    r"|File\s+\"?[^\"\n]*ipykernel_\d+[/\\][^\"\n]*\.py\"?,\s*line\s+(\d+)"  # temp-file cells
)


def error_lineno_from_traceback(traceback) -> Optional[int]:
    """1-based line number *within the submitted cell* of a failure.

    The Jupyter ``error`` message carries the traceback as pre-rendered,
    usually ANSI-coloured frames rather than structured data, so the line
    number has to be read back out of the text. Only cell frames are
    considered — a ``ValueError`` raised three frames deep inside pandas
    should report where *the cell* entered pandas, not a line in pandas.

    Returns the last cell frame's line (the innermost point still inside the
    cell), or ``None`` when the traceback names no cell frame — which is the
    honest answer for a kernel death or an abort, where no cell line failed.
    """
    if not traceback:
        return None
    lineno = None
    for frame in traceback:
        for match in _CELL_FRAME_RE.finditer(strip_ansi(str(frame))):
            captured = next((g for g in match.groups() if g), None)
            if captured is not None:
                try:
                    lineno = int(captured)
                except ValueError:
                    continue
    return lineno


def traceback_text(traceback) -> Optional[str]:
    """The traceback as stripped, bounded plain text for ``execution_log``.

    ANSI is removed because these frames are persisted and may later be
    replayed into a terminal UI; the same cap as stdout/stderr applies so a
    pathological recursion traceback cannot bloat the row.
    """
    if not traceback:
        return None
    joined = "\n".join(strip_ansi(str(frame)) for frame in traceback)
    return _bounded(joined) or None


# ── Session kernel manager ──────────────────────────────────────────


@dataclass
class CellRun:
    """Result of one cell submission, before execution_log is written."""

    outputs: ExecutionOutputs
    kernel_id: str
    language: str
    workspace: Path
    fresh_kernel: bool
    location: str = "local"
    tainted: bool = False
    taint_reasons: Tuple[str, ...] = ()


class SessionKernelManager:
    """At most one live kernel per (session_id, language); taint → restart."""

    def __init__(
        self,
        *,
        workspaces_root: Path,
        resolvers: Dict[str, Any] = None,
        bootstrap_fn=None,
        session_factory=None,
        provisioner: Optional[KernelProvisioner] = None,
    ):
        self._root = Path(workspaces_root)
        # Seam for tests/embedders: anything with the KernelSession protocol
        # (start/execute/is_alive/interrupt/shutdown, .kernel_id, .spec).
        self._session_factory = session_factory or KernelSession
        # Where kernels run. Local unless an embedder supplies otherwise; the
        # choice is per-manager rather than per-cell, since a session's kernel
        # holds state that cannot migrate mid-conversation.
        self._provisioner = provisioner or LocalProvisioner()
        self._resolvers: Dict[str, Any] = {
            "python": PythonEnvResolver(),
            "r": RKernelResolver(),
        }
        if resolvers:
            self._resolvers.update(resolvers)
        # bootstrap_fn(session, workspace, language) runs right after a kernel
        # starts — the artifact/host SDK injection hook (science/bridge.py).
        self._bootstrap_fn = bootstrap_fn
        self._env_resolvers: Dict[str, Any] = {}
        self._live: Dict[Tuple[str, str, Optional[str]], KernelSession] = {}
        self._locks: Dict[Tuple[str, str], threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def workspace_for(self, session_id: str) -> Path:
        return self._root / _safe_dirname(session_id)

    def resolver_for(self, language: str, env: Optional[str] = None):
        """The resolver for a language, or for a named durable environment.

        Environment resolvers are built on demand rather than registered: an
        env is created at runtime and there is no point requiring a manager
        rebuild to use one.
        """
        if env:
            cached = self._env_resolvers.get(env)
            if cached is None:
                cached = MicromambaEnvResolver(env)
                self._env_resolvers[env] = cached
            return cached
        resolver = self._resolvers.get(language)
        if resolver is None:
            raise ValueError(
                f"no kernel resolver for language {language!r}; "
                f"available: {sorted(self._resolvers)}"
            )
        return resolver

    def languages_available(self) -> List[str]:
        return [
            lang for lang, r in self._resolvers.items()
            if getattr(r, "available", lambda: True)()
        ]

    def _lock(self, key: Tuple[str, str]) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(key, threading.Lock())

    def ensure_kernel(
        self, session_id: str, language: str = "python", env: Optional[str] = None
    ) -> Tuple[KernelSession, bool]:
        """Start (or reuse) the kernel for a key; returns (session, fresh).

        Lets callers learn the kernel identity *before* submitting a cell,
        so the execution_log row can be inserted ahead of execution.

        A named *env* gets its own kernel: two environments are two different
        interpreters with different packages, and sharing state between them
        would be neither possible nor meaningful.
        """
        key = (session_id, language, env)
        with self._lock(key):
            return self._ensure_kernel(session_id, language, env)

    def run_cell(
        self,
        session_id: str,
        source: str,
        *,
        language: str = "python",
        timeout: float = DEFAULT_CELL_TIMEOUT_S,
        env: Optional[str] = None,
    ) -> CellRun:
        key = (session_id, language, env)
        with self._lock(key):
            session, fresh = self._ensure_kernel(session_id, language, env)
            outputs = session.execute(source, timeout=timeout)

            tainted = False
            reasons: Tuple[str, ...] = ()
            if outputs.timed_out:
                tainted = True
                reasons = ("cell timed out; interruption is not a rollback",)
            elif not session.is_alive():
                tainted = True
                reasons = ("kernel process died mid-session",)
            if tainted:
                session.shutdown()
                self._live.pop(key, None)

            return CellRun(
                outputs=outputs,
                kernel_id=session.kernel_id,
                language=language,
                workspace=session.workdir,
                fresh_kernel=fresh,
                location=getattr(session, "location", "local"),
                tainted=tainted,
                taint_reasons=reasons,
            )

    def _ensure_kernel(self, session_id, language, env=None) -> Tuple[KernelSession, bool]:
        key = (session_id, language, env)
        live = self._live.get(key)
        if live is not None and live.is_alive():
            return live, False
        if live is not None:
            live.shutdown()
            self._live.pop(key, None)
        spec = self.resolver_for(language, env).resolve()
        workspace = self.workspace_for(session_id)
        session = self._session_factory(
            spec, workdir=workspace, provisioner=self._provisioner
        )
        session.start()
        if self._bootstrap_fn is not None:
            try:
                self._bootstrap_fn(session, workspace, language)
            except Exception:
                session.shutdown()
                raise
        self._live[key] = session
        return session, True

    def interrupt(
        self, session_id: str, *, language: str = "python", env: Optional[str] = None
    ) -> bool:
        key = (session_id, language, env)
        live = self._live.get(key)
        if live is None:
            return False
        live.interrupt()
        live.shutdown()
        self._live.pop(key, None)
        return True

    def kernel_for(
        self, session_id, *, language="python", env: Optional[str] = None
    ) -> Optional[KernelSession]:
        return self._live.get((session_id, language, env))

    def list_live(self) -> List[Dict[str, Any]]:
        """Describe the live kernels, for the UI's compute pane.

        Liveness is probed per kernel rather than trusted from the map: a
        kernel process can die without anything having called back in, and
        reporting a dead namespace as live is worse than reporting nothing —
        the whole point of showing kernel state is telling the reader whether
        the namespace that produced their artifacts still exists.

        Read-only: dead entries are reported as such, not reaped. Eviction
        belongs to the execute path, which holds the per-key lock.
        """
        described: List[Dict[str, Any]] = []
        for (session_id, language, env), session in list(self._live.items()):
            try:
                alive = session.is_alive()
            except Exception:
                alive = False
            described.append(
                {
                    "kernel_id": getattr(session, "kernel_id", None),
                    "session_id": session_id,
                    "language": language,
                    "env_name": env or getattr(session.spec, "env_name", None),
                    "runtime_identity": getattr(session.spec, "runtime_identity", None),
                    "location": getattr(session, "location", "local"),
                    "workspace": str(getattr(session, "workdir", "")),
                    "alive": alive,
                }
            )
        return described

    def close_session(self, session_id: str) -> None:
        for key in [k for k in list(self._live) if k[0] == session_id]:
            self._live.pop(key).shutdown()

    def shutdown(self) -> None:
        for key in list(self._live):
            self._live.pop(key).shutdown()


def _safe_dirname(session_id: str) -> str:
    keep = "".join(c if c.isalnum() or c in "._-" else "_" for c in session_id)
    return keep or "session"


# ── Process-wide manager singleton (mirrors other tool singletons) ──

_manager: Optional[SessionKernelManager] = None
_manager_lock = threading.Lock()


def get_kernel_manager() -> SessionKernelManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            from opencodon_constants import get_opencodon_home

            from science.bridge import bootstrap_kernel

            _manager = SessionKernelManager(
                workspaces_root=Path(get_opencodon_home()) / "science" / "workspaces",
                bootstrap_fn=bootstrap_kernel,
            )
            atexit.register(_manager.shutdown)
        return _manager


def reset_kernel_manager() -> None:
    """Test hook: drop the singleton (shutting down any live kernels)."""
    global _manager
    with _manager_lock:
        if _manager is not None:
            _manager.shutdown()
        _manager = None
