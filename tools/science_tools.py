"""Science toolset — persistent kernels, artifacts, lineage, reproduction.

The model-facing surface of the science layer (science/*): ``run_code``
executes cells in a persistent per-session Jupyter kernel and records the
full execution/provenance trace; the artifact tools navigate the lineage
store; ``reproduce_artifact`` replays a version's producing cells and
checksum-verifies the result.

These ship **on by default** on every platform bundle — they're in
``_OPENCODON_CORE_TOOLS`` (toolsets.py), not behind an opt-in ``science``
toolset. opencodon is an open-science agent; a session that can't run code in
a real kernel and record provenance isn't one. The standalone ``science``
toolset still exists for narrowing (``--toolsets science``).

``run_code`` and ``reproduce_artifact`` stay service-gated on the jupyter
kernel stack via ``check_fn``, but that gate is now "installed **or**
installable" — on an install that lacks the stack the tools remain in the
schema and the deps are fetched at kernel-start time
(``science.kernels.ensure_kernels``). They drop out of the schema entirely
only when lazy installs are disabled and the stack is absent.
"""

import json

from tools.registry import registry


def _runtime():
    from science.runtime import get_science_runtime

    return get_science_runtime()


def _session(kw) -> str:
    return kw.get("session_id") or kw.get("task_id") or "adhoc"


def _kernels_ready() -> bool:
    try:
        from science.kernels import kernels_available

        return kernels_available()
    except Exception:
        return False


# ── run_code ────────────────────────────────────────────────────────


def run_code(
    code: str,
    language: str = "python",
    timeout: float = 60.0,
    inputs=None,
    description: str = None,
    session_id: str = "adhoc",
) -> str:
    try:
        result = _runtime().run_cell(
            session_id,
            code,
            language=language,
            timeout=float(timeout),
            inputs=inputs,
            description=description,
        )
        return json.dumps(result, ensure_ascii=False, default=str)
    except LookupError as exc:
        return json.dumps({"error": str(exc)})
    except Exception as exc:
        return json.dumps({"error": f"{type(exc).__name__}: {exc}"})


registry.register(
    name="run_code",
    toolset="science",
    schema={
        "name": "run_code",
        "description": (
            "Execute code in a persistent per-session kernel (variables and "
            "imports survive across calls). Every cell is recorded for "
            "reproducibility. The kernel runs in an isolated workspace, NOT "
            "your current directory — to read an existing local file, pass "
            "its ABSOLUTE path (a bare/relative filename will not resolve). "
            "In-kernel helpers: save_artifact(data_or_path, filename) "
            "publishes an output as a versioned artifact; "
            "load_artifact(version_id) returns a local path for an input "
            "declared in `inputs`; host.llm(prompt) / host.llm_batch(prompts) "
            "call the model from code."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Source code to execute."},
                "language": {
                    "type": "string",
                    "enum": ["python", "r"],
                    "description": "Kernel language (default python).",
                },
                "timeout": {
                    "type": "number",
                    "description": "Wall-clock budget in seconds (default 60).",
                },
                "inputs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Artifact version ids this cell may load via "
                        "load_artifact()."
                    ),
                },
                "description": {
                    "type": "string",
                    "description": (
                        "Short present-participle label for what this cell "
                        "does, e.g. 'Fitting the calibration curve'. Shown "
                        "in the session trace so a reader can follow the "
                        "work as a lab log rather than reading code."
                    ),
                },
            },
            "required": ["code", "description"],
        },
    },
    handler=lambda args, **kw: run_code(
        code=args.get("code", ""),
        language=args.get("language", "python"),
        timeout=args.get("timeout", 60.0),
        inputs=args.get("inputs"),
        description=args.get("description"),
        session_id=_session(kw),
    ),
    check_fn=_kernels_ready,
)


# ── artifact navigation ─────────────────────────────────────────────


def list_artifacts(session_id: str = "adhoc") -> str:
    try:
        runtime = _runtime()
        root = runtime.root_for(session_id)
        rows = runtime.store.artifacts_for_root(root)
        return json.dumps(
            {
                "root_session_id": root,
                "artifacts": [
                    {
                        "artifact_id": r["id"],
                        "filename": r["filename"],
                        "latest_version_id": r.get("latest_version_id"),
                        "latest_version_number": r.get("latest_version_number"),
                        "size_bytes": r.get("latest_size_bytes"),
                        "content_type": r.get("latest_content_type"),
                        "is_user_upload": bool(r.get("is_user_upload")),
                    }
                    for r in rows
                ],
            },
            ensure_ascii=False,
        )
    except Exception as exc:
        return json.dumps({"error": f"{type(exc).__name__}: {exc}"})


registry.register(
    name="list_artifacts",
    toolset="science",
    schema={
        "name": "list_artifacts",
        "description": (
            "List this conversation's versioned artifacts (files published "
            "by run_code cells or loaded as inputs), with latest-version ids "
            "usable in run_code inputs / load_artifact / artifact_lineage."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    handler=lambda args, **kw: list_artifacts(session_id=_session(kw)),
)


def load_artifact(version_id: str, session_id: str = "adhoc") -> str:
    """Materialize a version into the session workspace; returns its path."""
    try:
        runtime = _runtime()
        version = runtime.store.get_version(version_id)
        if version is None:
            return json.dumps({"error": f"artifact version {version_id!r} does not exist"})
        artifact = runtime.store.get_artifact(version["artifact_id"]) or {}
        workspace = runtime.manager.workspace_for(session_id)
        dest = workspace / "inputs" / (artifact.get("filename") or version_id)
        runtime.blobs.materialize(version["checksum"], dest)
        return json.dumps(
            {
                "path": str(dest),
                "filename": artifact.get("filename"),
                "version_number": version["version_number"],
                "sha256": version["checksum"],
                "note": (
                    "for tracked lineage, also declare this version id in "
                    "run_code inputs and read it via load_artifact() in-kernel"
                ),
            },
            ensure_ascii=False,
        )
    except Exception as exc:
        return json.dumps({"error": f"{type(exc).__name__}: {exc}"})


registry.register(
    name="load_artifact",
    toolset="science",
    schema={
        "name": "load_artifact",
        "description": (
            "Materialize an artifact version into the session workspace and "
            "return its local path."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "version_id": {
                    "type": "string",
                    "description": "Artifact version id (see list_artifacts).",
                }
            },
            "required": ["version_id"],
        },
    },
    handler=lambda args, **kw: load_artifact(
        version_id=args.get("version_id", ""), session_id=_session(kw)
    ),
)


def artifact_lineage(version_id: str, direction: str = "upstream") -> str:
    try:
        runtime = _runtime()
        rows = runtime.store.lineage(version_id, direction=direction)
        enriched = []
        for row in rows:
            artifact = runtime.store.get_artifact(row["artifact_id"]) or {}
            enriched.append(
                {
                    "version_id": row["id"],
                    "filename": artifact.get("filename"),
                    "version_number": row["version_number"],
                    "depth": row["depth"],
                    "sha256": row["checksum"],
                    "producing_cell_id": row.get("producing_cell_id"),
                }
            )
        return json.dumps(
            {"version_id": version_id, "direction": direction, "lineage": enriched},
            ensure_ascii=False,
        )
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
    except Exception as exc:
        return json.dumps({"error": f"{type(exc).__name__}: {exc}"})


registry.register(
    name="artifact_lineage",
    toolset="science",
    schema={
        "name": "artifact_lineage",
        "description": (
            "Walk an artifact version's provenance: upstream = what it was "
            "derived from, downstream = what was derived from it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "version_id": {"type": "string"},
                "direction": {
                    "type": "string",
                    "enum": ["upstream", "downstream"],
                },
            },
            "required": ["version_id"],
        },
    },
    handler=lambda args, **kw: artifact_lineage(
        version_id=args.get("version_id", ""),
        direction=args.get("direction", "upstream"),
    ),
)


def reproduce_artifact(version_id: str) -> str:
    try:
        from science.reproduce import reproduce

        return json.dumps(reproduce(version_id, runtime=_runtime()),
                          ensure_ascii=False, default=str)
    except Exception as exc:
        return json.dumps({"error": f"{type(exc).__name__}: {exc}"})


registry.register(
    name="reproduce_artifact",
    toolset="science",
    schema={
        "name": "reproduce_artifact",
        "description": (
            "Re-run the cells that produced an artifact version in a fresh "
            "kernel and checksum-compare the result (claims: reproduced / "
            "diverged / failed / indeterminate / ineligible)."
        ),
        "parameters": {
            "type": "object",
            "properties": {"version_id": {"type": "string"}},
            "required": ["version_id"],
        },
    },
    handler=lambda args, **kw: reproduce_artifact(
        version_id=args.get("version_id", "")
    ),
    check_fn=_kernels_ready,
)
