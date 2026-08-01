"""Imported science skills — attribution, loadability, and kernel helpers.

These skills are redistributed under Apache-2.0, which is a licence with
conditions rather than a free-for-all: §4 requires the notices to travel with
the work and §4(b) requires modified files to say they were modified. Those
are testable, so they are tested — an attribution that only exists because
someone remembered to add it will eventually not exist.
"""

import re
from pathlib import Path

import pytest
import yaml

SKILLS_ROOT = Path(__file__).resolve().parents[2] / "skills" / "science"
NOTICE = Path(__file__).resolve().parents[2] / "NOTICE"

SKILL_DIRS = sorted(p for p in SKILLS_ROOT.iterdir() if p.is_dir()) if SKILLS_ROOT.is_dir() else []
SKILL_IDS = [p.name for p in SKILL_DIRS]


def frontmatter(skill_dir: Path) -> dict:
    text = (skill_dir / "SKILL.md").read_text()
    match = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert match, f"{skill_dir.name} has no frontmatter block"
    return yaml.safe_load(match.group(1))


def test_skills_were_actually_imported():
    assert SKILL_DIRS, "expected imported skills under skills/science"


# ── SCI-P3-01 licence obligations ───────────────────────────────────


@pytest.mark.requirement("SCI-P3-01")
@pytest.mark.parametrize("skill_dir", SKILL_DIRS, ids=SKILL_IDS)
def test_provenance_records_the_apache_obligations(skill_dir):
    meta = frontmatter(skill_dir)
    assert meta["license"] == "Apache-2.0"

    provenance = meta["metadata"]["provenance"]
    assert provenance["upstream"] == "claude-science"
    assert provenance["upstream_license"] == "Apache-2.0"
    assert str(provenance["upstream_version"]).strip()
    assert str(provenance["retrieved"]).strip()
    # §4(b): a modified file has to say it was modified, and saying *what*
    # changed is the difference between a notice and a formality.
    assert len(provenance["modifications"].strip()) > 20


# ── SCI-P3-02 third-party attribution ───────────────────────────────


@pytest.mark.requirement("SCI-P3-02")
def test_third_party_entries_survive_the_import():
    """literature-review is the one importing a declared third_party block."""
    meta = frontmatter(SKILLS_ROOT / "literature-review")
    entries = meta["metadata"]["third_party"]
    named = {entry["name"] for entry in entries}

    assert {"Crossref", "OpenAlex"} <= named
    openalex = next(e for e in entries if e["name"] == "OpenAlex")
    # The URL is the attribution — paraphrasing it would lose the thing the
    # upstream service actually asked for.
    assert openalex["terms_url"].startswith("https://openalex.org/")


@pytest.mark.requirement("SCI-P3-02")
@pytest.mark.parametrize("skill_dir", SKILL_DIRS, ids=SKILL_IDS)
def test_declared_third_party_entries_are_well_formed(skill_dir):
    entries = frontmatter(skill_dir)["metadata"].get("third_party") or []
    for entry in entries:
        assert entry.get("name"), f"{skill_dir.name}: third_party entry with no name"
        assert entry.get("kind") in {"weights", "service", "dataset"}
        # Either a licence or a link — an entry with neither attributes nothing.
        assert entry.get("license") or entry.get("terms_url") or entry.get("info_url")


# ── SCI-P3-03 distribution-level notice ─────────────────────────────


@pytest.mark.requirement("SCI-P3-03")
def test_notice_states_the_derivation_and_the_weights_position():
    notice = NOTICE.read_text()
    assert "Claude Science" in notice
    assert "Apache License 2.0" in notice
    assert "modified" in notice.lower()
    # The weights position matters: we redistribute skills, never weights.
    assert "does not redistribute" in notice or "not redistribute" in notice


# ── SCI-P3-04 loadable as ordinary skills ───────────────────────────


@pytest.mark.requirement("SCI-P3-04")
@pytest.mark.parametrize("skill_dir", SKILL_DIRS, ids=SKILL_IDS)
def test_frontmatter_matches_the_opencodon_convention(skill_dir):
    meta = frontmatter(skill_dir)
    assert meta["name"] == skill_dir.name
    assert meta["description"].strip()
    assert meta["version"]
    assert "science" in meta["metadata"]["opencodon"]["tags"]


@pytest.mark.requirement("SCI-P3-04")
@pytest.mark.parametrize("skill_dir", SKILL_DIRS, ids=SKILL_IDS)
def test_body_survives_the_frontmatter_rewrite(skill_dir):
    """The rewrite replaces the header — it must not eat the instructions."""
    text = (skill_dir / "SKILL.md").read_text()
    body = text.split("---\n", 2)[-1]
    assert len(body.strip()) > 500, f"{skill_dir.name} body looks truncated"


# ── SCI-P3-05 / 06 kernel helpers ───────────────────────────────────


@pytest.mark.requirement("SCI-P3-05")
def test_helpers_are_staged_into_the_workspace(tmp_path, monkeypatch):
    from science import bridge

    skills = tmp_path / "skills"
    (skills / "science" / "demo").mkdir(parents=True)
    (skills / "science" / "demo" / "kernel.py").write_text("def demo_helper():\n    return 42\n")
    monkeypatch.setattr("opencodon.tools.skills_hub._skills_dir", lambda: skills)

    workspace = tmp_path / "ws"
    workspace.mkdir()
    assert bridge.stage_skill_helpers(workspace) == 1
    staged = workspace / ".opencodon-science" / "skill-helpers" / "demo.py"
    assert "demo_helper" in staged.read_text()


@pytest.mark.requirement("SCI-P3-05")
def test_staging_never_breaks_a_cell(tmp_path, monkeypatch):
    """A missing or unreadable skills tree must not stop code from running."""
    from science import bridge

    monkeypatch.setattr(
        "opencodon.tools.skills_hub._skills_dir", lambda: tmp_path / "does-not-exist"
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()
    assert bridge.stage_skill_helpers(workspace) == 0


@pytest.mark.requirement("SCI-P3-05")
def test_load_skill_helpers_defines_the_functions(science_runtime, monkeypatch, tmp_path):
    skills = tmp_path / "skills"
    (skills / "science" / "demo").mkdir(parents=True)
    (skills / "science" / "demo" / "kernel.py").write_text(
        "PALETTE = ['#111']\n\n\ndef demo_helper(x):\n    return x * 2\n"
    )
    monkeypatch.setattr("opencodon.tools.skills_hub._skills_dir", lambda: skills)

    result = science_runtime.run_cell(
        "s1",
        "names = load_skill_helpers('demo')\n"
        "print(sorted(names))\n"
        "print(demo_helper(21), PALETTE)\n",
    )
    assert result["status"] == "ok", result.get("error")
    assert "['PALETTE', 'demo_helper']" in result["stdout"]
    assert "42 ['#111']" in result["stdout"]


@pytest.mark.requirement("SCI-P3-06")
def test_load_skill_helpers_on_a_skill_without_helpers(science_runtime):
    result = science_runtime.run_cell(
        "s1", "load_skill_helpers('no-such-skill')"
    )
    assert result["status"] == "error"
    assert result["error"]["name"] == "LookupError"
    # The message says where it looked, so the fix is obvious.
    assert "ships no kernel helpers" in result["error"]["value"]


@pytest.mark.requirement("SCI-P3-05")
def test_real_figure_style_helpers_load(science_runtime, monkeypatch):
    """The imported skill's own kernel.py, not a fixture.

    Pointed at the repo's bundled ``skills/`` because ``_skills_dir()``
    resolves to the *installed* tree under OPENCODON_HOME, which the test
    harness isolates — the bundled copy is what gets synced there.
    """
    if not (SKILLS_ROOT / "figure-style" / "kernel.py").exists():
        pytest.skip("figure-style ships no kernel.py")
    monkeypatch.setattr(
        "opencodon.tools.skills_hub._skills_dir", lambda: SKILLS_ROOT.parent
    )
    result = science_runtime.run_cell(
        "s1",
        "names = load_skill_helpers('figure-style')\n"
        "print('apply_figure_style' in names, 'focal_palette' in names)\n",
    )
    assert result["status"] == "ok", result.get("error")
    assert "True True" in result["stdout"]
