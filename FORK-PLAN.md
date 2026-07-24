# Fork plan — independence from hermes-agent

Status: **decided 2026-07-23**. This is the executable decision record for
cutting ties with upstream `NousResearch/Hermes-Agent` and growing as an
independent open-source project. Companion to `implementation-design.md`
(the science layer, built) and `architecture.md` (the reference model).

## Strategy

- **Hard fork, shared history.** Own name, own repo, own roadmap; no
  upstream merges. Git history is retained so upstream security fixes stay
  one `git cherry-pick` away — cutting ties means we stop following their
  direction, not that we blind ourselves to their fixes.
- **License.** MIT permits everything including commercial use. The one
  obligation: the LICENSE keeps `Copyright (c) 2025 Nous Research` for
  inherited code, with our own copyright line added for new work. If a
  business forms around this, structure new original modules (science/,
  workbench UI) with clean copyright and adopt a CLA/DCO **before** taking
  outside contributions.
- **Weekly upstream triage (must-build, week one).** A scheduled job diffs
  upstream's commit log weekly and produces a report: commits touching
  kept paths, anything security/CVE/pin-related, provider-layer changes.
  Security + dependency pins are cherry-picked same week, always; bug
  fixes in kept files if they reproduce; features considered, never
  auto-adopted. Track cherry-picks/week and hours/week — if hours trend
  up, revisit the keep list.
  Given the keep decisions below (all providers, 4 messaging platforms,
  all UI surfaces), triage volume will be substantial — the automation is
  load-bearing, not optional.

## Keep / cut decisions (2026-07-23)

### KEEP — the spine
| Subsystem | Notes |
|---|---|
| Agent core (`run_agent.py`, `agent/`) | the loop, compression, caching, memory |
| State (`opencodon_state.py`) | includes the science tables |
| Tool framework + core tools | terminal, file, web/search, browser, todo, delegation, MCP client |
| **Science layer** (`science/`, `tools/science_tools.py`) | the product |
| Provider framework **+ all 33 model providers** | decided: keep all — maximum compatibility for adopters; accept the provider-churn triage load |
| Skills system + skills | prune to relevant categories in optional-skills |
| Cron/routines | scheduled science pipelines |
| CLI (`cli.py`, `opencodon_cli/`) | prune novelty subcommands (pets, journey, claw, achievements) |

### KEEP — UI surfaces (all, redesigned)
Decided: keep **TUI + tui_gateway, web dashboard, desktop app** — and
redesign each around the science use case (result-first, artifact-centric,
collapsible code) rather than the chat-generalist layout they ship with.
The redesign is the roadmap item; keeping them means no surface is
rebuilt from scratch.

### KEEP — messaging (curated)
Decided: keep **Slack, WhatsApp, Telegram, Discord** + gateway core +
api_server/webhook. All other platform adapters are cut (signal, matrix,
mattermost, email, sms, dingtalk, wecom, weixin, feishu, qqbot,
bluebubbles, yuanbao, homeassistant, msgraph).

### CUT
| Subsystem | Reason |
|---|---|
| ~14 messaging platform adapters (list above) | each tracks a third-party API we don't serve |
| Novelty/third-party plugins: achievements, pets, spotify, google_meet, kanban, teams_pipeline, video_gen, observability | product identity noise; can return as external plugins |
| External memory-provider plugins (honcho, mem0, supermemory, …) | keep the MemoryProvider ABC + built-in file memory |
| Nous ML machinery: `batch_runner`, `trajectory_compressor`, `mini_swe_runner`, rl/datagen/moa toolsets | training-data tooling, not our product |
| Non-science optional-skills categories (blockchain, creative, health, …) | keep research/mlops/devops |
| Website content, locales, brand assets | replaced by our own |

### Slimming status (2026-07-24)

Done, one commit per subsystem on main: novelty plugins (df9fa9fea),
external memory providers (c68ff92ec), optional-skills prune (e84df6aef),
Nous ML machinery (0a320cb30), platform adapters (148451147),
spotify + video generation (c170f87ec). uv.lock regenerated after each
extras change. image_gen and video_analyze are kept.

Deferred to the rebrand/CLI-pruning phase (not cut yet — each has
first-class plumbing in core files that a plugin delete alone won't
remove cleanly):
- honcho memory provider (~20 refs of core wiring around the
  MemoryProvider ABC)
- kanban (signal-handler + worker plumbing in opencodon_cli)
- moa toolset remnants

Pre-existing test failures catalogued against clean-worktree baselines
(not caused by, and not to be fixed within, the slimming pass):
test_gateway_wsl (2), test_resolve_provider_openrouter_pool (1),
test_signal_handler_kanban_worker (1), test_service_manager (2),
test_file_tools (3), test_approval (1), test_execution_flag_detection (3),
test_bedrock_integration (1), test_anthropic_adapter (3, OAuth
credential-file tests), test_live_system_guard_self_test (4, systemctl
pass-through), plus intermittent flakes in test_base_environment,
test_background_command, test_readiness, test_systemd_notify,
test_api_server. All verified failing at the pre-slim baseline
(fdb34795b) in a clean worktree.

## Execution sequence

1. Merge `science/frame-architecture` into the project's main line.
2. Pick the project name (open). Register repo/org, PyPI name.
3. Slimming pass per the CUT table — delete per subsystem, full test run
   per deletion, one commit each. Verify decoupling of cron delivery and
   async-delegation delivery from removed platforms.
4. Rebrand: package name, CLI entry points, `~/.opencodon` → own home dir
   (one-time migration), config naming, LICENSE dual copyright, NOTICE.
5. CI: tests + lint + `pip-audit`/Dependabot against the inherited
   pinning policy (we own CVE response now).
6. Upstream-watch automation + first weekly triage.
7. First tagged release; docs site.
8. Then the build roadmap: lockfile env identity → verified reproductions;
   result-first redesign of TUI/dashboard/desktop; artifact-aware resume.

### Rebrand status (2026-07-24)

Phase 1 complete on main (website cut 0c1621d99, module rename
29e95c515, distribution f5015b1c3, home/env/services 177ac4624, UI
surfaces 4e5c92117, LICENSE/NOTICE/README 84993d594, fallout 004a976be):
the project is now **opencodon** end to end. One-release compat shims:
HERMES_* env fallback, `hermes` CLI alias, legacy toolset aliases,
legacy gateway unit/kind names, legacy codex managed-block markers.
Deliberately NOT renamed: Nous Hermes LLM model ids, the Nous Portal
OAuth client id + client tag, the Honcho OAuth client id (all identify
us to external services), upstream URLs in the triage path map.
Remaining: folder move, GitHub org/repo (user), CI prune, first
release. Full suite = documented pre-existing baseline exactly.

## Publish status (2026-07-24)

Live at github.com/opencodon/opencodon (private until v0.1.0):
`origin` = git@github.com:opencodon/opencodon.git, `upstream` =
NousResearch/hermes-agent with push URL DISABLED. CI pruned to the
ci.yml orchestrator calling tests / lint / js-tests / uv-lockfile-check
/ osv-scanner, plus Dependabot (github-actions only). Cut: docker
build+lint, desktop E2E + evidence publishing, supply-chain diff
scanner, review-label gates, js-autofix, history/contributor checks,
lockfile-diff, label-rerun, live PR comment + CI timing report (and
their scripts/ci helpers + tests; classify_changes.py kept for the
detect-changes action). Issue/PR templates rebranded. Remaining: tag
v0.1.0 + CHANGELOG, flip public, PyPI.
