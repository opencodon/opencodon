"""Guardrail: dashboard plugins must NOT read the session token directly.

The dashboard host exposes a sanctioned, gated-mode-aware auth surface on the
plugin SDK (``window.__OPENCODON_PLUGIN_SDK__``): ``fetchJSON`` (JSON REST),
``authedFetch`` (uploads / blob downloads), and ``buildWsUrl`` /
``buildWsAuthParam`` (WebSockets). These handle BOTH dashboard auth modes —
loopback (``X-Hermes-Session-Token`` header) and gated OAuth
(``opencodon_session_at`` cookie / single-use ``?ticket=``).

Plugins that hand-roll ``fetch`` / ``WebSocket`` and read
``window.__OPENCODON_SESSION_TOKEN__`` directly send an empty token in gated mode
and 401/1008. That bug shipped in bundled plugins and was
invisible until the dashboard ran gated on hosted Fly agents.

This test fails if any bundled plugin's frontend reads the token global
directly, forcing new/edited plugins through the SDK surface instead. It is
the enforcement half of the "single sanctioned auth surface" design — the SDK
helpers are the carrot, this test is the stick.

If you have a legitimate reason to reference the token name (e.g. a comment
explaining why NOT to use it), add the file to ``_ALLOWED_FILES`` with a note.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

# Repo root: tests/plugins/<this file> → ../../
_REPO_ROOT = Path(__file__).resolve().parents[2]
_PLUGINS_DIR = _REPO_ROOT / "plugins"

# The forbidden global. Reading it directly bypasses the gated-mode auth path.
_FORBIDDEN = "__OPENCODON_SESSION_TOKEN__"

# Files explicitly allowed to mention the token (none today). Map path →
# reason so the allowance is self-documenting if one is ever needed.
_ALLOWED_FILES: dict[str, str] = {}


def _plugin_frontend_bundles() -> list[Path]:
    """Every plugin-shipped JS bundle the dashboard loads into the browser."""
    if not _PLUGINS_DIR.is_dir():
        return []
    # Plugin dashboards live at plugins/<name>/dashboard/dist/*.js
    return sorted(_PLUGINS_DIR.glob("*/dashboard/dist/*.js"))


def test_bundle_glob_resolves_plugin_names() -> None:
    """Sanity: when bundles exist, the glob must resolve their plugin names, so
    a layout change can't silently turn this guard into a no-op.

    No bundled plugin ships a ``dashboard/dist/`` frontend today — the last one
    went away with the kanban plugin. The guard is dormant rather than deleted:
    it re-arms on its own the moment a plugin dashboard lands again, which is
    exactly when the token-reading footgun comes back. The skip (rather than a
    hard assert) keeps that dormancy honest and visible in the test report.
    """
    bundles = _plugin_frontend_bundles()
    if not bundles:
        pytest.skip("no plugin ships a dashboard/dist frontend — guard dormant")
    names = {b.parent.parent.parent.name for b in bundles}
    assert names, "could not resolve plugin names from bundle paths"


@pytest.mark.parametrize(
    "bundle",
    _plugin_frontend_bundles(),
    ids=lambda p: str(p.relative_to(_REPO_ROOT)),
)
def test_plugin_bundle_does_not_read_session_token(bundle: Path) -> None:
    rel = str(bundle.relative_to(_REPO_ROOT))
    text = bundle.read_text(encoding="utf-8", errors="replace")

    if rel in _ALLOWED_FILES:
        return  # explicitly allowed (with a documented reason)

    # Only flag CODE reads of the token global, not mentions in ``//`` comments
    # (e.g. a comment explaining why the SDK helper is used instead). A line is
    # a code read if it contains the global and the global appears before any
    # ``//`` comment marker on that line.
    offending: list[str] = []
    for i, line in enumerate(text.splitlines(), start=1):
        idx = line.find(_FORBIDDEN)
        if idx == -1:
            continue
        comment_idx = line.find("//")
        in_comment = comment_idx != -1 and comment_idx < idx
        if not in_comment:
            offending.append(f"  {i}: {line.strip()}")

    if not offending:
        return

    pytest.fail(
        f"{rel} reads {_FORBIDDEN} directly — this bypasses gated-mode auth "
        f"and 401/1008s on OAuth-gated dashboards. Use the plugin SDK instead: "
        f"SDK.fetchJSON (JSON), SDK.authedFetch (uploads/downloads), or "
        f"SDK.buildWsUrl (WebSockets). Offending lines:\n" + "\n".join(offending)
    )
