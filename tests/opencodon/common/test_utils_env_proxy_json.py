"""Characterization tests for previously-untested utils helpers.

Written ahead of the Phase 1 restructure move (docs/plans/
2026-08-01-repo-restructure-plan.md) so the move is validated against
pinned behavior: safe_json_loads, env_bool, normalize_proxy_url,
normalize_proxy_env_vars.
"""

import os

import pytest

from opencodon.common.utils import (
    env_bool,
    normalize_proxy_env_vars,
    normalize_proxy_url,
    safe_json_loads,
)


class TestSafeJsonLoads:
    def test_valid_json_parses(self):
        assert safe_json_loads('{"a": 1}') == {"a": 1}
        assert safe_json_loads("[1, 2]") == [1, 2]
        assert safe_json_loads("null") is None

    def test_invalid_json_returns_default(self):
        assert safe_json_loads("{not json", default={}) == {}
        assert safe_json_loads("", default="fallback") == "fallback"

    def test_non_string_input_returns_default(self):
        assert safe_json_loads(None, default=0) == 0
        assert safe_json_loads({"already": "parsed"}, default=None) is None

    def test_default_default_is_none(self):
        assert safe_json_loads("{bad") is None


class TestEnvBool:
    @pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on"])
    def test_truthy_strings(self, monkeypatch, raw):
        monkeypatch.setenv("OPENCODON_TEST_FLAG", raw)
        assert env_bool("OPENCODON_TEST_FLAG") is True

    @pytest.mark.parametrize("raw", ["0", "false", "no", "off", "garbage"])
    def test_falsey_and_unknown_strings(self, monkeypatch, raw):
        monkeypatch.setenv("OPENCODON_TEST_FLAG", raw)
        assert env_bool("OPENCODON_TEST_FLAG") is False

    def test_unset_var_is_false_even_with_default_true(self, monkeypatch):
        # Characterized quirk: env_bool reads os.getenv(key, "") — never None —
        # so is_truthy_value's default branch is unreachable and `default` has
        # no effect. Unset always yields False. If this is ever fixed, callers
        # passing default=True must be re-audited.
        monkeypatch.delenv("OPENCODON_TEST_FLAG", raising=False)
        assert env_bool("OPENCODON_TEST_FLAG") is False
        assert env_bool("OPENCODON_TEST_FLAG", default=True) is False


class TestNormalizeProxyUrl:
    def test_none_and_empty_return_none(self):
        assert normalize_proxy_url(None) is None
        assert normalize_proxy_url("") is None
        assert normalize_proxy_url("   ") is None

    def test_socks_alias_rewritten_to_socks5(self):
        assert normalize_proxy_url("socks://127.0.0.1:7890") == "socks5://127.0.0.1:7890"

    def test_socks_alias_case_insensitive(self):
        assert normalize_proxy_url("SOCKS://127.0.0.1:7890") == "socks5://127.0.0.1:7890"

    def test_other_schemes_pass_through(self):
        for url in ("http://proxy:8080", "https://proxy:8443", "socks5://h:1080"):
            assert normalize_proxy_url(url) == url

    def test_whitespace_stripped(self):
        assert normalize_proxy_url("  http://proxy:8080  ") == "http://proxy:8080"


class TestNormalizeProxyEnvVars:
    def test_rewrites_socks_alias_in_place(self, monkeypatch):
        monkeypatch.setenv("HTTPS_PROXY", "socks://127.0.0.1:7890")
        normalize_proxy_env_vars()
        assert os.environ["HTTPS_PROXY"] == "socks5://127.0.0.1:7890"

    def test_leaves_canonical_values_untouched(self, monkeypatch):
        monkeypatch.setenv("HTTP_PROXY", "http://proxy:8080")
        normalize_proxy_env_vars()
        assert os.environ["HTTP_PROXY"] == "http://proxy:8080"

    def test_unset_vars_stay_unset(self, monkeypatch):
        for key in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY",
                    "https_proxy", "http_proxy", "all_proxy"):
            monkeypatch.delenv(key, raising=False)
        normalize_proxy_env_vars()
        assert "HTTPS_PROXY" not in os.environ
        assert "all_proxy" not in os.environ

    def test_lowercase_variants_rewritten(self, monkeypatch):
        monkeypatch.setenv("all_proxy", "socks://10.0.0.1:1080")
        normalize_proxy_env_vars()
        assert os.environ["all_proxy"] == "socks5://10.0.0.1:1080"
