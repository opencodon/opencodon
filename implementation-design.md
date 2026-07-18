# Implementation design — the frame architecture on hermes-agent

Status: proposal, 2026-07-18. Companion to `architecture.md` (the reference frame/loop model)
and successor to opencodon's engine-cutover direction.

**Governing principle:** hermes-agent is the base. We adopt a hermes primitive wherever it
satisfies the architecture's *invariant* (not its exact table names), and we build new
components only where hermes has nothing or where its design breaks a reproducibility
invariant. No rewrites for aesthetics. The opencodon repo is the parts donor, not the base —
its science-core modules get ported onto hermes, reversing the 2026-07-11 cutover.

Concept mapping used throughout: **frame = hermes `sessions` row; frame_messages = hermes
`messages`**. We keep hermes's names.

---

## 1. Verdict table

| Architecture subsystem | Hermes today | Verdict |
|---|---|---|
| `frames` tree (parent/root, tokens, cost) | `sessions` (`parent_session_id`, full token/cost accounting) | **EXTEND** — add `root_session_id` (+ backfill), optionally `conversation_type` |
| Append-only `frame_messages` | `messages` insert-only, `active`/`compacted` soft-delete, FTS | **KEEP** |
| The agent loop | `agent/conversation_loop.py` (sync, thread-driven, hardened) | **KEEP** — no async rewrite; every new capability hangs off tool calls |
| Compaction + query-back | `archive_and_compact()` lossless in-place, FTS-searchable | **KEEP** — inline archive is an acceptable realization of `compaction_archives` |
| `queued_user_messages` | interrupt/steer/queue, but queue is in-memory in gateway | **EXTEND (deferred)** — durable table when long science runs make crash-loss real |
| `notifications` bus | `async_delegations` + durable completion delivery | **EXTEND (deferred)** — generalize to typed notifications when remote compute lands |
| Orchestrator daemon | `gateway/` + `tui_gateway/`, JSON-RPC, shared TS client | **KEEP** |
| Multi-agent | `delegate_task` + async delegation; children are `sessions` rows | **KEEP** |
| Scheduling | `cron/` with immutable execution ledger | **KEEP** |
| Model layer / tiers / BYO | `providers/` + 33 provider plugins incl. local | **KEEP** — this is the pluggable-backend seam the doc asks for |
| Skills | Preloaded index + `skill_view` | **KEEP** — with one fix: deterministic loading in science sessions (§6) |
| MCP connectors | Full OAuth 2.1 client, per-tool include/exclude | **KEEP** |
| Memory | MEMORY.md/USER.md | **KEEP** — orthogonal to the science core |
| Credentials / zones | env-var per profile; `HERMES_HOME` profile isolation | **KEEP** — sensitivity zones stay "enforcement by absence" per profile |
| Persistent kernels | none (fresh subprocess per `execute_code`) | **BUILD** (port opencodon `execution/`) |
| `execution_log` / `host_call_log` | none | **BUILD** (port opencodon migrations + store invariants) |
| Artifacts / versions / dependency edges | none (`file_state.py` is concurrency-only) | **BUILD** (port opencodon `storage/` blob+artifact modules) |
| `content_snapshots` | `tool_result_storage.py` spills by tool_use_id | **EXTEND** — re-key spill storage by content hash |
| In-kernel `host.llm` | none (keys deliberately scrubbed from children) | **BUILD** — on hermes's existing PTC RPC bridge, keys stay parent-side |
| Reproduction (`reproduce(version_id)`) | none | **BUILD** (port opencodon `provenance/`) |
| Result-first / collapsible-code UI | web + desktop JSON-RPC clients exist | **BUILD (last)** — new event types, client work |
| Agent profiles (prompt+model+skills+connectors) | split across toolsets/personalities | **DEFER** — not needed for the science core |

Explicitly rejected redesigns (the "not for its own sake" list): async-native loop; renaming
sessions→frames; a lazy `search_skills` catalog; a general event-sourced outbox; an encrypted
keystore; replacing the gateway.

---

## 2. The one genuinely new component: the science layer

Everything we build is one cohesive, additive layer:

```
tools/kernel_tool.py        run_code(code, language) / load_artifact(version_id)
science/kernels.py          lazy KernelManager per (session, language) via jupyter_client
science/store.py            execution_log, host_call_log, artifacts, artifact_versions,
                            artifact_dependencies, content_snapshots  (tables in state.db)
science/blobstore.py        ~/.hermes/artifacts/<sha256>  (per profile)
science/host_bridge.py      host.* RPC inside the kernel (llm, artifacts, mcp) — parent-side
science/reproduce.py        re-run producing cell, checksum-verify
```

### 2.1 Storage: new tables in `state.db`, not a sidecar DB

The architecture's key invariant is atomicity: a tool-result append and its execution/provenance
rows must commit together (opencodon ADR-0005 enforced exactly this). Messages live in
`state.db`; a separate `science.db` would make that transaction impossible. So the science
tables go into `state.db` through `hermes_state.py`'s existing migration machinery, keyed by
`session_id` (+ message idx where relevant). Schemas are ported from opencodon
`storage/migrations/0001–0003` with `frame_id → session_id` renames; drop the hash-chained
`action_log`/`events` outbox (hermes's trajectory + ledger patterns cover the need — rebuild
only if RO-Crate export proves it necessary).

### 2.2 Kernels: jupyter_client, one lazy kernel per (session, language)

Port opencodon `execution/kernel_client.py` + `manager.py`. `run_code` is a **new toolset**,
coexisting with `execute_code` (PTC) and `terminal` — those stay untouched. Every cell writes
an `execution_log` row: source, language, kernel_id, env snapshot (`pip freeze` /
`sessionInfo()`), stdout/stderr, exit status. Local backend first; where the kernel *process*
lives later reuses `tools/environments/` (docker/ssh/modal) — that seam already exists.

### 2.3 Artifacts: explicit-API lineage

An `artifact` helper injected into the kernel namespace (port `execution/artifact_bridge.py`):
writes commit bytes to the content-addressed blob dir + an `artifact_versions` row (checksum,
`producing_cell_id`); `load_artifact(version_id)` returns a local path and inserts one
`artifact_dependencies` edge. Explicit API (v0 spec option (a)), no filesystem diffing.
Artifacts are the cross-language bridge (Python↔R), never shared memory.

### 2.4 Host bridge: reuse the PTC RPC design

`tools/code_execution_tool.py` already runs child code that calls back into whitelisted hermes
tools over a Unix-socket RPC, with credentials kept parent-side. Generalize that bridge into
the kernel: `host.llm(...)` (routed through `providers/` with tier selection — cheap model for
bulk, strong for reasoning), `host.artifacts`, `host.mcp`. Each call = one `host_call_log` row
(child of its cell), result inline or as a `data_ref` into `content_snapshots`. This preserves
hermes's key-scrubbing policy while delivering "LLM as a data primitive."

### 2.5 Convergence invariant

A code cell, an in-kernel `llm()` call, a skill-driven procedure, and a connector fetch must
all land in the same `execution_log` + `host_call_log` + artifact store. No new capability
gets a private data model. This is the property to defend in every review.

---

## 3. Build sequence (each step demoable, headless before UI)

1. **PR 1 — schema seam.** `sessions.root_session_id` migration + backfill; science tables +
   `science/store.py`. Pure additive, no behavior change.
2. **PR 2 — kernel + cell log.** `run_code` toolset on ipykernel; `execution_log` rows; CLI
   demo of a persistent session across turns.
3. **PR 3 — artifacts + lineage.** Blob store, artifact helper, `load_artifact`, dependency
   edges. Recursive-CTE lineage query works.
4. **PR 4 — host bridge.** `host.llm`/`host.artifacts`/`host.mcp` + `host_call_log`;
   re-key `tool_result_storage` spills into `content_snapshots`.
5. **PR 5 — reproduce + export.** `reproduce(version_id)` checksum-verify; port opencodon
   `rocrate.py` export over the new tables.
6. **PR 6 — R kernel.** IRkernel through the same path; per-language env snapshots.
7. **Then:** durable queued messages / notifications (with remote compute), UI result-first +
   collapsible-code contract (new JSON-RPC event types → web/desktop), agent profiles.

**Proof milestone (after PR 5):** the v0-spec CRISPR flow — messy editing-efficiency file in →
persistent-kernel analysis → figure + computed CSV as versioned artifacts → `reproduce`
re-runs and checksums match.

---

## 4. Repo mechanics

- **This repo (`opencodon-hermes`) is the canonical product repo.** All building happens here,
  and it replaces the `opencodon` repo once the science layer reaches parity (decided
  2026-07-18). It is a fork tracking `NousResearch/hermes-agent`; the science layer lives in
  **new modules** (`science/`, new tool files) and diffs to existing hermes core files stay
  minimal, so pulling upstream hermes fixes stays cheap for as long as it's worth doing.
- Opencodon is donor-only from now on: no new features land there, no migration of its
  experimental frame data. Port its science-core modules and keep its docs
  (SCIENCE-CORE-IMPLEMENTATION-PLAN, threat model, ADRs) as the reference for decisions
  already made once. Archive the repo when the PR 5 proof milestone passes here.
- Replacement checklist (for the eventual cutover): CLI name/entry points, `~/.opencodon`
  profile-dir compatibility (or a documented one-time migration), the curated MCP catalog,
  RO-Crate export parity, and the multi-model reviewer — each either ported or consciously
  dropped.

## 5. Risks

- **Loop surgery temptation.** If a phase appears to require editing `conversation_loop.py` /
  `run_agent.py`, treat it as a design smell and find the tool-call-shaped alternative.
- **Dual maintenance.** Until opencodon is formally frozen as donor, every science feature
  risks existing twice. Freeze it explicitly when PR 2 lands.
- **Kernel lifecycle vs hermes environments.** Persistent kernels + idle-reaping remote
  backends (modal/daytona) don't mix yet; keep kernels local-only until the environments seam
  is extended deliberately.

## 6. Determinism fix for skills

Hermes skill loading performs inline `` !`cmd` `` expansion and env templating at load time —
nondeterministic content entering the transcript, which breaks replay. In science sessions:
record the expanded content hash in the transcript (cheap) or disable expansion (cheapest).
Decide in PR 5 when replay lands.
