"""SessionDB HandoffMixin — extracted from state/__init__ (restructure Phase 4).

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



class HandoffMixin:
    def request_handoff(self, session_id: str, platform: str) -> bool:
        """Mark a session as pending handoff to the given platform.

        Returns True if the row was found and not already in flight; False if
        the session is already in a non-terminal handoff state.
        """
        def _do(conn):
            cur = conn.execute(
                "UPDATE sessions "
                "SET handoff_state = 'pending', "
                "    handoff_platform = ?, "
                "    handoff_error = NULL "
                "WHERE id = ? AND (handoff_state IS NULL "
                "                  OR handoff_state IN ('completed', 'failed'))",
                (platform, session_id),
            )
            return cur.rowcount > 0
        return self._execute_write(_do)

    def get_handoff_state(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Read the current handoff state for a session.

        Returns ``{"state", "platform", "error"}`` or None if the session has
        no handoff record.
        """
        try:
            cur = self._conn.execute(
                "SELECT handoff_state, handoff_platform, handoff_error "
                "FROM sessions WHERE id = ?",
                (session_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "state": row["handoff_state"],
                "platform": row["handoff_platform"],
                "error": row["handoff_error"],
            }
        except Exception:
            return None

    def list_pending_handoffs(self) -> List[Dict[str, Any]]:
        """Return all sessions in handoff_state='pending', oldest first.

        Used by the gateway's handoff watcher.
        """
        try:
            cur = self._conn.execute(
                "SELECT * FROM sessions "
                "WHERE handoff_state = 'pending' "
                "ORDER BY started_at ASC"
            )
            return [dict(r) for r in cur.fetchall()]
        except Exception:
            return []

    def claim_handoff(self, session_id: str) -> bool:
        """Atomically transition pending → running. Returns True if claimed."""
        def _do(conn):
            cur = conn.execute(
                "UPDATE sessions SET handoff_state = 'running' "
                "WHERE id = ? AND handoff_state = 'pending'",
                (session_id,),
            )
            return cur.rowcount > 0
        return self._execute_write(_do)

    def complete_handoff(self, session_id: str) -> None:
        """Mark a handoff as completed."""
        def _do(conn):
            conn.execute(
                "UPDATE sessions SET handoff_state = 'completed', "
                "handoff_error = NULL WHERE id = ?",
                (session_id,),
            )
        self._execute_write(_do)

    def fail_handoff(self, session_id: str, error: str) -> None:
        """Mark a handoff as failed and record the reason."""
        def _do(conn):
            conn.execute(
                "UPDATE sessions SET handoff_state = 'failed', "
                "handoff_error = ? WHERE id = ?",
                (error[:500], session_id),
            )
        self._execute_write(_do)

