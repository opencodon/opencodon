#!/usr/bin/env python3
"""Grade a science-integration phase against tests/requirements.yaml.

A phase is complete when every requirement it declares is *claimed* by at
least one test and every claiming test passes. Both halves matter: a
requirement nobody tested is reported as loudly as one whose test fails,
because "built it, never checked it" is the failure mode this exists to catch.

Tests claim requirements with a marker:

    @pytest.mark.requirement("SCI-P0-01")
    def test_failing_cell_records_location(...):

The mapping is many-to-many on purpose — one test often demonstrates several
promises, and a promise worth making is usually worth checking more than once.

A requirement whose only tests were *skipped* counts as unproven, not passed.
That matters here: several science tests skip when an optional stack (jupyter,
R+IRkernel) is absent, and a phase must not look green because its evidence
never ran.

Usage:
    scripts/phase_eval.py                          # every phase
    scripts/phase_eval.py --phase P2               # one phase
    scripts/phase_eval.py --phase P2 --integration # include live-network tests

Exit status is 0 only when the selected phases are fully covered and green, so
this works as a CI gate.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTRY = REPO_ROOT / "tests" / "requirements.yaml"

# Loaded into the pytest run via -p. Records which requirement ids each test
# claimed and how that test finished, then writes the map for us to read back.
_PLUGIN = '''
import json
import os
from collections import defaultdict

_outcomes = defaultdict(list)
_claims = {}


def pytest_runtest_protocol(item, nextitem):
    ids = [arg for marker in item.iter_markers("requirement") for arg in marker.args]
    if ids:
        _claims[item.nodeid] = ids
    return None  # advisory only — let the default protocol run


def pytest_runtest_logreport(report):
    ids = _claims.get(report.nodeid)
    if not ids:
        return
    # One record per test: the call phase is the verdict, except when setup
    # skipped or errored and no call phase ever happened.
    interesting = report.when == "call" or (
        report.when == "setup" and report.outcome in ("skipped", "failed")
    )
    if not interesting:
        return
    for rid in ids:
        _outcomes[rid].append({"test": report.nodeid, "outcome": report.outcome})


def pytest_sessionfinish(session, exitstatus):
    path = os.environ.get("PHASE_EVAL_OUT")
    if path:
        with open(path, "w") as handle:
            json.dump(_outcomes, handle)
'''

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"
)


def load_registry() -> dict:
    if not REGISTRY.exists():
        sys.exit(f"requirements registry not found: {REGISTRY}")
    return yaml.safe_load(REGISTRY.read_text())["phases"]


def collect_outcomes(paths: list[str], integration: bool) -> dict:
    """Run pytest and return ``{requirement_id: [{test, outcome}, ...]}``."""
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "phase_eval_plugin.py").write_text(_PLUGIN)
        out = Path(tmp) / "outcomes.json"

        cmd = [sys.executable, "-m", "pytest", *paths, "-q", "-p", "phase_eval_plugin"]
        if integration:
            # pyproject sets `addopts = -m 'not integration'`; a later -m wins.
            cmd += ["-m", ""]

        env = {
            **os.environ,
            "PHASE_EVAL_OUT": str(out),
            "PYTHONPATH": tmp + os.pathsep + os.environ.get("PYTHONPATH", ""),
        }
        proc = subprocess.run(
            cmd, cwd=REPO_ROOT, env=env, capture_output=True, text=True
        )
        if not out.exists():
            sys.stderr.write(proc.stdout[-4000:] + proc.stderr[-2000:])
            sys.exit("pytest produced no outcome map — see output above")
        return json.loads(out.read_text())


def grade(phases: dict, selected: list[str], outcomes: dict) -> bool:
    everything_ok = True

    for name in selected:
        phase = phases[name]
        reqs = phase.get("requirements", [])
        print(f"\n{BOLD}{name} — {phase['title']}{RESET} {DIM}({phase.get('status', '?')}){RESET}")
        print(f"{DIM}  {len(reqs)} requirement(s){RESET}\n")

        tally = {"ok": 0, "failing": 0, "skipped": 0, "untested": 0}
        for req in reqs:
            rid = req["id"]
            runs = outcomes.get(rid, [])
            fails = [r for r in runs if r["outcome"] == "failed"]
            passes = [r for r in runs if r["outcome"] == "passed"]

            if not runs:
                label, colour, key = "UNTESTED", YELLOW, "untested"
            elif fails:
                label, colour, key = "FAILING", RED, "failing"
            elif not passes:
                label, colour, key = "SKIPPED", YELLOW, "skipped"
            else:
                label, colour, key = f"ok ({len(passes)})", GREEN, "ok"

            tally[key] += 1
            if key != "ok":
                everything_ok = False

            print(f"  {colour}{label:>10}{RESET}  {BOLD}{rid}{RESET}  {req['statement']}")
            for run in fails:
                print(f"              {RED}↳ {run['test']}{RESET}")

        print(
            f"\n  {GREEN}{tally['ok']} ok{RESET} · {RED}{tally['failing']} failing{RESET}"
            f" · {YELLOW}{tally['skipped']} skipped{RESET}"
            f" · {YELLOW}{tally['untested']} untested{RESET}"
        )

    return everything_ok


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--phase", action="append", help="phase id (repeatable); default all")
    parser.add_argument(
        "--integration", action="store_true",
        help="also run tests marked integration (live network)",
    )
    parser.add_argument(
        "--paths", nargs="*", default=["tests"], help="pytest paths (default: tests)"
    )
    args = parser.parse_args()

    phases = load_registry()
    selected = args.phase or list(phases)
    if unknown := [p for p in selected if p not in phases]:
        sys.exit(f"unknown phase(s): {', '.join(unknown)}")

    outcomes = collect_outcomes(args.paths, args.integration)
    ok = grade(phases, selected, outcomes)

    print(f"\n{BOLD}{'PHASE(S) COMPLETE' if ok else 'NOT COMPLETE'}{RESET}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
