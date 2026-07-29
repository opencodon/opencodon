"""Run a science kernel on an SSH host.

The backend for a lab GPU box or a login node — the machine a researcher
already has, rather than one rented per session.

Transport differs from Modal in one useful way: SSH carries its own
encryption, so there is no TLS forwarder here. ``ssh -L`` opens the five
channel tunnels directly, and the OpenSSH client already handles keys, agents,
jump hosts and ``~/.ssh/config`` — reusing it means a host that works in a
terminal works here, without reimplementing any of that.

The kernel is started detached with ``nohup`` so it outlives the SSH session
that launched it; liveness is then a signal-0 check on its recorded pid.
"""

from __future__ import annotations

import json
import logging
import secrets
import shlex
import socket
import subprocess
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
from science.provisioners.remote import (
    RemoteStat,
    await_answering,
    localise,
    parse_stat_listing,
    stat_probe_argv,
    walk_dirs,
    walk_files,
)

logger = logging.getLogger(__name__)

CHANNELS = ("shell_port", "iopub_port", "stdin_port", "control_port", "hb_port")
# Written into the remote workspace by this backend, so never pulled back as
# though the cell had produced them.
_BOOKKEEPING = frozenset({"kernel.log", "kernel-connection.json"})
SSH_CONNECT_TIMEOUT_S = 20
KERNEL_LISTEN_TIMEOUT_S = 60.0
COMMAND_TIMEOUT_S = 120

# BatchMode: never sit at an interactive password prompt — an agent has no way
# to answer one, and the run would hang rather than fail.
BASE_SSH_OPTS = (
    "-o", "BatchMode=yes",
    "-o", f"ConnectTimeout={SSH_CONNECT_TIMEOUT_S}",
    "-o", "ServerAliveInterval=15",
)


@dataclass
class _SSHHandle:
    destination: str
    remote_workspace: str
    pid: Optional[int]
    tunnels: List[subprocess.Popen] = field(default_factory=list)
    local_ports: List[int] = field(default_factory=list)
    ssh_opts: tuple = ()
    synced: Dict[str, float] = field(default_factory=dict)
    # Remote (size, mtime) as of the last sync_in, i.e. immediately before the
    # cell ran. Anything differing afterwards is what the cell wrote, which is
    # the only thing worth pulling back.
    remote_seen: Dict[str, RemoteStat] = field(default_factory=dict)


def _port_open(port: int) -> bool:
    """Whether a forwarded local port still accepts a connection."""
    probe = socket.socket()
    probe.settimeout(1.0)
    try:
        probe.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def _reserve_local_ports(count: int) -> List[int]:
    """Distinct free local ports.

    Allocated with every socket held open at once, then all released. Binding
    and closing one at a time lets the OS hand the same port back twice — two
    ``ssh -L`` tunnels then race for it, the loser exits, and the symptom is a
    kernel that looks dead between cells and gets silently restarted, losing
    session state.
    """
    sockets = []
    try:
        for _ in range(count):
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            sockets.append(sock)
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


class SSHProvisioner(KernelProvisioner):
    """Provision the session kernel as an ipykernel on a remote SSH host."""

    name = "ssh"

    def __init__(
        self,
        host: str,
        *,
        user: Optional[str] = None,
        port: int = 22,
        identity: Optional[str] = None,
        python: str = "python3",
        remote_workspace: Optional[str] = None,
        extra_options: tuple = (),
    ):
        if not (host or "").strip():
            raise ValueError("an SSH host is required")
        self.host = host.strip()
        self.user = user
        self.port = int(port)
        self.identity = identity
        self.python = python
        # Extra `-o` flags, for the things real hosts need: ProxyJump, a
        # non-default known_hosts, a specific KexAlgorithms. Host-key checking
        # stays on by default — relaxing it is the caller's explicit choice.
        self.extra_options = tuple(extra_options)
        # One multiplexed TCP connection for every command, matching
        # tools/environments/ssh.py. Provisioning otherwise opens a dozen
        # separate connections in a few seconds — enough for sshd's MaxStartups
        # to start refusing them, which surfaces as an empty-stderr failure
        # that looks like nothing at all.
        self._control_path = f"/tmp/oc-ssh-{secrets.token_hex(6)}.sock"
        # Per-provisioner so two sessions to one host cannot share a workspace
        # and overwrite each other's cell.json.
        # Stored home-relative and without a leading "~". shlex.quote would
        # quote the tilde, and the remote shell then takes it literally —
        # creating an actual directory named "~" under $HOME.
        workspace = remote_workspace or f".opencodon-science/ws-{secrets.token_hex(4)}"
        self.remote_workspace = workspace[2:] if workspace.startswith("~/") else workspace

    @property
    def destination(self) -> str:
        return f"{self.user}@{self.host}" if self.user else self.host

    def describe_target(self) -> str:
        target = self.destination
        return f"{self.name}:{target}" + (f":{self.port}" if self.port != 22 else "")

    # ── ssh plumbing ────────────────────────────────────────────────

    @property
    def _opts(self) -> tuple:
        opts = BASE_SSH_OPTS + (
            "-p", str(self.port),
            "-o", "ControlMaster=auto",
            "-o", f"ControlPath={self._control_path}",
            "-o", "ControlPersist=120",
        )
        if self.identity:
            opts += ("-i", self.identity)
        for option in self.extra_options:
            opts += ("-o", option)
        return opts

    def _remote_root_expr(self) -> str:
        """Shell expression for the workspace root, absolute or under $HOME."""
        if self.remote_workspace.startswith("/"):
            return shlex.quote(self.remote_workspace)
        return '"$HOME"/' + shlex.quote(self.remote_workspace)

    def _run(
        self,
        command: str,
        *,
        timeout: int = COMMAND_TIMEOUT_S,
        input: Optional[str] = None,
    ):
        return subprocess.run(
            ["ssh", *self._opts, self.destination, command],
            input=input, capture_output=True, text=True, timeout=timeout,
        )

    def _check(self, command: str, what: str, *, input: Optional[str] = None) -> str:
        result = self._run(command, input=input)
        if result.returncode != 0:
            raise KernelStartError(
                f"{what} failed on {self.describe_target()}: "
                f"{(result.stderr or result.stdout).strip()[:400]}"
            )
        return result.stdout.strip()

    # ── provisioning ────────────────────────────────────────────────

    def provision(self, spec, workdir: Path) -> ProvisionedKernel:
        if spec.language != "python":
            raise KernelStartError(
                f"{self.describe_target()} provisions python kernels only, "
                f"got {spec.language!r}"
            )

        handle = _SSHHandle(
            destination=self.destination, remote_workspace="", pid=None,
            ssh_opts=self._opts,
        )
        client = None
        try:
            # Resolve ~ remotely: every later path is absolute, because
            # cell.json is read by a process whose cwd we do not control.
            target = self._remote_root_expr()
            # 0700: the workspace holds the session's data and, until the
            # kernel exits, its connection file. On a shared login node the
            # default umask would leave both world-readable.
            root = self._check(
                f"mkdir -p {target} && chmod 700 {target} && cd {target} && pwd",
                "creating the remote workspace",
            )
            handle.remote_workspace = root

            remote_ports = dict(zip(CHANNELS, self._free_remote_ports(len(CHANNELS))))
            key = secrets.token_hex(16)
            connection = {
                "transport": "tcp", "ip": "127.0.0.1", "key": key,
                "signature_scheme": "hmac-sha256", "kernel_name": "python3",
                **remote_ports,
            }
            conn_path = f"{root}/kernel-connection.json"
            # Bound to 127.0.0.1, not 0.0.0.0: the channels are reachable only
            # through the SSH tunnel, so a shared login node does not expose
            # someone else's kernel to the network.
            #
            # Piped over stdin, never interpolated into the command. The
            # connection JSON carries the HMAC key that authenticates every
            # message to this kernel, and a command string is argv — readable
            # from /proc by any other user on the host, which on a shared login
            # node is precisely the threat. `umask 077` covers the window
            # between create and chmod.
            self._check(
                f"umask 077 && cat > {shlex.quote(conn_path)}",
                "writing the kernel connection file",
                input=json.dumps(connection),
            )

            # Launched by a remote Python daemonizer rather than `nohup ... &`.
            # Shell backgrounding does not work here: a backgrounded ipykernel
            # keeps sshd's channel open no matter how its stdio is redirected —
            # nohup, setsid and `ssh -f` all still hang until the timeout,
            # while a backgrounded `sleep` returns fine. start_new_session plus
            # explicit fds means the kernel inherits nothing of the SSH session,
            # so the launch returns immediately and the kernel outlives it.
            launcher = (
                "import subprocess\n"
                f"cfg = {json.dumps({'root': root, 'conn': conn_path, 'python': self.python})}\n"
                "log = open(cfg['root'] + '/kernel.log', 'wb')\n"
                "p = subprocess.Popen(\n"
                "    [cfg['python'], '-m', 'ipykernel_launcher', '-f', cfg['conn']],\n"
                "    stdin=subprocess.DEVNULL, stdout=log, stderr=log,\n"
                "    start_new_session=True, cwd=cfg['root'])\n"
                "print(p.pid)\n"
            )
            pid = self._check(
                f"{shlex.quote(self.python)} -c {shlex.quote(launcher)}",
                "starting the remote kernel",
            )
            handle.pid = int(pid.split()[-1])
            self._await_listening(remote_ports, root)

            reserved = _reserve_local_ports(len(remote_ports))
            local_ports = dict(zip(remote_ports, reserved))
            for channel, remote_port in remote_ports.items():
                handle.tunnels.append(
                    self._open_tunnel(local_ports[channel], remote_port)
                )
            handle.local_ports = list(local_ports.values())
            self._await_tunnels(handle, handle.local_ports)

            client = self._connect(key, local_ports)
        except KernelStartError:
            self._teardown(handle, client)
            raise
        except Exception as exc:
            self._teardown(handle, client)
            raise KernelStartError(
                f"kernel failed to start on {self.describe_target()}: {exc}"
            ) from exc

        return ProvisionedKernel(
            manager=None,
            client=client,
            location=self.describe_target(),
            remote_workspace=handle.remote_workspace,
            handle=handle,
        )

    def _free_remote_ports(self, count: int) -> List[int]:
        """Ask the host for *count* free ports, in a single round trip.

        Fixed numbers would collide on a shared login node, where several
        people may be running kernels at once. All sockets are held open
        together so the host cannot return the same port twice.
        """
        picker = (
            "import socket\n"
            f"socks=[socket.socket() for _ in range({int(count)})]\n"
            "[s.bind(('127.0.0.1',0)) for s in socks]\n"
            "print(' '.join(str(s.getsockname()[1]) for s in socks))\n"
            "[s.close() for s in socks]\n"
        )
        out = self._check(
            f"{shlex.quote(self.python)} -c {shlex.quote(picker)}",
            "allocating remote ports",
        )
        ports = [int(value) for value in out.split()]
        if len(ports) != count:
            raise KernelStartError(
                f"expected {count} remote ports from {self.describe_target()}, "
                f"got {out!r}"
            )
        return ports

    def _open_tunnel(self, local_port: int, remote_port: int) -> subprocess.Popen:
        """One ``ssh -L`` forwarding a single ZMQ channel."""
        return subprocess.Popen(
            [
                "ssh", *self._opts, "-N",
                "-L", f"127.0.0.1:{local_port}:127.0.0.1:{remote_port}",
                self.destination,
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    def _await_tunnels(self, handle: "_SSHHandle", ports: List[int]) -> None:
        """Wait until every forwarded port accepts locally.

        Connecting before a tunnel is up produces a ZMQ socket that silently
        never delivers, which reads downstream as a dead kernel rather than as
        a tunnel that was not ready.
        """
        deadline = time.monotonic() + SSH_CONNECT_TIMEOUT_S
        pending = set(ports)
        while pending and time.monotonic() < deadline:
            for tunnel in handle.tunnels:
                # Exit 0 is expected and means success: with ControlMaster the
                # forwarding is handed to the shared master connection and the
                # `ssh -N` process exits straight away. Only a non-zero exit is
                # a real failure.
                if tunnel.poll() not in (None, 0):
                    raise KernelStartError(
                        f"an ssh tunnel to {self.describe_target()} failed "
                        f"(exit {tunnel.returncode})"
                    )
            for port in list(pending):
                probe = socket.socket()
                probe.settimeout(1.0)
                try:
                    probe.connect(("127.0.0.1", port))
                    pending.discard(port)
                except OSError:
                    pass
                finally:
                    probe.close()
            if pending:
                time.sleep(0.3)
        if pending:
            raise KernelStartError(
                f"ssh tunnels to {self.describe_target()} did not come up: "
                f"{sorted(pending)}"
            )

    def _await_listening(self, remote_ports: Dict[str, int], root: str) -> None:
        probe = (
            "import socket,sys\n"
            f"ports={sorted(remote_ports.values())}\n"
            "for p in ports:\n"
            "    s=socket.socket(); s.settimeout(1)\n"
            "    try: s.connect(('127.0.0.1',p))\n"
            "    except Exception: sys.exit(1)\n"
            "    finally: s.close()\n"
        )
        deadline = time.monotonic() + KERNEL_LISTEN_TIMEOUT_S
        while time.monotonic() < deadline:
            result = self._run(f"{shlex.quote(self.python)} -c {shlex.quote(probe)}")
            if result.returncode == 0:
                return
            time.sleep(1.5)
        # The kernel's own log is the only thing that explains *why* — a bare
        # "did not start" sends the reader to the wrong machine.
        log = self._run(f"tail -20 {shlex.quote(root)}/kernel.log").stdout.strip()
        raise KernelStartError(
            f"kernel did not bind its channels on {self.describe_target()} "
            f"within {KERNEL_LISTEN_TIMEOUT_S:.0f}s"
            + (f"; remote log:\n{log}" if log else "")
        )

    def _connect(self, key: str, local_ports: Dict[str, int]):
        from jupyter_client import BlockingKernelClient

        client = BlockingKernelClient()
        client.load_connection_info({
            "transport": "tcp", "ip": "127.0.0.1", "key": key,
            "signature_scheme": "hmac-sha256", **local_ports,
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
        handle: _SSHHandle = provisioned.handle
        if handle is None or handle.pid is None:
            return False
        # Not the tunnel *processes*: under ControlMaster they exit 0 as soon
        # as the master owns the forwarding, so polling them reports a healthy
        # kernel as dead — and the manager then restarts it every cell,
        # silently losing the session's variables. Probe the forwarded ports
        # instead, which is the property actually being relied on.
        if not all(_port_open(port) for port in handle.local_ports):
            return False
        try:
            return self._run(f"kill -0 {handle.pid}", timeout=30).returncode == 0
        except Exception:
            return False

    def shutdown(self, provisioned: ProvisionedKernel) -> None:
        self._teardown(provisioned.handle, None)

    def _teardown(self, handle: Optional[_SSHHandle], client) -> None:
        if client is not None:
            try:
                client.stop_channels()
            except Exception:
                pass
        if handle is None:
            return
        for tunnel in handle.tunnels:
            try:
                tunnel.terminate()
            except Exception:
                pass
        handle.tunnels.clear()
        if handle.pid:
            try:
                self._run(f"kill {handle.pid}", timeout=30)
            except Exception:
                pass
            handle.pid = None
        try:
            subprocess.run(
                ["ssh", "-O", "exit", "-o", f"ControlPath={self._control_path}",
                 self.destination],
                capture_output=True, timeout=15,
            )
        except Exception:
            pass

    # ── workspace mirroring ─────────────────────────────────────────

    def _remote_listing(self, root: str) -> Dict[str, RemoteStat]:
        """``{relative: (size, mtime)}`` for every file in the remote workspace."""
        argv = stat_probe_argv(self.python, root)
        result = self._run(" ".join(shlex.quote(part) for part in argv))
        if result.returncode != 0:
            # Not a KernelStartError: the kernel is running fine, the mirror
            # is what failed, and both callers treat that as recoverable.
            raise RuntimeError(
                f"listing the remote workspace on {self.describe_target()} "
                f"failed: {(result.stderr or result.stdout).strip()[:200]}"
            )
        return parse_stat_listing(result.stdout)

    def sync_in(self, provisioned: ProvisionedKernel, workdir: Path) -> None:
        handle: _SSHHandle = provisioned.handle
        if handle is None or not handle.remote_workspace:
            return
        root = handle.remote_workspace
        directories = walk_dirs(Path(workdir))
        if directories:
            joined = " ".join(shlex.quote(f"{root}/{d}") for d in directories)
            self._run(f"mkdir -p {joined}")

        for path in walk_files(Path(workdir)):
            relative = path.relative_to(workdir).as_posix()
            stamp = path.stat().st_mtime
            if handle.synced.get(relative) == stamp:
                continue
            payload = localise(path, Path(workdir), root)
            try:
                subprocess.run(
                    ["ssh", *self._opts, self.destination,
                     f"cat > {shlex.quote(f'{root}/{relative}')}"],
                    input=payload, capture_output=True, timeout=COMMAND_TIMEOUT_S,
                    check=True,
                )
                handle.synced[relative] = stamp
            except Exception as exc:
                logger.warning("ssh sync_in failed for %s: %s", relative, exc)

        # Baseline for sync_out, taken *after* the push and before the cell:
        # whatever differs from this is the cell's own work. Without it every
        # file we just uploaded reads as new on the way back, so a large
        # declared input would make the round trip twice per cell.
        try:
            handle.remote_seen = self._remote_listing(root)
        except Exception as exc:
            # An empty baseline is the safe direction — it costs a redundant
            # fetch, where a stale one would silently drop a written artifact.
            logger.warning("ssh sync_in baseline failed: %s", exc)
            handle.remote_seen = {}

    def sync_out(self, provisioned: ProvisionedKernel, workdir: Path) -> None:
        handle: _SSHHandle = provisioned.handle
        if handle is None or not handle.remote_workspace:
            return
        root = handle.remote_workspace
        try:
            listing = self._remote_listing(root)
        except Exception as exc:
            logger.warning("ssh sync_out listing failed: %s", exc)
            return

        baseline = handle.remote_seen
        for relative, stat in listing.items():
            if relative in _BOOKKEEPING:
                continue  # backend plumbing, not the session's workspace
            # The whole point of the baseline: an untouched file is not read,
            # not transferred and not compared, so a cell costs what it wrote
            # rather than what the workspace happens to hold.
            if baseline.get(relative) == stat:
                continue
            try:
                fetched = subprocess.run(
                    ["ssh", *self._opts, self.destination,
                     f"cat {shlex.quote(f'{root}/{relative}')}"],
                    capture_output=True, timeout=COMMAND_TIMEOUT_S, check=True,
                )
            except Exception as exc:
                logger.warning("ssh sync_out failed for %s: %s", relative, exc)
                continue
            destination = Path(workdir) / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() and destination.read_bytes() == fetched.stdout:
                continue
            destination.write_bytes(fetched.stdout)
            try:
                # Host-side mtime, so the file we just pulled down is not
                # pushed straight back up on the next cell.
                handle.synced[relative] = destination.stat().st_mtime
            except OSError:
                pass
        handle.remote_seen = listing
