"""The kernel SDK and its host-side filesystem contract.

Ported from the opencodon donor (``kernel_sdk/bootstrap.py`` +
``execution/artifact_bridge.py``). Contract (all paths under
``<workspace>/.opencodon-science/``):

- ``cell.json`` — the host writes it before each cell: ``execution_id``,
  ``inputs`` (``{version_id: {path, reference_name}}``), ``staging_dir``,
  ``journal``, and (when the host bridge is up) ``host`` (``{socket, token}``).
- ``journal-<execution_id>.jsonl`` — the SDK appends one line per artifact
  call (``load`` / ``stage``) in execution order; the host reads it back
  after the cell and turns it into host_call_log rows + dependency edges.

The injected SDK source imports only the standard library. In-kernel surface:

- ``load_artifact(version_id)`` → local read-only path of a declared input
- ``save_artifact(data_or_path, filename, content_type=None)`` → stages an
  output; the host ingests it as an artifact version after the cell
- ``host.llm(...)`` / ``host.tool(...)`` / ``host.models()`` → parent-side
  calls over the host-bridge socket (science/host_bridge.py)

The CAS path is never exposed to the kernel — inputs are verified copies.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

SCIENCE_DIR = ".opencodon-science"
# Skill-shipped kernel helpers are staged here, inside the workspace, so a
# remote kernel gets them through the ordinary workspace sync rather than
# needing a second channel back to the host.
SKILL_HELPERS_DIR = "skill-helpers"
INPUTS_DIR = "inputs"
CELL_CONFIG_NAME = "cell.json"
_SCAN_FILE_LIMIT = 256


def journal_name(execution_id: str) -> str:
    return f"journal-{execution_id}.jsonl"


def science_dir(workspace: Path) -> Path:
    return Path(workspace) / SCIENCE_DIR


# ── Injected Python SDK ─────────────────────────────────────────────
# The workspace path is baked in at injection time as __OPENCODON_SCIENCE_WS__.

_SDK_SOURCE = r'''
import json as _hs_json, os as _hs_os, shutil as _hs_shutil, socket as _hs_socket, uuid as _hs_uuid

def _hs_dir():
    return _hs_os.path.join(__OPENCODON_SCIENCE_WS__, ".opencodon-science")

def _hs_cell():
    with open(_hs_os.path.join(_hs_dir(), "cell.json"), "r") as _f:
        return _hs_json.load(_f)

def _hs_append(entry):
    cfg = _hs_cell()
    path = _hs_os.path.join(_hs_dir(), cfg["journal"])
    with open(path, "a") as _f:
        _f.write(_hs_json.dumps(entry) + "\n")

def load_skill_helpers(name, namespace=None):
    """Define a skill's kernel helpers in this namespace; returns their names.

    Some skills ship a kernel.py of plotting or analysis helpers their
    instructions then refer to. The host stages those into the workspace, so
    this works identically on a local and a remote kernel.
    """
    path = _hs_os.path.join(_hs_dir(), "skill-helpers", str(name) + ".py")
    if not _hs_os.path.exists(path):
        raise LookupError(
            "skill %r ships no kernel helpers (looked for %s)" % (name, path))
    with open(path, "r") as _f:
        _source = _f.read()
    _ns = namespace if namespace is not None else globals()
    _before = set(_ns)
    exec(compile(_source, path, "exec"), _ns)
    return sorted(n for n in set(_ns) - _before if not n.startswith("_"))

def load_artifact(version_id, reference_name=None):
    """Return the read-only path of a host-materialized input version."""
    cfg = _hs_cell()
    info = cfg.get("inputs", {}).get(version_id)
    if info is None:
        raise LookupError(
            "artifact version %r was not declared as an input to run_code; "
            "declare it in the inputs parameter for tracked lineage" % version_id)
    ref = reference_name or info.get("reference_name") or version_id
    _hs_append({"op": "load", "version_id": version_id, "reference_name": ref})
    return info["path"]

def save_artifact(data_or_path, filename, content_type=None):
    """Snapshot an output into host staging; ingested as an artifact version."""
    cfg = _hs_cell()
    token = "pend-" + _hs_uuid.uuid4().hex
    dest = _hs_os.path.join(cfg["staging_dir"], token)
    if isinstance(data_or_path, (bytes, bytearray)):
        with open(dest, "wb") as _f:
            _f.write(bytes(data_or_path))
    elif hasattr(data_or_path, "__fspath__") or (
        isinstance(data_or_path, str) and _hs_os.path.isfile(data_or_path)
    ):
        _hs_shutil.copyfile(_hs_os.fspath(data_or_path), dest)
    elif isinstance(data_or_path, str):
        with open(dest, "w") as _f:
            _f.write(data_or_path)
    else:
        raise TypeError("save_artifact needs bytes, str, or a path")
    _hs_append({"op": "stage", "pending_token": token,
                "filename": filename, "content_type": content_type,
                "staged_file": token})
    return token

class _OpencodonHost(object):
    """host.* — parent-side calls over the host-bridge unix socket."""

    def _call(self, method, params):
        cfg = _hs_cell()
        endpoint = cfg.get("host")
        if not endpoint:
            raise RuntimeError("host bridge is not available for this cell")
        payload = _hs_json.dumps({
            "token": endpoint["token"], "method": method, "params": params,
        }).encode("utf-8")
        s = _hs_socket.socket(_hs_socket.AF_UNIX, _hs_socket.SOCK_STREAM)
        try:
            s.settimeout(float(endpoint.get("timeout", 600)))
            s.connect(endpoint["socket"])
            s.sendall(payload + b"\n")
            chunks = []
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                if chunk.endswith(b"\n"):
                    break
            reply = _hs_json.loads(b"".join(chunks).decode("utf-8"))
        finally:
            s.close()
        if reply.get("error"):
            raise RuntimeError("host.%s failed: %s" % (method, reply["error"]))
        return reply.get("data")

    def llm(self, prompt, model=None, system=None, max_tokens=None):
        """One model call, made by the parent on the kernel's behalf."""
        return self._call("llm", {
            "prompt": prompt, "model": model, "system": system,
            "max_tokens": max_tokens,
        })

    def llm_batch(self, prompts, model=None, system=None, max_tokens=None,
                  max_concurrency=8):
        """Fan a list of prompts out in parallel; returns aligned results."""
        return self._call("llm_batch", {
            "prompts": list(prompts), "model": model, "system": system,
            "max_tokens": max_tokens, "max_concurrency": max_concurrency,
        })

    def tool(self, name, args=None):
        """Invoke a whitelisted opencodon tool; returns its parsed result."""
        return self._call("tool", {"name": name, "args": args or {}})

    def models(self):
        return self._call("models", {})

    def cheap_model(self):
        """Model to use for high-volume per-item work (one call per page).

        Resolved by the host, which is the only side that knows which
        provider is configured — a model slug is only valid against one.
        """
        return (self.models() or {}).get("cheap")

host = _OpencodonHost()
'''


def bootstrap_source(workspace: str) -> str:
    """Injectable Python SDK source with the workspace path baked in."""
    return f"__OPENCODON_SCIENCE_WS__ = {str(workspace)!r}\n" + _SDK_SOURCE


# ── Injected R SDK (same filesystem contract; no host bridge yet) ───

_R_SDK_SOURCE = r'''
.hs_dir <- function() file.path(.HS_WS, ".opencodon-science")
.hs_cell <- function() jsonlite::fromJSON(file.path(.hs_dir(), "cell.json"), simplifyVector = FALSE)
.hs_append <- function(entry) {
  cfg <- .hs_cell()
  line <- jsonlite::toJSON(entry, auto_unbox = TRUE, null = "null")
  cat(line, "\n", sep = "", file = file.path(.hs_dir(), cfg$journal), append = TRUE)
}
load_artifact <- function(version_id, reference_name = NULL) {
  cfg <- .hs_cell()
  info <- cfg$inputs[[version_id]]
  if (is.null(info)) stop(paste("artifact version", version_id, "was not declared as an input to run_code"))
  ref <- if (!is.null(reference_name)) reference_name else if (!is.null(info$reference_name)) info$reference_name else version_id
  .hs_append(list(op = "load", version_id = version_id, reference_name = ref))
  info$path
}
save_artifact <- function(data_or_path, filename, content_type = NULL) {
  cfg <- .hs_cell()
  token <- paste0("pend-", paste(sample(c(0:9, letters[1:6]), 32, replace = TRUE), collapse = ""))
  dest <- file.path(cfg$staging_dir, token)
  if (is.character(data_or_path) && length(data_or_path) == 1 && file.exists(data_or_path)) {
    file.copy(data_or_path, dest)
  } else if (is.character(data_or_path)) {
    writeChar(paste(data_or_path, collapse = "\n"), dest, eos = NULL)
  } else {
    stop("save_artifact needs a string or an existing file path")
  }
  if (is.null(content_type)) content_type <- "application/octet-stream"
  .hs_append(list(op = "stage", pending_token = token,
                  filename = filename, content_type = content_type, staged_file = token))
  token
}
'''


def r_bootstrap_source(workspace: str) -> str:
    escaped = str(workspace).replace("\\", "\\\\").replace('"', '\\"')
    return f'.HS_WS <- "{escaped}"\n' + _R_SDK_SOURCE


def bootstrap_kernel(session, workspace, language) -> None:
    """Inject the SDK into a freshly started kernel (kernel-manager hook)."""
    # The workspace path is baked into the injected SDK, so it has to be the
    # path *the kernel* will resolve — which is not the host path when the
    # kernel runs elsewhere.
    ws = getattr(session, "kernel_workspace", None) or str(workspace)
    if language == "python":
        src = bootstrap_source(ws)
    elif language == "r":
        src = r_bootstrap_source(ws)
    else:
        return
    result = session.execute(src, timeout=30.0)
    if result.is_error:
        raise RuntimeError(
            f"kernel SDK bootstrap failed: {result.error_name}: "
            f"{result.error_value}"
        )


# ── Host side: prepare / collect ────────────────────────────────────


@dataclass
class CellPreparation:
    workspace: Path
    execution_id: str
    staging_dir: Path
    input_paths: Dict[str, str]
    pre_snapshot: Dict[str, Tuple[int, int]]


@dataclass
class CollectedCell:
    loads: List[dict] = field(default_factory=list)
    stages: List[dict] = field(default_factory=list)
    ordered: List[Tuple[str, dict]] = field(default_factory=list)

    def loaded_version_ids(self) -> List[str]:
        seen: List[str] = []
        for load in self.loads:
            vid = load.get("version_id")
            if vid and vid not in seen:
                seen.append(vid)
        return seen

    def deps_before_each_stage(self) -> Dict[str, List[str]]:
        """Map each staged token to the version_ids loaded before it."""
        loaded: List[str] = []
        by_token: Dict[str, List[str]] = {}
        for op, data in self.ordered:
            if op == "load" and data.get("version_id"):
                if data["version_id"] not in loaded:
                    loaded.append(data["version_id"])
            elif op == "stage":
                by_token[data["pending_token"]] = list(loaded)
        return by_token


def _safe_reference(name: str, used: set) -> str:
    base = "".join(c if c.isalnum() or c in "._-" else "_" for c in name) or "input"
    candidate, n = base, 1
    while candidate in used:
        candidate = f"{base}-{n}"
        n += 1
    used.add(candidate)
    return candidate


def stage_skill_helpers(workspace: Path) -> int:
    """Copy installed skills' ``kernel.py`` into the workspace.

    Staging inside the workspace is what makes the helpers reach a remote
    kernel: the provisioners already mirror the workspace, so nothing extra
    has to cross the boundary. Best-effort by design — a missing or
    unreadable skills tree must never stop a cell from running.
    """
    try:
        from tools.skills_hub import _skills_dir

        root = Path(_skills_dir())
    except Exception:
        return 0
    if not root.is_dir():
        return 0

    dest = science_dir(workspace) / SKILL_HELPERS_DIR
    staged = 0
    for helper in root.glob("*/*/kernel.py"):
        try:
            target = dest / f"{helper.parent.name}.py"
            payload = helper.read_bytes()
            if target.exists() and target.read_bytes() == payload:
                staged += 1
                continue
            dest.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            staged += 1
        except OSError as exc:
            logger.warning("could not stage helpers for %s: %s", helper.parent.name, exc)
    return staged


def prepare_cell(
    workspace: Path,
    *,
    execution_id: str,
    inputs: List[dict],
    store,
    blobs,
    host_endpoint: Optional[dict] = None,
) -> CellPreparation:
    """Materialize declared inputs, write cell.json, snapshot the workspace.

    *inputs* is ``[{"version_id": ..., "reference_name": ...?}, ...]``.
    Raises LookupError for unknown versions. *store* is a ScienceStore,
    *blobs* a BlobStore.
    """
    workspace = Path(workspace)
    sci = science_dir(workspace)
    sci.mkdir(parents=True, exist_ok=True)
    inputs_dir = workspace / INPUTS_DIR
    inputs_dir.mkdir(parents=True, exist_ok=True)
    staging_dir = sci / f"staging-{execution_id}"
    staging_dir.mkdir(parents=True, exist_ok=True)
    stage_skill_helpers(workspace)

    input_paths: Dict[str, str] = {}
    cell_inputs: Dict[str, dict] = {}
    used: set = set()
    for entry in inputs or []:
        version_id = entry["version_id"]
        reference_name = entry.get("reference_name")
        version = store.get_version(version_id)
        if version is None:
            raise LookupError(f"input artifact version {version_id!r} does not exist")
        artifact = store.get_artifact(version["artifact_id"]) or {}
        reference_name = reference_name or artifact.get("filename") or version_id
        filename = _safe_reference(reference_name, used)
        dest = inputs_dir / filename
        blobs.materialize(version["checksum"], dest)
        input_paths[version_id] = str(dest)
        cell_inputs[version_id] = {
            "path": str(dest),
            "reference_name": reference_name,
        }

    config = {
        "execution_id": execution_id,
        "journal": journal_name(execution_id),
        "staging_dir": str(staging_dir),
        "inputs": cell_inputs,
    }
    if host_endpoint:
        config["host"] = host_endpoint
    journal_path = sci / journal_name(execution_id)
    if journal_path.exists():
        journal_path.unlink()
    (sci / CELL_CONFIG_NAME).write_text(json.dumps(config))
    return CellPreparation(
        workspace=workspace,
        execution_id=execution_id,
        staging_dir=staging_dir,
        input_paths=input_paths,
        pre_snapshot=snapshot_workspace(workspace),
    )


def collect_cell(workspace: Path, execution_id: str) -> CollectedCell:
    """Read the per-execution journal into ordered loads and stages."""
    sci = science_dir(Path(workspace))
    journal_path = sci / journal_name(execution_id)
    collected = CollectedCell()
    if not journal_path.exists():
        return collected
    staging_dir = sci / f"staging-{execution_id}"
    for line in journal_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("op") == "load":
            load = {
                "version_id": entry.get("version_id"),
                "reference_name": entry.get("reference_name"),
            }
            collected.loads.append(load)
            collected.ordered.append(("load", load))
        elif entry.get("op") == "stage":
            token = entry.get("staged_file")
            path = staging_dir / token if token else None
            if path is None or not path.is_file():
                continue
            stage = {
                "pending_token": entry.get("pending_token"),
                "filename": entry.get("filename"),
                "content_type": entry.get("content_type"),
                "path": str(path),
            }
            collected.stages.append(stage)
            collected.ordered.append(("stage", stage))
    return collected


def snapshot_workspace(workspace: Path) -> Dict[str, Tuple[int, int]]:
    """Bounded {relpath: (size, mtime_ns)} excluding SDK/inputs dirs."""
    workspace = Path(workspace)
    out: Dict[str, Tuple[int, int]] = {}
    for root, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs if d not in (SCIENCE_DIR, INPUTS_DIR)]
        for name in files:
            full = Path(root) / name
            rel = str(full.relative_to(workspace))
            try:
                st = full.stat()
            except OSError:
                continue
            out[rel] = (st.st_size, st.st_mtime_ns)
            if len(out) >= _SCAN_FILE_LIMIT:
                return out
    return out


def diff_workspace(pre: dict, post: dict) -> List[str]:
    """New or modified files since ``pre`` (bounded, sorted)."""
    changed = [
        rel for rel, meta in post.items() if rel not in pre or pre[rel] != meta
    ]
    return sorted(changed)[:_SCAN_FILE_LIMIT]
