"""Registers the core/tools-layer implementations of the science host seam.

Counterpart of ``science.hostinfra``: science sits below core/tools in the
layer stack, so instead of science importing upward, this module (tools
layer, allowed to import both core and science) hands the implementations
down at import time. Each callable defers its heavy import to call time,
preserving the lazy-import behavior the science layer had before the seam.

Imported for its side effect by ``tools/science_tools.py`` — every entry
point that can execute science code paths goes through the science toolset,
so registration always precedes first use.
"""

from __future__ import annotations

from typing import Any, Optional

from opencodon.science import hostinfra


def _dispatch_tool(name: str, args: dict, *, task_id: Optional[str] = None) -> Any:
    from opencodon.tools.model_tools import handle_function_call

    return handle_function_call(name, dict(args), task_id=task_id)


def _get_llm_client(task: str):
    from opencodon.core.auxiliary_client import get_text_auxiliary_client

    return get_text_auxiliary_client(task)


def _get_task_config(task: str) -> dict:
    from opencodon.core.auxiliary_client import _get_auxiliary_task_config

    return _get_auxiliary_task_config(task)


def _resolve_task_provider_model(task: str):
    from opencodon.core.auxiliary_client import _resolve_task_provider_model

    return _resolve_task_provider_model(task)


def _skills_dir() -> str:
    from opencodon.tools.skills_hub import _skills_dir

    return _skills_dir()


def _ensure_deps(spec: str, *, prompt: bool = False) -> None:
    from opencodon.tools.lazy_deps import ensure

    ensure(spec, prompt=prompt)


def _allow_lazy_installs() -> bool:
    from opencodon.tools.lazy_deps import _allow_lazy_installs

    return _allow_lazy_installs()


hostinfra.register_host_services(
    dispatch_tool=_dispatch_tool,
    get_llm_client=_get_llm_client,
    get_task_config=_get_task_config,
    resolve_task_provider_model=_resolve_task_provider_model,
    skills_dir=_skills_dir,
    ensure_deps=_ensure_deps,
    allow_lazy_installs=_allow_lazy_installs,
)
