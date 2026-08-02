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
import traceback as _traceback
from pathlib import Path

import pytest

# Populate the science host-service seam, as every production entry point
# does via the science toolset (tools/science_tools.py imports it first).
import opencodon.tools.science_host  # noqa: F401
from opencodon.state import SessionDB
from opencodon.science.blobstore import BlobStore
from opencodon.science.bridge import bootstrap_kernel
from opencodon.science.kernels import (
    EnvironmentSpec,
    ExecutionOutputs,
    SessionKernelManager,
)
from opencodon.science.runtime import ScienceRuntime


def _cell_traceback(exc: BaseException) -> tuple:
    """A Jupyter-shaped traceback for an exception raised by ``exec``.

    Real kernels return pre-rendered frames naming the cell as
    ``Cell In[n], line L``; ``exec`` frames carry the filename ``<string>``.
    Rendering them in the kernel's shape keeps the double honest for anything
    that reads line numbers back out of a traceback.
    """
    if isinstance(exc, SyntaxError) and exc.lineno:
        linenos = [exc.lineno]
    else:
        linenos = [
            frame.lineno
            for frame in _traceback.extract_tb(exc.__traceback__)
            if frame.filename == "<string>"
        ]
    frames = ["Traceback (most recent call last)"]
    frames += [f"Cell In[1], line {lineno}" for lineno in linenos]
    frames.append(f"{type(exc).__name__}: {exc}")
    return tuple(frames)


class FakeKernelSession:
    _counter = 0

    def __init__(self, spec, *, workdir, provisioner=None):
        self._spec = spec
        # Mirrors KernelSession: the double reports where it 'ran' so the
        # kernel_location column is exercised without a real remote.
        self.location = provisioner.describe_target() if provisioner else "local"
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
            out.traceback = _cell_traceback(exc)
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


class DisplayingKernelSession(FakeKernelSession):
    """A kernel that renders one inline figure per cell and saves nothing.

    Models the case the plain double cannot: rich display output exists in the
    protocol, is never forwarded to the model, and vanishes unless the cell
    explicitly saved an artifact.
    """

    def execute(self, source, *, timeout):
        out = super().execute(source, timeout=timeout)
        out.display.append({"data": {"image/png": "b64…"}, "metadata": {}})
        return out


@pytest.fixture
def db(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    yield db
    db.close()


def _runtime_with(tmp_path, db, session_factory):
    manager = SessionKernelManager(
        workspaces_root=tmp_path / "workspaces",
        resolvers={"python": FakeResolver()},
        bootstrap_fn=bootstrap_kernel,
        session_factory=session_factory,
    )
    blobs = BlobStore(tmp_path / "blobs")
    runtime = ScienceRuntime(db, blobs=blobs, manager=manager)
    yield runtime
    from opencodon.science.host_bridge import shutdown_bridges

    shutdown_bridges()
    manager.shutdown()


@pytest.fixture
def science_runtime(tmp_path, db):
    yield from _runtime_with(tmp_path, db, FakeKernelSession)


@pytest.fixture
def displaying_runtime(tmp_path, db):
    """``science_runtime``, but every cell also renders an unsaved figure."""
    yield from _runtime_with(tmp_path, db, DisplayingKernelSession)
