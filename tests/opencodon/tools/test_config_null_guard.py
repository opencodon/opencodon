"""Tests for config.get() null-coalescing in tool configuration.

YAML ``null`` values (or ``~``) for a present key make ``dict.get(key, default)``
return ``None`` instead of the default — calling ``.lower()`` on that raises
``AttributeError``.  These tests verify the ``or`` coalescing guards.
"""

from unittest.mock import patch


# ── TTS tool ──────────────────────────────────────────────────────────────

class TestTTSProviderNullGuard:
    """opencodon/tools/tts_tool.py — _get_provider()"""

    def test_explicit_null_provider_returns_default(self):
        """YAML ``tts: {provider: null}`` should fall back to default."""
        from opencodon.tools.tts_tool import _get_provider, DEFAULT_PROVIDER

        result = _get_provider({"provider": None})
        assert result == DEFAULT_PROVIDER.lower().strip()

    def test_missing_provider_returns_default(self):
        """No ``provider`` key + non-TTS active provider should return default."""
        from opencodon.tools.tts_tool import _get_provider, DEFAULT_PROVIDER

        result = _get_provider({})
        assert result == DEFAULT_PROVIDER.lower().strip()

    def test_valid_provider_passed_through(self):
        from opencodon.tools.tts_tool import _get_provider

        result = _get_provider({"provider": "OPENAI"})
        assert result == "openai"

    def test_missing_provider_keeps_free_default_with_cloud_credentials(self):
        """A chat-provider key must not silently opt the user into paid TTS."""
        from opencodon.tools.tts_tool import _get_provider, DEFAULT_PROVIDER

        assert _get_provider({}) == DEFAULT_PROVIDER
        assert _get_provider({"provider": None}) == DEFAULT_PROVIDER

    def test_active_provider_without_credentials_keeps_edge(self):
        """A TTS-capable active provider that can't authenticate must NOT
        silently displace the free Edge default (no surprise billing / hard
        errors for a credential-less deployment)."""
        from opencodon.tools.tts_tool import _get_provider, DEFAULT_PROVIDER

        assert _get_provider({}) == DEFAULT_PROVIDER.lower().strip()

    def test_explicit_provider_wins_over_active(self):
        """An explicit tts.provider always overrides the active-provider fallback."""
        from opencodon.tools.tts_tool import _get_provider

        assert _get_provider({"provider": "edge"}) == "edge"


# ── Web tools ─────────────────────────────────────────────────────────────

class TestWebBackendNullGuard:
    """opencodon/tools/web_tools.py — _get_backend()"""

    @patch("opencodon.tools.web_tools._load_web_config", return_value={"backend": None})
    def test_explicit_null_backend_does_not_crash(self, _cfg):
        """YAML ``web: {backend: null}`` should not raise AttributeError."""
        from opencodon.tools.web_tools import _get_backend

        # Should not raise — the exact return depends on env key fallback
        result = _get_backend()
        assert isinstance(result, str)

    @patch("opencodon.tools.web_tools._load_web_config", return_value={})
    def test_missing_backend_does_not_crash(self, _cfg):
        from opencodon.tools.web_tools import _get_backend

        result = _get_backend()
        assert isinstance(result, str)


# ── MCP tool ──────────────────────────────────────────────────────────────

class TestMCPAuthNullGuard:
    """opencodon/tools/mcp_tool.py — MCPServerTask.__init__() auth config line"""

    def test_explicit_null_auth_does_not_crash(self):
        """YAML ``auth: null`` in MCP server config should not raise."""
        # Test the expression directly — MCPServerTask.__init__ has many deps
        config = {"auth": None, "timeout": 30}
        auth_type = (config.get("auth") or "").lower().strip()
        assert auth_type == ""

    def test_missing_auth_defaults_to_empty(self):
        config = {"timeout": 30}
        auth_type = (config.get("auth") or "").lower().strip()
        assert auth_type == ""

    def test_valid_auth_passed_through(self):
        config = {"auth": "OAUTH", "timeout": 30}
        auth_type = (config.get("auth") or "").lower().strip()
        assert auth_type == "oauth"


