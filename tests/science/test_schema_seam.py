"""Schema-seam tests for the science layer.

Covers the behavior contracts, not snapshots:
- sessions.root_session_id is maintained at creation time and agrees with
  the existing chain-walking ``get_conversation_root``
- legacy databases (no root column / no science tables) are migrated by the
  declarative reconciler and the startup heal backfills roots
- ScienceStore round-trips the execution/provenance tables and preserves
  the invariants (per-session cell ordering, per-cell call ordering,
  content-addressed snapshot dedup, monotonic artifact versions, lineage
  reachability in both directions)
"""

import sqlite3

import pytest

from opencodon.state import SessionDB
from science.store import ScienceStore


@pytest.fixture
def db(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    yield db
    db.close()


@pytest.fixture
def store(db):
    return ScienceStore(db)


def _root_of(db, session_id):
    row = db.get_session(session_id)
    assert row is not None
    return row["root_session_id"]


# ── root_session_id maintenance ─────────────────────────────────────


class TestRootSessionId:
    def test_standalone_session_is_its_own_root(self, db):
        db.create_session("solo", source="cli")
        assert _root_of(db, "solo") == "solo"

    def test_children_inherit_root_transitively(self, db):
        db.create_session("root", source="cli")
        db.create_session("seg2", source="cli", parent_session_id="root")
        db.create_session("seg3", source="cli", parent_session_id="seg2")
        db.create_session("worker", source="delegate", parent_session_id="seg3")
        for sid in ("root", "seg2", "seg3", "worker"):
            assert _root_of(db, sid) == "root"

    def test_column_agrees_with_get_conversation_root(self, db):
        db.create_session("root", source="cli")
        db.create_session("seg2", source="cli", parent_session_id="root")
        db.create_session("child", source="delegate", parent_session_id="seg2")
        for sid in ("root", "seg2", "child"):
            assert _root_of(db, sid) == db.get_conversation_root(sid)

    def test_conflict_enrichment_adopts_parent_root(self, db):
        # Gateway pattern: a bare row exists before the agent's create_session
        # arrives carrying the real parent. The upsert must adopt the parent's
        # root, not keep the bare row's self-root.
        db.create_session("parent", source="cli")
        db.create_session("child", source="unknown")
        assert _root_of(db, "child") == "child"
        db.create_session("child", source="cli", parent_session_id="parent")
        assert db.get_session("child")["parent_session_id"] == "parent"
        assert _root_of(db, "child") == "parent"

    def test_dangling_parent_still_rejected_by_fk(self, db):
        # Unchanged contract: FK enforcement rejects inserts with a dangling
        # parent pointer (the pre-insert parent-root lookup must not bypass
        # it). Dangling parents exist only in legacy DBs — see
        # TestLegacyMigration for how the heal anchors those to self.
        with pytest.raises(sqlite3.IntegrityError):
            db.create_session(
                "orphan", source="cli", parent_session_id="never-existed"
            )


# ── legacy migration + startup heal ─────────────────────────────────


class TestLegacyMigration:
    def _make_legacy_db(self, path):
        """A pre-science database: minimal sessions table, no root column."""
        conn = sqlite3.connect(str(path))
        conn.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                parent_session_id TEXT,
                started_at REAL NOT NULL
            );
            INSERT INTO sessions VALUES
                ('root', 'cli', NULL, 1.0),
                ('seg2', 'cli', 'root', 2.0),
                ('child', 'delegate', 'seg2', 3.0),
                ('dangling', 'cli', 'deleted-parent', 4.0);
            """
        )
        conn.commit()
        conn.close()

    def test_reconciler_adds_column_and_heal_backfills(self, tmp_path):
        path = tmp_path / "state.db"
        self._make_legacy_db(path)
        db = SessionDB(path)
        try:
            assert _root_of(db, "root") == "root"
            assert _root_of(db, "seg2") == "root"
            assert _root_of(db, "child") == "root"
            # Parent row deleted by a legacy build: session anchors itself.
            assert _root_of(db, "dangling") == "dangling"
        finally:
            db.close()

    def test_heal_repairs_null_roots_on_reopen(self, tmp_path):
        path = tmp_path / "state.db"
        db = SessionDB(path)
        db.create_session("root", source="cli")
        db.create_session("child", source="cli", parent_session_id="root")
        with db._lock:
            db._conn.execute("UPDATE sessions SET root_session_id = NULL")
            db._conn.commit()
        db.close()
        db = SessionDB(path)
        try:
            assert _root_of(db, "root") == "root"
            assert _root_of(db, "child") == "root"
        finally:
            db.close()

    def test_parent_cycle_does_not_hang_startup(self, tmp_path):
        path = tmp_path / "state.db"
        db = SessionDB(path)
        db.create_session("a", source="cli")
        db.create_session("b", source="cli", parent_session_id="a")
        with db._lock:
            db._conn.execute(
                "UPDATE sessions SET parent_session_id = 'b', root_session_id = NULL"
                " WHERE id = 'a'"
            )
            db._conn.execute(
                "UPDATE sessions SET root_session_id = NULL WHERE id = 'b'"
            )
            db._conn.commit()
        db.close()
        # A corrupted cycle must not hang or crash schema init; cycle members
        # are unreachable from any anchor and may legitimately stay NULL.
        db = SessionDB(path)
        try:
            assert db.get_session("a") is not None
        finally:
            db.close()


# ── science tables presence ─────────────────────────────────────────


SCIENCE_TABLES = (
    "execution_log",
    "host_call_log",
    "content_snapshots",
    "artifacts",
    "artifact_versions",
    "artifact_dependencies",
)


class TestScienceSchema:
    def test_fresh_db_has_all_science_tables(self, db):
        with db._lock:
            names = {
                r[0]
                for r in db._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        for table in SCIENCE_TABLES:
            assert table in names

    @pytest.mark.requirement("SCI-P0-08")
    def test_science_columns_are_reconciled(self, tmp_path):
        # A database created before a science column existed gets it ADDed
        # on next open — same declarative contract as the core schema.
        path = tmp_path / "state.db"
        db = SessionDB(path)
        db.create_session("s1", source="cli")
        store = ScienceStore(db)
        cell_id = store.record_cell("s1", "print(1)", "python", "k1")
        with db._lock:
            db._conn.execute("ALTER TABLE execution_log DROP COLUMN env_snapshot")
            # has_magics is NOT NULL DEFAULT 0 — the riskier reconciler case,
            # since the ADD has to carry the default forward onto rows that
            # predate the column rather than leaving them NULL.
            db._conn.execute("ALTER TABLE execution_log DROP COLUMN has_magics")
            db._conn.commit()
        db.close()
        db = SessionDB(path)
        try:
            with db._lock:
                cols = {
                    r[1]
                    for r in db._conn.execute(
                        "PRAGMA table_info(execution_log)"
                    ).fetchall()
                }
            assert "env_snapshot" in cols
            assert "has_magics" in cols
            # The pre-existing row is readable and defaulted, not NULL.
            assert ScienceStore(db).get_cell(cell_id)["has_magics"] == 0
        finally:
            db.close()


# ── ScienceStore behavior ───────────────────────────────────────────


class TestExecutionLog:
    def test_cells_get_sequential_indexes_per_session(self, db, store):
        db.create_session("s1", source="cli")
        db.create_session("s2", source="cli")
        c0 = store.record_cell("s1", "print(1)", "python", "k1")
        c1 = store.record_cell("s1", "print(2)", "python", "k1")
        other = store.record_cell("s2", "print(3)", "python", "k2")
        cells = store.cells_for_session("s1")
        assert [c["id"] for c in cells] == [c0, c1]
        assert [c["cell_index"] for c in cells] == [0, 1]
        assert store.get_cell(other)["cell_index"] == 0

    def test_cell_records_full_execution_metadata(self, db, store):
        db.create_session("s1", source="cli")
        cell_id = store.record_cell(
            "s1",
            "df.to_csv('out.csv')",
            "python",
            "kernel-abc",
            exit_status="error",
            stderr="Boom",
            error_lineno=1,
            files_written=["out.csv"],
            files_read=["in.csv"],
            origin="agent",
        )
        cell = store.get_cell(cell_id)
        assert cell["exit_status"] == "error"
        assert cell["stderr"] == "Boom"
        assert '"out.csv"' in cell["files_written"]
        assert '"in.csv"' in cell["files_read"]


class TestHostCallLog:
    def test_calls_get_sequential_seq_within_cell(self, db, store):
        db.create_session("s1", source="cli")
        cell = store.record_cell("s1", "host.llm(...)", "python", "k1")
        first = store.record_host_call(cell, "llm", {"prompt": "hi"}, result="ok")
        second = store.record_host_call(cell, "query", {"sql": "SELECT 1"}, result="1")
        assert (first["seq"], second["seq"]) == (0, 1)
        calls = store.host_calls_for_cell(cell)
        assert [c["method"] for c in calls] == ["llm", "query"]
        assert calls[0]["data_inline"] == "ok"
        assert calls[0]["data_ref"] is None

    def test_large_result_spills_to_content_snapshot(self, db, store):
        db.create_session("s1", source="cli")
        cell = store.record_cell("s1", "host.query(...)", "python", "k1")
        big = "x" * 100
        rec = store.record_host_call(
            cell, "query", {}, result=big, inline_max_bytes=10
        )
        assert rec["data_ref"] is not None
        row = store.host_calls_for_cell(cell)[0]
        assert row["data_inline"] is None
        assert row["bytes"] == 100
        assert store.get_snapshot(rec["data_ref"]) == big

    def test_identical_payloads_share_one_snapshot(self, db, store):
        db.create_session("s1", source="cli")
        cell = store.record_cell("s1", "x", "python", "k1")
        big = "y" * 100
        r1 = store.record_host_call(cell, "query", {}, result=big, inline_max_bytes=10)
        r2 = store.record_host_call(cell, "query", {}, result=big, inline_max_bytes=10)
        assert r1["data_ref"] == r2["data_ref"]
        with db._lock:
            count = db._conn.execute(
                "SELECT COUNT(*) FROM content_snapshots"
            ).fetchone()[0]
        assert count == 1

    def test_put_snapshot_is_idempotent(self, store):
        h1 = store.put_snapshot("hello")
        h2 = store.put_snapshot("hello")
        assert h1 == h2
        assert store.get_snapshot(h1) == "hello"


class TestArtifacts:
    def test_versions_are_monotonic_and_latest_pointer_tracks(self, db, store):
        db.create_session("s1", source="cli")
        art = store.create_artifact("s1", "results.csv", session_id="s1")
        v1 = store.add_version(art, checksum="aa", size_bytes=10, storage_path="/b/aa")
        v2 = store.add_version(art, checksum="bb", size_bytes=20, storage_path="/b/bb")
        assert (v1["version_number"], v2["version_number"]) == (1, 2)
        assert store.get_artifact(art)["latest_version_id"] == v2["id"]
        assert store.latest_version(art)["checksum"] == "bb"

    def test_lineage_walks_both_directions(self, db, store):
        # raw.csv → clean.csv → figure.png
        db.create_session("s1", source="cli")
        raw = store.create_artifact("s1", "raw.csv")
        clean = store.create_artifact("s1", "clean.csv")
        fig = store.create_artifact("s1", "figure.png")
        raw_v = store.add_version(raw, checksum="r", size_bytes=1, storage_path="/r")
        clean_v = store.add_version(
            clean,
            checksum="c",
            size_bytes=1,
            storage_path="/c",
            dependencies=[{"depends_on_version_id": raw_v["id"]}],
        )
        fig_v = store.add_version(
            fig,
            checksum="f",
            size_bytes=1,
            storage_path="/f",
            dependencies=[{"depends_on_version_id": clean_v["id"]}],
        )
        upstream = store.lineage(fig_v["id"], direction="upstream")
        assert [(v["id"], v["depth"]) for v in upstream] == [
            (clean_v["id"], 1),
            (raw_v["id"], 2),
        ]
        downstream = store.lineage(raw_v["id"], direction="downstream")
        assert [(v["id"], v["depth"]) for v in downstream] == [
            (clean_v["id"], 1),
            (fig_v["id"], 2),
        ]

    def test_dependency_edges_are_idempotent(self, db, store):
        db.create_session("s1", source="cli")
        a = store.create_artifact("s1", "a")
        b = store.create_artifact("s1", "b")
        av = store.add_version(a, checksum="a", size_bytes=1, storage_path="/a")
        bv = store.add_version(b, checksum="b", size_bytes=1, storage_path="/b")
        store.add_dependency(bv["id"], av["id"], "input")
        store.add_dependency(bv["id"], av["id"], "input")
        with db._lock:
            count = db._conn.execute(
                "SELECT COUNT(*) FROM artifact_dependencies"
            ).fetchone()[0]
        assert count == 1

    def test_lineage_rejects_unknown_direction(self, store):
        with pytest.raises(ValueError):
            store.lineage("whatever", direction="sideways")

    def test_producing_cell_links_version_to_execution(self, db, store):
        # The convergence invariant: an artifact version points at the cell
        # that produced it, and that cell's host calls are reachable.
        db.create_session("s1", source="cli")
        cell = store.record_cell("s1", "save()", "python", "k1")
        store.record_host_call(cell, "llm", {}, result="hi")
        art = store.create_artifact("s1", "model.pkl")
        ver = store.add_version(
            art,
            checksum="mm",
            size_bytes=5,
            storage_path="/m",
            producing_cell_id=cell,
        )
        stored = store.get_version(ver["id"])
        assert stored["producing_cell_id"] == cell
        assert len(store.host_calls_for_cell(cell)) == 1
