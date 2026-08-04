"""Tests for the release script's pure logic.

Nothing here shells out to git or touches the real repo: the functions under
test are the ones that decide what gets released and what the notes say, and
those are the ones worth pinning.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import release  # noqa: E402


class TestTagSelection:
    """The fork's tag line must never pick up a donor CalVer tag."""

    def test_fork_tags_are_recognized(self) -> None:
        assert release.is_fork_release_tag("v0.1.0")
        assert release.is_fork_release_tag("v1.0.0")
        assert release.is_fork_release_tag("v12.3.4")

    def test_donor_calver_tags_are_rejected(self) -> None:
        # These parse as valid semver and sort ABOVE anything this fork ships,
        # which is exactly how they hijacked the "previous tag" lookup.
        assert not release.is_fork_release_tag("v2026.7.30")
        assert not release.is_fork_release_tag("v2026.6.5")

    def test_non_version_tags_are_rejected(self) -> None:
        assert not release.is_fork_release_tag("backup/precopystrip-20260616-2058")
        assert not release.is_fork_release_tag("v0.1")
        assert not release.is_fork_release_tag("0.1.0")

    def test_newest_wins_numerically_not_lexically(self) -> None:
        tags = ["v0.1.0", "v0.9.0", "v0.10.0"]

        assert max(tags, key=release.tag_sort_key) == "v0.10.0"


class TestCategorize:
    @pytest.mark.parametrize(
        ("subject", "category"),
        [
            ("feat(client): add a thing", "features"),
            ("fix(science): resolve the pin", "fixes"),
            ("refactor: move the science layer", "improvements"),
            ("restructure phase 4: split AIAgent", "improvements"),
            ("rebrand: lime brand color", "improvements"),
            ("docs: record phase 4 completion", "docs"),
            ("test: add per-phase requirement gate", "tests"),
            ("chore(deps): bump postcss", "chore"),
            ("ci: give the new workspaces the configs", "chore"),
        ],
    )
    def test_conventional_prefixes(self, subject: str, category: str) -> None:
        assert release.categorize(subject) == category

    @pytest.mark.parametrize(
        "subject",
        [
            "feat!: remove the Honcho memory provider",
            "feat(scope)!: drop a thing",
            "breaking: cut the API",
        ],
    )
    def test_breaking_beats_its_own_type(self, subject: str) -> None:
        # `feat!:` is a REMOVAL, not an addition — filing it under Added is how
        # a breaking change hides in a release note.
        assert release.categorize(subject) == "breaking"

    def test_unprefixed_falls_back_to_heuristics(self) -> None:
        assert release.categorize("add a new pane") == "features"
        assert release.categorize("fix the broken thing") == "fixes"

    def test_unclassifiable_lands_in_other(self) -> None:
        assert release.categorize("wip") == "other"


class TestSubjectCleaning:
    def test_strips_prefix_and_pr_number(self) -> None:
        assert (
            release.clean_subject("feat(client): open every session (#42)")
            == "open every session"
        )

    def test_leaves_a_bare_subject_alone(self) -> None:
        assert release.clean_subject("open every session") == "open every session"

    def test_pr_number_extracted_only_from_the_tail(self) -> None:
        assert release.pr_number("fix: thing (#42)") == "42"
        assert release.pr_number("fix: closes #42 in passing") is None


class TestCoauthors:
    def test_parses_trailers(self) -> None:
        body = "Some body\n\nCo-Authored-By: Ada Lovelace <ada@example.com>\n"

        assert release.coauthors(body) == ["Ada Lovelace"]

    def test_no_trailers_is_empty(self) -> None:
        assert release.coauthors("just a body") == []


class TestRenderChangelog:
    commits = [
        {"sha": "a", "subject": "feat: add a pane (#7)", "author": "Ada", "body": ""},
        {"sha": "b", "subject": "fix: stop the crash", "author": "Grace", "body": ""},
        {
            "sha": "c",
            "subject": "feat!: remove the old API",
            "author": "Ada",
            "body": "Co-Authored-By: Alan Turing <alan@example.com>\n",
        },
    ]

    def test_groups_by_category_with_breaking_first(self) -> None:
        notes = release.render_changelog(
            self.commits, "1.0.0", "2026-08-04", "https://example.com/r"
        )

        assert notes.index("### Breaking changes") < notes.index("### Added")
        assert notes.index("### Added") < notes.index("### Fixed")

    def test_links_pr_numbers(self) -> None:
        notes = release.render_changelog(
            self.commits, "1.0.0", "2026-08-04", "https://example.com/r"
        )

        assert "([#7](https://example.com/r/pull/7))" in notes

    def test_lists_authors_and_coauthors(self) -> None:
        notes = release.render_changelog(
            self.commits, "1.0.0", "2026-08-04", "https://example.com/r"
        )

        assert "Ada, Alan Turing, Grace" in notes

    def test_empty_categories_are_omitted(self) -> None:
        notes = release.render_changelog(
            self.commits, "1.0.0", "2026-08-04", "https://example.com/r"
        )

        assert "### Documentation" not in notes


class TestChangelogFile:
    def test_hand_written_section_is_found(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        changelog = tmp_path / "CHANGELOG.md"
        changelog.write_text(
            "# Changelog\n\n## Unreleased\n\n## 0.2.0 — 2026-08-04\n\nCurated prose.\n\n## 0.1.0 — x\n\nOld.\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(release, "CHANGELOG", changelog)

        section = release.changelog_section("0.2.0")

        assert section is not None
        assert "Curated prose." in section
        assert "Old." not in section  # stops at the next heading

    def test_absent_section_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        changelog = tmp_path / "CHANGELOG.md"
        changelog.write_text("# Changelog\n\n## Unreleased\n", encoding="utf-8")
        monkeypatch.setattr(release, "CHANGELOG", changelog)

        assert release.changelog_section("0.2.0") is None

    def test_generated_section_lands_under_unreleased(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        changelog = tmp_path / "CHANGELOG.md"
        changelog.write_text(
            "# Changelog\n\n## Unreleased\n\n## 0.1.0 — x\n\nOld.\n", encoding="utf-8"
        )
        monkeypatch.setattr(release, "CHANGELOG", changelog)

        release.insert_changelog("## 0.2.0 — 2026-08-04\n\nNew stuff.\n")
        text = changelog.read_text(encoding="utf-8")

        assert (
            text.index("## Unreleased")
            < text.index("## 0.2.0")
            < text.index("## 0.1.0")
        )


class TestBump:
    @pytest.mark.parametrize(
        ("current", "part", "expected"),
        [
            ("0.2.0", "major", "1.0.0"),
            ("0.2.0", "minor", "0.3.0"),
            ("0.2.0", "patch", "0.2.1"),
            ("1.9.9", "minor", "1.10.0"),
        ],
    )
    def test_bumps(self, current: str, part: str, expected: str) -> None:
        assert release.bump(current, part) == expected

    def test_lower_parts_reset(self) -> None:
        assert release.bump("1.2.3", "major") == "2.0.0"
        assert release.bump("1.2.3", "minor") == "1.3.0"

    def test_non_semver_is_rejected(self) -> None:
        with pytest.raises(release.ReleaseError, match="non-semver"):
            release.bump("2026.7.30.2", "minor")
