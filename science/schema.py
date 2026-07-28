"""DDL for the science-layer tables in state.db.

These tables implement the two-granularity execution trace and the
artifact/lineage store (frame_id → session_id):

- ``execution_log``          one row per code cell (kernel, env, io, status,
                             failure location, and whether the source is
                             replayable as a plain script)
- ``host_call_log``          one row per host.* call inside a cell
- ``content_snapshots``      large payloads stored once, content-addressed
- ``artifacts``              artifact identity, stable across versions
- ``artifact_versions``      per-version content metadata + provenance
- ``artifact_dependencies``  provenance edges: version → depends_on_version

Design notes:

- The schema is executed and column-reconciled by
  ``opencodon_state.SessionDB._init_schema()`` alongside the core schema, so the
  same declarative pattern applies: adding a column here is all that's needed
  for it to appear on existing databases at next startup.
- ``session_id`` / ``root_session_id`` columns are *soft* references (indexed
  TEXT, no FOREIGN KEY). state.db runs with ``PRAGMA foreign_keys=ON`` and
  sessions can be deleted by retention/hygiene paths; execution and lineage
  rows must survive that for reproducibility, so they keep the id without a
  hard constraint. Internal science relationships (call → cell,
  version → artifact, edge → version) are real FKs.
- Timestamps are REAL epoch seconds (``time.time()``), matching opencodon
  convention rather than the reference platform's integer columns.
- The reference platform's ``conda_env`` is generalized to ``env_name`` +
  ``env_snapshot`` — opencodon environments are not conda-specific.

Indexes that only reference first-version columns could live inline, but all
science indexes are kept in ``SCIENCE_INDEX_SQL`` (executed after column
reconciliation) so that a future index over a reconciler-added column never
breaks legacy databases — same ordering rule as ``DEFERRED_INDEX_SQL`` in
``opencodon_state``.
"""

SCIENCE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS execution_log (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    cell_index INTEGER NOT NULL,
    kernel_id TEXT NOT NULL,
    kernel_kind TEXT,
    language TEXT NOT NULL,
    env_name TEXT,
    env_snapshot TEXT,
    source TEXT NOT NULL,
    stdout TEXT,
    stderr TEXT,
    exit_status TEXT NOT NULL,
    error_lineno INTEGER,
    traceback TEXT,
    display_count INTEGER NOT NULL DEFAULT 0,
    has_magics INTEGER NOT NULL DEFAULT 0,
    origin TEXT NOT NULL DEFAULT 'agent',
    user_intervention TEXT,
    files_written TEXT,
    files_read TEXT,
    created_at REAL NOT NULL,
    UNIQUE (session_id, cell_index)
);

CREATE TABLE IF NOT EXISTS host_call_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_log_id TEXT NOT NULL REFERENCES execution_log(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    method TEXT NOT NULL,
    args_json TEXT NOT NULL DEFAULT '{}',
    derivable INTEGER NOT NULL DEFAULT 0,
    data_inline TEXT,
    data_ref TEXT REFERENCES content_snapshots(hash),
    error TEXT,
    bytes INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    UNIQUE (execution_log_id, seq)
);

CREATE TABLE IF NOT EXISTS content_snapshots (
    hash TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    root_session_id TEXT NOT NULL,
    session_id TEXT,
    filename TEXT NOT NULL,
    is_user_upload INTEGER NOT NULL DEFAULT 0,
    is_ephemeral INTEGER NOT NULL DEFAULT 0,
    latest_version_id TEXT,
    superseded_by_artifact_id TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS artifact_versions (
    id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
    version_number INTEGER NOT NULL,
    session_id TEXT,
    content_type TEXT NOT NULL DEFAULT 'application/octet-stream',
    size_bytes INTEGER NOT NULL,
    checksum TEXT NOT NULL,
    storage_path TEXT NOT NULL,
    language TEXT,
    is_intermediate INTEGER NOT NULL DEFAULT 0,
    producing_cell_id TEXT,
    parent_version_id TEXT,
    env_snapshot_hash TEXT,
    created_at REAL NOT NULL,
    UNIQUE (artifact_id, version_number)
);

CREATE TABLE IF NOT EXISTS artifact_dependencies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artifact_version_id TEXT NOT NULL REFERENCES artifact_versions(id) ON DELETE CASCADE,
    depends_on_version_id TEXT NOT NULL REFERENCES artifact_versions(id),
    reference_name TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    UNIQUE (artifact_version_id, depends_on_version_id, reference_name)
);
"""

SCIENCE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_execution_log_session
    ON execution_log(session_id, cell_index);
CREATE INDEX IF NOT EXISTS idx_host_call_log_cell
    ON host_call_log(execution_log_id, seq);
CREATE INDEX IF NOT EXISTS idx_artifacts_root
    ON artifacts(root_session_id);
CREATE INDEX IF NOT EXISTS idx_artifact_versions_artifact
    ON artifact_versions(artifact_id, version_number);
CREATE INDEX IF NOT EXISTS idx_artifact_versions_cell
    ON artifact_versions(producing_cell_id)
    WHERE producing_cell_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_artifact_deps_version
    ON artifact_dependencies(artifact_version_id);
CREATE INDEX IF NOT EXISTS idx_artifact_deps_depends_on
    ON artifact_dependencies(depends_on_version_id);
"""
