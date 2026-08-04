#!/usr/bin/env python3
"""Verify every version line in the repo agrees — and, on a release, that they
agree with the tag being built.

The repo carries the same version in several places that have no mechanical
link to each other:

    pyproject.toml                [project] version
    src/opencodon/common/version.py    __version__
    package.json + every npm workspace member

They drifted badly before 0.2.0 (the Python line said 0.1.0 while the JS
workspaces still carried the donor's 0.17.0, 1.0.0 and 0.0.1), which is
invisible until something ships with the wrong number stamped on it. The
release workflow runs this before it builds anything, so a mistyped tag fails
in seconds instead of producing installers that lie about what they are.

Usage:
    check_version_sync.py                 # all sources agree with each other
    check_version_sync.py --expect 0.2.0  # ...and with this version
    check_version_sync.py --expect v0.2.0 # leading "v" is accepted (git tags)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

PYPROJECT = Path("pyproject.toml")
VERSION_PY = Path("src/opencodon/common/version.py")
ROOT_PACKAGE_JSON = Path("package.json")


class VersionError(Exception):
    """A version source is missing, unparseable, or disagrees with the rest."""


def _read(root: Path, rel: Path) -> str:
    path = root / rel
    if not path.is_file():
        raise VersionError(f"{rel}: not found")
    return path.read_text(encoding="utf-8")


def pyproject_version(root: Path) -> str:
    """The `version` under `[project]` — NOT any other table's version key."""
    text = _read(root, PYPROJECT)
    project = re.search(r"^\[project\]$(.*?)(?=^\[|\Z)", text, re.M | re.S)
    if not project:
        raise VersionError(f"{PYPROJECT}: no [project] table")
    match = re.search(r'^version\s*=\s*"([^"]+)"', project.group(1), re.M)
    if not match:
        raise VersionError(f"{PYPROJECT}: no version in [project]")
    return match.group(1)


def module_version(root: Path) -> str:
    text = _read(root, VERSION_PY)
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.M)
    if not match:
        raise VersionError(f"{VERSION_PY}: no __version__")
    return match.group(1)


def npm_workspace_globs(root: Path) -> list[str]:
    """The `workspaces` globs from the root package.json, if any."""
    data = json.loads(_read(root, ROOT_PACKAGE_JSON))
    workspaces = data.get("workspaces") or []
    if isinstance(workspaces, dict):  # the {packages: [...]} form
        workspaces = workspaces.get("packages") or []
    return [str(entry) for entry in workspaces]


def npm_versions(root: Path) -> dict[Path, str]:
    """Every workspace package.json version, keyed by repo-relative path.

    A member without a `version` is skipped rather than failed: a private
    package that never ships (the root test harness) legitimately has none.
    """
    found: dict[Path, str] = {}

    manifests = [root / ROOT_PACKAGE_JSON]
    for glob in npm_workspace_globs(root):
        manifests.extend(sorted((root / glob).parent.glob(f"{Path(glob).name}/package.json")))

    for manifest in manifests:
        if not manifest.is_file():
            continue
        version = json.loads(manifest.read_text(encoding="utf-8")).get("version")
        if version is not None:
            found[manifest.relative_to(root)] = str(version)

    return found


def collect(root: Path) -> dict[Path, str]:
    """Every version source in the repo, keyed by repo-relative path."""
    return {
        PYPROJECT: pyproject_version(root),
        VERSION_PY: module_version(root),
        **npm_versions(root),
    }


def normalize_expected(raw: str) -> str:
    """`v0.2.0` and `0.2.0` name the same release — git tags carry the `v`."""
    return raw[1:] if raw.startswith("v") else raw


def check(root: Path, expect: str | None = None) -> dict[Path, str]:
    """Raise VersionError unless every source agrees (and matches `expect`)."""
    versions = collect(root)

    distinct = sorted(set(versions.values()))
    if len(distinct) > 1:
        listing = "\n".join(f"  {path}: {version}" for path, version in sorted(versions.items()))
        raise VersionError(f"version sources disagree ({', '.join(distinct)}):\n{listing}")

    if expect is not None:
        wanted = normalize_expected(expect)
        actual = distinct[0]
        if actual != wanted:
            raise VersionError(
                f"the repo is at {actual} but {wanted} was expected — "
                f"bump the version sources before tagging, or tag the right commit"
            )

    return versions


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--expect", help="version (or git tag) every source must match")
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="repo root (default: this checkout)")
    args = parser.parse_args(argv)

    try:
        versions = check(args.root, args.expect)
    except VersionError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    print(f"version {next(iter(versions.values()))} — {len(versions)} sources agree")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
