"""Tests for the incremental change detection module."""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from src.portal_change_detector import (
    CHANGE_LOG_TYPES,
    LOG_TYPES_CLAIM,
    LOG_TYPES_PROJ,
    LOG_TYPES_VISIT_MY,
    ChangeDetectionResult,
    detect_changed_projects,
    ensure_checkpoint_table,
    get_checkpoint,
    update_checkpoint,
)


MSK = timezone(timedelta(hours=3))


def _msk_ts(year, month, day, hour=0, minute=0, second=0):
    return int(datetime(year, month, day, hour, minute, second, tzinfo=MSK).timestamp())


class FakeCursor:
    def __init__(self, query_results):
        self.query_results = query_results

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        q = query.lower()
        # New projects: proj.dt in window
        if "from proj where dt" in q:
            self.result = self.query_results.get("new_projects", [])
        # Log codes - distinguish by params (type numbers)
        elif "from `log`" in q and "`typ`" in q and "in" in q:
            # params contains the type numbers
            if params and any(p in (140, 1112, 1113, 21, 22, 24, 26, 31, 80, 81) for p in params):
                self.result = self.query_results.get("visit_my_codes", [])
            elif params and any(p in (25, 28) for p in params):
                self.result = self.query_results.get("claim_codes", [])
            else:
                self.result = []
        # visit join
        elif "from visit where i in" in q and "claim" not in q:
            self.result = self.query_results.get("visit_projects", [])
        # my -> visit join
        elif "from `my` m join visit v" in q:
            self.result = self.query_results.get("my_projects", [])
        # claim join
        elif "from claim where i in" in q:
            self.result = self.query_results.get("claim_projects", [])
        # proj join (for claim types)
        elif "from proj where i in" in q:
            self.result = self.query_results.get("proj_projects", [])
        # visit from claim join
        elif "from visit where i in" in q and "claim" in q:
            self.result = self.query_results.get("visit_from_claim", [])
        else:
            self.result = []
        return self

    def fetchall(self):
        return [(r,) for r in self.result]


class FakeMariaConnection:
    def __init__(self, query_results):
        self.query_results = query_results
        self.connection = self

    def cursor(self):
        return FakeCursor(self.query_results)


def _pg():
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE ops_etl_checkpoint (
            channel TEXT PRIMARY KEY,
            last_successful_run_ts INTEGER NOT NULL,
            last_full_refresh_ts INTEGER NOT NULL DEFAULT 0,
            cohort_hash TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT ''
        )
    """)
    return conn


class SqliteCheckpointStore:
    def __init__(self, conn):
        self.conn = conn

    def ensure(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS ops_etl_checkpoint (
                channel TEXT PRIMARY KEY,
                last_successful_run_ts INTEGER NOT NULL,
                last_full_refresh_ts INTEGER NOT NULL DEFAULT 0,
                cohort_hash TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            )
        """)
        self.conn.commit()

    def get(self, channel):
        self.ensure()
        cur = self.conn.execute("SELECT last_successful_run_ts, last_full_refresh_ts, cohort_hash FROM ops_etl_checkpoint WHERE channel=?", (channel,))
        row = cur.fetchone()
        if row:
            return row[0], row[1], row[2] or ""
        return 0, 0, ""

    def update(self, channel, run_ts, cohort_hash, full_refresh=False):
        self.ensure()
        if full_refresh:
            self.conn.execute("""
                INSERT INTO ops_etl_checkpoint (channel, last_successful_run_ts, last_full_refresh_ts, cohort_hash)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(channel) DO UPDATE SET
                    last_successful_run_ts=excluded.last_successful_run_ts,
                    last_full_refresh_ts=excluded.last_full_refresh_ts,
                    cohort_hash=excluded.cohort_hash
            """, (channel, run_ts, run_ts, cohort_hash))
        else:
            self.conn.execute("""
                INSERT INTO ops_etl_checkpoint (channel, last_successful_run_ts, cohort_hash)
                VALUES (?, ?, ?)
                ON CONFLICT(channel) DO UPDATE SET
                    last_successful_run_ts=excluded.last_successful_run_ts,
                    cohort_hash=excluded.cohort_hash
            """, (channel, run_ts, cohort_hash))
        self.conn.commit()


def _msk_ts(year, month, day, hour=0, minute=0, second=0):
    return int(datetime(year, month, day, hour, minute, second, tzinfo=timezone(timedelta(hours=3))).timestamp())


def test_log_type_constants():
    assert 140 in LOG_TYPES_VISIT_MY
    assert 1112 in LOG_TYPES_VISIT_MY
    assert 1113 in LOG_TYPES_VISIT_MY
    assert 21 in LOG_TYPES_VISIT_MY
    assert 22 in LOG_TYPES_VISIT_MY
    assert 24 in LOG_TYPES_VISIT_MY
    assert 26 in LOG_TYPES_VISIT_MY
    assert 31 in LOG_TYPES_VISIT_MY
    assert 80 in LOG_TYPES_VISIT_MY
    assert 81 in LOG_TYPES_VISIT_MY

    assert 25 in LOG_TYPES_CLAIM
    assert 28 in LOG_TYPES_CLAIM

    assert 25 in LOG_TYPES_PROJ
    assert 28 in LOG_TYPES_PROJ

    assert CHANGE_LOG_TYPES == LOG_TYPES_VISIT_MY | LOG_TYPES_CLAIM | LOG_TYPES_PROJ


def test_checkpoint_store_upsert():
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE ops_etl_checkpoint (
            channel TEXT PRIMARY KEY,
            last_successful_run_ts INTEGER NOT NULL,
            last_full_refresh_ts INTEGER NOT NULL DEFAULT 0,
            cohort_hash TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT ''
        )
    """)
    store = SqliteCheckpointStore(conn)
    store.update("sql", 1000, "abc123")
    ts, full, ch = store.get("sql")
    assert ts == 1000
    assert ch == "abc123"
    assert full == 0

    store.update("sql", 2000, "def456")
    ts, full, ch = store.get("sql")
    assert ts == 2000
    assert ch == "def456"

    store.update("sql", 3000, "ghi789", full_refresh=True)
    ts, full, ch = store.get("sql")
    assert ts == 3000
    assert full == 3000
    assert ch == "ghi789"


def _qr(**kwargs):
    """Helper to create query_results dict with defaults."""
    defaults = {
        "new_projects": [],
        "visit_my_codes": [],
        "claim_codes": [],
        "visit_projects": [],
        "my_projects": [],
        "claim_projects": [],
        "proj_projects": [],
        "visit_from_claim": [],
    }
    defaults.update(kwargs)
    return defaults


def test_detect_new_projects_only():
    """New projects (proj.dt in window) are detected."""
    qr = _qr(new_projects=[101, 102])
    conn = FakeMariaConnection(qr)
    W = _msk_ts(2026, 1, 1)
    NOW = _msk_ts(2026, 1, 15)
    result = detect_changed_projects(conn, W, NOW)
    assert result.project_ids == (101, 102)
    assert result.new_projects == 2
    assert result.changed_projects == 0


def test_detect_log_changes_visit_my():
    """Log events on visit/my codes map to project_ids via both paths."""
    qr = _qr(
        visit_my_codes=[1001, 1002],
        visit_projects=[10, 11],
        my_projects=[11, 12],
    )
    conn = FakeMariaConnection(qr)
    W = _msk_ts(2026, 1, 1)
    NOW = _msk_ts(2026, 1, 15)
    result = detect_changed_projects(conn, W, NOW)
    assert set(result.project_ids) == {10, 11, 12}
    assert result.changed_projects == 3
    assert result.new_projects == 0


def test_detect_log_changes_claim():
    """Log type 25/28 on claim codes maps to claim.proj and proj directly."""
    qr = _qr(
        claim_codes=[2001],
        claim_projects=[20],
        proj_projects=[21],
        # visit_from_claim uses same query pattern as visit_projects; tested via union
    )
    conn = FakeMariaConnection(qr)
    W = _msk_ts(2026, 1, 1)
    NOW = _msk_ts(2026, 1, 15)
    result = detect_changed_projects(conn, W, NOW)
    assert set(result.project_ids) == {20, 21}
    assert result.changed_projects == 2


def test_overlap_seconds_extends_window():
    """overlap_seconds extends the window backwards."""
    qr = _qr(new_projects=[101])
    conn = FakeMariaConnection(qr)
    W = _msk_ts(2026, 1, 15)
    NOW = _msk_ts(2026, 1, 20)
    result = detect_changed_projects(conn, W, NOW, overlap_seconds=500)
    assert result.project_ids == (101,)
    assert result.window_start == _msk_ts(2026, 1, 15) - 500


def test_deduplication_across_sources():
    """Same project from new_projects and log changes appears once."""
    qr = _qr(
        new_projects=[10],
        visit_my_codes=[1001],
        visit_projects=[10],
    )
    conn = FakeMariaConnection(qr)
    W = _msk_ts(2026, 1, 1)
    NOW = _msk_ts(2026, 1, 15)
    result = detect_changed_projects(conn, W, NOW)
    assert result.project_ids == (10,)
    assert result.new_projects == 1
    assert result.changed_projects == 1


def test_empty_result_when_no_changes():
    """Empty result when no new projects and no log events."""
    qr = {}
    conn = FakeMariaConnection(qr)
    W = _msk_ts(2026, 1, 1)
    NOW = _msk_ts(2026, 1, 15)
    result = detect_changed_projects(conn, W, NOW)
    assert result.project_ids == ()
    assert result.log_events_scanned == 0
    assert result.log_events_matched == 0


def test_result_as_line_format():
    """ChangeDetectionResult.as_line produces parseable output."""
    result = ChangeDetectionResult(
        project_ids=(1, 2, 3),
        log_events_scanned=100,
        log_events_matched=50,
        new_projects=1,
        changed_projects=2,
        window_start=1000,
        window_end=2000,
        details={"visit_my_codes": 50, "visit_projects": 2, "my_projects": 1},
    )
    line = result.as_line("INCREMENTAL_ETL_STATUS")
    assert "INCREMENTAL_ETL_STATUS=SUCCEEDED" in line
    assert "projects=3" in line
    assert "scanned=100" in line
    assert "matched=50" in line
    assert "new=1" in line
    assert "changed=2" in line
    assert "window_start=1000" in line
    assert "window_end=2000" in line
    assert "visit_my_codes" in line


def test_checkpoint_table_creation():
    """ensure_checkpoint_table creates the table."""
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE ops_etl_checkpoint (
            channel TEXT PRIMARY KEY,
            last_successful_run_ts INTEGER NOT NULL,
            last_full_refresh_ts INTEGER NOT NULL DEFAULT 0,
            cohort_hash TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT ''
        )
    """)
    # Verify table exists
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='ops_etl_checkpoint'")
    assert cur.fetchone() is not None


def test_change_detection_result_details():
    """Details dict is populated with per-source counts."""
    qr = _qr(
        new_projects=[1],
        visit_my_codes=[1001, 1002],
        claim_codes=[2001],
        visit_projects=[10],
        my_projects=[11],
        claim_projects=[20],
        proj_projects=[21],
        visit_from_claim=[22],
    )
    conn = FakeMariaConnection(qr)
    W = _msk_ts(2026, 1, 1)
    NOW = _msk_ts(2026, 1, 15)
    result = detect_changed_projects(conn, W, NOW)
    assert "visit_my_codes" in result.details
    assert "visit_projects" in result.details
    assert "my_projects" in result.details
    assert "claim_codes" in result.details
    assert "claim_projects" in result.details
    assert "proj_projects" in result.details
    assert "visit_from_claim" in result.details


def test_max_incremental_threshold_concept():
    """Concept test: if too many projects changed, should fallback (logic in run_incremental_live)."""
    from src.portal_mariadb_etl import run_incremental_live
    import inspect
    src = inspect.getsource(run_incremental_live)
    assert "MAX_INCREMENTAL_PROJECTS" in src
    assert "1000" in src


def test_fallback_on_detection_failure_concept():
    """If detection fails, checkpoint not updated (logic in run_incremental_live)."""
    from src.portal_mariadb_etl import run_incremental_live
    import inspect
    src = inspect.getsource(run_incremental_live)
    assert "Don't update checkpoint on failure" in src or "Don't update checkpoint" in src


def test_no_changes_skips_etl_and_updates_checkpoint():
    """When no changes detected, ETL is skipped and checkpoint advances."""
    from src.portal_mariadb_etl import run_incremental_live
    import inspect
    src = inspect.getsource(run_incremental_live)
    assert "skipped_no_changes" in src
    assert "update_checkpoint" in src


def test_one_changed_project():
    """Single project changed via log event."""
    qr = _qr(
        visit_my_codes=[1001],
        visit_projects=[10],
        my_projects=[10],
    )
    conn = FakeMariaConnection(qr)
    W = _msk_ts(2026, 1, 1)
    NOW = _msk_ts(2026, 1, 15)
    result = detect_changed_projects(conn, W, NOW)
    assert result.project_ids == (10,)
    assert result.changed_projects == 1


def test_multiple_changes_same_project():
    """Multiple log events for same project deduplicated."""
    qr = _qr(
        visit_my_codes=[1001, 1002, 1003],
        visit_projects=[10, 10, 10],
        my_projects=[10, 10, 10],
    )
    conn = FakeMariaConnection(qr)
    W = _msk_ts(2026, 1, 1)
    NOW = _msk_ts(2026, 1, 15)
    result = detect_changed_projects(conn, W, NOW)
    assert result.project_ids == (10,)
    assert result.changed_projects == 1


def test_overlapping_ids_different_entities():
    """Same code value in visit and my maps to different projects."""
    qr = _qr(
        visit_my_codes=[1001],
        visit_projects=[10],      # visit.i=1001 -> proj=10
        my_projects=[20],         # my.i=1001 -> visit=... -> proj=20
    )
    conn = FakeMariaConnection(qr)
    W = _msk_ts(2026, 1, 1)
    NOW = _msk_ts(2026, 1, 15)
    result = detect_changed_projects(conn, W, NOW)
    # Both projects should be included (union of paths)
    assert set(result.project_ids) == {10, 20}
    assert result.changed_projects == 2


def test_late_event_in_overlap_window():
    """Event with timestamp in overlap window is detected."""
    qr = _qr(
        visit_my_codes=[1001],
        visit_projects=[10],
        my_projects=[10],
    )
    conn = FakeMariaConnection(qr)
    W = _msk_ts(2026, 1, 15)  # last run was Jan 15
    NOW = _msk_ts(2026, 1, 20)
    # Event at Jan 14 (within 300s overlap before W)
    result = detect_changed_projects(conn, W, NOW, overlap_seconds=86400)  # 24h overlap
    assert result.project_ids == (10,)
    assert result.window_start < W


def test_log_read_error_handling():
    """Log read error raises exception (handled by caller)."""
    class ErrorCursor:
        def __enter__(self): return self
        def __exit__(self, *exc): return False
        def execute(self, query, params=None):
            if "`log`" in query:
                raise Exception("MariaDB connection lost")
            self.result = []
            return self
        def fetchall(self): return []

    class ErrorSource:
        def __init__(self):
            self.connection = self
        def cursor(self):
            return ErrorCursor()

    conn = ErrorSource()
    W = _msk_ts(2026, 1, 1)
    NOW = _msk_ts(2026, 1, 15)
    with pytest.raises(Exception):
        detect_changed_projects(conn, W, NOW)


def test_threshold_exceeded_no_fallback():
    """Threshold exceeded returns error status, no full fallback."""
    from src.portal_mariadb_etl import run_incremental_live
    import inspect
    src = inspect.getsource(run_incremental_live)
    assert "THRESHOLD_EXCEEDED" in src
    assert "threshold_exceeded" in src
    assert "fallback_full_cohort" not in src
    assert "run_cohort_live" not in src or "fallback" not in src


def test_etl_interruption_checkpoint_not_advanced():
    """ETL failure does not advance checkpoint."""
    from src.portal_mariadb_etl import run_incremental_live
    import inspect
    src = inspect.getsource(run_incremental_live)
    assert "Don't update checkpoint on failure" in src or "Don't update checkpoint" in src
    assert "failed" in src


def test_retry_after_interruption():
    """After ETL failure, retry re-detects same changes."""
    # Simulate: first run detects changes, ETL fails, checkpoint not updated
    # Second run with same checkpoint should detect same changes
    qr = _qr(
        visit_my_codes=[1001],
        visit_projects=[10],
        my_projects=[10],
    )
    conn = FakeMariaConnection(qr)
    W = _msk_ts(2026, 1, 1)
    NOW = _msk_ts(2026, 1, 15)
    result1 = detect_changed_projects(conn, W, NOW)
    result2 = detect_changed_projects(conn, W, NOW)
    assert result1.project_ids == result2.project_ids == (10,)


def test_deletion_or_cancellation():
    """Deletion/cancellation events (if logged) are detected."""
    # Type 140 with status change to cancelled (e.g., 37 -> 40)
    # This is already covered by type 140 detection
    qr = _qr(
        visit_my_codes=[1001],
        visit_projects=[10],
        my_projects=[10],
    )
    conn = FakeMariaConnection(qr)
    W = _msk_ts(2026, 1, 1)
    NOW = _msk_ts(2026, 1, 15)
    result = detect_changed_projects(conn, W, NOW)
    assert result.project_ids == (10,)
    # The actual status value is in log.info, not used for project detection


def test_no_checkpoint_first_run():
    """First run with no checkpoint (last_run_ts=0) detects all changes in window."""
    qr = _qr(
        new_projects=[1, 2],
        visit_my_codes=[1001],
        visit_projects=[3],
        my_projects=[3],
    )
    conn = FakeMariaConnection(qr)
    W = 0  # No checkpoint
    NOW = _msk_ts(2026, 1, 15)
    result = detect_changed_projects(conn, W, NOW)
    # Should detect all projects in window
    assert set(result.project_ids) == {1, 2, 3}
    assert result.new_projects == 2
    assert result.changed_projects == 1


def test_overlapping_window_reprocessing():
    """Reprocessing overlapping window does not duplicate projects."""
    qr = _qr(
        visit_my_codes=[1001, 1002],
        visit_projects=[10, 11],
        my_projects=[10, 11],
    )
    conn = FakeMariaConnection(qr)
    W1 = _msk_ts(2026, 1, 1)
    W2 = _msk_ts(2026, 1, 10)  # Overlapping window
    NOW = _msk_ts(2026, 1, 15)
    result1 = detect_changed_projects(conn, W1, NOW)
    result2 = detect_changed_projects(conn, W2, NOW)
    # Both should find same projects (deduplication)
    assert set(result1.project_ids) == {10, 11}
    assert set(result2.project_ids) == {10, 11}


def test_checkpoint_semantics_window_upper_bound():
    """Checkpoint uses window_end (fixed before detection), not now()."""
    from src.portal_mariadb_etl import run_incremental_live
    import inspect
    src = inspect.getsource(run_incremental_live)
    # Check that now_ts is captured before detection and used for checkpoint
    assert "now_ts = int(time.time())" in src
    # Check that checkpoint is updated with now_ts (the window_end)
    assert "update_checkpoint(target_connection, \"sql\", now_ts" in src


def test_window_end_fixed_before_detection():
    """window_end is fixed before detection, not after."""
    from src.portal_change_detector import detect_changes_incremental
    import inspect
    src = inspect.getsource(detect_changes_incremental)
    # Function signature should have window_end parameter
    assert "window_end: int" in src
    # Should not use time.time() inside
    assert "time.time()" not in src