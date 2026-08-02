"""AIAgent AgentPersistenceMixin — extracted from run_agent.py (restructure Phase 4).

Verbatim method moves; the class is assembled in opencodon.core.run_agent.
"""
#!/usr/bin/env python3
"""
AI Agent Runner with Tool Calling

This module provides a clean, standalone agent that can execute AI models
with tool calling capabilities. It handles the conversation loop, tool execution,
and response management.

Features:
- Automatic tool calling loop until completion
- Configurable model parameters
- Error handling and recovery
- Message history management
- Support for multiple model providers

Usage:
    from opencodon.core.run_agent import AIAgent
    
    agent = AIAgent(base_url="http://localhost:30000/v1", model="claude-opus-4-20250514")
    response = agent.run_conversation("Tell me about the latest Python updates")
"""
from opencodon.common.repo import REPO_ROOT

# IMPORTANT: opencodon_bootstrap must be the very first import — UTF-8 stdio
# on Windows.  No-op on POSIX.  See opencodon_bootstrap.py for full rationale.
try:
    import opencodon_bootstrap  # noqa: F401
except ModuleNotFoundError:
    # Graceful fallback when opencodon_bootstrap isn't registered in the venv
    # yet — happens during partial ``opencodon update`` where git-reset landed
    # new code but ``uv pip install -e .`` didn't finish.  Missing bootstrap
    # means UTF-8 stdio setup is skipped on Windows; POSIX is unaffected.
    pass

import asyncio
import base64
import copy
import hashlib
import json
import logging
logger = logging.getLogger(__name__)
import os
import re
import sys
import tempfile
import time
import threading
import uuid
from typing import List, Dict, Any, Optional, Callable
# NOTE: `from openai import OpenAI` is deliberately NOT at module top — the
# SDK pulls ~240 ms of imports. We expose `OpenAI` as a thin proxy object
# that imports the SDK on first call/isinstance check. This preserves:
#   (a) the single in-module `OpenAI(**client_kwargs)` call site at
#       _create_openai_client, and
#   (b) `patch("opencodon.core.run_agent.OpenAI", ...)` test patterns used by ~28 test files.
#
# NOTE: `fire` is ONLY used in the `__main__` block below (for running
# run_agent.py directly as a CLI) — it is NOT needed for library usage.
# It is imported there, not here, so that importing run_agent from a
# daemon thread (e.g. curator's forked review agent) never fails with
# ModuleNotFoundError on broken/partial installs where `fire` isn't present.
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from opencodon_constants import get_opencodon_home


def _launch_cwd_for_session(source: str) -> Optional[str]:
    """Working directory to stamp on a new session row, or None.

    Only local CLI sessions get a recorded cwd: the directory the process was
    launched from is meaningful for ``opencodon -c`` / ``--resume`` (relaunch
    where you left off). Gateway/cron/remote-backend sessions have no stable
    host cwd to restore, so they record nothing.

    ``TERMINAL_ENV`` is set by the CLI's config bridge (``load_cli_config``);
    a non-"local" backend (docker/ssh/modal/...) means the host cwd is
    irrelevant to the agent's tools, so we skip it there too.
    """
    if source != "cli":
        return None
    backend = (os.environ.get("TERMINAL_ENV") or "local").strip().lower()
    if backend and backend != "local":
        return None
    try:
        return os.getcwd()
    except OSError:
        # cwd was unlinked out from under us — nothing meaningful to record.
        return None


def _session_source_for_agent(platform: Optional[str]) -> str:
    try:
        from opencodon.frontends.gateway.session_context import get_session_env

        source = get_session_env("OPENCODON_SESSION_SOURCE", "")
    except Exception:
        source = os.environ.get("OPENCODON_SESSION_SOURCE", "")
    source = str(source or "").strip()
    if source:
        return source
    return platform or "cli"


# OpenAI lazy proxy + safe stdio + proxy URL helpers — see agent/process_bootstrap.py.
# `OpenAI` is re-exported here so `patch("opencodon.core.run_agent.OpenAI", ...)` in tests works.
# The other `# noqa: F401` re-exports below cover names accessed via
# `mock.patch("opencodon.core.run_agent.<X>")`, `from run_agent import <X>` in production
# siblings, or the `_ra().<X>` indirection in agent/system_prompt.py — none
# of which ruff's in-module usage scan can see.
from opencodon.core.process_bootstrap import (
    OpenAI,  # noqa: F401  # re-exported for tests that mock.patch("opencodon.core.run_agent.OpenAI")
    _SafeWriter,  # noqa: F401  # re-exported for tests that `from run_agent import _SafeWriter`
    _get_proxy_for_base_url,
)
from opencodon.core.iteration_budget import IterationBudget


from opencodon.config.env_loader import load_opencodon_dotenv
from opencodon.config.timeouts import (
    get_provider_request_timeout,
    get_provider_stale_timeout,
)

_opencodon_home = get_opencodon_home()
_project_env = REPO_ROOT / '.env'
_loaded_env_paths = load_opencodon_dotenv(opencodon_home=_opencodon_home, project_env=_project_env)
if _loaded_env_paths:
    for _env_path in _loaded_env_paths:
        logger.info("Loaded environment variables from %s", _env_path)
else:
    logger.info("No .env file found. Using system environment variables.")


# Import our tool system
from opencodon.tools.model_tools import (
    get_tool_definitions,  # noqa: F401  # re-exported for tests that mock.patch("opencodon.core.run_agent.get_tool_definitions")
    get_toolset_for_tool,
    handle_function_call,  # noqa: F401  # re-exported for tests that mock.patch("opencodon.core.run_agent.handle_function_call")
    check_toolset_requirements,  # noqa: F401  # re-exported for tests that mock.patch("opencodon.core.run_agent.check_toolset_requirements")
)
from opencodon.tools.terminal_tool import cleanup_vm, get_active_env
from opencodon.tools.interrupt import set_interrupt as _set_interrupt
from opencodon.tools.browser_tool import cleanup_browser


# Agent internals extracted to agent/ package for modularity
from opencodon.core.memory.memory_manager import sanitize_context
from opencodon.core.error_classifier import FailoverReason
from opencodon.core.redact import redact_sensitive_text
from opencodon.core.message_content import flatten_message_text
from opencodon.core.providers.model_metadata import (
    estimate_request_tokens_rough,  # noqa: F401  # re-exported for tests that mock.patch("opencodon.core.run_agent.estimate_request_tokens_rough")
    is_local_endpoint,
)
from opencodon.core.providers.usage_pricing import normalize_usage
# Re-exported for tests that monkeypatch these symbols on run_agent.
from opencodon.core.context.context_compressor import (  # noqa: F401
    COMPRESSED_SUMMARY_METADATA_KEY,
    ContextCompressor,
)
from opencodon.core.retry_utils import jittered_backoff  # noqa: F401
from opencodon.core.prompt.prompt_builder import (  # noqa: F401  # re-exported via _ra() / mock.patch("opencodon.core.run_agent.<name>") / from run_agent import <name>
    DEFAULT_AGENT_IDENTITY,
    build_skills_system_prompt,
    build_context_files_prompt,
    build_environment_hints,
    load_soul_md,
)
from opencodon.core.process_bootstrap import _get_proxy_from_env  # noqa: F401
from opencodon.core.message_sanitization import (  # noqa: F401
    _SURROGATE_RE,
    _sanitize_surrogates,
    _sanitize_structure_surrogates,
    _sanitize_messages_surrogates,
    _escape_invalid_chars_in_json_strings,
    _repair_tool_call_arguments,
    _strip_non_ascii,
    _sanitize_messages_non_ascii,
    _sanitize_tools_non_ascii,
    _strip_images_from_messages,
    _sanitize_structure_non_ascii,
)
from opencodon.core.providers.codex_responses_adapter import (
    _derive_responses_function_call_id as _codex_derive_responses_function_call_id,
    _deterministic_call_id as _codex_deterministic_call_id,
    _split_responses_tool_id as _codex_split_responses_tool_id,
    _summarize_user_message_for_log,  # also used by _sync_external_memory_for_turn (memory boundary)
)
from opencodon.core.tool_guardrails import (
    ToolGuardrailDecision,
    append_toolguard_guidance,
    toolguard_synthetic_result,
)
from opencodon.core.tool_result_classification import (
    FILE_MUTATING_TOOL_NAMES as _FILE_MUTATING_TOOLS,
    file_mutation_result_landed,
)
from opencodon.core.trajectory import (
    convert_scratchpad_to_think,
    save_trajectory as _save_trajectory_to_file,
)
from opencodon.core.tool_dispatch_helpers import (
    _should_parallelize_tool_batch,  # noqa: F401  # re-exported for tests that `from run_agent import _should_parallelize_tool_batch`
    _is_destructive_command,  # noqa: F401  # re-exported for tests that access `run_agent._is_destructive_command`
    _extract_parallel_scope_path,  # noqa: F401  # re-exported for tests that `from run_agent import _extract_parallel_scope_path`
    _paths_overlap,  # noqa: F401  # re-exported for tests that `from run_agent import _paths_overlap`
    _is_multimodal_tool_result,
    _multimodal_text_summary,
    _append_subdir_hint_to_multimodal,  # noqa: F401  # re-exported for tests that `from run_agent import _append_subdir_hint_to_multimodal`
    _extract_file_mutation_targets,
    _extract_landed_file_mutation_paths,
    _extract_error_preview,
    _trajectory_normalize_msg,  # noqa: F401  # re-exported for tests that `from run_agent import _trajectory_normalize_msg`
)
from utils import atomic_json_write, base_url_host_matches, base_url_hostname, env_float, is_truthy_value, model_forces_max_completion_tokens

class _AgentModuleProxy:
    """Late-binding accessor for run_agent module globals (patchable, cycle-safe)."""

    def __getattr__(self, name):
        from opencodon.core import run_agent
        return getattr(run_agent, name)

    def __setattr__(self, name, value):
        from opencodon.core import run_agent
        setattr(run_agent, name, value)


_ra = _AgentModuleProxy()


class AgentPersistenceMixin:
    def _get_session_db_for_recall(self):
        """Return a SessionDB for recall, lazily creating it if an entrypoint forgot.

        Most frontends pass ``session_db`` into ``AIAgent`` explicitly, but recall
        is important enough that a missing constructor argument should degrade by
        opening the default state DB instead of making the advertised
        ``session_search`` tool unusable.
        """
        # Persistence-isolated forks (background review) must not lazily open the
        # canonical state DB: doing so would re-arm _flush_messages_to_session_db
        # to write the fork's harness turn into the user's real session. Recall
        # degrades to None for them (they don't use session_search anyway).
        if getattr(self, "_persist_disabled", False):
            return None
        if self._session_db is not None:
            return self._session_db
        try:
            from opencodon.state import SessionDB

            self._session_db = SessionDB()
            return self._session_db
        except Exception as exc:
            _ra.logger.debug("SessionDB unavailable for recall", exc_info=True)
            return None

    def _ensure_db_session(self) -> None:
        """Create session DB row on first use. Disables _session_db on failure."""
        if getattr(self, "_persist_disabled", False):
            return
        if self._session_db_created or not self._session_db:
            return
        source = _ra._session_source_for_agent(self.platform)
        try:
            try:
                from opencodon.core.profiles import get_active_profile_name
                _profile_for_session = get_active_profile_name()
                if _profile_for_session == "default":
                    _profile_for_session = None
            except Exception:
                _profile_for_session = None
            self._session_db.create_session(
                session_id=self.session_id,
                source=source,
                model=self.model,
                model_config=self._session_init_model_config,
                system_prompt=self._cached_system_prompt,
                user_id=None,
                parent_session_id=self._parent_session_id,
                cwd=_ra._launch_cwd_for_session(source),
                profile_name=_profile_for_session,
            )
            self._session_db_created = True
        except Exception as e:
            # Transient failure (e.g. SQLite lock). Keep _session_db alive —
            # _session_db_created stays False so next run_conversation() retries.
            _ra.logger.warning(
                "Session DB creation failed (will retry next turn): %s", e
            )

    @staticmethod
    def _summarize_background_review_actions(
        review_messages: List[Dict],
        prior_snapshot: List[Dict],
        notification_mode: str = "on",
    ) -> List[str]:
        """Forwarder — see ``agent.background_review.summarize_background_review_actions``."""
        from opencodon.core.background_review import summarize_background_review_actions
        return summarize_background_review_actions(
            review_messages,
            prior_snapshot,
            notification_mode=notification_mode,
        )

    def _spawn_background_review(
        self,
        messages_snapshot: List[Dict],
        review_memory: bool = False,
        review_skills: bool = False,
    ) -> None:
        """Spawn the background memory/skill review thread.

        Thin wrapper — the heavy lifting lives in
        ``agent.background_review.spawn_background_review_thread`` which
        returns the thread target.  ``threading.Thread`` is constructed
        here so existing tests that patch ``run_agent.threading.Thread``
        keep working.
        """
        from opencodon.core.background_review import spawn_background_review_thread
        from opencodon.tools.thread_context import propagate_context_to_thread
        target, _prompt = spawn_background_review_thread(
            self,
            messages_snapshot,
            review_memory=review_memory,
            review_skills=review_skills,
        )
        # Carry the active profile into the review thread so MEMORY.md / skill
        # review writes land in the right profile (#54937).
        t = threading.Thread(
            target=propagate_context_to_thread(target), daemon=True, name="bg-review"
        )
        t.start()

    def _build_memory_write_metadata(
        self,
        *,
        write_origin: Optional[str] = None,
        execution_context: Optional[str] = None,
        task_id: Optional[str] = None,
        tool_call_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Forwarder — see ``agent.background_review.build_memory_write_metadata``."""
        from opencodon.core.background_review import build_memory_write_metadata
        return build_memory_write_metadata(
            self,
            write_origin=write_origin,
            execution_context=execution_context,
            task_id=task_id,
            tool_call_id=tool_call_id,
        )

    def _apply_persist_user_message_override(self, messages: List[Dict]) -> None:
        """Rewrite the current-turn user message before persistence/return.

        Some call paths need an API-only user-message variant without letting
        that synthetic text leak into persisted transcripts or resumed session
        history. When an override is configured for the active turn, mutate the
        in-memory messages list in place so both persistence and returned
        history stay clean.  A paired timestamp override preserves the platform
        event _ra.time as message metadata, rather than embedding it in content.
        """
        idx = getattr(self, "_persist_user_message_idx", None)
        override = getattr(self, "_persist_user_message_override", None)
        timestamp = getattr(self, "_persist_user_message_timestamp", None)
        if idx is None or (override is None and timestamp is None):
            return
        if 0 <= idx < len(messages):
            msg = messages[idx]
            if isinstance(msg, dict) and msg.get("role") == "user":
                # Text-only call paths may pass a synthetic API-facing prompt
                # and a cleaner transcript string separately. Before the API
                # call, a plain-text override must not replace native image/audio
                # blocks. A list override, however, is the original clean
                # multimodal payload (for example before a queued /model note)
                # and must replace the API-local list once the turn is final.
                if override is not None and (
                    not isinstance(msg.get("content"), list) or isinstance(override, list)
                ):
                    msg["content"] = override
                if timestamp is not None:
                    msg["timestamp"] = timestamp

    def _persist_session(self, messages: List[Dict], conversation_history: List[Dict] = None):
        """Save session state to both JSON log and SQLite on any exit path.

        Ensures conversations are never lost, even on errors or early returns.

        Trailing empty-response scaffolding is dropped from the live list in
        place (it is ephemeral junk the real transcript should shed). The
        persist user-message *override* is NOT applied here — it is resolved
        inside ``_flush_messages_to_session_db`` and written only to the DB row,
        never mutating the live message list used by the API call (#48677 is
        thus closed for every persist caller, not just this one).
        """
        # Scaffolding removal mutates the live list (desired — ephemeral
        # retry/failure sentinels must not survive into the real transcript).
        # Close and turn-start persistence can run on separate CLI threads; the
        # marker test-and-append below must be one critical section or both can
        # observe the same unmarked dict and write duplicate durable rows.
        from opencodon.core.agent_runtime_helpers import note_turn_persisted

        persist_lock = getattr(self, "_session_persist_lock", None)
        if persist_lock is None:
            self._drop_trailing_empty_response_scaffolding(messages)
            self._session_messages = messages
            self._save_session_log(messages)
            self._flush_messages_to_session_db(messages, conversation_history)
            note_turn_persisted(self)
            return

        with persist_lock:
            self._drop_trailing_empty_response_scaffolding(messages)
            self._session_messages = messages
            self._save_session_log(messages)
            self._flush_messages_to_session_db(messages, conversation_history)
            note_turn_persisted(self)

    def _drop_trailing_empty_response_scaffolding(self, messages: List[Dict]) -> None:
        """Remove private empty-response retry/failure scaffolding from transcript tails.

        Also rewinds past any trailing tool-result / assistant(tool_calls) pair
        that the failed iteration left hanging. Without this, the tail ends at
        a raw ``tool`` message and the next user turn lands as
        ``...tool, user, user`` — a protocol-invalid sequence that most
        providers silently reject (returns empty content), causing the
        empty-retry loop to fire forever. (issue number to be backfilled once filed)
        """
        # Pass 1: strip the flagged scaffolding messages themselves.
        dropped_scaffolding = False
        while (
            messages
            and isinstance(messages[-1], dict)
            and (
                messages[-1].get("_empty_recovery_synthetic")
                or messages[-1].get("_empty_terminal_sentinel")
            )
        ):
            messages.pop()
            dropped_scaffolding = True

        # Pass 2: if we stripped scaffolding, rewind through any trailing
        # tool-result messages plus the assistant(tool_calls) message that
        # produced them. This preserves role alternation so the next user
        # message follows a user or assistant message, not an orphan tool
        # result. Only runs when scaffolding was actually present — normal
        # conversation tails (real tool loops mid-progress) are untouched.
        if not dropped_scaffolding:
            return

        # Drop any trailing tool-result messages
        while (
            messages
            and isinstance(messages[-1], dict)
            and messages[-1].get("role") == "tool"
        ):
            messages.pop()

        # Drop the assistant message that issued the tool calls, if the tail
        # now ends in an assistant-with-tool_calls (the pair that owned the
        # just-popped tool results). Without this, the tail is
        # ``assistant(tool_calls=...)`` with no tool answers, which some
        # providers also reject.
        if (
            messages
            and isinstance(messages[-1], dict)
            and messages[-1].get("role") == "assistant"
            and messages[-1].get("tool_calls")
        ):
            messages.pop()

    def _repair_message_sequence(self, messages: List[Dict]) -> int:
        """Forwarder — see ``agent.agent_runtime_helpers.repair_message_sequence``."""
        from opencodon.core.agent_runtime_helpers import repair_message_sequence
        return repair_message_sequence(self, messages)

    def _flush_messages_to_session_db(
        self,
        messages: List[Dict],
        conversation_history: Optional[List[Dict]] = None,
    ):
        """Serialize direct and turn-boundary session flushes per agent."""
        persist_lock = getattr(self, "_session_persist_lock", None)
        if persist_lock is None:
            return self._flush_messages_to_session_db_unlocked(messages, conversation_history)
        with persist_lock:
            return self._flush_messages_to_session_db_unlocked(messages, conversation_history)

    def _flush_messages_to_session_db_unlocked(
        self,
        messages: List[Dict],
        conversation_history: Optional[List[Dict]] = None,
    ):
        """Persist any un-flushed messages to the SQLite session store.

        Deduplicates via an intrinsic ``_ra._DB_PERSISTED_MARKER`` stamped on each
        written message dict, so repeated calls (from multiple exit paths) only
        write truly new messages — preventing the duplicate-write bug (#860)
        without relying on positional slices that can drift after
        message-sequence repair, and without a retained ``id(msg)`` set that
        CPython could alias onto a freed-then-reused address (#50372). The
        ``_flushed_db_message_ids`` attribute is now only a one-shot seed
        (translated to markers, then cleared each flush), not a persisted set.

        Note: the marker is stamped on the live/shared conversation dict, which
        correctly makes re-persistence idempotent across turns. No code path
        edits a persisted message's content/role in place expecting a re-write
        (in-place compaction resets the seed and re-diffs by identity).
        """
        # Persistence-isolated agents (e.g. the background skill/memory review
        # fork) must NEVER write into the canonical session store. The fork
        # shares the parent's session_id for prompt-cache warmth, so any write
        # here would land its harness turn ("Review the conversation above and
        # update the skill library…") inside the user's real session history,
        # where the next live turn re-reads it as an instruction and the agent
        # "becomes" the curator. Hard-stop before any DB touch.
        if getattr(self, "_persist_disabled", False):
            return
        if not self._session_db:
            return
        # Persist user-message override (#48677 chokepoint): historically this
        # mutated the live `messages` list in place, which — on the early
        # crash-resilience persist that runs BEFORE the API call is built —
        # stripped observed group-chat context off the live user message and
        # silently dropped it. Instead, resolve the override here and apply it
        # ONLY to the value written to the DB (see the write loop below); the
        # live dict is never mutated, so every caller (early persist, mid-loop
        # flush, /resume, /branch) is protected uniformly. Timestamp override is
        # metadata and is likewise applied only to the written row.
        _ov_idx = getattr(self, "_persist_user_message_idx", None)
        _ov_content = getattr(self, "_persist_user_message_override", None)
        _ov_timestamp = getattr(self, "_persist_user_message_timestamp", None)
        try:
            # Retry row creation if the earlier attempt failed transiently.
            if not self._session_db_created:
                self._ensure_db_session()
            # Positional flushing used to slice at
            # max(len(conversation_history), _last_flushed_db_idx). That
            # assumes the live `messages` list is the original history plus a
            # new tail. repair_message_sequence can shrink/merge the history
            # copy before the final flush, making len(conversation_history)
            # larger than len(messages); the slice is then empty and delivered
            # assistant responses never reach state.db (#46053).
            #
            # Track persistence with an intrinsic per-message marker rather than
            # id(msg). `messages` is a shallow copy of `conversation_history`, so
            # history dicts are skipped by identity, and new dicts appended
            # during this turn are written once even if repair compacts the list
            # around them. Unlike an id()-keyed set, a marker bound to the dict
            # cannot be aliased onto a freed-then-reused address, so a real turn
            # can never be silently skipped (see _ra._DB_PERSISTED_MARKER).
            #
            # `self._flushed_db_message_ids` is still honoured as a *one-shot*
            # seed: external callers (gateway shutdown, tests) populate it with
            # {id(m) for m in already_persisted} immediately before the flush,
            # while those objects are alive — so the ids are valid at that
            # instant. We translate the seed into durable markers and then clear
            # the set, so stale ids can never accumulate across turns and alias a
            # future message.
            current_session_id = getattr(self, "session_id", None)
            flushed_session_id = getattr(self, "_flushed_db_message_session_id", None)
            if flushed_session_id != current_session_id or self._last_flushed_db_idx == 0:
                seed_ids = set()
            else:
                seed_ids = getattr(self, "_flushed_db_message_ids", None)
                if not isinstance(seed_ids, set):
                    seed_ids = set()
            self._flushed_db_message_session_id = current_session_id
            history_ids = {
                id(item) for item in (conversation_history or [])
                if isinstance(item, dict)
            }

            for _msg_idx, msg in enumerate(messages):
                if not isinstance(msg, dict):
                    continue
                # Never write ephemeral recovery scaffolding to the session
                # store. The flush is append-only (it only advances
                # _last_flushed_db_idx via identity tracking), so a synthetic
                # message committed by a mid-turn persist cannot be un-written
                # when the end-of-turn drop removes it from the in-memory list —
                # the resumed transcript would then replay synthetic
                # "(empty)"/nudge/thinking-prefill turns as if they were genuine
                # context. Skip regardless of position: an answered nudge leaves
                # the synthetic pair buried mid-list, not just at the tail.
                if _ra._is_ephemeral_scaffolding(msg):
                    continue
                if msg.get(_ra._DB_PERSISTED_MARKER):
                    continue
                # Already-durable messages: either carried over from the loaded
                # history copy, or seeded by a caller. Stamp them so future
                # flushes skip them without consulting any id() set again.
                if id(msg) in history_ids or id(msg) in seed_ids:
                    msg[_ra._DB_PERSISTED_MARKER] = True
                    continue
                role = msg.get("role", "unknown")
                content = msg.get("content")
                # api_content sidecar: the exact bytes sent to the API when
                # they differ from the clean content (stamped by the turn
                # prologue for prefetch/plugin injections). Written verbatim
                # so replay can reproduce the sent prefix byte-for-byte.
                _row_api_content = msg.get("api_content")
                if not isinstance(_row_api_content, str):
                    _row_api_content = None
                _row_timestamp = msg.get("timestamp")
                # Apply the persist override to THIS row's written values only
                # (never to the live dict). A multimodal override is a complete
                # clean replacement for an API-local noted payload. Preserve the
                # historical text-only guard for a list payload, though: a plain
                # text override must not erase its image/audio transcript summary.
                # The close safety-net may flush a shortened snapshot while
                # turn setup still owns its staged CLI dict. In that shape the
                # normal turn index refers to the full history, not this list;
                # preserve the API-local override by recognizing the same dict.
                pending_cli_message = getattr(self, "_pending_cli_user_message", None)
                is_current_turn_user = (
                    _ov_idx == _msg_idx or msg is pending_cli_message
                )
                if is_current_turn_user and msg.get("role") == "user":
                    # Preflight compaction can re-anchor the override index at
                    # a message whose content was MERGED with the compaction
                    # summary (merge-summary-into-tail). Overwriting that with
                    # the clean gateway text would silently drop the summary
                    # from the durable transcript. The wire is already
                    # consistent — the merge popped the sidecar and the merged
                    # content is what gets sent — so keep it.
                    if (
                        _ov_content is not None
                        and (not isinstance(content, list) or isinstance(_ov_content, list))
                        and not msg.get(COMPRESSED_SUMMARY_METADATA_KEY)
                    ):
                        # The live content is what the API call sends; the
                        # override is the cleaned transcript value. If they
                        # differ and no injection already stamped the sidecar,
                        # keep the sent bytes in api_content so replay matches
                        # the wire (#48677 divergence, closed for the cache
                        # prefix too).
                        if (
                            _row_api_content is None
                            and isinstance(content, str)
                            and content != _ov_content
                        ):
                            _row_api_content = content
                        content = _ov_content
                    if _ov_timestamp is not None:
                        _row_timestamp = _ov_timestamp
                # Store the sidecar only when it actually differs.
                if _row_api_content == content:
                    _row_api_content = None
                # Load-_ra.time sanitize divergence: get_messages_as_conversation
                # replays user/assistant rows through
                # ``sanitize_context(content).strip()``, so content that
                # sanitize would rewrite (echoed/pasted <memory-context>
                # fences or system notes) replays different bytes after a
                # session reload even though THIS turn sent it verbatim.
                # Capture the sent bytes in the sidecar so a reloaded session
                # replays what was actually on the wire. Compared in wire form
                # (both sides .strip()-ed — the api_messages build strips
                # every outgoing content string) so plain surrounding
                # whitespace doesn't grow redundant sidecars.
                if (
                    _row_api_content is None
                    and role in ("user", "assistant")
                    and isinstance(content, str)
                    and content
                    and sanitize_context(content).strip() != content.strip()
                ):
                    _row_api_content = content
                # Persist multimodal tool results as their text summary only —
                # base64 images would bloat the session DB and aren't useful
                # for cross-session replay.
                if _is_multimodal_tool_result(content):
                    content = _multimodal_text_summary(content)
                elif isinstance(content, list):
                    # List of OpenAI-style content parts: strip images, keep text.
                    _txt = []
                    for p in content:
                        if isinstance(p, dict) and p.get("type") == "text":
                            _txt.append(str(p.get("text", "")))
                        elif isinstance(p, dict) and p.get("type") in {"image", "image_url", "input_image"}:
                            _txt.append("[screenshot]")
                    content = "\n".join(_txt) if _txt else None
                tool_calls_data = None
                if hasattr(msg, "tool_calls") and isinstance(msg.tool_calls, list) and msg.tool_calls:
                    tool_calls_data = [
                        {"name": tc.function.name, "arguments": tc.function.arguments}
                        for tc in msg.tool_calls
                    ]
                elif isinstance(msg.get("tool_calls"), list):
                    tool_calls_data = msg["tool_calls"]
                self._session_db.append_message(
                    session_id=self.session_id,
                    role=role,
                    content=content,
                    tool_name=msg.get("tool_name"),
                    tool_calls=tool_calls_data,
                    tool_call_id=msg.get("tool_call_id"),
                    finish_reason=msg.get("finish_reason"),
                    reasoning=msg.get("reasoning") if role == "assistant" else None,
                    reasoning_content=msg.get("reasoning_content") if role == "assistant" else None,
                    reasoning_details=msg.get("reasoning_details") if role == "assistant" else None,
                    codex_reasoning_items=msg.get("codex_reasoning_items") if role == "assistant" else None,
                    codex_message_items=msg.get("codex_message_items") if role == "assistant" else None,
                    timestamp=_row_timestamp,
                    api_content=_row_api_content,
                )
                msg[_ra._DB_PERSISTED_MARKER] = True
            # The intrinsic markers are now the sole source of truth. Reset the
            # one-shot seed so no id() outlives this flush to alias a message
            # allocated next turn at a recycled address.
            self._flushed_db_message_ids = set()
            self._last_flushed_db_idx = len(messages)
        except Exception as e:
            _ra.logger.warning("Session DB append_message failed: %s", e)

    def _get_messages_up_to_last_assistant(self, messages: List[Dict]) -> List[Dict]:
        """
        Get messages up to (but not including) the last assistant turn.
        
        This is used when we need to "roll back" to the last successful point
        in the conversation, typically when the final assistant message is
        incomplete or malformed.
        
        Args:
            messages: Full message list
            
        Returns:
            Messages up to the last complete assistant turn (ending with user/tool message)
        """
        if not messages:
            return []
        
        # Find the index of the last assistant message
        last_assistant_idx = None
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "assistant":
                last_assistant_idx = i
                break
        
        if last_assistant_idx is None:
            # No assistant message found, return all messages
            return messages.copy()
        
        # Return everything up to (not including) the last assistant message
        return messages[:last_assistant_idx]

    def _convert_to_trajectory_format(self, messages: List[Dict[str, Any]], user_query: str, completed: bool) -> List[Dict[str, Any]]:
        """Forwarder — see ``agent.agent_runtime_helpers.convert_to_trajectory_format``."""
        from opencodon.core.agent_runtime_helpers import convert_to_trajectory_format
        return convert_to_trajectory_format(self, messages, user_query, completed)

    def _save_trajectory(self, messages: List[Dict[str, Any]], user_query: str, completed: bool):
        """
        Save conversation trajectory to JSONL file.
        
        Args:
            messages (List[Dict]): Complete message history
            user_query (str): Original user query
            completed (bool): Whether the conversation completed successfully
        """
        if not self.save_trajectories:
            return
        
        trajectory = self._convert_to_trajectory_format(messages, user_query, completed)
        _save_trajectory_to_file(trajectory, self.model, completed)

    @staticmethod
    def _clean_session_content(content: str) -> str:
        """Convert REASONING_SCRATCHPAD to think tags and clean up whitespace."""
        if not content:
            return content
        content = convert_scratchpad_to_think(content)
        content = re.sub(r'\n+(<think>)', r'\n\1', content)
        content = re.sub(r'(</think>)\n+', r'\1\n', content)
        return content.strip()

    @staticmethod
    def _redact_message_content(content):
        """Apply secret redaction to message content (str or list-of-parts).

        Handles both plain-string content and the OpenAI/Anthropic multimodal
        shape where ``content`` is a list of ``{"type": "text", "text": ...}``
        / ``{"type": "image_url", ...}`` / ``{"type": "input_text", "content": ...}``
        parts. Image / binary parts are left untouched; only text fields are
        passed through ``redact_sensitive_text``.

        Respects ``OPENCODON_REDACT_SECRETS`` via ``redact_sensitive_text`` —
        when disabled the helper is effectively a no-op.
        """
        if content is None:
            return content
        if isinstance(content, str):
            return redact_sensitive_text(content)
        if isinstance(content, list):
            redacted = []
            for part in content:
                if isinstance(part, dict):
                    part = dict(part)
                    if isinstance(part.get("text"), str):
                        part["text"] = redact_sensitive_text(part["text"])
                    if isinstance(part.get("content"), str):
                        part["content"] = redact_sensitive_text(part["content"])
                redacted.append(part)
            return redacted
        return content

    def _save_session_log(self, messages: List[Dict[str, Any]] = None):
        """Optional per-session JSON snapshot writer.

        Gated by ``sessions.write_json_snapshots`` (default False).  state.db
        is the canonical message store; this writer exists only for users
        whose external tooling consumes ``~/.opencodon/sessions/session_{sid}.json``
        directly.  When the flag is off this is a fast no-op.

        When enabled, rewrites the snapshot after every persistence point with
        the full message list (assistant content normalized via
        ``_clean_session_content`` to convert REASONING_SCRATCHPAD to think
        tags).  The truncation guard ("don't overwrite a larger log with
        fewer messages") is preserved so resume + branch don't clobber a
        fuller existing snapshot.
        """
        if not getattr(self, "_session_json_enabled", False):
            return
        messages = messages or self._session_messages
        if not messages:
            return

        # Re-derive the target path each call so /branch and /compress
        # session-id changes land in the right file without any re-point
        # bookkeeping at the call sites.  Sanitize the session ID into a
        # single traversal-free path segment — session IDs can come from
        # untrusted input (X-Hermes-Session-Id header) and must not escape
        # the sessions directory.
        try:
            safe_sid = _ra._safe_session_filename_component(self.session_id)
            log_file = self.logs_dir / f"session_{safe_sid}.json"
        except Exception:
            return

        try:
            cleaned = []
            for msg in messages:
                # Mirror the SQLite flush: ephemeral recovery scaffolding is
                # internal retry state, never durable transcript content.
                if _ra._is_ephemeral_scaffolding(msg):
                    continue
                if msg.get("role") == "assistant" and msg.get("content"):
                    msg = dict(msg)
                    msg["content"] = self._clean_session_content(msg["content"])
                # Defence-in-depth: redact credentials from every message
                # content before persistence. Catches PATs / API keys / Bearer
                # tokens that may have leaked into assistant responses, tool
                # output, or user paste. Respects OPENCODON_REDACT_SECRETS via
                # redact_sensitive_text — no-op when disabled. (#19798, #19845)
                if "content" in msg:
                    msg = dict(msg)
                    msg["content"] = self._redact_message_content(msg.get("content"))
                cleaned.append(msg)

            # Guard: never overwrite a larger session log with fewer messages.
            # Protects against data loss when a resumed agent starts with
            # partial history and would otherwise clobber the full JSON log.
            if log_file.exists():
                try:
                    existing = json.loads(log_file.read_text(encoding="utf-8"))
                    existing_count = existing.get("message_count", len(existing.get("messages", [])))
                    if existing_count > len(cleaned):
                        logging.debug(
                            "Skipping session log overwrite: existing has %d messages, current has %d",
                            existing_count, len(cleaned),
                        )
                        return
                except Exception:
                    pass  # corrupted existing file — allow the overwrite

            entry = {
                "session_id": self.session_id,
                "model": self.model,
                "base_url": self.base_url,
                "platform": self.platform,
                "session_start": self.session_start.isoformat(),
                "last_updated": datetime.now().isoformat(),
                "system_prompt": redact_sensitive_text(self._cached_system_prompt or ""),
                "tools": self.tools or [],
                "message_count": len(cleaned),
                "messages": cleaned,
            }

            atomic_json_write(
                log_file,
                entry,
                indent=2,
                default=str,
            )

        except Exception as e:
            if self.verbose_logging:
                logging.warning(f"Failed to save session log: {e}")

    def _conversation_root_id(self) -> Optional[str]:
        """Resolve the stable conversation id for Portal usage attribution.

        Returns the session-lineage ROOT id rather than the current segment
        id, so one user-facing conversation keeps a single ``conversation=``
        tag across context-compression rotation (`/new` starts a genuinely
        new lineage). Delegate subagents resolve through their
        ``_parent_session_id`` so an entire delegation tree tags as the
        parent conversation.

        Best-effort: falls back to the raw session id when the session DB
        is unavailable or the lineage walk fails.
        """
        sid = getattr(self, "session_id", None)
        if not sid:
            return None
        # Subagents may not have a DB row yet on their first turn; walking
        # from the parent id still lands on the right root.
        start = getattr(self, "_parent_session_id", None) or sid
        db = getattr(self, "_session_db", None)
        if db is not None:
            try:
                root = db.get_conversation_root(start)
                if root:
                    return root
            except Exception:
                _ra.logger.debug("Conversation root lineage walk failed", exc_info=True)
        return start

