"""OpencodonCLI ShellPromptsMixin — extracted from shell.py (restructure Phase 4).

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

from opencodon.frontends.cli.fallback_config import get_fallback_chain
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


class ShellPromptsMixin:
    def _open_external_editor(self, buffer=None) -> bool:
        """Open the active input buffer in an external editor."""
        app = getattr(self, "_app", None)
        if not app:
            _shell._cprint(f"{_shell._DIM}External editor is only available inside the interactive CLI.{_shell._RST}")
            return False
        if self._command_running:
            _shell._cprint(f"{_shell._DIM}Wait for the current command to finish before opening the editor.{_shell._RST}")
            return False
        if self._sudo_state or self._secret_state or self._approval_state or getattr(self, "_slash_confirm_state", None) or self._clarify_state:
            _shell._cprint(f"{_shell._DIM}Finish the active prompt before opening the editor.{_shell._RST}")
            return False
        target_buffer = buffer or getattr(app, "current_buffer", None)
        if target_buffer is None:
            _shell._cprint(f"{_shell._DIM}No active input buffer is available for the external editor.{_shell._RST}")
            return False
        try:
            # Inline pastes so the editor (and the draft it submits) sees real
            # content; skip flag unconditionally so the editor-close text-change
            # doesn't re-collapse it, even when there was nothing to inline.
            self._inline_pastes(target_buffer)
            self._skip_paste_collapse = True
            # Open the editor, then submit the saved draft on a clean exit —
            # matching the TUI's Ctrl+G (openEditor), which sends the buffer
            # instead of requiring a second Enter. Submission in this CLI is
            # driven by the custom `enter` keybinding, NOT the buffer's
            # accept_handler, so validate_and_handle can't route through it;
            # chain a done-callback on the returned Task that re-uses the
            # real submit pipeline via _submit_editor_buffer().
            task = target_buffer.open_in_editor(validate_and_handle=False)
            if task is not None and hasattr(task, "add_done_callback"):
                task.add_done_callback(
                    lambda _t, b=target_buffer: self._submit_editor_buffer(b)
                )
            return True
        except Exception as exc:
            _shell._cprint(f"{_shell._DIM}Failed to open external editor: {exc}{_shell._RST}")
            return False

    def _submit_editor_buffer(self, buffer) -> None:
        """Submit the draft an external editor left in ``buffer``.

        Invoked from the Ctrl+G done-callback so saving the editor sends the
        prompt (TUI parity) instead of leaving it sitting in the input area.
        Mirrors the idle/queue branches of the `enter` keybinding handler:
        an empty save is ignored (never submits a blank turn), a slash command
        is dispatched, otherwise the text is routed through the same input
        queues the normal Enter path uses. Runs on the prompt_toolkit event
        loop via the Task callback, so it must be cheap and non-blocking.
        """
        try:
            text = (getattr(buffer, "text", "") or "").strip()
        except Exception:
            return
        if not text:
            # Editor saved empty / was cleared — match the TUI, which drops
            # an empty draft instead of submitting a blank turn.
            return

        app = getattr(self, "_app", None)

        # Slash commands: dispatch directly, same as the Enter handler's
        # _shell._looks_like_slash_command branch.
        if _shell._looks_like_slash_command(text):
            try:
                if not self.process_command(text):
                    self._should_exit = True
                    if app is not None and app.is_running:
                        app.exit()
            except Exception as exc:
                _shell._cprint(f"  {_shell._DIM}Command failed: {exc}{_shell._RST}")
            finally:
                self._reset_input_buffer(buffer)
                if app is not None:
                    app.invalidate()
            return

        # Regular prompt: route through the same queues the Enter handler uses.
        if self._agent_running:
            # Agent busy → honour the configured busy-input behaviour by
            # queueing for the next turn (the safe default; interrupt/steer
            # remain reachable via the normal Enter path).
            self._interrupt_queue.put(text) if self.busy_input_mode == "interrupt" else self._pending_input.put(text)
            preview = text[:80] + ("..." if len(text) > 80 else "")
            _shell._cprint(f"  Queued for the next turn: {preview}")
        else:
            self._pending_input.put(text)

        self._reset_input_buffer(buffer)
        if app is not None:
            app.invalidate()

    def _inline_pastes(self, buffer) -> None:
        """Replace collapsed-paste placeholders in ``buffer`` with real content.

        A big paste shows as a compact ``[Pasted text #N -> file]`` placeholder,
        but history recall and the external editor need the actual text — a bare
        reference is useless once the file is gone or on another machine. Inlining
        before ``reset(append_to_history=True)`` also lets prompt_toolkit persist
        the content through its normal path. Sets ``_skip_paste_collapse`` so the
        ensuing text-change doesn't re-collapse it.
        """
        try:
            existing = getattr(buffer, "text", "")
            expanded = self._expand_paste_references(existing)
            if expanded != existing and hasattr(buffer, "text"):
                self._skip_paste_collapse = True
                buffer.text = expanded
                if hasattr(buffer, "cursor_position"):
                    buffer.cursor_position = len(expanded)
        except Exception:
            _shell.logger.debug("Failed to inline paste placeholders", exc_info=True)

    def _reset_input_buffer(self, buffer) -> None:
        """Clear an input buffer after a programmatic submit (best-effort)."""
        try:
            buffer.reset(append_to_history=True)
        except Exception:
            try:
                buffer.text = ""
            except Exception:
                pass

    def _try_attach_clipboard_image(self) -> bool:
        """Check clipboard for an image and attach it if found.

        Saves the image to ~/.opencodon/images/ and appends the path to
        ``_attached_images``.  Returns True if an image was attached.
        """
        from opencodon.frontends.cli.clipboard import save_clipboard_image

        img_dir = get_opencodon_home() / "images"
        self._image_counter += 1
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        img_path = img_dir / f"clip_{ts}_{self._image_counter}.png"

        if save_clipboard_image(img_path):
            self._attached_images.append(img_path)
            return True
        self._image_counter -= 1
        return False

    def _write_osc52_clipboard(self, text: str) -> None:
        """Copy *text* to terminal clipboard via OSC 52."""
        payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
        seq = f"\x1b]52;c;{payload}\x07"
        out = getattr(self, "_app", None)
        output = getattr(out, "output", None) if out else None
        if output and hasattr(output, "write_raw"):
            output.write_raw(seq)
            output.flush()
            return
        if output and hasattr(output, "write"):
            output.write(seq)
            output.flush()
            return
        sys.stdout.write(seq)
        sys.stdout.flush()

    def _prefill_input_buffer(self, text: str) -> None:
        """Place ``text`` in the active prompt_toolkit buffer, editable."""
        app = getattr(self, "_app", None)
        if app is None:
            return
        try:
            buf = app.current_buffer
            buf.text = text
            if hasattr(buf, "cursor_position"):
                buf.cursor_position = len(text)
            app.invalidate()
        except Exception as e:
            _shell.logger.debug("undo: prefill buffer failed: %s", e)

    def _run_curses_picker(self, title: str, items: list[str], default_index: int = 0) -> int | None:
        """Run curses_single_select via run_in_terminal so prompt_toolkit handles terminal ownership cleanly."""
        import threading
        from opencodon.frontends.cli.curses_ui import curses_single_select

        result = [None]

        def _pick():
            result[0] = curses_single_select(title, items, default_index=default_index)

        # run_in_terminal requires an asyncio event loop — only exists in the
        # main prompt_toolkit thread.  If we're in a background thread (e.g.
        # process_loop), fall back to direct curses call.
        in_main_thread = threading.current_thread() is threading.main_thread()

        if self._app and in_main_thread:
            from prompt_toolkit.application import run_in_terminal
            was_visible = self._status_bar_visible
            self._status_bar_visible = False
            self._app.invalidate()
            try:
                run_in_terminal(_pick)
            finally:
                self._status_bar_visible = was_visible
                self._app.invalidate()
        else:
            _pick()

        return result[0]

    def _prompt_text_input(self, prompt_text: str) -> str | None:
        """Prompt for free-text input safely inside or outside prompt_toolkit.

        Mirrors the thread-aware guard in ``_run_curses_picker``: ``run_in_terminal``
        returns a coroutine that must be awaited by the prompt_toolkit event loop,
        which only exists on the main thread.  Slash commands are dispatched from
        the ``process_loop`` daemon thread (see issue #23185), so calling
        ``run_in_terminal`` from there orphans the coroutine — ``_ask`` never runs,
        and user keystrokes leak into the composer instead.  Fall back to a direct
        ``input()`` when we're off the main thread.
        """
        import threading
        result = [None]

        def _ask():
            try:
                result[0] = input(prompt_text).strip() or None
            except (KeyboardInterrupt, EOFError):
                pass

        in_main_thread = threading.current_thread() is threading.main_thread()

        # Slash-worker guard (#23185 / billing auto-reload hang): when a
        # prompt_toolkit app is running but we're on a non-main thread (the
        # process_loop / TUI slash-worker daemon thread), stdin is owned by the
        # event loop / JSON-RPC pipe.  A bare input() there blocks forever until
        # the worker's 45s timeout fires.  We cannot safely prompt off the main
        # thread, so cancel cleanly (None) instead of hanging — mirrors the
        # _stdin_fallback discipline in _prompt_text_input_modal.
        if self._app and not in_main_thread:
            self._invalidate()
            return None

        if self._app and in_main_thread:
            from prompt_toolkit.application import run_in_terminal
            was_visible = self._status_bar_visible
            self._status_bar_visible = False
            self._app.invalidate()
            try:
                run_in_terminal(_ask)
            except Exception:
                # WSL / Warp / certain terminal emulators silently drop the
                # scheduled coroutine.  Fall back to a direct input() so the
                # user's keystrokes don't leak into the agent buffer.
                try:
                    _ask()
                except Exception:
                    pass
            finally:
                self._status_bar_visible = was_visible
                self._app.invalidate()
        else:
            _ask()
        return result[0]

    def _prompt_text_input_modal(
        self,
        *,
        title: str,
        detail: str,
        choices: list[tuple[str, str, str]],
        timeout: float = 120,
    ) -> str | None:
        """Prompt through the prompt_toolkit composer instead of raw input().

        This is for CLI slash-command confirmations.  The old raw input() path
        fought prompt_toolkit's active stdin ownership: in some terminals the
        prompt appeared above the TUI, choices were redrawn later, and Enter
        could be interpreted as EOF/exit.  A first-class modal state keeps the
        choices visible and lets the normal Enter key binding submit the typed
        or highlighted choice.

        **Platform note (Windows — issue #33961):**
        Earlier code bypassed the modal on ``sys.platform == "win32"`` and fell
        back to a raw ``input()`` prompt.  When the confirm was triggered from the
        ``process_loop`` daemon thread (the normal case) that ``input()`` ran off
        the main thread and deadlocked against prompt_toolkit's stdin ownership —
        the user saw a frozen cursor and Ctrl-C was swallowed (bare ``/reset``
        froze; ``/reset now`` worked only because it skips the prompt entirely).

        Native Windows now uses the same path as Linux/macOS: the modal is set up
        on ``self._app.loop`` via ``call_soon_threadsafe`` and answered by the
        normal prompt_toolkit key bindings (the same input channel that already
        handles ordinary typing on Windows).  The raw ``input()`` fallback is kept
        only for the genuinely safe cases: no running app (unit tests /
        non-interactive), no resolvable event loop, or a scheduling failure.
        """
        import threading
        import time as _time

        if not choices:
            return None

        # If prompt_toolkit is not running (unit tests / non-interactive calls),
        # keep the simple stdin fallback.
        if not getattr(self, "_app", None):
            return self._prompt_text_input("Choice [1/2/3]: ")

        try:
            app_loop = self._app.loop
        except Exception:
            app_loop = None

        in_main_thread = threading.current_thread() is threading.main_thread()

        def _stdin_fallback() -> str | None:
            # On native Windows a raw input() from a non-main thread deadlocks
            # against prompt_toolkit's stdin ownership (#33961).  With an app
            # running we cannot safely prompt off the main thread, so cancel
            # cleanly (None) rather than hang the terminal.
            if sys.platform == "win32" and not in_main_thread:
                self._invalidate()
                return None
            return self._prompt_text_input("Choice [1/2/3]: ")

        if not in_main_thread and app_loop is None:
            return _stdin_fallback()

        response_queue = queue.Queue()

        def _setup_modal() -> None:
            self._capture_modal_input_snapshot()
            self._slash_confirm_state = {
                "title": title,
                "detail": detail,
                "choices": choices,
                "selected": 0,
                "response_queue": response_queue,
            }
            self._slash_confirm_deadline = _time.monotonic() + timeout
            self._invalidate()

        def _teardown_modal() -> None:
            self._slash_confirm_state = None
            self._slash_confirm_deadline = 0
            self._restore_modal_input_snapshot()
            self._invalidate()

        def _run_on_app_loop(fn) -> bool:
            if in_main_thread or app_loop is None:
                fn()
                return True
            ready = threading.Event()

            def _wrapped() -> None:
                try:
                    fn()
                finally:
                    ready.set()

            try:
                app_loop.call_soon_threadsafe(_wrapped)
            except Exception:
                return False
            return ready.wait(timeout=5)

        if not _run_on_app_loop(_setup_modal):
            return _stdin_fallback()

        _last_countdown_refresh = _time.monotonic()
        try:
            while True:
                try:
                    result = response_queue.get(timeout=1)
                    _run_on_app_loop(_teardown_modal)
                    return result
                except queue.Empty:
                    remaining = self._slash_confirm_deadline - _time.monotonic()
                    if remaining <= 0:
                        break
                    now = _time.monotonic()
                    if now - _last_countdown_refresh >= 5.0:
                        _last_countdown_refresh = now
                        self._invalidate()
        finally:
            if self._slash_confirm_state is not None:
                _run_on_app_loop(_teardown_modal)
        return None

    def _submit_slash_confirm_response(self, value: str | None) -> None:
        state = self._slash_confirm_state
        if not state:
            return
        state["response_queue"].put(value)
        self._slash_confirm_state = None
        self._slash_confirm_deadline = 0
        self._invalidate()

    def _normalize_slash_confirm_choice(
        self,
        raw: str | None,
        choices: list[tuple[str, str, str]],
    ) -> str | None:
        if raw is None:
            return None
        choice_raw = raw.strip().lower()
        if not choice_raw:
            return None
        aliases = {
            "1": "once",
            "once": "once",
            "approve": "once",
            "yes": "once",
            "y": "once",
            "ok": "once",
            "2": "always",
            "always": "always",
            "remember": "always",
            "3": "cancel",
            "cancel": "cancel",
            "nevermind": "cancel",
            "no": "cancel",
            "n": "cancel",
        }
        allowed = {choice[0] for choice in choices}
        normalized = aliases.get(choice_raw)
        if normalized in allowed:
            return normalized
        if choice_raw in allowed:
            return choice_raw
        return None

    def _get_slash_confirm_display_fragments(self):
        """Render the /new-/clear-style confirmation panel."""
        state = self._slash_confirm_state
        if not state:
            return []

        title = state.get("title") or "Confirm action"
        detail = state.get("detail") or ""
        choices = state.get("choices") or []
        selected = state.get("selected", 0)

        def _panel_box_width(title_text: str, content_lines: list[str], min_width: int = 56, max_width: int = 86) -> int:
            term_cols = shutil.get_terminal_size((100, 20)).columns
            longest = max([len(title_text)] + [len(line) for line in content_lines] + [min_width - 4])
            inner = min(max(longest + 4, min_width - 2), max_width - 2, max(24, term_cols - 6))
            return inner + 2

        def _wrap_panel_text(text: str, width: int, subsequent_indent: str = "") -> list[str]:
            wrapped = textwrap.wrap(
                text,
                width=max(8, width),
                replace_whitespace=False,
                drop_whitespace=False,
                subsequent_indent=subsequent_indent,
            )
            return wrapped or [""]

        def _append_panel_line(lines, border_style: str, content_style: str, text: str, box_width: int) -> None:
            inner_width = max(0, box_width - 2)
            lines.append((border_style, "│ "))
            lines.append((content_style, text.ljust(inner_width)))
            lines.append((border_style, " │\n"))

        def _append_blank_panel_line(lines, border_style: str, box_width: int) -> None:
            lines.append((border_style, "│" + (" " * box_width) + "│\n"))

        preview_lines = []
        for line in detail.splitlines():
            preview_lines.extend(_wrap_panel_text(line, 72))
        for idx, (_value, label, desc) in enumerate(choices):
            marker = "❯" if idx == selected else " "
            preview_lines.extend(_wrap_panel_text(f"{marker} [{idx + 1}] {label} — {desc}", 72, subsequent_indent="    "))
        preview_lines.append("Type 1/2/3 or use ↑/↓ then Enter. ESC/Ctrl+C cancels.")

        box_width = _panel_box_width(title, preview_lines)
        inner_text_width = max(8, box_width - 2)
        detail_wrapped = []
        for line in detail.splitlines():
            detail_wrapped.extend(_wrap_panel_text(line, inner_text_width))
        choice_wrapped: list[tuple[int, str]] = []
        for idx, (_value, label, desc) in enumerate(choices):
            marker = "❯" if idx == selected else " "
            for wrapped in _wrap_panel_text(f"{marker} [{idx + 1}] {label} — {desc}", inner_text_width, subsequent_indent="    "):
                choice_wrapped.append((idx, wrapped))

        term_rows = shutil.get_terminal_size((100, 24)).lines
        reserved_below = 6
        chrome_full = 6
        available = max(0, term_rows - reserved_below)
        max_detail_rows = max(1, available - chrome_full - len(choice_wrapped))
        max_detail_rows = min(max_detail_rows, 8)
        if len(detail_wrapped) > max_detail_rows:
            keep = max(1, max_detail_rows - 1)
            detail_wrapped = detail_wrapped[:keep] + ["… (detail truncated)"]

        lines = []
        lines.append(('class:approval-border', '╭' + ('─' * box_width) + '╮\n'))
        _append_panel_line(lines, 'class:approval-border', 'class:approval-title', title, box_width)
        _append_blank_panel_line(lines, 'class:approval-border', box_width)
        for wrapped in detail_wrapped:
            _append_panel_line(lines, 'class:approval-border', 'class:approval-desc', wrapped, box_width)
        _append_blank_panel_line(lines, 'class:approval-border', box_width)
        for idx, wrapped in choice_wrapped:
            style = 'class:approval-selected' if idx == selected else 'class:approval-choice'
            _append_panel_line(lines, 'class:approval-border', style, wrapped, box_width)
        _append_blank_panel_line(lines, 'class:approval-border', box_width)
        _append_panel_line(lines, 'class:approval-border', 'class:approval-cmd', 'Type 1/2/3 or use ↑/↓ then Enter. ESC/Ctrl+C cancels.', box_width)
        lines.append(('class:approval-border', '╰' + ('─' * box_width) + '╯\n'))
        return lines

    @classmethod
    def _split_destructive_skip(cls, cmd_text: Optional[str]) -> tuple[str, bool]:
        """Split inline-skip tokens out of a destructive slash command.

        Returns ``(remainder, skip)`` where ``remainder`` is the original
        text with the command word and any recognized skip tokens removed,
        and ``skip`` is True iff at least one skip token was found.

        Examples:
            "/reset now"            -> ("", True)
            "/reset --yes My title" -> ("My title", True)
            "/new My title"         -> ("My title", False)
            "/clear"                -> ("", False)
        """
        if not cmd_text:
            return "", False
        tokens = cmd_text.strip().split()
        if not tokens:
            return "", False
        # Drop leading "/cmd" word — callers pass the full command text.
        if tokens[0].startswith("/"):
            tokens = tokens[1:]
        skip = False
        kept: list[str] = []
        for tok in tokens:
            if tok.lower() in cls._DESTRUCTIVE_SKIP_TOKENS:
                skip = True
                continue
            kept.append(tok)
        return " ".join(kept), skip

    def _confirm_destructive_slash(
        self,
        command: str,
        detail: str,
        cmd_original: Optional[str] = None,
    ) -> Optional[str]:
        """Prompt the user to confirm a destructive session slash command.

        Used by ``/clear``, ``/new``/``/reset``, and ``/undo`` before they
        discard conversation state.  Three-option prompt:

          1. Approve Once — proceed this time only
          2. Always Approve — proceed and persist
             ``approvals.destructive_slash_confirm: false`` so future
             destructive commands run without confirmation
          3. Cancel — abort

        Gated by ``approvals.destructive_slash_confirm`` (default on).  If the
        gate is off the function returns ``"once"`` immediately without
        prompting.

        Inline-skip: if ``cmd_original`` contains ``now``, ``--yes``, or
        ``-y`` as an argument (e.g. ``/reset now``, ``/new --yes My title``),
        the modal is bypassed and ``"once"`` is returned immediately. This is
        an escape hatch for non-interactive use and for the degraded path where
        the modal can't be marshaled onto the app loop (native Windows itself now
        drives the modal normally — see #33961). Callers are responsible
        for stripping the skip tokens from any remaining argument parsing
        (see :meth:`_split_destructive_skip`).

        Returns ``"once"``, ``"always"``, or ``None`` (cancelled).  Callers
        proceed with the destructive action when the result is non-None.
        """
        # Inline-skip escape hatch — works regardless of platform/modal state.
        # See class-level _DESTRUCTIVE_SKIP_TOKENS for the accepted tokens.
        if cmd_original:
            _, _skip = self._split_destructive_skip(cmd_original)
            if _skip:
                return "once"

        # Gate check — respects prior "Always Approve" clicks.
        try:
            cfg = _shell.load_cli_config()
            approvals = cfg.get("approvals") if isinstance(cfg, dict) else None
            confirm_required = True
            if isinstance(approvals, dict):
                confirm_required = bool(approvals.get("destructive_slash_confirm", True))
        except Exception:
            confirm_required = True

        if not confirm_required:
            return "once"

        # Render a prompt_toolkit-native confirmation panel.  This keeps option
        # labels visible above the composer and avoids raw input()/EOF races with
        # the running TUI.
        choices = [
            ("once", "Approve Once", "proceed this time only"),
            ("always", "Always Approve", "proceed and silence this prompt permanently"),
            ("cancel", "Cancel", "keep current conversation"),
        ]
        raw = self._prompt_text_input_modal(
            title=f"⚠️  /{command} — destroys conversation state",
            detail=detail,
            choices=choices,
        )
        if raw is None:
            print(f"🟡 /{command} cancelled (no input).")
            return None
        choice = self._normalize_slash_confirm_choice(raw, choices)
        if choice is None:
            print(f"🟡 Unrecognized choice '{raw}'. /{command} cancelled.")
            return None

        if choice == "cancel":
            print(f"🟡 /{command} cancelled. Conversation unchanged.")
            return None

        if choice == "always":
            if _shell.save_config_value("approvals.destructive_slash_confirm", False):
                print("🔒 Future /clear, /new, /reset, and /undo will run without confirmation.")
                print("   Re-enable via `approvals.destructive_slash_confirm: true` in config.yaml.")
            else:
                print("⚠️  Couldn't persist opt-out — proceeding once.")

        return choice

    def _clarify_callback(self, question, choices):
        """
        Platform callback for the clarify tool. Called from the agent thread.

        Sets up the interactive selection UI (or freetext prompt for open-ended
        questions), then blocks until the user responds via the prompt_toolkit
        key bindings.  If no response arrives within the configured timeout the
        question is dismissed and the agent is told to decide on its own.
        """
        import time as _time

        from opencodon.tools.clarify_gateway import resolve_clarify_timeout

        # Canonical clarify timeout, shared with the gateway/TUI path. `<= 0`
        # means unlimited (never auto-skip mid-think) → a null deadline.
        timeout = resolve_clarify_timeout(_shell.CLI_CONFIG)
        response_queue = queue.Queue()
        is_open_ended = not choices

        self._clarify_state = {
            "question": question,
            "choices": choices if not is_open_ended else [],
            "selected": 0,
            "response_queue": response_queue,
        }
        self._clarify_deadline = None if timeout <= 0 else _time.monotonic() + timeout
        # Open-ended questions skip straight to freetext input
        self._clarify_freetext = is_open_ended

        # Trigger an immediate prompt_toolkit repaint from this (non-main)
        # thread. Modal prompts must paint at once and must not be gated by the
        # _invalidate throttle / resize guard — see _paint_now / _invalidate (#41098).
        self._paint_now()

        # Poll for the user's response. The countdown in the hint line updates
        # on each repaint; refresh it once a second so the timer stays visible
        # while we wait. Selection changes (↑/↓) trigger instant repaints via
        # the key bindings.
        _last_countdown_refresh = _time.monotonic()
        while True:
            try:
                result = response_queue.get(timeout=1)
                self._clarify_deadline = None
                self._persist_prompt_summary("?", "Clarify", question, str(result))
                return result
            except queue.Empty:
                # None deadline = unlimited: never auto-skip, just keep polling.
                if self._clarify_deadline is not None:
                    remaining = self._clarify_deadline - _time.monotonic()
                    if remaining <= 0:
                        break
                now = _time.monotonic()
                if now - _last_countdown_refresh >= 1.0:
                    _last_countdown_refresh = now
                    self._paint_now()

        # Timed out — tear down the UI and let the agent decide
        self._clarify_state = None
        self._clarify_freetext = False
        self._clarify_deadline = None
        self._paint_now()
        _shell._cprint(f"\n{_shell._DIM}(clarify timed out after {timeout}s — agent will decide){_shell._RST}")
        return (
            "The user did not provide a response within the time limit. "
            "Use your best judgement to make the choice and proceed."
        )

    def _sudo_password_callback(self) -> str:
        """
        Prompt for sudo password through the prompt_toolkit UI.
        
        Called from the agent thread when a sudo command is encountered.
        Uses the same clarify-style mechanism: sets UI state, waits on a
        queue for the user's response via the Enter key binding.
        """
        import time as _time

        timeout = 45
        response_queue = queue.Queue()

        self._capture_modal_input_snapshot()
        self._sudo_state = {
            "response_queue": response_queue,
        }
        self._sudo_deadline = _time.monotonic() + timeout

        # Modal prompt — paint immediately, bypassing the throttle/resize guard
        # so the prompt can't be dropped and time out unseen (#41098).
        self._paint_now()

        while True:
            try:
                result = response_queue.get(timeout=1)
                self._sudo_state = None
                self._sudo_deadline = 0
                self._restore_modal_input_snapshot()
                self._paint_now()
                if result:
                    _shell._cprint(f"\n{_shell._DIM}  ✓ Password received (cached for session){_shell._RST}")
                else:
                    _shell._cprint(f"\n{_shell._DIM}  ⏭ Skipped{_shell._RST}")
                return result
            except queue.Empty:
                remaining = self._sudo_deadline - _time.monotonic()
                if remaining <= 0:
                    break
                self._paint_now()

        self._sudo_state = None
        self._sudo_deadline = 0
        self._restore_modal_input_snapshot()
        self._paint_now()
        _shell._cprint(f"\n{_shell._DIM}  ⏱ Timeout — continuing without sudo{_shell._RST}")
        return ""

    def _approval_callback(self, command: str, description: str,
                           *, allow_permanent: bool = True,
                           smart_denied: bool = False) -> str:
        """
        Prompt for dangerous command approval through the prompt_toolkit UI.

        Called from the agent thread. Shows a selection UI similar to clarify
        with choices: once / session / always / deny. Smart DENY owner
        overrides show only once / deny. When allow_permanent is False for
        another reason (for example tirith), only 'always' is hidden.
        Long commands also get a 'view' option so the full command can be
        expanded before deciding.

        Uses _approval_lock to serialize concurrent requests (e.g. from
        parallel delegation subtasks) so each prompt gets its own turn
        and the shared _approval_state / _approval_deadline aren't clobbered.
        """
        import time as _time

        with self._approval_lock:
            timeout = int(_shell.CLI_CONFIG.get("approvals", {}).get("timeout", 300))
            response_queue = queue.Queue()

            self._approval_state = {
                "command": command,
                "description": description,
                "choices": self._approval_choices(
                    command,
                    allow_permanent=allow_permanent,
                    smart_denied=smart_denied,
                ),
                "selected": 0,
                "response_queue": response_queue,
            }
            self._approval_deadline = _time.monotonic() + timeout

            # Modal prompt — paint immediately, bypassing the throttle/resize
            # guard. A throttled paint here can be silently dropped (250ms
            # window collision or in-flight resize), leaving the panel unseen so
            # the command is denied on timeout without the user ever seeing it
            # (#41098). The countdown refreshes below paint the same way.
            self._paint_now()

            _last_countdown_refresh = _time.monotonic()
            while True:
                try:
                    result = response_queue.get(timeout=1)
                    self._approval_state = None
                    self._approval_deadline = 0
                    self._paint_now()
                    _outcome_labels = {
                        "once": "allowed once",
                        "session": "allowed for session",
                        "always": "added to allowlist",
                        "deny": "denied",
                    }
                    self._persist_prompt_summary(
                        "⚠", "Approval", command,
                        _outcome_labels.get(result, str(result)),
                    )
                    return result
                except queue.Empty:
                    remaining = self._approval_deadline - _time.monotonic()
                    if remaining <= 0:
                        break
                    now = _time.monotonic()
                    if now - _last_countdown_refresh >= 1.0:
                        _last_countdown_refresh = now
                        self._paint_now()

            self._approval_state = None
            self._approval_deadline = 0
            self._paint_now()
            _shell._cprint(f"\n{_shell._DIM}  ⏱ Timeout — denying command{_shell._RST}")
            return "deny"

    def _approval_choices(self, command: str, *, allow_permanent: bool = True,
                          smart_denied: bool = False) -> list[str]:
        """Return approval choices for a dangerous command prompt."""
        if smart_denied:
            choices = ["once", "deny"]
        else:
            choices = ["once", "session", "always", "deny"] if allow_permanent else ["once", "session", "deny"]
        if len(command) > 70:
            choices.append("view")
        return choices

    def _computer_use_approval_callback(self, action: str, args: dict, summary: str) -> str:
        """Adapt the generic approval UI for the computer_use tool.

        The computer_use handler expects verdicts of the form
        `approve_once` | `approve_session` | `always_approve` | `deny`.
        The CLI's built-in approval UI returns `once` | `session` | `always`
        | `deny`. Translate between the two.
        """
        # Build a command-ish string so the existing UI renders something
        # meaningful. `summary` is already a one-line human description.
        verdict = self._approval_callback(
            command=f"computer_use: {summary}",
            description=f"Allow computer_use to perform `{action}`?",
        )
        return {
            "once": "approve_once",
            "session": "approve_session",
            "always": "always_approve",
            "deny": "deny",
        }.get(verdict, "deny")

    def _handle_approval_selection(self) -> None:
        """Process the currently selected dangerous-command approval choice."""
        state = self._approval_state
        if not state:
            return

        selected = state.get("selected", 0)
        choices = state.get("choices")
        if not isinstance(choices, list):
            choices = []
        if not (0 <= selected < len(choices)):
            return

        chosen = choices[selected]
        if chosen == "view":
            state["show_full"] = True
            state["choices"] = [choice for choice in choices if choice != "view"]
            if state["selected"] >= len(state["choices"]):
                state["selected"] = max(0, len(state["choices"]) - 1)
            self._invalidate()
            return

        state["response_queue"].put(chosen)
        self._approval_state = None
        self._invalidate()

    def _get_approval_display_fragments(self):
        """Render the dangerous-command approval panel for the prompt_toolkit UI.

        Layout priority: title + command + choices must always render, even if
        the terminal is short or the description is long. Description is placed
        at the bottom of the panel and gets truncated to fit the remaining row
        budget. This prevents HSplit from clipping approve/deny off-screen when
        tirith findings produce multi-paragraph descriptions or when the user
        runs in a compact terminal pane.
        """
        state = self._approval_state
        if not state:
            return []

        def _panel_box_width(title_text: str, content_lines: list[str], min_width: int = 46, max_width: int = 76) -> int:
            term_cols = shutil.get_terminal_size((100, 20)).columns
            longest = max([len(title_text)] + [len(line) for line in content_lines] + [min_width - 4])
            inner = min(max(longest + 4, min_width - 2), max_width - 2, max(24, term_cols - 6))
            return inner + 2

        def _wrap_panel_text(text: str, width: int, subsequent_indent: str = "") -> list[str]:
            wrapped = textwrap.wrap(
                text,
                width=max(8, width),
                replace_whitespace=False,
                drop_whitespace=False,
                subsequent_indent=subsequent_indent,
            )
            return wrapped or [""]

        def _append_panel_line(lines, border_style: str, content_style: str, text: str, box_width: int) -> None:
            inner_width = max(0, box_width - 2)
            lines.append((border_style, "│ "))
            lines.append((content_style, text.ljust(inner_width)))
            lines.append((border_style, " │\n"))

        def _append_blank_panel_line(lines, border_style: str, box_width: int) -> None:
            lines.append((border_style, "│" + (" " * box_width) + "│\n"))

        command = state["command"]
        description = state["description"]
        choices = state["choices"]
        selected = state.get("selected", 0)
        show_full = state.get("show_full", False)

        title = "⚠️  Dangerous Command"
        cmd_display = command
        choice_labels = {
            "once": "Allow once",
            "session": "Allow for this session",
            "always": "Add to permanent allowlist",
            "deny": "Deny",
            "view": "Show full command",
        }

        preview_lines = _wrap_panel_text(description, 60)
        preview_lines.extend(_wrap_panel_text(cmd_display, 60))
        for i, choice in enumerate(choices):
            prefix = '❯ ' if i == selected else '  '
            preview_lines.extend(_wrap_panel_text(
                f"{prefix}{choice_labels.get(choice, choice)}",
                60,
                subsequent_indent="  ",
            ))

        box_width = _panel_box_width(title, preview_lines)
        inner_text_width = max(8, box_width - 2)

        # Pre-wrap the mandatory content — command + choices must always render.
        cmd_wrapped = _wrap_panel_text(cmd_display, inner_text_width)
        if not show_full and "view" in choices and len(cmd_wrapped) > 4:
            cmd_wrapped = cmd_wrapped[:3] + _wrap_panel_text(
                "… (choose Show full command)",
                inner_text_width,
            )

        # (choice_index, wrapped_line) so we can re-apply selected styling below
        choice_wrapped: list[tuple[int, str]] = []
        for i, choice in enumerate(choices):
            label = choice_labels.get(choice, choice)
            # Show number prefix for quick selection (1-9 for items 1-9, 0 for 10th item)
            if i < 9:
                num_prefix = str(i + 1)
            elif i == 9:
                num_prefix = '0'
            else:
                num_prefix = ' '  # No number for items beyond 10th
            if i == selected:
                prefix = f'❯ {num_prefix}. '
            else:
                prefix = f'  {num_prefix}. '
            for wrapped in _wrap_panel_text(f"{prefix}{label}", inner_text_width, subsequent_indent="    "):
                choice_wrapped.append((i, wrapped))

        # Budget vertical space so HSplit never clips the command or choices.
        # Panel chrome (full layout with separators):
        #   top border + title + blank_after_title
        #   + blank_between_cmd_choices + bottom border = 5 rows.
        # In tight terminals we collapse to:
        #   top border + title + bottom border = 3 rows (no blanks).
        #
        # reserved_below: rows consumed below the approval panel by the
        # spinner/tool-progress line, status bar, input area, separators, and
        # prompt symbol. Measured at ~6 rows during live PTY approval prompts;
        # budget 6 so we don't overestimate the panel's room.
        term_rows = shutil.get_terminal_size((100, 24)).lines
        chrome_full = 5
        chrome_tight = 3
        reserved_below = 6

        available = max(0, term_rows - reserved_below)
        mandatory_full = chrome_full + len(cmd_wrapped) + len(choice_wrapped)

        # If the full-chrome panel doesn't fit, drop the separator blanks.
        # This keeps the command and every choice on-screen in compact terminals.
        use_compact_chrome = mandatory_full > available
        chrome_rows = chrome_tight if use_compact_chrome else chrome_full

        # If the command itself is too long to leave room for choices (e.g. user
        # hit "view" on a multi-hundred-character command), truncate it so the
        # approve/deny buttons still render. Keep at least 1 row of command.
        max_cmd_rows = max(1, available - chrome_rows - len(choice_wrapped))
        if len(cmd_wrapped) > max_cmd_rows:
            keep = max(1, max_cmd_rows - 1) if max_cmd_rows > 1 else 1
            cmd_wrapped = cmd_wrapped[:keep] + _wrap_panel_text(
                "… (command truncated — use /logs or /debug for full text)",
                inner_text_width,
            )

        # Allocate any remaining rows to description. The extra -1 in full mode
        # accounts for the blank separator between choices and description.
        mandatory_no_desc = chrome_rows + len(cmd_wrapped) + len(choice_wrapped)
        desc_sep_cost = 0 if use_compact_chrome else 1
        available_for_desc = available - mandatory_no_desc - desc_sep_cost
        # Even on huge terminals, cap description height so the panel stays compact.
        available_for_desc = max(0, min(available_for_desc, 10))

        desc_wrapped = _wrap_panel_text(description, inner_text_width) if description else []
        if available_for_desc < 1 or not desc_wrapped:
            desc_wrapped = []
        elif len(desc_wrapped) > available_for_desc:
            keep = max(1, available_for_desc - 1)
            desc_wrapped = desc_wrapped[:keep] + ["… (description truncated)"]

        # Render: title → command → choices → description (description last so
        # any remaining overflow clips from the bottom of the least-critical
        # content, never from the command or choices). Use compact chrome (no
        # blank separators) when the terminal is tight.
        lines = []
        lines.append(('class:approval-border', '╭' + ('─' * box_width) + '╮\n'))
        _append_panel_line(lines, 'class:approval-border', 'class:approval-title', title, box_width)
        if not use_compact_chrome:
            _append_blank_panel_line(lines, 'class:approval-border', box_width)

        for wrapped in cmd_wrapped:
            _append_panel_line(lines, 'class:approval-border', 'class:approval-cmd', wrapped, box_width)
        if not use_compact_chrome:
            _append_blank_panel_line(lines, 'class:approval-border', box_width)

        for i, wrapped in choice_wrapped:
            style = 'class:approval-selected' if i == selected else 'class:approval-choice'
            _append_panel_line(lines, 'class:approval-border', style, wrapped, box_width)

        if desc_wrapped:
            if not use_compact_chrome:
                _append_blank_panel_line(lines, 'class:approval-border', box_width)
            for wrapped in desc_wrapped:
                _append_panel_line(lines, 'class:approval-border', 'class:approval-desc', wrapped, box_width)

        lines.append(('class:approval-border', '╰' + ('─' * box_width) + '╯\n'))
        return lines

    def _secret_capture_callback(self, var_name: str, prompt: str, metadata=None) -> dict:
        return _shell.prompt_for_secret(self, var_name, prompt, metadata)

    def _capture_modal_input_snapshot(self) -> None:
        """Temporarily clear the input buffer and save the user's in-progress draft."""
        if self._modal_input_snapshot is not None or not getattr(self, "_app", None):
            return
        try:
            buf = self._app.current_buffer
            self._modal_input_snapshot = {
                "text": buf.text,
                "cursor_position": buf.cursor_position,
            }
            buf.reset()
        except Exception:
            self._modal_input_snapshot = None

    def _restore_modal_input_snapshot(self) -> None:
        """Restore any draft text that was present before a modal prompt opened."""
        snapshot = self._modal_input_snapshot
        self._modal_input_snapshot = None
        if not snapshot or not getattr(self, "_app", None):
            return
        try:
            buf = self._app.current_buffer
            buf.text = snapshot.get("text", "")
            buf.cursor_position = min(snapshot.get("cursor_position", 0), len(buf.text))
        except Exception:
            pass

    def _clear_active_overlays_for_interrupt(self) -> None:
        """Drain and clear every input-blocking overlay left by an interrupted agent.

        approval/clarify/sudo/secret prompts each block a worker thread on a
        ``response_queue.get()``.  When the agent is interrupted the worker
        thread is torn down, but the overlay's state dict stays set — leaving
        the CLI input gated (``read_only`` condition + keypress filter) with no
        thread servicing the prompt.  The result is a frozen terminal until the
        prompt's own timeout expires.  Push a terminal value onto each queue so
        any still-blocked thread unblocks cleanly, then nil the state out and
        restore the user's pre-modal draft (#14026).

        Safe default per prompt: approval -> "deny", clarify/sudo/secret ->
        cancel (None / empty).  Each step is wrapped so a dead queue can't
        prevent clearing the others.
        """
        if self._approval_state:
            try:
                self._approval_state["response_queue"].put("deny")
            except Exception:
                pass
            self._approval_state = None
        if self._clarify_state:
            try:
                self._clarify_state["response_queue"].put(
                    "The user cancelled. Use your best judgement to proceed."
                )
            except Exception:
                pass
            self._clarify_state = None
            self._clarify_freetext = False
        if self._sudo_state:
            try:
                self._sudo_state["response_queue"].put("")
            except Exception:
                pass
            self._sudo_state = None
            self._sudo_deadline = 0
            self._restore_modal_input_snapshot()
        if self._secret_state:
            try:
                self._cancel_secret_capture()
            except Exception:
                self._secret_state = None

    def _submit_secret_response(self, value: str) -> None:
        if not self._secret_state:
            return
        self._secret_state["response_queue"].put(value)
        self._secret_state = None
        self._secret_deadline = 0
        # Modal teardown — paint directly so the secret panel clears at once and
        # isn't held by the _invalidate throttle/resize guard (#41098).
        self._paint_now()

    def _cancel_secret_capture(self) -> None:
        self._submit_secret_response("")

    def _clear_secret_input_buffer(self) -> None:
        if getattr(self, "_app", None):
            try:
                self._app.current_buffer.reset()
            except Exception:
                pass

