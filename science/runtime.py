"""ScienceRuntime — one recorded cell, end to end.

The orchestration glue the ``run_code`` tool (and ``reproduce``) drive:

1. ensure the session's kernel (lazy start; SDK bootstrap injected)
2. insert the ``execution_log`` row *first* (status ``running``) so in-cell
   ``host_call_log`` rows always have a parent to reference
3. prepare the cell (materialize declared inputs, write cell.json with the
   host-bridge endpoint), run it, collect the journal
4. record journal ops as host calls, ingest staged outputs as artifact
   versions (+ dependency edges from load-before-stage causality), finalize
   the cell row

Everything lands in the same execution/lineage tables no matter how the cell
came to run — the convergence invariant (implementation-design.md §2.5).
"""

from __future__ import annotations

import logging
import mimetypes
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from science import bridge as _bridge
from science.blobstore import BlobStore, get_blob_store
from science.host_bridge import get_host_bridge
from science.kernels import (
    DEFAULT_CELL_TIMEOUT_S,
    SessionKernelManager,
    env_snapshot_hash,
    get_kernel_manager,
)
from science.store import ScienceStore

logger = logging.getLogger(__name__)

# Stream text returned to the model is capped; the full text lives in
# execution_log (itself bounded by the kernel client's stream cap).
RESULT_STREAM_CHARS = 12_000


def _clip(text: Optional[str]) -> Optional[str]:
    if text and len(text) > RESULT_STREAM_CHARS:
        return text[:RESULT_STREAM_CHARS] + f"\n…[{len(text) - RESULT_STREAM_CHARS} chars truncated; full output in execution_log]"
    return text


class ScienceRuntime:
    def __init__(
        self,
        db=None,
        *,
        store: ScienceStore = None,
        blobs: BlobStore = None,
        manager: SessionKernelManager = None,
    ):
        if db is None and store is None:
            from opencodon_state import SessionDB

            db = SessionDB()
        self._db = db if db is not None else store._db
        self._store = store or ScienceStore(self._db)
        self._blobs = blobs or get_blob_store()
        self._manager = manager or get_kernel_manager()
        # Cache the env snapshot per (session, language) — recomputed only
        # when a fresh kernel starts, mirrored into each cell row's env hash.
        self._env_cache: Dict[tuple, str] = {}
        self._lock = threading.Lock()

    @property
    def store(self) -> ScienceStore:
        return self._store

    @property
    def blobs(self) -> BlobStore:
        return self._blobs

    @property
    def manager(self) -> SessionKernelManager:
        return self._manager

    def root_for(self, session_id: str) -> str:
        try:
            row = self._db.get_session(session_id)
        except Exception:
            row = None
        if row and row.get("root_session_id"):
            return row["root_session_id"]
        return session_id

    # ── the recorded cell ───────────────────────────────────────────

    def run_cell(
        self,
        session_id: str,
        source: str,
        *,
        language: str = "python",
        timeout: float = DEFAULT_CELL_TIMEOUT_S,
        inputs: Optional[List[Union[str, dict]]] = None,
        origin: str = "agent",
    ) -> Dict[str, Any]:
        root_session_id = self.root_for(session_id)
        execution_id = f"cell-{uuid.uuid4().hex}"
        declared_inputs = _normalize_inputs(inputs)

        kernel, fresh = self._manager.ensure_kernel(session_id, language)
        resolver = self._manager.resolver_for(language)
        env_key = (session_id, language)
        with self._lock:
            if fresh or env_key not in self._env_cache:
                try:
                    self._env_cache[env_key] = resolver.snapshot()
                except Exception:
                    self._env_cache[env_key] = ""
            env_snapshot = self._env_cache[env_key]

        self._store.record_cell(
            session_id,
            source,
            language,
            kernel.kernel_id,
            cell_id=execution_id,
            exit_status="running",
            origin=origin,
            env_name=kernel.spec.runtime_identity,
            env_snapshot=env_snapshot if fresh else None,
        )

        workspace = self._manager.workspace_for(session_id)
        host = get_host_bridge(workspace, self._store)
        prep = _bridge.prepare_cell(
            workspace,
            execution_id=execution_id,
            inputs=declared_inputs,
            store=self._store,
            blobs=self._blobs,
            host_endpoint=host.endpoint,
        )

        with host.current_cell(execution_id):
            run = self._manager.run_cell(
                session_id, source, language=language, timeout=timeout
            )

        collected = _bridge.collect_cell(workspace, execution_id)
        self._record_journal(execution_id, collected)

        post = _bridge.snapshot_workspace(workspace)
        files_written = _bridge.diff_workspace(prep.pre_snapshot, post)
        files_read = sorted(
            {info["reference_name"] for info in _reference_infos(declared_inputs, self._store)}
        )

        ingested = self._ingest_stages(
            collected,
            execution_id=execution_id,
            session_id=session_id,
            root_session_id=root_session_id,
            language=language,
            env_snapshot=env_snapshot,
        )

        self._store.update_cell(
            execution_id,
            exit_status=run.outputs.status,
            stdout=run.outputs.stdout or None,
            stderr=_stderr_with_error(run.outputs),
            kernel_id=run.kernel_id,
            files_written=files_written or None,
            files_read=files_read or None,
        )

        cell_row = self._store.get_cell(execution_id) or {}
        result: Dict[str, Any] = {
            "status": run.outputs.status,
            "cell_id": execution_id,
            "cell_index": cell_row.get("cell_index"),
            "kernel_id": run.kernel_id,
            # The kernel is normally started by ensure_kernel above (fresh),
            # not inside run_cell — OR the flags so both paths report it.
            "fresh_kernel": fresh or run.fresh_kernel,
            "stdout": _clip(run.outputs.stdout) or "",
            "stderr": _clip(run.outputs.stderr) or "",
            "artifacts": ingested,
        }
        if run.outputs.is_error:
            result["error"] = {
                "name": run.outputs.error_name,
                "value": run.outputs.error_value,
            }
        if run.tainted:
            result["kernel_restarted"] = True
            result["kernel_restart_reasons"] = list(run.taint_reasons)
        text_results = [
            r["data"].get("text/plain")
            for r in run.outputs.results
            if isinstance(r.get("data"), dict) and r["data"].get("text/plain")
        ]
        if text_results:
            result["result"] = _clip("\n".join(str(t) for t in text_results))
        return result

    # ── pieces ──────────────────────────────────────────────────────

    def _record_journal(self, execution_id: str, collected) -> None:
        """Every SDK artifact op is one host_call_log row — same trace as
        llm/tool calls, so a cell's data access is fully reconstructable."""
        for op, data in collected.ordered:
            try:
                if op == "load":
                    self._store.record_host_call(
                        execution_id,
                        "artifact.load",
                        {"version_id": data.get("version_id"),
                         "reference_name": data.get("reference_name")},
                        derivable=True,
                    )
                elif op == "stage":
                    self._store.record_host_call(
                        execution_id,
                        "artifact.stage",
                        {"filename": data.get("filename"),
                         "content_type": data.get("content_type")},
                        derivable=True,
                    )
            except Exception:
                logger.exception("failed to record journal op %s", op)

    def _ingest_stages(
        self,
        collected,
        *,
        execution_id: str,
        session_id: str,
        root_session_id: str,
        language: str,
        env_snapshot: Optional[str],
    ) -> List[Dict[str, Any]]:
        deps_by_token = collected.deps_before_each_stage()
        env_hash = env_snapshot_hash(env_snapshot)
        ingested: List[Dict[str, Any]] = []
        for stage in collected.stages:
            filename = stage.get("filename") or "unnamed"
            ref = self._blobs.put_path(Path(stage["path"]))
            artifact = self._store.find_artifact(root_session_id, filename)
            if artifact is None:
                artifact_id = self._store.create_artifact(
                    root_session_id, filename, session_id=session_id
                )
                parent_version_id = None
            else:
                artifact_id = artifact["id"]
                parent_version_id = artifact.get("latest_version_id")
            content_type = (
                stage.get("content_type")
                or mimetypes.guess_type(filename)[0]
                or "application/octet-stream"
            )
            deps = [
                {"depends_on_version_id": vid}
                for vid in deps_by_token.get(stage.get("pending_token"), [])
            ]
            version = self._store.add_version(
                artifact_id,
                checksum=ref.sha256,
                size_bytes=ref.size_bytes,
                storage_path=ref.path,
                content_type=content_type,
                session_id=session_id,
                language=language,
                producing_cell_id=execution_id,
                parent_version_id=parent_version_id,
                env_snapshot_hash=env_hash,
                dependencies=deps,
            )
            ingested.append(
                {
                    "artifact_id": artifact_id,
                    "version_id": version["id"],
                    "version_number": version["version_number"],
                    "filename": filename,
                    "sha256": ref.sha256,
                    "size_bytes": ref.size_bytes,
                }
            )
        return ingested


def _stderr_with_error(outputs) -> Optional[str]:
    """Fold the exception (name: value) into stderr for the cell row."""
    parts = []
    if outputs.stderr:
        parts.append(outputs.stderr)
    if outputs.is_error and (outputs.error_name or outputs.error_value):
        parts.append(f"{outputs.error_name}: {outputs.error_value}")
    return "\n".join(parts) or None


def _normalize_inputs(inputs) -> List[dict]:
    normalized: List[dict] = []
    for entry in inputs or []:
        if isinstance(entry, str):
            normalized.append({"version_id": entry})
        elif isinstance(entry, dict) and entry.get("version_id"):
            normalized.append(
                {"version_id": entry["version_id"],
                 "reference_name": entry.get("reference_name")}
            )
    return normalized


def _reference_infos(declared_inputs: List[dict], store) -> List[dict]:
    infos = []
    for entry in declared_inputs:
        version = store.get_version(entry["version_id"])
        if version is None:
            continue
        artifact = store.get_artifact(version["artifact_id"]) or {}
        infos.append(
            {"reference_name": entry.get("reference_name")
             or artifact.get("filename") or entry["version_id"]}
        )
    return infos


# ── Process-wide runtime singleton for the tool layer ───────────────

_runtime: Optional[ScienceRuntime] = None
_runtime_lock = threading.Lock()


def get_science_runtime() -> ScienceRuntime:
    global _runtime
    with _runtime_lock:
        if _runtime is None:
            _runtime = ScienceRuntime()
        return _runtime


def set_science_runtime(runtime: Optional[ScienceRuntime]) -> None:
    """Test hook / embedder hook: inject a runtime with explicit stores."""
    global _runtime
    with _runtime_lock:
        _runtime = runtime
