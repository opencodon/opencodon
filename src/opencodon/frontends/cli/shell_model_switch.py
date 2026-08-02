"""OpencodonCLI ShellModelSwitchMixin — extracted from shell.py (restructure Phase 4).

Verbatim method moves; the class is assembled in opencodon.frontends.cli.shell.
"""
#!/usr/bin/env python3
"""
opencodon CLI - Interactive Terminal Interface

A beautiful command-line interface for opencodon, inspired by Claude Code.
Features ASCII art branding, interactive REPL, toolset selection, and rich formatting.

Usage:
    python cli.py                          # Start interactive mode with all tools
    python cli.py --toolsets web,terminal  # Start with specific toolsets
    python cli.py --skills opencodon-dev,github-auth
    python cli.py --list-tools             # List available tools and exit
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

import logging
import copy
import os
import shutil
import sys
import json
import re
import concurrent.futures
import base64
import atexit
import errno
import tempfile
import time
import uuid
import textwrap
from collections import deque
from urllib.parse import unquote, urlparse
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

# Suppress startup messages for clean CLI experience
os.environ["OPENCODON_QUIET"] = "1"  # Our own modules

import yaml

from opencodon.core.fallback_config import get_fallback_chain
from opencodon.frontends.cli.cli_agent_setup_mixin import CLIAgentSetupMixin
from opencodon.frontends.cli.cli_commands_mixin import CLICommandsMixin

# prompt_toolkit for fixed input area TUI
from prompt_toolkit.history import FileHistory
from prompt_toolkit.styles import Style as PTStyle
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.application import Application
from prompt_toolkit.layout import Layout, HSplit, Window, FormattedTextControl, ConditionalContainer, WindowAlign
from prompt_toolkit.layout.processors import Processor, Transformation, PasswordProcessor, ConditionalProcessor
from prompt_toolkit.filters import Condition
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.widgets import TextArea
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit import print_formatted_text as _pt_print
from prompt_toolkit.formatted_text import ANSI as _PT_ANSI
try:
    from prompt_toolkit.cursor_shapes import CursorShape
    _STEADY_CURSOR = CursorShape.BLOCK  # Non-blinking block cursor
except (ImportError, AttributeError):
    _STEADY_CURSOR = None

try:
    from opencodon.frontends.cli.pt_input_extras import (
        install_ctrl_enter_alias,
        install_ignored_terminal_sequences,
        install_shift_enter_alias,
    )
    install_shift_enter_alias()
    install_ctrl_enter_alias()
    install_ignored_terminal_sequences()
    del install_shift_enter_alias, install_ctrl_enter_alias, install_ignored_terminal_sequences
except Exception:
    pass
import threading
import queue

def CanonicalUsage(*args, **kwargs):
    from opencodon.core.usage_pricing import CanonicalUsage as _CanonicalUsage

    return _CanonicalUsage(*args, **kwargs)


def estimate_usage_cost(*args, **kwargs):
    from opencodon.core.usage_pricing import estimate_usage_cost as _estimate_usage_cost

    return _estimate_usage_cost(*args, **kwargs)


def format_duration_compact(*args, **kwargs):
    seconds = float(args[0] if args else kwargs.get("seconds", 0.0))
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.0f}m"
    hours = minutes / 60
    if hours < 24:
        remaining_min = int(minutes % 60)
        return f"{int(hours)}h {remaining_min}m" if remaining_min else f"{int(hours)}h"
    days = hours / 24
    return f"{days:.1f}d"


# Cached reverse map of config.yaml ``model_aliases:`` so the TUI can show
# friendly names instead of full Palantir RIDs / long catalog IDs. Built
# lazily on first call; cache is process-lifetime (config is read once at
# session start, so further invalidation is unnecessary).
_REVERSE_ALIAS_CACHE: dict[str, str] | None = None


def _reverse_alias_for_display(model_name: str) -> str:
    """Return the shortest configured alias for ``model_name``, or ``model_name``.

    Looks up both ``model_aliases:`` (dict-based, full DirectAlias entries)
    and ``model.aliases:`` (string-based, set via ``opencodon config set``)
    from config.yaml. Multiple aliases pointing at the same model — the
    shortest wins, so ``opus47`` beats ``palantir-claude47``.
    """
    global _REVERSE_ALIAS_CACHE
    if not model_name:
        return model_name
    if _REVERSE_ALIAS_CACHE is None:
        rmap: dict[str, str] = {}
        try:
            from opencodon.config import load_config
            cfg = load_config() or {}
            ma = cfg.get("model_aliases")
            if isinstance(ma, dict):
                for alias, entry in ma.items():
                    if isinstance(entry, dict):
                        m = str(entry.get("model", "") or "").strip()
                        if m and (m not in rmap or len(alias) < len(rmap[m])):
                            rmap[m] = alias
            mdl = cfg.get("model", {}) or {}
            if isinstance(mdl, dict):
                simple = mdl.get("aliases")
                if isinstance(simple, dict):
                    for alias, val in simple.items():
                        if isinstance(val, str) and val.strip():
                            v = val.strip()
                            m = v.split("/", 1)[1] if "/" in v else v
                            if m and (m not in rmap or len(alias) < len(rmap[m])):
                                rmap[m] = alias
        except Exception:
            pass
        _REVERSE_ALIAS_CACHE = rmap
    return _REVERSE_ALIAS_CACHE.get(model_name, model_name)


def format_token_count_compact(*args, **kwargs):
    value = int(args[0] if args else kwargs.get("value", 0))
    abs_value = abs(value)
    if abs_value < 1_000:
        return str(value)

    sign = "-" if value < 0 else ""
    units = ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K"))
    for threshold, suffix in units:
        if abs_value >= threshold:
            scaled = abs_value / threshold
            if scaled < 10:
                text = f"{scaled:.2f}"
            elif scaled < 100:
                text = f"{scaled:.1f}"
            else:
                text = f"{scaled:.0f}"
            if "." in text:
                text = text.rstrip("0").rstrip(".")
            return f"{sign}{text}{suffix}"

    return f"{value:,}"


def is_table_divider(*args, **kwargs):
    from opencodon.core.markdown_tables import is_table_divider as _is_table_divider

    return _is_table_divider(*args, **kwargs)


def looks_like_table_row(*args, **kwargs):
    from opencodon.core.markdown_tables import looks_like_table_row as _looks_like_table_row

    return _looks_like_table_row(*args, **kwargs)


def realign_markdown_tables(*args, **kwargs):
    from opencodon.core.markdown_tables import realign_markdown_tables as _realign_markdown_tables

    return _realign_markdown_tables(*args, **kwargs)
# NOTE: `from agent.account_usage import ...` is deliberately NOT at module
# top — it transitively pulls the OpenAI SDK chain (~230 ms cold) and is only
# needed when the user runs `/limits`. Lazy-imported inside the handler below.
from opencodon.frontends.cli.banner import _format_context_length, format_banner_version_label

_COMMAND_SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")


# Load .env from ~/.opencodon/.env first, then project root as dev fallback.
# User-managed env files should override stale shell exports on restart.
from opencodon_constants import get_opencodon_home, display_opencodon_home
from opencodon.frontends.cli.browser_connect import (
    DEFAULT_BROWSER_CDP_URL,
    is_browser_debug_ready,
    manual_chrome_debug_command,
    try_launch_chrome_debug,
)
from opencodon.config.env_loader import load_opencodon_dotenv
from utils import base_url_host_matches, fast_safe_load

class _ShellProxy:
    """Late-binding accessor for shell module globals.

    Mixin methods read/write shell.py module state (so tests patching
    ``cli.<name>`` / ``shell.<name>`` keep working) without importing the
    module at mixin-import time (which would be circular in either
    direction).
    """

    def __getattr__(self, name):
        from opencodon.frontends.cli import shell
        return getattr(shell, name)

    def __setattr__(self, name, value):
        from opencodon.frontends.cli import shell
        setattr(shell, name, value)


_shell = _ShellProxy()


class ShellModelSwitchMixin:
    def _normalize_model_for_provider(self, resolved_provider: str) -> bool:
        """Normalize provider-specific model IDs and routing."""
        current_model = (self.model or "").strip()
        changed = False

        try:
            from opencodon.common.model_normalize import (
                _AGGREGATOR_PROVIDERS,
                normalize_model_for_provider,
            )

            if resolved_provider not in _AGGREGATOR_PROVIDERS:
                normalized_model = normalize_model_for_provider(current_model, resolved_provider)
                if normalized_model and normalized_model != current_model:
                    if not self._model_is_default:
                        self._console_print(
                            f"[yellow]⚠️  Normalized model '{current_model}' to '{normalized_model}' for {resolved_provider}.[/]"
                        )
                    self.model = normalized_model
                    current_model = normalized_model
                    changed = True
        except Exception:
            pass

        if resolved_provider == "copilot":
            try:
                from opencodon.core.models import copilot_model_api_mode, normalize_copilot_model_id

                canonical = normalize_copilot_model_id(current_model, api_key=self.api_key)
                if canonical and canonical != current_model:
                    if not self._model_is_default:
                        self._console_print(
                            f"[yellow]⚠️  Normalized Copilot model '{current_model}' to '{canonical}'.[/]"
                        )
                    self.model = canonical
                    current_model = canonical
                    changed = True

                resolved_mode = copilot_model_api_mode(current_model, api_key=self.api_key)
                if resolved_mode != self.api_mode:
                    self.api_mode = resolved_mode
                    changed = True
            except Exception:
                pass
            return changed

        if resolved_provider in {"opencode-zen", "opencode-go"}:
            try:
                from opencodon.core.models import normalize_opencode_model_id, opencode_model_api_mode

                canonical = normalize_opencode_model_id(resolved_provider, current_model)
                if canonical and canonical != current_model:
                    if not self._model_is_default:
                        self._console_print(
                            f"[yellow]⚠️  Stripped provider prefix from '{current_model}'; using '{canonical}' for {resolved_provider}.[/]"
                        )
                    self.model = canonical
                    current_model = canonical
                    changed = True

                resolved_mode = opencode_model_api_mode(resolved_provider, current_model)
                if resolved_mode != self.api_mode:
                    self.api_mode = resolved_mode
                    changed = True
            except Exception:
                pass
            return changed

        if resolved_provider != "openai-codex":
            return changed

        # 1. Strip provider prefix ("openai/gpt-5.4" → "gpt-5.4")
        if "/" in current_model:
            slug = current_model.split("/", 1)[1]
            if not self._model_is_default:
                self._console_print(
                    f"[yellow]⚠️  Stripped provider prefix from '{current_model}'; "
                    f"using '{slug}' for OpenAI Codex.[/]"
                )
            self.model = slug
            current_model = slug
            changed = True

        # 2. Replace untouched default with a Codex model
        if self._model_is_default:
            fallback_model = "gpt-5.3-codex"
            try:
                from opencodon.core.codex_models import get_codex_model_ids

                available = get_codex_model_ids(
                    access_token=self.api_key if self.api_key else None,
                )
                if available:
                    fallback_model = available[0]
            except Exception:
                pass

            if current_model != fallback_model:
                self.model = fallback_model
                changed = True

        return changed

    def _open_model_picker(self, providers: list, current_model: str, current_provider: str, user_provs=None, custom_provs=None) -> None:
        """Open prompt_toolkit-native /model picker modal."""
        self._capture_modal_input_snapshot()
        default_idx = next((i for i, p in enumerate(providers) if p.get("is_current")), 0)
        self._model_picker_state = {
            "stage": "provider",
            "providers": providers,
            "selected": default_idx,
            "current_model": current_model,
            "current_provider": current_provider,
            "user_provs": user_provs,
            "custom_provs": custom_provs,
        }
        self._invalidate(min_interval=0.0)

    def _confirm_expensive_model_switch(self, result) -> bool:
        """Ask for explicit confirmation before applying costly model switches."""
        if not getattr(result, "success", False):
            return True
        try:
            from opencodon.core.model_cost_guard import expensive_model_warning

            warning = expensive_model_warning(
                result.new_model,
                provider=result.target_provider,
                base_url=result.base_url or self.base_url or "",
                api_key=result.api_key or self.api_key or "",
                model_info=result.model_info,
            )
        except Exception:
            warning = None
        if warning is None:
            return True

        choices = [
            ("once", "Switch anyway", "Use this model for the current opencodon session."),
            ("cancel", "Cancel", "Keep the current model."),
        ]
        raw = self._prompt_text_input_modal(
            title="!!! Expensive Model Warning !!!",
            detail=warning.message,
            choices=choices,
            timeout=120,
        )
        choice = self._normalize_slash_confirm_choice(raw, choices)
        return choice == "once"

    def _confirm_and_apply_model_switch_result(
        self, result, persist_global: bool, custom_providers=None
    ) -> None:
        try:
            if result.success and not self._confirm_expensive_model_switch(result):
                _shell._cprint("  Model switch cancelled.")
                return
            self._apply_model_switch_result(
                result, persist_global, custom_providers=custom_providers
            )
        except Exception as exc:
            _shell._cprint(f"  ✗ Model selection failed: {exc}")

    def _close_model_picker(self) -> None:
        self._model_picker_state = None
        self._restore_modal_input_snapshot()
        self._invalidate(min_interval=0.0)

    def _snapshot_model_runtime(self) -> dict:
        """Capture current CLI and agent model runtime for one-turn restore."""
        agent = getattr(self, "agent", None)
        return {
            "model": self.model,
            "provider": self.provider,
            "requested_provider": self.requested_provider,
            "_explicit_api_key": getattr(self, "_explicit_api_key", None),
            "_explicit_base_url": getattr(self, "_explicit_base_url", None),
            "api_key": self.api_key,
            "base_url": self.base_url,
            "api_mode": self.api_mode,
            "agent_primary_runtime": copy.deepcopy(
                getattr(agent, "_primary_runtime", None)
            ) if agent is not None else None,
        }

    def _restore_model_runtime_snapshot(self, snapshot: dict | None) -> None:
        """Restore a model runtime captured before a one-turn override."""
        if not snapshot:
            return
        for key in (
            "model",
            "provider",
            "requested_provider",
            "_explicit_api_key",
            "_explicit_base_url",
            "api_key",
            "base_url",
            "api_mode",
        ):
            if key in snapshot:
                setattr(self, key, snapshot.get(key))

        agent = getattr(self, "agent", None)
        if agent is None:
            return

        primary = snapshot.get("agent_primary_runtime")
        if primary and hasattr(agent, "_restore_primary_runtime"):
            try:
                agent._primary_runtime = copy.deepcopy(primary)
                agent._fallback_activated = True
                agent._rate_limited_until = 0
                if agent._restore_primary_runtime():
                    return
            except Exception:
                _shell.logger.debug("CLI one-turn model restore via primary runtime failed", exc_info=True)

        if hasattr(agent, "switch_model"):
            try:
                agent.switch_model(
                    new_model=snapshot.get("model", ""),
                    new_provider=snapshot.get("provider", ""),
                    api_key=snapshot.get("api_key", ""),
                    base_url=snapshot.get("base_url", ""),
                    api_mode=snapshot.get("api_mode", ""),
                )
            except Exception as exc:
                _shell.logger.warning("CLI one-turn model restore failed: %s", exc)

    @staticmethod
    def _compute_model_picker_viewport(
        selected: int,
        scroll_offset: int,
        n: int,
        term_rows: int,
        reserved_below: int = 6,
        panel_chrome: int = 6,
        min_visible: int = 3,
    ) -> tuple[int, int]:
        """Resolve (scroll_offset, visible) for the /model picker viewport.

        ``reserved_below`` matches the approval / clarify panels — input area,
        status bar, and separators below the panel. ``panel_chrome`` covers
        this panel's own borders + blanks + hint row. The remaining rows hold
        the scrollable list, with the offset slid to keep ``selected`` on screen.
        """
        max_visible = max(min_visible, term_rows - reserved_below - panel_chrome)
        if n <= max_visible:
            return 0, n
        visible = max_visible
        if selected < scroll_offset:
            scroll_offset = selected
        elif selected >= scroll_offset + visible:
            scroll_offset = selected - visible + 1
        scroll_offset = max(0, min(scroll_offset, n - visible))
        return scroll_offset, visible

    def _clear_persisted_context_for_model_switch(self, result) -> None:
        """Drop a global context pin when its configured owner changes."""
        try:
            from opencodon.config import load_config_readonly
            from opencodon.common.route_identity import should_clear_context_pin

            config = load_config_readonly()
            model_cfg = config.get("model", {}) if isinstance(config, dict) else {}
            if not isinstance(model_cfg, dict) or "context_length" not in model_cfg:
                return
            if should_clear_context_pin(
                model_cfg.get("default") or model_cfg.get("model"),
                result.new_model,
                model_cfg.get("base_url"),
                result.base_url,
                model_cfg.get("provider"),
                result.target_provider,
            ):
                _shell.save_config_value("model.context_length", None)
        except Exception:
            _shell.save_config_value("model.context_length", None)

    def _handle_model_picker_selection(self, persist_global: bool = False) -> None:
        state = self._model_picker_state
        if not state:
            return
        selected = state.get("selected", 0)
        stage = state.get("stage")
        if stage == "provider":
            providers = state.get("providers") or []
            if selected >= len(providers):
                self._close_model_picker()
                return
            provider_data = providers[selected]
            # Use the curated model list from list_authenticated_providers()
            # (same lists as `opencodon model` and gateway pickers).
            # Only fall back to the live provider catalog when the curated
            # list is empty (e.g. user-defined endpoints with no curated list).
            model_list = provider_data.get("models", [])
            if not model_list:
                try:
                    from opencodon.core.models import provider_model_ids
                    live = provider_model_ids(provider_data["slug"])
                    if live:
                        model_list = live
                except Exception:
                    pass
            state["stage"] = "model"
            state["provider_data"] = provider_data
            state["model_list"] = model_list
            state["selected"] = 0
            self._invalidate(min_interval=0.0)
            return
        if stage == "model":
            provider_data = state.get("provider_data") or {}
            model_list = state.get("model_list") or []
            back_idx = len(model_list)
            cancel_idx = len(model_list) + 1
            if selected == back_idx:
                state["stage"] = "provider"
                state["selected"] = next((i for i, p in enumerate(state.get("providers") or []) if p.get("slug") == provider_data.get("slug")), 0)
                self._invalidate(min_interval=0.0)
                return
            if selected >= cancel_idx:
                self._close_model_picker()
                return
            if selected < len(model_list):
                from opencodon.frontends.cli.model_switch import switch_model
                chosen_model = model_list[selected]
                result = switch_model(
                    raw_input=chosen_model,
                    current_provider=self.provider or "",
                    current_model=self.model or "",
                    current_base_url=self.base_url or "",
                    current_api_key=self.api_key or "",
                    is_global=persist_global,
                    explicit_provider=provider_data.get("slug"),
                    user_providers=state.get("user_provs"),
                    custom_providers=state.get("custom_provs"),
                )
                # Capture before close — picker state is cleared on close.
                _picker_custom_provs = state.get("custom_provs")
                self._close_model_picker()
                if getattr(self, "_app", None):
                    _shell.threading.Thread(
                        target=self._confirm_and_apply_model_switch_result,
                        args=(result, persist_global, _picker_custom_provs),
                        daemon=True,
                    ).start()
                else:
                    self._confirm_and_apply_model_switch_result(
                        result, persist_global, custom_providers=_picker_custom_provs
                    )
                return
            self._close_model_picker()

    def _should_handle_model_command_inline(self, text: str, has_images: bool = False) -> bool:
        """Return True when /model should be handled immediately on the UI thread."""
        if not text or has_images or not _shell._looks_like_slash_command(text):
            return False
        try:
            from opencodon.frontends.cli.commands import resolve_command
            base = text.split(None, 1)[0].lower().lstrip('/')
            cmd = resolve_command(base)
            return bool(cmd and cmd.name == "model")
        except Exception:
            return False

