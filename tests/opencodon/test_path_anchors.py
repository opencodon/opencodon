"""Repo-root anchors in moved modules must survive relocation.

Modules that compute the repo root from ``__file__`` break silently when a
restructure phase moves them (this bit twice on 2026-08-01: get_project_root
and the node-bootstrap script path). These invariants pin the *resolved*
locations, so any future move — including the Phase 3 src/ flip — fails here
instead of in production.
"""

from pathlib import Path


def test_get_project_root_is_the_repo_checkout():
    from opencodon.config import get_project_root

    root = get_project_root()
    # The repo root is identified by its packaging metadata, not by name.
    assert (root / "pyproject.toml").is_file()
    assert (root / "opencodon").is_dir()


def test_install_method_project_root_matches_get_project_root():
    from opencodon.config import _install_method_project_root, get_project_root

    assert _install_method_project_root(None) == get_project_root()


def test_node_bootstrap_script_path_resolves_to_real_file():
    from opencodon.common import constants

    assert constants._NODE_BOOTSTRAP_SCRIPT.is_file(), (
        f"node-bootstrap.sh not found at {constants._NODE_BOOTSTRAP_SCRIPT} — "
        "a module move likely broke the __file__-relative repo-root anchor"
    )


def test_bundled_plugins_dir_resolves_to_real_directory():
    from opencodon.plugins_runtime import get_bundled_plugins_dir

    d = get_bundled_plugins_dir()
    assert d.is_dir(), f"bundled plugins dir missing at {d}"
    assert (d / "model-providers").is_dir()


def test_platform_plugin_env_var_injection_sees_real_plugins_dir():
    # The injector walks <repo>/plugins/platforms/*/plugin.yaml; a wrong root
    # makes it a silent no-op. Assert the path it derives actually exists.
    from opencodon.config import get_project_root

    assert (get_project_root() / "plugins" / "platforms").is_dir()


def test_core_repo_root_anchors_resolve():
    # agent/ -> opencodon/core/ move (Phase 3a) changed __file__ depth; these
    # three modules compute repo-level paths from __file__.
    from opencodon.core import runtime_cwd, i18n
    from opencodon.config import get_project_root

    root = get_project_root()
    assert runtime_cwd._PACKAGE_ROOT == root
    assert (root / "locales").is_dir()
