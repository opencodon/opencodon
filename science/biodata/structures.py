"""Structures and interactions — RCSB PDB, AlphaFold DB, InterPro, STRING.

Experimental structures, predicted structures, domain architecture, and the
interaction network.

The distinction that matters most here is experimental versus predicted. A PDB
entry is a measurement with a resolution; an AlphaFold model is a prediction
with a confidence, and a low-pLDDT region is frequently not a badly predicted
structure but a genuinely disordered one. Both are returned with the number
that says which — resolution for PDB, pLDDT for AlphaFold — because a
structure quoted without it invites treating a guess as an observation.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from science.apiclient import ApiClient, ApiError, bounded_count, clip

RCSB_URL = "https://data.rcsb.org/rest/v1"
RCSB_SEARCH_URL = "https://search.rcsb.org/rcsbsearch/v2"
ALPHAFOLD_URL = "https://alphafold.ebi.ac.uk/api"
INTERPRO_URL = "https://www.ebi.ac.uk/interpro/api"
STRING_URL = "https://string-db.org/api"

SOURCE_PDB = "pdb"
SOURCE_ALPHAFOLD = "alphafold"
SOURCE_INTERPRO = "interpro"
SOURCE_STRING = "string"

HUMAN_TAXID = 9606

# pLDDT bands as AlphaFold documents them — the boundary that matters is 70,
# below which backbone placement should not be trusted.
PLDDT_BANDS = ((90, "very high"), (70, "confident"), (50, "low"), (0, "very low"))


def _require(value: str, what: str, source: str) -> str:
    if not (value or "").strip():
        raise ApiError(source, f"a {what} is required")
    return value.strip()


def plddt_band(score: Optional[float]) -> Optional[str]:
    """Name the confidence band, so a bare number is not read as a score out of 100."""
    if score is None:
        return None
    for threshold, label in PLDDT_BANDS:
        if score >= threshold:
            return label
    return "very low"


# ── RCSB PDB ────────────────────────────────────────────────────────


def pdb_entry(pdb_id: str, **kwargs) -> Dict[str, Any]:
    """Metadata for one experimental structure."""
    pdb_id = _require(pdb_id, "PDB id", SOURCE_PDB).upper()
    payload = ApiClient(SOURCE_PDB, RCSB_URL, **kwargs).get_json(
        f"/core/entry/{pdb_id}"
    )
    info = payload.get("rcsb_entry_info") or {}
    citation = (payload.get("citation") or [{}])[0]
    return {
        "source": SOURCE_PDB,
        "pdb_id": payload.get("rcsb_id") or pdb_id,
        "title": (payload.get("struct") or {}).get("title"),
        "method": (payload.get("exptl") or [{}])[0].get("method"),
        # The measurement's quality — a 3.5 A structure does not support the
        # same claims as a 1.2 A one.
        "resolution": (info.get("resolution_combined") or [None])[0],
        "deposited": (payload.get("rcsb_accession_info") or {}).get("deposit_date"),
        "released": (payload.get("rcsb_accession_info") or {}).get("initial_release_date"),
        "polymer_entities": info.get("polymer_entity_count"),
        "ligands": info.get("nonpolymer_entity_count"),
        "authors": [a.get("name") for a in (payload.get("audit_author") or [])][:20],
        "doi": citation.get("pdbx_database_id_doi"),
        "pubmed_id": citation.get("pdbx_database_id_pub_med"),
    }


def pdb_search(query: str, *, limit: Optional[int] = None, **kwargs) -> Dict[str, Any]:
    """Full-text search over the PDB."""
    query = _require(query, "search query", SOURCE_PDB)
    count = bounded_count(limit)
    request = {
        "query": {"type": "terminal", "service": "full_text",
                  "parameters": {"value": query}},
        "return_type": "entry",
        "request_options": {"paginate": {"start": 0, "rows": count}},
    }
    payload = ApiClient(SOURCE_PDB, RCSB_SEARCH_URL, **kwargs).post_json(
        "/query", request
    )
    hits = payload.get("result_set") or []
    return {
        "source": SOURCE_PDB,
        "query": query,
        "total": payload.get("total_count"),
        "returned": len(hits),
        "results": [
            {"pdb_id": hit.get("identifier"), "score": hit.get("score")}
            for hit in hits
        ],
    }


# ── AlphaFold DB ────────────────────────────────────────────────────


def alphafold_model(accession: str, **kwargs) -> Dict[str, Any]:
    """Predicted structure for a UniProt accession, with its confidence."""
    accession = _require(accession, "UniProt accession", SOURCE_ALPHAFOLD).upper()
    payload = ApiClient(SOURCE_ALPHAFOLD, ALPHAFOLD_URL, **kwargs).get_json(
        f"/prediction/{accession}"
    )
    if not payload:
        raise ApiError(
            SOURCE_ALPHAFOLD, f"no AlphaFold model for {accession!r}", status=404
        )
    model = payload[0] if isinstance(payload, list) else payload
    score = model.get("globalMetricValue")
    return {
        "source": SOURCE_ALPHAFOLD,
        "accession": accession,
        "model_id": model.get("modelEntityId"),
        "uniprot_id": model.get("uniprotId"),
        "organism": model.get("organismScientificName"),
        "model_created": model.get("modelCreatedDate"),
        "tool": model.get("toolUsed"),
        # A prediction, not a measurement — the confidence travels with it.
        "mean_plddt": score,
        "confidence": plddt_band(score),
        "fraction_plddt_very_low": model.get("fractionPlddtVeryLow"),
        "pdb_url": model.get("pdbUrl"),
        "cif_url": model.get("cifUrl"),
    }


# ── InterPro ────────────────────────────────────────────────────────


def protein_domains(
    accession: str, *, limit: Optional[int] = None, **kwargs
) -> Dict[str, Any]:
    """Domain architecture for a protein."""
    accession = _require(accession, "UniProt accession", SOURCE_INTERPRO).upper()
    count = bounded_count(limit)
    payload = ApiClient(SOURCE_INTERPRO, INTERPRO_URL, **kwargs).get_json(
        f"/entry/interpro/protein/uniprot/{accession}/", {"page_size": count}
    )
    results = payload.get("results") or []
    domains: List[Dict[str, Any]] = []
    for row in results:
        meta = row.get("metadata") or {}
        domains.append({
            "accession": meta.get("accession"),
            "name": meta.get("name"),
            "type": meta.get("type"),
            "source_database": meta.get("source_database"),
        })
    return {
        "source": SOURCE_INTERPRO,
        "protein": accession,
        "total": payload.get("count"),
        "returned": len(domains),
        "results": domains,
    }


# ── STRING ──────────────────────────────────────────────────────────


def interaction_network(
    identifiers,
    *,
    species: int = HUMAN_TAXID,
    min_score: float = 0.4,
    limit: Optional[int] = None,
    **kwargs,
) -> Dict[str, Any]:
    """Protein-protein interaction partners with evidence-channel scores."""
    if isinstance(identifiers, str):
        identifiers = [part.strip() for part in identifiers.split(",") if part.strip()]
    identifiers = [str(v).strip() for v in (identifiers or []) if str(v).strip()]
    if not identifiers:
        raise ApiError(SOURCE_STRING, "at least one protein identifier is required")
    if not 0 <= min_score <= 1:
        raise ApiError(SOURCE_STRING, "min_score must be between 0 and 1")

    count = bounded_count(limit)
    payload = ApiClient(SOURCE_STRING, STRING_URL, **kwargs).get_json(
        "/json/network",
        {"identifiers": "%0d".join(identifiers), "species": int(species),
         "limit": count},
    )
    edges = payload if isinstance(payload, list) else []
    kept = [edge for edge in edges if (edge.get("score") or 0) >= min_score][:count]
    return {
        "source": SOURCE_STRING,
        "identifiers": identifiers,
        "species": int(species),
        "min_score": min_score,
        "edges_returned": len(kept),
        "results": [
            {
                "protein_a": edge.get("preferredName_A"),
                "protein_b": edge.get("preferredName_B"),
                "score": edge.get("score"),
                # The channels matter: a link supported only by text-mining is
                # weaker evidence than one from experiments, and a combined
                # score alone hides which it is.
                "experimental": edge.get("escore"),
                "database": edge.get("dscore"),
                "textmining": edge.get("tscore"),
                "coexpression": edge.get("ascore"),
            }
            for edge in kept
        ],
    }
