"""Real-kernel integration: the full path through jupyter_client/ipykernel.

Skipped when the science extra is not installed. The R test additionally
requires an R interpreter with IRkernel.
"""

import pytest

from science.blobstore import BlobStore
from science.bridge import bootstrap_kernel
from science.kernels import RKernelResolver, SessionKernelManager, kernels_installed
from science.runtime import ScienceRuntime

# kernels_installed, not kernels_available: the latter is the schema gate and
# reads True whenever the stack could be lazy-installed, which would run these
# against an environment that doesn't actually have a kernel.
pytestmark = pytest.mark.skipif(
    not kernels_installed(), reason="jupyter kernel stack not installed"
)


@pytest.fixture
def real_runtime(tmp_path, db):
    manager = SessionKernelManager(
        workspaces_root=tmp_path / "workspaces",
        bootstrap_fn=bootstrap_kernel,
    )
    runtime = ScienceRuntime(db, blobs=BlobStore(tmp_path / "blobs"), manager=manager)
    yield runtime
    from science.host_bridge import shutdown_bridges

    shutdown_bridges()
    manager.shutdown()


class TestRealPythonKernel:
    def test_persistent_state_artifacts_and_reproduce(self, real_runtime, db):
        db.create_session("s1", source="cli")
        first = real_runtime.run_cell("s1", "acc = [1, 2, 3]\nprint('ready')")
        assert first["status"] == "ok"
        assert "ready" in first["stdout"]

        # State survives across cells in the live kernel.
        second = real_runtime.run_cell(
            "s1", "save_artifact(str(sum(acc)), 'total.txt')\nprint(sum(acc))"
        )
        assert second["status"] == "ok"
        assert "6" in second["stdout"]
        [artifact] = second["artifacts"]
        assert real_runtime.blobs.read_bytes(artifact["sha256"]) == b"6"

        # Reproduce replays both cells in a fresh kernel and byte-matches.
        from science.reproduce import reproduce

        report = reproduce(artifact["version_id"], runtime=real_runtime)
        assert report["claim"] == "reproduced"

    @pytest.mark.requirement("SCI-P0-01", "SCI-P0-02")
    def test_failure_location_from_a_real_traceback(self, real_runtime, db):
        """The line number is read back out of IPython's own rendering.

        The doubles in conftest emit a traceback in the kernel's *shape*; only
        this test proves the parser survives what ipykernel actually sends —
        current frame format, ANSI colouring and all.
        """
        db.create_session("s-tb", source="cli")
        result = real_runtime.run_cell(
            "s-tb",
            "import json\n"
            "def parse(blob):\n"
            "    return json.loads(blob)\n"
            "\n"
            "parse('{not json')\n",
        )

        assert result["status"] == "error"
        row = real_runtime.store.get_cell(result["cell_id"])
        # Three frames are in play: the call at line 5, the cell-defined
        # helper at line 3, and the raise itself deep inside json/decoder.py.
        # The innermost *cell* line wins — line 3 names the statement that
        # actually broke, which is what a reader needs, while the library
        # frame is correctly ignored as not being the cell's code.
        assert row["error_lineno"] == 3
        assert "JSONDecodeError" in row["traceback"]
        assert "decoder.py" in row["traceback"], "library frames still recorded"
        assert "\x1b" not in row["traceback"]

    def test_timeout_taints_and_restarts_kernel(self, real_runtime, db):
        db.create_session("s2", source="cli")
        real_runtime.run_cell("s2", "x = 'survives?'")
        timed_out = real_runtime.run_cell(
            "s2", "import time\ntime.sleep(60)", timeout=3
        )
        assert timed_out["status"] == "timeout"
        assert timed_out.get("kernel_restarted") is True

        # Next cell runs in a fresh kernel: old state is gone by design.
        after = real_runtime.run_cell("s2", "print('x' in dir())")
        assert after["status"] == "ok"
        assert "False" in after["stdout"]
        assert after["fresh_kernel"] is True
        assert after["kernel_id"] != timed_out["kernel_id"]

    def test_error_traceback_captured(self, real_runtime, db):
        db.create_session("s3", source="cli")
        result = real_runtime.run_cell("s3", "1 / 0")
        assert result["status"] == "error"
        assert result["error"]["name"] == "ZeroDivisionError"


@pytest.mark.skipif(
    not RKernelResolver().available(),
    reason="R + IRkernel not installed",
)
class TestRealRKernel:
    def test_r_cell_and_artifact(self, real_runtime, db):
        db.create_session("r1", source="cli")
        result = real_runtime.run_cell(
            "r1",
            'save_artifact(paste(sum(1:10)), "rsum.txt")\ncat(sum(1:10))',
            language="r",
            timeout=120,
        )
        assert result["status"] == "ok"
        [artifact] = result["artifacts"]
        assert real_runtime.blobs.read_bytes(artifact["sha256"]).strip() == b"55"
