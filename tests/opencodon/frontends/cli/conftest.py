"""Fixtures shared across opencodon_cli tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _suppress_concurrent_opencodon_gate(request, monkeypatch):
    """Default ``_detect_concurrent_opencodon_instances`` to ``[]`` for every test.

    The Windows update path now refuses to proceed when another
    ``opencodon.exe`` is detected (issue #26670). On a developer's Windows
    machine running the test suite via ``opencodon`` itself, this would
    flag the running agent as a concurrent instance and abort every
    ``cmd_update`` test. Tests that want to exercise the gate explicitly
    re-patch ``_detect_concurrent_opencodon_instances`` with their own
    return value — autouse here gives a clean default without touching
    the rest of the suite.

    Tests that need to call the REAL function (e.g. unit tests for the
    helper itself) opt out with ``@pytest.mark.real_concurrent_gate``.
    """
    if request.node.get_closest_marker("real_concurrent_gate"):
        return
    try:
        from opencodon.frontends.cli import main as _cli_main
    except Exception:
        return
    # raising=False: under pytest's per-test spawn isolation, a concurrent
    # xdist worker importing a module that transitively touches opencodon_cli.main
    # can briefly expose a partially-initialized module object here — one where
    # _detect_concurrent_opencodon_instances isn't defined yet. A bare setattr
    # would raise AttributeError and error the (unrelated) test. The attribute
    # always exists once main.py finishes importing, so a no-op when it's
    # transiently absent is the correct, race-free default.
    monkeypatch.setattr(
        _cli_main,
        "_detect_concurrent_opencodon_instances",
        lambda *_a, **_k: [],
        raising=False,
    )
