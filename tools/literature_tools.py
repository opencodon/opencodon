"""Literature toolset — scholarly search, DOI resolution, and citation graph.

The model-facing surface of ``science/literature``: OpenAlex for discovery and
citation navigation, Crossref for authoritative DOI metadata, PubMed for
biomedical search and identifier conversion.

Opt-in rather than core. Unlike ``run_code``, these tools reach the public
internet on every call, so a session gets them by asking (``--toolsets
literature``) rather than by default.

Every handler funnels :class:`ScholarlyError` into a structured ``{"error",
"source", "status"}`` payload. A failed lookup is information — "that DOI is
not registered" is a useful answer — so it is returned as data rather than
raised into the tool loop as a crash.
"""

import json

from tools.registry import registry


def _openalex():
    from science.literature import openalex

    return openalex


def _crossref():
    from science.literature import crossref

    return crossref


def _pubmed():
    from science.literature import pubmed

    return pubmed


def _call(fn, **kwargs) -> str:
    """Run a literature call, rendering both outcomes as JSON for the model."""
    from science.literature.client import ScholarlyError

    try:
        return json.dumps(fn(**kwargs), ensure_ascii=False, default=str)
    except ScholarlyError as exc:
        return json.dumps(exc.as_dict(), ensure_ascii=False)
    except Exception as exc:  # transport bugs, unexpected upstream shapes
        return json.dumps({"error": f"{type(exc).__name__}: {exc}"})


_LIMIT_SCHEMA = {
    "type": "integer",
    "description": "Max results (default 10, capped at 25).",
}


# ── OpenAlex ────────────────────────────────────────────────────────


registry.register(
    name="literature_search",
    toolset="literature",
    schema={
        "name": "literature_search",
        "description": (
            "Search scholarly works across every discipline via OpenAlex. "
            "Returns title, authors, year, venue, citation count and abstract. "
            "Use for discovery; use literature_work for a known DOI."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Free-text search query."},
                "limit": _LIMIT_SCHEMA,
                "year_from": {
                    "type": "integer",
                    "description": "Only works published in or after this year.",
                },
            },
            "required": ["query"],
        },
    },
    handler=lambda args, **kw: _call(
        _openalex().search_works,
        query=args.get("query", ""),
        limit=args.get("limit"),
        year_from=args.get("year_from"),
    ),
)


registry.register(
    name="literature_work",
    toolset="literature",
    schema={
        "name": "literature_work",
        "description": (
            "Fetch one scholarly work by DOI or OpenAlex id. Accepts a bare "
            "DOI, a doi.org URL, or an OpenAlex work id."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "work_id": {"type": "string", "description": "DOI or OpenAlex id."}
            },
            "required": ["work_id"],
        },
    },
    handler=lambda args, **kw: _call(
        _openalex().get_work,
        work_id=args.get("work_id", ""),
    ),
)


registry.register(
    name="literature_citations",
    toolset="literature",
    schema={
        "name": "literature_citations",
        "description": (
            "Navigate the citation graph for a work. direction='cited_by' "
            "returns later work that cites it (what it influenced); "
            "direction='references' returns what it cited (what it built on)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "work_id": {"type": "string", "description": "DOI or OpenAlex id."},
                "direction": {
                    "type": "string",
                    "enum": ["cited_by", "references"],
                    "description": "Graph direction (default cited_by).",
                },
                "limit": _LIMIT_SCHEMA,
            },
            "required": ["work_id"],
        },
    },
    handler=lambda args, **kw: _call(
        (
            _openalex().references
            if args.get("direction") == "references"
            else _openalex().cited_by
        ),
        work_id=args.get("work_id", ""),
        limit=args.get("limit"),
    ),
)


# ── Crossref ────────────────────────────────────────────────────────


registry.register(
    name="literature_doi",
    toolset="literature",
    schema={
        "name": "literature_doi",
        "description": (
            "Resolve a DOI to authoritative Crossref metadata — the record the "
            "publisher deposited. Prefer this over literature_work when the "
            "exact title, journal or publication date matters."
        ),
        "parameters": {
            "type": "object",
            "properties": {"doi": {"type": "string", "description": "DOI to resolve."}},
            "required": ["doi"],
        },
    },
    handler=lambda args, **kw: _call(
        _crossref().get_work,
        doi=args.get("doi", ""),
    ),
)


# ── PubMed ──────────────────────────────────────────────────────────


registry.register(
    name="pubmed_search",
    toolset="literature",
    schema={
        "name": "pubmed_search",
        "description": (
            "Search PubMed and return matching PMIDs plus the total match "
            "count. Supports PubMed query syntax (MeSH terms, field tags like "
            "[au]/[ti], boolean operators). Follow with pubmed_fetch for "
            "titles and abstracts."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "PubMed query."},
                "limit": _LIMIT_SCHEMA,
            },
            "required": ["query"],
        },
    },
    handler=lambda args, **kw: _call(
        _pubmed().search,
        query=args.get("query", ""),
        limit=args.get("limit"),
    ),
)


registry.register(
    name="pubmed_fetch",
    toolset="literature",
    schema={
        "name": "pubmed_fetch",
        "description": (
            "Fetch PubMed records by PMID — title, abstract, journal, authors "
            "and date. Up to 25 PMIDs per call."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pmids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "PMIDs to fetch.",
                }
            },
            "required": ["pmids"],
        },
    },
    handler=lambda args, **kw: _call(
        _pubmed().fetch,
        pmids=args.get("pmids") or [],
    ),
)


registry.register(
    name="literature_convert_ids",
    toolset="literature",
    schema={
        "name": "literature_convert_ids",
        "description": (
            "Convert between DOI, PMID and PMCID. Accepts a mix of schemes; "
            "identifiers with no mapping are returned marked, not dropped."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Identifiers (DOI, PMID, or PMCID).",
                }
            },
            "required": ["ids"],
        },
    },
    handler=lambda args, **kw: _call(
        _pubmed().convert_ids,
        ids=args.get("ids") or [],
    ),
)
