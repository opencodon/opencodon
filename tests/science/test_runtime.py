"""Behavior contracts for ScienceRuntime — the recorded cell, end to end."""

import json

import pytest


class TestRunCell:
    def test_stdout_and_cell_row_finalized(self, science_runtime, db):
        db.create_session("s1", source="cli")
        result = science_runtime.run_cell("s1", "print('hello science')")
        assert result["status"] == "ok"
        assert "hello science" in result["stdout"]
        cell = science_runtime.store.get_cell(result["cell_id"])
        assert cell["exit_status"] == "ok"
        assert cell["session_id"] == "s1"
        assert "hello science" in cell["stdout"]
        assert cell["env_name"] == "fake-python"

    def test_state_persists_across_cells(self, science_runtime, db):
        db.create_session("s1", source="cli")
        science_runtime.run_cell("s1", "x = 41")
        result = science_runtime.run_cell("s1", "print(x + 1)")
        assert "42" in result["stdout"]
        cells = science_runtime.store.cells_for_session("s1")
        assert [c["cell_index"] for c in cells] == [0, 1]

    def test_error_cell_recorded_with_error(self, science_runtime, db):
        db.create_session("s1", source="cli")
        result = science_runtime.run_cell("s1", "raise ValueError('boom')")
        assert result["status"] == "error"
        assert result["error"]["name"] == "ValueError"
        cell = science_runtime.store.get_cell(result["cell_id"])
        assert cell["exit_status"] == "error"
        assert "boom" in cell["stderr"]

    def test_env_snapshot_recorded_on_fresh_kernel_only(self, science_runtime, db):
        db.create_session("s1", source="cli")
        first = science_runtime.run_cell("s1", "pass")
        second = science_runtime.run_cell("s1", "pass")
        first_cell = science_runtime.store.get_cell(first["cell_id"])
        second_cell = science_runtime.store.get_cell(second["cell_id"])
        assert first_cell["env_snapshot"]
        assert second_cell["env_snapshot"] is None

    def test_files_written_diff(self, science_runtime, db):
        db.create_session("s1", source="cli")
        result = science_runtime.run_cell(
            "s1", "open('notes.txt', 'w').write('hi')"
        )
        cell = science_runtime.store.get_cell(result["cell_id"])
        assert "notes.txt" in json.loads(cell["files_written"])


class TestArtifactIngestion:
    def test_save_artifact_creates_version(self, science_runtime, db):
        db.create_session("s1", source="cli")
        result = science_runtime.run_cell(
            "s1", "save_artifact('col1,col2\\n1,2\\n', 'results.csv')"
        )
        assert result["status"] == "ok"
        [artifact] = result["artifacts"]
        assert artifact["filename"] == "results.csv"
        assert artifact["version_number"] == 1
        blob = science_runtime.blobs.read_bytes(artifact["sha256"])
        assert blob == b"col1,col2\n1,2\n"
        version = science_runtime.store.get_version(artifact["version_id"])
        assert version["producing_cell_id"] == result["cell_id"]
        assert version["env_snapshot_hash"]

    def test_resave_bumps_version_and_parent(self, science_runtime, db):
        db.create_session("s1", source="cli")
        first = science_runtime.run_cell(
            "s1", "save_artifact('v1', 'out.txt')"
        )["artifacts"][0]
        second = science_runtime.run_cell(
            "s1", "save_artifact('v2', 'out.txt')"
        )["artifacts"][0]
        assert second["artifact_id"] == first["artifact_id"]
        assert second["version_number"] == 2
        version = science_runtime.store.get_version(second["version_id"])
        assert version["parent_version_id"] == first["version_id"]
        stored = science_runtime.store.get_artifact(first["artifact_id"])
        assert stored["latest_version_id"] == second["version_id"]

    def test_load_then_stage_records_lineage(self, science_runtime, db):
        db.create_session("s1", source="cli")
        raw = science_runtime.run_cell(
            "s1", "save_artifact('1\\n2\\n3\\n', 'raw.txt')"
        )["artifacts"][0]
        code = (
            f"path = load_artifact('{raw['version_id']}')\n"
            "total = sum(int(line) for line in open(path))\n"
            "save_artifact(str(total), 'sum.txt')\n"
        )
        derived = science_runtime.run_cell(
            "s1", code, inputs=[raw["version_id"]]
        )
        assert derived["status"] == "ok"
        [out] = derived["artifacts"]
        assert science_runtime.blobs.read_bytes(out["sha256"]) == b"6"

        upstream = science_runtime.store.lineage(out["version_id"])
        assert [(v["id"], v["depth"]) for v in upstream] == [(raw["version_id"], 1)]

        calls = science_runtime.store.host_calls_for_cell(derived["cell_id"])
        assert [c["method"] for c in calls] == ["artifact.load", "artifact.stage"]

        cell = science_runtime.store.get_cell(derived["cell_id"])
        assert "raw.txt" in json.loads(cell["files_read"])

    def test_undeclared_input_is_refused_in_kernel(self, science_runtime, db):
        db.create_session("s1", source="cli")
        raw = science_runtime.run_cell(
            "s1", "save_artifact('x', 'raw.txt')"
        )["artifacts"][0]
        result = science_runtime.run_cell(
            "s1", f"load_artifact('{raw['version_id']}')"  # not declared
        )
        assert result["status"] == "error"
        assert "not declared as an input" in result["error"]["value"]

    def test_unknown_declared_input_fails_fast(self, science_runtime, db):
        db.create_session("s1", source="cli")
        with pytest.raises(LookupError):
            science_runtime.run_cell("s1", "pass", inputs=["no-such-version"])


class TestRootScoping:
    def test_artifacts_scoped_to_root_session(self, science_runtime, db):
        db.create_session("root", source="cli")
        db.create_session("child", source="delegate", parent_session_id="root")
        result = science_runtime.run_cell(
            "child", "save_artifact('from child', 'child.txt')"
        )
        artifact = science_runtime.store.get_artifact(
            result["artifacts"][0]["artifact_id"]
        )
        assert artifact["root_session_id"] == "root"
        roots = science_runtime.store.artifacts_for_root("root")
        assert [a["filename"] for a in roots] == ["child.txt"]
