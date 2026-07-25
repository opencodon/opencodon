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

### B5 — REVISED SCOPE (measured 2026-07-25, attempt abandoned mid-cut)

The estimate below (~8,200 lines, 16 files) was **wrong by roughly 3×**. A full
attempt was made, reverted, and saved as a patch. What it actually found:

**True footprint: ~24,700 lines across 81 files.** Beyond the files listed
below, the cut also reaches:

| Surface | Detail |
|---|---|
| `opencodon_cli/auth.py` | **409 refs.** Nous is one entry in a generic `PROVIDERS`/`ProviderConfig` registry, but it owns a dedicated 1,260-line section (device-code flow, token refresh, model discovery, invoke-JWT minting, billing-scope step-up) plus ~8 special-case branches in the status and login/logout paths. It is also the **default Quick Setup path**, so removing it changes first-run onboarding. |
| The **Nous Tool Gateway** | An entire subsystem not in the original plan: `tools/managed_tool_gateway.py` + `tools/environments/managed_modal.py`, a Nous-hosted passthrough that bills vendor tools (Firecrawl, Krea, FAL, Browser Use, Modal, OpenAI audio) to a Portal subscription. Reaches 16 source files. Every consumer does have a BYO-key fallback, so it removes cleanly — but it is a second functional cut, not part of "billing UI". |
| `plugins/dashboard_auth/nous/` | Dashboard OAuth provider against `portal.nousresearch.com`. |
| `opencodon_cli/proxy/adapters/nous_portal.py` | Proxy upstream adapter. |
| `opencodon_cli/portal_cli.py` | The `hermes portal` onboarding command. |
| Desktop + web | `apps/desktop/src/app/settings/billing/*`, `store/billing-block.ts`, `apps/shared/src/billing-types.ts`, `web/src/pages/SystemPage.tsx`, plus `nous-girl.jpg` in two apps. |
| Tests | 35 dedicated files (~24k lines) plus ~30 more with incidental refs. |

**Two files were nearly destroyed by mistake — do not delete them:**

- `agent/account_usage.py` is **multi-provider**. `fetch_account_usage` dispatches
  to Codex, Anthropic, and OpenRouter and never to Nous; only
  `build_nous_credits_snapshot` / `nous_credits_lines` /
  `_snapshot_from_credits_state` / `CreditsView` / `build_credits_view` are
  Nous-specific. **Trim, don't delete** — it backs `/usage` for every provider.
- `agent/billing_links.py` is explicitly *"provider-agnostic"*: a 14-provider
  billing-URL table (OpenAI, Anthropic, OpenRouter, xAI, DeepSeek, Groq,
  Mistral, Together, Fireworks, Perplexity, Google, Cohere, Moonshot, NVIDIA)
  with one Nous branch. **Trim the `is_nous` routing bit, keep the table.**

Correctly Nous-only, safe to delete: `nous_rate_guard.py`, `portal_tags.py`,
`billing_view.py`, `billing_usage.py`, `subscription_view.py`,
`credits_tracker.py` (parses `x-nous-credits-*` headers), and all five
`opencodon_cli/nous_*.py` + `cli_billing_mixin.py`.

**Correction (same day): the three-way split above does not exist.** Verified
by reading module-level imports:

```
tools/managed_tool_gateway.py  -> tools.tool_backend_helpers
opencodon_cli/nous_subscription.py -> nous_account, managed_tool_gateway, tool_backend_helpers
opencodon_cli/nous_account.py  -> (leaf)
```

All module-level. So deleting the gateway forces deleting
`nous_subscription.py`, which forces deleting `nous_account.py` — the Portal
account layer that was supposed to be a *later* commit. And
`nous_subscription.py` is itself a hub: **30+ call sites in
`opencodon_cli/tools_config.py`** (the `hermes tools` UI) plus `web_server.py`,
`setup.py`, `status.py`, `model_setup_flows.py`, `portal_cli.py`, and
`agent/prompt_builder.py`. There is no independently-shippable "tool gateway"
commit.

`auth.py` **is** a real seam: it holds only two *lazy*, function-local imports
of `nous_account` (both for entitlement-error copy), so everything else depends
on auth rather than the reverse.

**Actual split — two commits:**

1. **B5-i — the managed-tools + billing + subscription layer** (~20k lines, one
   connected component): `managed_tool_gateway.py`, `environments/managed_modal.py`,
   `nous_subscription.py`, `nous_account.py`, `credits_tracker.py`,
   `portal_tags.py`, `nous_rate_guard.py`, `billing_view.py`, `billing_usage.py`,
   `subscription_view.py`, `cli_billing_mixin.py`, `portal_cli.py`, the TUI's
   Phase-2b RPC block, the desktop billing screens, the managed branches in
   `tools_config`/`setup`/`status`/`web_server`/`model_setup_flows`/`prompt_builder`,
   the ten tool-consumer BYO-key fallbacks, the trims to `account_usage.py` and
   `billing_links.py`, and the two lazy `auth.py` entitlement branches.
   Intermediate state is coherent: you can still authenticate to Nous as an
   inference provider, but there is no billing UI, no credits display, and no
   managed tools.
2. **B5-ii — the auth provider**: `auth.py`'s Nous `ProviderConfig` entry and its
   1,260-line section, `plugins/dashboard_auth/nous/`,
   `proxy/adapters/nous_portal.py`, and a replacement for Quick Setup's default
   option.

The only genuinely isolated sub-slice is the managed-Modal *terminal backend*
(`environments/managed_modal.py`, imported solely by `terminal_tool.py`, ~500
lines with the mode plumbing) — small enough that it is not worth its own
commit, and incoherent alone unless `resolve_modal_backend_state`'s "managed"
mode goes with it.

### B5-i progress (2026-07-25, second attempt)

**Source is complete and verified clean** — `ruff` passes, every module
imports, and no source file references a removed module. Saved as
`.fork/b5i-source-complete.patch` (116 files, −27,802 lines).

Done in that patch: the tool gateway and all ten leaf BYO-key fallbacks
(firecrawl, krea, fal, browser-use, modal, openai-audio, tts, transcription,
web_tools, terminal_tool); `nous_subscription` + `nous_account`; the five
Portal-only view modules; `credits_tracker` and the whole `_capture_credits`
/ notice path in `run_agent`; `portal_tags`; `nous_rate_guard` and its three
call sites in `conversation_loop`; `cli_billing_mixin` + `/topup` +
`/subscription`; the TUI's 22k-char Phase-2b RPC block; `portal_cli`; the
proxy adapter; `dashboard_auth/nous`; the `nous-girl.jpg` assets; the four
"Nous Subscription" rows in the `hermes tools` picker and every branch that
gated on them; `setup.py`'s availability summary rewritten onto direct
probes; `auth.py`'s two lazy account imports and its orphaned
billing-scope helpers.

Two files trimmed rather than deleted, per the warning above:
`agent/account_usage.py` and `agent/billing_links.py`. Two generic helpers
(`_has_agent_browser`, `_local_browser_runnable`) were relocated from
`nous_subscription` into `tools_config` rather than lost.

**Remaining: 34 test files / 109 failures.** Cause histogram:

| Count | Cause |
|---|---|
| 55 | test modules importing a deleted module (`nous_account`, `nous_billing`, `nous_subscription`, `portal_tags`, `credits_tracker`, `billing_view`, `proxy.adapters.nous_portal`) |
| ~12 | provider-parity suites enumerating the deleted `nous` provider profile (`build_extra_body`, `build_api_kwargs_extras`, `register`) |
| ~8 | tests importing removed functions (`is_nous_inference_route`, `build_nous_subscription_prompt`, `nous_credits_lines`) |
| ~7 | expected-output assertions (`extra_body` tags, `/usage` credits lines, console-engine command list) |

Lesson recorded: a bulk regex sweep over test files must be **single-line
only**. A multi-line `monkeypatch.setattr(...)` pattern silently ate 66 lines
of unrelated tests in `test_web_tools_config.py` and 115 in `test_setup.py`;
caught by comparing per-file deletion counts, then reverted and redone with a
strictly single-line pattern.

Patch from the abandoned attempt (81 files, reverted so the tree stays green):
`scratchpad/b5-partial.patch`. Reverted rather than pushed to completion
because the tree did not import mid-cut and the remaining work was
concentrated in `auth.py`, where a scope error is worse than anywhere else.

### B5-ii progress (2026-07-25) — the Nous auth provider

**Source is complete and verified clean.** `ruff check .` passes, every key
module imports, and a whole-tree AST name-resolution scan finds no dangling
`nous`-named references. `agent/`, `opencodon_cli/`, `gateway/`, `tools/`,
`plugins/`, `cron/`, `cli.py`, `run_agent.py` are all at zero non-URL Nous
references.

Removed in B5-ii:

- `opencodon_cli/auth.py`: the whole `# Nous Portal` section (54k chars), the
  `"nous"` `ProviderConfig` registry entry, 21 nous-named functions, the
  Portal constants, the entitlement branches in `format_auth_error`, the
  legacy `"systems"` auth-store migration, four host allowlists, and the
  sale-chrome rendering in the model picker. 8,178 → ~5,900 lines, 0 refs.
- `agent/auxiliary_client.py`: 9 nous-named functions, the `nous` rung of the
  provider chain, both stale-model self-heal blocks, both auth-refresh-parity
  blocks, the vision strict-backend branch, and `auxiliary_is_nous`.
- `agent/credential_pool.py`: `_sync_nous_entry_from_auth_store`, the seed
  branch, the refresh dispatch + failure-recovery blocks, the
  `runtime_api_key`/`runtime_base_url` agent-key paths.
- `opencodon_cli/models.py`: the entire 468-line Nous Portal block
  (tier detection, Portal recommendations, the recommended-models cache,
  `compute_sale_discount`, `_resolve_nous_pricing_credentials`) plus the
  curated `_PROVIDER_MODELS["nous"]` list — **this also completes B6.**
- `opencodon_cli/model_catalog.py`: `get_curated_nous_models`; the catalog
  manifest URL now points at this repo's raw GitHub path instead of the Nous
  docs host (the old fallback promoted to primary).
- `opencodon_cli/model_switch.py`: the Nous-Hermes-3/4 "not agentic" warning
  guard (`is_nous_hermes_non_agentic`) — the name collision it existed to
  catch does not exist post-fork — plus the picker branch and its callers in
  `cli.py` and `agent/agent_init.py`.
- `opencodon_cli/model_setup_flows.py`: `_model_flow_nous`; `setup.py`'s
  `_run_portal_one_shot` and `--portal`; quick setup rewired onto the generic
  `setup_model_provider`.
- `opencodon_cli/web_server.py`: `/api/portal`, the nous OAuth catalog entry +
  status dispatcher + device-code start + `_nous_poller`, the
  `nous_session_valid` health field, and the nous branch of
  `/api/model/recommended-default`.
- `opencodon_cli/debug.py` + `diagnostics_upload.py`: `hermes debug share
  --nous` (upload to Nous-internal S3 via NAS) — module deleted.
- `gateway/slash_commands.py`: `/topup` and the `credits_lines` plumbing.
- `gateway/relay/__init__.py`: mode 2 (Nous Portal) of the identity-token
  resolver. Generic OIDC client-credentials (mode 1) survives as the only
  mode; a missing `gateway.idp.token_url` is now an explicit error.
- `agent/model_metadata.py`: `_resolve_nous_context_length` and the
  Portal-authoritative cache-bypass branch.
- Dead flags/registry entries: `--portal-url` / `--inference-url` /
  `--client-id` / `--scope` on `model`/`login`/`auth add`, the `nous`
  `HermesOverlay` + label, the `nous` proxy default, `NOUS_BASE_URL`, the
  orphaned `cron.chronos` config block, and `credits_notices`.

Also removed in B5-ii, discovered while chasing dangling references:

- **The `/api/cron/fire` webhook** — the *inbound* half of Chronos managed
  cron, on both the dashboard (`web_server.py`) and the api_server adapter.
  With the Chronos plugin gone (B5-i) both handlers raised `ImportError` on
  `plugins.cron_providers.chronos.verify`, and the `cron.chronos.*` config keys
  they read had already been removed. Also dropped the `PUBLIC_API_PATHS`
  entry and the four Chronos test modules.
- **`catalog/model-catalog.json` regenerated.** `scripts/build_model_catalog.py`
  still emitted a `nous` provider block from the deleted
  `_PROVIDER_MODELS["nous"]`. Generator trimmed, manifest regenerated
  (openrouter only, 39 models, zero Nous references), and the drift-guard test
  updated. The generator's published-URL docstring now points at raw GitHub.
- **`_fetch_manifest_with_fallback` default made call-time.** `fallback_urls`
  defaulted to `DEFAULT_CATALOG_FALLBACK_URLS` as a *definition-time* bound
  default, so the constant could not be overridden or patched. Now `None` ->
  resolved at call time, with the constant as the single source of truth.

**Test surface** — 22 test modules touched, zero Nous references left in
`tests/`. Whole-module deletions: `test_sale_pricing.py`,
`test_quarantine_forensic_logging.py`, `test_diagnostics_upload.py`,
`test_nous_hermes_non_agentic.py`, `test_chronos_verify.py`,
`test_cron_fire_webhook.py`, `test_cron_fire_dashboard.py`,
`test_cron_dashboard_off_loop.py`, `appChromeStatusRuleDevCredits.test.tsx`.
Everything else was trimmed test-by-test.

Three test-repair patterns worth reusing:

1. **Retarget, don't delete, when the behaviour still exists.** The
   unhealthy-provider-cache tests used `nous` as their healthy fallback rung;
   they now use `local/custom` (and `api-key` where `local/custom` is the one
   marked unhealthy), so the coverage survives the provider's removal.
   Same for the keyword-only-signature test, now pinned on the whole tail of
   the signature rather than one removed parameter.
2. **Delete when the behaviour is gone.** `test_reasoning_sent_for_nous_route`
   covered a `nousresearch.com`-host bypass in
   `_supports_reasoning_extra_body`. Retargeting it to OpenRouter made it
   assert something false, so it was dropped instead.
3. **Removing a `patch()` from a backslash-continued `with` chain needs
   continuation-aware editing**, not line deletion — dropping the last item
   leaves a dangling `\`. Where the guard was merely defensive (patching a
   now-absent `_read_nous_auth` to "no Nous auth present"), replacing the
   expression with an inert `patch.dict("os.environ", {})` preserves the chain
   shape with zero risk.

Two bugs this pass caught that `ruff` could not:

1. `_load_auth_store` still called `_migrate_stale_nous_portal_url`, which had
   been removed — a `NameError` on *every* auth-store read. Found by an AST
   name-resolution scan over the whole tree, not by lint or import checks.
2. `setup.py`'s TTS section still read `selected_via_nous` after its
   assignment was removed.

Both are the same class of failure: a name referenced only inside a function
body, which neither `ruff` nor `import <module>` evaluates. The AST scan is
now the standard check after any function removal.

### Telegram managed-bot onboarding — CUT (2026-07-25)

The QR "automatic" bot-creation flow talked only to a Nous-hosted Cloudflare
Worker (`setup.hermes-agent.nousresearch.com`), so it could never work for a
fork. Removed end to end:

- `opencodon_cli/telegram_managed_bot.py` and its test module
- the four `/api/messaging/telegram/onboarding/*` dashboard endpoints and
  their ~360-line helper block, plus the two request models
- `setup.py`'s `_setup_telegram_auto{,_result}` and the "[1] Automatic /
  [2] Manual" prompt — `_setup_telegram` now goes straight to the token prompt
- the same auto/manual branch in `gateway.py`'s platform wizard, including
  the `auto_owner_user_id` allowlist pre-fill
- the web SPA's `TelegramOnboardingPanel` (~400 lines), its API client
  methods, and its three response types

**Telegram itself is untouched** — the platform adapter, allowlist, and home
channel all work exactly as before. Only the bot-*creation* shortcut is gone;
users create a bot via @BotFather and paste the token, which was already the
fallback path. One user-visible improvement falls out of it: Telegram now
shows the standard "Configure" button on the Channels page like every other
platform, instead of being special-cased to hide it behind the QR panel.

### How to run the tests (learned the hard way, 2026-07-25)

**Use `scripts/run_tests_parallel.py`. Never `pytest tests/` directly.**

The suite is designed for **per-file subprocess isolation** — one fresh
`python -m pytest <file>` per test file (see the script's own docstring and
`.github/workflows/tests.yml`, which drives it via `scripts/run_tests.sh`).
Module-level state leaks across files, so a single monolithic pytest process
produces failures that are pure cross-file pollution.

Concretely, on this tree:

| invocation | result |
|---|---|
| `pytest tests/` (one process) | exits 0 at 49% with no summary — output lost |
| `pytest tests/<dir>` per directory | 266 "failures", nearly all pollution |
| `scripts/run_tests_parallel.py -q` | real result, ~13 min |

Two further lessons from the same session:

- **Compare against a baseline measured the same way, on the same tree.** The
  honest procedure is: collect the failing-file list, `git stash`, re-run
  *those files* with `--files`, unstash, and diff. Without that step, 53
  failing files looked catastrophic; 23 of them fail identically before the
  change (macOS-specific), and the real number was 30.
- **`--files` takes a COLON-separated list**, not shell-globbed arguments.

### Verify a removal with an AST name-resolution scan

`ruff` and `import <module>` both miss a name that is only referenced inside a
function body — exactly what a function removal leaves behind. `scratchpad/
dangling.py` walks each changed file's own bindings and reports unresolved
`Load` names.

Run it **unfiltered**. The first version of this scan filtered hits to names
containing "nous", which is why it caught `_migrate_stale_nous_portal_url` and
`selected_via_nous` but silently missed two worse ones:

- `DebugShareResult` — a block cut in `debug.py` overran and swallowed the
  dataclass. 95 test failures.
- `_is_terminal_xai_oauth_refresh_error` /
  `_is_terminal_codex_oauth_refresh_error` — **xAI and Codex** helpers that
  happened to sit inside the Nous section of `auth.py` and went out with it.

That last one is the real lesson: **a section boundary is not a semantic
boundary.** When cutting a region, diff the removed `def` names against the
cut's stated scope (`git diff | grep '^-def '`) and restore anything whose
name has nothing to do with it. Three helpers in that block were genuinely
generic; a fourth (`_refresh_access_token`, which POSTs
`x-nous-refresh-token` to the portal) correctly stayed removed.

**Deliberately kept** (not Nous-coupled despite the name):
`agent/prompt_builder.py`'s identity line, which states the fork provenance
("a hard fork of Nous Research's hermes-agent"), and the
`anthropic_adapter` sanitizer that scrubs it off the Anthropic OAuth wire.

**Deferred out of B5-ii — both now RESOLVED by the user (2026-07-25):**

- **`@nous-research/ui`: KEEP.** It is a published third-party npm package
  (v0.18.2) imported across ~50 `web/src` files, plus font files referenced by
  `dashboard_auth/login_page.py`. A published package name cannot be renamed
  away. Its name therefore stays in `package.json` and in import statements —
  an accepted, documented exception to "no Nous references", alongside the MIT
  `LICENSE` / `NOTICE` attribution. Do not "fix" these in the Part A sweep.
- **Telegram managed-bot onboarding: CUT** (done — see below). BotFather token
  entry remains the supported path.
- `dashboard_auth/login_page.py` carries Nous Research branding and
  `@nous-research/ui` font files; `banner.py` prints a "Nous Research"
  attribution line. Both are Part A4/A5 (branded assets).
- All remaining `NousResearch/hermes-agent` repo/issue URLs and
  `ghcr.io/nousresearch/hermes-agent` image names are Part A4/A5.

### B5 (original estimate — superseded by the section above). Nous Portal provider + its billing/subscription stack — **CUT**
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
5. ~~**B4** — honcho~~ ✅ 730e3c589 *(B2/B3 dropped from the plan: keep)*
6. **B5** — Nous Portal + billing, split into B5a/B5b/B5c (see revised scope)
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
