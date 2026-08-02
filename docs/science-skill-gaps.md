# Science skills: unsupported host capabilities

Some skills under `skills/science/` were imported from Claude Science, which
exposes a richer in-kernel host SDK than opencodon does. Where a skill's entry
point depends on a capability opencodon has not ported, that entry point raises
`NotImplementedError` naming the gap rather than failing with a `TypeError` or
`AttributeError` several frames deep.

This file is the register of those gaps. It exists so the failure is a known,
stated limitation instead of a bug report.

## What opencodon's host SDK provides

Injected into the kernel by `src/opencodon/science/bridge.py`, served by
`src/opencodon/science/host_bridge.py`:

| Call | Shape |
|---|---|
| `host.llm(prompt, model?, system?, max_tokens?)` | one string prompt → text |
| `host.llm_batch(prompts, model?, system?, max_tokens?, max_concurrency?)` | list of **string** prompts, one shared model → `[{ok, text}]` |
| `host.tool(name, args)` | one allowlisted opencodon tool |
| `host.models()` | `{default, cheap, reasoning}` |
| `host.cheap_model()` / `host.reasoning_model()` | one resolved slug |
| `load_artifact(version_id)` / `save_artifact(...)` | module-level, not on `host` |

## What the imported skills additionally expect

The donor's `llm()` is polymorphic and multimodal: it accepts a **list of
per-item request dicts** (each with its own `prompt`, `model`, `max_tokens`
and `images`), takes `tools` / `tool_choice` for structured output, and returns
**dicts** carrying `tool_use` blocks rather than plain text.

Closing this means implementing three things in `src/opencodon/science/host_bridge.py`:

1. **Heterogeneous batch** — a batch where each item carries its own model and
   token budget, rather than one model shared across the list.
2. **Tool-calling passthrough** — forward `tools` / `tool_choice` to the
   OpenAI-compatible client and return `tool_use` blocks, plus a
   `host_call_log` shape that records them.
3. **Vision** — accept per-item image paths and encode them as content blocks.
   Note this one carries a real design question: image paths are *kernel-side*
   paths, read host-side. On a remote kernel they only resolve after the
   provisioner's `sync_out`, so the ordering has to be settled deliberately.

## Affected entry points

| Skill | Entry point | Needs |
|---|---|---|
| `pdf-explore` | `pdf_map` | heterogeneous batch |
| `pdf-explore` | `pdf_outline` | heterogeneous batch |
| `pdf-explore` | `pdf_scan` | heterogeneous batch |
| `pdf-explore` | `pdf_extract` | heterogeneous batch, tool-calling |
| `figure-composer` | `derive_outline` | tool-calling, vision |
| `paper-narrative` | `derive_paper_brief` | tool-calling |

Everything else in these skills works: `pdf-explore`'s parsing and page-cache
layer (`pdf_pages`, `pdf_resolve`, `pdf_text_cap`, `pdf_guard_text`), and both
figure skills' non-LLM helpers.

## Degrades rather than raises

`literature-review`'s `litrev_contact()` calls `host.get_user_email()`, which
opencodon does not implement. It already catches the failure and returns
`None`, so the effect is that Crossref and doi.org requests go out without a
contact address — losing the polite-pool rate ceiling, not correctness. It is
left as-is deliberately; `OPENCODON_SCHOLARLY_MAILTO` covers the same ground
for the `literature` toolset.
