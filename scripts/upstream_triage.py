#!/usr/bin/env python3
"""Weekly upstream triage for the hard fork (see FORK-PLAN.md).

Fetches the upstream remote, walks every upstream commit since the recorded
baseline, and buckets each one:

  SECURITY  — security keywords or dependency-pin changes; adopt same week.
  PROVIDER  — touches model-provider surfaces we keep; review, likely adopt.
  BUGFIX    — fix-style commit touching files that exist in our tree;
              adopt only if the bug reproduces here.
  FEATURE   — everything else touching kept files; never auto-adopted.
  N/A       — no changed file exists in our tree (cut subsystems); skipped.

Writes a markdown report to .fork/triage/<date>.md and prints a summary.
Run with --update-baseline after acting on the report to advance
.fork/upstream-baseline to the triaged tip.

Usage:
    python3 scripts/upstream_triage.py [--remote upstream] [--branch main]
                                       [--update-baseline] [--dry-run]
"""

from __future__ import annotations

import argparse
import datetime as _dt
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE_FILE = REPO_ROOT / ".fork" / "upstream-baseline"
TRIAGE_DIR = REPO_ROOT / ".fork" / "triage"

_SECURITY_SUBJECT = re.compile(
    r"\b(security|cve-\d|vulnerab|inject|sanitiz|traversal|xss|rce|"
    r"ssrf|csrf|escape|secret|credential|leak|auth bypass)\b",
    re.IGNORECASE,
)
# Pin/lockfile changes count as security-relevant: we inherit the pinning
# policy and own CVE response now (FORK-PLAN "CI" step).
_PIN_PATHS = ("uv.lock", "pyproject.toml")

_PROVIDER_PATHS = (
    "hermes_cli/auth.py",
    "hermes_cli/models.py",
    "hermes_cli/providers.py",
    "hermes_cli/model_normalize.py",
    "agent/model_metadata.py",
    "agent/auxiliary_client.py",
    "plugins/model_providers/",
    "plugins/image_gen/",
)

_BUGFIX_SUBJECT = re.compile(r"^(fix|bugfix|hotfix)\b|\bfix(es|ed)?\b", re.IGNORECASE)


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _tracked_files() -> set[str]:
    return set(_git("ls-files").splitlines())


def classify(subject: str, files: list[str], tracked: set[str]) -> str:
    kept = [
        f
        for f in files
        if f in tracked or any(f.startswith(p) for p in _PROVIDER_PATHS if p.endswith("/"))
    ]
    if not kept:
        return "N/A"
    if _SECURITY_SUBJECT.search(subject) or any(f in _PIN_PATHS for f in kept):
        return "SECURITY"
    if any(
        f == p or (p.endswith("/") and f.startswith(p))
        for f in kept
        for p in _PROVIDER_PATHS
    ):
        return "PROVIDER"
    if _BUGFIX_SUBJECT.search(subject):
        return "BUGFIX"
    return "FEATURE"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--remote", default="upstream")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--update-baseline", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="no fetch, no report file")
    args = ap.parse_args()

    if not BASELINE_FILE.exists():
        print(f"error: {BASELINE_FILE} missing; write the last-triaged upstream SHA to it", file=sys.stderr)
        return 2
    baseline = BASELINE_FILE.read_text().strip()

    if not args.dry_run:
        subprocess.run(
            ["git", "-C", str(REPO_ROOT), "fetch", args.remote, "--quiet"], check=True
        )
    tip = _git("rev-parse", f"{args.remote}/{args.branch}").strip()

    if tip == baseline:
        print(f"up to date: {args.remote}/{args.branch} is at the baseline ({baseline[:12]})")
        return 0

    log = _git(
        "log",
        "--reverse",
        "--format=%H%x00%ad%x00%s",
        "--date=short",
        f"{baseline}..{tip}",
    )
    tracked = _tracked_files()

    buckets: dict[str, list[tuple[str, str, str]]] = {
        k: [] for k in ("SECURITY", "PROVIDER", "BUGFIX", "FEATURE", "N/A")
    }
    for line in filter(None, log.splitlines()):
        sha, date, subject = line.split("\x00", 2)
        files = _git(
            "show", "--format=", "--name-only", "--diff-merges=first-parent", sha
        ).split()
        buckets[classify(subject, files, tracked)].append((sha, date, subject))

    total = sum(len(v) for v in buckets.values())
    today = _dt.date.today().isoformat()
    lines = [
        f"# Upstream triage {today}",
        "",
        f"Range: `{baseline[:12]}..{tip[:12]}` on `{args.remote}/{args.branch}` — {total} commits.",
        "",
        "Rules (FORK-PLAN): SECURITY cherry-picked same week, always. PROVIDER",
        "reviewed and usually adopted. BUGFIX adopted only if reproduced here.",
        "FEATURE never auto-adopted. N/A touches only cut subsystems.",
        "",
    ]
    for name, hint in (
        ("SECURITY", "adopt same week"),
        ("PROVIDER", "review, likely adopt"),
        ("BUGFIX", "adopt if reproduced"),
        ("FEATURE", "never auto-adopt"),
        ("N/A", "cut subsystems, skip"),
    ):
        rows = buckets[name]
        lines.append(f"## {name} ({len(rows)}) — {hint}")
        lines.append("")
        for sha, date, subject in rows:
            lines.append(f"- `{sha[:12]}` {date} {subject}")
        lines.append("")
    report = "\n".join(lines)

    if args.dry_run:
        print(report)
    else:
        TRIAGE_DIR.mkdir(parents=True, exist_ok=True)
        out = TRIAGE_DIR / f"{today}.md"
        out.write_text(report)
        print(f"wrote {out.relative_to(REPO_ROOT)}")

    for name in ("SECURITY", "PROVIDER", "BUGFIX", "FEATURE", "N/A"):
        print(f"  {name:9} {len(buckets[name])}")

    if args.update_baseline:
        BASELINE_FILE.write_text(tip + "\n")
        print(f"baseline advanced to {tip[:12]}")
    elif total:
        print("re-run with --update-baseline after acting on the report")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
