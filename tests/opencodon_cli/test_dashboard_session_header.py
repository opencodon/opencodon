"""The loopback session-token header the SPA actually sends.

Regression guard for a rebrand split: ``web/src/lib/api.ts`` was renamed to
send ``X-Opencodon-Session-Token`` while ``web_server`` kept checking
``X-Hermes-Session-Token``, so every non-public ``/api`` route 401'd for any
bundle built after the rename — the whole loopback dashboard, not one page.

The names are asserted as literals on purpose. Reading the constant from the
module would pass whatever it happens to say; the contract is with the shipped
bundle and with older clients, so both strings are pinned here.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from opencodon_cli import web_server

CURRENT_HEADER = "X-Opencodon-Session-Token"
LEGACY_HEADER = "X-Hermes-Session-Token"


@pytest.fixture
def client():
    """A client in loopback mode, whatever ran before this test.

    ``app`` is module-level and shared, and the loopback token check is
    skipped entirely when ``auth_required`` is set — so a test that engaged
    the OAuth gate earlier in the process would otherwise make these assert
    the wrong path.
    """
    previous = getattr(web_server.app.state, "auth_required", False)
    web_server.app.state.auth_required = False
    yield TestClient(web_server.app)
    web_server.app.state.auth_required = previous


class TestSessionHeaderNames:
    def test_spa_sends_the_header_the_server_checks(self):
        """The bundle's header name and the server's must not drift again."""
        api_ts = Path(__file__).resolve().parents[2] / "web" / "src" / "lib" / "api.ts"
        source = api_ts.read_text(encoding="utf-8")
        match = re.search(r'SESSION_HEADER\s*=\s*"([^"]+)"', source)
        assert match, "web/src/lib/api.ts no longer declares SESSION_HEADER"
        assert match.group(1) == CURRENT_HEADER
        assert web_server._SESSION_HEADER_NAME == CURRENT_HEADER

    def test_current_header_authenticates(self, client):
        resp = client.get(
            "/api/config", headers={CURRENT_HEADER: web_server._SESSION_TOKEN}
        )
        assert resp.status_code != 401

    def test_legacy_header_still_authenticates(self, client):
        """Older bundles and the desktop app keep working."""
        resp = client.get(
            "/api/config", headers={LEGACY_HEADER: web_server._SESSION_TOKEN}
        )
        assert resp.status_code != 401

    def test_bearer_still_authenticates(self, client):
        resp = client.get(
            "/api/config",
            headers={"Authorization": f"Bearer {web_server._SESSION_TOKEN}"},
        )
        assert resp.status_code != 401

    def test_missing_token_is_still_rejected(self, client):
        assert client.get("/api/config").status_code == 401

    def test_wrong_token_is_still_rejected(self, client):
        resp = client.get("/api/config", headers={CURRENT_HEADER: "not-the-token"})
        assert resp.status_code == 401

    def test_science_routes_are_gated_the_same_way(self, client):
        """The science surface must not be reachable without the token."""
        assert client.get("/api/science/frames").status_code == 401
        resp = client.get(
            "/api/science/frames",
            headers={CURRENT_HEADER: web_server._SESSION_TOKEN},
        )
        assert resp.status_code != 401
