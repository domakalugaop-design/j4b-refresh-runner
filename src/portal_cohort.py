"""Dynamic 12-month cohort discovery, storage, chunking and checkpointing.

Cohort selection rule (documented, reproducible):
  A project belongs to the rolling 12-month cohort when ANY of:
    1. proj.dt (creation) >= window start (Europe/Moscow);
    2. proj.dt1 or proj.dt2 (planned period) intersects the window;
    3. the project has controller actions (my.dt_visit, indexed) in the window;
    4. the project has claims (claim.dt, bounded scan) in the window.
  Project creation date alone is never the only criterion: projects created
  before the window but operationally active inside it are included.

The cohort is persisted in ops.portal_cohort (stable project_id, upsert-only).
Rows are never deleted: projects that leave the window keep their loaded data
and are flagged active=false (they stop being refreshed, nothing is removed).
"""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterator, Optional, Sequence, Tuple

from .portal_mariadb_etl import ETLError

MSK = timezone(timedelta(hours=3))
COHORT_MONTHS = 12
MAX_CHUNK_PROJECTS = 250
COHORT_TABLE = "ops.portal_cohort"
PROGRESS_TABLE = "ops.portal_etl_progress"


def cohort_window_start(today: Optional[date] = None) -> int:
    """Unix timestamp of 00:00 Europe/Moscow exactly COHORT_MONTHS months back."""
    today = today or datetime.now(MSK).date()
    months = COHORT_MONTHS
    year = today.year
    month = today.month
    year -= months // 12
    month -= months % 12
    if month <= 0:
        month += 12
        year -= 1
    # Clamp day (e.g. March 31 -> February 28).
    day = today.day
    while day > 28:
        try:
            anchor = date(year, month, day)
            break
        except ValueError:
            day -= 1
    else:
        anchor = date(year, month, day)
    start = datetime(anchor.year, anchor.month, anchor.day, 0, 0, 0, tzinfo=MSK)
    return int(start.timestamp())


def discover_cohort(source: Any, window_start: int, now_ts: int) -> Tuple[Tuple[int, ...], Dict[str, int]]:
    """Read-only cohort discovery against production MariaDB.

    Bounded by construction: proj is a small table (full scan), the action
    criterion uses the existing my.dt_visit index range limited to the window,
    and the claim criterion reuses the bounded claim scan (fail-closed checks
    stay in the ETL pipeline itself).
    """
    connection = source.connection
    with connection.cursor() as cursor:
        cursor.execute("SELECT i, dt, dt1, dt2 FROM proj")
        proj_rows = cursor.fetchall()
        cursor.execute(
            "SELECT DISTINCT v.proj FROM `my` AS m INNER JOIN visit AS v ON v.i = m.visit "
            "WHERE m.dt_visit >= %s AND m.dt_visit <= %s",
            (window_start, now_ts),
        )
        action_projects = {row[0] for row in cursor.fetchall()}
        cursor.execute(
            "SELECT DISTINCT proj FROM claim WHERE dt >= %s AND dt <= %s",
            (window_start, now_ts),
        )
        claim_projects = {row[0] for row in cursor.fetchall()}
    by_id = {row[0]: row for row in proj_rows}

    def date_signal(row: Tuple[Any, ...]) -> bool:
        _pid, dt, dt1, dt2 = row
        created = bool(dt and dt >= window_start)
        started = bool(dt1 and window_start <= dt1 <= now_ts)
        ended = bool(dt2 and window_start <= dt2 <= now_ts)
        running = bool(dt1 and dt1 <= now_ts and (not dt2 or dt2 >= now_ts))
        return created or started or ended or running

    cohort = {pid for pid, row in by_id.items() if date_signal(row)} | action_projects | claim_projects
    cohort = {pid for pid in cohort if pid in by_id}
    stats = {
        "proj_rows": len(proj_rows),
        "created_in_window": sum(1 for row in by_id.values() if row[1] and row[1] >= window_start),
        "action_projects": len(action_projects),
        "claim_projects": len(claim_projects),
        "cohort_size": len(cohort),
    }
    return tuple(sorted(cohort)), stats


def cohort_hash(project_ids: Sequence[int], window_start: int) -> str:
    payload = json.dumps({"window_start": window_start, "ids": list(project_ids)}, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def ensure_cohort_tables(connection: Any) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS %s ("
            "project_id INTEGER PRIMARY KEY, "
            "first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
            "last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
            "window_start_ts INTEGER NOT NULL, "
            "active BOOLEAN NOT NULL DEFAULT TRUE)" % COHORT_TABLE
        )
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS %s ("
            "channel TEXT NOT NULL, "
            "cohort_hash TEXT NOT NULL, "
            "chunk_index INTEGER NOT NULL, "
            "run_id TEXT NOT NULL, "
            "status TEXT NOT NULL, "
            "started_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
            "finished_at TIMESTAMPTZ, "
            "PRIMARY KEY (channel, cohort_hash, chunk_index))" % PROGRESS_TABLE
        )
    connection.commit()


def upsert_cohort(connection: Any, project_ids: Sequence[int], window_start: int) -> None:
    """Upsert the current cohort; never delete rows for projects leaving the window."""
    ensure_cohort_tables(connection)
    with connection.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO %s (project_id, window_start_ts, active) VALUES (%%s, %%s, TRUE) "
            "ON CONFLICT (project_id) DO UPDATE SET "
            "last_seen_at = now(), window_start_ts = EXCLUDED.window_start_ts, active = TRUE" % COHORT_TABLE,
            [(pid, window_start) for pid in project_ids],
        )
        cursor.execute(
            "UPDATE %s SET active = FALSE WHERE active = TRUE AND project_id <> ALL(%%s)" % COHORT_TABLE,
            (list(project_ids),),
        )
    connection.commit()


def load_active_cohort(connection: Any) -> Tuple[int, ...]:
    ensure_cohort_tables(connection)
    with connection.cursor() as cursor:
        cursor.execute("SELECT project_id FROM %s WHERE active = TRUE ORDER BY project_id" % COHORT_TABLE)
        rows = cursor.fetchall()
    if not rows:
        raise ETLError("dynamic cohort is empty; run the SQL ETL cohort discovery first")
    return tuple(row[0] for row in rows)


def chunk_project_ids(project_ids: Sequence[int], max_per_chunk: int = MAX_CHUNK_PROJECTS) -> Iterator[Tuple[int, Tuple[int, ...]]]:
    if max_per_chunk < 1:
        raise ETLError("chunk size must be positive")
    for index in range(0, len(project_ids), max_per_chunk):
        yield index // max_per_chunk, tuple(project_ids[index : index + max_per_chunk])


def completed_chunks(connection: Any, channel: str, digest: str) -> set:
    ensure_cohort_tables(connection)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT chunk_index FROM %s WHERE channel = %%s AND cohort_hash = %%s AND status = 'SUCCEEDED'" % PROGRESS_TABLE,
            (channel, digest),
        )
        return {row[0] for row in cursor.fetchall()}


def record_chunk(connection: Any, channel: str, digest: str, chunk_index: int, run_id: str, status: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO %s (channel, cohort_hash, chunk_index, run_id, status, finished_at) "
            "VALUES (%%s, %%s, %%s, %%s, %%s, CASE WHEN %%s = 'SUCCEEDED' THEN now() ELSE NULL END) "
            "ON CONFLICT (channel, cohort_hash, chunk_index) DO UPDATE SET "
            "run_id = EXCLUDED.run_id, status = EXCLUDED.status, "
            "finished_at = EXCLUDED.finished_at" % PROGRESS_TABLE,
            (channel, digest, chunk_index, run_id, status, status),
        )
    connection.commit()


@dataclass
class CohortRunSummary:
    status: str = "PREPARING"
    cohort_size: int = 0
    chunk_count: int = 0
    chunks_succeeded: int = 0
    chunks_skipped: int = 0
    chunks_failed: int = 0
    window_start: int = 0
    digest: str = ""
    stats: Dict[str, int] = field(default_factory=dict)

    def as_line(self, prefix: str) -> str:
        return (
            "%s=%s cohort_size=%d chunks=%d succeeded=%d skipped=%d failed=%d "
            "window_start=%d cohort_hash=%s stats=%s"
            % (
                prefix, self.status, self.cohort_size, self.chunk_count,
                self.chunks_succeeded, self.chunks_skipped, self.chunks_failed,
                self.window_start, self.digest, json.dumps(self.stats, sort_keys=True),
            )
        )
