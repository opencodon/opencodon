"""Crossref — authoritative bibliographic metadata for a DOI.

Crossref is where DOIs are registered, so it is the citation of record: when
OpenAlex and Crossref disagree about a title or a date, Crossref is the one
the publisher deposited. Used here for resolution rather than discovery.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

from opencodon.science.literature.client import (
    ScholarlyClient,
    ScholarlyError,
    clip,
    contact_email,
)

BASE_URL = "https://api.crossref.org"
SOURCE = "crossref"

# Deliberately permissive: the registry guarantees the `10.<registrant>/`
# prefix and essentially nothing about the suffix, so anything stricter would
# reject real DOIs. This exists to catch a title or a URL passed by mistake,
# not to validate registration.
_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")


def normalise_doi(doi: str) -> str:
    """Strip common DOI wrappers and reject what is clearly not a DOI.

    Rejecting locally matters: a bad DOI otherwise costs a network round trip
    to learn something the string itself already said.
    """
    value = (doi or "").strip()
    if not value:
        raise ScholarlyError(SOURCE, "a DOI is required")
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:", "DOI:"):
        if value.lower().startswith(prefix.lower()):
            value = value[len(prefix):]
            break
    value = value.strip()
    if not _DOI_RE.match(value):
        raise ScholarlyError(SOURCE, f"{value!r} is not a well-formed DOI")
    return value


def _date_parts(node: Optional[Dict[str, Any]]) -> Optional[str]:
    """Render Crossref's ``date-parts`` as an ISO-ish string.

    Precision varies by deposit — some records carry only a year — so the
    output is as precise as the record and no more.
    """
    parts = ((node or {}).get("date-parts") or [[]])[0]
    if not parts:
        return None
    return "-".join(f"{part:02d}" if index else str(part)
                    for index, part in enumerate(parts))


def _shape(message: Dict[str, Any]) -> Dict[str, Any]:
    authors = [
        " ".join(filter(None, [author.get("given"), author.get("family")])).strip()
        or author.get("name")
        for author in (message.get("author") or [])
    ]
    container = message.get("container-title") or []
    titles = message.get("title") or []
    abstract = message.get("abstract")
    if abstract:
        # Crossref deposits abstracts as JATS XML fragments.
        abstract = re.sub(r"<[^>]+>", " ", abstract)
    return {
        "doi": message.get("DOI"),
        "title": titles[0] if titles else None,
        "container": container[0] if container else None,
        "publisher": message.get("publisher"),
        "type": message.get("type"),
        "issued": _date_parts(message.get("issued")),
        "authors": [name for name in authors if name][:20],
        "author_count": len(authors),
        "referenced_by_count": message.get("is-referenced-by-count"),
        "url": message.get("URL"),
        "abstract": clip(abstract),
    }


def get_work(doi: str, **kwargs) -> Dict[str, Any]:
    """Bibliographic metadata for one DOI."""
    resolved = normalise_doi(doi)
    client = ScholarlyClient(
        SOURCE, BASE_URL, default_params={"mailto": contact_email()}, **kwargs
    )
    payload = client.get_json(f"/works/{resolved}")
    return {"source": SOURCE, **_shape(payload.get("message") or {})}
