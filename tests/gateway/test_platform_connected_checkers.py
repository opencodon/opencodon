"""
Verify that every gateway platform — built-in and plugin — has a connection
checker so ``GatewayConfig.get_connected_platforms()`` doesn't silently drop
platforms with bespoke auth requirements.
"""

from unittest.mock import MagicMock

import pytest

from gateway.config import Platform, _PLATFORM_CONNECTED_CHECKERS, _BUILTIN_PLATFORM_VALUES


def test_all_builtins_have_checker_or_generic_token_path():
    """Every built-in Platform member must be reachable by either:

    1. The generic ``config.token or config.api_key`` check, OR
    2. A platform-specific entry in ``_PLATFORM_CONNECTED_CHECKERS``.

    This guarantees ``get_connected_platforms()`` doesn't silently ignore
    a built-in just because nobody added it to the checker dict.
    """
    # Platforms covered by the generic token/api_key branch
    generic_token_values = {p.value for p in {
        Platform.TELEGRAM,
        Platform.DISCORD,
        Platform.SLACK,
    }}

    # Platforms with a bespoke checker
    checker_values = {p.value for p in set(_PLATFORM_CONNECTED_CHECKERS.keys())}

    # Platforms whose connection check now comes from a registered plugin entry
    # (is_connected / validate_config).  Several adapters migrated out of core
    # into bundled plugins (#41112); their checker moved with them to the
    # platform registry, so get_connected_platforms() resolves them via the
    # registry fallback rather than _PLATFORM_CONNECTED_CHECKERS.
    plugin_checker_values: set[str] = set()
    try:
        from opencodon.plugins_runtime import discover_plugins
        from gateway.platform_registry import platform_registry
        discover_plugins()
        for _entry in platform_registry.all_entries():
            if _entry.is_connected is not None or _entry.validate_config is not None:
                plugin_checker_values.add(_entry.name)
    except Exception:
        pass

    # Every built-in should be in one of the sets
    all_builtins = set(_BUILTIN_PLATFORM_VALUES)
    # Retired platforms (fork cut): enum members kept for stale-config
    # parsing, but their adapters and checkers were removed.
    retired = {
        "signal", "mattermost", "matrix", "homeassistant", "email", "sms",
        "dingtalk", "msgraph_webhook", "feishu", "wecom", "wecom_callback",
        "weixin", "bluebubbles", "qqbot", "yuanbao",
    }
    missing = (
        all_builtins
        - generic_token_values
        - checker_values
        - plugin_checker_values
        - retired
        - {"local"}
    )

    assert not missing, (
        f"Built-in platforms missing a connection checker: "
        f"{sorted(missing)}.  "
        f"Add them to _PLATFORM_CONNECTED_CHECKERS or generic_token_platforms."
    )


@pytest.mark.parametrize("platform, checker", list(_PLATFORM_CONNECTED_CHECKERS.items()))
def test_checker_handles_minimal_config(platform, checker):
    """Each bespoke checker must not crash on a minimal PlatformConfig."""
    mock_config = MagicMock()
    mock_config.extra = {}
    mock_config.token = None
    mock_config.api_key = None
    mock_config.enabled = True

    # Should return a bool without raising
    result = checker(mock_config)
    assert isinstance(result, bool)


@pytest.mark.parametrize("platform, checker", list(_PLATFORM_CONNECTED_CHECKERS.items()))
def test_checker_returns_true_when_configured(platform, checker, monkeypatch):
    """Each bespoke checker must return True when the config looks valid."""
    mock_config = MagicMock()
    mock_config.token = None
    mock_config.api_key = None
    mock_config.enabled = True

    # Set up platform-specific mock extra fields so the checker succeeds
    if platform == Platform.WHATSAPP_CLOUD:
        mock_config.extra = {"phone_number_id": "1", "access_token": "t"}
    elif platform in {
        Platform.API_SERVER,
        Platform.WEBHOOK,
        Platform.WHATSAPP,
    }:
        mock_config.extra = {}
    elif platform == Platform.RELAY:
        mock_config.extra = {"relay_url": "wss://connector.example/relay"}
    else:
        pytest.skip(f"No synthetic config defined for {platform.value}")

    result = checker(mock_config)
    assert result is True, f"{platform.value} checker should return True with valid-looking config"
