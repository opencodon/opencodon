"""Run a science kernel inside a Modal sandbox.

The kernel is a real ipykernel process in a Modal container; we reach its five
ZMQ channels through TLS tunnels and a local forwarder, so everything above the
provisioner seam — msg_id correlation, taint/restart, execution_log, lineage —
works unchanged against a remote kernel.

Three things this backend has to do that the local one does not:

1. **Its own liveness.** ``jupyter_client``'s check reads the heartbeat
   channel, and the heartbeat does not survive the tunnel: a kernel that
   answers ``kernel_info`` correctly still reports ``is_beating() == False``.
   Delegating to it would make the manager tear down and restart a healthy
   remote kernel on every cell.
2. **Its own workspace.** The container cannot see the host filesystem, so the
   workspace is mirrored in and out around each cell.
3. **Its own bootstrap path.** The injected SDK bakes in a workspace path, and
   that path has to be the container's.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from science.kernels import (
    KernelProvisioner,
    KernelStartError,
    ProvisionedKernel,
    READY_TIMEOUT_S,
)
from science.provisioners.forwarding import ForwarderSet
from science.provisioners.remote import (
    SKIP_DIRS as _SKIP_DIRS,
    await_answering,
    localise,
    walk_dirs as _walk_dirs,
    walk_files as _walk,
)

logger = logging.getLogger(__name__)

# Container-side ports for the five ZMQ channels. Fixed rather than random:
# each sandbox is its own network namespace, so there is nothing to collide
# with, and the ports must be declared to Modal before the kernel picks them.
CHANNEL_PORTS = {
    "shell_port": 51001,
    "iopub_port": 51002,
    "stdin_port": 51003,
    "control_port": 51004,
    "hb_port": 51005,
}

REMOTE_WORKSPACE = "/workspace"
CONNECTION_FILE = "/tmp/opencodon-kernel.json"
DEFAULT_IMAGE_PACKAGES = ("ipykernel==6.30.1",)
DEFAULT_SANDBOX_TIMEOUT_S = 3600
KERNEL_LISTEN_TIMEOUT_S = 60.0


@dataclass
class _ModalHandle:
    """Backend state for one provisioned kernel."""

    sandbox: Any
    process: Any
    forwarders: ForwarderSet
    key: str
    app_name: str
    synced_out: Dict[str, float] = field(default_factory=dict)


class ModalProvisioner(KernelProvisioner):
    """Provision the session kernel as an ipykernel inside a Modal sandbox."""

    name = "modal"

    def __init__(
        self,
        *,
        app_name: str = "opencodon-science",
        gpu: Optional[str] = None,
        image_packages: tuple = DEFAULT_IMAGE_PACKAGES,
        timeout: int = DEFAULT_SANDBOX_TIMEOUT_S,
        cpu: Optional[float] = None,
        memory: Optional[int] = None,
    ):
        self.app_name = app_name
        self.gpu = gpu
        self.image_packages = tuple(image_packages)
        self.timeout = timeout
        self.cpu = cpu
        self.memory = memory

    def describe_target(self) -> str:
        # The GPU is part of the identity: "this ran on modal" and "this ran
        # on an A100" are different provenance claims.
        suffix = f"/{self.gpu}" if self.gpu else ""
        return f"{self.name}:{self.app_name}{suffix}"

    # ── provisioning ────────────────────────────────────────────────

    def provision(self, spec, workdir: Path) -> ProvisionedKernel:
        # Arguments before dependencies: an R spec is a malformed call whether
        # or not the SDK happens to be installed, and reporting the missing
        # SDK first would send the caller to fix the wrong thing.
        if spec.language != "python":
            raise KernelStartError(
                f"{self.describe_target()} provisions python kernels only, "
                f"got {spec.language!r}"
            )

        try:
            import modal
        except ImportError as exc:
            raise KernelStartError(
                "the modal SDK is not installed — `uv sync --extra modal`"
            ) from exc

        key = secrets.token_hex(16)
        connection = {
            "transport": "tcp",
            "ip": "0.0.0.0",
            "key": key,
            "signature_scheme": "hmac-sha256",
            "kernel_name": "python3",
            **CHANNEL_PORTS,
        }

        sandbox = process = None
        forwarders = ForwarderSet()
        try:
            app = modal.App.lookup(self.app_name, create_if_missing=True)
            image = modal.Image.debian_slim().pip_install(*self.image_packages)
            sandbox = modal.Sandbox.create(
                app=app,
                image=image,
                timeout=self.timeout,
                gpu=self.gpu,
                cpu=self.cpu,
                memory=self.memory,
                encrypted_ports=list(CHANNEL_PORTS.values()),
                workdir=REMOTE_WORKSPACE,
            )
            sandbox.mkdir(REMOTE_WORKSPACE, parents=True)

            # Written via python rather than `echo`: shell quoting mangles the
            # JSON, and a malformed connection file fails later and opaquely,
            # as a kernel that starts but never answers.
            writer = sandbox.exec(
                "python3", "-c",
                "import sys; open(sys.argv[1],'w').write(sys.argv[2])",
                CONNECTION_FILE, json.dumps(connection),
            )
            if writer.wait() != 0:
                raise KernelStartError("could not write the kernel connection file")

            process = sandbox.exec(
                "python3", "-m", "ipykernel_launcher", "-f", CONNECTION_FILE
            )
            self._await_listening(sandbox)

            tunnels = sandbox.tunnels()
            local_ports = {
                channel: forwarders.add(tunnels[port].tls_socket)
                for channel, port in CHANNEL_PORTS.items()
            }
            client = self._connect(key, local_ports)
        except KernelStartError:
            self._cleanup(sandbox, process, forwarders)
            raise
        except Exception as exc:
            self._cleanup(sandbox, process, forwarders)
            raise KernelStartError(
                f"kernel failed to start on {self.describe_target()}: {exc}"
            ) from exc

        return ProvisionedKernel(
            manager=None,  # there is no local process to manage
            client=client,
            location=self.describe_target(),
            remote_workspace=REMOTE_WORKSPACE,
            handle=_ModalHandle(
                sandbox=sandbox, process=process, forwarders=forwarders,
                key=key, app_name=self.app_name,
            ),
        )

    def _await_listening(self, sandbox) -> None:
        """Block until the kernel has bound all five ports in the container."""
        probe = (
            "import socket,sys\n"
            f"ports={list(CHANNEL_PORTS.values())}\n"
            "for p in ports:\n"
            "    s=socket.socket(); s.settimeout(1)\n"
            "    try: s.connect(('127.0.0.1',p))\n"
            "    except Exception: sys.exit(1)\n"
            "    finally: s.close()\n"
        )
        deadline = time.monotonic() + KERNEL_LISTEN_TIMEOUT_S
        while time.monotonic() < deadline:
            if sandbox.exec("python3", "-c", probe).wait() == 0:
                return
            time.sleep(1.0)
        raise KernelStartError(
            f"kernel did not bind its channels on {self.describe_target()} "
            f"within {KERNEL_LISTEN_TIMEOUT_S:.0f}s"
        )

    def _connect(self, key: str, local_ports: Dict[str, int]):
        """Open a client and confirm the kernel answers.

        Deliberately not ``wait_for_ready()``: that infers liveness from the
        heartbeat, which does not survive the tunnel, so it reports a working
        kernel as dead. A ``kernel_info`` round-trip is the real check.
        """
        from jupyter_client import BlockingKernelClient

        client = BlockingKernelClient()
        client.load_connection_info({
            "transport": "tcp",
            "ip": "127.0.0.1",
            "key": key,
            "signature_scheme": "hmac-sha256",
            **local_ports,
        })
        client.start_channels()
        try:
            await_answering(
                client, timeout=READY_TIMEOUT_S, target=self.describe_target()
            )
        except Exception as exc:
            client.stop_channels()
            raise KernelStartError(
                f"kernel on {self.describe_target()} did not become ready: {exc}"
            ) from exc
        return client

    # ── lifecycle ───────────────────────────────────────────────────

    def is_alive(self, provisioned: ProvisionedKernel) -> bool:
        handle: _ModalHandle = provisioned.handle
        if handle is None:
            return False
        try:
            if handle.sandbox.poll() is not None:
                return False
            return handle.process.poll() is None
        except Exception:
            # An SDK/transport error is not proof of death, but it is not
            # proof of life either; report dead so the manager restarts rather
            # than hanging a cell against an unreachable kernel.
            return False

    def shutdown(self, provisioned: ProvisionedKernel) -> None:
        handle: _ModalHandle = provisioned.handle
        if handle is None:
            return
        self._cleanup(handle.sandbox, handle.process, handle.forwarders)

    @staticmethod
    def _cleanup(sandbox, process, forwarders) -> None:
        for closer in (
            lambda: forwarders.close() if forwarders else None,
            lambda: process.terminate() if process else None,
            lambda: sandbox.terminate() if sandbox else None,
        ):
            try:
                closer()
            except Exception:
                pass

    # ── workspace mirroring ─────────────────────────────────────────

    def sync_in(self, provisioned: ProvisionedKernel, workdir: Path) -> None:
        """Copy host workspace files the kernel will need into the container."""
        handle: _ModalHandle = provisioned.handle
        if handle is None:
            return
        # Directories first, and *empty ones included*: prepare_cell creates
        # the per-cell staging dir before the cell runs and leaves it empty,
        # so a files-only mirror never creates it — and save_artifact then
        # fails writing into a directory that does not exist.
        for directory in _walk_dirs(Path(workdir)):
            try:
                handle.sandbox.mkdir(
                    f"{REMOTE_WORKSPACE}/{directory}", parents=True
                )
            except Exception:
                pass
        for path in _walk(Path(workdir)):
            relative = path.relative_to(workdir).as_posix()
            stamp = path.stat().st_mtime
            # Only push what changed since we last saw it, so a large declared
            # input is uploaded once rather than on every cell.
            if handle.synced_out.get(relative) == stamp:
                continue
            remote = f"{REMOTE_WORKSPACE}/{relative}"
            parent = remote.rsplit("/", 1)[0]
            try:
                handle.sandbox.mkdir(parent, parents=True)
            except Exception:
                pass
            try:
                payload = localise(path, Path(workdir), REMOTE_WORKSPACE)
                with handle.sandbox.open(remote, "wb") as fh:
                    fh.write(payload)
                handle.synced_out[relative] = stamp
            except Exception as exc:
                logger.warning("modal sync_in failed for %s: %s", relative, exc)

    def sync_out(self, provisioned: ProvisionedKernel, workdir: Path) -> None:
        """Copy container-side workspace writes back to the host."""
        handle: _ModalHandle = provisioned.handle
        if handle is None:
            return
        try:
            remote_files = _remote_walk(handle.sandbox, REMOTE_WORKSPACE)
        except Exception as exc:
            logger.warning("modal sync_out listing failed: %s", exc)
            return
        for remote in remote_files:
            relative = remote[len(REMOTE_WORKSPACE) + 1:]
            destination = Path(workdir) / relative
            try:
                with handle.sandbox.open(remote, "rb") as fh:
                    payload = fh.read()
            except Exception as exc:
                logger.warning("modal sync_out failed for %s: %s", relative, exc)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() and destination.read_bytes() == payload:
                continue
            destination.write_bytes(payload)
            try:
                handle.synced_out[relative] = destination.stat().st_mtime
            except OSError:
                pass


def _remote_walk(sandbox, root: str, depth: int = 0) -> List[str]:
    """List files under *root* in the container, recursively."""
    if depth > 8:
        return []
    found: List[str] = []
    for entry in sandbox.ls(root):
        path = f"{root}/{entry}"
        if entry in _SKIP_DIRS:
            continue
        try:
            children = sandbox.ls(path)
        except Exception:
            # ls() only fails on a non-directory, so this is a file.
            found.append(path)
            continue
        # ls() succeeded: a directory, even when it came back empty. Recursing
        # on it yields nothing, which is right — an empty staging dir has no
        # artifacts to pull back. Treating "empty" as "file" instead would try
        # to read a directory and log a spurious failure for every cell.
        found.extend(_remote_walk(sandbox, path, depth + 1))
    return found
