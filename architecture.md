# Architecture — the frame and the agent loop

Status: **complete reference/data-model input**. The 55-table model supplies vocabulary and
compatibility seams; 

Companion to `v0_spec.md`. Where the spec says *what to build first*, this doc records *how the
reference platform (Claude for Science) is structured* — the control flow and persistence model
that the v0 core is a deliberately-reduced instance of. Verified against the platform's live
metadata schema, not paraphrased.

The design has one central abstraction (**the frame**) and one central cycle (**the loop**).
Everything else is machinery that keeps that cycle robust over long, interruptible, async
sessions.

---

## Part I — The frame and the agent loop (conceptual)

*The narrative architecture. Part II is the complete table-by-table data-model reference; Part III traces how skills, connectors, and tool execution move through it.*

## 1. The frame — the unit of everything

A **frame** is a single unit of agentic work: a top-level conversation, or a delegated sub-task.
It is the spine of the whole system.

Key columns (from the `frames` table):

| column | role |
|---|---|
| `id` | the frame's identity |
| `parent_frame_id` | the frame that spawned this one (null for a top-level session) |
| `root_frame_id` | the top of the tree this frame belongs to |
| `agent_name`, `model`, `effort` | who is running, as what, at what reasoning level |
| `status` | running / awaiting / completed / … |
| `input_data`, `output_data`, `context_data` | the task in, the structured result out, ambient state |
| `input_tokens`, `output_tokens`, `total_cost` | per-frame accounting |

**Why this shape matters:** the `parent_frame_id` / `root_frame_id` self-reference means
**multi-agent falls out for free**. A delegated sub-agent is just a child frame pointing at its
parent and sharing a `root_frame_id`. One table describes both a solo chat and a wide fan-out;
only the parent pointer differs. (This is the structural reason the v0 spec's advice is to keep
the frame as the unit from day one with these columns simply always-null — see §5.)

The conversation itself is **`frame_messages`** — one row per message
(`frame_id, idx, msg_json`), append-only, ordered by `idx`. That ordered log *is* the context
sent to the model each turn.

---

## 2. The loop

The agent loop is the cycle that grows `frame_messages`:

```
      ┌──────────────────────────────────────────────┐
      │ 1. assemble context                           │
      │    read frame_messages (+ live <summary>      │
      │    folds from compaction_archives)            │
      └───────────────────┬──────────────────────────┘
                          │  send to model
      ┌───────────────────┴──────────────────────────┐
      │ 2. model responds                             │
      │    → prose  → stream to user, TURN ENDS        │
      │    → tool call(s) → continue                   │
      └───────────────────┬──────────────────────────┘
                          │
      ┌───────────────────┴──────────────────────────┐
      │ 3. execute tools                              │
      │    code cell → execution_log row              │
      │      (source, language, conda_env, kernel_id, │
      │       stdout/stderr, exit_status,             │
      │       files_written / files_read)             │
      └───────────────────┬──────────────────────────┘
                          │
      ┌───────────────────┴──────────────────────────┐
      │ 4. nested host calls                          │
      │    each host.* inside the cell →              │
      │    host_call_log row (method, args,           │
      │    data_inline | data_ref, bytes)             │
      └───────────────────┬──────────────────────────┘
                          │
      ┌───────────────────┴──────────────────────────┐
      │ 5. append tool results to frame_messages      │
      │    → GOTO 1                                    │
      └────────────────────────────────────────────────┘
```

The cycle in one line: **context → model → tool calls → execute + persist → append results →
repeat**, until the model emits a terminal (prose) response.

### Two-granularity logging (the reproducibility backbone)

The platform logs execution at **two** nested levels, and this is what makes the *inside* of a
turn reproducible, not just the turn:

- **`execution_log`** — one row per code cell. Records `source`, `language`, `conda_env`,
  `kernel_id`, `stdout`/`stderr`, `exit_status`, and the lineage I/O (`files_written`,
  `files_read`). Because `kernel_id` + `conda_env` are per-cell, **one frame drives multiple
  persistent kernels** (Python, R, a stdlib repl) as separate processes sharing a workspace.
- **`host_call_log`** — one row per `host.*` call *inside* a cell (a `query`, an `llm`, an `mcp`,
  an `artifacts` lookup), child of the `execution_log` row via `execution_log_id` + `seq`. It
  records the method, args, and whether the result was returned inline (`data_inline`) or as a
  pointer to a content snapshot (`data_ref`) for large payloads.

The cell log answers *what code ran*; the host-call log answers *what data-access happened while
it ran*.

---

## 3. What makes the loop non-trivial

Two mechanisms turn a naive ReAct loop into something robust for long, real sessions:

**Interruptibility / steering — `queued_user_messages`.** A user message that arrives while a
frame is mid-loop is *queued* (with a `state`) and picked up at the next safe point, rather than
racing the running turn. This is what lets a user interrupt a long-running agent, or steer a
child, without corrupting the append-only transcript — and it's why a new message while a
background cell runs *backgrounds* the cell rather than killing it.

**Detached execution + the notification bus — `notifications`.** Long work (a backgrounded cell,
a remote compute job, a child frame) does not block the loop. The frame ends its turn; a
background poller advances the work and writes a `notifications` row when there's something to act
on; the frame is woken to handle it. The loop is fundamentally **async — it parks rather than
spins.** The same bus carries `compute_done` (remote jobs) and child-frame completions (different
`notification_type`), addressed sender → recipient → root.

---

## 4. Context-limit handling (compaction)

When a session's transcript approaches the model's context window, old turns are **folded into
summaries** rather than truncated. This is lossy in the context window but **lossless on disk**:

- **`compaction_archives`** — each row is one fold: `summary` (the compressed paraphrase kept
  live in context), `messages` (the full verbatim original chunk), `compaction_index` (which
  fold), and `message_count` / `token_count` (how much was reclaimed).
- A **query-back path** reaches into the archived `messages` for a fold to answer against the
  original — one-value extraction, prose recall over the whole span, or raw verbatim bytes. A
  folded conversation stays *queryable at full fidelity* even though it is no longer all
  in-context. In the transcript these appear as `<summary id=…>` blocks; the `id` is the fold to
  query.
- **`content_snapshots`** — content-addressed by `hash`. Large tool outputs are stored once and
  referenced (`data_ref` above), so a bloated message isn't re-embedded in context every turn.
  This attacks the other half of the problem: not just old turns, but individual heavy messages.
- **`frame_read_cursors` / `session_seen_marks`** — track how far a transcript has been
  read/seen, so resumption and incremental processing don't re-scan from the top.

The hard engineering here is the **reversibility + query-back path**, not the summarization. This
is why the v0 spec keeps compaction in Tier 3: it only bites once sessions get genuinely long, and
for early tiers a session that hits the limit can simply stop.

---

## 5. The layered picture

```
┌─────────────────────────────────────────────────────────────┐
│  frames            — units of agentic work (parent/root tree)│
│    └ frame_messages   — the append-only conversation log     │
│         └ execution_log    — each code cell (kernel, env, io)│
│              └ host_call_log — each host.* call inside a cell │
├─────────────────────────────────────────────────────────────┤
│  queued_user_messages — steer/interrupt a running frame      │
│  notifications        — async wakeups (compute_done, child)  │
│  compaction_archives  — fold old turns, keep verbatim on disk│
│  content_snapshots    — store big payloads once, by hash     │
└─────────────────────────────────────────────────────────────┘
```

Everything to the *right* is nested inside the thing to its left; everything *below the line* is
the machinery that keeps the loop robust.

---

## 6. Structural lessons for the open-source build

Both consistent with the "keep the seam, defer the machinery" discipline in `v0_spec.md`.

1. **Model the frame as the unit from day one — even in single-agent v0.** A frame row with
   `parent_frame_id` / `root_frame_id` columns that are simply always-null in v0 costs nothing now
   and turns multi-agent (spec Appendix B, Tier 3 #8) into a new *value* in an existing column,
   not a schema migration. Same move as the provider-agnostic `run_code(target="local")` seam.

2. **Log at two granularities: the cell, and the calls inside it.** The spec's `cells` table is
   the `execution_log` equivalent. What it does not yet have is the `host_call_log` layer — the
   record of data-access calls *within* a cell. v0 does not need it (v0 has only `run_code` +
   `load_artifact`, and reads are already captured as `artifact_dependencies` edges). But the
   moment an in-kernel `llm()` (Tier 1 #2) or connector calls (Tier 2 #5) arrive, a per-call log
   inside the cell starts earning its keep for reproducibility. Design the `cells` table so a
   child call-log table can hang off it later without restructuring.

3. **Make the loop async-shaped early.** Even before remote compute or multi-agent, a loop that
   can *park on a notification* rather than block is the pattern that makes every Tier 3 feature
   additive. The notification bus is a small table (`type` + `payload`, addressed); introducing it
   when the first long-running or backgrounded operation appears is cheaper than retrofitting a
   blocking loop.

---

## 7. The inference layer — who calls the model

The loop (§2) shows *when* the model is called; this section is *who calls it and how the model
backend is reached*. There is no separate "LLM server" per se — there is one long-lived
**orchestrator daemon**, and it is the single component that talks to the model on everyone's
behalf.

### The orchestrator is a persistent daemon, not a per-turn script

When a user sends a message they feed an already-running daemon; a turn is not a fresh process.
That daemon:
- **holds the agent loop** — assembles context from `frame_messages`, calls the model, dispatches
  tool calls, persists results, loops;
- **runs the background poller** — the same process harvests remote-compute jobs and child-frame
  results and writes `notifications`, which is why work proceeds while no turn is actively
  executing;
- **coordinates ownership** — `poller_lease` (`provider → holder, expires_at`) ensures exactly
  **one** poller owns a given provider's polling at a time, so parallel workers don't
  double-harvest.

### Three kinds of model call

1. **The agent-loop call.** Orchestrator → model, once per turn, to drive the conversation. This
   is "the loop."
2. **In-kernel calls (`host.llm`).** Code running in a kernel can itself call the model:
   `host.llm("…")` routes back through the host bridge to the orchestrator, which makes the
   request and returns the result into the kernel. Each such call is logged as a `host_call_log`
   row (child of its `execution_log` cell), so a model call made *by code* is captured for
   reproducibility exactly like a database query. Crucially this enables **LLM-at-data-scale** —
   `host.llm([...list...], max_concurrency=N)` fans out in parallel over thousands of records — a
   different thing from the agent reasoning about one problem. (This is roadmap Tier 1 #2, "LLM as
   a data primitive".)
3. **Sub-agent loop calls.** A delegated child frame runs its own loop and therefore makes its own
   agent-loop calls — same path as (1), just in a child frame under a shared `root_frame_id`.

### Model tiers

The SDK exposes tiers so a caller can route deliberately: `host.current_model()` (the session's
model), `host.reasoning_model()` (a stronger reasoning default), `host.list_models()`. In-kernel
calls should use a cheap model for bulk extraction and a strong one only for hard reasoning — you
don't pay top-tier rates to classify 10,000 rows.

### Two model backends

| backend | table | what it is |
|---|---|---|
| **Hosted API** (default) | `anthropic_api_keys` (`encrypted_api_key`) | the orchestrator holds an encrypted key and calls the hosted model API; both call types above use this by default |
| **Bring-your-own endpoint** | `managed_endpoints` | a registered model service — either a **local model-server container the daemon starts/stops on demand** (`start_script` / `stop_script` / `approved_script_hash` / `state: stopped→running` / `live_path`) or a remote model API. Once registered it becomes another model the loop or an in-kernel `llm()` can target. |

The `managed_endpoints` path is what lets a self-hoster plug in a local or private model (cost,
or data-sensitivity) **without changing the loop** — the model backend is configuration, not
control flow.

### The picture

```
   user ─▶ Orchestrator daemon ────────────────┐   ← holds the agent loop
             │  (frame_messages → model →       │
             │   tools → loop)                  │
             │                                   │
   ┌─────────┴─────────┐          ┌──────────────┴──────────────┐
   │ 1. agent-loop call│          │ background poller            │
   │    → model         │          │ (harvest jobs, write         │
   └───────────────────┘          │  notifications; poller_lease │
                                   │  = one owner per provider)   │
   kernel (your code)             └──────────────────────────────┘
     │ host.llm(...)   ── 2. in-kernel call ─▶ orchestrator ─▶ model
     │                    (host_call_log; parallel fan-out;
     │                     picks model tier)
     ▼
   model backend:
     • anthropic_api_keys  → hosted model API (default)
     • managed_endpoints   → local model-server container (start/stop on
                             demand) OR remote BYO model API
```

### Lessons for the open-source build

1. **Orchestrator = persistent daemon**, not a per-request handler — this is what makes the async
   loop, the poller, and interruption possible. In v0 it can be a single-process app, but keep the
   model call behind a small internal client so tiers/endpoints can be added without touching call
   sites.
2. **`host.llm()` in the kernel is a genuine differentiator** — it turns the model into a
   data-processing primitive, not just the agent's brain, and it's cheap because it reuses the
   client the loop already has.
3. **A pluggable model backend matters for an open-source project** — many users will want a local
   or private model for cost or data-sensitivity. Model "which model/endpoint" as configuration
   from early on, so BYO-model is a new backend behind an existing seam.

---

## Part II — Complete data model reference

*Generated from the live metadata DB (SQLite). 55 application tables (excluding `sqlite_*` internals and the Drizzle migration ledger), grouped into 14 functional subsystems. Row counts are from this project's DB at capture time; "access-restricted" marks tables whose contents are privacy-denied to querying — their schema is visible, their rows are not (credentials, grants, consent, agent/connector config).*

### Subsystem map

| # | Subsystem | Tables |
|---|---|---|
| 1 | Frames & conversation (the agent loop) | 10 |
| 2 | Execution & reproducibility | 4 |
| 3 | Artifacts & lineage | 6 |
| 4 | Context management (compaction) | 1 |
| 5 | Memory | 3 |
| 6 | Remote compute | 5 |
| 7 | Skills | 4 |
| 8 | Agents & specialists | 4 |
| 9 | Connectors (MCP) | 4 |
| 10 | Credentials & secrets | 5 |
| 11 | Model serving | 1 |
| 12 | Host access & capabilities | 4 |
| 13 | Scheduling & automation | 1 |
| 14 | Governance & safety | 3 |

### 1. Frames & conversation (the agent loop)

**`frames`** — one unit of agentic work; parent/root tree = multi-agent  
_7 rows_

| column | type | key |
|---|---|---|
| `id` | text | PK |
| `parent_frame_id` | text |  |
| `root_frame_id` | text |  |
| `agent_name` | text | req |
| `status` | text | req |
| `input_data` | text |  |
| `output_data` | text |  |
| `context_data` | text |  |
| `model` | text |  |
| `effort` | text |  |
| `input_tokens` | integer |  |
| `output_tokens` | integer |  |
| `total_cost` | real |  |
| `created_at` | integer | req |
| `updated_at` | integer | req |
| `completed_at` | integer |  |
| `project_id` | text |  |
| `name` | text |  |
| `conversation_type` | text | req |
| `artifact_id` | text |  |
| `task_summary` | text |  |
| `mentioned_artifact_ids` | text |  |
| `specialists_used` | text |  |
| `is_hidden` | integer |  |
| `status_description` | text |  |
| `compute_enabled` | text |  |
| `delegate_name` | text |  |
| `cache_read_tokens` | integer |  |
| `cache_write_tokens` | integer |  |
| `last_user_message_at` | integer |  |
| `last_extract_msg_idx` | integer |  |
| `root_seq` | integer | req |
| `aux_input_tokens` | integer |  |
| `aux_output_tokens` | integer |  |
| `aux_cache_read_tokens` | integer |  |
| `aux_cache_write_tokens` | integer |  |
| `aux_cost` | real |  |
| `token_class_usage` | text |  |

**`frame_messages`** — append-only conversation log, one row/message — IS the model context  
_365 rows_

| column | type | key |
|---|---|---|
| `frame_id` | text | req |
| `idx` | integer | req |
| `msg_json` | text | req |
| `msg_uuid` | text |  |

**`frame_system_prompts`** — system prompt per frame  
_6 rows_

| column | type | key |
|---|---|---|
| `frame_id` | text | PK |
| `hash` | text | req |
| `updated_at` | integer | req |
| `payload` | text | req |

**`frame_read_cursors`** — how far a transcript has been read (resumption)  
_10 rows_

| column | type | key |
|---|---|---|
| `root_frame_id` | text | PK |
| `message_uuid` | text |  |
| `message_index` | integer | req |
| `updated_at` | integer | req |

**`session_seen_marks`** — seen-token marks for incremental processing  
_5 rows_

| column | type | key |
|---|---|---|
| `root_frame_id` | text | PK |
| `seen_token` | text | req |
| `updated_at` | integer | req |

**`frame_branch_archives`** — archived branches when a conversation forks  
_0 rows_

| column | type | key |
|---|---|---|
| `frame_id` | text | req |
| `branch_id` | text | req |
| `payload` | text | req |
| `updated_at` | integer | req |

**`frame_backfill_poison`** — guards against re-processing poisoned backfill  
_0 rows_

| column | type | key |
|---|---|---|
| `frame_id` | text | PK |
| `fail_count` | integer | req |
| `terminal` | integer | req |
| `reason` | text |  |
| `updated_at` | integer | req |

**`queued_user_messages`** — mid-loop user messages picked up at next safe point (steering)  
_27 rows_

| column | type | key |
|---|---|---|
| `seq` | integer | PK |
| `frame_id` | text | req |
| `payload` | text | req |
| `intent_id` | text | req |
| `state` | text | req |
| `resolved_at` | integer |  |
| `created_at` | integer | req |

**`session_claims`** — textual claims made in a session, tagged entities/source (semantics inferred)  
_0 rows_

| column | type | key |
|---|---|---|
| `id` | text | PK |
| `root_frame_id` | text | req |
| `frame_id` | text | req |
| `step_id` | text |  |
| `claim_text` | text | req |
| `entities` | text |  |
| `source` | text | req |
| `created_at` | integer | req |

**`session_concurrency`** — concurrency accounting per session  
_0 rows_

| column | type | key |
|---|---|---|
| `root_frame_id` | text | PK |
| `max_concurrent` | integer | req |
| `created_at` | integer | req |
| `updated_at` | integer | req |

### 2. Execution & reproducibility

**`execution_log`** — one row per code cell (kernel, env, io, stdout/stderr)  
_84 rows_

| column | type | key |
|---|---|---|
| `id` | text | PK |
| `frame_id` | text | req |
| `cell_index` | integer | req |
| `kernel_id` | text | req |
| `conda_env` | text | req |
| `language` | text | req |
| `source` | text | req |
| `stdout` | text |  |
| `stderr` | text |  |
| `exit_status` | text | req |
| `created_at` | integer | req |
| `files_written` | text |  |
| `error_lineno` | integer |  |
| `kernel_kind` | text |  |
| `origin` | text | req |
| `detection` | text |  |
| `files_read` | text |  |
| `user_intervention` | text |  |

**`host_call_log`** — one row per host.* call inside a cell (method, args, inline|ref)  
_166 rows_

| column | type | key |
|---|---|---|
| `id` | integer | PK |
| `execution_log_id` | text | req |
| `seq` | integer | req |
| `method` | text | req |
| `args_json` | text | req |
| `derivable` | integer | req |
| `data_inline` | text |  |
| `data_ref` | text |  |
| `error` | text |  |
| `bytes` | integer | req |
| `created_at` | integer | req |

**`content_snapshots`** — large payloads stored once, content-addressed by hash  
_15 rows_

| column | type | key |
|---|---|---|
| `hash` | text | PK |
| `content` | text | req |
| `size_bytes` | integer | req |
| `created_at` | integer | req |

**`events`** — internal event stream  
_0 rows_

| column | type | key |
|---|---|---|
| `id` | text | PK |
| `frame_id` | text | req |
| `event_type` | text | req |
| `payload` | text |  |
| `timestamp` | integer | req |

### 3. Artifacts & lineage

**`artifacts`** — artifact identity (stable across versions)  
_11 rows_

| column | type | key |
|---|---|---|
| `id` | text | PK |
| `project_id` | text | req |
| `root_frame_id` | text | req |
| `frame_id` | text |  |
| `filename` | text | req |
| `created_at` | integer | req |
| `latest_version_id` | text |  |
| `is_user_upload` | integer | req |
| `is_ephemeral` | integer | req |
| `folder_id` | text |  |
| `sort_order` | integer | req |
| `priority` | text | req |
| `superseded_by_artifact_id` | text |  |
| `consumed_at` | integer |  |
| `is_branch_mint` | integer | req |
| `agent_rename_history` | text |  |

**`artifact_versions`** — per-version: content_type/size/checksum/storage_path + lineage cols  
_25 rows_

| column | type | key |
|---|---|---|
| `id` | text | PK |
| `artifact_id` | text | req |
| `version_number` | integer | req |
| `frame_id` | text |  |
| `content_type` | text | req |
| `size_bytes` | integer | req |
| `checksum` | text | req |
| `storage_path` | text | req |
| `created_at` | integer | req |
| `extracted_code` | text |  |
| `code_description` | text |  |
| `lineage_messages` | text |  |
| `agent_name` | text |  |
| `language` | text |  |
| `is_intermediate` | integer | req |
| `dependency_mappings` | text |  |
| `environment_snapshot` | text |  |
| `annotations` | text |  |
| `parent_version_id` | text |  |
| `lineage_snapshot_hash` | text |  |
| `env_snapshot_hash` | text |  |
| `producing_cell_id` | text |  |
| `cell_sources` | text |  |
| `is_checkpoint` | integer |  |

**`artifact_dependencies`** — provenance edges: version → depends_on_version  
_17 rows_

| column | type | key |
|---|---|---|
| `id` | text | PK |
| `artifact_version_id` | text | req |
| `depends_on_version_id` | text | req |
| `reference_name` | text |  |
| `created_at` | integer | req |

**`artifact_folders`** — folder organization in the artifact panel  
_1 rows_

| column | type | key |
|---|---|---|
| `id` | text | PK |
| `project_id` | text | req |
| `parent_id` | text |  |
| `name` | text | req |
| `sort_order` | integer | req |
| `root_frame_id` | text |  |
| `is_conversation_folder` | integer | req |
| `is_user_uploads_folder` | integer | req |
| `created_at` | integer | req |
| `updated_at` | integer | req |

**`annotations`** — user draw/comment on a rendered artifact (feedback loop)  
_0 rows_

| column | type | key |
|---|---|---|
| `id` | text | PK |
| `project_id` | text | req |
| `target_kind` | text | req |
| `target_key` | text | req |
| `label_idx` | integer | req |
| `content_checksum` | text |  |
| `body` | text | req |
| `created_at` | integer | req |
| `updated_at` | integer |  |

**`transcript_annotations`** — annotations attached to transcript spans  
_0 rows_

| column | type | key |
|---|---|---|
| `id` | text | PK |
| `root_frame_id` | text | req |
| `message_uuid` | text |  |
| `message_index` | integer | req |
| `block_index` | integer | req |
| `source` | text | req |
| `tool_name` | text |  |
| `anchor_text` | text | req |
| `start_offset` | integer |  |
| `end_offset` | integer |  |
| `kind` | text | req |
| `note` | text | req |
| `created_at` | integer | req |
| `updated_at` | integer | req |
| `origin` | text | req |
| `read_at` | integer |  |
| `anchor_prefix` | text |  |

### 4. Context management (compaction)

**`compaction_archives`** — folded turns: summary (live) + messages (verbatim on disk)  
_0 rows_

| column | type | key |
|---|---|---|
| `id` | text | PK |
| `frame_id` | text | req |
| `compaction_index` | integer | req |
| `message_count` | integer | req |
| `token_count` | integer |  |
| `summary` | text | req |
| `messages` | text | req |
| `created_at` | integer | req |

### 5. Memory

**`memories`** — durable facts (profile/project/artifact/frame scoped)  
_37 rows_

| column | type | key |
|---|---|---|
| `id` | text | PK |
| `user_id` | text | req |
| `body` | text | req |
| `subject_project_id` | text |  |
| `subject_artifact_id` | text |  |
| `subject_version_id` | text |  |
| `subject_frame_id` | text |  |
| `source_frame_id` | text |  |
| `origin` | text | req |
| `evidence` | text | req |
| `superseded_by` | text |  |
| `created_at` | integer | req |
| `updated_at` | integer | req |
| `last_surfaced_at` | integer |  |
| `category_id` | text |  |

**`memory_categories`** — user-defined memory categories  
_0 rows_

| column | type | key |
|---|---|---|
| `id` | text | PK |
| `user_id` | text | req |
| `name` | text | req |
| `name_lower` | text | req |
| `guidance` | text | req |
| `auto_recall` | integer | req |
| `created_at` | integer | req |
| `updated_at` | integer | req |

**`notes`** — project notes  
_0 rows_

| column | type | key |
|---|---|---|
| `id` | text | PK |
| `project_id` | text | req |
| `user_id` | text | req |
| `target_type` | text | req |
| `target_frame_id` | text | req |
| `target_message_index` | integer |  |
| `target_artifact_id` | text |  |
| `content` | text | req |
| `created_at` | integer | req |
| `updated_at` | integer | req |

### 6. Remote compute

**`compute_usage`** — job ledger: state pending→running→terminal, harvest contract  
_0 rows_

| column | type | key |
|---|---|---|
| `id` | text | PK |
| `job_id` | text | req |
| `environment` | text | req |
| `tier_type` | text | req |
| `provider` | text | req |
| `frame_id` | text |  |
| `project_id` | text |  |
| `started_at` | integer | req |
| `ended_at` | integer |  |
| `expires_at` | integer |  |
| `client_uuid` | text |  |
| `remote_workdir` | text |  |
| `remote_handle` | text |  |
| `state` | text | req |
| `output_specs` | text |  |
| `submit_cell_id` | text |  |
| `intent` | text |  |
| `hardware_details` | text |  |
| `root_frame_id` | text |  |
| `result` | text |  |
| `origin_tool_use_id` | text |  |

**`compute_providers`** — registered compute targets  
_access-restricted_

| column | type | key |
|---|---|---|
| `name` | text | PK |
| `family` | text | req |
| `memory_md` | text | req |
| `environments` | text | req |
| `memory_rev` | integer | req |
| `scratch_root` | text |  |
| `scheduler` | text |  |
| `probed_at` | integer |  |
| `data_roots` | text | req |
| `ssh_overrides` | text |  |
| `max_concurrent_jobs` | integer |  |
| `max_timeout_sec` | integer |  |
| `enabled` | integer | req |
| `scratch_root_source` | text | req |
| `home` | text |  |
| `scratch_root_revalidate_failed_at` | integer |  |
| `infer_config` | text |  |
| `app_name` | text |  |
| `prior_app_names` | text |  |
| `egress_policy` | text |  |
| `modal_environment` | text |  |

**`compute_pending_terminate`** — sandboxes queued for teardown  
_access-restricted_

| column | type | key |
|---|---|---|
| `sandbox_id` | text | PK |
| `provider` | text | req |
| `enqueued_at` | integer | req |
| `attempts` | integer | req |

**`poller_lease`** — one-owner-per-provider polling lease  
_1 rows_

| column | type | key |
|---|---|---|
| `provider` | text | PK |
| `holder` | text | req |
| `expires_at` | integer | req |

**`notifications`** — async wakeups: compute_done, child completion (type+payload)  
_1 rows_

| column | type | key |
|---|---|---|
| `id` | text | PK |
| `sender_frame_id` | text | req |
| `recipient_frame_id` | text | req |
| `root_frame_id` | text | req |
| `notification_type` | text | req |
| `payload` | text |  |
| `read_at` | integer |  |
| `created_at` | integer | req |

### 7. Skills

**`custom_skills`** — user-authored skills (name/description/content)  
_access-restricted_

| column | type | key |
|---|---|---|
| `id` | text | PK |
| `user_id` | text | req |
| `name` | text | req |
| `description` | text | req |
| `content` | text | req |
| `created_at` | integer | req |
| `updated_at` | integer | req |

**`agent_skill_assignments`** — pin a skill subset to a named agent profile  
_access-restricted_

| column | type | key |
|---|---|---|
| `id` | text | PK |
| `skill_id` | text | req |
| `agent_name` | text | req |
| `user_id` | text | req |
| `created_at` | integer | req |

**`skill_license_assents`** — per-skill license/consent decision  
_access-restricted_

| column | type | key |
|---|---|---|
| `id` | text | PK |
| `user_id` | text | req |
| `org_id` | text |  |
| `resource_key` | text | req |
| `skill_name` | text | req |
| `decision` | text | req |
| `notice_version` | text | req |
| `notice_text` | text | req |
| `created_at` | integer | req |
| `project_id` | text |  |

**`marketplace_sources`** — shared/community skill sources  
_access-restricted_

| column | type | key |
|---|---|---|
| `id` | text | PK |
| `user_id` | text | req |
| `slug` | text | req |
| `kind` | text | req |
| `marketplace_name` | text | req |
| `pinned_sha` | text | req |
| `license` | text | req |
| `offered_skills` | text |  |
| `created_at` | integer | req |
| `last_imported_at` | integer | req |

### 8. Agents & specialists

**`agents`** — custom agent profiles  
_access-restricted_

| column | type | key |
|---|---|---|
| `id` | text | PK |
| `name` | text | req |
| `url` | text | req |
| `description` | text |  |
| `parameters` | text |  |
| `created_at` | integer | req |
| `updated_at` | integer | req |

**`user_agents`** — user-owned agent definitions  
_access-restricted_

| column | type | key |
|---|---|---|
| `id` | text | PK |
| `user_id` | text | req |
| `name` | text | req |
| `display_name` | text | req |
| `description` | text | req |
| `system_prompt` | text | req |
| `icon_key` | text | req |
| `color_key` | text | req |
| `tags` | text | req |
| `skill_names` | text | req |
| `enabled` | integer | req |
| `created_at` | integer | req |
| `updated_at` | integer | req |
| `skill_tombstones` | text | req |
| `connector_tombstones` | text | req |
| `unrestricted` | integer | req |

**`custom_agent_prompts`** — custom prompt overrides  
_access-restricted_

| column | type | key |
|---|---|---|
| `id` | text | PK |
| `user_id` | text | req |
| `agent_name` | text | req |
| `prompt_text` | text | req |
| `created_at` | integer | req |
| `updated_at` | integer | req |

**`bundled_agent_settings`** — settings for bundled agents  
_access-restricted_

| column | type | key |
|---|---|---|
| `id` | text | PK |
| `user_id` | text | req |
| `agent_name` | text | req |
| `enabled` | integer | req |
| `created_at` | integer | req |
| `updated_at` | integer | req |

### 9. Connectors (MCP)

**`custom_mcp_servers`** — registered MCP servers (url/transport/oauth)  
_access-restricted_

| column | type | key |
|---|---|---|
| `id` | text | PK |
| `user_id` | text | req |
| `name` | text | req |
| `description` | text |  |
| `url` | text | req |
| `transport` | text | req |
| `oauth_server_url` | text |  |
| `client_id` | text |  |
| `scopes` | text |  |
| `created_at` | integer | req |
| `updated_at` | integer | req |
| `source` | text | req |
| `headers_helper` | text |  |
| `resource_identifier` | text |  |

**`mcp_tool_grants`** — per-tool allow/deny decision  
_access-restricted_

| column | type | key |
|---|---|---|
| `id` | text | PK |
| `user_id` | text | req |
| `server_id` | text | req |
| `tool_name` | text | req |
| `decision` | text | req |
| `created_at` | integer | req |

**`mcp_agent_assignments`** — which MCP servers a profile may use  
_access-restricted_

| column | type | key |
|---|---|---|
| `id` | text | PK |
| `mcp_server_id` | text | req |
| `agent_name` | text | req |
| `user_id` | text | req |
| `created_at` | integer | req |
| `excluded_tools` | text | req |

**`oauth_tokens`** — OAuth tokens for connectors  
_access-restricted_

| column | type | key |
|---|---|---|
| `id` | text | PK |
| `user_id` | text | req |
| `mcp_server_id` | text | req |
| `encrypted_access_token` | text | req |
| `encrypted_refresh_token` | text |  |
| `token_type` | text | req |
| `expires_at` | integer |  |
| `scopes` | text |  |
| `created_at` | integer | req |
| `updated_at` | integer | req |
| `client_id` | text |  |

### 10. Credentials & secrets

**`anthropic_api_keys`** — encrypted model API key (hosted backend)  
_access-restricted_

| column | type | key |
|---|---|---|
| `id` | text | PK |
| `user_id` | text | req |
| `encrypted_api_key` | text | req |
| `created_at` | integer | req |
| `updated_at` | integer | req |

**`cloud_credentials`** — AWS/GCP/etc credentials  
_access-restricted_

| column | type | key |
|---|---|---|
| `id` | text | PK |
| `user_id` | text | req |
| `provider` | text | req |
| `name` | text | req |
| `credential_type` | text | req |
| `encrypted_credentials` | text | req |
| `encrypted_refresh_token` | text |  |
| `token_expires_at` | integer |  |
| `default_bucket` | text |  |
| `region` | text |  |
| `created_at` | integer | req |
| `updated_at` | integer | req |

**`user_secrets`** — generic user secrets  
_access-restricted_

| column | type | key |
|---|---|---|
| `id` | text | PK |
| `user_id` | text | req |
| `name` | text | req |
| `provider` | text | req |
| `encrypted_value` | text | req |
| `credential_type` | text |  |
| `buckets` | text |  |
| `region` | text |  |
| `description` | text |  |
| `created_at` | integer | req |
| `updated_at` | integer | req |

**`credential_ask_decisions`** — record of credential-ask consent  
_access-restricted_

| column | type | key |
|---|---|---|
| `id` | text | PK |
| `user_id` | text | req |
| `provider` | text | req |
| `decision` | text | req |
| `created_at` | integer | req |

**`contact_email_decisions`** — contact-email consent decisions  
_access-restricted_

| column | type | key |
|---|---|---|
| `id` | text | PK |
| `user_id` | text | req |
| `decision` | text | req |
| `email` | text |  |
| `notice_version` | text | req |
| `notice_text` | text | req |
| `created_at` | integer | req |

### 11. Model serving

**`managed_endpoints`** — BYO model server: start/stop scripts, state, live_path  
_access-restricted_

| column | type | key |
|---|---|---|
| `name` | text | PK |
| `url` | text | req |
| `port` | integer | req |
| `credential_name` | text |  |
| `skill_name` | text | req |
| `start_script` | text | req |
| `stop_script` | text | req |
| `live_path` | text | req |
| `approved_script_hash` | text | req |
| `state` | text | req |
| `state_changed_at` | integer |  |
| `last_error` | text |  |
| `transcript` | text |  |
| `created_at` | integer | req |
| `registered_by` | text |  |

### 12. Host access & capabilities

**`host_grants`** — granted host directory paths (ro/rw)  
_access-restricted_

| column | type | key |
|---|---|---|
| `id` | text | PK |
| `user_id` | text | req |
| `host_path` | text | req |
| `mount_name` | text | req |
| `created_at` | integer | req |
| `mode` | text | req |

**`directory_attachments`** — attached local directories  
_access-restricted_

| column | type | key |
|---|---|---|
| `server_uuid` | text | req |
| `agent_name` | text | req |
| `user_id` | text | req |
| `created_at` | integer | req |
| `excluded_tools` | text | req |

**`capability_settings`** — per-capability enable/disable  
_access-restricted_

| column | type | key |
|---|---|---|
| `user_id` | text | req |
| `kind` | text | req |
| `key` | text | req |
| `enabled` | integer | req |
| `updated_at` | integer | req |

**`use_intent_declarations`** — declared use intents  
_access-restricted_

| column | type | key |
|---|---|---|
| `id` | text | PK |
| `user_id` | text | req |
| `org_id` | text |  |
| `intent` | text | req |
| `created_at` | integer | req |

### 13. Scheduling & automation

**`routine_schedules`** — recurring/scheduled runs (cron-like tick)  
_0 rows_

| column | type | key |
|---|---|---|
| `id` | text | PK |
| `root_frame_id` | text | req |
| `owner_user_id` | text | req |
| `label` | text |  |
| `on_tick` | text | req |
| `every_minutes` | integer | req |
| `enabled` | integer | req |
| `locked_at` | integer |  |
| `paused_reason` | text |  |
| `next_due` | integer | req |
| `tick_count` | integer | req |
| `missed_ticks` | integer | req |
| `last_fire_at` | integer |  |
| `last_ok_at` | integer |  |
| `idle_streak` | integer | req |
| `last_results` | text |  |
| `created_at` | integer | req |
| `updated_at` | integer | req |

### 14. Governance & safety

**`safety_feedback`** — safety feedback records  
_access-restricted_

| column | type | key |
|---|---|---|
| `id` | text | PK |
| `root_frame_id` | text | req |
| `user_id` | text | req |
| `type` | text | req |
| `model` | text |  |
| `reason` | text |  |
| `response_id` | text |  |
| `context_snapshot` | text |  |
| `created_at` | integer | req |

**`verification_checks`** — auditor/verification findings on frames  
_8 rows_

| column | type | key |
|---|---|---|
| `id` | text | PK |
| `root_frame_id` | text | req |
| `artifact_version_id` | text |  |
| `claim_id` | text |  |
| `claim` | text |  |
| `verdict` | text | req |
| `severity` | text |  |
| `evidence` | text |  |
| `rebuttal` | text |  |
| `reviewer_idx` | integer |  |
| `reviewer_model` | text |  |
| `reviewer_frame_id` | text |  |
| `source_ref` | text | req |
| `status` | text | req |
| `reflag_count` | integer |  |
| `created_at` | integer | req |
| `reviewer_kind` | text |  |

**`projects`** — project identity  
_1 rows_

| column | type | key |
|---|---|---|
| `id` | text | PK |
| `name` | text |  |
| `description` | text |  |
| `context` | text |  |
| `created_at` | integer | req |
| `updated_at` | integer | req |
| `user_id` | text |  |
| `uploads_frame_id` | text |  |
| `memory_enabled` | integer |  |

---

## Part III — How skills, MCP, and tool execution thread through the model

Parts I–II describe the structure; this part traces the *dynamics* — what actually gets written
to which table when the agent uses a tool, loads a skill, or calls a connector. Every path below
ends in the same place: rows in the execution/lineage tables, so the action is reproducible.

### A. Tool execution — the two-granularity trace

A "tool" is anything the model invokes in a turn. The ones that matter for reproducibility are
the code-execution tools. When the model emits a `run_code`-class tool call:

1. The orchestrator runs the source in the target kernel (`kernel_id` + `conda_env` select which
   of the several live kernels — Python, R, stdlib repl).
2. One **`execution_log`** row is written: `source`, `language`, `conda_env`, `kernel_id`,
   `exit_status`, `stdout`, `stderr`, and the lineage I/O — `files_written` and `files_read`. An
   `origin` column distinguishes agent-run cells from user/system-run ones; `detection` and
   `user_intervention` record whether a human touched the cell.
3. Any file the cell wrote becomes (or versions) an **`artifacts`** + **`artifact_versions`**
   pair; any artifact it read becomes an **`artifact_dependencies`** edge. This is the provenance
   DAG, captured as a side effect of normal I/O.
4. The tool result is appended to **`frame_messages`**, and the loop continues.

So a single code tool call touches, at minimum: `execution_log` (the cell), possibly
`artifact_versions` + `artifact_dependencies` (outputs/inputs), and `frame_messages` (the result
fed back to the model). Nothing about the run is left only in memory.

### B. `host.*` calls — the inner granularity

Inside a cell, code can call the host bridge (`host.query`, `host.llm`, `host.artifacts`,
`host.mcp`, …). Each such call is one **`host_call_log`** row, child of the `execution_log` row
via `execution_log_id` + `seq`:

- `method` + `args_json` — what was called with what;
- `data_inline` **vs** `data_ref` — small results are stored inline; large ones are pushed to
  **`content_snapshots`** (content-addressed by `hash`) and referenced, so a big payload is stored
  once and never re-embedded in context;
- `bytes`, `error`, `derivable` — size, failure, and whether the result is deterministically
  re-derivable.

This is why the *inside* of a cell is reproducible, not just its source: a cell that called
`host.llm()` 500 times to classify records has 500 `host_call_log` rows describing exactly what
crossed the boundary.

### C. Skills — discovered lazily, loaded on demand

Skills are **not** preloaded into the model's context. The catalog is an index of tiny entries
(`name` + `description` + `origin`); a skill's body enters context only when loaded. The access
pattern is two steps:

1. **Discover** — a `search_skills`-class call ranks catalog descriptions against a query and
   returns a few candidates. (The ranking is a lexical/word-overlap match over descriptions;
   whether it is specifically BM25 and capped at ~4 is an implementation detail of the reference
   platform, not a requirement.)
2. **Load** — a `skill(name)`-class call pulls that skill's full content into context; if the
   skill ships a `kernel.py`/`kernel.R` sidecar, its helper functions are injected into the live
   kernel so the procedure becomes immediately callable, not just readable.

Where skills live in the data model:
- **`custom_skills`** (`name`, `description`, `content`) — user-authored skills stored in the DB.
- **`marketplace_sources`** — shared/community sources a catalog can pull from.
- **`agent_skill_assignments`** (`skill_id → agent_name`) — pins an explicit skill *subset* to a
  named agent profile. (Confirmed: this is how a profile is scoped to a subset. Whether a profile
  with *no* assignments inherits the full catalog is the reference platform's default behavior,
  not verified here.)
- **`skill_license_assents`** — records the user's per-skill license/consent decision before a
  third-party skill is used.

A loaded skill is reference material the model reads; it does not itself create execution rows.
The rows appear when the skill's guidance leads the agent to run code or call the host — i.e.
skills feed back into paths A and B.

### D. Connectors (MCP) — external tools behind a consent gate

An MCP server is an external tool provider (a database, a SaaS API, a domain data source). It
enters the data model as:
- **`custom_mcp_servers`** — the registered server: `url`, `transport`, and the OAuth wiring
  (`oauth_server_url`, `client_id`, `scopes`, `resource_identifier`, `headers_helper`).
- **`oauth_tokens`** — the tokens obtained for it.
- **`mcp_tool_grants`** (`server_id`, `tool_name`, `decision`) — a **per-tool** allow/deny
  decision. Consent is scoped to individual tools, not the whole server.
- **`mcp_agent_assignments`** — which servers a given agent profile may reach.

At call time, an MCP tool is invoked through the host bridge from a repl cell
(`host.mcp(server, method, …)`), so it is logged exactly like any other host call — a
**`host_call_log`** row under its `execution_log` cell. That means a connector fetch is captured
with the same fidelity as a database query: method, args, and the returned bytes (inline or
snapshotted). A connector's *result* becomes an artifact only when code writes it to a file —
keeping the "everything feeds the artifact store" invariant intact.

### E. The unifying invariant

Four different capabilities — a code cell, an in-kernel LLM call, a skill-driven procedure, a
connector fetch — all converge on the same two-granularity execution trace (`execution_log` +
`host_call_log`) and the same artifact/lineage store. There is no capability with its own private
data model. That convergence is precisely what makes the whole system reproducible and is the
single most important property to preserve when extending it.
