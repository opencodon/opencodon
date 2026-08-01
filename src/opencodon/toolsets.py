#!/usr/bin/env python3
"""
Toolsets Module

This module provides a flexible system for defining and managing tool aliases/toolsets.
Toolsets allow you to group tools together for specific scenarios and can be composed
from individual tools or other toolsets.

Features:
- Define custom toolsets with specific tools
- Compose toolsets from other toolsets
- Built-in common toolsets for typical use cases
- Easy extension for new toolsets
- Support for dynamic toolset resolution

Usage:
    from toolsets import get_toolset, resolve_toolset, get_all_toolsets
    
    # Get tools for a specific toolset
    tools = get_toolset("research")
    
    # Resolve a toolset to get all tool names (including from composed toolsets)
    all_tools = resolve_toolset("full_stack")
"""

from typing import List, Dict, Any, Set, Optional


# Shared tool list for CLI and all messaging platform toolsets.
# Edit this once to update all platforms simultaneously.
_OPENCODON_CORE_TOOLS = [
    # Web
    "web_search", "web_extract",
    # Terminal + process management
    "terminal", "process",
    # Desktop GUI affordances: read the embedded terminal pane, close an agent's
    # read-only terminal tab, open a URL/file in the preview pane, and focus a
    # pane (all gated on OPENCODON_DESKTOP via check_fn — hidden outside the GUI).
    "read_terminal", "close_terminal", "open_preview", "focus_pane",
    # File manipulation
    "read_file", "write_file", "patch", "search_files",
    # Vision + image generation
    "vision_analyze", "image_generate",
    # Skills
    "skills_list", "skill_view", "skill_manage",
    # Browser automation
    "browser_navigate", "browser_snapshot", "browser_click",
    "browser_type", "browser_scroll", "browser_back",
    "browser_press", "browser_get_images",
    "browser_vision", "browser_console", "browser_cdp", "browser_dialog",
    # Text-to-speech
    "text_to_speech",
    # Planning & memory
    "todo", "memory",
    # Science layer — persistent kernels, artifact lineage, reproduction.
    # On by default: opencodon is an open-science agent, so a session must be
    # able to run code in a real kernel and record provenance without the user
    # first opting into a toolset. `run_code` and `reproduce_artifact` remain
    # check_fn-gated on the jupyter stack (tools/science_tools.py) — they drop
    # out of the schema on installs that lack it rather than erroring at call
    # time, and the kernel deps lazy-install on first use (science.kernels).
    "run_code", "load_artifact", "list_artifacts",
    "artifact_lineage", "reproduce_artifact",
    # NOTE: the desktop Project tools (project_list/create/switch) are
    # deliberately NOT here. They only make sense where a GUI can follow the
    # move, so they live in the `project` toolset and are enabled solely by the
    # GUI gateway (tui_gateway/server.py::_load_enabled_toolsets) — keeping them
    # off every CLI/messaging/cron schema (narrow waist).
    # Session history search
    "session_search",
    # Clarifying questions
    "clarify",
    # Code execution + delegation
    "execute_code", "delegate_task",
    # Cronjob management
    "cronjob",
    # Computer use (macOS, gated on cua-driver being installed via check_fn)
    "computer_use",
]

# Webhook events may originate from untrusted third-party content (for example,
# public PR titles/comments). Keep the default webhook toolset intentionally
# constrained to avoid local file/system execution by prompt injection.
_OPENCODON_WEBHOOK_SAFE_TOOLS = [
    "web_search",
    "web_extract",
    "vision_analyze",
    "clarify",
]


# Core toolset definitions
# These can include individual tools or reference other toolsets
TOOLSETS = {
    # Basic toolsets - individual tool categories
    "web": {
        "description": "Web research and content extraction tools",
        "tools": ["web_search", "web_extract"],
        "includes": []  # No other toolsets included
    },
    
    "search": {
        "description": "Web search only (no content extraction/scraping)",
        "tools": ["web_search"],
        "includes": []
    },

    "x_search": {
        "description": (
            "Search X (Twitter) posts and threads via xAI's built-in "
            "x_search Responses tool. Available when xAI credentials are "
            "configured (SuperGrok OAuth or XAI_API_KEY). Off by default; "
            "enable in `opencodon tools` → X (Twitter) Search."
        ),
        "tools": ["x_search"],
        "includes": []
    },
    
    "vision": {
        "description": "Image analysis and vision tools",
        "tools": ["vision_analyze"],
        "includes": []
    },

    "video": {
        "description": "Video analysis and understanding tools (opt-in, not in default toolset)",
        "tools": ["video_analyze"],
        "includes": []
    },
    
    "image_gen": {
        "description": "Creative generation tools (images)",
        "tools": ["image_generate"],
        "includes": []
    },

    "computer_use": {
        "description": (
            "Background desktop control via cua-driver (macOS/Windows/Linux) — "
            "screenshots, mouse, keyboard, scroll, drag. Does NOT steal the "
            "user's cursor or keyboard focus. Works with any tool-capable model."
        ),
        "tools": ["computer_use"],
        "includes": []
    },

    "terminal": {
        "description": "Terminal/command execution and process management tools",
        "tools": ["terminal", "process"],
        "includes": []
    },
    
    "skills": {
        "description": "Access, create, edit, and manage skill documents with specialized instructions and knowledge",
        "tools": ["skills_list", "skill_view", "skill_manage"],
        "includes": []
    },
    
    "browser": {
        "description": "Browser automation for web interaction (navigate, click, type, scroll, iframes, hold-click) with web search for finding URLs",
        "tools": [
            "browser_navigate", "browser_snapshot", "browser_click",
            "browser_type", "browser_scroll", "browser_back",
            "browser_press", "browser_get_images",
            "browser_vision", "browser_console", "browser_cdp",
            "browser_dialog", "web_search"
        ],
        "includes": []
    },
    
    "cronjob": {
        "description": "Cronjob management tool - create, list, update, pause, resume, remove, and trigger scheduled tasks",
        "tools": ["cronjob"],
        "includes": []
    },
    

    "file": {
        "description": "File manipulation tools: read, write, patch (with fuzzy matching), and search (content + files)",
        "tools": ["read_file", "write_file", "patch", "search_files"],
        "includes": []
    },
    
    "tts": {
        "description": "Text-to-speech: convert text to audio with Edge TTS (free), ElevenLabs, OpenAI, or xAI",
        "tools": ["text_to_speech"],
        "includes": []
    },
    
    "todo": {
        "description": "Task planning and tracking for multi-step work",
        "tools": ["todo"],
        "includes": []
    },
    
    "memory": {
        "description": "Persistent memory across sessions (personal notes + user profile)",
        "tools": ["memory"],
        "includes": []
    },

    "context_engine": {
        "description": "Runtime tools exposed by the active context engine",
        "tools": [],
        "includes": []
    },
    
    "session_search": {
        "description": "Search and recall past conversations with summarization",
        "tools": ["session_search"],
        "includes": []
    },

    "science": {
        "description": "Persistent kernels + reproducible execution/artifact lineage (science layer)",
        "tools": ["run_code", "load_artifact", "list_artifacts",
                  "artifact_lineage", "reproduce_artifact"],
        "includes": []
    },

    # Opt-in, unlike the science layer above: every call here reaches the
    # public internet, so a session asks for it rather than getting it by
    # default. Set OPENCODON_SCHOLARLY_MAILTO to enter the polite rate-limit
    # pools that OpenAlex, Crossref and NCBI operate.
    "literature": {
        "description": "Scholarly search, DOI resolution and citation graph (OpenAlex, Crossref, PubMed)",
        "tools": ["literature_search", "literature_work", "literature_citations",
                  "literature_doi", "pubmed_search", "pubmed_fetch",
                  "literature_convert_ids", "preprint_search", "preprint_get",
                  "preprint_published_versions", "arxiv_search",
                  "fulltext_search", "fulltext_get"],
        "includes": []
    },

    # Opt-in for the same reason as `literature`: every call reaches the
    # public internet. Set OPENCODON_SCHOLARLY_MAILTO (and optionally
    # NCBI_API_KEY) to enter the polite rate-limit pools.
    "biodata": {
        "description": "Genes, variants and chemistry from the public biology databases (Ensembl, gnomAD, ClinVar, PubChem, ChEMBL)",
        "tools": ["gene_lookup", "gene_resolve", "gene_sequence", "gene_homologs",
                  "protein_lookup", "variant_frequency", "gene_variants",
                  "clinvar_search", "clinvar_records", "compound_lookup",
                  "compound_similarity", "drug_search", "drug_bioactivities",
                  "tissue_expression", "eqtl_genes", "encode_experiments",
                  "tf_motifs", "pdb_entry", "pdb_search", "alphafold_model",
                  "protein_domains", "interaction_network", "trial_search",
                  "trial_record", "drug_label", "drug_approvals"],
        "includes": []
    },

    "environments": {
        "description": "Durable micromamba environments with lockfile identity (science layer)",
        "tools": ["env_list", "env_create", "env_install", "env_describe",
                  "env_remove"],
        "includes": []
    },

    "project": {
        "description": "Desktop Projects — create/switch named workspaces (GUI sessions only)",
        "tools": ["project_list", "project_create", "project_switch"],
        "includes": []
    },
    
    "clarify": {
        "description": "Ask the user clarifying questions (multiple-choice or open-ended)",
        "tools": ["clarify"],
        "includes": []
    },
    
    "code_execution": {
        "description": "Run Python scripts that call tools programmatically (reduces LLM round trips)",
        "tools": ["execute_code"],
        "includes": []
    },
    
    "delegation": {
        "description": "Spawn subagents with isolated context for complex subtasks",
        "tools": ["delegate_task"],
        "includes": []
    },


    # Tools are injected via MemoryManager, not the toolset system.

    "discord": {
        "description": "Discord read and participate tools (fetch messages, search members, create threads)",
        "tools": ["discord"],
        "includes": [],
    },

    "discord_admin": {
        "description": "Discord server management (list channels/roles, pin messages, assign roles)",
        "tools": ["discord_admin"],
        "includes": [],
    },

    # Scenario-specific toolsets
    
    "debugging": {
        "description": "Debugging and troubleshooting toolkit",
        "tools": ["terminal", "process"],
        "includes": ["web", "file"]  # For searching error messages and solutions, and file operations
    },
    
    "safe": {
        "description": "Safe toolkit without terminal access",
        "tools": [],
        "includes": ["web", "vision", "image_gen"]
    },

    # Coding posture (base opencodon — CLI/TUI/desktop/ACP). Auto-selected in a
    # code workspace; see agent/coding_context.py. Keeps everything you reach
    # for while pairing on code and drops the rest (messaging, tts, image_gen,
    # home-assistant, cron, computer-use). The science layer stays — analysis
    # code in a repo is still analysis, and dropping it here would silently
    # undo the default-on science surface for anyone working inside a project.
    "coding": {
        "description": "Coding-focused toolset: files, terminal, search, web docs, skills, todo, delegate, vision, browser, science",
        "tools": [
            "web_search", "web_extract",
            "terminal", "process", "read_terminal", "close_terminal",
            "read_file", "write_file", "patch", "search_files",
            "vision_analyze",
            "skills_list", "skill_view", "skill_manage",
            "browser_navigate", "browser_snapshot", "browser_click",
            "browser_type", "browser_scroll", "browser_back",
            "browser_press", "browser_get_images",
            "browser_vision", "browser_console", "browser_cdp", "browser_dialog",
            "todo", "memory",
            "session_search", "clarify",
            "execute_code", "delegate_task",
            # Science layer (see _OPENCODON_CORE_TOOLS)
            "run_code", "load_artifact", "list_artifacts",
            "artifact_lineage", "reproduce_artifact",
        ],
        "includes": [],
        # Posture toolset: selected per-session by agent/coding_context.py,
        # never auto-recovered into per-platform tool config (see the
        # non-configurable-toolset recovery loop in opencodon_cli/tools_config.py).
        "posture": True,
    },
    
    # ==========================================================================
    # Full opencodon toolsets (CLI + messaging platforms)
    #
    # All platforms share the same core tools. Note: agents do NOT get an
    # agent-callable send_message tool — outbound platform messaging is handled
    # outside the agent loop (cron delivery and the `opencodon send` CLI),
    # not by the model deciding to send on its own.
    # ==========================================================================

    "opencodon-acp": {
        "description": "Editor integration (VS Code, Zed, JetBrains) — coding-focused tools without messaging, audio, or clarify UI",
        "tools": [
            "web_search", "web_extract",
            "terminal", "process",
            "read_file", "write_file", "patch", "search_files",
            "vision_analyze",
            "skills_list", "skill_view", "skill_manage",
            "browser_navigate", "browser_snapshot", "browser_click",
            "browser_type", "browser_scroll", "browser_back",
            "browser_press", "browser_get_images",
            "browser_vision", "browser_console", "browser_cdp", "browser_dialog",
            "todo", "memory",
            "session_search",
            "execute_code", "delegate_task",
            # Science layer (see _OPENCODON_CORE_TOOLS)
            "run_code", "load_artifact", "list_artifacts",
            "artifact_lineage", "reproduce_artifact",
        ],
        "includes": []
    },

    "opencodon-api-server": {
        "description": "OpenAI-compatible API server — full agent tools accessible via HTTP (no interactive UI tools like clarify or send_message)",
        "tools": [
            # Web
            "web_search", "web_extract",
            # Terminal + process management
            "terminal", "process",
            # File manipulation
            "read_file", "write_file", "patch", "search_files",
            # Vision + image generation
            "vision_analyze", "image_generate",
            # Skills
            "skills_list", "skill_view", "skill_manage",
            # Browser automation
            "browser_navigate", "browser_snapshot", "browser_click",
            "browser_type", "browser_scroll", "browser_back",
            "browser_press", "browser_get_images",
            "browser_vision", "browser_console", "browser_cdp", "browser_dialog",
            # Planning & memory
            "todo", "memory",
            # Session history search
            "session_search",
            # Code execution + delegation
            "execute_code", "delegate_task",
            # Cronjob management
            "cronjob",
            # Science layer (see _OPENCODON_CORE_TOOLS)
            "run_code", "load_artifact", "list_artifacts",
            "artifact_lineage", "reproduce_artifact",
        ],
        "includes": []
    },

    "opencodon-cli": {
        "description": "Full interactive CLI toolset - all default tools plus cronjob management",
        "tools": _OPENCODON_CORE_TOOLS,
        "includes": []
    },

    "opencodon-cron": {
        # Mirrors opencodon-cli so cron's "default" toolset is the same set of
        # core tools users see interactively — then `opencodon tools` filters
        # them down per the platform config. _DEFAULT_OFF_TOOLSETS (moa)
        # are excluded by _get_platform_tools() unless the user explicitly
        # enables them.
        "description": "Default cron toolset - same core tools as opencodon-cli; gated by `opencodon tools`",
        "tools": _OPENCODON_CORE_TOOLS,
        "includes": []
    },

    "opencodon-telegram": {
        "description": "Telegram bot toolset - full access for personal use (terminal has safety checks)",
        "tools": _OPENCODON_CORE_TOOLS,
        "includes": []
    },
    
    "opencodon-discord": {
        "description": "Discord bot toolset - full access (terminal has safety checks via dangerous command approval)",
        "tools": _OPENCODON_CORE_TOOLS + [
            "discord",
            "discord_admin",
        ],
        "includes": []
    },
    
    "opencodon-whatsapp": {
        "description": "WhatsApp bot toolset - similar to Telegram (personal messaging, more trusted)",
        "tools": _OPENCODON_CORE_TOOLS,
        "includes": []
    },
    
    "opencodon-slack": {
        "description": "Slack bot toolset - full access for workspace use (terminal has safety checks)",
        "tools": _OPENCODON_CORE_TOOLS,
        "includes": []
    },
    
    "opencodon-webhook": {
        "description": "Webhook toolset - receive and process external webhook events",
        "tools": _OPENCODON_WEBHOOK_SAFE_TOOLS,
        "includes": []
    },

    "opencodon-gateway": {
        "description": "Gateway toolset - union of all messaging platform tools",
        "tools": [],
        "includes": ["opencodon-telegram", "opencodon-discord", "opencodon-whatsapp", "opencodon-slack", "opencodon-webhook"]
    }
}



# Toolset IDs from the opencodon lineage; configs written before the
# opencodon rename still reference them. Removed after one release.
_LEGACY_TOOLSET_ALIASES = {
    f"opencodon-{_x}": f"opencodon-{_x}"
    for _x in (
        "telegram", "discord", "whatsapp", "slack", "webhook", "gateway",
        "acp", "api-server", "cli", "cron",
    )
}


def get_toolset(name: str, *, include_registry: bool = True) -> Optional[Dict[str, Any]]:
    """
    Get a toolset definition by name.

    Args:
        name (str): Name of the toolset
        include_registry (bool): When True (default), merge in tools that
            plugins/overlays registered into this toolset via the registry.
            When False, return only the static ``TOOLSETS`` definition (the
            composite-authored view). Platform reverse-mapping in
            ``_get_platform_tools`` uses False so that a tool registered into a
            toolset but absent from a platform's static composite does not drop
            the whole toolset from inference. See issue #49622.

    Returns:
        Dict: Toolset definition with description, tools, and includes
        None: If toolset not found. With include_registry=False the static
            view only recognizes names literally present in ``TOOLSETS``, so
            registry/MCP-only toolsets AND registry-derived aliases return None
            (they have no static counterpart).
    """
    name = _LEGACY_TOOLSET_ALIASES.get(name, name)
    toolset = TOOLSETS.get(name)

    if not include_registry:
        # Static view only: return the built-in definition (copying the nested
        # tools/includes lists so callers can't mutate TOOLSETS), or None for
        # registry/MCP-only toolsets that have no static counterpart.
        if not toolset:
            return None
        return {
            **toolset,
            "tools": list(toolset.get("tools", [])),
            "includes": list(toolset.get("includes", [])),
        }

    try:
        from opencodon.tools.registry import registry
    except Exception:
        return toolset if toolset else None

    if toolset:
        merged_tools = sorted(
            set(toolset.get("tools", []))
            | set(registry.get_tool_names_for_toolset(name))
        )
        return {**toolset, "tools": merged_tools}

    registry_toolset = name
    description = f"Plugin toolset: {name}"
    alias_target = registry.get_toolset_alias_target(name)

    if name not in _get_plugin_toolset_names():
        registry_toolset = alias_target
        if not registry_toolset:
            return None
        description = f"MCP server '{name}' tools"
    else:
        reverse_aliases = {
            canonical: alias
            for alias, canonical in _get_registry_toolset_aliases().items()
            if alias not in TOOLSETS
        }
        alias = reverse_aliases.get(name)
        if alias:
            description = f"MCP server '{alias}' tools"

    return {
        "description": description,
        "tools": registry.get_tool_names_for_toolset(registry_toolset),
        "includes": [],
    }


def bundle_non_core_tools(toolset_name: str) -> Set[str]:
    """Return a ``opencodon-*`` bundle's platform-specific tools, excluding core.

    Platform bundles are defined as ``_OPENCODON_CORE_TOOLS + [platform extras]``.
    When a bundle name appears in ``disabled_toolsets``, subtracting the whole
    bundle would strip core tools (terminal, read_file, …) shared by every
    other enabled toolset, emptying the model's tool list (#33924). This
    returns only the bundle's non-core delta (its own extras plus those of any
    one-level ``includes``), so disabling a bundle removes its platform tools
    while leaving core intact.

    Bundle nesting is one level deep in practice (only ``opencodon-gateway``
    includes other bundles, and those leaves don't nest further), so a single
    ``includes`` pass is sufficient. Unknown/garbage names fall back to the
    full resolution minus core — never re-introducing the core wipe.
    """
    core = set(_OPENCODON_CORE_TOOLS)
    ts_def = get_toolset(toolset_name)
    if not (ts_def and "tools" in ts_def):
        return set(resolve_toolset(toolset_name)) - core
    to_remove = set(ts_def["tools"]) - core
    for inc in ts_def.get("includes", []):
        inc_def = get_toolset(inc)
        if inc_def and "tools" in inc_def:
            to_remove.update(set(inc_def["tools"]) - core)
    return to_remove


def resolve_toolset(name: str, visited: Set[str] = None, *, include_registry: bool = True) -> List[str]:
    """
    Recursively resolve a toolset to get all tool names.

    This function handles toolset composition by recursively resolving
    included toolsets and combining all tools.

    Args:
        name (str): Name of the toolset to resolve
        visited (Set[str]): Set of already visited toolsets (for cycle detection)
        include_registry (bool): When True (default), include tools that
            plugins/overlays registered into a toolset. When False, resolve only
            the static ``TOOLSETS`` definition (includes are still resolved, but
            statically). Platform reverse-mapping uses False so a registry-added
            tool cannot drop the whole toolset from inference (see #49622 and
            ``_get_platform_tools``).

    Returns:
        List[str]: List of all tool names in the toolset
    """
    if visited is None:
        visited = set()

    # Special aliases that represent all tools across every toolset
    # This ensures future toolsets are automatically included without changes.
    if name in {"all", "*"}:
        all_tools: Set[str] = set()
        for toolset_name in get_toolset_names():
            # Use a fresh visited set per branch to avoid cross-branch contamination
            resolved = resolve_toolset(toolset_name, visited.copy(), include_registry=include_registry)
            all_tools.update(resolved)
        return sorted(all_tools)

    # Check for cycles / already-resolved (diamond deps).
    # Silently return [] — either this is a diamond (not a bug, tools already
    # collected via another path) or a genuine cycle (safe to skip).
    if name in visited:
        return []

    visited.add(name)

    # Get toolset definition
    toolset = get_toolset(name, include_registry=include_registry)
    if not toolset:
        # Auto-generate a toolset for plugin platforms (opencodon-<name>).
        # Gives them _OPENCODON_CORE_TOOLS plus any tools the plugin registered
        # into a toolset matching the platform name. This is a registry-derived
        # view, so it only applies when registry tools are requested; the static
        # view (include_registry=False) has no plugin-platform definition.
        if include_registry and name.startswith("opencodon-"):
            platform_name = name[len("opencodon-"):]
            try:
                from opencodon.frontends.gateway.platform_registry import platform_registry
                if platform_registry.is_registered(platform_name):
                    plugin_tools = set(_OPENCODON_CORE_TOOLS)
                    try:
                        from opencodon.tools.registry import registry
                        plugin_tools.update(
                            e.name for e in registry._tools.values()
                            if e.toolset == platform_name
                        )
                    except Exception:
                        pass
                    return list(plugin_tools)
            except Exception:
                pass

        return []

    # Collect direct tools
    tools = set(toolset.get("tools", []))

    # Recursively resolve included toolsets, sharing the visited set across
    # sibling includes so diamond dependencies are only resolved once and
    # cycle warnings don't fire multiple times for the same cycle.
    for included_name in toolset.get("includes", []):
        included_tools = resolve_toolset(included_name, visited, include_registry=include_registry)
        tools.update(included_tools)

    return sorted(tools)


def resolve_multiple_toolsets(toolset_names: List[str]) -> List[str]:
    """
    Resolve multiple toolsets and combine their tools.
    
    Args:
        toolset_names (List[str]): List of toolset names to resolve
        
    Returns:
        List[str]: Combined list of all tool names (deduplicated)
    """
    all_tools = set()
    
    for name in toolset_names:
        tools = resolve_toolset(name)
        all_tools.update(tools)
    
    return sorted(all_tools)


def _get_plugin_toolset_names() -> Set[str]:
    """Return toolset names registered by plugins (from the tool registry).

    These are toolsets that exist in the registry but not in the static
    ``TOOLSETS`` dict — i.e. they were added by plugins at load time.
    """
    try:
        from opencodon.tools.registry import registry
        return {
            toolset_name
            for toolset_name in registry.get_registered_toolset_names()
            if toolset_name not in TOOLSETS
        }
    except Exception:
        return set()


def _get_registry_toolset_aliases() -> Dict[str, str]:
    """Return explicit toolset aliases registered in the live registry."""
    try:
        from opencodon.tools.registry import registry
        return registry.get_registered_toolset_aliases()
    except Exception:
        return {}


def get_all_toolsets() -> Dict[str, Dict[str, Any]]:
    """
    Get all available toolsets with their definitions.

    Includes both statically-defined toolsets and plugin-registered ones.
    
    Returns:
        Dict: All toolset definitions
    """
    result = dict(TOOLSETS)
    aliases = _get_registry_toolset_aliases()
    for ts_name in _get_plugin_toolset_names():
        display_name = ts_name
        for alias, canonical in aliases.items():
            if canonical == ts_name and alias not in TOOLSETS:
                display_name = alias
                break
        if display_name in result:
            continue
        toolset = get_toolset(display_name)
        if toolset:
            result[display_name] = toolset
    return result


def get_toolset_names() -> List[str]:
    """
    Get names of all available toolsets (excluding aliases).

    Includes plugin-registered toolset names.
    
    Returns:
        List[str]: List of toolset names
    """
    names = set(TOOLSETS.keys())
    aliases = _get_registry_toolset_aliases()
    for ts_name in _get_plugin_toolset_names():
        for alias, canonical in aliases.items():
            if canonical == ts_name and alias not in TOOLSETS:
                names.add(alias)
                break
        else:
            names.add(ts_name)
    return sorted(names)




def validate_toolset(name: str) -> bool:
    """
    Check if a toolset name is valid.
    
    Args:
        name (str): Toolset name to validate
        
    Returns:
        bool: True if valid, False otherwise
    """
    # Accept special alias names for convenience
    if name in {"all", "*"}:
        return True
    if name in TOOLSETS:
        return True
    if name in _get_plugin_toolset_names():
        return True
    return name in _get_registry_toolset_aliases()


def create_custom_toolset(
    name: str,
    description: str,
    tools: List[str] = None,
    includes: List[str] = None
) -> None:
    """
    Create a custom toolset at runtime.
    
    Args:
        name (str): Name for the new toolset
        description (str): Description of the toolset
        tools (List[str]): Direct tools to include
        includes (List[str]): Other toolsets to include
    """
    TOOLSETS[name] = {
        "description": description,
        "tools": tools or [],
        "includes": includes or []
    }




def get_toolset_info(name: str) -> Dict[str, Any]:
    """
    Get detailed information about a toolset including resolved tools.
    
    Args:
        name (str): Toolset name
        
    Returns:
        Dict: Detailed toolset information
    """
    toolset = get_toolset(name)
    if not toolset:
        return None
    
    resolved_tools = resolve_toolset(name)
    
    return {
        "name": name,
        "description": toolset["description"],
        "direct_tools": toolset["tools"],
        "includes": toolset["includes"],
        "resolved_tools": resolved_tools,
        "tool_count": len(resolved_tools),
        "is_composite": bool(toolset["includes"])
    }




if __name__ == "__main__":
    print("Toolsets System Demo")
    print("=" * 60)
    
    print("\nAvailable Toolsets:")
    print("-" * 40)
    for name, toolset in get_all_toolsets().items():
        info = get_toolset_info(name)
        composite = "[composite]" if info["is_composite"] else "[leaf]"
        print(f"  {composite} {name:20} - {toolset['description']}")
        print(f"     Tools: {len(info['resolved_tools'])} total")
    
    print("\nToolset Resolution Examples:")
    print("-" * 40)
    for name in ["web", "terminal", "safe", "debugging"]:
        tools = resolve_toolset(name)
        print(f"\n  {name}:")
        print(f"    Resolved to {len(tools)} tools: {', '.join(sorted(tools))}")
    
    print("\nMultiple Toolset Resolution:")
    print("-" * 40)
    combined = resolve_multiple_toolsets(["web", "vision", "terminal"])
    print("  Combining ['web', 'vision', 'terminal']:")
    print(f"    Result: {', '.join(sorted(combined))}")
    
    print("\nCustom Toolset Creation:")
    print("-" * 40)
    create_custom_toolset(
        name="my_custom",
        description="My custom toolset for specific tasks",
        tools=["web_search"],
        includes=["terminal", "vision"]
    )
    custom_info = get_toolset_info("my_custom")
    print("  Created 'my_custom' toolset:")
    print(f"    Description: {custom_info['description']}")
    print(f"    Resolved tools: {', '.join(custom_info['resolved_tools'])}")
