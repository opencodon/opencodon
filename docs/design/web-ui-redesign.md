# Web UI redesign — result-first dashboard

Status: **plan agreed, Phase 1 in progress**
Date: 2026-07-28

## 1. Why

The dashboard is inherited from hermes, and it answers hermes' question: *how is
my assistant configured?* Fifteen of its seventeen nav items are agent config,
and the root route redirects to a list of conversations.

opencodon has to answer a different question: **what did we find, how was it
produced, and will it hold up when someone checks?**

Today the science layer has no web surface at all. Of the 221 routes in
`opencodon_cli/web_server.py`, zero read `execution_log`, `host_call_log`,
`artifacts`, `artifact_versions`, or `artifact_dependencies`. Frames, lineage,
and reproduction exist only as agent tools (`tools/science_tools.py`). Closing
that gap is the redesign.

## 2. Principles

1. **Result-first.** The landing surface is work and its outputs, not a
   conversation list.
2. **Every scientific object has a URL.** Frame, cell, artifact version,
   environment. Addressable means citable and shareable.
3. **Provenance is a view, not a feature.** No artifact is shown without a path
   back to the cell, the inputs, and the environment that made it.
4. **Claims stay capped.** `reproduced` is the strongest word the UI uses, never
   "verified" — matching `science/reproduce.py`.
5. **Two planes, honestly drawn.** Workspace files are ephemeral and should look
   it; staged artifacts are durable and carry the weight.
6. **Config recedes.** Seventeen top-level ops pages collapse into one Settings
   area.
7. **Domain rendering is an ecosystem.** A viewer registry keyed on
   `content_type`, with domain viewers shipped as dashboard plugins.
8. **Export is a first-class exit.** RO-Crate reachable from every frame.
9. **Single-user now, multi-user additive later.** Stable object URLs and
   identity fields present in responses even while unused.

## 3. Information architecture

Seventeen nav items become five.

| New | Contains | Absorbs |
|---|---|---|
| **Frames** | index + detail (results, trace, context) | Sessions, Logs |
| **Artifacts** | durable plane, cross-frame, searchable, lineage | *(new)* |
| **Console** | the embedded TUI terminal | Chat |
| **Automations** | scheduled runs and watchers | Cron |
| **Settings** | Capabilities (Models, Compute & Environments, Skills, MCP, Channels) and Workspace (Keys, Permissions, Storage, Profiles, System) | the remaining twelve |

The Settings split follows the reference platform's Capabilities / Workspace
grouping, which reads better than a flat list of twelve.

### Screens

1. **Frames index** — question asked, output count, reproduce roll-up,
   environment, elapsed.
2. **Frame detail** — three zones: *Results* (artifact tray, dominant), *Trace*
   (cells, each expandable into its `host.*` calls), *Context* (kernel,
   environment, model, backend, Export).
3. **Artifact detail** — typed viewer, version timeline, lineage DAG with
   upstream/downstream toggle, producing cell, reproduce panel, dependents.
4. **Cell detail** — source, stdout/stderr, files read and written, host calls
   with `derivable` flags, versions produced.
5. **Reproduce report** — claim badge, checksum comparison, caveats, replayed
   cells.
6. **Export** — RO-Crate with a preview of contents.

## 4. The notebook question — decided

The reference platform's "notebook" is not a Jupyter kernel: `get_ipython` is
undefined there and `ipykernel` is absent. It is a long-lived interpreter
process whose submitted cells are rendered as a notebook-shaped log.

We run real Jupyter kernels (`jupyter_client.KernelManager`, iopub streaming,
one live kernel per `(session_id, language)` — `science/kernels.py`). That gives
us MIME-bundle outputs, honest interrupt/restart semantics, checkable liveness,
and real tracebacks.

**Decision: the web UI does not execute code.** No user-submitted cells, no
kernel controls, no submit path from the browser. Running analysis stays in the
CLI/TUI.

Consequences:

- The cell timeline is a **view over an append-only execution log**. Re-running
  and editing are out of scope; nothing in the UI can mutate history.
- One cell-timeline component serves three placements: the frame's trace, the
  artifact's provenance (filtered to producing cells), and the cell detail page.
- Kernel state is still shown — language, environment, whether the namespace
  that produced these artifacts is still alive — because it is a fact the reader
  needs, not a control.
- No browser-originated RCE surface. This removes the hardest security question
  from the redesign entirely.
- `execution_log.origin` and `user_intervention` stay in the schema and are
  exposed in API responses. They are how a future human-in-the-kernel feature
  would land without a migration.

## 5. Read-side API

New module `opencodon_cli/science_api.py`, mounted as an `APIRouter` — not
appended to the 18.7k-line `web_server.py`. Read-only; every route is a `GET`.

```
GET /api/science/frames                     list frames (sessions with cells or artifacts)
GET /api/science/frames/{id}                frame detail: cells summary, artifacts, environments
GET /api/science/frames/{id}/cells          the execution trace
GET /api/science/cells/{id}                 one cell + its host calls
GET /api/science/artifacts                  cross-frame index, searchable
GET /api/science/artifacts/{id}             artifact identity + every version
GET /api/science/versions/{id}              version + artifact + producing cell
GET /api/science/versions/{id}/lineage      ?direction=upstream|downstream
GET /api/science/versions/{id}/content      bounded inline preview
GET /api/science/versions/{id}/download     raw bytes from the CAS
GET /api/science/snapshots/{hash}           a content_snapshots payload
```

Notes:

- Profile scoping matches the rest of the dashboard: `?profile=` selects another
  profile's `state.db` via the existing opener.
- The artifact search endpoint must share its index with `list_artifacts` so the
  agent and the user resolve the same name the same way.
- Auth is the existing `/api/*` gate — session token on loopback, cookie
  session under the OAuth gate. No new auth surface.

## 6. Phases

- **Phase 1 — Provenance, read-only.** The API above, plus Frames, Artifacts,
  Artifact detail with lineage, and Cell detail. Nav collapse.
- **Phase 2 — Trace streaming.** Cells appear as they run, over the existing
  `/api/events` channel. Still no submission.
- **Phase 3 — Viewers and addressing.** Viewer registry on `content_type`,
  `{{artifact:VERSION_ID}}` references, `human_description` action labels so the
  trace reads as a lab log.
- **Phase 4 — Reproduce and export as UI actions.** Claim badges everywhere a
  version appears; RO-Crate download.

## 7. Risks

- `web_server.py` is a monolith. Every new route goes in its own module.
- The Console tab is a PTY in xterm.js; it has no addressable spans, so
  annotations and inline artifact embeds can never live there. Structure belongs
  to the trace. Accepted.
- Built assets ship from `opencodon_cli/web_dist/`; `opencodon dashboard` serves
  the built bundle, so UI changes need `npm run build` to appear there.
