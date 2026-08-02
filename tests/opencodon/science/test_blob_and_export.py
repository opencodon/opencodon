"""Blob store contracts + RO-Crate export shape."""

import json

from opencodon.science.blobstore import BlobStore
from opencodon.science.rocrate import export_rocrate


class TestBlobStore:
    def test_put_bytes_dedupes_by_content(self, tmp_path):
        blobs = BlobStore(tmp_path / "blobs")
        a = blobs.put_bytes(b"same content")
        b = blobs.put_bytes(b"same content")
        assert a.sha256 == b.sha256
        assert blobs.read_bytes(a.sha256) == b"same content"

    def test_put_path_and_materialize_roundtrip(self, tmp_path):
        blobs = BlobStore(tmp_path / "blobs")
        src = tmp_path / "input.bin"
        src.write_bytes(b"\x00\x01payload")
        ref = blobs.put_path(src)
        assert ref.size_bytes == 9
        dest = blobs.materialize(ref.sha256, tmp_path / "out" / "copy.bin")
        assert dest.read_bytes() == b"\x00\x01payload"
        # The materialized copy is independent of the CAS path.
        dest.write_bytes(b"tampered")
        assert blobs.read_bytes(ref.sha256) == b"\x00\x01payload"


class TestExport:
    def test_rocrate_captures_files_and_actions(self, science_runtime, db, tmp_path):
        db.create_session("s1", source="cli")
        raw = science_runtime.run_cell(
            "s1", "save_artifact('a,b\\n1,2\\n', 'data.csv')"
        )["artifacts"][0]
        code = (
            f"path = load_artifact('{raw['version_id']}')\n"
            "save_artifact(open(path).read().upper(), 'data_upper.csv')\n"
        )
        science_runtime.run_cell("s1", code, inputs=[raw["version_id"]])

        out = tmp_path / "crate"
        manifest_path = export_rocrate("s1", out, runtime=science_runtime)
        manifest = json.loads(manifest_path.read_text())
        graph = {e["@id"]: e for e in manifest["@graph"]}

        dataset = graph["./"]
        parts = {p["@id"] for p in dataset["hasPart"]}
        assert "data/data.csv@v1" in parts
        assert "data/data_upper.csv@v1" in parts
        # Exported bytes match the recorded blobs.
        assert (out / "data" / "data.csv@v1").read_bytes() == b"a,b\n1,2\n"

        file_entity = graph["data/data_upper.csv@v1"]
        assert file_entity["sha256"]
        action = graph[file_entity["resultOf"]["@id"]]
        assert action["@type"] == "CreateAction"
        assert {o["@id"] for o in action["object"]} == {"data/data.csv@v1"}
        assert {r["@id"] for r in action["result"]} == {"data/data_upper.csv@v1"}
        assert action["actionStatus"] == "CompletedActionStatus"
