"""Environment toolset — durable micromamba environments for science work.

Lets a session build an environment that outlives it: bioconda tooling, a
pinned numpy, an R stack. The reason this is worth a toolset rather than a
shell command is the identity — each environment exports a lockfile hash that
travels into ``execution_log``, which is what lets ``reproduce_artifact``
claim a result is *verified* rather than merely byte-identical.
"""

import json

from tools.registry import registry


def _env():
    from science import envmanager

    return envmanager


def _call(fn, **kwargs) -> str:
    from science.envmanager import EnvError

    try:
        result = fn(**kwargs)
        if hasattr(result, "as_dict"):
            result = result.as_dict()
        return json.dumps(result, ensure_ascii=False, default=str)
    except EnvError as exc:
        return json.dumps({"error": str(exc), "source": "micromamba"})
    except Exception as exc:
        return json.dumps({"error": f"{type(exc).__name__}: {exc}"})


registry.register(
    name="env_list",
    toolset="environments",
    schema={
        "name": "env_list",
        "description": "List durable science environments available to run_code.",
        "parameters": {"type": "object", "properties": {}},
    },
    handler=lambda args, **kw: _call(lambda: {"environments": _env().list_envs()}),
)

registry.register(
    name="env_create",
    toolset="environments",
    schema={
        "name": "env_create",
        "description": (
            "Create a durable micromamba environment from conda-forge and "
            "bioconda. Survives the session, and gives run_code an `env` to "
            "run in. Solving can take a few minutes for large stacks."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Environment name."},
                "packages": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Package specs, e.g. ['scanpy', 'samtools=1.19'].",
                },
                "python": {"type": "string", "description": "Python version (default 3.11)."},
            },
            "required": ["name", "packages"],
        },
    },
    handler=lambda args, **kw: _call(
        _env().create,
        name=args.get("name", ""),
        packages=args.get("packages") or [],
        python=args.get("python", "3.11"),
    ),
)

registry.register(
    name="env_install",
    toolset="environments",
    schema={
        "name": "env_install",
        "description": (
            "Add packages to an existing environment. The environment's lock "
            "identity changes, so cells run after this are distinguishable "
            "from cells run before it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Environment name."},
                "packages": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Package specs to add.",
                },
            },
            "required": ["name", "packages"],
        },
    },
    handler=lambda args, **kw: _call(
        _env().install,
        name=args.get("name", ""),
        packages=args.get("packages") or [],
    ),
)

registry.register(
    name="env_describe",
    toolset="environments",
    schema={
        "name": "env_describe",
        "description": (
            "An environment's recreatable identity — the lock hash recorded "
            "against every cell run in it, and what reproduce_artifact "
            "compares to decide 'verified'."
        ),
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Environment name."}},
            "required": ["name"],
        },
    },
    handler=lambda args, **kw: _call(_env().describe, name=args.get("name", "")),
)

registry.register(
    name="env_remove",
    toolset="environments",
    schema={
        "name": "env_remove",
        "description": "Delete a durable environment and everything installed in it.",
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Environment name."}},
            "required": ["name"],
        },
    },
    handler=lambda args, **kw: _call(
        lambda name: (_env().remove(name), {"removed": name})[1],
        name=args.get("name", ""),
    ),
)
