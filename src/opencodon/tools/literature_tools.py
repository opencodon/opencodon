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

from opencodon.tools.registry import registry


def _openalex():
    from opencodon.science.literature import openalex

    return openalex


def _crossref():
    from opencodon.science.literature import crossref

    return crossref


def _pubmed():
    from opencodon.science.literature import pubmed

    return pubmed


def _biorxiv():
    from opencodon.science.literature import biorxiv

    return biorxiv


def _arxiv():
    from opencodon.science.literature import arxiv

    return arxiv


def _europepmc():
    from opencodon.science.literature import europepmc

    return europepmc


def _call(fn, **kwargs) -> str:
    """Run a literature call, rendering both outcomes as JSON for the model."""
    from opencodon.science.literature.client import ScholarlyError

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


# ── preprints ───────────────────────────────────────────────────────


registry.register(
    name="preprint_search",
    toolset="literature",
    schema={
        "name": "preprint_search",
        "description": (
            "List bioRxiv or medRxiv preprints posted in a date window. NOTE: "
            "this API has no keyword search — it filters by date and category "
            "only, so use literature_search or pubmed_search to find a topic "
            "and this to survey what is new in a field."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "Window start, YYYY-MM-DD."},
                "date_to": {"type": "string", "description": "Window end, YYYY-MM-DD."},
                "server": {
                    "type": "string",
                    "enum": ["biorxiv", "medrxiv"],
                    "description": "Preprint server (default biorxiv).",
                },
                "category": {
                    "type": "string",
                    "description": "Subject category, e.g. 'neuroscience' (applied client-side).",
                },
                "limit": _LIMIT_SCHEMA,
            },
            "required": ["date_from", "date_to"],
        },
    },
    handler=lambda args, **kw: _call(
        _biorxiv().search_preprints,
        date_from=args.get("date_from", ""),
        date_to=args.get("date_to", ""),
        server=args.get("server", "biorxiv"),
        category=args.get("category"),
        limit=args.get("limit"),
    ),
)


registry.register(
    name="preprint_get",
    toolset="literature",
    schema={
        "name": "preprint_get",
        "description": (
            "Fetch one bioRxiv/medRxiv preprint by DOI, including whether it "
            "was later published in a peer-reviewed journal."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "doi": {"type": "string", "description": "Preprint DOI (10.1101/...)."},
                "server": {
                    "type": "string",
                    "enum": ["biorxiv", "medrxiv"],
                    "description": "Preprint server (default biorxiv).",
                },
            },
            "required": ["doi"],
        },
    },
    handler=lambda args, **kw: _call(
        _biorxiv().get_preprint,
        doi=args.get("doi", ""),
        server=args.get("server", "biorxiv"),
    ),
)


registry.register(
    name="preprint_published_versions",
    toolset="literature",
    schema={
        "name": "preprint_published_versions",
        "description": (
            "Preprints from a date window that subsequently appeared in a "
            "journal, with the published DOI and journal name. Use to gauge "
            "how much of a field's preprint output has cleared peer review."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "Window start, YYYY-MM-DD."},
                "date_to": {"type": "string", "description": "Window end, YYYY-MM-DD."},
                "server": {
                    "type": "string",
                    "enum": ["biorxiv", "medrxiv"],
                    "description": "Preprint server (default biorxiv).",
                },
                "limit": _LIMIT_SCHEMA,
            },
            "required": ["date_from", "date_to"],
        },
    },
    handler=lambda args, **kw: _call(
        _biorxiv().published_versions,
        date_from=args.get("date_from", ""),
        date_to=args.get("date_to", ""),
        server=args.get("server", "biorxiv"),
        limit=args.get("limit"),
    ),
)


registry.register(
    name="arxiv_search",
    toolset="literature",
    schema={
        "name": "arxiv_search",
        "description": (
            "Search arXiv — physics, maths, CS, quantitative biology, stats. "
            "Supports arXiv field syntax: 'au:hinton', 'cat:q-bio.GN', "
            "'ti:transformer AND cat:cs.LG'. A bare phrase searches all fields."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "arXiv query."},
                "limit": _LIMIT_SCHEMA,
                "sort_by": {
                    "type": "string",
                    "enum": ["relevance", "lastUpdatedDate", "submittedDate"],
                    "description": "Sort order (default relevance).",
                },
            },
            "required": ["query"],
        },
    },
    handler=lambda args, **kw: _call(
        _arxiv().search,
        query=args.get("query", ""),
        limit=args.get("limit"),
        sort_by=args.get("sort_by", "relevance"),
    ),
)


# ── open-access full text ───────────────────────────────────────────


registry.register(
    name="fulltext_search",
    toolset="literature",
    schema={
        "name": "fulltext_search",
        "description": (
            "Search Europe PMC, which indexes full text rather than just "
            "abstracts. Supports its syntax: 'DOI:10.1038/...', "
            "'AUTH:\"Doudna J\"', 'OPEN_ACCESS:Y'. Results flag which records "
            "have retrievable full text."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Europe PMC query."},
                "limit": _LIMIT_SCHEMA,
            },
            "required": ["query"],
        },
    },
    handler=lambda args, **kw: _call(
        _europepmc().search,
        query=args.get("query", ""),
        limit=args.get("limit"),
    ),
)


registry.register(
    name="fulltext_get",
    toolset="literature",
    schema={
        "name": "fulltext_get",
        "description": (
            "Retrieve open-access full text by DOI, PMID or PMCID, returned as "
            "sections rather than one blob. Pass `section` to read just one "
            "(e.g. 'methods') — sections not returned are named so you can ask "
            "for them without fetching the whole paper."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "identifier": {"type": "string", "description": "DOI, PMID or PMCID."},
                "section": {
                    "type": "string",
                    "description": "Optional section title to return alone, e.g. 'methods'.",
                },
            },
            "required": ["identifier"],
        },
    },
    handler=lambda args, **kw: _call(
        _europepmc().full_text,
        identifier=args.get("identifier", ""),
        section=args.get("section"),
    ),
)
