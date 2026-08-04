# Changelog

## Unreleased

## 0.2.0 — 2026-08-04

The first release that is opencodon rather than a refocused fork of someone
else's agent. Upstream tracking was abandoned on 2026-08-01; the science layer
became the default rather than an opt-in toolset; the source tree was
restructured into a layered `src/opencodon` package; and the surfaces were
rebuilt around results and provenance instead of a chat log.

This release **removes a substantial amount of upstream functionality**. Read
the Removed section before upgrading.

### Added

- **Science layer, on by default.** Kernels run locally, over SSH, or on Modal
  with GPUs; environments are durable micromamba installs; a cell records where
  it failed and whether it can replay; results carry a `verified` claim.
  Artifacts, lineage and reproduction are first-class.
- **Biodata tools** — genes, variants and chemistry from the public databases,
  plus expression, structures and clinical records.
- **Literature tools** — OpenAlex, Crossref and PubMed behind one client, with
  preprints and open-access full text.
- **Science skills**, imported with attribution: the Apache-2.0 science skill
  set, and the biomodel skills (one validated on CPU).
- **A browser session UI** (`apps/web`) that runs the full session without the
  TUI: result-first frames/artifacts/provenance surfaces, typed artifact
  viewers, `{{artifact:}}` references, cell permalinks, a paged and streamed
  trace, reproduce-with-claim badges, and RO-Crate export.
- **Projects are the unit of work**, not a folder guess — a projects landing
  that replaces the shell, a projects overview surface, and per-project actions
  on the project row.
- **Every session opens in its own tab**: a scrollable tab strip with visible
  close buttons, plus the compute pane and a files pane scoped to artifacts.

### Changed

- **The science layer is on by default.** `run_code`, `load_artifact`,
  `list_artifacts`, `artifact_lineage`, and `reproduce_artifact` moved into
  `_OPENCODON_CORE_TOOLS`, so every platform bundle (CLI, TUI, cron, ACP,
  api-server, and the messaging platforms) ships them without
  `--toolsets science`. They are also in the `coding` posture, which is
  auto-selected in code workspaces and would otherwise switch the science
  layer off inside a repo. The `opencodon-webhook` bundle still excludes
  them — webhook payloads are untrusted input and that bundle stays free of
  execution surface.
- `opencodon[science]` (`jupyter_client`, `ipykernel`) is now part of the
  `[all]` extra the installer uses, so a standard install carries real
  kernels. `run_code` / `reproduce_artifact` keep their `check_fn` gate, but
  it now reads "installed **or** installable": the tools stay in the schema
  on installs that lack the stack and the deps are fetched at kernel-start
  time via `LAZY_DEPS["tool.science"]`, instead of the tools silently
  vanishing.
- **Rebrand.** Bio-lime `#89C219` across every surface, the OPENCODON wordmark,
  and the Hermes/Nous identity dropped from the CLI banner, `--version`, skins,
  personas, the agent identity prompt and the TUI.
- **The source tree is a layered package.** Everything moved under
  `src/opencodon` (config, core, frontends, tools, plugins runtime, and the
  science layer), with import-linter guardrails enforcing the layering. The
  three god-objects — `AIAgent`, `OpencodonCLI`, `SessionDB` — were split into
  concern mixins, and the test tree re-mirrors the source.
- **The UI is a shared package.** `@opencodon/client` holds the session UI and
  is consumed by both the desktop and browser hosts; `ui-tui/` moved to
  `apps/tui/`; the root `web/` SPA was dropped in favour of `apps/web`.
- Shipped locales are English only.

### Removed

- **The kanban multi-agent board is gone.** The `opencodon kanban` command
  tree, the `/kanban` slash command (CLI and gateway), the `kanban` toolset
  and its `kanban_*` model tools, the board SQLite kernel, the dispatcher and
  notifier watchers that ran inside the gateway, the goal-mode worker loop,
  the `plugins/kanban/` dashboard and systemd unit, and the whole
  `tests/stress/` suite (which existed only to battle-test the board kernel)
  have all been removed.
- Follow-on cleanups: the `kanban` and `triage_specifier` /
  `kanban_decomposer` config blocks, the `kanban_task_*` plugin hooks, the
  `OPENCODON_KANBAN_*` environment contract, `project bind-board` and the
  projects `board_slug` binding, and the sidebar's collapsed kanban worktree
  lane (`<repoRoot>::kanban`) on both the Python and client side.
- **The Nous commercial layer**: managed tools, billing and subscriptions, the
  Nous auth provider and its model catalog.
- **The Honcho memory provider.**
- **The OpenClaw migration path.**
- The petdex mascot subsystem, the tips corpus, and the journey CLI renderer.
- Telegram managed-bot onboarding (BotFather setup stays).
- Retired messaging platforms and their migration code, the
  `codex_app_server` runtime, and the upstream release tooling and
  translations. Messaging no longer offers platforms no adapter can connect.
- Weekly upstream triage — the 0.1.0 automation — is retired. The fork evolves
  independently.

### Fixed

- Client: the Capabilities / Artifacts / Provenance rows open their page
  instead of reading as dead clicks; page navigation no longer ejects you from
  your project; lone-pane zones stop growing a permanent tab strip, and the
  main zone stops keeping a one-tab "NEW SESSION" strip.
- Science: the kernel language is validated before the Modal SDK is imported,
  the kernel key is kept off `argv`, only what a cell actually wrote is pulled
  back, and the cheap-model pin resolves where the provider is known.
- CLI: chat stops cleanly when credentials exist but no model is selected.
- Skills: a missing host capability is named instead of dying inside the SDK.
- Approvals: macOS temp cleanups are exempt from the dangerous-command gate.

## 0.1.0 — 2026-07-24

Initial release. **opencodon** is a hard fork of
[opencodon/opencodon](https://github.com/opencodon/opencodon)
(forked at upstream commit `8fc278207b0f`, 2026-07-23) refocused on
scientific computing workflows. Full git history is retained; upstream
security fixes are triaged weekly and cherry-picked.

### Added

- **Science layer** (`science/`, `tools/science_tools.py`): frame
  architecture for structured scientific workflows — the reason this fork
  exists.
- Weekly upstream-triage automation (`scripts/upstream_triage.py`):
  security fixes and dependency pins adopted same week, bug fixes when
  reproduced, features never auto-adopted.

### Kept from upstream

- Agent core (loop, compression, caching, memory) and the tool framework
  (terminal, file, web/search, browser, todo, delegation, MCP client).
- All 33 model providers.
- All three UI surfaces — TUI, web dashboard, desktop app (a result-first
  redesign for science is on the roadmap).
- Messaging: Slack, WhatsApp, Telegram, Discord + gateway core and
  api_server/webhook.
- Skills system, cron/routines, CLI.

### Removed from upstream

- 14 messaging platform adapters (signal, matrix, mattermost, email, sms,
  dingtalk, wecom, weixin, feishu, qqbot, bluebubbles, yuanbao,
  homeassistant, msgraph).
- Novelty plugins (achievements, pets, spotify, google_meet, kanban UI,
  teams_pipeline, video_gen, observability) and external memory-provider
  plugins (the `MemoryProvider` ABC and built-in file memory remain).
- ML training machinery (batch_runner, trajectory_compressor,
  mini_swe_runner, rl/datagen toolsets), non-science optional skills,
  upstream website content.

### Changed

- Rebranded end to end: package and CLI `opencodon`, home directory
  `~/.opencodon` (with one-time migration from `~/.opencodon`), env vars
  `OPENCODON_*`, service `opencodon-gateway`.
- One-release compatibility shims, to be removed in 0.2.0: `opencodon` CLI
  alias, `OPENCODON_*` env fallback, legacy toolset aliases, legacy gateway
  unit/kind names.

### Security

- Adopted every applicable upstream security fix through 2026-07-24:
  DNS-pinned SSRF-safe fetches, preflighted HTTP fetch guards, inbound
  Slack file-URL validation, credential-pool failure attribution /
  quarantine / cooldown fixes, reasoning code-fence escaping
  (`.fork/triage/2026-07-24.md` has the full disposition).
- Dependency pins updated for the 2026-07 advisory batch across Python
  (cryptography, Pillow, starlette, mcp, tornado, and others), npm
  (tar, dompurify, fast-uri, shell-quote, body-parser), and GitHub
  Actions. Known deferral: PyNaCl 1.6.2 (medium, voice-only) is blocked
  by discord.py's `<1.6` cap.

### License

MIT. The LICENSE retains `Copyright (c) 2025 Nous Research` for inherited
code, with the opencodon copyright line added for new work.
