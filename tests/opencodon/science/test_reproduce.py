"""reproduce(version_id) contracts — replay, compare, honest grading."""

from opencodon.science.reproduce import reproduce


class TestReproduce:
    def test_deterministic_chain_reproduces(self, science_runtime, db):
        db.create_session("s1", source="cli")
        raw = science_runtime.run_cell(
            "s1", "save_artifact('2\\n3\\n4\\n', 'raw.txt')"
        )["artifacts"][0]
        code = (
            f"path = load_artifact('{raw['version_id']}')\n"
            "total = sum(int(line) for line in open(path))\n"
            "save_artifact(str(total), 'sum.txt')\n"
        )
        out = science_runtime.run_cell("s1", code, inputs=[raw["version_id"]])[
            "artifacts"
        ][0]

        report = reproduce(out["version_id"], runtime=science_runtime)
        assert report["claim"] == "reproduced"
        assert report["expected_sha256"] == report["candidate_sha256"]
        assert len(report["replayed_cells"]) == 2
        assert all(r["status"] == "ok" for r in report["replayed_cells"])
        # Grading stays honest: observation-only environment is a caveat.
        assert any("observation-only" in c for c in report["caveats"])

    def test_nondeterministic_output_diverges(self, science_runtime, db, tmp_path):
        db.create_session("s1", source="cli")
        counter = tmp_path / "counter.txt"
        counter.write_text("0")
        code = (
            f"c = int(open({str(counter)!r}).read()) + 1\n"
            f"open({str(counter)!r}, 'w').write(str(c))\n"
            "save_artifact(str(c), 'roll.txt')\n"
        )
        out = science_runtime.run_cell("s1", code)["artifacts"][0]
        report = reproduce(out["version_id"], runtime=science_runtime)
        assert report["claim"] == "diverged"
        assert report["expected_sha256"] != report["candidate_sha256"]

    def test_failing_replay_is_reported(self, science_runtime, db, tmp_path):
        db.create_session("s1", source="cli")
        # Succeeds the first time, fails on replay (marker file exists after).
        marker = tmp_path / "ran-once"
        code = (
            f"import os\n"
            f"if os.path.exists({str(marker)!r}):\n"
            f"    raise RuntimeError('not idempotent')\n"
            f"open({str(marker)!r}, 'w').write('x')\n"
            "save_artifact('ok', 'once.txt')\n"
        )
        out = science_runtime.run_cell("s1", code)["artifacts"][0]
        report = reproduce(out["version_id"], runtime=science_runtime)
        assert report["claim"] == "failed"
        assert "status" in report["reason"]

    def test_upload_without_producing_cell_is_ineligible(self, science_runtime, db):
        db.create_session("s1", source="cli")
        store = science_runtime.store
        artifact_id = store.create_artifact("s1", "upload.bin", is_user_upload=True)
        version = store.add_version(
            artifact_id, checksum="aa", size_bytes=1, storage_path="/x"
        )
        report = reproduce(version["id"], runtime=science_runtime)
        assert report["claim"] == "ineligible"

    def test_unknown_version_is_ineligible(self, science_runtime):
        report = reproduce("no-such-version", runtime=science_runtime)
        assert report["claim"] == "ineligible"

    def test_replay_does_not_touch_original_versions(self, science_runtime, db):
        db.create_session("s1", source="cli")
        out = science_runtime.run_cell(
            "s1", "save_artifact('stable', 'keep.txt')"
        )["artifacts"][0]
        before = science_runtime.store.get_artifact(out["artifact_id"])
        reproduce(out["version_id"], runtime=science_runtime)
        after = science_runtime.store.get_artifact(out["artifact_id"])
        assert after["latest_version_id"] == before["latest_version_id"]
        versions = science_runtime.store.latest_version(out["artifact_id"])
        assert versions["version_number"] == 1
