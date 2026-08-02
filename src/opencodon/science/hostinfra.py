"""Host-service seam — the science layer's only view of the layers above it.

The science layer sits *below* core/tools in the import hierarchy
(frontends → core/tools → science → state/config → common), but a few of
its runtime paths need capabilities that only the layers above provide:
LLM completions for ``host.llm`` (core's auxiliary client), the tool
dispatcher for ``host.tool`` (tools/model_tools), the installed-skills
directory (tools/skills_hub), and the lazy dependency installer
(tools/lazy_deps).

Importing those upward would create a layering cycle, so the direction is
inverted: the tools layer registers concrete implementations here at import
time (``opencodon/tools/science_host.py``), and science code calls the
accessors below. Any entry point that can reach an accessor — cell
execution, host-bridge RPC, kernel start — is reached through the science
toolset (``tools/science_tools.py``), which imports the registration module
first, so the seam is populated before first use.

An unregistered accessor raises :class:`LookupError` naming the missing
service and the registration module; callers that can degrade gracefully
(e.g. ``kernels_available``) catch it, callers that cannot let it surface.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

_SERVICES: Dict[str, Callable[..., Any]] = {}

_KNOWN = (
    "dispatch_tool",
    "get_llm_client",
    "get_task_config",
    "resolve_task_provider_model",
    "skills_dir",
    "ensure_deps",
    "allow_lazy_installs",
)


def register_host_services(**services: Callable[..., Any]) -> None:
    """Install the upper-layer implementations. Idempotent; later wins."""
    unknown = set(services) - set(_KNOWN)
    if unknown:
        raise TypeError(f"unknown host services: {sorted(unknown)}")
    _SERVICES.update(services)


def _require(name: str) -> Callable[..., Any]:
    service = _SERVICES.get(name)
    if service is None:
        raise LookupError(
            f"science host service {name!r} is not registered — import "
            "opencodon.tools.science_host (normally pulled in by the science "
            "toolset) before using this code path"
        )
    return service


# ── Accessors (signatures mirror the registered implementations) ────


def dispatch_tool(name: str, args: dict, *, task_id: Optional[str] = None) -> Any:
    """Invoke an opencodon tool by name; returns the tool's raw result."""
    return _require("dispatch_tool")(name, args, task_id=task_id)


def get_llm_client(task: str):
    """(client, default_model) for an auxiliary task, e.g. ``science_llm``."""
    return _require("get_llm_client")(task)


def get_task_config(task: str) -> dict:
    """The ``auxiliary.<task>`` config mapping (empty dict when unset)."""
    return _require("get_task_config")(task)


def resolve_task_provider_model(task: str):
    """(provider, model, base_url, key, mode) for an auxiliary task."""
    return _require("resolve_task_provider_model")(task)


def skills_dir() -> str:
    """Path of the installed-skills tree."""
    return _require("skills_dir")()


def ensure_deps(spec: str, *, prompt: bool = False) -> None:
    """Lazy-install a dependency group (e.g. ``tool.science``); raises on failure."""
    return _require("ensure_deps")(spec, prompt=prompt)


def allow_lazy_installs() -> bool:
    """Whether lazy dependency installs are permitted by config."""
    return bool(_require("allow_lazy_installs")())
