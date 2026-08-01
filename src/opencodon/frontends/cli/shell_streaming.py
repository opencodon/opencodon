"""OpencodonCLI ShellStreamingMixin — extracted from shell.py (restructure Phase 4).

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


class ShellStreamingMixin:
    def _on_thinking(self, text: str) -> None:
        """Called by agent when thinking starts/stops. Updates TUI spinner."""
        if not text:
            self._flush_reasoning_preview(force=True)
        self._spinner_text = text or ""
        self._tool_start_time = 0.0  # clear tool timer when switching to thinking
        self._invalidate()

    def _on_notice(self, notice) -> None:
        """Queue an out-of-band AgentNotice for rendering at the next clean boundary.

        Notices fire from inside the agent turn — printing immediately races the
        streaming response and the line gets buried behind the prompt (see _shell._cprint's
        bg-thread caveat). So we QUEUE here and flush in _flush_credit_notices(), called
        right after run_conversation returns. Fail-soft: never break the turn.
        """
        try:
            text = getattr(notice, "text", "") or ""
            if not text:
                return
            level = getattr(notice, "level", "info") or "info"
            if not hasattr(self, "_pending_credit_notices"):
                self._pending_credit_notices = []
            self._pending_credit_notices.append((level, text))
        except Exception:
            pass

    def _flush_credit_notices(self) -> None:
        """Print any queued credit notices as level-colored lines. Called at turn end
        (after run_conversation) where _shell._cprint paints cleanly above the prompt."""
        try:
            pending = getattr(self, "_pending_credit_notices", None)
            if not pending:
                return
            self._pending_credit_notices = []
            for level, text in pending:
                color = {
                    "error": "\033[31m",
                    "warn": "\033[33m",
                    "success": "\033[32m",
                    "info": _shell._DIM,
                }.get(level, _shell._DIM)
                _shell._cprint(f"  {color}{text}{_shell._RST}")
        except Exception:
            pass

    def _on_notice_clear(self, key: str) -> None:
        """Notice cleared. The REPL prints lines (no persistent slot to wipe), so
        this drops any still-queued notice with that key is not tracked by key here;
        it's a no-op for rendering — kept so the agent's clear callback is bound
        symmetrically with the show callback (and so future REPL UIs can hook it)."""
        return

    def _current_reasoning_callback(self):
        """Return the active reasoning display callback for the current mode."""
        if self.show_reasoning and self.streaming_enabled:
            return self._stream_reasoning_delta
        if self.verbose and not self.show_reasoning:
            return self._on_reasoning
        return None

    def _emit_reasoning_preview(self, reasoning_text: str) -> None:
        """Render a buffered reasoning preview as a single [thinking] block."""
        preview_text = reasoning_text.strip()
        if not preview_text:
            return

        try:
            term_width = shutil.get_terminal_size().columns
        except Exception:
            term_width = 80
        prefix = "  [thinking] "
        wrap_width = max(30, term_width - len(prefix) - 2)

        paragraphs = []
        raw_paragraphs = re.split(r"\n\s*\n+", preview_text.replace("\r\n", "\n"))
        for paragraph in raw_paragraphs:
            compact = " ".join(line.strip() for line in paragraph.splitlines() if line.strip())
            if compact:
                paragraphs.append(textwrap.fill(compact, width=wrap_width))
        preview_text = "\n".join(paragraphs)
        if not preview_text:
            return

        if self.verbose:
            _shell._cprint(f"  {_shell._DIM}[thinking] {preview_text}{_shell._RST}")
            return

        lines = preview_text.splitlines()
        if len(lines) > 5:
            preview = "\n".join(lines[:5])
            preview += f"\n  ... ({len(lines) - 5} more lines)"
        else:
            preview = preview_text
        _shell._cprint(f"  {_shell._DIM}[thinking] {preview}{_shell._RST}")

    def _flush_reasoning_preview(self, *, force: bool = False) -> None:
        """Flush buffered reasoning text at natural boundaries.

        Some providers stream reasoning in tiny word or punctuation chunks.
        Buffer them here so the preview path does not print one `[thinking]`
        line per token.
        """
        buf = getattr(self, "_reasoning_preview_buf", "")
        if not buf:
            return

        try:
            term_width = shutil.get_terminal_size().columns
        except Exception:
            term_width = 80
        target_width = max(40, term_width - len("  [thinking] ") - 4)

        flush_text = ""

        if force:
            flush_text = buf
            buf = ""
        else:
            line_break = buf.rfind("\n")
            min_newline_flush = max(16, target_width // 3)
            if line_break != -1 and (
                line_break >= min_newline_flush
                or buf.endswith("\n\n")
                or buf.endswith(".\n")
                or buf.endswith("!\n")
                or buf.endswith("?\n")
                or buf.endswith(":\n")
            ):
                flush_text = buf[: line_break + 1]
                buf = buf[line_break + 1 :]
            elif len(buf) >= target_width:
                search_start = max(20, target_width // 2)
                search_end = min(len(buf), max(target_width + (target_width // 3), target_width + 8))
                cut = -1
                for boundary in (" ", "\t", ".", "!", "?", ",", ";", ":"):
                    cut = max(cut, buf.rfind(boundary, search_start, search_end))
                if cut != -1:
                    flush_text = buf[: cut + 1]
                    buf = buf[cut + 1 :]

        self._reasoning_preview_buf = buf.lstrip() if flush_text else buf
        if flush_text:
            self._emit_reasoning_preview(flush_text)

    def _format_submitted_user_message_preview(self, user_input: str) -> str:
        """Format the submitted user-message scrollback preview."""
        ts_suffix = (
            f" [dim]{datetime.now().strftime(getattr(self, 'timestamp_format', '%H:%M'))}[/]"
            if getattr(self, "show_timestamps", False) else ""
        )
        lines = user_input.split("\n")
        if len(lines) <= 1:
            return f"[bold {_shell._accent_hex()}]●[/] [bold]{_shell._escape(user_input)}[/]{ts_suffix}"

        first_lines = int(getattr(self, "user_message_preview_first_lines", 2))
        last_lines = int(getattr(self, "user_message_preview_last_lines", 2))
        first_lines = max(1, first_lines)
        last_lines = max(0, last_lines)
        head = lines[:first_lines]
        remaining_after_head = max(0, len(lines) - len(head))
        tail_count = min(last_lines, remaining_after_head)
        tail = lines[-tail_count:] if tail_count else []

        hidden_middle_count = len(lines) - len(head) - len(tail)
        if hidden_middle_count < 0:
            hidden_middle_count = 0
            tail = []

        preview_lines = [
            f"[bold {_shell._accent_hex()}]●[/] [bold]{_shell._escape(head[0])}[/]{ts_suffix}"
        ]
        preview_lines.extend(f"[bold]{_shell._escape(line)}[/]" for line in head[1:])

        if hidden_middle_count > 0:
            noun = "line" if hidden_middle_count == 1 else "lines"
            preview_lines.append(f"[dim]... (+{hidden_middle_count} more {noun})[/]")

        preview_lines.extend(f"[bold]{_shell._escape(line)}[/]" for line in tail)
        return "\n".join(preview_lines)

    def _expand_paste_references(self, text: str | None) -> str:
        """Expand [Pasted text #N -> file] placeholders into file contents."""
        if not isinstance(text, str) or "[Pasted text #" not in text:
            return text or ""
        paste_ref_re = re.compile(r'\[Pasted text #\d+: \d+ lines \u2192 (.+?)\]')

        def _expand_ref(match):
            path = Path(match.group(1))
            # Use try/except instead of path.exists() to avoid TOCTOU race:
            # the paste file may be deleted between check and read, causing
            # the input to be silently dropped (#17666).
            try:
                return path.read_text(encoding="utf-8")
            except (OSError, IOError):
                _shell.logger.warning("Paste file gone or unreadable, returning placeholder: %s", path)
                return match.group(0)

        return paste_ref_re.sub(_expand_ref, text)

    def _print_user_message_preview(self, user_input: str) -> None:
        """Render a user message using the normal chat scrollback style."""
        _shell.ChatConsole().print(f"[{_shell._accent_hex()}]{'─' * 40}[/]")
        text = str(user_input or "")
        if "\n" in text:
            _shell.ChatConsole().print(self._format_submitted_user_message_preview(text))
        else:
            _shell.ChatConsole().print(f"[bold {_shell._accent_hex()}]●[/] [bold]{_shell._escape(text)}[/]")

    def _stream_reasoning_delta(self, text: str) -> None:
        """Stream reasoning/thinking tokens into a dim box above the response.

        Opens a dim reasoning box on first token, streams line-by-line.
        The box is closed automatically when content tokens start arriving
        (via _stream_delta → _emit_stream_text).

        Once the response box is open, suppress any further reasoning
        rendering — a late thinking block (e.g. after an interrupt) would
        otherwise draw a reasoning box inside the response box.
        """
        if not text:
            return
        self._reasoning_shown_this_turn = True
        if getattr(self, "_stream_box_opened", False):
            return

        # Open reasoning box on first reasoning token
        if not getattr(self, "_reasoning_box_opened", False):
            self._reasoning_box_opened = True
            w = self._scrollback_box_width()
            r_label = " Reasoning "
            r_fill = w - 2 - len(r_label)
            _shell._cprint(f"\n{_shell._DIM}┌─{r_label}{'─' * max(r_fill - 1, 0)}┐{_shell._RST}")

        self._reasoning_buf = getattr(self, "_reasoning_buf", "") + text

        # Emit complete lines, and force-flush long partial lines so
        # reasoning is visible in real-time even without newlines.
        while "\n" in self._reasoning_buf:
            line, self._reasoning_buf = self._reasoning_buf.split("\n", 1)
            _shell._cprint(f"{_shell._DIM}{line}{_shell._RST}")
        if len(self._reasoning_buf) > 80:
            _shell._cprint(f"{_shell._DIM}{self._reasoning_buf}{_shell._RST}")
            self._reasoning_buf = ""

    def _close_reasoning_box(self) -> None:
        """Close the live reasoning box if it's open."""
        if getattr(self, "_reasoning_box_opened", False):
            # Flush remaining reasoning buffer
            buf = getattr(self, "_reasoning_buf", "")
            if buf:
                _shell._cprint(f"{_shell._DIM}{buf}{_shell._RST}")
                self._reasoning_buf = ""
            w = self._scrollback_box_width()
            _shell._cprint(f"{_shell._DIM}└{'─' * (w - 2)}┘{_shell._RST}")
            self._reasoning_box_opened = False

            # Flush any content that was deferred while reasoning was rendering.
            deferred = getattr(self, "_deferred_content", "")
            if deferred:
                self._deferred_content = ""
                self._emit_stream_text(deferred)

    def _stream_delta(self, text) -> None:
        """Line-buffered streaming callback for real-time token rendering.

        Receives text deltas from the agent as tokens arrive. Buffers
        partial lines and emits complete lines via _shell._cprint to work
        reliably with prompt_toolkit's patch_stdout.

        Reasoning/thinking blocks (<REASONING_SCRATCHPAD>, <think>, etc.)
        are suppressed during streaming since they'd display raw XML tags.
        The agent strips them from the final response anyway.

        A ``None`` value signals an intermediate turn boundary (tools are
        about to execute).  Flushes any open boxes and resets state so
        tool feed lines render cleanly between turns.
        """
        if text is None:
            self._flush_stream()
            self._reset_stream_state()
            return
        if not text:
            return

        self._stream_started = True

        # ── Tag-based reasoning suppression ──
        # Track whether we're inside a reasoning/thinking block.
        # These tags are model-generated (system prompt tells the model
        # to use them) and get stripped from final_response. We must
        # suppress them during streaming too — unless show_reasoning is
        # enabled, in which case we route the inner content to the
        # reasoning display box instead of discarding it.
        _OPEN_TAGS = ("<REASONING_SCRATCHPAD>", "<think>", "<reasoning>", "<THINKING>", "<thinking>", "<thought>")
        _CLOSE_TAGS = ("</REASONING_SCRATCHPAD>", "</think>", "</reasoning>", "</THINKING>", "</thinking>", "</thought>")

        # Append to a pre-filter buffer first
        self._stream_prefilt = getattr(self, "_stream_prefilt", "") + text

        # Check if we're entering a reasoning block.
        # Only match tags that appear at a "block boundary": start of the
        # stream, after a newline (with optional whitespace), or when nothing
        # but whitespace has been emitted on the current line.
        # This prevents false positives when models *mention* tags in prose
        # like "(/think not producing <think> tags)".
        #
        # _stream_last_was_newline tracks whether the last character emitted
        # (or the start of the stream) is a line boundary.  It's True at
        # stream start and set True whenever emitted text ends with '\n'.
        if not hasattr(self, "_stream_last_was_newline"):
            self._stream_last_was_newline = True  # start of stream = boundary

        if not getattr(self, "_in_reasoning_block", False):
            # Case-insensitive matching against a lowercased view so
            # mixed-case tag variants (<Think>, <THINKING>, …) are caught.
            prefilt_lower = self._stream_prefilt.lower()
            for tag in _OPEN_TAGS:
                tag_lower = tag.lower()
                search_start = 0
                while True:
                    idx = prefilt_lower.find(tag_lower, search_start)
                    if idx == -1:
                        break
                    # Check if this is a block boundary position
                    preceding = self._stream_prefilt[:idx]
                    if idx == 0:
                        # At buffer start — only a boundary if we're at
                        # a line start (stream start or last emit ended
                        # with newline)
                        is_block_boundary = getattr(self, "_stream_last_was_newline", True)
                    else:
                        # Find last newline in the buffer before the tag
                        last_nl = preceding.rfind("\n")
                        if last_nl == -1:
                            # No newline in buffer — boundary only if
                            # last emit was a newline AND only whitespace
                            # has accumulated before the tag
                            is_block_boundary = (
                                getattr(self, "_stream_last_was_newline", True)
                                and preceding.strip() == ""
                            )
                        else:
                            # Text between last newline and tag must be
                            # whitespace-only
                            is_block_boundary = preceding[last_nl + 1:].strip() == ""
                    if is_block_boundary:
                        # Emit everything before the tag
                        if preceding:
                            self._emit_stream_text(preceding)
                            self._stream_last_was_newline = preceding.endswith("\n")
                        self._in_reasoning_block = True
                        self._stream_prefilt = self._stream_prefilt[idx + len(tag):]
                        break
                    # Not a block boundary — keep searching after this occurrence
                    search_start = idx + 1
                if getattr(self, "_in_reasoning_block", False):
                    break

            # Could also be a partial open tag at the end — hold it back
            if not getattr(self, "_in_reasoning_block", False):
                # Check for partial tag match at the end (case-insensitive)
                safe = self._stream_prefilt
                for tag in _OPEN_TAGS:
                    tag_lower = tag.lower()
                    for i in range(1, len(tag)):
                        if prefilt_lower.endswith(tag_lower[:i]):
                            safe = self._stream_prefilt[:-i]
                            break
                if safe:
                    self._emit_stream_text(safe)
                    self._stream_last_was_newline = safe.endswith("\n")
                    self._stream_prefilt = self._stream_prefilt[len(safe):]
                return

        # Inside a reasoning block — look for close tag.
        # Keep accumulating _stream_prefilt because close tags can arrive
        # split across multiple tokens (e.g. "</REASONING_SCRATCH" + "PAD>...").
        if getattr(self, "_in_reasoning_block", False):
            prefilt_lower = self._stream_prefilt.lower()
            for tag in _CLOSE_TAGS:
                idx = prefilt_lower.find(tag.lower())
                if idx != -1:
                    self._in_reasoning_block = False
                    # When show_reasoning is on, route inner content to
                    # the reasoning display box instead of discarding.
                    if self.show_reasoning:
                        inner = self._stream_prefilt[:idx]
                        if inner:
                            self._stream_reasoning_delta(inner)
                    after = self._stream_prefilt[idx + len(tag):]
                    self._stream_prefilt = ""
                    # Process remaining text after close tag through full
                    # filtering (it could contain another open tag)
                    if after:
                        self._stream_delta(after)
                    return
            # When show_reasoning is on, stream reasoning content live
            # instead of silently accumulating. Keep only the tail that
            # could be a partial close tag prefix.
            max_tag_len = max(len(t) for t in _CLOSE_TAGS)
            if len(self._stream_prefilt) > max_tag_len:
                if self.show_reasoning:
                    # Route the safe prefix to reasoning display
                    safe_reasoning = self._stream_prefilt[:-max_tag_len]
                    self._stream_reasoning_delta(safe_reasoning)
                self._stream_prefilt = self._stream_prefilt[-max_tag_len:]
            return

    def _flush_stream(self) -> None:
        """Emit any remaining partial line from the stream buffer and close the box."""
        # If we're still inside a "reasoning block" at end-of-stream, it was
        # a false positive — the model mentioned a tag like <think> in prose
        # but never closed it.  Recover the buffered content as regular text.
        if getattr(self, "_in_reasoning_block", False) and getattr(self, "_stream_prefilt", ""):
            self._in_reasoning_block = False
            self._emit_stream_text(self._stream_prefilt)
            self._stream_prefilt = ""

        # Close reasoning box if still open (in case no content tokens arrived)
        self._close_reasoning_box()

        _tc = getattr(self, "_stream_text_ansi", "")

        # If the stream buffer has a trailing partial line that looks like
        # a table row, fold it into the table buffer so the whole block
        # gets re-aligned together.  Otherwise the final row prints raw
        # (with the model's original under-padded spacing) while the rows
        # above it are aligned.
        if (
            self._stream_buf
            and getattr(self, "_in_stream_table", False)
            and (_shell.looks_like_table_row(self._stream_buf) or _shell.is_table_divider(self._stream_buf))
        ):
            self._stream_table_buf.append(self._stream_buf)
            self._stream_buf = ""

        # Flush any buffered table rows first so their padding is
        # finalised before the stream remainder lands.
        if getattr(self, "_stream_table_buf", None):
            joined = "\n".join(self._stream_table_buf)
            self._stream_table_buf = []
            self._in_stream_table = False
            if self.final_response_markdown == "strip":
                joined = _shell._strip_markdown_syntax(joined)
            block = _shell.realign_markdown_tables(joined, _shell._terminal_width_for_streaming())
            for ln in block.split("\n"):
                _shell._cprint(f"{_shell._STREAM_PAD}{_tc}{ln}{_shell._RST}" if _tc else f"{_shell._STREAM_PAD}{ln}")

        if self._stream_buf:
            line = _shell._strip_markdown_syntax(self._stream_buf) if self.final_response_markdown == "strip" else self._stream_buf
            _shell._cprint(f"{_shell._STREAM_PAD}{_tc}{line}{_shell._RST}" if _tc else f"{_shell._STREAM_PAD}{line}")
            self._stream_buf = ""

        # Close the response box
        if self._stream_box_opened:
            w = self._scrollback_box_width()
            _shell._cprint(f"{_shell._ACCENT}╰{'─' * (w - 2)}╯{_shell._RST}")

    def _reset_stream_state(self) -> None:
        """Reset streaming state before each agent invocation."""
        self._stream_buf = ""
        self._stream_started = False
        self._stream_box_opened = False
        self._stream_text_ansi = ""
        self._stream_prefilt = ""
        self._in_reasoning_block = False
        self._stream_last_was_newline = True
        self._reasoning_box_opened = False
        self._reasoning_buf = ""
        self._reasoning_preview_buf = ""
        self._deferred_content = ""
        self._stream_table_buf = []
        self._in_stream_table = False

    def _on_reasoning(self, reasoning_text: str):
        """Callback for intermediate reasoning display during tool-call loops."""
        if not reasoning_text:
            return
        self._reasoning_preview_buf = getattr(self, "_reasoning_preview_buf", "") + reasoning_text
        self._flush_reasoning_preview(force=False)

