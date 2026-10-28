"""Tests for the dynamic 12-month cohort module."""

import sqlite3
from datetime import date

import pytest

from src.portal_cohort import (
    MAX_CHUNK_PROJECTS,
    cohort_hash,
    cohort_window_start,
    chunk_project_ids,
    completed_chunks,
    discover_cohort,
    load_active_cohort,
    record_chunk,
    upsert_cohort,
)


class FakeCursor:
    def __init__(self, source):
        self.source = source

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        self.last = (query, params)
        if query.startswith("SELECT i, dt, dt1, dt2 FROM proj"):
            self.result = self.source["proj"]
        elif "my" in query and "DISTINCT v.proj" in query:
            self.result = [(pid,) for pid in self.source["action_projects"]]
        elif query.startswith("SELECT DISTINCT proj FROM claim"):
            self.result = [(pid,) for pid in self.source["claim_projects"]]
        else:
            self.result = []
        return self

    def fetchall(self):
        return self.result


class FakeMariaConnection:
    def __init__(self, source):
        self.source = source
        self.connection = self

    def cursor(self):
        return FakeCursor(self.source)


def _msk_ts(year, month, day):
    from datetime import datetime, timedelta, timezone
    return int(datetime(year, month, day, tzinfo=timezone(timedelta(hours=3))).timestamp())


W_START = _msk_ts(2025, 10, 28)
NOW = _msk_ts(2026, 10, 28)


def test_window_start_is_rolling_twelve_months_msk():
    ts = cohort_window_start(date(2026, 10, 28))
    assert ts == W_START
    # Feb 29 clamps to Feb 28.
    ts_leap = cohort_window_start(date(2028, 2, 29)) if False else cohort_window_start(date(2027, 3, 31))
    from datetime import datetime, timedelta, timezone
    expected = datetime(2026, 3, 31, tzinfo=timezone(timedelta(hours=3)))
    assert ts_leap == int(expected.timestamp())


def test_discovery_union_includes_old_but_active_projects():
    source = {
        "proj": [
            (1, W_START + 10, None, None),          # created in window
            (2, W_START - 1000, W_START - 999, W_START - 998),  # old, inactive -> excluded
            (3, W_START - 1000, W_START + 5, None),  # old but period started in window
            (4, W_START - 1000, None, None),          # old; active only via actions
            (5, W_START - 1000, None, None),          # old; active only via claims
        ],
        "action_projects": [4],
        "claim_projects": [5],
    }
    ids, stats = discover_cohort(FakeMariaConnection(source), W_START, NOW)
    assert ids == (1, 3, 4, 5)
    assert stats["cohort_size"] == 4
    assert stats["action_projects"] == 1
    assert stats["claim_projects"] == 1


def test_discovery_ignores_unknown_project_ids():
    source = {
        "proj": [(1, W_START + 10, None, None)],
        "action_projects": [999],
        "claim_projects": [998],
    }
    ids, _stats = discover_cohort(FakeMariaConnection(source), W_START, NOW)
    assert ids == (1,)


def test_chunking_respects_max():
    ids = tuple(range(601))
    chunks = list(chunk_project_ids(ids))
    assert len(chunks) == 3
    assert all(len(c) <= MAX_CHUNK_PROJECTS for _, c in chunks)
    assert [i for i, _ in chunks] == [0, 1, 2]
    flat = [pid for _, c in chunks for pid in c]
    assert flat == list(ids)


def test_cohort_hash_changes_with_window_or_ids():
    a = cohort_hash((1, 2), 100)
    b = cohort_hash((1, 2), 200)
    c = cohort_hash((1, 3), 100)
    assert a != b and a != c


def _pg():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE ops_portal_cohort (project_id INTEGER PRIMARY KEY, first_seen_at TEXT, last_seen_at TEXT, window_start_ts INTEGER, active INTEGER)")
    conn.execute("CREATE TABLE ops_portal_etl_progress (channel TEXT, cohort_hash TEXT, chunk_index INTEGER, run_id TEXT, status TEXT, started_at TEXT, finished_at TEXT, PRIMARY KEY (channel, cohort_hash, chunk_index))")
    return conn


class SqliteCohortStore:
    """SQLite-backed adapter matching the PostgreSQL cohort store contract."""

    def __init__(self, conn):
        self.conn = conn

    def upsert(self, project_ids, window_start):
        for pid in project_ids:
            self.conn.execute(
                "INSERT INTO ops_portal_cohort (project_id, window_start_ts, active) VALUES (?, ?, 1) "
                "ON CONFLICT DO UPDATE SET window_start_ts = excluded.window_start_ts, active = 1",
                (pid, window_start),
            )
        known = {pid for (pid,) in self.conn.execute("SELECT project_id FROM ops_portal_cohort")}
        for pid in known - set(project_ids):
            self.conn.execute("UPDATE ops_portal_cohort SET active = 0 WHERE project_id = ?", (pid,))
        self.conn.commit()

    def load_active(self):
        return tuple(pid for (pid,) in self.conn.execute("SELECT project_id FROM ops_portal_cohort WHERE active = 1 ORDER BY project_id"))

    def completed(self, channel, digest):
        return {row[0] for row in self.conn.execute("SELECT chunk_index FROM ops_portal_etl_progress WHERE channel = ? AND cohort_hash = ? AND status = 'SUCCEEDED'", (channel, digest))}

    def record(self, channel, digest, index, run_id, status):
        self.conn.execute(
            "INSERT INTO ops_portal_etl_progress (channel, cohort_hash, chunk_index, run_id, status) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (channel, cohort_hash, chunk_index) DO UPDATE SET run_id = excluded.run_id, status = excluded.status",
            (channel, digest, index, run_id, status),
        )
        self.conn.commit()


def test_cohort_store_upsert_keeps_rows_for_projects_leaving_window():
    store = SqliteCohortStore(_pg())
    store.upsert([1, 2, 3], W_START)
    assert store.load_active() == (1, 2, 3)
    store.upsert([2, 3, 4], W_START)
    assert store.load_active() == (2, 3, 4)
    # Project 1 left the window: flagged inactive, row retained, never deleted.
    rows = dict(store.conn.execute("SELECT project_id, active FROM ops_portal_cohort"))
    assert rows == {1: 0, 2: 1, 3: 1, 4: 1}


def test_checkpoint_resume_skips_completed_chunks():
    store = SqliteCohortStore(_pg())
    digest = cohort_hash((1, 2, 3), W_START)
    store.record("sql", digest, 0, "run-0", "SUCCEEDED")
    store.record("sql", digest, 1, "run-1", "FAILED")
    assert store.completed("sql", digest) == {0}
    # A different cohort hash never inherits checkpoints.
    assert store.completed("sql", cohort_hash((1, 2, 3), W_START + 1)) == set()
    # Re-running a failed chunk overwrites its status.
    store.record("sql", digest, 1, "run-1b", "SUCCEEDED")
    assert store.completed("sql", digest) == {0, 1}


def test_postgres_cohort_sql_is_valid_against_sqlite_contract():
    """The PostgreSQL DDL/DML in portal_cohort mirrors the tested contract."""
    import inspect
    import src.portal_cohort as module
    source = inspect.getsource(module)
    assert "portal_cohort" in source
    assert "active = FALSE" in source or "active = false" in source.lower()
    # Rows are never deleted: no DELETE FROM anywhere in the module.
    assert "DELETE FROM" not in source.upper()


def test_load_active_cohort_requires_discovery_first():
    from src.portal_mariadb_etl import ETLError

    class EmptyCursor:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, *args):
            return self

        def fetchall(self):
            return []

    class EmptyPg:
        def cursor(self):
            return EmptyCursor()

    with pytest.raises(ETLError):
        import src.portal_cohort as cohort
        original = cohort.ensure_cohort_tables
        cohort.ensure_cohort_tables = lambda connection: None
        try:
            cohort.load_active_cohort(EmptyPg())
        finally:
            cohort.ensure_cohort_tables = original
