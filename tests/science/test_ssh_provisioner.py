"""SSHProvisioner — kernels on a host the researcher already has.

The unit tests cover the translation layer and the two liveness traps; the
integration test runs a real kernel over a real OpenSSH server.

There is no sshd on the machine this was built on, so the live target is a
Modal sandbox running openssh, reached through the same TLS forwarder the
Modal backend uses. That makes it genuine key auth and genuine `ssh -L`
tunnels rather than a mock of them.
"""

import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from science.kernels import KernelStartError, ProvisionedKernel, PythonEnvResolver
from science.provisioners.ssh_backend import (
    CHANNELS,
    SSHProvisioner,
    _port_open,
    _reserve_local_ports,
    _SSHHandle,
)


def _handle(**kwargs):
    defaults = dict(
        destination="ada@gpu-01", remote_workspace="/home/ada/ws", pid=4242,
    )
    defaults.update(kwargs)
    return _SSHHandle(**defaults)


def _provisioned(**kwargs):
    return ProvisionedKernel(
        manager=None, client=None, location="ssh:ada@gpu-01",
        remote_workspace="/home/ada/ws", handle=_handle(**kwargs),
    )


# ── SCI-P1-02 target identity ───────────────────────────────────────


@pytest.mark.requirement("SCI-P1-02")
def test_target_names_host_user_and_nondefault_port():
    assert SSHProvisioner("gpu-01").describe_target() == "ssh:gpu-01"
    assert SSHProvisioner("gpu-01", user="ada").describe_target() == "ssh:ada@gpu-01"
    assert SSHProvisioner("gpu-01", port=2222).describe_target() == "ssh:gpu-01:2222"


# ── SCI-P1-03 remote path handling ──────────────────────────────────


@pytest.mark.requirement("SCI-P1-03")
def test_tilde_is_stripped_from_the_workspace():
    """shlex.quote would quote the tilde, and the remote shell then takes it
    literally — creating a directory actually named "~" under $HOME."""
    provisioner = SSHProvisioner("gpu-01", remote_workspace="~/science/ws")
    assert provisioner.remote_workspace == "science/ws"
    # Expanded by the remote shell rather than quoted into a literal "~".
    assert provisioner._remote_root_expr().startswith('"$HOME"/')
    assert "~" not in provisioner._remote_root_expr()


@pytest.mark.requirement("SCI-P1-03")
def test_absolute_workspaces_are_not_placed_under_home():
    provisioner = SSHProvisioner("gpu-01", remote_workspace="/scratch/ada/ws")
    assert provisioner._remote_root_expr() == "/scratch/ada/ws"
    assert "$HOME" not in provisioner._remote_root_expr()


@pytest.mark.requirement("SCI-P1-03")
def test_workspace_paths_needing_quoting_are_quoted():
    """shlex.quote only quotes when it must, so prove the awkward case works."""
    provisioner = SSHProvisioner("gpu-01", remote_workspace="/scratch/my data")
    assert provisioner._remote_root_expr() == "'/scratch/my data'"


@pytest.mark.requirement("SCI-P1-03")
def test_each_provisioner_gets_its_own_remote_workspace():
    # Two sessions to one host must not share a workspace and overwrite each
    # other's cell.json.
    assert SSHProvisioner("gpu-01").remote_workspace != (
        SSHProvisioner("gpu-01").remote_workspace
    )


# ── SCI-P1-01 connection options ────────────────────────────────────


@pytest.mark.requirement("SCI-P1-01")
def test_batch_mode_is_always_on():
    """An agent cannot answer a password prompt; without BatchMode the run
    hangs at one instead of failing."""
    opts = SSHProvisioner("gpu-01")._opts
    assert "BatchMode=yes" in opts


@pytest.mark.requirement("SCI-P1-01")
def test_connections_are_multiplexed():
    """Provisioning opens a dozen commands in seconds — enough for sshd's
    MaxStartups to start refusing them without multiplexing."""
    opts = SSHProvisioner("gpu-01")._opts
    assert "ControlMaster=auto" in opts
    assert any(opt.startswith("ControlPath=") for opt in opts)


@pytest.mark.requirement("SCI-P1-01")
def test_extra_options_are_passed_through():
    provisioner = SSHProvisioner("gpu-01", extra_options=("ProxyJump=bastion",))
    assert "ProxyJump=bastion" in provisioner._opts
    # Host-key checking is not relaxed by default.
    assert "StrictHostKeyChecking=no" not in SSHProvisioner("gpu-01")._opts


@pytest.mark.requirement("SCI-P1-01")
def test_reserved_local_ports_are_distinct():
    """Binding and closing one at a time lets the OS reissue a port; two
    `ssh -L` tunnels then race for it and the loser dies."""
    ports = _reserve_local_ports(5)
    assert len(set(ports)) == 5


@pytest.mark.requirement("SCI-P1-01")
def test_channel_list_covers_every_zmq_channel():
    assert set(CHANNELS) == {
        "shell_port", "iopub_port", "stdin_port", "control_port", "hb_port"
    }


# ── SCI-P1-04 liveness ──────────────────────────────────────────────


@pytest.mark.requirement("SCI-P1-04")
def test_liveness_ignores_exited_tunnel_processes(monkeypatch):
    """Under ControlMaster an `ssh -N -L` exits 0 once the master owns the
    forwarding. Treating that as death restarts a healthy kernel every cell."""
    provisioner = SSHProvisioner("gpu-01")
    exited = subprocess.Popen(["true"])
    exited.wait()

    monkeypatch.setattr(
        provisioner, "_run",
        lambda *a, **k: subprocess.CompletedProcess([], 0, "", ""),
    )
    monkeypatch.setattr(
        "science.provisioners.ssh_backend._port_open", lambda port: True
    )

    provisioned = _provisioned(tunnels=[exited], local_ports=[1234])
    assert provisioner.is_alive(provisioned) is True


@pytest.mark.requirement("SCI-P1-04")
def test_liveness_fails_when_a_forwarded_port_is_gone(monkeypatch):
    provisioner = SSHProvisioner("gpu-01")
    monkeypatch.setattr(
        "science.provisioners.ssh_backend._port_open", lambda port: False
    )
    assert provisioner.is_alive(_provisioned(local_ports=[1234])) is False


@pytest.mark.requirement("SCI-P1-04")
def test_liveness_fails_when_the_remote_process_is_gone(monkeypatch):
    provisioner = SSHProvisioner("gpu-01")
    monkeypatch.setattr(
        "science.provisioners.ssh_backend._port_open", lambda port: True
    )
    monkeypatch.setattr(
        provisioner, "_run",
        lambda *a, **k: subprocess.CompletedProcess([], 1, "", "no such process"),
    )
    assert provisioner.is_alive(_provisioned(local_ports=[1234])) is False


@pytest.mark.requirement("SCI-P1-04")
def test_liveness_without_a_pid_is_false():
    assert SSHProvisioner("gpu-01").is_alive(_provisioned(pid=None)) is False


@pytest.mark.requirement("SCI-P1-04")
def test_port_open_reports_a_closed_port():
    ports = _reserve_local_ports(1)
    assert _port_open(ports[0]) is False


@pytest.mark.requirement("SCI-P1-04")
def test_non_python_language_is_refused_before_connecting():
    spec = PythonEnvResolver().resolve()
    r_spec = type(spec)(
        language="r", interpreter_path="R", argv=("R",), runtime_identity="R",
    )
    with pytest.raises(KernelStartError) as caught:
        SSHProvisioner("gpu-01").provision(r_spec, "/tmp")
    assert "python kernels only" in str(caught.value)


@pytest.mark.requirement("SCI-P1-04")
def test_a_host_is_required():
    with pytest.raises(ValueError):
        SSHProvisioner("")


# ── live SSH ────────────────────────────────────────────────────────


class ThrowawaySSHD:
    """A real OpenSSH server in a Modal sandbox, reached via a TLS forwarder."""

    SSH_PORT = 2222

    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.key = self.tmp / "id_ed25519"
        self.sandbox = None
        self.forwarder = None

    def start(self) -> dict:
        import modal

        from science.provisioners.forwarding import TLSForwarder

        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(self.key), "-q"],
            check=True,
        )
        public = (self.tmp / "id_ed25519.pub").read_text().strip()

        image = (
            modal.Image.debian_slim()
            .apt_install("openssh-server")
            .pip_install("ipykernel==6.30.1")
        )
        app = modal.App.lookup("opencodon-sshd-fixture", create_if_missing=True)
        self.sandbox = modal.Sandbox.create(
            app=app, image=image, timeout=900, encrypted_ports=[self.SSH_PORT]
        )
        setup = (
            "mkdir -p /run/sshd /root/.ssh && "
            f"echo {public!r} > /root/.ssh/authorized_keys && "
            "chmod 700 /root/.ssh && chmod 600 /root/.ssh/authorized_keys && "
            "ssh-keygen -A && "
            f"/usr/sbin/sshd -p {self.SSH_PORT} "
            "-o PermitRootLogin=prohibit-password -o PasswordAuthentication=no"
        )
        if self.sandbox.exec("bash", "-c", setup).wait() != 0:
            raise RuntimeError("sshd setup failed")

        host, port = self.sandbox.tunnels()[self.SSH_PORT].tls_socket
        self.forwarder = TLSForwarder(host, port)
        time.sleep(2)
        return {
            "host": "127.0.0.1",
            "port": self.forwarder.local_port,
            "user": "root",
            "identity": str(self.key),
            # A throwaway host whose key was generated seconds ago and is
            # discarded after: there is nothing meaningful to pin.
            "extra_options": (
                "StrictHostKeyChecking=no", "UserKnownHostsFile=/dev/null",
                "LogLevel=ERROR",
            ),
        }

    def stop(self) -> None:
        for closer in (
            lambda: self.forwarder and self.forwarder.close(),
            lambda: self.sandbox and self.sandbox.terminate(),
        ):
            try:
                closer()
            except Exception:
                pass


@pytest.fixture
def ssh_runtime(tmp_path, db):
    from science.blobstore import BlobStore
    from science.bridge import bootstrap_kernel
    from science.host_bridge import shutdown_bridges
    from science.kernels import SessionKernelManager
    from science.provisioners import get_provisioner
    from science.runtime import ScienceRuntime

    sshd = ThrowawaySSHD()
    connection = sshd.start()
    manager = SessionKernelManager(
        workspaces_root=tmp_path / "ws",
        bootstrap_fn=bootstrap_kernel,
        provisioner=get_provisioner("ssh", **connection),
    )
    runtime = ScienceRuntime(db, blobs=BlobStore(tmp_path / "blobs"), manager=manager)
    yield runtime
    shutdown_bridges()
    manager.shutdown()
    sshd.stop()


@pytest.mark.integration
@pytest.mark.requirement("SCI-P1-11")
def test_live_ssh_state_persists_and_artifacts_round_trip(ssh_runtime, db):
    db.create_session("s1", source="cli")

    first = ssh_runtime.run_cell(
        "s1", "import platform\nacc = [1, 2, 3]\nprint(platform.platform())",
        timeout=300,
    )
    assert first["status"] == "ok"
    assert "Linux" in first["stdout"], "cell did not run on the remote host"

    second = ssh_runtime.run_cell(
        "s1", "save_artifact(str(sum(acc)), 'total.txt')\nprint('sum', sum(acc))",
        timeout=300,
    )
    assert second["status"] == "ok"
    # `acc` came from the previous cell — the remote kernel was not restarted.
    assert "sum 6" in second["stdout"]

    [artifact] = second["artifacts"]
    assert ssh_runtime.blobs.read_bytes(artifact["sha256"]) == b"6"

    rows = [ssh_runtime.store.get_cell(r["cell_id"]) for r in (first, second)]
    assert rows[0]["kernel_id"] == rows[1]["kernel_id"], "kernel was restarted"
    assert rows[1]["kernel_location"].startswith("ssh:")
