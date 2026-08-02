"""AIAgent AgentLifecycleMixin — extracted from run_agent.py (restructure Phase 4).

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


class AgentLifecycleMixin:
    def _transition_context_engine_session(
        self,
        *,
        old_session_id: Optional[str] = None,
        new_session_id: Optional[str] = None,
        previous_messages: Optional[list] = None,
        carry_over_context: bool = False,
        reset_engine: bool = True,
        **extra_context,
    ) -> None:
        """Notify the active context engine about a host session transition.

        Generic host-side lifecycle helper. The built-in compressor keeps its
        existing reset behavior; plugin engines that implement richer hooks
        (``on_session_end``, ``on_session_reset``, ``on_session_start``,
        ``carry_over_new_session_context``) can flush old-session state,
        reset runtime counters, bind to the new session, and optionally
        carry retained context forward.
        """
        engine = getattr(self, "context_compressor", None)
        if not engine:
            return

        if old_session_id and previous_messages is not None and hasattr(engine, "on_session_end"):
            try:
                engine.on_session_end(old_session_id, previous_messages)
            except Exception as exc:
                _ra.logger.debug("context engine on_session_end during transition: %s", exc)

        if reset_engine and hasattr(engine, "on_session_reset"):
            try:
                engine.on_session_reset()
            except Exception as exc:
                _ra.logger.debug("context engine on_session_reset during transition: %s", exc)

        should_start = bool(
            old_session_id
            or previous_messages is not None
            or carry_over_context
            or extra_context
        )
        target_session_id = new_session_id or getattr(self, "session_id", "") or ""
        if should_start and target_session_id and hasattr(engine, "on_session_start"):
            start_context = {
                "old_session_id": old_session_id,
                "carry_over_context": carry_over_context,
                "platform": _ra._session_source_for_agent(getattr(self, "platform", None)),
                "model": getattr(self, "model", ""),
                "context_length": getattr(engine, "context_length", None),
                "conversation_id": getattr(self, "_gateway_session_key", None),
            }
            start_context.update(extra_context)
            start_context = {k: v for k, v in start_context.items() if v not in (None, "")}
            try:
                engine.on_session_start(target_session_id, **start_context)
            except Exception as exc:
                _ra.logger.debug("context engine on_session_start during transition: %s", exc)

        if (
            carry_over_context
            and old_session_id
            and target_session_id
            and hasattr(engine, "carry_over_new_session_context")
        ):
            try:
                engine.carry_over_new_session_context(old_session_id, target_session_id)
            except Exception as exc:
                _ra.logger.debug("context engine carry_over_new_session_context during transition: %s", exc)

    def reset_session_state(
        self,
        previous_messages: Optional[list] = None,
        old_session_id: Optional[str] = None,
        carry_over_context: bool = False,
    ):
        """Reset all session-scoped token counters to 0 for a fresh session.
        
        This method encapsulates the reset logic for all session-level metrics
        including:
        - Token usage counters (input, output, total, prompt, completion)
        - Cache read/write tokens
        - API call count
        - Reasoning tokens
        - Estimated cost tracking
        - Context compressor internal counters
        
        The method safely handles optional attributes (e.g., context compressor)
        using ``hasattr`` checks.

        When ``previous_messages`` / ``old_session_id`` / ``carry_over_context``
        are provided, the active context engine is notified through the
        full transition lifecycle (``_transition_context_engine_session``)
        instead of a bare reset. Default callers pass nothing and keep the
        existing reset-only behavior.
        """
        # Token usage counters
        self.session_total_tokens = 0
        self.session_input_tokens = 0
        self.session_output_tokens = 0
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self.session_cache_read_tokens = 0
        self.session_cache_write_tokens = 0
        self.session_reasoning_tokens = 0
        self.session_api_calls = 0
        self.session_estimated_cost_usd = 0.0
        self.session_cost_status = "unknown"
        self.session_cost_source = "none"
        
        # Turn counter (added after reset_session_state was first written — #2635)
        self._user_turn_count = 0

        # Copilot x-initiator: True for the first API call of a user turn,
        # False for tool-loop follow-ups (#3040).
        self._is_user_initiated_turn = False

        # Context engine reset/transition (works for built-in compressor and plugins)
        self._transition_context_engine_session(
            old_session_id=old_session_id,
            new_session_id=getattr(self, "session_id", None),
            previous_messages=previous_messages,
            carry_over_context=carry_over_context,
            reset_engine=True,
        )

        # Reset-only session switches (/new, /resume, /branch) update
        # agent.session_id before calling reset_session_state(). The built-in
        # compressor keeps durable cooldown state keyed by its bound session,
        # so rebind it when the active session changed but no full start hook ran.
        engine = getattr(self, "context_compressor", None)
        target_session_id = getattr(self, "session_id", "") or ""
        bound_session_id = getattr(engine, "_session_id", "") if engine is not None else ""
        if (
            engine is not None
            and hasattr(engine, "bind_session_state")
            and target_session_id
            and target_session_id != bound_session_id
        ):
            try:
                engine.bind_session_state(getattr(self, "_session_db", None), target_session_id)
            except Exception as exc:
                _ra.logger.debug("context engine bind_session_state during reset: %s", exc)

    def _ensure_lmstudio_runtime_loaded(self, config_context_length: Optional[int] = None) -> None:
        """
        Preload the LM Studio model unless configured to rely on LM Studio JIT loading.
        """
        if (self.provider or "").strip().lower() != "lmstudio":
            return
        if (getattr(self, "lmstudio_load_mode", "explicit") or "explicit").strip().lower() == "jit":
            _ra.logger.debug("LM Studio explicit preload skipped: lmstudio_load_mode=jit")
            return
        try:
            from opencodon.core.model_metadata import MINIMUM_CONTEXT_LENGTH
            from opencodon.frontends.cli.models import ensure_lmstudio_model_loaded
            if config_context_length is None:
                config_context_length = getattr(self, "_config_context_length", None)
            target_ctx = max(config_context_length or 0, MINIMUM_CONTEXT_LENGTH)
            loaded_ctx = ensure_lmstudio_model_loaded(
                self.model, self.base_url, getattr(self, "api_key", ""), target_ctx,
            )
            if loaded_ctx:
                # Push into the live compressor so the status bar reflects the
                # real loaded ctx the moment the load resolves, instead of
                # holding the previous model's value (or "ctx --") through the
                # next render tick.
                cc = getattr(self, "context_compressor", None)
                if cc is not None:
                    cc.update_model(
                        model=self.model,
                        context_length=loaded_ctx,
                        base_url=self.base_url,
                        api_key=getattr(self, "api_key", ""),
                        provider=self.provider,
                        api_mode=self.api_mode,
                    )
        except Exception as err:
            _ra.logger.debug("LM Studio preload skipped: %s", err)

    def _current_main_runtime(self) -> Dict[str, str]:
        """Return the live main runtime for session-scoped auxiliary routing."""
        return {
            "model": getattr(self, "model", "") or "",
            "provider": getattr(self, "provider", "") or "",
            "base_url": getattr(self, "base_url", "") or "",
            "api_key": getattr(self, "api_key", "") or "",
            "api_mode": getattr(self, "api_mode", "") or "",
            "auth_mode": getattr(self, "auth_mode", "") or "",
        }

    def _check_compression_model_feasibility(self) -> None:
        """Forwarder — see ``agent.conversation_compression.check_compression_model_feasibility``."""
        from opencodon.core.conversation_compression import check_compression_model_feasibility
        check_compression_model_feasibility(self)

    def _replay_compression_warning(self) -> None:
        """Forwarder — see ``agent.conversation_compression.replay_compression_warning``."""
        from opencodon.core.conversation_compression import replay_compression_warning
        replay_compression_warning(self)

    def _is_direct_openai_url(self, base_url: str = None) -> bool:
        """Return True when a base URL targets OpenAI's native API."""
        if base_url is not None:
            hostname = base_url_hostname(base_url)
        else:
            hostname = getattr(self, "_base_url_hostname", "") or base_url_hostname(
                getattr(self, "_base_url_lower", "")
            )
        return hostname == "api.openai.com"

    def _is_azure_openai_url(self, base_url: str = None) -> bool:
        """Return True when a base URL targets Azure OpenAI.

        Azure OpenAI exposes an OpenAI-compatible endpoint at
        ``{resource}.openai.azure.com/openai/v1`` that accepts the
        standard ``openai`` Python client.  Unlike api.openai.com it
        does NOT support the Responses API — gpt-5.x models are served
        on the regular ``/chat/completions`` path — so routing decisions
        must treat Azure separately from direct OpenAI.
        """
        if base_url is not None:
            url = str(base_url).lower()
        else:
            url = getattr(self, "_base_url_lower", "") or ""
        return "openai.azure.com" in url

    def _is_github_copilot_url(self, base_url: str = None) -> bool:
        """Return True when a base URL targets GitHub Copilot's OpenAI-compatible API."""
        if base_url is not None:
            hostname = base_url_hostname(base_url)
        else:
            hostname = getattr(self, "_base_url_hostname", "") or base_url_hostname(
                getattr(self, "_base_url_lower", "")
            )
        if not hostname:
            return False
        return hostname == "api.githubcopilot.com" or hostname.endswith(".githubcopilot.com")

    def _resolved_api_call_timeout(self) -> float:
        """Resolve the effective per-call request timeout in seconds.

        Priority:
          1. ``providers.<id>.models.<model>.timeout_seconds`` (per-model override)
          2. ``providers.<id>.request_timeout_seconds`` (provider-wide)
          3. ``OPENCODON_API_TIMEOUT`` env var (legacy escape hatch)
          4. 1800.0s default

        Used by OpenAI-wire chat completions (streaming and non-streaming) so
        the per-provider config knob wins over the 1800s default.  Without this
        helper, the hardcoded ``OPENCODON_API_TIMEOUT`` fallback would always be
        passed as a per-call ``timeout=`` kwarg, overriding the client-level
        timeout the AIAgent.__init__ path configured.
        """
        cfg = get_provider_request_timeout(self.provider, self.model)
        if cfg is not None:
            return cfg
        return env_float("OPENCODON_API_TIMEOUT", 1800.0)

    def _resolved_api_call_stale_timeout_base(self) -> tuple[float, bool]:
        """Resolve the base non-stream stale timeout and whether it is implicit.

        Priority:
          1. ``providers.<id>.models.<model>.stale_timeout_seconds``
          2. ``providers.<id>.stale_timeout_seconds``
          3. ``OPENCODON_API_CALL_STALE_TIMEOUT`` env var
          4. 90.0s default (_ra.time-to-first-byte for non-streaming / Codex
             internal-streaming requests; lowered from 300s in May 2026 so
             fallback providers kick in faster when upstream providers
             stall).  The detector still scales up for large contexts in
             ``_compute_non_stream_stale_timeout``.

        Returns ``(timeout_seconds, uses_implicit_default)`` so the caller can
        preserve legacy behaviors that only apply when the user has *not*
        explicitly configured a stale timeout, such as auto-disabling the
        detector for local endpoints.
        """
        cfg = _ra.get_provider_stale_timeout(self.provider, self.model)
        if cfg is not None:
            return cfg, False

        env_timeout = os.getenv("OPENCODON_API_CALL_STALE_TIMEOUT")
        if env_timeout is not None:
            return float(env_timeout), False

        # Reasoning-model floor: auto-mitigation for known reasoning models
        # (Nemotron 3 Ultra, OpenAI o1/o3, Anthropic Opus 4.x thinking,
        # DeepSeek R1, Qwen QwQ, xAI Grok reasoning, etc.) whose cloud
        # gateways idle-kill before the model's thinking phase ends.
        # uses_implicit_default is False here so the local-endpoint
        # short-circuit in _compute_non_stream_stale_timeout does not
        # disable stale detection for users running reasoning models on a
        # local NIM endpoint.
        from opencodon.core.reasoning_timeouts import get_reasoning_stale_timeout_floor
        reasoning_floor = get_reasoning_stale_timeout_floor(self.model)
        if reasoning_floor is not None:
            return reasoning_floor, False

        return 90.0, True

    def _compute_non_stream_stale_timeout(self, api_payload: Any) -> float:
        """Compute the effective non-stream stale timeout for this request.

        Accepts either the full ``api_kwargs`` dict (Chat Completions or
        Responses API) or a legacy ``messages`` list.  Context-size scaling
        applies the same way to both shapes via
        :func:`agent.chat_completion_helpers.estimate_request_context_tokens`.
        """
        stale_base, uses_implicit_default = self._resolved_api_call_stale_timeout_base()
        base_url = getattr(self, "_base_url", None) or self.base_url or ""
        if uses_implicit_default and base_url and is_local_endpoint(base_url):
            return float("inf")

        from opencodon.core.chat_completion_helpers import estimate_request_context_tokens
        est_tokens = estimate_request_context_tokens(api_payload)
        if est_tokens > 100_000:
            return max(stale_base, 240.0)
        if est_tokens > 50_000:
            return max(stale_base, 150.0)
        return stale_base

    def _is_openrouter_url(self) -> bool:
        """Return True when the base URL targets OpenRouter."""
        return base_url_host_matches(self._base_url_lower, "openrouter.ai")

    def _is_copilot_url(self) -> bool:
        """Return True when the base URL targets GitHub Copilot or GitHub Models."""
        return (
            "api.githubcopilot.com" in self._base_url_lower
            or "models.github.ai" in self._base_url_lower
        )

    def _anthropic_prompt_cache_policy(
        self,
        *,
        provider: Optional[str] = None,
        base_url: Optional[str] = None,
        api_mode: Optional[str] = None,
        model: Optional[str] = None,
    ) -> tuple[bool, bool]:
        """Forwarder — see ``agent.agent_runtime_helpers.anthropic_prompt_cache_policy``."""
        from opencodon.core.agent_runtime_helpers import anthropic_prompt_cache_policy
        return anthropic_prompt_cache_policy(self, provider=provider, base_url=base_url, api_mode=api_mode, model=model)

    @staticmethod
    def _model_requires_responses_api(model: str) -> bool:
        """Return True for models that require the Responses API path.

        GPT-5.x models are rejected on /v1/chat/completions by both
        OpenAI and OpenRouter (error: ``unsupported_api_for_model``).
        Detect these so the correct api_mode is set regardless of
        which provider is serving the model.
        """
        m = model.lower()
        # Strip vendor prefix (e.g. "openai/gpt-5.4" → "gpt-5.4")
        if "/" in m:
            m = m.rsplit("/", 1)[-1]
        return m.startswith("gpt-5")

    def _max_tokens_param(self, value: int) -> dict:
        """Return the correct max tokens kwarg for the current provider.

        OpenAI's newer models (gpt-4o, gpt-4.1, gpt-5+, o-series) require
        'max_completion_tokens'. Azure OpenAI and GitHub Copilot also require
        'max_completion_tokens' for those families served via their
        OpenAI-compatible endpoints. OpenRouter, local models, and older
        OpenAI models use 'max_tokens'.

        The check is URL-first (api.openai.com / Azure / Copilot all use the
        new kwarg), then falls back to a model-name check so third-party
        OpenAI-compatible endpoints fronting those models are recognised —
        URL-only detection misses that case and silently sends the wrong
        kwarg, which the upstream model rejects with a 400.
        """
        if (
            self._is_direct_openai_url()
            or self._is_azure_openai_url()
            or self._is_github_copilot_url()
            or model_forces_max_completion_tokens(self.model)
        ):
            return {"max_completion_tokens": value}
        return {"max_tokens": value}

    @staticmethod
    def _requested_output_cap_from_api_kwargs(api_kwargs: Any) -> Optional[int]:
        """Extract the outgoing response token cap from a prepared request."""
        if not isinstance(api_kwargs, dict):
            return None
        for key in ("max_output_tokens", "max_completion_tokens", "max_tokens"):
            raw = api_kwargs.get(key)
            try:
                value = int(raw)
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
        return None

    def _is_ollama_glm_backend(self) -> bool:
        """Detect Ollama-hosted GLM models affected by stop misreports.

        Ollama can misreport truncated output as finish_reason='stop'.
        Detection relies on explicit Ollama signatures:
        - Port 11434 (Ollama default)
        - "ollama" in the base URL (e.g. ollama.local, /ollama/ path)
        - provider explicitly set to "ollama"

        Crucially it does NOT match arbitrary local/private endpoints
        (LiteLLM/sglang/vLLM/LM Studio proxies, Tailscale boxes), which
        report finish_reason correctly and were the source of #13971's
        false-positive truncation continuations.
        """
        model_lower = (self.model or "").lower()
        provider_lower = (self.provider or "").lower()
        if "glm" not in model_lower and provider_lower != "zai":
            return False
        if "ollama" in self._base_url_lower or ":11434" in self._base_url_lower:
            return True
        return provider_lower == "ollama"

    def _should_treat_stop_as_truncated(
        self,
        finish_reason: str,
        assistant_message,
        messages: Optional[list] = None,
    ) -> bool:
        """Detect conservative stop->length misreports for Ollama-hosted GLM models."""
        if finish_reason != "stop" or self.api_mode != "chat_completions":
            return False
        if not self._is_ollama_glm_backend():
            return False
        if not any(
            isinstance(msg, dict) and msg.get("role") == "tool"
            for msg in (messages or [])
        ):
            return False
        if assistant_message is None or getattr(assistant_message, "tool_calls", None):
            return False

        content = getattr(assistant_message, "content", None)
        if not isinstance(content, str):
            return False

        visible_text = self._strip_think_blocks(content).strip()
        if not visible_text:
            return False
        if len(visible_text) < 20 or not re.search(r"\s", visible_text):
            return False

        return not self._has_natural_response_ending(visible_text)

    def _looks_like_codex_intermediate_ack(
        self,
        user_message: str,
        assistant_content: str,
        messages: List[Dict[str, Any]],
        require_workspace: bool = True,
    ) -> bool:
        """Forwarder — see ``agent.agent_runtime_helpers.looks_like_codex_intermediate_ack``."""
        from opencodon.core.agent_runtime_helpers import looks_like_codex_intermediate_ack
        return looks_like_codex_intermediate_ack(
            self, user_message, assistant_content, messages, require_workspace
        )

    def _cleanup_task_resources(self, task_id: str) -> None:
        """Forwarder — see ``agent.chat_completion_helpers.cleanup_task_resources``."""
        from opencodon.core.chat_completion_helpers import cleanup_task_resources
        return cleanup_task_resources(self, task_id)

    def shutdown_memory_provider(self, messages: list = None) -> None:
        """Shut down the memory provider and context engine — call at actual session boundaries.

        This calls on_session_end() then shutdown_all() on the memory
        manager, and on_session_end() on the context engine.
        NOT called per-turn — only at CLI exit, /reset, gateway
        session expiry, etc.
        """
        if self._memory_manager:
            try:
                self._memory_manager.on_session_end(messages or [])
            except Exception as e:
                _ra.logger.warning("Memory provider on_session_end failed during shutdown: %s", e, exc_info=True)
            try:
                self._memory_manager.shutdown_all()
            except Exception:
                pass
        # Notify context engine of session end (flush DAG, close DBs, etc.)
        if hasattr(self, "context_compressor") and self.context_compressor:
            try:
                self.context_compressor.on_session_end(
                    self.session_id or "",
                    messages or [],
                )
            except Exception:
                pass

    def commit_memory_session(self, messages: list = None) -> None:
        """Trigger end-of-session extraction without tearing providers down.
        Called when session_id rotates (e.g. /new, context compression);
        providers keep their state and continue running under the old
        session_id — they just flush pending extraction now."""
        if self._memory_manager:
            try:
                self._memory_manager.on_session_end(messages or [])
            except Exception:
                pass
        # Notify context engine of session end too — same lifecycle moment as
        # the memory manager's on_session_end. Without this, engines that
        # accumulate per-session state (DAGs, summaries) leak that state from
        # the rotated-out session into whatever comes next under the same
        # compressor instance. Mirrors the call in shutdown_memory_provider().
        # See issue #22394.
        if hasattr(self, "context_compressor") and self.context_compressor:
            try:
                self.context_compressor.on_session_end(
                    self.session_id or "",
                    messages or [],
                )
            except Exception:
                pass

    def _sync_external_memory_for_turn(
        self,
        *,
        original_user_message: Any,
        final_response: Any,
        interrupted: bool,
        messages: list | None = None,
    ) -> None:
        """Mirror a completed turn into external memory providers.

        Called at the end of ``run_conversation`` with the cleaned user
        message (``original_user_message``) and the finalised assistant
        response.  The external memory backend gets both ``sync_all`` (to
        persist the exchange) and ``queue_prefetch_all`` (to start
        warming context for the next turn) in one shot.

        Uses ``original_user_message`` rather than ``user_message``
        because the latter may carry injected skill content that bloats
        or breaks provider queries.

        Interrupted turns are skipped entirely (#15218).  A partial
        assistant output, an aborted tool chain, or a mid-stream reset
        is not durable conversational truth — mirroring it into an
        external memory backend pollutes future recall with state the
        user never saw completed.  The prefetch is gated on the same
        flag: the user's next message is almost certainly a retry of
        the same intent, and a prefetch keyed on the interrupted turn
        would fire against stale context.

        Normal completed turns still sync as before.  The whole body is
        wrapped in ``try/except Exception`` because external memory
        providers are strictly best-effort — a misconfigured or offline
        backend must not block the user from seeing their response.
        """
        if interrupted:
            return
        if not (self._memory_manager and final_response and original_user_message):
            return
        # Multimodal turns carry content as a list of typed parts; providers
        # expect plain strings, so flatten to text first (newline-joined for
        # memory, vs the default space-join used for log/trajectory previews).
        user_text = _summarize_user_message_for_log(original_user_message, sep="\n")
        response_text = _summarize_user_message_for_log(final_response, sep="\n")
        if not (user_text and response_text):
            return
        try:
            sync_kwargs = {"session_id": self.session_id or ""}
            if messages is not None:
                sync_kwargs["messages"] = messages
            self._memory_manager.sync_all(
                user_text,
                response_text,
                **sync_kwargs,
            )
            self._memory_manager.queue_prefetch_all(
                user_text,
                session_id=self.session_id or "",
            )
        except Exception:
            pass

    def release_clients(self) -> None:
        """Release LLM client resources WITHOUT tearing down session tool state.

        Used by the gateway when evicting this agent from _agent_cache for
        memory-management reasons (LRU cap or idle TTL) — the session may
        resume at any _ra.time with a freshly-built AIAgent that reuses the
        same task_id / session_id, so we must NOT kill:
          - process_registry entries for task_id (user's bg shells)
          - terminal sandbox for task_id (cwd, env, shell state)
          - browser daemon for task_id (open tabs, cookies)
          - memory provider (has its own lifecycle; keeps running)

        We DO close:
          - OpenAI/httpx client pool (big chunk of held memory + sockets;
            the rebuilt agent gets a fresh client anyway)
          - Active child subagents (per-turn artefacts; safe to drop)

        Safe to call multiple times.  Distinct from close() — which is the
        hard teardown for actual session boundaries (/new, /reset, session
        expiry).
        """
        # Close active child agents (per-turn; no cross-turn persistence).
        try:
            with self._active_children_lock:
                children = list(self._active_children)
                self._active_children.clear()
            for child in children:
                try:
                    child.release_clients()
                except Exception:
                    # Fall back to full close on children; they're per-turn.
                    try:
                        child.close()
                    except Exception:
                        pass
        except Exception:
            pass

        # Close the OpenAI/httpx client to release sockets immediately.
        try:
            client = getattr(self, "client", None)
            if client is not None:
                self._close_openai_client(client, reason="cache_evict", shared=True)
                self.client = None
        except Exception:
            pass

    def close(self) -> None:
        """Release all resources held by this agent instance.

        Cleans up subprocess resources that would otherwise become orphans:
        - Background processes tracked in ProcessRegistry
        - Terminal sandbox environments
        - Browser daemon sessions
        - Active child agents (subagent delegation)
        - OpenAI/httpx client connections

        Safe to call multiple times (idempotent).  Each cleanup step is
        independently guarded so a failure in one does not prevent the rest.
        """
        task_id = getattr(self, "session_id", None) or ""

        # 1. Kill background processes for this task
        try:
            from opencodon.tools.process_registry import process_registry
            process_registry.kill_all(task_id=task_id)
        except Exception:
            pass

        # 2. Clean terminal sandbox environments
        try:
            _ra.cleanup_vm(task_id)
        except Exception:
            pass

        # 3. Clean browser daemon sessions
        try:
            _ra.cleanup_browser(task_id)
        except Exception:
            pass

        # 4. Close active child agents
        try:
            with self._active_children_lock:
                children = list(self._active_children)
                self._active_children.clear()
            for child in children:
                try:
                    child.close()
                except Exception:
                    pass
        except Exception:
            pass

        # 5. Close the OpenAI/httpx client
        try:
            client = getattr(self, "client", None)
            if client is not None:
                self._close_openai_client(client, reason="agent_close", shared=True)
                self.client = None
        except Exception:
            pass

        # 6. Free conversation history.  Mirrors _release_evicted_agent_soft's
        # soft-eviction clear — close() is the hard teardown for true session
        # boundaries (/new, /reset, session expiry), so the message list won't
        # be reused.  Drops the reference proactively rather than waiting for
        # the agent object itself to be collected, which matters when a caller
        # still holds the closed agent (e.g. a draining background task).
        try:
            self._session_messages = []
        except Exception:
            pass

        # 7. Finalize the owned SQLite session row unless this agent is only a
        # temporary helper that deliberately handed session ownership forward
        # (manual compression helpers that rotate to a continuation session_id,
        # or background-review forks that share the live parent's session_id and
        # must leave it open). end_session() is first-reason-wins and no-ops on
        # an already-ended row, so this never clobbers a 'compression' /
        # 'cron_complete' / 'cli_close' reason set by an earlier terminal path.
        try:
            if getattr(self, "_end_session_on_close", True):
                session_db = getattr(self, "_session_db", None)
                session_id = getattr(self, "session_id", None)
                if session_db and session_id:
                    session_db.end_session(session_id, "agent_close")
        except Exception:
            pass

    def _hydrate_todo_store(self, history: List[Dict[str, Any]]) -> None:
        """
        Recover todo state from conversation history.
        
        The gateway creates a fresh AIAgent per message, so the in-memory
        TodoStore is empty. We scan the history for the most recent todo
        tool response and replay it to reconstruct the state.

        Hydration is restricted to tool results that are paired with an
        earlier assistant ``todo`` tool call. The gateway/API server accepts
        caller-supplied ``conversation_history``, so a forged bare
        ``role: tool`` message carrying a ``todos`` array must not be able to
        seed the store without a matching canonical tool call
        (GHSA-5g4g-6jrg-mw3g).
        """
        from opencodon.tools.todo_tool import MAX_TODO_RESULT_CHARS

        # Walk history backwards to find the most recent todo tool response
        last_todo_response = None
        for idx in range(len(history) - 1, -1, -1):
            msg = history[idx]
            if msg.get("role") != "tool":
                continue
            content = msg.get("content", "")
            if not isinstance(content, str):
                continue
            # Only accept tool results paired with a prior assistant todo call.
            if not self._tool_response_matches_todo_call(history, idx):
                continue
            if len(content) > MAX_TODO_RESULT_CHARS:
                _ra.logger.warning(
                    "Skipping oversized todo tool response during hydration: "
                    "session=%s chars=%d",
                    self.session_id or "none",
                    len(content),
                )
                continue
            # Quick check: todo responses contain "todos" key
            if '"todos"' not in content:
                continue
            try:
                data = json.loads(content)
                if "todos" in data and isinstance(data["todos"], list):
                    last_todo_response = data["todos"]
                    break
            except (json.JSONDecodeError, TypeError):
                continue

        if last_todo_response:
            # Replay the items into the store (replace mode)
            self._todo_store.write(last_todo_response, merge=False)
            if not self.quiet_mode:
                self._vprint(f"{self.log_prefix}📋 Restored {len(last_todo_response)} todo item(s) from history")
        _set_interrupt(False)

    @classmethod
    def _tool_response_matches_todo_call(
        cls,
        history: List[Dict[str, Any]],
        tool_index: int,
    ) -> bool:
        """Return True when a tool result belongs to a prior assistant todo call.

        Scans backwards from the tool result to the nearest assistant message
        and confirms it issued a ``todo`` tool call whose id matches this
        result's ``tool_call_id``. A ``user``/``system`` boundary (or a missing
        id) means the result is unpaired and must not hydrate the store.
        """
        if tool_index < 0 or tool_index >= len(history):
            return False
        tool_msg = history[tool_index]
        tool_call_id = tool_msg.get("tool_call_id")
        if not tool_call_id:
            return False

        for prior_idx in range(tool_index - 1, -1, -1):
            prior = history[prior_idx]
            role = prior.get("role")
            if role == "assistant":
                return cls._assistant_has_todo_tool_call(prior, tool_call_id)
            if role in {"user", "system"}:
                return False
        return False

    @classmethod
    def _assistant_has_todo_tool_call(
        cls,
        assistant_msg: Dict[str, Any],
        tool_call_id: str,
    ) -> bool:
        """True when the assistant message issued a ``todo`` call with this id."""
        tool_calls = assistant_msg.get("tool_calls")
        if not isinstance(tool_calls, list):
            return False

        for tool_call in tool_calls:
            if cls._get_tool_call_id_static(tool_call) != tool_call_id:
                continue
            if cls._get_tool_call_name_static(tool_call) == "todo":
                return True
        return False

