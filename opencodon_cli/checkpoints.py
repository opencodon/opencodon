"""`opencodon checkpoints` CLI subcommand.

Gives users direct visibility and control over the filesystem checkpoint
store at ``~/.opencodon/checkpoints/``.  Actions:

    opencodon checkpoints               # same as `status`
    opencodon checkpoints status        # total size, project count, breakdown
    opencodon checkpoints list          # per-project checkpoint counts + workdir
    opencodon checkpoints prune [opts]  # force a sweep (ignores the 24h marker)
    opencodon checkpoints clear [-f]    # nuke the entire base (asks first)

Examples::

    opencodon checkpoints
    opencodon checkpoints prune --retention-days 3 --max-size-mb 200
    opencodon checkpoints clear -f

None of these require the agent to be running.  Safe to call any time.
"""

from __future__ import annotations

import argparse
import time
from datetime import datetime
from pathlib import Path
from typing import Any


def _fmt_bytes(n: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(n or 0)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _fmt_ts(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return "—"


def _fmt_age(ts: Any) -> str:
    try:
        age = time.time() - float(ts)
    except (TypeError, ValueError):
        return "—"
    if age < 0:
        return "now"
    if age < 60:
        return f"{int(age)}s ago"
    if age < 3600:
        return f"{int(age / 60)}m ago"
    if age < 86400:
        return f"{int(age / 3600)}h ago"
    return f"{int(age / 86400)}d ago"


def cmd_status(args: argparse.Namespace) -> int:
    from tools.checkpoint_manager import store_status

    info = store_status()
    base = info["base"]
    print(f"Checkpoint base: {base}")
    print(f"Total size:      {_fmt_bytes(info['total_size_bytes'])}")
    print(f"  store/         {_fmt_bytes(info['store_size_bytes'])}")
    print(f"Projects:        {info['project_count']}")

    projects = sorted(
        info["projects"],
        key=lambda p: (p.get("last_touch") or 0),
        reverse=True,
    )
    if projects:
        print()
        print(f"  {'WORKDIR':<60}  {'COMMITS':>7}  {'LAST TOUCH':>12}  STATE")
        for p in projects[: args.limit if hasattr(args, "limit") and args.limit else 20]:
            wd = p.get("workdir") or "(unknown)"
            if len(wd) > 60:
                wd = "…" + wd[-59:]
            exists = p.get("exists")
            state = "live" if exists else "orphan"
            commits = p.get("commits", 0)
            last = _fmt_age(p.get("last_touch"))
            print(f"  {wd:<60}  {commits:>7}  {last:>12}  {state}")

    return 0


def cmd_list(args: argparse.Namespace) -> int:
    # `list` is just a terser status — already covered.
    return cmd_status(args)


def cmd_prune(args: argparse.Namespace) -> int:
    from tools.checkpoint_manager import prune_checkpoints

    retention_days = args.retention_days
    max_size_mb = args.max_size_mb

    print("Pruning checkpoint store…")
    print(f"  retention_days:    {retention_days}")
    print(f"  delete_orphans:    {not args.keep_orphans}")
    print(f"  max_total_size_mb: {max_size_mb}")
    print()

    result = prune_checkpoints(
        retention_days=retention_days,
        delete_orphans=not args.keep_orphans,
        max_total_size_mb=max_size_mb,
    )
    print(f"Scanned:         {result['scanned']}")
    print(f"Deleted orphan:  {result['deleted_orphan']}")
    print(f"Deleted stale:   {result['deleted_stale']}")
    print(f"Errors:          {result['errors']}")
    print(f"Bytes reclaimed: {_fmt_bytes(result['bytes_freed'])}")
    return 0


def _confirm(prompt: str) -> bool:
    try:
        resp = input(f"{prompt} [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return resp in {"y", "yes"}


def cmd_clear(args: argparse.Namespace) -> int:
    from tools.checkpoint_manager import CHECKPOINT_BASE, clear_all, store_status

    info = store_status()
    if info["total_size_bytes"] == 0 and not Path(CHECKPOINT_BASE).exists():
        print("Nothing to clear — checkpoint base does not exist.")
        return 0

    print(f"This will delete the ENTIRE checkpoint base at {info['base']}")
    print(f"  size:        {_fmt_bytes(info['total_size_bytes'])}")
    print(f"  projects:    {info['project_count']}")
    print()
    print("All /rollback history for every working directory will be lost.")
    if not args.force and not _confirm("Proceed?"):
        print("Aborted.")
        return 1

    result = clear_all()
    if result["deleted"]:
        print(f"Cleared. Reclaimed {_fmt_bytes(result['bytes_freed'])}.")
        return 0
    print("Could not clear checkpoint base (see logs).")
    return 2


def register_cli(parser: argparse.ArgumentParser) -> None:
    """Wire subcommands onto the ``opencodon checkpoints`` parser."""
    parser.set_defaults(func=cmd_status)  # bare `opencodon checkpoints` → status
    subs = parser.add_subparsers(dest="checkpoints_command", metavar="COMMAND")

    p_status = subs.add_parser(
        "status",
        help="Show total size, project count, and per-project breakdown",
    )
    p_status.add_argument("--limit", type=int, default=20,
                          help="Max projects to list (default 20)")
    p_status.set_defaults(func=cmd_status)

    p_list = subs.add_parser(
        "list",
        help="Alias for 'status'",
    )
    p_list.add_argument("--limit", type=int, default=20)
    p_list.set_defaults(func=cmd_list)

    p_prune = subs.add_parser(
        "prune",
        help="Delete orphan/stale checkpoints and GC the store",
    )
    p_prune.add_argument("--retention-days", type=int, default=7,
                         help="Drop projects whose last_touch is older than N days (default 7)")
    p_prune.add_argument("--max-size-mb", type=int, default=500,
                         help="After orphan/stale prune, drop oldest commits "
                              "per project until total size <= this (default 500)")
    p_prune.add_argument("--keep-orphans", action="store_true",
                         help="Skip deleting projects whose workdir no longer exists")
    p_prune.set_defaults(func=cmd_prune)

    p_clear = subs.add_parser(
        "clear",
        help="Delete the entire checkpoint base (all /rollback history)",
    )
    p_clear.add_argument("-f", "--force", action="store_true",
                         help="Skip confirmation prompt")
    p_clear.set_defaults(func=cmd_clear)

