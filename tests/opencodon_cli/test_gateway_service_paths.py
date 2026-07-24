from unittest.mock import patch


def test_service_path_skips_nonexistent_node_modules(tmp_path):
    """Service PATH should not include node_modules/.bin if it doesn't exist."""
    from opencodon_cli.gateway import _build_service_path_dirs
    with patch("opencodon_cli.gateway.get_opencodon_home", return_value=tmp_path / ".opencodon"):
        dirs = _build_service_path_dirs(project_root=tmp_path)
    node_modules_bin = str(tmp_path / "node_modules" / ".bin")
    assert node_modules_bin not in dirs


def test_service_path_includes_node_modules_when_present(tmp_path):
    """Service PATH should include node_modules/.bin when it exists."""
    nm_bin = tmp_path / "node_modules" / ".bin"
    nm_bin.mkdir(parents=True)
    from opencodon_cli.gateway import _build_service_path_dirs
    with patch("opencodon_cli.gateway.get_opencodon_home", return_value=tmp_path / ".opencodon"):
        dirs = _build_service_path_dirs(project_root=tmp_path)
    assert str(nm_bin) in dirs


def test_service_path_includes_opencodon_home_node_modules(tmp_path):
    """Service PATH should include ~/.opencodon/node_modules/.bin when it exists."""
    hermes_nm = tmp_path / ".opencodon" / "node_modules" / ".bin"
    hermes_nm.mkdir(parents=True)
    from opencodon_cli.gateway import _build_service_path_dirs
    with patch("opencodon_cli.gateway.get_opencodon_home", return_value=tmp_path / ".opencodon"):
        dirs = _build_service_path_dirs(project_root=tmp_path)
    assert str(hermes_nm) in dirs
