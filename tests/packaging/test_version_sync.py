"""Tests for the release version-sync guard.

These build synthetic repos in a tmp_path rather than asserting anything about
this repo's real package.json files — the point is the guard's behavior, not a
snapshot of the current version.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from check_version_sync import VersionError, check, normalize_expected  # noqa: E402


def write_repo(
    root: Path,
    *,
    py: str = "1.2.3",
    module: str = "1.2.3",
    npm: dict[str, str] | None = None,
    workspaces: list[str] | None = None,
) -> Path:
    """A minimal repo with the same version sources as the real one."""
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "opencodon"\nversion = "{py}"\n\n[build-system]\nversion = "99.0.0"\n',
        encoding="utf-8",
    )

    version_py = root / "src" / "opencodon" / "common"
    version_py.mkdir(parents=True)
    (version_py / "version.py").write_text(
        f'__version__ = "{module}"\n__release_date__ = "2026.8.4"\n', encoding="utf-8"
    )

    manifest: dict[str, object] = {"name": "opencodon", "version": py}
    if workspaces is not None:
        manifest["workspaces"] = workspaces
    (root / "package.json").write_text(json.dumps(manifest), encoding="utf-8")

    for rel, version in (npm or {}).items():
        member = root / rel
        member.mkdir(parents=True, exist_ok=True)
        (member / "package.json").write_text(
            json.dumps({"name": rel, "version": version}), encoding="utf-8"
        )

    return root


def test_agreeing_sources_pass(tmp_path: Path) -> None:
    write_repo(tmp_path, npm={"apps/client": "1.2.3"}, workspaces=["apps/*"])

    versions = check(tmp_path)

    assert set(versions.values()) == {"1.2.3"}
    assert Path("apps/client/package.json") in versions


def test_python_line_out_of_sync_fails(tmp_path: Path) -> None:
    write_repo(tmp_path, py="1.2.3", module="1.2.2")

    with pytest.raises(VersionError, match="disagree"):
        check(tmp_path)


def test_workspace_member_out_of_sync_fails(tmp_path: Path) -> None:
    # The exact drift 0.2.0 cleaned up: the Python line moved, a JS workspace
    # stayed on the donor's number.
    write_repo(tmp_path, npm={"apps/web": "0.17.0"}, workspaces=["apps/*"])

    with pytest.raises(VersionError, match="0.17.0"):
        check(tmp_path)


def test_expect_matches_tag_with_or_without_v(tmp_path: Path) -> None:
    write_repo(tmp_path)

    assert check(tmp_path, expect="1.2.3")
    assert check(tmp_path, expect="v1.2.3")


def test_expect_mismatch_fails(tmp_path: Path) -> None:
    write_repo(tmp_path)

    with pytest.raises(VersionError, match="the repo is at 1.2.3"):
        check(tmp_path, expect="v2.0.0")


def test_member_without_a_version_is_skipped(tmp_path: Path) -> None:
    # The root test harness ships nothing and carries no version; that is not
    # a drift, so it must not fail the release.
    root = write_repo(tmp_path, workspaces=["apps/*"])
    harness = root / "apps" / "harness"
    harness.mkdir(parents=True)
    (harness / "package.json").write_text(json.dumps({"name": "harness"}), encoding="utf-8")

    assert check(root)


def test_build_system_version_is_not_mistaken_for_the_project_version(tmp_path: Path) -> None:
    # pyproject has other tables with `version` keys; only [project] counts.
    write_repo(tmp_path, py="1.2.3", module="1.2.3")

    assert check(tmp_path, expect="1.2.3")


def test_missing_source_is_an_error_not_a_pass(tmp_path: Path) -> None:
    write_repo(tmp_path)
    (tmp_path / "src" / "opencodon" / "common" / "version.py").unlink()

    with pytest.raises(VersionError, match="not found"):
        check(tmp_path)


@pytest.mark.parametrize(("raw", "expected"), [("v1.2.3", "1.2.3"), ("1.2.3", "1.2.3")])
def test_normalize_expected(raw: str, expected: str) -> None:
    assert normalize_expected(raw) == expected
