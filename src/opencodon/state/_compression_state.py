"""SessionDB CompressionStateMixin — extracted from state/__init__ (restructure Phase 4).

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

from opencodon.core.memory_manager import sanitize_context
from opencodon.core.message_sanitization import _sanitize_surrogates
from opencodon_constants import get_opencodon_home
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar

from opencodon.state import (  # noqa: E402
    _COMPRESSION_CHILD_SQL,
    logger,
)


class CompressionStateMixin:
    def promote_to_session_reset(
        self, session_id: str, reason: str = "session_reset"
    ) -> bool:
        """Durably mark a session as ended by an intentional reset boundary.

        Promotes *only* live rows (``ended_at IS NULL``) or rows carrying an
        accidental end_reason that the recovery query
        (``find_latest_gateway_session_for_peer``) treats as recoverable:
        ``agent_close`` (older gateway cleanup bug) and ``ws_orphan_reap``
        (mistaken TUI reaper).  Explicit conversation boundaries such as
        ``compression``, ``session_reset``, ``session_switch``, etc. are
        preserved — the first writer wins for those, and a later expiry
        finalization must not silently overwrite them.

        Plain ``end_session()`` is NOT sufficient for reset boundaries: it
        no-ops on an already-ended row, so a row that agent cleanup already
        closed as ``agent_close`` would stay recoverable and stale-route
        recovery would resurrect the reset session with its full history
        (#61220, #61993, #63539).

        Keep this promotion set in sync with the recoverable set in
        ``find_latest_gateway_session_for_peer`` — any reason recovery would
        reopen must be promotable here.

        ``reason`` lets reset paths keep their auditable specific reasons
        (``idle``, ``daily``, ``suspended``, ``resume_pending_expired``).

        Returns ``True`` when the row was promoted, ``False`` when skipped
        (already has a different explicit end_reason, or row not found).
        """
        if not session_id:
            return False
        now = time.time()

        def _do(conn):
            cursor = conn.execute(
                "UPDATE sessions SET ended_at = ?, end_reason = ? "
                "WHERE id = ? AND (ended_at IS NULL "
                "OR end_reason IN ('agent_close', 'ws_orphan_reap'))",
                (now, reason, session_id),
            )
            return cursor.rowcount

        try:
            rows = self._execute_write(_do)
            return bool(rows)
        except Exception:
            return False

    def record_compression_failure_cooldown(
        self,
        session_id: str,
        cooldown_until: float,
        error: Optional[str] = None,
    ) -> None:
        """Persist the active compression-failure cooldown for a session."""
        if not session_id:
            return

        def _do(conn):
            conn.execute(
                "UPDATE sessions SET compression_failure_cooldown_until = ?, "
                "compression_failure_error = ? WHERE id = ?",
                (cooldown_until, error, session_id),
            )

        try:
            self._execute_write(_do)
        except sqlite3.Error as exc:
            logger.warning(
                "record_compression_failure_cooldown(%s) failed: %s",
                session_id, exc,
            )

    def get_compression_failure_cooldown(
        self,
        session_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Return the active compression-failure cooldown for ``session_id``."""
        if not session_id:
            return None
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT compression_failure_cooldown_until, compression_failure_error "
                "FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        cooldown_until = (
            row["compression_failure_cooldown_until"]
            if isinstance(row, sqlite3.Row)
            else row[0]
        )
        if cooldown_until is None:
            return None
        cooldown_until = float(cooldown_until)
        if cooldown_until <= now:
            return None
        error = (
            row["compression_failure_error"]
            if isinstance(row, sqlite3.Row)
            else row[1]
        )
        return {
            "cooldown_until": cooldown_until,
            "remaining_seconds": cooldown_until - now,
            "error": error,
        }

    def clear_compression_failure_cooldown(self, session_id: str) -> None:
        """Clear any persisted compression-failure cooldown for a session."""
        if not session_id:
            return

        def _do(conn):
            conn.execute(
                "UPDATE sessions SET compression_failure_cooldown_until = NULL, "
                "compression_failure_error = NULL WHERE id = ?",
                (session_id,),
            )

        try:
            self._execute_write(_do)
        except sqlite3.Error as exc:
            logger.warning(
                "clear_compression_failure_cooldown(%s) failed: %s",
                session_id, exc,
            )

    def get_compression_fallback_streak(self, session_id: str) -> int:
        """Return the persisted deterministic-fallback streak."""
        if not session_id:
            return 0
        with self._lock:
            conn = self._conn
            if conn is None:
                return 0
            row = conn.execute(
                "SELECT compression_fallback_streak FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return 0
        value = (
            row["compression_fallback_streak"]
            if isinstance(row, sqlite3.Row)
            else row[0]
        )
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    def set_compression_fallback_streak(self, session_id: str, streak: int) -> None:
        """Persist the deterministic-fallback streak for one session."""
        if not session_id:
            return
        normalized = max(0, int(streak))

        def _do(conn):
            conn.execute(
                "UPDATE sessions SET compression_fallback_streak = ? WHERE id = ?",
                (normalized, session_id),
            )

        self._execute_write(_do)

    def refresh_compression_lock(
        self,
        session_id: str,
        holder: str,
        ttl_seconds: float = 300.0,
    ) -> bool:
        """Extend the compression lock lease if ``holder`` still owns it."""
        if not session_id or not holder:
            return False
        now = time.time()
        expires_at = now + ttl_seconds

        def _do(conn):
            cur = conn.execute(
                "UPDATE compression_locks SET expires_at = ? "
                "WHERE session_id = ? AND holder = ? AND expires_at >= ?",
                (expires_at, session_id, holder, now),
            )
            return cur.rowcount > 0

        try:
            return bool(self._execute_write(_do))
        except sqlite3.Error as exc:
            logger.warning(
                "refresh_compression_lock(%s) failed: %s",
                session_id, exc,
            )
            return False

    def try_acquire_compression_lock(
        self,
        session_id: str,
        holder: str,
        ttl_seconds: float = 300.0,
    ) -> bool:
        """Try to atomically acquire the compression lock for ``session_id``.

        Returns ``True`` on success (caller now owns the lock and must
        release via :meth:`release_compression_lock`).  Returns ``False``
        if another holder already owns a non-expired lock — the caller
        MUST NOT proceed with compression in that case (its rotation would
        race against the holder's, splitting the session lineage).

        Expired locks (``expires_at < now``) are reclaimed transparently:
        the stale row is deleted and the new holder acquires it. This
        prevents a crashed compressor from permanently blocking the
        session.

        Implementation: single-transaction DELETE-expired + INSERT-or-IGNORE,
        followed by a SELECT to confirm we got the row. SQLite serialises
        writes, so the whole sequence is atomic against other writers.
        """
        if not session_id:
            return False
        now = time.time()
        expires_at = now + ttl_seconds

        def _do(conn):
            # First: reclaim any expired lock for this session_id.
            conn.execute(
                "DELETE FROM compression_locks "
                "WHERE session_id = ? AND expires_at < ?",
                (session_id, now),
            )
            # Then: try to insert. INSERT OR IGNORE returns no rowcount
            # difference — verify ownership via SELECT.
            conn.execute(
                "INSERT OR IGNORE INTO compression_locks "
                "(session_id, holder, acquired_at, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (session_id, holder, now, expires_at),
            )
            row = conn.execute(
                "SELECT holder FROM compression_locks WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            return row is not None and (
                row["holder"] if isinstance(row, sqlite3.Row) else row[0]
            ) == holder

        try:
            return bool(self._execute_write(_do))
        except sqlite3.Error as exc:
            logger.warning(
                "try_acquire_compression_lock(%s) failed: %s",
                session_id, exc,
            )
            # Fail open: returning False makes the caller skip compression,
            # which is the safe behaviour when the lock subsystem is broken.
            return False

    def release_compression_lock(self, session_id: str, holder: str) -> None:
        """Release the compression lock for ``session_id`` iff we own it.

        Idempotent: no-op when the lock has already expired and been
        reclaimed by a different holder, or when no lock exists. The
        ``holder`` check prevents a late-returning compressor from
        clobbering a fresh lock held by someone else.
        """
        if not session_id:
            return

        def _do(conn):
            conn.execute(
                "DELETE FROM compression_locks "
                "WHERE session_id = ? AND holder = ?",
                (session_id, holder),
            )

        try:
            self._execute_write(_do)
        except sqlite3.Error as exc:
            logger.warning(
                "release_compression_lock(%s) failed: %s",
                session_id, exc,
            )

    def get_compression_lock_holder(self, session_id: str) -> Optional[str]:
        """Return the current (non-expired) holder for ``session_id``, or None.

        Diagnostic helper — not used by the locking protocol itself.
        """
        if not session_id:
            return None
        now = time.time()
        row = self._conn.execute(
            "SELECT holder FROM compression_locks "
            "WHERE session_id = ? AND expires_at >= ?",
            (session_id, now),
        ).fetchone()
        if row is None:
            return None
        return row["holder"] if isinstance(row, sqlite3.Row) else row[0]

    def finalize_orphaned_compression_sessions(self) -> int:
        """Mark orphaned compression continuation sessions as ended.

        Targets child sessions that were never finalized: parent is ended
        with reason='compression', child has messages but no end_reason/ended_at
        and api_call_count=0.  Non-destructive: preserves all messages and sets
        end_reason='orphaned_compression'.  Fix for #20001.
        """
        cutoff = time.time() - 604800  # 7 days

        def _do(conn):
            now = time.time()
            result = conn.execute(
                """
                UPDATE sessions
                SET ended_at = ?,
                    end_reason = 'orphaned_compression'
                WHERE api_call_count = 0
                  AND end_reason IS NULL
                  AND ended_at IS NULL
                  AND started_at < ?
                  AND parent_session_id IS NOT NULL
                  AND EXISTS (
                      SELECT 1 FROM sessions p
                      WHERE p.id = sessions.parent_session_id
                        AND p.end_reason = 'compression'
                        AND p.ended_at IS NOT NULL
                  )
                  AND EXISTS (
                      SELECT 1 FROM messages m
                      WHERE m.session_id = sessions.id
                  )
                """,
                (now, cutoff),
            )
            return result.rowcount

        return self._execute_write(_do) or 0

    def _is_compression_ancestor(
        self, conn, *, ancestor_id: str, descendant_id: str
    ) -> bool:
        """Return True if *ancestor_id* is a compression predecessor of
        *descendant_id* (walking parent links up the continuation chain).

        The continuation edge is the canonical one shared with
        :func:`_ephemeral_child_sql` / :meth:`set_session_archived`
        (``_COMPRESSION_CHILD_SQL``): a parent → child edge counts only when the
        parent ended with ``end_reason = 'compression'`` and the child started
        at or after the parent's ``ended_at``, which distinguishes continuations
        from delegate subagents / branch children that also carry a
        ``parent_session_id``. Expressed as a single recursive CTE rather than a
        per-hop Python walk so the edge definition lives in exactly one place.
        """
        if not ancestor_id or not descendant_id or ancestor_id == descendant_id:
            return False
        # Walk parent links up from the descendant, following only compression
        # continuation edges, and check whether ancestor_id is reached.
        edge = _COMPRESSION_CHILD_SQL.format(a="child")
        row = conn.execute(
            f"""
            WITH RECURSIVE ancestors(id) AS (
                SELECT ?
                UNION
                SELECT parent.id
                FROM ancestors a
                JOIN sessions child ON child.id = a.id
                JOIN sessions parent ON parent.id = child.parent_session_id
                WHERE {edge}
            )
            SELECT 1 FROM ancestors WHERE id = ? AND id != ? LIMIT 1
            """,
            (descendant_id, ancestor_id, descendant_id),
        ).fetchone()
        return row is not None

    def get_compression_tip(self, session_id: str) -> Optional[str]:
        """Walk the compression-continuation chain forward and return the tip.

        A compression continuation is a child of a session whose
        ``end_reason = 'compression'``.  Older builds tried to distinguish
        continuations from branches/subagents by requiring
        ``child.started_at >= parent.ended_at``.  That ordering is too brittle:
        gateway + compression races can insert the real continuation row before
        the parent row's ``ended_at`` is written, while a stale websocket later
        creates/reuses a sibling that *does* satisfy the timestamp test.  The
        visible symptom is brutal: desktop resume follows the stale sibling and
        the user's latest messages look "lost" even though they are persisted in
        the real continuation chain.

        Instead, only follow children of compression-ended parents, exclude
        explicit branch/delegate/tool children, and prefer children that are
        themselves continuing the compression chain (``end_reason='compression'``)
        or still live over stale closed siblings such as ``ws_orphan_reap``.
        Returns the latest continuation tip, or the input id when no
        continuation exists.
        """
        current = session_id
        seen = {current} if current else set()
        # Bound the walk defensively — compression chains this deep are
        # pathological and shouldn't happen in practice. 100 = plenty.
        for _ in range(100):
            with self._lock:
                cursor = self._conn.execute(
                    """
                    SELECT child.id
                    FROM sessions parent
                    JOIN sessions child ON child.parent_session_id = parent.id
                    WHERE parent.id = ?
                      AND parent.end_reason = 'compression'
                      AND json_extract(COALESCE(child.model_config, '{}'), '$._branched_from') IS NULL
                      AND json_extract(COALESCE(child.model_config, '{}'), '$._delegate_from') IS NULL
                      AND COALESCE(child.source, '') != 'tool'
                    ORDER BY
                      CASE
                        WHEN child.end_reason = 'compression' THEN 0
                        WHEN child.ended_at IS NULL THEN 1
                        ELSE 2
                      END,
                      COALESCE(
                        (SELECT MAX(m.timestamp) FROM messages m WHERE m.session_id = child.id),
                        child.started_at
                      ) DESC,
                      child.started_at DESC,
                      child.id DESC
                    LIMIT 1
                    """,
                    (current,),
                )
                row = cursor.fetchone()
            if row is None:
                return current
            child_id = row["id"]
            if not child_id or child_id in seen:
                return current
            seen.add(child_id)
            current = child_id
        return current

    def _is_compression_child_row(self, child: Dict[str, Any]) -> bool:
        parent_id = child.get("parent_session_id")
        if not parent_id or self._is_branch_child_row(child):
            return False
        parent = self.get_session(parent_id)
        return bool(parent and parent.get("end_reason") == "compression")

    def get_compression_lineage(self, session_id: str) -> List[str]:
        """Return compression ancestors through tip in chronological order."""
        session = self.get_session(session_id)
        if not session or self._is_branch_child_row(session):
            return [session_id] if session else []

        root = session
        ancestors = {root["id"]}
        while self._is_compression_child_row(root):
            parent = self.get_session(root["parent_session_id"])
            if not parent or parent["id"] in ancestors:
                break
            root = parent
            ancestors.add(root["id"])

        lineage = [root["id"]]
        seen = {root["id"]}
        current = root
        while current.get("end_reason") == "compression":
            with self._lock:
                rows = self._conn.execute(
                    """
                    SELECT * FROM sessions
                    WHERE parent_session_id = ?
                    ORDER BY started_at ASC
                    """,
                    (current["id"],),
                ).fetchall()
            next_child = None
            for row in rows:
                candidate = dict(row)
                if not self._is_branch_child_row(candidate):
                    next_child = candidate
                    break
            if not next_child or next_child["id"] in seen:
                break
            lineage.append(next_child["id"])
            seen.add(next_child["id"])
            current = next_child
            if current["id"] == session_id:
                # Continue to include later compression tips only when the
                # requested session itself was compacted.
                continue
        return lineage if session_id in lineage else [session_id]

