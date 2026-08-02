"""Bio-data toolset — genes, variants and chemistry from the public databases.

The model-facing surface of ``science/biodata``. Opt-in like the literature
toolset, and for the same reason: every call reaches the public internet.

Errors come back as data. "No gnomAD record for that variant" and "that DOI is
not registered" are answers, not crashes, and a tool loop that has to catch
exceptions to learn them is worse than one that reads a field.
"""

import json

from opencodon.tools.registry import registry


def _genes():
    from opencodon.science.biodata import genes

    return genes


def _variants():
    from opencodon.science.biodata import variants

    return variants


def _chemistry():
    from opencodon.science.biodata import chemistry

    return chemistry


def _expression():
    from opencodon.science.biodata import expression

    return expression


def _structures():
    from opencodon.science.biodata import structures

    return structures


def _clinical():
    from opencodon.science.biodata import clinical

    return clinical


def _call(fn, **kwargs) -> str:
    from opencodon.science.apiclient import ApiError

    try:
        return json.dumps(fn(**kwargs), ensure_ascii=False, default=str)
    except ApiError as exc:
        return json.dumps(exc.as_dict(), ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"error": f"{type(exc).__name__}: {exc}"})


_LIMIT = {"type": "integer", "description": "Max results (default 10, capped at 25)."}


def _register(name, description, properties, required, handler):
    registry.register(
        name=name,
        toolset="biodata",
        schema={
            "name": name,
            "description": description,
            "parameters": {
                "type": "object", "properties": properties, "required": required,
            },
        },
        handler=handler,
    )


# ── genes ───────────────────────────────────────────────────────────


_register(
    "gene_lookup",
    "Genomic coordinates, biotype and description for a gene symbol (Ensembl). "
    "Use gene_resolve first if the symbol might be an alias or non-human.",
    {
        "symbol": {"type": "string", "description": "Gene symbol, e.g. BRCA1."},
        "species": {"type": "string", "description": "Ensembl species (default homo_sapiens)."},
    },
    ["symbol"],
    lambda args, **kw: _call(
        _genes().gene_lookup,
        symbol=args.get("symbol", ""),
        species=args.get("species", "homo_sapiens"),
    ),
)

_register(
    "gene_resolve",
    "Resolve a gene symbol, alias or identifier across naming schemes — returns "
    "Entrez, Ensembl and UniProt ids together. The right first step when a "
    "symbol comes from a paper, since symbols are ambiguous and get renamed.",
    {
        "query": {"type": "string", "description": "Symbol, alias or identifier."},
        "species": {"type": "string", "description": "Species (default human)."},
        "limit": _LIMIT,
    },
    ["query"],
    lambda args, **kw: _call(
        _genes().resolve_gene,
        query=args.get("query", ""),
        species=args.get("species", "human"),
        limit=args.get("limit"),
    ),
)

_register(
    "gene_sequence",
    "Sequence for an Ensembl gene/transcript id. Returns a bounded preview plus "
    "the true length — full sequences are far too large to return whole.",
    {
        "identifier": {"type": "string", "description": "Ensembl id, e.g. ENSG00000012048."},
        "kind": {
            "type": "string",
            "enum": ["genomic", "cdna", "cds", "protein"],
            "description": "Sequence type (default genomic).",
        },
    },
    ["identifier"],
    lambda args, **kw: _call(
        _genes().sequence,
        identifier=args.get("identifier", ""),
        kind=args.get("kind", "genomic"),
    ),
)

_register(
    "gene_homologs",
    "Orthologues and paralogues of a gene, with percent identity.",
    {
        "symbol": {"type": "string", "description": "Gene symbol."},
        "species": {"type": "string", "description": "Source species (default homo_sapiens)."},
        "target": {"type": "string", "description": "Restrict to one target species."},
        "limit": _LIMIT,
    },
    ["symbol"],
    lambda args, **kw: _call(
        _genes().homologs,
        symbol=args.get("symbol", ""),
        species=args.get("species", "homo_sapiens"),
        target=args.get("target"),
        limit=args.get("limit"),
    ),
)

_register(
    "protein_lookup",
    "Reviewed UniProt record for an accession — name, organism, function, "
    "sequence length and a bounded sequence preview.",
    {"accession": {"type": "string", "description": "UniProt accession, e.g. P38398."}},
    ["accession"],
    lambda args, **kw: _call(_genes().protein, accession=args.get("accession", "")),
)


# ── variants ────────────────────────────────────────────────────────


_register(
    "variant_frequency",
    "Population allele frequencies for a variant from gnomAD, broken down by "
    "ancestry. A variant common in any population is unlikely to cause a rare "
    "disease, whatever a prediction tool says. Variant id is chrom-pos-ref-alt, "
    "e.g. 1-55039841-G-T.",
    {
        "variant_id": {"type": "string", "description": "gnomAD id: chrom-pos-ref-alt."},
        "dataset": {"type": "string", "description": "gnomAD dataset (default gnomad_r4)."},
    },
    ["variant_id"],
    lambda args, **kw: _call(
        _variants().variant_frequency,
        variant_id=args.get("variant_id", ""),
        dataset=args.get("dataset", "gnomad_r4"),
    ),
)

_register(
    "gene_variants",
    "Variants observed in a gene (gnomAD). Filter with `consequence` (e.g. "
    "missense_variant) — a gene often carries tens of thousands, and the "
    "pre-filter total is reported so you can see what was left behind.",
    {
        "symbol": {"type": "string", "description": "Gene symbol."},
        "consequence": {
            "type": "string",
            "description": "VEP consequence filter, e.g. missense_variant.",
        },
        "limit": _LIMIT,
    },
    ["symbol"],
    lambda args, **kw: _call(
        _variants().gene_variants,
        symbol=args.get("symbol", ""),
        consequence=args.get("consequence"),
        limit=args.get("limit"),
    ),
)

_register(
    "clinvar_search",
    "Search ClinVar for clinically asserted variants; returns record ids and the "
    "total match count. Follow with clinvar_records.",
    {
        "query": {"type": "string", "description": "ClinVar query, e.g. 'BRCA1 pathogenic'."},
        "limit": _LIMIT,
    },
    ["query"],
    lambda args, **kw: _call(
        _variants().clinvar_search,
        query=args.get("query", ""),
        limit=args.get("limit"),
    ),
)

_register(
    "clinvar_records",
    "Clinical significance for ClinVar record ids. Always returns review_status "
    "alongside the classification: 'pathogenic, single submitter, no criteria' "
    "and 'pathogenic, reviewed by expert panel' are not the same claim.",
    {
        "ids": {
            "type": "array", "items": {"type": "string"},
            "description": "ClinVar record ids from clinvar_search.",
        }
    },
    ["ids"],
    lambda args, **kw: _call(_variants().clinvar_records, ids=args.get("ids") or []),
)


# ── chemistry ───────────────────────────────────────────────────────


_register(
    "compound_lookup",
    "Structure and physicochemical properties for a compound (PubChem): formula, "
    "SMILES, InChIKey, logP, TPSA, H-bond counts.",
    {
        "name": {"type": "string", "description": "Compound name, CID, SMILES or InChIKey."},
        "namespace": {
            "type": "string",
            "enum": ["name", "cid", "smiles", "inchikey"],
            "description": "How to interpret `name` (default name).",
        },
    },
    ["name"],
    lambda args, **kw: _call(
        _chemistry().compound,
        name=args.get("name", ""),
        namespace=args.get("namespace", "name"),
    ),
)

_register(
    "compound_similarity",
    "2D-similarity search over PubChem from a SMILES query, by Tanimoto threshold.",
    {
        "smiles": {"type": "string", "description": "Query SMILES."},
        "threshold": {"type": "integer", "description": "Tanimoto percentage 1-100 (default 90)."},
        "limit": _LIMIT,
    },
    ["smiles"],
    lambda args, **kw: _call(
        _chemistry().similar_compounds,
        smiles=args.get("smiles", ""),
        threshold=args.get("threshold", 90),
        limit=args.get("limit"),
    ),
)

_register(
    "drug_search",
    "Search ChEMBL for drugs and drug-like molecules — clinical phase, ATC "
    "codes, black-box warning, structure.",
    {
        "query": {"type": "string", "description": "Drug name or synonym."},
        "limit": _LIMIT,
    },
    ["query"],
    lambda args, **kw: _call(
        _chemistry().drug_search,
        query=args.get("query", ""),
        limit=args.get("limit"),
    ),
)

_register(
    "drug_bioactivities",
    "Measured bioactivities for a ChEMBL molecule — target, assay, value, units "
    "and pChEMBL, with the source document id so a number can be checked.",
    {
        "chembl_id": {"type": "string", "description": "ChEMBL molecule id, e.g. CHEMBL25."},
        "limit": _LIMIT,
    },
    ["chembl_id"],
    lambda args, **kw: _call(
        _chemistry().bioactivities,
        chembl_id=args.get("chembl_id", ""),
        limit=args.get("limit"),
    ),
)


# ── expression and regulation ───────────────────────────────────────


_register(
    "tissue_expression",
    "Median expression of a gene across GTEx tissues, highest first. Answers "
    "'where is this gene actually expressed' before any hypothesis about what "
    "it does there.",
    {
        "symbol": {"type": "string", "description": "Gene symbol."},
        "limit": _LIMIT,
    },
    ["symbol"],
    lambda args, **kw: _call(
        _expression().tissue_expression,
        symbol=args.get("symbol", ""),
        limit=args.get("limit"),
    ),
)

_register(
    "eqtl_genes",
    "Genes with a significant eQTL in a GTEx tissue (e.g. Liver, "
    "Brain_Cortex) — genes whose expression is under detectable genetic control.",
    {
        "tissue": {"type": "string", "description": "GTEx tissue id, e.g. Liver."},
        "limit": _LIMIT,
    },
    ["tissue"],
    lambda args, **kw: _call(
        _expression().eqtl_genes,
        tissue=args.get("tissue", ""),
        limit=args.get("limit"),
    ),
)

_register(
    "encode_experiments",
    "Search ENCODE for functional-genomics experiments — ChIP-seq, ATAC-seq, "
    "RNA-seq and the rest — by target, assay or biosample.",
    {
        "query": {"type": "string", "description": "Search term, e.g. CTCF."},
        "assay": {"type": "string", "description": "Assay title filter, e.g. 'TF ChIP-seq'."},
        "limit": _LIMIT,
    },
    ["query"],
    lambda args, **kw: _call(
        _expression().encode_experiments,
        query=args.get("query", ""),
        assay=args.get("assay"),
        limit=args.get("limit"),
    ),
)

_register(
    "tf_motifs",
    "Transcription-factor binding matrices from JASPAR. Matrix versions are "
    "distinct models of the same factor and scan differently, so the version "
    "is returned with the id.",
    {
        "query": {"type": "string", "description": "Transcription factor name, e.g. CTCF."},
        "limit": _LIMIT,
    },
    ["query"],
    lambda args, **kw: _call(
        _expression().tf_motifs,
        query=args.get("query", ""),
        limit=args.get("limit"),
    ),
)


# ── structures and interactions ─────────────────────────────────────


_register(
    "pdb_entry",
    "Experimental structure metadata from the PDB, including resolution — a "
    "3.5 A structure does not support the same claims as a 1.2 A one.",
    {"pdb_id": {"type": "string", "description": "PDB id, e.g. 1TUP."}},
    ["pdb_id"],
    lambda args, **kw: _call(_structures().pdb_entry, pdb_id=args.get("pdb_id", "")),
)

_register(
    "pdb_search",
    "Full-text search over the PDB for experimental structures.",
    {
        "query": {"type": "string", "description": "Search query."},
        "limit": _LIMIT,
    },
    ["query"],
    lambda args, **kw: _call(
        _structures().pdb_search,
        query=args.get("query", ""),
        limit=args.get("limit"),
    ),
)

_register(
    "alphafold_model",
    "Predicted structure for a UniProt accession, with mean pLDDT and a named "
    "confidence band. A prediction, not a measurement — and low confidence "
    "often means genuine disorder rather than a bad model.",
    {"accession": {"type": "string", "description": "UniProt accession, e.g. P38398."}},
    ["accession"],
    lambda args, **kw: _call(
        _structures().alphafold_model, accession=args.get("accession", "")
    ),
)

_register(
    "protein_domains",
    "Domain architecture for a protein from InterPro.",
    {
        "accession": {"type": "string", "description": "UniProt accession."},
        "limit": _LIMIT,
    },
    ["accession"],
    lambda args, **kw: _call(
        _structures().protein_domains,
        accession=args.get("accession", ""),
        limit=args.get("limit"),
    ),
)

_register(
    "interaction_network",
    "Protein-protein interaction partners from STRING, with per-channel "
    "evidence scores. A link supported only by text-mining is weaker than one "
    "from experiments, and a combined score alone hides which it is.",
    {
        "identifiers": {
            "type": "array", "items": {"type": "string"},
            "description": "Protein symbols, e.g. ['TP53'].",
        },
        "species": {"type": "integer", "description": "NCBI taxon id (default 9606, human)."},
        "min_score": {"type": "number", "description": "Minimum combined score 0-1 (default 0.4)."},
        "limit": _LIMIT,
    },
    ["identifiers"],
    lambda args, **kw: _call(
        _structures().interaction_network,
        identifiers=args.get("identifiers") or [],
        species=args.get("species", 9606),
        min_score=args.get("min_score", 0.4),
        limit=args.get("limit"),
    ),
)


# ── clinical and regulatory ─────────────────────────────────────────


_register(
    "trial_search",
    "Search ClinicalTrials.gov — phase, status, sponsor, enrolment and primary "
    "endpoints. A registration is not a result: registered, ongoing and "
    "terminated trials all appear and only `status` separates them.",
    {
        "query": {"type": "string", "description": "Condition, intervention or term."},
        "status": {
            "type": "string",
            "enum": ["RECRUITING", "ACTIVE_NOT_RECRUITING", "COMPLETED",
                     "TERMINATED", "WITHDRAWN", "SUSPENDED",
                     "NOT_YET_RECRUITING", "UNKNOWN"],
            "description": "Filter by overall status.",
        },
        "phase": {"type": "string", "description": "Phase filter, e.g. PHASE3."},
        "limit": _LIMIT,
    },
    ["query"],
    lambda args, **kw: _call(
        _clinical().trial_search,
        query=args.get("query", ""),
        status=args.get("status"),
        phase=args.get("phase"),
        limit=args.get("limit"),
    ),
)

_register(
    "trial_record",
    "One clinical trial by NCT identifier.",
    {"nct_id": {"type": "string", "description": "NCT id, e.g. NCT01234567."}},
    ["nct_id"],
    lambda args, **kw: _call(_clinical().trial_record, nct_id=args.get("nct_id", "")),
)

_register(
    "drug_label",
    "FDA structured product label — indications, warnings, boxed warning and "
    "contraindications. Research use only, not medical advice.",
    {
        "query": {"type": "string", "description": "Brand or generic drug name."},
        "limit": _LIMIT,
    },
    ["query"],
    lambda args, **kw: _call(
        _clinical().drug_label,
        query=args.get("query", ""),
        limit=args.get("limit"),
    ),
)

_register(
    "drug_approvals",
    "Drugs@FDA application records — sponsor, products, first approval date "
    "and marketing status.",
    {
        "query": {"type": "string", "description": "Brand or generic drug name."},
        "limit": _LIMIT,
    },
    ["query"],
    lambda args, **kw: _call(
        _clinical().drug_approvals,
        query=args.get("query", ""),
        limit=args.get("limit"),
    ),
)
