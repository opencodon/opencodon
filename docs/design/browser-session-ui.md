# Browser session UI — the web server as a first-class session host

Status: **working end to end**; remaining gaps in §7
Date: 2026-07-29

## 1. The problem

The dashboard's chat tab was an xterm.js terminal wired to `/api/pty`, which
spawned the Ink TUI, which dialed a WebSocket back into the *same* server that
spawned it. Consequences:

- Everything the browser showed was VT100 bytes. No addressable spans, so no
  inline artifacts, no per-message actions, no annotations —
  [`web-ui-redesign.md`](./web-ui-redesign.md) §7 accepted this as permanent.
- No projects, no session tabs, no file or artifact panes.
- The browser UI could not work unless Node and the TUI were installed and
  working, so "open the dashboard" was never a complete story on its own.

## 2. The seam that already existed

`tui_gateway` is misnamed. It is not TUI-specific: it is a JSON-RPC 2.0 agent
session host whose transport is an injected `Transport` protocol
(`tui_gateway/transport.py:67`), with stdio and WebSocket as equal peers. Its
dispatcher is **already mounted in this process** at `/api/ws`
(`web_server.py:17027` → `tui_gateway/ws.py:handle_ws`).

So the server could always host sessions directly. What was missing was a
browser client for that protocol — and one already existed too:
the renderer now in `apps/client` — a 134k-line React app that drives exactly
these RPCs and events, and whose only host coupling is the
`window.opencodonDesktop` bridge.

The change is therefore not a new UI. It is a transport swap.

```
browser (apps/client, built for the web by apps/web)
  └─ window.opencodonDesktop  ← apps/web/src/web-bridge.ts (HTTP/WS, not IPC)
       └─ WS /api/ws
            └─ tui_gateway.ws.handle_ws → WSTransport
                 └─ tui_gateway.server.dispatch          ← the seam
                      └─ _make_agent → AIAgent.run_conversation
```

No PTY, no Node, no TUI anywhere in that path.

## 3. The bridge

`apps/web/src/web-bridge.ts` installs `window.opencodonDesktop` when
one isn't already present, so the real Electron preload always wins. Members
fall into three kinds, and new ones must pick deliberately:

1. **Backed** — a real endpoint does the work (files, git, gateway, terminal).
2. **Emulated** — the browser has its own primitive (clipboard, external
   links, notifications, downloads, extra tabs).
3. **Absent** — genuinely Electron-only (OS trash, Finder reveal, in-place
   updates). These return inert values or reject. They must never report
   success, because a silent success reads to the UI as "the file was
   trashed."

### `mode: 'remote'` is the load-bearing decision

The bridge reports the connection as remote. That is the honest answer to the
question the renderer is actually asking — *whose filesystem is this?* — and in
a browser it is the server's, not the renderer's.

Declaring it routes every file, git, media, and project-tree call through the
existing `lib/desktop-fs.ts` and `lib/desktop-git.ts` facades, which already
mirror the whole surface onto `/api/fs/*` and `/api/git/*` for remote gateways.
The bridge reimplements none of it. It also correctly disables in-place app
updates, which a browser cannot perform.

`git` is deliberately **absent** from the bridge: `desktopGit()` substitutes
its REST implementation whenever the connection is remote, so a bridge-side
copy would be dead code free to drift from the one actually called.

### Auth

Two schemes, mirroring the server. On a loopback bind the page carries an
injected bearer token. Under the OAuth gate that token is withheld entirely and
the browser authenticates by cookie, with the gateway socket using single-use
tickets from `/api/auth/ws-ticket` — minted immediately before each dial, since
a ticket held across a reconnect is spent.

## 4. Packages

The UI is its own package, shared by two hosts:

```
apps/client   the UI. Host-agnostic: renders against whatever bridge is
              already installed, and never installs one itself.
apps/desktop  the Electron shell — main process, preload (bridge over IPC),
              packaging.
apps/web      the browser host — bridge over HTTP/WS, plus the build the
              dashboard serves.
```

Each host owns only its entry point and the config that genuinely differs
(base path, output dir, dev server); the rest comes from
`apps/client/vite.base.ts` so the two cannot drift into building the same
source differently. Two traps this arrangement sets:

- Tailwind v4 detects content from the *build root*, which is now the host
  package. `apps/client/src/styles.css` carries an explicit `@source` — without
  it the scan finds no components and emits a stylesheet with no utility
  classes, which typechecks, builds, passes tests, and renders an unstyled
  page.
- The desktop build sets `codeSplitting: false` so electron-builder doesn't OOM
  scanning thousands of files. The web build must *not* inherit that; see §7.

## 5. Serving

`opencodon_cli/web_app.py` mounts the bundle at `/app`, before the config SPA's
catch-all. It injects the same bootstrap globals `mount_spa` does, and honours
`OPENCODON_SERVE_HEADLESS` so `opencodon serve` stays API-only.

The two UIs coexist: `/` is the config dashboard, `/app` is the session UI.

```bash
npm run build --workspace @opencodon/web   # → opencodon_cli/web_app_dist/
opencodon dashboard --no-open              # → http://127.0.0.1:9119/app
```

For development, `npm run dev --workspace @opencodon/web` serves it with HMR and proxies
`/api` (WebSockets included) at a running dashboard. Export
`OPENCODON_DASHBOARD_SESSION_TOKEN` before starting both so they agree on a
credential — the dev server injects it the way the FastAPI mount would.

## 6. `/api/shell`

`/api/pty` only ever spawns the TUI. The renderer's terminal pane wants a
login shell, so `/api/shell` provides one. It lives beside `pty_ws` rather than
in its own module because it shares the entire auth/host/peer preamble;
splitting it out would mean exporting six private gates and letting the two
drift. It is 1:1 with the socket — reattach semantics belong to agent sessions,
not scratch terminals.

## 7. What works, and what doesn't yet

Verified in a browser against `opencodon dashboard`: gateway connects, a new
session is created and streams a reply, sessions group by project, session
tabs open side by side, the file tree browses the real workspace, the artifact
index lists and filters, and the terminal spawns a shell and echoes writes.

Since resolved:

- **Two artifact surfaces** — the session UI now carries a Provenance surface
  (`apps/client/src/app/science/`) over the same `/api/science` endpoints:
  runs, cell traces, artifact version timelines, typed previews, lineage, and
  RO-Crate export.
- **Bundle size** — the web build splits (§4), so first paint is a 3.9 MB entry
  chunk rather than 27.8 MB. The desktop build keeps its single chunk, which is
  a packaging constraint, not a preference.
- **Compute pane** — live kernels with host CPU/memory, behind a new
  `GET /api/science/kernels`. Reports, never controls. Scoped to this process's
  kernels and says so, because a CLI or cron run holds its own in another
  process and an authoritative-looking pane that omits them is worse than none.
- **Files pane scoping** — the pane header switches between the workspace tree
  and the session's staged artifacts. The two planes stay distinct rather than
  merged: "this file exists" and "this file is a result" are different claims.
- **Projects overview** — `/projects` lists every project with its session
  count and last activity beside the recent sessions across all of them.

Known gaps:

- **Landing surface.** The overview exists but the app still opens on chat, per
  `apps/desktop/DESIGN.md` ("chat is the home surface"). This client is shared
  with the Electron shell, so where the app opens is a product decision rather
  than a UI detail; flipping it is a one-line default once someone decides.
- **The config SPA at `/`.** Smaller than it first looked: `/app` already
  carries Settings (config, keys, providers, models, env, gateway, keybinds,
  appearance, notifications, plugins, toolsets, memory, terminal backends),
  plus Capabilities, Messaging, Cron, Agents, and Profiles. What remains only
  at `/` is a handful of ops pages — analytics, webhooks, pairing, docs,
  version-resolve, system stats — none of which the reference platform has
  either. So retiring `/` is a scoping decision, not a porting backlog.
- **Notebook.** The reference streams live kernel cells. We show kernel state
  but never submit code, per `web-ui-redesign.md` §4; that decision stands.
- **Absent bridge members** (§3.3) are stubs. Anything routing a user through
  "reveal in Finder" needs a browser-appropriate alternative, not a silent
  no-op.

## 8. Consequence for the old plan

[`web-ui-redesign.md`](./web-ui-redesign.md) §7 accepted that the Console tab
could never carry structure because it was a PTY. That constraint is gone —
the browser now renders structured messages directly. Its §4 decision that
*the web UI does not execute code* still stands and is unaffected: this change
adds no browser-originated code execution beyond the shell pane, which is an
authenticated terminal on the same host, not a kernel submit path.
