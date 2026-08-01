"""Contracts for the dashboard's read-only science API.

Data is produced through the real ScienceRuntime (cells, staged artifacts,
lineage edges, blobs), then read back over the router — so these assert the
wire shape the web UI depends on, not a hand-built fixture.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from opencodon_cli import science_api
from opencodon.state import SessionDB


@pytest.fixture
def client(tmp_path, db, science_runtime):
    """A TestClient whose routes open their own SessionDB per request.

    Routes close the handle they open, so the opener must hand out a fresh
    connection each call rather than the shared fixture's — same contract as
    ``_open_session_db_for_profile`` in production.
    """
    db_path = tmp_path / "state.db"
    # Save/restore rather than clearing: importing web_server installs the
    # production opener at module scope, and clearing it here would leave a
    # later test in the same process reading the real state.db.
    prev_db, prev_blobs = science_api._db_opener, science_api._blob_opener
    prev_gate = science_api._reproduce_gate
    science_api.set_db_opener(lambda profile: SessionDB(db_path))
    science_api.set_blob_store_opener(lambda: science_runtime.blobs)
    # Importing web_server installs a gate that opens on a loopback bind, and
    # that import may have happened in an earlier test in this process. Pin
    # the closed gate so tests do not inherit whatever ran before them.
    science_api.set_reproduce_gate(science_api._deny_reproduction)
    app = FastAPI()
    app.include_router(science_api.router)
    yield TestClient(app)
    science_api._db_opener, science_api._blob_opener = prev_db, prev_blobs
    science_api._reproduce_gate = prev_gate


@pytest.fixture
def frame(science_runtime, db):
    """One frame: a raw artifact, a derived artifact, and a failed cell."""
    db.create_session("s1", source="cli")
    raw = science_runtime.run_cell("s1", "save_artifact('1\\n2\\n3\\n', 'raw.txt')")
    derived = science_runtime.run_cell(
        "s1",
        (
            f"path = load_artifact('{raw['artifacts'][0]['version_id']}')\n"
            "total = sum(int(line) for line in open(path))\n"
            "save_artifact(str(total), 'sum.txt')\n"
        ),
        inputs=[raw["artifacts"][0]["version_id"]],
    )
    failed = science_runtime.run_cell("s1", "raise ValueError('boom')")
    return {
        "raw_version": raw["artifacts"][0]["version_id"],
        "raw_cell": raw["cell_id"],
        "derived_version": derived["artifacts"][0]["version_id"],
        "derived_cell": derived["cell_id"],
        "failed_cell": failed["cell_id"],
    }


class TestFrames:
    def test_empty_when_no_science_record(self, client):
        body = client.get("/api/science/frames").json()
        assert body == {"frames": [], "total": 0, "limit": 50, "offset": 0}

    def test_frame_rolls_up_cells_and_artifacts(self, client, frame):
        body = client.get("/api/science/frames").json()
        assert body["total"] == 1
        [row] = body["frames"]
        assert row["frame_id"] == "s1"
        assert row["cell_count"] == 3
        assert row["failed_cell_count"] == 1
        assert row["artifact_count"] == 2
        assert row["languages"] == ["python"]
        assert row["session_missing"] is False

    def test_child_session_cells_fold_onto_root(self, client, science_runtime, db):
        db.create_session("root", source="cli")
        db.create_session("child", source="delegate", parent_session_id="root")
        science_runtime.run_cell("child", "save_artifact('hi', 'child.txt')")
        frames = client.get("/api/science/frames").json()["frames"]
        assert [f["frame_id"] for f in frames] == ["root"]
        assert frames[0]["cell_count"] == 1
        assert frames[0]["artifact_count"] == 1

    def test_detail_lists_artifacts_and_environments(self, client, frame):
        body = client.get("/api/science/frames/s1").json()
        assert body["cell_count"] == 3
        assert body["failed_cell_count"] == 1
        assert sorted(a["filename"] for a in body["artifacts"]) == [
            "raw.txt",
            "sum.txt",
        ]
        [env] = body["environments"]
        assert env["language"] == "python"
        assert env["cell_count"] == 3

    def test_detail_404_for_unknown_frame(self, client):
        assert client.get("/api/science/frames/nope").status_code == 404

    def test_pagination_windows_the_index(self, client, science_runtime, db):
        for i in range(3):
            db.create_session(f"s{i}", source="cli")
            science_runtime.run_cell(f"s{i}", "pass")
        page = client.get(
            "/api/science/frames", params={"limit": 2, "offset": 0}
        ).json()
        assert page["total"] == 3
        assert len(page["frames"]) == 2
        rest = client.get(
            "/api/science/frames", params={"limit": 2, "offset": 2}
        ).json()
        assert len(rest["frames"]) == 1
        seen = {f["frame_id"] for f in page["frames"] + rest["frames"]}
        assert seen == {"s0", "s1", "s2"}

    def test_frame_survives_session_deletion(self, client, science_runtime, db):
        db.create_session("gone", source="cli")
        science_runtime.run_cell("gone", "save_artifact('x', 'kept.txt')")
        with db._lock:
            db._conn.execute("DELETE FROM sessions WHERE id = 'gone'")
            db._conn.commit()

        [row] = client.get("/api/science/frames").json()["frames"]
        assert row["frame_id"] == "gone"
        assert row["session_missing"] is True
        assert row["cell_count"] == 1
        assert row["artifact_count"] == 1
        # And the detail view still resolves.
        detail = client.get("/api/science/frames/gone").json()
        assert detail["session_missing"] is True
        assert [a["filename"] for a in detail["artifacts"]] == ["kept.txt"]

    def test_cells_carry_host_call_and_version_counts(self, client, frame):
        cells = client.get("/api/science/frames/s1/cells").json()["cells"]
        assert [c["cell_index"] for c in cells] == [0, 1, 2]
        derived = next(c for c in cells if c["cell_id"] == frame["derived_cell"])
        # artifact.load + artifact.stage
        assert derived["host_call_count"] == 2
        assert derived["version_count"] == 1
        assert derived["origin"] == "agent"
        failed = next(c for c in cells if c["cell_id"] == frame["failed_cell"])
        assert failed["exit_status"] == "error"

    def test_since_cursor_returns_only_newer_cells(
        self, client, science_runtime, db, frame
    ):
        first = client.get("/api/science/frames/s1/cells").json()
        cursor = first["cursor"]
        assert cursor is not None

        # Nothing new yet.
        idle = client.get(
            "/api/science/frames/s1/cells", params={"since": cursor}
        ).json()
        assert idle["cells"] == []
        assert idle["cursor"] == cursor

        science_runtime.run_cell("s1", "print('later')")
        fresh = client.get(
            "/api/science/frames/s1/cells", params={"since": cursor}
        ).json()
        assert len(fresh["cells"]) == 1
        assert "later" in fresh["cells"][0]["source"]
        assert fresh["cursor"] > cursor


class TestCells:
    def test_detail_includes_host_calls_and_versions(self, client, frame):
        body = client.get(f"/api/science/cells/{frame['derived_cell']}").json()
        assert [c["method"] for c in body["host_calls"]] == [
            "artifact.load",
            "artifact.stage",
        ]
        assert [v["version_id"] for v in body["versions"]] == [
            frame["derived_version"]
        ]
        assert "load_artifact" in body["source"]
        assert body["files_read"] == ["raw.txt"]

    def test_404_for_unknown_cell(self, client):
        assert client.get("/api/science/cells/nope").status_code == 404


class TestArtifacts:
    def test_list_and_search(self, client, frame):
        body = client.get("/api/science/artifacts").json()
        assert body["total"] == 2
        assert {a["filename"] for a in body["artifacts"]} == {"raw.txt", "sum.txt"}

        hits = client.get("/api/science/artifacts", params={"search": "sum"}).json()
        assert [a["filename"] for a in hits["artifacts"]] == ["sum.txt"]

        scoped = client.get(
            "/api/science/artifacts", params={"frame_id": "other"}
        ).json()
        assert scoped["artifacts"] == []

    def test_detail_lists_version_timeline(self, client, science_runtime, db, frame):
        science_runtime.run_cell("s1", "save_artifact('1\\n2\\n', 'raw.txt')")
        artifact_id = next(
            a["artifact_id"]
            for a in client.get("/api/science/artifacts").json()["artifacts"]
            if a["filename"] == "raw.txt"
        )
        body = client.get(f"/api/science/artifacts/{artifact_id}").json()
        assert [v["version_number"] for v in body["versions"]] == [1, 2]
        assert body["latest_version_number"] == 2

    def test_404_for_unknown_artifact(self, client):
        assert client.get("/api/science/artifacts/nope").status_code == 404


class TestVersions:
    def test_detail_carries_filename_and_producing_cell(self, client, frame):
        body = client.get(f"/api/science/versions/{frame['derived_version']}").json()
        assert body["filename"] == "sum.txt"
        assert body["frame_id"] == "s1"
        assert body["producing_cell"]["cell_id"] == frame["derived_cell"]
        assert [d["version_id"] for d in body["depends_on"]] == [
            frame["raw_version"]
        ]

    def test_lineage_walks_both_directions(self, client, frame):
        up = client.get(
            f"/api/science/versions/{frame['derived_version']}/lineage"
        ).json()
        assert [(v["version_id"], v["depth"]) for v in up["lineage"]] == [
            (frame["raw_version"], 1)
        ]
        assert up["lineage"][0]["filename"] == "raw.txt"

        down = client.get(
            f"/api/science/versions/{frame['raw_version']}/lineage",
            params={"direction": "downstream"},
        ).json()
        assert [v["version_id"] for v in down["lineage"]] == [
            frame["derived_version"]
        ]

    def test_lineage_rejects_bad_direction(self, client, frame):
        resp = client.get(
            f"/api/science/versions/{frame['raw_version']}/lineage",
            params={"direction": "sideways"},
        )
        assert resp.status_code == 422

    def test_content_preview_decodes_text(self, client, frame):
        body = client.get(
            f"/api/science/versions/{frame['derived_version']}/content"
        ).json()
        assert body["binary"] is False
        assert body["truncated"] is False
        assert body["text"] == "6"

    def test_content_marks_binary_payloads(self, client, science_runtime, db):
        db.create_session("s1", source="cli")
        result = science_runtime.run_cell(
            "s1", "save_artifact(b'\\xff\\xfe\\x00binary', 'blob.bin')"
        )
        version_id = result["artifacts"][0]["version_id"]
        body = client.get(f"/api/science/versions/{version_id}/content").json()
        assert body["binary"] is True
        assert body["text"] is None

    def test_content_truncates_large_payloads(
        self, client, science_runtime, db, monkeypatch
    ):
        monkeypatch.setattr(science_api, "CONTENT_PREVIEW_MAX_BYTES", 8)
        db.create_session("s1", source="cli")
        result = science_runtime.run_cell(
            "s1", "save_artifact('x' * 64, 'long.txt')"
        )
        body = client.get(
            f"/api/science/versions/{result['artifacts'][0]['version_id']}/content"
        ).json()
        assert body["truncated"] is True
        assert body["text"] == "x" * 8

    def test_download_returns_bytes_and_checksum_header(self, client, frame):
        resp = client.get(
            f"/api/science/versions/{frame['derived_version']}/download"
        )
        assert resp.status_code == 200
        assert resp.content == b"6"
        assert 'filename="sum.txt"' in resp.headers["content-disposition"]
        assert resp.headers["x-content-sha256"]

    def test_404_for_unknown_version(self, client):
        assert client.get("/api/science/versions/nope").status_code == 404
        assert client.get("/api/science/versions/nope/lineage").status_code == 404
        assert client.get("/api/science/versions/nope/content").status_code == 404


class TestSnapshots:
    def test_roundtrip_and_404(self, client, science_runtime, db):
        digest = science_runtime.store.put_snapshot("recorded payload")
        body = client.get(f"/api/science/snapshots/{digest}").json()
        assert body["content"] == "recorded payload"
        assert body["size_bytes"] == len("recorded payload")
        assert client.get("/api/science/snapshots/deadbeef").status_code == 404


class TestActionLabels:
    def test_description_round_trips_to_the_trace(self, client, science_runtime, db):
        db.create_session("s1", source="cli")
        science_runtime.run_cell(
            "s1", "x = 1", description="Setting up the fit"
        )
        [cell] = client.get("/api/science/frames/s1/cells").json()["cells"]
        assert cell["description"] == "Setting up the fit"

    def test_description_is_optional(self, client, science_runtime, db):
        db.create_session("s1", source="cli")
        science_runtime.run_cell("s1", "x = 1")
        [cell] = client.get("/api/science/frames/s1/cells").json()["cells"]
        assert cell["description"] is None


class TestExport:
    def test_frame_exports_as_a_zipped_ro_crate(self, client, frame):
        resp = client.get("/api/science/frames/s1/export")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/zip"
        assert 's1-ro-crate.zip' in resp.headers["content-disposition"]

        import io
        import zipfile

        with zipfile.ZipFile(io.BytesIO(resp.content)) as archive:
            names = archive.namelist()
            assert "ro-crate-metadata.json" in names
            # Artifact bytes travel with the metadata, not just references.
            assert any(name.startswith("data/") for name in names)

    def test_404_for_a_frame_with_nothing_to_export(self, client, science_runtime, db):
        db.create_session("bare", source="cli")
        science_runtime.run_cell("bare", "x = 1")
        assert client.get("/api/science/frames/bare/export").status_code == 404


class TestReproduce:
    def test_default_gate_denies(self):
        # The shipped default, independent of whatever a server installed.
        assert science_api._deny_reproduction() is False

    def test_403_when_the_gate_is_closed(self, client, frame):
        resp = client.post(
            f"/api/science/versions/{frame['derived_version']}/reproduce"
        )
        assert resp.status_code == 403
        assert "local dashboard" in resp.json()["detail"]

    def test_runs_to_a_claim_when_allowed(self, client, frame, monkeypatch):
        monkeypatch.setattr(science_api, "_reproduce_gate", lambda: True)
        started = client.post(
            f"/api/science/versions/{frame['derived_version']}/reproduce"
        ).json()
        assert started["state"] == "running"

        # The pool has one worker; shutting it down waits for the job.
        science_api._reproduce_executor().shutdown(wait=True)
        science_api._repro_pool = None

        report = client.get(
            f"/api/science/reproductions/{started['job_id']}"
        ).json()
        assert report["state"] in ("done", "error")
        assert report["report"]["claim"] in (
            "reproduced",
            "diverged",
            "failed",
            "indeterminate",
            "ineligible",
        )

    def test_unknown_version_is_rejected_before_scheduling(
        self, client, monkeypatch
    ):
        monkeypatch.setattr(science_api, "_reproduce_gate", lambda: True)
        assert (
            client.post("/api/science/versions/nope/reproduce").status_code == 404
        )

    def test_404_for_unknown_job(self, client):
        assert client.get("/api/science/reproductions/nope").status_code == 404
