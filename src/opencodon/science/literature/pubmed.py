"""PubMed / PMC via NCBI E-utilities, plus ID conversion.

PubMed is the primary biomedical index — for clinical and life-science work it
is the source, not one of several. Its API is older than the others here and
shows it in two ways:

- **Search and fetch are separate calls.** ESearch returns PMIDs and a total;
  the records themselves need a second EFetch. Both are exposed rather than
  hidden behind one call, because a caller often wants the count without
  paying for the records.
- **Abstracts are XML only.** ESummary is JSON but omits the abstract, so
  anything that actually wants the text has to parse EFetch's XML.

NCBI's usage policy asks for a tool name and contact address, and grants a
higher rate ceiling to clients that send an API key — honoured via
``NCBI_API_KEY`` when present.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional
from xml.etree import ElementTree

from opencodon.science.literature.client import (
    ScholarlyClient,
    ScholarlyError,
    bounded_count,
    clip,
    contact_email,
)

EUTILS_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
IDCONV_URL = "https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0"
SOURCE = "pubmed"


def _default_params() -> Dict[str, Any]:
    return {
        "tool": "opencodon",
        "email": contact_email(),
        "api_key": os.environ.get("NCBI_API_KEY") or None,
    }


def _client(base: str = EUTILS_URL, **kwargs) -> ScholarlyClient:
    return ScholarlyClient(SOURCE, base, default_params=_default_params(), **kwargs)


# ── search ──────────────────────────────────────────────────────────


def search(query: str, *, limit: Optional[int] = None, **kwargs) -> Dict[str, Any]:
    """ESearch — PMIDs matching *query*, with the unclipped total."""
    if not (query or "").strip():
        raise ScholarlyError(SOURCE, "a search query is required")
    count = bounded_count(limit)
    payload = _client(**kwargs).get_json(
        "/esearch.fcgi",
        {"db": "pubmed", "term": query, "retmode": "json", "retmax": count},
    )
    result = payload.get("esearchresult") or {}
    pmids = result.get("idlist") or []
    return {
        "source": SOURCE,
        "query": query,
        # The total is what the query actually matched, independent of the
        # page size — the number a caller needs to judge whether to narrow.
        "total": int(result.get("count") or 0),
        "returned": len(pmids),
        "pmids": pmids,
    }


# ── fetch ───────────────────────────────────────────────────────────


def _text(node: Optional[ElementTree.Element]) -> Optional[str]:
    if node is None:
        return None
    return " ".join("".join(node.itertext()).split()) or None


def _abstract(article: ElementTree.Element) -> Optional[str]:
    """Join the labelled sections of a structured abstract in document order."""
    chunks: List[str] = []
    for node in article.findall(".//Abstract/AbstractText"):
        body = _text(node)
        if not body:
            continue
        label = node.get("Label")
        chunks.append(f"{label}: {body}" if label else body)
    return clip(" ".join(chunks)) if chunks else None


def _authors(article: ElementTree.Element) -> List[str]:
    names = []
    for node in article.findall(".//AuthorList/Author"):
        last, initials = _text(node.find("LastName")), _text(node.find("Initials"))
        collective = _text(node.find("CollectiveName"))
        if last:
            names.append(f"{last} {initials}" if initials else last)
        elif collective:
            names.append(collective)
    return names


def _pub_date(article: ElementTree.Element) -> Optional[str]:
    node = article.find(".//Journal/JournalIssue/PubDate")
    if node is None:
        return None
    parts = [_text(node.find(tag)) for tag in ("Year", "Month", "Day")]
    joined = "-".join(part for part in parts if part)
    # MedlineDate carries the free-text ranges ("1998 Nov-Dec") that defeat
    # the structured fields.
    return joined or _text(node.find("MedlineDate"))


def _shape_article(article: ElementTree.Element) -> Dict[str, Any]:
    ids = {
        node.get("IdType"): _text(node)
        for node in article.findall(".//ArticleIdList/ArticleId")
    }
    authors = _authors(article)
    return {
        "pmid": ids.get("pubmed"),
        "doi": ids.get("doi"),
        "pmcid": ids.get("pmc"),
        "title": _text(article.find(".//ArticleTitle")),
        "journal": _text(article.find(".//Journal/Title")),
        "date": _pub_date(article),
        "authors": authors[:20],
        "author_count": len(authors),
        "abstract": _abstract(article),
    }


def fetch(pmids, **kwargs) -> Dict[str, Any]:
    """EFetch — full records, including abstracts, for up to ``MAX_RESULTS`` PMIDs."""
    if isinstance(pmids, str):
        pmids = [part.strip() for part in pmids.split(",") if part.strip()]
    pmids = [str(pmid).strip() for pmid in (pmids or []) if str(pmid).strip()]
    if not pmids:
        raise ScholarlyError(SOURCE, "at least one PMID is required")
    pmids = pmids[: bounded_count(len(pmids))]

    xml = _client(**kwargs).get_text(
        "/efetch.fcgi",
        {"db": "pubmed", "id": ",".join(pmids), "retmode": "xml"},
        accept="application/xml",
    )
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        raise ScholarlyError(SOURCE, f"PubMed returned unparseable XML: {exc}") from None

    articles = [
        _shape_article(article)
        for article in root.findall(".//PubmedArticle")
    ]
    return {"source": SOURCE, "requested": len(pmids), "returned": len(articles),
            "results": articles}


# ── identifier conversion ───────────────────────────────────────────


def convert_ids(ids, **kwargs) -> Dict[str, Any]:
    """Map between DOI, PMID and PMCID via the PMC ID Converter.

    Accepts any mix of the three schemes; the service infers each one's type.
    Records with no mapping come back marked rather than dropped, so a caller
    can tell "not in PMC" from "we forgot to ask".
    """
    if isinstance(ids, str):
        ids = [part.strip() for part in ids.split(",") if part.strip()]
    ids = [str(value).strip() for value in (ids or []) if str(value).strip()]
    if not ids:
        raise ScholarlyError(SOURCE, "at least one identifier is required")
    ids = ids[: bounded_count(len(ids))]

    payload = _client(IDCONV_URL, **kwargs).get_json(
        "/", {"ids": ",".join(ids), "format": "json"}
    )
    records = []
    for record in payload.get("records") or []:
        records.append(
            {
                "requested": record.get("requested-id"),
                "pmid": record.get("pmid"),
                "pmcid": record.get("pmcid"),
                "doi": record.get("doi"),
                "error": record.get("errmsg") or record.get("status"),
            }
        )
    return {"source": SOURCE, "requested": len(ids), "records": records}
