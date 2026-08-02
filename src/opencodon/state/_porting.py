"""SessionDB ImportExportMixin — extracted from state/__init__ (restructure Phase 4).

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



class ImportExportMixin:
    def export_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Export a single session with all its messages as a dict."""
        session = self.get_session(session_id)
        if not session:
            return None
        messages = self.get_messages(session_id)
        return {**session, "messages": messages}

    def export_session_lineage(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Export a compression lineage as one logical session dict."""
        lineage_ids = self.get_compression_lineage(session_id)
        if not lineage_ids:
            return None
        segments = []
        for sid in lineage_ids:
            segment = self.export_session(sid)
            if segment:
                segments.append(segment)
        if not segments:
            return None
        base = dict(segments[-1])
        total_messages = sum(len(seg.get("messages") or []) for seg in segments)
        base["segments"] = segments
        base["lineage_session_ids"] = [seg["id"] for seg in segments]
        base["message_count"] = total_messages
        base["messages"] = [msg for seg in segments for msg in (seg.get("messages") or [])]
        return base

    def export_all(self, source: str = None) -> List[Dict[str, Any]]:
        """
        Export all sessions (with messages) as a list of dicts.
        Suitable for writing to a JSONL file for backup/analysis.
        """
        sessions = self.search_sessions(source=source, limit=100000)
        results = []
        for session in sessions:
            messages = self.get_messages(session["id"])
            results.append({**session, "messages": messages})
        return results

    @staticmethod
    def _import_text_or_none(value: Any, field: str) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, str):
            return value
        raise ValueError(f"{field} must be a string")

    @staticmethod
    def _import_json_object_or_none(value: Any, field: str) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{field} must be valid JSON") from exc
            if not isinstance(parsed, dict):
                raise ValueError(f"{field} must be a JSON object")
            return value
        if not isinstance(value, dict):
            raise ValueError(f"{field} must be a JSON object")
        try:
            return json.dumps(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} must be JSON serializable") from exc

    @staticmethod
    def _float_or_none(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _import_int_or_none(value: Any, field: str) -> Optional[int]:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} must be an integer") from exc

    @staticmethod
    def _int_or_default(value: Any, default: int = 0) -> int:
        if value is None:
            return default
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _reasoning_json_value(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value

    @staticmethod
    def _import_error(index: int, session_id: str, error: str) -> Dict[str, Any]:
        item: Dict[str, Any] = {"index": index, "error": error}
        if session_id:
            item["session_id"] = session_id
        return item

    def import_sessions(self, sessions: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Import sessions exported by :meth:`export_session` or ``export_all``.

        Existing session IDs are skipped. Imported child sessions keep their
        parent only when that parent already exists or is included in the same
        import payload; otherwise the child is detached so partial imports don't
        fail foreign-key validation. Gateway routing, handoff, rewind, and other
        live runtime state are intentionally reset: this restores conversation
        history, not ownership of a live channel or process.
        """
        if not isinstance(sessions, list):
            raise ValueError("sessions must be a list")
        if len(sessions) > self._IMPORT_MAX_SESSIONS:
            raise ValueError(
                f"sessions must contain at most {self._IMPORT_MAX_SESSIONS} entries"
            )

        normalized: List[Dict[str, Any]] = []
        errors: List[Dict[str, Any]] = []
        seen_ids: set[str] = set()
        total_messages = 0
        total_bytes = 0
        session_text_fields = (
            "source",
            "user_id",
            "model",
            "system_prompt",
            "end_reason",
            "cwd",
            "git_branch",
            "git_repo_root",
            "billing_provider",
            "billing_base_url",
            "billing_mode",
            "cost_status",
            "cost_source",
            "pricing_version",
            "title",
        )
        message_text_fields = (
            "role",
            "tool_call_id",
            "tool_name",
            "effect_disposition",
            "finish_reason",
            "reasoning",
            "reasoning_content",
            "platform_message_id",
            "message_id",
        )

        for index, raw in enumerate(sessions):
            if not isinstance(raw, dict):
                errors.append(self._import_error(index, "", "session must be an object"))
                continue
            session_id = str(raw.get("id") or "").strip()
            if not session_id:
                errors.append(self._import_error(index, "", "session id is required"))
                continue
            if session_id in seen_ids:
                errors.append(self._import_error(index, session_id, "duplicate session id"))
                continue
            messages = raw.get("messages") or []
            if not isinstance(messages, list):
                errors.append(self._import_error(index, session_id, "messages must be a list"))
                continue
            if len(messages) > self._IMPORT_MAX_MESSAGES_PER_SESSION:
                errors.append(
                    self._import_error(
                        index,
                        session_id,
                        "messages exceeds the per-session import limit",
                    )
                )
                continue
            if any(not isinstance(msg, dict) for msg in messages):
                errors.append(
                    self._import_error(
                        index,
                        session_id,
                        "messages must contain only objects",
                    )
                )
                continue

            try:
                session_bytes = len(
                    json.dumps(raw, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                )
            except (TypeError, ValueError):
                errors.append(
                    self._import_error(index, session_id, "session must be JSON serializable")
                )
                continue
            if session_bytes > self._IMPORT_MAX_SESSION_BYTES:
                errors.append(
                    self._import_error(index, session_id, "session exceeds the import size limit")
                )
                continue
            total_bytes += session_bytes
            if total_bytes > self._IMPORT_MAX_TOTAL_BYTES:
                errors.append(
                    self._import_error(index, session_id, "import exceeds the total size limit")
                )
                continue

            try:
                clean_session = dict(raw)
                clean_session["id"] = session_id
                clean_session["model_config"] = self._import_json_object_or_none(
                    clean_session.get("model_config"), "model_config"
                )
                clean_session["parent_session_id"] = self._import_text_or_none(
                    clean_session.get("parent_session_id"), "parent_session_id"
                )
                for field in session_text_fields:
                    clean_session[field] = self._import_text_or_none(
                        clean_session.get(field), field
                    )

                clean_messages: List[Dict[str, Any]] = []
                for message_index, message in enumerate(messages):
                    clean_message = dict(message)
                    role = clean_message.get("role")
                    if not isinstance(role, str) or not role:
                        raise ValueError(f"messages[{message_index}].role must be a non-empty string")
                    for field in message_text_fields:
                        if field == "role":
                            continue
                        clean_message[field] = self._import_text_or_none(
                            clean_message.get(field), field
                        )
                    clean_message["token_count"] = self._import_int_or_none(
                        clean_message.get("token_count"), "token_count"
                    )
                    clean_messages.append(clean_message)
            except ValueError as exc:
                errors.append(self._import_error(index, session_id, str(exc)))
                continue

            total_messages += len(clean_messages)
            if total_messages > self._IMPORT_MAX_TOTAL_MESSAGES:
                errors.append(
                    self._import_error(
                        index,
                        session_id,
                        "messages exceeds the total import limit",
                    )
                )
                continue
            seen_ids.add(session_id)
            normalized.append(
                {"index": index, "session": clean_session, "messages": clean_messages}
            )

        if errors:
            return {
                "ok": False,
                "imported": 0,
                "skipped": 0,
                "detached": 0,
                "errors": errors,
            }

        def _do(conn):
            imported_ids: List[str] = []
            skipped_ids: List[str] = []
            parent_updates: List[tuple[str, str]] = []
            detached = 0

            for item in normalized:
                raw = item["session"]
                messages = item["messages"]
                session_id = str(raw.get("id") or "").strip()
                exists = conn.execute(
                    "SELECT 1 FROM sessions WHERE id = ? LIMIT 1",
                    (session_id,),
                ).fetchone()
                if exists:
                    skipped_ids.append(session_id)
                    continue

                started_at = self._float_or_none(raw.get("started_at"))
                if started_at is None:
                    started_at = time.time()
                archived = 1 if raw.get("archived") else 0

                conn.execute(
                    """INSERT INTO sessions (
                           id, source, user_id, model, model_config, system_prompt,
                           parent_session_id, started_at, ended_at, end_reason,
                           message_count, tool_call_count, input_tokens, output_tokens,
                           cache_read_tokens, cache_write_tokens, reasoning_tokens,
                           cwd, git_branch, git_repo_root,
                           billing_provider, billing_base_url, billing_mode,
                           estimated_cost_usd, actual_cost_usd, cost_status, cost_source,
                           pricing_version, title, api_call_count, archived
                       )
                       VALUES (
                           :id, :source, :user_id, :model, :model_config,
                           :system_prompt, NULL, :started_at, :ended_at,
                           :end_reason, 0, 0, :input_tokens, :output_tokens,
                           :cache_read_tokens, :cache_write_tokens,
                           :reasoning_tokens, :cwd, :git_branch, :git_repo_root,
                           :billing_provider, :billing_base_url, :billing_mode,
                           :estimated_cost_usd, :actual_cost_usd, :cost_status,
                           :cost_source, :pricing_version, :title,
                           :api_call_count, :archived
                       )""",
                    {
                        "id": session_id,
                        "source": str(raw.get("source") or "import"),
                        "user_id": raw.get("user_id"),
                        "model": raw.get("model"),
                        "model_config": raw.get("model_config"),
                        "system_prompt": raw.get("system_prompt"),
                        "started_at": started_at,
                        "ended_at": self._float_or_none(raw.get("ended_at")),
                        "end_reason": raw.get("end_reason"),
                        "input_tokens": self._int_or_default(raw.get("input_tokens")),
                        "output_tokens": self._int_or_default(raw.get("output_tokens")),
                        "cache_read_tokens": self._int_or_default(
                            raw.get("cache_read_tokens")
                        ),
                        "cache_write_tokens": self._int_or_default(
                            raw.get("cache_write_tokens")
                        ),
                        "reasoning_tokens": self._int_or_default(
                            raw.get("reasoning_tokens")
                        ),
                        "cwd": raw.get("cwd"),
                        "git_branch": raw.get("git_branch"),
                        "git_repo_root": raw.get("git_repo_root"),
                        "billing_provider": raw.get("billing_provider"),
                        "billing_base_url": raw.get("billing_base_url"),
                        "billing_mode": raw.get("billing_mode"),
                        "estimated_cost_usd": self._float_or_none(
                            raw.get("estimated_cost_usd")
                        ),
                        "actual_cost_usd": self._float_or_none(
                            raw.get("actual_cost_usd")
                        ),
                        "cost_status": raw.get("cost_status"),
                        "cost_source": raw.get("cost_source"),
                        "pricing_version": raw.get("pricing_version"),
                        "title": raw.get("title"),
                        "api_call_count": self._int_or_default(raw.get("api_call_count")),
                        "archived": archived,
                    },
                )

                sanitized_messages: List[Dict[str, Any]] = []
                for msg in messages:
                    clean = dict(msg)
                    for key in (
                        "reasoning_details",
                        "codex_reasoning_items",
                        "codex_message_items",
                    ):
                        clean[key] = self._reasoning_json_value(clean.get(key))
                    sanitized_messages.append(clean)

                total_messages, total_tool_calls = self._insert_message_rows(
                    conn,
                    session_id,
                    sanitized_messages,
                )
                conn.execute(
                    "UPDATE sessions SET message_count = ?, tool_call_count = ? WHERE id = ?",
                    (total_messages, total_tool_calls, session_id),
                )

                parent_id = str(raw.get("parent_session_id") or "").strip()
                if parent_id:
                    parent_updates.append((session_id, parent_id))
                imported_ids.append(session_id)

            parent_by_child = dict(parent_updates)

            def _would_create_cycle(session_id: str, parent_id: str) -> bool:
                seen = {session_id}
                current = parent_id
                while current:
                    if current in seen:
                        return True
                    seen.add(current)
                    if current in parent_by_child:
                        current = parent_by_child[current]
                        continue
                    row = conn.execute(
                        "SELECT parent_session_id FROM sessions WHERE id = ? LIMIT 1",
                        (current,),
                    ).fetchone()
                    if row is None:
                        return False
                    current = row["parent_session_id"]
                return False

            for session_id, parent_id in parent_updates:
                parent_exists = conn.execute(
                    "SELECT 1 FROM sessions WHERE id = ? LIMIT 1",
                    (parent_id,),
                ).fetchone()
                if parent_exists and not _would_create_cycle(session_id, parent_id):
                    conn.execute(
                        "UPDATE sessions SET parent_session_id = ? WHERE id = ?",
                        (parent_id, session_id),
                    )
                else:
                    # Drop only the closing edge. Later entries can still attach
                    # to this now-root session, preserving the acyclic portion
                    # of a malformed imported lineage.
                    parent_by_child.pop(session_id, None)
                    detached += 1

            return {
                "ok": True,
                "imported": len(imported_ids),
                "skipped": len(skipped_ids),
                "detached": detached,
                "imported_ids": imported_ids,
                "skipped_ids": skipped_ids,
                "errors": [],
            }

        return self._execute_write(_do)

