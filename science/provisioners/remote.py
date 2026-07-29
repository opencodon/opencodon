"""Shared machinery for kernels that do not run on this filesystem.

Modal and SSH differ in how bytes get to the far end and how a process is
started there. Everything else they need is the same, and all of it was
learned the hard way against a live backend:

- readiness has to wait for iopub, not just for a shell reply
- ``cell.json`` carries absolute host paths that mean nothing remotely
- empty directories matter, because the staging dir is created empty
- user data must be copied byte-for-byte, never path-rewritten
- pulling the workspace back has to cost what the cell *wrote*, not what the
  workspace holds

Keeping these here means a second backend inherits the fixes rather than
rediscovering them.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Dict, List, Tuple

from science.bridge import CELL_CONFIG_NAME

logger = logging.getLogger(__name__)

# Directories never worth shipping to the far end.
SKIP_DIRS = {"__pycache__", ".git"}

# A file's identity for change detection: size and mtime, kept as the strings
# the remote printed. Comparing the raw text sidesteps float round-tripping —
# the only question being asked is "is this the same as last time".
RemoteStat = Tuple[str, str]

# Listed remotely rather than by walking directories over the RPC boundary.
# One round trip returns the whole tree with the stats needed to tell what the
# cell touched, and os.walk cannot mistake an empty directory for a file — the
# failure the per-directory `ls` walk used to hit on every cell.
STAT_PROBE_SRC = """
import os, sys
root = sys.argv[1]
skip = set(sys.argv[2].split(",")) if len(sys.argv) > 2 else set()
for dirpath, dirnames, filenames in os.walk(root):
    dirnames[:] = [d for d in dirnames if d not in skip]
    for name in filenames:
        path = os.path.join(dirpath, name)
        try:
            st = os.stat(path)
        except OSError:
            continue
        sys.stdout.write(
            "%d\\t%.6f\\t%s\\n" % (st.st_size, st.st_mtime,
                                   os.path.relpath(path, root))
        )
"""


def stat_probe_argv(python: str, root: str) -> List[str]:
    """Argv running :data:`STAT_PROBE_SRC` against *root*."""
    return [python, "-c", STAT_PROBE_SRC, root, ",".join(sorted(SKIP_DIRS))]


def parse_stat_listing(text: str) -> Dict[str, RemoteStat]:
    """``{relative_path: (size, mtime)}`` from the probe's output.

    Unparseable lines are dropped rather than raising: a listing is a cache
    key, and one malformed row should cost a redundant fetch, not the sync.
    """
    listing: Dict[str, RemoteStat] = {}
    for line in (text or "").splitlines():
        # Split from the left exactly twice, so a path containing a tab
        # survives intact in the remainder.
        parts = line.rstrip("\n").split("\t", 2)
        if len(parts) != 3:
            continue
        size, mtime, relative = parts
        if relative:
            listing[relative] = (size, mtime)
    return listing


def await_answering(client, *, timeout: float, target: str) -> None:
    """Confirm the shell *and* iopub channels are actually carrying traffic.

    Waiting only for the ``kernel_info`` shell reply is not enough. iopub is a
    ZMQ PUB/SUB socket, and a subscriber that has connected but not finished
    subscribing silently drops whatever is published in the gap — the classic
    slow-joiner problem, widened by any tunnel. The kernel then looks healthy
    on shell while the first cell's ``idle`` status is published into the void,
    and that cell waits out its entire timeout for a message never delivered.

    So: ping until an iopub message for our own request comes back.
    ``kernel_info`` is idempotent, which is what makes retrying it safe.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        msg_id = client.kernel_info()
        try:
            client.get_shell_msg(timeout=max(1.0, deadline - time.monotonic()))
        except Exception as exc:
            raise TimeoutError(f"{target} did not answer kernel_info: {exc}") from None
        while time.monotonic() < deadline:
            try:
                msg = client.get_iopub_msg(timeout=2.0)
            except Exception:
                break  # nothing yet — ping again
            if msg.get("parent_header", {}).get("msg_id") == msg_id:
                return
    raise TimeoutError(f"{target} delivered no iopub message within {timeout:.0f}s")


def localise(path: Path, workdir: Path, remote_workspace: str) -> bytes:
    """Rewrite host workspace paths inside ``cell.json`` to remote paths.

    ``prepare_cell`` records absolute paths — ``staging_dir`` and each declared
    input's ``path`` — because for a local kernel the host path *is* the
    kernel's path. A remote kernel resolving them writes staged artifacts into
    a directory that does not exist on its side, and the failure surfaces as a
    bewildering "no such file" from inside ``save_artifact`` rather than as
    anything about remoteness.

    Only ``cell.json`` is rewritten. User data is copied byte-for-byte:
    guessing at path-like strings inside a CSV or a pickle would corrupt it.
    """
    raw = path.read_bytes()
    if path.name != CELL_CONFIG_NAME:
        return raw
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw
    return text.replace(str(workdir), remote_workspace).encode("utf-8")


def walk_files(root: Path) -> List[Path]:
    """Every file under *root*, skipping noise."""
    return [
        path
        for path in root.rglob("*")
        if path.is_file() and not any(part in SKIP_DIRS for part in path.parts)
    ]


def walk_dirs(root: Path) -> List[str]:
    """Workspace-relative directories, parents before children.

    Empty ones included on purpose: ``prepare_cell`` creates the per-cell
    staging directory and leaves it empty, so a files-only mirror never creates
    it and ``save_artifact`` then writes into a directory that does not exist.
    """
    directories = [
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_dir() and not any(part in SKIP_DIRS for part in path.parts)
    ]
    return sorted(directories, key=lambda p: p.count("/"))
