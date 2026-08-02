"""Literature and evidence layer — the scholarly APIs, shaped for an agent.

Phase 2 of the science-capability plan. Each module wraps one public API
behind a bounded, polite, retrying client:

- ``client``    — shared transport (politeness, retry/backoff, context caps)
- ``openalex``  — works, search, and the citation graph in both directions
- ``crossref``  — authoritative DOI metadata
- ``pubmed``    — PubMed search/fetch and DOI ↔ PMID ↔ PMCID conversion

Everything returns plain dicts and raises exactly one error type
(:class:`~science.literature.client.ScholarlyError`), so the tool layer above
has a single failure path to translate.
"""

from opencodon.science.literature.client import ScholarlyError

__all__ = ["ScholarlyError"]
