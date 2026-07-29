"""Serve the full session UI to a browser at ``/app``.

The dashboard's own SPA (``web/``, mounted at ``/``) answers *how is my agent
configured?*  This mount answers *let me actually work* — it serves the
renderer from ``apps/desktop``, the same React app the desktop shell loads,
built for the browser.

That app drives sessions over the JSON-RPC gateway at ``/api/ws``, which runs
**inside this process** (``tui_gateway.ws.handle_ws``).  No PTY, no Node, no
Ink TUI: the browser talks to the same ``tui_gateway.server.dispatch`` seam the
desktop app uses, so a session can be created, streamed, and resumed with
nothing running but this server.

The browser gets its host capabilities from ``src/lib/web-bridge.ts``, which
implements ``window.opencodonDesktop`` against this server's REST API instead
of Electron IPC.

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

WEB_APP_DIST = (
    Path(os.environ["OPENCODON_WEB_APP_DIST"])
    if "OPENCODON_WEB_APP_DIST" in os.environ
    else Path(__file__).parent / "web_app_dist"
)

#: Path prefix the bundle is built against (``base: '/app/'`` in
#: ``vite.config.web.ts``).  Changing one without the other breaks asset URLs.
MOUNT_PATH = "/app"

_BUILD_HINT = "Session UI not built. Run: cd apps/desktop && npm run build:web"


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
    index_path = root / "index.html"

    # `opencodon serve` is the headless backend: API and WebSockets only. It
    # must never serve a browser UI, matching mount_spa's contract.
    if os.environ.get("OPENCODON_SERVE_HEADLESS") == "1" or not index_path.is_file():
        return False

    def _serve_index(prefix: str = "") -> Response:
        try:
            html = index_path.read_text(encoding="utf-8")
        except OSError:
            return JSONResponse({"error": _BUILD_HINT}, status_code=404)

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

    assets = root / "assets"
    if assets.is_dir():
        application.mount(
            f"{MOUNT_PATH}/assets",
            StaticFiles(directory=assets),
            name="web-app-assets",
        )

    @application.get(MOUNT_PATH)
    async def web_app_root(request: Request) -> Response:
        return _serve_index(normalise_prefix(request.headers.get("x-forwarded-prefix")))

    @application.get(MOUNT_PATH + "/{full_path:path}")
    async def web_app_spa(full_path: str, request: Request) -> Response:
        # The renderer routes on the hash, so every non-asset path under /app
        # is the same document. Static files that exist are served as-is so
        # fonts and icons referenced by absolute path still resolve.
        candidate = (root / full_path).resolve()
        if (
            full_path
            and candidate.is_file()
            and candidate.is_relative_to(root.resolve())
        ):
            return Response(
                candidate.read_bytes(),
                media_type=_media_type(candidate),
            )

        return _serve_index(normalise_prefix(request.headers.get("x-forwarded-prefix")))

    return True


def _media_type(path: Path) -> str:
    import mimetypes

    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"
