"""SessionDB GatewayRoutingMixin — extracted from state/__init__ (restructure Phase 4).

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



class GatewayRoutingMixin:
    def record_gateway_session_peer(
        self,
        session_id: str,
        *,
        source: str,
        user_id: str = None,
        session_key: str = None,
        chat_id: str = None,
        chat_type: str = None,
        thread_id: str = None,
        display_name: str = None,
        origin_json: str = None,
    ) -> None:
        """Persist the gateway routing peer for an existing session row.

        ``display_name`` / ``origin_json`` carry the gateway's presentation
        and full origin metadata (#9006) so consumers (mcp_serve, mirror,
        channel directory) can read routing data from state.db instead of
        sessions.json.  They are COALESCE'd only in the sense that ``None``
        leaves the existing value untouched.
        """
        if not session_id or not session_key:
            return

        def _do(conn):
            conn.execute(
                """UPDATE sessions
                   SET session_key = ?, source = ?, user_id = ?, chat_id = ?,
                       chat_type = ?, thread_id = ?,
                       display_name = COALESCE(?, display_name),
                       origin_json = COALESCE(?, origin_json)
                   WHERE id = ?""",
                (
                    session_key,
                    source,
                    user_id,
                    chat_id,
                    chat_type,
                    thread_id,
                    display_name,
                    origin_json,
                    session_id,
                ),
            )

        self._execute_write(_do)

    def set_expiry_finalized(self, session_id: str, finalized: bool = True) -> None:
        """Mark a gateway session's expiry-finalization flag in state.db.

        Mirrors ``SessionEntry.expiry_finalized`` (sessions.json) so the flag
        survives even if the JSON index is pruned or lost (#9006).
        """
        if not session_id:
            return

        def _do(conn):
            conn.execute(
                "UPDATE sessions SET expiry_finalized = ? WHERE id = ?",
                (1 if finalized else 0, session_id),
            )

        self._execute_write(_do)

    def save_gateway_routing_entry(
        self, session_key: str, entry_json: str, *, scope: str = ""
    ) -> None:
        """Upsert one gateway routing entry (session_key -> SessionEntry JSON).

        The gateway_routing table is the durable replacement for
        sessions.json: one row per routing key, holding the full serialized
        ``SessionEntry`` so the gateway can rehydrate exactly what it wrote.

        ``scope`` namespaces the index the way separate sessions.json files
        did (one per sessions_dir) — callers pass their sessions_dir path so
        two stores with different directories never share routing state.
        """
        if not session_key or not entry_json:
            return

        def _do(conn):
            conn.execute(
                """INSERT INTO gateway_routing (scope, session_key, entry_json, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(scope, session_key) DO UPDATE SET
                       entry_json = excluded.entry_json,
                       updated_at = excluded.updated_at""",
                (scope, session_key, entry_json, time.time()),
            )

        self._execute_write(_do)

    def replace_gateway_routing_entries(
        self, entries: Dict[str, str], *, scope: str = ""
    ) -> None:
        """Atomically replace the routing index for *scope* with *entries*.

        Mirrors the sessions.json full-rewrite semantics: keys absent from
        *entries* are removed (pruned/reset sessions disappear from the
        index).  Runs as a single write transaction.  Other scopes are
        untouched.
        """
        now = time.time()

        def _do(conn):
            conn.execute("DELETE FROM gateway_routing WHERE scope = ?", (scope,))
            if entries:
                conn.executemany(
                    "INSERT INTO gateway_routing (scope, session_key, entry_json, updated_at) "
                    "VALUES (?, ?, ?, ?)",
                    [(scope, k, v, now) for k, v in entries.items() if k and v],
                )

        self._execute_write(_do)

    def load_gateway_routing_entries(self, *, scope: str = "") -> Dict[str, str]:
        """Load routing entries for *scope* as {session_key: entry_json}."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT session_key, entry_json FROM gateway_routing WHERE scope = ?",
                (scope,),
            ).fetchall()
        return {r["session_key"]: r["entry_json"] for r in rows}

    def delete_gateway_routing_entries(
        self, session_keys: List[str], *, scope: str = ""
    ) -> None:
        """Remove routing entries for the given session keys in *scope*."""
        if not session_keys:
            return

        def _do(conn):
            conn.executemany(
                "DELETE FROM gateway_routing WHERE scope = ? AND session_key = ?",
                [(scope, k) for k in session_keys],
            )

        self._execute_write(_do)

    def list_gateway_sessions(
        self,
        *,
        platform: Optional[str] = None,
        active_only: bool = True,
    ) -> List[Dict[str, Any]]:
        """List gateway sessions (rows with a session_key) from state.db.

        Returns the newest row per session_key — the same shape consumers got
        from sessions.json: one live mapping per routing key.  ``platform``
        filters on ``source``; ``active_only`` restricts to sessions that
        have not ended.
        """
        query = """
            SELECT sessions.*,
                   COALESCE(
                       (SELECT MAX(m.timestamp) FROM messages m
                        WHERE m.session_id = sessions.id),
                       sessions.started_at
                   ) AS last_active
            FROM sessions
            WHERE session_key IS NOT NULL
              AND started_at = (
                  SELECT MAX(s2.started_at) FROM sessions s2
                  WHERE s2.session_key = sessions.session_key
              )
        """
        params: list = []
        if platform:
            query += " AND LOWER(source) = LOWER(?)"
            params.append(platform)
        if active_only:
            query += " AND ended_at IS NULL"
        query += " ORDER BY last_active DESC"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def find_session_by_origin(
        self,
        *,
        platform: str,
        chat_id: str,
        thread_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> Optional[str]:
        """Find the most recent live session_id for a platform + chat origin.

        Equivalent of gateway/mirror's sessions.json scan: matches on
        source + chat_id (+ thread_id when provided).  When ``user_id`` is
        provided, exact sender matches are preferred; if multiple distinct
        users share the chat and none matches, returns None rather than
        contaminating another participant's session.
        """
        if not platform or chat_id in (None, ""):
            return None
        query = """
            SELECT id, user_id, started_at FROM sessions
            WHERE LOWER(source) = LOWER(?)
              AND session_key IS NOT NULL
              AND chat_id = ?
              AND ended_at IS NULL
        """
        params: list = [platform, str(chat_id)]
        if thread_id is not None:
            query += " AND COALESCE(thread_id, '') = ?"
            params.append(str(thread_id))
        query += " ORDER BY started_at DESC"
        with self._lock:
            rows = [dict(r) for r in self._conn.execute(query, params).fetchall()]
        if not rows:
            return None
        if user_id:
            exact = [r for r in rows if str(r.get("user_id") or "") == str(user_id)]
            if exact:
                return str(exact[0]["id"])
            if len(rows) > 1:
                return None
        elif len(rows) > 1:
            distinct_users = {
                str(r.get("user_id") or "").strip()
                for r in rows
                if str(r.get("user_id") or "").strip()
            }
            if len(distinct_users) > 1:
                return None
        return str(rows[0]["id"])

    def _backfill_gateway_metadata_from_sessions_json(
        self, cursor: sqlite3.Cursor
    ) -> None:
        """One-time v18 backfill of gateway metadata from sessions.json.

        Existing gateway sessions predate the display_name / origin_json /
        expiry_finalized columns; copy what sessions.json knows so consumers
        can switch to state.db without losing pre-migration sessions.
        Only fills NULL columns — never overwrites data written by newer code.
        """
        sessions_file = get_opencodon_home() / "sessions" / "sessions.json"
        if not sessions_file.exists():
            return
        with open(sessions_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return
        for key, entry in data.items():
            if str(key).startswith("_") or not isinstance(entry, dict):
                continue
            session_id = entry.get("session_id")
            if not session_id:
                continue
            origin = entry.get("origin")
            cursor.execute(
                """UPDATE sessions
                   SET session_key = COALESCE(session_key, ?),
                       chat_id = COALESCE(chat_id, ?),
                       chat_type = COALESCE(chat_type, ?),
                       thread_id = COALESCE(thread_id, ?),
                       display_name = COALESCE(display_name, ?),
                       origin_json = COALESCE(origin_json, ?),
                       expiry_finalized = CASE
                           WHEN COALESCE(expiry_finalized, 0) = 0 AND ? = 1 THEN 1
                           ELSE expiry_finalized
                       END
                   WHERE id = ?""",
                (
                    entry.get("session_key") or key,
                    (origin or {}).get("chat_id") if isinstance(origin, dict) else None,
                    entry.get("chat_type"),
                    (origin or {}).get("thread_id") if isinstance(origin, dict) else None,
                    entry.get("display_name"),
                    json.dumps(origin) if isinstance(origin, dict) else None,
                    1 if entry.get("expiry_finalized") or entry.get("memory_flushed") else 0,
                    str(session_id),
                ),
            )

    def find_latest_gateway_session_for_peer(
        self,
        *,
        source: str,
        user_id: Optional[str] = None,
        session_key: Optional[str] = None,
        chat_id: Optional[str] = None,
        chat_type: Optional[str] = None,
        thread_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Find the latest recoverable gateway session for a routing peer.

        ``sessions.json`` is the fast routing index, but it can be missing or
        pruned after process-level restart bugs.  New gateway sessions persist
        the deterministic ``session_key`` on the durable session row so the
        mapping can be rebuilt exactly.  Rows ended only by older gateway
        cleanup's ``agent_close`` bug or a mistaken TUI ``ws_orphan_reap``
        (dashboard viewer disconnect before #60609) are treated as recoverable;
        explicit conversation boundaries such as /new, /resume switches, and
        compression splits are not.
        """
        if not session_key:
            return None
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM sessions
                WHERE session_key = ?
                  AND source = ?
                  AND (ended_at IS NULL OR end_reason IN ('agent_close', 'ws_orphan_reap'))
                  AND (COALESCE(message_count, 0) > 0 OR EXISTS (
                      SELECT 1 FROM messages WHERE messages.session_id = sessions.id LIMIT 1
                  ))
                ORDER BY started_at DESC
                LIMIT 1
                """,
                (session_key, source),
            ).fetchone()
            if row is not None:
                return dict(row)

            # Conservative fallback for rows created by current code but with a
            # temporarily-missing exact key: still require the complete peer
            # tuple so we never cross chats/threads/users.
            if chat_id is None or chat_type is None:
                return None
            row = self._conn.execute(
                """
                SELECT * FROM sessions
                WHERE source = ?
                  AND COALESCE(user_id, '') = COALESCE(?, '')
                  AND COALESCE(chat_id, '') = COALESCE(?, '')
                  AND COALESCE(chat_type, '') = COALESCE(?, '')
                  AND COALESCE(thread_id, '') = COALESCE(?, '')
                  AND (ended_at IS NULL OR end_reason IN ('agent_close', 'ws_orphan_reap'))
                  AND (COALESCE(message_count, 0) > 0 OR EXISTS (
                      SELECT 1 FROM messages WHERE messages.session_id = sessions.id LIMIT 1
                  ))
                ORDER BY started_at DESC
                LIMIT 1
                """,
                (source, user_id, chat_id, chat_type, thread_id),
            ).fetchone()
        return dict(row) if row else None

    def has_platform_message_id(
        self, session_id: str, platform_message_id: str
    ) -> bool:
        """Check if a message with the given platform_message_id exists.

        Uses the idx_messages_platform_msg_id partial index for efficient
        lookup. Used by the gateway's transient-failure dedupe guard (#47237)
        to skip re-persisting a user message that was already saved on a
        prior retry of the same inbound platform message.
        """
        with self._lock:
            cursor = self._conn.execute(
                "SELECT 1 FROM messages "
                "WHERE session_id = ? AND platform_message_id = ? LIMIT 1",
                (session_id, platform_message_id),
            )
            return cursor.fetchone() is not None

    def apply_telegram_topic_migration(self) -> None:
        """Create Telegram DM topic-mode tables on explicit /topic opt-in.

        This migration is deliberately not part of automatic SessionDB startup
        reconciliation. Operators must be able to upgrade opencodon, keep the old
        Telegram bot behavior running, and only mutate topic-mode state when the
        user executes /topic to opt into the feature.

        Schema versions:
          v1 — initial shape (no ON DELETE CASCADE on session_id FK)
          v2 — session_id FK gets ON DELETE CASCADE so session pruning
               automatically clears bindings.
        """
        def _do(conn):
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS telegram_dm_topic_mode (
                    chat_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    activated_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    has_topics_enabled INTEGER,
                    allows_users_to_create_topics INTEGER,
                    capability_checked_at REAL,
                    intro_message_id TEXT,
                    pinned_message_id TEXT
                );

                CREATE TABLE IF NOT EXISTS telegram_dm_topic_bindings (
                    chat_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    session_key TEXT NOT NULL,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    managed_mode TEXT NOT NULL DEFAULT 'auto',
                    linked_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (chat_id, thread_id)
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_telegram_dm_topic_bindings_session
                ON telegram_dm_topic_bindings(session_id);

                CREATE INDEX IF NOT EXISTS idx_telegram_dm_topic_bindings_user
                ON telegram_dm_topic_bindings(user_id, chat_id);
                """
            )

            # v1 → v2: rebuild telegram_dm_topic_bindings if its session_id FK
            # lacks ON DELETE CASCADE. SQLite can't ALTER a foreign key, so we
            # rebuild the table. Only runs once per DB (version gate).
            current = conn.execute(
                "SELECT value FROM state_meta WHERE key = ?",
                ("telegram_dm_topic_schema_version",),
            ).fetchone()
            current_version = int(current[0]) if current and str(current[0]).isdigit() else 0
            if current_version < 2:
                fk_rows = conn.execute(
                    "PRAGMA foreign_key_list('telegram_dm_topic_bindings')"
                ).fetchall()
                needs_rebuild = any(
                    row[2] == "sessions" and (row[6] or "") != "CASCADE"
                    for row in fk_rows
                )
                if needs_rebuild:
                    conn.executescript(
                        """
                        CREATE TABLE telegram_dm_topic_bindings_new (
                            chat_id TEXT NOT NULL,
                            thread_id TEXT NOT NULL,
                            user_id TEXT NOT NULL,
                            session_key TEXT NOT NULL,
                            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                            managed_mode TEXT NOT NULL DEFAULT 'auto',
                            linked_at REAL NOT NULL,
                            updated_at REAL NOT NULL,
                            PRIMARY KEY (chat_id, thread_id)
                        );
                        INSERT INTO telegram_dm_topic_bindings_new
                            SELECT chat_id, thread_id, user_id, session_key,
                                   session_id, managed_mode, linked_at, updated_at
                            FROM telegram_dm_topic_bindings;
                        DROP TABLE telegram_dm_topic_bindings;
                        ALTER TABLE telegram_dm_topic_bindings_new
                            RENAME TO telegram_dm_topic_bindings;
                        CREATE UNIQUE INDEX idx_telegram_dm_topic_bindings_session
                            ON telegram_dm_topic_bindings(session_id);
                        CREATE INDEX idx_telegram_dm_topic_bindings_user
                            ON telegram_dm_topic_bindings(user_id, chat_id);
                        """
                    )

            conn.execute(
                "INSERT INTO state_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                ("telegram_dm_topic_schema_version", "2"),
            )
        self._execute_write(_do)

    def enable_telegram_topic_mode(
        self,
        *,
        chat_id: str,
        user_id: str,
        has_topics_enabled: Optional[bool] = None,
        allows_users_to_create_topics: Optional[bool] = None,
    ) -> None:
        """Enable Telegram DM topic mode for one private chat/user.

        This method intentionally owns the explicit topic migration. Ordinary
        SessionDB startup must not create these side tables.
        """
        self.apply_telegram_topic_migration()
        now = time.time()

        def _to_int(value: Optional[bool]) -> Optional[int]:
            if value is None:
                return None
            return 1 if value else 0

        def _do(conn):
            conn.execute(
                """
                INSERT INTO telegram_dm_topic_mode (
                    chat_id, user_id, enabled, activated_at, updated_at,
                    has_topics_enabled, allows_users_to_create_topics,
                    capability_checked_at
                ) VALUES (?, ?, 1, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    user_id = excluded.user_id,
                    enabled = 1,
                    updated_at = excluded.updated_at,
                    has_topics_enabled = excluded.has_topics_enabled,
                    allows_users_to_create_topics = excluded.allows_users_to_create_topics,
                    capability_checked_at = excluded.capability_checked_at
                """,
                (
                    str(chat_id),
                    str(user_id),
                    now,
                    now,
                    _to_int(has_topics_enabled),
                    _to_int(allows_users_to_create_topics),
                    now,
                ),
            )
        self._execute_write(_do)

    def disable_telegram_topic_mode(
        self,
        *,
        chat_id: str,
        clear_bindings: bool = True,
    ) -> None:
        """Disable Telegram DM topic mode for one private chat.

        When ``clear_bindings`` is True (default) the (chat_id, thread_id)
        bindings for this chat are also cleared so re-enabling later
        starts from a clean slate. Set to False if the operator wants to
        preserve bindings for a later re-enable.

        Never creates the topic-mode tables from scratch; if they don't
        exist there is nothing to disable and the call is a no-op.
        """
        def _do(conn):
            try:
                conn.execute(
                    "UPDATE telegram_dm_topic_mode SET enabled = 0, updated_at = ? "
                    "WHERE chat_id = ?",
                    (time.time(), str(chat_id)),
                )
                if clear_bindings:
                    conn.execute(
                        "DELETE FROM telegram_dm_topic_bindings WHERE chat_id = ?",
                        (str(chat_id),),
                    )
            except sqlite3.OperationalError:
                # Tables don't exist yet — nothing to disable.
                return
        self._execute_write(_do)

    def is_telegram_topic_mode_enabled(self, *, chat_id: str, user_id: str) -> bool:
        """Return whether Telegram DM topic mode is enabled for this chat/user."""
        with self._lock:
            try:
                row = self._conn.execute(
                    """
                    SELECT enabled FROM telegram_dm_topic_mode
                    WHERE chat_id = ? AND user_id = ?
                    """,
                    (str(chat_id), str(user_id)),
                ).fetchone()
            except sqlite3.OperationalError:
                return False
        if row is None:
            return False
        enabled = row["enabled"] if isinstance(row, sqlite3.Row) else row[0]
        return bool(enabled)

    def get_telegram_topic_binding(
        self,
        *,
        chat_id: str,
        thread_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Return the session binding for a Telegram DM topic, if present."""
        with self._lock:
            try:
                row = self._conn.execute(
                    """
                    SELECT * FROM telegram_dm_topic_bindings
                    WHERE chat_id = ? AND thread_id = ?
                    """,
                    (str(chat_id), str(thread_id)),
                ).fetchone()
            except sqlite3.OperationalError:
                return None
        return dict(row) if row else None

    def list_telegram_topic_bindings_for_chat(
        self,
        *,
        chat_id: str,
    ) -> List[Dict[str, Any]]:
        """All Telegram DM topic bindings for one chat, newest first.

        Read-only; returns [] if the bindings table doesn't exist yet
        (does not trigger the topic-mode migration).
        """
        with self._lock:
            try:
                rows = self._conn.execute(
                    "SELECT * FROM telegram_dm_topic_bindings "
                    "WHERE chat_id = ? ORDER BY updated_at DESC",
                    (str(chat_id),),
                ).fetchall()
            except sqlite3.OperationalError:
                return []
        return [dict(row) for row in rows]

    def get_telegram_topic_binding_by_session(
        self,
        *,
        session_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Return the Telegram DM topic binding for a given session_id, if present.

        Uses the UNIQUE INDEX on telegram_dm_topic_bindings(session_id) for an
        efficient reverse lookup. Returns None when the session has no binding or
        the table does not exist yet.
        """
        with self._lock:
            try:
                row = self._conn.execute(
                    """
                    SELECT * FROM telegram_dm_topic_bindings
                    WHERE session_id = ?
                    """,
                    (str(session_id),),
                ).fetchone()
            except sqlite3.OperationalError:
                return None
        return dict(row) if row else None

    def delete_telegram_topic_binding(
        self,
        *,
        chat_id: str,
        thread_id: str,
    ) -> int:
        """Remove the binding row for a single (chat, thread) pair.

        Called when the Telegram Bot API confirms a topic was deleted
        externally (``Thread not found`` after the same-thread retry
        already failed).  Without this prune, the stale row keeps
        living in ``telegram_dm_topic_bindings`` and the
        recovery logic in ``gateway.run._recover_telegram_topic_thread_id``
        cheerfully redirects future inbound messages to the deleted
        topic, causing tool progress, approvals, and replies to land
        in the wrong place.  Issue #31501.

        When this prune removes the chat's *last* remaining binding,
        the chat's row in ``telegram_dm_topic_mode`` is also flipped to
        ``enabled = 0`` in the same transaction.  Otherwise the chat
        would be left in topic mode with zero lanes — and
        ``gateway.run._recover_telegram_topic_thread_id`` keeps treating
        the chat as topic-enabled, lobby messages keep hunting for a
        binding that no longer exists, and a user who disabled topics in
        the Telegram client (rather than via ``/topic off``) stays stuck
        until the next send happens to fail. Clearing the flag makes
        recovery fully stand down once the dead topics are gone.

        Returns the number of binding rows deleted (0 when the binding
        was already absent or the topic-mode tables haven't been
        migrated yet — both are silent no-ops; we never raise from
        a cleanup hot path).
        """
        chat_id = str(chat_id)
        thread_id = str(thread_id)
        deleted = {"count": 0}

        def _do(conn):
            try:
                cursor = conn.execute(
                    """
                    DELETE FROM telegram_dm_topic_bindings
                    WHERE chat_id = ? AND thread_id = ?
                    """,
                    (chat_id, thread_id),
                )
                deleted["count"] = cursor.rowcount or 0
            except sqlite3.OperationalError:
                # Tables don't exist yet — nothing to prune.
                deleted["count"] = 0
                return
            if not deleted["count"]:
                return
            # If that was the chat's last binding, disable topic mode for
            # the chat so recovery stops steering lobby messages at a now
            # empty lane set. Same transaction → no read-after-prune race.
            try:
                remaining = conn.execute(
                    """
                    SELECT 1 FROM telegram_dm_topic_bindings
                    WHERE chat_id = ? LIMIT 1
                    """,
                    (chat_id,),
                ).fetchone()
                if remaining is None:
                    conn.execute(
                        "UPDATE telegram_dm_topic_mode "
                        "SET enabled = 0, updated_at = ? WHERE chat_id = ?",
                        (time.time(), chat_id),
                    )
            except sqlite3.OperationalError:
                # telegram_dm_topic_mode absent — binding prune still stands.
                pass

        self._execute_write(_do)
        return deleted["count"]

    def bind_telegram_topic(
        self,
        *,
        chat_id: str,
        thread_id: str,
        user_id: str,
        session_key: str,
        session_id: str,
        managed_mode: str = "auto",
    ) -> None:
        """Bind one Telegram DM topic thread to one opencodon session.

        A opencodon session may only be linked to one Telegram topic in MVP.
        Rebinding the same topic to the same session is idempotent; trying to
        link the same session to a different topic raises ValueError.
        """
        self.apply_telegram_topic_migration()
        now = time.time()
        chat_id = str(chat_id)
        thread_id = str(thread_id)
        user_id = str(user_id)
        session_key = str(session_key)
        session_id = str(session_id)

        def _do(conn):
            existing_session = conn.execute(
                """
                SELECT chat_id, thread_id FROM telegram_dm_topic_bindings
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            if existing_session is not None:
                linked_chat = existing_session["chat_id"] if isinstance(existing_session, sqlite3.Row) else existing_session[0]
                linked_thread = existing_session["thread_id"] if isinstance(existing_session, sqlite3.Row) else existing_session[1]
                if str(linked_chat) != chat_id or str(linked_thread) != thread_id:
                    raise ValueError("session is already linked to another Telegram topic")

            conn.execute(
                """
                INSERT INTO telegram_dm_topic_bindings (
                    chat_id, thread_id, user_id, session_key, session_id,
                    managed_mode, linked_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, thread_id) DO UPDATE SET
                    user_id = excluded.user_id,
                    session_key = excluded.session_key,
                    session_id = excluded.session_id,
                    managed_mode = excluded.managed_mode,
                    updated_at = excluded.updated_at
                """,
                (
                    chat_id,
                    thread_id,
                    user_id,
                    session_key,
                    session_id,
                    managed_mode,
                    now,
                    now,
                ),
            )
        self._execute_write(_do)

    def is_telegram_session_linked_to_topic(self, *, session_id: str) -> bool:
        """Return True if a opencodon session is already bound to any Telegram DM topic.

        Read-only: does NOT trigger the telegram-topic migration. If the
        topic-mode tables have not been created yet (i.e. nobody has run
        ``/topic`` in this profile), the session is by definition unbound
        and we return False.
        """
        with self._lock:
            try:
                row = self._conn.execute(
                    """
                    SELECT 1 FROM telegram_dm_topic_bindings
                    WHERE session_id = ?
                    LIMIT 1
                    """,
                    (str(session_id),),
                ).fetchone()
            except sqlite3.OperationalError:
                return False
        return row is not None

    def list_unlinked_telegram_sessions_for_user(
        self,
        *,
        chat_id: str,
        user_id: str,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """List previous Telegram sessions for this user that are not bound to a topic.

        Read-only: does NOT trigger the telegram-topic migration. If the
        topic-mode tables are absent, fall back to a simpler query that
        just returns this user's Telegram sessions — there can't be any
        bindings yet.
        """
        with self._lock:
            try:
                rows = self._conn.execute(
                    """
                    SELECT s.*,
                        COALESCE(
                            (SELECT SUBSTR(REPLACE(REPLACE(m.content, X'0A', ' '), X'0D', ' '), 1, 63)
                             FROM messages m
                             WHERE m.session_id = s.id AND m.role = 'user' AND m.content IS NOT NULL
                             ORDER BY m.timestamp, m.id LIMIT 1),
                            ''
                        ) AS _preview_raw,
                        COALESCE(
                            (SELECT MAX(m2.timestamp) FROM messages m2 WHERE m2.session_id = s.id),
                            s.started_at
                        ) AS last_active
                    FROM sessions s
                    WHERE s.source = 'telegram'
                      AND s.user_id = ?
                      AND NOT EXISTS (
                          SELECT 1 FROM telegram_dm_topic_bindings b
                          WHERE b.session_id = s.id
                      )
                    ORDER BY last_active DESC, s.started_at DESC
                    LIMIT ?
                    """,
                    (str(user_id), int(limit)),
                ).fetchall()
            except sqlite3.OperationalError:
                # telegram_dm_topic_bindings doesn't exist yet — no bindings
                # means every telegram session for this user is "unlinked".
                rows = self._conn.execute(
                    """
                    SELECT s.*,
                        COALESCE(
                            (SELECT SUBSTR(REPLACE(REPLACE(m.content, X'0A', ' '), X'0D', ' '), 1, 63)
                             FROM messages m
                             WHERE m.session_id = s.id AND m.role = 'user' AND m.content IS NOT NULL
                             ORDER BY m.timestamp, m.id LIMIT 1),
                            ''
                        ) AS _preview_raw,
                        COALESCE(
                            (SELECT MAX(m2.timestamp) FROM messages m2 WHERE m2.session_id = s.id),
                            s.started_at
                        ) AS last_active
                    FROM sessions s
                    WHERE s.source = 'telegram'
                      AND s.user_id = ?
                    ORDER BY last_active DESC, s.started_at DESC
                    LIMIT ?
                    """,
                    (str(user_id), int(limit)),
                ).fetchall()

        sessions: List[Dict[str, Any]] = []
        for row in rows:
            session = dict(row)
            raw = str(session.pop("_preview_raw", "") or "").strip()
            session["preview"] = raw[:60] + ("..." if len(raw) > 60 else "") if raw else ""
            sessions.append(session)
        return sessions

