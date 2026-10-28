"""Incremental change detection for Portal SQL ETL.

Reads the MariaDB event log (table `log`) to identify projects with
changes since the last successful run checkpoint.

Log type → entity mapping (validated 2026-10-28):
  140  : status change      → visit.code + my.code
  1112 : form submit        → visit.code + my.code
  1113 : form submit        → visit.code + my.code
  21   : comment/feedback   → visit.code + my.code
  22   : report review      → visit.code + my.code
  24   : experience/qualif  → visit.code + my.code
  25   : claim review       → claim.code + proj.code
  26   : chat/message       → visit.code + my.code
  28   : claim-related      → visit.code + claim.code + proj.code
  31   : unknown            → visit.code + my.code
  80   : unknown            → visit.code + my.code
  81   : unknown            → visit.code + my.code

CRITICAL: `log.code` space overlaps between `visit` and `my` tables.
Both paths must be joined and unioned to avoid missing changes.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Sequence, Set, Tuple

from .portal_mariadb_etl import ETLError

# Log types that reference visit/my (code overlaps between both tables)
LOG_TYPES_VISIT_MY = frozenset({
    140, 1112, 1113, 21, 22, 24, 26, 31, 80, 81,
})

# Log types that reference claim
LOG_TYPES_CLAIM = frozenset({25, 28})

# Log types that reference proj directly
LOG_TYPES_PROJ = frozenset({25, 28})

# All log types that indicate a change relevant to SQL ETL
CHANGE_LOG_TYPES = LOG_TYPES_VISIT_MY | LOG_TYPES_CLAIM | LOG_TYPES_PROJ

CHECKPOINT_TABLE = "ops.etl_checkpoint"


@dataclass
class ChangeDetectionResult:
    project_ids: Tuple[int, ...]
    log_events_scanned: int
    log_events_matched: int
    new_projects: int
    changed_projects: int
    window_start: int
    window_end: int
    details: Dict[str, int] = field(default_factory=dict)

    def as_line(self, prefix: str) -> str:
        return (
            f"{prefix}=SUCCEEDED projects={len(self.project_ids)} "
            f"scanned={self.log_events_scanned} matched={self.log_events_matched} "
            f"new={self.new_projects} changed={self.changed_projects} "
            f"window_start={self.window_start} window_end={self.window_end} "
            f"details={json.dumps(self.details, sort_keys=True)}"
        )


def ensure_checkpoint_table(connection: Any) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            f"CREATE TABLE IF NOT EXISTS {CHECKPOINT_TABLE} ("
            "channel TEXT PRIMARY KEY, "
            "last_successful_run_ts INTEGER NOT NULL, "
            "last_full_refresh_ts INTEGER NOT NULL DEFAULT 0, "
            "cohort_hash TEXT NOT NULL DEFAULT '', "
            "updated_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
    connection.commit()


def get_checkpoint(connection: Any, channel: str) -> Tuple[int, int, str]:
    """Return (last_successful_run_ts, last_full_refresh_ts, cohort_hash)."""
    ensure_checkpoint_table(connection)
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT last_successful_run_ts, last_full_refresh_ts, cohort_hash "
            f"FROM {CHECKPOINT_TABLE} WHERE channel=%s",
            (channel,),
        )
        row = cursor.fetchone()
    if row:
        return row[0], row[1], row[2] or ""
    return 0, 0, ""


def update_checkpoint(
    connection: Any,
    channel: str,
    run_ts: int,
    cohort_hash: str,
    *,
    full_refresh: bool = False,
) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            f"INSERT INTO {CHECKPOINT_TABLE} (channel, last_successful_run_ts, cohort_hash) "
            f"VALUES (%s, %s, %s) "
            f"ON CONFLICT (channel) DO UPDATE SET "
            f"last_successful_run_ts = EXCLUDED.last_successful_run_ts, "
            f"cohort_hash = EXCLUDED.cohort_hash, "
            f"updated_at = now()" + (", last_full_refresh_ts = EXCLUDED.last_full_refresh_ts" if full_refresh else ""),
            (channel, run_ts, cohort_hash),
        )
    connection.commit()


def detect_changed_projects(
    source: Any,
    window_start: int,
    window_end: int,
    *,
    overlap_seconds: int = 300,
) -> ChangeDetectionResult:
    """Detect project_ids with changes in (window_start - overlap, window_end].

    Uses a safe overlap to handle clock skew and transaction boundaries.
    """
    effective_start = max(0, window_start - overlap_seconds)
    connection = source.connection

    # 1. New projects: proj.dt in window
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT i FROM proj WHERE dt > %s AND dt <= %s",
            (effective_start, window_end),
        )
        new_project_ids = {row[0] for row in cursor.fetchall()}

    # 2. Log-based changes
    changed_project_ids: Set[int] = set()
    log_events_scanned = 0
    log_events_matched = 0
    details: Dict[str, int] = {}

    # 2a. visit/my overlapping code space
    if LOG_TYPES_VISIT_MY:
        placeholders = ",".join(["%s"] * len(LOG_TYPES_VISIT_MY))
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT DISTINCT `code` FROM `log` "
                f"WHERE `typ` IN ({placeholders}) AND `dt` > %s AND `dt` <= %s",
                tuple(LOG_TYPES_VISIT_MY) + (effective_start, window_end),
            )
            codes = [row[0] for row in cursor.fetchall()]
        log_events_scanned += len(codes)
        if codes:
            ph = ",".join(["%s"] * len(codes))
            # Join via visit
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT DISTINCT proj FROM visit WHERE i IN ({ph})",
                    tuple(codes),
                )
                visit_projects = {row[0] for row in cursor.fetchall()}
            # Join via my -> visit
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT DISTINCT v.proj FROM `my` m JOIN visit v ON v.i = m.visit "
                    f"WHERE m.i IN ({ph})",
                    tuple(codes),
                )
                my_projects = {row[0] for row in cursor.fetchall()}
            changed_project_ids |= visit_projects | my_projects
            log_events_matched += len(visit_projects) + len(my_projects)
            details["visit_my_codes"] = len(codes)
            details["visit_projects"] = len(visit_projects)
            details["my_projects"] = len(my_projects)

    # 2b. claim-referencing types
    if LOG_TYPES_CLAIM:
        placeholders = ",".join(["%s"] * len(LOG_TYPES_CLAIM))
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT DISTINCT `code` FROM `log` "
                f"WHERE `typ` IN ({placeholders}) AND `dt` > %s AND `dt` <= %s",
                tuple(LOG_TYPES_CLAIM) + (effective_start, window_end),
            )
            codes = [row[0] for row in cursor.fetchall()]
        log_events_scanned += len(codes)
        if codes:
            ph = ",".join(["%s"] * len(codes))
            # claim.code -> claim.proj
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT DISTINCT proj FROM claim WHERE i IN ({ph})",
                    tuple(codes),
                )
                claim_projects = {row[0] for row in cursor.fetchall()}
            # proj.code (for type 25, 28)
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT DISTINCT i FROM proj WHERE i IN ({ph})",
                    tuple(codes),
                )
                proj_projects = {row[0] for row in cursor.fetchall()}
            # visit.code (for type 28)
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT DISTINCT proj FROM visit WHERE i IN ({ph})",
                    tuple(codes),
                )
                visit_from_claim = {row[0] for row in cursor.fetchall()}
            changed_project_ids |= claim_projects | proj_projects | visit_from_claim
            log_events_matched += len(claim_projects) + len(proj_projects) + len(visit_from_claim)
            details["claim_codes"] = len(codes)
            details["claim_projects"] = len(claim_projects)
            details["proj_projects"] = len(proj_projects)
            details["visit_from_claim"] = len(visit_from_claim)

    # 3. Union and deduplicate
    all_changed = changed_project_ids | new_project_ids
    result = ChangeDetectionResult(
        project_ids=tuple(sorted(all_changed)),
        log_events_scanned=log_events_scanned,
        log_events_matched=log_events_matched,
        new_projects=len(new_project_ids),
        changed_projects=len(changed_project_ids),
        window_start=effective_start,
        window_end=window_end,
        details=details,
    )
    return result


def detect_changes_incremental(
    source: Any,
    last_run_ts: int,
    now_ts: int,
    *,
    overlap_seconds: int = 300,
) -> ChangeDetectionResult:
    """High-level incremental detection using last successful run timestamp."""
    return detect_changed_projects(source, last_run_ts, now_ts, overlap_seconds=overlap_seconds)