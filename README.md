# opencodon 🧬

**The open-science AI agent.** opencodon runs research sessions the way a lab notebook should work: every analysis is a *frame* — a first-class record of what was asked, what code ran, what data went in, and what artifacts came out. Results are reproducible by construction and exportable as [RO-Crate](https://www.researchobject.org/ro-crate/) research objects.

> Status: pre-release (`0.1.0.dev0`). APIs, commands, and storage layout may still change before the first tagged release.

## Why opencodon

Most AI agents optimize for chat. Science needs provenance:

- **Frames, not just chats** — each session is a frame with a two-granularity execution trace: every cell the agent runs, and every host call the code makes back into the agent.
- **Artifact lineage** — datasets, figures, and models are content-addressed and versioned, with a dependency DAG linking every artifact to the exact executions and inputs that produced it.
- **Honest reproduction** — `reproduce()` replays a session's code against its recorded inputs and reports `reproduced` / `diverged` / `failed` — it never claims more than it verified.
- **Real kernels** — Python and R execute in Jupyter kernels with a filesystem contract and a token-authenticated host bridge, not string-eval sandboxes.
- **RO-Crate export** — hand a collaborator a standards-compliant research object, not a chat log.

## Inherited strengths

opencodon is a hard fork of [hermes-agent](https://github.com/NousResearch/hermes-agent) by Nous Research, and keeps its best machinery:

- **Any model** — 33 providers (OpenRouter, OpenAI, Anthropic, local endpoints, …); switch with `opencodon model`, no lock-in.
- **Three UI surfaces** — terminal TUI, web dashboard, and desktop app (being redesigned result-first for science work).
- **Lives where you do** — Telegram, Discord, Slack, and WhatsApp via a single gateway process.
- **A learning loop** — agent-curated memory, autonomous skill creation, session search with cross-session recall.
- **Scheduled automations** — built-in cron with delivery to any connected platform.
- **Runs anywhere** — local, Docker, SSH, Modal, Daytona backends; a $5 VPS is plenty.

## Install

```bash
git clone https://github.com/opencodon/opencodon.git
cd opencodon
uv sync --extra science
opencodon
```

On native Windows, run the PowerShell installer instead: [scripts/install.ps1](scripts/install.ps1) (installs uv, Python, Node.js, and a portable Git Bash — no admin required).

`opencodon` stores its state in `~/.opencodon` (`OPENCODON_HOME` to override; `%LOCALAPPDATA%\opencodon` on native Windows). Upgrading from a hermes-agent install? Your `HERMES_*` environment variables are honored for one release, and pointing `OPENCODON_HOME` at your old `~/.hermes` adopts it in place.

## Science quickstart

Enable the science toolset and ask for an analysis — the agent runs code in a real kernel, saves artifacts with lineage, and the session is replayable:

```bash
opencodon --toolsets science
```

Inside a session the agent can `run_code` (Python/R), `save_artifact` / `load_artifact` with automatic versioning, trace lineage, and export the whole session as an RO-Crate.

## License and attribution

MIT. Inherited code is Copyright (c) 2025 Nous Research; fork changes are Copyright (c) 2026 opencodon contributors. See [LICENSE](LICENSE) and [NOTICE](NOTICE). The `hermes-*` model names in provider catalogs refer to Nous Research's LLMs, not this project.

Weekly upstream triage: security and dependency-pin fixes from hermes-agent are reviewed and cherry-picked every week (`scripts/upstream_triage.py`); features are never auto-adopted.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). The repository predates the fork by 17,000+ commits — `git blame` and the issue references in commit messages point at [upstream](https://github.com/NousResearch/hermes-agent) history.
