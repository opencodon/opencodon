# Fork-cleanup verification tools

Two scripts that made the Nous/Hermes removal safe. Both are general-purpose;
keep them for the remaining Part A rename work.

## `dangling.py` — catch removals that lint cannot

```bash
python .fork/tools/dangling.py $(git diff --name-only HEAD | grep '\.py$')
```

Reports `Load`-context names with no binding in the file's own scopes. This is
the only cheap check that catches a name referenced **only inside a function
body** — neither `ruff` nor `import <module>` evaluates those, so a removed
helper produces a `NameError` that first fires in production.

It found two real breakages during B5-ii that everything else missed:
`_load_auth_store` calling a removed `_migrate_stale_nous_portal_url` (a
`NameError` on *every* auth-store read), and a block cut in `debug.py` that
overran and swallowed the `DebugShareResult` dataclass.

**Run it unfiltered.** An earlier version filtered to names containing "nous"
and therefore missed both `DebugShareResult` and two xAI/Codex helpers that had
been sitting inside the Nous section of `auth.py`.

## `droptest.py` — AST-safe test removal

```bash
python .fork/tools/droptest.py tests/path/test_x.py TestClass test_name ...
```

Deletes whole test functions/classes by AST span (`end_lineno`), drops spans
nested inside other selected spans, and re-parses before writing so it refuses
to leave a broken file. Line- or regex-based deletion silently corrupts
multi-line `def foo(\n args\n):` signatures and eats neighbouring tests.

## Running the suite

Use `scripts/run_tests_parallel.py -q` — **not** `pytest tests/`. The suite
relies on per-file subprocess isolation; a single pytest process reports
hundreds of failures that are pure cross-file state pollution.
