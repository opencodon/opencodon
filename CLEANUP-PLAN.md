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

### B2. Kanban — **CUT** (fork plan deferral: "signal-handler + worker plumbing")
`opencodon_cli/kanban{,_db,_decompose,_diagnostics,_specify,_swarm}.py`,
`agent/kanban_stop.py`, `tools/kanban_tools.py`, `plugins/kanban/`
→ **13 files, 25,825 lines**, plus 224 files referencing it (ACP adapter,
prompt builder, system prompt, tool executor, desktop sidebar, routes).
Largest single win in the pass.

### B3. MoA (mixture-of-agents) — **CUT** (fork plan: "moa toolset remnants")
`agent/moa_loop.py`, `agent/moa_trace.py`, `opencodon_cli/moa_cmd.py`,
`opencodon_cli/moa_config.py` → 4 files, 1,915 lines; 12 files referencing.

### B4. Honcho memory provider — **CUT** (fork plan deferral)
`plugins/memory/honcho/`, `tests/honcho_plugin/` → 19 files, 14,213 lines;
120 files referencing. Keep the `MemoryProvider` ABC + built-in file memory.

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

## Open question — B5 scope

`FORK-PLAN.md` deliberately kept the Nous Portal OAuth client ID and provider
because they "identify us to external services." Cutting all Nous mentions
means **cutting the Portal provider and its billing/subscription/credits stack
(~8,200 lines)**. That is real functionality removed, not a rename.

Recommendation: **cut it.** A provider that authenticates against the upstream
org's paid portal is not opencodon functionality, and it drags the entire
billing UI with it. 32 providers remain, including every major API and OAuth
route. Alternative if you want it kept: leave B5 out and accept ~368 files
retaining `nous`/`portal` identifiers.

**B6 recommendation: drop from default catalog, don't rewrite the IDs.**

---

## Execution order

Deletions before renames — deleting 25k lines of kanban removes thousands of
`hermes` refs for free, so Part A shrinks a lot if B runs first.

1. **C** — dead weight (zero risk, ~14 MB, no code paths)
2. **B8** — orphans (already dead)
3. **B1, B7, B9** — pets, upstream-named skills, novelty CLI
4. **B3, B4** — moa, honcho
5. **B2** — kanban (largest; deep core plumbing)
6. **B5** — Nous Portal + billing *(pending decision)*
7. **B6** — model IDs out of default catalog
8. **A1** — expire compat shims
9. **A2, A3** — identifier + docstring rename sweep
10. **A4, A5** — docs and assets
11. Final verification: `git grep -ri -E 'hermes|nous' ` should return only
    `LICENSE`, `NOTICE`, `.fork/`, and third-party model IDs if B6 kept them.

Per step: delete → `scripts/run_tests.sh` → one commit. Failures are compared
against the documented pre-existing baseline in `FORK-PLAN.md` (26 local
macOS-specific failures; the suite is green on ubuntu/CI).
