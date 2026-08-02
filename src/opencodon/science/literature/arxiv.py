"""arXiv — preprints in physics, maths, CS, quantitative biology and stats.

The complement to bioRxiv: where computational and methods work appears
first. Unlike bioRxiv it has real query syntax (field prefixes ``all:``,
``ti:``, ``au:``, ``cat:``, joined with AND/OR/ANDNOT), so this is a search
interface rather than a date window.

The API answers in Atom rather than JSON, which is why this module parses XML
where its neighbours read dicts.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from xml.etree import ElementTree

from opencodon.science.literature.client import (
    ScholarlyClient,
    ScholarlyError,
    bounded_count,
    clip,
)

BASE_URL = "https://export.arxiv.org"
SOURCE = "arxiv"

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
    "opensearch": "http://a9.com/-/spec/opensearch/1.1/",
}

SORT_FIELDS = ("relevance", "lastUpdatedDate", "submittedDate")


def _text(node: Optional[ElementTree.Element]) -> Optional[str]:
    if node is None:
        return None
    return " ".join("".join(node.itertext()).split()) or None


def _shape_entry(entry: ElementTree.Element) -> Dict[str, Any]:
    authors = [
        _text(node.find("atom:name", NS))
        for node in entry.findall("atom:author", NS)
    ]
    authors = [name for name in authors if name]

    pdf_url = None
    for link in entry.findall("atom:link", NS):
        if link.get("title") == "pdf":
            pdf_url = link.get("href")

    categories = [
        node.get("term")
        for node in entry.findall("atom:category", NS)
        if node.get("term")
    ]
    # The id is a versioned abs URL; the bare id is what people cite.
    abs_url = _text(entry.find("atom:id", NS)) or ""
    arxiv_id = abs_url.rsplit("/abs/", 1)[-1] if "/abs/" in abs_url else abs_url

    return {
        "arxiv_id": arxiv_id,
        "url": abs_url or None,
        "pdf_url": pdf_url,
        "title": _text(entry.find("atom:title", NS)),
        "authors": authors[:20],
        "author_count": len(authors),
        "published": _text(entry.find("atom:published", NS)),
        "updated": _text(entry.find("atom:updated", NS)),
        "primary_category": (
            entry.find("arxiv:primary_category", NS).get("term")
            if entry.find("arxiv:primary_category", NS) is not None
            else (categories[0] if categories else None)
        ),
        "categories": categories,
        "doi": _text(entry.find("arxiv:doi", NS)),
        "comment": clip(_text(entry.find("arxiv:comment", NS)), 300),
        "summary": clip(_text(entry.find("atom:summary", NS))),
    }


def search(
    query: str,
    *,
    limit: Optional[int] = None,
    sort_by: str = "relevance",
    **kwargs,
) -> Dict[str, Any]:
    """Search arXiv.

    *query* accepts arXiv's own field syntax — ``au:hinton``,
    ``cat:q-bio.GN``, ``ti:transformer AND cat:cs.LG``. A bare phrase is
    searched across all fields.
    """
    if not (query or "").strip():
        raise ScholarlyError(SOURCE, "a search query is required")
    if sort_by not in SORT_FIELDS:
        raise ScholarlyError(
            SOURCE, f"sort_by must be one of {', '.join(SORT_FIELDS)}, got {sort_by!r}"
        )

    count = bounded_count(limit)
    # A bare phrase would otherwise be parsed as a field-less term and match
    # nothing useful, so qualify it.
    search_query = query if ":" in query else f"all:{query}"

    xml = _client(**kwargs).get_text(
        "/api/query",
        {
            "search_query": search_query,
            "max_results": count,
            "sortBy": sort_by,
            "sortOrder": "descending",
        },
        accept="application/atom+xml",
    )
    return _parse_feed(xml, query)


def get_papers(arxiv_ids, **kwargs) -> Dict[str, Any]:
    """Fetch specific papers by arXiv id (with or without a version suffix)."""
    if isinstance(arxiv_ids, str):
        arxiv_ids = [part.strip() for part in arxiv_ids.split(",") if part.strip()]
    arxiv_ids = [str(value).strip() for value in (arxiv_ids or []) if str(value).strip()]
    if not arxiv_ids:
        raise ScholarlyError(SOURCE, "at least one arXiv id is required")
    arxiv_ids = arxiv_ids[: bounded_count(len(arxiv_ids))]

    xml = _client(**kwargs).get_text(
        "/api/query",
        {"id_list": ",".join(arxiv_ids), "max_results": len(arxiv_ids)},
        accept="application/atom+xml",
    )
    return _parse_feed(xml, ",".join(arxiv_ids))


def _client(**kwargs) -> ScholarlyClient:
    return ScholarlyClient(SOURCE, BASE_URL, **kwargs)


def _parse_feed(xml: str, query: str) -> Dict[str, Any]:
    try:
        feed = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        raise ScholarlyError(SOURCE, f"arXiv returned unparseable Atom: {exc}") from None

    entries: List[Dict[str, Any]] = [
        _shape_entry(entry) for entry in feed.findall("atom:entry", NS)
    ]
    total = _text(feed.find("opensearch:totalResults", NS))
    try:
        total = int(total) if total is not None else None
    except ValueError:
        total = None

    return {
        "source": SOURCE,
        "query": query,
        "total": total,
        "returned": len(entries),
        "results": entries,
    }
