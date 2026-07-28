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
- Kernels are local-only for now: remote kernel backends wait for a
  deliberate extension of tools/environments/.

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
import shutil
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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


class KernelSession:
    """One live kernel process bound to a workspace directory."""

    def __init__(self, spec: EnvironmentSpec, *, workdir: Path):
        self._spec = spec
        self._workdir = Path(workdir)
        self._km: Any = None
        self._kc: Any = None
        self.kernel_id = f"krn-{uuid.uuid4().hex[:16]}"

    @property
    def workdir(self) -> Path:
        return self._workdir

    @property
    def spec(self) -> EnvironmentSpec:
        return self._spec

    def start(self) -> None:
        import os

        # First actual use — fetch the kernel stack if this install doesn't
        # carry it (lean install, broken [all] resolve). Raises
        # FeatureUnavailable with a remediation hint if that's not possible.
        ensure_kernels()

        from jupyter_client.kernelspec import KernelSpec
        from jupyter_client.manager import KernelManager

        self._workdir.mkdir(parents=True, exist_ok=True)
        km = KernelManager(kernel_name="python3")
        km._kernel_spec = KernelSpec(
            argv=list(self._spec.argv),
            display_name="opencodon-science-kernel",
            language=self._spec.language,
        )
        try:
            if self._spec.env_overrides:
                km.start_kernel(
                    cwd=str(self._workdir),
                    env={**os.environ, **self._spec.env_overrides},
                )
            else:
                km.start_kernel(cwd=str(self._workdir))
            kc = km.client()
            kc.start_channels()
            kc.wait_for_ready(timeout=READY_TIMEOUT_S)
        except Exception as exc:
            try:
                km.shutdown_kernel(now=True)
            except Exception:
                pass
            raise KernelStartError(f"kernel failed to start: {exc}") from exc
        self._km = km
        self._kc = kc

    def is_alive(self) -> bool:
        return self._km is not None and self._km.is_alive()

    def execute(self, source: str, *, timeout: float) -> ExecutionOutputs:
        if self._kc is None:
            raise RuntimeError("kernel session is not started")
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
        if self._km is not None:
            try:
                self._km.shutdown_kernel(now=True)
            except Exception:
                pass
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


# ── Session kernel manager ──────────────────────────────────────────


@dataclass
class CellRun:
    """Result of one cell submission, before execution_log is written."""

    outputs: ExecutionOutputs
    kernel_id: str
    language: str
    workspace: Path
    fresh_kernel: bool
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
    ):
        self._root = Path(workspaces_root)
        # Seam for tests/embedders: anything with the KernelSession protocol
        # (start/execute/is_alive/interrupt/shutdown, .kernel_id, .spec).
        self._session_factory = session_factory or KernelSession
        self._resolvers: Dict[str, Any] = {
            "python": PythonEnvResolver(),
            "r": RKernelResolver(),
        }
        if resolvers:
            self._resolvers.update(resolvers)
        # bootstrap_fn(session, workspace, language) runs right after a kernel
        # starts — the artifact/host SDK injection hook (science/bridge.py).
        self._bootstrap_fn = bootstrap_fn
        self._live: Dict[Tuple[str, str], KernelSession] = {}
        self._locks: Dict[Tuple[str, str], threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def workspace_for(self, session_id: str) -> Path:
        return self._root / _safe_dirname(session_id)

    def resolver_for(self, language: str):
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
        self, session_id: str, language: str = "python"
    ) -> Tuple[KernelSession, bool]:
        """Start (or reuse) the kernel for a key; returns (session, fresh).

        Lets callers learn the kernel identity *before* submitting a cell,
        so the execution_log row can be inserted ahead of execution.
        """
        key = (session_id, language)
        with self._lock(key):
            return self._ensure_kernel(session_id, language)

    def run_cell(
        self,
        session_id: str,
        source: str,
        *,
        language: str = "python",
        timeout: float = DEFAULT_CELL_TIMEOUT_S,
    ) -> CellRun:
        key = (session_id, language)
        with self._lock(key):
            session, fresh = self._ensure_kernel(session_id, language)
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
                tainted=tainted,
                taint_reasons=reasons,
            )

    def _ensure_kernel(self, session_id, language) -> Tuple[KernelSession, bool]:
        key = (session_id, language)
        live = self._live.get(key)
        if live is not None and live.is_alive():
            return live, False
        if live is not None:
            live.shutdown()
            self._live.pop(key, None)
        spec = self.resolver_for(language).resolve()
        workspace = self.workspace_for(session_id)
        session = self._session_factory(spec, workdir=workspace)
        session.start()
        if self._bootstrap_fn is not None:
            try:
                self._bootstrap_fn(session, workspace, language)
            except Exception:
                session.shutdown()
                raise
        self._live[key] = session
        return session, True

    def interrupt(self, session_id: str, *, language: str = "python") -> bool:
        key = (session_id, language)
        live = self._live.get(key)
        if live is None:
            return False
        live.interrupt()
        live.shutdown()
        self._live.pop(key, None)
        return True

    def kernel_for(self, session_id, *, language="python") -> Optional[KernelSession]:
        return self._live.get((session_id, language))

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
