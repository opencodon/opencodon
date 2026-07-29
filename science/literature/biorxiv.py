"""bioRxiv / medRxiv — preprints, and their journal versions.

Preprints are where life-science results appear first, often a year or more
before the journal version, so anything asking "what is current?" has to look
here rather than only at indexed literature.

The API is date-oriented, not query-oriented: there is no keyword search, only
a date window plus a cursor. Category filtering is therefore done client-side
after the window is fetched, which is a real limitation and is reported in the
response rather than hidden — a caller who filters a narrow window may get
fewer results than the window's total suggests.

The ``/pubs/`` route answers the question that makes preprints trustworthy:
which of these were subsequently published, and where.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from science.literature.client import (
    ScholarlyClient,
    ScholarlyError,
    bounded_count,
    clip,
)

BASE_URL = "https://api.biorxiv.org"
SOURCE = "biorxiv"
SERVERS = ("biorxiv", "medrxiv")


def _client(**kwargs) -> ScholarlyClient:
    return ScholarlyClient(SOURCE, BASE_URL, **kwargs)


def _check_server(server: str) -> str:
    server = (server or "biorxiv").strip().lower()
    if server not in SERVERS:
        raise ScholarlyError(
            SOURCE, f"server must be one of {', '.join(SERVERS)}, got {server!r}"
        )
    return server


def _shape(raw: Dict[str, Any]) -> Dict[str, Any]:
    authors = [a.strip() for a in (raw.get("authors") or "").split(";") if a.strip()]
    return {
        "doi": raw.get("doi"),
        "title": raw.get("title"),
        "authors": authors[:20],
        "author_count": len(authors),
        "date": raw.get("date"),
        "version": raw.get("version"),
        "category": raw.get("category"),
        "type": raw.get("type"),
        "license": raw.get("license"),
        # Present on the /details route; the value is the journal DOI when the
        # preprint has been published, and "NA" when it has not.
        "published_doi": (raw.get("published") or None)
        if (raw.get("published") or "").upper() != "NA"
        else None,
        "server": raw.get("server") or None,
        "abstract": clip(raw.get("abstract")),
    }


def search_preprints(
    date_from: str,
    date_to: str,
    *,
    server: str = "biorxiv",
    category: Optional[str] = None,
    limit: Optional[int] = None,
    cursor: int = 0,
    **kwargs,
) -> Dict[str, Any]:
    """Preprints posted in a date window, optionally narrowed by category.

    Dates are ``YYYY-MM-DD``. ``category`` is applied client-side because the
    upstream route does not accept one.
    """
    server = _check_server(server)
    if not (date_from or "").strip() or not (date_to or "").strip():
        raise ScholarlyError(SOURCE, "both date_from and date_to are required (YYYY-MM-DD)")

    count = bounded_count(limit)
    payload = _client(**kwargs).get_json(
        f"/details/{server}/{date_from}/{date_to}/{int(cursor)}"
    )
    message = (payload.get("messages") or [{}])[0]
    if (message.get("status") or "").lower() not in ("ok", ""):
        raise ScholarlyError(SOURCE, f"{server} rejected the window: {message.get('status')}")

    collection = payload.get("collection") or []
    wanted = (category or "").strip().lower()
    if wanted:
        collection = [
            item for item in collection
            if (item.get("category") or "").lower() == wanted
        ]

    results = [_shape(item) for item in collection[:count]]
    return {
        "source": SOURCE,
        "server": server,
        "window": f"{date_from}:{date_to}",
        "category": wanted or None,
        # `total` describes the whole window upstream; when a category filter
        # is applied it is not the number of matches, so say which is which.
        "window_total": _as_int(message.get("total")),
        "filtered_from": len(payload.get("collection") or []),
        "returned": len(results),
        "results": results,
    }


def get_preprint(doi: str, *, server: str = "biorxiv", **kwargs) -> Dict[str, Any]:
    """One preprint by DOI, including whether it was later published."""
    server = _check_server(server)
    if not (doi or "").strip():
        raise ScholarlyError(SOURCE, "a DOI is required")

    payload = _client(**kwargs).get_json(f"/details/{server}/{doi.strip()}")
    collection = payload.get("collection") or []
    if not collection:
        raise ScholarlyError(SOURCE, f"no {server} preprint for DOI {doi!r}", status=404)
    # Revisions come back oldest-first; the last entry is the current version.
    return {"source": SOURCE, "server": server, **_shape(collection[-1])}


def published_versions(
    date_from: str,
    date_to: str,
    *,
    server: str = "biorxiv",
    limit: Optional[int] = None,
    cursor: int = 0,
    **kwargs,
) -> Dict[str, Any]:
    """Preprints from a window that later appeared in a peer-reviewed journal."""
    server = _check_server(server)
    count = bounded_count(limit)
    payload = _client(**kwargs).get_json(
        f"/pubs/{server}/{date_from}/{date_to}/{int(cursor)}"
    )
    message = (payload.get("messages") or [{}])[0]
    results: List[Dict[str, Any]] = []
    for item in (payload.get("collection") or [])[:count]:
        authors = [
            a.strip() for a in (item.get("preprint_authors") or "").split(";") if a.strip()
        ]
        results.append(
            {
                "preprint_doi": item.get("preprint_doi"),
                "published_doi": item.get("published_doi"),
                "published_journal": item.get("published_journal"),
                "title": item.get("preprint_title"),
                "authors": authors[:20],
                "preprint_date": item.get("preprint_date"),
                "published_date": item.get("published_date"),
            }
        )
    return {
        "source": SOURCE,
        "server": server,
        "window": f"{date_from}:{date_to}",
        "window_total": _as_int(message.get("total")),
        "returned": len(results),
        "results": results,
    }


def _as_int(value: Any) -> Optional[int]:
    """bioRxiv returns counts as strings; callers want to compare them."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
