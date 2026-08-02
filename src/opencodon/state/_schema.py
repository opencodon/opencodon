"""SessionDB SchemaMaintenanceMixin — extracted from state/__init__ (restructure Phase 4).

Methods are verbatim moves; the class is assembled in opencodon.state.
"""
#!/usr/bin/env python3
"""
SQLite State Store for opencodon.

Provides persistent session storage with FTS5 full-text search, replacing
the per-session JSONL file approach. Stores session metadata, full message
history, and model configuration for CLI and gateway sessions.

Key design decisions:
- WAL mode for concurrent readers + one writer (gateway multi-platform)
- FTS5 virtual table for fast text search across all session messages
- Compression-triggered session splitting via parent_session_id chains
- Batch runner and RL trajectories are NOT stored here (separate systems)
- Session source tagging ('cli', 'telegram', 'discord', etc.) for filtering
"""

import asyncio
import json
import logging
import os
import random
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path

from opencodon.core.memory.memory_manager import sanitize_context
from opencodon.core.message_sanitization import _sanitize_surrogates
from opencodon_constants import get_opencodon_home
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar

from opencodon.state import (  # noqa: E402
    DEFERRED_INDEX_SQL,
    FTS_SQL,
    FTS_STORAGE_VERSION,
    FTS_TRIGRAM_SQL,
    LEGACY_FTS_SQL,
    LEGACY_FTS_TRIGRAM_SQL,
    SCHEMA_SQL,
    SCHEMA_VERSION,
    _FTS_TRIGGERS,
    _ROOT_BACKFILL_SQL,
    _ephemeral_child_sql,
    logger,
)


class SchemaMaintenanceMixin:
    @staticmethod
    def _parse_schema_columns(schema_sql: str) -> Dict[str, Dict[str, str]]:
        """Extract expected columns per table from SCHEMA_SQL.

        Uses an in-memory SQLite database to parse the SQL — SQLite itself
        handles all syntax (DEFAULT expressions with commas, inline
        REFERENCES, CHECK constraints, etc.) so there are zero regex
        edge cases.  The in-memory DB is opened, the schema DDL is
        executed, and PRAGMA table_info extracts the column metadata.

        Adding a column to SCHEMA_SQL is all that's needed; the
        reconciliation loop picks it up automatically.
        """
        ref = sqlite3.connect(":memory:")
        try:
            ref.executescript(schema_sql)
            table_columns: Dict[str, Dict[str, str]] = {}
            for (tbl,) in ref.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall():
                cols: Dict[str, str] = {}
                for row in ref.execute(
                    f'PRAGMA table_info("{tbl}")'
                ).fetchall():
                    # row: (cid, name, type, notnull, dflt_value, pk)
                    col_name = row[1]
                    col_type = row[2] or ""
                    notnull = row[3]
                    default = row[4]
                    pk = row[5]
                    # Reconstruct the type expression for ALTER TABLE ADD COLUMN
                    parts = [col_type] if col_type else []
                    if notnull and not pk:
                        parts.append("NOT NULL")
                    if default is not None:
                        parts.append(f"DEFAULT {default}")
                    cols[col_name] = " ".join(parts)
                table_columns[tbl] = cols
            return table_columns
        finally:
            ref.close()

    def _reconcile_columns(
        self, cursor: sqlite3.Cursor, schema_sql: str = None
    ) -> None:
        """Ensure live tables have every column declared in *schema_sql*.

        Follows the Beets/sqlite-utils pattern: the CREATE TABLE definition
        in SCHEMA_SQL (or the given *schema_sql* — e.g. the science-layer
        schema) is the single source of truth for the desired schema.
        On every startup this method diffs the live columns (via PRAGMA
        table_info) against the declared columns, and ADDs any that are
        missing.

        This makes column additions a declarative operation — just add
        the column to the schema constant and it appears on the next startup.
        Version-gated migration blocks are no longer needed for ADD COLUMN.
        """
        expected = self._parse_schema_columns(schema_sql or SCHEMA_SQL)
        for table_name, declared_cols in expected.items():
            # Get current columns from the live table
            try:
                rows = cursor.execute(
                    f'PRAGMA table_info("{table_name}")'
                ).fetchall()
            except sqlite3.OperationalError:
                continue  # Table doesn't exist yet (shouldn't happen after executescript)
            live_cols = set()
            for row in rows:
                # PRAGMA table_info returns (cid, name, type, notnull, dflt_value, pk)
                name = row[1] if isinstance(row, (tuple, list)) else row["name"]
                live_cols.add(name)

            for col_name, col_type in declared_cols.items():
                if col_name not in live_cols:
                    safe_name = col_name.replace('"', '""')
                    try:
                        cursor.execute(
                            f'ALTER TABLE "{table_name}" ADD COLUMN "{safe_name}" {col_type}'
                        )
                    except sqlite3.OperationalError as exc:
                        # Expected: "duplicate column name" from a race or
                        # re-run.  Unexpected: "Cannot add a NOT NULL column
                        # with default value NULL" from a schema mistake.
                        # Log at DEBUG so it's visible in agent.log.
                        logger.debug(
                            "reconcile %s.%s: %s", table_name, col_name, exc,
                        )

    def _init_schema(self):
        """Create tables and FTS if they don't exist, reconcile columns.

        Schema management follows the declarative reconciliation pattern
        (Beets, sqlite-utils): SCHEMA_SQL is the single source of truth.
        On existing databases, _reconcile_columns() diffs live columns
        against SCHEMA_SQL and ADDs any missing ones.  This eliminates
        the version-gated migration chain for column additions, making
        it impossible for reordered or inserted migrations to skip columns.

        The schema_version table is retained for future data migrations
        (transforming existing rows) which cannot be handled declaratively.
        """
        cursor = self._conn.cursor()

        cursor.executescript(SCHEMA_SQL)

        # ── Declarative column reconciliation ──────────────────────────
        # Diff live tables against SCHEMA_SQL and ADD any missing columns.
        # This is idempotent and self-healing: even if a version-gated
        # migration was skipped (e.g. due to version renumbering), the
        # column gets created here.
        self._reconcile_columns(cursor)

        # Indexes that reference reconciler-added columns must be created
        # AFTER _reconcile_columns runs — declaring them in SCHEMA_SQL
        # makes the initial executescript fail on legacy DBs (the index's
        # WHERE clause references a column that doesn't exist yet).
        try:
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_platform_msg_id "
                "ON messages(session_id, platform_message_id) "
                "WHERE platform_message_id IS NOT NULL"
            )
        except sqlite3.OperationalError as exc:
            logger.debug("idx_messages_platform_msg_id create skipped: %s", exc)

        # Deferred indexes that reference the reconciler-added ``active``
        # column (idx_messages_session_active) — same ordering constraint.
        cursor.executescript(DEFERRED_INDEX_SQL)

        # Heal NULL ``active`` rows unconditionally on every startup.
        # On real-world DBs the reconciler-added ``active`` column can lack
        # its NOT NULL DEFAULT 1 (older reconciler builds reconstructed the
        # type without the default — see #51646: PRAGMA shows
        # (17,'active','INTEGER',0,None,0) in the wild), so INSERTs that
        # omitted the column wrote NULL and the ``WHERE active = 1``
        # transcript loaders hid the whole history.  The INSERTs now set
        # active=1 explicitly; this idempotent repair un-hides rows written
        # before the fix.  It was previously gated at ``current_version <
        # 12`` which never re-ran for already-v12+ databases.
        try:
            cursor.execute(
                "UPDATE messages SET active = 1 WHERE active IS NULL"
            )
        except sqlite3.OperationalError:
            pass

        # ── Science layer (execution_log / host_call_log / artifacts /
        # content_snapshots) ───────────────────────────────────────────
        # The science tables live in state.db — not a sidecar DB — so a
        # tool-result append and its execution/provenance rows can commit
        # in one transaction. Their DDL lives in state/science_schema.py
        # (state owns all state.db DDL; the tables serve the science layer)
        # and goes through the same declarative reconciliation as SCHEMA_SQL.
        # Index creation is deferred until after reconciliation for the
        # same reason as DEFERRED_INDEX_SQL above.
        from opencodon.state.science_schema import (
            SCIENCE_INDEX_SQL,
            SCIENCE_SCHEMA_SQL,
        )

        cursor.executescript(SCIENCE_SCHEMA_SQL)
        self._reconcile_columns(cursor, schema_sql=SCIENCE_SCHEMA_SQL)
        cursor.executescript(SCIENCE_INDEX_SQL)

        # Heal NULL root_session_id rows on every startup (same idempotent
        # pattern as the ``active`` repair above). This is both the one-time
        # backfill after the reconciler ADDs the column on a legacy DB, and
        # ongoing repair for rows written by older builds sharing this DB.
        try:
            needs_root_backfill = cursor.execute(
                "SELECT 1 FROM sessions WHERE root_session_id IS NULL LIMIT 1"
            ).fetchone()
            if needs_root_backfill:
                cursor.execute(_ROOT_BACKFILL_SQL)
        except sqlite3.OperationalError as exc:
            logger.debug("root_session_id backfill skipped: %s", exc)

        fts5_available = self._sqlite_supports_fts5(cursor)
        fts_migrations_complete = True
        if not fts5_available:
            # Existing FTS triggers can still fire on messages INSERT/UPDATE
            # even though the current sqlite runtime cannot read the virtual
            # tables they target. Drop only the triggers so core persistence
            # continues; if a future runtime has FTS5, _ensure_fts_schema()
            # recreates them.
            self._drop_fts_triggers(cursor)

        # ── Schema version bookkeeping ─────────────────────────────────
        # Bump to current so future data migrations (if any) can gate on
        # version.  No version-gated column additions remain.
        cursor.execute("SELECT version FROM schema_version LIMIT 1")
        row = cursor.fetchone()
        if row is None:
            cursor.execute(
                "INSERT INTO schema_version (version) VALUES (?)",
                (SCHEMA_VERSION,),
            )
        else:
            current_version = row["version"] if isinstance(row, sqlite3.Row) else row[0]
            # Data migrations that can't be expressed declaratively (row
            # backfills, index changes tied to a specific version step) stay
            # in a version-gated chain. Column additions are handled by
            # _reconcile_columns() above and no longer need entries here.
            if current_version < 10 and SCHEMA_VERSION == 10:
                # v10: trigram FTS5 table for CJK/substring search. The
                # virtual table + triggers are created unconditionally via
                # FTS_TRIGRAM_SQL below, but existing rows need a one-time
                # backfill into the FTS index.
                #
                # Only run this when v10 itself is the target schema. Current
                # v11+ code drops and rebuilds both FTS tables below, so doing
                # the v10-only trigram backfill first only burns startup time
                # and WAL space before v11 throws the work away.
                if fts5_available:
                    _fts_trigram_exists = self._fts_table_probe(
                        cursor, "messages_fts_trigram"
                    )
                    if _fts_trigram_exists is False:
                        if self._ensure_fts_schema(
                            cursor, "messages_fts_trigram", FTS_TRIGRAM_SQL
                        ):
                            cursor.execute(
                                "INSERT INTO messages_fts_trigram(rowid, content) "
                                "SELECT id, content FROM messages WHERE content IS NOT NULL"
                            )
                        else:
                            fts_migrations_complete = False
                    elif _fts_trigram_exists is None:
                        fts_migrations_complete = False
                else:
                    fts_migrations_complete = False
            if current_version < 11 and SCHEMA_VERSION < 23:
                # v11 (SUPERSEDED by v23): re-index FTS5 tables to cover
                # tool_name + tool_calls in inline mode (#16751). v23 drops
                # and rebuilds both FTS tables in external-content form, so
                # running the v11 inline backfill first would only burn
                # startup time and WAL space before v23 throws the work
                # away — and its inline INSERT shape no longer matches the
                # current external-content FTS_SQL anyway. Kept only for
                # source archaeology; unreachable while SCHEMA_VERSION >= 23.
                pass
            if current_version < 16:
                # v16: tag delegate subagent rows so pickers stay clean after
                # parent deletes that used to orphan them (parent_session_id → NULL).
                try:
                    cursor.execute(
                        "UPDATE sessions SET model_config = json_set("
                        "COALESCE(model_config, '{}'), '$._delegate_from', parent_session_id) "
                        f"WHERE parent_session_id IS NOT NULL "
                        "AND json_extract(COALESCE(model_config, '{}'), '$._delegate_from') IS NULL "
                        f"AND {_ephemeral_child_sql('sessions')}"
                    )
                    cursor.execute(
                        "UPDATE sessions SET model_config = json_set("
                        "COALESCE(model_config, '{}'), '$._delegate_from', '__orphaned__') "
                        "WHERE parent_session_id IS NULL "
                        "AND json_extract(COALESCE(model_config, '{}'), '$._delegate_from') IS NULL "
                        "AND json_extract(COALESCE(model_config, '{}'), '$._branched_from') IS NULL "
                        "AND title IS NULL "
                        "AND message_count <= 25 "
                        "AND EXISTS (SELECT 1 FROM messages m "
                        "            WHERE m.session_id = sessions.id AND m.role = 'tool') "
                        "AND NOT EXISTS (SELECT 1 FROM sessions ch "
                        "                WHERE ch.parent_session_id = sessions.id)"
                    )
                except sqlite3.OperationalError:
                    pass
            if current_version < 18:
                # v18: gateway metadata consolidation (#9006). Backfill
                # display_name / origin_json / expiry_finalized from
                # sessions.json so pre-migration gateway sessions are
                # discoverable from state.db without the JSON index.
                try:
                    self._backfill_gateway_metadata_from_sessions_json(cursor)
                except Exception as exc:
                    # Backfill is best-effort: sessions.json may be absent,
                    # corrupted, or partially stale. Missing metadata simply
                    # means consumers fall back to sessions.json for those
                    # rows until the gateway rewrites them.
                    logger.debug("v18 gateway metadata backfill skipped: %s", exc)
            if current_version < 20:
                # v20: per-model usage attribution (issue #51607). Going
                # forward update_token_counts() records each API call into
                # session_model_usage keyed by the live model, but existing
                # sessions only have their aggregate totals on the sessions
                # row. Seed one usage row per historical session from those
                # aggregates so insights reads uniformly from the new table.
                # INSERT OR IGNORE keeps it idempotent: if newer code already
                # wrote a (session_id, model, provider) row for a session, the
                # PK conflict skips the stale aggregate rather than doubling it.
                try:
                    cursor.execute(
                        """INSERT OR IGNORE INTO session_model_usage (
                               session_id, model, billing_provider,
                               billing_base_url, billing_mode,
                               api_call_count, input_tokens,
                               output_tokens, cache_read_tokens,
                               cache_write_tokens, reasoning_tokens,
                               estimated_cost_usd, actual_cost_usd,
                               cost_status, cost_source, first_seen, last_seen
                           )
                           SELECT id, COALESCE(model, 'unknown'),
                                  COALESCE(billing_provider, ''),
                                  COALESCE(billing_base_url, ''),
                                  COALESCE(billing_mode, ''),
                                  COALESCE(api_call_count, 0),
                                  COALESCE(input_tokens, 0),
                                  COALESCE(output_tokens, 0),
                                  COALESCE(cache_read_tokens, 0),
                                  COALESCE(cache_write_tokens, 0),
                                  COALESCE(reasoning_tokens, 0),
                                  COALESCE(estimated_cost_usd, 0),
                                  COALESCE(actual_cost_usd, 0),
                                  cost_status, cost_source,
                                  started_at, COALESCE(ended_at, started_at)
                           FROM sessions
                           WHERE COALESCE(input_tokens, 0)
                                 + COALESCE(output_tokens, 0)
                                 + COALESCE(cache_read_tokens, 0)
                                 + COALESCE(cache_write_tokens, 0)
                                 + COALESCE(reasoning_tokens, 0) > 0"""
                    )
                except sqlite3.OperationalError:
                    pass
            if current_version < 22:
                # v22: task-dimension usage attribution (issue #23270).
                # session_model_usage gains a ``task`` column ('' = main agent
                # loop; 'vision'/'compression'/'title_generation'/... =
                # auxiliary calls) so aux model spend is visible in analytics.
                # The column participates in the PRIMARY KEY and SQLite cannot
                # ALTER a PK, so rebuild the table. The reconciler will have
                # already ADDed the plain column on legacy DBs (harmless);
                # the rebuild bakes it into the PK properly. Existing rows are
                # main-loop accounting by definition → task=''.
                try:
                    legacy_pk = cursor.execute(
                        "SELECT COUNT(*) FROM pragma_table_info('session_model_usage') "
                        "WHERE name = 'task' AND pk > 0"
                    ).fetchone()[0]
                    if not legacy_pk:
                        cursor.execute("ALTER TABLE session_model_usage RENAME TO session_model_usage_v21")
                        cursor.execute(
                            """CREATE TABLE session_model_usage (
                                   session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                                   model TEXT NOT NULL,
                                   billing_provider TEXT NOT NULL DEFAULT '',
                                   billing_base_url TEXT NOT NULL DEFAULT '',
                                   billing_mode TEXT NOT NULL DEFAULT '',
                                   task TEXT NOT NULL DEFAULT '',
                                   api_call_count INTEGER NOT NULL DEFAULT 0,
                                   input_tokens INTEGER NOT NULL DEFAULT 0,
                                   output_tokens INTEGER NOT NULL DEFAULT 0,
                                   cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                                   cache_write_tokens INTEGER NOT NULL DEFAULT 0,
                                   reasoning_tokens INTEGER NOT NULL DEFAULT 0,
                                   estimated_cost_usd REAL NOT NULL DEFAULT 0,
                                   actual_cost_usd REAL NOT NULL DEFAULT 0,
                                   cost_status TEXT,
                                   cost_source TEXT,
                                   first_seen REAL,
                                   last_seen REAL,
                                   PRIMARY KEY (session_id, model, billing_provider, billing_base_url, billing_mode, task)
                               )"""
                        )
                        cursor.execute(
                            """INSERT INTO session_model_usage (
                                   session_id, model, billing_provider, billing_base_url,
                                   billing_mode, task, api_call_count, input_tokens,
                                   output_tokens, cache_read_tokens, cache_write_tokens,
                                   reasoning_tokens, estimated_cost_usd, actual_cost_usd,
                                   cost_status, cost_source, first_seen, last_seen
                               )
                               SELECT session_id, model, billing_provider, billing_base_url,
                                      billing_mode, '', api_call_count, input_tokens,
                                      output_tokens, cache_read_tokens, cache_write_tokens,
                                      reasoning_tokens, estimated_cost_usd, actual_cost_usd,
                                      cost_status, cost_source, first_seen, last_seen
                               FROM session_model_usage_v21"""
                        )
                        cursor.execute("DROP TABLE session_model_usage_v21")
                        cursor.execute(
                            "CREATE INDEX IF NOT EXISTS idx_session_model_usage_session "
                            "ON session_model_usage(session_id)"
                        )
                        cursor.execute(
                            "CREATE INDEX IF NOT EXISTS idx_session_model_usage_model "
                            "ON session_model_usage(model)"
                        )
                except sqlite3.OperationalError as exc:
                    logger.debug("v22 session_model_usage rebuild skipped: %s", exc)
            if current_version < 23:
                # v23: FTS storage redesign (issues #22478, #43690, #55233).
                # The v11 inline-mode FTS tables each store a full private
                # copy of every message (content || tool_name || tool_calls),
                # and the trigram index additionally covers role='tool' rows
                # (~90% of message bytes: base64 payloads, file dumps) at
                # ~2.6x amplification — together ~75% of state.db on heavy
                # installs (observed: 18.9 GB of a 25 GB DB).
                #
                # OPT-IN, NOT AUTOMATIC. The transition (demote old vtables →
                # new external-content schema → backfill → teardown → VACUUM)
                # is disk-heavy (transient ~2x file size to fully reclaim via
                # VACUUM) and long (~1-2h background on a 25 GB DB). Doing it
                # silently on every big user's next open — with a completeness
                # guarantee that depends on the process staying alive long
                # enough — is the wrong default. So on an EXISTING install we
                # touch nothing here: the v22 inline FTS keeps working exactly
                # as before, and we only record a flag advertising that the
                # optimization is available. `opencodon sessions optimize-storage`
                # performs the whole transition as one deliberate, disk-checked,
                # progress-reported foreground operation.
                #
                # DECOUPLED VERSIONING. Crucially, this does NOT hold back the
                # main schema_version. The FTS storage LAYOUT is tracked by an
                # independent `fts_storage_version` marker (see
                # _fts_storage_version / SETTLE below), so schema_version
                # advances to SCHEMA_VERSION here like every other migration —
                # future v24+ migrations land automatically for legacy-FTS
                # users too. Only the FTS *layout* waits for opt-in.
                if fts5_available and self._db_has_legacy_inline_fts(cursor):
                    self.set_meta("fts_optimize_available", "1", cursor=cursor)

            # The FTS storage layout is versioned independently of the main
            # schema (see the v23 note above). Stamp the current layout so the
            # main version can always advance: a fresh/optimized DB is at
            # FTS_STORAGE_VERSION; a legacy DB is left at whatever it had
            # (absent/0) until `optimize-storage` runs. An INTERRUPTED
            # optimize (legacy vtables already demoted, but rebuild markers
            # or demoted trash tables still present) is NOT stamped either —
            # the marker is the source of truth for "fully optimized", and
            # `fts_optimize_available()` keeps offering the resume until the
            # transition actually completes.
            if (
                fts5_available
                and not self._db_has_legacy_inline_fts(cursor)
                and cursor.execute(
                    "SELECT 1 FROM state_meta "
                    "WHERE key = 'fts_rebuild_high_water' LIMIT 1"
                ).fetchone() is None
                and not self._has_fts_trash(cursor)
            ):
                self.set_meta(
                    "fts_storage_version", str(FTS_STORAGE_VERSION), cursor=cursor
                )

            # Advance schema_version to current for ALL non-FTS-layout
            # migrations. This is deliberately NOT gated on the FTS opt-in —
            # holding the whole version back would block every future schema
            # migration for a user who never optimizes. FTS5 being unavailable
            # is the one case we skip (we can't have created the current FTS
            # objects, so claiming the current schema would be a lie).
            if (
                current_version < SCHEMA_VERSION
                and fts_migrations_complete
                and fts5_available
            ):
                cursor.execute(
                    "UPDATE schema_version SET version = ?",
                    (SCHEMA_VERSION,),
                )

        # Unique title index — always ensure it exists. Older databases may
        # contain duplicate aliases from before the constraint was enforced;
        # preserve every session while letting the newest one retain the alias.
        title_index_sql = (
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_title_unique "
            "ON sessions(title) WHERE title IS NOT NULL"
        )
        try:
            cursor.execute(title_index_sql)
        except sqlite3.IntegrityError:
            # The index is an optimization — its creation must never abort
            # opening the database, so the repair itself is also guarded.
            try:
                cursor.execute(
                    """UPDATE sessions AS older
                       SET title = NULL
                       WHERE title IS NOT NULL
                         AND EXISTS (
                             SELECT 1 FROM sessions AS newer
                             WHERE newer.title = older.title
                               AND newer.rowid > older.rowid
                         )"""
                )
                logger.warning(
                    "Cleared %d duplicate session title(s) while restoring the unique index",
                    cursor.rowcount,
                )
                cursor.execute(title_index_sql)
            except sqlite3.Error:
                logger.exception(
                    "Could not repair duplicate session titles; "
                    "unique title index not created"
                )
        except sqlite3.OperationalError:
            pass  # Index already exists

        if fts5_available:
            # FTS5 setup. Run the DDL even when the virtual table exists so
            # CREATE TRIGGER IF NOT EXISTS repairs trigger-only degradation from
            # an earlier no-FTS5 runtime.
            #
            # OPT-IN v23 boundary: a legacy v22 install (inline-content FTS,
            # not yet opted into `opencodon db optimize`) must keep its EXISTING
            # inline schema + triggers. Running the v23 external-content DDL
            # here would create the trigram source VIEW and leave the DB in a
            # mixed inline/external state. So for a legacy DB we only ensure
            # its inline triggers exist (via the legacy DDL), and skip the
            # v23 view/external tables entirely. Fresh installs and opted-in
            # DBs have no legacy inline FTS, so they get the v23 DDL.
            if self._db_has_legacy_inline_fts(cursor):
                triggers_need_repair = (
                    self._fts_trigger_count(cursor) < len(_FTS_TRIGGERS)
                )
                self._fts_enabled = self._ensure_fts_schema(
                    cursor, "messages_fts", LEGACY_FTS_SQL
                )
                if self._fts_enabled:
                    trigram_enabled = self._ensure_fts_schema(
                        cursor, "messages_fts_trigram", LEGACY_FTS_TRIGRAM_SQL
                    )
                    self._trigram_available = trigram_enabled
                    if triggers_need_repair:
                        self._rebuild_legacy_fts_indexes(
                            cursor, include_trigram=trigram_enabled
                        )
            else:
                triggers_need_repair = (
                    self._fts_trigger_count(cursor) < len(_FTS_TRIGGERS)
                )
                self._fts_enabled = self._ensure_fts_schema(
                    cursor, "messages_fts", FTS_SQL
                )

                # Trigram FTS5 for CJK/substring search. This is optional
                # relative to the main FTS table; if it cannot be created,
                # CJK search falls back to LIKE.
                if self._fts_enabled:
                    trigram_enabled = self._ensure_fts_schema(
                        cursor, "messages_fts_trigram", FTS_TRIGRAM_SQL
                    )
                    self._trigram_available = trigram_enabled
                    if triggers_need_repair:
                        self._rebuild_fts_indexes(
                            cursor,
                            include_trigram=trigram_enabled,
                        )
                    # CJK-bigram index (cjk_unicode61). Strictly additive to
                    # the surfaces above and gated on the loadable tokenizer:
                    self._ensure_fts_cjk_schema(cursor)

        self._conn.commit()

    def vacuum(self) -> int:
        """Run VACUUM to reclaim disk space after large deletes.

        SQLite does not shrink the database file when rows are deleted —
        freed pages just get reused on the next insert. After a prune that
        removed hundreds of sessions, the file stays bloated unless we
        explicitly VACUUM.

        VACUUM rewrites the entire DB, so it's expensive (seconds per
        100MB) and cannot run inside a transaction. It also acquires an
        exclusive lock, so callers must ensure no other writers are
        active. Safe to call at startup before the gateway/CLI starts
        serving traffic.

        FTS5 segments are merged first via :meth:`optimize_fts` so the
        subsequent VACUUM reclaims the pages freed by the merge. This is a
        layout-only optimization — search results are unchanged.

        Returns the number of FTS indexes that were optimized (0 if the
        merge step failed or no FTS tables exist).
        """
        # Merge FTS5 segments before VACUUM so the freed pages are returned
        # to the OS in the same pass. optimize_fts() manages its own lock.
        optimized = 0
        try:
            optimized = self.optimize_fts()
        except Exception as exc:
            logger.warning("FTS optimize before VACUUM failed: %s", exc)
        # VACUUM cannot be executed inside a transaction.
        with self._lock:
            # Best-effort WAL checkpoint first, then VACUUM.
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception as exc:
                logger.debug("WAL checkpoint (TRUNCATE) before VACUUM failed: %s", exc)
            self._conn.execute("VACUUM")
        return optimized

    def maybe_auto_prune_and_vacuum(
        self,
        retention_days: int = 90,
        min_interval_hours: int = 24,
        vacuum: bool = True,
        sessions_dir: Optional[Path] = None,
    ) -> Dict[str, Any]:
        """Idempotent auto-maintenance: prune old sessions + optional VACUUM.

        Records the last run timestamp in state_meta so subsequent calls
        within ``min_interval_hours`` no-op. Designed to be called once at
        startup from long-lived entrypoints (CLI, gateway, cron scheduler).

        When *sessions_dir* is provided, on-disk transcript files
        (``.json`` / ``.jsonl`` / ``request_dump_*``) for pruned sessions
        are removed as part of the same sweep (issue #3015).

        Never raises. On any failure, logs a warning and returns a dict
        with ``"error"`` set.

        Returns a dict with keys:
          - ``"skipped"`` (bool) — true if within min_interval_hours of last run
          - ``"pruned"`` (int)   — number of sessions deleted
          - ``"vacuumed"`` (bool) — true if VACUUM ran
          - ``"error"`` (str, optional) — present only on failure
        """
        result: Dict[str, Any] = {"skipped": False, "pruned": 0, "vacuumed": False}
        try:
            # Skip if another process/call did maintenance recently.
            last_raw = self.get_meta("last_auto_prune")
            now = time.time()
            if last_raw:
                try:
                    last_ts = float(last_raw)
                    if now - last_ts < min_interval_hours * 3600:
                        result["skipped"] = True
                        return result
                except (TypeError, ValueError):
                    pass  # corrupt meta; treat as no prior run

            pruned = self.prune_sessions(
                older_than_days=retention_days,
                sessions_dir=sessions_dir,
            )
            result["pruned"] = pruned

            # Only VACUUM if we actually freed rows — VACUUM on a tight DB
            # is wasted I/O. Threshold keeps small DBs from paying the cost.
            if vacuum and pruned > 0:
                try:
                    self.vacuum()
                    result["vacuumed"] = True
                except Exception as exc:
                    logger.warning("state.db VACUUM failed: %s", exc)

            # Record the attempt even if pruned == 0, so we don't retry
            # every startup within the min_interval_hours window.
            self.set_meta("last_auto_prune", str(now))

            if pruned > 0:
                logger.info(
                    "state.db auto-maintenance: pruned %d session(s) older than %d days%s",
                    pruned,
                    retention_days,
                    " + VACUUM" if result["vacuumed"] else "",
                )
        except Exception as exc:
            # Maintenance must never block startup. Log and return error marker.
            logger.warning("state.db auto-maintenance failed: %s", exc)
            result["error"] = str(exc)

        return result

