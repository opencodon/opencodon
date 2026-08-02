"""The host-service seam: science's only channel to the layers above it.

The science layer may not import core/tools (layering: frontends →
core/tools → science → state/config → common). These tests pin the seam's
contract: registered services round-trip, unregistered access fails with a
pointer to the registration module, and importing the tools-layer
registration module populates every service the science code paths use.
"""

import pytest

from opencodon.science import hostinfra


@pytest.fixture
def clean_seam(monkeypatch):
    """An empty service registry, restored afterwards."""
    monkeypatch.setattr(hostinfra, "_SERVICES", {})


def test_unregistered_service_raises_lookup_error_naming_the_fix(clean_seam):
    with pytest.raises(LookupError) as exc:
        hostinfra.skills_dir()
    assert "skills_dir" in str(exc.value)
    assert "opencodon.tools.science_host" in str(exc.value)


def test_registered_service_round_trips(clean_seam):
    calls = []
    hostinfra.register_host_services(
        dispatch_tool=lambda name, args, task_id=None: calls.append(
            (name, args, task_id)
        )
        or "ok"
    )
    assert hostinfra.dispatch_tool("web_search", {"q": "x"}) == "ok"
    assert calls == [("web_search", {"q": "x"}, None)]


def test_unknown_service_name_is_rejected(clean_seam):
    with pytest.raises(TypeError):
        hostinfra.register_host_services(not_a_service=lambda: None)


def test_tools_registration_module_populates_every_service(clean_seam):
    import importlib

    import opencodon.tools.science_host as science_host

    importlib.reload(science_host)  # clean_seam wiped its import-time work
    for name in hostinfra._KNOWN:
        assert callable(hostinfra._SERVICES.get(name)), name


def test_kernels_available_degrades_when_seam_is_empty(clean_seam, monkeypatch):
    """Without the tools layer nothing could lazy-install, so the gate is
    installed-or-nothing rather than an exception."""
    from opencodon.science import kernels

    monkeypatch.setattr(kernels, "kernels_installed", lambda: False)
    assert kernels.kernels_available() is False
