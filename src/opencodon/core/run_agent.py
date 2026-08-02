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
from opencodon.common.repo import REPO_ROOT

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


# Internal flags that mark a message as ephemeral empty-response/prefill
# recovery scaffolding: the synthetic assistant "(empty)" turn and user nudge
# injected after an empty response, the terminal "(empty)" sentinel, and the
# thinking-only prefill placeholder. These exist only to drive the next API
# retry; the in-memory loop pops them before appending the real response.
# Persistence must mirror that, otherwise an append-only flush can commit them
# to the session store and a resumed session replays synthetic "(empty)"/nudge
# turns as if they were genuine context.
_EPHEMERAL_SCAFFOLDING_FLAGS = (
    "_empty_recovery_synthetic",
    "_empty_terminal_sentinel",
    "_thinking_prefill",
    # verify-on-stop and pre_verify nudges append a synthetic user nudge to
    # keep the agent going one more turn before it can claim completion.
    # The nudge exists only to drive the verification loop; persisting it
    # poisons the resumed transcript and breaks prompt-prefix cache reuse
    # on later turns. The assistant candidate is NOT synthetic — it is
    # persisted and emitted as an interim message (#65919).
    "_verification_stop_synthetic",
    "_pre_verify_synthetic",
)


def _is_ephemeral_scaffolding(msg: Any) -> bool:
    """Return True when ``msg`` is internal recovery scaffolding that must never
    be persisted to the durable transcript (SQLite session store or JSON log)."""
    return isinstance(msg, dict) and any(
        msg.get(flag) for flag in _EPHEMERAL_SCAFFOLDING_FLAGS
    )


_MAX_TOOL_WORKERS = 8

# Intrinsic marker stamped on a message dict once it has been written to the
# SQLite session store.  Used by ``_flush_messages_to_session_db`` to decide
# what is already durable.  An object-identity (``id(msg)``) dedup set cannot be
# trusted across turns: once a flushed message dict is dropped from the live
# list (e.g. by scaffolding rewind or in-place compaction) and garbage-
# collected, CPython is free to hand its address to a brand-new assistant/tool
# message, whose ``id()`` then collides with the stale entry and the real turn
# is silently never persisted.  A marker bound to the dict itself cannot be
# aliased that way.  The ``_`` prefix is mandatory: the wire sanitizers
# (agent/transports/chat_completions.py, agent/chat_completion_helpers.py) strip
# every top-level ``_``-prefixed key before the request leaves the process, so
# this never reaches a strict OpenAI-compatible gateway.
_DB_PERSISTED_MARKER = "_db_persisted"


# Guard so the OpenRouter metadata pre-warm thread is only spawned once per
# process, not once per AIAgent instantiation.  Without this, long-running
# gateway processes leak one OS thread per incoming message and eventually
# exhaust the system thread limit (RuntimeError: can't start new thread).
_openrouter_prewarm_done = threading.Event()

# =========================================================================
# Large tool result handler — save oversized output to temp file
# =========================================================================


# =========================================================================
# Qwen Portal headers — mimics QwenCode CLI for portal.qwen.ai compatibility.
# Extracted as a module-level helper so both __init__ and
# _apply_client_headers_for_base_url can share it.
# =========================================================================
_QWEN_CODE_VERSION = "0.14.1"


def _routermint_headers() -> dict:
    """Return the User-Agent RouterMint needs to avoid Cloudflare 1010 blocks."""
    from opencodon.frontends.cli import __version__ as _OPENCODON_VERSION

    return {
        "User-Agent": f"OpencodonAgent/{_OPENCODON_VERSION}",
    }


def _pool_may_recover_from_rate_limit(pool) -> bool:
    """Decide whether to wait for credential-pool rotation instead of falling back.

    The existing pool-rotation path requires the pool to (1) exist and (2) have
    at least one entry not currently in exhaustion cooldown.  But rotation is
    only meaningful when the pool has more than one entry.

    With a single-credential pool (common for Vertex service accounts and any
    "one personal key" configuration), the primary entry just 429'd and there
    is nothing to rotate to.  Waiting for the pool cooldown to expire means
    retrying against the same exhausted quota — the daily-quota 429 will recur
    immediately, and the retry budget is burned.

    In that case we must fall back to the configured ``fallback_model``
    instead.  Returns True only when rotation has somewhere to go.

    See issues #11314 and #13636.
    """
    if pool is None:
        return False
    if not pool.has_available():
        return False
    return len(pool.entries()) > 1


def _qwen_portal_headers() -> dict:
    """Return default HTTP headers required by Qwen Portal API."""
    import platform as _plat

    _ua = f"QwenCode/{_QWEN_CODE_VERSION} ({_plat.system().lower()}; {_plat.machine()})"
    return {
        "User-Agent": _ua,
        "X-DashScope-CacheControl": "enable",
        "X-DashScope-UserAgent": _ua,
        "X-DashScope-AuthType": "qwen-oauth",
    }


def _safe_session_filename_component(session_id: str) -> str:
    """Return a stable, path-safe filename component for a session ID.

    Session IDs can originate from untrusted input (e.g. the
    ``X-Hermes-Session-Id`` API header) and are otherwise interpolated raw
    into on-disk artifact filenames under ``~/.opencodon/sessions/``.  Without
    sanitization, a traversal-shaped ID such as ``../../../../etc/pwned``
    would let a caller write the session snapshot / request dump outside the
    sessions directory.  This collapses every non ``[A-Za-z0-9_-]`` character
    to ``_`` (so no path separators or ``.`` survive), caps the length, and —
    when sanitization changed the string — appends a short content hash so two
    distinct IDs that sanitize to the same component don't collide.  The
    result is always a single, traversal-free path segment.
    """
    raw = str(session_id or "").strip()
    sanitized = re.sub(r"[^\w-]", "_", raw).strip("._")
    sanitized = sanitized[:96] or "session"
    if raw and sanitized == raw:
        return sanitized
    digest = hashlib.sha256(
        raw.encode("utf-8", errors="surrogatepass")
    ).hexdigest()[:12]
    return f"{sanitized}_{digest}"


class _StreamErrorEvent(Exception):
    """Synthesized provider error surfaced from a Responses ``error`` SSE frame.

    Some Codex-style Responses backends (xAI for subscription/quota
    failures, custom relays under malformed-tool-call conditions) emit a
    standalone ``type=error`` frame instead of routing the failure
    through ``response.failed`` or returning an HTTP 4xx.  The fallback
    streaming path raises this exception so ``_summarize_api_error`` and
    ``_extract_api_error_context`` see a familiar ``.body`` /
    ``.status_code`` shape and the entitlement detector can match the
    underlying provider message ("do not have an active Grok
    subscription", etc.).
    """

    def __init__(
        self,
        message: str,
        *,
        code: Optional[str] = None,
        param: Optional[str] = None,
        status_code: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.param = param
        self.status_code = status_code
        # OpenAI SDK-shaped body so _extract_api_error_context /
        # _summarize_api_error / classify_api_error all pick it up.
        self.body: Dict[str, Any] = {
            "error": {
                "message": message,
                "code": code,
                "param": param,
                "type": "error",
            }
        }


from opencodon.core.agent_emit import AgentEmitMixin
from opencodon.core.agent_errors import AgentErrorsMixin
from opencodon.core.agent_persistence import AgentPersistenceMixin
from opencodon.core.agent_clients import AgentClientsMixin
from opencodon.core.agent_streaming import AgentStreamingMixin
from opencodon.core.agent_message_prep import AgentMessagePrepMixin
from opencodon.core.agent_control import AgentControlMixin
from opencodon.core.agent_lifecycle import AgentLifecycleMixin
from opencodon.core.agent_tool_exec import AgentToolExecMixin


class AIAgent(
    AgentEmitMixin,
    AgentErrorsMixin,
    AgentPersistenceMixin,
    AgentClientsMixin,
    AgentStreamingMixin,
    AgentMessagePrepMixin,
    AgentControlMixin,
    AgentLifecycleMixin,
    AgentToolExecMixin,
):
    """
    AI Agent with tool calling capabilities.

    This class manages the conversation flow, tool execution, and response handling
    for AI models that support function calling.
    """

    _TOOL_CALL_ARGUMENTS_CORRUPTION_MARKER = (
        "[opencodon: tool call arguments were corrupted in this session and "
        "have been dropped to keep the conversation alive. See issue #15236.]"
    )

    @property
    def base_url(self) -> str:
        return self._base_url

    @base_url.setter
    def base_url(self, value: str) -> None:
        self._base_url = value
        self._base_url_lower = value.lower() if value else ""
        self._base_url_hostname = base_url_hostname(value)

    def __init__(
        self,
        base_url: str = None,
        api_key: str = None,
        provider: str = None,
        api_mode: str = None,
        acp_command: str = None,
        acp_args: list[str] | None = None,
        command: str = None,
        args: list[str] | None = None,
        model: str = "",
        max_iterations: int = 90,  # Default tool-calling iterations (shared with subagents)
        tool_delay: float = 1.0,
        enabled_toolsets: List[str] = None,
        disabled_toolsets: List[str] = None,
        save_trajectories: bool = False,
        verbose_logging: bool = False,
        quiet_mode: bool = False,
        tool_progress_mode: str = "all",
        ephemeral_system_prompt: str = None,
        log_prefix_chars: int = 100,
        log_prefix: str = "",
        providers_allowed: List[str] = None,
        providers_ignored: List[str] = None,
        providers_order: List[str] = None,
        provider_sort: str = None,
        provider_require_parameters: bool = False,
        provider_data_collection: str = None,
        openrouter_min_coding_score: Optional[float] = None,
        session_id: str = None,
        tool_progress_callback: callable = None,
        tool_start_callback: callable = None,
        tool_complete_callback: callable = None,
        thinking_callback: callable = None,
        reasoning_callback: callable = None,
        clarify_callback: callable = None,
        read_terminal_callback: callable = None,
        step_callback: callable = None,
        stream_delta_callback: callable = None,
        interim_assistant_callback: callable = None,
        tool_gen_callback: callable = None,
        status_callback: callable = None,
        notice_callback: callable = None,
        notice_clear_callback: callable = None,
        event_callback: Optional[Callable[[str, dict], None]] = None,
        reaction_callback: Optional[Callable[[str], None]] = None,
        max_tokens: int = None,
        reasoning_config: Dict[str, Any] = None,
        service_tier: str = None,
        request_overrides: Dict[str, Any] = None,
        prefill_messages: List[Dict[str, Any]] = None,
        platform: str = None,
        user_id: str = None,
        user_id_alt: str = None,
        user_name: str = None,
        chat_id: str = None,
        chat_name: str = None,
        chat_type: str = None,
        thread_id: str = None,
        gateway_session_key: str = None,
        skip_context_files: bool = False,
        load_soul_identity: bool = False,
        skip_memory: bool = False,
        session_db=None,
        parent_session_id: str = None,
        iteration_budget: "IterationBudget" = None,
        fallback_model: Dict[str, Any] = None,
        credential_pool=None,
        checkpoints_enabled: bool = False,
        checkpoint_max_snapshots: int = 20,
        checkpoint_max_total_size_mb: int = 500,
        checkpoint_max_file_size_mb: int = 10,
        pass_session_id: bool = False,
    ):
        """Forwarder — see ``agent.agent_init.init_agent``."""
        from opencodon.core.agent_init import init_agent
        init_agent(
            self,
            base_url=base_url,
            api_key=api_key,
            provider=provider,
            api_mode=api_mode,
            acp_command=acp_command,
            acp_args=acp_args,
            command=command,
            args=args,
            model=model,
            max_iterations=max_iterations,
            tool_delay=tool_delay,
            enabled_toolsets=enabled_toolsets,
            disabled_toolsets=disabled_toolsets,
            save_trajectories=save_trajectories,
            verbose_logging=verbose_logging,
            quiet_mode=quiet_mode,
            tool_progress_mode=tool_progress_mode,
            ephemeral_system_prompt=ephemeral_system_prompt,
            log_prefix_chars=log_prefix_chars,
            log_prefix=log_prefix,
            providers_allowed=providers_allowed,
            providers_ignored=providers_ignored,
            providers_order=providers_order,
            provider_sort=provider_sort,
            provider_require_parameters=provider_require_parameters,
            provider_data_collection=provider_data_collection,
            openrouter_min_coding_score=openrouter_min_coding_score,
            session_id=session_id,
            tool_progress_callback=tool_progress_callback,
            tool_start_callback=tool_start_callback,
            tool_complete_callback=tool_complete_callback,
            thinking_callback=thinking_callback,
            reasoning_callback=reasoning_callback,
            clarify_callback=clarify_callback,
            read_terminal_callback=read_terminal_callback,
            step_callback=step_callback,
            stream_delta_callback=stream_delta_callback,
            interim_assistant_callback=interim_assistant_callback,
            tool_gen_callback=tool_gen_callback,
            status_callback=status_callback,
            notice_callback=notice_callback,
            notice_clear_callback=notice_clear_callback,
            event_callback=event_callback,
            reaction_callback=reaction_callback,
            max_tokens=max_tokens,
            reasoning_config=reasoning_config,
            service_tier=service_tier,
            request_overrides=request_overrides,
            prefill_messages=prefill_messages,
            platform=platform,
            user_id=user_id,
            user_id_alt=user_id_alt,
            user_name=user_name,
            chat_id=chat_id,
            chat_name=chat_name,
            chat_type=chat_type,
            thread_id=thread_id,
            gateway_session_key=gateway_session_key,
            skip_context_files=skip_context_files,
            load_soul_identity=load_soul_identity,
            skip_memory=skip_memory,
            session_db=session_db,
            parent_session_id=parent_session_id,
            iteration_budget=iteration_budget,
            fallback_model=fallback_model,
            credential_pool=credential_pool,
            checkpoints_enabled=checkpoints_enabled,
            checkpoint_max_snapshots=checkpoint_max_snapshots,
            checkpoint_max_total_size_mb=checkpoint_max_total_size_mb,
            checkpoint_max_file_size_mb=checkpoint_max_file_size_mb,
            pass_session_id=pass_session_id,
        )






    def switch_model(self, new_model, new_provider, api_key='', base_url='', api_mode=''):
        """Forwarder — see ``agent.agent_runtime_helpers.switch_model``."""
        from opencodon.core.agent_runtime_helpers import switch_model
        return switch_model(self, new_model, new_provider, api_key, base_url, api_mode)










    # ── Buffered retry/fallback status ────────────────────────────────────
    # Retry and fallback chains were flooding the CLI/gateway with status
    # noise that users found confusing: a single transient 429 could produce
    # 10+ "Provider/Endpoint/Retrying in 5s..." lines before the request
    # eventually succeeded.  The buffered helpers below capture these
    # status messages instead of emitting them immediately.  They are
    # flushed (shown to the user) ONLY when every retry and fallback has
    # been exhausted; on success they are silently dropped.  Backend logs
    # (agent.log) are unaffected — every individual emission site still
    # writes to ``logger.warning`` / ``logger.info`` for diagnosis.







    # Stream-diagnostic class header preserved for backward compat —
    # actual list lives in ``agent.stream_diag.STREAM_DIAG_HEADERS``.
    from opencodon.core.stream_diag import STREAM_DIAG_HEADERS as _STREAM_DIAG_HEADERS  # noqa: E402






















    @staticmethod
    def _provider_model_requires_responses_api(
        model: str,
        *,
        provider: Optional[str] = None,
    ) -> bool:
        """Return True when this provider/model pair should use Responses API."""
        normalized_provider = (provider or "").strip().lower()
        if normalized_provider == "custom":
            # Generic custom endpoints are conservative by default. They may
            # relay GPT-5 models without full Responses semantics, so only
            # direct OpenAI/xAI URL detection should auto-upgrade them.
            return False
        if normalized_provider == "copilot":
            try:
                from opencodon.core.models import _should_use_copilot_responses_api
                return _should_use_copilot_responses_api(model)
            except Exception:
                # Fall back to the generic GPT-5 rule if Copilot-specific
                # logic is unavailable for any reason.
                pass
        return AIAgent._model_requires_responses_api(model)











    # ------------------------------------------------------------------
    # Background memory/skill review — prompts live in agent.background_review
    # ------------------------------------------------------------------
    from opencodon.core.background_review import (
        _MEMORY_REVIEW_PROMPT,
        _SKILL_REVIEW_PROMPT,
        _COMBINED_REVIEW_PROMPT,
    )











    def _format_tools_for_system_message(self) -> str:
        """Forwarder — see ``agent.system_prompt.format_tools_for_system_message``."""
        from opencodon.core.system_prompt import format_tools_for_system_message
        return format_tools_for_system_message(self)





    @staticmethod
    def _coerce_api_error_detail(value: Any) -> str:
        """Return a display-safe string for structured provider error fields."""
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            for key in ("message", "detail", "error", "code", "type"):
                nested = value.get(key)
                if isinstance(nested, str) and nested.strip():
                    return nested
            for key in ("message", "detail", "error", "code", "type"):
                if key in value:
                    nested_detail = AIAgent._coerce_api_error_detail(value[key])
                    if nested_detail:
                        return nested_detail
            try:
                return json.dumps(value, ensure_ascii=False, sort_keys=True)
            except TypeError:
                return str(value)
        if isinstance(value, (list, tuple)):
            parts = [
                AIAgent._coerce_api_error_detail(item)
                for item in value
            ]
            return "; ".join(part for part in parts if part)
        if value is None:
            return ""
        return str(value)

    @staticmethod
    def _summarize_api_error(error: Exception) -> str:
        """Extract a human-readable one-liner from an API error.

        Handles Cloudflare HTML error pages (502, 503, etc.) by pulling the
        <title> tag instead of dumping raw HTML.  Falls back to a truncated
        str(error) for everything else.
        """
        raw = str(error)

        if (
            isinstance(error, ValueError)
            and "expected ident at line" in raw.lower()
        ):
            return f"Malformed provider streaming response: {raw[:300]}"

        # Cloudflare / proxy HTML pages: grab the <title> for a clean summary
        if "<!DOCTYPE" in raw or "<html" in raw:
            m = re.search(r"<title[^>]*>([^<]+)</title>", raw, re.IGNORECASE)
            title = m.group(1).strip() if m else "HTML error page (title not found)"
            # Also grab Cloudflare Ray ID if present
            ray = re.search(r"Cloudflare Ray ID:\s*<strong[^>]*>([^<]+)</strong>", raw)
            ray_id = ray.group(1).strip() if ray else None
            status_code = getattr(error, "status_code", None)
            parts = []
            if status_code:
                parts.append(f"HTTP {status_code}")
            parts.append(title)
            if ray_id:
                parts.append(f"Ray {ray_id}")
            return " — ".join(parts)

        # JSON body errors from OpenAI/Anthropic SDKs
        body = getattr(error, "body", None)
        if isinstance(body, dict):
            msg = body.get("error", {}).get("message") if isinstance(body.get("error"), dict) else body.get("message")
            if msg:
                status_code = getattr(error, "status_code", None)
                prefix = f"HTTP {status_code}: " if status_code else ""
                msg = AIAgent._coerce_api_error_detail(msg)
                return AIAgent._decorate_xai_entitlement_error(f"{prefix}{msg[:300]}")

        # SDK may leave body empty while httpx still has the payload (#36109).
        # Redact before returning: the raw provider/proxy error body is
        # attacker-influenced and may echo Authorization / x-api-key / request
        # JSON, which would otherwise leak into final_response + logs (this path
        # widens exposure vs the old empty-body "HTTP 400" string).
        response = getattr(error, "response", None)
        if response is not None:
            try:
                snippet = (getattr(response, "text", None) or "").strip()
            except Exception:
                snippet = ""
            if snippet:
                status_code = getattr(error, "status_code", None)
                prefix = f"HTTP {status_code}: " if status_code else ""
                try:
                    payload = json.loads(snippet)
                except (json.JSONDecodeError, TypeError):
                    payload = None
                if isinstance(payload, dict):
                    err = payload.get("error")
                    if isinstance(err, dict) and err.get("message"):
                        return redact_sensitive_text(f"{prefix}{str(err['message'])[:300]}")
                    if payload.get("message"):
                        return redact_sensitive_text(f"{prefix}{str(payload['message'])[:300]}")
                return redact_sensitive_text(f"{prefix}{snippet[:300]}")

        # Fallback: truncate the raw string but give more room than 200 chars
        status_code = getattr(error, "status_code", None)
        prefix = f"HTTP {status_code}: " if status_code else ""
        return AIAgent._decorate_xai_entitlement_error(f"{prefix}{raw[:500]}")


























    # Bare absolute / home / Windows-drive file paths in a footer line.
    # Anchors mirror the gateway's ``extract_local_files`` bare-path
    # detector so that anything the gateway WOULD auto-attach is wrapped
    # in inline-code backticks here first (the extractor skips paths inside
    # `code` spans).  Defense-in-depth: even if a future error message
    # echoes a credential path (config.yaml, .env, auth.json) into the
    # user-facing footer, it can never be matched as a deliverable bare
    # path and silently uploaded to a messaging channel (#35584).
    _FOOTER_PATH_RE = re.compile(
        r"(?<![/:\w.`])(?:~/|/|[A-Za-z]:[/\\])(?:[\w.\-]+[/\\])*[\w.\-]+\.[\w]+",
    )





























    def _build_system_prompt_parts(self, system_message: str = None) -> Dict[str, str]:
        """Forwarder — see ``agent.system_prompt.build_system_prompt_parts``."""
        from opencodon.core.system_prompt import build_system_prompt_parts
        return build_system_prompt_parts(self, system_message=system_message)

    def _build_system_prompt(self, system_message: str = None) -> str:
        """Forwarder — see ``agent.system_prompt.build_system_prompt``."""
        from opencodon.core.system_prompt import build_system_prompt
        return build_system_prompt(self, system_message=system_message)



    _VALID_API_ROLES = frozenset({"system", "user", "assistant", "tool", "function", "developer"})







    def _invalidate_system_prompt(self):
        """Forwarder — see ``agent.system_prompt.invalidate_system_prompt``."""
        from opencodon.core.system_prompt import invalidate_system_prompt
        invalidate_system_prompt(self)





































    # ── Unified streaming API call ─────────────────────────────────────────
























    # ── Per-turn primary restoration ─────────────────────────────────────




    # 20 MB base64 ≈ 15 MB decoded image — generous but prevents OOM from an
    # oversized data: URL (a 100 MB+ payload creates ~275 MB of memory pressure,
    # and gateway users sharing the same process can trivially OOM it).
    _MAX_DATA_URL_BASE64_BYTES = 20 * 1024 * 1024

    @staticmethod
    def _materialize_data_url_for_vision(image_url: str) -> tuple[str, Optional[Path]]:
        header, _, data = str(image_url or "").partition(",")
        if len(data) > AIAgent._MAX_DATA_URL_BASE64_BYTES:
            logger.warning(
                "data-URL payload too large (%d bytes), skipping", len(data)
            )
            return "", None
        mime = "image/jpeg"
        if header.startswith("data:"):
            mime_part = header[len("data:"):].split(";", 1)[0].strip()
            if mime_part.startswith("image/"):
                mime = mime_part
        suffix = {
            "image/png": ".png",
            "image/gif": ".gif",
            "image/webp": ".webp",
            "image/jpeg": ".jpg",
            "image/jpg": ".jpg",
        }.get(mime, ".jpg")
        tmp = tempfile.NamedTemporaryFile(prefix="anthropic_image_", suffix=suffix, delete=False)
        try:
            with tmp:
                tmp.write(base64.b64decode(data))
        except Exception:
            # delete=False means a corrupt/unsupported data URL would otherwise
            # leak a zero-byte temp file on every failed materialization.
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
            raise
        path = Path(tmp.name)
        return str(path), path












































    def run_conversation(
        self,
        user_message: Any,
        system_message: str = None,
        conversation_history: List[Dict[str, Any]] = None,
        task_id: str = None,
        stream_callback: Optional[callable] = None,
        persist_user_message: Optional[Any] = None,
        persist_user_timestamp: Optional[float] = None,
        moa_config: Optional[dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Forwarder — see ``agent.conversation_loop.run_conversation``."""
        from opencodon.core.aux_accounting import (
            reset_accounting_context,
            set_accounting_context,
        )
        from opencodon.core.conversation_loop import run_conversation
        # Publish the session accounting handles so auxiliary
        # calls record their token usage into session_model_usage (task
        # dimension) — the fix for aux spend being invisible in analytics
        # (issue #23270).
        acct_token = set_accounting_context(
            getattr(self, "_session_db", None), getattr(self, "session_id", None)
        )
        from opencodon.core.auxiliary_client import scoped_runtime_main

        # The outer token restores the caller's Context even though turn setup
        # replaces the value with the live runtime after fallback restoration.
        # Keep the scope local instead of storing ContextVar tokens on the agent,
        # which may be observed from another thread.
        with scoped_runtime_main({}):
            try:
                return run_conversation(
                    self,
                    user_message,
                    system_message,
                    conversation_history,
                    task_id,
                    stream_callback,
                    persist_user_message,
                    persist_user_timestamp=persist_user_timestamp,
                    moa_config=moa_config,
                )
            finally:
                reset_accounting_context(acct_token)

    def chat(self, message: str, stream_callback: Optional[callable] = None) -> str:
        """
        Simple chat interface that returns just the final response.

        Args:
            message (str): User message
            stream_callback: Optional callback invoked with each text delta during streaming.

        Returns:
            str: Final assistant response
        """
        result = self.run_conversation(message, stream_callback=stream_callback)
        return result["final_response"]


def main(
    query: str = None,
    model: str = "",
    api_key: str = None,
    base_url: str = "",
    max_turns: int = 10,
    enabled_toolsets: str = None,
    disabled_toolsets: str = None,
    list_tools: bool = False,
    save_trajectories: bool = False,
    save_sample: bool = False,
    verbose: bool = False,
    log_prefix_chars: int = 20
):
    """
    Main function for running the agent directly.

    Args:
        query (str): Natural language query for the agent. Defaults to Python 3.13 example.
        model (str): Model name to use (OpenRouter format: provider/model). Defaults to anthropic/claude-sonnet-4.6.
        api_key (str): API key for authentication. Uses OPENROUTER_API_KEY env var if not provided.
        base_url (str): Base URL for the model API. Defaults to https://openrouter.ai/api/v1
        max_turns (int): Maximum number of API call iterations. Defaults to 10.
        enabled_toolsets (str): Comma-separated list of toolsets to enable. Supports predefined
                              toolsets (e.g., "research", "development", "safe").
                              Multiple toolsets can be combined: "web,vision"
        disabled_toolsets (str): Comma-separated list of toolsets to disable (e.g., "terminal")
        list_tools (bool): Just list available tools and exit
        save_trajectories (bool): Save conversation trajectories to JSONL files (appends to trajectory_samples.jsonl). Defaults to False.
        save_sample (bool): Save a single trajectory sample to a UUID-named JSONL file for inspection. Defaults to False.
        verbose (bool): Enable verbose logging for debugging. Defaults to False.
        log_prefix_chars (int): Number of characters to show in log previews for tool calls/responses. Defaults to 20.

    Toolset Examples:
        - "research": Web search, extract, crawl + vision tools
    """
    print("🤖 AI Agent with Tool Calling")
    print("=" * 50)
    
    # Handle tool listing
    if list_tools:
        from opencodon.tools.model_tools import get_all_tool_names, get_available_toolsets
        from toolsets import get_all_toolsets, get_toolset_info
        
        print("📋 Available Tools & Toolsets:")
        print("-" * 50)
        
        # Show new toolsets system
        print("\n🎯 Predefined Toolsets (New System):")
        print("-" * 40)
        all_toolsets = get_all_toolsets()
        
        # Group by category
        basic_toolsets = []
        composite_toolsets = []
        scenario_toolsets = []
        
        for name, toolset in all_toolsets.items():
            info = get_toolset_info(name)
            if info:
                entry = (name, info)
                if name in {"web", "terminal", "vision", "creative", "reasoning"}:
                    basic_toolsets.append(entry)
                elif name in {"research", "development", "analysis", "content_creation", "full_stack"}:
                    composite_toolsets.append(entry)
                else:
                    scenario_toolsets.append(entry)
        
        # Print basic toolsets
        print("\n📌 Basic Toolsets:")
        for name, info in basic_toolsets:
            tools_str = ', '.join(info['resolved_tools']) if info['resolved_tools'] else 'none'
            print(f"  • {name:15} - {info['description']}")
            print(f"    Tools: {tools_str}")
        
        # Print composite toolsets
        print("\n📂 Composite Toolsets (built from other toolsets):")
        for name, info in composite_toolsets:
            includes_str = ', '.join(info['includes']) if info['includes'] else 'none'
            print(f"  • {name:15} - {info['description']}")
            print(f"    Includes: {includes_str}")
            print(f"    Total tools: {info['tool_count']}")
        
        # Print scenario-specific toolsets
        print("\n🎭 Scenario-Specific Toolsets:")
        for name, info in scenario_toolsets:
            print(f"  • {name:20} - {info['description']}")
            print(f"    Total tools: {info['tool_count']}")
        
        
        # Show legacy toolset compatibility
        print("\n📦 Legacy Toolsets (for backward compatibility):")
        legacy_toolsets = get_available_toolsets()
        for name, info in legacy_toolsets.items():
            status = "✅" if info["available"] else "❌"
            print(f"  {status} {name}: {info['description']}")
            if not info["available"]:
                print(f"    Requirements: {', '.join(info['requirements'])}")
        
        # Show individual tools
        all_tools = get_all_tool_names()
        print(f"\n🔧 Individual Tools ({len(all_tools)} available):")
        for tool_name in sorted(all_tools):
            toolset = get_toolset_for_tool(tool_name)
            print(f"  📌 {tool_name} (from {toolset})")
        
        print("\n💡 Usage Examples:")
        print("  # Use predefined toolsets")
        print("  python run_agent.py --enabled_toolsets=research --query='search for Python news'")
        print("  python run_agent.py --enabled_toolsets=development --query='debug this code'")
        print("  python run_agent.py --enabled_toolsets=safe --query='analyze without terminal'")
        print("  ")
        print("  # Combine multiple toolsets")
        print("  python run_agent.py --enabled_toolsets=web,vision --query='analyze website'")
        print("  ")
        print("  # Disable toolsets")
        print("  python run_agent.py --disabled_toolsets=terminal --query='no command execution'")
        print("  ")
        print("  # Run with trajectory saving enabled")
        print("  python run_agent.py --save_trajectories --query='your question here'")
        return
    
    # Parse toolset selection arguments
    enabled_toolsets_list = None
    disabled_toolsets_list = None
    
    if enabled_toolsets:
        enabled_toolsets_list = [t.strip() for t in enabled_toolsets.split(",")]
        print(f"🎯 Enabled toolsets: {enabled_toolsets_list}")
    
    if disabled_toolsets:
        disabled_toolsets_list = [t.strip() for t in disabled_toolsets.split(",")]
        print(f"🚫 Disabled toolsets: {disabled_toolsets_list}")
    
    if save_trajectories:
        print("💾 Trajectory saving: ENABLED")
        print("   - Successful conversations → trajectory_samples.jsonl")
        print("   - Failed conversations → failed_trajectories.jsonl")
    
    # Initialize agent with provided parameters
    try:
        agent = AIAgent(
            base_url=base_url,
            model=model,
            api_key=api_key,
            max_iterations=max_turns,
            enabled_toolsets=enabled_toolsets_list,
            disabled_toolsets=disabled_toolsets_list,
            save_trajectories=save_trajectories,
            verbose_logging=verbose,
            log_prefix_chars=log_prefix_chars
        )
    except RuntimeError as e:
        print(f"❌ Failed to initialize agent: {e}")
        return
    
    # Use provided query or default to Python 3.13 example
    if query is None:
        user_query = (
            "Tell me about the latest developments in Python 3.13 and what new features "
            "developers should know about. Please search for current information and try it out."
        )
    else:
        user_query = query
    
    print(f"\n📝 User Query: {user_query}")
    print("\n" + "=" * 50)
    
    # Run conversation
    result = agent.run_conversation(user_query)
    
    print("\n" + "=" * 50)
    print("📋 CONVERSATION SUMMARY")
    print("=" * 50)
    print(f"✅ Completed: {result['completed']}")
    print(f"📞 API Calls: {result['api_calls']}")
    print(f"💬 Messages: {len(result['messages'])}")
    
    if result['final_response']:
        print("\n🎯 FINAL RESPONSE:")
        print("-" * 30)
        print(result['final_response'])
    
    # Save sample trajectory to UUID-named file if requested
    if save_sample:
        sample_id = str(uuid.uuid4())[:8]
        sample_filename = f"sample_{sample_id}.json"
        
        # Convert messages to trajectory format (same as batch_runner)
        trajectory = agent._convert_to_trajectory_format(
            result['messages'], 
            user_query, 
            result['completed']
        )
        
        entry = {
            "conversations": trajectory,
            "timestamp": datetime.now().isoformat(),
            "model": model,
            "completed": result['completed'],
            "query": user_query
        }
        
        try:
            with open(sample_filename, "w", encoding="utf-8") as f:
                # Pretty-print JSON with indent for readability
                f.write(json.dumps(entry, ensure_ascii=False, indent=2))
            print(f"\n💾 Sample trajectory saved to: {sample_filename}")
        except Exception as e:
            print(f"\n⚠️ Failed to save sample: {e}")
    
    print("\n👋 Agent execution completed!")


if __name__ == "__main__":
    import fire
    fire.Fire(main)
