#!/usr/bin/env python3
"""
SQLite State Store for opencodon.

Provides persistent session storage with FTS5 full-text search, replacing
the per-session JSONL file approach. Stores session metadata, full message
history, and model configuration for CLI and gateway sessions.

Key design decisions:
- WAL mode for concurrent readers + one writer (gateway multi-platform)
- FTS5 virtual table for fast text search across all session messages
- Compression-triggered session splitting via parent_session_id chains
- Batch runner and RL trajectories are NOT stored here (separate systems)
- Session source tagging ('cli', 'telegram', 'discord', etc.) for filtering
"""

import asyncio
import json
import logging
import os
import random
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path

from opencodon.core.memory_manager import sanitize_context
from opencodon.core.message_sanitization import _sanitize_surrogates
from opencodon_constants import get_opencodon_home
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar

logger = logging.getLogger(__name__)


def _scrub_surrogates(value: Any) -> Any:
    """Replace lone surrogates when *value* is text; pass anything else through.

    sqlite3 encodes bound ``str`` parameters as UTF-8 and raises
    ``UnicodeEncodeError`` on lone surrogates (U+D800..U+DFFF), so a single
    such code point anywhere in a message aborts the whole write. No-op for
    well-formed text.
    """
    return _sanitize_surrogates(value) if isinstance(value, str) else value


def workspace_key(row: Dict[str, Any]) -> Optional[str]:
    """A session's workspace grouping key: its git repo root when known, else
    its cwd.

    Branch is deliberately excluded so checking out a new branch doesn't
    fragment a workspace's session history. Returns None for cwd-less (unbound)
    sessions. Both fields are already recorded on ``sessions`` — this just picks
    the coarser identity for grouping/filtering.
    """
    root = (row.get("git_repo_root") or "").strip()
    if root:
        return root

    cwd = (row.get("cwd") or "").strip()
    return cwd or None


def _delegate_from_json(col: str = "model_config") -> str:
    return f"json_extract(COALESCE({col}, '{{}}'), '$._delegate_from')"


def _cwd_prefix_clause(cwd_prefix: str) -> Tuple[str, List[str]]:
    prefix = cwd_prefix.rstrip("/\\") or cwd_prefix
    return "(s.cwd = ? OR s.cwd LIKE ? OR s.cwd LIKE ?)", [prefix, f"{prefix}/%", f"{prefix}\\%"]


# A child session counts as a /branch (kept visible, never cascade-deleted) if
# it carries the stable marker OR the legacy end_reason heuristic holds.
_BRANCH_CHILD_SQL = (
    "json_extract(COALESCE({a}.model_config, '{{}}'), '$._branched_from') IS NOT NULL"
    " OR EXISTS (SELECT 1 FROM sessions p"
    "            WHERE p.id = {a}.parent_session_id"
    "            AND p.end_reason = 'branched'"
    "            AND {a}.started_at >= p.ended_at)"
)

_COMPRESSION_CHILD_SQL = (
    "EXISTS (SELECT 1 FROM sessions p"
    "        WHERE p.id = {a}.parent_session_id"
    "        AND p.end_reason = 'compression')"
)

# Rows that surface in pickers: roots + branch children (subagent runs and
# compression continuations stay hidden).
_LISTABLE_CHILD_SQL = f"(s.parent_session_id IS NULL OR {_BRANCH_CHILD_SQL.format(a='s')})"


def _ephemeral_child_sql(alias: str = "s") -> str:
    """Subagent runs (cascade-delete targets), not branches or compression tips."""
    branch = _BRANCH_CHILD_SQL.format(a=alias)
    compression = _COMPRESSION_CHILD_SQL.format(a=alias)
    return (
        f"({alias}.parent_session_id IS NOT NULL"
        f" AND NOT ({branch})"
        f" AND NOT ({compression}))"
    )


def _collect_delegate_child_ids(conn, parent_ids: List[str]) -> List[str]:
    """Delegate-subagent ids to cascade-delete with *parent_ids*.

    Only rows carrying the ``_delegate_from`` marker (set at creation, and
    backfilled by the v16 migration) — generic untagged children keep the
    orphan-don't-delete contract. Walks marker chains recursively so an
    orchestrator subagent's own delegate children go too (FK safety).
    """
    df = _delegate_from_json()
    seeds = {sid for sid in parent_ids if sid}
    # Seed the visited set with the parents themselves. A delegation marker
    # chain can loop back onto a parent — a cycle, or a parent that is also
    # another parent's delegate child when several ids are deleted at once —
    # and without this guard that parent would be collected as one of its own
    # descendants and cascade-deleted along with all of its messages. Callers
    # delete the parents separately, so parents must never appear in the
    # returned child set. (#49148)
    found: set[str] = set(seeds)
    frontier = list(seeds)
    while frontier:
        ph = ",".join("?" * len(frontier))
        cursor = conn.execute(
            f"SELECT id FROM sessions WHERE {df} IN ({ph}) "
            f"OR (parent_session_id IN ({ph}) AND {df} IS NOT NULL)",
            frontier + frontier,
        )
        frontier = [row["id"] for row in cursor.fetchall() if row["id"] not in found]
        found.update(frontier)
    # Return only the discovered children — never the parents themselves.
    return [sid for sid in found if sid not in seeds]


def _delete_delegate_children(conn, parent_ids: List[str]) -> List[str]:
    ids = _collect_delegate_child_ids(conn, parent_ids)
    if ids:
        ph = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM messages WHERE session_id IN ({ph})", ids)
        # FK safety: orphan any untagged stragglers pointing at a doomed row.
        conn.execute(
            f"UPDATE sessions SET parent_session_id = NULL "
            f"WHERE parent_session_id IN ({ph})",
            ids,
        )
        conn.execute(f"DELETE FROM sessions WHERE id IN ({ph})", ids)
    return ids

T = TypeVar("T")

DEFAULT_DB_PATH = get_opencodon_home() / "state.db"

SCHEMA_VERSION = 23

# FTS storage-layout version, tracked INDEPENDENTLY of SCHEMA_VERSION in the
# state_meta key ``fts_storage_version``. The main schema version advances
# freely on open (so future migrations always land); the FTS *layout* only
# reaches the current version when a DB is either born fresh or explicitly
# optimized via ``opencodon sessions optimize-storage``. A legacy DB sits at
# layout 0 (marker absent) with a working inline index until the user opts in.
#   1 = v23 external-content layout (content/tool_name/tool_calls,
#       tool-row-excluded trigram)
FTS_STORAGE_VERSION = 1

# Cap on user-controlled FTS5 query input before regex/sanitizer processing.
# Search queries do not need to be arbitrarily large, and bounding them keeps
# sanitizer/runtime behavior predictable under adversarial input.
MAX_FTS5_QUERY_CHARS = 2_048

# ---------------------------------------------------------------------------
# WAL-compatibility fallback
# ---------------------------------------------------------------------------
# SQLite's WAL mode requires shared-memory (mmap) coordination and fcntl
# byte-range locks that don't reliably work on network filesystems (NFS,
# SMB/CIFS, some FUSE mounts, WSL1).  Upstream documents this explicitly:
# https://www.sqlite.org/wal.html#sometimes_queries_return_sqlite_busy_in_wal_mode
#
# On those filesystems ``PRAGMA journal_mode=WAL`` raises
# ``sqlite3.OperationalError: locking protocol`` (SQLITE_PROTOCOL).  If we
# propagate that, every feature backed by state.db breaks silently —
# /resume, /title, /history, /branch, etc.
#
# Instead, fall back to ``journal_mode=DELETE`` (the pre-WAL default) which
# works on NFS.  Concurrency drops — concurrent readers are blocked during
# a write — but the feature works.
_WAL_INCOMPAT_MARKERS = (
    "locking protocol",       # SQLITE_PROTOCOL on NFS/SMB
    "not authorized",         # Some FUSE mounts block WAL pragma outright
)

# Last SessionDB() init error, per-process.  Surfaced in /resume and
# related slash-command error strings so users know WHY the DB is
# unavailable instead of getting a bare "Session database not available."
# Only SessionDB.__init__ writes to this; other stores report their own
# connect failures via their caller's error handling, not via
# /resume-style slash commands.
_last_init_error: Optional[str] = None
_last_init_error_lock = threading.Lock()

# Paths for which we've already logged a WAL-fallback WARNING.  Without
# this, a store that opens a fresh connection per operation would re-log
# the same filesystem-incompat warning every time, filling errors.log.
_wal_fallback_warned_paths: set[str] = set()
_wal_fallback_warned_lock = threading.Lock()

_FTS_TRIGGERS = (
    "messages_fts_insert",
    "messages_fts_delete",
    "messages_fts_update",
    "messages_fts_trigram_insert",
    "messages_fts_trigram_delete",
    "messages_fts_trigram_update",
)


def _set_last_init_error(msg: Optional[str]) -> None:
    """Record (or clear) the most recent state.db init failure.

    Thread-safe via _last_init_error_lock.  Callers pass a message to
    record a failure or None to clear.  SessionDB.__init__ only calls
    this to SET on failure — it deliberately does NOT clear on success,
    because in a multi-threaded caller (e.g. gateway / web_server per-
    request SessionDB() instantiation), a concurrent successful open
    racing past a different thread's failure would erase the cause
    string that thread's /resume handler is about to format.  Explicit
    clears (e.g. test fixtures) are still supported by passing None.
    """
    global _last_init_error
    with _last_init_error_lock:
        _last_init_error = msg


def get_last_init_error() -> Optional[str]:
    """Return the most recent state.db init failure, if any.

    Slash-command handlers (``/resume``, ``/title``, ``/history``, ``/branch``)
    call this to surface the underlying cause in their error messages when
    ``_session_db is None``.  Returns ``None`` if SessionDB initialized
    successfully (or hasn't been attempted).
    """
    return _last_init_error


# Distinctive opening shared by both background-review harness prompts
# (_SKILL_REVIEW_PROMPT and _MEMORY_REVIEW_PROMPT in agent/background_review.py).
# Matched case-sensitively against the leading content of a user/system message.
_REVIEW_HARNESS_PREFIXES = (
    "Review the conversation above and update the skill library",
    "Review the conversation above and consider saving to memory",
)


def _is_background_review_harness_message(msg: Dict[str, Any]) -> bool:
    """True when ``msg`` is a persisted background-review harness prompt.

    These are user/system turns the forked skill/memory review agent wrote into
    a real session in older builds (before the ``_persist_disabled`` isolation
    fix). They instruct the agent to act as the curator under a hard tool
    restriction, so replaying them as live history hijacks the session.
    """
    if not isinstance(msg, dict):
        return False
    if msg.get("role") not in {"user", "system"}:
        return False
    content = msg.get("content")
    if not isinstance(content, str):
        return False
    head = content.lstrip()
    return any(head.startswith(p) for p in _REVIEW_HARNESS_PREFIXES)


def _strip_background_review_harness(
    messages: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Drop background-review harness messages and the curator-mode assistant
    reply that immediately followed each one.

    Walk the list once; when a harness user/system message is found, skip it and
    also skip the next message if it is the assistant turn that answered it.
    Everything else passes through untouched and in order.
    """
    if not messages:
        return messages
    out: List[Dict[str, Any]] = []
    skip_next_assistant = False
    for msg in messages:
        if _is_background_review_harness_message(msg):
            skip_next_assistant = True
            continue
        if skip_next_assistant:
            skip_next_assistant = False
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                # The curator-mode reply to the harness prompt — drop it.
                continue
        out.append(msg)
    return out


def format_session_db_unavailable(prefix: str = "Session database not available") -> str:
    """Format a user-facing 'session DB unavailable' message with cause.

    When ``SessionDB()`` init fails, callers set ``_session_db = None`` and
    several slash commands (/resume, /title, /history, /branch) previously
    responded with a bare ``"Session database not available."`` — no
    indication of WHY.  This helper includes the captured cause (typically
    ``"locking protocol"`` from NFS/SMB) and points users at the known
    culprit so they can fix it themselves.

    Example output:
        Session database not available: locking protocol (state.db may be
        on NFS/SMB — see https://www.sqlite.org/wal.html).
    """
    cause = get_last_init_error()
    if not cause:
        return f"{prefix}."
    hint = ""
    if any(marker in cause.lower() for marker in _WAL_INCOMPAT_MARKERS):
        hint = " (state.db may be on NFS/SMB/FUSE — see https://www.sqlite.org/wal.html)"
    return f"{prefix}: {cause}{hint}."


def _on_disk_journal_mode(conn: sqlite3.Connection) -> Optional[str]:
    """Read the journal mode from the SQLite DB header on disk.

    Returns the mode string (e.g. ``"wal"``, ``"delete"``), or ``None``
    if the value cannot be determined (new DB, or PRAGMA read failed).
    """
    try:
        row = conn.execute("PRAGMA journal_mode").fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    mode = row[0]
    if isinstance(mode, bytes):  # defensive: sqlite3 occasionally returns bytes
        try:
            mode = mode.decode("ascii")
        except UnicodeDecodeError:
            return None
    return str(mode).strip().lower() if mode is not None else None


def _apply_macos_checkpoint_barrier(conn: sqlite3.Connection) -> None:
    """Enable ``PRAGMA checkpoint_fullfsync`` on macOS (no-op elsewhere).

    On Darwin, ``synchronous=FULL`` (the WAL default) issues a plain
    ``fsync()``, which Apple documents does *not* guarantee that data
    has reached stable storage or that writes are not reordered — see
    the ``fsync(2)`` man page.  SQLite's WAL corruption-safety guarantee
    assumes the OS honors the fsync write barrier; macOS does not unless
    the app uses ``F_FULLFSYNC``.

    During a launchd *system* shutdown/reboot the OS page cache is
    dropped (effectively a power-loss event for in-flight pages), so a
    WAL checkpoint whose ``fsync()`` "reported" durable may never have
    hit the platter — corrupting ``state.db`` with a malformed image.
    This is the trigger in issue #30636 ("SIGTERM during launchd
    shutdown under high load"), distinct from a plain in-session kill
    (which the page cache survives and SQLite recovers from).

    ``checkpoint_fullfsync=1`` forces an ``F_FULLFSYNC`` barrier only at
    checkpoint boundaries — where WAL frames land in the main DB — so the
    cost amortizes to roughly +0.1 ms/commit (vs ~+4 ms for the broader
    ``fullfsync=1`` that flushes on every commit's WAL sync).  Guarded by
    ``sys.platform == "darwin"`` because ``F_FULLFSYNC`` is macOS-only;
    on other platforms the PRAGMA is a no-op, so we skip it entirely.

    Best-effort: never raises.
    """
    if sys.platform != "darwin":
        return
    try:
        conn.execute("PRAGMA checkpoint_fullfsync=1")
    except sqlite3.OperationalError:
        pass


def _enforce_macos_synchronous_full(conn: sqlite3.Connection) -> None:
    """Enforce ``PRAGMA synchronous=FULL`` on macOS to prevent btree corruption.

    On Darwin, the default ``synchronous=NORMAL`` only calls ``fsync()``,
    which Apple's fsync(2) man page explicitly states does *not* guarantee
    data-on-platter or write-ordering. During a WAL checkpoint race with
    process termination (e.g., launchd shutdown), this can leave the main
    DB with half-written btree pages → ``btreeInitPage error 11``.

    WAL mode's durability guarantee assumes the OS honors fsync barriers;
    macOS does not unless we explicitly set ``synchronous=FULL``, which issues
    a real ``fsync()`` on every transaction commit.  The ``F_FULLFSYNC``
    barrier at checkpoint boundaries is handled separately by
    :func:`_apply_macos_checkpoint_barrier`.

    This function is called after any successful WAL activation (either
    from ``apply_wal_with_fallback()`` setting a fresh WAL or when probing
    an existing WAL mode). It ensures macOS connections always use FULL
    synchronous mode, even if a prior connection set ``synchronous=NORMAL``.

    Best-effort: never raises.
    """
    if sys.platform != "darwin":
        return
    try:
        conn.execute("PRAGMA synchronous=FULL")
    except sqlite3.OperationalError:
        pass


def apply_wal_with_fallback(
    conn: sqlite3.Connection,
    *,
    db_label: str = "state.db",
) -> str:
    """Set ``journal_mode=WAL`` on ``conn``, falling back to DELETE on failure.

    Returns the journal mode actually set (``"wal"`` or ``"delete"``).

    On WAL-incompatible filesystems (NFS, SMB, some FUSE), SQLite raises
    ``OperationalError("locking protocol")`` when setting WAL.  We fall
    back to DELETE mode — the pre-WAL default, which works on NFS — and
    log one WARNING explaining why.

    The WARNING is deduplicated per ``db_label``: repeated connections
    to the same underlying DB log once per process, not once per call.
    Different db_labels log independently, so each database gets one
    warning on the same NFS mount.

    Shared by :class:`SessionDB` and the other per-profile SQLite stores so
    every database gets identical fallback behavior.

    Never downgrades to DELETE if the on-disk DB header reports WAL — see _on_disk_journal_mode.
    """
    # Read-only probe — no flock, no checkpoint, no WAL/SHM unlink.
    # Skipping the set-pragma prevents WAL-init from unlinking files other connections hold open.
    try:
        current_mode = conn.execute("PRAGMA journal_mode").fetchone()
        if current_mode and current_mode[0] == "wal":
            _apply_macos_checkpoint_barrier(conn)
            _enforce_macos_synchronous_full(conn)
            return "wal"
    except sqlite3.OperationalError:
        pass

    try:
        conn.execute("PRAGMA journal_mode=WAL")
        _apply_macos_checkpoint_barrier(conn)
        _enforce_macos_synchronous_full(conn)
        return "wal"
    except sqlite3.OperationalError as exc:
        msg = str(exc).lower()
        if not any(marker in msg for marker in _WAL_INCOMPAT_MARKERS):
            # Unrelated OperationalError — don't silently swallow.
            raise
        # Don't downgrade if another process already set WAL on disk.
        existing = _on_disk_journal_mode(conn)
        if existing == "wal":
            raise
        _log_wal_fallback_once(db_label, exc)
        conn.execute("PRAGMA journal_mode=DELETE")
        return "delete"


def _log_wal_fallback_once(db_label: str, exc: Exception) -> None:
    """Log a single WARNING per (process, db_label) about WAL fallback.

    Without this dedup, NFS users of a store that opens a fresh connection
    per operation would fill errors.log with hundreds of identical
    warnings per hour.
    """
    with _wal_fallback_warned_lock:
        if db_label in _wal_fallback_warned_paths:
            return
        _wal_fallback_warned_paths.add(db_label)
    logger.warning(
        "%s: WAL journal_mode unsupported on this filesystem (%s) — "
        "falling back to journal_mode=DELETE (slower rollback-journal "
        "mode; reduces concurrency but works on NFS/SMB/FUSE). See "
        "https://www.sqlite.org/wal.html for details. This warning "
        "fires once per process per database.",
        db_label,
        exc,
    )

# ---------------------------------------------------------------------------
# Malformed-schema recovery
# ---------------------------------------------------------------------------
# A distinct, nastier failure class than a malformed FTS *inverted index*:
# the ``sqlite_master`` schema table itself becomes inconsistent — most
# commonly a DUPLICATE object definition, e.g. two ``CREATE VIRTUAL TABLE
# messages_fts`` rows.  SQLite parses the entire schema while preparing the
# FIRST statement on a connection, so on this class *every* statement raises
# before it runs — including ``PRAGMA journal_mode`` (which is why this trips
# in ``apply_wal_with_fallback`` during ``SessionDB.__init__``, long before
# ``_init_schema`` is reached) and even ``PRAGMA integrity_check`` and a plain
# ``DROP TABLE``.  The only operations that still work are
# ``PRAGMA writable_schema=ON`` plus direct ``sqlite_master`` surgery.
#
# Symptom users hit (Desktop/Dashboard show "no sessions" while 200+ JSON
# files sit on disk):
#   sqlite3.DatabaseError: malformed database schema (messages_fts) -
#   table messages_fts already exists
#
# The canonical ``sessions`` / ``messages`` data is intact in these cases —
# only the derived schema is broken — so recovery preserves all transcripts
# and merely rebuilds the FTS layer.
_MALFORMED_SCHEMA_MARKERS = (
    "malformed database schema",
    "database disk image is malformed",
)

# Process-global guard so auto-repair is attempted at most once per DB path
# per process (prevents repair loops and serialises concurrent web_server /
# gateway opens against the same malformed file).
_repair_attempted_paths: set[str] = set()
_repair_attempt_lock = threading.Lock()


def is_malformed_db_error(exc: BaseException) -> bool:
    """True if *exc* is a SQLite 'malformed schema / disk image' error.

    These are the corruption classes where the schema fails to parse, so
    targeted ``sqlite_master`` surgery (not an ordinary FTS rebuild) is the
    only recovery path.
    """
    if not isinstance(exc, sqlite3.DatabaseError):
        return False
    return any(marker in str(exc).lower() for marker in _MALFORMED_SCHEMA_MARKERS)


def _claim_repair_attempt(db_path: Path) -> bool:
    """Claim the one-shot repair attempt for *db_path* in this process.

    Returns True for the first caller, False afterwards. Keeps a malformed
    DB from triggering an unbounded repair/reopen loop and stops concurrent
    callers from racing surgery on the same file.
    """
    key = str(db_path)
    with _repair_attempt_lock:
        if key in _repair_attempted_paths:
            return False
        _repair_attempted_paths.add(key)
        return True


def _backup_db_file(db_path: Path) -> Optional[Path]:
    """Copy a (possibly malformed) DB file to a timestamped backup beside it.

    Raw file copy on purpose: the DB won't open cleanly, so we preserve the
    bytes exactly for forensics / manual restore. WAL and SHM sidecars are
    copied too when present. Returns the backup path, or None on failure.
    """
    import datetime
    import shutil

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = db_path.with_name(f"{db_path.name}.malformed-backup-{stamp}")
    try:
        shutil.copy2(db_path, backup_path)
        for suffix in ("-wal", "-shm"):
            sidecar = db_path.with_name(db_path.name + suffix)
            if sidecar.exists():
                shutil.copy2(sidecar, backup_path.with_name(backup_path.name + suffix))
        return backup_path
    except Exception as exc:  # pragma: no cover - best effort
        logger.warning("Could not back up malformed DB %s: %s", db_path, exc)
        return None


def _db_opens_cleanly(db_path: Path) -> Optional[str]:
    """Probe a DB on a fresh connection. Returns None if healthy, else a reason.

    Runs the same first-statement (``PRAGMA journal_mode``) that trips the
    malformed-schema parse, then ``PRAGMA integrity_check`` and a canonical
    ``sessions`` read, and finally a rolled-back ``messages`` write so that
    FTS5 index corruption — which leaves base-table reads and
    ``integrity_check`` passing while every ``INSERT INTO messages`` fails
    through the FTS triggers — is reported as unhealthy rather than slipping
    past as a false "ok" (#50502).
    """
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        # Best-effort tokenizer load: a DB carrying the messages_fts_cjk
        # index needs the cjk_unicode61 extension before any statement can
        # touch that table — including the trigger-driven write probe below.
        # Without it, this probe sees the DB exactly as a tokenizer-less
        # SessionDB open would (which drops the cjk triggers to keep writes
        # working), so tokenizer absence must never classify as corruption.
        load_fts5_cjk_extension(conn)
        conn.execute("PRAGMA journal_mode").fetchone()
        rows = conn.execute("PRAGMA integrity_check").fetchall()
        problems = [str(r[0]) for r in rows if r and str(r[0]).lower() != "ok"]
        if problems:
            return "; ".join(problems[:3])
        conn.execute("SELECT COUNT(*) FROM sessions").fetchone()

        # FTS5 read probe: run a representative MATCH query against the
        # messages_fts* virtual tables. The FTS *write* probe below catches
        # the corruption class where base tables read fine but writes fail
        # through the triggers (#50502). It does NOT catch partial FTS5
        # index corruption — bad shadow-table segments where reads still
        # parse but MATCH / snippet / rank queries error out with
        # "database disk image is malformed" (a `sqlite3.DatabaseError`,
        # not `OperationalError`). session_search, /resume title resolution,
        # and any feature relying on FTS5 discovery then break silently
        # because the official repair tool's check-only path reports the
        # DB as healthy. #66724.
        # Catch the full sqlite3 exception hierarchy (not just
        # OperationalError) so the malformed-shadow-table class is reported
        # rather than letting it crash the caller.
        for fts_table in ("messages_fts", "messages_fts_trigram", "messages_fts_cjk"):
            try:
                # No-op queries against the actual FTS5 APIs the search
                # tools use. The trigram table is included because it backs
                # the title-resolution path; either corruption mode would
                # break session recall without this probe. MATCH '""' is
                # the empty phrase-token probe — FTS5 rejects MATCH ''
                # outright ("fts5: syntax error"), but a quoted empty
                # phrase parses, scans zero rows, and exercises the same
                # shadow-table read path the search tools use.
                conn.execute(
                    f"SELECT 1 FROM {fts_table} WHERE {fts_table} MATCH '\"\"' LIMIT 1"
                ).fetchone()
            except sqlite3.OperationalError as exc:
                # Use the canonical capability classifier instead of a
                # hand-rolled substring check. On SQLite builds without the
                # fts5 module, the legacy messages_fts table may exist on
                # disk (from a prior build that had FTS5) and MATCH queries
                # against it raise OperationalError("no such module: fts5");
                # the substring check below would misclassify that as
                # corruption and send the DB into the repair path, whose
                # final fallback deletes the messages_fts% schema
                # (opencodon_state.py:645-723). The supported degraded-runtime
                # path (SessionDB._is_fts5_unavailable_error + the
                # regression suite in tests/test_opencodon_state.py:600-632)
                # treats both "no such module: fts5" and
                # "no such tokenizer: trigram" as the capability error.
                if SessionDB._is_fts5_unavailable_error(exc):
                    # Degraded runtime — not the corruption class we probe.
                    continue
                msg = str(exc).lower()
                if "no such table" in msg or "no such column" in msg:
                    # FTS5 not built yet (brand new file mid-init) — not the
                    # corruption class we probe.
                    continue
                return f"fts5 read probe failed on {fts_table}: {exc}"
            except sqlite3.DatabaseError as exc:
                # This is the corruption class #66724 actually wants caught:
                # partial shadow-table damage where MATCH / snippet / rank
                # queries raise DatabaseError("database disk image is malformed")
                # while reads of the FTS5 table itself parse fine.
                return f"fts5 read probe failed on {fts_table}: {exc}"

        # FTS write probe: drive a row through the messages_fts* triggers in a
        # transaction that is always rolled back, so a corrupt FTS index that
        # rejects writes is caught even though reads look healthy. The probe is
        # best-effort — if the messages/sessions tables don't exist yet (brand
        # new file mid-init) the OperationalError is treated as "not yet a
        # populated DB", not corruption.
        probe_session_id = f"_opencodon_fts_health_probe_{time.time_ns()}"
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO sessions (id, source, started_at) VALUES (?, ?, ?)",
                (probe_session_id, "_health_probe", time.time()),
            )
            conn.execute(
                "INSERT INTO messages (session_id, role, content, timestamp) "
                "VALUES (?, ?, ?, ?)",
                (probe_session_id, "user", "_fts_health_probe", time.time()),
            )
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError as exc:
            # Missing tables / FTS disabled — not the corruption class we probe.
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            msg = str(exc).lower()
            if "no such table" in msg or "no such column" in msg:
                return None
            if "no such tokenizer: cjk_unicode61" in msg:
                # This probe process couldn't load the cjk extension while
                # the DB carries the cjk index — capability gap, not
                # corruption. A tokenizer-capable SessionDB serves it fine;
                # a tokenizer-less one self-heals by dropping the triggers.
                return None
            return str(exc)
        return None
    except sqlite3.DatabaseError as exc:
        return str(exc)
    finally:
        conn.close()


def repair_state_db_schema(db_path: Path, *, backup: bool = True) -> Dict[str, Any]:
    """Repair a state.db whose ``sqlite_master`` schema is malformed or whose
    FTS indexes reject writes.

    Handles two corruption classes: the "duplicate object definition" /
    malformed-schema class where even ``PRAGMA`` statements fail, and the FTS
    write-corruption class (#50502) where base tables read fine and
    ``integrity_check`` passes but writes fail through the ``messages_fts*``
    triggers. Tries least-destructive recovery first and escalates:

      1. **Rebuild FTS indexes in place** via the FTS5 ``'rebuild'`` command,
         which rewrites the internal b-tree segments from the canonical
         ``messages`` rows without dropping or recreating anything. Fixes the
         FTS write-corruption class while preserving the schema intact.
      2. **De-duplicate** ``sqlite_master`` (keep the lowest rowid per
         ``type``/``name``). Fixes the canonical "table X already exists"
         case and PRESERVES the existing FTS index intact.
      3. **Drop the FTS schema** (every ``messages_fts*`` object) + ``VACUUM``.
         The next ``SessionDB()`` open rebuilds the FTS indexes from the
         canonical ``messages`` table.

    Canonical ``sessions`` / ``messages`` rows are never modified. A
    timestamped raw backup is taken first unless ``backup=False``.

    Returns a report dict: ``{repaired: bool, strategy: str|None,
    backup_path: str|None, error: str|None}``.
    """
    report: Dict[str, Any] = {
        "repaired": False,
        "strategy": None,
        "backup_path": None,
        "error": None,
    }

    db_path = Path(db_path)
    if not db_path.exists():
        report["error"] = f"{db_path} does not exist"
        return report

    if _db_opens_cleanly(db_path) is None:
        report["repaired"] = True
        report["strategy"] = "already_healthy"
        return report

    if backup:
        bpath = _backup_db_file(db_path)
        report["backup_path"] = str(bpath) if bpath else None

    # ── Strategy 0: rebuild FTS indexes in place (FTS write-corruption) ──
    # The FTS5 'rebuild' command rewrites the internal index from the canonical
    # content table. This is the recommended, least-destructive recovery for a
    # corrupt FTS index that rejects message writes while reads still succeed.
    try:
        conn = sqlite3.connect(str(db_path), isolation_level=None)
        try:
            # The cjk index can only be rebuilt with its tokenizer loaded;
            # best-effort (a tokenizer-less host skips it at the probe below).
            load_fts5_cjk_extension(conn)
            for table_name in (
                "messages_fts", "messages_fts_trigram", "messages_fts_cjk"
            ):
                try:
                    conn.execute(
                        f"INSERT INTO {table_name}({table_name}) VALUES('rebuild')"
                    )
                except sqlite3.OperationalError:
                    # Table absent (FTS disabled / trigram off / cjk not
                    # present or tokenizer unavailable) — skip it.
                    continue
        finally:
            conn.close()
        if _db_opens_cleanly(db_path) is None:
            report["repaired"] = True
            report["strategy"] = "rebuild_fts"
            logger.warning(
                "state.db FTS indexes rebuilt in place (schema preserved): %s",
                db_path,
            )
            return report
    except sqlite3.DatabaseError as exc:
        logger.warning("state.db FTS in-place rebuild pass failed: %s", exc)

    # ── Strategy 0.5: rebuild stale B-tree indexes (#63386) ──
    # PRAGMA integrity_check can report "wrong # of entries in index" when a
    # B-tree index (e.g. idx_sessions_handoff_state) falls out of sync with its
    # base table. REINDEX rewrites the index b-tree from the canonical table
    # rows using the existing index definition, fixing the mismatch without
    # touching data or FTS schema.
    try:
        conn = sqlite3.connect(str(db_path), isolation_level=None)
        try:
            conn.execute("REINDEX")
            conn.commit()
        finally:
            conn.close()
        if _db_opens_cleanly(db_path) is None:
            report["repaired"] = True
            report["strategy"] = "reindex_btree"
            logger.warning(
                "state.db B-tree indexes rebuilt via REINDEX: %s", db_path
            )
            return report
    except sqlite3.DatabaseError as exc:
        logger.warning("state.db REINDEX pass failed: %s", exc)

    # ── Strategy 1: de-duplicate sqlite_master (keeps FTS index) ──
    try:
        conn = sqlite3.connect(str(db_path), isolation_level=None)
        try:
            conn.execute("PRAGMA writable_schema=ON")
            dupes = conn.execute(
                "SELECT type, name, COUNT(*) AS c, MIN(rowid) AS keep "
                "FROM sqlite_master GROUP BY type, name HAVING c > 1"
            ).fetchall()
            for type_, name, _count, keep in dupes:
                conn.execute(
                    "DELETE FROM sqlite_master "
                    "WHERE type IS ? AND name IS ? AND rowid <> ?",
                    (type_, name, keep),
                )
            conn.execute("PRAGMA writable_schema=OFF")
            conn.commit()
        finally:
            conn.close()
        if _db_opens_cleanly(db_path) is None:
            report["repaired"] = True
            report["strategy"] = "dedup_schema"
            logger.warning(
                "state.db schema repaired by de-duplicating sqlite_master "
                "(FTS index preserved): %s", db_path
            )
            return report
    except sqlite3.DatabaseError as exc:
        logger.warning("state.db dedup repair pass failed: %s", exc)

    # ── Strategy 2: drop all FTS schema, VACUUM, rebuild on next open ──
    try:
        conn = sqlite3.connect(str(db_path), isolation_level=None)
        try:
            conn.execute("PRAGMA writable_schema=ON")
            conn.execute("DELETE FROM sqlite_master WHERE name LIKE 'messages_fts%'")
            conn.execute("PRAGMA writable_schema=OFF")
            conn.commit()
            conn.execute("VACUUM")
        finally:
            conn.close()
        reason = _db_opens_cleanly(db_path)
        if reason is None:
            report["repaired"] = True
            report["strategy"] = "drop_fts_rebuild"
            logger.warning(
                "state.db schema repaired by dropping FTS schema; indexes "
                "will rebuild from messages on next open: %s", db_path
            )
            return report
        report["error"] = reason
    except sqlite3.DatabaseError as exc:
        report["error"] = str(exc)

    if not report["repaired"]:
        logger.error(
            "state.db schema repair could not recover %s automatically "
            "(backup: %s); manual restore from backup may be required.",
            db_path, report["backup_path"],
        )
    return report


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    user_id TEXT,
    session_key TEXT,
    chat_id TEXT,
    chat_type TEXT,
    thread_id TEXT,
    display_name TEXT,
    origin_json TEXT,
    expiry_finalized INTEGER DEFAULT 0,
    model TEXT,
    model_config TEXT,
    system_prompt TEXT,
    parent_session_id TEXT,
    root_session_id TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    end_reason TEXT,
    message_count INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    reasoning_tokens INTEGER DEFAULT 0,
    cwd TEXT,
    git_branch TEXT,
    git_repo_root TEXT,
    -- The project this session BELONGS to (a projects.db row id), as opposed to
    -- ``cwd``, which is where it RUNS. Membership used to be re-derived on every
    -- read by longest-prefix matching cwd against project_folders, so renaming
    -- or moving a folder silently emptied a project, nested projects were
    -- ambiguous, and a linked worktree outside the repo root needed special
    -- casing. Recording it at creation makes membership a fact instead of an
    -- inference. NULL means "no project" — legacy rows and genuinely detached
    -- sessions alike, both of which still fall back to the cwd derivation.
    project_id TEXT,
    billing_provider TEXT,
    billing_base_url TEXT,
    billing_mode TEXT,
    estimated_cost_usd REAL,
    actual_cost_usd REAL,
    cost_status TEXT,
    cost_source TEXT,
    pricing_version TEXT,
    title TEXT,
    api_call_count INTEGER DEFAULT 0,
    handoff_state TEXT,
    handoff_platform TEXT,
    handoff_error TEXT,
    compression_failure_cooldown_until REAL,
    compression_failure_error TEXT,
    compression_fallback_streak INTEGER NOT NULL DEFAULT 0,
    profile_name TEXT,
    rewind_count INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (parent_session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,
    content TEXT,
    tool_call_id TEXT,
    tool_calls TEXT,
    tool_name TEXT,
    effect_disposition TEXT,
    timestamp REAL NOT NULL,
    token_count INTEGER,
    finish_reason TEXT,
    reasoning TEXT,
    reasoning_content TEXT,
    reasoning_details TEXT,
    codex_reasoning_items TEXT,
    codex_message_items TEXT,
    platform_message_id TEXT,
    observed INTEGER DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    compacted INTEGER NOT NULL DEFAULT 0,
    api_content TEXT
);

CREATE TABLE IF NOT EXISTS session_model_usage (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    billing_provider TEXT NOT NULL DEFAULT '',
    billing_base_url TEXT NOT NULL DEFAULT '',
    billing_mode TEXT NOT NULL DEFAULT '',
    task TEXT NOT NULL DEFAULT '',
    api_call_count INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd REAL NOT NULL DEFAULT 0,
    actual_cost_usd REAL NOT NULL DEFAULT 0,
    cost_status TEXT,
    cost_source TEXT,
    first_seen REAL,
    last_seen REAL,
    PRIMARY KEY (session_id, model, billing_provider, billing_base_url, billing_mode, task)
);

CREATE TABLE IF NOT EXISTS state_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS gateway_routing (
    scope TEXT NOT NULL DEFAULT '',
    session_key TEXT NOT NULL,
    entry_json TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (scope, session_key)
);

CREATE TABLE IF NOT EXISTS compression_locks (
    session_id TEXT PRIMARY KEY,
    holder TEXT NOT NULL,
    acquired_at REAL NOT NULL,
    expires_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS async_delegations (
    delegation_id TEXT PRIMARY KEY,
    origin_session TEXT NOT NULL,
    origin_ui_session_id TEXT NOT NULL DEFAULT '',
    parent_session_id TEXT,
    state TEXT NOT NULL,
    dispatched_at REAL NOT NULL,
    completed_at REAL,
    updated_at REAL NOT NULL,
    event_json TEXT,
    result_json TEXT,
    delivery_state TEXT NOT NULL DEFAULT 'pending',
    delivery_attempts INTEGER NOT NULL DEFAULT 0,
    delivered_at REAL,
    owner_pid INTEGER,
    owner_started_at INTEGER,
    task_json TEXT,
    delivery_claim TEXT,
    delivery_claimed_at REAL
);

CREATE INDEX IF NOT EXISTS idx_sessions_source ON sessions(source);
CREATE INDEX IF NOT EXISTS idx_sessions_source_id ON sessions(source, id);
CREATE INDEX IF NOT EXISTS idx_sessions_parent ON sessions(parent_session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_compression_locks_expires ON compression_locks(expires_at);
CREATE INDEX IF NOT EXISTS idx_session_model_usage_session ON session_model_usage(session_id);
CREATE INDEX IF NOT EXISTS idx_session_model_usage_model ON session_model_usage(model);
CREATE INDEX IF NOT EXISTS idx_async_delegations_delivery
    ON async_delegations(delivery_state, completed_at);
"""

# Indexes that reference columns added in later schema versions must be
# created AFTER _reconcile_columns() has had a chance to ADD them on
# existing databases. SCHEMA_SQL above is run by sqlite executescript
# which would otherwise fail on legacy DBs ("no such column: active").
DEFERRED_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_messages_session_active
    ON messages(session_id, active, timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_active_null
    ON messages(active) WHERE active IS NULL;
CREATE INDEX IF NOT EXISTS idx_sessions_session_key
    ON sessions(session_key, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_gateway_peer
    ON sessions(source, user_id, chat_id, chat_type, thread_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_handoff_state
    ON sessions(handoff_state, started_at);
CREATE INDEX IF NOT EXISTS idx_sessions_root
    ON sessions(root_session_id);
-- "this project's sessions, newest first" is the sidebar's hot query.
CREATE INDEX IF NOT EXISTS idx_sessions_project
    ON sessions(project_id, started_at DESC);
"""

# Backfill sessions.root_session_id from parent chains (run whenever NULL
# roots exist — same heal-on-startup pattern as the ``active`` repair).
# Anchors are sessions that are their own root in practice: no parent, a
# self-parent, or a dangling parent pointer (parent row deleted by a legacy
# build before FK enforcement). Children inherit their anchor's root.
# Cycle-safe: a cycle can never descend from an anchor (every cycle member's
# parent is inside the cycle), and the depth cap bounds degenerate chains.
_ROOT_BACKFILL_SQL = """
WITH RECURSIVE root_map(id, root, depth) AS (
    SELECT s.id, s.id, 0
      FROM sessions s
     WHERE s.parent_session_id IS NULL
        OR s.parent_session_id = s.id
        OR NOT EXISTS (
               SELECT 1 FROM sessions p WHERE p.id = s.parent_session_id
           )
    UNION ALL
    SELECT c.id, r.root, r.depth + 1
      FROM sessions c
      JOIN root_map r ON c.parent_session_id = r.id
     WHERE r.depth < 64
       AND c.id != c.parent_session_id
)
UPDATE sessions
   SET root_session_id = (
           SELECT root FROM root_map WHERE root_map.id = sessions.id
       )
 WHERE root_session_id IS NULL
   AND (SELECT root FROM root_map WHERE root_map.id = sessions.id) IS NOT NULL
"""

# ── Deferred FTS rebuild bookkeeping (schema v23) ──
# While a background index rebuild is pending, two state_meta keys define
# which message rows are currently IN the FTS indexes:
#
#   fts_rebuild_high_water  H — MAX(messages.id) at the moment the old
#                                indexes were dropped
#   fts_rebuild_progress    P — highest id the chunked backfill has indexed
#
# A row is indexed iff  id <= P  (backfilled)  OR  id > H  (inserted after
# the drop; ids are AUTOINCREMENT so new rows are always > H and the insert
# triggers index them live).  Rows in (P, H] are not yet indexed.
#
# Every trigger below gates on that same predicate: firing an FTS5
# external-content 'delete' for a row that is NOT in the index corrupts the
# index, and skipping it for a row that IS indexed leaves a stale entry.
# When no rebuild is pending both keys are absent and COALESCE turns the
# predicate into a tautology (id > -1 OR id <= -1), i.e. normal operation.
# The two state_meta PK probes per write are negligible next to the FTS
# insert itself.
FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    tool_name,
    tool_calls,
    content='messages',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS messages_fts_insert AFTER INSERT ON messages
WHEN (new.id > COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                         WHERE key = 'fts_rebuild_high_water'), -1)
   OR new.id <= COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                          WHERE key = 'fts_rebuild_progress'), -1))
BEGIN
    INSERT INTO messages_fts(rowid, content, tool_name, tool_calls)
    VALUES (new.id, new.content, new.tool_name, new.tool_calls);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_delete AFTER DELETE ON messages
WHEN (old.id > COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                         WHERE key = 'fts_rebuild_high_water'), -1)
   OR old.id <= COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                          WHERE key = 'fts_rebuild_progress'), -1))
BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content, tool_name, tool_calls)
    VALUES ('delete', old.id, old.content, old.tool_name, old.tool_calls);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_update AFTER UPDATE ON messages
WHEN (old.content IS NOT new.content
    OR old.tool_name IS NOT new.tool_name
    OR old.tool_calls IS NOT new.tool_calls)
   AND (old.id > COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                           WHERE key = 'fts_rebuild_high_water'), -1)
     OR old.id <= COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                            WHERE key = 'fts_rebuild_progress'), -1))
BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content, tool_name, tool_calls)
    VALUES ('delete', old.id, old.content, old.tool_name, old.tool_calls);
    INSERT INTO messages_fts(rowid, content, tool_name, tool_calls)
    VALUES (new.id, new.content, new.tool_name, new.tool_calls);
END;
"""

# Trigram FTS5 table for CJK substring search.  The default unicode61
# tokenizer splits CJK characters into individual tokens, breaking phrase
# matching.  The trigram tokenizer creates overlapping 3-byte sequences so
# substring queries work natively for any script (CJK, Thai, etc.).
#
# The trigram index is the most expensive index in state.db (~2.6x the size
# of the text it covers), and ``role='tool'`` rows are ~90% of message bytes
# while being almost entirely machine noise (base64 payloads, file dumps,
# delegation transcripts).  The index therefore reads through
# ``messages_fts_trigram_src``, a view that excludes tool rows — they stay
# fully stored in ``messages`` and fully searchable via the standard
# ``messages_fts`` index; they just don't get trigram (CJK substring)
# treatment.  ``search_messages`` routes CJK queries that filter on
# ``role='tool'`` to the LIKE fallback for the same reason.
FTS_TRIGRAM_SQL = """
CREATE VIEW IF NOT EXISTS messages_fts_trigram_src AS
    SELECT id, role, content, tool_name, tool_calls
    FROM messages
    WHERE role <> 'tool';

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts_trigram USING fts5(
    content,
    tool_name,
    tool_calls,
    content='messages_fts_trigram_src',
    content_rowid='id',
    tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS messages_fts_trigram_insert AFTER INSERT ON messages
WHEN new.role <> 'tool'
   AND (new.id > COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                           WHERE key = 'fts_rebuild_high_water'), -1)
     OR new.id <= COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                            WHERE key = 'fts_rebuild_progress'), -1))
BEGIN
    INSERT INTO messages_fts_trigram(rowid, content, tool_name, tool_calls)
    VALUES (new.id, new.content, new.tool_name, new.tool_calls);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_trigram_delete AFTER DELETE ON messages
WHEN old.role <> 'tool'
   AND (old.id > COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                           WHERE key = 'fts_rebuild_high_water'), -1)
     OR old.id <= COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                            WHERE key = 'fts_rebuild_progress'), -1))
BEGIN
    INSERT INTO messages_fts_trigram(messages_fts_trigram, rowid, content, tool_name, tool_calls)
    VALUES ('delete', old.id, old.content, old.tool_name, old.tool_calls);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_trigram_update AFTER UPDATE ON messages
WHEN (old.content IS NOT new.content
    OR old.tool_name IS NOT new.tool_name
    OR old.tool_calls IS NOT new.tool_calls
    OR old.role IS NOT new.role)
   AND (old.id > COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                           WHERE key = 'fts_rebuild_high_water'), -1)
     OR old.id <= COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                            WHERE key = 'fts_rebuild_progress'), -1))
BEGIN
    INSERT INTO messages_fts_trigram(messages_fts_trigram, rowid, content, tool_name, tool_calls)
    SELECT 'delete', old.id, old.content, old.tool_name, old.tool_calls
    WHERE old.role <> 'tool';
    INSERT INTO messages_fts_trigram(rowid, content, tool_name, tool_calls)
    SELECT new.id, new.content, new.tool_name, new.tool_calls
    WHERE new.role <> 'tool';
END;
"""

# ── CJK-bigram FTS index (replaces the trigram index when available) ────
#
# The trigram tokenizer needs >=3 chars per query term, so 1-2 char CJK
# terms (ubiquitous in Korean/Chinese: 일본, 구글, 项目, ...) fall through
# to a LIKE full-table scan — measured 3-6s CPU per query on multi-GB
# installs and the dominant base cost of session_search on CJK workloads.
#
# ``cjk_unicode61`` (native/fts5_cjk/, a ~250-line loadable FTS5 tokenizer
# with no dependencies) wraps unicode61: maximal CJK runs are re-emitted as
# overlapping character bigrams (Lucene CJKAnalyzer semantics), everything
# else passes through unchanged. FTS5 phrase semantics turn a query term's
# consecutive bigrams into exact substring matching down to 2 chars at
# index speed. Contributed by Soju06 (PR #65544).
#
# Same v23 storage discipline as the trigram table it replaces:
# external-content over a tool-row-excluding view (zero inline text
# copies; tool rows stay searchable via ``messages_fts``), triggers gated
# on a DEDICATED marker pair (``fts_cjk_rebuild_high_water`` /
# ``fts_cjk_rebuild_progress``) so a cjk-only backfill — e.g. the
# trigram→cjk upgrade on an already-optimized DB — never gates the
# complete ``messages_fts`` index's triggers.
#
# The table exists ONLY when the loadable tokenizer is available
# (``~/.opencodon/lib/libfts5_cjk.so``, built by ``native/fts5_cjk/build.sh``).
# A process that cannot load it self-heals by dropping the cjk triggers
# (message writes keep working; the index goes stale and is rebuilt by the
# next ``opencodon sessions optimize-storage`` on a capable host).
#
# Split DDL: the table/view part is safe to ensure any time; the triggers
# are created ONLY while the index is complete-or-marker-gated. A stale
# index (trigger gap of unknown extent) must keep its triggers DROPPED —
# an external-content 'delete' op for a rowid the index never held is the
# canonical FTS5 index-corruption hazard the v23 marker gating exists to
# prevent.
FTS_CJK_TABLE_SQL = """
CREATE VIEW IF NOT EXISTS messages_fts_cjk_src AS
    SELECT id, role, content, tool_name, tool_calls
    FROM messages
    WHERE role <> 'tool';

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts_cjk USING fts5(
    content,
    tool_name,
    tool_calls,
    content='messages_fts_cjk_src',
    content_rowid='id',
    tokenize='cjk_unicode61'
);
"""

FTS_CJK_TRIGGER_SQL = """
CREATE TRIGGER IF NOT EXISTS messages_fts_cjk_insert AFTER INSERT ON messages
WHEN new.role <> 'tool'
   AND (new.id > COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                           WHERE key = 'fts_cjk_rebuild_high_water'), -1)
     OR new.id <= COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                            WHERE key = 'fts_cjk_rebuild_progress'), -1))
BEGIN
    INSERT INTO messages_fts_cjk(rowid, content, tool_name, tool_calls)
    VALUES (new.id, new.content, new.tool_name, new.tool_calls);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_cjk_delete AFTER DELETE ON messages
WHEN old.role <> 'tool'
   AND (old.id > COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                           WHERE key = 'fts_cjk_rebuild_high_water'), -1)
     OR old.id <= COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                            WHERE key = 'fts_cjk_rebuild_progress'), -1))
BEGIN
    INSERT INTO messages_fts_cjk(messages_fts_cjk, rowid, content, tool_name, tool_calls)
    VALUES ('delete', old.id, old.content, old.tool_name, old.tool_calls);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_cjk_update AFTER UPDATE ON messages
WHEN (old.content IS NOT new.content
    OR old.tool_name IS NOT new.tool_name
    OR old.tool_calls IS NOT new.tool_calls
    OR old.role IS NOT new.role)
   AND (old.id > COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                           WHERE key = 'fts_cjk_rebuild_high_water'), -1)
     OR old.id <= COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                            WHERE key = 'fts_cjk_rebuild_progress'), -1))
BEGIN
    INSERT INTO messages_fts_cjk(messages_fts_cjk, rowid, content, tool_name, tool_calls)
    SELECT 'delete', old.id, old.content, old.tool_name, old.tool_calls
    WHERE old.role <> 'tool';
    INSERT INTO messages_fts_cjk(rowid, content, tool_name, tool_calls)
    SELECT new.id, new.content, new.tool_name, new.tool_calls
    WHERE new.role <> 'tool';
END;
"""

_FTS_CJK_TRIGGERS = (
    "messages_fts_cjk_insert",
    "messages_fts_cjk_delete",
    "messages_fts_cjk_update",
)

# state_meta breadcrumb set when a tokenizer-less process had to drop the
# cjk triggers to keep message writes alive: rows written from that moment
# on are missing from the cjk index, so it must not serve reads until
# `opencodon sessions optimize-storage` rebuilds it on a capable host.
FTS_CJK_STALE_KEY = "fts_cjk_stale"


def fts5_cjk_so_path() -> Path:
    """Location of the cjk_unicode61 loadable extension."""
    env = os.getenv("OPENCODON_FTS5_CJK_SO")
    if env:
        return Path(env).expanduser()
    return get_opencodon_home() / "lib" / "libfts5_cjk.so"


def _cjk_fts_config_enabled() -> bool:
    """config.yaml ``sessions.cjk_fts`` (default on), via its env bridge."""
    return os.getenv("OPENCODON_CJK_FTS", "1").strip().lower() not in (
        "0", "false", "off", "no",
    )


def load_fts5_cjk_extension(conn: sqlite3.Connection) -> bool:
    """Best-effort load of the cjk_unicode61 tokenizer into ``conn``.

    Returns False (never raises) when the .so is absent, the feature is
    disabled via ``sessions.cjk_fts``, or this Python build has extension
    loading compiled out — every caller treats False as "behave exactly as
    before the cjk index existed".
    """
    if not _cjk_fts_config_enabled():
        return False
    path = fts5_cjk_so_path()
    if not path.exists():
        return False
    try:
        conn.enable_load_extension(True)
        try:
            conn.load_extension(str(path))
        finally:
            conn.enable_load_extension(False)
        return True
    except Exception:
        logger.warning("fts5_cjk extension load failed (%s)", path, exc_info=True)
        return False



# ── Legacy (v22 / inline-content) FTS DDL ──────────────────────────────
# Used ONLY to keep an existing pre-v23 install's search working and its
# triggers repairable UNTIL the user opts into `opencodon db optimize`. This is
# the exact inline shape v11..v22 shipped: each virtual table stores its own
# copy of ``content || tool_name || tool_calls`` and the trigram table indexes
# every row (including role='tool'). We never CREATE these on a fresh install —
# fresh installs are born on the v23 external-content schema above. These
# constants exist so a legacy DB is never accidentally handed the v23 DDL
# (which would create the external-content trigram source VIEW and leave the
# DB in a mixed, broken state). `optimize_fts_storage()` is what migrates a
# legacy DB to the v23 shape.
LEGACY_FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content
);

CREATE TRIGGER IF NOT EXISTS messages_fts_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (
        new.id,
        COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, '') || ' ' || COALESCE(new.tool_calls, '')
    );
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_delete AFTER DELETE ON messages BEGIN
    DELETE FROM messages_fts WHERE rowid = old.id;
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_update AFTER UPDATE ON messages BEGIN
    DELETE FROM messages_fts WHERE rowid = old.id;
    INSERT INTO messages_fts(rowid, content) VALUES (
        new.id,
        COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, '') || ' ' || COALESCE(new.tool_calls, '')
    );
END;
"""

LEGACY_FTS_TRIGRAM_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts_trigram USING fts5(
    content,
    tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS messages_fts_trigram_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts_trigram(rowid, content) VALUES (
        new.id,
        COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, '') || ' ' || COALESCE(new.tool_calls, '')
    );
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_trigram_delete AFTER DELETE ON messages BEGIN
    DELETE FROM messages_fts_trigram WHERE rowid = old.id;
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_trigram_update AFTER UPDATE ON messages BEGIN
    DELETE FROM messages_fts_trigram WHERE rowid = old.id;
    INSERT INTO messages_fts_trigram(rowid, content) VALUES (
        new.id,
        COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, '') || ' ' || COALESCE(new.tool_calls, '')
    );
END;
"""


from opencodon.state._fts import FtsSearchMixin
from opencodon.state._schema import SchemaMaintenanceMixin
from opencodon.state._gateway_meta import GatewayRoutingMixin
from opencodon.state._compression_state import CompressionStateMixin
from opencodon.state._transcripts import TranscriptMixin
from opencodon.state._porting import ImportExportMixin
from opencodon.state._handoff import HandoffMixin


class SessionDB(
    FtsSearchMixin,
    SchemaMaintenanceMixin,
    GatewayRoutingMixin,
    CompressionStateMixin,
    TranscriptMixin,
    ImportExportMixin,
    HandoffMixin,
):
    """
    SQLite-backed session storage with FTS5 search.

    Thread-safe for the common gateway pattern (multiple reader threads,
    single writer via WAL mode). Each method opens its own cursor.
    """

    # ── Write-contention tuning ──
    # With multiple opencodon processes (gateway + CLI sessions + worktree agents)
    # all sharing one state.db, WAL write-lock contention causes visible TUI
    # freezes.  SQLite's built-in busy handler uses a deterministic sleep
    # schedule that causes convoy effects under high concurrency.
    #
    # Instead, we keep the SQLite timeout short (1s) and handle retries at the
    # application level with random jitter, which naturally staggers competing
    # writers and avoids the convoy.
    _WRITE_MAX_RETRIES = 15
    _WRITE_RETRY_MIN_S = 0.020   # 20ms
    _WRITE_RETRY_MAX_S = 0.150   # 150ms
    # Attempt a WAL checkpoint every N successful writes (PASSIVE mode).
    _CHECKPOINT_EVERY_N_WRITES = 50
    # Merge fragmented FTS5 segments every N successful writes. The message
    # triggers append one segment per insert; left unmaintained these grow
    # into tens of thousands of segments, so every MATCH must scan them all
    # and every insert pays a growing automerge cost — which lengthens the
    # write-lock hold time and starves competing writers (gateway + cron
    # processes share one state.db), surfacing as "database is locked".
    # 'optimize' is a no-op once the index is already merged, so an idle DB
    # pays almost nothing; the cadence is deliberately coarse so the one-off
    # merge cost is amortised far below the checkpoint cadence.
    _OPTIMIZE_EVERY_N_WRITES = 1000
    # Session imports intentionally use a lower cap than exports: import holds
    # one BEGIN IMMEDIATE transaction, so bounded batches avoid starving live
    # gateway/CLI writers. The dashboard accepts one exported JSON/JSONL file
    # at a time, so these still cover normal history restores.
    _IMPORT_MAX_SESSIONS = 500
    _IMPORT_MAX_MESSAGES_PER_SESSION = 10_000
    _IMPORT_MAX_TOTAL_MESSAGES = 50_000
    _IMPORT_MAX_SESSION_BYTES = 5 * 1024 * 1024
    _IMPORT_MAX_TOTAL_BYTES = 25 * 1024 * 1024

    def __init__(self, db_path: Path = None, read_only: bool = False):
        self.db_path = db_path or DEFAULT_DB_PATH
        self.read_only = read_only

        self._lock = threading.Lock()
        self._write_count = 0
        # One-shot guard for the runtime FTS rebuild recovery on the write
        # path. A corrupt FTS shadow table makes EVERY message write raise
        # the malformed/corrupt error class via the sync triggers; we repair
        # in place at most once per SessionDB instance so a genuinely
        # unrecoverable database can't put writers into a rebuild loop.
        self._fts_runtime_rebuild_attempted = False
        self._fts_enabled = False
        self._trigram_available = False
        # CJK-bigram index (cjk_unicode61 loadable tokenizer). _fts_cjk_loaded:
        # extension present on the writer connection; _fts_cjk_available: the
        # messages_fts_cjk table is queryable AND not marked stale. Set during
        # _init_schema / _probe_fts_cjk.
        self._fts_cjk_loaded = False
        self._fts_cjk_available = False
        self._fts_unavailable_warned = False
        self._conn = None
        try:
            if read_only:
                # Read-only attach for cross-profile aggregation: SELECT-only,
                # so we skip schema init entirely (no DDL, no FTS probe, no
                # column reconcile). Crucially this takes NO write lock, so
                # polling another profile's live DB on every sidebar refresh
                # never contends with that profile's running backend. The DB
                # must already exist + be initialised (callers guard on
                # db_path.exists()); a SELECT against an empty file raises and
                # the caller degrades per-profile.
                self._conn = sqlite3.connect(
                    f"file:{self.db_path}?mode=ro",
                    uri=True,
                    check_same_thread=False,
                    timeout=1.0,
                    isolation_level=None,
                )
                self._conn.row_factory = sqlite3.Row
                return

            self.db_path.parent.mkdir(parents=True, exist_ok=True)

            def _connect_and_init():
                self._conn = sqlite3.connect(
                    str(self.db_path),
                    check_same_thread=False,
                    # Short timeout — application-level retry with random
                    # jitter handles contention instead of sitting in
                    # SQLite's internal busy handler for up to 30s.
                    timeout=1.0,
                    # auto-starts transactions on DML, which conflicts with
                    # our explicit BEGIN IMMEDIATE.  None = we manage
                    # transactions ourselves.
                    isolation_level=None,
                )
                self._conn.row_factory = sqlite3.Row
                apply_wal_with_fallback(self._conn, db_label="state.db")
                self._conn.execute("PRAGMA foreign_keys=ON")
                self._fts_cjk_loaded = load_fts5_cjk_extension(self._conn)
                self._init_schema()

            try:
                _connect_and_init()
            except sqlite3.DatabaseError as exc:
                # The malformed-schema class (e.g. a duplicate sqlite_master
                # row for messages_fts) fails on the very first statement —
                # before _init_schema can run — so it can't be caught at the
                # FTS-rebuild layer. Recover by repairing sqlite_master in
                # place (backup first; canonical sessions/messages preserved),
                # then reopen once. This is what lets Desktop/Dashboard
                # self-heal instead of silently showing "no sessions".
                if not is_malformed_db_error(exc) or not _claim_repair_attempt(self.db_path):
                    raise
                logger.error(
                    "state.db schema is malformed (%s) — attempting automatic "
                    "repair (a backup copy is made first).", exc,
                )
                try:
                    if self._conn is not None:
                        self._conn.close()
                except Exception:
                    pass
                report = repair_state_db_schema(self.db_path)
                if not report.get("repaired"):
                    raise
                _connect_and_init()

            # NOTE: the v23 FTS optimization is OPT-IN (`opencodon db optimize`),
            # never auto-started on open. Legacy installs keep their working
            # v22 inline FTS untouched here; only the explicit foreground
            # command demotes + rebuilds. This avoids a background worker
            # racing session lifecycle and the surprise disk/latency cost on
            # an unattended open. (An interrupted optimize resumes when the
            # user re-runs the command.)
        except Exception as exc:
            # Capture the cause so /resume and friends can surface WHY the
            # session DB is unavailable instead of a bare "Session database
            # not available."  Callers that catch this exception keep their
            # existing ``self._session_db = None`` degradation path.
            #
            # Note: we deliberately do NOT clear _last_init_error on the
            # success path (no else branch).  In multi-threaded callers
            # (gateway, web_server per-request SessionDB()), a concurrent
            # successful open racing past this failure would erase the
            # cause that another thread's /resume is about to format.
            # Tests that need to reset the state can call
            # ``opencodon_state._set_last_init_error(None)`` explicitly.
            _set_last_init_error(f"{type(exc).__name__}: {exc}")
            raise

    # ── Core write helper ──














    def _execute_write(
        self,
        fn: Callable[[sqlite3.Connection], T],
    ) -> T:
        """Execute a write transaction with BEGIN IMMEDIATE and jitter retry.

        *fn* receives the connection and should perform INSERT/UPDATE/DELETE
        statements.  The caller must NOT call ``commit()`` — that's handled
        here after *fn* returns.

        BEGIN IMMEDIATE acquires the WAL write lock at transaction start
        (not at commit time), so lock contention surfaces immediately.
        On ``database is locked``, we release the Python lock, sleep a
        random 20-150ms, and retry — breaking the convoy pattern that
        SQLite's built-in deterministic backoff creates.

        Returns whatever *fn* returns.
        """
        last_err: Optional[Exception] = None
        for attempt in range(self._WRITE_MAX_RETRIES):
            try:
                with self._lock:
                    self._conn.execute("BEGIN IMMEDIATE")
                    try:
                        result = fn(self._conn)
                        self._conn.commit()
                    except BaseException:
                        try:
                            self._conn.rollback()
                        except Exception:
                            pass
                        raise
                # Success — periodic best-effort checkpoint + FTS merge.
                self._write_count += 1
                if self._write_count % self._CHECKPOINT_EVERY_N_WRITES == 0:
                    self._try_wal_checkpoint()
                if self._write_count % self._OPTIMIZE_EVERY_N_WRITES == 0:
                    self._try_optimize_fts()
                return result
            except sqlite3.OperationalError as exc:
                err_msg = str(exc).lower()
                if "locked" in err_msg or "busy" in err_msg:
                    last_err = exc
                    if attempt < self._WRITE_MAX_RETRIES - 1:
                        jitter = random.uniform(
                            self._WRITE_RETRY_MIN_S,
                            self._WRITE_RETRY_MAX_S,
                        )
                        time.sleep(jitter)
                        continue
                # Non-lock error or retries exhausted — propagate.
                raise
            except sqlite3.DatabaseError as exc:
                # Corrupt FTS shadow tables make every write raise the
                # malformed/corrupt error class through the FTS sync triggers
                # while the canonical messages table is intact. The gateway
                # session store has its own retry queue for transcript
                # appends (#65637 salvage), but cron and CLI writers call
                # SessionDB directly — without this, their writes hard-fail
                # until the next process restart triggers the offline repair.
                # Rebuild the FTS index in place (once per instance) via
                # rebuild_fts() and retry the failed write immediately.
                if not self._try_runtime_fts_rebuild(exc):
                    raise
                continue
        # Retries exhausted (shouldn't normally reach here).
        raise last_err or sqlite3.OperationalError(
            "database is locked after max retries"
        )





    def close(self):
        """Close the database connection.

        Attempts a TRUNCATE WAL checkpoint first so that exiting processes
        help shrink the WAL file.
        """
        with self._lock:
            if self._conn:
                try:
                    self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except Exception as exc:
                    logger.debug("WAL checkpoint (TRUNCATE) at close failed: %s", exc)
                self._conn.close()
                self._conn = None

    # ── Chunked FTS rebuild engine (v23 opt-in optimize) ──
    #
    # `optimize_fts_storage()` (the `opencodon sessions optimize-storage`
    # command) drops the legacy inline FTS indexes and backfills the new
    # external-content ones. A single blocking rebuild measured ~16 minutes
    # of held write lock on a real 25 GB DB, so the backfill runs in small
    # chunks, each in its own short write transaction:
    #   - concurrent readers/writers are never starved (WAL stays small,
    #     each chunk checkpoints via the normal _execute_write cadence);
    #   - an interrupted run (Ctrl-C, crash) resumes from
    #     fts_rebuild_progress when the command is re-run;
    #   - multiple processes sharing the DB don't double-run it — each chunk
    #     claims work by compare-and-swap on fts_rebuild_progress, so even a
    #     concurrent second runner just interleaves chunks safely.
    #
    # THROTTLING (the part that keeps a live gateway sharing the DB
    # responsive): a greedy chunk loop re-acquires BEGIN IMMEDIATE nearly
    # back-to-back and can starve another process's writer into exhausting
    # its lock retries (an early 5000-row/50ms version owned the write lock
    # ~85% of the time and visibly froze concurrent CLI sessions on a large
    # install). Two layers prevent that:
    #   1. Small chunks (500 rows) — a foreground write queues behind a
    #      chunk for at most ~tens of ms.
    #   2. Inter-chunk pause — the loop sleeps max(_FTS_REBUILD_MIN_PAUSE,
    #      chunk cost x _FTS_REBUILD_DUTY_FACTOR) between chunks, capping
    #      this process's share of DB bandwidth so concurrent writers always
    #      find open windows. This works cross-process (unlike any
    #      same-process activity stamp) because it bounds our own duty
    #      cycle unconditionally.

    _FTS_REBUILD_CHUNK_ROWS = 500
    _FTS_REBUILD_DUTY_FACTOR = 4.0      # sleep >= 4x chunk cost (≤20% duty)
    _FTS_REBUILD_MIN_PAUSE = 0.2        # seconds — floor between chunks



    # Demoted v22 FTS shadow tables awaiting teardown (see the v23 migration:
    # DROP of a multi-GB FTS vtable blocks for minutes, so the migration
    # demotes the vtable definitions out of sqlite_master and renames the
    # orphaned shadow tables — now plain tables — to fts_v22_trash_*; the
    # worker empties them in bounded chunks, then drops them cheaply).
    _FTS_TRASH_PREFIX = "fts_v22_trash_"



    # ── CJK-bigram index backfill (dedicated marker pair) ──
    #
    # Same chunk engine as the main deferred rebuild, but on the
    # ``fts_cjk_rebuild_*`` markers so a cjk-only backfill (the common case:
    # an already-optimized v23 DB gaining the cjk index) never gates the
    # complete ``messages_fts`` / trigram triggers.





    # ── Opt-in v23 FTS storage optimization (`opencodon sessions optimize-storage`) ──
    #
    # This is the ONLY path that migrates an existing legacy (v22 inline) DB
    # to the v23 external-content schema. It is deliberately foreground and
    # user-invoked, never automatic, because it is disk-heavy and long. It
    # runs the throttled/resumable chunk engine above to completion
    # synchronously — demote → new schema → chunked backfill → chunked
    # teardown — with progress callbacks, a disk preflight in the CLI
    # wrapper, a VACUUM at the end, and a defensive schema_version bump.








    # =========================================================================
    # Session lifecycle
    # =========================================================================

    def _insert_session_row(
        self,
        session_id: str,
        source: str,
        model: str = None,
        model_config: Dict[str, Any] = None,
        system_prompt: str = None,
        user_id: str = None,
        session_key: str = None,
        chat_id: str = None,
        chat_type: str = None,
        thread_id: str = None,
        parent_session_id: str = None,
        cwd: str = None,
        profile_name: str = None,
        git_repo_root: str = None,
        project_id: str = None,
    ) -> None:
        """Insert a session row, enriching NULL metadata on conflict.

        The gateway's ``get_or_create_session`` creates a bare row (source +
        user_id) *before* the agent exists; the agent's later
        ``create_session`` then carries the real ``model`` / ``model_config`` /
        ``system_prompt``. A plain ``INSERT OR IGNORE`` silently dropped that
        enrichment, leaving gateway sessions with NULL model/billing metadata.
        The ``ON CONFLICT`` upsert backfills those fields via ``COALESCE`` —
        only filling columns that are still NULL, never overwriting values an
        earlier writer already set (so a later bare call with source="unknown"
        can't clobber a real source/model).

        ``chat_id``/``thread_id`` record the messaging origin (the chat/room and
        thread the session was started in) so that gateway ``/resume`` can prove
        a persisted, now-inactive row belongs to the caller's chat/thread before
        switching to it (IDOR scoping — without them the ``sessions`` table has
        no chat/thread to compare).

        When ``parent_session_id`` is set (compression fork, delegate/subagent
        spawn, branch continuation) and this row's own ``cwd``/``git_repo_root``/
        ``git_branch`` are still NULL after the insert, they are backfilled from
        the parent row. Callers of ``create_session`` for a child session
        historically didn't propagate these fields themselves (e.g. the
        compression-fork path), so a lineage could silently lose its working
        directory and drop out of the project sidebar every time it forked
        (#64709). This only fills NULLs — an explicit ``cwd``/``git_repo_root``
        on the child is never overwritten. For compression forks specifically
        (parent ended with ``end_reason='compression'``), the gateway origin
        columns (``user_id``/``session_key``/``chat_id``/``chat_type``/
        ``thread_id``/``display_name``/``origin_json``) are inherited too, so a
        crash before the gateway re-records the peer can't strand the child
        without a recoverable routing mapping (#59527).
        """
        def _do(conn):
            # root_session_id: a child inherits its parent's root (falling
            # back to the parent itself for parents predating the column);
            # a top-level session — or one whose parent row is missing —
            # is its own root.
            root_session_id = session_id
            if parent_session_id and parent_session_id != session_id:
                parent_root = conn.execute(
                    "SELECT COALESCE(root_session_id, id) FROM sessions WHERE id = ?",
                    (parent_session_id,),
                ).fetchone()
                if parent_root and parent_root[0]:
                    root_session_id = parent_root[0]
            conn.execute(
                """INSERT INTO sessions (
                   id, source, user_id, session_key, chat_id, chat_type, thread_id,
                   model, model_config, system_prompt, parent_session_id,
                   root_session_id, cwd, profile_name, git_repo_root, project_id,
                   started_at
                )
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       model = COALESCE(sessions.model, excluded.model),
                       model_config = COALESCE(sessions.model_config, excluded.model_config),
                       system_prompt = COALESCE(sessions.system_prompt, excluded.system_prompt),
                       session_key = COALESCE(sessions.session_key, excluded.session_key),
                       chat_id = COALESCE(sessions.chat_id, excluded.chat_id),
                       chat_type = COALESCE(sessions.chat_type, excluded.chat_type),
                       thread_id = COALESCE(sessions.thread_id, excluded.thread_id),
                       parent_session_id = COALESCE(sessions.parent_session_id, excluded.parent_session_id),
                       root_session_id = CASE
                           WHEN sessions.parent_session_id IS NULL
                                AND excluded.parent_session_id IS NOT NULL
                           THEN excluded.root_session_id
                           ELSE COALESCE(sessions.root_session_id, excluded.root_session_id)
                       END,
                       cwd = COALESCE(sessions.cwd, excluded.cwd),
                       profile_name = COALESCE(sessions.profile_name, excluded.profile_name),
                       git_repo_root = COALESCE(sessions.git_repo_root, excluded.git_repo_root),
                       project_id = COALESCE(sessions.project_id, excluded.project_id)""",
                (
                    session_id,
                    source,
                    user_id,
                    session_key,
                    chat_id,
                    chat_type,
                    thread_id,
                    model,
                    json.dumps(model_config) if model_config else None,
                    system_prompt,
                    parent_session_id,
                    root_session_id,
                    cwd,
                    profile_name,
                    git_repo_root,
                    project_id,
                    time.time(),
                ),
            )
            if parent_session_id:
                conn.execute(
                    """UPDATE sessions
                       SET cwd = COALESCE(sessions.cwd,
                                 (SELECT p.cwd FROM sessions p
                                   WHERE p.id = sessions.parent_session_id)),
                           git_repo_root = COALESCE(sessions.git_repo_root,
                                           (SELECT p.git_repo_root FROM sessions p
                                             WHERE p.id = sessions.parent_session_id)),
                           git_branch = COALESCE(sessions.git_branch,
                                        (SELECT p.git_branch FROM sessions p
                                          WHERE p.id = sessions.parent_session_id)),
                           -- A compression fork, delegate, or branch continuation
                           -- is the same work as its parent, so it belongs to the
                           -- same project. Inherited rather than re-derived: the
                           -- child may not carry a cwd of its own yet, and
                           -- deriving would drop the lineage out of the project
                           -- on every fork.
                           project_id = COALESCE(sessions.project_id,
                                        (SELECT p.project_id FROM sessions p
                                          WHERE p.id = sessions.parent_session_id))
                     WHERE id = ? AND parent_session_id IS NOT NULL""",
                    (session_id,),
                )
                # Belt-and-suspenders for gateway routing metadata (#59527):
                # the gateway re-records the peer on the child after rotation
                # (d5b4879d4), but a hard crash between child creation and that
                # write leaves the child row without origin columns, so
                # ``find_latest_gateway_session_for_peer`` can't recover the
                # mapping on restart. Inherit them from the parent at creation
                # time — but ONLY for compression forks (parent already ended
                # with end_reason='compression'). Delegate/subagent children
                # are spawned while the parent is still live and must NOT
                # inherit routing keys, or peer recovery could repoint gateway
                # traffic into a subagent's session.
                conn.execute(
                    """UPDATE sessions
                       SET user_id = COALESCE(sessions.user_id,
                                     (SELECT p.user_id FROM sessions p
                                       WHERE p.id = sessions.parent_session_id)),
                           session_key = COALESCE(sessions.session_key,
                                         (SELECT p.session_key FROM sessions p
                                           WHERE p.id = sessions.parent_session_id)),
                           chat_id = COALESCE(sessions.chat_id,
                                     (SELECT p.chat_id FROM sessions p
                                       WHERE p.id = sessions.parent_session_id)),
                           chat_type = COALESCE(sessions.chat_type,
                                       (SELECT p.chat_type FROM sessions p
                                         WHERE p.id = sessions.parent_session_id)),
                           thread_id = COALESCE(sessions.thread_id,
                                       (SELECT p.thread_id FROM sessions p
                                         WHERE p.id = sessions.parent_session_id)),
                           display_name = COALESCE(sessions.display_name,
                                          (SELECT p.display_name FROM sessions p
                                            WHERE p.id = sessions.parent_session_id)),
                           origin_json = COALESCE(sessions.origin_json,
                                         (SELECT p.origin_json FROM sessions p
                                           WHERE p.id = sessions.parent_session_id))
                     WHERE id = ? AND parent_session_id IS NOT NULL
                       AND EXISTS (
                           SELECT 1 FROM sessions p
                           WHERE p.id = sessions.parent_session_id
                             AND p.end_reason = 'compression'
                       )""",
                    (session_id,),
                )
        self._execute_write(_do)

    def create_session(self, session_id: str, source: str, **kwargs) -> str:
        """Create a new session record. Returns the session_id."""
        self._insert_session_row(session_id, source, **kwargs)
        return session_id



    # ── Gateway routing index (replaces sessions.json, #9006 follow-up) ────









    def end_session(self, session_id: str, end_reason: str) -> None:
        """Mark a session as ended.

        No-ops when the session is already ended. The first end_reason wins:
        compression-split sessions must keep their ``end_reason = 'compression'``
        record even if a later stale ``end_session()`` call (e.g. from a
        desynced CLI session_id after ``/resume`` or ``/branch``) targets them
        with a different reason. Use ``reopen_session()`` first if you
        intentionally need to re-end a closed session with a new reason.
        """
        def _do(conn):
            conn.execute(
                "UPDATE sessions SET ended_at = ?, end_reason = ? "
                "WHERE id = ? AND ended_at IS NULL",
                (time.time(), end_reason, session_id),
            )
        self._execute_write(_do)

    def reopen_session(self, session_id: str) -> None:
        """Clear ended_at/end_reason so a session can be resumed."""
        def _do(conn):
            conn.execute(
                "UPDATE sessions SET ended_at = NULL, end_reason = NULL WHERE id = ?",
                (session_id,),
            )
        self._execute_write(_do)


    def update_session_cwd(
        self, session_id: str, cwd: str, git_branch: str = None, git_repo_root: str = None
    ) -> None:
        """Persist the session working directory when a frontend knows it.

        ``git_branch`` records the git branch checked out in ``cwd`` at the time
        the session started/resumed. The sidebar groups main-checkout sessions
        by this so feature-branch work doesn't pile under a single "main" row
        (the main checkout's *current* branch is transient and would
        misattribute past sessions).

        ``git_repo_root`` records the git repo this cwd belongs to — the
        authoritative project key. Resolving it here, at the lowest level, means
        every surface reads the same membership instead of re-probing git in the
        GUI over a partial page. Each field is only written when non-empty so a
        probe failure never clobbers a previously-captured value.
        """
        if not session_id or not cwd:
            return

        branch = (git_branch or "").strip()
        repo_root = (git_repo_root or "").strip()

        sets = ["cwd = ?"]
        params: List[Any] = [cwd]
        if branch:
            sets.append("git_branch = ?")
            params.append(branch)
        if repo_root:
            sets.append("git_repo_root = ?")
            params.append(repo_root)
        params.append(session_id)

        def _do(conn):
            conn.execute(f"UPDATE sessions SET {', '.join(sets)} WHERE id = ?", params)

        self._execute_write(_do)

    def set_session_project(self, session_id: str, project_id: Optional[str]) -> None:
        """Record (or clear) the project a session belongs to.

        Deliberately unconditional, unlike the COALESCE-guarded columns around
        it: this is the one field the user can reassign, and an explicit move
        must be able to overwrite an earlier value — including back to NULL for
        "no project". Membership is not something to infer twice.
        """
        if not session_id:
            return

        pid = (project_id or "").strip() or None

        def _do(conn):
            conn.execute(
                "UPDATE sessions SET project_id = ? WHERE id = ?", (pid, session_id)
            )

        self._execute_write(_do)

    def backfill_session_projects(self, session_to_project: Dict[str, str]) -> int:
        """Fill in ``project_id`` for rows that don't have one yet.

        Used once per database to adopt sessions that predate the column, from
        the same cwd-prefix derivation that used to run on every read. Only
        touches NULLs, so re-running is harmless and an explicit assignment is
        never overwritten. Returns the number of rows adopted.
        """
        pairs = [
            (pid, sid)
            for sid, pid in (session_to_project or {}).items()
            if sid and pid
        ]
        if not pairs:
            return 0

        def _do(conn):
            total = 0
            for pid, sid in pairs:
                cursor = conn.execute(
                    "UPDATE sessions SET project_id = ? "
                    "WHERE id = ? AND project_id IS NULL",
                    (pid, sid),
                )
                total += cursor.rowcount
            return total

        return int(self._execute_write(_do) or 0)

    def sessions_without_project(self, limit: int = 5000) -> List[Dict[str, Any]]:
        """Rows that have a cwd but no project yet — the backfill's input."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, cwd FROM sessions "
                "WHERE project_id IS NULL AND cwd IS NOT NULL AND TRIM(cwd) != '' "
                "ORDER BY started_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [{"id": row[0], "cwd": row[1]} for row in rows]

    def backfill_repo_roots(self, cwd_to_root: Dict[str, str]) -> None:
        """Persist resolved git repo roots for cwds that don't have one yet.

        Backfills history so projects light up for sessions created before the
        column existed, without clobbering an already-recorded root. Only
        non-empty roots are written (a non-git cwd stays NULL).
        """
        pairs = [(root, cwd) for cwd, root in cwd_to_root.items() if root and cwd]
        if not pairs:
            return

        def _do(conn):
            for root, cwd in pairs:
                conn.execute(
                    "UPDATE sessions SET git_repo_root = ? "
                    "WHERE cwd = ? AND COALESCE(git_repo_root, '') = ''",
                    (root, cwd),
                )

        self._execute_write(_do)






    # ──────────────────────────────────────────────────────────────────────
    # Compression locks
    # ──────────────────────────────────────────────────────────────────────
    # Atomic per-session locks that prevent two compression paths from
    # racing on the same session_id and producing orphan child sessions.
    #
    # The race: ``conversation_compression.py`` rotates ``agent.session_id``
    # as a side effect of a successful compression (end old session, create
    # new). That mutation is local to the AIAgent instance — but ``state.db``
    # is shared across all instances. Two AIAgents that share the same
    # ``session_id`` at the moment they both decide to compress (most
    # commonly the parent turn's agent + a background-review fork started
    # right after the turn ended) each end the parent and create their own
    # NEW session, parented to the same old id. The gateway SessionEntry
    # only catches one rotation; the other child silently accumulates
    # writes — Damien's "parent → two orphan children" repro shape.
    #
    # The lock is keyed by ``session_id`` and is held for the duration of
    # the compress() call plus the rotation. ``holder`` identifies the
    # current owner (pid:tid:nonce) for diagnostics; the lock is recovered
    # via ``expires_at`` if the holder process crashed without releasing.




    def update_session_meta(
        self,
        session_id: str,
        model_config_json: str,
        model: Optional[str] = None,
    ) -> None:
        """Update model_config and optionally model for an existing session.

        Uses COALESCE so that passing model=None leaves the stored model
        column unchanged.  Routes through _execute_write for the standard
        BEGIN IMMEDIATE + jitter-retry + lock guarantee.
        """
        def _do(conn):
            conn.execute(
                "UPDATE sessions SET model_config = ?, model = COALESCE(?, model) WHERE id = ?",
                (model_config_json, model, session_id),
            )
        self._execute_write(_do)

    def update_system_prompt(self, session_id: str, system_prompt: str) -> None:
        """Store the full assembled system prompt snapshot."""
        def _do(conn):
            conn.execute(
                "UPDATE sessions SET system_prompt = ? WHERE id = ?",
                (system_prompt, session_id),
            )
        self._execute_write(_do)

    def update_session_model(self, session_id: str, model: str) -> None:
        """Update the model for a session after a mid-session switch.

        Unlike ``update_token_counts`` which uses ``COALESCE(model, ?)``
        (only filling in NULL), this unconditionally sets the model column
        so that the dashboard reflects the user's latest /model choice.
        """
        def _do(conn):
            conn.execute(
                "UPDATE sessions SET model = ? WHERE id = ?",
                (model, session_id),
            )
        self._execute_write(_do)

    def update_session_billing_route(
        self,
        session_id: str,
        *,
        provider: str,
        base_url: str,
        billing_mode: Optional[str] = None,
    ) -> None:
        """Unconditionally update the billing provider/base_url for a session.

        Unlike ``update_token_counts`` which uses ``COALESCE(billing_provider, ?)``
        (only filling in NULL), this unconditionally sets the billing fields so
        that the dashboard reflects the user's latest /model switch.

        Also nulls ``system_prompt`` so the cached snapshot (which embeds a
        stale ``Model:`` / ``Provider:`` header) is rebuilt — matching the
        behavior of ``update_session_model`` (see #48173, #48248).
        """
        def _do(conn):
            conn.execute(
                """UPDATE sessions SET
                   billing_provider = ?,
                   billing_base_url = ?,
                   billing_mode = COALESCE(?, billing_mode),
                   system_prompt = NULL
                   WHERE id = ?""",
                (provider, base_url, billing_mode, session_id),
            )
        self._execute_write(_do)

    def update_token_counts(
        self,
        session_id: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        model: str = None,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        reasoning_tokens: int = 0,
        estimated_cost_usd: Optional[float] = None,
        actual_cost_usd: Optional[float] = None,
        cost_status: Optional[str] = None,
        cost_source: Optional[str] = None,
        pricing_version: Optional[str] = None,
        billing_provider: Optional[str] = None,
        billing_base_url: Optional[str] = None,
        billing_mode: Optional[str] = None,
        api_call_count: int = 0,
        absolute: bool = False,
    ) -> None:
        """Update token counters and backfill model if not already set.

        When *absolute* is False (default), values are **incremented** — use
        this for per-API-call deltas (CLI path).

        When *absolute* is True, values are **set directly** — use this when
        the caller already holds cumulative totals (gateway path, where the
        cached agent accumulates across messages).
        """
        # Ensure the session row exists so the UPDATE doesn't silently affect
        # 0 rows.  Under concurrent load (cron + delegate_task) the
        # initial create_session() may have failed due to SQLite locking.
        # INSERT OR IGNORE is cheap and idempotent.
        self._insert_session_row(session_id, "unknown", model=model)
        if absolute:
            sql = """UPDATE sessions SET
                   input_tokens = ?,
                   output_tokens = ?,
                   cache_read_tokens = ?,
                   cache_write_tokens = ?,
                   reasoning_tokens = ?,
                   estimated_cost_usd = COALESCE(?, 0),
                   actual_cost_usd = CASE
                       WHEN ? IS NULL THEN actual_cost_usd
                       ELSE ?
                   END,
                   cost_status = COALESCE(?, cost_status),
                   cost_source = COALESCE(?, cost_source),
                   pricing_version = COALESCE(?, pricing_version),
                   billing_provider = COALESCE(billing_provider, ?),
                   billing_base_url = COALESCE(billing_base_url, ?),
                   billing_mode = COALESCE(billing_mode, ?),
                   model = COALESCE(model, ?),
                   api_call_count = ?
                   WHERE id = ?"""
        else:
            sql = """UPDATE sessions SET
                   input_tokens = input_tokens + ?,
                   output_tokens = output_tokens + ?,
                   cache_read_tokens = cache_read_tokens + ?,
                   cache_write_tokens = cache_write_tokens + ?,
                   reasoning_tokens = reasoning_tokens + ?,
                   estimated_cost_usd = COALESCE(estimated_cost_usd, 0) + COALESCE(?, 0),
                   actual_cost_usd = CASE
                       WHEN ? IS NULL THEN actual_cost_usd
                       ELSE COALESCE(actual_cost_usd, 0) + ?
                   END,
                   cost_status = COALESCE(?, cost_status),
                   cost_source = COALESCE(?, cost_source),
                   pricing_version = COALESCE(?, pricing_version),
                   billing_provider = COALESCE(billing_provider, ?),
                   billing_base_url = COALESCE(billing_base_url, ?),
                   billing_mode = COALESCE(billing_mode, ?),
                   model = COALESCE(model, ?),
                   api_call_count = COALESCE(api_call_count, 0) + ?
                   WHERE id = ?"""
        has_accounted_usage = bool(
            input_tokens or output_tokens or cache_read_tokens
            or cache_write_tokens or reasoning_tokens or api_call_count
            or estimated_cost_usd or actual_cost_usd
        )
        params = (
            input_tokens,
            output_tokens,
            cache_read_tokens,
            cache_write_tokens,
            reasoning_tokens,
            estimated_cost_usd,
            actual_cost_usd,
            actual_cost_usd,
            cost_status,
            cost_source,
            pricing_version,
            billing_provider if has_accounted_usage else None,
            billing_base_url if has_accounted_usage else None,
            billing_mode if has_accounted_usage else None,
            model if has_accounted_usage else None,
            api_call_count,
            session_id,
        )
        # Per-model usage attribution.  ``update_token_counts`` is the single
        # chokepoint every per-API-call delta flows through (CLI, gateway, cron,
        # delegated runs — see conversation_loop / codex_runtime), and each call
        # carries the model/provider *active at the time of that call*.  The
        # ``sessions`` row only keeps one (model, billing_provider) pair, so a
        # mid-session ``/model`` switch otherwise attributes every token to the
        # initial model (issue #51607).  Recording the per-call delta into
        # session_model_usage keyed by the live model preserves an accurate
        # per-model breakdown regardless of how many times the user switches.
        #
        # Only the incremental path records here. Absolute cumulative updates
        # cannot be split back into routes; Insights reconciles any positive
        # residual against the aggregate session row instead.
        record_model_usage = (not absolute) and (
            input_tokens or output_tokens or cache_read_tokens
            or cache_write_tokens or reasoning_tokens or api_call_count
            or estimated_cost_usd
        )

        def _do(conn):
            row = conn.execute(
                "SELECT model, billing_provider, api_call_count FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            existing_model = row["model"] if row is not None else None
            existing_provider = row["billing_provider"] if row is not None else None
            existing_api_calls = int((row["api_call_count"] if row is not None else 0) or 0)

            # Session creation records the requested primary route before any API
            # call. If it fails and fallback succeeds, the first accounted usage
            # event is the first authoritative route. After that, preserve the
            # legacy row: one row cannot represent mixed-provider usage.
            first_accounted_route = (
                existing_api_calls == 0
                and has_accounted_usage
                and bool(model)
                and bool(billing_provider)
                and (existing_model != model or existing_provider != billing_provider)
            )
            if first_accounted_route:
                conn.execute(
                    """UPDATE sessions
                       SET model = ?, billing_provider = ?,
                       billing_base_url = ?, billing_mode = ?
                       WHERE id = ?""",
                    (model, billing_provider, billing_base_url, billing_mode, session_id),
                )
            conn.execute(sql, params)
            if record_model_usage:
                self._record_model_usage(
                    conn,
                    session_id,
                    model=model,
                    billing_provider=billing_provider,
                    billing_base_url=billing_base_url,
                    billing_mode=billing_mode,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_read_tokens=cache_read_tokens,
                    cache_write_tokens=cache_write_tokens,
                    reasoning_tokens=reasoning_tokens,
                    estimated_cost_usd=estimated_cost_usd,
                    actual_cost_usd=actual_cost_usd,
                    cost_status=cost_status,
                    cost_source=cost_source,
                    api_call_count=api_call_count,
                )
        self._execute_write(_do)

    def _record_model_usage(
        self,
        conn,
        session_id: str,
        *,
        model: Optional[str],
        billing_provider: Optional[str],
        billing_base_url: Optional[str],
        billing_mode: Optional[str],
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int,
        cache_write_tokens: int,
        reasoning_tokens: int,
        estimated_cost_usd: Optional[float],
        actual_cost_usd: Optional[float],
        cost_status: Optional[str],
        cost_source: Optional[str],
        api_call_count: int,
        task: str = "",
    ) -> None:
        """Accumulate a per-API-call usage delta into session_model_usage.

        Runs inside the caller's write transaction (after the ``sessions``
        UPDATE) so the per-model rows stay consistent with the summary row.
        When the caller omits the model/provider (some paths only pass token
        deltas), fall back to the values already recorded on the session row —
        the same COALESCE-from-session behaviour the summary update uses.

        ``task`` distinguishes what kind of work consumed the tokens:
        ``''`` (empty) is the main agent loop; auxiliary calls record their
        task name (``vision``, ``compression``, ``title_generation``, ...)
        via :meth:`record_auxiliary_usage` (issue #23270).
        """
        row = conn.execute(
            "SELECT model, billing_provider, billing_base_url, billing_mode "
            "FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        sess_model = row["model"] if row is not None else None
        sess_provider = row["billing_provider"] if row is not None else None
        sess_base_url = row["billing_base_url"] if row is not None else None
        sess_billing_mode = row["billing_mode"] if row is not None else None

        # Aux-task rows (task != '') must NOT inherit the session's main-loop
        # route: an aux call may use a completely different provider/model
        # (vision on gemini while the main loop runs anthropic). Missing info
        # stays 'unknown'/empty rather than borrowing a misleading route.
        if task:
            eff_model = model or "unknown"
            eff_provider = billing_provider or ""
            eff_base_url = billing_base_url or ""
            eff_billing_mode = billing_mode or ""
        else:
            eff_model = model or sess_model or "unknown"
            eff_provider = billing_provider or sess_provider or ""
            eff_base_url = billing_base_url or sess_base_url or ""
            eff_billing_mode = billing_mode or sess_billing_mode or ""
        now = time.time()
        conn.execute(
            """INSERT INTO session_model_usage (
                   session_id, model, billing_provider, billing_base_url, billing_mode,
                   task, api_call_count, input_tokens, output_tokens,
                   cache_read_tokens, cache_write_tokens, reasoning_tokens,
                   estimated_cost_usd, actual_cost_usd, cost_status, cost_source,
                   first_seen, last_seen
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(session_id, model, billing_provider, billing_base_url, billing_mode, task)
               DO UPDATE SET
                   api_call_count = api_call_count + excluded.api_call_count,
                   input_tokens = input_tokens + excluded.input_tokens,
                   output_tokens = output_tokens + excluded.output_tokens,
                   cache_read_tokens = cache_read_tokens + excluded.cache_read_tokens,
                   cache_write_tokens = cache_write_tokens + excluded.cache_write_tokens,
                   reasoning_tokens = reasoning_tokens + excluded.reasoning_tokens,
                   estimated_cost_usd = estimated_cost_usd + excluded.estimated_cost_usd,
                   actual_cost_usd = actual_cost_usd + excluded.actual_cost_usd,
                   cost_status = COALESCE(excluded.cost_status, cost_status),
                   cost_source = COALESCE(excluded.cost_source, cost_source),
                   last_seen = excluded.last_seen""",
            (
                session_id,
                eff_model,
                eff_provider,
                eff_base_url,
                eff_billing_mode,
                task or "",
                api_call_count or 0,
                input_tokens or 0,
                output_tokens or 0,
                cache_read_tokens or 0,
                cache_write_tokens or 0,
                reasoning_tokens or 0,
                float(estimated_cost_usd or 0.0),
                float(actual_cost_usd or 0.0),
                cost_status,
                cost_source,
                now,
                now,
            ),
        )

    def ensure_session(
        self,
        session_id: str,
        source: str = "unknown",
        model: str = None,
        **kwargs,
    ) -> str:
        """Ensure a session row exists (INSERT OR IGNORE). Accepts optional kwargs."""
        self._insert_session_row(session_id, source, model=model, **kwargs)
        return session_id

    def record_auxiliary_usage(
        self,
        session_id: str,
        task: str,
        *,
        model: Optional[str] = None,
        billing_provider: Optional[str] = None,
        billing_base_url: Optional[str] = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        reasoning_tokens: int = 0,
        estimated_cost_usd: Optional[float] = None,
    ) -> None:
        """Record an auxiliary LLM call's usage against *session_id* (issue #23270).

        Auxiliary calls (vision, compression, title_generation, web_extract,
        session_search, ...) historically discarded their usage, leaving the
        dashboard's per-model analytics blind to aux model spend. This writes
        a per-(model, provider, task) delta into ``session_model_usage`` —
        the same table the main loop's ``update_token_counts`` feeds — WITHOUT
        touching the ``sessions`` summary row. That separation is deliberate:
        the gateway overwrites session counters with absolute main-loop totals,
        so folding aux tokens into the summary row would either be clobbered
        or double-counted. Insights/analytics read the union of both.

        Best-effort by contract: callers must never fail an aux call because
        accounting failed.
        """
        if not session_id or not task:
            return
        # FK on session_model_usage.session_id → sessions.id: ensure the row
        # exists (same INSERT OR IGNORE guard update_token_counts uses — the
        # initial create_session() can fail under concurrent SQLite locking).
        self._insert_session_row(session_id, "unknown")

        def _do(conn):
            self._record_model_usage(
                conn,
                session_id,
                model=model,
                billing_provider=billing_provider,
                billing_base_url=billing_base_url,
                billing_mode=None,
                input_tokens=input_tokens or 0,
                output_tokens=output_tokens or 0,
                cache_read_tokens=cache_read_tokens or 0,
                cache_write_tokens=cache_write_tokens or 0,
                reasoning_tokens=reasoning_tokens or 0,
                estimated_cost_usd=estimated_cost_usd,
                actual_cost_usd=None,
                cost_status=None,
                cost_source=None,
                api_call_count=1,
                task=task,
            )
        self._execute_write(_do)

    def prune_empty_ghost_sessions(self, sessions_dir: "Optional[Path]" = None) -> int:
        """Remove empty TUI ghost sessions (no messages, no title, >24hr old)."""
        cutoff = time.time() - 86400  # Only sessions older than 24 hours

        def _do(conn):
            rows = conn.execute("""
                SELECT id FROM sessions
                WHERE source = 'tui'
                  AND title IS NULL
                  AND ended_at IS NOT NULL
                  AND started_at < ?
                  AND NOT EXISTS (
                      SELECT 1 FROM messages WHERE messages.session_id = sessions.id
                  )
            """, (cutoff,)).fetchall()
            ids = [r[0] if isinstance(r, (tuple, list)) else r["id"] for r in rows]
            if ids:
                placeholders = ",".join("?" * len(ids))
                conn.execute(
                    f"DELETE FROM sessions WHERE id IN ({placeholders})", ids
                )
            return ids

        removed_ids = self._execute_write(_do) or []
        # Clean up any on-disk session files (belt-and-suspenders)
        if sessions_dir and removed_ids:
            for sid in removed_ids:
                self._remove_session_files(sessions_dir, sid)
        return len(removed_ids)


    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get a session by ID."""
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            )
            row = cursor.fetchone()
        return dict(row) if row else None

    def resolve_session_id(self, session_id_or_prefix: str) -> Optional[str]:
        """Resolve an exact or uniquely prefixed session ID to the full ID.

        Returns the exact ID when it exists. Otherwise treats the input as a
        prefix and returns the single matching session ID if the prefix is
        unambiguous. Returns None for no matches or ambiguous prefixes.
        """
        exact = self.get_session(session_id_or_prefix)
        if exact:
            return exact["id"]

        escaped = (
            session_id_or_prefix
            .replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        with self._lock:
            cursor = self._conn.execute(
                "SELECT id FROM sessions WHERE id LIKE ? ESCAPE '\\' ORDER BY started_at DESC LIMIT 2",
                (f"{escaped}%",),
            )
            matches = [row["id"] for row in cursor.fetchall()]
        if len(matches) == 1:
            return matches[0]
        return None

    # Maximum length for session titles
    MAX_TITLE_LENGTH = 100

    @staticmethod
    def sanitize_title(title: Optional[str]) -> Optional[str]:
        """Validate and sanitize a session title.

        - Strips leading/trailing whitespace
        - Removes ASCII control characters (0x00-0x1F, 0x7F) and problematic
          Unicode control chars (zero-width, RTL/LTR overrides, etc.)
        - Collapses internal whitespace runs to single spaces
        - Normalizes empty/whitespace-only strings to None
        - Enforces MAX_TITLE_LENGTH

        Returns the cleaned title string or None.
        Raises ValueError if the title exceeds MAX_TITLE_LENGTH after cleaning.
        """
        if not title:
            return None

        # Lone surrogates cannot be bound by sqlite3 (UnicodeEncodeError at
        # UTF-8 encode time) — scrub them like every other write path here.
        title = _sanitize_surrogates(title)

        # Remove ASCII control characters (0x00-0x1F, 0x7F) but keep
        # whitespace chars (\t=0x09, \n=0x0A, \r=0x0D) so they can be
        # normalized to spaces by the whitespace collapsing step below
        cleaned = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', title)

        # Remove problematic Unicode control characters:
        # - Zero-width chars (U+200B-U+200F, U+FEFF)
        # - Directional overrides (U+202A-U+202E, U+2066-U+2069)
        # - Object replacement (U+FFFC), interlinear annotation (U+FFF9-U+FFFB)
        cleaned = re.sub(
            r'[\u200b-\u200f\u2028-\u202e\u2060-\u2069\ufeff\ufffc\ufff9-\ufffb]',
            '', cleaned,
        )

        # Collapse internal whitespace runs and strip
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()

        if not cleaned:
            return None

        if len(cleaned) > SessionDB.MAX_TITLE_LENGTH:
            raise ValueError(
                f"Title too long ({len(cleaned)} chars, max {SessionDB.MAX_TITLE_LENGTH})"
            )

        return cleaned


    def _set_session_title(
        self,
        session_id: str,
        title: str,
        *,
        only_if_empty: bool,
    ) -> bool:
        title = self.sanitize_title(title)

        def _do(conn):
            if only_if_empty:
                current = conn.execute(
                    "SELECT title FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if current is None or current["title"] is not None:
                    return 0

            if title:
                # Check uniqueness (allow the same session to keep its own title)
                cursor = conn.execute(
                    "SELECT id FROM sessions WHERE title = ? AND id != ?",
                    (title, session_id),
                )
                conflict = cursor.fetchone()
                if conflict:
                    conflict_id = conflict["id"]
                    # A compression continuation is the live, projected-forward
                    # head of its conversation; its compressed predecessors are
                    # ended and hidden from the session list (list_sessions_rich
                    # projects roots → tip). When the title that "conflicts" is
                    # held by such a hidden ancestor, the user has no way to free
                    # it — renaming the visible tip back to the base name would
                    # dead-end with "already in use by <session they can't see>".
                    # Treat this as a transfer: move the title off the ancestor
                    # onto the continuation. Uniqueness is preserved (still only
                    # one session carries the exact title) and the parent-link
                    # lineage is untouched.
                    if self._is_compression_ancestor(
                        conn, ancestor_id=conflict_id, descendant_id=session_id
                    ):
                        conn.execute(
                            "UPDATE sessions SET title = NULL WHERE id = ?",
                            (conflict_id,),
                        )
                    else:
                        raise ValueError(
                            f"Title '{title}' is already in use by session {conflict_id}"
                        )
            predicate = " AND title IS NULL" if only_if_empty else ""
            cursor = conn.execute(
                f"UPDATE sessions SET title = ? WHERE id = ?{predicate}",
                (title, session_id),
            )
            return cursor.rowcount

        rowcount = self._execute_write(_do)
        return rowcount > 0

    def set_session_title(self, session_id: str, title: str) -> bool:
        """Set or update a session's title.

        Returns True if session was found and title was set.
        Raises ValueError if title is already in use by another session,
        or if the title fails validation (too long, invalid characters).
        Empty/whitespace-only strings are normalized to None (clearing the title).
        """
        return self._set_session_title(session_id, title, only_if_empty=False)

    def set_auto_title_if_empty(self, session_id: str, title: str) -> bool:
        """Set an auto-generated title only when the current title is NULL.

        The predicate and write run in one transaction so a concurrent manual
        rename cannot be overwritten. Validation and uniqueness behavior match
        :meth:`set_session_title`.
        """
        return self._set_session_title(session_id, title, only_if_empty=True)

    def get_session_title(self, session_id: str) -> Optional[str]:
        """Get the title for a session, or None."""
        with self._lock:
            cursor = self._conn.execute(
                "SELECT title FROM sessions WHERE id = ?", (session_id,)
            )
            row = cursor.fetchone()
        return row["title"] if row else None

    def set_session_archived(self, session_id: str, archived: bool) -> bool:
        """Archive or unarchive a session.

        Archived sessions are hidden from the default session list but keep all
        their messages — this is a soft hide, not a delete. For compression
        chains, archive the whole logical conversation. Desktop lists compression
        roots projected forward to their latest continuation; updating only the
        displayed tip lets the still-unarchived root resurrect it on refresh.
        Returns True when at least one row was updated.
        """
        def _do(conn):
            cursor = conn.execute(
                """
                WITH RECURSIVE
                  ancestors(id) AS (
                    SELECT ?
                    UNION
                    SELECT parent.id
                    FROM ancestors a
                    JOIN sessions child ON child.id = a.id
                    JOIN sessions parent ON parent.id = child.parent_session_id
                    WHERE parent.end_reason = 'compression'
                  ),
                  descendants(id) AS (
                    SELECT ?
                    UNION
                    SELECT child.id
                    FROM descendants d
                    JOIN sessions parent ON parent.id = d.id
                    JOIN sessions child ON child.parent_session_id = parent.id
                    WHERE parent.end_reason = 'compression'
                  ),
                  lineage(id) AS (
                    SELECT id FROM ancestors
                    UNION
                    SELECT id FROM descendants
                  )
                UPDATE sessions
                SET archived = ?
                WHERE id IN (SELECT id FROM lineage)
                """,
                (session_id, session_id, 1 if archived else 0),
            )
            rowcount = cursor.rowcount
            if rowcount is None or rowcount < 0:
                rowcount = conn.execute("SELECT changes()").fetchone()[0]
            return rowcount
        rowcount = self._execute_write(_do)
        return rowcount > 0

    def get_session_by_title(self, title: str) -> Optional[Dict[str, Any]]:
        """Look up a session by exact title. Returns session dict or None."""
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM sessions WHERE title = ?", (title,)
            )
            row = cursor.fetchone()
        return dict(row) if row else None

    def resolve_session_by_title(self, title: str) -> Optional[str]:
        """Resolve a title to a session ID, preferring the latest in a lineage.

        If the exact title exists, returns that session's ID.
        If not, searches for "title #N" variants and returns the latest one.
        If the exact title exists AND numbered variants exist, returns the
        latest numbered variant (the most recent continuation).
        """
        # First try exact match
        exact = self.get_session_by_title(title)

        # Also search for numbered variants: "title #2", "title #3", etc.
        # Escape SQL LIKE wildcards (%, _) in the title to prevent false matches
        escaped = title.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with self._lock:
            cursor = self._conn.execute(
                "SELECT id, title, started_at FROM sessions "
                "WHERE title LIKE ? ESCAPE '\\' ORDER BY started_at DESC",
                (f"{escaped} #%",),
            )
            numbered = cursor.fetchall()

        if numbered:
            # Return the most recent numbered variant
            return numbered[0]["id"]
        elif exact:
            return exact["id"]
        return None

    def get_next_title_in_lineage(self, base_title: str) -> str:
        """Generate the next title in a lineage (e.g., "my session" → "my session #2").

        Strips any existing " #N" suffix to find the base name, then finds
        the highest existing number and increments.
        """
        # Strip existing #N suffix to find the true base
        match = re.match(r'^(.*?) #(\d+)$', base_title)
        if match:
            base = match.group(1)
        else:
            base = base_title

        # Find all existing numbered variants
        # Escape SQL LIKE wildcards (%, _) in the base to prevent false matches
        escaped = base.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with self._lock:
            cursor = self._conn.execute(
                "SELECT title FROM sessions WHERE title = ? OR title LIKE ? ESCAPE '\\'",
                (base, f"{escaped} #%"),
            )
            existing = [row["title"] for row in cursor.fetchall()]

        if not existing:
            return base  # No conflict, use the base name as-is

        # Find the highest number
        max_num = 1  # The unnumbered original counts as #1
        for t in existing:
            m = re.match(r'^.* #(\d+)$', t)
            if m:
                max_num = max(max_num, int(m.group(1)))

        return f"{base} #{max_num + 1}"


    # Columns excluded from compact_rows projections: only the payload-heavy
    # blob no list consumer renders. Everything else — including gateway
    # routing fields and desktop sidebar fields like git_branch — stays, and
    # the projection is derived from SCHEMA_SQL so columns added later via
    # declarative reconciliation are included automatically instead of
    # silently dropping out of list rows.
    _SESSION_COMPACT_EXCLUDED = frozenset({"system_prompt"})
    _session_compact_cols_sql: Optional[str] = None

    @classmethod
    def _compact_session_cols(cls) -> str:
        """SELECT list for compact_rows: every ``sessions`` column declared in
        SCHEMA_SQL except the ``system_prompt`` blob, aliased with the ``s``
        prefix used by list_sessions_rich/_get_session_rich_row queries."""
        if cls._session_compact_cols_sql is None:
            declared = cls._parse_schema_columns(SCHEMA_SQL)["sessions"]
            cls._session_compact_cols_sql = ", ".join(
                f"s.{name}" for name in declared
                if name not in cls._SESSION_COMPACT_EXCLUDED
            )
        return cls._session_compact_cols_sql

    def distinct_session_cwds(self, include_archived: bool = False) -> List[Dict[str, Any]]:
        """Distinct non-empty session cwds with usage stats, for repo discovery.

        Aggregates across ALL session history (not a single page), so the desktop
        can surface every git repo the user has worked in — not just the repos
        that happen to be in the currently-loaded recents. Children/branches
        count: a worktree session is still a real workspace signal.
        """
        where = "cwd IS NOT NULL AND TRIM(cwd) != ''"
        if not include_archived:
            where += " AND archived = 0"
        with self._lock:
            rows = self._conn.execute(
                "SELECT cwd AS cwd, COUNT(*) AS sessions, "
                "MAX(COALESCE(ended_at, started_at, 0)) AS last_active "
                f"FROM sessions WHERE {where} GROUP BY cwd"
            ).fetchall()
        return [
            {
                "cwd": r["cwd"],
                "sessions": int(r["sessions"] or 0),
                "last_active": float(r["last_active"] or 0),
            }
            for r in rows
        ]

    def list_sessions_rich(
        self,
        source: str = None,
        exclude_sources: List[str] = None,
        cwd_prefix: str = None,
        limit: int = 20,
        offset: int = 0,
        include_children: bool = False,
        min_message_count: int = 0,
        project_compression_tips: bool = True,
        order_by_last_active: bool = False,
        include_archived: bool = False,
        archived_only: bool = False,
        id_query: str = None,
        search_query: str = None,
        compact_rows: bool = False,
    ) -> List[Dict[str, Any]]:
        """List sessions with preview (first user message) and last active timestamp.

        Returns dicts with keys: id, source, model, title, started_at, ended_at,
        message_count, preview (first 60 chars of first user message),
        last_active (timestamp of last message).

        Uses a single query with correlated subqueries instead of N+2 queries.

        By default, child sessions (subagent runs, compression continuations)
        are excluded.  Pass ``include_children=True`` to include them.

        With ``project_compression_tips=True`` (default), sessions that are
        roots of compression chains are projected forward to their latest
        continuation — one logical conversation = one list entry, showing the
        live continuation's id/message_count/title/last_active. This prevents
        compressed continuations from being invisible to users while keeping
        delegate subagents and branches hidden. Pass ``False`` to return the
        raw root rows (useful for admin/debug UIs).

        Pass ``order_by_last_active=True`` to sort by most-recent activity
        instead of original conversation start time. For compression chains,
        the "most-recent activity" is taken from the live tip (not the root),
        so an old conversation that was compressed and continued recently
        surfaces in the correct slot. Ordering is computed at SQL level via
        a recursive CTE that walks compression-continuation edges, so LIMIT
        and OFFSET still apply efficiently.

        ``search_query`` matches case-insensitive substrings against each
        surfaced row's title and id (and, like ``id_query``, every title/id in
        its forward compression chain). A punctuation-stripped variant is also
        matched so e.g. ``an94`` finds ``AN-94``. Only honored in the
        ``order_by_last_active`` path.

        Pass ``compact_rows=True`` for dashboard and picker callers that only
        need lightweight metadata. This omits the ``system_prompt`` blob from
        the SELECT so SQLite never copies it out of the B-tree page — a
        significant I/O saving on large databases where the blob routinely
        runs to tens of kilobytes per row.
        """
        where_clauses = []
        params = []

        if not include_children:
            # Show root sessions and branch sessions, while still hiding
            # sub-agent runs and compression continuations (which also carry a
            # parent_session_id but were spawned while the parent was still
            # live — i.e., started_at < parent.ended_at).
            #
            # Branch sessions are identified two ways, OR'd for robustness:
            #   1. A stable ``_branched_from`` marker in model_config, written
            #      by /branch at creation time. This survives the parent being
            #      reopened and re-ended with a different end_reason (e.g.
            #      tui_shutdown overwriting 'branched'), which otherwise hides
            #      the branch — see issue #20856.
            #   2. The legacy heuristic (parent ended with 'branched' before the
            #      child started), covering branch sessions created before the
            #      marker existed.
            where_clauses.append(_LISTABLE_CHILD_SQL)
            where_clauses.append(f"{_delegate_from_json('s.model_config')} IS NULL")

        if source:
            where_clauses.append("s.source = ?")
            params.append(source)
        if exclude_sources:
            placeholders = ",".join("?" for _ in exclude_sources)
            where_clauses.append(f"s.source NOT IN ({placeholders})")
            params.extend(exclude_sources)
        if cwd_prefix:
            clause, clause_params = _cwd_prefix_clause(cwd_prefix)
            where_clauses.append(clause)
            params.extend(clause_params)
        if min_message_count > 0:
            where_clauses.append("s.message_count >= ?")
            params.append(min_message_count)
        if archived_only:
            where_clauses.append("s.archived = 1")
        elif not include_archived:
            where_clauses.append("s.archived = 0")

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

        # Optional session-id filter, pushed into SQL so callers (Desktop
        # session-id search) don't have to fetch every row and filter in
        # Python. ``id_query`` is matched as a case-insensitive substring
        # against each surfaced row's id AND every id in its forward
        # compression chain — so searching a compression *root* id or a *tip*
        # id both resolve to the same projected conversation. Only used in the
        # order_by_last_active path (which builds the chain CTE); other callers
        # pass id_query=None.
        id_needle = (id_query or "").strip().lower()
        search_needle = (search_query or "").strip().lower()
        if order_by_last_active:
            # Compute effective_last_active by walking each surfaced session's
            # compression-continuation chain forward in SQL and taking the MAX
            # timestamp across the chain. This lets us ORDER BY + LIMIT at SQL
            # level instead of fetching every row and sorting in Python, while
            # still surfacing old compression roots whose live tip is fresh.
            #
            # The CTE seeds from rows the outer WHERE admits (roots + branch
            # children), then recursively joins forward through robust
            # compression-continuation edges. Do NOT require
            # child.started_at >= parent.ended_at here: real desktop/gateway
            # races can insert the continuation row before the parent's
            # ended_at is written, while stale websocket siblings may satisfy
            # the timestamp test and hijack resume/list projection.
            outer_where = where_sql
            id_params: List[Any] = []
            filter_clauses: List[str] = []

            def _like_pattern(needle: str) -> str:
                escaped = (
                    needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                )
                return f"%{escaped}%"

            if id_needle:
                # Admit a surfaced row if its own id or any id in its forward
                # compression chain matches the needle. LIKE with a leading
                # wildcard can't use an index, but the chain membership and
                # the small result set keep this bounded — far cheaper than
                # fetching every session and scanning in Python.
                filter_clauses.append(
                    "EXISTS (SELECT 1 FROM chain cq"
                    "        WHERE cq.root_id = s.id"
                    "          AND LOWER(cq.cur_id) LIKE ? ESCAPE '\\')"
                )
                id_params.append(_like_pattern(id_needle))
            if search_needle:
                # Same chain-membership trick as id_query, but matching either
                # the title or the id of any session in the chain. The compact
                # (punctuation-stripped) variant lets `an94` match `AN-94`.
                compact_needle = re.sub(r"[\W_]+", "", search_needle)
                compact_sql = (
                    "REPLACE(REPLACE(REPLACE(REPLACE(LOWER(COALESCE({0}, '')),"
                    " '-', ''), '_', ''), '.', ''), ' ', '')"
                )
                search_clause = (
                    "EXISTS (SELECT 1 FROM chain cq"
                    " JOIN sessions cs ON cs.id = cq.cur_id"
                    " WHERE cq.root_id = s.id"
                    " AND (LOWER(COALESCE(cs.title, '')) LIKE ? ESCAPE '\\'"
                    " OR LOWER(cq.cur_id) LIKE ? ESCAPE '\\'"
                )
                id_params.extend([_like_pattern(search_needle)] * 2)
                if compact_needle:
                    search_clause += (
                        f" OR {compact_sql.format('cs.title')} LIKE ? ESCAPE '\\'"
                    )
                    id_params.append(_like_pattern(compact_needle))
                filter_clauses.append(search_clause + "))")
            if filter_clauses:
                combined = " AND ".join(filter_clauses)
                outer_where = (
                    f"{where_sql} AND {combined}" if where_sql else f"WHERE {combined}"
                )
            _sel = self._compact_session_cols() if compact_rows else "s.*"
            query = f"""
                WITH RECURSIVE chain(root_id, cur_id) AS (
                    SELECT s.id, s.id FROM sessions s {where_sql}
                    UNION ALL
                    SELECT c.root_id, child.id
                    FROM chain c
                    JOIN sessions parent ON parent.id = c.cur_id
                    JOIN sessions child ON child.parent_session_id = c.cur_id
                    WHERE parent.end_reason = 'compression'
                      AND json_extract(COALESCE(child.model_config, '{{}}'), '$._branched_from') IS NULL
                      AND json_extract(COALESCE(child.model_config, '{{}}'), '$._delegate_from') IS NULL
                      AND COALESCE(child.source, '') != 'tool'
                ),
                chain_max AS (
                    SELECT
                        root_id,
                        MAX(COALESCE(
                            (SELECT MAX(m.timestamp) FROM messages m WHERE m.session_id = cur_id),
                            (SELECT started_at FROM sessions ss WHERE ss.id = cur_id)
                        )) AS effective_last_active
                    FROM chain
                    GROUP BY root_id
                )
                SELECT {_sel},
                    COALESCE(
                        (SELECT SUBSTR(REPLACE(REPLACE(m.content, X'0A', ' '), X'0D', ' '), 1, 63)
                         FROM messages m
                         WHERE m.session_id = s.id AND m.role = 'user' AND m.content IS NOT NULL
                         ORDER BY m.timestamp, m.id LIMIT 1),
                        ''
                    ) AS _preview_raw,
                    COALESCE(
                        (SELECT MAX(m2.timestamp) FROM messages m2 WHERE m2.session_id = s.id),
                        s.started_at
                    ) AS last_active,
                    COALESCE(cm.effective_last_active, s.started_at) AS _effective_last_active
                FROM sessions s
                LEFT JOIN chain_max cm ON cm.root_id = s.id
                {outer_where}
                ORDER BY _effective_last_active DESC, s.started_at DESC, s.id DESC
                LIMIT ? OFFSET ?
            """
            # WHERE params apply twice (CTE seed + outer select); the id filter
            # only applies to the outer select.
            params = params + params + id_params + [limit, offset]
        else:
            _sel = self._compact_session_cols() if compact_rows else "s.*"
            query = f"""
                SELECT {_sel},
                    COALESCE(
                        (SELECT SUBSTR(REPLACE(REPLACE(m.content, X'0A', ' '), X'0D', ' '), 1, 63)
                         FROM messages m
                         WHERE m.session_id = s.id AND m.role = 'user' AND m.content IS NOT NULL
                         ORDER BY m.timestamp, m.id LIMIT 1),
                        ''
                    ) AS _preview_raw,
                    COALESCE(
                        (SELECT MAX(m2.timestamp) FROM messages m2 WHERE m2.session_id = s.id),
                        s.started_at
                    ) AS last_active
                FROM sessions s
                {where_sql}
                ORDER BY s.started_at DESC
                LIMIT ? OFFSET ?
            """
            params.extend([limit, offset])
        with self._lock:
            cursor = self._conn.execute(query, params)
            rows = cursor.fetchall()
        sessions = []
        for row in rows:
            s = dict(row)
            # Build the preview from the raw substring
            raw = s.pop("_preview_raw", "").strip()
            if raw:
                text = raw[:60]
                s["preview"] = text + ("..." if len(raw) > 60 else "")
            else:
                s["preview"] = ""
            # Drop the internal ordering column so callers see a clean dict.
            s.pop("_effective_last_active", None)
            sessions.append(s)

        # Project compression roots forward to their tips. Each row whose
        # end_reason is 'compression' has a continuation child; replace the
        # surfaced fields (id, message_count, title, last_active, ended_at,
        # end_reason, preview) with the tip's values so the list entry acts
        # as the live conversation. Keep the root's started_at to preserve
        # chronological ordering by original conversation start.
        if project_compression_tips and not include_children:
            projected = []
            for s in sessions:
                if s.get("end_reason") != "compression":
                    projected.append(s)
                    continue
                tip_id = self.get_compression_tip(s["id"])
                if tip_id == s["id"]:
                    projected.append(s)
                    continue
                tip_row = self._get_session_rich_row(tip_id, compact_rows=compact_rows)
                if not tip_row:
                    projected.append(s)
                    continue
                # Preserve the root's started_at for stable sort order, but
                # surface the tip's identity and activity data.
                merged = dict(s)
                for key in (
                    "id", "ended_at", "end_reason", "message_count",
                    "tool_call_count", "title", "last_active", "preview",
                    "model", "system_prompt", "cwd", "git_branch", "git_repo_root",
                ):
                    if key in tip_row:
                        merged[key] = tip_row[key]
                merged["_lineage_root_id"] = s["id"]
                projected.append(merged)
            sessions = projected

        return sessions

    def list_cron_job_runs(
        self,
        job_id: str,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """List the run sessions produced by a single cron job, newest first.

        Cron runs are flat, independent sessions whose id is
        ``cron_{job_id}_{timestamp}`` (see ``cron/scheduler.run_job``). They are
        never compression roots and never branch, so this deliberately skips the
        ``list_sessions_rich`` recursive compression-chain CTE / leading-wildcard
        ``id_query`` path — that path seeds from *every* ``source='cron'`` row in
        the DB and only filters to one job's runs after the scan, so it scales
        with the whole cron pile (a heavy history makes the desktop run-history
        endpoint time out before it eventually populates).

        Instead this binds to one job with a ``[prefix, prefix_hi)`` range over
        the id (an index range scan, not a ``%...%`` substring), filters
        ``source='cron'``, and orders by ``started_at DESC``. Work scales with
        the requested window, not the total cron history.

        Returns the same enriched row shape as ``list_sessions_rich`` (adds
        ``preview`` + ``last_active``) so callers can reuse it.
        """
        prefix = f"cron_{job_id}_"
        # Half-open upper bound for an index range scan: increment the final
        # byte of the prefix so the range covers exactly the ids that start
        # with ``prefix`` and nothing else. ``prefix`` always ends in '_', but
        # compute it generically rather than hardcoding the successor char.
        prefix_hi = prefix[:-1] + chr(ord(prefix[-1]) + 1)

        query = """
            SELECT s.*,
                COALESCE(
                    (SELECT SUBSTR(REPLACE(REPLACE(m.content, X'0A', ' '), X'0D', ' '), 1, 63)
                     FROM messages m
                     WHERE m.session_id = s.id AND m.role = 'user' AND m.content IS NOT NULL
                     ORDER BY m.timestamp, m.id LIMIT 1),
                    ''
                ) AS _preview_raw,
                COALESCE(
                    (SELECT MAX(m2.timestamp) FROM messages m2 WHERE m2.session_id = s.id),
                    s.started_at
                ) AS last_active
            FROM sessions s
            WHERE s.source = 'cron' AND s.id >= ? AND s.id < ?
            ORDER BY s.started_at DESC, s.id DESC
            LIMIT ? OFFSET ?
        """
        with self._lock:
            cursor = self._conn.execute(query, (prefix, prefix_hi, limit, offset))
            rows = cursor.fetchall()

        runs: List[Dict[str, Any]] = []
        for row in rows:
            s = dict(row)
            raw = s.pop("_preview_raw", "").strip()
            if raw:
                text = raw[:60]
                s["preview"] = text + ("..." if len(raw) > 60 else "")
            else:
                s["preview"] = ""
            runs.append(s)
        return runs

    def _get_session_rich_row(self, session_id: str, compact_rows: bool = False) -> Optional[Dict[str, Any]]:
        """Fetch a single session with the same enriched columns as
        ``list_sessions_rich`` (preview + last_active). Returns None if the
        session doesn't exist.

        Pass ``compact_rows=True`` to omit the ``system_prompt`` blob (see
        ``list_sessions_rich`` for details).
        """
        _sel = self._compact_session_cols() if compact_rows else "s.*"
        query = f"""
            SELECT {_sel},
                COALESCE(
                    (SELECT SUBSTR(REPLACE(REPLACE(m.content, X'0A', ' '), X'0D', ' '), 1, 63)
                     FROM messages m
                     WHERE m.session_id = s.id AND m.role = 'user' AND m.content IS NOT NULL
                     ORDER BY m.timestamp, m.id LIMIT 1),
                    ''
                ) AS _preview_raw,
                COALESCE(
                    (SELECT MAX(m2.timestamp) FROM messages m2 WHERE m2.session_id = s.id),
                    s.started_at
                ) AS last_active
            FROM sessions s
            WHERE s.id = ?
        """
        with self._lock:
            cursor = self._conn.execute(query, (session_id,))
            row = cursor.fetchone()
        if not row:
            return None
        s = dict(row)
        raw = s.pop("_preview_raw", "").strip()
        if raw:
            text = raw[:60]
            s["preview"] = text + ("..." if len(raw) > 60 else "")
        else:
            s["preview"] = ""
        return s

    # =========================================================================
    # Message storage
    # =========================================================================

    # Sentinel prefix used to distinguish JSON-encoded structured content
    # (multimodal messages: lists of parts like text + image_url) from plain
    # string content. The NUL byte is not legal in normal text, so this
    # cannot collide with real user content.
    _CONTENT_JSON_PREFIX = "\x00json:"














    # Columns every conversation projection decodes. Shared by
    # get_messages_as_conversation and get_resume_conversations so a single
    # SELECT can feed both the model-fed and display views.
    _CONVERSATION_ROW_COLUMNS = (
        "role, content, tool_call_id, tool_calls, tool_name, effect_disposition, "
        "finish_reason, reasoning, reasoning_content, reasoning_details, "
        "codex_reasoning_items, codex_message_items, platform_message_id, observed, timestamp, "
        "api_content"
    )







    # =========================================================================
    # Rewind (soft-delete) — see /rewind slash command + issue #21910
    # =========================================================================




    # =========================================================================
    # Search
    # =========================================================================















    # =========================================================================
    # Utility
    # =========================================================================

    def session_count(
        self,
        source: str = None,
        cwd_prefix: str = None,
        min_message_count: int = 0,
        include_archived: bool = False,
        archived_only: bool = False,
        exclude_children: bool = False,
        exclude_sources: List[str] = None,
    ) -> int:
        """Count sessions, optionally filtered by source.

        Pass ``exclude_children=True`` to count only the conversations that
        ``list_sessions_rich`` surfaces (root + branch sessions), hiding
        sub-agent runs and compression continuations. Use it whenever the count
        is paired with a ``list_sessions_rich`` page (e.g. sidebar "load more"
        totals) so the total matches the number of listable rows — otherwise the
        raw row count is inflated by children and "load more" never settles.

        Pass ``exclude_sources`` to drop whole source classes from the count
        (e.g. ``["cron"]`` so the recents "load more" total matches a
        cron-excluded ``list_sessions_rich`` page and doesn't keep "load more"
        stuck on for buried scheduler sessions).
        """
        where_clauses = []
        params = []

        if exclude_children:
            # Mirror list_sessions_rich's child-exclusion clause exactly so the
            # count lines up with the rows: roots (no parent) plus branch
            # children (parent ended with end_reason='branched').
            where_clauses.append(_LISTABLE_CHILD_SQL)
            where_clauses.append(f"{_delegate_from_json('s.model_config')} IS NULL")
        if source:
            where_clauses.append("s.source = ?")
            params.append(source)
        if exclude_sources:
            placeholders = ",".join("?" for _ in exclude_sources)
            where_clauses.append(f"s.source NOT IN ({placeholders})")
            params.extend(exclude_sources)
        if cwd_prefix:
            clause, clause_params = _cwd_prefix_clause(cwd_prefix)
            where_clauses.append(clause)
            params.extend(clause_params)
        if min_message_count > 0:
            where_clauses.append("s.message_count >= ?")
            params.append(min_message_count)
        if archived_only:
            where_clauses.append("s.archived = 1")
        elif not include_archived:
            where_clauses.append("s.archived = 0")

        where_sql = f" WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

        with self._lock:
            cursor = self._conn.execute(f"SELECT COUNT(*) FROM sessions s{where_sql}", params)
            return cursor.fetchone()[0]



    # =========================================================================
    # Export and cleanup
    # =========================================================================

    def _is_branch_child_row(self, session: Dict[str, Any]) -> bool:
        raw = session.get("model_config")
        if not raw:
            return False
        try:
            cfg = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, json.JSONDecodeError):
            return False
        return isinstance(cfg, dict) and cfg.get("_branched_from") is not None















    @staticmethod
    def _remove_session_files(sessions_dir: Optional[Path], session_id: str) -> None:
        """Remove on-disk transcript files for a session.

        Cleans up ``{session_id}.json``, ``{session_id}.jsonl``, and any
        ``request_dump_{session_id}_*.json`` files left by the gateway.
        Silently skips files that don't exist and swallows OSError so a
        filesystem hiccup never blocks a DB operation.
        """
        if sessions_dir is None:
            return
        for suffix in (".json", ".jsonl"):
            p = sessions_dir / f"{session_id}{suffix}"
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
        # request_dump files use session_id as a prefix component
        try:
            for p in sessions_dir.glob(f"request_dump_{session_id}_*.json"):
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass
        except OSError:
            pass

    def delete_session(
        self,
        session_id: str,
        sessions_dir: Optional[Path] = None,
    ) -> bool:
        """Delete a session and all its messages.

        Delegate subagent children (``model_config._delegate_from``) are
        cascade-deleted with the parent so they never resurface in session
        pickers as orphaned rows. Branch / compression children are orphaned
        (``parent_session_id → NULL``) so they remain accessible independently.
        When *sessions_dir* is provided, also removes on-disk transcript
        files (``.json`` / ``.jsonl`` / ``request_dump_*``) for every deleted
        session. Returns True if the session was found and deleted.
        """
        removed_delegate_ids: List[str] = []

        def _do(conn):
            cursor = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE id = ?", (session_id,)
            )
            if cursor.fetchone()[0] == 0:
                return False
            removed_delegate_ids.extend(_delete_delegate_children(conn, [session_id]))
            # Orphan remaining child sessions (branches, etc.) so FK is satisfied.
            conn.execute(
                "UPDATE sessions SET parent_session_id = NULL "
                "WHERE parent_session_id = ?",
                (session_id,),
            )
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            return True

        deleted = self._execute_write(_do)
        if deleted:
            for delegate_id in removed_delegate_ids:
                self._remove_session_files(sessions_dir, delegate_id)
            self._remove_session_files(sessions_dir, session_id)
        return bool(deleted)

    def delete_session_if_empty(
        self,
        session_id: str,
        sessions_dir: Optional[Path] = None,
    ) -> bool:
        """Delete *session_id* only when it never gained resumable content.

        A session is considered empty when it has no messages and no
        user-assigned title. Used by CLI exit / session-rotation paths so
        immediately-started-and-quit sessions don't pile up in ``/resume``
        and ``opencodon sessions list`` output. (Pattern ported from
        google-gemini/gemini-cli#27770.)

        The emptiness check and delete run in one transaction, so a message
        flushed concurrently by another writer can't be lost. Sessions with
        children (delegate subagent runs) are preserved — a parent that
        spawned work is not "empty" even if its own transcript never
        flushed. Returns True if the session was deleted.
        """
        def _do(conn):
            cursor = conn.execute(
                """
                DELETE FROM sessions
                WHERE id = ?
                  AND title IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM messages WHERE messages.session_id = sessions.id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM sessions child
                      WHERE child.parent_session_id = sessions.id
                  )
                """,
                (session_id,),
            )
            return cursor.rowcount > 0

        deleted = self._execute_write(_do)
        if deleted:
            self._remove_session_files(sessions_dir, session_id)
        return bool(deleted)

    def delete_sessions(
        self,
        session_ids: List[str],
        sessions_dir: Optional[Path] = None,
    ) -> int:
        """Delete every session in *session_ids* in a single transaction.

        Backs the dashboard's bulk-select-then-delete flow on the
        sessions page (``POST /api/sessions/bulk-delete``). Mirrors the
        single-session :meth:`delete_session` contract per row:

        * Unknown IDs are silently skipped (no 404) — selection state
          in the UI can race against another tab's delete, and we'd
          rather succeed-on-the-rest than fail-the-whole-batch.
        * Delegate subagent children (``model_config._delegate_from``) are
          cascade-deleted with their parent; branch children are orphaned
          (``parent_session_id → NULL``) so they stay accessible.
        * Messages and the session row both go in one
          ``_execute_write`` call so a partial failure can't leave the
          DB in a "messages gone but session row still there" state.
        * On-disk transcript / ``request_dump_*`` files are cleaned up
          outside the DB transaction when *sessions_dir* is provided,
          matching :meth:`prune_sessions` and
          :meth:`delete_empty_sessions`.

        Returns the count of sessions that actually existed and were
        deleted (may be less than ``len(session_ids)`` if some IDs were
        already gone).
        """
        if not session_ids:
            return 0
        # Dedup + drop any non-string entries up-front. Avoids
        # double-counting in the WHERE-IN list and protects against
        # callers that pass a list with stray ``None`` values.
        unique_ids = list({sid for sid in session_ids if isinstance(sid, str) and sid})
        if not unique_ids:
            return 0

        removed_ids: list[str] = []
        removed_delegate_ids: list[str] = []

        def _do(conn):
            placeholders = ",".join("?" * len(unique_ids))
            # First, filter to IDs that actually exist — we want to
            # return the real deleted count, not the input length.
            cursor = conn.execute(
                f"SELECT id FROM sessions WHERE id IN ({placeholders})",
                unique_ids,
            )
            existing = [row["id"] for row in cursor.fetchall()]
            if not existing:
                return 0

            existing_placeholders = ",".join("?" * len(existing))
            removed_delegate_ids.extend(_delete_delegate_children(conn, existing))
            # Orphan remaining children whose parent is in the kill list so the
            # FK constraint stays satisfied. Pin children whose parent
            # is itself in the kill list rather than NULL-ing parents
            # of survivors — the IN list on ``parent_session_id`` does
            # exactly this.
            conn.execute(
                f"UPDATE sessions SET parent_session_id = NULL "
                f"WHERE parent_session_id IN ({existing_placeholders})",
                existing,
            )
            conn.execute(
                f"DELETE FROM messages WHERE session_id IN ({existing_placeholders})",
                existing,
            )
            conn.execute(
                f"DELETE FROM sessions WHERE id IN ({existing_placeholders})",
                existing,
            )
            removed_ids.extend(existing)
            return len(existing)

        count = self._execute_write(_do)
        for sid in removed_delegate_ids:
            self._remove_session_files(sessions_dir, sid)
        for sid in removed_ids:
            self._remove_session_files(sessions_dir, sid)
        return count

    def count_empty_sessions(self) -> int:
        """Return the count of empty, non-active, non-archived sessions.

        "Empty" = ``message_count = 0`` AND the session has ended
        (``ended_at IS NOT NULL``) AND is not archived. The ``ended_at``
        guard matches the safety contract used by :meth:`prune_sessions`:
        only ended sessions are candidates for bulk deletion, so a freshly
        spawned session whose first message hasn't landed yet — or one
        held open by the live agent — is never sniped out from under
        the runtime.

        Backs the ``GET /api/sessions/empty/count`` endpoint that lets the
        web dashboard hide its "Delete empty" button when there's nothing
        to clean up, and pre-populate the confirm dialog with the actual
        count.
        """
        with self._lock:
            cursor = self._conn.execute(
                "SELECT COUNT(*) FROM sessions "
                "WHERE message_count = 0 "
                "AND ended_at IS NOT NULL "
                "AND archived = 0"
            )
            return cursor.fetchone()[0]

    def delete_empty_sessions(
        self,
        sessions_dir: Optional[Path] = None,
    ) -> int:
        """Delete every empty, ended, non-archived session.

        Mirrors :meth:`prune_sessions`' transactional shape:

        * Selects candidate IDs first (``message_count = 0`` AND
          ``ended_at IS NOT NULL`` AND ``archived = 0``) so we never
          touch a live session or one the user deliberately archived.
        * Orphans any child whose parent is in the kill list — children
          of an empty parent are kept and re-parented to ``NULL`` rather
          than cascade-deleted, matching ``delete_session`` /
          ``prune_sessions`` semantics so branch/subagent transcripts
          survive an inadvertent parent cleanup.
        * Deletes the rows in a single ``_execute_write`` callback so
          the operation is atomic — a partial failure (e.g. SIGKILL
          mid-loop) doesn't leave the DB in a "messages-deleted but
          session-row-still-there" half-state.
        * Cleans up on-disk transcript files (``.json`` / ``.jsonl`` /
          ``request_dump_*``) outside the DB transaction when
          ``sessions_dir`` is provided. Empty sessions don't typically
          have transcript files, but the gateway can leave a stub
          ``request_dump_*`` if it crashed before the first reply —
          so we still sweep, matching ``prune_sessions``.

        Returns the number of sessions deleted.
        """
        removed_ids: list[str] = []

        def _do(conn):
            cursor = conn.execute(
                "SELECT id FROM sessions "
                "WHERE message_count = 0 "
                "AND ended_at IS NOT NULL "
                "AND archived = 0"
            )
            session_ids = {row["id"] for row in cursor.fetchall()}

            if not session_ids:
                return 0

            placeholders = ",".join("?" * len(session_ids))
            conn.execute(
                f"UPDATE sessions SET parent_session_id = NULL "
                f"WHERE parent_session_id IN ({placeholders})",
                list(session_ids),
            )

            for sid in session_ids:
                # DELETE FROM messages is paranoia — by construction
                # these rows have ``message_count = 0`` — but if a
                # bookkeeping bug ever lets the counter drift below the
                # real row count, we still leave a clean FK state.
                conn.execute(
                    "DELETE FROM messages WHERE session_id = ?", (sid,)
                )
                conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))
                removed_ids.append(sid)
            return len(session_ids)

        count = self._execute_write(_do)
        for sid in removed_ids:
            self._remove_session_files(sessions_dir, sid)
        return count

    @staticmethod
    def _prune_filter_where(
        *,
        started_before: Optional[float] = None,
        started_after: Optional[float] = None,
        source: Optional[str] = None,
        title_like: Optional[str] = None,
        end_reason: Optional[str] = None,
        cwd_prefix: Optional[str] = None,
        min_messages: Optional[int] = None,
        max_messages: Optional[int] = None,
        archived: Optional[bool] = None,
        model_like: Optional[str] = None,
        provider: Optional[str] = None,
        user_id: Optional[str] = None,
        chat_id: Optional[str] = None,
        chat_type: Optional[str] = None,
        branch_like: Optional[str] = None,
        min_tokens: Optional[int] = None,
        max_tokens: Optional[int] = None,
        min_cost: Optional[float] = None,
        max_cost: Optional[float] = None,
        min_tool_calls: Optional[int] = None,
        max_tool_calls: Optional[int] = None,
    ) -> Tuple[str, list]:
        """Build the shared WHERE clause for bulk prune/archive selection.

        All filters AND together. Only ended sessions are ever candidates
        (``ended_at IS NOT NULL``) so a live session is never selected.
        ``archived`` is a tri-state: ``None`` = both, ``True`` = only
        archived rows, ``False`` = only unarchived rows.

        String matching conventions: ``model_like`` / ``branch_like`` /
        ``title_like`` are case-insensitive substring matches (model slugs
        and branch names vary in prefix format); ``provider`` / ``user_id``
        / ``chat_id`` / ``chat_type`` / ``source`` / ``end_reason`` are
        exact (case-insensitive for provider). Token bounds apply to
        ``input_tokens + output_tokens``; cost bounds apply to
        ``COALESCE(actual_cost_usd, estimated_cost_usd)``.

        The clause references the ``s`` table alias — callers must select
        ``FROM sessions s``.
        """
        clauses = ["s.ended_at IS NOT NULL"]
        params: list = []
        if started_before is not None:
            clauses.append("s.started_at < ?")
            params.append(started_before)
        if started_after is not None:
            clauses.append("s.started_at >= ?")
            params.append(started_after)
        if source:
            clauses.append("s.source = ?")
            params.append(source)
        if title_like:
            clauses.append("LOWER(COALESCE(s.title, '')) LIKE ?")
            params.append(f"%{title_like.lower()}%")
        if end_reason:
            clauses.append("s.end_reason = ?")
            params.append(end_reason)
        if cwd_prefix:
            clause, clause_params = _cwd_prefix_clause(cwd_prefix)
            clauses.append(clause)
            params.extend(clause_params)
        if min_messages is not None:
            clauses.append("s.message_count >= ?")
            params.append(min_messages)
        if max_messages is not None:
            clauses.append("s.message_count <= ?")
            params.append(max_messages)
        if model_like:
            clauses.append("LOWER(COALESCE(s.model, '')) LIKE ?")
            params.append(f"%{model_like.lower()}%")
        if provider:
            clauses.append("LOWER(COALESCE(s.billing_provider, '')) = ?")
            params.append(provider.lower())
        if user_id:
            clauses.append("s.user_id = ?")
            params.append(user_id)
        if chat_id:
            clauses.append("s.chat_id = ?")
            params.append(chat_id)
        if chat_type:
            clauses.append("s.chat_type = ?")
            params.append(chat_type)
        if branch_like:
            clauses.append("LOWER(COALESCE(s.git_branch, '')) LIKE ?")
            params.append(f"%{branch_like.lower()}%")
        if min_tokens is not None:
            clauses.append(
                "(COALESCE(s.input_tokens, 0) + COALESCE(s.output_tokens, 0)) >= ?"
            )
            params.append(min_tokens)
        if max_tokens is not None:
            clauses.append(
                "(COALESCE(s.input_tokens, 0) + COALESCE(s.output_tokens, 0)) <= ?"
            )
            params.append(max_tokens)
        if min_cost is not None:
            clauses.append(
                "COALESCE(s.actual_cost_usd, s.estimated_cost_usd, 0) >= ?"
            )
            params.append(min_cost)
        if max_cost is not None:
            clauses.append(
                "COALESCE(s.actual_cost_usd, s.estimated_cost_usd, 0) <= ?"
            )
            params.append(max_cost)
        if min_tool_calls is not None:
            clauses.append("COALESCE(s.tool_call_count, 0) >= ?")
            params.append(min_tool_calls)
        if max_tool_calls is not None:
            clauses.append("COALESCE(s.tool_call_count, 0) <= ?")
            params.append(max_tool_calls)
        if archived is True:
            clauses.append("s.archived = 1")
        elif archived is False:
            clauses.append("s.archived = 0")
        return " AND ".join(clauses), params

    def list_prune_candidates(
        self,
        older_than_days: Optional[float] = None,
        source: str = None,
        **filters,
    ) -> List[Dict[str, Any]]:
        """Return the sessions a matching :meth:`prune_sessions` /
        :meth:`archive_sessions` call would touch, without modifying anything.

        Backs ``--dry-run`` and pre-confirmation counts. Accepts the same
        keyword filters as :meth:`_prune_filter_where` (unknown names raise
        ``TypeError`` there). Rows are ordered oldest-first and carry
        ``id, source, title, model, started_at, ended_at, message_count,
        archived``.
        """
        if filters.get("started_before") is None and older_than_days is not None:
            filters["started_before"] = time.time() - (older_than_days * 86400)
        where, params = self._prune_filter_where(source=source, **filters)
        with self._lock:
            cursor = self._conn.execute(
                f"""SELECT s.id, s.source, s.title, s.model, s.started_at,
                           s.ended_at, s.message_count, s.archived
                    FROM sessions s WHERE {where}
                    ORDER BY s.started_at ASC""",
                params,
            )
            return [dict(row) for row in cursor.fetchall()]

    def archive_sessions(
        self,
        older_than_days: Optional[float] = None,
        source: str = None,
        **filters,
    ) -> int:
        """Bulk-archive (soft-hide) every session matching the filters.

        Same filter surface as :meth:`prune_sessions`, but instead of deleting
        rows it flips ``archived = 1`` via :meth:`set_session_archived` so
        each match's compression lineage is archived as a unit (an unarchived
        compression root would otherwise resurrect the conversation in
        Desktop's projected list). Nothing is deleted; messages and transcript
        files are untouched. Returns the number of sessions matched.

        ``archived`` defaults to ``False`` here (only select rows not yet
        archived) so repeat runs are idempotent no-ops.
        """
        filters.setdefault("archived", False)
        rows = self.list_prune_candidates(
            older_than_days=older_than_days, source=source, **filters
        )
        for row in rows:
            self.set_session_archived(row["id"], True)
        return len(rows)

    def prune_sessions(
        self,
        older_than_days: Optional[float] = 90,
        source: str = None,
        sessions_dir: Optional[Path] = None,
        **filters,
    ) -> int:
        """Delete sessions matching the filters. Returns count deleted.

        Default behavior (no keyword filters) is unchanged: delete ended
        sessions older than ``older_than_days`` days, optionally restricted
        to ``source``. Additional keyword filters AND together — the full
        set is defined by :meth:`_prune_filter_where`:

        * ``started_before`` / ``started_after`` — epoch bounds on
          ``started_at``. ``started_before`` overrides ``older_than_days``;
          pass ``older_than_days=None`` for no upper age bound (e.g. when
          only pruning a recent window via ``started_after``).
        * ``title_like`` / ``model_like`` / ``branch_like`` —
          case-insensitive substring matches.
        * ``end_reason`` / ``provider`` / ``user_id`` / ``chat_id`` /
          ``chat_type`` — exact matches (provider case-insensitive, against
          ``billing_provider``).
        * ``cwd_prefix`` — session cwd equals or is under this path.
        * ``min_messages`` / ``max_messages`` — bounds on message_count.
        * ``min_tokens`` / ``max_tokens`` — bounds on input+output tokens.
        * ``min_cost`` / ``max_cost`` — bounds on USD cost
          (actual, falling back to estimated).
        * ``min_tool_calls`` / ``max_tool_calls`` — bounds on tool_call_count.
        * ``archived`` — tri-state: None = both (default), True = only
          archived, False = only unarchived.

        Only prunes ended sessions (not active ones).  Child sessions outside
        the prune window are orphaned (parent_session_id set to NULL) rather
        than cascade-deleted.  When *sessions_dir* is provided, also removes
        on-disk transcript files (``.json`` / ``.jsonl`` /
        ``request_dump_*``) for every pruned session, outside the DB
        transaction.
        """
        if filters.get("started_before") is None and older_than_days is not None:
            filters["started_before"] = time.time() - (older_than_days * 86400)
        where, where_params = self._prune_filter_where(source=source, **filters)
        removed_ids: list[str] = []

        def _do(conn):
            cursor = conn.execute(
                f"SELECT s.id FROM sessions s WHERE {where}", where_params
            )
            session_ids = {row["id"] for row in cursor.fetchall()}

            if not session_ids:
                return 0

            # Orphan any sessions whose parent is about to be deleted
            placeholders = ",".join("?" * len(session_ids))
            conn.execute(
                f"UPDATE sessions SET parent_session_id = NULL "
                f"WHERE parent_session_id IN ({placeholders})",
                list(session_ids),
            )

            for sid in session_ids:
                conn.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
                conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))
                removed_ids.append(sid)
            return len(session_ids)

        count = self._execute_write(_do)
        # Clean up on-disk files outside the DB transaction
        for sid in removed_ids:
            self._remove_session_files(sessions_dir, sid)
        return count

    # ── Meta key/value (for scheduler bookkeeping) ──

    def get_meta(self, key: str) -> Optional[str]:
        """Read a value from the state_meta key/value store."""
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM state_meta WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        return row["value"] if isinstance(row, sqlite3.Row) else row[0]

    def set_meta(
        self, key: str, value: str, *, cursor: Optional[sqlite3.Cursor] = None
    ) -> None:
        """Write a value to the state_meta key/value store.

        When ``cursor`` is provided the write is issued on that cursor
        inline (used during ``_init_schema``, which already holds an open
        transaction — routing through ``_execute_write`` there would nest
        BEGIN IMMEDIATE and deadlock). Otherwise a normal write transaction
        is used.
        """
        if cursor is not None:
            cursor.execute(
                "INSERT INTO state_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            return

        def _do(conn):
            conn.execute(
                "INSERT INTO state_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
        self._execute_write(_do)












    # ── Space reclamation ──

    # FTS5 virtual tables whose b-tree segments we merge on optimize. The
    # trigram table is created lazily / may be disabled, and the cjk-bigram
    # table only exists (and is only queryable) when the loadable tokenizer
    # is present — so we probe each before touching it (see optimize_fts).
    _FTS_TABLES = ("messages_fts", "messages_fts_trigram", "messages_fts_cjk")






    # ── Handoff (cross-platform session transfer) ──────────────────────────
    #
    # State machine:
    #   None       — no handoff in flight
    #   "pending"  — CLI requested handoff, gateway hasn't picked it up yet
    #   "running"  — gateway is processing (session switch + synthetic turn)
    #   "completed"— gateway successfully delivered the synthetic turn
    #   "failed"   — gateway hit an error; reason in handoff_error
    #
    # The CLI writes "pending" then poll-waits for terminal state. The gateway
    # watcher transitions pending→running→{completed,failed}.








class AsyncSessionDB:
    """Async door onto SessionDB: offloads each call via asyncio.to_thread so a blocking SQLite call never freezes the event loop. Generic forwarder — the audit confirms no method returns a live cursor/generator."""

    def __init__(self, db: "SessionDB") -> None:
        self._db = db

    def __getattr__(self, name: str):
        attr = getattr(self._db, name)
        if not callable(attr):
            return attr

        async def _offloaded(*args, **kwargs):
            return await asyncio.to_thread(attr, *args, **kwargs)

        return _offloaded
