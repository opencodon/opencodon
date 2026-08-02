"""AIAgent AgentStreamingMixin — extracted from run_agent.py (restructure Phase 4).

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
_project_env = Path(__file__).resolve().parents[3] / '.env'
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
from opencodon.core.memory_manager import sanitize_context
from opencodon.core.error_classifier import FailoverReason
from opencodon.core.redact import redact_sensitive_text
from opencodon.core.message_content import flatten_message_text
from opencodon.core.model_metadata import (
    estimate_request_tokens_rough,  # noqa: F401  # re-exported for tests that mock.patch("opencodon.core.run_agent.estimate_request_tokens_rough")
    is_local_endpoint,
)
from opencodon.core.usage_pricing import normalize_usage
# Re-exported for tests that monkeypatch these symbols on run_agent.
from opencodon.core.context_compressor import (  # noqa: F401
    COMPRESSED_SUMMARY_METADATA_KEY,
    ContextCompressor,
)
from opencodon.core.retry_utils import jittered_backoff  # noqa: F401
from opencodon.core.prompt_builder import (  # noqa: F401  # re-exported via _ra() / mock.patch("opencodon.core.run_agent.<name>") / from run_agent import <name>
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
from opencodon.core.codex_responses_adapter import (
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


class AgentStreamingMixin:
    def _disable_codex_reasoning_replay(
        self,
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, int]:
        """Disable Responses encrypted reasoning replay and strip cached state.

        Called from the conversation_loop retry path when the provider
        rejects a replayed ``codex_reasoning_items`` blob with HTTP 400
        ``invalid_encrypted_content``.  Sets ``self._codex_reasoning_replay_enabled``
        to ``False`` (consumed by ``codex_responses_adapter._chat_messages_to_responses_input``
        and ``transports/codex.py`` to drop ``reasoning.encrypted_content``
        from subsequent requests) and pops ``codex_reasoning_items`` from
        every assistant message in ``messages`` so they cannot be replayed
        again later in the session.

        Returns a small stats dict ``{"messages": int, "items": int}``
        counting what was stripped — purely for diagnostic logging.
        """
        stripped_messages = 0
        stripped_items = 0
        target_messages = messages if isinstance(messages, list) else []

        for msg in target_messages:
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            items = msg.pop("codex_reasoning_items", None)
            if isinstance(items, list) and items:
                stripped_messages += 1
                stripped_items += len(items)

        self._codex_reasoning_replay_enabled = False
        return {"messages": stripped_messages, "items": stripped_items}

    def _reset_stream_delivery_tracking(self) -> None:
        """Reset tracking for text delivered during the current model response."""
        # Flush any benign partial-tag tail held by the think scrubber
        # first (#17924): an innocent '<' at the end of the stream that
        # turned out not to be a tag prefix should reach the UI.  Then
        # flush the context scrubber.  Order matters — the think
        # scrubber's output feeds into the context scrubber's state.
        think_scrubber = getattr(self, "_stream_think_scrubber", None)
        if think_scrubber is not None:
            think_tail = think_scrubber.flush()
            if think_tail:
                # Route the tail through the context scrubber too so a
                # memory-context span straddling the final boundary is
                # still caught.
                ctx_scrubber = getattr(self, "_stream_context_scrubber", None)
                if ctx_scrubber is not None:
                    think_tail = ctx_scrubber.feed(think_tail)
                if think_tail:
                    callbacks = [cb for cb in (self.stream_delta_callback, self._stream_callback) if cb is not None]
                    for cb in callbacks:
                        try:
                            cb(think_tail)
                        except Exception:
                            pass
                    self._record_streamed_assistant_text(think_tail)
        # Flush any benign partial-tag tail held by the context scrubber so it
        # reaches the UI before we clear state for the next model call.  If
        # the scrubber is mid-span, flush() drops the orphaned content.
        scrubber = getattr(self, "_stream_context_scrubber", None)
        if scrubber is not None:
            tail = scrubber.flush()
            if tail:
                callbacks = [cb for cb in (self.stream_delta_callback, self._stream_callback) if cb is not None]
                for cb in callbacks:
                    try:
                        cb(tail)
                    except Exception:
                        pass
                self._record_streamed_assistant_text(tail)
        self._current_streamed_assistant_text = ""
        self._current_streamed_reasoning_text = ""

    def _record_streamed_assistant_text(self, text: str) -> None:
        """Accumulate visible assistant text emitted through stream callbacks."""
        # Single-writer guard (#65991): a superseded stream must not pollute the
        # turn's accumulated text (which also feeds the interim-visible-text
        # de-dup comparison), even when a caller reaches this directly (the
        # tool-suppressed content path) rather than through _fire_stream_delta.
        if self._stream_writer_superseded():
            return
        if isinstance(text, str) and text:
            self._current_streamed_assistant_text = (
                getattr(self, "_current_streamed_assistant_text", "") + text
            )

    @staticmethod
    def _normalize_interim_visible_text(text: str) -> str:
        if not isinstance(text, str):
            return ""
        return re.sub(r"\s+", " ", text).strip()

    def _interim_content_was_streamed(self, content: str) -> bool:
        visible_content = self._normalize_interim_visible_text(
            self._strip_think_blocks(content or "")
        )
        if not visible_content:
            return False
        streamed = self._normalize_interim_visible_text(
            self._strip_think_blocks(getattr(self, "_current_streamed_assistant_text", "") or "")
        )
        # Prefix match (not exact equality): the final response may be the
        # streamed text plus a trailing delta, or the stream may have been
        # partial when the verify nudge fired.  In both cases the streamed
        # content is a prefix of the final — that's enough to mark it
        # previewed (fails safe to a benign duplicate, never loses text).
        # The reverse direction (streamed longer than final) is NOT matched:
        # that could suppress a needed resend in the gateway path where
        # already_streamed=True calls on_segment_break() instead of
        # on_commentary() (#65919 review).
        return bool(streamed) and visible_content.startswith(streamed)

    def _extract_codex_interim_visible_parts(
        self,
        assistant_msg: Dict[str, Any],
    ) -> List[str]:
        """Extract visible Codex commentary as one string per message item.

        Codex Responses can keep user-facing mid-turn narration as structured
        ``phase=commentary`` message items while final answer text remains in
        assistant ``content``.  Non-streaming gateway surfaces need that
        commentary through the interim assistant callback before tool calls run.
        ``phase=analysis`` remains hidden because it is provider scratchpad.
        """
        if not getattr(self, "show_commentary", True):
            # display.show_commentary=false — commentary stays on the
            # reasoning channel (pre-commentary-channel behavior).
            return []
        items = assistant_msg.get("codex_message_items")
        if not isinstance(items, list):
            return []

        messages: List[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "message":
                continue
            phase = item.get("phase")
            if not isinstance(phase, str) or phase.strip().lower() != "commentary":
                continue
            content_parts = item.get("content")
            if not isinstance(content_parts, list):
                continue
            item_parts: List[str] = []
            for part in content_parts:
                if not isinstance(part, dict):
                    continue
                if part.get("type") != "output_text":
                    continue
                text = part.get("text")
                if isinstance(text, str) and text.strip():
                    item_parts.append(text)
            visible = "".join(item_parts).strip()
            if visible:
                visible = self._strip_think_blocks(visible).strip()
                visible = redact_sensitive_text(visible)
            if visible:
                messages.append(visible)
        return messages

    def _extract_codex_interim_visible_text(self, assistant_msg: Dict[str, Any]) -> str:
        """Extract all visible Codex commentary for comparison/fallback."""
        return "\n\n".join(
            self._extract_codex_interim_visible_parts(assistant_msg)
        ).strip()

    def _interim_assistant_visible_text(self, assistant_msg: Dict[str, Any]) -> str:
        """Return the exact assistant text eligible for interim delivery.

        Prefer structured Codex commentary over top-level content. A Codex
        response can contain both commentary and a partial/final-answer message
        while tools are still pending; treating top-level content as progress
        in that shape leaks the answer before the tool call runs.

        Content may be a string or a structured parts list (e.g. after vision
        turns or context compaction), so flatten it before stripping reasoning.
        """
        visible = self._extract_codex_interim_visible_text(assistant_msg)
        if visible:
            return visible
        content = assistant_msg.get("content")
        return self._strip_think_blocks(flatten_message_text(content)).strip()

    def _interim_text_was_delivered(self, text: str) -> bool:
        normalized = self._normalize_interim_visible_text(text)
        if not normalized:
            return False
        return normalized in getattr(self, "_delivered_interim_texts", set())

    def _record_delivered_interim_text(self, text: str) -> None:
        normalized = self._normalize_interim_visible_text(text)
        if normalized:
            delivered = getattr(self, "_delivered_interim_texts", None)
            if not isinstance(delivered, set):
                delivered = set()
                self._delivered_interim_texts = delivered
            delivered.add(normalized)

    def _fire_streamed_codex_commentary(self, text: str) -> None:
        """Deliver a completed live Codex commentary message immediately."""
        cb = getattr(self, "interim_assistant_callback", None)
        if cb is None or not isinstance(text, str):
            return
        visible = self._strip_think_blocks(text).strip()
        if visible:
            visible = redact_sensitive_text(visible)
        if not visible or visible == "(empty)" or self._interim_text_was_delivered(visible):
            return
        try:
            cb(visible, already_streamed=False)
            self._record_delivered_interim_text(visible)
        except Exception:
            _ra.logger.debug("interim_assistant_callback error", exc_info=True)

    def _emit_interim_assistant_message(
        self, assistant_msg: Dict[str, Any]
    ) -> None:
        """Surface a real mid-turn assistant commentary message to the UI layer.

        Does NOT set ``_response_was_previewed`` — that flag means "the final
        response was already shown to the user," but this helper is called for
        ordinary tool-call narration, intermediate acknowledgements, and
        verification candidates alike. Setting it here would cause the CLI to
        suppress a *different* final summary (e.g. from ``_handle_max_iterations``)
        when the only streamed text was unrelated mid-turn commentary. (#65919
        review: response-loss blocker)
        """
        cb = getattr(self, "interim_assistant_callback", None)
        if cb is None or not isinstance(assistant_msg, dict):
            return
        commentary_parts = self._extract_codex_interim_visible_parts(assistant_msg)
        undelivered_parts: List[str] = []
        pending_keys: set[str] = set()
        for part in commentary_parts:
            key = self._normalize_interim_visible_text(part)
            if (
                not key
                or key in pending_keys
                or self._interim_text_was_delivered(part)
            ):
                continue
            pending_keys.add(key)
            undelivered_parts.append(part)
        visible = (
            "\n\n".join(undelivered_parts).strip()
            if commentary_parts
            else self._interim_assistant_visible_text(assistant_msg)
        )
        if (
            not visible
            or visible == "(empty)"
            or self._interim_text_was_delivered(visible)
        ):
            return
        already_streamed = self._interim_content_was_streamed(visible)
        try:
            cb(visible, already_streamed=already_streamed)
            if undelivered_parts:
                for part in undelivered_parts:
                    self._record_delivered_interim_text(part)
            else:
                self._record_delivered_interim_text(visible)
        except Exception:
            _ra.logger.debug("interim_assistant_callback error", exc_info=True)

    def _ensure_stream_writer_state(self) -> None:
        """Lazily create the single-writer guard fields (#65991).

        The fields are normally set in ``agent_init``, but agents constructed
        via ``AIAgent.__new__`` (test doubles, legacy/partially-initialized
        instances) skip that path. Claiming/checking the writer must not crash
        those agents, so initialize the fields on first use.
        """
        if getattr(self, "_stream_writer_lock", None) is None:
            self._stream_writer_lock = threading.Lock()
        if not hasattr(self, "_stream_writer_token"):
            self._stream_writer_token = 0
        if getattr(self, "_stream_writer_tls", None) is None:
            self._stream_writer_tls = threading.local()
        if not hasattr(self, "_stream_writer_dropped"):
            self._stream_writer_dropped = 0

    def _claim_stream_writer(self) -> int:
        """Claim exclusive ownership of the streaming delta sink for the calling
        stream attempt and return its monotonic writer token (#65991).

        Every streaming attempt (each provider path, each retry) calls this
        right before it begins consuming its stream. Claiming bumps the shared
        token, so any earlier attempt still alive on another thread is
        immediately superseded: its cached token no longer matches and the sink
        fences its late chunks out. The token is stored per-thread, so a thread
        that never claimed (a non-streaming caller) is never treated as a
        writer and can never be fenced.
        """
        self._ensure_stream_writer_state()
        with self._stream_writer_lock:
            self._stream_writer_token += 1
            token = self._stream_writer_token
        self._stream_writer_tls.token = token
        return token

    def _stream_writer_is_current(self, token: int) -> bool:
        """True when ``token`` (from a prior _claim_stream_writer) is still the
        active writer — i.e. no newer stream attempt has claimed the sink since
        (#65991). Lets a stream loop bail out the instant it is superseded."""
        return token == getattr(self, "_stream_writer_token", token)

    def _stream_writer_superseded(self) -> bool:
        """True when the calling thread claimed the delta sink but a newer
        stream attempt has since claimed it — i.e. this thread is a stale
        writer whose chunks must be dropped (#65991).

        A thread that never claimed (``token is None``) is not a writer and is
        never reported as superseded, so non-streaming delta callers are
        unaffected.
        """
        tls = getattr(self, "_stream_writer_tls", None)
        token = getattr(tls, "token", None) if tls is not None else None
        if token is None:
            return False
        return token != getattr(self, "_stream_writer_token", token)

    def _note_dropped_stream_writer(self, where: str) -> None:
        """Record + log that a superseded stream's delta was discarded."""
        try:
            self._stream_writer_dropped = int(getattr(self, "_stream_writer_dropped", 0)) + 1
        except Exception:
            self._stream_writer_dropped = 1
        # Log sparsely (first drop, then powers of two) so a chatty superseded
        # stream can't flood the log, but a real provider problem is still
        # visible. A silent discard would hide genuine failures.
        _n = self._stream_writer_dropped
        if _n == 1 or (_n & (_n - 1)) == 0:
            _ra.logger.warning(
                "Dropped delta from a superseded stream writer at %s "
                "(discarded=%d this turn) — a stale stream tried to write into "
                "the turn after a retry superseded it.",
                where, _n,
            )

    def _fire_stream_delta(self, text: str) -> None:
        """Fire all registered stream delta callbacks (display + TTS)."""
        # Single-writer guard (#65991): a superseded stream must not interleave
        # its tokens into the turn alongside the retry that replaced it.
        if self._stream_writer_superseded():
            self._note_dropped_stream_writer("_fire_stream_delta")
            return
        # If a tool iteration set the break flag, prepend a single paragraph
        # break before the first real text delta.  This prevents the original
        # problem (text concatenation across tool boundaries) without stacking
        # blank lines when multiple tool iterations run back-to-back.
        if getattr(self, "_stream_needs_break", False) and text and text.strip():
            self._stream_needs_break = False
            text = "\n\n" + text
            prepended_break = True
        else:
            prepended_break = False
        if isinstance(text, str):
            # Suppress reasoning/thinking blocks via the stateful
            # scrubber (#17924).  Earlier versions ran _strip_think_blocks
            # per-delta here, which destroyed downstream state machines
            # when a tag was split across deltas (e.g. MiniMax-M2.7
            # sends '<think>' and its content as separate deltas —
            # regex case 2 erased the first delta, so the CLI/gateway
            # state machine never saw the open tag and leaked the
            # reasoning content as regular response text).
            think_scrubber = getattr(self, "_stream_think_scrubber", None)
            if think_scrubber is not None:
                text = think_scrubber.feed(text or "")
            else:
                # Defensive: legacy callers without the scrubber attribute.
                text = self._strip_think_blocks(text or "")
            # Then feed through the stateful context scrubber so memory-context
            # spans split across chunks cannot leak to the UI (#5719).
            scrubber = getattr(self, "_stream_context_scrubber", None)
            if scrubber is not None:
                text = scrubber.feed(text)
            else:
                # Defensive: legacy callers without the scrubber attribute.
                text = sanitize_context(text)
            # Only strip leading newlines on the first delta — mid-stream "\n" is legitimate markdown.
            if not prepended_break and not getattr(
                self, "_current_streamed_assistant_text", ""
            ):
                text = text.lstrip("\n")
        if not text:
            return
        callbacks = [cb for cb in (self.stream_delta_callback, self._stream_callback) if cb is not None]
        delivered = False
        for cb in callbacks:
            try:
                cb(text)
                delivered = True
            except Exception:
                pass
        if delivered:
            self._record_streamed_assistant_text(text)

    def _fire_reasoning_delta(self, text: str) -> None:
        """Fire reasoning callback if registered."""
        # Single-writer guard (#65991): fence out a superseded stream's
        # reasoning deltas the same way as content deltas.
        if self._stream_writer_superseded():
            self._note_dropped_stream_writer("_fire_reasoning_delta")
            return
        cb = self.reasoning_callback
        if cb is not None:
            try:
                cb(text)
            except Exception:
                pass
            else:
                # Only checkpoint reasoning that a surface actually displayed.
                # show_reasoning=false leaves the callback unset, so hidden
                # provider thinking never becomes visible transcript content.
                if isinstance(text, str) and text:
                    self._current_streamed_reasoning_text = (
                        getattr(self, "_current_streamed_reasoning_text", "")
                        + text
                    )

    def _fire_tool_gen_started(self, tool_name: str) -> None:
        """Notify display layer that the model is generating tool call arguments.

        Fires once per tool name when the streaming response begins producing
        tool_call / tool_use tokens.  Gives the TUI a chance to show a spinner
        or status line so the user isn't staring at a frozen screen while a
        large tool payload (e.g. a 45 KB write_file) is being generated.
        """
        cb = self.tool_gen_callback
        if cb is not None:
            try:
                cb(tool_name)
            except Exception:
                pass

    def _has_stream_consumers(self) -> bool:
        """Return True if any streaming consumer is registered."""
        return (
            self.stream_delta_callback is not None
            or getattr(self, "_stream_callback", None) is not None
        )

