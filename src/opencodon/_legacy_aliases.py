"""Meta-path aliases for pre-restructure module names.

The 2026-08 restructure moved every package under ``opencodon.*`` (see
docs/plans/2026-08-01-repo-restructure-plan.md). External plugins and
user scripts written against the old layout import names like ``agent.redact``
or ``opencodon_cli.config``; this finder resolves any such name to the
canonical module and aliases it in ``sys.modules`` so BOTH names refer to
one module object (state and monkeypatching stay coherent).

Installed from ``opencodon/__init__``. Remove once legacy imports are no
longer supported.
"""

import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import sys

#: legacy top-level name -> canonical dotted prefix
ALIASES = {
    "agent": "opencodon.core",
    "tools": "opencodon.tools",
    "cron": "opencodon.cron",
    "providers": "opencodon.providers",
    "gateway": "opencodon.frontends.gateway",
    "tui_gateway": "opencodon.frontends.tui",
    "acp_adapter": "opencodon.frontends.acp",
    "opencodon_cli": "opencodon.frontends.cli",
    "cli": "opencodon.frontends.cli.shell",
    "run_agent": "opencodon.core.run_agent",
    "model_tools": "opencodon.tools.model_tools",
    "opencodon_state": "opencodon.state",
    "utils": "opencodon.common.utils",
    "opencodon_constants": "opencodon.common.constants",
    "opencodon_logging": "opencodon.common.logging_setup",
    "opencodon_time": "opencodon.common.timeutils",
    "toolsets": "opencodon.toolsets",
    "mcp_serve": "opencodon.frontends.mcp",
    # legacy shim-era module renames that differ from a plain prefix swap
    "opencodon_cli.config": "opencodon.config",
    "opencodon_cli.colors": "opencodon.common.colors",
    "opencodon_cli.default_soul": "opencodon.config.default_soul",
    "opencodon_cli.route_identity": "opencodon.common.route_identity",
    "opencodon_cli.managed_scope": "opencodon.config.managed_scope",
    "opencodon_cli._subprocess_compat": "opencodon.common._subprocess_compat",
    "opencodon_cli.model_normalize": "opencodon.common.model_normalize",
    "opencodon_cli.timeouts": "opencodon.config.timeouts",
    "opencodon_cli.env_loader": "opencodon.config.env_loader",
    "opencodon_cli.moa_config": "opencodon.config.moa_config",
    "opencodon_cli.plugins": "opencodon.plugins_runtime",
    "opencodon_cli.middleware": "opencodon.plugins_runtime.middleware",
    # Phase 3b-1: auth/model/profile stack relocated out of the CLI frontend.
    "opencodon_cli.auth": "opencodon.core.auth",
    "opencodon_cli.models": "opencodon.core.models",
    "opencodon_cli.runtime_provider": "opencodon.core.runtime_provider",
    "opencodon_cli.profiles": "opencodon.core.profiles",
    "opencodon_cli.providers": "opencodon.core.providers",
    "opencodon_cli.codex_models": "opencodon.core.codex_models",
    "opencodon_cli.copilot_auth": "opencodon.core.copilot_auth",
    "opencodon_cli.model_catalog": "opencodon.core.model_catalog",
    "opencodon_cli.model_cost_guard": "opencodon.core.model_cost_guard",
    "opencodon_cli.fallback_config": "opencodon.core.fallback_config",
    "opencodon_cli.memory_oauth": "opencodon.core.memory_oauth",
    "opencodon_cli.urllib_security": "opencodon.common.urllib_security",
}


def _canonical_for(name: str):
    if name in ALIASES:
        return ALIASES[name]
    top = name.split(".", 1)[0]
    if top in ALIASES and "." in name:
        # longest-prefix match for the explicit two-level entries first
        parts = name.split(".")
        two = ".".join(parts[:2])
        if two in ALIASES:
            return ALIASES[two] + name[len(two):]
        return ALIASES[top] + name[len(top):]
    return None


class _AliasLoader(importlib.abc.Loader):
    def __init__(self, canonical: str):
        self._canonical = canonical

    def create_module(self, spec):
        # Returning an existing module makes the import system alias it
        # under the requested (legacy) name — one shared module object.
        return importlib.import_module(self._canonical)

    def exec_module(self, module):
        pass


class _LegacyAliasFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        canonical = _canonical_for(name)
        if canonical is None:
            return None
        try:
            canonical_spec = importlib.util.find_spec(canonical)
        except (ImportError, ValueError):
            return None
        if canonical_spec is None:
            return None
        spec = importlib.machinery.ModuleSpec(
            name, _AliasLoader(canonical), origin=canonical_spec.origin,
            is_package=canonical_spec.submodule_search_locations is not None,
        )
        if canonical_spec.submodule_search_locations is not None:
            spec.submodule_search_locations = list(
                canonical_spec.submodule_search_locations
            )
        return spec


def install() -> None:
    # Must PRECEDE PathFinder: an aliased parent package exposes the real
    # package's __path__, so PathFinder would happily load "agent.x" from
    # the canonical file as a DUPLICATE module. Front position costs one
    # dict probe per import.
    if not any(isinstance(f, _LegacyAliasFinder) for f in sys.meta_path):
        sys.meta_path.insert(0, _LegacyAliasFinder())
