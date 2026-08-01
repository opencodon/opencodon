"""Unit tests for opencodon_cli.toolset_validation (see #38798).

Pure logic — the validity predicate is injected, so these tests need neither the
tool registry nor a running opencodon.
"""

import pytest

from opencodon.frontends.cli.toolset_validation import validate_platform_toolsets

# A representative set of real toolset names. `opencodon` is deliberately absent —
# that is the corruption #38798 reported (`opencodon-cli` rewritten to `opencodon`).
_KNOWN = {
    "opencodon-cli",
    "opencodon-telegram",
    "opencodon-discord",
    "terminal",
    "web",
}


def _is_valid(name):
    return name in _KNOWN


def test_valid_config_produces_no_warnings():
    cfg = {"cli": ["opencodon-cli"], "telegram": ["opencodon-telegram"]}
    assert validate_platform_toolsets(cfg, _is_valid) == []


def test_38798_corruption_warns_and_suggests_correct_name():
    # The exact reported shape: cli holds 'opencodon' instead of 'opencodon-cli'.
    warnings = validate_platform_toolsets({"cli": ["opencodon"]}, _is_valid)
    unknown = [w for w in warnings if "unknown toolset 'opencodon'" in w]
    assert len(unknown) == 1
    # Actionable: points at the valid name the entry should have been.
    assert "did you mean 'opencodon-cli'?" in unknown[0]
    # And the zero-valid-toolsets safety net fires.
    assert any("zero valid toolsets" in w for w in warnings)


def test_mixed_valid_and_invalid_flags_only_the_invalid():
    cfg = {"cli": ["opencodon-cli"], "discord": ["bogus"]}
    warnings = validate_platform_toolsets(cfg, _is_valid)
    # One valid entry exists, so no zero-valid warning.
    assert not any("zero valid toolsets" in w for w in warnings)
    assert len(warnings) == 1
    assert "platform 'discord'" in warnings[0]
    assert "unknown toolset 'bogus'" in warnings[0]


def test_unknown_without_valid_platform_default_omits_suggestion():
    # opencodon-mystery is not a known toolset, so no "did you mean" hint.
    warnings = validate_platform_toolsets({"mystery": ["nope"]}, _is_valid)
    unknown = [w for w in warnings if "unknown toolset 'nope'" in w]
    assert len(unknown) == 1
    assert "did you mean" not in unknown[0]


@pytest.mark.parametrize("value", [None, {}, [], "opencodon-cli", 42])
def test_non_dict_or_empty_yields_no_warnings(value):
    assert validate_platform_toolsets(value, _is_valid) == []


def test_scalar_toolset_value_is_accepted():
    # Some configs store the toolset as a bare string rather than a list.
    assert validate_platform_toolsets({"cli": "opencodon-cli"}, _is_valid) == []


def test_non_string_entries_are_skipped_not_counted_invalid():
    cfg = {"cli": [None, 123, "opencodon-cli"]}
    # The junk entries are ignored; the valid one keeps it from being "zero".
    assert validate_platform_toolsets(cfg, _is_valid) == []


def test_all_invalid_reports_each_and_the_zero_state():
    cfg = {"cli": ["opencodon"], "discord": ["opencodon"]}
    warnings = validate_platform_toolsets(cfg, _is_valid)
    assert sum("unknown toolset" in w for w in warnings) == 2
    assert any("zero valid toolsets" in w for w in warnings)


def test_real_validate_toolset_treats_opencodon_cli_valid_and_opencodon_invalid():
    # Ties the helper to reality: the canonical registry check agrees that
    # `opencodon-cli` is the real toolset and `opencodon` is not (the #38798 crux).
    from toolsets import validate_toolset

    assert validate_toolset("opencodon-cli") is True
    assert validate_toolset("opencodon") is False
    warnings = validate_platform_toolsets({"cli": ["opencodon"]}, validate_toolset)
    assert any("did you mean 'opencodon-cli'?" in w for w in warnings)
