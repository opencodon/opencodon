# Repo Restructure Plan — src/opencodon, strict layering

Date: 2026-08-01. Status: approved direction, not yet started.
Precondition resolved: upstream hermes tracking is abandoned (2026-08-01
decision) — cherry-pick compatibility no longer constrains renames or moves.

## Goals

1. One installable namespace (`src/opencodon/`) instead of 11 loose root
   modules + 10 root packages installed as global names (`tools`, `utils`,
   `cli`, `agent`, ...).
2. Strict one-way layering, enforced in CI:
   `frontends → core/tools → state/config → common`. Kills today's inversion
   (`agent/` imports `run_agent` and `opencodon_cli`; `gateway`/`tui_gateway`
   import `cli.py`; 33 files in `tools/` import `opencodon_cli`).
3. Split god files: `cli.py` (15.8k lines), `opencodon_state.py` (9.8k),
   `run_agent.py` (6.6k).
4. One config loader (today: `load_cli_config` in cli.py, `load_config` in
   opencodon_cli/config.py, raw YAML in gateway).
5. Regroup grab-bags: `agent/` (136 files, flat) and `opencodon_cli/`
   (186 files — CLI + web server + proxy + PTY bridges + skin engine).

Not in scope / keep as-is: tool registry pattern, plugin discovery surfaces,
slash-command registry design, `apps/*` npm workspaces, `skills/`,
`optional-skills/`, repo-root `plugins/` (discovery paths reference it),
runtime behavior (prompt caching untouched — this is file layout only).

## Target layout

```
src/opencodon/
├── common/          # constants, logging, time, paths, utils — leaf layer, imports nothing
├── config/          # ONE loader + DEFAULT_CONFIG + OPTIONAL_ENV_VARS metadata
├── state/           # opencodon_state.py split: db, sessions, search, migrations
├── core/
│   ├── agent.py         # AIAgent shell (from run_agent.py)
│   ├── loop.py          # conversation loop
│   ├── prompt/          # system_prompt, prompt_builder, caching
│   ├── context/         # compression, context_engine, references
│   ├── providers/       # anthropic/bedrock/vertex/gemini/codex adapters (from agent/)
│   ├── memory/          # memory_manager, memory_provider ABC
│   ├── media/           # tts/transcription/image-gen registries + providers
│   ├── credentials/     # credential_pool, secret_sources, redact
│   └── skills/          # skill loading, curator, skill_commands
├── tools/           # unchanged internally; registry.py stays the waist
├── toolsets.py
├── cron/
├── plugins_runtime/ # PluginManager + hook surface (loader only)
└── frontends/
    ├── cli/         # cli.py split into command modules; display, skin engine
    ├── server/      # web_server, proxy, pty_bridge, dashboard auth
    ├── gateway/     # + platforms/
    ├── tui/         # tui_gateway
    ├── acp/         # acp_adapter
    └── mcp/         # mcp_serve.py
```

Repo root keeps: `plugins/`, `skills/`, `optional-skills/`, `apps/`,
`science/`, `tests/`, `tests-js/`, `scripts/`, `docs/`, `docker/`, `nix/`.

Why `src/` + `opencodon/` (two levels, each with one job): `src/` is a
non-importable container that keeps the code off the default `sys.path`, so
every import goes through the *installed* package — packaging bugs surface in
tests, and stale-venv / name-shadowing bugs (both hit historically) die.
`opencodon/` is the namespace that appears in imports. Packages directly
under `src/` would either put generic names back on `sys.path` or make `src`
itself the package name (anti-pattern).

## Testing discipline (applies to every phase — user directive 2026-08-01)

Tests lead the changes; they are the steering mechanism, not the afterthought.

1. **Tests move WITH the code, in the same commit.** A module never moves
   without its test file moving (and its imports updating) alongside it.
2. **Validate before and after.** Before moving a component, run its tests on
   the original layout (`scripts/run_tests.sh <path>`) to prove they pass;
   run the same tests after the move. A test that was already failing gets
   recorded in the phase notes, never silently carried.
3. **Untested components get tests BEFORE they move.** If a component has no
   coverage (or only incidental coverage), write characterization tests
   against its current behavior first, land them, then move the component
   against that pinned behavior. Test the component the right way per repo
   rules: behavior/invariants not snapshots, no change-detector tests, no
   writes to `~/.opencodon/` (temp `OPENCODON_HOME`), timing bounds ≥ 2s.
4. **Every phase PR ends green** via `scripts/run_tests.sh` (never bare
   pytest); `⚠ FLAKY` results are bugs to fix in-phase.
5. **Coverage audit is part of each phase's scoping**: before touching a
   subsystem, list its modules vs `tests/` mirrors; the gap list becomes the
   characterization-test worklist for that phase.

## Phases — one PR each, always green, `git mv` so history follows

### Phase 0 — Guardrails (no file moves) — DONE 2026-08-01
- Snapshot current import graph (grep-based, checked into `docs/plans/`).
- Add `import-linter` to CI with contracts describing the TARGET layering;
  grandfather current violations as a shrinking allowlist.
- Retire upstream-triage automation: launchd job
  `com.opencodon.upstream-triage`, `scripts/upstream_triage.py`,
  `scripts/install_triage_schedule.sh`, `.fork/` (keep FORK-PLAN.md as a
  historical record). Requires user's machine for launchd unload.

### Phase 1 — Skeleton + leaves — DONE 2026-08-01

Executed with one deviation: the package lands at repo-root `opencodon/`
(not `src/opencodon/`) until Phase 3 — a mixed src+flat setuptools layout
can't express two package roots cleanly, so the `src/` flip happens in
Phase 3's single packaging change when every package moves. The root
`opencodon` launcher script moved to `bin/opencodon` to free the name
(nix/devShell.nix updated). Module renames: `opencodon_constants →
opencodon/common/constants.py`, `opencodon_logging → logging_setup.py`,
`opencodon_time → timeutils.py`, `utils → utils.py`, `toolsets →
opencodon/toolsets.py`. Old paths are sys.modules-aliasing shims (single
module object — monkeypatching stays coherent). Tests moved to
`tests/opencodon/` with canonical imports; new characterization tests for
previously-untested utils helpers (safe_json_loads, env_bool,
normalize_proxy_url/env_vars) in test_utils_env_proxy_json.py.
- Create `src/opencodon/`; fix packaging (drop the `py-modules` hack in
  pyproject.toml `[tool.setuptools]`; editable install).
- `git mv` the dependency-free root modules into `common/`:
  `opencodon_constants`, `opencodon_time`, `opencodon_logging`, `utils`,
  `toolsets` (toolsets stays importable as `opencodon.toolsets`).
- Leave one-line re-export shims at the old root paths so nothing else
  changes yet.

### Phase 2 — Break the inversion (highest value, highest risk)

**Phase 2a DONE 2026-08-01 (config layer):** `opencodon_cli/config.py`
(8k lines) alias-moved wholesale to `opencodon/config/__init__.py`, plus its
four leaf deps: `colors → opencodon/common/colors`, `route_identity →
opencodon/common/route_identity`, `default_soul → opencodon/config/
default_soul`, `managed_scope → opencodon/config/managed_scope`.
`save_config_value` extracted from cli.py into opencodon.config (now
call-time home resolution — profile/override-aware, no import-time
snapshot). 166 production files flipped to canonical imports. New
`opencodon-layer-purity` contract; grandfather lists shrunk:
agent→opencodon_cli 90→62, tools→opencodon_cli 71→34, gateway→cli 2→0.
Gotchas hit (recorded for later phases): `__file__`-relative repo-root
anchors break on every move (get_project_root, node-bootstrap path —
pinned by tests/opencodon/test_path_anchors.py); tests that stub
`sys.modules["opencodon_cli.config"]` or patch `cli.save_config_value`
need their targets flipped; `masked_secret_prompt` deferred to
function level in reconcile_config.

**Phase 2b partial DONE 2026-08-01 (leaf batch):** alias-moved
`plugins.py → opencodon/plugins_runtime/` (+ `middleware.py` beside it),
`_subprocess_compat`/`model_normalize → opencodon/common/`,
`timeouts`/`env_loader`/`moa_config → opencodon/config/`. 76 more files
flipped. Scoreboard: agent→opencodon_cli 62→32, tools→opencodon_cli
34→15. Same gotchas recurred and were fixed: `__file__` repo-root anchor
in get_bundled_plugins_dir (now pinned in test_path_anchors), and
`sys.modules["opencodon_cli.plugins"]` stub keys in tests.

**Phase 2 remaining:** auth (5.9k), runtime_provider, models, profiles,
copilot_auth, providers-catalog helpers are core modules misfiled in
opencodon_cli but tangled with `agent.*` — they relocate in Phase 3 when
agent/ becomes opencodon/core/ (moving them into `opencodon` now would
invert config-layer purity). agent→run_agent helper flips also fold into
Phase 3/4.
- Unify the three config loaders into `opencodon/config/`; `save_config_value`
  moves here (kills `gateway → cli.py` and `tui_gateway → cli.py` reaches).
- Move shared display/callback abstractions out of frontends into core.
- Flip the ~70 `agent|tools → opencodon_cli` imports and 14
  `agent → run_agent` imports.
- E2E validation against a temp `OPENCODON_HOME` (CLAUDE.md rule), all three
  loaders' consumers exercised (CLI, `opencodon tools`, gateway).

### Phase 3 — Move the packages
- `git mv` `agent/ → src/opencodon/core/` with the subpackage regrouping.
- `gateway/`, `tui_gateway/ → frontends/tui/`, `acp_adapter/ → frontends/acp/`,
  `mcp_serve.py → frontends/mcp/`.
- `opencodon_cli/` splits: CLI-proper → `frontends/cli/`, web server + proxy +
  PTY bridges + dashboard auth → `frontends/server/`.
- Old top-level names become shim packages for one release, then deleted
  (no external API consumers: no PyPI, installer builds from source).
- Land at a quiet moment: open PRs merged/rebased first (multiple agent
  sessions + worktrees share this repo).

### Phase 4 — Split the god files (incremental, many PRs, after 1–3)
- `cli.py`: command registry already exists → handlers extract to
  `frontends/cli/commands/*.py`; `OpencodonCLI` becomes a thin dispatcher.
- `opencodon_state.py` → `state/` split (db/sessions/search/migrations).
- `run_agent.py` shrinks to the `AIAgent` shell as already-extracted helpers
  in `agent/` move home under `core/`.

### Phase 5 — Tests, CI, docs
- Re-mirror `tests/` to the new tree; update CI change classifier path rules,
  `scripts/run_tests.sh`, Dockerfile, nix, `.github/`.
- Rewrite AGENTS.md "Project Structure" + CLAUDE.md path references.
- Root-clutter sweep: `test_durations.json`, duplicate `.pytest-cache`,
  `cli-config.yaml.example` (81KB), `.web_ui_build.lock`, stray `__pycache__`.

## Risks

- Phase 2 touches live config resolution — the "wrong loader" class of bug.
  Mitigate with temp-OPENCODON_HOME E2E per consumer, not just unit mocks.
- Phase 3 conflicts with any in-flight branch; schedule it, don't drift into it.
- launchd/installer/desktop packaging scripts hardcode paths — grep for old
  module names (`hermes_cli` incident precedent: stale venv silently
  satisfies old imports locally; CI is the truth).
- import-linter contracts are the regression backstop — land them FIRST.
