"""OpencodonCLI ShellSessionUXMixin — extracted from shell.py (restructure Phase 4).

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

from opencodon.core.providers.fallback_config import get_fallback_chain
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
    from opencodon.core.providers.usage_pricing import CanonicalUsage as _CanonicalUsage

    return _CanonicalUsage(*args, **kwargs)


def estimate_usage_cost(*args, **kwargs):
    from opencodon.core.providers.usage_pricing import estimate_usage_cost as _estimate_usage_cost

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


class ShellSessionUXMixin:
    def _restore_session_cwd(self, session_meta: dict, *, quiet: bool = False) -> None:
        """Relaunch a resumed session in the directory it was started from.

        Idempotent and safe to call from every resume path. When the stored
        ``cwd`` differs from the current process directory, we both
        ``_shell.os.chdir()`` (so the process and any ``_shell.os.getcwd()`` fallback agree)
        and retarget ``TERMINAL_CWD`` (so the terminal tool, code-exec tool,
        and relative-path resolution all land in the same place — the local
        terminal backend snapshots cwd on first use, which happens after this).

        No-ops when: the session recorded no cwd (gateway/remote/older
        sessions), the directory no longer exists, or we're already there.
        A missing directory degrades to a single dim warning rather than a
        crash — repos get moved and deleted.
        """
        recorded = (session_meta or {}).get("cwd")
        if not recorded:
            return
        recorded = _shell.os.path.expanduser(str(recorded))
        try:
            current = _shell.os.getcwd()
        except OSError:
            current = None
        if current and _shell.os.path.realpath(recorded) == _shell.os.path.realpath(current):
            return  # Already where the session lived — nothing to announce.

        if not _shell.os.path.isdir(recorded):
            msg = f"⚠ Session's working directory is gone: {recorded} — staying in {current or '.'}"
            if quiet:
                print(msg, file=sys.stderr)
            else:
                self._console_print(f"[dim]{_shell._escape(msg)}[/dim]")
            return

        try:
            _shell.os.chdir(recorded)
        except OSError as e:
            msg = f"⚠ Could not enter session's working directory {recorded}: {e}"
            if quiet:
                print(msg, file=sys.stderr)
            else:
                self._console_print(f"[dim]{_shell._escape(msg)}[/dim]")
            return

        # Retarget the terminal/code-exec tools to match the process cwd.
        _shell.os.environ["TERMINAL_CWD"] = recorded

        msg = f"↻ Working directory: {recorded}"
        if quiet:
            print(msg, file=sys.stderr)
        else:
            self._console_print(f"[dim]{_shell._escape(msg)}[/dim]")

    def _render_resume_history_panel_lines(self, panel) -> list[str]:
        """Render the resume panel at the current terminal width for resize replay."""
        from io import StringIO

        buf = StringIO()
        width = _shell.shutil.get_terminal_size((80, 24)).columns
        console = _shell.Console(
            file=buf,
            force_terminal=True,
            color_system="truecolor",
            highlight=False,
            width=width,
        )
        with _shell._suspend_output_history():
            console.print(panel)
        return buf.getvalue().rstrip("\n").splitlines()

    def _resolve_checkpoint_ref(self, ref: str, checkpoints: list) -> str | None:
        """Resolve a checkpoint number or hash to a full commit hash."""
        try:
            idx = int(ref) - 1  # 1-indexed for user
            if 0 <= idx < len(checkpoints):
                return checkpoints[idx]["hash"]
            else:
                print(f"  Invalid checkpoint number. Use 1-{len(checkpoints)}.")
                return None
        except ValueError:
            # Treat as a git hash
            return ref

    def _notify_session_boundary(self, event_type: str) -> None:
        """Fire a session-boundary plugin hook (on_session_finalize or on_session_reset).

        Non-blocking — errors are caught and logged.  Safe to call from any
        lifecycle point (shutdown, /new, /reset).
        """
        try:
            from opencodon.plugins_runtime import invoke_hook as _invoke_hook
            _invoke_hook(
                event_type,
                session_id=self.agent.session_id if self.agent else None,
                platform=getattr(self, "platform", None) or "cli",
                reason="new_session" if event_type == "on_session_reset" else "session_boundary",
            )
        except Exception:
            pass

    def _discard_session_if_empty(self, session_id: Optional[str]) -> bool:
        """Drop a just-ended session row when it never gained content.

        Starting the CLI and immediately quitting (or rotating with /new,
        /clear) used to leave an empty untitled row behind that clutters
        ``/resume`` and ``opencodon sessions list``. Delegates the
        check-and-delete to ``SessionDB.delete_session_if_empty``, which
        only removes rows with no messages, no title, and no child
        sessions. Ported from google-gemini/gemini-cli#27770.
        """
        if not self._session_db or not session_id:
            return False
        # In-memory transcript is authoritative: if this CLI object holds
        # conversation messages (flushed to the DB or not), the session is
        # not empty. Protects against pruning a real conversation whose DB
        # flush failed or hasn't happened yet.
        if getattr(self, "conversation_history", None):
            return False
        try:
            from opencodon_constants import get_opencodon_home as _ghh
            return self._session_db.delete_session_if_empty(
                session_id, sessions_dir=_ghh() / "sessions"
            )
        except Exception:
            _shell.logger.debug(
                "Could not prune empty session %s", session_id, exc_info=True
            )
            return False

    def _launch_session_boundary_memory_flush(
        self,
        history_snapshot: list,
        *,
        session_id: Optional[str] = None,
    ) -> Optional[list]:
        """Stage old-session memory extraction so /new stays responsive.

        The context-engine ``on_session_end`` boundary is delivered
        synchronously here: it is cheap (local state clear, no LLM call) and
        ordering-sensitive — it must land before ``reset_session_state()``
        rebinds the engine to the new session.

        The memory-provider half (LLM-bound extraction, seconds) is NOT run
        here. The returned snapshot is handed by ``new_session()`` to
        ``MemoryManager.commit_session_boundary_async`` as a single
        end→switch task on the manager's serialized background worker, so
        extraction can never race the provider rebinding (providers key off
        internal ``_session_id`` state — a late ``on_session_end`` after
        ``on_session_switch`` would misattribute the old transcript to the
        new session).

        Returns the history snapshot to queue, or ``None`` when there is
        nothing to extract (no agent / empty history / no memory manager).
        """
        agent = getattr(self, "agent", None)
        if not agent or not history_snapshot:
            return None

        engine = getattr(agent, "context_compressor", None)
        if engine is not None and hasattr(engine, "on_session_end"):
            try:
                engine.on_session_end(session_id or "", history_snapshot)
            except Exception:
                _shell.logger.debug(
                    "Context engine on_session_end failed at /new boundary",
                    exc_info=True,
                )

        # No provider extraction to queue when no memory manager is
        # configured — new_session() falls back to the inline switch path.
        if getattr(agent, "_memory_manager", None) is None:
            return None
        return history_snapshot

    def new_session(self, silent=False, title=None):
        """Start a fresh session with a new session ID and cleared agent state."""
        old_session_id = self.session_id
        _boundary_snapshot = None
        if self.agent and self.conversation_history:
            # Deliver the context-engine boundary synchronously and get back
            # the history snapshot for the deferred provider extraction —
            # queued below (after rotation) so /new never blocks on the
            # LLM-bound extraction call.
            _boundary_snapshot = self._launch_session_boundary_memory_flush(
                list(self.conversation_history),
                session_id=old_session_id,
            )
            self._notify_session_boundary("on_session_finalize")
        elif self.agent:
            # First session or empty history — still finalize the old session
            self._notify_session_boundary("on_session_finalize")

        if self._session_db and old_session_id:
            # Flush any un-persisted messages from the current turn to the
            # old session *before* rotating.  /new can be called mid-turn
            # when _flush_messages_to_session_db() has not yet run — without
            # this, messages generated during the current turn are silently
            # lost on session rotation (#47202).
            if self.agent:
                try:
                    self.agent._flush_messages_to_session_db(
                        self.conversation_history,
                        conversation_history=self.conversation_history,
                    )
                except Exception:
                    pass  # best-effort
            try:
                self._session_db.end_session(old_session_id, "new_session")
            except Exception:
                pass
            # Don't let immediately-rotated empty sessions pile up in
            # /resume and `opencodon sessions list` (gemini-cli#27770 port).
            self._discard_session_if_empty(old_session_id)

        self.session_start = _shell.datetime.now()
        timestamp_str = self.session_start.strftime("%Y%m%d_%H%M%S")
        short_uuid = uuid.uuid4().hex[:6]
        self.session_id = f"{timestamp_str}_{short_uuid}"
        self.conversation_history = []
        self._pending_title = None
        self._resumed = False
        self.reasoning_config = _shell._parse_reasoning_config(
            _shell.CLI_CONFIG["agent"].get("reasoning_effort", "")
        )
        # /new is a full conversation boundary: session-scoped runtime
        # overrides (/model --session, /fast, one-turn restores) do not carry
        # forward.  Re-derive model/provider and service tier from config.yaml
        # so a session-only switch never leaks into the next session (#48055,
        # #23131).
        self._pending_one_turn_model_restore = None
        self.service_tier = _shell._parse_service_tier_config(
            _shell.CLI_CONFIG["agent"].get("service_tier", "")
        )
        _model_config = _shell.CLI_CONFIG.get("model", {})
        _config_model = (
            (_model_config.get("default") or _model_config.get("model") or "")
            if isinstance(_model_config, dict)
            else (_model_config or "")
        )
        if _config_model and _config_model != getattr(self, "model", None):
            _config_provider = (
                _model_config.get("provider", "")
                if isinstance(_model_config, dict)
                else ""
            )
            try:
                from opencodon.frontends.cli.model_switch import switch_model as _switch_model

                _reset_result = _switch_model(
                    raw_input=_config_model,
                    current_provider=self.provider or "",
                    current_model=self.model or "",
                    current_base_url=self.base_url or "",
                    current_api_key=self.api_key or "",
                    is_global=False,
                    explicit_provider=_config_provider or "",
                )
                if _reset_result.success:
                    if self.agent:
                        self.agent.switch_model(
                            new_model=_reset_result.new_model,
                            new_provider=_reset_result.target_provider,
                            api_key=_reset_result.api_key,
                            base_url=_reset_result.base_url,
                            api_mode=_reset_result.api_mode,
                        )
                    self.model = _reset_result.new_model
                    self.provider = _reset_result.target_provider
                    self.requested_provider = _reset_result.target_provider
                    self._explicit_api_key = _reset_result.api_key
                    self._explicit_base_url = _reset_result.base_url
                    if _reset_result.api_key:
                        self.api_key = _reset_result.api_key
                    if _reset_result.base_url:
                        self.base_url = _reset_result.base_url
                    if _reset_result.api_mode:
                        self.api_mode = _reset_result.api_mode
                    if not silent:
                        _shell._cprint(
                            f"  (model reset to config default: "
                            f"{_reset_result.new_model})"
                        )
            except Exception:
                # Best-effort: an unreachable config default must never block
                # /new. The session keeps the current working model.
                _shell.logger.debug("/new model reset to config default failed", exc_info=True)
        _shell._sync_process_session_id(self.session_id)

        if self.agent:
            self.agent.session_id = self.session_id
            self.agent.session_start = self.session_start
            self.agent.reasoning_config = self.reasoning_config
            self.agent.reset_session_state()
            if hasattr(self.agent, "_last_flushed_db_idx"):
                self.agent._last_flushed_db_idx = 0
            if hasattr(self.agent, "_todo_store"):
                try:
                    from opencodon.tools.todo_tool import TodoStore
                    self.agent._todo_store = TodoStore()
                except Exception:
                    pass
            if hasattr(self.agent, "_invalidate_system_prompt"):
                self.agent._invalidate_system_prompt()

            if self._session_db:
                try:
                    self.agent._session_db_created = False
                    self._session_db.create_session(
                        session_id=self.session_id,
                        source=_shell.os.environ.get("OPENCODON_SESSION_SOURCE", "cli"),
                        model=self.model,
                        model_config={
                            "max_iterations": self.max_turns,
                            "reasoning_config": self.reasoning_config,
                        },
                    )
                    self.agent._session_db_created = True
                except Exception:
                    pass
                if title and self._session_db:
                    from opencodon.state import SessionDB
                    try:
                        sanitized = SessionDB.sanitize_title(title)
                    except ValueError as e:
                        _shell._cprint(f"  Title rejected: {e}")
                        sanitized = None
                        title = None
                    if sanitized:
                        try:
                            self._session_db.set_session_title(self.session_id, sanitized)
                            self._pending_title = None
                            title = sanitized
                        except ValueError as e:
                            _shell._cprint(f"  {e} — session started untitled.")
                            title = None
                        except Exception:
                            title = None
                    elif title is not None:
                        # sanitize_title returned empty (whitespace-only / unprintable)
                        _shell._cprint("  Title is empty after cleanup — session started untitled.")
                        title = None
            # Notify memory providers that session_id rotated to a fresh
            # conversation. reset=True signals providers to flush accumulated
            # per-session state (_session_turns, _turn_counter, _document_id).
            # Fires BEFORE the plugin on_session_reset hook (shell hooks only
            # see the new id; Python providers see the transition). See #6672.
            #
            # When the old session has history, end-of-session extraction
            # (LLM-bound, seconds) and this switch are queued as ONE task on
            # the memory manager's serialized worker — end strictly before
            # switch, without blocking /new (#16454). With no history there
            # is nothing to extract; switch inline as before.
            try:
                _mm = getattr(self.agent, "_memory_manager", None)
                if _mm is not None:
                    if _boundary_snapshot:
                        _mm.commit_session_boundary_async(
                            _boundary_snapshot,
                            new_session_id=self.session_id,
                            parent_session_id=old_session_id or "",
                            reason="new_session",
                        )
                    else:
                        _mm.on_session_switch(
                            self.session_id,
                            parent_session_id=old_session_id or "",
                            reset=True,
                            reason="new_session",
                        )
            except Exception:
                pass
            self._notify_session_boundary("on_session_reset")

        if not silent:
            if title:
                print(f"(^_^)v New session started: {title}")
            else:
                print("(^_^)v New session started!")

    def _consume_pending_resume_selection(self, text: str) -> bool:
        """Resolve a bare numeric reply that follows a bare ``/resume`` prompt.

        After ``/resume`` (no args) prints the recent-sessions list it arms
        ``self._pending_resume_sessions``. The next submitted input is given
        one chance to be a bare session number (``3``); if so we resume that
        session here. Anything else (another command, free text, blank) simply
        disarms the prompt and is handled normally by the caller.

        Returns True if the input was consumed as a resume selection (caller
        must not treat it as chat); False otherwise. The pending state is
        always one-shot: it is cleared on the first submitted input regardless
        of outcome. See #34584.
        """
        pending = self._pending_resume_sessions
        if not pending:
            return False
        # One-shot: disarm now so a non-matching input can't leave the prompt
        # armed and hijack a later number the user meant as chat.
        self._pending_resume_sessions = None

        if not isinstance(text, str):
            return False
        stripped = text.strip()
        # Only a pure number selects; let "/resume 3", titles, or any other
        # text fall through to normal handling.
        if not stripped.isdigit():
            return False

        index = int(stripped)
        if index < 1 or index > len(pending):
            _shell._cprint(f"  Resume index {index} is out of range.")
            _shell._cprint("  Use /resume with no arguments to see available sessions.")
            return True

        self._handle_resume_command(f"/resume {index}")
        return True

    def save_conversation(self):
        """Save the current conversation to a JSON snapshot under ~/.opencodon/sessions/saved/.

        The snapshot is a convenience export for sharing or off-line inspection;
        every message is already persisted incrementally to the SQLite session
        DB, so the live session remains resumable via ``opencodon --resume <id>``
        regardless of whether the user ever runs ``/save``.
        """
        if not self.conversation_history:
            print("(;_;) No conversation to save.")
            return

        timestamp = _shell.datetime.now().strftime("%Y%m%d_%H%M%S")
        saved_dir = get_opencodon_home() / "sessions" / "saved"
        try:
            saved_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            print(f"(x_x) Failed to create save directory {saved_dir}: {e}")
            return
        path = saved_dir / f"opencodon_conversation_{timestamp}.json"

        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({
                    "model": self.model,
                    "session_id": self.session_id,
                    "session_start": self.session_start.isoformat(),
                    "messages": self.conversation_history,
                }, f, indent=2, ensure_ascii=False)
            print(f"(^_^)v Conversation snapshot saved to: {path}")
            if self.session_id:
                print(f"       Resume the live session with: opencodon --resume {self.session_id}")
        except Exception as e:
            print(f"(x_x) Failed to save: {e}")

    def retry_last(self):
        """Retry the last user message by removing the last exchange and re-sending.
        
        Removes the last assistant response (and any tool-call messages) and
        the last user message, then re-sends that user message to the agent.
        Returns the message to re-send, or None if there's nothing to retry.
        """
        if not self.conversation_history:
            print("(._.) No messages to retry.")
            return None
        
        # Walk backwards to find the last user message
        last_user_idx = None
        for i in range(len(self.conversation_history) - 1, -1, -1):
            if self.conversation_history[i].get("role") == "user":
                last_user_idx = i
                break
        
        if last_user_idx is None:
            print("(._.) No user message found to retry.")
            return None
        
        # Extract the message text and remove everything from that point forward
        last_message = self.conversation_history[last_user_idx].get("content", "")
        self.conversation_history = self.conversation_history[:last_user_idx]
        
        print(f"(^_^)b Retrying: \"{last_message[:60]}{'...' if len(last_message) > 60 else ''}\"")
        return last_message

    def undo_last(self, n: int = 1, prefill: bool = True):
        """Back up N user turns: truncate history, soft-delete on disk, prefill.

        Walks backwards N user messages and discards everything from the
        Nth-from-last user message onward (its assistant response, tool
        calls, etc.). ``n`` defaults to 1 (the last exchange); ``/undo 3``
        backs up three user turns. If ``n`` exceeds the number of user
        turns, it backs up to the oldest one.

        Beyond the in-memory ``conversation_history`` slice, this also:
          • soft-deletes the truncated rows in SessionDB (``active=0``) so
            they're hidden from re-prompts and search but kept for audit;
          • notifies memory providers via ``on_session_switch(rewound=True)``;
          • mirrors /branch's agent surgery (system-prompt invalidation +
            flush-index reset);
          • when ``prefill`` is set and an input buffer is available,
            pre-fills the composer with the backed-up message text so it
            can be edited and resubmitted.

        ``prefill=False`` is used by callers that drive the undo
        programmatically (e.g. checkpoint rollback) and don't want to
        touch the user's input buffer.
        """
        if not self.conversation_history:
            print("(._.) No messages to undo.")
            return

        if n < 1:
            n = 1

        # Walk backwards collecting the indices of the last N user messages.
        user_indices = []
        for i in range(len(self.conversation_history) - 1, -1, -1):
            if self.conversation_history[i].get("role") == "user":
                user_indices.append(i)
                if len(user_indices) >= n:
                    break

        if not user_indices:
            print("(._.) No user message found to undo.")
            return

        # The oldest of the collected user messages is our truncation point.
        cut_idx = user_indices[-1]
        turns_undone = len(user_indices)

        removed_count = len(self.conversation_history) - cut_idx
        removed_msg = self.conversation_history[cut_idx].get("content", "")
        removed_text = self._undo_content_to_text(removed_msg)

        # Truncate the in-memory history to before that user message.
        self.conversation_history = self.conversation_history[:cut_idx]

        # Soft-delete the truncated rows on disk so re-prompts and search
        # see the clean transcript while the rows survive for audit.
        rewound_rows = 0
        if self._session_db is not None and self.session_id:
            try:
                recents = self._session_db.list_recent_user_messages(
                    self.session_id, limit=max(turns_undone, 10)
                )
                if recents:
                    target_idx = min(turns_undone - 1, len(recents) - 1)
                    target_id = recents[target_idx]["id"]
                    result = self._session_db.rewind_to_message(
                        self.session_id, target_id
                    )
                    rewound_rows = result.get("rewound_count", 0)
                    # Prefer the DB's decoded target text for the prefill —
                    # it's the canonical persisted copy.
                    db_text = self._undo_content_to_text(
                        (result.get("target_message") or {}).get("content")
                    )
                    if db_text:
                        removed_text = db_text
            except ValueError as e:
                # Non-user target / cross-session — keep the in-memory undo
                # but skip the soft-delete; surface a debug-level note.
                _shell.logger.debug("undo: soft-delete skipped: %s", e)
            except Exception as e:
                _shell.logger.debug("undo: soft-delete failed: %s", e)

        # Agent surgery: invalidate the system-prompt cache and reset the
        # flush index so the next turn re-flushes from the truncated head.
        if self.agent is not None:
            if hasattr(self.agent, "_invalidate_system_prompt"):
                try:
                    self.agent._invalidate_system_prompt()
                except Exception:
                    pass
            if hasattr(self.agent, "_last_flushed_db_idx"):
                try:
                    self.agent._last_flushed_db_idx = len(self.conversation_history)
                except Exception:
                    pass
            # Notify memory providers — same hook /branch fires, with the
            # rewound flag so per-turn document caches invalidate (#6672, #21910).
            try:
                _mm = getattr(self.agent, "_memory_manager", None)
                if _mm is not None and self.session_id:
                    _mm.on_session_switch(
                        self.session_id,
                        parent_session_id="",
                        reset=False,
                        rewound=True,
                    )
            except Exception:
                pass

        turn_word = "turn" if turns_undone == 1 else "turns"
        msg_count = rewound_rows or removed_count
        print(
            f"(^_^)b Undid {turns_undone} {turn_word} ({msg_count} message(s)). "
            f"Backed up to: \"{removed_text[:60]}{'...' if len(removed_text) > 60 else ''}\""
        )
        remaining = len(self.conversation_history)
        print(f"  {remaining} message(s) remaining in history.")

        # Pre-fill the composer with the backed-up message so the user can
        # edit and resubmit (Claude-Code-style). Editable, not auto-sent.
        if prefill and removed_text:
            self._prefill_input_buffer(removed_text)

    @staticmethod
    def _undo_content_to_text(content) -> str:
        """Flatten message content (str or content-part list) to plain text."""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [
                p.get("text", "")
                for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            ]
            return "\n".join(t for t in parts if t)
        return ""

    def _manual_compress(self, cmd_original: str = ""):
        """Manually trigger context compression on the current conversation.

        Two modes:

        * ``/compress [<focus>]`` — compress the *whole* history. An
          optional focus topic guides the summariser to preserve
          information related to *focus* while being more aggressive
          about discarding everything else.  Inspired by Claude Code's
          ``/compact <focus>`` feature.
        * ``/compress here [N]`` — boundary-aware compression. Summarize
          everything *except* the most recent ``N`` exchanges (default
          2), which are preserved verbatim. Inspired by Claude Code's
          Rewind "Summarize up to here" action (v2.1.139, May 2026,
          https://code.claude.com/docs/en/whats-new/2026-w20). Lets the
          user pick the compression boundary instead of leaving it to
          the automatic token-budget heuristic.
        """
        if not self.conversation_history or len(self.conversation_history) < 4:
            print("(._.) Not enough conversation to compress (need at least 4 messages).")
            return

        if not self.agent:
            print("(._.) No active agent -- send a message first.")
            return

        # No compression_enabled gate here: the config flag disables
        # *automatic* compaction only. Manual /compress is an explicit user
        # action — the context-overflow error path (conversation_loop.py)
        # directs users here when auto-compaction is off, and the gateway's
        # /compress handler has never gated on the flag.

        from opencodon.frontends.cli.partial_compress import (
            extract_compress_flags,
            parse_partial_compress_args,
            rejoin_compressed_head_and_tail,
            split_history_for_partial_compress,
            summarize_compress_preview,
        )
        from opencodon.core.context.conversation_compression import (
            finalize_context_engine_compression_notification,
        )

        # Args after the command word (e.g. "/compress here 3" -> "here 3").
        raw_args = ""
        if cmd_original:
            _parts = cmd_original.strip().split(None, 1)
            if len(_parts) > 1:
                raw_args = _parts[1].strip()

        # Strip --preview/--dry-run/--aggressive before positional parsing
        # so the flags coexist with 'here [N]' / focus-topic forms.
        raw_args, preview, aggressive = extract_compress_flags(raw_args)
        partial, keep_last, focus_topic = parse_partial_compress_args(raw_args)
        focus_topic = focus_topic or ""

        if aggressive:
            # LLM-free hard truncation is not supported: it would need its
            # own transcript-persistence path outside the guarded
            # _compress_context rotation machinery. Surface that instead of
            # silently mis-parsing the flag as a focus topic.
            print("(._.) --aggressive is not supported; use '/compress here [N]' "
                  "to keep only recent exchanges, or /undo to drop turns.")
            if not preview:
                return

        if preview:
            from opencodon.core.providers.model_metadata import estimate_request_tokens_rough
            _sys_prompt = getattr(self.agent, "_cached_system_prompt", "") or ""
            _tools = getattr(self.agent, "tools", None) or None
            approx_tokens = estimate_request_tokens_rough(
                self.conversation_history,
                system_prompt=_sys_prompt,
                tools=_tools,
            )
            report = summarize_compress_preview(
                self.conversation_history,
                partial,
                keep_last,
                focus_topic or None,
                approx_tokens,
            )
            for line in report["lines"]:
                print(f"🗜️  {line}")
            return

        original_count = len(self.conversation_history)
        with self._busy_command("Compressing context..."):
            try:
                from opencodon.core.providers.model_metadata import estimate_request_tokens_rough
                from opencodon.core.context.manual_compression_feedback import summarize_manual_compression
                original_history = list(self.conversation_history)

                # Boundary-aware split: only the head is summarized; the
                # most recent `keep_last` exchanges ride along verbatim.
                tail: list = []
                head = original_history
                if partial:
                    head, tail = split_history_for_partial_compress(
                        original_history, keep_last
                    )
                    if not tail:
                        # Split degenerated (everything would be kept, or
                        # no head left to compress). Fall back to full
                        # compression so the user still gets an action.
                        partial = False
                        head = original_history

                # Include system prompt + tool schemas in the estimate —
                # a transcript-only number understates real request pressure
                # and can even appear to grow after compression because a
                # dense handoff summary replaces many short turns (#6217).
                _sys_prompt = getattr(self.agent, "_cached_system_prompt", "") or ""
                _tools = getattr(self.agent, "tools", None) or None
                approx_tokens = estimate_request_tokens_rough(
                    original_history,
                    system_prompt=_sys_prompt,
                    tools=_tools,
                )
                if partial:
                    print(f"🗜️  Summarizing up to here: compressing {len(head)} of "
                          f"{original_count} messages (~{approx_tokens:,} tokens), "
                          f"keeping last {keep_last} exchange(s) verbatim...")
                elif focus_topic:
                    print(f"🗜️  Compressing {original_count} messages (~{approx_tokens:,} tokens), "
                          f"focus: \"{focus_topic}\"...")
                else:
                    print(f"🗜️  Compressing {original_count} messages (~{approx_tokens:,} tokens)...")

                # Pass None as system_message so _compress_context rebuilds
                # the system prompt from scratch via _build_system_prompt(None).
                # Passing _cached_system_prompt caused duplication because
                # _build_system_prompt appends system_message to prompt_parts
                # which already contain the agent identity — resulting in the
                # identity block appearing twice (issue #15281).
                compressed, _ = self.agent._compress_context(
                    head,
                    None,
                    approx_tokens=approx_tokens,
                    focus_topic=focus_topic or None,
                    force=True,
                    defer_context_engine_notification=True,
                )
                if partial and tail:
                    compressed = rejoin_compressed_head_and_tail(compressed, tail)
                self.conversation_history = compressed
                # _compress_context ends the old session and creates a new child
                # session on the agent (run_agent.py::_compress_context). Sync the
                # CLI's session_id so /status, /resume, exit summary, and title
                # generation all point at the live continuation session, not the
                # ended parent. Without this, subsequent end_session() calls target
                # the already-closed parent and the child is orphaned.
                if (
                    getattr(self.agent, "session_id", None)
                    and self.agent.session_id != self.session_id
                ):
                    self.session_id = self.agent.session_id
                    self._pending_title = None
                    # Manual /compress replaces conversation_history with a new
                    # compressed handoff for the child session. Persist it from
                    # offset 0 so resume can recover the continuation after exit.
                    self.agent._flush_messages_to_session_db(self.conversation_history, None)
                finalize_context_engine_compression_notification(
                    self.agent,
                    committed=True,
                )
                new_tokens = estimate_request_tokens_rough(
                    self.conversation_history,
                    system_prompt=_sys_prompt,
                    tools=_tools,
                )
                summary = summarize_manual_compression(
                    original_history,
                    self.conversation_history,
                    approx_tokens,
                    new_tokens,
                    compression_state=getattr(
                        self.agent, "context_compressor", None
                    ),
                )
                if summary.get("aborted") or summary.get("fallback_used"):
                    icon = "⚠️"
                else:
                    icon = "🗜️" if summary["noop"] else "✅"
                print(f"  {icon} {summary['headline']}")
                print(f"     {summary['token_line']}")
                if summary["note"]:
                    print(f"     {summary['note']}")

            except Exception as e:
                finalize_context_engine_compression_notification(
                    self.agent,
                    committed=False,
                )
                print(f"  ❌ Compression failed: {e}")

    def _persist_prompt_summary(self, icon: str, label: str, detail: str, outcome: str) -> None:
        """Print a one-line scrollback summary of a resolved modal prompt.

        Modal panels (approval / clarify) live in the prompt_toolkit layout and
        vanish on the next repaint, so the question and the decision leave no
        trace in the terminal scrollback. When display.persist_prompts is on
        (default), emit a dim single line after the prompt resolves so the
        decision survives in chat history.
        """
        if not _shell.CLI_CONFIG.get("display", {}).get("persist_prompts", True):
            return
        detail = " ".join(detail.split())
        if len(detail) > 120:
            detail = detail[:119] + "…"
        outcome = " ".join(outcome.split())
        if len(outcome) > 120:
            outcome = outcome[:119] + "…"
        _shell._cprint(f"\n{_shell._DIM}{icon} {label}: {detail} → {outcome}{_shell._RST}")

    def _clear_terminal_on_exit(self):
        """Clear screen + scrollback so nothing is stranded above the exit summary.

        Called from ``_print_exit_summary`` after ``app.run()`` has returned and
        prompt_toolkit has torn down its renderer + restored terminal modes —
        so a direct write to the real stdout fd is safe (the StdoutProxy /
        patch_stdout layer is gone by now).

        Sequence: ``ESC[3J`` (erase scrollback) + ``ESC[2J`` (erase visible
        screen) + ``ESC[H`` (cursor home). Modern terminals on Linux, macOS and
        Windows (Terminal / conhost with VT processing, which prompt_toolkit
        already enables) all honor these. Best-effort: skip silently when
        stdout isn't a real console, and fall back to the platform ``clear`` /
        ``cls`` command if the escape write fails.
        """
        try:
            stream = sys.stdout
            if stream is None or not stream.isatty():
                return
        except Exception:
            return
        try:
            stream.write("\033[3J\033[2J\033[H")
            stream.flush()
            return
        except Exception:
            pass
        # Fallback: shell clear command (rarely needed — escapes work on every
        # VT-capable terminal, but this covers exotic stdout wrappers).
        try:
            _shell.os.system("cls" if _shell.os.name == "nt" else "clear")
        except Exception:
            pass

    def _persist_active_session_before_close(self):
        """Best-effort SQLite/JSON flush before the CLI marks a session closed.

        ``run_conversation()`` normally persists at turn boundaries, but a
        terminal close/SIGHUP/SIGTERM can unwind the prompt_toolkit app while
        the agent thread still holds the current turn only in memory.  Flush the
        agent's live ``_session_messages`` before ``end_session()`` so resume,
        session_search, and state.db do not lose the interrupted turn.
        """
        agent = getattr(self, "agent", None)
        if not agent or not hasattr(agent, "_persist_session"):
            return

        persist_lock = getattr(agent, "_session_persist_lock", None)

        def _snapshot_and_persist() -> None:
            # This snapshot must share the staging lock with ``chat()``. Without
            # it, close can retain a mutable history baseline just before chat
            # appends its pending dict; the later flush then mistakes that dict
            # for durable history and stamps it without writing a row (#63766).
            messages = getattr(agent, "_session_messages", None)
            pending_cli_message = getattr(agent, "_pending_cli_user_message", None)
            if not isinstance(messages, list):
                messages = getattr(self, "conversation_history", None)
            if not isinstance(messages, list):
                return
            if isinstance(pending_cli_message, dict) and not any(
                message is pending_cli_message for message in messages
            ):
                # The UI has accepted a new input but the worker still exposes its
                # prior snapshot. Include only that staged dict; the baseline below
                # keeps any durable resumed prefix from being re-appended.
                messages = [*messages, pending_cli_message]
            if not messages:
                return

            # A normal turn builds a new list that reuses the resumed-history dicts.
            # Keep that CLI history as the baseline so a signal between assigning
            # ``_session_messages`` and the turn's DB flush cannot append its durable
            # prefix a second time. Once the CLI takes the turn result, however, both
            # names can point at the same live list; passing that alias would mark an
            # unflushed tail durable without writing it. Marker-only persistence is
            # correct only in that alias case.
            conversation_history = getattr(self, "conversation_history", None)
            pending_cli_message = getattr(agent, "_pending_cli_user_message", None)
            if (
                isinstance(conversation_history, list)
                and conversation_history
                and conversation_history[-1] is pending_cli_message
            ):
                # The UI accepted this user message before the agent finished its
                # early persistence. Its dict can already be in ``messages`` but is
                # not durable yet, so exclude it from the resumed-history baseline.
                conversation_history = conversation_history[:-1]
            elif not isinstance(conversation_history, list) or conversation_history is messages:
                conversation_history = None

            # A first-turn close can arrive before the worker builds its cached
            # prompt. Build or restore it before the DB row is created so the
            # durable transcript never leaves a NULL system_prompt cache entry.
            if getattr(agent, "_cached_system_prompt", None) is None:
                try:
                    from opencodon.core.conversation_loop import _restore_or_build_system_prompt

                    _restore_or_build_system_prompt(agent, None, conversation_history)
                except Exception:
                    _shell.logger.debug("Could not build system prompt during CLI close", exc_info=True)
                    return
            if getattr(agent, "_cached_system_prompt", None) is None:
                return

            agent._ensure_db_session()
            agent._persist_session(messages, conversation_history)
            if getattr(agent, "session_id", None):
                self.session_id = agent.session_id

        try:
            if persist_lock is None:
                _snapshot_and_persist()
            else:
                with persist_lock:
                    _snapshot_and_persist()
        except (Exception, KeyboardInterrupt) as e:
            _shell.logger.debug("Could not persist active CLI session before close: %s", e)

