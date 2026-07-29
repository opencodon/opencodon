"""Genes, genomes and proteins — Ensembl, MyGene, UniProt.

The identifier layer. Almost every other bio question starts by resolving
"BRCA1" into something a database will accept, and the schemes do not agree:
Ensembl speaks ENSG, NCBI speaks Entrez, UniProt speaks accessions, and the
literature speaks symbols that are ambiguous across species and get renamed.

So these three are grouped: Ensembl for genomic coordinates and sequence,
MyGene for cross-scheme identifier resolution, UniProt for the protein.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from science.apiclient import ApiClient, ApiError, bounded_count, clip

ENSEMBL_URL = "https://rest.ensembl.org"
MYGENE_URL = "https://mygene.info/v3"
UNIPROT_URL = "https://rest.uniprot.org"

SOURCE_ENSEMBL = "ensembl"
SOURCE_MYGENE = "mygene"
SOURCE_UNIPROT = "uniprot"

DEFAULT_SPECIES = "homo_sapiens"
# Sequence is the one field here that can be megabytes; a whole gene's DNA
# would swamp a context window for no benefit over a length and a head.
SEQUENCE_PREVIEW_CHARS = 1000


def _require(value: str, what: str, source: str) -> str:
    if not (value or "").strip():
        raise ApiError(source, f"a {what} is required")
    return value.strip()


# ── Ensembl ─────────────────────────────────────────────────────────


def gene_lookup(
    symbol: str, *, species: str = DEFAULT_SPECIES, **kwargs
) -> Dict[str, Any]:
    """Genomic coordinates and biotype for a gene symbol."""
    symbol = _require(symbol, "gene symbol", SOURCE_ENSEMBL)
    payload = ApiClient(SOURCE_ENSEMBL, ENSEMBL_URL, **kwargs).get_json(
        f"/lookup/symbol/{species}/{symbol}", {"expand": 0}
    )
    return {
        "source": SOURCE_ENSEMBL,
        "gene_id": payload.get("id"),
        "symbol": payload.get("display_name"),
        "description": payload.get("description"),
        "biotype": payload.get("biotype"),
        "assembly": payload.get("assembly_name"),
        "chromosome": payload.get("seq_region_name"),
        "start": payload.get("start"),
        "end": payload.get("end"),
        "strand": payload.get("strand"),
        "canonical_transcript": payload.get("canonical_transcript"),
        "species": species,
    }


def sequence(
    identifier: str, *, kind: str = "genomic", **kwargs
) -> Dict[str, Any]:
    """Sequence for an Ensembl id, previewed rather than returned whole."""
    identifier = _require(identifier, "an Ensembl identifier", SOURCE_ENSEMBL)
    payload = ApiClient(SOURCE_ENSEMBL, ENSEMBL_URL, **kwargs).get_json(
        f"/sequence/id/{identifier}", {"type": kind}
    )
    seq = payload.get("seq") or ""
    return {
        "source": SOURCE_ENSEMBL,
        "id": payload.get("id"),
        "type": kind,
        "molecule": payload.get("molecule"),
        # The full length is reported even though the text is cut, so a caller
        # can tell a truncated preview from a short sequence.
        "length": len(seq),
        "sequence_preview": seq[:SEQUENCE_PREVIEW_CHARS],
        "truncated": len(seq) > SEQUENCE_PREVIEW_CHARS,
    }


def homologs(
    symbol: str, *, species: str = DEFAULT_SPECIES, target: Optional[str] = None,
    limit: Optional[int] = None, **kwargs,
) -> Dict[str, Any]:
    """Orthologues and paralogues of a gene."""
    symbol = _require(symbol, "gene symbol", SOURCE_ENSEMBL)
    params: Dict[str, Any] = {"content-type": "application/json"}
    if target:
        params["target_species"] = target
    payload = ApiClient(SOURCE_ENSEMBL, ENSEMBL_URL, **kwargs).get_json(
        f"/homology/symbol/{species}/{symbol}", params
    )
    entries = (payload.get("data") or [{}])[0].get("homologies") or []
    count = bounded_count(limit)
    return {
        "source": SOURCE_ENSEMBL,
        "symbol": symbol,
        "total": len(entries),
        "returned": min(len(entries), count),
        "results": [
            {
                "species": entry.get("target", {}).get("species"),
                "gene_id": entry.get("target", {}).get("id"),
                "protein_id": entry.get("target", {}).get("protein_id"),
                "type": entry.get("type"),
                "identity": entry.get("target", {}).get("perc_id"),
            }
            for entry in entries[:count]
        ],
    }


# ── MyGene ──────────────────────────────────────────────────────────


def resolve_gene(
    query: str, *, species: str = "human", limit: Optional[int] = None, **kwargs
) -> Dict[str, Any]:
    """Resolve a symbol, alias or identifier across naming schemes.

    The step most bio questions actually start with — a symbol from a paper is
    ambiguous across species and may since have been renamed.
    """
    query = _require(query, "gene query", SOURCE_MYGENE)
    count = bounded_count(limit)
    payload = ApiClient(SOURCE_MYGENE, MYGENE_URL, **kwargs).get_json(
        "/query",
        {"q": query, "species": species, "size": count,
         "fields": "symbol,name,entrezgene,ensembl.gene,uniprot.Swiss-Prot,taxid,alias"},
    )
    hits = payload.get("hits") or []
    results = []
    for hit in hits:
        ensembl = hit.get("ensembl") or {}
        if isinstance(ensembl, list):  # multi-mapping genes come back as a list
            ensembl = ensembl[0] if ensembl else {}
        uniprot = hit.get("uniprot") or {}
        results.append({
            "symbol": hit.get("symbol"),
            "name": hit.get("name"),
            "entrez_id": hit.get("entrezgene"),
            "ensembl_id": ensembl.get("gene"),
            "uniprot_id": uniprot.get("Swiss-Prot"),
            "taxid": hit.get("taxid"),
            "score": hit.get("_score"),
        })
    return {
        "source": SOURCE_MYGENE,
        "query": query,
        "total": payload.get("total"),
        "returned": len(results),
        "results": results,
    }


# ── UniProt ─────────────────────────────────────────────────────────


def protein(accession: str, **kwargs) -> Dict[str, Any]:
    """Reviewed protein record for a UniProt accession."""
    accession = _require(accession, "UniProt accession", SOURCE_UNIPROT)
    payload = ApiClient(SOURCE_UNIPROT, UNIPROT_URL, **kwargs).get_json(
        f"/uniprotkb/{accession}",
        {"fields": "accession,id,protein_name,gene_names,organism_name,"
                   "length,sequence,cc_function,ft_domain"},
    )
    description = (payload.get("proteinDescription") or {}).get("recommendedName") or {}
    functions: List[str] = []
    for comment in payload.get("comments") or []:
        if comment.get("commentType") == "FUNCTION":
            for text in comment.get("texts") or []:
                if text.get("value"):
                    functions.append(text["value"])
    seq = (payload.get("sequence") or {}).get("value") or ""
    return {
        "source": SOURCE_UNIPROT,
        "accession": payload.get("primaryAccession"),
        "id": payload.get("uniProtkbId"),
        "name": (description.get("fullName") or {}).get("value"),
        "genes": [
            (gene.get("geneName") or {}).get("value")
            for gene in (payload.get("genes") or [])
            if (gene.get("geneName") or {}).get("value")
        ],
        "organism": (payload.get("organism") or {}).get("scientificName"),
        "length": len(seq),
        "sequence_preview": seq[:SEQUENCE_PREVIEW_CHARS],
        "truncated": len(seq) > SEQUENCE_PREVIEW_CHARS,
        "function": clip(" ".join(functions)) if functions else None,
    }
