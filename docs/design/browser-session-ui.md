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
- **Landing surface** — the browser now opens on that overview, and a project
  is carried by the route rather than held as sidebar state. See §9.

Known gaps:
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

## 9. Project-first — the route carries the project

Status: **web done**; the desktop flip is a deferred one-line default.

The browser opens on `/projects`, and picking one enters `/projects/:projectId`.
Everything downstream of that — the sidebar's session list, the file tree,
terminals, the artifact index — reads one atom, `$projectScope`, and the **route
is its only writer** (`syncProjectScopeFromRoute`, called from the wiring
controller). That is what makes back/forward, a reload, and a pasted link all
agree; the atom was previously persisted to localStorage under a single
unnamespaced key, which was a second source of truth that outlived the profile
switches it should not have.

Routes:

```
/                                     new chat (the desktop home)
/projects                             the picker
/projects/:projectId                  a project's new-chat draft
/projects/:projectId/sessions/:id     a session inside a project
/:sessionId                           still valid; re-homed into the project
                                      form once the tree can say which project
                                      owns its cwd (adoptBareSessionRoute)
```

Four decisions worth keeping:

- **Projects are created, never discovered.** `projects.tree` still returns three
  tiers — explicit rows, git repos auto-promoted from session cwds, and a bucket
  for cwd-less sessions — but the last two are dropped at ingestion
  (`userCreatedOnly` in `store/projects.ts`). One gate, so the sidebar, the
  picker, scope resolution and path matching cannot disagree about what exists.
  The home-dir git crawl is no longer called from the sidebar for the same
  reason: it could only produce rows the app now drops.
- **Route ids are projects.db row ids.** With discovery gone every project has
  one, so there is no path-shaped or otherwise unstable ref to encode around, and
  no slug indirection. The literal `sessions` segment keeps the two levels
  unambiguous — a partial or unknown tail parses as *nothing*, rather than
  silently degrading to the project home.
- **`sessionRoute()` defaults to the current project.** The command palette,
  keybinds, notification clicks and tab-close all build session routes; rather
  than teach eighteen call sites about scope, the wiring mirrors the routed id
  into `routes.ts` and `sessionRoute` reads it. Pass an explicit `null` for the
  bare form.
- **A session's own cwd still wins for the file tree.** Rooting the tree at the
  project would hide a linked worktree, which legitimately lives outside the
  repo root. The project root is the *fallback*, which is exactly the case of a
  fresh draft inside a project — previously an empty pane.

Inside a project the session list is bucketed by **recency** (Today / Yesterday /
This week / This month / Earlier), not by repo → branch → worktree. Being in the
project already answers "which checkout"; "what was I just doing" is the question
a session list is for. The lane tree still exists and still feeds the worktree
actions and the files pane — only the row grouping changed. One trap worth
naming: session rows carry epoch **seconds** while `Date.now()` is milliseconds,
and comparing them directly silently files every session under "Earlier".

The picker's chrome is deliberately sparse — projects, recents, New project, and
one Settings entry. Its job is *choosing*; the per-project surfaces belong inside
a project and the profile-wide ones behind Settings. Resist growing a row of
buttons back onto it.

### The landing replaces the shell

The picker is **not a page inside the app** — while it is showing, it *is* the
app. It was first built as a route in the workspace pane's table, which wrapped
it in the project-scoped shell: a sessions sidebar listing every session in the
profile, sitting next to a project card claiming six. Both numbers were correct,
which is the tell. A surface whose job is to establish scope cannot render inside
a frame that already assumes it.

So `ContribController` branches on `$landingOpen`: either the landing, or the
titlebar band + `LayoutTreeRoot` + statusbar. Three things make that safe:

- **The branch sits inside `ContribWiring`.** The socket, session stores,
  streaming, the overlay set, and `TitlebarControls` (whose traffic lights may
  never unmount) all live in the wiring and stay mounted across it. Leaving a
  project is a re-home, not a reboot — see `apps/desktop/AGENTS.md`.
- **The pane furniture is cheap to drop.** `$layoutTree` is a module atom
  hydrated from localStorage, so zone arrangement, presets and widths restore
  intact on the way back in. What genuinely dies is per-tile React state; if
  unsent composer text ever needs to survive, lift it into a store rather than
  keeping the shell mounted.
- **`syncLandingOpen` tracks the last *non-overlay* path.** Overlays render over
  whatever is beneath and must not change what that is. Without this, opening
  Settings from the landing paints the whole chat shell behind the settings card
  and closing it strands the user in a project they never picked.

Two corollaries: `/projects` is absent from the workspace route table and from
`BUILTIN_PAGES` in `route-tile.tsx` (a project picker docked beside the project
it was meant to pick is nonsense), and the sidebar's Projects row is an
**exit** — first in the list, arrow icon, titled with the project's own name,
and the one row that can never show an active state, because firing it unmounts
the sidebar.

### The sidebar lists sessions, and only sessions

The drill-in project overview is gone: no `projectOverview`, no
`ProjectOverviewRow`, no `onEnterProject`. Projects and sessions were two row
kinds with two destinations in one column, so a click there was ambiguous by
construction. Choosing a project belongs to the landing, and the landing
replaces this shell, so the two lists can never compete for the same click.

What remains inside a project is one list — that project's sessions, bucketed by
recency — and the section header just reads "Sessions"; the project's name lives
in the exit row above it rather than being said twice. The lane model
(`projectModel`) stays, because resolving `$projectScope` to its repos and
worktrees still needs it; only the *listing* of projects went away. On the bare
`/` chat route, where no project is in scope, the section falls back to the flat
session list it always had.

### Several sessions at once

Multi-session tabs are not new work — `openSessionTile(storedId, 'center')`
docks a session into the main zone's group, which renders it as a tab in that
zone's strip. Three doors exist already: ⌘/⌃-click a sidebar row, middle-click
one (browser muscle memory), or "Open in new tab" in the row's context menu.
⇧⌘-click pops a session into its own window, and dragging a row into a zone
splits instead of stacking. Tabs carry the full session verb set through
`SessionTabMenu` (close others/right/all) and persist across restarts, re-resuming
on boot.

Not done here: per-project settings, skills, or model defaults. Config is
strictly per-profile (`$OPENCODON_HOME/config.yaml`) and the only per-scope
mechanism that exists is the per-session model override on `session.create`, so
the picker exposes the global surfaces rather than pretending to scope them.
Project scoping is also presentation-only at the boundary: `/api/fs/*` and
`/api/git/*` take any absolute host path with no root confinement (`_fs_path`,
`web_server.py`), so nothing here is a security boundary.

## 10. Identity is a row; execution is a directory

The reference product models a project as a **container row, not a directory** —
frames, artifacts and folders hang off it by foreign key with `ON DELETE
cascade`, and each conversation gets an ephemeral workspace keyed by frame id.
That fits an analysis tool, where the scratch dir is disposable and the durable
output is an artifact. It does not fit a coding agent: worktrees *are*
directories, the review pane needs a checkout to diff, and the agent reads
`AGENTS.md` / `CLAUDE.md` from the working directory.

So we take the container's *identity* and keep the directory's *execution*:

- **`sessions.project_id`** records membership at creation
  (`opencodon_state.py`; written from `_ensure_session_db_row` via
  `session.create`'s `project_id` param). `cwd` stays, but it now means only
  "where this session runs".
- **`_project_for_session`** (`tui_gateway/project_tree.py`) prefers the recorded
  id and falls back to the cwd-prefix derivation. The client mirrors this in
  `liveSessionProjectId`.

The derivation it replaces was wrong in five ways that the column fixes outright:
a folder rename or move silently emptied a project; two nested projects were
ambiguous; a linked worktree outside the repo root needed special casing; a
session with no cwd belonged nowhere; and two projects could not share a folder.

Three rules the implementation depends on:

- **A fork inherits, it does not re-derive.** Compression forks, delegates and
  branch continuations copy the parent's `project_id` in the same `ON CONFLICT`
  block that copies its cwd — a child often has no cwd of its own yet, and
  deriving would drop the lineage out of the project on every fork.
- **Moving the cwd does not move the project.** `session.cwd.set` deliberately
  leaves membership alone; an agent `cd`-ing into a sibling checkout must not
  emigrate the conversation. The one exception is a session that never had a
  project — adopting it is new information, not a reassignment.
- **A stale id falls through.** A recorded id naming a project that no longer
  exists (or is archived) is ignored in favour of the derivation, so deleting a
  project can't strand its sessions in a phantom row.

Legacy rows are adopted once per database by
`_backfill_session_projects_once`, guarded by a `state_meta` flag rather than by
"are there NULLs left" — a session legitimately outside every project is NULL
forever, so a count-based guard would re-scan on every tree build.

One migration trap, learned the hard way: an index over a reconciler-added
column must go in `DEFERRED_INDEX_SQL`, not `SCHEMA_SQL`. `_init_schema` runs
`executescript(SCHEMA_SQL)` *before* `_reconcile_columns`, so on an existing
database the `CREATE INDEX` fires against a column that doesn't exist yet and
the whole open fails.

### Agent Context

`projects.context` is free text the user writes once and every agent in that
project reads — conventions, domain background, house rules. It is deliberately
**not** the same field as `description`, which is the blurb in the project list
and never reaches a prompt; the two look alike in the create dialog, so each
carries a hint saying which is which.

`_load_project_context` (`agent/prompt_builder.py`) resolves it by id from
`agent.project_id`, which the gateway sets from the session row, and falls back
to the cwd derivation for the CLI and TUI — surfaces that have a directory but
no project scope. Two details that matter:

- It is **additive**, not part of the first-match-wins chain that picks exactly
  one of `.opencodon.md` / `AGENTS.md` / `CLAUDE.md` / `.cursorrules`. A repo's
  AGENTS.md describes the code; the project context describes how the user wants
  agents to work in it. Both belong, and the project's leads.
- The cwd handed to the fallback is **unresolved**. `project_folders` stores
  `os.path.abspath` values, so a `.resolve()`d path misses every project reached
  through a symlink — on macOS, anything under `/tmp`.

### Two things we are NOT taking from the reference

- **Project-scoped artifact storage.** Artifacts stay in the user's own folder,
  where the work is. A `proj_<id>/…` blob store with virtual folders is right
  when a project has no directory; ours does, and putting outputs anywhere but
  the checkout would hide them from git, the file tree, and every other tool the
  user already has.
- **The per-frame ephemeral workspace.** The piece most specific to disposable
  analysis, and the one that would hurt most in a tool whose output is a branch
  you intend to merge.
