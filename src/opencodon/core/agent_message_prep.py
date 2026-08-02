"""AIAgent AgentMessagePrepMixin — extracted from run_agent.py (restructure Phase 4).

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


class AgentMessagePrepMixin:
    def _has_content_after_think_block(self, content: str) -> bool:
        """
        Check if content has actual text after any reasoning/thinking blocks.

        This detects cases where the model only outputs reasoning but no actual
        response, which indicates an incomplete generation that should be retried.
        Must stay in sync with _strip_think_blocks() tag variants.

        Args:
            content: The assistant message content to check

        Returns:
            True if there's meaningful content after think blocks, False otherwise
        """
        if not content:
            return False

        # Remove all reasoning tag variants (must match _strip_think_blocks)
        cleaned = self._strip_think_blocks(content)

        # Check if there's any non-whitespace content remaining
        return bool(cleaned.strip())

    def _strip_think_blocks(self, content: str) -> str:
        """Forwarder — see ``agent.agent_runtime_helpers.strip_think_blocks``."""
        from opencodon.core.agent_runtime_helpers import strip_think_blocks
        return strip_think_blocks(self, content)

    @staticmethod
    def _has_natural_response_ending(content: str) -> bool:
        """Heuristic: does visible assistant text look intentionally finished?"""
        if not content:
            return False
        stripped = content.rstrip()
        if not stripped:
            return False
        if stripped.endswith("```"):
            return True
        if stripped.endswith('^'):
            return True
        last = stripped[-1]
        if last in '.!?:)"\']}。！？：）】」』》^':
            return True
        # Emoji ranges (Misc Symbols, Dingbats, Emoticons, Supplemental, etc.)
        if ord(last) >= 0x1F300:
            return True
        return False

    def _extract_reasoning(self, assistant_message) -> Optional[str]:
        """Forwarder — see ``agent.agent_runtime_helpers.extract_reasoning``."""
        from opencodon.core.agent_runtime_helpers import extract_reasoning
        return extract_reasoning(self, assistant_message)

    @staticmethod
    def _sanitize_api_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Forwarder — see ``agent.agent_runtime_helpers.sanitize_api_messages``."""
        from opencodon.core.agent_runtime_helpers import sanitize_api_messages
        return sanitize_api_messages(messages)

    @staticmethod
    def _is_thinking_only_assistant(
        msg: Dict[str, Any],
        *,
        drop_codex_reasoning_items: bool = True,
    ) -> bool:
        """Return True if ``msg`` is an assistant turn whose only payload is reasoning.

        "Thinking-only" means the model emitted reasoning (``reasoning`` or
        ``reasoning_content``) but no visible text and no tool_calls. When sent
        back to providers that convert reasoning into thinking blocks (native
        Anthropic, OpenRouter Anthropic, third-party Anthropic-compatible
        gateways), the resulting message has only thinking blocks — which
        Anthropic rejects with HTTP 400 "The final block in an assistant
        message cannot be `thinking`."

        Symmetric with Claude Code's ``filterOrphanedThinkingOnlyMessages``
        (src/utils/messages.ts). We drop the whole turn from the API copy
        rather than fabricating stub text — the message log (UI transcript)
        keeps the reasoning block; only the wire copy is cleaned.
        """
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            return False
        if msg.get("tool_calls"):
            return False
        # Does it have any actual output?
        content = msg.get("content")
        if isinstance(content, str):
            if content.strip():
                return False
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    if block:  # non-empty non-dict string etc.
                        return False
                    continue
                btype = block.get("type")
                if btype in {"thinking", "redacted_thinking"}:
                    continue
                if btype == "text":
                    text = block.get("text", "")
                    if isinstance(text, str) and text.strip():
                        return False
                    continue
                # tool_use, image, document, etc. — real payload
                return False
        elif content is not None and content != "":
            return False
        # Content is empty-ish. Is there reasoning to make it thinking-only?
        reasoning = msg.get("reasoning_content") or msg.get("reasoning")
        if isinstance(reasoning, str) and reasoning.strip():
            return True
        # reasoning_details list form
        rd = msg.get("reasoning_details")
        if isinstance(rd, list) and rd:
            return True
        # Codex Responses stores encrypted reasoning state under a separate
        # assistant-message key. Treat only real reasoning items as
        # thinking-only; empty/junk lists should fall through to the generic
        # empty-turn handling instead of being dropped here.
        codex_items = msg.get("codex_reasoning_items")
        if drop_codex_reasoning_items and isinstance(codex_items, list):
            return any(
                isinstance(item, dict) and item.get("type") == "reasoning"
                for item in codex_items
            )
        return False

    @staticmethod
    def _drop_thinking_only_and_merge_users(
        messages: List[Dict[str, Any]],
        *,
        drop_codex_reasoning_items: bool = True,
    ) -> List[Dict[str, Any]]:
        """Forwarder — see ``agent.agent_runtime_helpers.drop_thinking_only_and_merge_users``."""
        from opencodon.core.agent_runtime_helpers import drop_thinking_only_and_merge_users
        return drop_thinking_only_and_merge_users(
            messages,
            drop_codex_reasoning_items=drop_codex_reasoning_items,
        )

    @staticmethod
    def _content_has_image_parts(content: Any) -> bool:
        if not isinstance(content, list):
            return False
        for part in content:
            if isinstance(part, dict) and part.get("type") in {"image_url", "input_image"}:
                return True
        return False

    def _describe_image_for_anthropic_fallback(self, image_url: str, role: str) -> str:
        cache_key = hashlib.sha256(str(image_url or "").encode("utf-8")).hexdigest()
        cached = self._anthropic_image_fallback_cache.get(cache_key)
        if cached:
            return cached

        role_label = {
            "assistant": "assistant",
            "tool": "tool result",
        }.get(role, "user")
        analysis_prompt = (
            "Describe everything visible in this image in thorough detail. "
            "Include any text, code, UI, data, objects, people, layout, colors, "
            "and any other notable visual information."
        )

        vision_source = str(image_url or "")
        cleanup_path: Optional[Path] = None
        if vision_source.startswith("data:"):
            vision_source, cleanup_path = self._materialize_data_url_for_vision(vision_source)

        description = ""
        try:
            from opencodon.tools.vision_tools import vision_analyze_tool

            result_json = asyncio.run(
                vision_analyze_tool(image_url=vision_source, user_prompt=analysis_prompt)
            )
            result = json.loads(result_json) if isinstance(result_json, str) else {}
            description = (result.get("analysis") or "").strip()
        except Exception as e:
            description = f"Image analysis failed: {e}"
        finally:
            if cleanup_path and cleanup_path.exists():
                try:
                    cleanup_path.unlink()
                except OSError:
                    pass

        if not description:
            description = "Image analysis failed."

        note = f"[The {role_label} attached an image. Here's what it contains:\n{description}]"
        if vision_source and not str(image_url or "").startswith("data:"):
            note += (
                f"\n[If you need a closer look, use vision_analyze with image_url: {vision_source}]"
            )

        self._anthropic_image_fallback_cache[cache_key] = note
        return note

    def _model_supports_vision(self) -> bool:
        """Return True if the active provider+model reports native vision.

        Used to decide whether to strip image content parts from API-bound
        messages (for non-vision models) or let the provider adapter handle
        them natively (for vision-capable models).

        Resolution order (see ``agent.image_routing._supports_vision_override``):
          1. ``model.supports_vision`` (top-level, single-model shortcut)
          2. ``providers.<provider>.models.<model>.supports_vision``
          3. models.dev capability lookup
        Custom/local models absent from models.dev would otherwise be
        misclassified as non-vision and have their images stripped.
        """
        try:
            from opencodon.config import load_config
            from opencodon.core.image_routing import _lookup_supports_vision
            cfg = load_config()
            provider = (getattr(self, "provider", "") or "").strip()
            model = (getattr(self, "model", "") or "").strip()
            return _lookup_supports_vision(provider, model, cfg) is True
        except Exception:
            return False

    def _provider_supports_vision_tool_messages(self) -> bool:
        """Return True if the active provider accepts list-type tool content.

        Some providers (e.g. Xiaomi MiMo) support multimodal user messages
        but reject list-type tool message content with 400 errors.  This
        checks the provider profile's ``supports_vision_tool_messages`` field.
        """
        try:
            from opencodon.providers import get_provider_profile
            provider = (getattr(self, "provider", "") or "").strip()
            profile = get_provider_profile(provider)
            if profile is not None:
                return getattr(profile, "supports_vision_tool_messages", True)
        except Exception:
            pass
        return True  # default: assume compatible

    def _preprocess_anthropic_content(self, content: Any, role: str) -> Any:
        if not self._content_has_image_parts(content):
            return content

        text_parts: List[str] = []
        image_notes: List[str] = []
        for part in content:
            if isinstance(part, str):
                if part.strip():
                    text_parts.append(part.strip())
                continue
            if not isinstance(part, dict):
                continue

            ptype = part.get("type")
            if ptype in {"text", "input_text"}:
                text = str(part.get("text", "") or "").strip()
                if text:
                    text_parts.append(text)
                continue

            if ptype in {"image_url", "input_image"}:
                image_data = part.get("image_url", {})
                image_url = image_data.get("url", "") if isinstance(image_data, dict) else str(image_data or "")
                if image_url:
                    image_notes.append(self._describe_image_for_anthropic_fallback(image_url, role))
                else:
                    image_notes.append("[An image was attached but no image source was available.]")
                continue

            text = str(part.get("text", "") or "").strip()
            if text:
                text_parts.append(text)

        prefix = "\n\n".join(note for note in image_notes if note).strip()
        suffix = "\n".join(text for text in text_parts if text).strip()
        if prefix and suffix:
            return f"{prefix}\n\n{suffix}"
        if prefix:
            return prefix
        if suffix:
            return suffix
        return "[A multimodal message was converted to text for Anthropic compatibility.]"

    def _get_transport(self, api_mode: str = None):
        """Return the cached transport for the given (or current) api_mode.

        Lazy-initializes on first call per api_mode. Returns None if no
        transport is registered for the mode.
        """
        mode = api_mode or self.api_mode
        cache = getattr(self, "_transport_cache", None)
        if cache is None:
            cache = {}
            self._transport_cache = cache
        t = cache.get(mode)
        if t is None:
            from opencodon.core.transports import get_transport
            t = get_transport(mode)
            cache[mode] = t
        return t

    def _prepare_anthropic_messages_for_api(self, api_messages: list) -> list:
        # Fast exit when no message carries image content at all.
        if not any(
            isinstance(msg, dict) and self._content_has_image_parts(msg.get("content"))
            for msg in api_messages
        ):
            return api_messages

        # The Anthropic adapter (agent/anthropic_adapter.py:_convert_content_part_to_anthropic)
        # already translates OpenAI-style image_url/input_image parts into
        # native Anthropic ``{"type": "image", "source": ...}`` blocks. When
        # the active model supports vision we let the adapter do its job and
        # skip this legacy text-fallback preprocessor entirely.
        if self._model_supports_vision():
            return api_messages

        # Non-vision Anthropic model (rare today, but keep the fallback for
        # compat): replace each image part with a vision_analyze text note.
        transformed = copy.deepcopy(api_messages)
        for msg in transformed:
            if not isinstance(msg, dict):
                continue
            msg["content"] = self._preprocess_anthropic_content(
                msg.get("content"),
                str(msg.get("role", "user") or "user"),
            )
        return transformed

    def _prepare_messages_for_non_vision_model(self, api_messages: list) -> list:
        """Strip native image parts when the active model lacks vision.

        Runs on the chat.completions / codex_responses paths. Vision-capable
        models pass through unchanged (provider and any downstream translator
        handle the image parts natively). Non-vision models get each image
        replaced by a cached vision_analyze text description so the turn
        doesn't fail with "model does not support image input".
        """
        if not any(
            isinstance(msg, dict) and self._content_has_image_parts(msg.get("content"))
            for msg in api_messages
        ):
            return api_messages

        if self._model_supports_vision():
            return api_messages

        transformed = copy.deepcopy(api_messages)
        for msg in transformed:
            if not isinstance(msg, dict):
                continue
            # Reuse the Anthropic text-fallback preprocessor — the behaviour is
            # identical (walk content parts, replace images with cached
            # descriptions, merge back into a single text or structured
            # content). Naming is historical.
            msg["content"] = self._preprocess_anthropic_content(
                msg.get("content"),
                str(msg.get("role", "user") or "user"),
            )
        return transformed

    def _tool_result_content_for_active_model(self, tool_name: str, result: Any) -> Any:
        """Return the tool message content that is safe for the active model.

        Multimodal tool results normally unwrap to OpenAI-style content parts so
        vision-capable models can inspect screenshots.  Text-only providers must
        not receive those image parts, because a rejected tool result becomes
        part of the canonical history and can make the next user turn fail before
        the agent has a chance to recover.
        """
        if not _is_multimodal_tool_result(result):
            return result

        content = result.get("content") or []
        if not self._content_has_image_parts(content):
            return content

        if self._model_supports_vision():
            # Vision-capable on paper — but if the provider rejects list-type
            # tool content (e.g. Xiaomi MiMo's 400 "text is not set"), or if
            # we've already learned this lesson in-session, short-circuit to
            # a text summary so we don't burn a round-trip relearning it.
            if not self._provider_supports_vision_tool_messages():
                _ra.logger.debug(
                    "Tool %s: provider %s does not accept list-type tool "
                    "content — sending text summary",
                    tool_name, getattr(self, "provider", ""),
                )
                return _multimodal_text_summary(result)
            key = (
                (getattr(self, "provider", "") or "").strip().lower(),
                (getattr(self, "model", "") or "").strip(),
            )
            no_list = getattr(self, "_no_list_tool_content_models", None)
            if no_list and key in no_list:
                _ra.logger.debug(
                    "Tool %s: model %s/%s known to reject list-type tool "
                    "content this session — sending text summary",
                    tool_name, key[0], key[1],
                )
                return _multimodal_text_summary(result)
            return content

        summary = _multimodal_text_summary(result)
        if tool_name == "computer_use":
            return json.dumps({
                "error": (
                    "computer_use returned screenshot/image content, but the active "
                    "model/provider does not support image input. Switch to a "
                    "vision-capable model for desktop computer use, or use browser "
                    "tools for browser tasks."
                ),
                "text_summary": summary,
            })

        _ra.logger.warning(
            "Tool %s returned image content for non-vision model %s/%s; "
            "falling back to text summary",
            tool_name,
            self.provider,
            self.model,
        )
        return summary

    def _try_shrink_image_parts_in_messages(
        self,
        api_messages: list,
        *,
        max_dimension: int = 8000,
    ) -> bool:
        """Forwarder — see ``agent.conversation_compression.try_shrink_image_parts_in_messages``."""
        from opencodon.core.conversation_compression import try_shrink_image_parts_in_messages
        return try_shrink_image_parts_in_messages(
            api_messages,
            max_dimension=max_dimension,
        )

    def _try_strip_image_parts_from_tool_messages(
        self,
        api_messages: list,
        *,
        remember_model: bool = True,
    ) -> bool:
        """Downgrade list-type tool messages to text summaries in-place.

        Recovery path for providers that reject list-type tool message content
        (e.g. Xiaomi MiMo's 400 "text is not set"; see issue #27344).  Walks
        ``api_messages`` for any ``role: "tool"`` message whose ``content`` is
        a list containing image parts, replaces the content with the existing
        text part(s) (or a minimal placeholder if none survive), and by default
        records the active (provider, model) in
        ``self._no_list_tool_content_models`` so subsequent
        ``_tool_result_content_for_active_model`` calls in this session
        preemptively downgrade screenshots without a round-trip.

        413 payload-size recovery passes ``remember_model=False`` because that
        error means this request body was too large, not that the provider/model
        rejects list-type tool content in general.

        Returns True when at least one tool message was downgraded — the
        caller (the 400 recovery branch in ``agent.conversation_loop``) uses
        this to decide whether to retry the API call with the modified
        history or surface the original error.
        """
        if not isinstance(api_messages, list):
            return False

        if remember_model:
            # Record (provider, model) so we don't relearn this lesson.
            key = (
                (getattr(self, "provider", "") or "").strip().lower(),
                (getattr(self, "model", "") or "").strip(),
            )
            if not hasattr(self, "_no_list_tool_content_models"):
                self._no_list_tool_content_models = set()
            if key[1]:  # only record when we actually have a model id
                self._no_list_tool_content_models.add(key)

        changed = False
        for msg in api_messages:
            if not isinstance(msg, dict) or msg.get("role") != "tool":
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue

            # Salvage any text parts so the model still sees some signal.
            text_parts: List[str] = []
            had_image = False
            for part in content:
                if not isinstance(part, dict):
                    if isinstance(part, str) and part.strip():
                        text_parts.append(part.strip())
                    continue
                ptype = part.get("type")
                if ptype == "image_url" or ptype == "input_image":
                    had_image = True
                    continue
                if ptype in {"text", "input_text"}:
                    text = str(part.get("text") or "").strip()
                    if text:
                        text_parts.append(text)

            if not had_image:
                # List-type content but no image parts — leave alone (some
                # providers reject ANY list content, but stripping a
                # text-only list doesn't reduce ambiguity; let the caller
                # surface the original error if this turns out to be the
                # case).
                continue

            if text_parts:
                msg["content"] = "\n\n".join(text_parts)
            else:
                msg["content"] = (
                    "[image content removed — provider does not accept "
                    "list-type tool message content]"
                )
            changed = True

        return changed

    def _anthropic_preserve_dots(self) -> bool:
        """True when using an anthropic-compatible endpoint that preserves dots in model names.
        Alibaba/DashScope keeps dots (e.g. qwen3.5-plus).
        MiniMax keeps dots (e.g. MiniMax-M2.7).
        Xiaomi MiMo keeps dots (e.g. mimo-v2.5, mimo-v2.5-pro).
        OpenCode Go/Zen keeps dots for non-Claude models (e.g. minimax-m2.5-free).
        ZAI/Zhipu keeps dots (e.g. glm-4.7, glm-5.1).
        AWS Bedrock uses dotted inference-profile IDs
        (e.g. ``global.anthropic.claude-opus-4-7``,
        ``us.anthropic.claude-sonnet-4-5-20250929-v1:0``) and rejects
        the hyphenated form with
        ``HTTP 400 The provided model identifier is invalid``.
        Regression for #11976; mirrors the opencode-go fix for #5211
        (commit f77be22c), which extended this same allowlist."""
        if (getattr(self, "provider", "") or "").lower() in {
            "alibaba", "minimax", "minimax-cn",
            "opencode-go", "opencode-zen",
            "zai", "bedrock",
            "xiaomi", "vertex",
        }:
            return True
        base = (getattr(self, "base_url", "") or "").lower()
        return (
            "dashscope" in base
            or "aliyuncs" in base
            or "minimax" in base
            or "opencode.ai/zen/" in base
            or "bigmodel.cn" in base
            or "xiaomimimo.com" in base
            # Vertex AI OpenAI-compat endpoint — Gemini model ids keep dots
            # (e.g. google/gemini-3.5-flash); the hyphenated form is wrong.
            or "aiplatform.googleapis.com" in base
            # AWS Bedrock runtime endpoints — defense-in-depth when
            # ``provider`` is unset but ``base_url`` still names Bedrock.
            or "bedrock-runtime." in base
        )

    def _is_qwen_portal(self) -> bool:
        """Return True when the base URL targets Qwen Portal."""
        return base_url_host_matches(self._base_url_lower, "portal.qwen.ai")

    def _qwen_prepare_chat_messages(self, api_messages: list) -> list:
        prepared = copy.deepcopy(api_messages)
        if not prepared:
            return prepared

        for msg in prepared:
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if isinstance(content, str):
                msg["content"] = [{"type": "text", "text": content}]
            elif isinstance(content, list):
                # Normalize: convert bare strings to text dicts, keep dicts as-is.
                # deepcopy already created independent copies, no need for dict().
                normalized_parts = []
                for part in content:
                    if isinstance(part, str):
                        normalized_parts.append({"type": "text", "text": part})
                    elif isinstance(part, dict):
                        normalized_parts.append(part)
                if normalized_parts:
                    msg["content"] = normalized_parts

        # Inject cache_control on the last part of the system message.
        for msg in prepared:
            if isinstance(msg, dict) and msg.get("role") == "system":
                content = msg.get("content")
                if isinstance(content, list) and content and isinstance(content[-1], dict):
                    content[-1]["cache_control"] = {"type": "ephemeral"}
                break

        return prepared

    def _qwen_prepare_chat_messages_inplace(self, messages: list) -> None:
        """In-place variant — mutates an already-copied message list."""
        if not messages:
            return

        for msg in messages:
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if isinstance(content, str):
                msg["content"] = [{"type": "text", "text": content}]
            elif isinstance(content, list):
                normalized_parts = []
                for part in content:
                    if isinstance(part, str):
                        normalized_parts.append({"type": "text", "text": part})
                    elif isinstance(part, dict):
                        normalized_parts.append(part)
                if normalized_parts:
                    msg["content"] = normalized_parts

        for msg in messages:
            if isinstance(msg, dict) and msg.get("role") == "system":
                content = msg.get("content")
                if isinstance(content, list) and content and isinstance(content[-1], dict):
                    content[-1]["cache_control"] = {"type": "ephemeral"}
                break

    def _build_api_kwargs(self, api_messages: list) -> dict:
        """Forwarder — see ``agent.chat_completion_helpers.build_api_kwargs``."""
        from opencodon.core.chat_completion_helpers import build_api_kwargs
        return build_api_kwargs(self, api_messages)

    def _supports_reasoning_extra_body(self) -> bool:
        """Return True when reasoning extra_body is safe to send for this route/model.

        OpenRouter forwards unknown extra_body fields to upstream providers.
        Some providers/routes reject `reasoning` with 400s, so gate it to
        known reasoning-capable model families.
        """
        if (
            base_url_host_matches(self._base_url_lower, "models.github.ai")
            or base_url_host_matches(self._base_url_lower, "githubcopilot.com")
        ):
            try:
                from opencodon.core.models import github_model_reasoning_efforts

                return bool(github_model_reasoning_efforts(self.model))
            except Exception:
                return False
        if (self.provider or "").strip().lower() == "lmstudio":
            opts = self._lmstudio_reasoning_options_cached()
            # "off-only" (or absent) means no real reasoning capability.
            return any(opt and opt != "off" for opt in opts)
        # Ollama Cloud (and any Ollama-compatible server): the native
        # /api/show capabilities list is authoritative — emit reasoning_effort
        # only for models that declare the "thinking" capability. deepseek-v4
        # has it; gemma3 / qwen3-coder don't. Cached per (model, base_url).
        if base_url_host_matches(self._base_url_lower, "ollama.com"):
            return self._ollama_supports_thinking_cached()
        if "openrouter" not in self._base_url_lower:
            return False
        if "api.mistral.ai" in self._base_url_lower:
            return False

        model = (self.model or "").lower()
        reasoning_model_prefixes = (
            "deepseek/",
            "anthropic/",
            "openai/",
            "x-ai/",
            "google/gemini-2",
            "google/gemma-4",
            "qwen/qwen3",
            "tencent/hy3",
            "xiaomi/",
        )
        return any(model.startswith(prefix) for prefix in reasoning_model_prefixes)

    def _lmstudio_reasoning_options_cached(self) -> list[str]:
        """Probe LM Studio's published reasoning ``allowed_options`` once per
        (model, base_url). The list (e.g. ``["off","on"]`` or
        ``["off","minimal","low"]``) is needed both for the supports-reasoning
        gate and for clamping the emitted ``reasoning_effort`` so toggle-style
        models don't 400 on ``high``. Cache is keyed on (model, base_url) so
        ``/model`` swaps and base-URL changes don't reuse a stale list.
        Non-empty results are cached permanently (model capabilities don't
        change). Empty results (transient probe failure OR genuinely
        non-reasoning model) are cached with a 60-second TTL to avoid an
        HTTP round-trip on every turn while still retrying reasonably soon.
        """
        import time as _time

        cache = getattr(self, "_lm_reasoning_opts_cache", None)
        if cache is None:
            cache = self._lm_reasoning_opts_cache = {}
        key = (self.model, self.base_url)
        cached = cache.get(key)
        if cached is not None:
            opts, ts = cached
            # Non-empty → permanent. Empty → 60s TTL.
            if opts or (_time.monotonic() - ts) < 60:
                return opts
        try:
            from opencodon.core.models import lmstudio_model_reasoning_options
            opts = lmstudio_model_reasoning_options(
                self.model, self.base_url, getattr(self, "api_key", ""),
            )
        except Exception:
            opts = []
        cache[key] = (opts, _time.monotonic())
        return opts

    def _ollama_supports_thinking_cached(self) -> bool:
        """Probe Ollama's ``/api/show`` capabilities once per (model, base_url).

        Returns True only when the model declares the ``thinking`` capability.
        Caching mirrors the LM Studio probe: a True/False result is permanent
        (capabilities don't change), while a probe failure (None) is cached
        with a 60-second TTL so a transient outage doesn't suppress reasoning
        for the rest of the session but also doesn't round-trip every turn.
        """
        import time as _time

        cache = getattr(self, "_ollama_thinking_cache", None)
        if cache is None:
            cache = self._ollama_thinking_cache = {}
        key = (self.model, self.base_url)
        cached = cache.get(key)
        if cached is not None:
            supported, ts = cached
            # Definitive True/False → permanent. Unknown (None) → 60s TTL.
            if supported is not None or (_time.monotonic() - ts) < 60:
                return bool(supported)
        try:
            from opencodon.core.models import ollama_model_supports_thinking
            supported = ollama_model_supports_thinking(
                self.model, self.base_url, getattr(self, "api_key", "")
            )
        except Exception:
            supported = None
        cache[key] = (supported, _time.monotonic())
        return bool(supported)

    def _resolve_lmstudio_summary_reasoning_effort(self) -> Optional[str]:
        """Resolve a safe top-level ``reasoning_effort`` for LM Studio.

        The iteration-limit summary path calls ``chat.completions.create()``
        directly, bypassing the transport. Share the helper so the two paths
        can't drift on effort resolution and clamping.
        """
        from opencodon.core.lmstudio_reasoning import resolve_lmstudio_effort
        return resolve_lmstudio_effort(
            self.reasoning_config,
            self._lmstudio_reasoning_options_cached(),
        )

    def _github_models_reasoning_extra_body(self) -> dict | None:
        """Format reasoning payload for GitHub Models/OpenAI-compatible routes."""
        try:
            from opencodon.core.models import github_model_reasoning_efforts
        except Exception:
            return None

        supported_efforts = github_model_reasoning_efforts(self.model)
        if not supported_efforts:
            return None

        if self.reasoning_config and isinstance(self.reasoning_config, dict):
            if self.reasoning_config.get("enabled") is False:
                return None
            requested_effort = str(
                self.reasoning_config.get("effort", "medium")
            ).strip().lower()
        else:
            requested_effort = "medium"

        if requested_effort == "xhigh" and "xhigh" not in supported_efforts and "high" in supported_efforts:
            requested_effort = "high"
        elif requested_effort not in supported_efforts:
            if requested_effort == "minimal" and "low" in supported_efforts:
                requested_effort = "low"
            elif "medium" in supported_efforts:
                requested_effort = "medium"
            else:
                requested_effort = supported_efforts[0]

        return {"effort": requested_effort}

    def _build_assistant_message(self, assistant_message, finish_reason: str) -> dict:
        """Forwarder — see ``agent.chat_completion_helpers.build_assistant_message``."""
        from opencodon.core.chat_completion_helpers import build_assistant_message
        return build_assistant_message(self, assistant_message, finish_reason)

    def _needs_thinking_reasoning_pad(self) -> bool:
        """Return True when the active provider enforces reasoning_content echo-back.

        DeepSeek v4 thinking and Kimi / Moonshot thinking both reject replays
        of assistant tool-call messages that omit ``reasoning_content`` (refs
        #15250, #17400). Xiaomi MiMo thinking mode has the same requirement.

        Result cached on the AIAgent instance keyed by (provider, model,
        base_url); invalidated whenever ``switch_model()`` /
        ``_try_activate_fallback()`` mutate any of those. This is hot — the
        agent loop hits ~16 invocations per turn, each of which would
        otherwise re-run ~5 ``base_url_host_matches`` (and therefore
        ``urlparse``) calls under it. Caching drops the per-turn cost from
        ~5us × 16 = ~80us to <1us.
        """
        key = (self.provider, self.model, getattr(self, "_base_url_lower", self.base_url))
        cached = getattr(self, "_thinking_pad_cache", None)
        if cached is not None and cached[0] == key:
            return cached[1]
        result = (
            self._needs_deepseek_tool_reasoning()
            or self._needs_kimi_tool_reasoning()
            or self._needs_mimo_tool_reasoning()
        )
        self._thinking_pad_cache = (key, result)
        return result

    def _needs_kimi_tool_reasoning(self) -> bool:
        """Return True when the current provider is Kimi / Moonshot thinking mode.

        Kimi ``/coding`` and Moonshot thinking mode both require
        ``reasoning_content`` on every assistant tool-call message; omitting
        it causes the next replay to fail with HTTP 400.

        Detection is host-driven, not model-name-driven: aggregators like
        OpenRouter that re-export Kimi/Moonshot models speak their own
        protocol and reject ``reasoning_content`` echoes. We only enable the
        kimi-reasoning replay when the request actually targets a
        kimi/moonshot endpoint or the dedicated kimi-coding provider.
        """
        return (
            self.provider in {"kimi-coding", "kimi-coding-cn"}
            or base_url_host_matches(self.base_url, "api.kimi.com")
            or base_url_host_matches(self.base_url, "moonshot.ai")
            or base_url_host_matches(self.base_url, "moonshot.cn")
        )

    def _needs_deepseek_tool_reasoning(self) -> bool:
        """Return True when the current provider is DeepSeek thinking mode.

        DeepSeek V4 thinking mode requires ``reasoning_content`` on every
        assistant tool-call turn; omitting it causes HTTP 400 when the
        message is replayed in a subsequent API request (#15250).
        """
        provider = (self.provider or "").lower()
        model = (self.model or "").lower()
        return (
            provider == "deepseek"
            or "deepseek" in model
            or base_url_host_matches(self.base_url, "api.deepseek.com")
        )

    def _needs_mimo_tool_reasoning(self) -> bool:
        """Return True when the current provider is Xiaomi MiMo thinking mode.

        MiMo thinking mode requires ``reasoning_content`` on every assistant
        tool-call message when replaying history; omitting it causes HTTP 400.
        Refs: https://platform.xiaomimimo.com/docs/zh-CN/usage-guide/passing-back-reasoning_content
        """
        provider = (self.provider or "").lower()
        model = (self.model or "").lower()
        return (
            provider == "xiaomi"
            or "mimo" in model
            or base_url_host_matches(self.base_url, "api.xiaomimimo.com")
            or base_url_host_matches(self.base_url, "xiaomimimo.com")
        )

    def _copy_reasoning_content_for_api(self, source_msg: dict, api_msg: dict) -> None:
        """Forwarder — see ``agent.agent_runtime_helpers.copy_reasoning_content_for_api``."""
        from opencodon.core.agent_runtime_helpers import copy_reasoning_content_for_api
        return copy_reasoning_content_for_api(self, source_msg, api_msg)

    def _reapply_reasoning_echo_for_provider(self, api_messages: list) -> int:
        """Forwarder — see ``agent.agent_runtime_helpers.reapply_reasoning_echo_for_provider``."""
        from opencodon.core.agent_runtime_helpers import reapply_reasoning_echo_for_provider
        return reapply_reasoning_echo_for_provider(self, api_messages)

    @staticmethod
    def _sanitize_tool_calls_for_strict_api(api_msg: dict, model: "str | None" = None) -> dict:
        """Strip Codex Responses API fields from tool_calls for strict providers.

        Providers like Mistral, Fireworks, and other strict OpenAI-compatible APIs
        validate the Chat Completions schema and reject unknown fields (call_id,
        response_item_id) with 400 or 422 errors. These fields are preserved in
        the internal message history — this method only modifies the outgoing
        API copy.

        ``extra_content`` (Gemini thought_signature) is also stripped — strict
        providers reject it with "Extra inputs are not permitted" — UNLESS the
        outgoing ``model`` is itself Gemini-family, in which case it must be
        replayed (Gemini 3 thinking models 400 without it). Defaults to
        stripping when no model is supplied.

        Creates new tool_call dicts rather than mutating in-place, so the
        original messages list retains call_id/response_item_id for Codex
        Responses API compatibility (e.g. if the session falls back to a
        Codex provider later).

        Fields stripped: call_id, response_item_id, extra_content (model-gated)
        """
        tool_calls = api_msg.get("tool_calls")
        if not isinstance(tool_calls, list):
            return api_msg
        from opencodon.core.transports.chat_completions import _model_consumes_thought_signature
        _STRIP_KEYS = {"call_id", "response_item_id"}
        if not _model_consumes_thought_signature(model):
            _STRIP_KEYS = _STRIP_KEYS | {"extra_content"}
        api_msg["tool_calls"] = [
            {k: v for k, v in tc.items() if k not in _STRIP_KEYS}
            if isinstance(tc, dict) else tc
            for tc in tool_calls
        ]
        return api_msg

    @staticmethod
    def _sanitize_tool_call_arguments(
        messages: list,
        *,
        logger=None,
        session_id: str = None,
    ) -> int:
        """Forwarder — see ``agent.agent_runtime_helpers.sanitize_tool_call_arguments``."""
        from opencodon.core.agent_runtime_helpers import sanitize_tool_call_arguments
        return sanitize_tool_call_arguments(messages, logger=_ra.logger, session_id=session_id)

    def _should_sanitize_tool_calls(self) -> bool:
        """Determine if tool_calls need sanitization for strict APIs.

        Codex Responses API uses fields like call_id and response_item_id
        that are not part of the standard Chat Completions schema. These
        fields must be stripped when calling any other API to avoid
        validation errors (400 Bad Request).

        Returns:
            bool: True if sanitization is needed (non-Codex API), False otherwise.
        """
        return self.api_mode != "codex_responses"

