"""Shared FAL.ai SDK plumbing.

Holds the stateless atoms that every FAL-backed tool needs:

* :func:`import_fal_client` — lazy import + ``lazy_deps`` integration so
  ``fal_client`` isn't pulled at cold start (it added ~64 ms per CLI
  invocation when imported eagerly).
* :func:`_extract_http_status` — small helper used by
  ``_submit_fal_request``.

``_submit_fal_request`` intentionally stays on
:mod:`tools.image_generation_tool`, which is the patch target for
``tests/tools/test_image_generation.py`` and for the
``plugins/image_gen/fal/`` plugin's ``_it`` indirection.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Union
from urllib.parse import urlencode


def import_fal_client() -> Any:
    """Import ``fal_client`` (via ``lazy_deps`` when available) and return
    the module reference.

    Callers are responsible for caching the result on their own module
    global — keeping per-module globals lets tests monkey-patch the
    target module's ``fal_client`` attribute and have the patched value
    stick for that module's call sites.

    Raises :class:`ImportError` if the package is genuinely unavailable.
    """
    try:
        from tools.lazy_deps import ensure as _lazy_ensure
        _lazy_ensure("image.fal", prompt=False)
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001 — lazy_deps surfaces install hints
        raise ImportError(str(exc))
    import fal_client  # type: ignore  # noqa: WPS433 — intentionally lazy
    return fal_client


def _extract_http_status(exc: BaseException) -> Optional[int]:
    """Return an HTTP status code from httpx/fal exceptions, else None.

    Defensive across exception shapes — httpx.HTTPStatusError exposes
    ``.response.status_code`` while fal_client wrappers may expose
    ``.status_code`` directly.
    """
    response = getattr(exc, "response", None)
    if response is not None:
        status = getattr(response, "status_code", None)
        if isinstance(status, int):
            return status
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    return None
