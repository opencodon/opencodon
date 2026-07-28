"""The provisioner seam — where a kernel runs is pluggable.

Everything above this seam (msg_id correlation, taint/restart, the
execution_log and lineage writes) is transport-agnostic, so a remote backend
supplies a provisioner and nothing else. These tests pin that: a custom
provisioner is honoured, its target is recorded per cell, and a cell that ran
elsewhere writes exactly the same rows as one that ran here.

The remote *targets* (Modal, SSH) are covered by integration tests that need a
configured backend; this file covers the seam itself, which does not.
"""

import pytest

from science.kernels import (
    KernelProvisioner,
    KernelSession,
    KernelStartError,
    LocalProvisioner,
    SessionKernelManager,
)
from science.blobstore import BlobStore
from science.bridge import bootstrap_kernel
from science.runtime import ScienceRuntime

from conftest import FakeKernelSession, FakeResolver


class FakeRemoteProvisioner(KernelProvisioner):
    """Stands in for Modal/SSH — names a target without needing one."""

    name = "modal"

    def __init__(self, app="opencodon-gpu"):
        self.app = app
        self.provisioned = 0

    def describe_target(self) -> str:
        return f"{self.name}:{self.app}"

    def provision(self, spec, workdir):
        self.provisioned += 1
        raise AssertionError("not reached: the double never calls provision()")


class FailingProvisioner(KernelProvisioner):
    name = "ssh"

    def describe_target(self) -> str:
        return "ssh:gpu-01.example.org"

    def provision(self, spec, workdir):
        raise KernelStartError(
            f"kernel failed to start on {self.describe_target()}: connection refused"
        )


def _runtime(tmp_path, db, provisioner):
    manager = SessionKernelManager(
        workspaces_root=tmp_path / "workspaces",
        resolvers={"python": FakeResolver()},
        bootstrap_fn=bootstrap_kernel,
        session_factory=FakeKernelSession,
        provisioner=provisioner,
    )
    runtime = ScienceRuntime(db, blobs=BlobStore(tmp_path / "blobs"), manager=manager)
    return runtime, manager


# ── SCI-P1-01 the seam ──────────────────────────────────────────────


@pytest.mark.requirement("SCI-P1-01")
def test_local_is_the_default_provisioner():
    session = KernelSession(FakeResolver().resolve(), workdir="/tmp/unused")
    assert isinstance(session._provisioner, LocalProvisioner)
    assert session.location == "local"


@pytest.mark.requirement("SCI-P1-01")
def test_a_custom_provisioner_reaches_the_session(tmp_path, db):
    remote = FakeRemoteProvisioner()
    runtime, manager = _runtime(tmp_path, db, remote)
    try:
        session, _ = manager.ensure_kernel("s1", "python")
        assert session.location == "modal:opencodon-gpu"
    finally:
        manager.shutdown()


@pytest.mark.requirement("SCI-P1-01")
def test_provisioner_interface_is_abstract(tmp_path):
    """A backend must implement provision() — there is no silent default."""
    with pytest.raises(NotImplementedError):
        KernelProvisioner().provision(FakeResolver().resolve(), tmp_path)


# ── SCI-P1-02 location is recorded ──────────────────────────────────


@pytest.mark.requirement("SCI-P1-02")
def test_local_cells_record_where_they_ran(science_runtime):
    result = science_runtime.run_cell("s1", "x = 1")
    row = science_runtime.store.get_cell(result["cell_id"])
    assert row["kernel_location"] == "local"


@pytest.mark.requirement("SCI-P1-02")
def test_remote_cells_record_the_target(tmp_path, db):
    runtime, manager = _runtime(tmp_path, db, FakeRemoteProvisioner("gpu-a100"))
    try:
        result = runtime.run_cell("s1", "x = 1")
        row = runtime.store.get_cell(result["cell_id"])
        # Not merely "remote" — which target, so a result computed on an A100
        # is distinguishable from one computed on this laptop.
        assert row["kernel_location"] == "modal:gpu-a100"
    finally:
        from science.host_bridge import shutdown_bridges

        shutdown_bridges()
        manager.shutdown()


# ── SCI-P1-03 convergence ───────────────────────────────────────────


@pytest.mark.requirement("SCI-P1-03")
def test_remote_cells_write_the_same_rows_as_local(tmp_path, db):
    """The convergence invariant: no field is populated only when local."""
    from science.host_bridge import shutdown_bridges

    source = "save_artifact('payload', 'out.txt')\nprint('done')"
    rows = {}
    for label, provisioner in [
        ("local", LocalProvisioner()),
        ("remote", FakeRemoteProvisioner("gpu-a100")),
    ]:
        runtime, manager = _runtime(tmp_path / label, db, provisioner)
        try:
            result = runtime.run_cell(f"s-{label}", source)
            rows[label] = (runtime.store.get_cell(result["cell_id"]), result)
        finally:
            shutdown_bridges()
            manager.shutdown()

    local_row, local_result = rows["local"]
    remote_row, remote_result = rows["remote"]

    # Same columns populated, ignoring the ones that legitimately differ.
    varying = {"id", "session_id", "kernel_id", "created_at", "kernel_location"}
    local_filled = {k for k, v in local_row.items() if v is not None} - varying
    remote_filled = {k for k, v in remote_row.items() if v is not None} - varying
    assert local_filled == remote_filled

    # And the artifact path works identically.
    assert len(local_result["artifacts"]) == len(remote_result["artifacts"]) == 1
    assert local_row["files_written"] == remote_row["files_written"]
    assert local_row["exit_status"] == remote_row["exit_status"] == "ok"


# ── SCI-P1-04 legible failure ───────────────────────────────────────


@pytest.mark.requirement("SCI-P1-04")
def test_a_provisioner_that_cannot_start_names_its_target():
    session = KernelSession(
        FakeResolver().resolve(), workdir="/tmp/unused",
        provisioner=FailingProvisioner(),
    )
    with pytest.raises(KernelStartError) as caught:
        session.start()

    message = str(caught.value)
    assert "ssh:gpu-01.example.org" in message
    assert "connection refused" in message


@pytest.mark.requirement("SCI-P1-04")
def test_a_failed_start_leaves_no_half_live_session():
    session = KernelSession(
        FakeResolver().resolve(), workdir="/tmp/unused",
        provisioner=FailingProvisioner(),
    )
    with pytest.raises(KernelStartError):
        session.start()
    # is_alive must stay False so the manager treats this as a taint and
    # retries cleanly rather than handing out a dead kernel.
    assert session.is_alive() is False
