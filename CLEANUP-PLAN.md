# Cleanup plan — de-Hermes / de-Nous + slim to necessary functionality

Working doc for the cleanup pass. Companion to `FORK-PLAN.md` (the keep/cut
decision record). Every item below is a discrete, separately-committable unit.

Baseline measured 2026-07-25 on `claude/cleanup-hermes-nous-removal-8dac01`:

- 5762 tracked files, 121 MB working tree
- **1940 files** contain `hermes` or `nous research` (case-insensitive)
- **16,067 matching lines**

---

## MUST KEEP — legal obligation, do not touch

The MIT license requires retaining the upstream copyright notice. These three
mentions of Nous Research are **not** removable:

| File | What stays |
|---|---|
| `LICENSE` | `Copyright (c) 2025 Nous Research` line |
| `NOTICE` | fork provenance paragraph + Nous copyright |
| `.fork/upstream-baseline`, `.fork/triage/` | cherry-pick provenance for upstream security fixes |

Everything else is fair game.

---

## Part A — Hermes → opencodon (rename, no functionality lost)

The Phase-1 rebrand renamed modules and user-facing product strings but left
internal identifiers, docstrings, test fixtures, and compat shims. ~1900 files.

### A1. Compat shims — deliberate one-release aliases, now expiring
Introduced by the rebrand as a migration courtesy. Removing them is a
**breaking change** for anyone who installed pre-rebrand; the repo went public
only at v0.1.0, so blast radius is effectively zero.

| Shim | Location |
|---|---|
| `HERMES_*` env-var fallback | `opencodon_bootstrap.py` |
| `hermes` CLI alias | `scripts/install.ps1`, console-script entries |
| Legacy systemd/launchd unit names (`hermes.service`, `hermes-dashboard.service`) | `gateway/run.py`, `gateway/status.py`, `opencodon_cli/gateway.py`, `opencodon_cli/subcommands/gateway.py` |
| `X-Hermes-Session-Id` / `X-Hermes-Session-Key` HTTP headers | `gateway/run.py`, `gateway/platforms/api_server.py`, `opencodon_cli/web_server.py`, `run_agent.py`, `agent/agent_runtime_helpers.py` |
| Legacy codex managed-block markers | `opencodon_cli/codex_runtime_plugin_migration.py` |

### A2. Internal identifiers — pure mechanical rename
No external contract; safe to rename wholesale.

| Symbol | Occurrences |
|---|---|
| `HermesCLI` / `HermesCLI.__new__` | 325 |
| `HermesTokenStorage` | 104 |
| `hermes.exe` (Windows packaging) | 101 |
| `load_hermes_dotenv` | 84 + 39 qualified |
| `hermes_test` (fixture prefix) | 83 |
| `get_default_hermes_root` / `hermes_root` | 145 |
| `_hermes_now` (cron clock helper) | 75 + 28 qualified |
| `get_hermes_dir` / `hermes_dir` | 110 |
| `HermesHome`, `HermesOverlay`, `HermesConsoleEngine` | 155 |
| `hermes_bin`, `hermes_host`, `hermes_bot`, `hermes_env`, `hermes_slug`, `hermes_id`, `hermes_config` | ~250 |
| `_spawn_hermes_action`, `_setup_hermes_auth`, `with_hermes_node_path`, `expose_hermes_tools`, `generate_hermes_tools_module`, `_is_hermes_internal_secret` | ~200 |
| `hermes_session_pkce`, `hermes_pkce` | 65 |
| `/opt/hermes` container paths | 74 |

### A3. Docstrings, help text, comments
`opencodon_cli/main.py` still opens with `Hermes CLI - Main entry point.` and
documents every subcommand as `hermes <cmd>` (509 hits in that one file).
Same pattern in `opencodon_cli/config.py` (202), `gateway.py` (195),
`web_server.py` (227), `auth.py` (134), `setup.py` (138), `cli.py` (124).

### A4. Docs referencing upstream by name
`AGENTS.md` (56), `CONTRIBUTING.md` (51), `SECURITY.md`, `architecture.md`,
`README.md`, `CHANGELOG.md`, `cli-config.yaml.example` (58),
`Dockerfile` (49), `nix/*.nix` (226 across 12 files).

### A5. Branded assets
`apps/desktop/public/hermes.png`, `hermes-sprite.png`,
`apps/desktop/public/hermes-frames/` (8 frames) — 2.2 MB. These are the pet
sprite sheets; they go away entirely with **B1**.

---

## Part B — Subsystem deletions (functionality cut)

Each of these was already marked CUT or deferred in `FORK-PLAN.md`, or is
orphaned by an earlier cut.

### B1. Pet / companion system — **CUT** (fork plan: novelty)
| Piece | Files | Lines |
|---|---|---|
| `agent/pet/`, `opencodon_cli/pets.py`, `tests/agent/test_pet_engine.py` | 13 | 4,538 |
| Desktop pet UI (`pet-overlay/`, `components/pet/`, `pet-generate/`, `pet-settings.tsx`, `store/pet*.ts`, `vibe-hearts.tsx`) | 28 | 4,225 |
| Branded sprite assets (A5) | 10 | 2.2 MB |

### B2. Kanban — **KEEP** (reversed 2026-07-25, user decision)
Real footprint is **58 files, 53,192 lines** — double the original estimate,
which counted neither the 44 test files nor `gateway/kanban_watchers.py`, the
dashboard plugin, or the docs.

Reversed after reading it. FORK-PLAN listed "kanban" inside a line reading
*"Novelty/third-party plugins: achievements, pets, spotify, google_meet,
kanban, teams_pipeline, video_gen, observability"*. That grouping describes
`plugins/kanban/` (the dashboard), not the kernel — and FORK-PLAN's own
deferral note concedes it ("signal-handler + worker plumbing in
opencodon_cli"). What it actually is:

- a 9,675-line SQLite kernel, 15 tables, statuses triage → todo → ready →
  running → scheduled → blocked → done → archived
- a **dispatcher** hosted in the gateway (`kanban.dispatch_in_gateway`,
  default true) that spawns worker agents to work tasks unattended
- `decompose` — aux LLM fans a task into a dependency graph of children
  assigned across the profile roster; the root outlives its children so the
  orchestrator can judge completion and add more work
- `specify` — aux LLM turns a one-liner into goal + approach + acceptance
  criteria
- `swarm` — parallel specialists → verifier → synthesizer, blackboard kept as
  JSON comments on the root task (no second scheduler, no new service)
- `tools/kanban_tools.py` (2,058 lines) — structured tool surface so workers
  can claim/complete/block from inside Docker/Modal/SSH backends where neither
  the CLI nor the DB is reachable

FORK-PLAN explicitly KEEPS cron/routines for *"scheduled science pipelines."*
Kanban is the richer member of that same family and the only thing here that
can drive a multi-step pipeline to completion unattended.

### B3. MoA (mixture-of-agents) — **KEEP** (reversed 2026-07-25, user decision)
`agent/moa_loop.py`, `agent/moa_trace.py`, `opencodon_cli/moa_cmd.py`,
`opencodon_cli/moa_config.py` → 4 files, 1,915 lines.

FORK-PLAN filed "moa toolsets" under *"Nous ML machinery — training-data
tooling, not our product."* That was true of its neighbours
(`batch_runner`, `trajectory_compressor`, `mini_swe_runner`, rl/datagen),
all already cut in 0a320cb30. What remains is the **inference** runtime:
`/moa` marks a turn, fans out to N advisory reference models concurrently
(cap 8, no tools), and feeds their output to an aggregator — with per-advisor
usage priced at each advisor's *own* model rate, because folding advisor
tokens into the aggregator's usage would misprice every one.

Generic multi-model ensembling, not Nous-specific, not training-related.
Live in the UI (`moa.reference` / `moa.aggregating` events render as labelled
thinking chunks; config in `model-settings.tsx` + `model-menu-panel.tsx`) and
off by default via `_DEFAULT_OFF_TOOLSETS`, so it costs nothing unused.

### B4. Honcho memory provider — **CUT** (fork plan deferral; confirmed 2026-07-25)
`plugins/memory/honcho/`, `tests/honcho_plugin/` → 19 files, 14,213 lines;
~130 core refs with 10 hard import sites. Keep the `MemoryProvider` ABC +
built-in file memory.

A third-party hosted service (`honcho-ai==2.2.0`, optional extra): peer cards
of standing facts, dialectic Q&A, semantic search, persistent conclusions, via
five model tools. Its whole class — mem0, supermemory — is already gone
(c68ff92ec); this one was deferred only because the wiring is mechanical, not
because the decision was in doubt.

### B5. Nous Portal provider + its billing/subscription stack — **CUT**
This is the big one the fork plan deliberately deferred ("identifies us to
external services"). Removing all Nous mentions means removing the provider.

| Piece | Files | Lines |
|---|---|---|
| `plugins/model-providers/nous/` | 2 | — |
| `agent/nous_rate_guard.py`, `portal_tags.py`, `billing_links.py`, `billing_view.py`, `billing_usage.py`, `subscription_view.py`, `credits_tracker.py`, `account_usage.py`, `aux_accounting.py` | 9 | 3,724 |
| `opencodon_cli/nous_{account,auth_keepalive,billing,subscription}.py`, `cli_billing_mixin.py` | 5 | 4,438 |

The billing/credits/subscription stack is Nous-Portal-specific (57 nous refs in
`credits_tracker.py` alone, 36 in `account_usage.py`) — it prices Portal
subscriptions, not generic API keys. **32 other providers remain untouched.**

⚠️ **Decision needed** — see "Open question" below.

### B6. Nous Hermes model IDs in provider catalogs
`nousresearch/hermes-4-405b`, `NousResearch/Hermes-3-Llama-3.1-405B/70B`, etc.
— 151 lines. These are **third-party model identifiers on OpenRouter/HF**;
they are how you address someone else's model. Removing them removes the
ability to call those models. Recommend: drop them from the default catalog
(`catalog/model-catalog.json`, `cli-config.yaml.example`) since they're not our
product, but do not rewrite the strings — a renamed model ID is a broken API call.

### B7. Skills named after upstream
`skills/hermes-desktop-plugins/`, `skills/hermes-themes/`,
`skills/autonomous-ai-agents/hermes-agent/` (205 hits — a skill *about*
hermes-agent), `skills/software-development/hermes-agent-skill-authoring/`
→ 8 files, 2,308 lines.

### B8. Orphans from already-completed cuts
| Orphan | Orphaned by | Files | Lines |
|---|---|---|---|
| `tools/microsoft_graph_{auth,client}.py` + tests | msgraph adapter cut (148451147) | 4 | 1,089 |
| `skills/yuanbao/` | yuanbao adapter cut | 1 | — |

### B9. Novelty CLI subcommands — **CUT** (fork plan: "prune novelty subcommands")
`opencodon_cli/{claw,journey,goals,tips,skin_cmd,skin_engine}.py`
→ 6 files, 4,624 lines. `tips.py` alone carries 91 hermes refs.

---

## Part C — Dead weight, no functionality impact

| Item | Size | Rationale |
|---|---|---|
| `infographic/` (9 dirs of PNGs) | **13 MB** | Upstream PR-decoration artifacts for *their* issues (`feishu-group-events`, `win-clh-lock-traceback`, `list-profiles-perf-54751`). Referenced only by `.gitignore`/`.dockerignore`/`nix/lib.nix`. Unrelated to `skills/creative/baoyu-infographic`, which stays. |
| `contributors/emails/` + `scripts/add_contributor.py` + test | 120 files | Upstream contributor roster (Nous-era GitHub noreply addresses). `scripts/release.py` reads it — needs decoupling. |
| `.mailmap` | 108 lines | Same: maps upstream commit emails. |
| `hermes-already-has-routines.md` | 6 KB | Stale pre-fork research note, named after upstream. |
| `.plans/openai-api-server.md`, `.plans/streaming-support.md` | 35 KB | Upstream design drafts; superseded by `architecture.md`. |
| `locales/` — 15 non-`en` YAML files | 532 KB | Fork plan: "locales replaced by our own". Only 3 modules consume i18n. |
| `CONTRIBUTING.es.md`, `SECURITY.es.md` | 48 KB | Spanish translations of docs being rewritten; will desync immediately. |
| `tools/neutts_samples/*.wav` | 564 KB | TTS voice samples — check if any test/tool actually loads them. |

---

## Decisions taken

| # | Item | Decision | When |
|---|---|---|---|
| B5 | Nous Portal provider + billing/subscription/credits stack (~8,200 lines) | **CUT** — overrides the FORK-PLAN deferral. A provider authenticating against the upstream org's paid portal is not opencodon functionality, and it drags the whole billing UI with it. 32 providers remain. | 2026-07-25 |
| B6 | Nous Hermes model IDs | **Drop the entries** from the default catalog; do NOT rewrite the ID strings — a renamed model ID is a broken API call. | 2026-07-25 |
| B2 | Kanban | **KEEP** — it is the unattended pipeline engine, not novelty. | 2026-07-25 |
| B3 | MoA | **KEEP** — inference-time ensembling, misfiled as training tooling. | 2026-07-25 |
| B4 | Honcho | **CUT** — third-party SaaS; its whole class is already gone. | 2026-07-25 |
| B7 | The four `hermes-*` skills | **RENAME, not cut** — they are the project's own docs. Moved to Part A as A6. | 2026-07-25 |
| B9 | `goals.py`, `skin_engine.py`, `skin_cmd.py` | **KEEP** — the persistent-goal loop and the theming SDK for all three UI surfaces. Only pets/claw/journey/tips were novelty. | 2026-07-25 |

---

## Execution order

Deletions before renames — every subsystem removed takes its `hermes`
references with it, so Part A shrinks as B proceeds.

1. ~~**C** — dead weight~~ ✅ 595604184
2. ~~**B8** — orphans~~ ✅ eef11a895
3. ~~**B1** — pets~~ ✅ b0a145b94
4. ~~**B9** — tips + journey CLI~~ ✅ 77e2ca2ba; ~~claw~~ ✅ 168d0196e
5. **B4** — honcho *(B2/B3 dropped from the plan: keep)*
6. **B5** — Nous Portal + billing
7. **B6** — model IDs out of the default catalog
8. **C2** — locales down to `en` (live subsystem: `SUPPORTED_LANGUAGES`,
   alias map, desktop catalog)
9. **A1** — expire compat shims
10. **A2, A3** — identifier + docstring rename sweep (the bulk: ~1,900 files)
11. **A4, A5** — docs and assets
12. **A6** — rename the four self-documentation skills
13. Retired-platform residue sweep (14 cut adapters left entries in
    `desktop-toolsets.ts`, `session-source.ts`, `platform-icon.tsx`,
    `cli-config.yaml.example`, `gateway/config.py`)
14. Final verification: `git grep -ri -E 'hermes|nous'` should return only
    `LICENSE`, `NOTICE`, `.fork/`, and the ported-fix provenance comments
    (`openclaw/openclaw#NNNN` and friends).

Per step: delete → `scripts/run_tests.sh` → compare failures against the
saved baseline → one commit. Baseline confirmed empirically at 2075 files /
40,991 passed / **26 failed**, matching the documented macOS-specific set in
`FORK-PLAN.md` exactly; the suite is green on ubuntu/CI.

Note: no local test environment existed at the start of this pass (no pytest
anywhere, and `scripts/run_tests.sh` was resolving a stale pre-rebrand
`~/.hermes/hermes-agent/venv`). Built with
`uv sync --locked --python 3.11 --extra all --extra dev --extra messaging`
plus `npm ci` for the JS workspaces.
