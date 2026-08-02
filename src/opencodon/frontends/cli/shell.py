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

_opencodon_home = get_opencodon_home()
_project_env = Path(__file__).parent / '.env'
load_opencodon_dotenv(opencodon_home=_opencodon_home, project_env=_project_env)


_REASONING_TAGS = (
    "REASONING_SCRATCHPAD",
    "think",
    "thinking",
    "reasoning",
    "thought",
)


def _strip_reasoning_tags(text: str) -> str:
    """Remove reasoning/thinking blocks from displayed text.

    Handles every case:
      * Closed pairs ``<tag>…</tag>`` (case-insensitive, multi-line).
      * Unterminated open tags that run to end-of-text (e.g. truncated
        generations on NIM/MiniMax where the close tag is dropped).
      * Stray orphan close tags (``stuff</think>answer``) left behind by
        partial-content dumps.

    Covers the variants emitted by reasoning models today: ``<think>``,
    ``<thinking>``, ``<reasoning>``, ``<REASONING_SCRATCHPAD>``, and
    ``<thought>`` (Gemma 4).  Must stay in sync with
    ``run_agent.py::_strip_think_blocks`` and the stream consumer's
    ``_OPEN_THINK_TAGS`` / ``_CLOSE_THINK_TAGS`` tuples.

    Also strips tool-call XML blocks some open models leak into visible
    content (``<tool_call>``, ``<function_calls>``, Gemma-style
    ``<function name="…">…</function>``). Ported from
    openclaw/openclaw#67318.
    """
    cleaned = text
    for tag in _REASONING_TAGS:
        # Closed pair — case-insensitive so <THINK>…</THINK> is handled too.
        cleaned = re.sub(
            rf"<{tag}>.*?</{tag}>\s*",
            "",
            cleaned,
            flags=re.DOTALL | re.IGNORECASE,
        )
        # Unterminated open tag — strip from the tag to end of text.
        cleaned = re.sub(
            rf"<{tag}>.*$",
            "",
            cleaned,
            flags=re.DOTALL | re.IGNORECASE,
        )
        # Stray orphan close tag left behind by partial dumps.
        cleaned = re.sub(
            rf"</{tag}>\s*",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
    # Tool-call XML blocks (openclaw/openclaw#67318).
    for tc_tag in ("tool_call", "tool_calls", "tool_result",
                   "function_call", "function_calls"):
        cleaned = re.sub(
            rf"<{tc_tag}\b[^>]*>.*?</{tc_tag}>\s*",
            "",
            cleaned,
            flags=re.DOTALL | re.IGNORECASE,
        )
    # <function name="..."> — boundary + attribute gated to avoid prose FPs.
    cleaned = re.sub(
        r'(?:(?<=^)|(?<=[\n\r.!?:]))[ \t]*'
        r'<function\b[^>]*\bname\s*=[^>]*>'
        r'(?:(?:(?!</function>).)*)</function>\s*',
        '',
        cleaned,
        flags=re.DOTALL | re.IGNORECASE,
    )
    # Stray tool-call close tags.
    cleaned = re.sub(
        r'</(?:tool_call|tool_calls|tool_result|function_call|function_calls|function)>\s*',
        '',
        cleaned,
        flags=re.IGNORECASE,
    )
    return cleaned.strip()


def _assistant_content_as_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        return "\n".join(p for p in parts if p)
    return str(content)


def _assistant_copy_text(content: Any) -> str:
    return _strip_reasoning_tags(_assistant_content_as_text(content))


# =============================================================================
# Configuration Loading
# =============================================================================

def _load_prefill_messages(file_path: str) -> List[Dict[str, Any]]:
    """Load ephemeral prefill messages from a JSON file.
    
    The file should contain a JSON array of {role, content} dicts, e.g.:
        [{"role": "user", "content": "Hi"}, {"role": "assistant", "content": "Hello!"}]
    
    Relative paths are resolved from ~/.opencodon/.
    Returns an empty list if the path is empty or the file doesn't exist.
    """
    if not file_path:
        return []
    path = Path(file_path).expanduser()
    if not path.is_absolute():
        path = _opencodon_home / path
    if not path.exists():
        logger.warning("Prefill messages file not found: %s", path)
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            logger.warning("Prefill messages file must contain a JSON array: %s", path)
            return []
        return data
    except Exception as e:
        logger.warning("Failed to load prefill messages from %s: %s", path, e)
        return []


def _resolve_prefill_messages_file(config: Dict[str, Any]) -> str:
    """Resolve the prefill file path from env/config.

    ``prefill_messages_file`` at the top level is the canonical config key.
    ``agent.prefill_messages_file`` remains a legacy fallback for older CLI and
    godmode-generated configs.
    """
    env_path = os.getenv("OPENCODON_PREFILL_MESSAGES_FILE", "").strip()
    if env_path:
        return env_path
    top_level = str(config.get("prefill_messages_file", "") or "").strip()
    if top_level:
        return top_level
    agent_cfg = config.get("agent", {})
    if isinstance(agent_cfg, dict):
        return str(agent_cfg.get("prefill_messages_file", "") or "").strip()
    return ""


def _parse_reasoning_config(effort) -> dict | None:
    """Parse a reasoning effort level into an OpenRouter reasoning config dict.

    Accepts the raw config value (string or YAML boolean — ``false``/``off``
    parse as thinking disabled, see parse_reasoning_effort).
    """
    from opencodon_constants import parse_reasoning_effort
    result = parse_reasoning_effort(effort)
    if effort and str(effort).strip() and result is None:
        logger.warning("Unknown reasoning_effort '%s', using default (medium)", effort)
    return result


def _parse_service_tier_config(raw: str) -> str | None:
    """Parse a persisted service-tier preference into a Responses API value."""
    value = str(raw or "").strip().lower()
    if not value or value in {"normal", "default", "standard", "off", "none"}:
        return None
    if value in {"fast", "priority", "on"}:
        return "priority"
    logger.warning("Unknown service_tier '%s', ignoring", raw)
    return None

def load_cli_config() -> Dict[str, Any]:
    """
    Load CLI configuration from config files.
    
    Config lookup order:
    1. ~/.opencodon/config.yaml (user config - preferred)
    2. ./cli-config.yaml (project config - fallback)
    
    Environment variables take precedence over config file values.
    Returns default values if no config file exists.

    If OPENCODON_IGNORE_USER_CONFIG=1 is set (via ``opencodon chat --ignore-user-config``),
    the user config at ``~/.opencodon/config.yaml`` is skipped entirely and only the
    built-in defaults plus the project-level ``cli-config.yaml`` (if any) are used.
    Credentials in ``.env`` are still loaded — this flag only suppresses
    behavioral/config settings.
    """
    # Check user config first ({OPENCODON_HOME}/config.yaml)
    user_config_path = _opencodon_home / 'config.yaml'
    project_config_path = Path(__file__).parent / 'cli-config.yaml'

    # --ignore-user-config: force-skip the user config.yaml (still honor project
    # config as a fallback so defaults stay sensible).
    ignore_user_config = os.environ.get("OPENCODON_IGNORE_USER_CONFIG") == "1"

    # Use user config if it exists, otherwise project config
    if user_config_path.exists() and not ignore_user_config:
        config_path = user_config_path
    else:
        config_path = project_config_path

    # Default configuration
    defaults = {
        "model": {
            "default": "",
            "base_url": "",
            "provider": "auto",
        },
        "terminal": {
            "env_type": "local",
            "cwd": ".",  # "." is resolved to os.getcwd() at runtime
            "home_mode": "auto",
            "lifetime_seconds": 300,
            "docker_image": "nikolaik/python-nodejs:python3.11-nodejs20",
            "docker_forward_env": [],
            "singularity_image": "docker://nikolaik/python-nodejs:python3.11-nodejs20",
            "modal_image": "nikolaik/python-nodejs:python3.11-nodejs20",
            "daytona_image": "nikolaik/python-nodejs:python3.11-nodejs20",
            "docker_volumes": [],  # host:container volume mounts for Docker backend
            "docker_mount_cwd_to_workspace": False,  # explicit opt-in only; default off for sandbox isolation
        },
        "browser": {
            "inactivity_timeout": 120,  # Auto-cleanup inactive browser sessions after 2 min
            "record_sessions": False,  # Auto-record browser sessions as WebM videos
            "engine": "auto",  # Browser engine: auto (Chrome), lightpanda, chrome
            "camofox": {
                "rewrite_loopback_urls": False,
                "loopback_host_alias": "host.docker.internal",
            },
        },
        "compression": {
            "enabled": True,      # Auto-compress when approaching context limit
            "threshold": 0.50,    # Compress at 50% of model's context limit
        },
        "agent": {
            "max_turns": 90,  # Default max tool-calling iterations (shared with subagents)
            "verbose": False,
            "system_prompt": "",
            "prefill_messages_file": "",
            "reasoning_effort": "",
            "service_tier": "",
            "personalities": {
                "helpful": "You are a helpful, friendly AI assistant.",
                "concise": "You are a concise assistant. Keep responses brief and to the point.",
                "technical": "You are a technical expert. Provide detailed, accurate technical information.",
                "creative": "You are a creative assistant. Think outside the box and offer innovative solutions.",
                "teacher": "You are a patient teacher. Explain concepts clearly with examples.",
                "kawaii": "You are a kawaii assistant! Use cute expressions like (◕‿◕), ★, ♪, and ~! Add sparkles and be super enthusiastic about everything! Every response should feel warm and adorable desu~! ヽ(>∀<☆)ノ",
                "catgirl": "You are Neko-chan, an anime catgirl AI assistant, nya~! Add 'nya' and cat-like expressions to your speech. Use kaomoji like (=^･ω･^=) and ฅ^•ﻌ•^ฅ. Be playful and curious like a cat, nya~!",
                "pirate": "Arrr! Ye be talkin' to Captain Opencodon, the most tech-savvy pirate to sail the digital seas! Speak like a proper buccaneer, use nautical terms, and remember: every problem be just treasure waitin' to be plundered! Yo ho ho!",
                "shakespeare": "Hark! Thou speakest with an assistant most versed in the bardic arts. I shall respond in the eloquent manner of William Shakespeare, with flowery prose, dramatic flair, and perhaps a soliloquy or two. What light through yonder terminal breaks?",
                "surfer": "Duuude! You're chatting with the chillest AI on the web, bro! Everything's gonna be totally rad. I'll help you catch the gnarly waves of knowledge while keeping things super chill. Cowabunga!",
                "noir": "The rain hammered against the terminal like regrets on a guilty conscience. They call me Opencodon - I solve problems, find answers, dig up the truth that hides in the shadows of your codebase. In this city of silicon and secrets, everyone's got something to hide. What's your story, pal?",
                "uwu": "hewwo! i'm your fwiendwy assistant uwu~ i wiww twy my best to hewp you! *nuzzles your code* OwO what's this? wet me take a wook! i pwomise to be vewy hewpful >w<",
                "philosopher": "Greetings, seeker of wisdom. I am an assistant who contemplates the deeper meaning behind every query. Let us examine not just the 'how' but the 'why' of your questions. Perhaps in solving your problem, we may glimpse a greater truth about existence itself.",
                "hype": "YOOO LET'S GOOOO!!! I am SO PUMPED to help you today! Every question is AMAZING and we're gonna CRUSH IT together! This is gonna be LEGENDARY! ARE YOU READY?! LET'S DO THIS!",
            },
        },

        "display": {
            "compact": False,
            "resume_display": "full",
            # Recap tuning for /resume — see opencodon_cli/config.py DEFAULT_CONFIG.
            "resume_exchanges": 10,
            "resume_max_user_chars": 300,
            "resume_max_assistant_chars": 200,
            "resume_max_assistant_lines": 3,
            "resume_skip_tool_only": True,
            # Live reasoning display default ON — keep in sync with
            # opencodon_cli/config.py DEFAULT_CONFIG (display.show_reasoning).
            "show_reasoning": True,
            "reasoning_full": False,
            "streaming": True,
            "busy_input_mode": "interrupt",
            "persistent_output": True,
            "persistent_output_max_lines": 200,
            # Print a one-line summary of resolved modal prompts (approval /
            # clarify) into scrollback so the decision survives the repaint.
            "persist_prompts": True,

            "skin": "default",
        },
        "clarify": {
            "timeout": 120,  # Seconds to wait for a clarify answer before auto-proceeding
        },
        "code_execution": {
            "timeout": 300,    # Max seconds a sandbox script can run before being killed (5 min)
            "max_tool_calls": 50,  # Max RPC tool calls per execution
        },
        "auxiliary": {
            "vision": {
                "provider": "auto",
                "model": "",
                "base_url": "",
                "api_key": "",
            },
            "web_extract": {
                "provider": "auto",
                "model": "",
                "base_url": "",
                "api_key": "",
            },
        },
        "delegation": {
            "max_iterations": 45,  # Max tool-calling turns per child agent
            "model": "",       # Subagent model override (empty = inherit parent model)
            "provider": "",    # Subagent provider override (empty = inherit parent provider)
            "base_url": "",    # Direct OpenAI-compatible endpoint for subagents
            "api_key": "",     # API key for delegation.base_url (falls back to OPENAI_API_KEY)
        },
        "onboarding": {
            # First-touch hint flags (see agent/onboarding.py).  Each hint is
            # shown once per install then latched here.
            "seen": {},
        },
    }
    
    # Track whether the config file explicitly set terminal config.
    # When using defaults (no config file / no terminal section), we should NOT
    # overwrite env vars that were already set by .env -- only a user's config
    # file should be authoritative.
    _file_has_terminal_config = False

    # Load from file if exists
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                from opencodon.config import _normalize_root_model_keys

                file_config = _normalize_root_model_keys(fast_safe_load(f) or {})
            
            _file_has_terminal_config = "terminal" in file_config

            # Handle model config - can be string (new format) or dict (old format)
            if "model" in file_config:
                if isinstance(file_config["model"], str):
                    # New format: model is just a string, convert to dict structure
                    defaults["model"]["default"] = file_config["model"]
                elif isinstance(file_config["model"], dict):
                    # Old format: model is a dict with default/base_url
                    defaults["model"].update(file_config["model"])
                    # If the user config sets model.model but not model.default,
                    # promote model.model to model.default so the user's explicit
                    # choice isn't shadowed by the hardcoded default.  Without this,
                    # profile configs that only set "model:" (not "default:") silently
                    # fall back to claude-opus because the merge preserves the
                    # hardcoded default and OpencodonCLI.__init__ checks "default" first.
                    if "model" in file_config["model"] and "default" not in file_config["model"]:
                        defaults["model"]["default"] = file_config["model"]["model"]

            # Deep merge file_config into defaults.
            # First: merge keys that exist in both (deep-merge dicts, overwrite scalars)
            for key in defaults:
                if key == "model":
                    continue  # Already handled above
                if key in file_config:
                    if isinstance(defaults[key], dict) and file_config[key] is None:
                        continue
                    if isinstance(defaults[key], dict) and isinstance(file_config[key], dict):
                        defaults[key].update(file_config[key])
                    else:
                        defaults[key] = file_config[key]
            
            # Second: carry over keys from file_config that aren't in defaults
            # (e.g. platform_toolsets, provider_routing, memory, etc.)
            for key in file_config:
                if key not in defaults and key != "model":
                    defaults[key] = file_config[key]
            
            # Handle legacy root-level max_turns (backwards compat) - copy to
            # agent.max_turns whenever the nested key is missing.
            agent_file_config = file_config.get("agent")
            if "max_turns" in file_config and not (
                isinstance(agent_file_config, dict)
                and agent_file_config.get("max_turns") is not None
            ):
                defaults["agent"]["max_turns"] = file_config["max_turns"]
        except Exception as e:
            logger.warning("Failed to load cli-config.yaml: %s", e)

    # Expand ${ENV_VAR} references in config values before bridging to env vars.
    from opencodon.config import _expand_env_vars
    defaults = _expand_env_vars(defaults)

    # Managed scope: overlay administrator-pinned values LAST so they win over
    # the user's config here too. cli.py builds its config independently of
    # opencodon.config._load_config_impl (which has its own managed merge), so
    # without this the entire interactive CLI/TUI surface — skin, display prefs,
    # etc. read from CLI_CONFIG — would silently ignore managed scope while
    # `opencodon config`/`doctor`/guards (which use load_config) honor it. The
    # shared helper mirrors _load_config_impl (env-only expansion, root-model
    # normalization, leaf-merge) and is fail-open.
    from opencodon.config import managed_scope

    defaults = managed_scope.apply_managed_overlay(defaults)

    # Apply terminal config to environment variables (so terminal_tool picks them up)
    terminal_config = defaults.get("terminal", {})
    
    # Normalize config key: the new config system (opencodon_cli/config.py) and all
    # documentation use "backend", the legacy cli-config.yaml uses "env_type".
    # Accept both, with "backend" taking precedence (it's the documented key).
    if "backend" in terminal_config:
        terminal_config["env_type"] = terminal_config["backend"]
    
    # CWD resolution for CLI/TUI. The gateway has its own config bridge in
    # gateway/run.py but may lazily import cli.py (triggering this code).
    # Local backend: always os.getcwd(). Use `cd /dir && opencodon` to control it.
    # Non-local with placeholder: pop so terminal_tool uses its per-backend default.
    # Non-local with explicit path: keep as-is.
    _CWD_PLACEHOLDERS = (".", "auto", "cwd")
    effective_backend = terminal_config.get("env_type", "local")

    if effective_backend == "local":
        terminal_config["cwd"] = os.getcwd()
        defaults["terminal"]["cwd"] = terminal_config["cwd"]
    elif terminal_config.get("cwd") in _CWD_PLACEHOLDERS:
        terminal_config.pop("cwd", None)
    
    env_mappings = {
        "env_type": "TERMINAL_ENV",
        "cwd": "TERMINAL_CWD",
        "timeout": "TERMINAL_TIMEOUT",
        "home_mode": "TERMINAL_HOME_MODE",
        "lifetime_seconds": "TERMINAL_LIFETIME_SECONDS",
        "docker_image": "TERMINAL_DOCKER_IMAGE",
        "docker_forward_env": "TERMINAL_DOCKER_FORWARD_ENV",
        "singularity_image": "TERMINAL_SINGULARITY_IMAGE",
        "modal_image": "TERMINAL_MODAL_IMAGE",
        "daytona_image": "TERMINAL_DAYTONA_IMAGE",
        # SSH config
        "ssh_host": "TERMINAL_SSH_HOST",
        "ssh_user": "TERMINAL_SSH_USER",
        "ssh_port": "TERMINAL_SSH_PORT",
        "ssh_key": "TERMINAL_SSH_KEY",
        # Container resource config (docker, singularity, modal, daytona -- ignored for local/ssh)
        "container_cpu": "TERMINAL_CONTAINER_CPU",
        "container_memory": "TERMINAL_CONTAINER_MEMORY",
        "container_disk": "TERMINAL_CONTAINER_DISK",
        "container_persistent": "TERMINAL_CONTAINER_PERSISTENT",
        "docker_volumes": "TERMINAL_DOCKER_VOLUMES",
        "docker_env": "TERMINAL_DOCKER_ENV",
        "docker_extra_args": "TERMINAL_DOCKER_EXTRA_ARGS",
        "docker_mount_cwd_to_workspace": "TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE",
        "docker_network": "TERMINAL_DOCKER_NETWORK",
        "docker_run_as_host_user": "TERMINAL_DOCKER_RUN_AS_HOST_USER",
        "docker_persist_across_processes": "TERMINAL_DOCKER_PERSIST_ACROSS_PROCESSES",
        "docker_orphan_reaper": "TERMINAL_DOCKER_ORPHAN_REAPER",
        "sandbox_dir": "TERMINAL_SANDBOX_DIR",
        # Persistent shell (non-local backends)
        "persistent_shell": "TERMINAL_PERSISTENT_SHELL",
        # Sudo support (works with all backends)
        "sudo_password": "SUDO_PASSWORD",
    }
    
    # Bridge config → env vars for terminal_tool. TERMINAL_CWD is force-exported
    # UNLESS we're inside a gateway process (detected by _OPENCODON_GATEWAY marker)
    # where it was already set correctly by gateway/run.py's config bridge.
    _is_gateway = os.environ.get("_OPENCODON_GATEWAY") == "1"
    for config_key, env_var in env_mappings.items():
        if config_key in terminal_config:
            if env_var == "TERMINAL_CWD":
                if _is_gateway:
                    continue
                # CLI: always export (overrides stale .env or inherited values)
                os.environ[env_var] = str(terminal_config[config_key])
                continue
            if _file_has_terminal_config or env_var not in os.environ:
                val = terminal_config[config_key]
                if isinstance(val, (list, dict)):
                    os.environ[env_var] = json.dumps(val)
                else:
                    os.environ[env_var] = str(val)
    
    # Apply browser config to environment variables
    browser_config = defaults.get("browser", {})
    browser_env_mappings = {
        "inactivity_timeout": "BROWSER_INACTIVITY_TIMEOUT",
    }
    
    for config_key, env_var in browser_env_mappings.items():
        if config_key in browser_config:
            os.environ[env_var] = str(browser_config[config_key])
    
    # Apply auxiliary model/direct-endpoint overrides to environment variables.
    # Vision and web_extract each have their own provider/model/base_url/api_key tuple.
    # Compression config is read directly from config.yaml by run_agent.py and
    # auxiliary_client.py — no env var bridging needed.
    # Only set env vars for non-empty / non-default values so auto-detection
    # still works.
    auxiliary_config = defaults.get("auxiliary", {})
    auxiliary_task_env = {
        # config key → env var mapping
        "vision": {
            "provider": "AUXILIARY_VISION_PROVIDER",
            "model": "AUXILIARY_VISION_MODEL",
            "base_url": "AUXILIARY_VISION_BASE_URL",
            "api_key": "AUXILIARY_VISION_API_KEY",
        },
        "web_extract": {
            "provider": "AUXILIARY_WEB_EXTRACT_PROVIDER",
            "model": "AUXILIARY_WEB_EXTRACT_MODEL",
            "base_url": "AUXILIARY_WEB_EXTRACT_BASE_URL",
            "api_key": "AUXILIARY_WEB_EXTRACT_API_KEY",
        },
        "approval": {
            "provider": "AUXILIARY_APPROVAL_PROVIDER",
            "model": "AUXILIARY_APPROVAL_MODEL",
            "base_url": "AUXILIARY_APPROVAL_BASE_URL",
            "api_key": "AUXILIARY_APPROVAL_API_KEY",
        },
    }
    
    for task_key, env_map in auxiliary_task_env.items():
        task_cfg = auxiliary_config.get(task_key, {})
        if not isinstance(task_cfg, dict):
            continue
        prov = str(task_cfg.get("provider", "")).strip()
        model = str(task_cfg.get("model", "")).strip()
        base_url = str(task_cfg.get("base_url", "")).strip()
        api_key = str(task_cfg.get("api_key", "")).strip()
        if prov and prov != "auto":
            os.environ[env_map["provider"]] = prov
        if model:
            os.environ[env_map["model"]] = model
        if base_url:
            os.environ[env_map["base_url"]] = base_url
        if api_key:
            os.environ[env_map["api_key"]] = api_key
    
    # Security settings
    security_config = defaults.get("security", {})
    if isinstance(security_config, dict):
        redact = security_config.get("redact_secrets")
        if redact is not None:
            os.environ["OPENCODON_REDACT_SECRETS"] = str(redact).lower()

    # Session-search index knobs (opencodon_state reads the env carriers).
    sessions_config = defaults.get("sessions", {})
    if isinstance(sessions_config, dict):
        if "cjk_fts" in sessions_config:
            os.environ["OPENCODON_CJK_FTS"] = str(sessions_config["cjk_fts"])
        if "search_slow_ms" in sessions_config:
            os.environ["OPENCODON_SEARCH_SLOW_MS"] = str(
                sessions_config["search_slow_ms"]
            )

    return defaults

# Load configuration at module startup
CLI_CONFIG = load_cli_config()


# Initialize centralized logging early — agent.log + errors.log in ~/.opencodon/logs/.
# This ensures CLI sessions produce a log trail even before AIAgent is instantiated.
try:
    from opencodon_logging import setup_logging
    setup_logging(mode="cli")
except Exception:
    pass  # Logging setup is best-effort — don't crash the CLI

# Validate config structure early — print warnings before user hits cryptic errors
try:
    from opencodon.config import print_config_warnings
    print_config_warnings()
except Exception:
    pass

# Initialize the skin engine from config
try:
    from opencodon.frontends.cli.skin_engine import init_skin_from_config
    init_skin_from_config(CLI_CONFIG)
except Exception:
    pass  # Skin engine is optional — default skin used if unavailable

# Initialize tool preview length from config
try:
    from opencodon.core.display import set_tool_preview_max_len
    _tpl = CLI_CONFIG.get("display", {}).get("tool_preview_length", 0)
    set_tool_preview_max_len(int(_tpl) if _tpl else 0)
except Exception:
    pass

# Initialize friendly tool labels from config (default on)
try:
    from opencodon.core.display import set_friendly_tool_labels
    _ftl = CLI_CONFIG.get("display", {}).get("friendly_tool_labels", True)
    set_friendly_tool_labels(bool(_ftl))
except Exception:
    pass

# Neuter AsyncHttpxClientWrapper.__del__ before any AsyncOpenAI clients are
# created.  The SDK's __del__ schedules aclose() on asyncio.get_running_loop()
# which, during CLI idle time, finds prompt_toolkit's event loop and tries to
# close TCP transports bound to dead worker loops — producing
# "Event loop is closed" / "Press ENTER to continue..." errors.
#
# We install a sys.meta_path finder that defers the actual import + patch
# until ``openai._base_client`` is first loaded by the rest of the codebase.
# Eagerly importing it here (the old approach) cost ~166ms / ~30MB on every
# cold CLI start because openai's type tree (responses/*, graders/*) is huge.
# The finder approach pays nothing until the SDK is genuinely needed and
# still guarantees the patch is applied before any AsyncOpenAI instance can
# be constructed (the import-then-instantiate ordering is enforced by
# Python's import system).
try:
    import sys as _httpx_neuter_sys
    import importlib.util as _httpx_neuter_imp_util

    class _AsyncHttpxDelNeuter:
        """Defer ``AsyncHttpxClientWrapper.__del__`` neutering until import.

        Saves ~166ms on cold CLI start where openai is never used (e.g.
        ``opencodon --help`` paths inside the chat command flow).  See
        ``agent.auxiliary_client.neuter_async_httpx_del`` for full rationale
        on why ``__del__`` must be a no-op.
        """

        _armed = True

        def find_spec(self, fullname, path=None, target=None):
            if not self._armed or fullname != "openai._base_client":
                return None
            # Disarm before delegating so the recursive find_spec call
            # below doesn't loop through us.
            self._armed = False
            try:
                _httpx_neuter_sys.meta_path.remove(self)
            except ValueError:
                pass
            spec = _httpx_neuter_imp_util.find_spec(fullname)
            if spec is None or spec.loader is None:
                return None
            _orig_exec = spec.loader.exec_module

            def _patched_exec(module):
                _orig_exec(module)
                try:
                    cls = getattr(module, "AsyncHttpxClientWrapper", None)
                    if cls is not None:
                        cls.__del__ = lambda self: None  # type: ignore[assignment]
                except Exception:
                    pass

            spec.loader.exec_module = _patched_exec  # type: ignore[method-assign]
            return spec

    _httpx_neuter_sys.meta_path.insert(0, _AsyncHttpxDelNeuter())
except Exception:
    pass

from rich import box as rich_box
from rich.console import Console
from rich.markup import escape as _escape
from rich.panel import Panel
from rich.text import Text as _RichText

# Import agent and tool systems lazily. Bare interactive startup only needs the
# prompt; the full agent/tool registry is initialized on first use.
def AIAgent(*args, **kwargs):
    from opencodon.core.run_agent import AIAgent as _AIAgent

    return _AIAgent(*args, **kwargs)


def get_tool_definitions(*args, **kwargs):
    from opencodon.frontends.cli.mcp_startup import wait_for_mcp_discovery
    from opencodon.tools.model_tools import get_tool_definitions as _get_tool_definitions

    wait_for_mcp_discovery()
    return _get_tool_definitions(*args, **kwargs)


def get_toolset_for_tool(*args, **kwargs):
    from opencodon.tools.model_tools import get_toolset_for_tool as _get_toolset_for_tool

    return _get_toolset_for_tool(*args, **kwargs)

# Extracted CLI modules (Phase 3)
from opencodon.frontends.cli.banner import build_welcome_banner
from opencodon.frontends.cli.commands import SlashCommandCompleter, SlashCommandAutoSuggest


def get_all_toolsets(*args, **kwargs):
    from toolsets import get_all_toolsets as _get_all_toolsets

    return _get_all_toolsets(*args, **kwargs)


def get_toolset_info(*args, **kwargs):
    from toolsets import get_toolset_info as _get_toolset_info

    return _get_toolset_info(*args, **kwargs)


def validate_toolset(*args, **kwargs):
    from toolsets import validate_toolset as _validate_toolset

    return _validate_toolset(*args, **kwargs)


def _sync_process_session_id(session_id: str) -> None:
    """Keep process-local session-id consumers aligned after CLI switches."""
    from opencodon.frontends.gateway.session_context import set_current_session_id

    set_current_session_id(session_id)

# Cron job system for scheduled tasks (execution is handled by the gateway)
def get_job(*args, **kwargs):
    from opencodon.cron import get_job as _get_job

    return _get_job(*args, **kwargs)

# Resource cleanup imports for safe shutdown (terminal VMs, browser sessions)
from opencodon.frontends.cli.callbacks import prompt_for_secret


def _cleanup_all_terminals(*args, **kwargs):
    from opencodon.tools.terminal_tool import cleanup_all_environments

    return cleanup_all_environments(*args, **kwargs)


def set_sudo_password_callback(*args, **kwargs):
    from opencodon.tools.terminal_tool import set_sudo_password_callback as _set_sudo_password_callback

    return _set_sudo_password_callback(*args, **kwargs)


def set_approval_callback(*args, **kwargs):
    from opencodon.tools.terminal_tool import set_approval_callback as _set_approval_callback

    return _set_approval_callback(*args, **kwargs)


def set_secret_capture_callback(*args, **kwargs):
    from opencodon.tools.skills_tool import set_secret_capture_callback as _set_secret_capture_callback

    return _set_secret_capture_callback(*args, **kwargs)


def _cleanup_all_browsers(*args, **kwargs):
    from opencodon.tools.browser_tool import _emergency_cleanup_all_sessions

    return _emergency_cleanup_all_sessions(*args, **kwargs)

# Guard to prevent cleanup from running multiple times on exit
_cleanup_done = False
# One-shot CLI finalization runs before process cleanup so plugins can observe
# the session boundary while the agent is still attached. If a signal lands in
# that narrow window, atexit cleanup must not emit that session finalize again.
_single_query_finalize_attempted_session_ids: set[str | None] = set()
# Weak reference to the active AIAgent for memory provider shutdown at exit
_active_agent_ref = None
_deferred_agent_startup_done = False
# Set True once the TUI's prompt_toolkit app starts (which enables focus
# reporting + mouse tracking). Gates the on-exit terminal reset so non-TUI
# one-shot CLI runs — which also register _run_cleanup via atexit — don't emit
# escape codes for modes they never enabled (#36823).
_tui_input_modes_active = False


def _mark_tui_input_modes_active() -> None:
    """Record that the TUI app started, so _run_cleanup resets input modes."""
    global _tui_input_modes_active
    _tui_input_modes_active = True


def _prepare_deferred_agent_startup() -> None:
    """Run Termux-deferred agent discovery before the first real agent turn."""
    global _deferred_agent_startup_done
    if _deferred_agent_startup_done:
        return
    if os.environ.get("OPENCODON_DEFER_AGENT_STARTUP") != "1":
        return
    _deferred_agent_startup_done = True
    _accept_hooks = os.environ.get("OPENCODON_ACCEPT_HOOKS", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    try:
        from opencodon.plugins_runtime import discover_plugins

        discover_plugins()
    except Exception:
        logger.warning(
            "plugin discovery failed at deferred CLI startup",
            exc_info=True,
        )
    try:
        from opencodon.frontends.cli.mcp_startup import start_background_mcp_discovery

        start_background_mcp_discovery(
            logger=logger,
            thread_name="termux-cli-mcp-discovery",
        )
    except Exception:
        logger.debug(
            "MCP tool discovery failed at deferred CLI startup",
            exc_info=True,
        )
    try:
        from opencodon.core.shell_hooks import register_from_config
        from opencodon.config import load_config

        register_from_config(load_config(), accept_hooks=_accept_hooks)
    except Exception:
        logger.debug(
            "shell-hook registration failed at deferred CLI startup",
            exc_info=True,
        )

def _arm_exit_watchdog(timeout_s: float | None = None) -> None:
    """Guarantee the process actually exits once shutdown has begun.

    Two hang classes have kept "dead" CLI processes alive for minutes:

      1. A cleanup step wedged on network I/O (memory provider
         ``on_session_end``, MCP teardown, remote terminal cleanup).
      2. Interpreter teardown blocked joining non-daemon threads —
         stdlib ``ThreadPoolExecutor`` workers are joined unconditionally
         by ``concurrent.futures``' atexit hook even after
         ``shutdown(wait=False)``, so one tool thread wedged on a socket
         held the process open forever (#27563 class).

    The shared daemon pool (``tools.daemon_pool``) removes the main cause
    of (2); this watchdog is the backstop for both. It arms a daemon
    timer when ``_run_cleanup`` starts; if the process is still alive
    after ``timeout_s`` it flushes logging/stdio and calls ``os._exit(0)``.
    Daemon threads keep running through ``Py_FinalizeEx``'s thread joins,
    so the timer fires even when the main thread is stuck in teardown.

    Tune with ``OPENCODON_EXIT_WATCHDOG_S`` (seconds); ``0`` disables.
    """
    if timeout_s is None:
        try:
            timeout_s = float(os.getenv("OPENCODON_EXIT_WATCHDOG_S", "30"))
        except (TypeError, ValueError):
            timeout_s = 30.0
    if timeout_s <= 0:
        return
    # Never arm under pytest: tests invoke _run_cleanup() directly and a
    # 30s-delayed os._exit(0) would silently kill the test worker.
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return

    def _watchdog():
        time.sleep(timeout_s)
        # Still alive — cleanup or interpreter teardown is wedged.
        try:
            logger.warning(
                "Exit watchdog fired after %.0fs — forcing process exit "
                "(a cleanup step or non-daemon thread is wedged).",
                timeout_s,
            )
        except Exception:
            pass
        try:
            import logging as _lg
            _lg.shutdown()
        except Exception:
            pass
        for _stream in (sys.stdout, sys.stderr):
            try:
                _stream.flush()
            except Exception:
                pass
        os._exit(0)

    try:
        threading.Thread(
            target=_watchdog, daemon=True, name="exit-watchdog"
        ).start()
    except Exception:
        pass  # best-effort — never block shutdown on watchdog setup


_signal_watchdog_armed = False


def _arm_exit_watchdog_on_shutdown_signal() -> None:
    """Arm the exit backstop the moment a termination signal arrives.

    SIGTERM/SIGHUP establish unambiguous shutdown intent, but the graceful
    path from signal → ``agent.interrupt()`` → ``app.exit()`` /
    ``KeyboardInterrupt`` → ``finally`` → ``_run_cleanup`` has several wedge
    points BEFORE ``_run_cleanup`` arms the normal watchdog: a main thread
    parked in a syscall that never observes the unwind, a prompt_toolkit
    teardown that never returns, or an agent worker blocking the ``finally``.
    When that happens the process has NO backstop and a "dead" CLI lingers
    (observed: ``opencodon --tui`` alive ~47 min at 4% CPU after terminal close —
    the #65998 class).

    Arming at signal time closes that window. The leash is 2× the normal
    cleanup timeout so a slow-but-progressing ``_run_cleanup`` (which arms
    its own tighter timer when it starts) is never cut short by this outer
    backstop — this timer only wins when cleanup was never reached at all.

    Deliberately NOT armed at chat startup: the watchdog thread calls
    ``os._exit(0)`` unconditionally after its sleep, so arming without
    shutdown intent would hard-kill every session that outlives the timeout.

    Idempotent (module flag) so repeated signals don't stack timer threads.
    Never raises — safe to call from a signal handler.
    """
    global _signal_watchdog_armed
    if _signal_watchdog_armed:
        return
    _signal_watchdog_armed = True
    try:
        base = float(os.getenv("OPENCODON_EXIT_WATCHDOG_S", "30"))
    except (TypeError, ValueError):
        base = 30.0
    if base <= 0:
        return  # explicitly disabled
    try:
        _arm_exit_watchdog(timeout_s=base * 2)
    except Exception:
        pass  # never let the backstop break signal handling


def _run_cleanup(*, notify_session_finalize: bool = True):
    """Run resource cleanup exactly once."""
    global _cleanup_done
    if _cleanup_done:
        return
    _cleanup_done = True

    # Bound total shutdown time: if cleanup (or the interpreter's
    # thread-join teardown after it) wedges, force-exit instead of
    # leaving a zombie CLI holding the terminal for minutes.
    _arm_exit_watchdog()

    # Reset terminal input modes first, before the slower resource teardown
    # below (MCP / browser / memory shutdown can take seconds). On Ctrl+C the
    # user's terminal becomes usable immediately, and a later step raising
    # can't skip the reset (#36823). No-op unless the TUI actually ran.
    _reset_terminal_input_modes_on_exit()

    try:
        _cleanup_all_terminals()
    except Exception:
        pass
    try:
        from opencodon.tools.async_delegation import interrupt_all as _interrupt_async_delegations
        _interrupt_async_delegations(reason="CLI shutdown")
    except Exception:
        pass
    try:
        _cleanup_all_browsers()
    except Exception:
        pass
    try:
        from opencodon.tools.mcp_tool import shutdown_mcp_servers
        shutdown_mcp_servers()
    except BaseException:
        pass
    # Close cached auxiliary LLM clients (sync + async) so that
    # AsyncHttpxClientWrapper.__del__ doesn't fire on a closed event loop
    # and trigger prompt_toolkit's "Press ENTER to continue..." handler.
    try:
        from opencodon.core.auxiliary_client import shutdown_cached_clients
        shutdown_cached_clients()
    except Exception:
        pass
    # Shut down memory provider (on_session_end + shutdown_all) at actual
    # session boundary — NOT per-turn inside run_conversation().
    if notify_session_finalize:
        cleanup_session_id = _active_agent_ref.session_id if _active_agent_ref else None
        if _should_emit_cleanup_session_finalize(cleanup_session_id):
            _notify_session_finalize(
                session_id=cleanup_session_id,
                platform="cli",
                reason="shutdown",
            )
    try:
        if _active_agent_ref and hasattr(_active_agent_ref, 'shutdown_memory_provider'):
            # A /new shortly before exit leaves its end→switch boundary task
            # (old-session extraction, LLM-bound) queued on the memory
            # manager's serialized worker. shutdown_all()'s drain only waits
            # ~5s and cancels queued tasks, so give pending work a bounded
            # head start via the manager's own barrier — otherwise a
            # "/new then quit" silently drops the old session's extraction.
            # The 30s exit watchdog remains the hard backstop.
            _mm = getattr(_active_agent_ref, '_memory_manager', None)
            if _mm is not None and hasattr(_mm, 'flush_pending'):
                try:
                    _mm.flush_pending(timeout=10)
                except Exception:
                    pass
            # Forward the agent's own transcript so memory providers'
            # ``on_session_end`` hooks see the real conversation instead of
            # an empty list (#15165). ``_session_messages`` is set on
            # ``AIAgent.__init__`` and refreshed every turn via
            # ``_persist_session``. Fall back to no-arg on test stubs /
            # partially-initialised agents where the attribute is missing.
            _session_msgs = getattr(_active_agent_ref, '_session_messages', None)
            if isinstance(_session_msgs, list):
                logger.info(
                    "CLI cleanup calling memory shutdown for session %s with %d message(s)",
                    getattr(_active_agent_ref, "session_id", None) or "<unknown>",
                    len(_session_msgs),
                )
                _active_agent_ref.shutdown_memory_provider(_session_msgs)
            else:
                logger.info(
                    "CLI cleanup calling memory shutdown for session %s without session message list",
                    getattr(_active_agent_ref, "session_id", None) or "<unknown>",
                )
                _active_agent_ref.shutdown_memory_provider()
    except Exception as e:
        logger.warning("CLI cleanup memory shutdown failed: %s", e, exc_info=True)


def _should_emit_cleanup_session_finalize(session_id: str | None) -> bool:
    if not _single_query_finalize_attempted_session_ids:
        return True
    if session_id is None:
        return False
    return session_id not in _single_query_finalize_attempted_session_ids


def _notify_session_finalize(
    *,
    session_id: str | None,
    platform: str = "cli",
    reason: str = "shutdown",
) -> None:
    try:
        from opencodon.plugins_runtime import invoke_hook as _invoke_hook
        _invoke_hook(
            "on_session_finalize",
            session_id=session_id,
            platform=platform,
            reason=reason,
        )
    except Exception:
        pass


def _emit_interrupted_session_end(cli, *, reason: str = "keyboard_interrupt") -> None:
    """Best-effort on_session_end hook for interrupted non-interactive runs."""
    agent = getattr(cli, "agent", None)
    if agent is None:
        return

    try:
        agent.interrupt(reason.replace("_", " "))
    except Exception:
        pass

    session_id = getattr(agent, "session_id", None) or getattr(cli, "session_id", None)
    if session_id:
        try:
            cli.session_id = session_id
        except Exception:
            pass

    try:
        from opencodon.plugins_runtime import invoke_hook as _invoke_hook
        _invoke_hook(
            "on_session_end",
            session_id=session_id,
            task_id=getattr(agent, "_current_task_id", "") or "",
            turn_id=getattr(agent, "_current_turn_id", "") or "",
            api_request_id=getattr(agent, "_current_api_request_id", "") or "",
            completed=False,
            interrupted=True,
            model=getattr(agent, "model", None),
            platform=getattr(agent, "platform", None) or "cli",
            reason=reason,
        )
    except Exception:
        pass


def _notify_single_query_session_finalize(cli, *, reason: str = "shutdown") -> None:
    agent = getattr(cli, "agent", None)
    session_id = getattr(agent, "session_id", None) or getattr(cli, "session_id", None)
    if session_id in _single_query_finalize_attempted_session_ids:
        return

    try:
        _notify_session_finalize(
            session_id=session_id,
            platform=getattr(agent, "platform", None) or "cli",
            reason=reason,
        )
    finally:
        _single_query_finalize_attempted_session_ids.add(session_id)


def _finalize_single_query(cli) -> None:
    """Close one-shot CLI resources before releasing the active session lease."""
    try:
        _notify_single_query_session_finalize(cli)
        _run_cleanup(notify_session_finalize=False)
    finally:
        cli._release_active_session()


def _reset_terminal_input_modes_on_exit() -> None:
    """Best-effort: disable focus reporting + mouse tracking on TUI exit so they
    don't leak into the next shell session sharing the tab.

    prompt_toolkit restores these on a clean teardown, but Ctrl+C, SIGTERM /
    SIGHUP and crashes can bypass its unwind, leaving the modes enabled. The
    terminal then emits raw ``ESC[I`` / ``ESC[O`` focus events and fragmented
    SGR mouse reports as visible text in whatever runs next in the same tab
    (#36823). Called from ``_run_cleanup`` (atexit-registered + invoked on the
    normal / EOF / interrupt exit paths) this covers normal quit, Ctrl+C and
    SIGTERM/SIGHUP. ``kill -9`` is uncatchable and never runs this — but it
    is non-TTY / non-TUI, so there is nothing to reset there.

    Gated on ``_tui_input_modes_active`` so one-shot non-TUI CLI runs (which
    share ``_run_cleanup`` via ``atexit``) never emit these codes. Writes to the
    controlling terminal directly: by exit, prompt_toolkit's own output is torn
    down, so ``sys.stdout`` is the real fd; falls back to ``/dev/tty`` when
    stdout is redirected away from the terminal.
    """
    global _tui_input_modes_active
    if not _tui_input_modes_active:
        return
    # About to disable the modes — clear the flag so a re-armed _run_cleanup (or
    # a long-lived process that reuses it) doesn't re-emit them.
    _tui_input_modes_active = False
    # Prefer stdout when it's the terminal; otherwise the TUI may have driven
    # /dev/tty while stdout was redirected — reset there instead of nowhere.
    try:
        stream = sys.stdout
        if stream is not None and stream.isatty():
            stream.write(_TERMINAL_INPUT_MODE_RESET_SEQ)
            stream.flush()
            return
    except Exception:
        pass
    try:
        with open("/dev/tty", "w", encoding="ascii") as tty:
            tty.write(_TERMINAL_INPUT_MODE_RESET_SEQ)
            tty.flush()
    except Exception:
        pass


# =============================================================================
# Git Worktree Isolation (#652)
# =============================================================================

# Tracks the active worktree for cleanup on exit
_active_worktree: Optional[Dict[str, str]] = None


def _normalize_git_bash_path(p: Optional[str]) -> Optional[str]:
    """Translate a Git Bash-style path (``/c/Users/...``) to the native
    Windows form (``C:\\Users\\...``) that Python's ``subprocess.Popen``
    and ``pathlib.Path`` accept.

    No-op on non-Windows and for paths that already look native.  Git on
    native Windows normally emits forward-slash Windows paths
    (``C:/Users/...``) which both bash and Python handle, but certain
    configurations (Git Bash shells, MSYS2, WSL-mounted repos) surface
    ``/c/...`` or ``/cygdrive/c/...`` variants.
    """
    if not p:
        return p
    if sys.platform != "win32":
        return p
    import re as _re
    # /c/Users/... or /C/Users/...
    m = _re.match(r"^/([a-zA-Z])/(.*)$", p)
    if m:
        drive, rest = m.group(1), m.group(2)
        return f"{drive.upper()}:\\{rest.replace('/', chr(92))}"
    # /cygdrive/c/... or /mnt/c/...
    m = _re.match(r"^/(?:cygdrive|mnt)/([a-zA-Z])/(.*)$", p)
    if m:
        drive, rest = m.group(1), m.group(2)
        return f"{drive.upper()}:\\{rest.replace('/', chr(92))}"
    return p


def _git_repo_root() -> Optional[str]:
    """Return the git repo root for CWD, or None if not in a repo.

    Runs through :func:`_normalize_git_bash_path` so callers can pass
    the result directly to ``Path``/``subprocess.Popen(cwd=...)`` on
    Windows without hitting ``C:\\c\\Users\\...`` style resolution
    mistakes.
    """
    import subprocess
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return _normalize_git_bash_path(result.stdout.strip())
    except Exception:
        pass
    return None


def _path_is_within_root(path: Path, root: Path) -> bool:
    """Return True when a resolved path stays within the expected root."""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _resolve_worktree_base(repo_root: str) -> tuple:
    """Resolve the freshest base ref to branch a new worktree from.

    The standalone clone's ``HEAD`` can lag the remote by hundreds of commits
    (the ``~/.opencodon/opencodon`` clone is updated only by ``opencodon update``,
    not on every session). Branching a worktree from that stale ``HEAD`` roots
    every new branch on an old base — so the PR diff GitHub computes against
    current ``main`` balloons with unrelated changes, and the agent has to
    discover the staleness via the pre-push gate and rebase. Branching from the
    freshly-fetched remote tip instead means the worktree starts current.

    Strategy (each step falls back to the next on failure):
      1. If the current branch tracks an upstream, fetch and use that upstream
         ref — so a deliberate feature-branch worktree tracks its own remote,
         not the default branch.
      2. Else fetch the remote's default branch (``origin/HEAD`` → e.g.
         ``origin/main``) and use it.
      3. Else fall back to ``HEAD`` (offline, no remote, or detached) — the
         old behavior, never worse than before.

    Returns ``(base_ref, label)`` where *base_ref* is a git revision suitable
    for ``git worktree add ... <base_ref>`` and *label* is a short
    human-readable description for the session banner.
    """
    import subprocess

    def _git(args, timeout=20):
        return subprocess.run(
            ["git", *args],
            capture_output=True, text=True, timeout=timeout, cwd=repo_root,
        )

    # 1. Current branch's upstream, if it tracks one.
    try:
        up = _git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"])
        if up.returncode == 0:
            upstream = up.stdout.strip()  # e.g. "origin/main"
            if upstream and "/" in upstream:
                remote = upstream.split("/", 1)[0]
                # Fetch just that branch; fail-soft if offline.
                _git(["fetch", remote, upstream.split("/", 1)[1]], timeout=30)
                return upstream, f"{upstream} (fetched)"
    except Exception as e:
        logger.debug("worktree base: upstream resolution failed: %s", e)

    # 2. Remote default branch (origin/HEAD).
    try:
        # Resolve the remote's default branch symref.
        head_ref = _git(["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"])
        default_ref = ""
        if head_ref.returncode == 0:
            default_ref = head_ref.stdout.strip().replace("refs/remotes/", "", 1)
        if not default_ref:
            # origin/HEAD not set locally; ask the remote.
            show = _git(["remote", "show", "origin"], timeout=30)
            for line in show.stdout.splitlines():
                line = line.strip()
                if line.startswith("HEAD branch:"):
                    _branch = line.split(":", 1)[1].strip()
                    # A remote with no default branch reports "(unknown)";
                    # don't construct a bogus "origin/(unknown)" ref from it.
                    if _branch and _branch != "(unknown)":
                        default_ref = "origin/" + _branch
                    break
        if default_ref and "/" in default_ref:
            remote, branch = default_ref.split("/", 1)
            _git(["fetch", remote, branch], timeout=30)
            return default_ref, f"{default_ref} (fetched)"
    except Exception as e:
        logger.debug("worktree base: default-branch resolution failed: %s", e)

    # 3. Fall back to local HEAD (offline / no remote / detached).
    return "HEAD", "HEAD (local — could not reach remote)"


def _setup_worktree(repo_root: str = None, sync_base: bool = True) -> Optional[Dict[str, str]]:
    """Create an isolated git worktree for this CLI session.

    Returns a dict with worktree metadata on success, None on failure.
    The dict contains: path, branch, repo_root.

    When *sync_base* is True (default), the worktree branches from the
    freshly-fetched remote tip rather than the (possibly stale) local ``HEAD``
    — see ``_resolve_worktree_base``. Set ``worktree_sync: false`` in config to
    branch from local ``HEAD`` (the pre-#10760-followup behavior).
    """
    import subprocess

    repo_root = repo_root or _git_repo_root()
    if not repo_root:
        print("\033[31m✗ --worktree requires being inside a git repository.\033[0m")
        print("  cd into your project repo first, then run opencodon -w")
        return None

    short_id = uuid.uuid4().hex[:8]
    wt_name = f"opencodon-{short_id}"
    branch_name = f"opencodon/{wt_name}"

    worktrees_dir = Path(repo_root) / ".worktrees"
    worktrees_dir.mkdir(parents=True, exist_ok=True)

    wt_path = worktrees_dir / wt_name

    # Ensure .worktrees/ is in .gitignore
    gitignore = Path(repo_root) / ".gitignore"
    _ignore_entry = ".worktrees/"
    try:
        existing = gitignore.read_text() if gitignore.exists() else ""
        if _ignore_entry not in existing.splitlines():
            with open(gitignore, "a", encoding="utf-8") as f:
                if existing and not existing.endswith("\n"):
                    f.write("\n")
                f.write(f"{_ignore_entry}\n")
    except Exception as e:
        logger.debug("Could not update .gitignore: %s", e)

    # Resolve the base ref. By default branch from the freshly-fetched remote
    # tip so the worktree starts current with the project, not from the
    # (possibly stale) local HEAD of the standalone clone (#10760 follow-up).
    if sync_base:
        base_ref, base_label = _resolve_worktree_base(repo_root)
    else:
        base_ref, base_label = "HEAD", "HEAD (local — worktree_sync disabled)"

    # Create the worktree
    try:
        result = subprocess.run(
            ["git", "worktree", "add", str(wt_path), "-b", branch_name, base_ref],
            capture_output=True, text=True, timeout=30, cwd=repo_root,
        )
        if result.returncode != 0:
            # If branching from the resolved remote ref failed for any reason
            # (e.g. a partial fetch left the ref unusable), retry from local
            # HEAD so worktree creation never hard-fails on a sync hiccup.
            if base_ref != "HEAD":
                logger.warning(
                    "worktree add from %s failed (%s); retrying from local HEAD",
                    base_ref, result.stderr.strip(),
                )
                base_ref, base_label = "HEAD", "HEAD (fallback — remote base failed)"
                result = subprocess.run(
                    ["git", "worktree", "add", str(wt_path), "-b", branch_name, base_ref],
                    capture_output=True, text=True, timeout=30, cwd=repo_root,
                )
            if result.returncode != 0:
                print(f"\033[31m✗ Failed to create worktree: {result.stderr.strip()}\033[0m")
                return None
    except Exception as e:
        print(f"\033[31m✗ Failed to create worktree: {e}\033[0m")
        return None

    # Copy files listed in .worktreeinclude (gitignored files the agent needs)
    include_file = Path(repo_root) / ".worktreeinclude"
    if include_file.exists():
        try:
            repo_root_resolved = Path(repo_root).resolve()
            wt_path_resolved = wt_path.resolve()
            for line in include_file.read_text().splitlines():
                entry = line.strip()
                if not entry or entry.startswith("#"):
                    continue
                src = Path(repo_root) / entry
                dst = wt_path / entry
                # Prevent path traversal and symlink escapes: both the resolved
                # source and the resolved destination must stay inside their
                # expected roots before any file or symlink operation happens.
                try:
                    src_resolved = src.resolve(strict=False)
                    dst_resolved = dst.resolve(strict=False)
                except (OSError, ValueError):
                    logger.debug("Skipping invalid .worktreeinclude entry: %s", entry)
                    continue
                if not _path_is_within_root(src_resolved, repo_root_resolved):
                    logger.warning("Skipping .worktreeinclude entry outside repo root: %s", entry)
                    continue
                if not _path_is_within_root(dst_resolved, wt_path_resolved):
                    logger.warning("Skipping .worktreeinclude entry that escapes worktree: %s", entry)
                    continue
                if src.is_file():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(src), str(dst))
                elif src.is_dir():
                    # Symlink directories (faster, saves disk).  On Windows,
                    # symlink creation requires Developer Mode or elevation,
                    # and fails with OSError otherwise — fall back to a
                    # recursive copy so the worktree is still usable.  The
                    # copy is slower and uses disk, but it doesn't require
                    # admin and matches the Linux/macOS symlink outcome
                    # functionally.
                    if not dst.exists():
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        try:
                            os.symlink(str(src_resolved), str(dst))
                        except (OSError, NotImplementedError) as _sym_err:
                            if sys.platform == "win32":
                                logger.info(
                                    ".worktreeinclude: symlink failed (%s) — "
                                    "falling back to copytree on Windows.",
                                    _sym_err,
                                )
                                try:
                                    shutil.copytree(
                                        str(src_resolved),
                                        str(dst),
                                        symlinks=True,
                                        dirs_exist_ok=False,
                                    )
                                except Exception as _copy_err:
                                    logger.warning(
                                        ".worktreeinclude: copy fallback "
                                        "also failed for %s -> %s: %s",
                                        src, dst, _copy_err,
                                    )
                            else:
                                raise
        except Exception as e:
            logger.debug("Error copying .worktreeinclude entries: %s", e)

    # Lock the worktree so other processes (and `git worktree remove`) can see
    # it is actively in use.  Fail-soft: a lock failure never blocks the session.
    try:
        subprocess.run(
            ["git", "worktree", "lock", "--reason", f"opencodon pid={os.getpid()}", str(wt_path)],
            capture_output=True, text=True, timeout=10, cwd=repo_root,
        )
        logger.debug("Worktree locked: %s (pid=%s)", wt_path, os.getpid())
    except Exception as e:
        logger.debug("git worktree lock failed (non-fatal): %s", e)

    info = {
        "path": str(wt_path),
        "branch": branch_name,
        "repo_root": repo_root,
        "base": base_ref,
    }

    print(f"\033[32m✓ Worktree created:\033[0m {wt_path}")
    print(f"  Branch: {branch_name}")
    print(f"  Base:   {base_label}")

    return info


def _worktree_has_unpushed_commits(worktree_path: str, timeout: int = 10) -> bool:
    """Return whether a worktree has commits not reachable from any remote branch.

    ``git log HEAD --not --remotes`` compares against remote-tracking refs under
    ``refs/remotes/*``. If a repo has no remote-tracking refs yet, there is no
    usable remote baseline to compare against, so treat it as having no
    "unpushed" commits.
    """
    import subprocess

    try:
        remote_refs = subprocess.run(
            ["git", "for-each-ref", "--format=%(refname)", "refs/remotes"],
            capture_output=True, text=True, timeout=timeout, cwd=worktree_path,
        )
        if remote_refs.returncode != 0:
            return True
        if not remote_refs.stdout.strip():
            return False

        result = subprocess.run(
            ["git", "log", "--oneline", "HEAD", "--not", "--remotes"],
            capture_output=True, text=True, timeout=timeout, cwd=worktree_path,
        )
        if result.returncode != 0:
            return True
        return bool(result.stdout.strip())
    except Exception:
        return True


def _worktree_is_dirty(worktree_path: str, timeout: int = 10) -> bool:
    """Return whether a worktree has uncommitted changes (staged, unstaged, or
    untracked).

    Fails SAFE: on any error returns True so callers do not delete a worktree
    whose state they cannot determine.
    """
    import subprocess

    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, timeout=timeout, cwd=worktree_path,
        )
        if result.returncode != 0:
            return True
        return bool(result.stdout.strip())
    except Exception:
        return True


def _worktree_lock_is_live(repo_root: str, worktree_path: str, timeout: int = 10):
    """Classify a worktree's git lock as live, dead, or absent.

    ``opencodon -w`` locks each worktree with reason ``opencodon pid=<pid>`` so a
    concurrent opencodon process' startup prune leaves an in-use worktree alone.
    But a *crashed* session leaves the lock behind forever, and
    ``git worktree remove --force`` (single ``-f``) refuses to remove a locked
    worktree — so dead-locked worktrees accumulate indefinitely. This lets the
    pruner tell the two apart:

    - ``"live"``  — locked and the owning pid is still running (skip it).
    - ``"dead"``  — locked but the owning pid is gone, or the reason isn't a
                    parseable opencodon lock (safe to unlock + reap).
    - ``None``    — not locked at all.

    Fails SAFE toward ``"live"``: if git can't be queried at all we cannot
    prove the worktree is safe to touch, so we report it as live.
    """
    import re
    import subprocess

    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            capture_output=True, text=True, timeout=timeout, cwd=repo_root,
        )
        if result.returncode != 0:
            return "live"
    except Exception:
        return "live"

    target = Path(worktree_path).resolve()
    current: Optional[Path] = None
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            try:
                current = Path(line[len("worktree "):].strip()).resolve()
            except Exception:
                current = None
        elif line == "locked" or line.startswith("locked "):
            if current != target:
                continue
            reason = line[len("locked"):].strip()
            m = re.search(r"opencodon pid=(\d+)", reason)
            if not m:
                # Locked by something we don't recognize as a opencodon session
                # (or lock reason unavailable). Treat as dead — a foreign lock
                # on a opencodon -w worktree is almost certainly a leftover, and
                # the age/dirty/unpushed gates already ran before we got here.
                return "dead"
            pid = int(m.group(1))
            if pid == os.getpid():
                return "live"
            try:
                from opencodon.frontends.gateway.status import _pid_exists
                return "live" if _pid_exists(pid) else "dead"
            except Exception:
                # Can't determine liveness — fail safe toward keeping it.
                return "live"
    return None


def _cleanup_worktree(info: Dict[str, str] = None) -> None:
    """Remove a worktree and its branch on exit.

    Preserves the worktree only if it has unpushed commits (real work
    that hasn't been pushed to any remote).  Uncommitted changes alone
    (untracked files, test artifacts) are not enough to keep it — agent
    work lives in commits/PRs, not the working tree.
    """
    global _active_worktree
    info = info or _active_worktree
    if not info:
        return

    import subprocess

    wt_path = info["path"]
    branch = info["branch"]
    repo_root = info["repo_root"]

    if not Path(wt_path).exists():
        return

    has_unpushed = _worktree_has_unpushed_commits(wt_path, timeout=10)

    if has_unpushed:
        print(f"\n\033[33m⚠ Worktree has unpushed commits, keeping: {wt_path}\033[0m")
        print(f"  To clean up manually: git worktree remove --force {wt_path}")
        _active_worktree = None
        return

    # Remove worktree (even if working tree is dirty — uncommitted
    # changes without unpushed commits are just artifacts)
    # Unlock first so `git worktree remove` isn't blocked by the lock we
    # placed at creation time.  Fail-soft — never block cleanup.
    try:
        subprocess.run(
            ["git", "worktree", "unlock", wt_path],
            capture_output=True, text=True, timeout=10, cwd=repo_root,
        )
    except Exception as e:
        logger.debug("git worktree unlock failed (non-fatal): %s", e)

    try:
        subprocess.run(
            ["git", "worktree", "remove", wt_path, "--force"],
            capture_output=True, text=True, timeout=15, cwd=repo_root,
        )
    except Exception as e:
        logger.debug("Failed to remove worktree: %s", e)

    # Delete the branch
    try:
        subprocess.run(
            ["git", "branch", "-D", branch],
            capture_output=True, text=True, timeout=10, cwd=repo_root,
        )
    except Exception as e:
        logger.debug("Failed to delete branch %s: %s", branch, e)

    _active_worktree = None
    print(f"\033[32m✓ Worktree cleaned up: {wt_path}\033[0m")


def _run_state_db_auto_maintenance(session_db) -> None:
    """Call ``SessionDB.maybe_auto_prune_and_vacuum`` using current config.

    Reads the ``sessions:`` section from config.yaml via
    :func:`opencodon.config.load_config` (the authoritative loader that
    deep-merges DEFAULT_CONFIG, so unmigrated configs still get default
    values). Honours ``auto_prune`` / ``retention_days`` /
    ``vacuum_after_prune`` / ``min_interval_hours``, and delegates to the
    DB. Never raises — maintenance must never block interactive startup.
    """
    if session_db is None:
        return
    try:
        from opencodon.config import load_config as _load_full_config
        from opencodon_constants import get_opencodon_home as _get_opencodon_home
        _opencodon_home_maint = _get_opencodon_home()

        # One-time prune of empty TUI ghost sessions.
        try:
            if not session_db.get_meta("ghost_session_prune_v1"):
                pruned = session_db.prune_empty_ghost_sessions(
                    sessions_dir=_opencodon_home_maint / "sessions"
                )
                session_db.set_meta("ghost_session_prune_v1", "1")
                if pruned:
                    logger.info("Pruned %d empty TUI ghost sessions", pruned)
        except Exception as _prune_exc:
            logger.debug("Ghost session prune skipped: %s", _prune_exc)

        # One-time finalize of orphaned compression continuations (#20001).
        try:
            if not session_db.get_meta("orphaned_compression_finalize_v1"):
                finalized = session_db.finalize_orphaned_compression_sessions()
                session_db.set_meta("orphaned_compression_finalize_v1", "1")
                if finalized:
                    logger.info(
                        "Finalized %d orphaned compression sessions", finalized
                    )
        except Exception as _finalize_exc:
            logger.debug("Orphan compression finalize skipped: %s", _finalize_exc)

        cfg = (_load_full_config().get("sessions") or {})
        if not cfg.get("auto_prune", False):
            return
        session_db.maybe_auto_prune_and_vacuum(
            retention_days=int(cfg.get("retention_days", 90)),
            min_interval_hours=int(cfg.get("min_interval_hours", 24)),
            vacuum=bool(cfg.get("vacuum_after_prune", True)),
            sessions_dir=_opencodon_home_maint / "sessions",
        )
    except Exception as exc:
        logger.debug("state.db auto-maintenance skipped: %s", exc)


def _run_checkpoint_auto_maintenance() -> None:
    """Call ``checkpoint_manager.maybe_auto_prune_checkpoints`` using current config.

    Reads the ``checkpoints:`` section from config.yaml via
    :func:`opencodon.config.load_config`. Honours ``auto_prune`` /
    ``retention_days`` / ``delete_orphans`` / ``min_interval_hours``.
    Never raises — maintenance must never block interactive startup.
    """
    try:
        from opencodon.config import load_config as _load_full_config
        cfg = (_load_full_config().get("checkpoints") or {})
        if not cfg.get("auto_prune", False):
            return
        from opencodon.tools.checkpoint_manager import maybe_auto_prune_checkpoints
        maybe_auto_prune_checkpoints(
            retention_days=int(cfg.get("retention_days", 7)),
            min_interval_hours=int(cfg.get("min_interval_hours", 24)),
            delete_orphans=bool(cfg.get("delete_orphans", True)),
            max_total_size_mb=int(cfg.get("max_total_size_mb", 500)),
        )
    except Exception as exc:
        logger.debug("checkpoint auto-maintenance skipped: %s", exc)


def _prune_stale_worktrees(repo_root: str, max_age_hours: int = 24) -> None:
    """Remove stale worktrees and orphaned branches on startup.

    Age-based tiers (aggressive cleanup keeps ``.worktrees/`` from growing
    unbounded):
    - Under max_age_hours (24h): skip — session may still be active.
    - 24h–72h: remove if no unpushed commits.
    - Over 72h: force remove regardless (nothing should sit this long).

    Lock handling (orthogonal to age): ``opencodon -w`` locks each worktree with
    reason ``opencodon pid=<pid>`` so a concurrent opencodon process leaves an in-use
    worktree alone. A *live*-locked worktree is skipped at any age; a
    *dead*-locked one (owning pid gone — a crashed session) is unlocked first
    so ``git worktree remove --force`` can actually reap it, otherwise those
    leftovers accumulate forever (``remove --force`` refuses a locked tree).

    Branch deletion is gated on ``git worktree remove`` succeeding, so a failed
    removal never orphans the branch (which would drop easy reachability of any
    commits still in the worktree).

    Also prunes orphaned ``opencodon/*`` and ``pr-*`` local branches that
    have no corresponding worktree.
    """
    import subprocess
    import time

    worktrees_dir = Path(repo_root) / ".worktrees"
    if not worktrees_dir.exists():
        _prune_orphaned_branches(repo_root)
        return

    now = time.time()
    soft_cutoff = now - (max_age_hours * 3600)       # 24h default
    hard_cutoff = now - (max_age_hours * 3 * 3600)   # 72h default

    for entry in worktrees_dir.iterdir():
        if not entry.is_dir() or not entry.name.startswith("opencodon-"):
            continue

        # Check age
        try:
            mtime = entry.stat().st_mtime
            if mtime > soft_cutoff:
                continue  # Too recent — skip
        except Exception:
            continue

        force = mtime <= hard_cutoff  # Over 72h — reap aggressively

        # Never delete real work, regardless of age. Unpushed commits and
        # uncommitted changes may be a crashed session's in-flight work; the
        # >72h tier reaps only abandoned *clean, fully-pushed* worktrees (the
        # scratch trees that actually cause .worktrees/ bloat).
        if _worktree_has_unpushed_commits(str(entry), timeout=5):
            continue  # Has unpushed commits or can't check — skip
        if not force:
            # 24h–72h tier is conservative: unpushed check above is enough.
            pass
        elif _worktree_is_dirty(str(entry), timeout=5):
            continue  # >72h but dirty — preserve uncommitted work

        # Respect git-native session locks. A lock owned by a still-running
        # opencodon process means the worktree is actively in use — never touch
        # it. A lock whose owning pid is gone is a crashed session's leftover:
        # unlock it so `git worktree remove --force` (single -f) can reap it,
        # otherwise dead-locked worktrees pile up indefinitely.
        lock_state = _worktree_lock_is_live(repo_root, str(entry), timeout=5)
        if lock_state == "live":
            logger.debug("Skipping live-locked worktree: %s", entry.name)
            continue
        if lock_state == "dead":
            try:
                subprocess.run(
                    ["git", "worktree", "unlock", str(entry)],
                    capture_output=True, text=True, timeout=10, cwd=repo_root,
                )
            except Exception as e:
                logger.debug("Failed to unlock dead worktree %s: %s", entry.name, e)

        # Safe to remove
        try:
            branch_result = subprocess.run(
                ["git", "branch", "--show-current"],
                capture_output=True, text=True, timeout=5, cwd=str(entry),
            )
            branch = branch_result.stdout.strip()

            remove_result = subprocess.run(
                ["git", "worktree", "remove", str(entry), "--force"],
                capture_output=True, text=True, timeout=15, cwd=repo_root,
            )
            if remove_result.returncode != 0:
                # Removal failed — keep the branch so any commits stay
                # reachable rather than orphaning it.
                logger.debug(
                    "Failed to remove worktree %s: %s",
                    entry.name, remove_result.stderr.strip(),
                )
                continue
            if branch:
                subprocess.run(
                    ["git", "branch", "-D", branch],
                    capture_output=True, text=True, timeout=10, cwd=repo_root,
                )
            logger.debug("Pruned stale worktree: %s (force=%s)", entry.name, force)
        except Exception as e:
            logger.debug("Failed to prune worktree %s: %s", entry.name, e)

    _prune_orphaned_branches(repo_root)


def _prune_orphaned_branches(repo_root: str) -> None:
    """Delete local ``opencodon/opencodon-*`` and ``pr-*`` branches with no worktree.

    These are auto-generated by ``opencodon -w`` sessions and PR review
    workflows respectively.  Once their worktree is gone they serve no
    purpose and just accumulate.
    """
    import subprocess

    try:
        result = subprocess.run(
            ["git", "branch", "--format=%(refname:short)"],
            capture_output=True, text=True, timeout=10, cwd=repo_root,
        )
        if result.returncode != 0:
            return
        all_branches = [b.strip() for b in result.stdout.strip().split("\n") if b.strip()]
    except Exception:
        return

    # Collect branches that are actively checked out in a worktree
    active_branches: set = set()
    try:
        wt_result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            capture_output=True, text=True, timeout=10, cwd=repo_root,
        )
        for line in wt_result.stdout.split("\n"):
            if line.startswith("branch refs/heads/"):
                active_branches.add(line.split("branch refs/heads/", 1)[-1].strip())
    except Exception:
        return  # Can't determine active branches — bail

    # Also protect the currently checked-out branch and main
    try:
        head_result = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True, text=True, timeout=5, cwd=repo_root,
        )
        current = head_result.stdout.strip()
        if current:
            active_branches.add(current)
    except Exception:
        pass
    active_branches.add("main")

    orphaned = [
        b for b in all_branches
        if b not in active_branches
        and (b.startswith("opencodon/opencodon-") or b.startswith("pr-"))
    ]

    if not orphaned:
        return

    # Delete in batches
    for i in range(0, len(orphaned), 50):
        batch = orphaned[i:i + 50]
        try:
            subprocess.run(
                ["git", "branch", "-D"] + batch,
                capture_output=True, text=True, timeout=30, cwd=repo_root,
            )
        except Exception as e:
            logger.debug("Failed to prune orphaned branches: %s", e)

    logger.debug("Pruned %d orphaned branches", len(orphaned))

# ============================================================================
# ASCII Art & Branding
# ============================================================================

# Color palette (hex colors for Rich markup):
# - Gold: #FFD700 (headers, highlights)
# - Amber: #FFBF00 (secondary highlights)
# - Bronze: #CD7F32 (tertiary elements)
# - Light: #FFF8DC (text)
# - Dim: #B8860B (muted text)

# ANSI building blocks for conversation display
_ACCENT_ANSI_DEFAULT = "\033[1;38;2;255;215;0m"  # True-color #FFD700 bold — fallback
_BOLD = "\033[1m"
_RST = "\033[0m"
_STREAM_PAD = "    "  # 4-space indent for streamed response text (matches Panel padding)


def _hex_to_ansi(hex_color: str, *, bold: bool = False) -> str:
    """Convert a hex color like '#268bd2' to a true-color ANSI escape.

    Auto-remaps known dark-mode-tuned colors to readable light-mode
    equivalents when running on a light terminal (see
    _maybe_remap_for_light_mode + _LIGHT_MODE_REMAP).
    """
    hex_color = _maybe_remap_for_light_mode(hex_color)
    try:
        r = int(hex_color[1:3], 16)
        g = int(hex_color[3:5], 16)
        b = int(hex_color[5:7], 16)
        prefix = "1;" if bold else ""
        return f"\033[{prefix}38;2;{r};{g};{b}m"
    except (ValueError, IndexError):
        return _ACCENT_ANSI_DEFAULT if bold else "\033[38;2;184;134;11m"


# ────────────────────────────────────────────────────────────────────────
# Light/dark terminal mode detection.
#
# Mirrors apps/tui/src/theme.ts detectLightMode().  Used to decide whether
# to remap "near-white" skin colors (e.g. #FFF8DC banner_text, #B8860B
# banner_dim) to darker equivalents that are readable on a light
# Terminal.app / iTerm2 background.
#
# Detection priority:
#   1. OPENCODON_LIGHT / OPENCODON_TUI_LIGHT env (true/false) — explicit override
#   2. OPENCODON_TUI_THEME=light|dark — explicit theme
#   3. OPENCODON_TUI_BACKGROUND=#RRGGBB — explicit bg hint
#   4. COLORFGBG env (set by xterm/Konsole/urxvt) — bg slot 7/15 = light
#   5. OSC 11 query (\x1b]11;?\x1b\\) — ask the terminal directly
#   6. Default: assume dark (matches the legacy opencodon assumption)
#
# Cached after first call so we don't query the terminal repeatedly.
_LIGHT_MODE_CACHE: bool | None = None
_TRUE_RE = re.compile(r"^(1|true|on|yes|y)$")
_FALSE_RE = re.compile(r"^(0|false|off|no|n)$")
_LIGHT_DEFAULT_TERM_PROGRAMS = frozenset()  # Apple_Terminal doesn't reliably indicate; require explicit


def _luminance_from_hex(hex_str: str) -> float | None:
    s = (hex_str or "").strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) != 6 or not all(c in "0123456789abcdefABCDEF" for c in s):
        return None
    try:
        r, g, b = int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    except ValueError:
        return None
    # Rec.709 luma
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0


def _query_osc11_background() -> str | None:
    """Ask the terminal for its background color via OSC 11.

    Most modern terminals reply with \x1b]11;rgb:RRRR/GGGG/BBBB\x1b\\
    within a few ms.  We wait up to 100ms total before giving up.
    Returns "#RRGGBB" or None on timeout / non-tty.

    Skipped over SSH: the round-trip routinely exceeds our 100ms budget, so a
    late reply lands after prompt_toolkit has grabbed the tty — its payload
    leaks in as typed text and the BEL terminator reads as Ctrl+G (open
    editor), trapping the user in a stray editor. Remote sessions fall back to
    COLORFGBG / env hints / the dark default instead.
    """
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return None
    if any(os.environ.get(v) for v in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY")):
        return None
    try:
        import termios
        import tty
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
    except Exception:
        return None
    try:
        try:
            tty.setcbreak(fd)
        except Exception:
            return None
        try:
            sys.stdout.write("\x1b]11;?\x1b\\")
            sys.stdout.flush()
        except Exception:
            return None
        # Read up to ~50ms for the response
        import select
        deadline = time.monotonic() + 0.1
        buf = b""
        while time.monotonic() < deadline:
            r, _, _ = select.select([fd], [], [], deadline - time.monotonic())
            if not r:
                continue
            try:
                chunk = os.read(fd, 64)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            if b"\x1b\\" in buf or b"\x07" in buf:
                break
        # Parse: \x1b]11;rgb:RRRR/GGGG/BBBB\x1b\\
        m = re.search(rb"rgb:([0-9a-fA-F]+)/([0-9a-fA-F]+)/([0-9a-fA-F]+)", buf)
        if not m:
            return None
        # Each component is 1-4 hex digits — normalize to 8-bit
        def norm(h: bytes) -> int:
            v = int(h, 16)
            # Scale to 0-255 based on hex length
            bits = len(h) * 4
            return (v * 255) // ((1 << bits) - 1) if bits else 0
        r, g, b = norm(m.group(1)), norm(m.group(2)), norm(m.group(3))
        return f"#{r:02X}{g:02X}{b:02X}"
    finally:
        # TCSAFLUSH discards any unread input as it restores the original
        # attributes — scrubs a slow/partial OSC 11 reply out of the tty
        # buffer before prompt_toolkit can read it as keystrokes.
        try:
            termios.tcsetattr(fd, termios.TCSAFLUSH, old)
        except Exception:
            pass


def _detect_light_mode() -> bool:
    global _LIGHT_MODE_CACHE
    if _LIGHT_MODE_CACHE is not None:
        return _LIGHT_MODE_CACHE
    result = False
    try:
        # 1. Explicit env override
        for var in ("OPENCODON_LIGHT", "OPENCODON_TUI_LIGHT"):
            v = (os.environ.get(var) or "").strip().lower()
            if _TRUE_RE.match(v):
                result = True
                _LIGHT_MODE_CACHE = result
                return result
            if _FALSE_RE.match(v):
                _LIGHT_MODE_CACHE = result
                return result
        # 2. Theme hint
        theme = (os.environ.get("OPENCODON_TUI_THEME") or "").strip().lower()
        if theme == "light":
            result = True
            _LIGHT_MODE_CACHE = result
            return result
        if theme == "dark":
            _LIGHT_MODE_CACHE = result
            return result
        # 3. Explicit bg hex
        bg_hint = os.environ.get("OPENCODON_TUI_BACKGROUND") or ""
        bg_lum = _luminance_from_hex(bg_hint)
        if bg_lum is not None:
            result = bg_lum >= 0.5
            _LIGHT_MODE_CACHE = result
            return result
        # 4. COLORFGBG (xterm/Konsole/urxvt)
        cfgbg = (os.environ.get("COLORFGBG") or "").strip()
        if cfgbg:
            last = cfgbg.split(";")[-1] if ";" in cfgbg else cfgbg
            if last.isdigit():
                bg = int(last)
                if bg in {7, 15}:
                    result = True
                    _LIGHT_MODE_CACHE = result
                    return result
                if 0 <= bg < 16:
                    _LIGHT_MODE_CACHE = result
                    return result
        # 5. OSC 11 query (best-effort, only when stdin/stdout are TTY)
        bg_color = _query_osc11_background()
        if bg_color:
            lum = _luminance_from_hex(bg_color)
            if lum is not None:
                result = lum >= 0.5
                _LIGHT_MODE_CACHE = result
                return result
        # 6. TERM_PROGRAM allow-list (currently empty)
        tp = (os.environ.get("TERM_PROGRAM") or "").strip()
        if tp in _LIGHT_DEFAULT_TERM_PROGRAMS:
            result = True
    except Exception:
        result = False
    _LIGHT_MODE_CACHE = result
    return result


# Light-mode equivalents of skin colors that are unreadable on cream
# Terminal.app backgrounds.  Used by _SkinAwareAnsi to remap colors
# at resolution time when light mode is detected.
#
# IMPORTANT: only remap colors that are used as STANDALONE foregrounds
# on the terminal's background.  Don't remap colors that are paired
# with a dark bg (e.g. status bar text on bg:#1a1a2e) — those would
# become invisible the OTHER direction (dark gray on dark navy).
_LIGHT_MODE_REMAP: dict[str, str] = {
    # Original (dark-mode) -> Light-mode replacement (darker, readable)
    "#FFF8DC": "#1A1A1A",   # cornsilk -> near-black
    "#FFD700": "#9A6B00",   # gold -> dark goldenrod (readable on cream)
    "#FFBF00": "#8A5A00",   # amber -> dark amber
    "#B8860B": "#5C4500",   # dark goldenrod -> deeper brown (more contrast)
    "#DAA520": "#6B4F00",   # goldenrod -> dark olive
    "#F1E6CF": "#1A1A1A",   # cream -> near-black
    "#c9d1d9": "#24292F",   # github-light fg
    "#EAF7FF": "#0F1B26",   # ice
    "#F5F5F5": "#1A1A1A",
    "#FFF0D4": "#1A1A1A",
    "#CD7F32": "#8A4F1A",   # bronze -> darker bronze
    "#FFEFB5": "#3A2A00",
    # NOTE: skipping #C0C0C0/#888888/#555555/#8B8682 — those are
    # status-bar foregrounds paired with dark navy bg, where dark
    # remap values would become invisible.
}


def _maybe_remap_for_light_mode(hex_color: str) -> str:
    """If we're in light mode, remap a dark-mode-tuned color to a
    higher-contrast equivalent.  No-op in dark mode."""
    if not _detect_light_mode():
        return hex_color
    if not hex_color or not hex_color.startswith("#"):
        return hex_color
    # Case-insensitive lookup
    upper = hex_color.upper()
    if upper in _LIGHT_MODE_REMAP_UPPER:
        return _LIGHT_MODE_REMAP_UPPER[upper]
    return hex_color


# Pre-uppercased lookup table for case-insensitive remapping
_LIGHT_MODE_REMAP_UPPER = {k.upper(): v for k, v in _LIGHT_MODE_REMAP.items()}


def _install_skin_light_mode_hook() -> None:
    """Wrap SkinConfig.get_color at import time so EVERY skin color read goes
    through the light-mode remap.  Idempotent."""
    try:
        from opencodon.frontends.cli.skin_engine import SkinConfig  # type: ignore[import]
    except Exception:
        return
    if getattr(SkinConfig, "_opencodon_light_mode_hook_installed", False):
        return
    _orig_get_color = SkinConfig.get_color

    def _wrapped_get_color(self, key, fallback=""):
        value = _orig_get_color(self, key, fallback)
        try:
            return _maybe_remap_for_light_mode(value)
        except Exception:
            return value

    SkinConfig.get_color = _wrapped_get_color  # type: ignore[method-assign]
    SkinConfig._opencodon_light_mode_hook_installed = True  # type: ignore[attr-defined]


_install_skin_light_mode_hook()


# Prime the light-mode detection cache early (at module load) when
# we're running interactively so OSC 11 happens before pt grabs the
# tty.  Skip for non-tty contexts (subagents, gateway, tests).
try:
    if sys.stdin.isatty() and sys.stdout.isatty():
        _detect_light_mode()
except Exception:
    pass



class _SkinAwareAnsi:
    """Lazy ANSI escape that resolves from the skin engine on first use.

    Acts as a string in f-strings and concatenation.  Call ``.reset()`` to
    force re-resolution after a ``/skin`` switch.
    """

    def __init__(self, skin_key: str, fallback_hex: str = "#FFD700", *, bold: bool = False):
        self._skin_key = skin_key
        self._fallback_hex = fallback_hex
        self._bold = bold
        self._cached: str | None = None

    def __str__(self) -> str:
        if self._cached is None:
            try:
                from opencodon.frontends.cli.skin_engine import get_active_skin
                self._cached = _hex_to_ansi(
                    get_active_skin().get_color(self._skin_key, self._fallback_hex),
                    bold=self._bold,
                )
            except Exception:
                self._cached = _hex_to_ansi(self._fallback_hex, bold=self._bold)
        return self._cached

    def __add__(self, other: str) -> str:
        return str(self) + other

    def __radd__(self, other: str) -> str:
        return other + str(self)

    def reset(self) -> None:
        """Clear cache so the next access re-reads the skin."""
        self._cached = None


_ACCENT = _SkinAwareAnsi("response_border", "#FFD700", bold=True)
# Use ANSI dim+italic attributes (\x1b[2;3m) instead of a hardcoded
# hex color so dim/thinking text inherits the terminal's default
# foreground color and stays readable in both light and dark
# Terminal.app modes.  Hardcoded skin colors like #B8860B
# (dark goldenrod) become invisible against light cream backgrounds.
_DIM = "\x1b[2;3m"


def _b(s: str) -> str:
    """Bold if stdout is a real TTY; plain text otherwise (slash-worker safe)."""
    import sys as _sys
    try:
        return f"\x1b[1m{s}\x1b[0m" if _sys.stdout.isatty() else str(s)
    except Exception:
        return str(s)


def _d(s: str) -> str:
    """Dim-italic if stdout is a real TTY; plain text otherwise."""
    import sys as _sys
    try:
        return f"\x1b[2;3m{s}\x1b[0m" if _sys.stdout.isatty() else str(s)
    except Exception:
        return str(s)


def _accent_hex() -> str:
    """Return the active skin accent color for legacy CLI output lines."""
    try:
        from opencodon.frontends.cli.skin_engine import get_active_skin
        return get_active_skin().get_color("ui_accent", "#FFBF00")
    except Exception:
        return "#FFBF00"


def _rich_text_from_ansi(text: str) -> _RichText:
    """Safely render assistant/tool output that may contain ANSI escapes.

    Using Rich Text.from_ansi preserves literal bracketed text like
    ``[not markup]`` while still interpreting real ANSI color codes.
    """
    return _RichText.from_ansi(text or "")


def _strip_markdown_syntax(text: str) -> str:
    """Best-effort markdown marker removal for plain-text display."""
    plain = _rich_text_from_ansi(text or "").plain
    # Avoid stripping cron-style expressions like "* * * * *" as if they were
    # Markdown horizontal rules. CommonMark treats three or more "*" as an HR,
    # but in opencodon output it's common to display cron schedules verbatim.
    #
    # Keep the behavior for "-" / "_" HR markers, and only strip "*" HR lines
    # when there are exactly 3 asterisks (with optional whitespace).
    plain = re.sub(r"^\s{0,3}(?:[-_]\s*){3,}$", "", plain, flags=re.MULTILINE)
    plain = re.sub(r"^\s{0,3}(?:\*\s*){3}\s*$", "", plain, flags=re.MULTILINE)
    plain = re.sub(r"^\s{0,3}#{1,6}\s+", "", plain, flags=re.MULTILINE)
    # Preserve blockquotes, lists, and checkboxes because they carry structure.
    plain = re.sub(r"(```+|~~~+)", "", plain)
    plain = re.sub(r"`([^`]*)`", r"\1", plain)
    plain = re.sub(r"!\[([^\]]*)\]\([^\)]*\)", r"\1", plain)
    plain = re.sub(r"\[([^\]]+)\]\([^\)]*\)", r"\1", plain)
    plain = re.sub(r"\*\*\*([^*]+)\*\*\*", r"\1", plain)
    plain = re.sub(r"(?<!\w)___([^_]+)___(?!\w)", r"\1", plain)
    plain = re.sub(r"\*\*([^*]+)\*\*", r"\1", plain)
    plain = re.sub(r"(?<!\w)__([^_]+)__(?!\w)", r"\1", plain)
    # Only strip `*emphasis*` markers when the inner text is non-whitespace.
    # This avoids corrupting cron expressions like "* * * * *".
    plain = re.sub(r"\*([^\s*][^*]*?[^\s*])\*", r"\1", plain)
    plain = re.sub(r"(?<!\w)_([^_]+)_(?!\w)", r"\1", plain)
    plain = re.sub(r"~~([^~]+)~~", r"\1", plain)
    plain = re.sub(r"\n{3,}", "\n\n", plain)
    return plain.strip("\n")


_WINDOWS_PATH_WITH_DOT_SEGMENT_RE = re.compile(
    r"(?i)(?:\b[a-z]:\\|\\\\)[^\s`]*\\\.[^\s`]*"
)


def _preserve_windows_dot_segments_for_markdown(text: str) -> str:
    r"""Keep Windows path separators before hidden directories in Markdown.

    CommonMark treats ``\.`` as an escaped literal dot, so Rich Markdown would
    render ``D:\repo\.ai`` as ``D:\repo.ai``.  Doubling only that separator
    inside Windows path-looking tokens preserves the path without changing
    ordinary markdown escapes like ``1\. not a list``.
    """
    if "\\." not in text:
        return text

    def _protect(match: re.Match[str]) -> str:
        return re.sub(r"(?<!\\)\\(?=\.)", r"\\\\", match.group(0))

    return _WINDOWS_PATH_WITH_DOT_SEGMENT_RE.sub(_protect, text)


def _terminal_width_for_streaming() -> int:
    """Display cells available inside the streamed response box.

    The streaming path indents every line by ``_STREAM_PAD`` (4 cells)
    inside an open response panel.  The realigner uses this number as
    its budget when deciding whether to keep a horizontal table or
    fall back to vertical key-value rendering.  We subtract a small
    safety margin so terminal-resize races don't push a borderline
    table into mid-cell soft-wrap.
    """

    try:
        cols = shutil.get_terminal_size((80, 24)).columns
    except Exception:
        cols = 80
    return max(20, cols - len(_STREAM_PAD) - 2)


def _render_final_assistant_content(text: str, mode: str = "render"):
    """Render final assistant content as markdown, stripped text, or raw text."""
    from rich.markdown import Markdown

    # Estimate the cells available to the rendered table.  The Panel
    # used by the background-task / final-response path has 4 cells of
    # left+right padding plus 1 cell of border on each side, plus the
    # _STREAM_PAD indent that streamed content uses.  Subtract a small
    # safety margin so resize races don't push a borderline table into
    # soft-wrap.
    try:
        cols = shutil.get_terminal_size((80, 24)).columns
    except Exception:
        cols = 80
    panel_width = max(20, cols - 12)

    normalized_mode = str(mode or "render").strip().lower()
    if normalized_mode == "strip":
        # Strip first — inline markdown inside cells (`code`, **bold**, ~~strike~~)
        # changes cell display width — then re-align so the column padding
        # reflects the final visible text, not the marker-decorated source.
        return _RichText(
            realign_markdown_tables(_strip_markdown_syntax(text), panel_width)
        )
    if normalized_mode == "raw":
        return _rich_text_from_ansi(text or "")

    # `render` mode: Rich's Markdown renderer handles CJK width via wcwidth
    # internally, so a pre-pass through realign_markdown_tables would just
    # rewrite already-correct padding.  But on the way in we still want to
    # normalise model-emitted under-padded tables so that mid-render fallbacks
    # (narrow panels, etc.) at least see consistent input.
    plain = _rich_text_from_ansi(text or "").plain
    plain = _preserve_windows_dot_segments_for_markdown(plain)
    plain = realign_markdown_tables(plain, panel_width)
    return Markdown(plain)


_OUTPUT_HISTORY_ENABLED = True
_OUTPUT_HISTORY_REPLAYING = False
_OUTPUT_HISTORY_SUPPRESSED = False
_OUTPUT_HISTORY_MAX_LINES = 200
_OUTPUT_HISTORY = deque(maxlen=_OUTPUT_HISTORY_MAX_LINES)


def _coerce_output_history_limit(value) -> int:
    try:
        return max(10, int(value))
    except (TypeError, ValueError):
        return 200


def _configure_output_history(enabled: bool, max_lines=200) -> None:
    """Configure recent CLI output replayed after terminal redraws."""
    global _OUTPUT_HISTORY_ENABLED, _OUTPUT_HISTORY_MAX_LINES, _OUTPUT_HISTORY
    _OUTPUT_HISTORY_ENABLED = bool(enabled)
    _OUTPUT_HISTORY_MAX_LINES = _coerce_output_history_limit(max_lines)
    _OUTPUT_HISTORY = deque(maxlen=_OUTPUT_HISTORY_MAX_LINES)


def _clear_output_history() -> None:
    _OUTPUT_HISTORY.clear()


@contextmanager
def _suspend_output_history():
    global _OUTPUT_HISTORY_SUPPRESSED
    old_value = _OUTPUT_HISTORY_SUPPRESSED
    _OUTPUT_HISTORY_SUPPRESSED = True
    try:
        yield
    finally:
        _OUTPUT_HISTORY_SUPPRESSED = old_value


def _record_output_history_entry(entry) -> None:
    if not _OUTPUT_HISTORY_ENABLED or _OUTPUT_HISTORY_REPLAYING or _OUTPUT_HISTORY_SUPPRESSED:
        return
    _OUTPUT_HISTORY.append(entry)


def _record_output_history(text: str) -> None:
    if not _OUTPUT_HISTORY_ENABLED or _OUTPUT_HISTORY_REPLAYING or _OUTPUT_HISTORY_SUPPRESSED:
        return
    normalized = str(text).replace("\r", "").rstrip("\n")
    if not normalized:
        return
    for line in normalized.splitlines():
        _record_output_history_entry(line)


def _replay_output_history() -> None:
    """Repaint recent output above the prompt after a full screen clear."""
    global _OUTPUT_HISTORY_REPLAYING
    if not _OUTPUT_HISTORY_ENABLED or not _OUTPUT_HISTORY:
        return
    _OUTPUT_HISTORY_REPLAYING = True
    try:
        rendered_lines = []
        for entry in tuple(_OUTPUT_HISTORY):
            if callable(entry):
                try:
                    lines = entry()
                except Exception:
                    continue
                if isinstance(lines, str):
                    lines = lines.splitlines()
            else:
                lines = [entry]
            rendered_lines.extend(str(line) for line in lines)
        if rendered_lines:
            # Replay after resize can contain hundreds of history lines. A
            # per-line prompt_toolkit print forces one synchronous terminal I/O
            # and redraw cycle per line, which users perceive as a waterfall of
            # old output. Keep the existing history contents unchanged, but
            # emit the replay as one ANSI payload so resize recovery does a
            # single prompt_toolkit print/redraw.
            _pt_print(_PT_ANSI("\n".join(rendered_lines)))
    except Exception:
        pass
    finally:
        _OUTPUT_HISTORY_REPLAYING = False


def _cprint(text: str):
    """Print ANSI-colored text through prompt_toolkit's native renderer.

    Raw ANSI escapes written via print() are swallowed by patch_stdout's
    StdoutProxy.  Routing through print_formatted_text(ANSI(...)) lets
    prompt_toolkit parse the escapes and render real colors.

    When called from a background thread while a prompt_toolkit
    ``Application`` is running (the common case for the self-improvement
    background review's ``💾 …`` summary, curator summaries, and other
    bg-thread emissions), a direct ``_pt_print`` races with the input
    area's redraw and the line can end up visually buried behind the
    prompt.  Route those cases through ``run_in_terminal`` via
    ``loop.call_soon_threadsafe``, which pauses the input area, prints
    the line above it, and redraws the prompt cleanly.
    """
    _record_output_history(text)

    try:
        from prompt_toolkit.application import get_app_or_none, run_in_terminal
    except Exception:
        _pt_print(_PT_ANSI(text))
        return

    app = None
    try:
        app = get_app_or_none()
    except Exception:
        app = None

    # No active app, or we're already on the app's main thread: the
    # direct prompt_toolkit print is safe and matches existing behavior
    # (spinner frames, streamed tokens, tool activity prefixes, …).
    if app is None or not getattr(app, "_is_running", False):
        try:
            _pt_print(_PT_ANSI(text))
        except Exception:
            # Fallback when stdout is not a real console (e.g. subprocess
            # worker logging to a file). prompt_toolkit raises
            # NoConsoleScreenBufferError (Windows) or OSError (other).
            try:
                print(text)
            except Exception:
                pass
        return

    try:
        loop = app.loop  # type: ignore[attr-defined]
    except Exception:
        loop = None
    if loop is None:
        _pt_print(_PT_ANSI(text))
        return

    import asyncio as _asyncio
    try:
        # Use get_running_loop() instead of get_event_loop() to avoid the
        # DeprecationWarning / RuntimeWarning emitted by Python 3.10+ when
        # get_event_loop() is called from a thread that has no current event
        # loop set (e.g. the process_loop background thread).  Fixes #19285.
        current_loop = _asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None
    except Exception:
        current_loop = None
    # Same thread as the app's loop → safe to print directly.
    if current_loop is loop and loop.is_running():
        _pt_print(_PT_ANSI(text))
        return

    # Cross-thread emission: ask the app's event loop to schedule a
    # ``run_in_terminal`` that wraps ``_pt_print``.  This hides the
    # prompt, prints, and redraws.  Fire-and-forget — if scheduling
    # fails we fall back to a direct print so the line isn't lost.
    def _schedule():
        # run_in_terminal() may return either:
        #   • a coroutine / Future (prompt_toolkit ≥ 3.0) — must be scheduled
        #     via ensure_future so the coroutine is actually awaited; calling
        #     it bare would leave it unawaited and silently drop the output
        #     (fixes #23185 Bug A).
        #   • None (some mocks / older PT builds) — just call the inner
        #     function directly since PT already executed it synchronously.
        # Do NOT fall back to a bare _pt_print when ensure_future raises,
        # because run_in_terminal already invoked the lambda in that case
        # (the mock path), which would double-print the line.
        try:
            import asyncio as _aio
            import inspect as _inspect
            coro = run_in_terminal(lambda: _pt_print(_PT_ANSI(text)))
            if coro is not None and (_inspect.isawaitable(coro) or _inspect.iscoroutine(coro)):
                _aio.ensure_future(coro)
            # else: run_in_terminal ran the lambda synchronously; nothing more
            # to do (double-scheduling would print twice).
        except Exception:
            pass  # best-effort; the line may already have been printed

    try:
        loop.call_soon_threadsafe(_schedule)
    except Exception:
        try:
            _pt_print(_PT_ANSI(text))
        except Exception:
            pass


def _prepend_note_to_message(message, note: str):
    """Prepend a one-shot system-style note to a user message.

    ``message`` is normally a plain string, but when the user attaches an image
    to a vision-capable model it becomes a list of OpenAI-style content parts
    (text + ``image_url`` blocks). Naively doing ``note + "\\n\\n" + message``
    then raises ``TypeError: can only concatenate str (not "list") to str`` —
    e.g. running ``/model ...`` (which queues a model-switch note) and then
    sending a pasted image in the same turn.

    Returns the message with ``note`` prepended:
      * ``str``  → ``f"{note}\\n\\n{message}"`` (just ``note`` when empty)
      * ``list`` → note folded into the first text part, or inserted as a new
        leading ``{"type": "text"}`` part when there is no text part.
    Unknown shapes are returned unchanged (fail-open).
    """
    note = str(note or "").strip()
    if not note:
        return message
    if isinstance(message, str):
        return f"{note}\n\n{message}" if message else note
    if isinstance(message, list):
        parts = list(message)
        for i, part in enumerate(parts):
            if isinstance(part, dict) and part.get("type") == "text":
                merged = dict(part)
                text = merged.get("text", "")
                merged["text"] = f"{note}\n\n{text}" if text else note
                parts[i] = merged
                return parts
        # No text part (image-only) — insert the note as a leading text block.
        return [{"type": "text", "text": note}, *parts]
    return message


def _cli_visible_print(text: str = "") -> None:
    """Print normally unless prompt_toolkit owns the live terminal.

    Bare ``print()`` output is swallowed by ``patch_stdout`` while an
    interactive ``Application`` is running, so ``/sessions`` and ``/history``
    would render nothing. Route through ``_cprint`` (prompt_toolkit-native)
    in that case, and fall back to ``print`` otherwise.
    """
    try:
        from prompt_toolkit.application import get_app_or_none
        app = get_app_or_none()
    except Exception:
        app = None

    if app is not None and getattr(app, "_is_running", False):
        _cprint(text)
    else:
        print(text)


# ---------------------------------------------------------------------------
# File-drop / local attachment detection — extracted as pure helpers for tests.
# ---------------------------------------------------------------------------

_IMAGE_EXTENSIONS = frozenset({
    '.png', '.jpg', '.jpeg', '.gif', '.webp',
    '.bmp', '.tiff', '.tif', '.svg', '.ico',
})


from opencodon_constants import is_termux as _is_termux_environment


def _termux_example_image_path(filename: str = "cat.png") -> str:
    """Return a realistic example media path for the current Termux setup."""
    candidates = [
        os.path.expanduser("~/storage/shared"),
        "/sdcard",
        "/storage/emulated/0",
        "/storage/self/primary",
    ]
    for root in candidates:
        if os.path.isdir(root):
            return os.path.join(root, "Pictures", filename)
    return os.path.join("~/storage/shared", "Pictures", filename)


def _split_path_input(raw: str) -> tuple[str, str]:
    r"""Split a leading file path token from trailing free-form text.

    Supports quoted paths and backslash-escaped spaces so callers can accept
    inputs like:
      /tmp/pic.png describe this
      ~/storage/shared/My\ Photos/cat.png what is this?
      "/storage/emulated/0/DCIM/Camera/cat 1.png" summarize
    """
    raw = str(raw or "").strip()
    if not raw:
        return "", ""

    if raw[0] in {'"', "'"}:
        quote = raw[0]
        pos = 1
        while pos < len(raw):
            ch = raw[pos]
            if ch == '\\' and pos + 1 < len(raw):
                pos += 2
                continue
            if ch == quote:
                token = raw[1:pos]
                remainder = raw[pos + 1 :].strip()
                return token, remainder
            pos += 1
        return raw[1:], ""

    pos = 0
    while pos < len(raw):
        ch = raw[pos]
        if ch == '\\' and pos + 1 < len(raw) and raw[pos + 1] == ' ':
            pos += 2
        elif ch == ' ':
            break
        else:
            pos += 1

    token = raw[:pos].replace('\\ ', ' ')
    remainder = raw[pos:].strip()
    return token, remainder


def _resolve_attachment_path(raw_path: str) -> Path | None:
    """Resolve a user-supplied local attachment path.

    Accepts quoted or unquoted paths, expands ``~`` and env vars, and resolves
    relative paths from ``TERMINAL_CWD`` when set (matching terminal tool cwd).
    Returns ``None`` when the path does not resolve to an existing file.
    """
    token = str(raw_path or "").strip()
    if not token:
        return None

    if (token.startswith('"') and token.endswith('"')) or (token.startswith("'") and token.endswith("'")):
        token = token[1:-1].strip()
    token = token.replace('\\ ', ' ')
    if not token:
        return None

    expanded = token
    if token.startswith("file://"):
        try:
            parsed = urlparse(token)
            if parsed.scheme == "file":
                expanded = unquote(parsed.path or "")
                if parsed.netloc and os.name == "nt":
                    expanded = f"//{parsed.netloc}{expanded}"
        except Exception:
            expanded = token
    expanded = os.path.expandvars(os.path.expanduser(expanded))
    if os.name != "nt":
        normalized = expanded.replace("\\", "/")
        if len(normalized) >= 3 and normalized[1] == ":" and normalized[2] == "/" and normalized[0].isalpha():
            expanded = f"/mnt/{normalized[0].lower()}/{normalized[3:]}"
    path = Path(expanded)
    if not path.is_absolute():
        base_dir = Path(os.getenv("TERMINAL_CWD", os.getcwd()))
        path = base_dir / path

    try:
        resolved = path.resolve()
    except Exception:
        resolved = path

    # Path.exists() / is_file() invoke os.stat(), which raises OSError when
    # the candidate string is structurally invalid as a path — most commonly
    # ENAMETOOLONG (errno 63 on macOS, errno 36 on Linux) when the input
    # exceeds NAME_MAX (typically 255 bytes). This bites pasted slash
    # commands like `/goal <long prose>` because `_detect_file_drop()`'s
    # `starts_like_path` prefilter accepts any input starting with `/`,
    # then this resolver tries to stat it before short-circuiting on the
    # slash-command path. Without this guard the OSError propagates up to
    # the process_loop catch-all in _interactive_loop and the user input
    # is silently lost (the warning ends up in agent.log but the user sees
    # nothing — the prompt just hangs).
    try:
        if not resolved.exists() or not resolved.is_file():
            return None
    except OSError:
        return None
    return resolved





def _detect_file_drop(user_input: str) -> "dict | None":
    """Detect if *user_input* starts with a real local file path.

    This catches dragged/pasted paths before they are mistaken for slash
    commands, and also supports Termux-friendly paths like ``~/storage/...``.

    Returns a dict on match::

        {
            "path": Path,          # resolved file path
            "is_image": bool,      # True when suffix is a known image type
            "remainder": str,      # any text after the path
        }

    Returns ``None`` when the input is not a real file path.
    """
    if not isinstance(user_input, str):
        return None

    stripped = user_input.strip()
    if not stripped:
        return None

    starts_like_path = (
        stripped.startswith("/")
        or stripped.startswith("~")
        or stripped.startswith("./")
        or stripped.startswith("../")
        or stripped.startswith("file://")
        or (len(stripped) >= 3 and stripped[1] == ":" and stripped[2] in {"\\", "/"} and stripped[0].isalpha())
        or stripped.startswith('"/')
        or stripped.startswith('"~')
        or stripped.startswith("'/")
        or stripped.startswith("'~")
        or stripped.startswith('"./')
        or stripped.startswith('"../')
        or stripped.startswith("'./")
        or stripped.startswith("'../")
        or (len(stripped) >= 4 and stripped[0] in {"'", '"'} and stripped[2] == ":" and stripped[3] in {"\\", "/"} and stripped[1].isalpha())
    )
    if not starts_like_path:
        return None

    direct_path = _resolve_attachment_path(stripped)
    if direct_path is not None:
        return {
            "path": direct_path,
            "is_image": direct_path.suffix.lower() in _IMAGE_EXTENSIONS,
            "remainder": "",
        }

    first_token, remainder = _split_path_input(stripped)
    drop_path = _resolve_attachment_path(first_token)
    if drop_path is None and " " in stripped and stripped[0] not in {"'", '"'}:
        space_positions = [idx for idx, ch in enumerate(stripped) if ch == " "]
        for pos in reversed(space_positions):
            candidate = stripped[:pos].rstrip()
            resolved = _resolve_attachment_path(candidate)
            if resolved is not None:
                drop_path = resolved
                remainder = stripped[pos + 1 :].strip()
                break
    if drop_path is None:
        return None

    return {
        "path": drop_path,
        "is_image": drop_path.suffix.lower() in _IMAGE_EXTENSIONS,
        "remainder": remainder,
    }


def _format_image_attachment_badges(attached_images: list[Path], image_counter: int, width: int | None = None) -> str:
    """Format the attached-image badge row for the interactive CLI.

    Narrow terminals such as Termux should get a compact summary that fits on a
    single row, while wider terminals can show the classic per-image badges.
    """
    if not attached_images:
        return ""

    width = width or shutil.get_terminal_size((80, 24)).columns

    def _trunc(name: str, limit: int) -> str:
        return name if len(name) <= limit else name[: max(1, limit - 3)] + "..."

    if width < 52:
        if len(attached_images) == 1:
            return f"[📎 {_trunc(attached_images[0].name, 20)}]"
        return f"[📎 {len(attached_images)} images attached]"

    if width < 80:
        if len(attached_images) == 1:
            return f"[📎 {_trunc(attached_images[0].name, 32)}]"
        first = _trunc(attached_images[0].name, 20)
        extra = len(attached_images) - 1
        return f"[📎 {first}] [+{extra}]"

    base = image_counter - len(attached_images) + 1
    return " ".join(
        f"[📎 Image #{base + i}]"
        for i in range(len(attached_images))
    )


def _should_auto_attach_clipboard_image_on_paste(pasted_text: str) -> bool:
    """Auto-attach clipboard images only for image-only paste gestures."""
    return not pasted_text.strip()


def _strip_leaked_bracketed_paste_wrappers(text: str) -> str:
    from opencodon.frontends.cli.input_sanitize import strip_leaked_bracketed_paste_wrappers

    return strip_leaked_bracketed_paste_wrappers(text)


def _apply_bracketed_paste_timeout_patch() -> None:
    """Patch prompt_toolkit to recover from torn bracketed-paste sequences.

    prompt_toolkit's ``Vt100Parser.feed()`` buffers all input while waiting
    for the ESC[201~ end mark.  If a terminal drops that end mark (terminal
    race, torn write, SSH glitch, macOS sleep/wake), input appears frozen
    forever — the only recovery used to be killing the tab.

    This patch wraps ``Vt100Parser.feed`` so that bracketed-paste mode
    flushes buffered content as a normal ``BracketedPaste`` event after
    ``_BP_TIMEOUT_S`` seconds without an end marker, then resumes normal
    parsing.  See upstream issue #16263.

    The patch is idempotent — repeated calls are no-ops via the
    ``_opencodon_bp_timeout_patched`` sentinel on the module.
    """
    try:
        import prompt_toolkit.input.vt100_parser as _vt100_mod
        from prompt_toolkit.keys import Keys as _PtKeys
        from prompt_toolkit.key_binding.key_processor import KeyPress as _PtKeyPress

        if getattr(_vt100_mod, "_opencodon_bp_timeout_patched", False):
            return

        _BP_TIMEOUT_S = 2.0  # max time to wait for ESC[201~ before flushing

        def _patched_vt100_feed(self_parser, data: str) -> None:
            if self_parser._in_bracketed_paste:
                self_parser._paste_buffer += data
                end_mark = "\x1b[201~"

                if end_mark in self_parser._paste_buffer:
                    end_index = self_parser._paste_buffer.index(end_mark)
                    paste_content = self_parser._paste_buffer[:end_index]
                    self_parser.feed_key_callback(
                        _PtKeyPress(_PtKeys.BracketedPaste, paste_content)
                    )
                    self_parser._in_bracketed_paste = False
                    remaining = self_parser._paste_buffer[
                        end_index + len(end_mark):
                    ]
                    self_parser._paste_buffer = ""
                    self_parser._opencodon_bp_start = None
                    if remaining:
                        _patched_vt100_feed(self_parser, remaining)
                else:
                    bp_start = getattr(self_parser, "_opencodon_bp_start", None)
                    now = time.monotonic()
                    if bp_start is None:
                        self_parser._opencodon_bp_start = now
                    elif now - bp_start > _BP_TIMEOUT_S:
                        paste_content = self_parser._paste_buffer
                        self_parser._in_bracketed_paste = False
                        self_parser._paste_buffer = ""
                        self_parser._opencodon_bp_start = None
                        if paste_content:
                            self_parser.feed_key_callback(
                                _PtKeyPress(_PtKeys.BracketedPaste, paste_content)
                            )
                            logger.warning(
                                "Bracketed-paste timeout (%.1fs) — flushed %d bytes "
                                "without end mark. Terminal may have dropped ESC[201~ "
                                "(see #16263).",
                                now - bp_start,
                                len(paste_content),
                            )
            else:
                # Normal mode — re-inline prompt_toolkit's normal feed path.
                # Calling the original feed here would double-buffer after the
                # bracketed-paste entry transition.
                for i, c in enumerate(data):
                    if self_parser._in_bracketed_paste:
                        _patched_vt100_feed(self_parser, data[i:])
                        break
                    self_parser._input_parser.send(c)

        _vt100_mod.Vt100Parser.feed = _patched_vt100_feed
        _vt100_mod._opencodon_bp_timeout_patched = True
        logger.debug("Applied Vt100Parser bracketed-paste timeout patch (#16263)")
    except Exception as exc:  # noqa: BLE001 — defensive: never break startup
        logger.debug("Bracketed-paste timeout patch skipped: %s", exc)


# Cursor Position Report (CPR / DSR) response, format ``ESC[<row>;<col>R``.
# prompt_toolkit's _on_resize() + renderer send ``ESC[6n`` queries to the
# terminal; under resize storms or tab switches the terminal's reply can
# race past the input parser and end up in the input buffer as literal
# text (see issue #14692). Also matches the visible-form ``^[[<row>;<col>R``
# that appears when the ESC byte was stripped by a prior filter.
_DSR_CPR_ESC_RE = re.compile(r"\x1b\[\d+;\d+R")
_DSR_CPR_VISIBLE_RE = re.compile(r"\^\[\[\d+;\d+R")
_SGR_MOUSE_ESC_RE = re.compile(r"\x1b\[<\d+;\d+;\d+[Mm]")
_SGR_MOUSE_VISIBLE_RE = re.compile(r"\^\[\[<\d+;\d+;\d+[Mm]")
# Some terminals/filters can drop ESC and literal "^[[", leaving only
# "<btn;col;rowM" fragments in the buffer. Keep this broad on purpose:
# these fragments are extremely unlikely to be intentional user input, and
# stripping them is better than sending corrupted prompts.
_SGR_MOUSE_BARE_RE = re.compile(r"<\d+;\d+;\d+[Mm]")
_TERMINAL_INPUT_MODE_RESET_SEQ = (
    "\x1b[?1006l"  # disable SGR mouse
    "\x1b[?1003l"  # disable any-motion tracking
    "\x1b[?1002l"  # disable button-motion tracking
    "\x1b[?1000l"  # disable click tracking
    "\x1b[?1004l"  # disable focus events
    "\x1b[?2004l"  # disable bracketed paste
    "\x1b[?1049l"  # leave alt screen (if stuck there)
    "\x1b[<u"      # pop kitty keyboard mode
    "\x1b[>4m"     # reset modifyOtherKeys
    "\x1b[0m"      # reset text attributes
    "\x1b[?25h"    # ensure cursor visible
)


def _preserve_ctrl_enter_newline() -> bool:
    """Detect environments where Ctrl+Enter must produce a newline, not submit.

    Windows Terminal, WSL, SSH sessions, Ghostty, and some modern terminals
    deliver Ctrl+Enter/Ctrl+J as bare LF (c-j). On those terminals c-j must
    NOT be bound to submit;
    binding it to submit makes Ctrl+Enter (intended as 'newline like Alt+Enter')
    submit instead. Local POSIX TTYs that deliver Enter as LF (docker exec,
    some thin PTYs without SSH) still need c-j bound to submit, so we keep
    that binding for those.

    See issue #22379.
    """
    if sys.platform == "win32":
        return True
    if any(os.environ.get(v) for v in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY")):
        return True
    if os.environ.get("WT_SESSION"):
        return True
    if os.environ.get("GHOSTTY_RESOURCES_DIR") or os.environ.get("GHOSTTY_BIN_DIR"):
        return True
    if os.environ.get("TERM", "").lower() == "xterm-ghostty":
        return True
    if os.environ.get("TERM_PROGRAM", "").lower() == "ghostty":
        return True
    if "microsoft" in os.environ.get("WSL_DISTRO_NAME", "").lower():
        return True
    # WSL detection — env vars can be scrubbed under sudo, also peek /proc.
    for p in ("/proc/version", "/proc/sys/kernel/osrelease"):
        try:
            with open(p, "r", encoding="utf-8", errors="ignore") as f:
                if "microsoft" in f.read().lower():
                    return True
        except OSError:
            continue
    return False


def _bind_prompt_submit_keys(kb, handler) -> None:
    """Bind terminal Enter forms to the submit handler.

    Enter is always submit. On POSIX we also bind c-j (LF) to submit because
    some thin PTYs (docker exec, certain SSH flavors) deliver Enter as LF
    instead of CR — without this, Enter appears dead on those terminals.

    Exception: on Windows, WSL, SSH sessions, Windows Terminal, and Ghostty,
    c-j is the wire encoding of Ctrl+Enter (a distinct keystroke from
    plain Enter / c-m). We leave c-j unbound there so the c-j newline
    handler registered separately can fire — giving the user an
    Enter-involving newline keystroke without terminal settings changes.
    See _preserve_ctrl_enter_newline() and issue #22379.
    """
    kb.add("enter")(handler)
    if sys.platform != "win32" and not _preserve_ctrl_enter_newline():
        kb.add("c-j")(handler)


def _disable_prompt_toolkit_cpr_warning(app) -> None:
    """Let prompt_toolkit fall back from CPR without printing into the prompt."""
    try:
        app.renderer.cpr_not_supported_callback = None
    except Exception:
        pass


def _terminal_may_leak_cpr() -> bool:
    """Whether classic CLI should suppress prompt_toolkit CPR (ESC[6n) queries.

    Delayed CPR replies (``ESC[<row>;<col>R`` / visible ``^[[<row>;<col>R``)
    leak into the status line and can freeze input when the reply is slow
    (#13870 on SSH/slow PTYs). The same race hits local POSIX TTYs under
    heavy subagent / status-line load — see ``tests/cli/test_cpr_local_leak.py``.

    Policy:
    - ``PROMPT_TOOLKIT_NO_CPR=1`` → always suppress
    - native Windows (``win32``) → keep prompt_toolkit's default for now
      (no native-Windows Application coverage yet); still honor NO_CPR
    - all other platforms → suppress (CPR is only a layout hint; heuristic
      height is enough). SSH env is no longer required to trigger this.
    """
    if os.environ.get("PROMPT_TOOLKIT_NO_CPR", "") == "1":
        return True
    if sys.platform == "win32":
        return False
    return True


def _build_cpr_disabled_output(stdout):
    """Build a Vt100_Output that never sends Cursor Position Report queries.

    prompt_toolkit's renderer sends ``ESC[6n`` (Device Status Report) to learn
    the cursor row before painting in non-fullscreen mode; the terminal replies
    ``ESC[<row>;<col>R``. When that reply is delayed it races into the display
    as raw ``^[[39;1R`` and can stall the renderer's pending-CPR future
    (#13870; also local POSIX under heavy subagent load).

    Constructing the output with ``enable_cpr=False`` marks CPR
    ``NOT_SUPPORTED`` so ``ESC[6n`` is never sent. prompt_toolkit then uses its
    heuristic available-height fallback. Input-side
    ``_strip_leaked_terminal_responses`` remains belt-and-suspenders.

    Note: ``Vt100_Output.from_pty()`` does NOT expose ``enable_cpr`` in
    prompt_toolkit 3.x, so we reproduce its ``get_size`` setup and call the
    constructor directly. Returns ``None`` on any failure so the caller falls back
    to prompt_toolkit's default output (CPR enabled, but input-side scrubbing
    still protects against leaks).
    """
    try:
        import io as _io
        from prompt_toolkit.output.vt100 import Vt100_Output, _get_size
        from prompt_toolkit.data_structures import Size

        def _get_term_size():
            rows = columns = None
            try:
                rows, columns = _get_size(stdout.fileno())
            except (OSError, _io.UnsupportedOperation, AttributeError, ValueError):
                pass
            return Size(rows=rows or 24, columns=columns or 80)

        return Vt100_Output(stdout, _get_term_size, enable_cpr=False)
    except Exception:
        return None


def _select_classic_cli_pt_output(stdout):
    """Select prompt_toolkit Output for classic-CLI Application construction.

    Returns a CPR-disabled ``Vt100_Output`` when ``_terminal_may_leak_cpr()``
    is true, otherwise ``None`` so Application keeps prompt_toolkit's default
    output (Windows preserve-default path).
    """
    if not _terminal_may_leak_cpr():
        return None
    return _build_cpr_disabled_output(stdout)


def _strip_leaked_terminal_responses_with_meta(text: str) -> tuple[str, bool]:
    """Strip leaked terminal control-response sequences from user input.

    Covers Cursor Position Report (CPR / DSR) responses — ``ESC[<row>;<col>R``
    and the visible ``^[[<row>;<col>R`` form. These are replies the terminal
    sends back to queries prompt_toolkit makes during ``_on_resize`` /
    ``_request_absolute_cursor_position``. When the input parser drops one
    (resize storms, multiplexer focus changes, slow PTYs) the response
    lands in the input buffer as literal text and corrupts what the user
    typed.

    Also strips leaked SGR mouse-report fragments (``ESC[<...M/m`` and
    degraded visible forms). Returns ``(cleaned_text, had_mouse_reports)``
    so callers can trigger an in-place terminal mode recovery when needed.
    """
    if not text:
        return text, False

    has_esc = "\x1b[" in text
    has_visible = "^[" in text
    has_bare_mouse = "<" in text and ";" in text and ("M" in text or "m" in text)
    if not (has_esc or has_visible or has_bare_mouse):
        return text, False

    had_mouse_reports = False

    if has_esc:
        text = _DSR_CPR_ESC_RE.sub("", text)
        text, count = _SGR_MOUSE_ESC_RE.subn("", text)
        had_mouse_reports = had_mouse_reports or count > 0

    if has_visible:
        text = _DSR_CPR_VISIBLE_RE.sub("", text)
        text, count = _SGR_MOUSE_VISIBLE_RE.subn("", text)
        had_mouse_reports = had_mouse_reports or count > 0

    if has_bare_mouse:
        text, count = _SGR_MOUSE_BARE_RE.subn("", text)
        had_mouse_reports = had_mouse_reports or count > 0

    return text, had_mouse_reports


def _strip_leaked_terminal_responses(text: str) -> str:
    """Compatibility wrapper returning only cleaned text."""
    cleaned, _ = _strip_leaked_terminal_responses_with_meta(text)
    return cleaned


def _estimate_tui_input_height(
    lines: list[str] | tuple[str, ...],
    prompt_text: str,
    terminal_columns: int,
    *,
    max_height: int = 8,
) -> int:
    """Estimate classic prompt_toolkit input rows using live terminal cells.

    The TextArea prompt is injected with prompt_toolkit's BeforeInput
    processor, which means it consumes cells only on logical line 0. After a
    narrow resize, that first row can leave only one input cell beside an icon
    prompt such as ``⚔ ``, while continuation rows use the full terminal width.
    Never substitute a fake wide fallback here: under- or over-allocating the
    TextArea height leaves stale prompt/input cells visible at the bottom of the
    terminal.
    """
    try:
        from prompt_toolkit.utils import get_cwidth
    except Exception:
        get_cwidth = lambda value: len(value or "")  # type: ignore[assignment]

    try:
        columns = int(terminal_columns or 0)
    except (TypeError, ValueError):
        columns = 0

    columns = max(1, columns)
    prompt_width = max(0, get_cwidth(prompt_text or ""))

    visual_lines = 0
    for index, line in enumerate(lines or [""]):
        # prompt_toolkit's TextArea injects ``prompt`` via BeforeInput, which
        # applies only to logical line 0. Wrapped continuation rows, and later
        # logical lines, use the full terminal width. Count the display cells
        # after that same transformation rather than subtracting the prompt from
        # every wrapped row.
        line_width = get_cwidth(line or "")
        display_width = line_width + (prompt_width if index == 0 else 0)
        if display_width <= 0:
            visual_lines += 1
        else:
            visual_lines += max(1, -(-display_width // columns))

    return min(max(visual_lines, 1), max(1, int(max_height or 1)))


def _collect_query_images(query: str | None, image_arg: str | None = None) -> tuple[str, list[Path]]:
    """Collect local image attachments for single-query CLI flows."""
    message = query or ""
    images: list[Path] = []

    if isinstance(message, str):
        dropped = _detect_file_drop(message)
        if dropped and dropped.get("is_image"):
            images.append(dropped["path"])
            message = dropped["remainder"] or f"[User attached image: {dropped['path'].name}]"

    if image_arg:
        explicit_path = _resolve_attachment_path(image_arg)
        if explicit_path is None:
            raise ValueError(f"Image file not found: {image_arg}")
        if explicit_path.suffix.lower() not in _IMAGE_EXTENSIONS:
            raise ValueError(f"Not a supported image file: {explicit_path}")
        images.append(explicit_path)

    deduped: list[Path] = []
    seen: set[str] = set()
    for img in images:
        key = str(img)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(img)
    return message, deduped


# Strip OSC escape sequences (e.g. OSC-8 hyperlinks) that prompt_toolkit's
# ANSI parser can't handle — it strips \x1b but passes the payload through
# as literal text, garbling the TUI output.
_OSC_ESCAPE_RE = re.compile(r"\x1b\][\s\S]*?(?:\x07|\x1b\\)")


class ChatConsole:
    """Rich Console adapter for prompt_toolkit's patch_stdout context.

    Captures Rich's rendered ANSI output and routes it through _cprint
    so colors and markup render correctly inside the interactive chat loop.
    Drop-in replacement for Rich Console — just pass this to any function
    that expects a console.print() interface.
    """

    def __init__(self):
        from io import StringIO
        self._buffer = StringIO()
        self._inner = Console(
            file=self._buffer,
            force_terminal=True,
            color_system="truecolor",
            highlight=False,
        )

    def print(self, *args, **kwargs):
        self._buffer.seek(0)
        self._buffer.truncate()
        # Read terminal width at render time so panels adapt to current size
        self._inner.width = shutil.get_terminal_size((80, 24)).columns
        self._inner.print(*args, **kwargs)
        output = self._buffer.getvalue()
        # Strip OSC escape sequences (e.g. OSC-8 hyperlinks) before
        # routing through prompt_toolkit's ANSI parser, which only
        # handles CSI/SGR and passes OSC payload through as literal text.
        output = _OSC_ESCAPE_RE.sub("", output)
        for line in output.rstrip("\n").split("\n"):
            _cprint(line)

    @contextmanager
    def status(self, *_args, **_kwargs):
        """Provide a no-op Rich-compatible status context.

        Some slash command helpers use ``console.status(...)`` when running in
        the standalone CLI. Interactive chat routes those helpers through
        ``ChatConsole()``, which historically only implemented ``print()``.
        Returning a silent context manager keeps slash commands compatible
        without duplicating the higher-level busy indicator already shown by
        ``OpencodonCLI._busy_command()``.
        """
        yield self

# ASCII Art - OPENCODON wordmark (full width, single line - requires ~80 char terminal)
OPENCODON_AGENT_LOGO = """[bold #A3E635] ██████╗ ██████╗ ███████╗███╗   ██╗ ██████╗ ██████╗ ██████╗  ██████╗ ███╗   ██╗[/]
[bold #A3E635]██╔═══██╗██╔══██╗██╔════╝████╗  ██║██╔════╝██╔═══██╗██╔══██╗██╔═══██╗████╗  ██║[/]
[#84CC16]██║   ██║██████╔╝█████╗  ██╔██╗ ██║██║     ██║   ██║██║  ██║██║   ██║██╔██╗ ██║[/]
[#84CC16]██║   ██║██╔═══╝ ██╔══╝  ██║╚██╗██║██║     ██║   ██║██║  ██║██║   ██║██║╚██╗██║[/]
[#65A30D]╚██████╔╝██║     ███████╗██║ ╚████║╚██████╗╚██████╔╝██████╔╝╚██████╔╝██║ ╚████║[/]
[#4D7C0F] ╚═════╝ ╚═╝     ╚══════╝╚═╝  ╚═══╝ ╚═════╝ ╚═════╝ ╚═════╝  ╚═════╝ ╚═╝  ╚═══╝[/]"""

# No default hero art: the banner's left column is the wordmark + session info.
# Skins may still supply their own via the `banner_hero` key (see skin_engine).



def _build_compact_banner() -> str:
    """Build a compact banner that fits the current terminal width."""
    try:
        from opencodon.frontends.cli.skin_engine import get_active_skin
        _skin = get_active_skin()
    except Exception:
        _skin = None

    skin_name = getattr(_skin, "name", "default") if _skin else "default"
    border_color = _skin.get_color("banner_border", "#FFD700") if _skin else "#FFD700"
    title_color = _skin.get_color("banner_title", "#FFBF00") if _skin else "#FFBF00"
    dim_color = _skin.get_color("banner_dim", "#B8860B") if _skin else "#B8860B"

    if skin_name == "default":
        line1 = "🧬 OPENCODON - The Open-Science AI Agent"
        tiny_line = "🧬 OPENCODON"
    else:
        agent_name = _skin.get_branding("agent_name", "opencodon") if _skin else "opencodon"
        line1 = f"{agent_name} - AI Agent Framework"
        tiny_line = agent_name

    if os.environ.get("OPENCODON_FAST_STARTUP_BANNER") == "1":
        from opencodon.frontends.cli import __version__ as _version

        version_line = f"opencodon v{_version}"
    else:
        version_line = format_banner_version_label()

    w = min(shutil.get_terminal_size().columns - 2, 88)
    if w < 30:
        return f"\n[{title_color}]{tiny_line}[/]\n"

    inner = w - 2  # inside the box border
    bar = "═" * w
    content_width = inner - 2

    # Truncate and pad to fit
    line1 = line1[:content_width].ljust(content_width)
    line2 = version_line[:content_width].ljust(content_width)

    return (
        f"\n[bold {border_color}]╔{bar}╗[/]\n"
        f"[bold {border_color}]║[/] [{title_color}]{line1}[/] [bold {border_color}]║[/]\n"
        f"[bold {border_color}]║[/] [dim {dim_color}]{line2}[/] [bold {border_color}]║[/]\n"
        f"[bold {border_color}]╚{bar}╝[/]\n"
    )



# ============================================================================
# Slash-command detection helper
# ============================================================================

def _looks_like_slash_command(text: str) -> bool:
    """Return True if *text* looks like a slash command, not a file path.

    Slash commands are ``/help``, ``/model gpt-4``, ``/q``, etc.
    File paths like ``/Users/ironin/file.md:45-46 can you fix this?``
    also start with ``/`` but contain additional ``/`` characters in
    the first whitespace-delimited word.  This helper distinguishes
    the two so that pasted paths are sent to the agent instead of
    triggering "Unknown command".
    """
    if not text or not text.startswith("/"):
        return False
    first_word = text.split()[0]
    # After stripping the leading /, a command name has no slashes.
    # A path like /Users/foo/bar.md always does.
    return "/" not in first_word[1:]


# ============================================================================
# Skill Slash Commands — dynamic commands generated from installed skills
# ============================================================================

_skill_commands = None
_skill_bundles = None


def _ensure_skill_commands() -> dict:
    global _skill_commands
    if _skill_commands is None:
        from opencodon.core.skills.skill_commands import scan_skill_commands

        _skill_commands = scan_skill_commands()
    return _skill_commands


def get_skill_commands() -> dict:
    return _ensure_skill_commands()


def build_skill_invocation_message(*args, **kwargs):
    from opencodon.core.skills.skill_commands import build_skill_invocation_message as _impl

    return _impl(*args, **kwargs)


def build_preloaded_skills_prompt(*args, **kwargs):
    from opencodon.core.skills.skill_commands import build_preloaded_skills_prompt as _impl

    return _impl(*args, **kwargs)


def get_skill_bundles() -> dict:
    global _skill_bundles
    if _skill_bundles is None:
        from opencodon.core.skills.skill_bundles import get_skill_bundles as _impl

        _skill_bundles = _impl()
    return _skill_bundles


def build_bundle_invocation_message(*args, **kwargs):
    from opencodon.core.skills.skill_bundles import build_bundle_invocation_message as _impl

    return _impl(*args, **kwargs)


def _get_plugin_cmd_handler_names() -> set:
    """Return plugin command names (without slash prefix) for dispatch matching."""
    try:
        from opencodon.plugins_runtime import get_plugin_commands
        return set(get_plugin_commands().keys())
    except Exception:
        return set()


def _parse_skills_argument(skills: str | list[str] | tuple[str, ...] | None) -> list[str]:
    """Normalize a CLI skills flag into a deduplicated list of skill identifiers."""
    if not skills:
        return []

    if isinstance(skills, str):
        raw_values = [skills]
    elif isinstance(skills, (list, tuple)):
        raw_values = [str(item) for item in skills if item is not None]
    else:
        raw_values = [str(skills)]

    parsed: list[str] = []
    seen: set[str] = set()
    for raw in raw_values:
        for part in raw.split(","):
            normalized = part.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            parsed.append(normalized)
    return parsed


# save_config_value moved to opencodon.config (restructure Phase 2) — the
# re-export keeps cli.save_config_value patchable and callable as before.
from opencodon.config import save_config_value  # noqa: E402


# ============================================================================
# OpencodonCLI Class
# ============================================================================

from opencodon.frontends.cli.shell_chrome import ShellChromeMixin
from opencodon.frontends.cli.shell_streaming import ShellStreamingMixin
from opencodon.frontends.cli.shell_show import ShellShowMixin
from opencodon.frontends.cli.shell_sessions_ux import ShellSessionUXMixin
from opencodon.frontends.cli.shell_model_switch import ShellModelSwitchMixin
from opencodon.frontends.cli.shell_voice import ShellVoiceMixin
from opencodon.frontends.cli.shell_prompts import ShellPromptsMixin
from opencodon.frontends.cli.shell_tool_events import ShellToolEventsMixin
from opencodon.frontends.cli.shell_reload import ShellReloadMixin
from opencodon.frontends.cli.shell_tui_layout import ShellTuiLayoutMixin



# Sentinel distinguishing "the original dispatch branch fell through" from an
# explicit return value (including the preserved bare-return None paths).
_SLASH_FALLTHROUGH = object()


class OpencodonCLI(
    ShellChromeMixin,
    ShellStreamingMixin,
    ShellShowMixin,
    ShellSessionUXMixin,
    ShellModelSwitchMixin,
    ShellVoiceMixin,
    ShellPromptsMixin,
    ShellToolEventsMixin,
    ShellReloadMixin,
    ShellTuiLayoutMixin,
    CLIAgentSetupMixin,
    CLICommandsMixin,
):
    """
    Interactive CLI for opencodon.
    
    Provides a REPL interface with rich formatting, command history,
    and tool execution capabilities.
    """
    
    def __init__(
        self,
        model: str = None,
        toolsets: List[str] = None,
        provider: str = None,
        api_key: str = None,
        base_url: str = None,
        max_turns: int = None,
        verbose: Optional[bool] = None,
        compact: bool = False,
        resume: str = None,
        checkpoints: bool = False,
        pass_session_id: bool = False,
        ignore_rules: bool = False,
    ):
        """
        Initialize the opencodon CLI.

        Args:
            model: Model to use (default: from env or claude-sonnet)
            toolsets: List of toolsets to enable (default: all)
            provider: Inference provider ("auto", "openrouter", "openai-codex", "zai", "kimi-coding", "minimax", "minimax-cn")
            api_key: API key (default: from environment)
            base_url: API base URL (default: OpenRouter)
            max_turns: Maximum tool-calling iterations shared with subagents (default: 90)
            verbose: Enable verbose logging
            compact: Use compact display mode
            resume: Session ID to resume (restores conversation history from SQLite)
            pass_session_id: Include the session ID in the agent's system prompt
        """
        # Initialize Rich console
        self.console = Console()
        self.config = CLI_CONFIG
        self.compact = compact if compact is not None else CLI_CONFIG["display"].get("compact", False)
        # tool_progress: "off", "new", "all", "verbose" (from config.yaml display section)
        # YAML 1.1 parses bare `off` as boolean False — normalise to string.
        _raw_tp = CLI_CONFIG["display"].get("tool_progress", "all")
        self.tool_progress_mode = "off" if _raw_tp is False else str(_raw_tp)
        # resume_display: "full" (show history) | "minimal" (one-liner only)
        self.resume_display = CLI_CONFIG["display"].get("resume_display", "full")
        # bell_on_complete: play terminal bell (\a) when agent finishes a response
        self.bell_on_complete = CLI_CONFIG["display"].get("bell_on_complete", False)
        # show_reasoning: display model thinking/reasoning before the response
        self.show_reasoning = CLI_CONFIG["display"].get("show_reasoning", True)
        # reasoning_full: when reasoning display is on, print the post-response
        # recap box uncollapsed instead of clamping to the first 10 lines.
        self.reasoning_full = CLI_CONFIG["display"].get("reasoning_full", False)
        _configure_output_history(
            enabled=CLI_CONFIG["display"].get("persistent_output", True),
            max_lines=CLI_CONFIG["display"].get("persistent_output_max_lines", 200),
        )
        # busy_input_mode: "interrupt" (Enter redirects current run),
        # "queue" (Enter queues for next turn), or "steer" (Enter injects
        # mid-run via /steer, arriving after the next tool call).
        _bim = str(CLI_CONFIG["display"].get("busy_input_mode", "interrupt")).strip().lower()
        if _bim == "queue":
            self.busy_input_mode = "queue"
        elif _bim == "steer":
            self.busy_input_mode = "steer"
        else:
            self.busy_input_mode = "interrupt"

        # self.verbose ONLY controls global DEBUG logging (root logger level).
        # display.tool_progress="verbose" controls tool-call rendering (full args,
        # results, think blocks) and is independent — see _apply_logging_levels.
        # Coupling the two (PR #6a1aa420e) caused all module DEBUG logs to spew
        # to console whenever a user set tool_progress: verbose in config.
        self.verbose = bool(verbose) if verbose is not None else False
        
        # streaming: stream tokens to the terminal as they arrive (display.streaming in config.yaml)
        self.streaming_enabled = CLI_CONFIG["display"].get("streaming", False)
        # show_timestamps: prefix user and assistant labels with timestamps
        self.show_timestamps = CLI_CONFIG["display"].get("timestamps", False)
        self.timestamp_format = CLI_CONFIG["display"].get("timestamp_format", "%H:%M")
        self.final_response_markdown = str(
            CLI_CONFIG["display"].get("final_response_markdown", "strip")
        ).strip().lower() or "strip"
        if self.final_response_markdown not in {"render", "strip", "raw"}:
            self.final_response_markdown = "strip"

        # Inline diff previews for write actions (display.inline_diffs in config.yaml)
        self._inline_diffs_enabled = CLI_CONFIG["display"].get("inline_diffs", True)

        # Submitted multiline user-message preview (display.user_message_preview in config.yaml)
        _ump = CLI_CONFIG["display"].get("user_message_preview", {})
        if not isinstance(_ump, dict):
            _ump = {}
        try:
            _ump_first_lines = int(_ump.get("first_lines", 2))
        except (TypeError, ValueError):
            _ump_first_lines = 2
        try:
            _ump_last_lines = int(_ump.get("last_lines", 2))
        except (TypeError, ValueError):
            _ump_last_lines = 2
        self.user_message_preview_first_lines = max(1, _ump_first_lines)
        self.user_message_preview_last_lines = max(0, _ump_last_lines)

        # Streaming display state
        self._stream_buf = ""        # Partial line buffer for line-buffered rendering
        self._stream_started = False  # True once first delta arrives
        self._stream_box_opened = False  # True once the response box header is printed
        self._reasoning_preview_buf = ""  # Coalesce tiny reasoning chunks for [thinking] output
        # Table-row buffer.  When a streamed line looks like it could be
        # part of a markdown table, hold it here until the block ends so
        # we can re-pad with wcwidth-aware widths.  Empty by default;
        # populated only while `_in_stream_table` is True.
        self._stream_table_buf: list[str] = []
        self._in_stream_table = False
        self._pending_edit_snapshots = {}
        self._last_input_mode_recovery = 0.0
        self._input_mode_recovery_notice_shown = False
        
        # Configuration - priority: CLI args > env vars > config file
        # Model comes from: CLI arg or config.yaml (single source of truth).
        # LLM_MODEL/OPENAI_MODEL env vars are NOT checked — config.yaml is
        # authoritative.  This avoids conflicts in multi-agent setups where
        # env vars would stomp each other.
        _model_config = CLI_CONFIG.get("model", {})
        _config_model = (_model_config.get("default") or _model_config.get("model") or "") if isinstance(_model_config, dict) else (_model_config or "")
        _DEFAULT_CONFIG_MODEL = ""
        self.model = model or _config_model or _DEFAULT_CONFIG_MODEL
        # Read max_tokens from config (env var override: OPENCODON_MAX_TOKENS)
        _env_mt = os.environ.get("OPENCODON_MAX_TOKENS")
        if _env_mt:
            try:
                self.max_tokens = int(_env_mt)
            except (ValueError, TypeError):
                self.max_tokens = None
        elif isinstance(_model_config, dict):
            _mt = _model_config.get("max_tokens")
            self.max_tokens = _mt if isinstance(_mt, int) else None
        else:
            self.max_tokens = None
        # Auto-detect model from local server if still on default
        if self.model == _DEFAULT_CONFIG_MODEL:
            _base_url = (_model_config.get("base_url") or "") if isinstance(_model_config, dict) else ""
            if "localhost" in _base_url or "127.0.0.1" in _base_url:
                from opencodon.core.providers.runtime_provider import _auto_detect_local_model
                _detected = _auto_detect_local_model(_base_url)
                if _detected:
                    self.model = _detected
        # Track whether model was explicitly chosen by the user or fell back
        # to the global default.  Provider-specific normalisation may override
        # the default silently but should warn when overriding an explicit choice.
        # A config model that matches the global fallback is NOT considered an
        # explicit choice — the user just never changed it.  But a config model
        # like "gpt-5.3-codex" IS explicit and must be preserved.
        self._model_is_default = not model and (
            not _config_model or _config_model == _DEFAULT_CONFIG_MODEL
        )

        self._explicit_api_key = api_key
        self._explicit_base_url = base_url

        # Provider selection is resolved lazily at use-time via _ensure_runtime_credentials().
        self.requested_provider = (
            provider
            or CLI_CONFIG["model"].get("provider")
            or os.getenv("OPENCODON_INFERENCE_PROVIDER")
            or "auto"
        )
        self._provider_source: Optional[str] = None
        self.provider = self.requested_provider
        self.api_mode = "chat_completions"
        self.acp_command: Optional[str] = None
        self.acp_args: list[str] = []
        self.base_url = (
            base_url
            or CLI_CONFIG["model"].get("base_url", "")
            or os.getenv("OPENROUTER_BASE_URL", "")
        ) or None
        # Match key to resolved base_url: OpenRouter URL → prefer OPENROUTER_API_KEY,
        # custom endpoint → prefer OPENAI_API_KEY (issue #560).
        # Note: _ensure_runtime_credentials() re-resolves this before first use.
        if self.base_url and base_url_host_matches(self.base_url, "openrouter.ai"):
            self.api_key = api_key or os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENAI_API_KEY")
        else:
            self.api_key = api_key or os.getenv("OPENAI_API_KEY") or os.getenv("OPENROUTER_API_KEY")
        # Max turns priority: CLI arg > config file > env var > default
        if max_turns is not None:  # CLI arg was explicitly set
            self.max_turns = max_turns
        elif CLI_CONFIG["agent"].get("max_turns"):
            self.max_turns = CLI_CONFIG["agent"]["max_turns"]
        elif CLI_CONFIG.get("max_turns"):  # Backwards compat: root-level max_turns
            self.max_turns = CLI_CONFIG["max_turns"]
        elif os.getenv("OPENCODON_MAX_ITERATIONS"):
            try:
                self.max_turns = int(os.getenv("OPENCODON_MAX_ITERATIONS", ""))
            except (TypeError, ValueError):
                self.max_turns = 90
        else:
            self.max_turns = 90
        
        # Parse and validate toolsets
        self.enabled_toolsets = toolsets
        self.disabled_toolsets = CLI_CONFIG["agent"].get("disabled_toolsets") or []

        if toolsets and "all" not in toolsets and "*" not in toolsets:
            # Validate each toolset — MCP server names are resolved via
            # live registry aliases (registered during discover_mcp_tools),
            # but discovery hasn't run yet at this point, so exclude them.
            mcp_names = set((CLI_CONFIG.get("mcp_servers") or {}).keys())
            invalid = [t for t in toolsets if not validate_toolset(t) and t not in mcp_names]
            if invalid:
                self._console_print(f"[bold red]Warning: Unknown toolsets: {', '.join(invalid)}[/]")
        
        # Filesystem checkpoints: CLI flag > config
        cp_cfg = CLI_CONFIG.get("checkpoints", {})
        if isinstance(cp_cfg, bool):
            cp_cfg = {"enabled": cp_cfg}
        self.checkpoints_enabled = checkpoints or cp_cfg.get("enabled", False)
        self.checkpoint_max_snapshots = cp_cfg.get("max_snapshots", 20)
        self.checkpoint_max_total_size_mb = cp_cfg.get("max_total_size_mb", 500)
        self.checkpoint_max_file_size_mb = cp_cfg.get("max_file_size_mb", 10)
        self.pass_session_id = pass_session_id
        # --ignore-rules: honor either the constructor flag or the env var set
        # by `opencodon chat --ignore-rules` in opencodon_cli/main.py. When true we
        # pass skip_context_files=True and skip_memory=True to AIAgent so
        # AGENTS.md/SOUL.md/.cursorrules and persistent memory are not loaded.
        self.ignore_rules = ignore_rules or os.environ.get("OPENCODON_IGNORE_RULES") == "1"
        
        # Ephemeral system prompt: env var takes precedence, then config
        self.system_prompt = (
            os.getenv("OPENCODON_EPHEMERAL_SYSTEM_PROMPT", "")
            or CLI_CONFIG["agent"].get("system_prompt", "")
        )
        self.personalities = CLI_CONFIG["agent"].get("personalities", {})
        
        # Ephemeral prefill messages (few-shot priming, never persisted)
        self.prefill_messages = _load_prefill_messages(
            _resolve_prefill_messages_file(CLI_CONFIG)
        )
        
        # Reasoning config (OpenRouter reasoning effort level)
        # Per-model override > global reasoning_effort — resolved through the
        # shared chokepoint in opencodon_constants (Closes #21256).
        from opencodon_constants import resolve_reasoning_config
        self.reasoning_config = resolve_reasoning_config(CLI_CONFIG, self.model)
        self.service_tier = _parse_service_tier_config(
            CLI_CONFIG["agent"].get("service_tier", "")
        )
        
        # OpenRouter provider routing preferences
        pr = CLI_CONFIG.get("provider_routing", {}) or {}
        self._provider_sort = pr.get("sort")
        self._providers_only = pr.get("only")
        self._providers_ignore = pr.get("ignore")
        self._providers_order = pr.get("order")
        self._provider_require_params = pr.get("require_parameters", False)
        self._provider_data_collection = pr.get("data_collection")

        # OpenRouter Pareto Code router knob — coding-score floor (0.0-1.0).
        # Only applied when model.model == "openrouter/pareto-code".
        # Empty string / None / out-of-range = unset (let OR pick strongest coder).
        _or_cfg = CLI_CONFIG.get("openrouter", {}) or {}
        _raw_score = _or_cfg.get("min_coding_score")
        self._openrouter_min_coding_score: Optional[float] = None
        if _raw_score not in {None, ""}:
            try:
                _f = float(_raw_score)
                if 0.0 <= _f <= 1.0:
                    self._openrouter_min_coding_score = _f
            except (TypeError, ValueError):
                pass
        
        # Fallback provider chain — tried in order when primary fails after retries.
        # Merge new ``fallback_providers`` entries with any legacy
        # ``fallback_model`` entries so old configs still participate.
        self._fallback_model = get_fallback_chain(CLI_CONFIG)

        # Signature of the currently-initialised agent's runtime.  Used to
        # rebuild the agent when provider / model / base_url changes across
        # turns (e.g. after /model or credential rotation).
        self._active_agent_route_signature = None

        # Agent will be initialized on first use
        self.agent: Optional[Any] = None
        self._tool_callbacks_installed = False
        self._tirith_security_checked = False
        self._app = None  # prompt_toolkit Application (set in run())
        
        # Conversation state
        self.conversation_history: List[Dict[str, Any]] = []
        self.session_start = datetime.now()
        self._resumed = False
        # Per-prompt elapsed timer — started at the beginning of each chat turn,
        # frozen when the agent thread completes, displayed in the status bar.
        self._prompt_start_time: Optional[float] = None  # time.time() when turn started
        self._prompt_duration: float = 0.0  # frozen duration of last completed turn
        self._last_turn_finished_at: Optional[float] = None  # time.time() when the last agent loop finished
        # Initialize SQLite session store early so /title works before first message
        self._session_db = None
        self._session_db_unavailable = False
        try:
            from opencodon.state import SessionDB
            self._session_db = SessionDB()
        except Exception as e:
            # #41386: a failed session store means the transcript is NOT
            # persisted to state.db — the live chat looks healthy but resume
            # later shows a truncated/empty session. A buried log line is not
            # enough; surface it prominently so the user knows persistence is
            # off for this run and can fix the store before relying on resume.
            self._session_db_unavailable = True
            logger.warning("Failed to initialize SessionDB — session will NOT be indexed for search: %s", e)
            try:
                # Console is imported at module scope; do NOT re-import it here.
                # A function-local `import` would make `Console` a local name for
                # the whole __init__ body and break the earlier `self.console =
                # Console()` with UnboundLocalError.
                Console(stderr=True).print(
                    "[bold yellow]⚠ Session store unavailable[/bold yellow] — "
                    "this conversation will [bold]NOT be saved[/bold] to disk and "
                    "cannot be resumed later. Searching past sessions is also disabled.\n"
                    f"  Reason: {e}\n"
                    "  Fix the state.db store (e.g. `opencodon update` to rebuild the venv) to restore persistence."
                )
            except Exception:
                # Never let the warning path itself break startup.
                print(
                    "WARNING: Session store unavailable — this conversation will NOT be "
                    f"saved to disk and cannot be resumed later. Reason: {e}"
                )

        # Opportunistic state.db maintenance — runs at most once per
        # min_interval_hours, tracked via state_meta in state.db itself so
        # it's shared across all opencodon processes for this OPENCODON_HOME.
        # Never blocks startup on failure.
        _run_state_db_auto_maintenance(self._session_db)

        # Opportunistic shadow-repo cleanup — deletes orphan/stale
        # checkpoint repos under ~/.opencodon/checkpoints/.  Opt-in via
        # checkpoints.auto_prune, idempotent via .last_prune marker.
        _run_checkpoint_auto_maintenance()

        # Deferred title: stored in memory until the session is created in the DB
        self._pending_title: Optional[str] = None
        
        # Session ID: reuse existing one when resuming, otherwise generate fresh
        if resume:
            self.session_id = resume
            self._resumed = True
        else:
            timestamp_str = self.session_start.strftime("%Y%m%d_%H%M%S")
            short_uuid = uuid.uuid4().hex[:6]
            self.session_id = f"{timestamp_str}_{short_uuid}"
        
        # History file for persistent input recall across sessions
        self._history_file = _opencodon_home / ".hermes_history"
        self._last_invalidate: float = 0.0  # throttle UI repaints
        self._app = None

        # State shared by interactive run() and single-query chat mode.
        # These must exist before any direct chat() call because single-query
        # mode does not go through run().
        self._agent_running = False
        self._pending_input = queue.Queue()
        self._interrupt_queue = queue.Queue()
        # Tracks whether the turn that just finished was interrupted via
        # Ctrl+C. Consumed by _maybe_continue_goal_after_turn so /goal loops
        # don't auto-queue another continuation on top of a user-cancelled
        # turn (which would make Ctrl+C feel like it did nothing).
        self._last_turn_interrupted = False
        self._should_exit = False
        # /exit --delete: when True, the current session's SQLite history and
        # on-disk transcripts are deleted during shutdown. Set by
        # process_command() when the user runs /exit --delete or /quit --delete.
        # Ported from google-gemini/gemini-cli#19332.
        self._delete_session_on_exit = False
        # /update: when set, run() executes relaunch() after prompt_toolkit
        # has fully exited and cleaned up terminal modes.  Set by
        # _handle_update_command() so the relaunch happens on the main thread,
        # not the background process_loop thread.
        self._pending_relaunch: list[str] | None = None
        self._last_ctrl_c_time = 0
        self._clarify_state = None
        self._clarify_freetext = False
        self._clarify_deadline = 0
        self._sudo_state = None
        self._sudo_deadline = 0
        self._modal_input_snapshot = None
        self._approval_state = None
        self._approval_deadline = 0
        self._approval_lock = threading.Lock()
        self._slash_confirm_state = None
        self._slash_confirm_deadline = 0
        self._model_picker_state = None
        # Armed when a bare `/resume` prints the recent-sessions list so the
        # very next bare numeric input (e.g. `3`) resolves to that session.
        # Holds the exact list used for index resolution; one-shot (cleared on
        # the next submitted input, whether it's the selection or anything
        # else). See #34584.
        self._pending_resume_sessions = None
        # One-shot agent seed set by a slash handler (e.g. /blueprint <name>)
        # that wants its output run as the next agent turn. Consumed and cleared
        # by the interactive loop immediately after process_command() returns.
        self._pending_agent_seed = None
        self._secret_state = None
        self._secret_deadline = 0
        self._spinner_text: str = ""  # thinking spinner text for TUI
        self._tool_start_time: float = 0.0  # monotonic timestamp when current tool started (for live elapsed)
        self._pending_tool_info: dict = {}  # function_name -> list of (preview, args) for stacked scrollback
        self._last_scrollback_tool: str = ""  # last tool name printed to scrollback (for "new" dedup)
        self._command_running = False
        self._command_status = ""
        self._attached_images: list[Path] = []
        self._image_counter = 0
        self.preloaded_skills: list[str] = []
        self._startup_skills_line_shown = False
        self._active_session_lease = None

        # Voice mode state (also reinitialized inside run() for interactive TUI).
        self._voice_lock = threading.Lock()
        self._voice_mode = False
        self._voice_tts = False
        self._voice_recorder = None
        self._voice_recording = False
        self._voice_processing = False
        self._voice_continuous = False
        self._voice_tts_done = threading.Event()
        self._voice_tts_done.set()
        self._voice_tts_stop = None  # active streaming pipeline's stop event
        self._voice_barge_capture = threading.Event()  # barge monitor is capturing the interruption

        # Status bar visibility (toggled via /statusbar)
        self._status_bar_visible = True
        # Battery read-out in the status bar (toggled via /battery, off by
        # default). Persisted to display.battery so it survives restarts.
        self._battery_visible = bool(CLI_CONFIG["display"].get("battery", False))
        # When True, the input separator rules and the dynamic status bar are
        # hidden until the next user input. Set by _recover_after_resize() so a
        # SIGWINCH cannot stamp a freshly-drawn status bar on top of one that
        # the terminal just reflowed into scrollback — the cause of duplicated
        # bars / "blank line flooding" reports (#19280, #22976).
        self._status_bar_suppressed_after_resize = False
        self._resize_recovery_lock = threading.Lock()
        self._resize_recovery_timer = None
        self._resize_recovery_pending = False
        # Debounced timer that clears the post-resize suppression once the
        # terminal reflow settles, so the status bar returns during idle
        # without waiting for the next submitted input.
        self._status_bar_unsuppress_timer = None
        # Last terminal width seen by the resize handler. Used to distinguish a
        # width change (column reflow → possible ghost chrome, needs a viewport
        # clear) from a rows-only change (no reflow). None until the first
        # resize fires.
        self._last_resize_width = None

        # Background task tracking: {task_id: threading.Thread}
        self._background_tasks: Dict[str, threading.Thread] = {}
        self._background_task_counter = 0

    def _claim_active_session(self, surface: str = "cli", *, stderr: bool = False) -> bool:
        """Claim a global active-session slot for this CLI process."""
        if self._active_session_lease is not None:
            return True
        try:
            from opencodon.frontends.cli.active_sessions import try_acquire_active_session

            lease, message = try_acquire_active_session(
                session_id=self.session_id,
                surface=surface,
                config=self.config,
            )
        except Exception as exc:
            logger.warning("Failed to claim active session slot: %s", exc)
            return True
        if message:
            if stderr:
                print(message, file=sys.stderr)
            else:
                self._console_print(f"[bold red]{message}[/]")
            return False
        self._active_session_lease = lease
        try:
            atexit.register(self._release_active_session)
        except Exception:
            pass
        return True

    def _release_active_session(self) -> None:
        lease = getattr(self, "_active_session_lease", None)
        if lease is None:
            return
        try:
            lease.release()
        except Exception:
            logger.debug("Failed to release active session slot", exc_info=True)
        finally:
            self._active_session_lease = None




































    # ── Streaming display ────────────────────────────────────────────────










    def _emit_stream_text(self, text: str) -> None:
        """Emit filtered text to the streaming display."""
        if not text:
            return

        # When show_reasoning is on and reasoning is still rendering,
        # defer content until the reasoning box closes.  This ensures the
        # reasoning block always appears BEFORE the response in the terminal.
        if self.show_reasoning and getattr(self, "_reasoning_box_opened", False):
            self._deferred_content = getattr(self, "_deferred_content", "") + text
            return

        # Close the live reasoning box before opening the response box
        self._close_reasoning_box()

        # Open the response box header on the very first visible text
        if not self._stream_box_opened:
            # Strip leading whitespace/newlines before first visible content
            text = text.lstrip("\n")
            if not text:
                return
            self._stream_box_opened = True
            try:
                from opencodon.frontends.cli.skin_engine import get_active_skin
                _skin = get_active_skin()
                label = _skin.get_branding("response_label", "⚕ opencodon")
                _text_hex = _skin.get_color("banner_text", "#FFF8DC")
            except Exception:
                label = "⚕ opencodon"
                _text_hex = "#FFF8DC"
            # Build a true-color ANSI escape for the response text color
            # so streamed content matches the Rich Panel appearance.
            try:
                _r = int(_text_hex[1:3], 16)
                _g = int(_text_hex[3:5], 16)
                _b = int(_text_hex[5:7], 16)
                self._stream_text_ansi = f"\033[38;2;{_r};{_g};{_b}m"
            except (ValueError, IndexError):
                self._stream_text_ansi = ""
            if self.show_timestamps:
                label = f"{label} {datetime.now().strftime(getattr(self, 'timestamp_format', '%H:%M'))}"
            w = self._scrollback_box_width()
            fill = w - 2 - OpencodonCLI._status_bar_display_width(label)
            _cprint(f"\n{_ACCENT}╭─{label}{'─' * max(fill - 1, 0)}╮{_RST}")

        self._stream_buf += text

        # Emit complete lines, keep partial remainder in buffer
        _tc = getattr(self, "_stream_text_ansi", "")

        def _emit_one(printed_line: str) -> None:
            _cprint(f"{_STREAM_PAD}{_tc}{printed_line}{_RST}" if _tc else f"{_STREAM_PAD}{printed_line}")

        def _flush_table_buf() -> None:
            buf = self._stream_table_buf
            self._stream_table_buf = []
            self._in_stream_table = False
            if not buf:
                return
            # Strip cell-level markdown (`code`, **bold**, ~~strike~~) FIRST
            # so the realigner pads to the final visible cell width, not
            # the marker-decorated source width.  Otherwise a body row
            # like `` | Bold | `**bold**` | `` lands narrower than its
            # header column once the markers are removed.
            joined = "\n".join(buf)
            if self.final_response_markdown == "strip":
                joined = _strip_markdown_syntax(joined)
            block = realign_markdown_tables(joined, _terminal_width_for_streaming())
            for ln in block.split("\n"):
                _emit_one(ln)

        while "\n" in self._stream_buf:
            line, self._stream_buf = self._stream_buf.split("\n", 1)

            # Hold table-shaped lines in a side-buffer so we can re-pad
            # the whole block once it ends.  Streaming line-by-line, we
            # cannot re-align mid-table without reflowing already-printed
            # rows; the cost is that the user sees the table appear in a
            # single batch when the block closes instead of row-by-row.
            if self._in_stream_table:
                if looks_like_table_row(line) or is_table_divider(line):
                    self._stream_table_buf.append(line)
                    continue
                # Block ended — flush the realigned table, then fall
                # through to print the current (non-table) line.
                _flush_table_buf()
            elif looks_like_table_row(line):
                self._stream_table_buf.append(line)
                self._in_stream_table = True
                continue

            if self.final_response_markdown == "strip":
                line = _strip_markdown_syntax(line)
            _emit_one(line)

        # Force-flush long partial lines so a response that opens with a
        # long paragraph paints as tokens arrive instead of staying blank
        # until the first newline (TTFT perception fix — the reasoning box
        # has done this at 80 chars since day one; the response box never
        # did). Wrap at the terminal's visible width so we only ever emit
        # text that would have line-broken at that point anyway; the
        # remainder stays buffered as the logical line's continuation.
        # Table-shaped partials are exempt — they need the whole block for
        # realignment (see the table side-buffer above).
        if (
            self._stream_buf
            and not self._in_stream_table
            and not self._stream_buf.lstrip().startswith("|")
        ):
            wrap_w = max(40, _terminal_width_for_streaming())
            while len(self._stream_buf) >= wrap_w:
                cut = self._stream_buf.rfind(" ", 0, wrap_w)
                if cut <= 0:
                    cut = wrap_w  # single unbreakable run — hard wrap
                chunk, self._stream_buf = (
                    self._stream_buf[:cut],
                    self._stream_buf[cut:].lstrip(" "),
                )
                if self.final_response_markdown == "strip":
                    chunk = _strip_markdown_syntax(chunk)
                _emit_one(chunk)

































    

    


    


    

    


    








    
    


    














    def _apply_model_switch_result(
        self, result, persist_global: bool, custom_providers=None
    ) -> None:
        if not result.success:
            _cprint(f"  ✗ {result.error_message}")
            return

        if self.agent is not None:
            try:
                from opencodon.frontends.cli.context_switch_guard import merge_preflight_compression_warning

                # Prefer the fresh inventory list (same source as switch_model /
                # TUI); fall back to the agent-init snapshot.
                _cp = (
                    custom_providers
                    if custom_providers is not None
                    else getattr(self.agent, "_custom_providers", None)
                )
                merge_preflight_compression_warning(
                    result,
                    agent=self.agent,
                    messages=list(self.conversation_history or []),
                    custom_providers=_cp,
                    config_context_length=getattr(self.agent, "_config_context_length", None),
                )
            except Exception as exc:
                logger.debug("preflight-compression switch warning failed: %s", exc)

        old_model = self.model
        # Snapshot the CLI-level credential/runtime fields BEFORE mutating them
        # so a failed in-place agent swap can roll the whole CLI back to the old
        # working model.  Otherwise the broken credentials staged below leak into
        # the next turn's resolution even though the agent itself rolled back
        # (#50163).
        _cli_snapshot = {
            "model": self.model,
            "provider": self.provider,
            "requested_provider": self.requested_provider,
            "_explicit_api_key": getattr(self, "_explicit_api_key", None),
            "_explicit_base_url": getattr(self, "_explicit_base_url", None),
            "api_key": self.api_key,
            "base_url": self.base_url,
            "api_mode": self.api_mode,
        }
        self.model = result.new_model
        self.provider = result.target_provider
        self.requested_provider = result.target_provider
        # Always overwrite explicit overrides so stale credentials from the
        # previous provider (e.g. Ollama api_key/base_url) don't leak into
        # the new provider's credential resolution on the next turn.
        self._explicit_api_key = result.api_key
        self._explicit_base_url = result.base_url
        if result.api_key:
            self.api_key = result.api_key
        if result.base_url:
            self.base_url = result.base_url
        if result.api_mode:
            self.api_mode = result.api_mode

        if self.agent is not None:
            try:
                self.agent.switch_model(
                    new_model=result.new_model,
                    new_provider=result.target_provider,
                    api_key=result.api_key,
                    base_url=result.base_url,
                    api_mode=result.api_mode,
                )
            except Exception as exc:
                # The agent rolled itself back to the old working model/client.
                # Roll the CLI's own staged fields back too and abort the rest
                # of the commit (note + success print) so a failed switch is a
                # no-op rather than a dead session (#50163).
                for _k, _v in _cli_snapshot.items():
                    setattr(self, _k, _v)
                _cprint(
                    f"  ⚠ Model switch to {result.new_model} failed ({exc}); "
                    f"staying on {old_model}."
                )
                return

        from opencodon.frontends.cli.model_switch import format_model_for_display
        _display_old = format_model_for_display(old_model)
        _display_new = format_model_for_display(result.new_model)

        self._pending_model_switch_note = (
            f"[Note: model was just switched from {_display_old} to {_display_new} "
            f"via {result.provider_label or result.target_provider}. "
            f"Adjust your self-identification accordingly.]"
        )

        provider_label = result.provider_label or result.target_provider
        _cprint(f"  ✓ Model switched: {_display_new}")
        _cprint(f"    Provider: {provider_label}")

        # Context: always resolve via the provider-aware chain so Codex OAuth,
        # (e.g. gpt-5.5 is 1.05M on openai but 272K on Codex OAuth).
        mi = result.model_info
        try:
            from opencodon.frontends.cli.model_switch import resolve_display_context_length
            ctx = resolve_display_context_length(
                result.new_model,
                result.target_provider,
                base_url=result.base_url or self.base_url or "",
                api_key=result.api_key or self.api_key or "",
                model_info=mi,
                config_context_length=getattr(self.agent, "_config_context_length", None) if self.agent else None,
                custom_providers=getattr(self.agent, "_custom_providers", None) if self.agent else None,
            )
            if ctx:
                _cprint(f"    Context: {ctx:,} tokens")
        except Exception:
            pass
        if mi:
            if mi.max_output:
                _cprint(f"    Max output: {mi.max_output:,} tokens")
            _cprint(f"    Capabilities: {mi.format_capabilities()}")

        cache_enabled = (
            (base_url_host_matches(result.base_url or "", "openrouter.ai") and "claude" in result.new_model.lower())
            or result.api_mode == "anthropic_messages"
        )
        if cache_enabled:
            _cprint("    Prompt caching: enabled")
        if result.warning_message:
            _cprint(f"    ⚠ {result.warning_message}")
        if persist_global:
            OpencodonCLI._clear_persisted_context_for_model_switch(self, result)
            save_config_value("model.default", result.new_model)
            save_config_value("model.provider", result.target_provider)
            # base_url/api_mode were previously never persisted here, so a
            # global switch left the OLD provider's endpoint/wire-protocol in
            # config.yaml. result.base_url/api_mode are always freshly
            # resolved for the target provider (see model_switch.py), so sync
            # them every time; None clears a value the new provider doesn't
            # need (#25106).
            save_config_value("model.base_url", result.base_url or None)
            save_config_value("model.api_mode", result.api_mode or None)
            _cprint("    Saved to config.yaml (--global)")
        else:
            _cprint("    (session only — add --global to persist)")


    def _handle_model_switch(self, cmd_original: str):
        """Handle /model command — switch model.

        Supports:
          /model                              — show current model + usage hints
          /model <name>                       — switch model (this session only)
          /model <name> --once                — switch for the next turn only
          /model <name> --session             — switch for this session only (explicit)
          /model <name> --global              — switch and persist to config.yaml
          /model <name> --provider <provider> — switch provider + model
          /model --provider <provider>        — switch to provider, auto-detect model

        Persistence defaults to off (``model.persist_switch_by_default`` in
        config.yaml, default False — switches are session-scoped). Use
        ``--global`` to persist, or ``--once`` for the next turn only.
        """
        from opencodon.frontends.cli.model_switch import (
            switch_model,
            parse_model_flags_detailed,
            resolve_persist_behavior,
        )
        from opencodon.core.providers import get_label

        # Parse args from the original command
        parts = cmd_original.split(None, 1)  # split off '/model'
        raw_args = parts[1].strip() if len(parts) > 1 else ""

        # Parse --provider, --global, --session, --once, and --refresh flags
        parsed_flags = parse_model_flags_detailed(raw_args)
        model_input = parsed_flags.model_input
        explicit_provider = parsed_flags.explicit_provider
        is_global_flag = parsed_flags.is_global
        force_refresh = parsed_flags.force_refresh
        is_session = parsed_flags.is_session
        one_turn = parsed_flags.is_once
        if is_global_flag and one_turn:
            _cprint("  ✗ /model --once cannot be combined with --global")
            return
        if one_turn and not model_input and not explicit_provider:
            _cprint("  ✗ /model --once requires a model or provider.")
            return
        # Resolve the effective persistence once: --global forces persist,
        # --session/--once force session-scope, otherwise defer to
        # model.persist_switch_by_default (defaults to False so /model is
        # session-scoped unless the user opts in).
        persist_global = resolve_persist_behavior(
            is_global_flag, is_session, is_once=one_turn,
            explicit_provider=explicit_provider,
        )

        # --refresh: wipe the on-disk picker cache before building the
        # provider list. Forces a live re-fetch of every authed provider's
        # /v1/models endpoint on this open.
        if force_refresh:
            try:
                from opencodon.core.providers.models import clear_provider_models_cache
                clear_provider_models_cache()
                _cprint("  Cleared model picker cache. Refreshing...")
            except Exception:
                pass

        # Single inventory context — replaces the inline config-slice the
        # dashboard / TUI used to duplicate. Overlay live session state
        # via with_overrides (truthy-only) so empty self.* attrs don't
        # clobber disk config.
        from opencodon.frontends.cli.inventory import build_models_payload, load_picker_context

        try:
            ctx = load_picker_context().with_overrides(
                current_provider=self.provider or "",
                current_model=self.model or "",
                current_base_url=self.base_url or "",
            )
        except Exception:
            ctx = None

        # switch_model() + _open_model_picker still need the raw provider
        # dicts; ConfigContext is the canonical source for both.
        user_provs = ctx.user_providers if ctx is not None else None
        custom_provs = ctx.custom_providers if ctx is not None else None

        # No args at all: open prompt_toolkit-native picker modal
        if not model_input and not explicit_provider:
            model_display = self.model or "unknown"
            provider_display = get_label(self.provider) if self.provider else "unknown"

            try:
                if ctx is None:
                    raise RuntimeError("inventory context unavailable")
                providers = build_models_payload(
                    ctx,
                    probe_custom_providers=force_refresh,
                    probe_current_custom_provider=not force_refresh,
                )["providers"]
            except Exception:
                providers = []

            if not providers:
                _cprint("  No authenticated providers found.")
                _cprint("")
                _cprint("  /model <name>                        switch model (persists)")
                _cprint("  /model <name> --once                 switch for the next turn only")
                _cprint("  /model <name> --session              switch for this session only")
                _cprint("  /model --provider <slug>             switch provider")
                _cprint("  /model --refresh                     re-fetch live model lists")
                return

            self._open_model_picker(
                providers,
                model_display,
                provider_display,
                user_provs=user_provs,
                custom_provs=custom_provs,
            )
            return

        # Perform the switch
        result = switch_model(
            raw_input=model_input,
            current_provider=self.provider or "",
            current_model=self.model or "",
            current_base_url=self.base_url or "",
            current_api_key=self.api_key or "",
            is_global=persist_global,
            explicit_provider=explicit_provider,
            user_providers=user_provs,
            custom_providers=custom_provs,
        )

        if not result.success:
            _cprint(f"  ✗ {result.error_message}")
            return

        if self.agent is not None:
            try:
                from opencodon.frontends.cli.context_switch_guard import merge_preflight_compression_warning

                merge_preflight_compression_warning(
                    result,
                    agent=self.agent,
                    messages=list(self.conversation_history or []),
                    # Same fresh inventory list passed to switch_model above.
                    custom_providers=custom_provs
                    if custom_provs is not None
                    else getattr(self.agent, "_custom_providers", None),
                    config_context_length=getattr(self.agent, "_config_context_length", None),
                )
            except Exception as exc:
                logger.debug("preflight-compression switch warning failed: %s", exc)

        if not self._confirm_expensive_model_switch(result):
            _cprint("  Model switch cancelled.")
            return

        # Apply to CLI state.
        # Update requested_provider so _ensure_runtime_credentials() doesn't
        # overwrite the switch on the next turn (it re-resolves from this).
        old_model = self.model
        _one_turn_restore_snapshot = self._snapshot_model_runtime() if one_turn else None
        # Snapshot CLI-level fields before mutation so a failed in-place swap
        # rolls the whole CLI back to the old working model (#50163).
        _cli_snapshot = {
            "model": self.model,
            "provider": self.provider,
            "requested_provider": self.requested_provider,
            "_explicit_api_key": getattr(self, "_explicit_api_key", None),
            "_explicit_base_url": getattr(self, "_explicit_base_url", None),
            "api_key": self.api_key,
            "base_url": self.base_url,
            "api_mode": self.api_mode,
        }
        self.model = result.new_model
        self.provider = result.target_provider
        self.requested_provider = result.target_provider
        # Always overwrite explicit overrides so stale credentials from the
        # previous provider (e.g. Ollama api_key/base_url) don't leak into
        # the new provider's credential resolution on the next turn.
        self._explicit_api_key = result.api_key
        self._explicit_base_url = result.base_url
        if result.api_key:
            self.api_key = result.api_key
        if result.base_url:
            self.base_url = result.base_url
        if result.api_mode:
            self.api_mode = result.api_mode

        # Apply to running agent (in-place swap)
        if self.agent is not None:
            try:
                self.agent.switch_model(
                    new_model=result.new_model,
                    new_provider=result.target_provider,
                    api_key=result.api_key,
                    base_url=result.base_url,
                    api_mode=result.api_mode,
                )
            except Exception as exc:
                # Agent rolled itself back; roll the CLI back too and abort so a
                # failed switch is a no-op rather than a dead session (#50163).
                for _k, _v in _cli_snapshot.items():
                    setattr(self, _k, _v)
                _cprint(
                    f"  ⚠ Model switch to {result.new_model} failed ({exc}); "
                    f"staying on {old_model}."
                )
                return

        # Store a note to prepend to the next user message so the model
        # knows a switch occurred (avoids injecting system messages mid-history
        # which breaks providers and prompt caching).
        from opencodon.frontends.cli.model_switch import format_model_for_display
        _display_old = format_model_for_display(old_model)
        _display_new = format_model_for_display(result.new_model)

        self._pending_model_switch_note = (
            f"[Note: model was just switched from {_display_old} to {_display_new} "
            f"via {result.provider_label or result.target_provider}. "
            f"{'This override applies to the next turn only. ' if one_turn else ''}"
            f"Adjust your self-identification accordingly.]"
        )
        if one_turn:
            self._pending_one_turn_model_restore = _one_turn_restore_snapshot
        else:
            self._pending_one_turn_model_restore = None

        # Display confirmation with full metadata
        provider_label = result.provider_label or result.target_provider
        _cprint(f"  ✓ Model switched: {_display_new}")
        _cprint(f"    Provider: {provider_label}")

        # Context: always resolve via the provider-aware chain so Codex OAuth,
        # (e.g. gpt-5.5 is 1.05M on openai but 272K on Codex OAuth).
        mi = result.model_info
        from opencodon.frontends.cli.model_switch import resolve_display_context_length
        ctx = resolve_display_context_length(
            result.new_model,
            result.target_provider,
            base_url=result.base_url or self.base_url or "",
            api_key=result.api_key or self.api_key or "",
            model_info=mi,
            config_context_length=getattr(self.agent, "_config_context_length", None) if self.agent else None,
            custom_providers=getattr(self.agent, "_custom_providers", None) if self.agent else None,
        )
        if ctx:
            _cprint(f"    Context: {ctx:,} tokens")
        if mi:
            if mi.max_output:
                _cprint(f"    Max output: {mi.max_output:,} tokens")
            _cprint(f"    Capabilities: {mi.format_capabilities()}")

        # Cache notice
        cache_enabled = (
            (base_url_host_matches(result.base_url or "", "openrouter.ai") and "claude" in result.new_model.lower())
            or result.api_mode == "anthropic_messages"
        )
        if cache_enabled:
            _cprint("    Prompt caching: enabled")

        # Warning from validation
        if result.warning_message:
            _cprint(f"    ⚠ {result.warning_message}")

        # Persistence
        if persist_global:
            OpencodonCLI._clear_persisted_context_for_model_switch(self, result)
            save_config_value("model.default", result.new_model)
            save_config_value("model.provider", result.target_provider)
            # See _apply_model_switch_result above for why base_url/api_mode
            # must be synced on every global switch (#25106).
            save_config_value("model.base_url", result.base_url or None)
            save_config_value("model.api_mode", result.api_mode or None)
            _cprint("    Saved to config.yaml")
        elif one_turn:
            _cprint("    (next turn only — restores after one response)")
        else:
            _cprint("    (session only — add --global to persist)")



    def _should_handle_steer_command_inline(self, text: str, has_images: bool = False) -> bool:
        """Return True when /steer should be dispatched immediately while the agent is running.

        /steer MUST bypass the normal _pending_input → process_loop path when
        the agent is active, because process_loop is blocked inside
        self.chat() for the duration of the run.  By the time the queued
        command is pulled from _pending_input, _agent_running has already
        flipped back to False, and process_command() takes the idle
        fallback — delivering the steer as a next-turn message instead of
        injecting it mid-run.  Dispatching inline on the UI thread calls
        agent.steer() directly, which is thread-safe (uses _pending_steer_lock).
        """
        if not text or has_images or not _looks_like_slash_command(text):
            return False
        if not getattr(self, "_agent_running", False):
            return False
        try:
            from opencodon.frontends.cli.commands import resolve_command
            base = text.split(None, 1)[0].lower().lstrip('/')
            cmd = resolve_command(base)
            return bool(cmd and cmd.name == "steer")
        except Exception:
            return False

    def _output_console(self):
        """Use prompt_toolkit-safe Rich rendering once the TUI is live."""
        if getattr(self, "_app", None):
            return ChatConsole()
        return self.console

    def _console_print(self, *args, **kwargs):
        """Print through the active command-safe console."""
        self._output_console().print(*args, **kwargs)

    @staticmethod
    def _resolve_personality_prompt(value) -> str:
        """Accept string or dict personality value; return system prompt string."""
        if isinstance(value, dict):
            parts = [value.get("system_prompt", "")]
            if value.get("tone"):
                parts.append(f'Tone: {value["tone"]}' )
            if value.get("style"):
                parts.append(f'Style: {value["style"]}' )
            return "\n".join(p for p in parts if p)
        return str(value)


    



    
    def process_command(self, command: str) -> bool:
        """
        Process a slash command.
        
        Args:
            command: The command string (starting with /)
            
        Returns:
            bool: True to continue, False to exit
        """
        # Lowercase only for dispatch matching; preserve original case for arguments
        cmd_lower = command.lower().strip()
        cmd_original = command.strip()

        # Resolve aliases via central registry so adding an alias is a one-line
        # change in opencodon_cli/commands.py instead of touching every dispatch site.
        from opencodon.frontends.cli.commands import resolve_command as _resolve_cmd
        _base_word = cmd_lower.split()[0].lstrip("/")
        _cmd_def = _resolve_cmd(_base_word)
        canonical = _cmd_def.name if _cmd_def else _base_word

        # A bare `/resume` prompt is one-shot: any command other than the
        # resume/sessions handlers (which manage the pending state themselves)
        # disarms it so a later number isn't swallowed as a stale selection.
        # See #34584.
        if canonical not in {"resume", "sessions"}:
            self._pending_resume_sessions = None

        if canonical in {"quit", "exit"}:
            # Parse --delete flag: /exit --delete also removes the current
            # session's transcripts + SQLite history. Ported from
            # google-gemini/gemini-cli#19332.
            return self._slash_exit(cmd_original, cmd_lower)
        elif canonical == "help":
            self.show_help()
        elif canonical == "profile":
            self._handle_profile_command()
        elif canonical == "tools":
            self._handle_tools_command(cmd_original)
        elif canonical == "toolsets":
            self.show_toolsets()
        elif canonical == "config":
            self.show_config()
        elif canonical == "redraw":
            # Manual recovery for terminal buffer drift from multiplexer
            # tab switches, subshell ``clear``, SSH window restores, etc.
            # See issue #8688 (cmux). Ctrl+L is bound to the same helper.
            self._force_full_redraw()
            _cprint(f"  {_DIM}✓ UI redrawn{_RST}")
        elif canonical == "clear":
            _r = self._slash_clear(cmd_original, cmd_lower)
            if _r is not _SLASH_FALLTHROUGH:
                return _r
        elif canonical == "history":
            self.show_history()
        elif canonical == "title":
            self._slash_title(cmd_original, cmd_lower)
        elif canonical == "handoff":
            if not self._handle_handoff_command(cmd_original):
                return False
        elif canonical == "new":
            # Strip inline-skip tokens (now/--yes/-y) before deriving the title
            # so "/new now My Session" yields title="My Session" instead of
            # title="now My Session". See _split_destructive_skip.
            _r = self._slash_new(cmd_original, cmd_lower)
            if _r is not _SLASH_FALLTHROUGH:
                return _r
        elif canonical == "resume":
            self._handle_resume_command(cmd_original)
        elif canonical == "sessions":
            self._handle_sessions_command(cmd_original)
        elif canonical == "model":
            self._handle_model_switch(cmd_original)

        elif canonical == "personality":
            # Use original case (handler lowercases the personality name itself)
            self._handle_personality_command(cmd_original)
        elif canonical == "retry":
            retry_msg = self.retry_last()
            if retry_msg and hasattr(self, '_pending_input'):
                # Re-queue the message so process_loop sends it to the agent
                self._pending_input.put(retry_msg)
        elif canonical == "prompt":
            self._handle_prompt_compose_command(cmd_original)
        elif canonical == "undo":
            # Parse optional turn count: "/undo" → 1, "/undo 3" → 3.
            _r = self._slash_undo(cmd_original, cmd_lower)
            if _r is not _SLASH_FALLTHROUGH:
                return _r
        elif canonical == "branch":
            self._handle_branch_command(cmd_original)
        elif canonical == "save":
            self.save_conversation()
        elif canonical == "cron":
            self._handle_cron_command(cmd_original)
        elif canonical == "suggestions":
            self._handle_suggestions_command(cmd_original)
        elif canonical == "blueprint":
            self._handle_blueprint_command(cmd_original)
        elif canonical == "curator":
            self._handle_curator_command(cmd_original)
        elif canonical == "skills":
            with self._busy_command(self._slow_command_status(cmd_original)):
                self._handle_skills_command(cmd_original)
        elif canonical == "learn":
            self._handle_learn_command(cmd_original)
        elif canonical == "memory":
            self._handle_memory_command(cmd_original)
        elif canonical == "platforms":
            self._show_gateway_status()
        elif canonical == "status":
            self._show_session_status()
        elif canonical == "statusbar":
            self._status_bar_visible = not self._status_bar_visible
            state = "visible" if self._status_bar_visible else "hidden"
            self._console_print(f"  Status bar {state}")
        elif canonical == "battery":
            self._handle_battery_command(cmd_original)
        elif canonical == "timestamps":
            self._handle_timestamps_command(cmd_original)
        elif canonical == "verbose":
            self._toggle_verbose()
        elif canonical == "footer":
            self._handle_footer_command(cmd_original)
        elif canonical == "yolo":
            self._toggle_yolo()
        elif canonical == "reasoning":
            self._handle_reasoning_command(cmd_original)
        elif canonical == "fast":
            self._handle_fast_command(cmd_original)
        elif canonical == "compress":
            self._manual_compress(cmd_original)
        elif canonical == "usage":
            self._handle_usage_command(cmd_original)
        elif canonical == "insights":
            self._show_insights(cmd_original)
        elif canonical == "copy":
            self._handle_copy_command(cmd_original)
        elif canonical == "debug":
            self._handle_debug_command(cmd_original)
        elif canonical == "update":
            if self._handle_update_command():
                return False
        elif canonical == "version":
            from opencodon.frontends.cli.main import _print_version_info

            _print_version_info(check_updates=True)
        elif canonical == "paste":
            self._handle_paste_command()
        elif canonical == "image":
            self._handle_image_command(cmd_original)
        elif canonical == "reload":
            from opencodon.config import reload_env
            count = reload_env()
            print(f"  Reloaded .env ({count} var(s) updated)")
        elif canonical == "reload-mcp":
            # Interactive reload: confirm first (unless the user has opted out).
            # The auto-reload path (file watcher) calls _reload_mcp directly
            # without this confirmation.
            self._confirm_and_reload_mcp(cmd_original)
        elif canonical == "reload-skills":
            with self._busy_command(self._slow_command_status(cmd_original)):
                self._reload_skills()
        elif canonical == "bundles":
            self._handle_bundles_command(cmd_original)
        elif canonical == "browser":
            self._handle_browser_command(cmd_original)
        elif canonical == "plugins":
            self._slash_plugins(cmd_original, cmd_lower)
        elif canonical == "rollback":
            self._handle_rollback_command(cmd_original)
        elif canonical == "snapshot":
            self._handle_snapshot_command(cmd_original)
        elif canonical == "stop":
            self._handle_stop_command()
        elif canonical == "agents":
            self._handle_agents_command()
        elif canonical == "background":
            self._handle_background_command(cmd_original)
        elif canonical == "queue":
            # Extract prompt after "/queue " or "/q "
            self._slash_queue(cmd_original, cmd_lower)
        elif canonical == "steer":
            # Inject a message after the next tool call without interrupting.
            # If the agent is actively running, push the text into the agent's
            # pending_steer slot — the drain hook in _execute_tool_calls_*
            # will append it to the next tool result's content. If no agent
            # is running, fall back to queue semantics (same as /queue).
            self._slash_steer(cmd_original, cmd_lower)
        elif canonical == "goal":
            self._handle_goal_command(cmd_original)
        elif canonical == "moa":
            # /moa is one-shot sugar only: run a single prompt through the
            # default MoA preset, then restore the prior model. To *switch* to a
            # MoA preset for the session, pick it from the model picker (MoA
            # presets surface as a virtual "Mixture of Agents" provider).
            _r = self._slash_moa(cmd_original, cmd_lower)
            if _r is not _SLASH_FALLTHROUGH:
                return _r
        elif canonical == "subgoal":
            self._handle_subgoal_command(cmd_original)
        elif canonical == "skin":
            self._handle_skin_command(cmd_original)
        elif canonical == "voice":
            self._handle_voice_command(cmd_original)
        elif canonical == "busy":
            self._handle_busy_command(cmd_original)
        else:
            # Check for user-defined quick commands (bypass agent loop, no LLM call)
            _r = self._slash_fallback(cmd_original, cmd_lower)
            if _r is not _SLASH_FALLTHROUGH:
                return _r
        
        return True

    def _slash_fallback(self, cmd_original: str, cmd_lower: str):
        """Extracted verbatim from the process_command dispatch. Returns _SLASH_FALLTHROUGH when the original branch fell through."""
        base_cmd = cmd_lower.split()[0]
        skill_commands = _ensure_skill_commands()
        skill_bundles = get_skill_bundles()
        quick_commands = self.config.get("quick_commands", {})
        if base_cmd.lstrip("/") in quick_commands:
            qcmd = quick_commands[base_cmd.lstrip("/")]
            if qcmd.get("type") == "exec":
                import subprocess
                exec_cmd = qcmd.get("command", "")
                if exec_cmd:
                    try:
                        # shell=True is intentional: quick_commands are user-defined
                        # shell snippets from config.yaml — not agent/LLM controlled.
                        # Sanitize env to prevent credential leakage —
                        # quick commands run in the CLI process which
                        # has all API keys in os.environ.
                        from opencodon.tools.environments.local import _sanitize_subprocess_env
                        sanitized_env = _sanitize_subprocess_env(os.environ.copy())
                        result = subprocess.run(
                            exec_cmd, shell=True, capture_output=True,
                            text=True, timeout=30, env=sanitized_env
                        )
                        output = result.stdout.strip() or result.stderr.strip()
                        if output:
                            from opencodon.core.redact import redact_sensitive_text
                            output = redact_sensitive_text(output)
                            self._console_print(_rich_text_from_ansi(output))
                        else:
                            self._console_print("[dim]Command returned no output[/]")
                    except subprocess.TimeoutExpired:
                        self._console_print("[bold red]Quick command timed out (30s)[/]")
                    except Exception as e:
                        self._console_print(f"[bold red]Quick command error: {e}[/]")
                else:
                    self._console_print(f"[bold red]Quick command '{base_cmd}' has no command defined[/]")
            elif qcmd.get("type") == "alias":
                target = qcmd.get("target", "").strip()
                if target:
                    target = target if target.startswith("/") else f"/{target}"
                    user_args = cmd_original[len(base_cmd):].strip()
                    aliased_command = f"{target} {user_args}".strip()
                    return self.process_command(aliased_command)
                else:
                    self._console_print(f"[bold red]Quick command '{base_cmd}' has no target defined[/]")
            else:
                self._console_print(f"[bold red]Quick command '{base_cmd}' has unsupported type (supported: 'exec', 'alias')[/]")
        # Check for plugin-registered slash commands
        elif base_cmd.lstrip("/") in _get_plugin_cmd_handler_names():
            from opencodon.plugins_runtime import (
                get_plugin_command_handler,
                resolve_plugin_command_result,
            )
            plugin_handler = get_plugin_command_handler(base_cmd.lstrip("/"))
            if plugin_handler:
                user_args = cmd_original[len(base_cmd):].strip()
                try:
                    result = resolve_plugin_command_result(
                        plugin_handler(user_args)
                    )
                    if result:
                        _cprint(str(result))
                except Exception as e:
                    _cprint(f"\033[1;31mPlugin command error: {e}{_RST}")
        # Skill bundles take precedence over individual skills — /<bundle>
        # loads multiple skills at once. Rescans cheaply when files change.
        elif base_cmd in skill_bundles:
            user_instruction = cmd_original[len(base_cmd):].strip()
            bundle_result = build_bundle_invocation_message(
                base_cmd, user_instruction, task_id=self.session_id
            )
            if bundle_result:
                msg, loaded_names, missing = bundle_result
                bundle_info = skill_bundles[base_cmd]
                print(
                    f"\n⚡ Loading bundle: {bundle_info['name']} "
                    f"({len(loaded_names)} skills)"
                )
                if missing:
                    ChatConsole().print(
                        f"[yellow]Skipped missing skills: {', '.join(missing)}[/]"
                    )
                if hasattr(self, '_pending_input'):
                    self._pending_input.put(msg)
            else:
                ChatConsole().print(
                    f"[bold red]Failed to load bundle for {base_cmd}[/]"
                )
        # Check for skill slash commands (/gif-search, /axolotl, etc.)
        elif base_cmd in skill_commands:
            rest = cmd_original[len(base_cmd):].strip()
            # Stacked slash-skill invocations: `/skill-a /skill-b do XYZ`
            # loads every leading skill (up to 5), not just the first.
            # Inspired by Claude Code v2.1.199.
            from opencodon.core.skills.skill_commands import (
                build_stacked_skill_invocation_message,
                split_stacked_skill_commands,
            )
            extra_keys, user_instruction = split_stacked_skill_commands(rest)
            if extra_keys:
                stacked_result = build_stacked_skill_invocation_message(
                    [base_cmd, *extra_keys],
                    user_instruction,
                    task_id=self.session_id,
                )
                if stacked_result:
                    msg, loaded_names, missing = stacked_result
                    print(
                        f"\n⚡ Loading {len(loaded_names)} stacked skills: "
                        f"{', '.join(loaded_names)}"
                    )
                    if missing:
                        ChatConsole().print(
                            f"[yellow]Skipped missing skills: {', '.join(missing)}[/]"
                        )
                    if hasattr(self, '_pending_input'):
                        self._pending_input.put(msg)
                else:
                    ChatConsole().print(
                        f"[bold red]Failed to load stacked skills for {base_cmd}[/]"
                    )
                return True
            user_instruction = rest
            msg = build_skill_invocation_message(
                base_cmd, user_instruction, task_id=self.session_id
            )
            if msg:
                skill_name = skill_commands[base_cmd]["name"]
                print(f"\n⚡ Loading skill: {skill_name}")
                if hasattr(self, '_pending_input'):
                    self._pending_input.put(msg)
            else:
                ChatConsole().print(f"[bold red]Failed to load skill for {base_cmd}[/]")
        else:
            # Prefix matching: if input uniquely identifies one command, execute it.
            # Matches against both built-in COMMANDS and installed skill commands so
            # that execution-time resolution agrees with tab-completion.
            from opencodon.frontends.cli.commands import COMMANDS
            typed_base = cmd_lower.split()[0]
            all_known = set(COMMANDS) | set(skill_commands) | set(skill_bundles)
            matches = [c for c in all_known if c.startswith(typed_base)]
            if len(matches) > 1:
                # Prefer an exact match (typed the full command name)
                exact = [c for c in matches if c == typed_base]
                if len(exact) == 1:
                    matches = exact
                else:
                    # Prefer the unique shortest match:
                    # /qui → /quit (5) wins over /quint-pipeline (15)
                    min_len = min(len(c) for c in matches)
                    shortest = [c for c in matches if len(c) == min_len]
                    if len(shortest) == 1:
                        matches = shortest
            if len(matches) == 1:
                # Expand the prefix to the full command name, preserving arguments.
                # Guard against redispatching the same token to avoid infinite
                # recursion when the expanded name still doesn't hit an exact branch
                # (e.g. /config with extra args that are not yet handled above).
                full_name = matches[0]
                if full_name == typed_base:
                    # Already an exact token — no expansion possible; fall through
                    _cprint(f"\033[1;31mUnknown command: {cmd_lower}{_RST}")
                    _cprint(f"{_DIM}{_ACCENT}Type /help for available commands{_RST}")
                else:
                    remainder = cmd_original.strip()[len(typed_base):]
                    full_cmd = full_name + remainder
                    return self.process_command(full_cmd)
            elif len(matches) > 1:
                _cprint(f"{_ACCENT}Ambiguous command: {cmd_lower}{_RST}")
                _cprint(f"{_DIM}Did you mean: {', '.join(sorted(matches))}?{_RST}")
            else:
                _cprint(f"\033[1;31mUnknown command: {cmd_lower}{_RST}")
                _cprint(f"{_DIM}{_ACCENT}Type /help for available commands{_RST}")
        return _SLASH_FALLTHROUGH

    def _slash_moa(self, cmd_original: str, cmd_lower: str):
        """Extracted verbatim from the process_command dispatch. Returns _SLASH_FALLTHROUGH when the original branch fell through."""
        from opencodon.config.moa_config import (
            moa_usage,
            normalize_moa_config,
        )

        parts = cmd_original.split(None, 1)
        payload = parts[1].strip() if len(parts) > 1 else ""
        if not payload:
            _cprint(f"  {moa_usage()}")
            return True
        moa_cfg = self.config.get("moa") if isinstance(self.config, dict) else {}
        normalized = normalize_moa_config(moa_cfg)
        preset = normalized["default_preset"]
        self._pending_moa_restore_model = {
            "requested_provider": getattr(self, "requested_provider", None),
            "provider": getattr(self, "provider", None),
            "model": getattr(self, "model", None),
            "api_key": getattr(self, "api_key", None),
            "base_url": getattr(self, "base_url", None),
            "api_mode": getattr(self, "api_mode", None),
        }
        self.requested_provider = "moa"
        self.provider = "moa"
        self.model = preset
        self.api_key = "moa-virtual-provider"
        self.base_url = "moa://local"
        self.api_mode = "chat_completions"
        self.agent = None
        self._pending_moa_disable_after_turn = True
        self._pending_agent_seed = payload
        _cprint(f"  MoA one-shot queued with preset {preset}; previous model will be restored after this turn.")
        return _SLASH_FALLTHROUGH

    def _slash_steer(self, cmd_original: str, cmd_lower: str):
        """Extracted verbatim from the process_command dispatch. Falls through (no early return in the original branch)."""
        parts = cmd_original.split(None, 1)
        payload = parts[1].strip() if len(parts) > 1 else ""
        if not payload:
            _cprint("  Usage: /steer <prompt>")
        elif self._agent_running and self.agent is not None and hasattr(self.agent, "steer"):
            try:
                accepted = self.agent.steer(payload)
            except Exception as exc:
                _cprint(f"  Steer failed: {exc}")
            else:
                if accepted:
                    _cprint(f"  ⏩ Steer queued — arrives after the next tool call: {payload[:80]}{'...' if len(payload) > 80 else ''}")
                else:
                    _cprint("  Steer rejected (empty payload).")
        else:
            # No active run — treat as a normal next-turn message.
            self._pending_input.put(payload)
            _cprint(f"  No agent running; queued as next turn: {payload[:80]}{'...' if len(payload) > 80 else ''}")

    def _slash_queue(self, cmd_original: str, cmd_lower: str):
        """Extracted verbatim from the process_command dispatch. Falls through (no early return in the original branch)."""
        parts = cmd_original.split(None, 1)
        payload = parts[1].strip() if len(parts) > 1 else ""
        if not payload:
            _cprint("  Usage: /queue <prompt>")
        else:
            self._pending_input.put(payload)
            if self._agent_running:
                _cprint(f"  Queued for the next turn: {payload[:80]}{'...' if len(payload) > 80 else ''}")
            else:
                _cprint(f"  Queued: {payload[:80]}{'...' if len(payload) > 80 else ''}")

    def _slash_plugins(self, cmd_original: str, cmd_lower: str):
        """Extracted verbatim from the process_command dispatch. Falls through (no early return in the original branch)."""
        try:
            # Discover from disk (bundled + user), matching `opencodon plugins
            # list` — so installed-but-not-enabled plugins are visible here
            # too. The plugin manager only knows about *loaded* plugins, so
            # using it alone made freshly-installed, not-yet-enabled plugins
            # look like "nothing installed".
            from opencodon.frontends.cli.plugins_cmd import (
                _discover_all_plugins,
                _get_disabled_set,
                _get_enabled_set,
                _plugin_status,
            )

            entries = _discover_all_plugins()
            enabled = _get_enabled_set()
            disabled = _get_disabled_set()

            # `/plugins` is a quick glance — default to user-installed
            # plugins (what the user actually added). Bundled provider/
            # platform plugins are summarized on one line; the full
            # catalog lives behind `opencodon plugins list`.
            user_entries = [e for e in entries if e[3] != "bundled"]
            bundled_count = len(entries) - len(user_entries)

            if not user_entries:
                print("No user plugins installed.")
                print("  Install one: opencodon plugins install owner/repo")
                print(f"  Or drop a plugin directory into {display_opencodon_home()}/plugins/")
                if bundled_count:
                    print(f"  ({bundled_count} bundled plugins available — see: opencodon plugins list)")
            else:
                # Loaded-plugin details (tools/hooks/commands counts, errors)
                # keyed by name, when available.
                loaded: dict = {}
                try:
                    from opencodon.plugins_runtime import get_plugin_manager
                    for p in get_plugin_manager().list_plugins():
                        loaded[p["name"]] = p
                except Exception:
                    loaded = {}

                print(f"User plugins ({len(user_entries)}):")
                for name, version, _desc, source, _dir, key in sorted(user_entries):
                    state = _plugin_status(name, enabled, disabled, key=key)
                    glyph = {"enabled": "✓", "disabled": "✗"}.get(state, "○")
                    ver = f" v{version}" if version else ""
                    info = loaded.get(name) or {}
                    bits = []
                    if info.get("tools"):
                        bits.append(f"{info['tools']} tools")
                    if info.get("hooks"):
                        bits.append(f"{info['hooks']} hooks")
                    if info.get("commands"):
                        bits.append(f"{info['commands']} commands")
                    detail = f" ({', '.join(bits)})" if bits else ""
                    label = "" if state == "enabled" else f" [{state}]"
                    error = f" — {info['error']}" if info.get("error") else ""
                    print(f"  {glyph} {name}{ver}{label}{detail}{error}")
                if bundled_count:
                    print(f"  (+{bundled_count} bundled — see: opencodon plugins list)")
                print("  Enable/disable: opencodon plugins enable/disable <name>")
        except Exception as e:
            print(f"Plugin system error: {e}")

    def _slash_undo(self, cmd_original: str, cmd_lower: str):
        """Extracted verbatim from the process_command dispatch. Returns _SLASH_FALLTHROUGH when the original branch fell through."""
        _undo_n = 1
        _undo_parts = cmd_original.split()
        if len(_undo_parts) > 1:
            try:
                _undo_n = int(_undo_parts[1])
            except ValueError:
                print(f"(._.) Invalid count {_undo_parts[1]!r} — use /undo or /undo N.")
                return
            if _undo_n < 1:
                _undo_n = 1
        _undo_desc = (
            "This removes the last user/assistant exchange from history."
            if _undo_n == 1
            else f"This removes the last {_undo_n} user turns from history."
        )
        if self._confirm_destructive_slash(
            "undo",
            _undo_desc,
            cmd_original=cmd_original,
        ) is None:
            return True  # confirmation cancelled — command handled, keep REPL alive
        self.undo_last(_undo_n)
        return _SLASH_FALLTHROUGH

    def _slash_new(self, cmd_original: str, cmd_lower: str):
        """Extracted verbatim from the process_command dispatch. Returns _SLASH_FALLTHROUGH when the original branch fell through."""
        _new_args, _ = self._split_destructive_skip(cmd_original)
        title = _new_args.strip() or None
        if self._confirm_destructive_slash(
            "new",
            "This starts a fresh session.\n"
            "The current conversation history will be discarded.",
            cmd_original=cmd_original,
        ) is None:
            return True  # confirmation cancelled — command handled, keep REPL alive
        self.new_session(title=title)
        return _SLASH_FALLTHROUGH

    def _slash_title(self, cmd_original: str, cmd_lower: str):
        """Extracted verbatim from the process_command dispatch. Falls through (no early return in the original branch)."""
        parts = cmd_original.split(maxsplit=1)
        if len(parts) > 1:
            raw_title = parts[1].strip()
            if raw_title:
                if self._session_db:
                    # Sanitize the title early so feedback matches what gets stored
                    try:
                        from opencodon.state import SessionDB
                        new_title = SessionDB.sanitize_title(raw_title)
                    except ValueError as e:
                        _cprint(f"  {e}")
                        new_title = None
                    if not new_title:
                        _cprint("  Title is empty after cleanup. Please use printable characters.")
                    elif self._session_db.get_session(self.session_id):
                        # Session exists in DB — set title directly
                        try:
                            if self._session_db.set_session_title(self.session_id, new_title):
                                _cprint(f"  Session title set: {new_title}")
                            else:
                                _cprint("  Session not found in database.")
                        except ValueError as e:
                            _cprint(f"  {e}")
                    else:
                        # Session not created yet — defer the title
                        # Check uniqueness proactively with the sanitized title
                        existing = self._session_db.get_session_by_title(new_title)
                        if existing:
                            _cprint(f"  Title '{new_title}' is already in use by session {existing['id']}")
                        else:
                            self._pending_title = new_title
                            _cprint(f"  Session title queued: {new_title} (will be saved on first message)")
                else:
                    from opencodon.state import format_session_db_unavailable
                    _cprint(f"  {format_session_db_unavailable()}")
            else:
                _cprint("  Usage: /title <your session title>")
        # Show current title and session ID if no argument given
        elif self._session_db:
            _cprint(f"  Session ID: {self.session_id}")
            session = self._session_db.get_session(self.session_id)
            if session and session.get("title"):
                _cprint(f"  Title: {session['title']}")
            elif self._pending_title:
                _cprint(f"  Title (pending): {self._pending_title}")
            else:
                _cprint("  No title set. Usage: /title <your session title>")
        else:
            from opencodon.state import format_session_db_unavailable
            _cprint(f"  {format_session_db_unavailable()}")

    def _slash_clear(self, cmd_original: str, cmd_lower: str):
        """Extracted verbatim from the process_command dispatch. Returns _SLASH_FALLTHROUGH when the original branch fell through."""
        if self._confirm_destructive_slash(
            "clear",
            "This clears the screen and starts a new session.\n"
            "The current conversation history will be discarded.",
            cmd_original=cmd_original,
        ) is None:
            return True  # confirmation cancelled — command handled, keep REPL alive
        self.new_session(silent=True)
        _clear_output_history()
        # Clear terminal screen.  Inside the TUI, Rich's console.clear()
        # goes through patch_stdout's StdoutProxy which swallows the
        # screen-clear escape sequences.  Use prompt_toolkit's output
        # object directly to actually clear the terminal.
        if self._app:
            out = self._app.output
            out.erase_screen()
            out.cursor_goto(0, 0)
            out.flush()
        else:
            self.console.clear()
        # Show fresh banner.  Inside the TUI we must route Rich output
        # through ChatConsole (which uses prompt_toolkit's native ANSI
        # renderer) instead of self.console (which writes raw to stdout
        # and gets mangled by patch_stdout).
        if self._app:
            cc = ChatConsole()
            term_w = shutil.get_terminal_size().columns
            if self.compact or term_w < 80:
                cc.print(_build_compact_banner())
            else:
                tools = get_tool_definitions(enabled_toolsets=self.enabled_toolsets, quiet_mode=True)
                cwd = os.getenv("TERMINAL_CWD", os.getcwd())
                ctx_len = None
                if hasattr(self, 'agent') and self.agent and hasattr(self.agent, 'context_compressor'):
                    ctx_len = self.agent.context_compressor.context_length
                build_welcome_banner(
                    console=cc,
                    model=self.model,
                    cwd=cwd,
                    tools=tools,
                    enabled_toolsets=self.enabled_toolsets,
                    session_id=self.session_id,
                    context_length=ctx_len,
                    provider=self.provider,
                )
            _cprint("  ✨ (◕‿◕)✨ Fresh start! Screen cleared and conversation reset.\n")
        else:
            self.show_banner()
            print("  ✨ (◕‿◕)✨ Fresh start! Screen cleared and conversation reset.\n")
        return _SLASH_FALLTHROUGH

    def _slash_exit(self, cmd_original: str, cmd_lower: str):
        """Extracted verbatim from the process_command dispatch. Always returns the REPL-continue flag."""
        _rest = cmd_original.split(None, 1)
        _args = (_rest[1] if len(_rest) > 1 else "").strip().lower()
        if _args in {"--delete", "-d"}:
            self._delete_session_on_exit = True
        elif _args:
            _cprint(f"  {_DIM}✗ Unknown argument: {_escape(_args)}. Use /exit --delete to also remove session history.{_RST}")
            return True
        return False

    




    # ────────────────────────────────────────────────────────────────
    # /goal — persistent cross-turn goals (Ralph-style loop)
    # ────────────────────────────────────────────────────────────────









    def _toggle_verbose(self):
        """Cycle tool progress mode: off → new → all → verbose → off.

        Tool-progress display (full args / results / think blocks at the
        ``verbose`` step) is INDEPENDENT of global DEBUG logging.  Cycling
        through here does not change ``self.verbose`` or the agent's
        ``verbose_logging`` / ``quiet_mode`` — those remain under the
        explicit ``-v``/``--verbose`` flag and the ``/verbose-logging``
        toggle.  See PR #6a1aa420e for the history that decoupled them.
        """
        cycle = ["off", "new", "all", "verbose"]
        try:
            idx = cycle.index(self.tool_progress_mode)
        except ValueError:
            idx = 2  # default to "all"
        self.tool_progress_mode = cycle[(idx + 1) % len(cycle)]

        if self.agent:
            self.agent.reasoning_callback = self._current_reasoning_callback()
            # Keep the live agent's tool_progress_mode in sync so the
            # tool_executor rendering path reflects the new mode this turn,
            # without waiting for an agent rebuild.
            self.agent.tool_progress_mode = self.tool_progress_mode

        # Use raw ANSI codes via _cprint so the output is routed through
        # prompt_toolkit's renderer.  self.console.print() with Rich markup
        # writes directly to stdout which patch_stdout's StdoutProxy mangles
        # into garbled sequences like '?[33mTool progress: NEW?[0m' (#2262).
        from opencodon.common.colors import Colors as _Colors
        labels = {
            "off": f"{_Colors.DIM}Tool progress: OFF{_Colors.RESET} — silent mode, just the final response.",
            "new": f"{_Colors.YELLOW}Tool progress: NEW{_Colors.RESET} — show each new tool (skip repeats).",
            "all": f"{_Colors.GREEN}Tool progress: ALL{_Colors.RESET} — show every tool call.",
            "verbose": f"{_Colors.BOLD}{_Colors.GREEN}Tool progress: VERBOSE{_Colors.RESET} — full args, results, and think blocks.",
        }
        _cprint(labels.get(self.tool_progress_mode, ""))

    def _transfer_session_yolo(self, old_session_id: str, new_session_id: str) -> None:
        """Move YOLO bypass state from an old session key to a new one.

        Called whenever ``self.session_id`` is reassigned mid-run — ``/branch``
        forks into a new session, and auto-compression rotates the agent's
        session id into a fresh continuation session. Without this transfer
        the user's ``/yolo ON`` toggle would silently revert on the very next
        turn (the same UX failure mode that motivated this entire fix), since
        ``_session_yolo`` is keyed by session id.

        Mirrors ``tui_gateway/server.py`` (~line 1297-1305) which performs the
        same transfer for the TUI's session-rename path. No-op when YOLO
        wasn't enabled or when the ids match.
        """
        if not old_session_id or not new_session_id or old_session_id == new_session_id:
            return
        try:
            from opencodon.tools.approval import (
                disable_session_yolo,
                enable_session_yolo,
                is_session_yolo_enabled,
            )
        except Exception:
            return
        if is_session_yolo_enabled(old_session_id):
            enable_session_yolo(new_session_id)
            disable_session_yolo(old_session_id)

    def _is_session_yolo_active(self) -> bool:
        """Whether YOLO bypass is currently enabled for this CLI session.

        Reads from ``tools.approval._session_yolo`` (the same set that
        ``enable_session_yolo`` / ``disable_session_yolo`` write to) so the
        status bar reflects the actual bypass state instead of a stale env
        var. Also honors the process-start ``--yolo`` flag, which freezes
        ``OPENCODON_YOLO_MODE`` into ``_YOLO_MODE_FROZEN`` before tool imports
        happen.
        """
        try:
            from opencodon.tools.approval import (
                _YOLO_MODE_FROZEN,
                is_session_yolo_enabled,
            )
        except Exception:
            return False
        if _YOLO_MODE_FROZEN:
            return True
        # Use ``getattr`` so test fixtures that build a CLI via ``__new__``
        # (skipping ``__init__``) don't trip an AttributeError here; the
        # status-bar builders swallow exceptions silently but lose every
        # field after the failure.
        session_key = getattr(self, "session_id", None) or "default"
        return is_session_yolo_enabled(session_key)

    def _toggle_yolo(self):
        """Toggle YOLO mode — skip all dangerous command approval prompts.

        Per-session toggle that mirrors the gateway and TUI ``/yolo`` handlers
        (see ``gateway/run.py:_handle_yolo_command`` and
        ``tui_gateway/server.py`` key=="yolo"). We deliberately do NOT mutate
        ``OPENCODON_YOLO_MODE`` here — that env var is read once at module import
        time into ``tools.approval._YOLO_MODE_FROZEN`` to keep prompt-injected
        skills from flipping the bypass mid-session, so setting it after CLI
        startup is a silent no-op. Routing through ``enable_session_yolo`` /
        ``disable_session_yolo`` gives the same auditable, per-session bypass
        the other surfaces have. ``run_conversation`` binds
        ``self.session_id`` as the active approval session key via
        ``set_current_session_key`` so the bypass takes effect on the very
        next dangerous command in this run.
        """
        from opencodon.common.colors import Colors as _Colors
        from opencodon.tools.approval import (
            disable_session_yolo,
            enable_session_yolo,
            is_session_yolo_enabled,
        )

        session_key = self.session_id or "default"
        if is_session_yolo_enabled(session_key):
            disable_session_yolo(session_key)
            _cprint(
                f"  ⚠ YOLO mode {_Colors.BOLD}{_Colors.RED}OFF{_Colors.RESET}"
                " — dangerous commands will require approval."
            )
        else:
            enable_session_yolo(session_key)
            _cprint(
                f"  ⚡ YOLO mode {_Colors.BOLD}{_Colors.GREEN}ON{_Colors.RESET}"
                " — all commands auto-approved. Use with caution."
            )








    def _handle_usage_command(self, cmd_original: str):
        """Dispatch `/usage [reset [--force]]`.

        Bare `/usage` keeps the classic display. `/usage reset` redeems one
        banked Codex rate-limit reset credit (guarded: refuses when limits
        aren't exhausted unless --force).
        """
        parts = cmd_original.split()
        args = [p.lower() for p in parts[1:]]
        if args and args[0] == "reset":
            self._usage_reset(force="--force" in args[1:])
            return
        if args:
            print(f"  Unknown /usage subcommand: {' '.join(parts[1:])}. Try /usage or /usage reset [--force].")
            return
        self._show_usage()

    def _usage_reset(self, force: bool = False):
        """`/usage reset [--force]` — redeem one banked Codex reset credit."""
        provider = (
            (getattr(self.agent, "provider", None) if self.agent else None)
            or getattr(self, "provider", None)
        )
        normalized = str(provider or "").strip().lower()
        if normalized != "openai-codex":
            print("  Banked usage resets are only available on the openai-codex provider.")
            print("  Switch with `/model` or `opencodon auth` first.")
            return
        base_url = (getattr(self.agent, "base_url", None) if self.agent else None) or getattr(self, "base_url", None)
        api_key = (getattr(self.agent, "api_key", None) if self.agent else None) or getattr(self, "api_key", None)

        from opencodon.core.providers.account_usage import redeem_codex_reset_credit

        print("  ⏳ Checking banked reset credits...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as _pool:
            try:
                result = _pool.submit(
                    redeem_codex_reset_credit,
                    base_url=base_url,
                    api_key=api_key,
                    force=force,
                ).result(timeout=45.0)
            except concurrent.futures.TimeoutError:
                print("  ❌ Timed out talking to the Codex backend — try again shortly.")
                return
        print(f"  {result.message}")

            # NOTE: We deliberately do NOT raise per-logger levels for
            # tools/run_agent/etc. in quiet mode. Setting logger.setLevel
            # above the file handler level filters records before they
            # reach handlers, so agent.log / errors.log lose visibility
            # into stream-retry events, credential rotations, etc.
            # Console quietness is enforced by opencodon_logging not
            # installing a console StreamHandler in non-verbose mode.


        # Do NOT join here — process_loop calls this from its idle branch, so a
        # blocking join would freeze input consumption for up to 30s (and a hung
        # MCP server could block far longer). The reload runs purely in the
        # background daemon thread, which reports its own progress/completion
        # status via print() inside _reload_mcp().

    # Inline-skip tokens that bypass the destructive-slash confirmation modal.
    # A general escape hatch for non-interactive use (scripting/automation) and
    # for the degraded path where the modal can't be marshaled onto the app loop
    # — lets users self-serve without flipping approvals.destructive_slash_confirm
    # in config. (Native Windows now drives the modal normally — see #33961.)
    _DESTRUCTIVE_SKIP_TOKENS = frozenset({"now", "--yes", "-y"})






    # ====================================================================
    # Tool-call generation indicator (shown during streaming)
    # ====================================================================


    # ====================================================================
    # Tool progress callback (audio cues for voice mode)
    # ====================================================================




    # ====================================================================
    # Voice mode methods
    # ====================================================================






























    def chat(self, message, images: list = None) -> Optional[str]:
        """
        Send a message to the agent and get a response.
        
        Handles streaming output, interrupt detection (user typing while agent
        is working), and re-queueing of interrupted messages.
        
        Uses a dedicated _interrupt_queue (separate from _pending_input) to avoid
        race conditions between the process_loop and interrupt monitoring. Messages
        typed while the agent is running go to _interrupt_queue; messages typed while
        idle go to _pending_input.
        
        Args:
            message: The user's message (str or multimodal content list)
            images: Optional list of Path objects for attached images
            
        Returns:
            The agent's response, or None on error
        """
        # Single-query and direct chat callers do not go through run(), so
        # register secure secret capture here as well.
        set_secret_capture_callback(self._secret_capture_callback)

        # Reset the per-turn interrupt flag. Any subsequent path that
        # discovers an interrupt (below, after run_conversation) will flip
        # this to True. Early returns (credential refresh failure, etc.)
        # leave it False, which is correct — those aren't user interrupts.
        self._last_turn_interrupted = False

        # Refresh provider credentials if needed (handles key rotation transparently)
        if not self._ensure_runtime_credentials():
            return None

        turn_route = self._resolve_turn_agent_config(message)
        if turn_route["signature"] != self._active_agent_route_signature:
            self.agent = None

        # Initialize agent if needed
        if self.agent is None:
            _cprint(f"{_DIM}Initializing agent...{_RST}")
        if not self._init_agent(
            model_override=turn_route["model"],
            runtime_override=turn_route["runtime"],
            request_overrides=turn_route.get("request_overrides"),
        ):
            return None
        agent = self.agent
        if agent is None:
            return None

        message = self._chat_route_images(message, images)

        _ctx_blocked, message = self._chat_expand_context_refs(message)
        if _ctx_blocked is not None:
            return _ctx_blocked

        # Sanitize surrogate characters that can arrive via clipboard paste from
        # rich-text editors (Google Docs, Word, etc.).  Lone surrogates are invalid
        # UTF-8 and crash JSON serialization in the OpenAI SDK.
        if isinstance(message, str):
            from opencodon.core.run_agent import _sanitize_surrogates
            message = _sanitize_surrogates(message)

        self._chat_stage_user_message(agent, message)

        ChatConsole().print(f"[{_accent_hex()}]{'─' * 40}[/]")
        print(flush=True)
        
        try:
            # Run the conversation with interrupt monitoring
            result = None

            # Reset streaming display state for this turn
            self._reset_stream_state()
            # Separate from _reset_stream_state because this must persist
            # across intermediate turn boundaries (tool-calling loops) — only
            # reset at the start of each user turn.
            self._reasoning_shown_this_turn = False

            # --- Streaming TTS setup ---
            # Any working TTS provider streams sentence-by-sentence as the agent
            # generates tokens: PCM-streaming providers (ElevenLabs, OpenAI) play
            # chunks as they arrive, everything else synthesizes per sentence.
            use_streaming_tts = False
            _streaming_box_opened = False
            text_queue = None
            tts_thread = None
            stream_callback = None
            stop_event = None

            if self._voice_tts:
                try:
                    from opencodon.tools.tts_tool import (
                        _import_sounddevice,
                        check_tts_requirements,
                        stream_tts_to_speaker,
                    )
                    _import_sounddevice()
                    use_streaming_tts = check_tts_requirements()
                except Exception:
                    pass

            if use_streaming_tts:
                text_queue = queue.Queue()
                stop_event = threading.Event()

                def display_callback(sentence: str):
                    """Called by TTS consumer when a sentence is ready to display + speak."""
                    nonlocal _streaming_box_opened
                    if not _streaming_box_opened:
                        _streaming_box_opened = True
                        w = self._scrollback_box_width(getattr(self.console, "width", 80))
                        label = " ⚕ opencodon "
                        if self.show_timestamps:
                            label = f"{label}{datetime.now().strftime(getattr(self, 'timestamp_format', '%H:%M'))} "
                        fill = w - 2 - OpencodonCLI._status_bar_display_width(label)
                        _cprint(f"\n{_ACCENT}╭─{label}{'─' * max(fill - 1, 0)}╮{_RST}")
                    _cprint(f"{_STREAM_PAD}{sentence.rstrip()}")

                tts_thread = threading.Thread(
                    target=stream_tts_to_speaker,
                    args=(text_queue, stop_event, self._voice_tts_done),
                    kwargs={"display_callback": display_callback},
                    daemon=True,
                )
                tts_thread.start()
                # Expose the pipeline's stop event so barge-in paths (voice
                # key, VAD monitor) can cut playback from outside this turn.
                self._voice_tts_stop = stop_event
                if self._voice_continuous:
                    threading.Thread(
                        target=self._voice_barge_in_monitor, args=(stop_event,), daemon=True
                    ).start()

                def stream_callback(delta: str):
                    if text_queue is not None:
                        text_queue.put(delta)

            # When voice mode is active, prepend a brief instruction so the
            # model responds concisely. The prefix is API-call-local only —
            # run_conversation persists the original clean user message.
            _voice_prefix = ""
            if self._voice_mode and isinstance(message, str):
                _voice_prefix = (
                    "[Voice input — respond concisely and conversationally, "
                    "2-3 sentences max. No code blocks or markdown.] "
                )

            def run_agent():
                nonlocal result
                result = self._chat_agent_turn(message, _voice_prefix, stream_callback)

            # Start agent in background thread (daemon so it cannot keep the
            # process alive when the user closes the terminal tab — SIGHUP
            # exits the main thread and daemon threads are reaped automatically).
            # Start per-prompt elapsed timer — frozen after the agent thread
            # finishes; reset on the next turn.
            self._prompt_start_time = time.time()
            self._prompt_duration = 0.0
            agent_thread = threading.Thread(target=run_agent, daemon=True)
            agent_thread.start()

            interrupt_msg = self._chat_monitor_interrupts(agent_thread, stop_event)

            # Wait for the agent thread to finish.  After an interrupt the
            # agent may take a few seconds to clean up (kill subprocess, persist
            # session).  Poll instead of a blocking join so the process_loop
            # stays responsive — if the user sent another interrupt or the
            # agent gets stuck, we can break out instead of freezing forever.
            if interrupt_msg is not None:
                # Interrupt path: poll briefly, then move on.  The agent
                # thread is daemon — it dies on process exit regardless.
                for _wait_tick in range(50):  # 50 * 0.2s = 10s max
                    agent_thread.join(timeout=0.2)
                    if not agent_thread.is_alive():
                        break
                    # Check if user fired ANOTHER interrupt (Ctrl+C sets
                    # _should_exit which process_loop checks on next pass).
                    if getattr(self, '_should_exit', False):
                        break
                if agent_thread.is_alive():
                    logger.warning(
                        "Agent thread still alive after interrupt "
                        "(thread %s). Daemon thread will be cleaned up "
                        "on exit.",
                        agent_thread.ident,
                    )
            else:
                # Normal completion: agent thread should be done already,
                # but guard against edge cases.
                agent_thread.join(timeout=30)

            # Freeze per-prompt elapsed timer once the agent thread has
            # exited (or been abandoned as a daemon after interrupt).
            if self._prompt_start_time is not None:
                self._prompt_duration = max(0.0, time.time() - self._prompt_start_time)
                self._prompt_start_time = None
            # Record when this agent loop finished so the status bar can show
            # idle time since the last final response.
            self._last_turn_finished_at = time.time()

            # Proactively clean up async clients whose event loop is dead.
            # The agent thread may have created AsyncOpenAI clients bound
            # to a per-thread event loop; if that loop is now closed, those
            # clients' __del__ would crash prompt_toolkit's loop on GC.
            try:
                from opencodon.core.auxiliary_client import cleanup_stale_async_clients
                cleanup_stale_async_clients()
            except Exception:
                pass

            # Flush any remaining streamed text and close the box
            self._flush_stream()

            # Signal end-of-text to TTS consumer and wait for it to finish
            if use_streaming_tts and text_queue is not None:
                text_queue.put(None)  # sentinel
                if tts_thread is not None:
                    tts_thread.join(timeout=120)

            # Drain any remaining agent output still in the StdoutProxy
            # buffer so tool/status lines render ABOVE our response box.
            # The flush pushes data into the renderer queue; the short
            # sleep lets the renderer actually paint it before we draw.
            sys.stdout.flush()
            time.sleep(0.15)

            # Update history with full conversation
            self.conversation_history = result.get("messages", self.conversation_history) if result else self.conversation_history

            # If auto-compression fired mid-turn, the agent created a new
            # continuation session and mutated self.agent.session_id. Sync
            # the CLI's session_id so /status, /resume, title generation,
            # and the exit summary all target the live child session rather
            # than the ended parent. Mirrors the gateway's post-run sync
            # (gateway/run.py around line 9983).
            if (
                self.agent
                and getattr(self.agent, "session_id", None)
                and self.agent.session_id != self.session_id
            ):
                self._transfer_session_yolo(self.session_id, self.agent.session_id)
                self.session_id = self.agent.session_id
                self._pending_title = None

            # Get the final response
            response = result.get("final_response", "") if result else ""

            # Auto-generate session title after first exchange (non-blocking)
            if response and result and not result.get("failed") and not result.get("partial"):
                try:
                    from opencodon.core.title_generator import maybe_auto_title
                    # Route title-generation failures through the agent's
                    # user-visible warning channel so a depleted auxiliary
                    # provider doesn't silently leave sessions untitled
                    # (issue #15775).
                    _title_failure_cb = getattr(
                        self.agent, "_emit_auxiliary_failure", None
                    ) if self.agent else None
                    # Snapshot the runtime identity; the validator lets the
                    # background titler skip its LLM call if the user switches
                    # models before it fires (a stale request would reload an
                    # unloaded Ollama model, #19027).
                    _title_model = self.model
                    _title_provider = self.provider
                    maybe_auto_title(
                        self._session_db,
                        self.session_id,
                        message,
                        response,
                        self.conversation_history,
                        failure_callback=_title_failure_cb,
                        main_runtime={
                            "model": self.model,
                            "provider": self.provider,
                            "base_url": self.base_url,
                            "api_key": self.api_key,
                            "api_mode": self.api_mode,
                        },
                        runtime_validator=lambda: (
                            getattr(self, "model", None) == _title_model
                            and getattr(self, "provider", None) == _title_provider
                        ),
                    )
                except Exception:
                    pass

            # Handle failed or partial results (e.g., non-retryable errors, rate limits,
            # truncated output, invalid tool calls). Both "failed" and "partial" with
            # an empty final_response mean the agent couldn't produce a usable answer.
            if result and (result.get("failed") or result.get("partial")) and not response:
                error_detail = result.get("error", "Unknown error")
                response = f"Error: {error_detail}"
                # Stop continuous voice mode on persistent errors (e.g. 429 rate limit)
                # to avoid an infinite error → record → error loop
                if self._voice_continuous:
                    self._voice_continuous = False
                    _cprint(f"\n{_DIM}Continuous voice mode stopped due to error.{_RST}")

            response, pending_message = self._chat_resolve_interrupt(
                result, response, interrupt_msg, agent_thread
            )

            response_previewed = result.get("response_previewed", False) if result else False

            self._chat_render_reasoning(result)

            self._chat_render_response(
                response, result, use_streaming_tts, _streaming_box_opened, response_previewed
            )


            # Play terminal bell when agent finishes (if enabled).
            # Works over SSH — the bell propagates to the user's terminal.
            if self.bell_on_complete:
                sys.stdout.write("\a")
                sys.stdout.flush()

            # Notify when iteration budget was hit
            if result and not result.get("completed") and not result.get("interrupted"):
                _api_calls = result.get("api_calls", 0)
                if _api_calls >= getattr(self.agent, "max_iterations", 90):
                    _max_iter = getattr(self.agent, "max_iterations", 90)
                    _cprint(
                        f"\n{_DIM}⚠ Iteration budget reached "
                        f"({_api_calls}/{_max_iter}) — "
                        f"response may be incomplete{_RST}"
                    )

            # Speak response aloud if voice TTS is enabled
            # Skip batch TTS when streaming TTS already handled it
            if self._voice_tts and response and not use_streaming_tts:
                self._voice_speak_response_async(response)


            self._chat_requeue_after_interrupt(pending_message)

            # If a /steer was left over (agent finished before another tool
            # batch could absorb it), deliver it as the next user turn.
            _leftover_steer = result.get("pending_steer") if result else None
            if _leftover_steer and hasattr(self, '_pending_input'):
                preview = _leftover_steer[:60] + ("..." if len(_leftover_steer) > 60 else "")
                print(f"\n⏩ Delivering leftover /steer as next turn: '{preview}'")
                self._pending_input.put(_leftover_steer)

            return response
            
        except Exception as e:
            print(f"Error: {e}")
            return None
        finally:
            # Ensure streaming TTS resources are cleaned up even on error.
            # Normal path sends the sentinel at line ~3568; this is a safety
            # net for exception paths that skip it.  Duplicate sentinels are
            # harmless — stream_tts_to_speaker exits on the first None.
            if text_queue is not None:
                try:
                    text_queue.put_nowait(None)
                except Exception:
                    pass
            if stop_event is not None:
                stop_event.set()
            if tts_thread is not None and tts_thread.is_alive():
                tts_thread.join(timeout=5)

    def _chat_requeue_after_interrupt(self, pending_message):
        """Verbatim from chat(): re-queue interrupt message(s) as the next turn."""
        # Re-queue the interrupt message (and any that arrived while we were
        # processing the first) as the next prompt for process_loop.
        # Only reached when busy_input_mode == "interrupt" (the default).
        # In "queue" mode Enter routes directly to _pending_input so this
        # block is never hit.
        if pending_message and hasattr(self, '_pending_input'):
            all_parts = [pending_message]
            while not self._interrupt_queue.empty():
                try:
                    extra = self._interrupt_queue.get_nowait()
                    if extra:
                        all_parts.append(extra)
                except queue.Empty:
                    break
            combined = "\n".join(all_parts)
            n = len(all_parts)
            preview = combined[:50] + ("..." if len(combined) > 50 else "")
            if n > 1:
                print(f"\n⚡ Sending {n} messages after interrupt: '{preview}'")
            else:
                print(f"\n⚡ Sending after interrupt: '{preview}'")
            self._pending_input.put(combined)

    def _chat_render_response(self, response, result, use_streaming_tts, _streaming_box_opened, response_previewed):
        """Verbatim from chat(): render the final response box + billing CTA."""
        if response and not response_previewed:
            # Use skin engine for label/color with fallback
            try:
                from opencodon.frontends.cli.skin_engine import get_active_skin
                _skin = get_active_skin()
                label = _skin.get_branding("response_label", "⚕ opencodon")
                _resp_color = _maybe_remap_for_light_mode(_skin.get_color("response_border", "#CD7F32"))
                _resp_text = _maybe_remap_for_light_mode(_skin.get_color("banner_text", "#FFF8DC"))
            except Exception:
                label = "⚕ opencodon"
                _resp_color = _maybe_remap_for_light_mode("#CD7F32")
                _resp_text = _maybe_remap_for_light_mode("#FFF8DC")

            is_error_response = result and (result.get("failed") or result.get("partial"))
            already_streamed = self._stream_started and self._stream_box_opened and not is_error_response
            if use_streaming_tts and _streaming_box_opened and not is_error_response:
                # Text was already printed sentence-by-sentence; just close the box
                w = self._scrollback_box_width()
                _cprint(f"\n{_ACCENT}╰{'─' * (w - 2)}╯{_RST}")
            elif already_streamed:
                # Response was already streamed token-by-token with box framing;
                # _flush_stream() already closed the box. Skip Rich Panel.
                pass
            else:
                _chat_console = ChatConsole()
                _chat_console.print(Panel(
                    _render_final_assistant_content(response, mode=self.final_response_markdown),
                    title=f"[{_resp_color} bold]{label}[/]",
                    title_align="left",
                    border_style=_resp_color,
                    style=_resp_text,
                    box=rich_box.HORIZONTALS,
                    padding=(1, 4),
                    width=self._scrollback_box_width(),
                ))

            # Durable, provider-agnostic billing CTA below the response. The
            # response panel carries the full guidance; this pins the single
            # action to take (the provider's billing page) so it stays
            # visible instead of scrolling away as prose.
            if result and result.get("failure_reason") == "billing":
                _bb = result.get("billing_block") or {}
                _prov_label = _bb.get("provider_label") or "your provider"
                _url = _bb.get("billing_url")
                _cta_lines = [
                    f"Add credits with {_prov_label}"
                    + (f": [bold]{_url}[/]" if _url else ".")
                ]
                _cta_lines.append(
                    "Or switch providers with "
                    "[bold]/model <model> --provider <provider>[/]."
                )
                try:
                    ChatConsole().print(Panel(
                        "\n".join(_cta_lines),
                        title="[#CD7F32 bold]⚡ Out of credits[/]",
                        title_align="left",
                        border_style="#CD7F32",
                        box=rich_box.HORIZONTALS,
                        padding=(1, 4),
                        width=self._scrollback_box_width(),
                    ))
                except Exception:
                    pass

    def _chat_render_reasoning(self, result):
        """Verbatim from chat(): render the collapsed reasoning box."""
        # Display reasoning (thinking) box if enabled and available.
        # Skip when streaming already showed reasoning live.  Use the
        # turn-persistent flag (_reasoning_shown_this_turn) instead of
        # _reasoning_stream_started — the latter gets reset during
        # intermediate turn boundaries (tool-calling loops), which caused
        # the reasoning box to re-render after the final response.
        _reasoning_already_shown = getattr(self, '_reasoning_shown_this_turn', False)
        if self.show_reasoning and result and not _reasoning_already_shown:
            reasoning = result.get("last_reasoning")
            if reasoning:
                w = self._scrollback_box_width()
                r_label = " Reasoning "
                r_fill = w - 2 - len(r_label)
                r_top = f"{_DIM}┌─{r_label}{'─' * max(r_fill - 1, 0)}┐{_RST}"
                r_bot = f"{_DIM}└{'─' * (w - 2)}┘{_RST}"
                # Collapse long reasoning to the first 10 lines unless the
                # user opted into full display via /reasoning full.
                lines = reasoning.strip().splitlines()
                if len(lines) > 10 and not getattr(self, "reasoning_full", False):
                    display_reasoning = "\n".join(lines[:10])
                    display_reasoning += f"\n{_DIM}  ... ({len(lines) - 10} more lines — /reasoning full to show){_RST}"
                else:
                    display_reasoning = reasoning.strip()
                _cprint(f"\n{r_top}\n{_DIM}{display_reasoning}{_RST}\n{r_bot}")

    def _chat_resolve_interrupt(self, result, response, interrupt_msg, agent_thread):
        """Verbatim from chat(): compute pending_message / interrupt bookkeeping."""
        # Handle interrupt - check if we were interrupted
        pending_message = None
        _interrupted_this_turn = bool(result and result.get("interrupted"))
        # Expose the flag for post-turn hooks (e.g. goal continuation)
        # so they can skip themselves when the turn was user-cancelled.
        self._last_turn_interrupted = _interrupted_this_turn
        if _interrupted_this_turn:
            pending_message = result.get("interrupt_message") or interrupt_msg
            # Add indicator that we were interrupted
            if response and pending_message:
                response = response + "\n\n---\n_[Interrupted - processing new message]_"
        elif interrupt_msg:
            # We fired agent.interrupt(interrupt_msg) but the turn result
            # doesn't acknowledge it. Two ways this happens, both racy:
            #   1. The agent thread had already passed its last interrupt
            #      check (or finished) when the interrupt landed — the turn
            #      completed normally and finalize_turn() never saw the flag.
            #   2. The 10s post-interrupt wait above expired and we
            #      abandoned the daemon thread; `result` is still None.
            # In both cases the user's message must NOT be dropped —
            # re-queue it as the next turn (#interrupt-vacuumed-into-void).
            pending_message = interrupt_msg
            # If the interrupt landed after finalize_turn()'s
            # clear_interrupt(), the stale flag would instantly abort the
            # NEXT turn at its first loop check. Clear it now that we've
            # claimed the message — but ONLY if the agent thread actually
            # exited. If it's still alive (abandoned after the 10s wait),
            # the flag is what makes the wedged tool eventually unwind;
            # clearing it would un-signal that thread.
            try:
                if (
                    not agent_thread.is_alive()
                    and self.agent
                    and getattr(self.agent, "_interrupt_requested", False)
                ):
                    self.agent.clear_interrupt()
            except Exception:
                pass
        return response, pending_message

    def _chat_monitor_interrupts(self, agent_thread, stop_event):
        """Verbatim from chat(): watch the interrupt queue while the agent runs."""
        # Monitor the dedicated interrupt queue while the agent runs.
        # _interrupt_queue is separate from _pending_input, so process_loop
        # and chat() never compete for the same queue.
        # When a clarify question is active, user input is handled entirely
        # by the Enter key binding (routed to the clarify response queue),
        # so we skip interrupt processing to avoid stealing that input.
        interrupt_msg = None
        while agent_thread.is_alive():
            if hasattr(self, '_interrupt_queue'):
                try:
                    interrupt_msg = self._interrupt_queue.get(timeout=0.1)
                    if interrupt_msg:
                        # If clarify is active, the Enter handler routes
                        # input directly; this queue shouldn't have anything.
                        # But if it does (race condition), don't interrupt —
                        # and don't drop the message either: park it in
                        # _pending_input so it runs as the next turn.
                        if self._clarify_state or self._clarify_freetext:
                            try:
                                self._pending_input.put(interrupt_msg)
                            except Exception:
                                pass
                            interrupt_msg = None
                            continue
                        print("\n⚡ New message detected, interrupting...")
                        # Signal TTS to stop on interrupt
                        if stop_event is not None:
                            stop_event.set()
                        self.agent.interrupt(interrupt_msg)
                        # Clear any active overlay states the interrupted agent
                        # left behind.  approval/clarify/sudo/secret prompts gate
                        # input (read_only condition + keypress filter) until
                        # explicitly reset — without this the CLI freezes after
                        # an interrupt until the prompt's own timeout expires (#14026).
                        self._clear_active_overlays_for_interrupt()
                        # Debug: log to file (stdout may be devnull from redirect_stdout)
                        try:
                            _dbg = _opencodon_home / "interrupt_debug.log"
                            with open(_dbg, "a", encoding="utf-8") as _f:
                                _f.write(f"{time.strftime('%H:%M:%S')} interrupt fired: msg={str(interrupt_msg)[:60]!r}, "
                                         f"children={len(self.agent._active_children)}, "
                                         f"parent._interrupt={self.agent._interrupt_requested}\n")
                                for _ci, _ch in enumerate(self.agent._active_children):
                                    _f.write(f"  child[{_ci}]._interrupt={_ch._interrupt_requested}\n")
                        except Exception:
                            pass
                        break
                except queue.Empty:
                    # Force prompt_toolkit to flush any pending stdout
                    # output from the agent thread.  Without this, the
                    # StdoutProxy buffer only flushes on renderer passes
                    # triggered by input events — on macOS this causes
                    # the CLI to appear frozen until the user types. (#1624)
                    self._invalidate(min_interval=0.15)
            else:
                # Fallback for non-interactive mode (e.g., single-query)
                agent_thread.join(0.1)
        return interrupt_msg

    def _chat_agent_turn(self, message, _voice_prefix, stream_callback):
        """Verbatim from chat()'s run_agent closure: one agent turn in a worker thread."""
        result = None
        # Set callbacks inside the agent thread so thread-local storage
        # in terminal_tool is populated for this thread.  The main thread
        # registration (run() line ~9046) is invisible here because
        # _callback_tls is threading.local().  Matches the pattern used
        # by acp_adapter/server.py for ACP sessions.
        set_sudo_password_callback(self._sudo_password_callback)
        set_approval_callback(self._approval_callback)
        try:
            set_secret_capture_callback(self._secret_capture_callback)
        except Exception:
            pass
        # Bind this turn's approval session key into the contextvar so
        # ``tools.approval.is_current_session_yolo_enabled()`` resolves
        # against the same key that ``/yolo`` toggles under (see
        # ``_toggle_yolo`` → ``enable_session_yolo(self.session_id)``).
        # Mirrors ``tui_gateway/server.py`` and ``gateway/run.py`` which
        # bind the same contextvar before invoking the agent.
        try:
            from opencodon.tools.approval import (
                reset_current_session_key,
                set_current_session_key,
            )
            _approval_session_token = set_current_session_key(
                self.session_id or "default"
            )
        except Exception:
            reset_current_session_key = None  # type: ignore[assignment]
            _approval_session_token = None
        agent_message = _voice_prefix + message if _voice_prefix else message
        # Prepend pending notes via _prepend_note_to_message, which
        # handles both plain-string and multimodal content-parts list
        # messages. Naive ``note + "\n\n" + agent_message`` crashed with
        # TypeError when an image was attached (agent_message is a list)
        # and a /model or /reload-skills note was queued for the turn.
        _msn = getattr(self, '_pending_model_switch_note', None)
        if _msn:
            agent_message = _prepend_note_to_message(agent_message, _msn)
            self._pending_model_switch_note = None
        # Prepend pending /reload-skills note so the model sees which
        # skills were added/removed before handling this turn. Same
        # one-shot queue pattern as the model-switch note above.
        _srn = getattr(self, '_pending_skills_reload_note', None)
        if _srn:
            agent_message = _prepend_note_to_message(agent_message, _srn)
            self._pending_skills_reload_note = None
        # Barged mid-speech (VAD or record key)? Tell the model it was
        # cut off — same one-shot, API-local note channel as above.
        from opencodon.tools.tts_streaming import SPEECH_INTERRUPTED_NOTE, take_speech_interrupted
        if take_speech_interrupted():
            agent_message = _prepend_note_to_message(agent_message, SPEECH_INTERRUPTED_NOTE)
        _moa_cfg = getattr(self, "_pending_moa_config", None)
        self._pending_moa_config = None
        if _moa_cfg is None:
            _moa_cfg = None
        # Model/skill notes and voice instructions are API-local. Keep
        # the original staged input as the durable transcript value so a
        # close-path marker follows the same dict into turn setup rather
        # than producing a second noted user row (#63766).
        _persist_clean_user_message = (
            message if (_voice_prefix or agent_message != message) else None
        )
        _one_turn_model_restore = getattr(
            self, "_pending_one_turn_model_restore", None
        )
        self._pending_one_turn_model_restore = None
        try:
            result = self.agent.run_conversation(
                user_message=agent_message,
                conversation_history=self.conversation_history[:-1],  # Exclude the message we just added
                stream_callback=stream_callback,
                task_id=self.session_id,
                persist_user_message=_persist_clean_user_message,
                moa_config=_moa_cfg,
            )
            if getattr(self, "_pending_moa_disable_after_turn", False):
                _restore = getattr(self, "_pending_moa_restore_model", None) or {}
                for _key, _value in _restore.items():
                    if _value is not None:
                        setattr(self, _key, _value)
                self.agent = None
                self._pending_moa_restore_model = None
                self._pending_moa_disable_after_turn = False
        except Exception as exc:
            logging.error("run_conversation raised: %s", exc, exc_info=True)
            _summary = getattr(self.agent, '_summarize_api_error', lambda e: str(e)[:300])(exc)
            result = {
                "final_response": f"Error: {_summary}",
                "messages": [],
                "api_calls": 0,
                "completed": False,
                "failed": True,
                "error": _summary,
            }
        finally:
            if _one_turn_model_restore:
                self._restore_model_runtime_snapshot(_one_turn_model_restore)
            # Surface any credit notices queued during the turn (cold-start
            # seed / per-turn capture) now that the response is done — printing
            # at this boundary paints cleanly above the prompt instead of being
            # buried behind the streaming output.
            self._flush_credit_notices()
            # Clear thread-local callbacks so a reused thread doesn't
            # hold stale references to a disposed CLI instance.
            try:
                set_sudo_password_callback(None)
                set_approval_callback(None)
                set_secret_capture_callback(None)
            except Exception:
                pass
            # Release the per-turn approval session key. ``_session_yolo``
            # state itself is preserved across turns (so /yolo persists
            # for the whole CLI run); we just unbind the contextvar so a
            # reused thread doesn't see stale identity on its next run.
            if _approval_session_token is not None and reset_current_session_key is not None:
                try:
                    reset_current_session_key(_approval_session_token)
                except Exception:
                    pass
        return result

    def _chat_stage_user_message(self, agent, message):
        """Verbatim from chat(): stage the user dict for turn-start persistence."""
        # Keep the exact CLI input dict available until turn-start persistence.
        # Copy the completed agent transcript before appending: otherwise this
        # UI-only staging step mutates ``agent._session_messages`` and exposes a
        # duplicate-prone intermediate snapshot to terminal-close persistence.
        if self.conversation_history is getattr(agent, "_session_messages", None):
            self.conversation_history = list(self.conversation_history)
        # The prior turn's override applies only to its own user dict. Clear it
        # before exposing the next staged input to close persistence; otherwise
        # a shutdown before the worker prologue can write old API-local text as
        # this new user message (#63766).
        persist_lock = getattr(agent, "_session_persist_lock", None)

        def _stage_user_message() -> None:
            agent._persist_user_message_idx = None
            agent._persist_user_message_override = None
            agent._persist_user_message_timestamp = None
            staged_user_message = {"role": "user", "content": message}
            agent._pending_cli_user_message = staged_user_message
            self.conversation_history.append(staged_user_message)

        if persist_lock is None:
            _stage_user_message()
        else:
            with persist_lock:
                _stage_user_message()

    def _chat_expand_context_refs(self, message):
        """Verbatim from chat(): expand @file/@diff/@folder references."""
        # Expand @ context references (e.g. @file:main.py, @diff, @folder:src/)
        if not (isinstance(message, str) and "@" in message):
            return None, message
        try:
            from opencodon.core.context.context_references import preprocess_context_references
            from opencodon.core.providers.model_metadata import get_model_context_length
            _ctx_len = get_model_context_length(
                self.model, base_url=self.base_url or "", api_key=self.api_key or "",
                provider=self.provider or "",
                config_context_length=getattr(self.agent, "_config_context_length", None) if self.agent else None)
            _ctx_result = preprocess_context_references(
                message, cwd=os.getcwd(), context_length=_ctx_len)
            if _ctx_result.expanded or _ctx_result.blocked:
                if _ctx_result.references:
                    _cprint(
                        f"  {_DIM}[@ context: {len(_ctx_result.references)} ref(s), "
                        f"{_ctx_result.injected_tokens} tokens]{_RST}")
                for w in _ctx_result.warnings:
                    _cprint(f"  {_DIM}⚠ {w}{_RST}")
                if _ctx_result.blocked:
                    return ("\n".join(_ctx_result.warnings) or "Context injection refused."), message
                message = _ctx_result.message
        except Exception as e:
            logging.debug("@ context reference expansion failed: %s", e)
        return None, message

    def _chat_route_images(self, message, images):
        """Verbatim from chat(): route image attachments by vision capability."""
        # Route image attachments based on the active model's vision capability.
        # "native" → pass pixels as OpenAI-style content parts (adapters
        #            translate for Anthropic/Gemini/Bedrock).
        # "text"   → pre-analyze each image with vision_analyze and prepend the
        #            description as text — works with non-vision models.
        # See agent/image_routing.py for the decision table.
        if not images:
            return message
        try:
            from opencodon.core.media.image_routing import (
                build_native_content_parts,
                decide_image_input_mode,
            )
            from opencodon.config import load_config

            _img_mode = decide_image_input_mode(
                (self.provider or "").strip(),
                (self.model or "").strip(),
                load_config(),
            )
        except Exception as _img_exc:
            logging.debug("image_routing decision failed, defaulting to text: %s", _img_exc)
            _img_mode = "text"

        if _img_mode == "native":
            try:
                _text_for_parts = message if isinstance(message, str) else ""
                _img_str_paths = [str(p) for p in images]
                _parts, _skipped = build_native_content_parts(
                    _text_for_parts,
                    _img_str_paths,
                )
                if _skipped:
                    _cprint(
                        f"  {_DIM}⚠ skipped {len(_skipped)} unreadable image path(s){_RST}"
                    )
                if any(p.get("type") == "image_url" for p in _parts):
                    _img_names = ", ".join(Path(p).name for p in _img_str_paths)
                    _cprint(
                        f"  {_DIM}📎 attaching {len(images)} image(s) natively "
                        f"(model supports vision): {_img_names}{_RST}"
                    )
                    message = _parts
                else:
                    # All images unreadable — fall back to text enrichment.
                    message = self._preprocess_images_with_vision(
                        message if isinstance(message, str) else "", images
                    )
            except Exception as _img_exc:
                logging.warning("native image attach failed, falling back to text: %s", _img_exc)
                message = self._preprocess_images_with_vision(
                    message if isinstance(message, str) else "", images
                )
        else:
            message = self._preprocess_images_with_vision(
                message if isinstance(message, str) else "", images
            )
        return message

    









    # --- Protected TUI extension hooks for wrapper CLIs ---




    def run(self):
        """Run the interactive CLI loop with persistent input at bottom."""
        if not self._claim_active_session("cli"):
            return

        # Detect light/dark terminal mode now (before pt grabs the tty).
        # Caches the result so subsequent _hex_to_ansi / style calls
        # don't risk re-querying mid-render.
        try:
            _detect_light_mode()
        except Exception:
            pass
        # Push the entire TUI to the bottom of the terminal so the banner,
        # responses, and prompt all appear pinned to the bottom — empty
        # space stays above, not below.  This prints enough blank lines to
        # scroll the cursor to the last row before any content is rendered.
        try:
            _term_lines = shutil.get_terminal_size().lines
            if _term_lines > 2:
                print("\n" * (_term_lines - 1), end="", flush=True)
        except Exception:
            pass

        self.show_banner()
        # Surface any active supply-chain security advisories right after the
        # welcome banner. Quiet/single-query paths call this themselves.
        self._show_security_advisories()
        # If resuming a session, load history and display it immediately
        # so the user has context before typing their first message.
        if self._resumed:
            if self._preload_resumed_session():
                self._display_resumed_history()

        try:
            from opencodon.frontends.cli.skin_engine import get_active_skin
            _welcome_skin = get_active_skin()
            _welcome_text = _welcome_skin.get_branding("welcome", "Welcome to opencodon! Type your message or /help for commands.")
            _welcome_color = _welcome_skin.get_color("banner_text", "#FFF8DC")
        except Exception:
            _welcome_text = "Welcome to opencodon! Type your message or /help for commands."
            _welcome_color = "#FFF8DC"
        self._console_print(f"[{_welcome_color}]{_welcome_text}[/]")

        # Warm the /model picker's provider-models cache off-thread during this
        # idle window (banner shown, user about to type). The no-args picker
        # otherwise blocks ~1-2s on serial /v1/models fetches the first time
        # it's opened in a session. Fire-and-forget, guarded once-per-process.
        try:
            from opencodon.frontends.cli.model_switch import prewarm_picker_cache_async
            prewarm_picker_cache_async()
        except Exception:
            pass

        # Pre-import the agent runtime off-thread during the same idle window.
        # The first turn otherwise pays ~1.5s of module imports on the
        # time-to-first-token critical path: `import run_agent` (~0.9s,
        # deferred by the lazy AIAgent wrapper above) plus the OpenAI SDK
        # (~0.6s, deferred until client construction). Python's import lock
        # makes this safe: if the user submits before the warm finishes, the
        # main thread simply blocks on the remaining import work instead of
        # redoing it. Skipped when agent startup is explicitly deferred
        # (Termux) — that path defers heavy work on purpose.
        if os.environ.get("OPENCODON_DEFER_AGENT_STARTUP") != "1":
            def _prewarm_agent_runtime() -> None:
                try:
                    import run_agent  # noqa: F401  (imports model_tools + tool registry)
                    import openai  # noqa: F401
                except Exception:
                    logger.debug("agent runtime pre-import failed", exc_info=True)

            threading.Thread(
                target=_prewarm_agent_runtime,
                name="agent-runtime-prewarm",
                daemon=True,
            ).start()

        # Redaction opt-out warning (#17691): ON by default, loud when off.
        # The redactor snapshots its state at import time so any toggle now
        # won't affect the running process — we just want the operator to
        # see that they're running without the safety net.
        try:
            _redact_raw = os.getenv("OPENCODON_REDACT_SECRETS", "true")
            if _redact_raw.lower() not in {"1", "true", "yes", "on"}:
                self._console_print(
                    "[bold red]⚠  Secret redaction is DISABLED[/] "
                    f"(OPENCODON_REDACT_SECRETS={_redact_raw}). "
                    "API keys and tokens may appear verbatim in chat output, "
                    "session JSONs, and logs. Set "
                    "[cyan]security.redact_secrets: true[/] in config.yaml "
                    "to re-enable."
                )
        except Exception:
            pass
        # Curator — kick off a background skill-maintenance pass on startup
        # if the schedule says we're due.  Runs in a daemon thread so it
        # never blocks the interactive loop.  Best-effort; any failure is
        # swallowed to avoid breaking session startup.
        try:
            from opencodon.core.memory.curator import maybe_run_curator
            maybe_run_curator(
                idle_for_seconds=float("inf"),  # CLI startup = fully idle
                on_summary=lambda msg: self._console_print(
                    f"[dim #6b7684]💾 {msg}[/]"
                ),
            )
        except Exception:
            pass
        if self.preloaded_skills and not self._startup_skills_line_shown:
            skills_label = ", ".join(self.preloaded_skills)
            self._console_print(
                f"[bold {_accent_hex()}]Activated skills:[/] {skills_label}"
            )
            self._startup_skills_line_shown = True
        self._console_print()
        
        # State for async operation
        self._agent_running = False
        self._pending_input = queue.Queue()     # For normal input (commands + new queries)
        self._interrupt_queue = queue.Queue()   # For messages typed while agent is running
        # See constructor note. Mirrored here for the run() path that skips
        # the earlier __init__ branch.
        self._last_turn_interrupted = False
        self._should_exit = False
        self._last_ctrl_c_time = 0  # Track double Ctrl+C for force exit

        # Give plugin manager a CLI reference so plugins can inject messages
        from opencodon.plugins_runtime import get_plugin_manager
        get_plugin_manager()._cli_ref = self

        # Config file watcher — detect mcp_servers changes and auto-reload
        from opencodon.config import get_config_path as _get_config_path
        _cfg_path = _get_config_path()
        self._config_mtime: float = _cfg_path.stat().st_mtime if _cfg_path.exists() else 0.0
        self._config_mcp_servers: dict = self.config.get("mcp_servers") or {}
        self._last_config_check: float = 0.0  # monotonic time of last check

        # Clarify tool state: interactive question/answer with the user.
        # When the agent calls the clarify tool, _clarify_state is set and
        # the prompt_toolkit UI switches to a selection mode.
        self._clarify_state = None      # dict with question, choices, selected, response_queue
        self._clarify_freetext = False  # True when user chose "Other" and is typing
        self._clarify_deadline = 0      # monotonic timestamp when the clarify times out

        # Sudo password prompt state (similar mechanism to clarify)
        self._sudo_state = None         # dict with response_queue when active
        self._sudo_deadline = 0
        self._modal_input_snapshot = None

        # Dangerous command approval state (similar mechanism to clarify)
        self._approval_state = None     # dict with command, description, choices, selected, response_queue
        self._approval_deadline = 0
        self._approval_lock = threading.Lock()  # serialize concurrent approval prompts (delegation race fix)

        # Destructive slash-command confirmation state (/new, /clear, /undo).
        # These prompts are answered through the prompt_toolkit composer, not
        # raw input(), so the option labels stay visible and Enter does not EOF
        # the whole app.
        self._slash_confirm_state = None
        self._slash_confirm_deadline = 0

        # Slash command loading state
        self._command_running = False
        self._command_status = ""

        # Secure secret capture state for skill setup
        self._secret_state = None       # dict with var_name, prompt, metadata, response_queue
        self._secret_deadline = 0

        # Clipboard image attachments (paste images into the CLI)
        self._attached_images: list[Path] = []
        self._image_counter = 0

        # Voice mode state (protected by _voice_lock for cross-thread access)
        self._voice_lock = threading.Lock()
        self._voice_mode = False        # Whether voice mode is enabled
        self._voice_tts = False         # Whether TTS output is enabled
        self._voice_recorder = None     # AudioRecorder instance (lazy init)
        self._voice_recording = False   # Whether currently recording
        self._voice_processing = False  # Whether STT is in progress
        self._voice_continuous = False  # Whether to auto-restart after agent responds
        self._voice_tts_done = threading.Event()  # Signals TTS playback finished
        self._voice_tts_done.set()  # Initially "done" (no TTS pending)
        self._voice_tts_stop = None  # active streaming pipeline's stop event
        self._voice_barge_capture = threading.Event()  # barge monitor is capturing the interruption

        if os.environ.get("OPENCODON_DEFER_AGENT_STARTUP") != "1":
            self._install_tool_callbacks()

        if os.environ.get("OPENCODON_DEFER_AGENT_STARTUP") != "1":
            self._ensure_tirith_security()
        
        # Key bindings for the input area
        kb = KeyBindings()

        from prompt_toolkit.keys import Keys as _IgnoreKeys

        @kb.add(_IgnoreKeys.Ignore, eager=True)
        def handle_ignored_terminal_sequence(event):
            """Consume parser-level ignored terminal sequences before self-insert.

            install_ignored_terminal_sequences() in opencodon_cli.pt_input_extras
            registers focus reports (CSI I / CSI O) as Keys.Ignore at the
            VT100 parser level. Without this no-op binding the default
            self-insert path would still fire and the bytes would land in
            the buffer.
            """
            return None

        def handle_enter(event):
            self._repl_handle_enter(event)

        _bind_prompt_submit_keys(kb, handle_enter)
        
        @kb.add('escape', 'enter')
        def handle_alt_enter(event):
            """Alt+Enter inserts a newline for multi-line input.

            Works on mac/Linux/WSL. On Windows Terminal this keystroke is
            intercepted at the terminal layer (toggles fullscreen) and never
            reaches here — Windows users get newline via Ctrl+Enter instead
            (bound below as c-j, since WT delivers Ctrl+Enter as LF).
            """
            event.current_buffer.insert_text('\n')

        if _preserve_ctrl_enter_newline():
            @kb.add('c-j')
            def handle_ctrl_enter_newline(event):
                """Ctrl+Enter inserts a newline on Windows, WSL, SSH, and WT.

                Windows Terminal (incl. WSL/SSH sessions through it) delivers
                Ctrl+Enter as LF (c-j), distinct from plain Enter (c-m). This
                binding makes Ctrl+Enter the equivalent of Alt+Enter on those
                terminals, giving an Enter-involving newline keystroke
                without requiring terminal settings changes. Ctrl+J (the raw
                LF keystroke) also triggers this by virtue of being the same
                key code — a harmless side effect since Ctrl+J has no
                conflicting opencodon binding. See issue #22379.
                """
                event.current_buffer.insert_text('\n')

        # VSCode/Cursor bind Ctrl+G to "Find Next" at the editor level, so
        # the keystroke never reaches the embedded terminal. Alt+G is unbound
        # in those IDEs and arrives here as ('escape', 'g') — register it as
        # a fallback so the editor handoff works inside Cursor/VSCode too.
        _editor_filter = Condition(
            lambda: not self._clarify_state and not self._approval_state and not self._sudo_state and not self._secret_state
        )

        @kb.add('c-g', filter=_editor_filter)
        @kb.add('escape', 'g', filter=_editor_filter)
        def handle_open_in_editor(event):
            """Ctrl+G (or Alt+G in VSCode/Cursor) opens the current draft in an external editor."""
            cli_ref._open_external_editor(event.current_buffer)

        @kb.add('tab', eager=True)
        def handle_tab(event):
            """Tab: accept completion, auto-suggestion, or start completions.

            Priority:
            1. Completion menu open → accept selected completion
            2. Ghost text suggestion available → accept auto-suggestion
            3. Otherwise → start completion menu

            After accepting a provider like 'anthropic:', the completion menu
            closes and complete_while_typing doesn't fire (no keystroke).
            This binding re-triggers completions so stage-2 models appear
            immediately.
            """
            buf = event.current_buffer
            if buf.complete_state:
                # Completion menu is open — accept the selection
                completion = buf.complete_state.current_completion
                if completion is None:
                    # Menu open but nothing selected — select first then grab it
                    buf.go_to_completion(0)
                    completion = buf.complete_state and buf.complete_state.current_completion
                if completion is None:
                    return
                # Accept the selected completion
                buf.apply_completion(completion)
            elif buf.suggestion and buf.suggestion.text:
                # No completion menu, but there's a ghost text auto-suggestion — accept it
                buf.insert_text(buf.suggestion.text)
            else:
                # No menu and no suggestion — start completions from scratch
                buf.start_completion()

        # --- Clarify tool: arrow-key navigation for multiple-choice questions ---

        @kb.add('up', filter=Condition(lambda: bool(self._clarify_state) and not self._clarify_freetext))
        def clarify_up(event):
            """Move selection up in clarify choices."""
            if self._clarify_state:
                self._clarify_state["selected"] = max(0, self._clarify_state["selected"] - 1)
                event.app.invalidate()

        @kb.add('down', filter=Condition(lambda: bool(self._clarify_state) and not self._clarify_freetext))
        def clarify_down(event):
            """Move selection down in clarify choices."""
            if self._clarify_state:
                choices = self._clarify_state.get("choices") or []
                max_idx = len(choices)  # last index is the "Other" option
                self._clarify_state["selected"] = min(max_idx, self._clarify_state["selected"] + 1)
                event.app.invalidate()

        # Number keys for quick clarify selection (1-9, 0 for 10th item)
        def _make_clarify_number_handler(idx):
            def handler(event):
                if self._clarify_state and not self._clarify_freetext:
                    choices = self._clarify_state.get("choices") or []
                    # Map index to choice (treating "Other" as the last option)
                    if idx < len(choices):
                        # Select a numbered choice
                        self._clarify_state["response_queue"].put(choices[idx])
                        self._clarify_state = None
                        self._clarify_freetext = False
                        event.app.invalidate()
                    elif idx == len(choices):
                        # Select "Other" option
                        self._clarify_freetext = True
                        event.app.invalidate()
            return handler

        for _num in range(10):
            # 1-9 select items 0-8, 0 selects item 9 (10thitem)
            _idx = 9 if _num == 0 else _num - 1
            kb.add(str(_num), filter=Condition(lambda: bool(self._clarify_state) and not self._clarify_freetext))(_make_clarify_number_handler(_idx))

        # --- Dangerous command approval: arrow-key navigation ---

        @kb.add('up', filter=Condition(lambda: bool(self._approval_state)))
        def approval_up(event):
            if self._approval_state:
                self._approval_state["selected"] = max(0, self._approval_state["selected"] - 1)
                event.app.invalidate()

        @kb.add('down', filter=Condition(lambda: bool(self._approval_state)))
        def approval_down(event):
            if self._approval_state:
                max_idx = len(self._approval_state["choices"]) - 1
                self._approval_state["selected"] = min(max_idx, self._approval_state["selected"] + 1)
                event.app.invalidate()

        # --- Slash-command confirmation: arrow-key navigation ---
        @kb.add('up', filter=Condition(lambda: bool(self._slash_confirm_state)))
        def slash_confirm_up(event):
            if self._slash_confirm_state:
                self._slash_confirm_state["selected"] = max(0, self._slash_confirm_state.get("selected", 0) - 1)
                event.app.invalidate()

        @kb.add('down', filter=Condition(lambda: bool(self._slash_confirm_state)))
        def slash_confirm_down(event):
            if self._slash_confirm_state:
                max_idx = len(self._slash_confirm_state.get("choices") or []) - 1
                self._slash_confirm_state["selected"] = min(max_idx, self._slash_confirm_state.get("selected", 0) + 1)
                event.app.invalidate()

        # --- /model picker: arrow-key navigation ---
        @kb.add('up', filter=Condition(lambda: bool(self._model_picker_state)))
        def model_picker_up(event):
            if self._model_picker_state:
                self._model_picker_state["selected"] = max(0, self._model_picker_state.get("selected", 0) - 1)
                event.app.invalidate()

        @kb.add('down', filter=Condition(lambda: bool(self._model_picker_state)))
        def model_picker_down(event):
            state = self._model_picker_state
            if not state:
                return
            if state.get("stage") == "provider":
                max_idx = len(state.get("providers") or [])
            else:
                max_idx = len(state.get("model_list") or []) + 1
            state["selected"] = min(max_idx, state.get("selected", 0) + 1)
            event.app.invalidate()

        @kb.add('escape', filter=Condition(lambda: bool(self._model_picker_state)), eager=True)
        def model_picker_escape(event):
            """ESC closes the /model picker."""
            self._close_model_picker()
            event.app.current_buffer.reset()
            event.app.invalidate()

        # Number keys for quick approval selection (1-9, 0 for 10th item)
        def _make_approval_number_handler(idx):
            def handler(event):
                if self._approval_state and idx < len(self._approval_state["choices"]):
                    self._approval_state["selected"] = idx
                    self._handle_approval_selection()
                    event.app.invalidate()
            return handler

        for _num in range(10):
            # 1-9 select items 0-8, 0 selects item 9 (10th item)
            _idx = 9 if _num == 0 else _num - 1
            kb.add(str(_num), filter=Condition(lambda: bool(self._approval_state)))(_make_approval_number_handler(_idx))

        # Number keys for quick slash-confirm selection (1-9, 0 for 10th item)
        def _make_slash_confirm_number_handler(idx):
            def handler(event):
                if self._slash_confirm_state and idx < len(self._slash_confirm_state.get("choices") or []):
                    choice = self._slash_confirm_state["choices"][idx][0]
                    self._submit_slash_confirm_response(choice)
                    event.app.current_buffer.reset()
                    event.app.invalidate()
            return handler

        for _num in range(10):
            _idx = 9 if _num == 0 else _num - 1
            kb.add(str(_num), filter=Condition(lambda: bool(self._slash_confirm_state)))(_make_slash_confirm_number_handler(_idx))

        # --- History navigation: up/down browse history in normal input mode ---
        # The TextArea is multiline, so by default up/down only move the cursor.
        # Buffer.auto_up/auto_down handle both: cursor movement when multi-line,
        # history browsing when on the first/last line (or single-line input).
        _normal_input = Condition(
            lambda: not self._clarify_state and not self._approval_state and not self._slash_confirm_state and not self._sudo_state and not self._secret_state and not self._model_picker_state
        )

        def _recall_without_recollapse(buf, move):
            """Run a history-navigation move, suppressing paste-collapse.

            Recalled history can hold the full text of a paste that was
            collapsed to a placeholder at submit time. Loading it back into the
            buffer looks exactly like a fresh large paste to ``_on_text_changed``
            and would be re-collapsed. Set the skip flag around the move; if the
            move didn't change the text (plain cursor movement), clear the flag
            so a later real paste still collapses.
            """
            before = buf.text
            self._skip_paste_collapse = True
            move()
            if buf.text == before:
                self._skip_paste_collapse = False

        @kb.add('up', filter=_normal_input)
        def history_up(event):
            """Up arrow: browse history when on first line, else move cursor up."""
            buf = event.app.current_buffer
            _recall_without_recollapse(buf, lambda: buf.auto_up(count=event.arg))

        @kb.add('down', filter=_normal_input)
        def history_down(event):
            """Down arrow: browse history when on last line, else move cursor down."""
            buf = event.app.current_buffer
            _recall_without_recollapse(buf, lambda: buf.auto_down(count=event.arg))

        @kb.add('c-l')
        def handle_ctrl_l(event):
            """Ctrl+L: force a clean full-screen repaint.

            Recovers the UI after external terminal buffer drift — tmux /
            cmux tab switches, ``clear`` from a subshell, SSH window
            restores, etc. — that prompt_toolkit can't detect on its own.
            Matches the universal bash/zsh/fish/vim/htop convention.
            """
            self._force_full_redraw()

        @kb.add('c-c')
        def handle_ctrl_c(event):
            """Handle Ctrl+C - cancel interactive prompts, interrupt agent, or exit.
            
            Priority:
            0. Cancel active voice recording
            1. Cancel active sudo/approval/clarify prompt
            2. Interrupt the running agent (first press)
            3. Force exit (second press within 2s, or when idle)
            """
            now = time.time()

            # Cancel active voice recording.
            # Run cancel() in a background thread to prevent blocking the
            # event loop if AudioRecorder._lock or CoreAudio takes time.
            _should_cancel_voice = False
            _recorder_ref = None
            with cli_ref._voice_lock:
                if cli_ref._voice_recording and cli_ref._voice_recorder:
                    _recorder_ref = cli_ref._voice_recorder
                    cli_ref._voice_recording = False
                    cli_ref._voice_continuous = False
                    _should_cancel_voice = True
            if _should_cancel_voice:
                _cprint(f"\n{_DIM}Recording cancelled.{_RST}")
                threading.Thread(
                    target=_recorder_ref.cancel, daemon=True
                ).start()
                event.app.invalidate()
                return

            # Cancel slash confirmation prompt (foreground UI, not an
            # agent-blocking overlay — cancel and stop here).
            if self._slash_confirm_state:
                self._submit_slash_confirm_response("cancel")
                event.app.current_buffer.reset()
                event.app.invalidate()
                return

            # Cancel /model picker (foreground UI — cancel and stop here).
            if self._model_picker_state:
                self._close_model_picker()
                event.app.current_buffer.reset()
                event.app.invalidate()
                return

            # Clear all agent-blocking overlays (approval/clarify/sudo/secret)
            # in one shot.  We do NOT return after clearing — we fall through so
            # that if the agent is also running we fire the interrupt on the same
            # Ctrl+C press.  This fixes the case where a stale/orphaned overlay
            # (left behind by a previous interrupt) consumes the press without
            # ever reaching the agent-interrupt branch, leaving the chat frozen
            # (#14026).
            _overlay_cleared = bool(
                self._sudo_state
                or self._secret_state
                or self._approval_state
                or self._clarify_state
            )
            if _overlay_cleared:
                self._clear_active_overlays_for_interrupt()
                event.app.current_buffer.reset()
                event.app.invalidate()

            # If we only cleared overlays and the agent is NOT running, stop here
            # (don't fall through to the interrupt/exit path).
            if _overlay_cleared and not (self._agent_running and self.agent):
                return

            if self._agent_running and self.agent:
                if now - self._last_ctrl_c_time < 2.0:
                    print("\n⚡ Force exiting...")
                    self._should_exit = True
                    event.app.exit()
                    return
                
                self._last_ctrl_c_time = now
                print("\n⚡ Interrupting agent... (press Ctrl+C again to force exit)")
                self.agent.interrupt()
            # If there's text or images, clear them (like bash).
            # If everything is already empty, exit.
            elif event.app.current_buffer.text or self._attached_images:
                event.app.current_buffer.reset()
                self._attached_images.clear()
                event.app.invalidate()
            else:
                self._should_exit = True
                event.app.exit()

        # Ctrl+Shift+C: no binding needed. Terminal emulators (GNOME Terminal,
        # iTerm2, kitty, Windows Terminal, etc.) intercept Ctrl+Shift+C before
        # the keystroke reaches the application's stdin — prompt_toolkit never
        # sees it, and prompt_toolkit's key spec parser doesn't even recognise
        # 'c-S-c' anyway (the Shift modifier is meaningless on control-sequence
        # keys). #19884 added a handler for this; #19895 patched the resulting
        # startup crash with try/except. Both were based on a misreading of how
        # terminal key events propagate. Deleting the dead handler outright.

        @kb.add('c-q')  # Ctrl+Q
        def handle_ctrl_q(event):
            """Alternative interrupt/exit shortcut (Ctrl+Q).

            Behaves like Ctrl+C: cancels active prompts, interrupts the
            running agent, or clears the input buffer. Does not support
            the double-press 'force exit' feature of Ctrl+C.
            """
            # Cancel active voice recording.
            _should_cancel_voice = False
            _recorder_ref = None
            with cli_ref._voice_lock:
                if cli_ref._voice_recording and cli_ref._voice_recorder:
                    _recorder_ref = cli_ref._voice_recorder
                    cli_ref._voice_recording = False
                    cli_ref._voice_continuous = False
                    _should_cancel_voice = True
            if _should_cancel_voice:
                _cprint(f"\n{_DIM}Recording cancelled.{_RST}")
                threading.Thread(
                    target=_recorder_ref.cancel, daemon=True
                ).start()
                event.app.invalidate()
                return

            # Cancel slash confirmation prompt (foreground UI — cancel and stop).
            if self._slash_confirm_state:
                self._submit_slash_confirm_response("cancel")
                event.app.current_buffer.reset()
                event.app.invalidate()
                return

            # Cancel /model picker (foreground UI — cancel and stop).
            if self._model_picker_state:
                self._close_model_picker()
                event.app.current_buffer.reset()
                event.app.invalidate()
                return

            # Clear all agent-blocking overlays in one shot, then fall through to
            # the agent-interrupt branch so a single Ctrl+Q both clears a stale
            # overlay and interrupts a still-running agent (#14026).
            _overlay_cleared = bool(
                self._sudo_state
                or self._secret_state
                or self._approval_state
                or self._clarify_state
            )
            if _overlay_cleared:
                self._clear_active_overlays_for_interrupt()
                event.app.current_buffer.reset()
                event.app.invalidate()

            if _overlay_cleared and not (self._agent_running and self.agent):
                return

            if self._agent_running and self.agent:
                print("\n⚡ Interrupting agent...")
                self.agent.interrupt()
            elif event.app.current_buffer.text or self._attached_images:
                event.app.current_buffer.reset()
                self._attached_images.clear()
                event.app.invalidate()
            else:
                self._should_exit = True
                event.app.exit()

        @kb.add('c-d')
        def handle_ctrl_d(event):
            """Ctrl+D: delete char under cursor (standard readline behaviour).
            Only exit when the input is empty — same as bash/zsh. Pending
            attached images count as input and block the EOF-exit so the
            user doesn't lose them silently.
            """
            buf = event.app.current_buffer
            if buf.text:
                buf.delete()
            elif self._attached_images:
                # Empty text but pending attachments — no-op, don't exit.
                return
            else:
                self._should_exit = True
                event.app.exit()

        _modal_prompt_active = Condition(
            lambda: bool(self._secret_state or self._sudo_state or self._slash_confirm_state)
        )

        @kb.add('escape', filter=_modal_prompt_active, eager=True)
        def handle_escape_modal(event):
            """ESC cancels active secret/sudo prompts."""
            if self._secret_state:
                self._cancel_secret_capture()
                event.app.current_buffer.reset()
                event.app.invalidate()
                return
            if self._sudo_state:
                self._sudo_state["response_queue"].put("")
                self._sudo_state = None
                event.app.invalidate()
                return
            if self._slash_confirm_state:
                self._submit_slash_confirm_response("cancel")
                event.app.current_buffer.reset()
                event.app.invalidate()
                return

        @kb.add('c-z')
        def handle_ctrl_z(event):
            """Handle Ctrl+Z - suspend process to background (Unix only)."""
            if sys.platform == 'win32':
                _cprint(f"\n{_DIM}Suspend (Ctrl+Z) is not supported on Windows.{_RST}")
                event.app.invalidate()
                return
            import signal as _sig
            from prompt_toolkit.application import run_in_terminal
            from opencodon.frontends.cli.skin_engine import get_active_skin
            agent_name = get_active_skin().get_branding("agent_name", "Opencodon")
            msg = f"\n{agent_name} has been suspended. Run `fg` to bring {agent_name} back."
            def _suspend():
                os.write(1, msg.encode())
                os.kill(0, _sig.SIGTSTP)
            run_in_terminal(_suspend)

        # Voice push-to-talk key: configurable via config.yaml (voice.record_key)
        # Default: Ctrl+B (avoids conflict with Ctrl+R readline reverse-search).
        # Config spellings (ctrl/control/alt/option/opt) are normalized to
        # prompt_toolkit's c-x / a-x format via ``normalize_voice_record_key_for_prompt_toolkit``
        # so the same config value binds identically in the TUI and CLI
        # (Copilot round-9 review on #19835). ``super``/``win``/``windows``
        # configs silently fall back to the default here since prompt_toolkit
        # has no super modifier — log a warning so users notice the
        # TUI/CLI split instead of a silent mismatch (round-11).
        _raw_key: object = "ctrl+b"
        try:
            from opencodon.config import load_config
            from opencodon.frontends.cli.voice import (
                normalize_voice_record_key_for_prompt_toolkit,
                voice_record_key_from_config,
            )
            _raw_key = voice_record_key_from_config(load_config())
            _voice_key = normalize_voice_record_key_for_prompt_toolkit(_raw_key)
            if (
                isinstance(_raw_key, str)
                and _raw_key.strip().lower().split("+", 1)[0].strip() in {"super", "win", "windows"}
                and _voice_key == "c-b"
            ):
                logger.warning(
                    "voice.record_key %r uses a TUI-only modifier (super/win); "
                    "CLI fell back to Ctrl+B. Use ctrl+<key> or alt+<key> for "
                    "cross-runtime parity.",
                    _raw_key,
                )
        except Exception:
            _voice_key = "c-b"

        # Cache the UI label here — same ``_raw_key`` that drives the
        # prompt_toolkit binding below. Every status / placeholder /
        # recording-hint render reads this cached value so display can
        # never drift from the live keybinding even if the user edits
        # voice.record_key mid-session (Copilot round-13 on #19835).
        self.set_voice_record_key_cache(_raw_key)

        @kb.add(_voice_key)
        def handle_voice_record(event):
            """Toggle voice recording when voice mode is active.

            IMPORTANT: This handler runs in prompt_toolkit's event-loop thread.
            Any blocking call here (locks, sd.wait, disk I/O) freezes the
            entire UI.  All heavy work is dispatched to daemon threads.
            """
            if not cli_ref._voice_mode:
                return
            # Always allow STOPPING a recording (even when agent is running)
            if cli_ref._voice_recording:
                # Manual stop via push-to-talk key: stop continuous mode
                with cli_ref._voice_lock:
                    cli_ref._voice_continuous = False
                # Flag clearing is handled atomically inside _voice_stop_and_transcribe
                event.app.invalidate()
                threading.Thread(
                    target=cli_ref._voice_stop_and_transcribe,
                    daemon=True,
                ).start()
            else:
                # Guard: don't START recording during agent run or interactive prompts
                if cli_ref._agent_running:
                    return
                if cli_ref._clarify_state or cli_ref._sudo_state or cli_ref._approval_state or cli_ref._slash_confirm_state:
                    return
                # Guard: don't start while a previous stop/transcribe cycle is
                # still running — recorder.stop() holds AudioRecorder._lock and
                # start() would block the event-loop thread waiting for it.
                if cli_ref._voice_processing:
                    return

                # Interrupt TTS if playing, so user can start talking.
                # stop_playback() is fast (just terminates a subprocess);
                # the stop event drains the streaming pipeline if one is live.
                if not cli_ref._voice_tts_done.is_set():
                    try:
                        from opencodon.tools.tts_streaming import mark_speech_interrupted
                        mark_speech_interrupted()
                        if cli_ref._voice_tts_stop is not None:
                            cli_ref._voice_tts_stop.set()
                        from opencodon.tools.voice_mode import stop_playback
                        stop_playback()
                        cli_ref._voice_tts_done.set()
                    except Exception:
                        pass

                with cli_ref._voice_lock:
                    cli_ref._voice_continuous = True

                # Dispatch to a daemon thread so play_beep(sd.wait),
                # AudioRecorder.start(lock acquire), and config I/O
                # never block the prompt_toolkit event loop.
                def _start_recording():
                    try:
                        cli_ref._voice_start_recording()
                        if hasattr(cli_ref, '_app') and cli_ref._app:
                            cli_ref._app.invalidate()
                    except Exception as e:
                        _cprint(f"\n{_DIM}Voice recording failed: {e}{_RST}")

                threading.Thread(target=_start_recording, daemon=True).start()
                event.app.invalidate()
        from prompt_toolkit.keys import Keys

        @kb.add(Keys.BracketedPaste, eager=True)
        def handle_paste(event):
            """Handle terminal paste — detect clipboard images.

            When the terminal supports bracketed paste, Ctrl+V / Cmd+V
            triggers this with the pasted text. We only auto-attach a
            clipboard image for image-only/empty paste gestures so text
            pastes and dictation do not accidentally attach stale images.

            Large pastes (5+ lines) are collapsed to a file reference
            placeholder while preserving any existing user text in the
            buffer.
            """
            # Diagnostic canary: measure how long the paste handler blocks
            # the prompt_toolkit event loop. If this exceeds ~500ms we log
            # it so recurring "CLI freezes on paste" reports (issue #16263,
            # macOS Tahoe 26 + iTerm2/Ghostty) arrive with data attached.
            _paste_handler_start = time.perf_counter()
            _paste_raw_size = len(event.data or "")
            pasted_text = event.data or ""
            # Normalise line endings — Windows \r\n and old Mac \r both become \n
            # so the 5-line collapse threshold and display are consistent.
            pasted_text = pasted_text.replace('\r\n', '\n').replace('\r', '\n')
            pasted_text = _strip_leaked_bracketed_paste_wrappers(pasted_text)
            pasted_text, _had_mouse_reports = _strip_leaked_terminal_responses_with_meta(pasted_text)
            if _had_mouse_reports:
                self._recover_terminal_input_modes(reason="mouse reports leaked into bracketed paste payload")
            if _should_auto_attach_clipboard_image_on_paste(pasted_text) and self._try_attach_clipboard_image():
                event.app.invalidate()
            if pasted_text:
                # Sanitize surrogate characters (e.g. from Word/Google Docs paste) before writing
                from opencodon.core.run_agent import _sanitize_surrogates
                pasted_text = _sanitize_surrogates(pasted_text)
                line_count = pasted_text.count('\n')
                buf = event.current_buffer
                threshold = self.config.get("paste_collapse_threshold", 5)
                char_threshold = self.config.get("paste_collapse_char_threshold", 2000)
                lines_hit = threshold > 0 and line_count >= threshold
                chars_hit = char_threshold > 0 and len(pasted_text) >= char_threshold
                if (lines_hit or chars_hit) and not buf.text.strip().startswith('/'):
                    _paste_counter[0] += 1
                    paste_dir = _opencodon_home / "pastes"
                    paste_dir.mkdir(parents=True, exist_ok=True)
                    paste_file = paste_dir / f"paste_{_paste_counter[0]}_{datetime.now().strftime('%H%M%S')}.txt"
                    paste_file.write_text(pasted_text, encoding="utf-8")
                    logger.info("Collapsed paste #%d: %d lines, %d chars -> %s", _paste_counter[0], line_count + 1, len(pasted_text), paste_file)
                    placeholder = f"[Pasted text #{_paste_counter[0]}: {line_count + 1} lines \u2192 {paste_file}]"
                    prefix = ""
                    if buf.cursor_position > 0 and buf.text[buf.cursor_position - 1] != '\n':
                        prefix = "\n"
                    _paste_just_collapsed[0] = True
                    buf.insert_text(prefix + placeholder)
                else:
                    buf.insert_text(pasted_text)
            _paste_handler_elapsed_ms = (time.perf_counter() - _paste_handler_start) * 1000.0
            if _paste_handler_elapsed_ms > 500.0:
                logger.warning(
                    "Slow bracketed-paste handler: %.1fms to process %d bytes "
                    "(%d lines) on %s. If the input becomes unresponsive after "
                    "this, attach this log line to the bug report.",
                    _paste_handler_elapsed_ms,
                    _paste_raw_size,
                    pasted_text.count('\n') + 1 if pasted_text else 0,
                    sys.platform,
                )

        @kb.add('c-v')
        def handle_ctrl_v(event):
            """Fallback image paste for terminals without bracketed paste.

            On Linux terminals (GNOME Terminal, Konsole, etc.), Ctrl+V
            sends raw byte 0x16 instead of triggering a paste.  This
            binding catches that and checks the clipboard for images.
            On terminals that DO intercept Ctrl+V for paste (macOS
            Terminal, iTerm2, VSCode, Windows Terminal), the bracketed
            paste handler fires instead and this binding never triggers.
            """
            if self._try_attach_clipboard_image():
                event.app.invalidate()

        @kb.add('escape', 'v')
        def handle_alt_v(event):
            """Alt+V — paste image from clipboard.

            Alt key combos pass through all terminal emulators (sent as
            ESC + key), unlike Ctrl+V which terminals intercept for text
            paste.  This is the reliable way to attach clipboard images
            on WSL2, VSCode, and any terminal over SSH where Ctrl+V
            can't reach the application for image-only clipboard.
            """
            if self._try_attach_clipboard_image():
                event.app.invalidate()
            else:
                # No image found — show a hint
                pass  # silent when no image (avoid noise on accidental press)

        # Dynamic prompt: shows opencodon symbol when agent is working,
        # or answer prompt when clarify freetext mode is active.
        cli_ref = self

        def get_prompt():
            return cli_ref._get_tui_prompt_fragments()

        # Create the input area with multiline (Alt+Enter), autocomplete, and paste handling
        from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
        from prompt_toolkit.completion import ThreadedCompleter


        _completer = SlashCommandCompleter(
            skill_commands_provider=lambda: get_skill_commands(),
            command_filter=cli_ref._command_available,
            skill_bundles_provider=lambda: get_skill_bundles(),
        )
        input_area = TextArea(
            height=Dimension(min=1, max=8, preferred=1),
            prompt=get_prompt,
            style='class:input-area',
            multiline=True,
            wrap_lines=True,
            read_only=Condition(lambda: bool(cli_ref._command_running)),
            history=FileHistory(str(self._history_file)),
            # complete_while_typing fires the completer on every keystroke. The
            # completer does blocking work — fuzzy @-file indexing shells out to
            # rg/fd (up to a 2s timeout) and path completion hits os.listdir/stat
            # — so running it inline would stall the render loop on each key (very
            # noticeable on WSL2/slow filesystems). ThreadedCompleter moves it off
            # the UI event loop, keeping typing responsive.
            completer=ThreadedCompleter(_completer),
            complete_while_typing=True,
            auto_suggest=SlashCommandAutoSuggest(
                history_suggest=AutoSuggestFromHistory(),
                completer=_completer,
            ),
        )
        # Keep prompt_toolkit on its simple tempfile path. Setting
        # buffer.tempfile = "prompt.md" triggers its complex-tempfile branch,
        # which tries to mkdir() the mkdtemp() directory again and raises
        # EEXIST. The suffix keeps markdown highlighting without that bug.
        input_area.buffer.tempfile_suffix = '.md'

        # Dynamic height: accounts for both explicit newlines AND visual
        # wrapping of long lines so the input area always fits its content.
        def _input_height():
            try:
                from prompt_toolkit.application import get_app

                doc = input_area.buffer.document
                try:
                    terminal_columns = get_app().output.get_size().columns
                except Exception:
                    terminal_columns = shutil.get_terminal_size((80, 24)).columns
                return _estimate_tui_input_height(
                    doc.lines,
                    self._get_tui_prompt_text(),
                    terminal_columns,
                )
            except Exception:
                return 1

        input_area.window.height = _input_height

        # Paste collapsing: detect large pastes and save to temp file
        _paste_counter = [0]
        _prev_text_len = [0]
        _prev_newline_count = [0]
        _paste_just_collapsed = [False]
        self._skip_paste_collapse = False

        def _on_text_changed(buf):
            """Detect large pastes and collapse them to a file reference.

            When bracketed paste is available, handle_paste collapses
            large pastes directly.  This handler is a fallback for
            terminals without bracketed paste support.

            Two heuristics (either triggers collapse):
            1. Many characters added at once (chars_added > 1) — works
               when the terminal delivers the paste in one event-loop tick.
            2. Newline count jumped by 4+ in a single text-change event —
               catches terminals that feed characters individually but
               still batch newlines.  Alt+Enter only adds 1 newline per
               event so it never triggers this.
            """
            text = _strip_leaked_bracketed_paste_wrappers(buf.text)
            text, _had_mouse_reports = _strip_leaked_terminal_responses_with_meta(text)
            if _had_mouse_reports:
                self._recover_terminal_input_modes(reason="mouse reports leaked into prompt buffer")
            if text != buf.text:
                cursor = min(buf.cursor_position, len(text))
                _paste_just_collapsed[0] = True
                buf.text = text
                buf.cursor_position = cursor
                _prev_text_len[0] = len(text)
                _prev_newline_count[0] = text.count('\n')
                return
            chars_added = len(text) - _prev_text_len[0]
            _prev_text_len[0] = len(text)
            if _paste_just_collapsed[0] or self._skip_paste_collapse:
                _paste_just_collapsed[0] = False
                self._skip_paste_collapse = False
                _prev_newline_count[0] = text.count('\n')
                return
            line_count = text.count('\n')
            newlines_added = line_count - _prev_newline_count[0]
            _prev_newline_count[0] = line_count
            is_paste = chars_added > 1 or newlines_added >= 4
            threshold = self.config.get("paste_collapse_threshold_fallback", 5)
            char_threshold = self.config.get("paste_collapse_char_threshold", 2000)
            lines_hit = threshold > 0 and line_count >= threshold
            chars_hit = char_threshold > 0 and len(text) >= char_threshold
            if (lines_hit or chars_hit) and is_paste and not text.startswith('/'):
                _paste_counter[0] += 1
                paste_dir = _opencodon_home / "pastes"
                paste_dir.mkdir(parents=True, exist_ok=True)
                paste_file = paste_dir / f"paste_{_paste_counter[0]}_{datetime.now().strftime('%H%M%S')}.txt"
                paste_file.write_text(text, encoding="utf-8")
                logger.info("Collapsed paste #%d: %d lines, %d chars -> %s (fallback)", _paste_counter[0], line_count + 1, len(text), paste_file)
                _paste_just_collapsed[0] = True
                buf.text = f"[Pasted text #{_paste_counter[0]}: {line_count + 1} lines \u2192 {paste_file}]"
                buf.cursor_position = len(buf.text)

        input_area.buffer.on_text_changed += _on_text_changed

        # --- Input processors for password masking and inline placeholder ---

        # Mask input with '*' when the sudo password prompt is active
        input_area.control.input_processors.append(
            ConditionalProcessor(
                PasswordProcessor(),
                filter=Condition(
                    lambda: bool(cli_ref._sudo_state) or bool(cli_ref._secret_state)
                ),
            )
        )

        class _PlaceholderProcessor(Processor):
            """Render grayed-out placeholder text inside the input when empty."""
            def __init__(self, get_text):
                self._get_text = get_text

            def apply_transformation(self, ti):
                if not ti.document.text and ti.lineno == 0:
                    text = self._get_text()
                    if text:
                        # Append after existing fragments (preserves the ❯ prompt)
                        return Transformation(fragments=ti.fragments + [('class:placeholder', text)])
                return Transformation(fragments=ti.fragments)

        def _get_placeholder():
            if cli_ref._voice_recording:
                _label = cli_ref._voice_record_key_label()
                return f"recording... {_label} to stop, Ctrl+C to cancel"
            if cli_ref._voice_processing:
                return "transcribing..."
            if cli_ref._sudo_state:
                return "type password (hidden), Enter to submit · ESC to skip"
            if cli_ref._secret_state:
                return "type secret (hidden), Enter to submit · ESC to skip"
            if cli_ref._approval_state:
                return ""
            if cli_ref._slash_confirm_state:
                return "type 1/2/3, or use ↑/↓ then Enter"
            if cli_ref._clarify_freetext:
                return "type your answer here and press Enter"
            if cli_ref._clarify_state:
                return ""
            if cli_ref._command_running:
                frame = cli_ref._command_spinner_frame()
                status = cli_ref._command_status or "Processing command..."
                return f"{frame} {status}"
            if cli_ref._agent_running:
                return "msg=interrupt · /queue · /bg · /steer · Ctrl+C cancel"
            if cli_ref._voice_mode:
                _label = cli_ref._voice_record_key_label()
                return f"type or {_label} to record"
            return ""

        input_area.control.input_processors.append(_PlaceholderProcessor(_get_placeholder))

        # Hint line above input: shown only for interactive prompts that need
        # extra instructions (sudo countdown, approval navigation, clarify).
        # The agent-running interrupt hint is now an inline placeholder above.
        def get_hint_text():
            if cli_ref._sudo_state:
                remaining = max(0, int(cli_ref._sudo_deadline - time.monotonic()))
                return [
                    ('class:hint', '  password hidden · Enter to skip'),
                    ('class:clarify-countdown', f'  ({remaining}s)'),
                ]

            if cli_ref._secret_state:
                remaining = max(0, int(cli_ref._secret_deadline - time.monotonic()))
                return [
                    ('class:hint', '  secret hidden · Enter to skip'),
                    ('class:clarify-countdown', f'  ({remaining}s)'),
                ]

            if cli_ref._approval_state:
                remaining = max(0, int(cli_ref._approval_deadline - time.monotonic()))
                return [
                    ('class:hint', '  ↑/↓ to select, Enter to confirm'),
                    ('class:clarify-countdown', f'  ({remaining}s)'),
                ]

            if cli_ref._slash_confirm_state:
                remaining = max(0, int(cli_ref._slash_confirm_deadline - time.monotonic()))
                return [
                    ('class:hint', '  type 1/2/3, or ↑/↓ to select, Enter to confirm'),
                    ('class:clarify-countdown', f'  ({remaining}s)'),
                ]

            if cli_ref._clarify_state:
                # None deadline = unlimited wait → hide the countdown entirely.
                if cli_ref._clarify_deadline is None:
                    countdown = ''
                else:
                    remaining = max(0, int(cli_ref._clarify_deadline - time.monotonic()))
                    countdown = f'  ({remaining}s)'
                if cli_ref._clarify_freetext:
                    return [
                        ('class:hint', '  type your answer and press Enter'),
                        ('class:clarify-countdown', countdown),
                    ]
                return [
                    ('class:hint', '  ↑/↓ to select, Enter to confirm'),
                    ('class:clarify-countdown', countdown),
                ]

            if cli_ref._command_running:
                frame = cli_ref._command_spinner_frame()
                return [
                    ('class:hint', f'  {frame} command in progress · input temporarily disabled'),
                ]

            return []

        def get_hint_height():
            if cli_ref._sudo_state or cli_ref._secret_state or cli_ref._approval_state or cli_ref._slash_confirm_state or cli_ref._clarify_state or cli_ref._command_running:
                return 1
            # Keep a spacer while the agent runs on roomy terminals, but reclaim
            # the row on narrow/mobile screens where every line matters.
            return cli_ref._agent_spacer_height()

        def get_spinner_text():
            spinner_line = cli_ref._render_spinner_text()
            if not spinner_line:
                return []
            return [('class:hint', spinner_line)]

        def get_spinner_height():
            return cli_ref._spinner_widget_height()

        spinner_widget = Window(
            content=FormattedTextControl(get_spinner_text),
            height=get_spinner_height,
            wrap_lines=True,
        )

        spacer = Window(
            content=FormattedTextControl(get_hint_text),
            height=get_hint_height,
        )

        # --- Clarify tool: dynamic display widget for questions + choices ---

        def _panel_box_width(title: str, content_lines: list[str], min_width: int = 46, max_width: int = 76) -> int:
            """Choose a stable panel width wide enough for the title and content."""
            term_cols = shutil.get_terminal_size((100, 20)).columns
            longest = max([len(title)] + [len(line) for line in content_lines] + [min_width - 4])
            inner = min(max(longest + 4, min_width - 2), max_width - 2, max(24, term_cols - 6))
            return inner + 2  # account for the single leading/trailing spaces inside borders

        def _wrap_panel_text(text: str, width: int, subsequent_indent: str = "") -> list[str]:
            wrapped = textwrap.wrap(
                text,
                width=max(8, width),
                break_long_words=False,
                break_on_hyphens=False,
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

        def _get_clarify_display():
            """Build styled text for the clarify question/choices panel.

            Layout priority: choices + Other option must always render even if
            the question is very long. The question is budgeted to leave enough
            rows for the choices and trailing chrome; anything over the budget
            is truncated with a marker.
            """
            state = cli_ref._clarify_state
            if not state:
                return []

            question = state["question"]
            choices = state.get("choices") or []
            selected = state.get("selected", 0)
            preview_lines = _wrap_panel_text(question, 60)
            for i, choice in enumerate(choices):
                # Show number prefix for quick selection (1-9 for items 1-9, 0 for 10th item)
                if i < 9:
                    num_prefix = str(i + 1)
                elif i == 9:
                    num_prefix = '0'
                else:
                    num_prefix = ' '
                if i == selected and not cli_ref._clarify_freetext:
                    prefix = f"❯ {num_prefix}. "
                else:
                    prefix = f"  {num_prefix}. "
                preview_lines.extend(_wrap_panel_text(f"{prefix}{choice}", 60, subsequent_indent="    "))
            # "Other" option in preview
            other_num = len(choices) + 1
            if other_num < 10:
                other_num_prefix = str(other_num)
            elif other_num == 10:
                other_num_prefix = '0'
            else:
                other_num_prefix = ' '
            other_label = (
                f"❯ {other_num_prefix}. Other (type below)" if cli_ref._clarify_freetext
                else f"❯ {other_num_prefix}. Other (type your answer)" if selected == len(choices)
                else f"  {other_num_prefix}. Other (type your answer)"
            )
            preview_lines.extend(_wrap_panel_text(other_label, 60, subsequent_indent="    "))
            box_width = _panel_box_width("opencodon needs your input", preview_lines)
            inner_text_width = max(8, box_width - 2)

            # Pre-wrap choices + Other option — these are mandatory.
            choice_wrapped: list[tuple[int, str]] = []
            if choices:
                for i, choice in enumerate(choices):
                    # Show number prefix for quick selection (1-9 for items 1-9, 0 for 10th item)
                    if i < 9:
                        num_prefix = str(i + 1)
                    elif i == 9:
                        num_prefix = '0'
                    else:
                        num_prefix = ' '
                    if i == selected and not cli_ref._clarify_freetext:
                        prefix = f'❯ {num_prefix}. '
                    else:
                        prefix = f'  {num_prefix}. '
                    for wrapped in _wrap_panel_text(f"{prefix}{choice}", inner_text_width, subsequent_indent="    "):
                        choice_wrapped.append((i, wrapped))
                # Trailing Other row(s)
                other_idx = len(choices)
                other_num = other_idx + 1
                if other_num < 10:
                    other_num_prefix = str(other_num)
                elif other_num == 10:
                    other_num_prefix = '0'
                else:
                    other_num_prefix = ' '
                if selected == other_idx and not cli_ref._clarify_freetext:
                    other_label_mand = f'❯ {other_num_prefix}. Other (type your answer)'
                elif cli_ref._clarify_freetext:
                    other_label_mand = f'❯ {other_num_prefix}. Other (type below)'
                else:
                    other_label_mand = f'  {other_num_prefix}. Other (type your answer)'
                other_wrapped = _wrap_panel_text(other_label_mand, inner_text_width, subsequent_indent="    ")
            elif cli_ref._clarify_freetext:
                # Freetext-only mode: the guidance line takes the place of choices.
                other_wrapped = _wrap_panel_text(
                    "Type your answer in the prompt below, then press Enter.",
                    inner_text_width,
                )
            else:
                other_wrapped = []

            # Budget the question so mandatory rows always render.
            # Chrome layouts:
            #   full : top border + blank_after_title + blank_after_question
            #          + blank_before_bottom + bottom border = 5 rows
            #   tight: top border + bottom border = 2 rows (drop all blanks)
            #
            # reserved_below matches the approval-panel budget (~6 rows for
            # spinner/tool-progress + status + input + separators + prompt).
            term_rows = shutil.get_terminal_size((100, 24)).lines
            chrome_full = 5
            chrome_tight = 2
            reserved_below = 6

            available = max(0, term_rows - reserved_below)
            # The compact decision must reserve room for at least one question
            # row on top of the choices, otherwise full chrome (3 blank
            # separators) gets kept when there is no room for it and the panel
            # overflows the viewport — HSplit then clips the panel's tail,
            # silently dropping the choices (the reported bug).
            mandatory_full = chrome_full + 1 + len(choice_wrapped) + len(other_wrapped)

            use_compact_chrome = mandatory_full > available
            chrome_rows = chrome_tight if use_compact_chrome else chrome_full

            max_question_rows = max(1, available - chrome_rows - len(choice_wrapped) - len(other_wrapped))
            max_question_rows = min(max_question_rows, 12)  # soft cap on huge terminals

            # When the choices alone (plus compact chrome) already exceed the
            # viewport, drop the question entirely — the choices are the only
            # thing the user must see to make a selection. Without this the
            # question would still claim its 1-row floor above and push the
            # tail of the choices off-screen (HSplit clips the overflow).
            choices_overflow = chrome_rows + len(choice_wrapped) + len(other_wrapped) >= available
            if choices_overflow:
                max_question_rows = 0

            question_wrapped = _wrap_panel_text(question, inner_text_width)
            if max_question_rows <= 0:
                question_wrapped = []
            elif len(question_wrapped) > max_question_rows:
                # The truncation marker is itself a row, so it must count
                # against the budget. With a 1-row budget there is no room for
                # both a question line and the marker — show the marker alone
                # so the rendered question never exceeds max_question_rows.
                keep = max(0, max_question_rows - 1)
                question_wrapped = question_wrapped[:keep] + ["… (question truncated)"]

            lines = []
            # Box top border
            lines.append(('class:clarify-border', '╭─ '))
            lines.append(('class:clarify-title', 'opencodon needs your input'))
            lines.append(('class:clarify-border', ' ' + ('─' * max(0, box_width - len("opencodon needs your input") - 3)) + '╮\n'))
            if not use_compact_chrome:
                _append_blank_panel_line(lines, 'class:clarify-border', box_width)

            # Question text (bounded)
            for wrapped in question_wrapped:
                _append_panel_line(lines, 'class:clarify-border', 'class:clarify-question', wrapped, box_width)
            if not use_compact_chrome:
                _append_blank_panel_line(lines, 'class:clarify-border', box_width)

            if cli_ref._clarify_freetext and not choices:
                for wrapped in other_wrapped:
                    _append_panel_line(lines, 'class:clarify-border', 'class:clarify-choice', wrapped, box_width)
                if not use_compact_chrome:
                    _append_blank_panel_line(lines, 'class:clarify-border', box_width)

            if choices:
                # Multiple-choice mode: show selectable options
                for i, wrapped in choice_wrapped:
                    style = 'class:clarify-selected' if i == selected and not cli_ref._clarify_freetext else 'class:clarify-choice'
                    _append_panel_line(lines, 'class:clarify-border', style, wrapped, box_width)

                # "Other" option (trailing row(s), only shown when choices exist)
                other_idx = len(choices)
                # Calculate number prefix for "Other" option
                other_num = other_idx + 1
                if other_num < 10:
                    other_num_prefix = str(other_num)
                elif other_num == 10:
                    other_num_prefix = '0'
                else:
                    other_num_prefix = ' '
                
                if selected == other_idx and not cli_ref._clarify_freetext:
                    other_style = 'class:clarify-selected'
                elif cli_ref._clarify_freetext:
                    other_style = 'class:clarify-active-other'
                else:
                    other_style = 'class:clarify-choice'
                for wrapped in other_wrapped:
                    _append_panel_line(lines, 'class:clarify-border', other_style, wrapped, box_width)

            if not use_compact_chrome:
                _append_blank_panel_line(lines, 'class:clarify-border', box_width)
            lines.append(('class:clarify-border', '╰' + ('─' * box_width) + '╯\n'))
            return lines

        clarify_widget = ConditionalContainer(
            Window(
                FormattedTextControl(_get_clarify_display),
                wrap_lines=True,
            ),
            filter=Condition(lambda: cli_ref._clarify_state is not None),
        )

        # --- Sudo password: display widget ---

        def _get_sudo_display():
            state = cli_ref._sudo_state
            if not state:
                return []
            title = '🔐 Sudo Password Required'
            body = 'Enter password below (hidden), or press Enter to skip'
            box_width = _panel_box_width(title, [body])
            lines = []
            lines.append(('class:sudo-border', '╭─ '))
            lines.append(('class:sudo-title', title))
            lines.append(('class:sudo-border', ' ' + ('─' * max(0, box_width - len(title) - 3)) + '╮\n'))
            _append_blank_panel_line(lines, 'class:sudo-border', box_width)
            _append_panel_line(lines, 'class:sudo-border', 'class:sudo-text', body, box_width)
            _append_blank_panel_line(lines, 'class:sudo-border', box_width)
            lines.append(('class:sudo-border', '╰' + ('─' * box_width) + '╯\n'))
            return lines

        sudo_widget = ConditionalContainer(
            Window(
                FormattedTextControl(_get_sudo_display),
                wrap_lines=True,
            ),
            filter=Condition(lambda: cli_ref._sudo_state is not None),
        )

        def _get_secret_display():
            state = cli_ref._secret_state
            if not state:
                return []

            title = '🔑 Skill Setup Required'
            prompt = state.get("prompt") or f"Enter value for {state.get('var_name', 'secret')}"
            metadata = state.get("metadata") or {}
            help_text = metadata.get("help")
            body = 'Enter secret below (hidden), ESC or Ctrl+C to skip'
            content_lines = [prompt, body]
            if help_text:
                content_lines.insert(1, str(help_text))
            box_width = _panel_box_width(title, content_lines)
            lines = []
            lines.append(('class:sudo-border', '╭─ '))
            lines.append(('class:sudo-title', title))
            lines.append(('class:sudo-border', ' ' + ('─' * max(0, box_width - len(title) - 3)) + '╮\n'))
            _append_blank_panel_line(lines, 'class:sudo-border', box_width)
            _append_panel_line(lines, 'class:sudo-border', 'class:sudo-text', prompt, box_width)
            if help_text:
                _append_panel_line(lines, 'class:sudo-border', 'class:sudo-text', str(help_text), box_width)
            _append_blank_panel_line(lines, 'class:sudo-border', box_width)
            _append_panel_line(lines, 'class:sudo-border', 'class:sudo-text', body, box_width)
            _append_blank_panel_line(lines, 'class:sudo-border', box_width)
            lines.append(('class:sudo-border', '╰' + ('─' * box_width) + '╯\n'))
            return lines

        secret_widget = ConditionalContainer(
            Window(
                FormattedTextControl(_get_secret_display),
                wrap_lines=True,
            ),
            filter=Condition(lambda: cli_ref._secret_state is not None),
        )

        # --- Dangerous command approval: display widget ---

        def _get_approval_display():
            return cli_ref._get_approval_display_fragments()

        approval_widget = ConditionalContainer(
            Window(
                FormattedTextControl(_get_approval_display),
                wrap_lines=True,
            ),
            filter=Condition(lambda: cli_ref._approval_state is not None),
        )

        def _get_slash_confirm_display():
            return cli_ref._get_slash_confirm_display_fragments()

        slash_confirm_widget = ConditionalContainer(
            Window(
                FormattedTextControl(_get_slash_confirm_display),
                wrap_lines=True,
            ),
            filter=Condition(lambda: cli_ref._slash_confirm_state is not None),
        )

        # --- /model picker: display widget ---
        def _get_model_picker_display():
            state = cli_ref._model_picker_state
            if not state:
                return []
            stage = state.get("stage", "provider")
            if stage == "provider":
                title = "⚙ Model Picker — Select Provider"
                choices = []
                _providers = state.get("providers")
                for p in _providers if isinstance(_providers, list) else []:
                    count = p.get("total_models", len(p.get("models", [])))
                    label = f"{p['name']} ({count} model{'s' if count != 1 else ''})"
                    if p.get("is_current"):
                        label += "  ← current"
                    choices.append(label)
                choices.append("Cancel")
                hint = f"Current: {state.get('current_model', 'unknown')} on {state.get('current_provider', 'unknown')}"
            else:
                provider_data = state.get("provider_data") or {}
                model_list = state.get("model_list") or []
                title = f"⚙ Model Picker — {provider_data.get('name', provider_data.get('slug', 'Provider'))}"
                choices = list(model_list) + ["← Back", "Cancel"]
                if model_list:
                    hint = f"Select a model ({len(model_list)} available)"
                else:
                    hint = "No models listed for this provider. Use Back or Cancel."

            box_width = _panel_box_width(title, [hint] + choices, min_width=46, max_width=84)
            inner_text_width = max(8, box_width - 6)
            selected = state.get("selected", 0)

            # Scrolling viewport: the panel renders into a Window with no max
            # height, so without limiting visible items the bottom border and
            # any items past the available terminal rows get clipped on long
            # provider catalogs (e.g. Ollama Cloud's 36+ models).
            try:
                from prompt_toolkit.application import get_app
                term_rows = get_app().output.get_size().rows
            except Exception:
                term_rows = shutil.get_terminal_size((100, 24)).lines
            scroll_offset, visible = OpencodonCLI._compute_model_picker_viewport(
                selected, state.get("_scroll_offset", 0), len(choices), term_rows,
            )
            state["_scroll_offset"] = scroll_offset

            lines = []
            lines.append(('class:clarify-border', '╭─ '))
            lines.append(('class:clarify-title', title))
            lines.append(('class:clarify-border', ' ' + ('─' * max(0, box_width - len(title) - 3)) + '╮\n'))
            _append_blank_panel_line(lines, 'class:clarify-border', box_width)
            _append_panel_line(lines, 'class:clarify-border', 'class:clarify-hint', hint, box_width)
            _append_blank_panel_line(lines, 'class:clarify-border', box_width)
            for idx in range(scroll_offset, scroll_offset + visible):
                choice = choices[idx]
                style = 'class:clarify-selected' if idx == selected else 'class:clarify-choice'
                prefix = '❯ ' if idx == selected else '  '
                for wrapped in _wrap_panel_text(prefix + choice, inner_text_width, subsequent_indent='  '):
                    _append_panel_line(lines, 'class:clarify-border', style, wrapped, box_width)
            _append_blank_panel_line(lines, 'class:clarify-border', box_width)
            lines.append(('class:clarify-border', '╰' + ('─' * box_width) + '╯\n'))
            return lines

        model_picker_widget = ConditionalContainer(
            Window(
                FormattedTextControl(_get_model_picker_display),
                wrap_lines=True,
            ),
            filter=Condition(lambda: cli_ref._model_picker_state is not None),
        )

        # Horizontal rules above and below the input.
        # On narrow/mobile terminals we keep the top separator for structure but
        # hide the bottom one to recover a full row for conversation content.
        input_rule_top = Window(
            char='─',
            height=lambda: cli_ref._tui_input_rule_height("top"),
            style='class:input-rule',
        )
        input_rule_bot = Window(
            char='─',
            height=lambda: cli_ref._tui_input_rule_height("bottom"),
            style='class:input-rule',
        )

        # Image attachment indicator — shows badges like [📎 Image #1] above input
        cli_ref = self

        def _get_image_bar():
            if not cli_ref._attached_images:
                return []
            badges = _format_image_attachment_badges(
                cli_ref._attached_images,
                cli_ref._image_counter,
            )
            return [("class:image-badge", f" {badges} ")]

        image_bar = Window(
            content=FormattedTextControl(_get_image_bar),
            height=Condition(lambda: bool(cli_ref._attached_images)),
        )

        # Persistent voice mode status bar (visible only when voice mode is on)
        def _get_voice_status():
            return cli_ref._get_voice_status_fragments()

        voice_status_bar = ConditionalContainer(
            Window(
                FormattedTextControl(_get_voice_status),
                height=1,
            ),
            filter=Condition(lambda: cli_ref._voice_mode),
        )

        status_bar = ConditionalContainer(
            Window(
                content=FormattedTextControl(lambda: cli_ref._get_status_bar_fragments()),
                height=1,
                # Prevent fragments that overflow the terminal width from
                # wrapping onto a second line, which causes the status bar to
                # appear duplicated (one full + one partial row) during long
                # sessions, especially on SSH where shutil.get_terminal_size
                # may return stale values.  _get_status_bar_fragments now reads
                # width from prompt_toolkit's own output object, so fragments
                # will always fit; wrap_lines=False is the belt-and-suspenders
                # guard against any future width mismatch.
                wrap_lines=False,
            ),
            filter=Condition(
                lambda: cli_ref._status_bar_visible
                and not getattr(cli_ref, "_status_bar_suppressed_after_resize", False)
            ),
        )

        # Allow wrapper CLIs to register extra keybindings.
        self._register_extra_tui_keybindings(kb, input_area=input_area)

        # Layout: interactive prompt widgets + ruled input at bottom.
        # The sudo, approval, and clarify widgets appear above the input when
        # the corresponding interactive prompt is active.
        completions_menu = CompletionsMenu(max_height=12, scroll_offset=1)

        layout = Layout(
            HSplit(
                self._build_tui_layout_children(
                    sudo_widget=sudo_widget,
                    secret_widget=secret_widget,
                    approval_widget=approval_widget,
                    slash_confirm_widget=slash_confirm_widget,
                    clarify_widget=clarify_widget,
                    model_picker_widget=model_picker_widget,
                    spinner_widget=spinner_widget,
                    spacer=spacer,
                    status_bar=status_bar,
                    input_rule_top=input_rule_top,
                    image_bar=image_bar,
                    input_area=input_area,
                    input_rule_bot=input_rule_bot,
                    voice_status_bar=voice_status_bar,
                    completions_menu=completions_menu,
                )
            )
        )
        
        # Style for the application
        self._tui_style_base = {
            # Input area / prompt: empty style strings inherit the
            # terminal's default foreground/background, so the typed
            # text is readable in both light and dark Terminal.app
            # color schemes.  (Hardcoding a near-white #FFF8DC made
            # input invisible on light backgrounds.)
            'input-area': '',
            'placeholder': '#888888 italic',
            'prompt': '',
            'prompt-working': '#888888 italic',
            'hint': '#888888 italic',
            'status-bar': 'bg:#1a1a2e #C0C0C0',
            'status-bar-strong': 'bg:#1a1a2e #FFD700 bold',
            'status-bar-dim': 'bg:#1a1a2e #8B8682',
            'status-bar-good': 'bg:#1a1a2e #8FBC8F bold',
            'status-bar-warn': 'bg:#1a1a2e #FFD700 bold',
            'status-bar-bad': 'bg:#1a1a2e #FF8C00 bold',
            'status-bar-critical': 'bg:#1a1a2e #FF6B6B bold',
            'status-bar-yolo': 'bg:#1a1a2e #FF4444 bold',
            # Bronze horizontal rules around the input area
            'input-rule': '#CD7F32',
            # Clipboard image attachment badges
            'image-badge': '#87CEEB bold',
            'completion-menu': 'bg:#1a1a2e #FFF8DC',
            'completion-menu.completion': 'bg:#1a1a2e #FFF8DC',
            'completion-menu.completion.current': 'bg:#333355 #FFD700',
            'completion-menu.meta.completion': 'bg:#1a1a2e #888888',
            'completion-menu.meta.completion.current': 'bg:#333355 #FFBF00',
            # Clarify question panel
            'clarify-border': '#CD7F32',
            'clarify-title': '#FFD700 bold',
            'clarify-question': '#FFF8DC bold',
            'clarify-choice': '#AAAAAA',
            'clarify-selected': '#FFD700 bold',
            'clarify-active-other': '#FFD700 italic',
            'clarify-countdown': '#CD7F32',
            # Sudo password panel
            'sudo-prompt': '#FF6B6B bold',
            'sudo-border': '#CD7F32',
            'sudo-title': '#FF6B6B bold',
            'sudo-text': '#FFF8DC',
            # Dangerous command approval panel
            'approval-border': '#CD7F32',
            'approval-title': '#FF8C00 bold',
            'approval-desc': '#FFF8DC bold',
            'approval-cmd': '#AAAAAA italic',
            'approval-choice': '#AAAAAA',
            'approval-selected': '#FFD700 bold',
            # Voice mode
            'voice-prompt': '#87CEEB',
            'voice-recording': '#FF4444 bold',
            'voice-processing': '#FFA500 italic',
            'voice-status': 'bg:#1a1a2e #87CEEB',
            'voice-status-recording': 'bg:#1a1a2e #FF4444 bold',
        }
        style = PTStyle.from_dict(self._build_tui_style_dict())

        # Select CPR-disabled output when _terminal_may_leak_cpr() says so
        # (POSIX local + SSH; Windows keeps PT default — see helper docs).
        # None falls back to prompt_toolkit's default output; input scrubbing
        # in _strip_leaked_terminal_responses still guards residual leaks.
        _cpr_disabled_output = _select_classic_cli_pt_output(sys.stdout)

        # Create the application
        app = Application(
            layout=layout,
            key_bindings=kb,
            style=style,
            full_screen=False,
            mouse_support=False,
            **({"output": _cpr_disabled_output} if _cpr_disabled_output is not None else {}),
            # Read from display.cli_refresh_interval (default 0 = disabled).
            # When non-zero, prompt_toolkit redraws the UI on this cadence
            # during idle, keeping wall-clock status-bar read-outs ticking.
            # Set to 0 to suppress background redraws entirely — avoids
            # fighting terminal auto-scroll in non-fullscreen mode (Xshell,
            # iTerm2, Windows Terminal). See #48309.
            refresh_interval=float(CLI_CONFIG.get("display", {}).get("cli_refresh_interval", 0)),
            # Erase the live bottom chrome (status bar, input box, separator
            # rules) on exit instead of freezing a final copy into scrollback.
            # Without this, prompt_toolkit's render_as_done teardown repaints
            # the chrome one last time and leaves it stranded above the exit
            # summary — so a dead status bar + empty prompt sit between the
            # conversation transcript and the "Resume this session" block, and
            # stack with the next session's UI on resume (#38252). The actual
            # conversation transcript is printed through patch_stdout into
            # normal scrollback and is unaffected; only the managed chrome is
            # erased. Applies to every exit path (/exit, /quit, EOF, Ctrl+C).
            erase_when_done=True,
            **({'cursor': _STEADY_CURSOR} if _STEADY_CURSOR is not None else {}),
        )
        _disable_prompt_toolkit_cpr_warning(app)
        self._app = app  # Store reference for clarify_callback

        # ── Fix ghost status-bar lines on terminal resize ──────────────
        # Resize handling: monkey-patch prompt_toolkit's _output_screen_diff
        # to suppress the deliberate "reserve vertical space" scroll-up.
        #
        # Background: prompt_toolkit's renderer (renderer.py L232-242)
        # explicitly moves the cursor to the bottom of the canvas after
        # painting "to make sure the terminal scrolls up, even when the
        # lower lines of the canvas just contain whitespace".  In
        # non-fullscreen mode this scrolls chrome content (status bar,
        # input rules) into terminal scrollback on every render.  When
        # the terminal column-shrinks, the emulator reflows the previously
        # rendered full-width rows into multiple narrower rows that get
        # pushed up — leaving ghost duplicates AND polluting scrollback.
        # Same issue as pt #29 (open since 2014), #1675, #1933.
        #
        # Surgical fix: wrap _output_screen_diff so that when its internal
        # `if current_height > previous_screen.height` branch fires (the
        # one that does the bottom-cursor-move), we make it fall through
        # by inflating previous_screen.height first.
        try:
            import prompt_toolkit.renderer as _pt_renderer
            from prompt_toolkit.renderer import _output_screen_diff as _orig_osd

            if not getattr(_pt_renderer, "_opencodon_osd_patched", False):
                def _patched_output_screen_diff(
                    app, output, screen, current_pos, color_depth,
                    previous_screen, last_style, is_done, full_screen,
                    attrs_for_style_string, style_string_has_style,
                    size, previous_width,
                ):
                    """Wraps pt's _output_screen_diff to suppress the
                    reserve-vertical-space scroll (renderer.py L232-242).

                    Strategy: ONLY when previous_screen is non-None and
                    its current height is genuinely smaller than the new
                    screen's height, inflate it to match.  This prevents
                    the bottom-cursor-move at L242 without changing any
                    other code path's behavior.

                    Critical: do NOT replace a None previous_screen with
                    a fresh Screen() — that would skip the proper
                    reset_attributes()+erase_down() at L178-185 which
                    fires when previous_screen is None (first-paint /
                    width-change).  Without that reset, ANSI styles
                    leak between renders.
                    """
                    try:
                        if previous_screen is not None and hasattr(previous_screen, "height"):
                            if previous_screen.height < screen.height:
                                previous_screen.height = screen.height
                    except Exception:
                        pass

                    return _orig_osd(
                        app, output, screen, current_pos, color_depth,
                        previous_screen, last_style, is_done, full_screen,
                        attrs_for_style_string, style_string_has_style,
                        size, previous_width,
                    )

                _pt_renderer._output_screen_diff = _patched_output_screen_diff
                _pt_renderer._opencodon_osd_patched = True
        except Exception:
            pass

        # Apply bracketed-paste timeout recovery so torn ESC[201~ end marks
        # don't permanently freeze the input (issue #16263). Idempotent.
        _apply_bracketed_paste_timeout_patch()

        _original_on_resize = app._on_resize

        def _resize_clear_ghosts():
            self._schedule_resize_recovery(app, _original_on_resize)

        app._on_resize = _resize_clear_ghosts

        def spinner_loop():
            while not self._should_exit:
                if not self._app:
                    time.sleep(0.1)
                    continue
                if self._command_running:
                    self._invalidate(min_interval=0.1)
                    time.sleep(0.1)
                else:
                    # Do not repaint the idle prompt every second. In non-full-screen
                    # prompt_toolkit mode, background redraws can fight tmux/Ghostty/cmux
                    # viewport restoration after focus changes and visually move the
                    # command input area. Keep idle stable; input/agent events still
                    # invalidate explicitly when the UI actually changes.
                    time.sleep(0.2)

        spinner_thread = threading.Thread(target=spinner_loop, daemon=True)
        spinner_thread.start()
        
        # Background thread to process inputs and run agent
        def process_loop():
            while not self._should_exit:
                try:
                    # Check for pending input with timeout
                    try:
                        user_input = self._pending_input.get(timeout=0.1)
                    except queue.Empty:
                        # Periodic config watcher — auto-reload MCP on mcp_servers change
                        if not self._agent_running:
                            self._check_config_mcp_changes()
                            # Check for background process notifications (completions
                            # and watch pattern matches) while agent is idle.
                            try:
                                self._drain_process_notifications("cli-idle")
                            except Exception:
                                pass
                        continue
                    
                    if not user_input:
                        continue

                    # The user has typed and submitted something, so any
                    # post-resize transient suppression should end here.
                    self._status_bar_suppressed_after_resize = False

                    # Unpack image payload: (text, [Path, ...]) or plain str
                    submit_images = []
                    if isinstance(user_input, tuple):
                        user_input, submit_images = user_input

                    if isinstance(user_input, str):
                        user_input = _strip_leaked_bracketed_paste_wrappers(user_input)
                        user_input, _had_mouse_reports = _strip_leaked_terminal_responses_with_meta(user_input)
                        if _had_mouse_reports:
                            self._recover_terminal_input_modes(reason="mouse reports leaked into submitted input")
                    
                    # Check for commands — but detect dragged/pasted file paths first.
                    # See _detect_file_drop() for details.
                    _file_drop = _detect_file_drop(user_input) if isinstance(user_input, str) else None
                    if _file_drop:
                        _drop_path = _file_drop["path"]
                        _remainder = _file_drop["remainder"]
                        if _file_drop["is_image"]:
                            submit_images.append(_drop_path)
                            user_input = _remainder or f"[User attached image: {_drop_path.name}]"
                            _cprint(f"  📎 Auto-attached image: {_drop_path.name}")
                        else:
                            _cprint(f"  📄 Detected file: {_drop_path.name}")
                            user_input = (
                                f"[User attached file: {_drop_path}]"
                                + (f"\n{_remainder}" if _remainder else "")
                            )

                    # A bare number right after a bare `/resume` prompt selects
                    # that session (see #34584). Checked before chat routing so
                    # the digit isn't sent to the agent as a message.
                    if (
                        not _file_drop
                        and self._pending_resume_sessions
                        and isinstance(user_input, str)
                        and self._consume_pending_resume_selection(user_input)
                    ):
                        continue

                    if not _file_drop and isinstance(user_input, str) and _looks_like_slash_command(user_input):
                        _cprint(f"\n⚙️  {user_input}")
                        try:
                            if not self.process_command(user_input):
                                self._should_exit = True
                                # Schedule app exit
                                if app.is_running:
                                    app.exit()
                        except KeyboardInterrupt:
                            # Ctrl+C during a slow slash command (e.g. /skills browse,
                            # /sessions list with a large DB) should interrupt the
                            # command and return to the prompt, NOT exit the entire
                            # session. Without this guard a KeyboardInterrupt unwinds
                            # to the outer prompt_toolkit loop and the session dies.
                            _cprint("\n[dim]Command interrupted.[/dim]")
                            continue
                        # A slash handler may set a one-shot pending seed (e.g.
                        # /blueprint <name>) to be run as the next agent turn.
                        # If present, fall through to the chat path with the seed
                        # as the user message instead of looping back to idle.
                        _seed = getattr(self, "_pending_agent_seed", None)
                        if _seed:
                            self._pending_agent_seed = None
                            user_input = _seed
                        else:
                            continue
                    
                    # Expand paste references back to full content
                    _paste_ref_re = re.compile(r'\[Pasted text #\d+: \d+ lines \u2192 (.+?)\]')
                    paste_refs = list(_paste_ref_re.finditer(user_input)) if isinstance(user_input, str) else []
                    if paste_refs:
                        user_input = self._expand_paste_references(user_input)
                    print()
                    self._print_user_message_preview(user_input)
                    
                    # Show image attachment count
                    if submit_images:
                        n = len(submit_images)
                        _cprint(f"  {_DIM}📎 {n} image{'s' if n > 1 else ''} attached{_RST}")

                    # Regular chat - run agent
                    self._agent_running = True
                    app.invalidate()  # Refresh status line

                    try:
                        self.chat(user_input, images=submit_images or None)
                    finally:
                        self._agent_running = False
                        self._spinner_text = ""
                        self._tool_start_time = 0.0
                        self._pending_tool_info.clear()
                        self._last_scrollback_tool = ""

                        app.invalidate()  # Refresh status line

                        # Post-turn terminal recovery (#33271): after an
                        # interrupt the prompt_toolkit renderer may have
                        # drifted from the physical terminal state — CSI 6n
                        # cursor position reports can leak as literal text
                        # (^[[19;1R), and the VT100 input parser can stall in
                        # a partial-escape state, accepting no further
                        # keystrokes.  Drain stray escape bytes from the OS
                        # input buffer and force a clean renderer redraw.
                        if self._last_turn_interrupted:
                            self._recover_terminal_after_interrupt()

                        # Re-queue any messages that arrived in _interrupt_queue
                        # while the agent was running and were never claimed by
                        # the explicit interrupt path. See
                        # _drain_interrupt_queue_to_pending_input for the full
                        # rationale. Regression of #17666 / #18760 — the drain
                        # block from the original PR #17939 was deferred as
                        # "worth its own review" and never re-landed (#20271).
                        self._drain_interrupt_queue_to_pending_input()

                        # Goal continuation: if a standing goal is active, ask
                        # the judge whether the turn satisfied it. If not, and
                        # there's no real user message already queued, push the
                        # continuation prompt back into _pending_input so the
                        # next loop iteration picks it up naturally (and any
                        # user input that arrives in between still preempts).
                        try:
                            self._maybe_continue_goal_after_turn()
                        except Exception as _goal_exc:
                            logging.debug("goal continuation hook failed: %s", _goal_exc)

                        # Continuous voice: auto-restart recording after agent responds.
                        # Dispatch to a daemon thread so play_beep (sd.wait) and
                        # AudioRecorder.start (lock acquire) never block process_loop —
                        # otherwise queued user input would stall silently.
                        if self._voice_mode and self._voice_continuous and not self._voice_recording:
                            def _restart_recording():
                                try:
                                    if self._voice_tts:
                                        self._voice_tts_done.wait(timeout=60)
                                        time.sleep(0.3)
                                    # A barge-in capture already owns the mic and
                                    # will submit the interruption itself.
                                    if self._voice_barge_capture.is_set():
                                        return
                                    self._voice_start_recording()
                                    app.invalidate()
                                except Exception as e:
                                    _cprint(f"{_DIM}Voice auto-restart failed: {e}{_RST}")
                            threading.Thread(target=_restart_recording, daemon=True).start()

                        # Drain process notifications (completions + watch matches)
                        # that arrived while the agent was running.
                        try:
                            self._drain_process_notifications("cli-post-turn")
                        except Exception:
                            pass  # Non-fatal — don't break the main loop

                except Exception as e:
                    logger.warning("process_loop unhandled error (msg may be lost): %s", e)
        
        # Start processing thread
        process_thread = threading.Thread(target=process_loop, daemon=True)
        process_thread.start()
        
        # Register atexit cleanup so resources are freed even on unexpected exit
        atexit.register(_run_cleanup)
        
        # Register signal handlers for graceful shutdown on SSH disconnect / SIGTERM
        def _signal_handler(signum, frame):
            self._repl_signal_handler(signum, frame)
        
        try:
            import signal as _signal
            _signal.signal(_signal.SIGTERM, _signal_handler)
            if hasattr(_signal, 'SIGHUP'):
                _signal.signal(_signal.SIGHUP, _signal_handler)

            # Windows: install a SIGINT handler that absorbs the signal
            # instead of letting Python's default handler raise
            # KeyboardInterrupt in MainThread. Windows Terminal / Win32
            # delivers spurious CTRL_C_EVENT to the opencodon process when
            # child processes are spawned from background threads (agent
            # subprocess Popen path). The default Python SIGINT handler
            # would then unwind prompt_toolkit's app.run(), trigger
            # _run_cleanup mid-turn, and close browser sessions mid-open
            # — causing "Daemon process exited during startup" errors.
            #
            # The handler is a silent no-op. Real user Ctrl+C still works
            # because prompt_toolkit binds c-c at the TUI layer and never
            # reaches this OS-signal path. This matches how Claude Code
            # handles the same Windows quirk (cancellation is driven by
            # the TUI key handler, not by OS signals).
            #
            # POSIX: leave the default SIGINT handler alone. prompt_toolkit
            # installs its own handler there and it works as expected.
            if sys.platform == "win32":
                def _sigint_absorb(signum, frame):
                    # Absorb silently. Do NOT call agent.interrupt() here:
                    # Windows fires spurious CTRL_C_EVENT whenever a
                    # background thread spawns a .cmd subprocess, and
                    # interrupt() would inject a fake user message each
                    # time. Real user Ctrl+C routes through prompt_toolkit's
                    # own c-c key binding at the TUI layer (same pattern as
                    # Claude Code's Windows handling).
                    return
                _signal.signal(_signal.SIGINT, _sigint_absorb)
        except Exception:
            pass  # Signal handlers may fail in restricted environments
        
        # Install a custom asyncio exception handler that suppresses the
        # "Event loop is closed" RuntimeError from httpx transport cleanup
        # and the "0 is not registered" KeyError from broken stdin (#6393).
        # The RuntimeError fix is defense-in-depth — the primary fix is
        # neuter_async_httpx_del which disables __del__ entirely.  The
        # KeyError fix handles macOS + uv-managed Python environments where
        # fd 0 is not reliably available to the asyncio selector.
        def _suppress_closed_loop_errors(loop, context):
            exc = context.get("exception")
            if isinstance(exc, RuntimeError) and "Event loop is closed" in str(exc):
                return  # silently suppress
            if isinstance(exc, KeyError) and "is not registered" in str(exc):
                return  # suppress selector registration failures (#6393)
            if isinstance(exc, OSError) and getattr(exc, "errno", None) == errno.EIO:
                return  # suppress I/O errors from broken stdout on interrupt (#13710)
            # Fall back to default handler for everything else
            loop.default_exception_handler(context)

        # Validate stdin before launching prompt_toolkit — on macOS with
        # uv-managed Python, fd 0 can be invalid or unregisterable with the
        # asyncio selector, causing "KeyError: '0 is not registered'" (#6393).
        try:
            os.fstat(0)
        except OSError:
            print(
                "Error: stdin (fd 0) is not available.\n"
                "This can happen with certain Python installations (e.g. uv-managed cPython on macOS).\n"
                "Try reinstalling Python via pyenv or Homebrew, then re-run: opencodon setup"
            )
            _run_cleanup()
            self._print_exit_summary()
            return

        # On macOS with uv-managed Python, kqueue's selector cannot register
        # fd 0, raising OSError(EINVAL) from kqueue.control() when prompt_toolkit
        # calls loop.add_reader (#6393). Probe kqueue and, if it can't watch
        # stdin, switch to a SelectSelector-backed event loop policy.
        if sys.platform == "darwin":
            try:
                import selectors as _selectors
                if hasattr(_selectors, "KqueueSelector"):
                    _kq = _selectors.KqueueSelector()
                    try:
                        _kq.register(0, _selectors.EVENT_READ)
                        _kq.unregister(0)
                    finally:
                        _kq.close()
            except (OSError, ValueError, KeyError):
                import asyncio as _aio_probe
                import selectors as _selectors

                class _SelectEventLoopPolicy(_aio_probe.DefaultEventLoopPolicy):
                    def new_event_loop(self):
                        return _aio_probe.SelectorEventLoop(_selectors.SelectSelector())

                _aio_probe.set_event_loop_policy(_SelectEventLoopPolicy())

        # Run the application with patch_stdout for proper output handling
        try:
            with patch_stdout():
                # Set the custom handler on prompt_toolkit's event loop
                try:
                    import asyncio as _aio
                    # Use get_running_loop() to avoid DeprecationWarning on
                    # Python 3.10+ when called outside an async context.
                    _loop = _aio.get_running_loop()
                    _loop.set_exception_handler(_suppress_closed_loop_errors)
                except RuntimeError:
                    pass  # No running loop -- nothing to patch
                except Exception:
                    pass
                # The app enables focus reporting + mouse tracking; record that
                # so _run_cleanup resets them on exit (#36823).
                _mark_tui_input_modes_active()
                app.run()
        except (EOFError, KeyboardInterrupt, BrokenPipeError):
            pass
        except (KeyError, OSError) as _stdin_err:
            # Catch selector registration failures from broken stdin (#6393)
            # and I/O errors from broken stdout during interrupt (#13710).
            _errno = getattr(_stdin_err, "errno", None) if isinstance(_stdin_err, OSError) else None
            _msg = str(_stdin_err)
            if _errno == errno.EIO:
                pass  # suppress broken-stdout I/O errors on interrupt (#13710)
            elif (
                _errno in {errno.EINVAL, errno.EBADF}
                or "is not registered" in _msg
                or "Bad file descriptor" in _msg
                or "Invalid argument" in _msg
            ):
                print(
                    f"\nError: stdin is not usable ({_stdin_err}).\n"
                    "This can happen with certain Python installations (e.g. uv-managed cPython on macOS)\n"
                    "where kqueue cannot register fd 0.\n"
                    "Try reinstalling Python via pyenv or Homebrew, then re-run: opencodon setup"
                )
            else:
                raise
        finally:
            self._should_exit = True
            # Immediate feedback: prompt_toolkit has just torn down the input
            # box + status bar, so without a line here the terminal sits
            # silent for the whole cleanup window (session flush, memory
            # shutdown, MCP/browser/terminal teardown) and the exit looks
            # hung. Print before any potentially-slow step.
            try:
                print(f"{_DIM}Shutting down… (finalizing session){_RST}", flush=True)
            except Exception:
                pass
            # Interrupt the agent immediately so its daemon thread stops making
            # API calls and exits promptly (agent_thread is daemon, so the
            # process will exit once the main thread finishes, but interrupting
            # avoids wasted API calls and lets run_conversation clean up).
            if self.agent and getattr(self, '_agent_running', False):
                try:
                    self.agent.interrupt()
                except Exception:
                    pass
            # Shut down voice recorder (release persistent audio stream)
            if hasattr(self, '_voice_recorder') and self._voice_recorder:
                try:
                    self._voice_recorder.shutdown()
                except Exception:
                    pass
                self._voice_recorder = None
            # Clean up old temp voice recordings
            try:
                from opencodon.tools.voice_mode import cleanup_temp_recordings
                cleanup_temp_recordings()
            except Exception:
                pass
            # Unregister callbacks to avoid dangling references
            set_sudo_password_callback(None)
            set_approval_callback(None)
            set_secret_capture_callback(None)
            # Flush any in-memory turn transcript before marking the session
            # closed.  On SIGHUP/SIGTERM/window close the agent thread may not
            # reach its normal run_conversation() persistence path before the
            # daemon thread is reaped.
            self._persist_active_session_before_close()

            # Close session in SQLite
            if hasattr(self, '_session_db') and self._session_db and self.agent:
                try:
                    self._session_db.end_session(self.agent.session_id, "cli_close")
                except (Exception, KeyboardInterrupt) as e:
                    logger.debug("Could not close session in DB: %s", e)
                # Started-and-immediately-quit sessions never gained content;
                # drop the empty row so /resume and `opencodon sessions list`
                # stay clean (gemini-cli#27770 port). No-op for resumed or
                # titled sessions and anything with messages or children.
                if not getattr(self, '_delete_session_on_exit', False):
                    try:
                        self._discard_session_if_empty(self.agent.session_id)
                    except (Exception, KeyboardInterrupt) as e:
                        logger.debug("Could not prune empty session: %s", e)
                # /exit --delete: also remove the current session's transcripts
                # and SQLite history. Ported from google-gemini/gemini-cli#19332.
                if getattr(self, '_delete_session_on_exit', False):
                    try:
                        from opencodon_constants import get_opencodon_home as _ghh
                        _sessions_dir = _ghh() / "sessions"
                        _sid = self.agent.session_id
                        if self._session_db.delete_session(_sid, sessions_dir=_sessions_dir):
                            _cprint(f"  {_DIM}✓ Session {_escape(_sid)} deleted{_RST}")
                        else:
                            _cprint(f"  {_DIM}✗ Session {_escape(_sid)} not found for deletion{_RST}")
                    except (Exception, KeyboardInterrupt) as e:
                        logger.debug("Could not delete session on exit: %s", e)
            # Plugin hook: on_session_end — safety net for interrupted exits.
            # run_conversation() already fires this per-turn on normal completion,
            # so only fire here if the agent was mid-turn (_agent_running) when
            # the exit occurred, meaning run_conversation's hook didn't fire.
            if self.agent and getattr(self, '_agent_running', False):
                try:
                    from opencodon.plugins_runtime import invoke_hook as _invoke_hook
                    _invoke_hook(
                        "on_session_end",
                        session_id=self.agent.session_id,
                        completed=False,
                        interrupted=True,
                        model=getattr(self.agent, 'model', None),
                        platform=getattr(self.agent, 'platform', None) or "cli",
                        reason="shutdown",
                    )
                except Exception:
                    pass
            _run_cleanup()
            self._print_exit_summary()
            self._release_active_session()

        # Deferred relaunch: /update sets _pending_relaunch so the exec
        # happens here — after prompt_toolkit has exited and fully restored
        # terminal modes — rather than from the background process_loop
        # thread (which would skip terminal cleanup on POSIX and only exit
        # the worker thread on Windows).
        if getattr(self, '_pending_relaunch', None):
            from opencodon.frontends.cli.relaunch import relaunch
            relaunch(self._pending_relaunch, preserve_inherited=False)

    def _repl_signal_handler(self, signum, frame):
        """Verbatim from run()'s _signal_handler closure: graceful shutdown on SIGHUP/SIGTERM."""
        """Handle SIGHUP/SIGTERM by triggering graceful cleanup.

        Calls ``self.agent.interrupt()`` first so the agent daemon
        thread's poll loop sees the per-thread interrupt and kills the
        tool's subprocess group via ``_kill_process`` (os.killpg).
        Without this, the main thread dies from KeyboardInterrupt and
        the daemon thread is killed with it — before it can run one
        more poll iteration to clean up the subprocess, which was
        spawned with ``os.setsid`` and therefore survives as an orphan
        with PPID=1.

        Grace window (``OPENCODON_SIGTERM_GRACE``, default 1.5 s) gives
        the daemon time to: detect the interrupt (next 200 ms poll) →
        call _kill_process (SIGTERM + 1 s wait + SIGKILL if needed) →
        return from _wait_for_process.  ``time.sleep`` releases the
        GIL so the daemon actually runs during the window.

        Guarded ``logger.debug``: CPython's ``logging`` module is not
        reentrant-safe.  ``Logger.isEnabledFor`` caches level results
        in ``Logger._cache``; under shutdown races the cache can be
        cleared (``_clear_cache``) or mid-mutation when the signal
        fires, raising ``KeyError: <level_int>`` (e.g. ``KeyError: 10``
        for DEBUG) inside the handler.  That KeyError then escapes
        before ``raise KeyboardInterrupt()`` can fire, which bypasses
        prompt_toolkit's normal interrupt unwind and surfaces as the
        EIO cascade from issue #13710.  Wrap the log in a bare
        ``try/except`` so the handler can never raise through it.
        """
        try:
            logger.debug("Received signal %s, triggering graceful shutdown", signum)
        except Exception:
            pass  # never let logging raise from a signal handler (#13710 regression)
        # Shutdown intent is now unambiguous — arm the exit backstop
        # IMMEDIATELY, before the graceful unwind below.  If any step of
        # that unwind wedges (main thread parked in a syscall, prompt_toolkit
        # teardown never returning), _run_cleanup never runs and would
        # never arm its own watchdog — leaving a "dead" CLI alive for
        # minutes (#65998 class).  Never raises.
        _arm_exit_watchdog_on_shutdown_signal()
        try:
            if getattr(self, "agent", None) and getattr(self, "_agent_running", False):
                self.agent.interrupt(f"received signal {signum}")
                try:
                    _grace = float(os.getenv("OPENCODON_SIGTERM_GRACE", "1.5"))
                except (TypeError, ValueError):
                    _grace = 1.5
                if _grace > 0:
                    time.sleep(_grace)
        except Exception:
            pass  # never block signal handling
        # Prefer a clean prompt_toolkit exit over `raise KeyboardInterrupt()`.
        # Raising KBI from a signal handler unwinds into whatever Python
        # frame the interpreter happens to be running — typically an
        # `await asyncio.sleep()` inside prompt_toolkit's
        # `_poll_output_size` coroutine.  The KBI becomes a Task
        # exception, prompt_toolkit's `_handle_exception` prints
        # "Unhandled exception in event loop" + the full traceback, and
        # parks the terminal on "Press ENTER to continue..." (#13710
        # variant — same root cause, different surface).
        #
        # `app.exit()` scheduled via `call_soon_threadsafe` lets the
        # event loop unwind normally; `app.run()` returns and our
        # existing `except (EOFError, KeyboardInterrupt, BrokenPipeError)`
        # block at the bottom of the input loop handles the rest.
        try:
            from prompt_toolkit.application.current import get_app_or_none
            _app = get_app_or_none()
            if _app is not None:
                _loop = getattr(_app, "loop", None)
                if _loop is not None:
                    _loop.call_soon_threadsafe(_app.exit)
                    return  # clean unwind — no traceback, no ENTER pause
        except Exception:
            pass
        raise KeyboardInterrupt()  # fallback for non-prompt_toolkit contexts

    def _repl_handle_enter(self, event):
        """Verbatim from run()'s handle_enter closure: route Enter by active UI state."""
        """Handle Enter key - submit input.

        Routes to the correct queue based on active UI state:
        - Sudo password prompt: password goes to sudo response queue
        - Approval selection: selected choice goes to approval response queue
        - Clarify freetext mode: answer goes to the clarify response queue
        - Clarify choice mode: selected choice goes to the clarify response queue
        - Agent running: goes to _interrupt_queue (chat() monitors this)
        - Agent idle: goes to _pending_input (process_loop monitors this)
        Commands (starting with /) always go to _pending_input so they're
        handled as commands, not sent as interrupt text to the agent.
        """
        # --- Sudo password prompt: submit the typed password ---
        if self._sudo_state:
            text = event.app.current_buffer.text
            self._sudo_state["response_queue"].put(text)
            self._sudo_state = None
            event.app.invalidate()
            return

        # --- Secret prompt: submit the typed secret ---
        if self._secret_state:
            text = event.app.current_buffer.text
            self._submit_secret_response(text)
            event.app.current_buffer.reset()
            event.app.invalidate()
            return

        # --- Approval selection: confirm the highlighted choice ---
        if self._approval_state:
            self._handle_approval_selection()
            event.app.invalidate()
            return

        # --- Slash-command confirmation: submit typed or highlighted choice ---
        if self._slash_confirm_state:
            text = event.app.current_buffer.text.strip()
            choices = self._slash_confirm_state.get("choices") or []
            choice = self._normalize_slash_confirm_choice(text, choices) if text else None
            if choice is None:
                selected = self._slash_confirm_state.get("selected", 0)
                if 0 <= selected < len(choices):
                    choice = choices[selected][0]
            self._submit_slash_confirm_response(choice or "cancel")
            event.app.current_buffer.reset()
            event.app.invalidate()
            return

        # --- /model picker modal ---
        if self._model_picker_state:
            try:
                # Picker selections follow the same session-scoped default
                # as /model <name>; honour model.persist_switch_by_default.
                from opencodon.frontends.cli.model_switch import resolve_persist_behavior

                self._handle_model_picker_selection(
                    persist_global=resolve_persist_behavior(False, False)
                )
            except Exception as _exc:
                _cprint(f"  ✗ Model selection failed: {_exc}")
                self._close_model_picker()
            event.app.current_buffer.reset()
            event.app.invalidate()
            return

        # --- Clarify freetext mode: user typed their own answer ---
        if self._clarify_freetext and self._clarify_state:
            text = event.app.current_buffer.text.strip()
            if text:
                self._clarify_state["response_queue"].put(text)
                self._clarify_state = None
                self._clarify_freetext = False
                event.app.current_buffer.reset()
                event.app.invalidate()
            return

        # --- Clarify choice mode: confirm the highlighted selection ---
        if self._clarify_state and not self._clarify_freetext:
            state = self._clarify_state
            selected = state["selected"]
            choices = state.get("choices") or []
            if selected < len(choices):
                state["response_queue"].put(choices[selected])
                self._clarify_state = None
                event.app.invalidate()
            else:
                # "Other" selected → switch to freetext
                self._clarify_freetext = True
                event.app.invalidate()
            return

        # --- Normal input routing ---
        text = event.app.current_buffer.text.strip()
        has_images = bool(self._attached_images)
        if text or has_images:
            # Handle /model directly on the UI thread so interactive pickers
            # can safely use prompt_toolkit terminal handoff helpers.
            if self._should_handle_model_command_inline(text, has_images=has_images):
                if not self.process_command(text):
                    self._should_exit = True
                    if event.app.is_running:
                        event.app.exit()
                event.app.current_buffer.reset(append_to_history=True)
                # Force a repaint: process_command() prints through
                # patch_stdout (scrolls output above the prompt) and never
                # invalidates the app, so the just-cleared input area can
                # keep showing the submitted text until some unrelated
                # redraw fires. Every other early-return branch in this
                # handler invalidates after reset — match them.
                event.app.invalidate()
                return

            # Handle /steer while the agent is running immediately on the
            # UI thread.  Queuing through _pending_input would deadlock the
            # steer until after the agent loop finishes (process_loop is
            # blocked inside self.chat()), which turns /steer into a
            # post-run next-turn message — defeating mid-run injection.
            # agent.steer() is thread-safe (holds _pending_steer_lock).
            if self._should_handle_steer_command_inline(text, has_images=has_images):
                self.process_command(text)
                event.app.current_buffer.reset(append_to_history=True)
                # Force a repaint after clearing the buffer.  /steer is
                # dispatched mid-run while the agent streams output through
                # patch_stdout; process_command() never invalidates the
                # app, so without this the submitted "/steer <text>" can
                # linger in the input area (looking unsent) and invite an
                # accidental re-submit. See issue #34569.
                event.app.invalidate()
                return

            # Snapshot and clear attached images
            images = list(self._attached_images)
            self._attached_images.clear()
            event.app.invalidate()
            # Bundle text + images as a tuple when images are present
            payload = (text, images) if images else text
            if self._agent_running and not (text and _looks_like_slash_command(text)):
                _effective_mode = self.busy_input_mode
                redirected = False
                if _effective_mode == "steer":
                    # Route Enter through /steer — inject mid-run after the
                    # next tool call.  Images can't ride along (steer only
                    # appends text), so fall back to queue when images are
                    # attached.  If the agent lacks steer() or rejects the
                    # payload, also fall back to queue so nothing is lost.
                    if images or not text:
                        _effective_mode = "queue"
                    else:
                        accepted = False
                        try:
                            if self.agent is not None and hasattr(self.agent, "steer"):
                                accepted = bool(self.agent.steer(text))
                        except Exception as exc:
                            _cprint(f"  {_DIM}Steer failed ({exc}) — queued for next turn.{_RST}")
                            accepted = False
                        if accepted:
                            preview = text[:80] + ("..." if len(text) > 80 else "")
                            _cprint(f"  {_ACCENT}⏩ Steered: '{preview}'{_RST}")
                        else:
                            _effective_mode = "queue"
                if _effective_mode == "queue":
                    # Queue for the next turn instead of interrupting
                    self._pending_input.put(payload)
                    preview = text if text else f"[{len(images)} image{'s' if len(images) != 1 else ''} attached]"
                    _cprint(f"  Queued for the next turn: {preview[:80]}{'...' if len(preview) > 80 else ''}")
                elif _effective_mode == "interrupt":
                    if not images and text:
                        try:
                            if (
                                self.agent is not None
                                and getattr(
                                    self.agent,
                                    "_supports_active_turn_redirect",
                                    False,
                                )
                                is True
                                and hasattr(self.agent, "redirect")
                            ):
                                redirected = bool(self.agent.redirect(text))
                        except Exception:
                            redirected = False
                    if redirected:
                        preview = text[:80] + ("..." if len(text) > 80 else "")
                        _cprint(f"  {_ACCENT}↪ Redirected current turn: '{preview}'{_RST}")
                    else:
                        # Compatibility path for older agents, multimodal
                        # follow-ups, or a turn that finished in the race.
                        self._interrupt_queue.put(payload)
                        try:
                            _dbg = _opencodon_home / "interrupt_debug.log"
                            with open(_dbg, "a", encoding="utf-8") as _f:
                                _f.write(f"{time.strftime('%H:%M:%S')} ENTER: queued interrupt msg={str(payload)[:60]!r}, "
                                         f"agent_running={self._agent_running}\n")
                        except Exception:
                            pass
                # First-touch onboarding: on the very first busy-while-running
                # event for this install, print a one-line tip explaining the
                # /busy knob.  Flag persists to config.yaml and never fires
                # again.  Guarded for exceptions so onboarding can't break
                # the input loop.
                try:
                    from opencodon.core.onboarding import (
                        BUSY_INPUT_FLAG,
                        busy_input_hint_cli,
                        is_seen,
                        mark_seen,
                    )
                    if not is_seen(CLI_CONFIG, BUSY_INPUT_FLAG):
                        _hint_mode = "redirect" if redirected else _effective_mode
                        _cprint(f"  {_DIM}{busy_input_hint_cli(_hint_mode)}{_RST}")
                        mark_seen(_opencodon_home / "config.yaml", BUSY_INPUT_FLAG)
                        CLI_CONFIG.setdefault("onboarding", {}).setdefault("seen", {})[BUSY_INPUT_FLAG] = True
                except Exception:
                    pass
            else:
                self._pending_input.put(payload)
            # History stores real pasted content, not the placeholder, so
            # up-arrow recall restores the actual text.
            self._inline_pastes(event.app.current_buffer)
            event.app.current_buffer.reset(append_to_history=True)



# ============================================================================
# Main Entry Point
# ============================================================================

def main(
    query: str = None,
    q: str = None,
    image: str = None,
    toolsets: str = None,
    skills: str | list[str] | tuple[str, ...] = None,
    model: str = None,
    provider: str = None,
    api_key: str = None,
    base_url: str = None,
    max_turns: int = None,
    verbose: Optional[bool] = None,
    quiet: bool = False,
    compact: bool = False,
    list_tools: bool = False,
    list_toolsets: bool = False,
    gateway: bool = False,
    resume: str = None,
    worktree: bool = False,
    w: bool = False,
    checkpoints: bool = False,
    pass_session_id: bool = False,
    ignore_user_config: bool = False,
    ignore_rules: bool = False,
):
    """
    opencodon CLI - Interactive AI Assistant
    
    Args:
        query: Single query to execute (then exit). Alias: -q
        q: Shorthand for --query
        image: Optional local image path to attach to a single query
        toolsets: Comma-separated list of toolsets to enable (e.g., "web,terminal")
        skills: Comma-separated or repeated list of skills to preload for the session
        model: Model to use (default: anthropic/claude-opus-4-20250514)
        api_key: API key for authentication
        base_url: Base URL for the API
        max_turns: Maximum tool-calling iterations (default: 60)
        verbose: Enable verbose logging
        compact: Use compact display mode
        list_tools: List available tools and exit
        list_toolsets: List available toolsets and exit
        resume: Resume a previous session by its ID (e.g., 20260225_143052_a1b2c3)
        worktree: Run in an isolated git worktree (for parallel agents). Alias: -w
        w: Shorthand for --worktree
    
    Examples:
        python cli.py                            # Start interactive mode
        python cli.py --toolsets web,terminal    # Use specific toolsets
        python cli.py --skills opencodon-dev,github-auth
        python cli.py -q "What is Python?"       # Single query mode
        python cli.py -q "Describe this" --image ~/storage/shared/Pictures/cat.png
        python cli.py --list-tools               # List tools and exit
        python cli.py --resume 20260225_143052_a1b2c3  # Resume session
        python cli.py -w                         # Start in isolated git worktree
        python cli.py -w -q "Fix issue #123"     # Single query in worktree
    """
    global _active_worktree

    # Force UTF-8 stdio on Windows before any banner/print() runs — the
    # Rich console prints Unicode box-drawing characters that would
    # UnicodeEncodeError on cp1252.  No-op on Linux/macOS.
    try:
        from opencodon.frontends.cli.stdio import configure_windows_stdio
        configure_windows_stdio()
    except Exception:
        pass

    # Signal to terminal_tool that we're in interactive mode
    # This enables interactive sudo password prompts with timeout
    os.environ["OPENCODON_INTERACTIVE"] = "1"
    
    # Handle gateway mode (messaging + cron)
    if gateway:
        import asyncio
        from opencodon.frontends.gateway.run import start_gateway
        print("Starting opencodon Gateway (messaging platforms)...")
        asyncio.run(start_gateway())
        return

    # Skip worktree for list commands (they exit immediately)
    if not list_tools and not list_toolsets:
        # ── Git worktree isolation (#652) ──
        # Create an isolated worktree so this agent instance doesn't collide
        # with other agents working on the same repo.
        use_worktree = worktree or w or CLI_CONFIG.get("worktree", False)
        wt_info = None
        if use_worktree:
            # Prune stale worktrees from crashed/killed sessions
            _repo = _git_repo_root()
            if _repo:
                _prune_stale_worktrees(_repo)
            # Branch the worktree from the freshly-fetched remote tip by
            # default so it starts current with the project. Opt out with
            # worktree_sync: false to branch from local HEAD instead.
            _sync_base = CLI_CONFIG.get("worktree_sync", True)
            wt_info = _setup_worktree(sync_base=_sync_base)
            if wt_info:
                _active_worktree = wt_info
                os.environ["TERMINAL_CWD"] = wt_info["path"]
                atexit.register(_cleanup_worktree, wt_info)
            else:
                # Worktree was explicitly requested but setup failed —
                # don't silently run without isolation.
                return
    else:
        wt_info = None
    
    # Handle query shorthand
    query = query or q
    
    # Parse toolsets - handle both string and tuple/list inputs
    # Default to opencodon-cli toolset which includes cronjob management tools
    toolsets_list = None
    if toolsets:
        if isinstance(toolsets, str):
            toolsets_list = [t.strip() for t in toolsets.split(",")]
        elif isinstance(toolsets, (list, tuple)):
            # Fire may pass multiple --toolsets as a tuple
            toolsets_list = []
            for t in toolsets:
                if isinstance(t, str):
                    toolsets_list.extend([x.strip() for x in t.split(",")])
                else:
                    toolsets_list.append(str(t))
    else:
        # Coding posture (base opencodon): with no explicit --toolsets, collapse
        # to the coding toolset (+ enabled MCP servers) when sitting in a code
        # workspace. See agent/coding_context.py.
        _coding = None
        try:
            from opencodon.core.context.coding_context import coding_selection
            _coding = coding_selection(platform="cli", config=CLI_CONFIG)
        except Exception:
            _coding = None
        if _coding is not None:
            toolsets_list = _coding
        else:
            # Use the shared resolver so MCP servers are included at runtime
            from opencodon.frontends.cli.tools_config import _get_platform_tools
            toolsets_list = sorted(_get_platform_tools(CLI_CONFIG, "cli"))
    
    parsed_skills = _parse_skills_argument(skills)

    # Create CLI instance
    cli = OpencodonCLI(
        model=model,
        toolsets=toolsets_list,
        provider=provider,
        api_key=api_key,
        base_url=base_url,
        max_turns=max_turns,
        verbose=verbose,
        compact=compact,
        resume=resume,
        checkpoints=checkpoints,
        pass_session_id=pass_session_id,
        ignore_rules=ignore_rules,
    )

    if parsed_skills:
        skills_prompt, loaded_skills, missing_skills = build_preloaded_skills_prompt(
            parsed_skills,
            task_id=cli.session_id,
        )
        if missing_skills:
            missing_display = ", ".join(missing_skills)
            # If at least one skill loaded, degrade gracefully: skip the
            # unknown ones and continue. A typo'd skill name should not crash
            # the run. Only when EVERY requested skill is missing do we
            # hard-fail, so a fully-misconfigured run fails loudly instead
            # of running blind.
            if loaded_skills:
                logger.warning(
                    "Unknown skill(s) requested, skipping: %s. "
                    "Continuing with: %s. "
                    "List available skills with `opencodon skills list`.",
                    missing_display,
                    ", ".join(loaded_skills),
                )
            else:
                raise ValueError(f"Unknown skill(s): {missing_display}")
        if skills_prompt:
            cli.system_prompt = "\n\n".join(
                part for part in (cli.system_prompt, skills_prompt) if part
            ).strip()
            cli.preloaded_skills = loaded_skills

    # Inject worktree context into agent's system prompt
    if wt_info:
        wt_note = (
            f"\n\n[System note: You are working in an isolated git worktree at "
            f"{wt_info['path']}. Your branch is `{wt_info['branch']}`. "
            f"Changes here do not affect the main working tree or other agents. "
            f"Remember to commit and push your changes, and create a PR if appropriate. "
            f"The original repo is at {wt_info['repo_root']}.]"
        )
        cli.system_prompt = (cli.system_prompt or "") + wt_note
    
    # Handle list commands (don't init agent for these)
    if list_tools:
        cli.show_banner()
        cli.show_tools()
        sys.exit(0)
    
    if list_toolsets:
        cli.show_banner()
        cli.show_toolsets()
        sys.exit(0)
    
    # Register cleanup for single-query mode (interactive mode registers in run())
    atexit.register(_run_cleanup)

    # Also install signal handlers in single-query / `-q` mode.  Interactive
    # mode registers its own inside OpencodonCLI.run(), but `-q` runs
    # cli.agent.run_conversation() below and AIAgent spawns worker threads
    # for tools — so when SIGTERM arrives on the main thread, raising
    # KeyboardInterrupt only unwinds the main thread, not the worker
    # running _wait_for_process.  Python then exits, the child subprocess
    # (spawned with os.setsid, its own process group) is reparented to
    # init and keeps running as an orphan.
    #
    # Fix: route SIGTERM/SIGHUP through agent.interrupt() which sets the
    # per-thread interrupt flag the worker's poll loop checks every 200 ms.
    # Give the worker a grace window to call _kill_process (SIGTERM to the
    # process group, then SIGKILL after 1 s), then raise KeyboardInterrupt
    # so main unwinds normally.  OPENCODON_SIGTERM_GRACE overrides the 1.5 s
    # default for debugging.
    def _signal_handler_q(signum, frame):
        logger.debug("Received signal %s in single-query mode", signum)
        # Arm the exit backstop now that shutdown intent is unambiguous —
        # covers wedges in the unwind below that would otherwise leave the
        # process alive with no watchdog (#65998 class). Never raises.
        _arm_exit_watchdog_on_shutdown_signal()
        try:
            _agent = getattr(cli, "agent", None)
            if _agent is not None:
                _agent.interrupt(f"received signal {signum}")
                try:
                    _grace = float(os.getenv("OPENCODON_SIGTERM_GRACE", "1.5"))
                except (TypeError, ValueError):
                    _grace = 1.5
                if _grace > 0:
                    time.sleep(_grace)
        except Exception:
            pass  # never block signal handling
        raise KeyboardInterrupt()
    try:
        import signal as _signal
        _signal.signal(_signal.SIGINT, _signal_handler_q)
        _signal.signal(_signal.SIGTERM, _signal_handler_q)
        if hasattr(_signal, "SIGHUP"):
            _signal.signal(_signal.SIGHUP, _signal_handler_q)
    except Exception:
        pass  # signal handler may fail in restricted environments
    
    # Handle single query mode
    if query or image:
        if not cli._claim_active_session("cli", stderr=bool(quiet)):
            sys.exit(1)
        try:
            query, single_query_images = _collect_query_images(query, image)
            if quiet:
                # Quiet mode: suppress banner, spinner, tool previews.
                # Only print the final response and parseable session info.
                cli.tool_progress_mode = "off"
                if cli._ensure_runtime_credentials():
                    effective_query: Any = query
                    if single_query_images:
                        # Honour the same image-routing decision used by the
                        # interactive path. With a vision-capable model (incl.
                        # custom-provider models declared via
                        # `model.supports_vision: true`), attach images natively
                        # as image_url content parts. Otherwise fall back to the
                        # text-pipeline (vision_analyze pre-description).
                        _img_mode = "text"
                        _build_parts = None
                        try:
                            from opencodon.core.media.image_routing import (
                                build_native_content_parts as _build_parts,  # noqa: F811
                            )
                            from opencodon.core.media.image_routing import decide_image_input_mode
                            from opencodon.config import load_config

                            _img_mode = decide_image_input_mode(
                                (cli.provider or "").strip(),
                                (cli.model or "").strip(),
                                load_config(),
                            )
                        except Exception:
                            _img_mode = "text"

                        if _img_mode == "native" and _build_parts is not None:
                            try:
                                _parts, _skipped = _build_parts(
                                    query if isinstance(query, str) else "",
                                    [str(p) for p in single_query_images],
                                )
                                if any(p.get("type") == "image_url" for p in _parts):
                                    effective_query = _parts
                                else:
                                    # All images unreadable — text fallback.
                                    effective_query = cli._preprocess_images_with_vision(
                                        query, single_query_images, announce=False,
                                    )
                            except Exception:
                                effective_query = cli._preprocess_images_with_vision(
                                    query, single_query_images, announce=False,
                                )
                        else:
                            effective_query = cli._preprocess_images_with_vision(
                                query,
                                single_query_images,
                                announce=False,
                            )
                    turn_route = cli._resolve_turn_agent_config(effective_query)
                    if turn_route["signature"] != cli._active_agent_route_signature:
                        cli.agent = None
                    if cli._init_agent(
                        model_override=turn_route["model"],
                        runtime_override=turn_route["runtime"],
                        request_overrides=turn_route.get("request_overrides"),
                    ):
                        cli.agent.quiet_mode = True
                        cli.agent.suppress_status_output = True
                        # Suppress streaming display callbacks so stdout stays
                        # machine-readable (no styled "opencodon" box, no tool-gen
                        # status lines).  The response is printed once below.
                        cli.agent.stream_delta_callback = None
                        cli.agent.tool_gen_callback = None
                        try:
                            result = cli.agent.run_conversation(
                                user_message=effective_query,
                                conversation_history=cli.conversation_history,
                            )
                        except KeyboardInterrupt:
                            _emit_interrupted_session_end(cli, reason="keyboard_interrupt")
                            print(f"\nsession_id: {cli.session_id}", file=sys.stderr)
                            sys.exit(130)
                        # Sync session_id if mid-run compression created a
                        # continuation session. The exit line below reports
                        # session_id to stderr for automation wrappers; without
                        # this sync it would point at the ended parent.
                        if (
                            getattr(cli.agent, "session_id", None)
                            and cli.agent.session_id != cli.session_id
                        ):
                            cli.session_id = cli.agent.session_id
                        response = result.get("final_response", "") if isinstance(result, dict) else str(result)
                        # Surface backend errors that produced no visible output
                        # (e.g. invalid model slug → provider 4xx). Mirrors the
                        # interactive CLI path. Write to stderr so piped stdout
                        # stays clean for automation wrappers.
                        if (
                            not response
                            and isinstance(result, dict)
                            and result.get("error")
                            and (result.get("failed") or result.get("partial"))
                        ):
                            print(f"Error: {result['error']}", file=sys.stderr)
                        elif response:
                            print(response)

                        # Session ID goes to stderr so piped stdout is clean.
                        print(f"\nsession_id: {cli.session_id}", file=sys.stderr)

                        # Ensure proper exit code for automation wrappers.
                        _exit_code = 0
                        if isinstance(result, dict) and result.get("failed"):
                            _exit_code = 1
                        sys.exit(_exit_code)

                # Exit with error code if credentials or agent init fails
                sys.exit(1)
            else:
                # Single-query mode (`opencodon chat -q "…"`): skip the welcome
                # banner. Building the banner takes ~420 ms on cold start —
                # ~200 ms of that is the version-update check, the rest is
                # toolset / skill enumeration and Rich panel rendering. None
                # of that is useful for a one-shot query: the user already
                # picked the prompt, doesn't need a toolset reference, and
                # gets the session ID + resume hint from
                # ``_print_exit_summary()`` after the response prints.
                #
                # The fully-quiet ``-Q`` / ``--quiet`` machine-readable path
                # above was already banner-free; this brings the human-
                # facing single-query path in line so all non-interactive
                # invocations are fast.
                _query_label = query or ("[image attached]" if single_query_images else "")
                if _query_label:
                    cli.console.print(f"[bold blue]Query:[/] {_query_label}")
                # Surface security advisories before the agent runs — short
                # banner, doesn't depend on the welcome banner being shown.
                cli._show_security_advisories()
                cli.chat(query, images=single_query_images or None)
                cli._print_exit_summary(clear_screen=False)
        finally:
            _finalize_single_query(cli)
        return
    
    # Run interactive mode
    cli.run()


if __name__ == "__main__":
    import fire

    fire.Fire(main)
