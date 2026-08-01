# Changelog

## Unreleased

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
