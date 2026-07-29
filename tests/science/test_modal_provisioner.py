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
    _remote_listing,
)
from science.provisioners.remote import (
    localise,
    parse_stat_listing,
    stat_probe_argv,
    walk_dirs,
)


class StubStream:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return self._payload


class StubExec:
    def __init__(self, payload, returncode=0):
        self.stdout = StubStream(payload)
        self._returncode = returncode

    def wait(self):
        return self._returncode

    def poll(self):
        return None


class StubFile:
    def __init__(self, payload, sink=None):
        self._payload = payload
        self._sink = sink

    def read(self):
        return self._payload

    def write(self, data):
        if self._sink is not None:
            self._sink.append(data)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class StubSandbox:
    """Enough of modal.Sandbox to exercise the translation layer.

    ``listing`` is what the in-container stat probe would print: one
    ``(size, mtime, relative_path)`` per file.
    """

    def __init__(self, listing=(), contents=None, alive=True, exec_fails=False):
        self.listing = list(listing)
        self.contents = dict(contents or {})
        self.alive = alive
        self.exec_fails = exec_fails
        self.execs = 0
        self.opened = []
        self.written = {}

    def exec(self, *argv):
        self.execs += 1
        if self.exec_fails:
            return StubExec("", returncode=1)
        rows = "".join(f"{size}\t{mtime}\t{rel}\n" for size, mtime, rel in self.listing)
        return StubExec(rows)

    def open(self, path, mode="r"):
        relative = path[len(REMOTE_WORKSPACE) + 1:]
        if "w" in mode:
            sink = self.written.setdefault(relative, [])
            return StubFile(None, sink=sink)
        self.opened.append(relative)
        return StubFile(self.contents[relative])

    def mkdir(self, path, parents=False):
        return None

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
def test_language_is_checked_before_the_sdk_import(monkeypatch):
    """Argument validation must not depend on an optional dependency.

    CI runs without the modal extra, so an SDK-first check reported "modal is
    not installed" for a call that was malformed regardless — sending the
    caller to fix the wrong thing, and making the test above pass or fail
    depending on which extras happened to be present.
    """
    import builtins

    real_import = builtins.__import__

    def no_modal(name, *args, **kwargs):
        if name == "modal":
            raise ImportError("No module named 'modal'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_modal)

    spec = PythonEnvResolver().resolve()
    r_spec = type(spec)(
        language="r", interpreter_path="R", argv=("R",), runtime_identity="R",
    )
    with pytest.raises(KernelStartError) as caught:
        ModalProvisioner().provision(r_spec, "/tmp")
    assert "python kernels only" in str(caught.value)

    # And a well-formed call with no SDK still says what is actually missing.
    with pytest.raises(KernelStartError) as caught:
        ModalProvisioner().provision(spec, "/tmp")
    assert "modal SDK is not installed" in str(caught.value)


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
def test_remote_listing_reports_files_with_their_stats():
    """The listing is one exec, and only files appear in it.

    The `ls`-per-directory walk this replaced had to infer "directory" from
    whether ls() raised, and read the empty staging dir as a file — logging a
    spurious failure on every cell. os.walk cannot make that mistake.
    """
    sandbox = StubSandbox(listing=[
        ("12", "1000.000000", "out.txt"),
        ("34", "1001.000000", "sub/nested.bin"),
    ])
    found = _remote_listing(sandbox)

    assert found == {
        "out.txt": ("12", "1000.000000"),
        "sub/nested.bin": ("34", "1001.000000"),
    }
    assert sandbox.execs == 1


@pytest.mark.requirement("SCI-P1-03")
def test_remote_listing_skips_the_directories_never_worth_shipping():
    """__pycache__ is generated remotely and must not ride back to the host."""
    argv = stat_probe_argv("python3", REMOTE_WORKSPACE)
    assert "__pycache__" in argv[-1]
    assert ".git" in argv[-1]


@pytest.mark.requirement("SCI-P1-03")
def test_a_malformed_listing_row_costs_a_fetch_not_the_sync():
    assert parse_stat_listing("garbage\n12\t1000.0\tkept.txt\n") == {
        "kept.txt": ("12", "1000.0")
    }


@pytest.mark.requirement("SCI-P1-03")
def test_only_files_the_cell_touched_are_pulled_back(tmp_path):
    """The cost of a cell is what it wrote, not what the workspace holds.

    Re-reading every file each cell made cell N pay for the artifacts of cells
    1..N-1, and pulled back the inputs sync_in had just pushed up.
    """
    workdir = tmp_path / "ws"
    workdir.mkdir()
    before = [
        ("9", "1000.000000", "big_input.csv"),
        ("4", "1000.000000", "notes.txt"),
    ]
    after = before[:1] + [
        ("7", "2000.000000", "notes.txt"),        # rewritten by the cell
        ("5", "2000.000000", "figure.png"),       # created by the cell
    ]

    sandbox = StubSandbox(listing=before, contents={
        "notes.txt": b"after!!", "figure.png": b"png..",
    })
    provisioned = _provisioned(sandbox=sandbox)
    provisioner = ModalProvisioner()

    provisioner.sync_in(provisioned, workdir)   # establishes the baseline
    sandbox.listing = after
    provisioner.sync_out(provisioned, workdir)

    assert sandbox.opened == ["notes.txt", "figure.png"]
    assert "big_input.csv" not in sandbox.opened
    assert (workdir / "notes.txt").read_bytes() == b"after!!"


@pytest.mark.requirement("SCI-P1-03")
def test_an_unreadable_baseline_pulls_everything_rather_than_nothing(tmp_path):
    """A stale baseline would silently drop an artifact; a redundant fetch
    only costs bandwidth, so failure resolves that way."""
    workdir = tmp_path / "ws"
    workdir.mkdir()
    sandbox = StubSandbox(
        listing=[("4", "1000.000000", "out.txt")],
        contents={"out.txt": b"data"},
        exec_fails=True,
    )
    provisioned = _provisioned(sandbox=sandbox)
    ModalProvisioner().sync_in(provisioned, workdir)

    assert provisioned.handle.remote_seen == {}
    sandbox.exec_fails = False
    ModalProvisioner().sync_out(provisioned, workdir)
    assert (workdir / "out.txt").read_bytes() == b"data"


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
