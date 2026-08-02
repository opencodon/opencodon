"""OpenAlex — works, authors, venues and the citation graph.

OpenAlex (OurResearch) is the open successor to Microsoft Academic Graph. It
is the only source here that exposes the citation graph in both directions,
which is what makes "what built on this?" answerable rather than just "what
did this cite?".

One shape quirk drives most of the code below: OpenAlex does not return
abstracts as text. It returns ``abstract_inverted_index`` — a token → position
map — because a positional index is not a reproduction of the abstract for
copyright purposes. Reconstructing it is expected client behaviour, not a
workaround.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from opencodon.science.literature.client import (
    ScholarlyClient,
    ScholarlyError,
    bounded_count,
    clip,
    contact_email,
)

BASE_URL = "https://api.openalex.org"
SOURCE = "openalex"

# Asking for only the fields we shape keeps responses small and, more to the
# point, keeps a schema change upstream from silently widening our payload.
WORK_FIELDS = (
    "id,doi,title,publication_year,type,cited_by_count,"
    "authorships,primary_location,abstract_inverted_index,referenced_works"
)


def _client(**kwargs) -> ScholarlyClient:
    return ScholarlyClient(
        SOURCE, BASE_URL, default_params={"mailto": contact_email()}, **kwargs
    )


def reconstruct_abstract(index: Optional[Dict[str, List[int]]]) -> Optional[str]:
    """Rebuild abstract text from OpenAlex's inverted index.

    Positions are absolute across the whole abstract, so the reconstruction is
    a scatter into a sparse list rather than a concatenation. Gaps are possible
    when the upstream index is incomplete; they are dropped rather than
    rendered as blanks.
    """
    if not index:
        return None
    positioned: Dict[int, str] = {}
    for token, positions in index.items():
        for position in positions or []:
            positioned[position] = token
    if not positioned:
        return None
    return " ".join(positioned[i] for i in sorted(positioned))


def _shape_work(raw: Dict[str, Any]) -> Dict[str, Any]:
    """One work, reduced to the fields a reader actually needs."""
    location = raw.get("primary_location") or {}
    venue = (location.get("source") or {}).get("display_name")
    authors = [
        (authorship.get("author") or {}).get("display_name")
        for authorship in (raw.get("authorships") or [])
    ]
    return {
        "id": raw.get("id"),
        "doi": raw.get("doi"),
        "title": raw.get("title"),
        "year": raw.get("publication_year"),
        "type": raw.get("type"),
        "venue": venue,
        # Long author lists are the single biggest payload risk here — a
        # consortium paper can carry thousands.
        "authors": [name for name in authors if name][:20],
        "author_count": len(authors),
        "cited_by_count": raw.get("cited_by_count"),
        "abstract": clip(reconstruct_abstract(raw.get("abstract_inverted_index"))),
    }


def _normalise_id(work_id: str) -> str:
    """Accept a DOI, a bare OpenAlex id, or a full OpenAlex URL."""
    work_id = (work_id or "").strip()
    if not work_id:
        raise ScholarlyError(SOURCE, "a work id or DOI is required")
    lowered = work_id.lower()
    if lowered.startswith("10."):
        return f"doi:{work_id}"
    if lowered.startswith(("http://", "https://")):
        if "doi.org/" in lowered:
            return f"doi:{work_id.split('doi.org/', 1)[1]}"
        return work_id.rsplit("/", 1)[-1]
    return work_id


def search_works(
    query: str, *, limit: Optional[int] = None, year_from: Optional[int] = None, **kwargs
) -> Dict[str, Any]:
    """Full-text search over works."""
    if not (query or "").strip():
        raise ScholarlyError(SOURCE, "a search query is required")
    count = bounded_count(limit)
    params: Dict[str, Any] = {
        "search": query,
        "per-page": count,
        "select": WORK_FIELDS,
    }
    if year_from:
        params["filter"] = f"from_publication_date:{int(year_from)}-01-01"

    payload = _client(**kwargs).get_json("/works", params)
    results = payload.get("results") or []
    return {
        "source": SOURCE,
        "query": query,
        "total": (payload.get("meta") or {}).get("count"),
        "returned": len(results),
        "results": [_shape_work(work) for work in results],
    }


def get_work(work_id: str, **kwargs) -> Dict[str, Any]:
    """One work by DOI or OpenAlex id."""
    payload = _client(**kwargs).get_json(
        f"/works/{_normalise_id(work_id)}", {"select": WORK_FIELDS}
    )
    return {"source": SOURCE, **_shape_work(payload)}


def cited_by(work_id: str, *, limit: Optional[int] = None, **kwargs) -> Dict[str, Any]:
    """Works that cite *work_id* — the forward edge of the citation graph."""
    resolved = get_work(work_id, **kwargs)
    openalex_id = (resolved.get("id") or "").rsplit("/", 1)[-1]
    count = bounded_count(limit)
    payload = _client(**kwargs).get_json(
        "/works",
        {
            "filter": f"cites:{openalex_id}",
            "per-page": count,
            "select": WORK_FIELDS,
        },
    )
    results = payload.get("results") or []
    return {
        "source": SOURCE,
        "direction": "cited_by",
        "work": resolved.get("id"),
        "total": (payload.get("meta") or {}).get("count"),
        "returned": len(results),
        "results": [_shape_work(work) for work in results],
    }


def references(work_id: str, *, limit: Optional[int] = None, **kwargs) -> Dict[str, Any]:
    """Works cited *by* ``work_id`` — the backward edge."""
    resolved_raw = _client(**kwargs).get_json(
        f"/works/{_normalise_id(work_id)}", {"select": "id,referenced_works"}
    )
    referenced = resolved_raw.get("referenced_works") or []
    count = bounded_count(limit)
    wanted = [ref.rsplit("/", 1)[-1] for ref in referenced[:count]]

    results: List[Dict[str, Any]] = []
    if wanted:
        payload = _client(**kwargs).get_json(
            "/works",
            {
                "filter": f"openalex_id:{'|'.join(wanted)}",
                "per-page": count,
                "select": WORK_FIELDS,
            },
        )
        results = [_shape_work(work) for work in payload.get("results") or []]

    return {
        "source": SOURCE,
        "direction": "references",
        "work": resolved_raw.get("id"),
        "total": len(referenced),
        "returned": len(results),
        "results": results,
    }
