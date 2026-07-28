"""ModalProvisioner — remote kernels, and the parts of that which are subtle.

The unit tests here use a stub sandbox, because the interesting logic is not
"does Modal work" but the translation layer around it: which paths get
rewritten, which liveness signal is trusted, how a directory is told from a
file across an RPC boundary. Each of these was a real bug found while getting
a kernel to run remotely, and each fails in a way that looks like something
else entirely.

The live tests at the bottom are integration-marked; they create real
sandboxes and are excluded from the default run.
"""

import json

import pytest

from science.kernels import KernelStartError, PythonEnvResolver
from science.provisioners.forwarding import ForwarderSet, TLSForwarder
from science.provisioners.modal_backend import (
    CHANNEL_PORTS,
    REMOTE_WORKSPACE,
    ModalProvisioner,
    _remote_walk,
)
from science.provisioners.remote import localise, walk_dirs


class StubSandbox:
    """Enough of modal.Sandbox to exercise the translation layer."""

    def __init__(self, tree=None, alive=True):
        # tree maps path -> list of children (dir) or None (file)
        self.tree = tree or {}
        self.alive = alive

    def ls(self, path):
        if path not in self.tree or self.tree[path] is None:
            raise FileNotFoundError(f"not a directory: {path}")
        return self.tree[path]

    def poll(self):
        return None if self.alive else 1


class StubProcess:
    def __init__(self, alive=True):
        self.alive = alive

    def poll(self):
        return None if self.alive else 0


def _provisioned(sandbox=None, process=None):
    from science.kernels import ProvisionedKernel
    from science.provisioners.modal_backend import _ModalHandle

    return ProvisionedKernel(
        manager=None, client=None, location="modal:test",
        remote_workspace=REMOTE_WORKSPACE,
        handle=_ModalHandle(
            sandbox=sandbox or StubSandbox(), process=process or StubProcess(),
            forwarders=ForwarderSet(), key="k", app_name="test",
        ),
    )


# ── SCI-P1-02 target identity ───────────────────────────────────────


@pytest.mark.requirement("SCI-P1-02")
def test_target_names_the_gpu():
    assert ModalProvisioner().describe_target() == "modal:opencodon-science"
    # "ran on modal" and "ran on an A100" are different provenance claims.
    assert ModalProvisioner(gpu="A100").describe_target() == (
        "modal:opencodon-science/A100"
    )


# ── SCI-P1-04 legible failure ───────────────────────────────────────


@pytest.mark.requirement("SCI-P1-04")
def test_non_python_language_is_refused_before_a_sandbox_is_made():
    spec = PythonEnvResolver().resolve()
    r_spec = type(spec)(
        language="r", interpreter_path="R", argv=("R",), runtime_identity="R",
    )
    with pytest.raises(KernelStartError) as caught:
        ModalProvisioner().provision(r_spec, "/tmp")
    assert "python kernels only" in str(caught.value)


@pytest.mark.requirement("SCI-P1-04")
def test_liveness_does_not_trust_the_heartbeat():
    """The bug this guards: hb does not survive the tunnel.

    A remote kernel that answers kernel_info still reports is_beating()==False,
    so delegating liveness to jupyter_client would restart a healthy kernel on
    every cell and destroy the session state it exists to hold.
    """
    provisioner = ModalProvisioner()
    assert provisioner.is_alive(_provisioned()) is True
    assert provisioner.is_alive(_provisioned(sandbox=StubSandbox(alive=False))) is False
    assert provisioner.is_alive(_provisioned(process=StubProcess(alive=False))) is False


@pytest.mark.requirement("SCI-P1-04")
def test_an_unreachable_sandbox_reads_as_dead_not_alive():
    class Exploding(StubSandbox):
        def poll(self):
            raise ConnectionError("transport gone")

    # Not proof of death, but not proof of life either — report dead so the
    # manager restarts rather than hanging a cell on an unreachable kernel.
    assert ModalProvisioner().is_alive(_provisioned(sandbox=Exploding())) is False


# ── SCI-P1-03 workspace translation ─────────────────────────────────


@pytest.mark.requirement("SCI-P1-03")
def test_cell_json_paths_are_rewritten_for_the_container(tmp_path):
    workdir = tmp_path / "ws"
    (workdir / ".opencodon-science").mkdir(parents=True)
    cell = workdir / ".opencodon-science" / "cell.json"
    cell.write_text(json.dumps({
        "execution_id": "cell-1",
        "staging_dir": f"{workdir}/.opencodon-science/staging-cell-1",
        "inputs": {"v1": {"path": f"{workdir}/inputs/data.csv"}},
    }))

    rewritten = json.loads(localise(cell, workdir, REMOTE_WORKSPACE).decode())
    assert rewritten["staging_dir"].startswith(REMOTE_WORKSPACE)
    assert rewritten["inputs"]["v1"]["path"].startswith(REMOTE_WORKSPACE)
    assert str(workdir) not in json.dumps(rewritten)


@pytest.mark.requirement("SCI-P1-03")
def test_user_data_is_copied_byte_for_byte(tmp_path):
    """Only cell.json is rewritten — guessing at paths inside data corrupts it."""
    workdir = tmp_path / "ws"
    workdir.mkdir()
    payload = f"col\n{workdir}/looks/like/a/path\n".encode()
    data = workdir / "data.csv"
    data.write_bytes(payload)

    assert localise(data, workdir, REMOTE_WORKSPACE) == payload


@pytest.mark.requirement("SCI-P1-03")
def test_binary_files_survive_localisation(tmp_path):
    workdir = tmp_path / "ws"
    workdir.mkdir()
    blob = workdir / "model.bin"
    blob.write_bytes(b"\x00\x01\x02\xff\xfe")
    assert localise(blob, workdir, REMOTE_WORKSPACE) == b"\x00\x01\x02\xff\xfe"


@pytest.mark.requirement("SCI-P1-03")
def test_empty_directories_are_mirrored(tmp_path):
    """prepare_cell leaves the staging dir empty; a files-only mirror misses it
    and save_artifact then writes into a directory that does not exist."""
    workdir = tmp_path / "ws"
    (workdir / ".opencodon-science" / "staging-cell-1").mkdir(parents=True)
    (workdir / "inputs").mkdir()
    (workdir / "__pycache__").mkdir()

    dirs = walk_dirs(workdir)
    assert ".opencodon-science/staging-cell-1" in dirs
    assert "inputs" in dirs
    assert "__pycache__" not in dirs
    # Parents before children, or the child mkdir races its parent.
    assert dirs.index(".opencodon-science") < dirs.index(
        ".opencodon-science/staging-cell-1"
    )


@pytest.mark.requirement("SCI-P1-03")
def test_remote_walk_tells_an_empty_directory_from_a_file():
    """ls() succeeding means directory — even when it returns nothing.

    Reading the empty staging dir as a file logged a failure on every cell.
    """
    sandbox = StubSandbox(tree={
        REMOTE_WORKSPACE: ["out.txt", "empty_dir", "sub"],
        f"{REMOTE_WORKSPACE}/out.txt": None,
        f"{REMOTE_WORKSPACE}/empty_dir": [],
        f"{REMOTE_WORKSPACE}/sub": ["nested.bin"],
        f"{REMOTE_WORKSPACE}/sub/nested.bin": None,
    })
    found = _remote_walk(sandbox, REMOTE_WORKSPACE)

    assert f"{REMOTE_WORKSPACE}/out.txt" in found
    assert f"{REMOTE_WORKSPACE}/sub/nested.bin" in found
    assert f"{REMOTE_WORKSPACE}/empty_dir" not in found


@pytest.mark.requirement("SCI-P1-03")
def test_remote_walk_is_depth_bounded():
    deep = {f"{REMOTE_WORKSPACE}{'/d' * i}": [f"d"] for i in range(30)}
    sandbox = StubSandbox(tree=deep)
    # Terminates rather than recursing forever on a cyclic or pathological tree.
    assert _remote_walk(sandbox, REMOTE_WORKSPACE) == []


# ── forwarding ──────────────────────────────────────────────────────


@pytest.mark.requirement("SCI-P1-01")
def test_forwarders_take_ephemeral_ports():
    """Two sessions must not collide on a fixed local port."""
    a, b = TLSForwarder("example.invalid", 443), TLSForwarder("example.invalid", 443)
    try:
        assert a.local_port != b.local_port
        assert a.local_port > 1024
    finally:
        a.close()
        b.close()


@pytest.mark.requirement("SCI-P1-01")
def test_channel_ports_cover_every_zmq_channel():
    assert set(CHANNEL_PORTS) == {
        "shell_port", "iopub_port", "stdin_port", "control_port", "hb_port"
    }
    # Distinct, or two channels would share a tunnel.
    assert len(set(CHANNEL_PORTS.values())) == 5


# ── live Modal ──────────────────────────────────────────────────────


@pytest.fixture
def modal_runtime(tmp_path, db, request):
    """A ScienceRuntime whose kernel lives in a real Modal sandbox."""
    from science.blobstore import BlobStore
    from science.bridge import bootstrap_kernel
    from science.host_bridge import shutdown_bridges
    from science.kernels import SessionKernelManager
    from science.provisioners import get_provisioner
    from science.runtime import ScienceRuntime

    gpu = getattr(request, "param", None)
    provisioner = get_provisioner(
        "modal", gpu=gpu,
        app_name="opencodon-science-gpu" if gpu else "opencodon-science",
    )
    manager = SessionKernelManager(
        workspaces_root=tmp_path / "ws",
        bootstrap_fn=bootstrap_kernel,
        provisioner=provisioner,
    )
    runtime = ScienceRuntime(db, blobs=BlobStore(tmp_path / "blobs"), manager=manager)
    yield runtime
    shutdown_bridges()
    manager.shutdown()


@pytest.mark.integration
@pytest.mark.requirement("SCI-P1-10")
def test_live_modal_state_persists_and_artifacts_round_trip(modal_runtime, db):
    db.create_session("s1", source="cli")

    first = modal_runtime.run_cell(
        "s1", "import platform\nacc = [1, 2, 3]\nprint(platform.platform())",
        timeout=300,
    )
    assert first["status"] == "ok"
    assert "Linux" in first["stdout"], "cell did not run in the container"

    second = modal_runtime.run_cell(
        "s1", "save_artifact(str(sum(acc)), 'total.txt')\nprint('sum', sum(acc))",
        timeout=300,
    )
    assert second["status"] == "ok"
    # `acc` came from the previous cell: the remote kernel held state.
    assert "sum 6" in second["stdout"]

    [artifact] = second["artifacts"]
    assert modal_runtime.blobs.read_bytes(artifact["sha256"]) == b"6"

    rows = [modal_runtime.store.get_cell(r["cell_id"]) for r in (first, second)]
    assert rows[0]["kernel_id"] == rows[1]["kernel_id"], "kernel was restarted"
    assert rows[1]["kernel_location"].startswith("modal:")


@pytest.mark.integration
@pytest.mark.requirement("SCI-P1-12")
@pytest.mark.parametrize("modal_runtime", ["T4"], indirect=True)
def test_live_modal_gpu_is_visible(modal_runtime, db):
    db.create_session("g1", source="cli")
    result = modal_runtime.run_cell(
        "g1",
        "import subprocess\n"
        "print(subprocess.run(['nvidia-smi', '--query-gpu=name', "
        "'--format=csv,noheader'], capture_output=True, text=True).stdout.strip())",
        timeout=300,
    )
    assert result["status"] == "ok"
    assert "Tesla" in result["stdout"] or "NVIDIA" in result["stdout"]
    location = modal_runtime.store.get_cell(result["cell_id"])["kernel_location"]
    assert location.endswith("/T4"), "the accelerator must be in the provenance"
