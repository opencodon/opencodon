"""RO-Crate export of a root session's provenance (science/rocrate.py).

Produces a self-contained directory: artifact version bytes under ``data/``
plus ``ro-crate-metadata.json`` (RO-Crate 1.1 JSON-LD) describing each
version as a File and each producing cell as a CreateAction whose objects
are the input versions and results the output versions — the provenance DAG
in an archive another tool can read.

Leaner than the donor's rocrate.py on purpose: it covers the science tables
this port maintains (cells, versions, dependency edges) and nothing else.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List

ROCRATE_CONTEXT = "https://w3id.org/ro/crate/1.1/context"


def export_rocrate(root_session_id: str, out_dir: Path, *, runtime=None) -> Path:
    """Export every artifact + producing cell of a root session.

    Returns the path of ``ro-crate-metadata.json``.
    """
    if runtime is None:
        from science.runtime import get_science_runtime

        runtime = get_science_runtime()
    store, blobs = runtime.store, runtime.blobs

    out_dir = Path(out_dir)
    data_dir = out_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    graph: List[Dict[str, Any]] = []
    file_ids: List[Dict[str, str]] = []
    cell_ids: Dict[str, str] = {}

    artifacts = store.artifacts_for_root(root_session_id)
    version_entity: Dict[str, str] = {}

    for artifact in artifacts:
        versions = store._rows(
            "SELECT * FROM artifact_versions WHERE artifact_id = ? "
            "ORDER BY version_number",
            (artifact["id"],),
        )
        for version in versions:
            rel = f"data/{artifact['filename']}@v{version['version_number']}"
            try:
                blobs.materialize(version["checksum"], out_dir / rel)
            except FileNotFoundError:
                continue
            version_entity[version["id"]] = rel
            file_ids.append({"@id": rel})
            entity = {
                "@id": rel,
                "@type": "File",
                "name": artifact["filename"],
                "version": version["version_number"],
                "contentSize": version["size_bytes"],
                "encodingFormat": version["content_type"],
                "sha256": version["checksum"],
                "dateCreated": _iso(version["created_at"]),
            }
            producing = version.get("producing_cell_id")
            if producing:
                entity["resultOf"] = {"@id": f"#cell-{producing}"}
                cell_ids.setdefault(producing, version["session_id"] or "")
            graph.append(entity)

    for cell_id in list(cell_ids):
        cell = store.get_cell(cell_id)
        if cell is None:
            continue
        inputs = [
            {"@id": version_entity[dep["depends_on_version_id"]]}
            for out_vid, deps in _deps_by_producing_cell(store, cell_id).items()
            for dep in deps
            if dep["depends_on_version_id"] in version_entity
        ]
        outputs = [
            {"@id": rel}
            for vid, rel in version_entity.items()
            if (store.get_version(vid) or {}).get("producing_cell_id") == cell_id
        ]
        graph.append(
            {
                "@id": f"#cell-{cell_id}",
                "@type": "CreateAction",
                "name": f"cell {cell['cell_index']} ({cell['language']})",
                "instrument": {"@id": "#opencodon-science"},
                "description": cell["source"],
                "actionStatus": (
                    "CompletedActionStatus"
                    if cell["exit_status"] == "ok"
                    else "FailedActionStatus"
                ),
                "startTime": _iso(cell["created_at"]),
                "object": _dedupe(inputs),
                "result": outputs,
            }
        )

    metadata = {
        "@context": ROCRATE_CONTEXT,
        "@graph": [
            {
                "@id": "ro-crate-metadata.json",
                "@type": "CreativeWork",
                "about": {"@id": "./"},
                "conformsTo": {"@id": "https://w3id.org/ro/crate/1.1"},
            },
            {
                "@id": "./",
                "@type": "Dataset",
                "name": f"opencodon science session {root_session_id}",
                "datePublished": _iso(time.time()),
                "hasPart": file_ids,
            },
            {
                "@id": "#opencodon-science",
                "@type": "SoftwareApplication",
                "name": "opencodon science layer",
            },
            *graph,
        ],
    }
    manifest = out_dir / "ro-crate-metadata.json"
    manifest.write_text(json.dumps(metadata, indent=2, ensure_ascii=False))
    return manifest


def _deps_by_producing_cell(store, cell_id: str) -> Dict[str, List[dict]]:
    """Dependency edges of every version the cell produced, keyed by version."""
    out: Dict[str, List[dict]] = {}
    rows = store._rows(
        """SELECT d.* FROM artifact_dependencies d
             JOIN artifact_versions v ON v.id = d.artifact_version_id
            WHERE v.producing_cell_id = ?""",
        (cell_id,),
    )
    for row in rows:
        out.setdefault(row["artifact_version_id"], []).append(row)
    return out


def _dedupe(refs: List[dict]) -> List[dict]:
    seen, out = set(), []
    for ref in refs:
        if ref["@id"] not in seen:
            seen.add(ref["@id"])
            out.append(ref)
    return out


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch or 0))
