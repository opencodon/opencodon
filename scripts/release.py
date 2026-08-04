#!/usr/bin/env python3
"""Cut a release: bump the version, write the changelog, tag, publish.

Ported from the donor's `scripts/release.py`, which cut every Hermes release
from a maintainer's machine rather than from CI. Two deliberate differences:

  * Plain semver tags (`v0.2.0`), not the donor's dual semver + CalVer
    (`v2026.7.30` titled "v0.19.1"). This fork's own tag line starts at
    `v0.1.0`, and the `v2026.*` tags in this repo point at donor commits that
    are not ancestors of `main` — reusing that scheme would collide with them.
    `__release_date__` still records the date, in the donor's `2026.8.4` form.

  * Every npm workspace is bumped, not just `apps/desktop`. They all carry the
    same version as of 0.2.0; leaving seven of them behind is how they drifted
    to 1.0.0 / 0.17.0 / 0.0.1 in the first place.

The GitHub release is changelog-only, like the donor's. Nothing here builds or
attaches installers: `setup.py` blocks wheels and sdists outside a Nix build,
and the desktop installers are built by hand. Install is `scripts/install.sh`.

Usage:
    python scripts/release.py --bump minor              # dry run, prints everything
    python scripts/release.py --bump minor --publish    # commit, tag, push, release
    python scripts/release.py --version 1.0.0 --publish # explicit version
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from datetime import date as date_cls
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

VERSION_PY = REPO_ROOT / "src" / "opencodon" / "common" / "version.py"
PYPROJECT = REPO_ROOT / "pyproject.toml"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"

# Ordered — this is the order sections appear in the notes.
CATEGORY_TITLES = {
    "breaking": "Breaking changes",
    "features": "Added",
    "fixes": "Fixed",
    "improvements": "Changed",
    "docs": "Documentation",
    "tests": "Tests",
    "chore": "Chore",
    "other": "Other",
}

# The donor's patterns, kept as-is: this repo's history uses the same
# conventional-commit prefixes, so its categorization carries over unchanged.
CATEGORY_PATTERNS = {
    "breaking": [r"^breaking[\s:(]", r"^\w+!:", r"^\w+\([^)]*\)!:"],
    "features": [r"^feat[\s:(]", r"^feature[\s:(]", r"^add[\s:(]"],
    "fixes": [r"^fix[\s:(]", r"^bugfix[\s:(]", r"^bug[\s:(]", r"^hotfix[\s:(]"],
    "improvements": [
        r"^improve[\s:(]",
        r"^perf[\s:(]",
        r"^enhance[\s:(]",
        r"^refactor[\s:(]",
        r"^restructure[\s:(]",
        r"^rebrand[\s:(]",
        r"^cleanup[\s:(]",
        r"^clean[\s:(]",
        r"^update[\s:(]",
        r"^optimize[\s:(]",
    ],
    "docs": [r"^doc[\s:(]", r"^docs[\s:(]"],
    "tests": [r"^test[\s:(]", r"^tests[\s:(]"],
    "chore": [
        r"^chore[\s:(]",
        r"^ci[\s:(]",
        r"^build[\s:(]",
        r"^deps[\s:(]",
        r"^bump[\s:(]",
        r"^style[\s:(]",
    ],
}


class ReleaseError(Exception):
    """Anything that should stop the release with a readable message."""


# ── git ─────────────────────────────────────────────────────────────────────


def git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(cwd or REPO_ROOT),
    )
    if result.returncode != 0:
        raise ReleaseError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


# A donor CalVer tag (`v2026.7.30`) is a syntactically valid semver that sorts
# ABOVE every version this fork will ever ship, so a naive "newest vX.Y.Z" picks
# it and diffs against someone else's history. The discriminator is the leading
# component: a year is four digits, this fork's major is not.
CALVER_YEAR_FLOOR = 1000


def is_fork_release_tag(tag: str) -> bool:
    match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", tag)
    return bool(match) and int(match.group(1)) < CALVER_YEAR_FLOOR


def tag_sort_key(tag: str) -> tuple[int, int, int]:
    return tuple(int(part) for part in tag.lstrip("v").split("."))  # type: ignore[return-value]


def last_release_tag() -> str | None:
    """The newest tag on THIS fork's version line, or None before the first."""
    candidates = [
        tag.strip()
        for tag in git("tag", "--list").splitlines()
        if is_fork_release_tag(tag.strip())
    ]
    return max(candidates, key=tag_sort_key) if candidates else None


# ── versions ────────────────────────────────────────────────────────────────


def current_version() -> str:
    match = re.search(
        r'^__version__\s*=\s*"([^"]+)"', VERSION_PY.read_text(encoding="utf-8"), re.M
    )
    if not match:
        raise ReleaseError(f"{VERSION_PY.relative_to(REPO_ROOT)}: no __version__")
    return match.group(1)


def bump(version: str, part: str) -> str:
    parts = version.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise ReleaseError(f"cannot bump non-semver version {version!r}")
    major, minor, patch = (int(p) for p in parts)

    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    if part == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ReleaseError(f"unknown bump part: {part}")


def npm_manifests() -> list[Path]:
    """Every workspace package.json that carries a version, plus the root."""
    root_manifest = REPO_ROOT / "package.json"
    data = json.loads(root_manifest.read_text(encoding="utf-8"))
    globs = data.get("workspaces") or []
    if isinstance(globs, dict):
        globs = globs.get("packages") or []

    found = [root_manifest]
    for glob in globs:
        found.extend(
            sorted((REPO_ROOT / glob).parent.glob(f"{Path(glob).name}/package.json"))
        )

    return [
        m
        for m in found
        if m.is_file() and "version" in json.loads(m.read_text(encoding="utf-8"))
    ]


def write_version(version: str, release_date: str) -> list[Path]:
    """Stamp `version` into every source. Returns the paths it changed."""
    touched: list[Path] = []

    module = VERSION_PY.read_text(encoding="utf-8")
    module = re.sub(
        r'^__version__\s*=\s*"[^"]+"', f'__version__ = "{version}"', module, flags=re.M
    )
    module = re.sub(
        r'^__release_date__\s*=\s*"[^"]+"',
        f'__release_date__ = "{release_date}"',
        module,
        flags=re.M,
    )
    VERSION_PY.write_text(module, encoding="utf-8")
    touched.append(VERSION_PY)

    # Only the [project] table's version — other tables have their own.
    pyproject = PYPROJECT.read_text(encoding="utf-8")
    pyproject = re.sub(
        r'(\[project\][^\[]*?^version\s*=\s*)"[^"]+"',
        rf'\g<1>"{version}"',
        pyproject,
        count=1,
        flags=re.M | re.S,
    )
    PYPROJECT.write_text(pyproject, encoding="utf-8")
    touched.append(PYPROJECT)

    for manifest in npm_manifests():
        text = manifest.read_text(encoding="utf-8")
        text, count = re.subn(
            r'^(\s*"version":\s*)"[^"]*"',
            rf'\g<1>"{version}"',
            text,
            count=1,
            flags=re.M,
        )
        if count:
            manifest.write_text(text, encoding="utf-8")
            touched.append(manifest)

    return touched


def refresh_lockfiles() -> list[Path]:
    """Re-lock after the version stamps. `uv lock` and npm both record it."""
    touched = []

    if shutil.which("uv"):
        subprocess.run(
            ["uv", "lock"], cwd=str(REPO_ROOT), check=True, capture_output=True
        )
        touched.append(REPO_ROOT / "uv.lock")
    else:
        raise ReleaseError("uv not found — needed to refresh uv.lock")

    if shutil.which("npm"):
        subprocess.run(
            ["npm", "install", "--ignore-scripts", "--package-lock-only"],
            cwd=str(REPO_ROOT),
            check=True,
            capture_output=True,
        )
        touched.append(REPO_ROOT / "package-lock.json")
    else:
        raise ReleaseError("npm not found — needed to refresh package-lock.json")

    return touched


# ── changelog ───────────────────────────────────────────────────────────────

_NUL = "\x00"


def commits_since(tag: str | None) -> list[dict]:
    """Commits reachable from HEAD but not from `tag`, newest first."""
    rev_range = f"{tag}..HEAD" if tag else "HEAD"
    # `%x00`/`%x1e` are git's own placeholders — a literal NUL in the argv
    # string is rejected by subprocess before git ever sees it.
    raw = git(
        "log", "--no-merges", "--pretty=format:%H%x00%s%x00%an%x00%b%x1e", rev_range
    )

    commits = []
    for record in raw.split("\x1e"):
        record = record.strip().lstrip("\n")
        if not record:
            continue
        sha, subject, author, body = (record.split(_NUL) + ["", "", "", ""])[:4]
        commits.append({"sha": sha, "subject": subject, "author": author, "body": body})
    return commits


def categorize(subject: str) -> str:
    lowered = subject.lower()
    for category, patterns in CATEGORY_PATTERNS.items():
        if any(re.match(pattern, lowered) for pattern in patterns):
            return category
    # The donor's heuristic tail, for commits with no conventional prefix.
    if any(word in lowered for word in ("add ", "new ", "implement", "support ")):
        return "features"
    if any(word in lowered for word in ("fix ", "fixed ", "resolve", "patch ")):
        return "fixes"
    if any(word in lowered for word in ("refactor", "cleanup", "improve", "update ")):
        return "improvements"
    return "other"


def pr_number(subject: str) -> str | None:
    match = re.search(r"\(#(\d+)\)\s*$", subject)
    return match.group(1) if match else None


def coauthors(body: str) -> list[str]:
    return re.findall(r"^Co-Authored-By:\s*([^<]+?)\s*<", body, re.M | re.I)


def clean_subject(subject: str) -> str:
    """Strip the conventional prefix and trailing PR number for readability."""
    subject = re.sub(r"\s*\(#\d+\)\s*$", "", subject)
    subject = re.sub(r"^[a-z]+(\([^)]*\))?!?:\s*", "", subject, flags=re.I)
    return subject.strip()


def render_changelog(
    commits: list[dict], version: str, release_date: str, repo_url: str
) -> str:
    """The `## <version>` section body — the same text the release notes use."""
    grouped: dict[str, list[dict]] = defaultdict(list)
    for commit in commits:
        grouped[categorize(commit["subject"])].append(commit)

    contributors: set[str] = set()
    for commit in commits:
        contributors.add(commit["author"])
        contributors.update(coauthors(commit["body"]))

    lines = [f"## {version} — {release_date}", ""]

    for category, title in CATEGORY_TITLES.items():
        entries = grouped.get(category)
        if not entries:
            continue
        lines.append(f"### {title}")
        lines.append("")
        for commit in entries:
            text = clean_subject(commit["subject"])
            number = pr_number(commit["subject"])
            suffix = f" ([#{number}]({repo_url}/pull/{number}))" if number else ""
            lines.append(f"- {text}{suffix}")
        lines.append("")

    if contributors:
        lines.append("### Contributors")
        lines.append("")
        lines.append(", ".join(sorted(contributors)))
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def changelog_section(version: str) -> str | None:
    """An existing `## <version>` section, if the maintainer already wrote one."""
    if not CHANGELOG.is_file():
        return None
    text = CHANGELOG.read_text(encoding="utf-8")
    pattern = rf"^## {re.escape(version)}(?: .*)?$"
    match = re.search(pattern, text, re.M)
    if not match:
        return None
    rest = text[match.end() :]
    end = re.search(r"^## ", rest, re.M)
    return (rest[: end.start()] if end else rest).strip() + "\n"


def insert_changelog(section: str) -> None:
    """Put a generated section directly below the `## Unreleased` heading."""
    text = (
        CHANGELOG.read_text(encoding="utf-8")
        if CHANGELOG.is_file()
        else "# Changelog\n\n## Unreleased\n"
    )
    marker = re.search(r"^## Unreleased\s*$", text, re.M)
    if marker:
        cut = marker.end()
        text = f"{text[:cut]}\n\n{section.rstrip()}\n{text[cut:]}"
    else:
        heading = re.search(r"^# .*$", text, re.M)
        cut = heading.end() if heading else 0
        text = f"{text[:cut]}\n\n{section.rstrip()}\n{text[cut:]}"
    CHANGELOG.write_text(text, encoding="utf-8")


# ── publish ─────────────────────────────────────────────────────────────────


def release_paths() -> list[Path]:
    """Every file the release itself rewrites."""
    return [
        VERSION_PY,
        PYPROJECT,
        CHANGELOG,
        REPO_ROOT / "uv.lock",
        REPO_ROOT / "package-lock.json",
        *npm_manifests(),
    ]


def dirty_release_paths() -> list[str]:
    """Uncommitted edits to the files the release is about to rewrite.

    Scoped rather than whole-tree on purpose. The guard exists so the release
    never sweeps up (or silently overwrites) work in progress in the files it
    stamps — and staging explicit paths already keeps everything else out. A
    whole-tree check just means an unrelated edit anywhere, including another
    session's or worktree's, blocks the release for no reason.
    """
    relative = [
        str(path.relative_to(REPO_ROOT)) for path in release_paths() if path.exists()
    ]
    status = git("status", "--porcelain", "--untracked-files=no", "--", *relative)
    return [line.strip() for line in status.splitlines() if line.strip()]


def verify_versions(expect: str) -> None:
    """Reuse the standalone guard so writing and checking never disagree."""
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "check_version_sync.py"),
            "--expect",
            expect,
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    if result.returncode != 0:
        raise ReleaseError(
            f"version sync check failed after bumping:\n{result.stderr.strip()}"
        )
    print(f"  ✓ {result.stdout.strip()}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--bump",
        choices=["major", "minor", "patch"],
        help="bump part of the current version",
    )
    group.add_argument("--version", help="explicit version, e.g. 1.0.0")
    parser.add_argument(
        "--publish", action="store_true", help="actually commit, tag, push and release"
    )
    parser.add_argument("--date", help="release date as 2026.8.4 (default: today)")
    parser.add_argument(
        "--repo-url",
        default="https://github.com/opencodon/opencodon",
        help="for PR links in the notes",
    )
    args = parser.parse_args(argv)

    try:
        return run(args)
    except ReleaseError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


def run(args: argparse.Namespace) -> int:
    # Only on the publish path: a dry run writes nothing, and refusing to
    # preview the notes because some other file is dirty is just obstructive.
    if args.publish and (dirty := dirty_release_paths()):
        listing = "\n".join(f"  {entry}" for entry in dirty)
        raise ReleaseError(f"these release files have uncommitted changes:\n{listing}")

    current = current_version()
    version = args.version or bump(current, args.bump)
    today = date_cls.today()
    release_date = args.date or f"{today.year}.{today.month}.{today.day}"
    tag = f"v{version}"

    if git("tag", "--list", tag):
        raise ReleaseError(f"tag {tag} already exists")

    previous = last_release_tag()
    commits = commits_since(previous)
    if not commits:
        raise ReleaseError(
            f"no commits since {previous or 'the beginning'} — nothing to release"
        )

    existing = changelog_section(version)
    if existing:
        notes = existing
        source = f"CHANGELOG.md (hand-written '## {version}' section)"
    else:
        notes = render_changelog(commits, version, f"{today:%Y-%m-%d}", args.repo_url)
        source = f"generated from {len(commits)} commits since {previous or 'the first commit'}"

    print(f"\n{'=' * 60}")
    print(f"  {current} -> {version}   (tag {tag}, release date {release_date})")
    print(f"  notes: {source}")
    print(f"{'=' * 60}\n")
    print(notes)

    if not args.publish:
        print(f"{'=' * 60}")
        print("  Dry run. Re-run with --publish to cut it.")
        print(f"{'=' * 60}")
        return 0

    print("Publishing:")

    touched = write_version(version, release_date)
    print(f"  ✓ stamped {version} into {len(touched)} files")

    touched += refresh_lockfiles()
    print("  ✓ refreshed uv.lock and package-lock.json")

    verify_versions(tag)

    if not existing:
        insert_changelog(notes)
        touched.append(CHANGELOG)
        print("  ✓ wrote the generated section into CHANGELOG.md")

    # Explicit paths only: other sessions and worktrees share this checkout.
    git("add", *[str(path.relative_to(REPO_ROOT)) for path in touched])

    # Nothing staged means the version was already stamped and the changelog
    # already written — the "release PR" flow, where a reviewed PR carries the
    # bump and this run only tags the merge. Committing an empty tree here
    # would just fail, so tag what is already there.
    if git("diff", "--cached", "--name-only"):
        git("commit", "-m", f"chore: release {tag}")
        print(f"  ✓ committed chore: release {tag}")
    else:
        print(
            f"  · already at {version} — tagging the existing commit, nothing to commit"
        )

    notes_file = REPO_ROOT / ".release_notes.md"
    notes_file.write_text(notes, encoding="utf-8")

    git("tag", "-a", tag, "-m", f"opencodon {version}")
    print(f"  ✓ created tag {tag}")

    # The branch and THIS tag — not `--tags`, which shoves every local tag at
    # the remote (this checkout carries donor CalVer and backup tags that have
    # no business being republished).
    git("push", "origin", "HEAD")
    git("push", "origin", tag)
    print(f"  ✓ pushed HEAD and {tag} to origin")

    if not shutil.which("gh"):
        print("  ✗ gh not found — create the release manually:")
        print(
            f"    gh release create {tag} --title 'opencodon {version}' --notes-file .release_notes.md"
        )
        return 1

    result = subprocess.run(
        [
            "gh",
            "release",
            "create",
            tag,
            "--title",
            f"opencodon {version}",
            "--notes-file",
            str(notes_file),
            "--verify-tag",
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    if result.returncode != 0:
        print(f"  ✗ gh release create failed: {result.stderr.strip()}")
        print(f"    Notes kept at {notes_file.name}; the tag is pushed, so retry with:")
        print(
            f"    gh release create {tag} --title 'opencodon {version}' --notes-file .release_notes.md"
        )
        return 1

    notes_file.unlink(missing_ok=True)
    print(f"  ✓ released: {result.stdout.strip()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
