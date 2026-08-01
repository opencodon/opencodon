"""Serve the session UI to a browser, at ``/app`` and at the site root.

The renderer is built from ``apps/web`` — the same React app the Electron
shell loads (``@opencodon/client``), built for the browser.  It drives
sessions over the JSON-RPC gateway at ``/api/ws``, which runs **inside this
process** (``tui_gateway.ws.handle_ws``).  No PTY, no Node, no Ink TUI: the
browser talks to the same ``tui_gateway.server.dispatch`` seam the desktop app
uses, so a session can be created, streamed, and resumed with nothing running
but this server.

The dashboard used to ship a second, separate SPA under ``web/`` mounted at
``/``.  That app is gone; this one answers both prefixes from a single bundle.
``/app`` stays because it is the documented, linked-to entry point, and it is
what the built asset URLs are keyed to (``base: '/app/'``).

The browser gets its host capabilities from ``apps/web/src/web-bridge.ts``,
which implements ``window.opencodonDesktop`` against this server's REST API
instead of Electron IPC.

Kept out of ``web_server.py`` deliberately: that module is ~18.8k lines and
the redesign doc's standing rule is that every new route goes in its own
module.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

#: Where the built bundle lives.  ``OPENCODON_WEB_DIST`` is the canonical
#: override (Docker, nix, desktop); ``OPENCODON_WEB_APP_DIST`` is kept as a
#: narrower escape hatch for serving this mount from a different build.
WEB_APP_DIST = Path(
    os.environ.get("OPENCODON_WEB_APP_DIST")
    or os.environ.get("OPENCODON_WEB_DIST")
    or Path(__file__).parent / "web_dist"
)

#: Path prefix the bundle is built against (``base: '/app/'`` in
#: ``apps/web/vite.config.ts``).  Changing one without the other breaks asset
#: URLs — including the ones served at the root mount, which reference
#: ``/app/assets/...`` too.
MOUNT_PATH = "/app"

#: Shown when the bundle is missing. ``web_server.mount_spa`` reuses it so
#: the root and ``/app`` mounts never disagree about how to fix it.
BUILD_HINT = "Session UI not built. Run: npm run build --workspace @opencodon/web"

#: Absolute asset directories the built CSS references with ``url(...)``.
#: Behind a path-prefix proxy these have to be rewritten (see
#: :func:`_mount_prefixed_css`).
_CSS_ASSET_DIRS = ("/fonts/", "/ds-assets/", f"{MOUNT_PATH}/assets/")


def _index_server(
    root: Path,
    *,
    session_token: Callable[[], str],
    auth_required: Callable[[], bool],
) -> Callable[..., Response]:
    """Return a function rendering ``index.html`` for a given proxy prefix."""
    index_path = root / "index.html"

    def _serve_index(prefix: str = "") -> Response:
        try:
            html = index_path.read_text(encoding="utf-8")
        except OSError:
            # The dist dir existed at mount time but index.html is missing or
            # unreadable now (partial build, wiped dist, permissions). Without
            # this guard every request raises FileNotFoundError (500).
            return JSONResponse({"error": BUILD_HINT}, status_code=404)

        gated = auth_required()

        # Under the OAuth gate the long-lived session token is deliberately
        # withheld: the browser authenticates by cookie, and the gateway
        # socket uses single-use tickets minted from /api/auth/ws-ticket.
        # Injecting a bearer token here would hand out a credential the gate
        # is designed to refuse.
        parts = [
            f'window.__OPENCODON_BASE_PATH__="{prefix}";',
            f"window.__OPENCODON_AUTH_REQUIRED__={'true' if gated else 'false'};",
        ]
        if not gated:
            parts.insert(0, f'window.__OPENCODON_SESSION_TOKEN__="{session_token()}";')

        html = html.replace("</head>", f"<script>{''.join(parts)}</script></head>", 1)

        if prefix:
            # Behind a path-prefix proxy the bundle's absolute /app/... asset
            # URLs would resolve against the proxy root. Rewrite them.
            html = html.replace(f'"{MOUNT_PATH}/', f'"{prefix}{MOUNT_PATH}/')

        return HTMLResponse(
            html,
            # The token and auth mode are baked into this document, so it must
            # never be cached. Hashed assets under /app/assets still are.
            headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
        )

    return _serve_index


def _mount_prefixed_css(
    application: FastAPI,
    root: Path,
    normalise_prefix: Callable[[Optional[str]], str],
) -> None:
    """Serve built CSS with absolute asset URLs rewritten for a proxy prefix.

    The built CSS contains absolute ``url(/fonts/...)`` / ``url(/ds-assets/...)``
    references. Browsers resolve those against the document origin, so behind
    ``example.com/opencodon/*`` they'd hit the proxy's own root instead of this
    backend. This route sits in front of the ``StaticFiles`` mount and rewrites
    them when a prefix is in play; with no prefix it is a plain file read.
    """

    @application.get(f"{MOUNT_PATH}/assets/{{filename}}.css")
    async def serve_css(filename: str, request: Request) -> Response:
        css_path = root / "assets" / f"{filename}.css"
        if not css_path.is_file() or not css_path.resolve().is_relative_to(
            root.resolve()
        ):
            return JSONResponse({"error": "not found"}, status_code=404)
        prefix = normalise_prefix(request.headers.get("x-forwarded-prefix"))
        css = css_path.read_text(encoding="utf-8")
        if prefix:
            for asset_dir in _CSS_ASSET_DIRS:
                css = css.replace(f"url({asset_dir}", f"url({prefix}{asset_dir}")
                css = css.replace(f'url("{asset_dir}', f'url("{prefix}{asset_dir}')
                css = css.replace(f"url('{asset_dir}", f"url('{prefix}{asset_dir}")
        return Response(content=css, media_type="text/css")


def _serve_file_or_index(
    root: Path,
    full_path: str,
    serve_index: Callable[..., Response],
    prefix: str,
) -> Response:
    """Serve *full_path* from the dist when it is a real file, else the SPA.

    The renderer routes on the hash, so every non-asset path is the same
    document. Static files that exist are served as-is so fonts, icons, and
    anything else referenced by absolute path still resolve.
    """
    candidate = (root / full_path).resolve()
    if full_path and candidate.is_file() and candidate.is_relative_to(root.resolve()):
        return Response(candidate.read_bytes(), media_type=_media_type(candidate))
    return serve_index(prefix)


def mount_web_app(
    application: FastAPI,
    *,
    session_token: Callable[[], str],
    auth_required: Callable[[], bool],
    normalise_prefix: Callable[[Optional[str]], str],
    dist: Optional[Path] = None,
) -> bool:
    """Mount the browser session UI at ``/app``.

    Returns True when the bundle was found and mounted.

    The three callables are injected rather than imported so this module has
    no import-time dependency on ``web_server``'s private state — the token is
    regenerated per process and the auth mode is only settled after import,
    so both must be read per request, not captured at mount time.
    """
    root = dist or WEB_APP_DIST

    # `opencodon serve` is the headless backend: API and WebSockets only. It
    # must never serve a browser UI, matching mount_root_spa's contract.
    if os.environ.get("OPENCODON_SERVE_HEADLESS") == "1" or not (root / "index.html").is_file():
        return False

    serve_index = _index_server(
        root, session_token=session_token, auth_required=auth_required
    )

    assets = root / "assets"
    if assets.is_dir():
        _mount_prefixed_css(application, root, normalise_prefix)
        application.mount(
            f"{MOUNT_PATH}/assets",
            StaticFiles(directory=assets),
            name="web-app-assets",
        )

    @application.get(MOUNT_PATH)
    async def web_app_root(request: Request) -> Response:
        return serve_index(normalise_prefix(request.headers.get("x-forwarded-prefix")))

    @application.get(MOUNT_PATH + "/{full_path:path}")
    async def web_app_spa(full_path: str, request: Request) -> Response:
        prefix = normalise_prefix(request.headers.get("x-forwarded-prefix"))
        return _serve_file_or_index(root, full_path, serve_index, prefix)

    return True


def mount_root_spa(
    application: FastAPI,
    *,
    session_token: Callable[[], str],
    auth_required: Callable[[], bool],
    normalise_prefix: Callable[[Optional[str]], str],
    dist: Optional[Path] = None,
) -> None:
    """Serve the same bundle from the site root, as the final catch-all.

    ``mount_web_app`` has already registered the bundle (and its assets) under
    ``/app``; this adds ``/`` and every unmatched path, so a user who lands on
    the bare host gets the UI. Register it LAST — the catch-all would otherwise
    swallow every route declared after it.
    """
    root = dist or WEB_APP_DIST
    serve_index = _index_server(
        root, session_token=session_token, auth_required=auth_required
    )

    @application.get("/{full_path:path}")
    async def serve_spa(full_path: str, request: Request) -> Response:
        # An unmatched /api/* path is a missing/renamed endpoint, NOT a
        # client-side route. Falling through to index.html here returns
        # `<!doctype html>` with status 200, which makes JSON clients (the
        # desktop app's fetchJson, dashboard fetch wrappers) blow up with an
        # opaque `SyntaxError: Unexpected token '<'`. Return a real 404 JSON
        # so the caller sees a clear "no such endpoint" instead.
        if full_path == "api" or full_path.startswith("api/"):
            return JSONResponse(
                {"detail": f"No such API endpoint: /{full_path}"},
                status_code=404,
            )
        prefix = normalise_prefix(request.headers.get("x-forwarded-prefix"))
        return _serve_file_or_index(root, full_path, serve_index, prefix)


def _media_type(path: Path) -> str:
    import mimetypes

    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"
