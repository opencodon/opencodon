"""ScienceStore — typed accessors for the science-layer tables.

All writes go through ``SessionDB._execute_write`` (BEGIN IMMEDIATE + jitter
retry), so a science row commits with the same durability and contention
semantics as a message row — and a caller composing a multi-row write (e.g.
cell + artifact version + dependency edges) inside one closure gets a single
atomic transaction. Reads follow the SessionDB read pattern (connection lock,
``sqlite3.Row``).

The store deliberately holds no state of its own beyond the SessionDB handle;
it is safe to construct ad hoc wherever a ``SessionDB`` already exists.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Union

if TYPE_CHECKING:  # pragma: no cover
    from opencodon_state import SessionDB

# host.* call results larger than this are stored once in content_snapshots
# (content-addressed) and referenced via data_ref, instead of being embedded
# inline in the host_call_log row.
SNAPSHOT_INLINE_MAX_BYTES = 16 * 1024

# Depth cap for lineage traversal — guards degenerate dependency chains.
LINEAGE_MAX_DEPTH = 64


def _new_id() -> str:
    return uuid.uuid4().hex


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _insert_snapshot(conn, content: str) -> str:
    """Insert *content* into content_snapshots if absent; return its hash."""
    digest = _content_hash(content)
    conn.execute(
        "INSERT OR IGNORE INTO content_snapshots (hash, content, size_bytes, created_at) "
        "VALUES (?, ?, ?, ?)",
        (digest, content, len(content.encode("utf-8")), time.time()),
    )
    return digest


class ScienceStore:
    """Accessors for execution_log / host_call_log / artifacts / snapshots."""

    def __init__(self, db: "SessionDB"):
        self._db = db

    # ── read helpers ────────────────────────────────────────────────

    def _rows(self, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
        with self._db._lock:
            rows = self._db._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def _row(self, sql: str, params: tuple = ()) -> Optional[Dict[str, Any]]:
        with self._db._lock:
            row = self._db._conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    # ── execution_log ───────────────────────────────────────────────

    def record_cell(
        self,
        session_id: str,
        source: str,
        language: str,
        kernel_id: str,
        *,
        exit_status: str = "ok",
        stdout: str = None,
        stderr: str = None,
        cell_index: int = None,
        kernel_kind: str = None,
        env_name: str = None,
        env_snapshot: str = None,
        env_lock_hash: str = None,
        kernel_location: str = None,
        error_lineno: int = None,
        traceback: str = None,
        display_count: int = 0,
        has_magics: int = 0,
        origin: str = "agent",
        user_intervention: str = None,
        description: str = None,
        files_written: list = None,
        files_read: list = None,
        cell_id: str = None,
    ) -> str:
        """Record one executed code cell; returns the execution_log id.

        ``cell_index`` is assigned as the next index for the session when not
        given (computed inside the write transaction, so concurrent writers
        cannot collide).
        """
        cell_id = cell_id or _new_id()

        def _do(conn):
            idx = cell_index
            if idx is None:
                idx = conn.execute(
                    "SELECT COALESCE(MAX(cell_index), -1) + 1 FROM execution_log "
                    "WHERE session_id = ?",
                    (session_id,),
                ).fetchone()[0]
            conn.execute(
                """INSERT INTO execution_log (
                       id, session_id, cell_index, kernel_id, kernel_kind,
                       language, env_name, env_snapshot, env_lock_hash,
                       kernel_location, source, stdout, stderr,
                       exit_status, error_lineno, traceback, display_count,
                       has_magics, origin, user_intervention, description,
                       files_written, files_read, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    cell_id,
                    session_id,
                    idx,
                    kernel_id,
                    kernel_kind,
                    language,
                    env_name,
                    env_snapshot,
                    env_lock_hash,
                    kernel_location,
                    source,
                    stdout,
                    stderr,
                    exit_status,
                    error_lineno,
                    traceback,
                    display_count,
                    has_magics,
                    origin,
                    user_intervention,
                    description,
                    json.dumps(files_written) if files_written else None,
                    json.dumps(files_read) if files_read else None,
                    time.time(),
                ),
            )
            return cell_id

        return self._db._execute_write(_do)

    def update_cell(self, cell_id: str, **fields) -> None:
        """Finalize a cell row inserted with exit_status='running'.

        Accepts any execution_log column; list values (files_written /
        files_read) are JSON-encoded.
        """
        if not fields:
            return
        columns, params = [], []
        for name, value in fields.items():
            if isinstance(value, (list, tuple)):
                value = json.dumps(list(value))
            columns.append(f'"{name}" = ?')
            params.append(value)
        params.append(cell_id)

        def _do(conn):
            conn.execute(
                f"UPDATE execution_log SET {', '.join(columns)} WHERE id = ?",
                params,
            )

        self._db._execute_write(_do)

    def get_cell(self, cell_id: str) -> Optional[Dict[str, Any]]:
        return self._row("SELECT * FROM execution_log WHERE id = ?", (cell_id,))

    def cells_for_session(self, session_id: str) -> List[Dict[str, Any]]:
        return self._rows(
            "SELECT * FROM execution_log WHERE session_id = ? ORDER BY cell_index",
            (session_id,),
        )

    # ── host_call_log ───────────────────────────────────────────────

    def record_host_call(
        self,
        execution_log_id: str,
        method: str,
        args: Union[str, dict, list, None] = None,
        *,
        result: Union[str, dict, list, None] = None,
        derivable: bool = False,
        error: str = None,
        inline_max_bytes: int = SNAPSHOT_INLINE_MAX_BYTES,
    ) -> Dict[str, Any]:
        """Record one host.* call made inside a cell.

        Small results are stored inline (``data_inline``); results larger
        than *inline_max_bytes* are content-addressed into
        ``content_snapshots`` and referenced via ``data_ref`` — the snapshot
        insert and the call row commit in one transaction.

        Returns ``{"id", "seq", "data_ref"}`` (``data_ref`` is None when the
        result was inlined or absent).
        """
        args_json = args if isinstance(args, str) else json.dumps(args or {})
        content: Optional[str] = None
        if result is not None:
            content = result if isinstance(result, str) else json.dumps(result)
        nbytes = len(content.encode("utf-8")) if content is not None else 0

        def _do(conn):
            seq = conn.execute(
                "SELECT COALESCE(MAX(seq), -1) + 1 FROM host_call_log "
                "WHERE execution_log_id = ?",
                (execution_log_id,),
            ).fetchone()[0]
            data_inline = data_ref = None
            if content is not None:
                if nbytes > inline_max_bytes:
                    data_ref = _insert_snapshot(conn, content)
                else:
                    data_inline = content
            cursor = conn.execute(
                """INSERT INTO host_call_log (
                       execution_log_id, seq, method, args_json, derivable,
                       data_inline, data_ref, error, bytes, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    execution_log_id,
                    seq,
                    method,
                    args_json,
                    1 if derivable else 0,
                    data_inline,
                    data_ref,
                    error,
                    nbytes,
                    time.time(),
                ),
            )
            return {"id": cursor.lastrowid, "seq": seq, "data_ref": data_ref}

        return self._db._execute_write(_do)

    def host_calls_for_cell(self, execution_log_id: str) -> List[Dict[str, Any]]:
        return self._rows(
            "SELECT * FROM host_call_log WHERE execution_log_id = ? ORDER BY seq",
            (execution_log_id,),
        )

    # ── content_snapshots ───────────────────────────────────────────

    def put_snapshot(self, content: str) -> str:
        """Store *content* content-addressed; returns its sha256 hash.

        Idempotent: identical content maps to the same hash and is stored
        exactly once.
        """
        return self._db._execute_write(lambda conn: _insert_snapshot(conn, content))

    def get_snapshot(self, digest: str) -> Optional[str]:
        row = self._row(
            "SELECT content FROM content_snapshots WHERE hash = ?", (digest,)
        )
        return row["content"] if row else None

    # ── artifacts & versions ────────────────────────────────────────

    def create_artifact(
        self,
        root_session_id: str,
        filename: str,
        *,
        session_id: str = None,
        is_user_upload: bool = False,
        is_ephemeral: bool = False,
        artifact_id: str = None,
    ) -> str:
        """Create an artifact identity row; returns the artifact id."""
        artifact_id = artifact_id or _new_id()

        def _do(conn):
            conn.execute(
                """INSERT INTO artifacts (
                       id, root_session_id, session_id, filename,
                       is_user_upload, is_ephemeral, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    artifact_id,
                    root_session_id,
                    session_id,
                    filename,
                    1 if is_user_upload else 0,
                    1 if is_ephemeral else 0,
                    time.time(),
                ),
            )
            return artifact_id

        return self._db._execute_write(_do)

    def add_version(
        self,
        artifact_id: str,
        *,
        checksum: str,
        size_bytes: int,
        storage_path: str,
        content_type: str = "application/octet-stream",
        session_id: str = None,
        language: str = None,
        is_intermediate: bool = False,
        producing_cell_id: str = None,
        parent_version_id: str = None,
        env_snapshot_hash: str = None,
        version_id: str = None,
        dependencies: List[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Append a new version to an artifact.

        Assigns the next ``version_number``, updates
        ``artifacts.latest_version_id``, and (optionally) inserts dependency
        edges — all in one transaction. *dependencies* is a list of
        ``{"depends_on_version_id": ..., "reference_name": ...}`` dicts
        (``reference_name`` optional).

        Returns ``{"id", "version_number"}``.
        """
        version_id = version_id or _new_id()

        def _do(conn):
            number = conn.execute(
                "SELECT COALESCE(MAX(version_number), 0) + 1 FROM artifact_versions "
                "WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchone()[0]
            conn.execute(
                """INSERT INTO artifact_versions (
                       id, artifact_id, version_number, session_id, content_type,
                       size_bytes, checksum, storage_path, language,
                       is_intermediate, producing_cell_id, parent_version_id,
                       env_snapshot_hash, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    version_id,
                    artifact_id,
                    number,
                    session_id,
                    content_type,
                    size_bytes,
                    checksum,
                    storage_path,
                    language,
                    1 if is_intermediate else 0,
                    producing_cell_id,
                    parent_version_id,
                    env_snapshot_hash,
                    time.time(),
                ),
            )
            conn.execute(
                "UPDATE artifacts SET latest_version_id = ? WHERE id = ?",
                (version_id, artifact_id),
            )
            for dep in dependencies or []:
                conn.execute(
                    """INSERT OR IGNORE INTO artifact_dependencies (
                           artifact_version_id, depends_on_version_id,
                           reference_name, created_at
                       ) VALUES (?, ?, ?, ?)""",
                    (
                        version_id,
                        dep["depends_on_version_id"],
                        dep.get("reference_name") or "",
                        time.time(),
                    ),
                )
            return {"id": version_id, "version_number": number}

        return self._db._execute_write(_do)

    def add_dependency(
        self,
        artifact_version_id: str,
        depends_on_version_id: str,
        reference_name: str = "",
    ) -> None:
        """Insert one provenance edge (idempotent)."""

        def _do(conn):
            conn.execute(
                """INSERT OR IGNORE INTO artifact_dependencies (
                       artifact_version_id, depends_on_version_id,
                       reference_name, created_at
                   ) VALUES (?, ?, ?, ?)""",
                (
                    artifact_version_id,
                    depends_on_version_id,
                    reference_name or "",
                    time.time(),
                ),
            )

        self._db._execute_write(_do)

    def get_artifact(self, artifact_id: str) -> Optional[Dict[str, Any]]:
        return self._row("SELECT * FROM artifacts WHERE id = ?", (artifact_id,))

    def find_artifact(
        self, root_session_id: str, filename: str
    ) -> Optional[Dict[str, Any]]:
        """The identity row for *filename* within a root session (newest)."""
        return self._row(
            "SELECT * FROM artifacts WHERE root_session_id = ? AND filename = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (root_session_id, filename),
        )

    def artifacts_for_root(self, root_session_id: str) -> List[Dict[str, Any]]:
        """All artifacts of a root session with their latest-version facts."""
        return self._rows(
            """SELECT a.*, v.version_number AS latest_version_number,
                      v.checksum AS latest_checksum,
                      v.size_bytes AS latest_size_bytes,
                      v.content_type AS latest_content_type,
                      v.producing_cell_id AS latest_producing_cell_id
                 FROM artifacts a
                 LEFT JOIN artifact_versions v ON v.id = a.latest_version_id
                WHERE a.root_session_id = ?
                ORDER BY a.created_at""",
            (root_session_id,),
        )

    def get_version(self, version_id: str) -> Optional[Dict[str, Any]]:
        return self._row(
            "SELECT * FROM artifact_versions WHERE id = ?", (version_id,)
        )

    def latest_version(self, artifact_id: str) -> Optional[Dict[str, Any]]:
        return self._row(
            "SELECT * FROM artifact_versions WHERE artifact_id = ? "
            "ORDER BY version_number DESC LIMIT 1",
            (artifact_id,),
        )

    def lineage(
        self,
        version_id: str,
        *,
        direction: str = "upstream",
        max_depth: int = LINEAGE_MAX_DEPTH,
    ) -> List[Dict[str, Any]]:
        """Transitive provenance of a version, nearest-first.

        ``direction="upstream"`` walks what this version was derived *from*;
        ``"downstream"`` walks what was derived from *it*. Each returned row
        is an artifact_versions dict plus a ``depth`` key (1 = direct edge).
        Cycle-safe via UNION dedup and the depth cap.
        """
        if direction == "upstream":
            from_col, to_col = "artifact_version_id", "depends_on_version_id"
        elif direction == "downstream":
            from_col, to_col = "depends_on_version_id", "artifact_version_id"
        else:
            raise ValueError(f"direction must be upstream|downstream, got {direction!r}")
        return self._rows(
            f"""WITH RECURSIVE lineage(version_id, depth) AS (
                    SELECT ?, 0
                    UNION
                    SELECT d.{to_col}, l.depth + 1
                      FROM artifact_dependencies d
                      JOIN lineage l ON d.{from_col} = l.version_id
                     WHERE l.depth < ?
                )
                SELECT v.*, l.depth AS depth
                  FROM lineage l
                  JOIN artifact_versions v ON v.id = l.version_id
                 WHERE l.depth > 0
                 ORDER BY l.depth, v.created_at""",
            (version_id, max_depth),
        )
