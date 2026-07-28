"""Read-only HTTP surface over the science layer.

The dashboard's science pages (frames, artifacts, lineage, provenance) read
through this router. Every route is a ``GET``: the web UI observes the
execution record, it never writes to it and never submits code. Running
analysis stays in the CLI/TUI, so there is no browser-originated execution
path to secure — see ``docs/design/web-ui-redesign.md``.

Two seams keep this module independent of ``web_server``'s 18k-line module:

- :func:`set_db_opener` — the server injects its profile-aware SessionDB
  opener at import time, so ``?profile=`` works exactly as it does for the
  session routes. Without it (tests, embedding) the module opens this
  process's own ``state.db``.
- :func:`set_blob_store_opener` — same idea for the content-addressed blob
  store that backs artifact bytes.

Frames vs sessions: a *frame* is a root session plus its compression-chain
descendants. ``artifacts.root_session_id`` is already keyed that way; cells
are keyed by the individual ``session_id``, so frame-scoped cell reads expand
the chain first. Cell and artifact rows outlive session deletion by design
(soft references, see ``science/schema.py``), so reads never inner-join
``sessions`` to reach them.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/science", tags=["science"])

# Bytes returned inline by the ``/content`` preview route. Larger payloads are
# truncated with ``truncated: true``; ``/download`` serves the whole blob.
CONTENT_PREVIEW_MAX_BYTES = 256 * 1024

# Cell source / stream text is clipped in *list* responses only; detail routes
# return the recorded value untouched.
LIST_TEXT_CLIP = 2000


# ── injectable seams ────────────────────────────────────────────────

_db_opener: Optional[Callable[[Optional[str]], Any]] = None
_blob_opener: Optional[Callable[[], Any]] = None


def set_db_opener(opener: Callable[[Optional[str]], Any]) -> None:
    """Install the profile-aware ``SessionDB`` opener used by every route."""
    global _db_opener
    _db_opener = opener


def set_blob_store_opener(opener: Callable[[], Any]) -> None:
    """Install the blob-store accessor used by content/download routes."""
    global _blob_opener
    _blob_opener = opener


def _open_db(profile: Optional[str]):
    if _db_opener is not None:
        return _db_opener(profile)
    from opencodon_state import SessionDB

    return SessionDB()


def _blobs():
    if _blob_opener is not None:
        return _blob_opener()
    from science.blobstore import get_blob_store

    return get_blob_store()


# ── sqlite helpers ──────────────────────────────────────────────────
#
# Same read pattern as ScienceStore: take the SessionDB connection lock and
# read through the shared connection (rows arrive as sqlite3.Row).


def _rows(db, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    with db._lock:
        return [dict(r) for r in db._conn.execute(sql, params).fetchall()]


def _row(db, sql: str, params: tuple = ()) -> Optional[Dict[str, Any]]:
    with db._lock:
        row = db._conn.execute(sql, params).fetchone()
    return dict(row) if row else None


def _placeholders(values) -> str:
    return ",".join("?" for _ in values)


def _clip(text: Optional[str], limit: int = LIST_TEXT_CLIP) -> Optional[str]:
    if text is None:
        return None
    return text if len(text) <= limit else text[:limit] + "\n…[clipped]"


def _json_or_none(raw: Optional[str]):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


# ── frame membership ────────────────────────────────────────────────


def _frame_session_ids(db, frame_id: str) -> List[str]:
    """Every session id in *frame_id*'s chain, the root included.

    Falls back to ``[frame_id]`` when the session row is gone but its
    execution rows survive.
    """
    rows = _rows(
        db,
        "SELECT id FROM sessions WHERE COALESCE(root_session_id, id) = ?",
        (frame_id,),
    )
    ids = [r["id"] for r in rows]
    return ids or [frame_id]


def _cells_for_sessions(db, session_ids: List[str]) -> List[Dict[str, Any]]:
    if not session_ids:
        return []
    return _rows(
        db,
        "SELECT * FROM execution_log "
        f"WHERE session_id IN ({_placeholders(session_ids)}) "
        "ORDER BY created_at, cell_index",
        tuple(session_ids),
    )


def _cell_summary(cell: Dict[str, Any], *, clip: bool = True) -> Dict[str, Any]:
    """Shape one execution_log row for the wire."""
    text = (lambda v: _clip(v)) if clip else (lambda v: v)
    return {
        "cell_id": cell["id"],
        "session_id": cell["session_id"],
        "cell_index": cell["cell_index"],
        "kernel_id": cell.get("kernel_id"),
        "kernel_kind": cell.get("kernel_kind"),
        "language": cell.get("language"),
        "env_name": cell.get("env_name"),
        "source": text(cell.get("source")),
        "stdout": text(cell.get("stdout")),
        "stderr": text(cell.get("stderr")),
        "exit_status": cell.get("exit_status"),
        "error_lineno": cell.get("error_lineno"),
        # origin/user_intervention are always reported even though the web UI
        # cannot create user-origin cells today — a future human-in-the-kernel
        # feature lands without changing this contract.
        "origin": cell.get("origin") or "agent",
        "user_intervention": cell.get("user_intervention"),
        # Present-participle action label the agent supplies with the cell;
        # null on cells recorded before the field existed.
        "description": cell.get("description"),
        "files_written": _json_or_none(cell.get("files_written")),
        "files_read": _json_or_none(cell.get("files_read")),
        "created_at": cell.get("created_at"),
    }


def _version_summary(version: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "version_id": version["id"],
        "artifact_id": version["artifact_id"],
        "version_number": version["version_number"],
        "session_id": version.get("session_id"),
        "content_type": version.get("content_type"),
        "size_bytes": version.get("size_bytes"),
        "sha256": version.get("checksum"),
        "language": version.get("language"),
        "is_intermediate": bool(version.get("is_intermediate")),
        "producing_cell_id": version.get("producing_cell_id"),
        "parent_version_id": version.get("parent_version_id"),
        "env_snapshot_hash": version.get("env_snapshot_hash"),
        "created_at": version.get("created_at"),
    }


def _artifact_summary(artifact: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "artifact_id": artifact["id"],
        "frame_id": artifact.get("root_session_id"),
        "session_id": artifact.get("session_id"),
        "filename": artifact.get("filename"),
        "is_user_upload": bool(artifact.get("is_user_upload")),
        "is_ephemeral": bool(artifact.get("is_ephemeral")),
        "latest_version_id": artifact.get("latest_version_id"),
        "latest_version_number": artifact.get("latest_version_number"),
        "latest_content_type": artifact.get("latest_content_type"),
        "latest_size_bytes": artifact.get("latest_size_bytes"),
        "latest_sha256": artifact.get("latest_checksum"),
        "superseded_by_artifact_id": artifact.get("superseded_by_artifact_id"),
        "created_at": artifact.get("created_at"),
    }


_ARTIFACT_WITH_LATEST_SQL = """
    SELECT a.*, v.version_number AS latest_version_number,
           v.checksum       AS latest_checksum,
           v.size_bytes     AS latest_size_bytes,
           v.content_type   AS latest_content_type
      FROM artifacts a
      LEFT JOIN artifact_versions v ON v.id = a.latest_version_id
"""


# ── routes: frames ──────────────────────────────────────────────────


# Rollup for the frames index, aggregated in SQLite rather than in Python.
#
# ``frame_of`` maps every execution row onto its root session in one pass; the
# LEFT JOIN to sessions keeps frames whose session row was pruned (their
# execution record survives, so they must stay listed). Sorting and the
# LIMIT/OFFSET window both happen in SQL so the page never materialises the
# whole execution log.
_FRAME_ROLLUP_SQL = """
    WITH frame_of AS (
        SELECT e.id            AS cell_id,
               e.exit_status   AS exit_status,
               e.language      AS language,
               e.created_at    AS created_at,
               COALESCE(s.root_session_id, e.session_id) AS frame_id
          FROM execution_log e
          LEFT JOIN sessions s ON s.id = e.session_id
    ),
    cell_agg AS (
        SELECT frame_id,
               COUNT(*)                                          AS cell_count,
               SUM(CASE WHEN exit_status <> 'ok' THEN 1 ELSE 0 END) AS failed_cell_count,
               MAX(created_at)                                   AS last_cell_at
          FROM frame_of
         GROUP BY frame_id
    ),
    artifact_agg AS (
        SELECT root_session_id AS frame_id, COUNT(*) AS artifact_count
          FROM artifacts
         GROUP BY root_session_id
    ),
    frames AS (
        SELECT frame_id FROM cell_agg
        UNION
        SELECT frame_id FROM artifact_agg
    )
    SELECT f.frame_id                              AS frame_id,
           COALESCE(c.cell_count, 0)               AS cell_count,
           COALESCE(c.failed_cell_count, 0)        AS failed_cell_count,
           COALESCE(a.artifact_count, 0)           AS artifact_count,
           c.last_cell_at                          AS last_cell_at,
           s.id                                    AS session_id,
           s.title, s.model, s.cwd, s.source, s.started_at, s.ended_at,
           s.profile_name
      FROM frames f
      LEFT JOIN cell_agg     c ON c.frame_id = f.frame_id
      LEFT JOIN artifact_agg a ON a.frame_id = f.frame_id
      LEFT JOIN sessions     s ON s.id       = f.frame_id
     ORDER BY COALESCE(c.last_cell_at, s.started_at, 0) DESC
     LIMIT ? OFFSET ?
"""

_FRAME_COUNT_SQL = """
    SELECT COUNT(*) AS n FROM (
        SELECT COALESCE(s.root_session_id, e.session_id) AS frame_id
          FROM execution_log e
          LEFT JOIN sessions s ON s.id = e.session_id
        UNION
        SELECT root_session_id FROM artifacts
    )
"""


@router.get("/frames")
def list_frames(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    profile: Optional[str] = None,
):
    """Frames that carry a science record — cells, artifacts, or both."""
    db = _open_db(profile)
    try:
        total = (_row(db, _FRAME_COUNT_SQL) or {"n": 0})["n"]
        rows = _rows(db, _FRAME_ROLLUP_SQL, (limit, offset))
        if not rows:
            return {"frames": [], "total": total, "limit": limit, "offset": offset}

        # Languages are a small per-frame set; fetch them only for the page.
        ids = [r["frame_id"] for r in rows]
        languages: Dict[str, List[str]] = {}
        for row in _rows(
            db,
            "SELECT DISTINCT COALESCE(s.root_session_id, e.session_id) AS frame_id, "
            "       e.language AS language "
            "  FROM execution_log e "
            "  LEFT JOIN sessions s ON s.id = e.session_id "
            f" WHERE COALESCE(s.root_session_id, e.session_id) IN ({_placeholders(ids)}) "
            "   AND e.language IS NOT NULL",
            tuple(ids),
        ):
            languages.setdefault(row["frame_id"], []).append(row["language"])

        frames = [
            {
                "frame_id": row["frame_id"],
                "title": row["title"],
                "model": row["model"],
                "cwd": row["cwd"],
                "source": row["source"],
                "profile": row["profile_name"],
                "started_at": row["started_at"],
                "ended_at": row["ended_at"],
                # True when the session row is gone but its execution record
                # survives — provenance outlives retention.
                "session_missing": row["session_id"] is None,
                "cell_count": row["cell_count"],
                "failed_cell_count": row["failed_cell_count"],
                "artifact_count": row["artifact_count"],
                "last_cell_at": row["last_cell_at"],
                "languages": sorted(languages.get(row["frame_id"], [])),
            }
            for row in rows
        ]
        return {
            "frames": frames,
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    finally:
        db.close()


@router.get("/frames/{frame_id}")
def get_frame(frame_id: str, profile: Optional[str] = None):
    """Frame detail: results first, then the trace, then the environments."""
    db = _open_db(profile)
    try:
        session_ids = _frame_session_ids(db, frame_id)
        cells = _cells_for_sessions(db, session_ids)
        artifacts = _rows(
            db,
            _ARTIFACT_WITH_LATEST_SQL + " WHERE a.root_session_id = ? ORDER BY a.created_at",
            (frame_id,),
        )
        if not cells and not artifacts:
            raise HTTPException(status_code=404, detail="frame not found")

        meta = _row(
            db,
            "SELECT id, title, model, cwd, source, started_at, ended_at, profile_name "
            "FROM sessions WHERE id = ?",
            (frame_id,),
        )

        # One row per distinct environment the frame executed in.
        environments: Dict[tuple, Dict[str, Any]] = {}
        for cell in cells:
            key = (cell.get("language"), cell.get("env_name"))
            env = environments.setdefault(
                key,
                {
                    "language": cell.get("language"),
                    "env_name": cell.get("env_name"),
                    "kernel_kind": cell.get("kernel_kind"),
                    "cell_count": 0,
                    "snapshot": _json_or_none(cell.get("env_snapshot")),
                },
            )
            env["cell_count"] += 1

        return {
            "frame_id": frame_id,
            "title": (meta or {}).get("title"),
            "model": (meta or {}).get("model"),
            "cwd": (meta or {}).get("cwd"),
            "source": (meta or {}).get("source"),
            "profile": (meta or {}).get("profile_name"),
            "started_at": (meta or {}).get("started_at"),
            "ended_at": (meta or {}).get("ended_at"),
            "session_missing": meta is None,
            "session_ids": session_ids,
            "cell_count": len(cells),
            "failed_cell_count": sum(1 for c in cells if c["exit_status"] != "ok"),
            "artifacts": [_artifact_summary(a) for a in artifacts],
            "environments": list(environments.values()),
        }
    finally:
        db.close()


@router.get("/frames/{frame_id}/cells")
def get_frame_cells(
    frame_id: str,
    since: Optional[float] = Query(
        None,
        description=(
            "Return only cells recorded after this epoch-second cursor. The "
            "frame page polls with the newest cursor it holds to pick up cells "
            "as they are recorded."
        ),
    ),
    profile: Optional[str] = None,
):
    """The frame's execution trace, with per-cell host-call counts.

    ``cursor`` in the response is the newest ``created_at`` the caller has now
    seen — pass it back as ``since`` on the next poll. It is null when the
    frame has no cells, in which case the caller keeps its previous cursor.
    """
    db = _open_db(profile)
    try:
        cells = _cells_for_sessions(db, _frame_session_ids(db, frame_id))
        cursor = max((c["created_at"] or 0) for c in cells) if cells else None
        if since is not None:
            cells = [c for c in cells if (c["created_at"] or 0) > since]
        if not cells:
            return {"frame_id": frame_id, "cells": [], "cursor": cursor}

        ids = [c["id"] for c in cells]
        host_counts: Dict[str, int] = {}
        for row in _rows(
            db,
            "SELECT execution_log_id, COUNT(*) AS n FROM host_call_log "
            f"WHERE execution_log_id IN ({_placeholders(ids)}) "
            "GROUP BY execution_log_id",
            tuple(ids),
        ):
            host_counts[row["execution_log_id"]] = row["n"]

        produced: Dict[str, int] = {}
        for row in _rows(
            db,
            "SELECT producing_cell_id, COUNT(*) AS n FROM artifact_versions "
            f"WHERE producing_cell_id IN ({_placeholders(ids)}) "
            "GROUP BY producing_cell_id",
            tuple(ids),
        ):
            produced[row["producing_cell_id"]] = row["n"]

        out = []
        for cell in cells:
            summary = _cell_summary(cell)
            summary["host_call_count"] = host_counts.get(cell["id"], 0)
            summary["version_count"] = produced.get(cell["id"], 0)
            out.append(summary)
        return {"frame_id": frame_id, "cells": out, "cursor": cursor}
    finally:
        db.close()


# ── routes: cells ───────────────────────────────────────────────────


@router.get("/cells/{cell_id}")
def get_cell(cell_id: str, profile: Optional[str] = None):
    """One cell, its host calls, and the versions it produced."""
    db = _open_db(profile)
    try:
        cell = _row(db, "SELECT * FROM execution_log WHERE id = ?", (cell_id,))
        if cell is None:
            raise HTTPException(status_code=404, detail="cell not found")

        host_calls = [
            {
                "seq": call["seq"],
                "method": call["method"],
                "args": _json_or_none(call.get("args_json")),
                "derivable": bool(call.get("derivable")),
                "data_inline": call.get("data_inline"),
                "data_ref": call.get("data_ref"),
                "error": call.get("error"),
                "bytes": call.get("bytes"),
                "created_at": call.get("created_at"),
            }
            for call in _rows(
                db,
                "SELECT * FROM host_call_log WHERE execution_log_id = ? ORDER BY seq",
                (cell_id,),
            )
        ]

        versions = _rows(
            db,
            "SELECT * FROM artifact_versions WHERE producing_cell_id = ? "
            "ORDER BY created_at",
            (cell_id,),
        )

        detail = _cell_summary(cell, clip=False)
        detail["env_snapshot"] = _json_or_none(cell.get("env_snapshot"))
        detail["host_calls"] = host_calls
        detail["versions"] = [_version_summary(v) for v in versions]
        return detail
    finally:
        db.close()


# ── routes: artifacts ───────────────────────────────────────────────


@router.get("/artifacts")
def list_artifacts(
    frame_id: Optional[str] = None,
    search: Optional[str] = None,
    include_ephemeral: bool = False,
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    profile: Optional[str] = None,
):
    """The durable plane, across frames.

    Ordering and filename matching mirror the ``list_artifacts`` agent tool so
    a name resolves the same way for the agent and for the reader.
    """
    db = _open_db(profile)
    try:
        clauses, params = [], []
        if frame_id:
            clauses.append("a.root_session_id = ?")
            params.append(frame_id)
        if search:
            clauses.append("a.filename LIKE ?")
            params.append(f"%{search}%")
        if not include_ephemeral:
            clauses.append("a.is_ephemeral = 0")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""

        total = (
            _row(db, f"SELECT COUNT(*) AS n FROM artifacts a{where}", tuple(params))
            or {"n": 0}
        )["n"]
        rows = _rows(
            db,
            _ARTIFACT_WITH_LATEST_SQL + where + " ORDER BY a.created_at DESC LIMIT ? OFFSET ?",
            tuple(params) + (limit, offset),
        )
        return {
            "artifacts": [_artifact_summary(r) for r in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    finally:
        db.close()


@router.get("/artifacts/{artifact_id}")
def get_artifact(artifact_id: str, profile: Optional[str] = None):
    """Artifact identity plus its full version timeline."""
    db = _open_db(profile)
    try:
        artifact = _row(
            db, _ARTIFACT_WITH_LATEST_SQL + " WHERE a.id = ?", (artifact_id,)
        )
        if artifact is None:
            raise HTTPException(status_code=404, detail="artifact not found")
        versions = _rows(
            db,
            "SELECT * FROM artifact_versions WHERE artifact_id = ? "
            "ORDER BY version_number",
            (artifact_id,),
        )
        detail = _artifact_summary(artifact)
        detail["versions"] = [_version_summary(v) for v in versions]
        return detail
    finally:
        db.close()


# ── routes: versions ────────────────────────────────────────────────


def _require_version(db, version_id: str) -> Dict[str, Any]:
    version = _row(
        db, "SELECT * FROM artifact_versions WHERE id = ?", (version_id,)
    )
    if version is None:
        raise HTTPException(status_code=404, detail="artifact version not found")
    return version


@router.get("/versions/{version_id}")
def get_version(version_id: str, profile: Optional[str] = None):
    """A version, its artifact, the cell that produced it, and its edges."""
    db = _open_db(profile)
    try:
        version = _require_version(db, version_id)
        artifact = _row(
            db, "SELECT * FROM artifacts WHERE id = ?", (version["artifact_id"],)
        )
        producing_cell = None
        if version.get("producing_cell_id"):
            cell = _row(
                db,
                "SELECT * FROM execution_log WHERE id = ?",
                (version["producing_cell_id"],),
            )
            if cell is not None:
                producing_cell = _cell_summary(cell, clip=False)

        detail = _version_summary(version)
        detail["filename"] = (artifact or {}).get("filename")
        detail["frame_id"] = (artifact or {}).get("root_session_id")
        detail["producing_cell"] = producing_cell
        detail["depends_on"] = _rows(
            db,
            "SELECT depends_on_version_id AS version_id, reference_name "
            "FROM artifact_dependencies WHERE artifact_version_id = ? "
            "ORDER BY created_at",
            (version_id,),
        )
        return detail
    finally:
        db.close()


@router.get("/versions/{version_id}/lineage")
def get_version_lineage(
    version_id: str,
    direction: str = Query("upstream", pattern="^(upstream|downstream)$"),
    profile: Optional[str] = None,
):
    """Transitive provenance, nearest first, via ``ScienceStore.lineage``."""
    db = _open_db(profile)
    try:
        _require_version(db, version_id)
        from science.store import ScienceStore

        rows = ScienceStore(db).lineage(version_id, direction=direction)
        filenames = {
            r["id"]: r["filename"]
            for r in _rows(db, "SELECT id, filename FROM artifacts")
        }
        lineage = []
        for row in rows:
            entry = _version_summary(row)
            entry["depth"] = row.get("depth")
            entry["filename"] = filenames.get(row["artifact_id"])
            lineage.append(entry)
        return {
            "version_id": version_id,
            "direction": direction,
            "lineage": lineage,
        }
    finally:
        db.close()


@router.get("/versions/{version_id}/content")
def get_version_content(version_id: str, profile: Optional[str] = None):
    """Bounded inline preview of a version's bytes.

    Text decodes to ``text``; anything that is not valid UTF-8 reports
    ``binary: true`` and no body — the viewer layer fetches those through
    ``/download`` instead.
    """
    db = _open_db(profile)
    try:
        version = _require_version(db, version_id)
        checksum = version["checksum"]
        blobs = _blobs()
        if not blobs.exists(checksum):
            raise HTTPException(status_code=404, detail="artifact bytes are missing")

        data = blobs.read_bytes(checksum)
        truncated = len(data) > CONTENT_PREVIEW_MAX_BYTES
        head = data[:CONTENT_PREVIEW_MAX_BYTES]
        try:
            text = head.decode("utf-8")
        except UnicodeDecodeError:
            return {
                "version_id": version_id,
                "content_type": version.get("content_type"),
                "size_bytes": version.get("size_bytes"),
                "binary": True,
                "truncated": truncated,
                "text": None,
            }
        return {
            "version_id": version_id,
            "content_type": version.get("content_type"),
            "size_bytes": version.get("size_bytes"),
            "binary": False,
            "truncated": truncated,
            "text": text,
        }
    finally:
        db.close()


@router.get("/versions/{version_id}/download")
def download_version(version_id: str, profile: Optional[str] = None):
    """The version's raw bytes, straight from the content-addressed store."""
    db = _open_db(profile)
    try:
        version = _require_version(db, version_id)
        artifact = _row(
            db, "SELECT filename FROM artifacts WHERE id = ?", (version["artifact_id"],)
        )
        blobs = _blobs()
        if not blobs.exists(version["checksum"]):
            raise HTTPException(status_code=404, detail="artifact bytes are missing")
        data = blobs.read_bytes(version["checksum"])
    finally:
        db.close()

    filename = (artifact or {}).get("filename") or f"{version_id}.bin"
    # Quote-strip keeps a filename with a quote from breaking the header.
    safe = filename.replace('"', "")
    return Response(
        content=data,
        media_type=version.get("content_type") or "application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{safe}"',
            "X-Content-SHA256": version["checksum"],
        },
    )


@router.get("/snapshots/{digest}")
def get_snapshot(digest: str, profile: Optional[str] = None):
    """A ``content_snapshots`` payload — large host-call results, env dumps."""
    db = _open_db(profile)
    try:
        row = _row(
            db,
            "SELECT hash, content, size_bytes, created_at FROM content_snapshots "
            "WHERE hash = ?",
            (digest,),
        )
        if row is None:
            raise HTTPException(status_code=404, detail="snapshot not found")
        return {
            "hash": row["hash"],
            "size_bytes": row["size_bytes"],
            "created_at": row["created_at"],
            "content": row["content"],
        }
    finally:
        db.close()
