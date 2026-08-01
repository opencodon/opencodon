"""Tests for empty model fallback — when provider is configured but model is missing."""

from unittest.mock import patch


class TestGetDefaultModelForProvider:
    """Unit tests for opencodon_cli.models.get_default_model_for_provider."""

    def test_known_provider_returns_first_model(self):
        from opencodon.frontends.cli.models import get_default_model_for_provider
        result = get_default_model_for_provider("openai-codex")
        # Should return first model from _PROVIDER_MODELS["openai-codex"]
        assert result
        assert isinstance(result, str)

    def test_openrouter_returns_preferred_silent_default(self):
        """OpenRouter has no static catalog (live fetch), but the silent
        default must still resolve — to the cost-safe preferred model, never
        the curated list's Anthropic flagship (claude-fable-5)."""
        from opencodon.frontends.cli.models import (
            PREFERRED_SILENT_DEFAULT_MODEL,
            get_default_model_for_provider,
        )
        result = get_default_model_for_provider("openrouter")
        assert result == PREFERRED_SILENT_DEFAULT_MODEL
        assert "claude" not in result.lower()

    def test_unknown_provider_returns_empty(self):
        from opencodon.frontends.cli.models import get_default_model_for_provider
        assert get_default_model_for_provider("nonexistent-provider") == ""

    def test_custom_provider_returns_empty(self):
        """Custom provider has no model catalog — should return empty."""
        from opencodon.frontends.cli.models import get_default_model_for_provider
        # Custom providers don't have entries in _PROVIDER_MODELS
        assert get_default_model_for_provider("some-random-custom") == ""


    def test_catalog_label_overrides_constant(self):
        """A ``"default": true`` label in the cached catalog manifest wins over
        the in-repo constant, so maintainers can rotate the silent default
        without shipping a release."""
        from unittest.mock import patch

        from opencodon.frontends.cli import models as models_mod

        with patch(
            "opencodon.frontends.cli.model_catalog.get_default_model_from_cache",
            return_value="qwen/qwen3.7-plus",
        ):
            assert (
                models_mod.get_preferred_silent_default_model("openrouter")
                == "qwen/qwen3.7-plus"
            )
            # The OpenRouter catalog carries qwen3.7-plus, so the full
            # resolver follows the label.
            assert (
                models_mod.get_default_model_for_provider("openrouter")
                == "qwen/qwen3.7-plus"
            )

    def test_no_catalog_cache_falls_back_to_constant(self):
        """With no cached manifest (fresh install / offline), the in-repo
        constant is the silent default."""
        from unittest.mock import patch

        from opencodon.frontends.cli import models as models_mod

        with patch(
            "opencodon.frontends.cli.model_catalog.get_default_model_from_cache",
            return_value=None,
        ):
            assert (
                models_mod.get_preferred_silent_default_model("openrouter")
                == models_mod.PREFERRED_SILENT_DEFAULT_MODEL
            )

    def test_stale_label_not_in_catalog_falls_back(self, monkeypatch):
        """If the labeled default is no longer in the provider's curated
        catalog, fall back to entry [0] rather than returning an absent id.

        Only reachable for a provider that has BOTH a static curated list and
        silent-default status, so the static list is injected here rather than
        depending on whichever shipped provider happens to satisfy both.
        """
        from unittest.mock import patch

        from opencodon.frontends.cli import models as models_mod

        monkeypatch.setitem(
            models_mod._PROVIDER_MODELS, "openrouter", ["a/first", "b/second"]
        )
        with patch(
            "opencodon.frontends.cli.model_catalog.get_default_model_from_cache",
            return_value="does-not-exist-model",
        ):
            assert models_mod.get_default_model_for_provider("openrouter") == "a/first"


class TestDetectStaticProviderCostSafeDefault:
    """detect_static_provider_for_model must apply the same cost-safe default
    as get_default_model_for_provider when a bare provider name is typed as a
    model (e.g. ``/model nous``)."""


    def test_provider_without_override_still_uses_first_model(self):
        """Providers outside _SILENT_DEFAULT_PROVIDERS are unchanged."""
        from opencodon.frontends.cli.models import (
            _PROVIDER_MODELS,
            _SILENT_DEFAULT_PROVIDERS,
            detect_static_provider_for_model,
        )

        for provider in ("anthropic", "xai"):
            if provider in _SILENT_DEFAULT_PROVIDERS:
                continue
            result = detect_static_provider_for_model(provider, "openrouter")
            assert result is not None
            assert result[1] == _PROVIDER_MODELS[provider][0]


class TestGatewayEmptyModelFallback:
    """Test that _resolve_session_agent_runtime fills in empty model from provider catalog."""

    def test_empty_model_filled_from_provider(self):
        """When config has no model but provider is openai-codex, use first codex model."""
        from opencodon.frontends.gateway.run import GatewayRunner

        runner = object.__new__(GatewayRunner)
        runner._session_model_overrides = {}

        # Mock _resolve_gateway_model to return empty string
        # Mock _resolve_runtime_agent_kwargs to return openai-codex provider
        with patch("opencodon.frontends.gateway.run._resolve_gateway_model", return_value=""), \
             patch("opencodon.frontends.gateway.run._resolve_runtime_agent_kwargs", return_value={
                 "provider": "openai-codex",
                 "api_key": "test-key",
                 "base_url": "https://chatgpt.com/backend-api/codex",
                 "api_mode": "codex_responses",
             }):
            model, kwargs = runner._resolve_session_agent_runtime()

        # Model should have been filled in from provider catalog
        assert model, "Model should not be empty when provider is known"
        assert isinstance(model, str)
        assert kwargs["provider"] == "openai-codex"

    def test_nonempty_model_not_overridden(self):
        """When config has a model set, don't override it."""
        from opencodon.frontends.gateway.run import GatewayRunner

        runner = object.__new__(GatewayRunner)
        runner._session_model_overrides = {}

        with patch("opencodon.frontends.gateway.run._resolve_gateway_model", return_value="gpt-5.4"), \
             patch("opencodon.frontends.gateway.run._resolve_runtime_agent_kwargs", return_value={
                 "provider": "openai-codex",
                 "api_key": "test-key",
                 "base_url": "https://chatgpt.com/backend-api/codex",
                 "api_mode": "codex_responses",
             }):
            model, kwargs = runner._resolve_session_agent_runtime()

        assert model == "gpt-5.4", "Explicit model should not be overridden"

    def test_empty_model_no_provider_stays_empty(self):
        """When both model and provider are empty, model stays empty."""
        from opencodon.frontends.gateway.run import GatewayRunner

        runner = object.__new__(GatewayRunner)
        runner._session_model_overrides = {}

        with patch("opencodon.frontends.gateway.run._resolve_gateway_model", return_value=""), \
             patch("opencodon.frontends.gateway.run._resolve_runtime_agent_kwargs", return_value={
                 "provider": "",
                 "api_key": "test-key",
                 "base_url": "https://example.com",
                 "api_mode": "chat_completions",
             }):
            model, kwargs = runner._resolve_session_agent_runtime()

        # Can't fill in a default without knowing the provider
        assert model == ""


class TestResolveGatewayModel:
    """Test _resolve_gateway_model reads model from config correctly."""

    def test_returns_default_key(self):
        from opencodon.frontends.gateway.run import _resolve_gateway_model
        assert _resolve_gateway_model({"model": {"default": "gpt-5.4"}}) == "gpt-5.4"

    def test_returns_model_key_fallback(self):
        from opencodon.frontends.gateway.run import _resolve_gateway_model
        assert _resolve_gateway_model({"model": {"model": "gpt-5.4"}}) == "gpt-5.4"

    def test_returns_empty_when_missing(self):
        from opencodon.frontends.gateway.run import _resolve_gateway_model
        assert _resolve_gateway_model({"model": {}}) == ""

    def test_returns_empty_when_no_model_section(self):
        from opencodon.frontends.gateway.run import _resolve_gateway_model
        assert _resolve_gateway_model({}) == ""

    def test_string_model_config(self):
        from opencodon.frontends.gateway.run import _resolve_gateway_model
        assert _resolve_gateway_model({"model": "my-model"}) == "my-model"
