"""Variants — gnomAD population frequency and ClinVar clinical significance.

The two halves of "should I care about this variant?", and they answer
different questions that are easy to conflate:

- **gnomAD** says how *common* it is. A variant at 5% allele frequency in any
  population is almost certainly not causing a rare disease, whatever a
  prediction tool says about it.
- **ClinVar** says what submitters have *asserted* clinically — an aggregation
  of claims with review status attached, not a fact. The review status is
  therefore returned alongside every classification rather than buried, since
  "pathogenic, 1 submitter, no assertion criteria" and "pathogenic, reviewed by
  expert panel" are not the same claim.

gnomAD is GraphQL rather than REST, which is why this module posts.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from science.apiclient import ApiClient, ApiError, bounded_count

GNOMAD_URL = "https://gnomad.broadinstitute.org"
EUTILS_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

SOURCE_GNOMAD = "gnomad"
SOURCE_CLINVAR = "clinvar"

DEFAULT_DATASET = "gnomad_r4"
REFERENCE_GENOMES = ("GRCh38", "GRCh37")

_GENE_QUERY = """
query Gene($symbol: String!, $genome: ReferenceGenomeId!) {
  gene(gene_symbol: $symbol, reference_genome: $genome) {
    gene_id symbol chrom start stop
  }
}
"""

_VARIANT_QUERY = """
query Variant($id: String!, $dataset: DatasetId!) {
  variant(variantId: $id, dataset: $dataset) {
    variant_id rsids
    genome { ac an af populations { id ac an } }
    exome  { ac an af populations { id ac an } }
  }
}
"""


_GENE_VARIANTS_QUERY = """
query GeneVariants($symbol: String!, $genome: ReferenceGenomeId!, $dataset: DatasetId!) {
  gene(gene_symbol: $symbol, reference_genome: $genome) {
    gene_id
    variants(dataset: $dataset) {
      variant_id rsids consequence
      genome { ac an af }
      exome  { ac an af }
    }
  }
}
"""


def _gnomad(**kwargs) -> ApiClient:
    return ApiClient(SOURCE_GNOMAD, GNOMAD_URL, **kwargs)


def _graphql(query: str, variables: Dict[str, Any], **kwargs) -> Dict[str, Any]:
    payload = _gnomad(**kwargs).post_json(
        "/api", {"query": query, "variables": variables}
    )
    # GraphQL reports failure in the body with HTTP 200, so the transport's
    # status handling never sees it.
    errors = payload.get("errors") or []
    if errors:
        raise ApiError(
            SOURCE_GNOMAD,
            "; ".join(str(e.get("message", e)) for e in errors)[:300],
        )
    return payload.get("data") or {}


def gene_region(
    symbol: str, *, reference_genome: str = "GRCh38", **kwargs
) -> Dict[str, Any]:
    """Genomic interval for a gene, as gnomAD knows it."""
    if not (symbol or "").strip():
        raise ApiError(SOURCE_GNOMAD, "a gene symbol is required")
    if reference_genome not in REFERENCE_GENOMES:
        raise ApiError(
            SOURCE_GNOMAD,
            f"reference_genome must be one of {', '.join(REFERENCE_GENOMES)}",
        )
    data = _graphql(
        _GENE_QUERY,
        {"symbol": symbol.strip().upper(), "genome": reference_genome},
        **kwargs,
    )
    gene = data.get("gene")
    if not gene:
        raise ApiError(SOURCE_GNOMAD, f"no gnomAD gene for {symbol!r}", status=404)
    return {"source": SOURCE_GNOMAD, "reference_genome": reference_genome, **gene}


def _frequencies(node: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not node:
        return None
    populations = [
        {
            "population": pop.get("id"),
            "allele_count": pop.get("ac"),
            "allele_number": pop.get("an"),
            "allele_frequency": (
                pop["ac"] / pop["an"] if pop.get("an") else None
            ),
        }
        for pop in (node.get("populations") or [])
    ]
    return {
        "allele_count": node.get("ac"),
        "allele_number": node.get("an"),
        "allele_frequency": node.get("af"),
        # Ancestry-specific frequencies are the point: a variant rare overall
        # can be common in one population, which changes its interpretation.
        "populations": populations,
    }


def variant_frequency(
    variant_id: str, *, dataset: str = DEFAULT_DATASET, **kwargs
) -> Dict[str, Any]:
    """Population allele frequencies for a variant.

    *variant_id* is gnomAD's ``chrom-pos-ref-alt`` form, e.g. ``17-43094464-G-A``.
    """
    if not (variant_id or "").strip():
        raise ApiError(SOURCE_GNOMAD, "a variant id (chrom-pos-ref-alt) is required")
    data = _graphql(
        _VARIANT_QUERY,
        {"id": variant_id.strip(), "dataset": dataset},
        **kwargs,
    )
    variant = data.get("variant")
    if not variant:
        raise ApiError(
            SOURCE_GNOMAD, f"no gnomAD record for {variant_id!r}", status=404
        )
    return {
        "source": SOURCE_GNOMAD,
        "dataset": dataset,
        "variant_id": variant.get("variant_id"),
        "rsids": variant.get("rsids") or [],
        "genome": _frequencies(variant.get("genome")),
        "exome": _frequencies(variant.get("exome")),
    }


def gene_variants(
    symbol: str,
    *,
    reference_genome: str = "GRCh38",
    dataset: str = DEFAULT_DATASET,
    consequence: Optional[str] = None,
    limit: Optional[int] = None,
    **kwargs,
) -> Dict[str, Any]:
    """Variants observed in a gene, newest-frequency-first is *not* implied.

    gnomAD returns them in genomic order and there are often tens of thousands,
    so ``consequence`` (e.g. ``missense_variant``) is the filter that makes the
    result useful rather than merely truncated — and the pre-filter total is
    reported so a caller can see how much was left behind.
    """
    if not (symbol or "").strip():
        raise ApiError(SOURCE_GNOMAD, "a gene symbol is required")
    data = _graphql(
        _GENE_VARIANTS_QUERY,
        {
            "symbol": symbol.strip().upper(),
            "genome": reference_genome,
            "dataset": dataset,
        },
        **kwargs,
    )
    gene = data.get("gene")
    if not gene:
        raise ApiError(SOURCE_GNOMAD, f"no gnomAD gene for {symbol!r}", status=404)

    variants = gene.get("variants") or []
    if consequence:
        wanted = consequence.strip().lower()
        variants = [v for v in variants if (v.get("consequence") or "").lower() == wanted]

    count = bounded_count(limit)
    return {
        "source": SOURCE_GNOMAD,
        "gene_id": gene.get("gene_id"),
        "symbol": symbol.strip().upper(),
        "dataset": dataset,
        "consequence": consequence,
        "total": len(variants),
        "returned": min(len(variants), count),
        "results": [
            {
                "variant_id": v.get("variant_id"),
                "rsids": v.get("rsids") or [],
                "consequence": v.get("consequence"),
                "genome": (v.get("genome") or {}).get("af"),
                "exome": (v.get("exome") or {}).get("af"),
            }
            for v in variants[:count]
        ],
    }


# ── ClinVar ─────────────────────────────────────────────────────────


def _eutils(**kwargs) -> ApiClient:
    import os

    from science.apiclient import contact_email

    return ApiClient(
        SOURCE_CLINVAR, EUTILS_URL,
        default_params={
            "tool": "opencodon",
            "email": contact_email(),
            "api_key": os.environ.get("NCBI_API_KEY") or None,
        },
        **kwargs,
    )


def clinvar_search(query: str, *, limit: Optional[int] = None, **kwargs) -> Dict[str, Any]:
    """Search ClinVar; returns record ids plus the unclipped match total."""
    if not (query or "").strip():
        raise ApiError(SOURCE_CLINVAR, "a search query is required")
    count = bounded_count(limit)
    payload = _eutils(**kwargs).get_json(
        "/esearch.fcgi",
        {"db": "clinvar", "term": query, "retmode": "json", "retmax": count},
    )
    result = payload.get("esearchresult") or {}
    ids = result.get("idlist") or []
    return {
        "source": SOURCE_CLINVAR,
        "query": query,
        "total": int(result.get("count") or 0),
        "returned": len(ids),
        "ids": ids,
    }


def clinvar_records(ids, **kwargs) -> Dict[str, Any]:
    """Clinical significance and review status for ClinVar record ids."""
    if isinstance(ids, str):
        ids = [part.strip() for part in ids.split(",") if part.strip()]
    ids = [str(value).strip() for value in (ids or []) if str(value).strip()]
    if not ids:
        raise ApiError(SOURCE_CLINVAR, "at least one ClinVar id is required")
    ids = ids[: bounded_count(len(ids))]

    payload = _eutils(**kwargs).get_json(
        "/esummary.fcgi",
        {"db": "clinvar", "id": ",".join(ids), "retmode": "json"},
    )
    documents = payload.get("result") or {}
    records: List[Dict[str, Any]] = []
    for uid in documents.get("uids") or []:
        doc = documents.get(uid) or {}
        germline = (doc.get("germline_classification") or {})
        genes = [g.get("symbol") for g in (doc.get("genes") or []) if g.get("symbol")]
        records.append({
            "id": uid,
            "title": doc.get("title"),
            "accession": doc.get("accession"),
            "genes": genes,
            "classification": germline.get("description"),
            # Never returned without its review status: an assertion from one
            # submitter with no criteria is not the same claim as one reviewed
            # by an expert panel, and the word "pathogenic" alone hides that.
            "review_status": germline.get("review_status"),
            "last_evaluated": germline.get("last_evaluated"),
            "variation_type": doc.get("obj_type"),
        })
    return {
        "source": SOURCE_CLINVAR,
        "requested": len(ids),
        "returned": len(records),
        "results": records,
    }
