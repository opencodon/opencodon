"""Shared transport for the public science APIs.

Every source in science/literature and science/biodata talks to a public,
unauthenticated API run by a non-profit, a university or a public agency. That shapes the design more
than any of them individually:

- **Politeness is the price of throughput.** OpenAlex and Crossref both run a
  faster "polite pool" for clients that identify themselves with a contact
  address; NCBI asks the same in its usage policy. Sending a real User-Agent
  and mailto is not decoration, it is how the rate limit gets raised.
- **These services rate-limit rather than fail.** A 429 is a normal, expected
  response under load, not an error worth surfacing to the model — so it is
  retried with backoff, honouring ``Retry-After`` when the server sets it.
- **Everything here is unbounded upstream.** A broad query can match millions
  of works and an abstract can run to several thousand words. Left alone that
  becomes context the caller pays for on every subsequent turn, so result
  counts and text lengths are capped here rather than trusted to callers.
"""

from __future__ import annotations

import logging
import os
import random
import time
from typing import Any, Callable, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 20.0
MAX_ATTEMPTS = 3
MAX_BACKOFF_S = 8.0

# 429 is rate limiting; 5xx are upstream wobbles. Both clear on their own.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

# Context budget. A literature call is a lookup, not a bulk export — a caller
# who needs more should paginate deliberately rather than get it by accident.
MAX_RESULTS = 25
DEFAULT_RESULTS = 10
ABSTRACT_CHARS = 1500

PROJECT_URL = "https://github.com/opencodon/opencodon"
CONTACT_ENV = "OPENCODON_SCHOLARLY_MAILTO"


class ApiError(RuntimeError):
    """A failure worth showing the caller, named by source.

    Carries the upstream status so a caller can tell "no such DOI" (404) from
    "the service is down" (503) without parsing prose.
    """

    def __init__(
        self,
        source: str,
        message: str,
        *,
        status: Optional[int] = None,
        retryable: bool = False,
    ):
        super().__init__(message)
        self.source = source
        self.message = message
        self.status = status
        self.retryable = retryable

    def as_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"error": self.message, "source": self.source}
        if self.status is not None:
            payload["status"] = self.status
        return payload


def contact_email() -> Optional[str]:
    """Contact address advertised to scholarly APIs, if the user set one."""
    value = (os.environ.get(CONTACT_ENV) or "").strip()
    return value or None


def user_agent() -> str:
    """Identify this client, with a contact address when one is configured.

    The mailto is appended in the form OpenAlex and Crossref document, so the
    same string satisfies both without per-source special-casing.
    """
    try:
        from importlib.metadata import version

        release = version("opencodon")
    except Exception:
        release = "dev"
    email = contact_email()
    suffix = f"; mailto:{email}" if email else ""
    return f"opencodon/{release} (+{PROJECT_URL}{suffix})"


def clip(text: Optional[str], limit: int = ABSTRACT_CHARS) -> Optional[str]:
    """Bound a free-text field, marking the cut so it never reads as complete."""
    if not text:
        return None
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"… [{len(text) - limit} more chars]"


def bounded_count(requested: Optional[int], default: int = DEFAULT_RESULTS) -> int:
    """Clamp a caller-supplied result count into the context budget."""
    if not requested or requested < 1:
        return default
    return min(int(requested), MAX_RESULTS)


def _retry_after_seconds(response: httpx.Response) -> Optional[float]:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        # Only the delta-seconds form is honoured; the HTTP-date form is rare
        # here and not worth the parsing risk — backoff covers that case.
        return max(0.0, float(raw.strip()))
    except ValueError:
        return None


class ApiClient:
    """A polite, bounded, retrying JSON client for one public API."""

    def __init__(
        self,
        source: str,
        base_url: str,
        *,
        default_params: Optional[Dict[str, Any]] = None,
        timeout: float = DEFAULT_TIMEOUT_S,
        max_attempts: int = MAX_ATTEMPTS,
        sleep: Callable[[float], None] = time.sleep,
        transport: Optional[httpx.BaseTransport] = None,
    ):
        self.source = source
        self.base_url = base_url.rstrip("/")
        self.default_params = dict(default_params or {})
        self.timeout = timeout
        self.max_attempts = max_attempts
        self._sleep = sleep
        self._transport = transport

    # ── request ─────────────────────────────────────────────────────

    def get_json(
        self, path: str, params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """GET *path* and decode JSON.

        Raises :class:`ApiError` when the body is not JSON, so a caller
        never has to distinguish a decode failure from a transport one.
        """
        response = self.get(path, params, accept="application/json")
        try:
            return response.json()
        except ValueError:
            raise ApiError(
                self.source,
                f"{self.source} returned a non-JSON response for {path}",
                status=response.status_code,
            ) from None

    def get_text(
        self, path: str, params: Optional[Dict[str, Any]] = None, *, accept: str = "*/*"
    ) -> str:
        """GET *path* as text — for the XML-only endpoints (NCBI EFetch)."""
        return self.get(path, params, accept=accept).text

    def post_json(
        self, path: str, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """POST JSON and decode JSON — for the GraphQL endpoints (gnomAD).

        Shares the retry and politeness policy with :meth:`get`; only the verb
        and the body differ.
        """
        response = self._request("POST", path, json_body=payload)
        try:
            return response.json()
        except ValueError:
            raise ApiError(
                self.source,
                f"{self.source} returned a non-JSON response for {path}",
                status=response.status_code,
            ) from None

    def get(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        accept: str = "application/json",
    ) -> httpx.Response:
        """GET *path*, retrying the transient failures.

        Raises :class:`ApiError` on a non-retryable status and on
        exhausted retries — never a bare httpx exception, so every caller has
        exactly one failure type to handle.
        """
        return self._request("GET", path, params=params, accept=accept)

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        accept: str = "application/json",
        json_body: Optional[Dict[str, Any]] = None,
    ) -> httpx.Response:
        url = f"{self.base_url}/{path.lstrip('/')}"
        merged = {**self.default_params, **(params or {})}
        merged = {k: v for k, v in merged.items() if v is not None}
        headers = {"User-Agent": user_agent(), "Accept": accept}

        last_error: Optional[str] = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                with httpx.Client(
                    timeout=self.timeout, transport=self._transport, follow_redirects=True
                ) as client:
                    response = client.request(
                        method, url, params=merged, headers=headers, json=json_body
                    )
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt == self.max_attempts:
                    break
                self._backoff(attempt, None)
                continue

            if response.status_code in RETRYABLE_STATUS:
                last_error = f"HTTP {response.status_code}"
                if attempt == self.max_attempts:
                    raise ApiError(
                        self.source,
                        f"{self.source} is unavailable after {self.max_attempts} "
                        f"attempts (last: HTTP {response.status_code})",
                        status=response.status_code,
                        retryable=True,
                    )
                self._backoff(attempt, _retry_after_seconds(response))
                continue

            if response.status_code >= 400:
                # Not worth retrying: a bad request or a missing record will
                # be just as bad the second time.
                raise ApiError(
                    self.source,
                    f"{self.source} returned HTTP {response.status_code} for {path}",
                    status=response.status_code,
                )

            return response

        raise ApiError(
            self.source,
            f"could not reach {self.source} after {self.max_attempts} attempts "
            f"(last: {last_error})",
            retryable=True,
        )

    def _backoff(self, attempt: int, retry_after: Optional[float]) -> None:
        """Wait before a retry — the server's instruction wins if it gave one."""
        if retry_after is not None:
            delay = min(retry_after, MAX_BACKOFF_S)
        else:
            # Exponential with jitter, so concurrent callers do not resynchronise
            # into a thundering herd against a service that is already strained.
            delay = min(2.0 ** (attempt - 1), MAX_BACKOFF_S)
            delay *= 0.5 + random.random() / 2
        logger.debug("%s: retrying in %.2fs (attempt %d)", self.source, delay, attempt)
        self._sleep(delay)
