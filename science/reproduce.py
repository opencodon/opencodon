"""reproduce(version_id) — replay the producing cells, checksum-verify.

Simplified port of the donor's ReproductionRunner, graded honestly:

- The replay prefix is every cell of the producing session in the same
  language up to (and including) the producing cell, in cell_index order —
  an approximation of the donor's kernel-epoch prefix (opencodon does not
  persist epochs; a restart mid-history makes the prefix a superset).
- Each replayed cell re-declares the *exact historical inputs* its original
  recorded via ``artifact.load`` host calls, so lineage is honored, not
  guessed.
- Replays run in a scratch session (``<session>~repro~<n>``) with a fresh
  kernel; candidate outputs become artifacts under the scratch root, never
  touching the original artifact's version chain or latest pointer.
- The claim is capped at ``reproduced`` (bytes matched) — never "verified" —
  because the local environment is an observation, not a recreatable recipe
  (no lockfile identity yet).
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

REPRO_CELL_TIMEOUT_S = 120.0


def reproduce(
    version_id: str,
    *,
    runtime=None,
    timeout: float = REPRO_CELL_TIMEOUT_S,
) -> Dict[str, Any]:
    """Re-run the cells that produced *version_id*; compare checksums.

    Returns a report dict with ``claim`` ∈ {"reproduced", "diverged",
    "failed", "indeterminate", "ineligible"} plus supporting evidence.
    """
    if runtime is None:
        from science.runtime import get_science_runtime

        runtime = get_science_runtime()
    store = runtime.store

    version = store.get_version(version_id)
    if version is None:
        return {"claim": "ineligible", "reason": f"artifact version {version_id!r} does not exist"}
    artifact = store.get_artifact(version["artifact_id"]) or {}
    producing_id = version.get("producing_cell_id")
    if not producing_id:
        return {
            "claim": "ineligible",
            "reason": "artifact has no producing cell; imports/uploads are not reproducible by rerun",
        }
    producing = store.get_cell(producing_id)
    if producing is None:
        return {"claim": "ineligible", "reason": "producing cell record is missing"}

    session_id = producing["session_id"]
    language = producing["language"]
    prefix = [
        cell
        for cell in store.cells_for_session(session_id)
        if cell["language"] == language
        and cell.get("origin") != "reproduction"
        and cell["cell_index"] <= producing["cell_index"]
    ]

    caveats: List[str] = [
        "environment is observation-only (no lockfile identity); a byte match "
        "is graded 'reproduced', not 'verified'",
    ]
    if any(c["exit_status"] not in ("ok", "running") for c in prefix[:-1]):
        caveats.append(
            "a prior cell in the replay prefix had a non-ok status; kernel "
            "state basis is approximate"
        )

    repro_session = f"{session_id}~repro~{uuid.uuid4().hex[:8]}"
    replayed: List[Dict[str, Any]] = []
    target_outputs: List[Dict[str, Any]] = []
    try:
        for cell in prefix:
            inputs = _historical_inputs(store, cell["id"])
            result = runtime.run_cell(
                repro_session,
                cell["source"],
                language=language,
                timeout=timeout,
                inputs=inputs,
                origin="reproduction",
            )
            replayed.append(
                {"original_cell_id": cell["id"], "replay_cell_id": result["cell_id"],
                 "status": result["status"]}
            )
            if cell["id"] == producing_id:
                target_outputs = result.get("artifacts") or []
                if result["status"] != "ok":
                    return _report(
                        "failed", version, replayed, caveats,
                        reason=f"target cell replay ended with status {result['status']!r}",
                        repro_session=repro_session,
                    )
    finally:
        try:
            runtime.manager.close_session(repro_session)
        except Exception:
            logger.exception("failed to close reproduction kernels")

    filename = artifact.get("filename")
    candidate = next(
        (o for o in target_outputs if o.get("filename") == filename), None
    )
    if candidate is None:
        return _report(
            "indeterminate", version, replayed, caveats,
            reason=f"replay produced no output named {filename!r}",
            repro_session=repro_session,
        )

    if candidate["sha256"] == version["checksum"]:
        claim, reason = "reproduced", "candidate bytes are identical to the recorded version"
    else:
        claim, reason = "diverged", "candidate bytes differ from the recorded version"
    report = _report(
        claim, version, replayed, caveats, reason=reason,
        repro_session=repro_session,
    )
    report["expected_sha256"] = version["checksum"]
    report["candidate_sha256"] = candidate["sha256"]
    report["candidate_version_id"] = candidate["version_id"]
    return report


def _historical_inputs(store, cell_id: str) -> List[dict]:
    """The exact inputs a cell loaded, recovered from its host-call trace."""
    inputs: List[dict] = []
    seen = set()
    for call in store.host_calls_for_cell(cell_id):
        if call.get("method") != "artifact.load":
            continue
        try:
            args = json.loads(call.get("args_json") or "{}")
        except ValueError:
            continue
        vid = args.get("version_id")
        if vid and vid not in seen:
            seen.add(vid)
            inputs.append(
                {"version_id": vid, "reference_name": args.get("reference_name")}
            )
    return inputs


def _report(
    claim: str,
    version: dict,
    replayed: List[dict],
    caveats: List[str],
    *,
    reason: str,
    repro_session: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "claim": claim,
        "reason": reason,
        "version_id": version["id"],
        "artifact_id": version["artifact_id"],
        "replayed_cells": replayed,
        "caveats": caveats,
        "reproduction_session": repro_session,
    }
