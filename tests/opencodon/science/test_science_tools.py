"""Model-facing science tools: registration, dispatch, session scoping."""

import json

import pytest

import opencodon.tools.science_tools as science_tools
from opencodon.science.runtime import set_science_runtime


@pytest.fixture(autouse=True)
def _inject_runtime(science_runtime):
    set_science_runtime(science_runtime)
    yield
    set_science_runtime(None)


class TestRegistration:
    def test_science_toolset_lists_all_tools(self):
        from toolsets import get_toolset

        toolset = get_toolset("science")
        assert toolset is not None
        for name in (
            "run_code",
            "load_artifact",
            "list_artifacts",
            "artifact_lineage",
            "reproduce_artifact",
        ):
            assert name in toolset["tools"]

    def test_tools_are_registered(self):
        from opencodon.tools.registry import registry

        for name in ("run_code", "list_artifacts", "artifact_lineage"):
            assert registry.get_entry(name) is not None

    SCIENCE_TOOLS = (
        "run_code",
        "load_artifact",
        "list_artifacts",
        "artifact_lineage",
        "reproduce_artifact",
    )

    def test_science_in_core_tools(self):
        # opencodon is an open-science agent: the science layer is part of the
        # default bundle every platform ships, not an opt-in toolset. This
        # inverts the donor's footprint rule deliberately.
        from toolsets import _OPENCODON_CORE_TOOLS

        for name in self.SCIENCE_TOOLS:
            assert name in _OPENCODON_CORE_TOOLS

    def test_science_in_default_cli_toolset(self):
        # The bundle the default config actually enables (agent.toolsets).
        from toolsets import resolve_toolset

        resolved = resolve_toolset("opencodon-cli")
        for name in self.SCIENCE_TOOLS:
            assert name in resolved

    def test_science_survives_coding_posture(self):
        # The coding posture is auto-selected in any code workspace; if it
        # dropped these, science would silently switch off inside a repo.
        from toolsets import resolve_toolset

        resolved = resolve_toolset("coding")
        for name in self.SCIENCE_TOOLS:
            assert name in resolved

    def test_science_excluded_from_webhook_toolset(self):
        # Webhook payloads are untrusted third-party content; that bundle
        # stays narrow on purpose (no execution surface).
        from toolsets import resolve_toolset

        assert "run_code" not in resolve_toolset("opencodon-webhook")


class TestHandlers:
    def test_run_code_records_and_returns(self, db):
        db.create_session("s1", source="cli")
        raw = json.loads(
            science_tools.run_code("print('via tool')", session_id="s1")
        )
        assert raw["status"] == "ok"
        assert "via tool" in raw["stdout"]

    def test_artifact_flow_through_tools(self, db, science_runtime):
        db.create_session("s1", source="cli")
        made = json.loads(
            science_tools.run_code(
                "save_artifact('x,y\\n', 'pts.csv')", session_id="s1"
            )
        )
        version_id = made["artifacts"][0]["version_id"]

        listed = json.loads(science_tools.list_artifacts(session_id="s1"))
        assert [a["filename"] for a in listed["artifacts"]] == ["pts.csv"]
        assert listed["artifacts"][0]["latest_version_id"] == version_id

        loaded = json.loads(
            science_tools.load_artifact(version_id, session_id="s1")
        )
        assert loaded["filename"] == "pts.csv"
        with open(loaded["path"]) as fh:
            assert fh.read() == "x,y\n"

        lineage = json.loads(science_tools.artifact_lineage(version_id))
        assert lineage["lineage"] == []

    def test_run_code_reads_external_file_by_absolute_path(self, db, tmp_path):
        # The simple flow: read an existing local file by ABSOLUTE path (the
        # kernel's cwd is an isolated workspace, so a bare name won't resolve).
        db.create_session("s1", source="cli")
        src = tmp_path / "raw.csv"
        src.write_text("7\n8\n")
        out = json.loads(
            science_tools.run_code(
                f"print(sum(int(x) for x in open({str(src)!r})))",
                session_id="s1",
            )
        )
        assert out["status"] == "ok"
        assert "15" in out["stdout"]

    def test_bad_version_reports_error_json(self):
        result = json.loads(science_tools.load_artifact("nope"))
        assert "does not exist" in result["error"]

    def test_run_code_unknown_input_reports_error(self, db):
        db.create_session("s1", source="cli")
        result = json.loads(
            science_tools.run_code(
                "pass", inputs=["missing-version"], session_id="s1"
            )
        )
        assert "does not exist" in result["error"]
