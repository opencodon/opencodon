"""Europe PMC — the open-access full text, not just the abstract.

This is the one source here that returns whole papers, which makes bounding
the central design problem rather than a detail. A research article runs
30–80k characters; handed back whole it would consume most of a context
window and be paid for again on every subsequent turn.

So full text is returned **as sections** — each clipped, the set capped, and
the ones omitted named rather than silently dropped. A caller who wants the
Methods can ask for the Methods; nobody gets 60k characters by accident.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional
from xml.etree import ElementTree

from science.literature.client import (
    ScholarlyClient,
    ScholarlyError,
    bounded_count,
    clip,
    contact_email,
)

BASE_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest"
SOURCE = "europepmc"

# Per-section and whole-document budgets. A section longer than this is
# truncated with a marker; sections beyond the cap are listed by title so the
# caller knows what exists without paying for it.
SECTION_CHARS = 4000
MAX_SECTIONS = 12


def _client(**kwargs) -> ScholarlyClient:
    return ScholarlyClient(
        SOURCE, BASE_URL, default_params={"email": contact_email()}, **kwargs
    )


def _shape_result(raw: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": raw.get("id"),
        "source_db": raw.get("source"),
        "pmid": raw.get("pmid"),
        "pmcid": raw.get("pmcid"),
        "doi": raw.get("doi"),
        "title": raw.get("title"),
        "journal": ((raw.get("journalInfo") or {}).get("journal") or {}).get("title"),
        "year": raw.get("pubYear"),
        "authors": [
            author.get("fullName")
            for author in ((raw.get("authorList") or {}).get("author") or [])
            if author.get("fullName")
        ][:20],
        "cited_by_count": raw.get("citedByCount"),
        "is_open_access": (raw.get("isOpenAccess") or "N") == "Y",
        "has_full_text": bool((raw.get("fullTextIdList") or {}).get("fullTextId")),
        "abstract": clip(raw.get("abstractText")),
    }


def search(query: str, *, limit: Optional[int] = None, **kwargs) -> Dict[str, Any]:
    """Search Europe PMC.

    Accepts its query syntax — ``DOI:10.1038/…``, ``AUTH:"Doudna J"``,
    ``OPEN_ACCESS:Y`` — or a bare phrase.
    """
    if not (query or "").strip():
        raise ScholarlyError(SOURCE, "a search query is required")
    count = bounded_count(limit)
    payload = _client(**kwargs).get_json(
        "/search",
        {"query": query, "format": "json", "resultType": "core", "pageSize": count},
    )
    results = ((payload.get("resultList") or {}).get("result")) or []
    return {
        "source": SOURCE,
        "query": query,
        "total": payload.get("hitCount"),
        "returned": len(results),
        "results": [_shape_result(item) for item in results],
    }


def _resolve_pmcid(identifier: str, **kwargs) -> str:
    """Accept a PMCID directly, or find one for a DOI/PMID."""
    value = (identifier or "").strip()
    if not value:
        raise ScholarlyError(SOURCE, "an identifier is required")
    if value.upper().startswith("PMC"):
        return value.upper()

    field = "DOI" if value.startswith("10.") else "EXT_ID"
    found = search(f"{field}:{value}", limit=1, **kwargs)
    if not found["results"]:
        raise ScholarlyError(SOURCE, f"no Europe PMC record for {value!r}", status=404)
    pmcid = found["results"][0].get("pmcid")
    if not pmcid:
        raise ScholarlyError(
            SOURCE,
            f"{value!r} is indexed but has no open-access full text in Europe PMC",
            status=404,
        )
    return pmcid


def _section_title(node: ElementTree.Element) -> Optional[str]:
    title = node.find("title")
    if title is None:
        return None
    return " ".join("".join(title.itertext()).split()) or None


def _section_text(node: ElementTree.Element) -> str:
    """Paragraph text of a section, excluding nested subsection bodies."""
    parts = []
    for para in node.findall("p"):
        text = " ".join("".join(para.itertext()).split())
        if text:
            parts.append(text)
    return "\n\n".join(parts)


def full_text(
    identifier: str,
    *,
    section: Optional[str] = None,
    max_sections: int = MAX_SECTIONS,
    **kwargs,
) -> Dict[str, Any]:
    """Open-access full text for a DOI, PMID or PMCID, split into sections.

    Pass *section* to retrieve one section by (case-insensitive, substring)
    title — the way to read Methods without paying for Results.
    """
    pmcid = _resolve_pmcid(identifier, **kwargs)
    try:
        xml = _client(**kwargs).get_text(
            f"/{pmcid}/fullTextXML", accept="application/xml"
        )
    except ScholarlyError as exc:
        # Having a PMCID is not the same as having open-access full text: a
        # record can be indexed in PMC with only the abstract deposited, and
        # the full-text route then 404s. Say that, rather than passing on a
        # bare HTTP code that reads like a broken request.
        if exc.status == 404:
            raise ScholarlyError(
                SOURCE,
                f"{pmcid} is in Europe PMC but its full text is not open access; "
                "only the abstract is available",
                status=404,
            ) from None
        raise
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        raise ScholarlyError(
            SOURCE, f"Europe PMC returned unparseable JATS: {exc}"
        ) from None

    title_node = root.find(".//article-title")
    article_title = (
        " ".join("".join(title_node.itertext()).split()) if title_node is not None else None
    )

    abstract_node = root.find(".//abstract")
    abstract = None
    if abstract_node is not None:
        abstract = clip(" ".join("".join(abstract_node.itertext()).split()))

    sections: List[Dict[str, Any]] = []
    for node in root.findall(".//body//sec"):
        heading = _section_title(node)
        body = _section_text(node)
        if not body:
            continue
        sections.append(
            {"title": heading, "chars": len(body), "text": clip(body, SECTION_CHARS)}
        )

    if section:
        wanted = section.strip().lower()
        matched = [
            entry for entry in sections
            if wanted in (entry["title"] or "").lower()
        ]
        if not matched:
            available = [entry["title"] for entry in sections if entry["title"]]
            raise ScholarlyError(
                SOURCE,
                f"no section matching {section!r}; available: {', '.join(available) or 'none'}",
                status=404,
            )
        sections, omitted = matched, []
    else:
        cap = max(1, min(int(max_sections), MAX_SECTIONS))
        omitted = [entry["title"] for entry in sections[cap:]]
        sections = sections[:cap]

    return {
        "source": SOURCE,
        "pmcid": pmcid,
        "title": article_title,
        "abstract": abstract,
        "returned_sections": len(sections),
        # Named rather than dropped: a caller can see Methods exists and ask
        # for it, which is the whole point of not returning the paper whole.
        "omitted_sections": omitted,
        "sections": sections,
    }
