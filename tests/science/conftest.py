"""Shared fixtures for science-layer tests.

FakeKernelSession exec()s cell source in-process in a persistent namespace.
The real SDK bootstrap (science/bridge.py) is plain stdlib Python, so it runs
unchanged — the cell.json/journal filesystem contract and every store write
are exercised for real; only the Jupyter transport is skipped. Real-kernel
coverage lives in test_kernel_integration.py.
"""

import contextlib
import io
import os
from pathlib import Path

import pytest

from opencodon_state import SessionDB
from science.blobstore import BlobStore
from science.bridge import bootstrap_kernel
from science.kernels import (
    EnvironmentSpec,
    ExecutionOutputs,
    SessionKernelManager,
)
from science.runtime import ScienceRuntime


class FakeKernelSession:
    _counter = 0

    def __init__(self, spec, *, workdir):
        self._spec = spec
        self._workdir = Path(workdir)
        FakeKernelSession._counter += 1
        self.kernel_id = f"fake-{FakeKernelSession._counter}"
        self._ns = {}
        self._alive = False

    @property
    def spec(self):
        return self._spec

    @property
    def workdir(self):
        return self._workdir

    def start(self):
        self._workdir.mkdir(parents=True, exist_ok=True)
        self._alive = True

    def is_alive(self):
        return self._alive

    def execute(self, source, *, timeout):
        out = ExecutionOutputs(status="ok")
        stdout, stderr = io.StringIO(), io.StringIO()
        cwd = os.getcwd()
        try:
            os.chdir(self._workdir)
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                exec(source, self._ns)  # noqa: S102 - test double
        except BaseException as exc:  # mirror kernel error capture
            out.status = "error"
            out.error_name = type(exc).__name__
            out.error_value = str(exc)
        finally:
            os.chdir(cwd)
        out.stdout = stdout.getvalue()
        out.stderr = stderr.getvalue()
        return out

    def interrupt(self):
        pass

    def shutdown(self):
        self._alive = False


class FakeResolver:
    def available(self):
        return True

    def resolve(self):
        return EnvironmentSpec(
            language="python",
            interpreter_path="fake",
            argv=("fake",),
            runtime_identity="fake-python",
        )

    def snapshot(self):
        return '{"language": "python", "runtime": "fake"}'


@pytest.fixture
def db(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    yield db
    db.close()


@pytest.fixture
def science_runtime(tmp_path, db):
    manager = SessionKernelManager(
        workspaces_root=tmp_path / "workspaces",
        resolvers={"python": FakeResolver()},
        bootstrap_fn=bootstrap_kernel,
        session_factory=FakeKernelSession,
    )
    blobs = BlobStore(tmp_path / "blobs")
    runtime = ScienceRuntime(db, blobs=blobs, manager=manager)
    yield runtime
    from science.host_bridge import shutdown_bridges

    shutdown_bridges()
    manager.shutdown()
