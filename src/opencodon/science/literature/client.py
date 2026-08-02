"""Scholarly-flavoured aliases for the shared API transport.

The transport moved to ``science.apiclient`` when the bio-data sources started
using it too; these names are kept because the literature modules and their
tests read better with them.
"""

from opencodon.science.apiclient import (  # noqa: F401
    ABSTRACT_CHARS,
    DEFAULT_RESULTS,
    DEFAULT_TIMEOUT_S,
    MAX_ATTEMPTS,
    MAX_BACKOFF_S,
    MAX_RESULTS,
    RETRYABLE_STATUS,
    ApiClient as ScholarlyClient,
    ApiError as ScholarlyError,
    bounded_count,
    clip,
    contact_email,
    user_agent,
)

__all__ = [
    "ScholarlyClient", "ScholarlyError", "bounded_count", "clip",
    "contact_email", "user_agent", "MAX_RESULTS", "DEFAULT_RESULTS",
    "ABSTRACT_CHARS", "RETRYABLE_STATUS",
]
