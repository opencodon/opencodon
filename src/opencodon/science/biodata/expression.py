"""Expression and regulation — GTEx, ENCODE, JASPAR.

Where a gene is expressed, and what regulates it.

GTEx keys on a versioned GENCODE id (``ENSG00000012048.20``), not a symbol and
not a bare Ensembl id, and the version is GTEx's own — v26 for the v8 dataset,
which is *not* whatever Ensembl currently serves. So a symbol is resolved
through GTEx's own reference endpoint first; passing a plausible-looking id
from elsewhere returns an empty result rather than an error, which is the
worst kind of failure to debug.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from opencodon.science.apiclient import ApiClient, ApiError, bounded_count, clip

GTEX_URL = "https://gtexportal.org/api/v2"
ENCODE_URL = "https://www.encodeproject.org"
JASPAR_URL = "https://jaspar.elixir.no/api/v1"

SOURCE_GTEX = "gtex"
SOURCE_ENCODE = "encode"
SOURCE_JASPAR = "jaspar"

DEFAULT_DATASET = "gtex_v8"


def _require(value: str, what: str, source: str) -> str:
    if not (value or "").strip():
        raise ApiError(source, f"a {what} is required")
    return value.strip()


# ── GTEx ────────────────────────────────────────────────────────────


def _gtex(**kwargs) -> ApiClient:
    return ApiClient(SOURCE_GTEX, GTEX_URL, **kwargs)


def resolve_gencode_id(symbol: str, **kwargs) -> Dict[str, Any]:
    """GTEx's own record for a gene symbol, including its versioned GENCODE id."""
    symbol = _require(symbol, "gene symbol", SOURCE_GTEX)
    payload = _gtex(**kwargs).get_json("/reference/gene", {"geneId": symbol})
    rows = payload.get("data") or []
    if not rows:
        raise ApiError(SOURCE_GTEX, f"no GTEx gene for {symbol!r}", status=404)
    gene = rows[0]
    return {
        "source": SOURCE_GTEX,
        "symbol": gene.get("geneSymbol"),
        "gencode_id": gene.get("gencodeId"),
        "gencode_version": gene.get("gencodeVersion"),
        "entrez_id": gene.get("entrezGeneId"),
        "gene_type": gene.get("geneType"),
        "chromosome": gene.get("chromosome"),
        "start": gene.get("start"),
        "end": gene.get("end"),
        "strand": gene.get("strand"),
        "genome_build": gene.get("genomeBuild"),
    }


def tissue_expression(
    symbol: str, *, dataset: str = DEFAULT_DATASET, limit: Optional[int] = None, **kwargs
) -> Dict[str, Any]:
    """Median expression per tissue, highest first.

    Sorted rather than returned in GTEx's tissue order, because with a result
    cap the interesting tissues must survive the truncation.
    """
    gene = resolve_gencode_id(symbol, **kwargs)
    payload = _gtex(**kwargs).get_json(
        "/expression/medianGeneExpression",
        {"gencodeId": gene["gencode_id"], "datasetId": dataset, "itemsPerPage": 250},
    )
    rows = payload.get("data") or []
    ordered = sorted(rows, key=lambda r: r.get("median") or 0, reverse=True)
    count = bounded_count(limit)
    page = ordered[:count]
    return {
        "source": SOURCE_GTEX,
        "symbol": gene["symbol"],
        "gencode_id": gene["gencode_id"],
        "dataset": dataset,
        "unit": (rows[0].get("unit") if rows else None) or "TPM",
        "tissues_measured": len(rows),
        "returned": len(page),
        "results": [
            {
                "tissue": row.get("tissueSiteDetailId"),
                "median": row.get("median"),
                "ontology_id": row.get("ontologyId"),
            }
            for row in page
        ],
    }


def eqtl_genes(
    tissue: str, *, limit: Optional[int] = None, **kwargs
) -> Dict[str, Any]:
    """Genes with a significant eQTL in a tissue."""
    tissue = _require(tissue, "GTEx tissue id", SOURCE_GTEX)
    count = bounded_count(limit)
    payload = _gtex(**kwargs).get_json(
        "/association/egene",
        {"tissueSiteDetailId": tissue, "datasetId": DEFAULT_DATASET,
         "itemsPerPage": count},
    )
    rows = (payload.get("data") or [])[:count]
    return {
        "source": SOURCE_GTEX,
        "tissue": tissue,
        "returned": len(rows),
        "results": [
            {
                "gene_symbol": row.get("geneSymbol"),
                "gencode_id": row.get("gencodeId"),
                "p_value": row.get("pValue"),
                "q_value": row.get("qValue"),
                "empirical_p_value": row.get("empiricalPValue"),
            }
            for row in rows
        ],
    }


# ── ENCODE ──────────────────────────────────────────────────────────


def encode_experiments(
    query: str,
    *,
    assay: Optional[str] = None,
    organism: str = "Homo sapiens",
    limit: Optional[int] = None,
    **kwargs,
) -> Dict[str, Any]:
    """Search ENCODE experiments — assay, biosample, target."""
    query = _require(query, "search term", SOURCE_ENCODE)
    count = bounded_count(limit)
    params: Dict[str, Any] = {
        "type": "Experiment", "searchTerm": query, "limit": count,
        "format": "json", "replicates.library.biosample.donor.organism.scientific_name": organism,
    }
    if assay:
        params["assay_title"] = assay
    payload = ApiClient(SOURCE_ENCODE, ENCODE_URL, **kwargs).get_json("/search/", params)
    graph = payload.get("@graph") or []
    return {
        "source": SOURCE_ENCODE,
        "query": query,
        "total": payload.get("total"),
        "returned": len(graph),
        "results": [
            {
                "accession": item.get("accession"),
                "assay": item.get("assay_title") or item.get("assay_term_name"),
                "target": ((item.get("target") or {}).get("label")),
                "biosample": item.get("biosample_summary"),
                "status": item.get("status"),
                "description": clip(item.get("description"), 200),
            }
            for item in graph
        ],
    }


# ── JASPAR ──────────────────────────────────────────────────────────


def tf_motifs(
    query: str, *, collection: str = "CORE", limit: Optional[int] = None, **kwargs
) -> Dict[str, Any]:
    """Transcription-factor binding matrices from JASPAR."""
    query = _require(query, "transcription factor name", SOURCE_JASPAR)
    count = bounded_count(limit)
    payload = ApiClient(SOURCE_JASPAR, JASPAR_URL, **kwargs).get_json(
        "/matrix/",
        {"search": query, "collection": collection, "page_size": count,
         "format": "json"},
    )
    results = payload.get("results") or []
    return {
        "source": SOURCE_JASPAR,
        "query": query,
        "collection": collection,
        "total": payload.get("count"),
        "returned": len(results),
        "results": [
            {
                "matrix_id": row.get("matrix_id"),
                "name": row.get("name"),
                "base_id": row.get("base_id"),
                # Versions matter: MA0139.1 and MA0139.2 are different models
                # of the same factor and give different scans.
                "version": row.get("version"),
                "collection": row.get("collection"),
                "sequence_logo": row.get("sequence_logo"),
            }
            for row in results
        ],
    }
