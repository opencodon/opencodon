"""Behavior contract for the weekly upstream-triage classifier (FORK-PLAN)."""

import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "upstream_triage",
    Path(__file__).resolve().parents[2] / "scripts" / "upstream_triage.py",
)
triage = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(triage)

TRACKED = {
    "uv.lock",
    "pyproject.toml",
    "opencodon_cli/auth.py",
    "agent/agent.py",
    "tools/file_tools.py",
}


class TestClassify:
    def test_cut_subsystem_is_not_applicable(self):
        """Commits touching only files we deleted are skipped entirely."""
        assert (
            triage.classify("fix(spotify): token refresh", ["plugins/spotify/client.py"], TRACKED)
            == "N/A"
        )

    def test_security_keyword_wins(self):
        assert (
            triage.classify("fix: path traversal in file tools", ["tools/file_tools.py"], TRACKED)
            == "SECURITY"
        )

    def test_pin_change_is_security(self):
        """Dependency-pin changes are always same-week adopts — we own CVE response."""
        assert triage.classify("chore: bump deps", ["uv.lock"], TRACKED) == "SECURITY"

    def test_provider_surface(self):
        assert (
            triage.classify("feat: add new provider", ["opencodon_cli/auth.py"], TRACKED)
            == "PROVIDER"
        )

    def test_provider_directory_prefix_even_if_file_is_new(self):
        """New files under kept provider dirs count as provider churn."""
        assert (
            triage.classify(
                "feat: add provider plugin", ["plugins/model_providers/newco/__init__.py"], TRACKED
            )
            == "PROVIDER"
        )

    def test_bugfix_in_kept_code(self):
        assert (
            triage.classify("fix(agent): off-by-one in retry", ["agent/agent.py"], TRACKED)
            == "BUGFIX"
        )

    def test_feature_never_auto_adopted_bucket(self):
        assert (
            triage.classify("feat: shiny new thing", ["agent/agent.py"], TRACKED) == "FEATURE"
        )

    def test_upstream_module_paths_translate_to_fork_names(self):
        """Upstream still says hermes_cli/; our tree says opencodon_cli/.
        Classification must not N/A commits touching renamed modules."""
        assert (
            triage.classify("fix: auth retry", ["hermes_cli/auth.py"], TRACKED)
            == "PROVIDER"
        )
        assert triage.to_fork_path("tests/hermes_cli/test_auth.py") == (
            "tests/opencodon_cli/test_auth.py"
        )

    def test_mixed_cut_and_kept_uses_kept_files(self):
        """A commit spanning cut + kept paths is judged on the kept files only."""
        assert (
            triage.classify(
                "fix: shared helper",
                ["plugins/spotify/client.py", "agent/agent.py"],
                TRACKED,
            )
            == "BUGFIX"
        )
