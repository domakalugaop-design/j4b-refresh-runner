"""Extract only the bounded status-40 completion cohort for the project mart.

Uses the owner-approved direct MariaDB/TCP/no-TLS contract and the existing
Keychain service. Reads `log` by its `type` index, then resolves only returned
action IDs through indexed `my.i`, `visit.i`, and `proj.i` keys. No actor IDs,
customer text, forms, or personal fields are selected.

Example:
  python3 scripts/extract_project_controller_completion_cohort.py /tmp/completion.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

import pymysql

MSK = ZoneInfo("Europe/Moscow")
W0 = int(dt.datetime(2026, 7, 26, 0, 0, tzinfo=MSK).timestamp())
W1 = int(dt.datetime(2026, 10, 27, 0, 0, tzinfo=MSK).timestamp())
READ_TIMEOUT_SECONDS = 120
CHUNK = 800


def _password() -> str:
    result = subprocess.run(
        ["security", "find-generic-password", "-s", "j4b-production-db-readonly", "-w"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode or not result.stdout.strip():
        raise RuntimeError("Keychain item j4b-production-db-readonly is unavailable")
    return result.stdout.strip()


def extract() -> dict:
    conn = pymysql.connect(
        host="lk.j4b.ru", port=3306, user="watch", password=_password(),
        database="j4b", connect_timeout=10, read_timeout=READ_TIMEOUT_SECONDS,
        charset="utf8mb4",
    )
    try:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            cur.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
            captured_at = dt.datetime.now(dt.timezone.utc).isoformat()

            # Exact event-time definition: any log typ=140 (transition to 40)
            # during the half-open business window. The type index is forced.
            cur.execute(
                "SELECT code,dt FROM log FORCE INDEX (type) "
                "WHERE typ=140 AND dt>=%s AND dt<%s ORDER BY dt",
                (W0, W1),
            )
            raw_events = cur.fetchall()

            action_months: dict[tuple[int, str], None] = {}
            unmapped_by_month: dict[str, set[int]] = defaultdict(set)
            for action_id, timestamp in raw_events:
                month = dt.datetime.fromtimestamp(timestamp, MSK).strftime("%Y-%m")
                action_months[(int(action_id), month)] = None

            action_ids = sorted({action_id for action_id, _month in action_months})
            action_map = {}
            duplicate_mapping_ids = set()
            for offset in range(0, len(action_ids), CHUNK):
                chunk = action_ids[offset:offset + CHUNK]
                placeholders = ",".join(["%s"] * len(chunk))
                cur.execute(
                    f"SELECT m.i,m.visit,v.proj,p.name,p.typ FROM my m "
                    f"LEFT JOIN visit v ON v.i=m.visit LEFT JOIN proj p ON p.i=v.proj "
                    f"WHERE m.i IN ({placeholders})",
                    chunk,
                )
                for action_id, visit_id, project_id, project_name, project_type in cur.fetchall():
                    action_id = int(action_id)
                    if action_id in action_map:
                        duplicate_mapping_ids.add(action_id)
                        continue
                    action_map[action_id] = {
                        "visit_id": int(visit_id) if visit_id is not None else None,
                        "project_id": int(project_id) if project_id is not None else None,
                        "project_name": project_name or "",
                        "project_type_code": int(project_type) if project_type is not None else None,
                    }
            if duplicate_mapping_ids:
                raise RuntimeError(
                    f"REFUSED_JOIN_MULTIPLICATION: {len(duplicate_mapping_ids)} action ids have multiple my/visit/proj mapping rows"
                )

            groups = defaultdict(lambda: {"actions": set(), "visits": set()})
            directory = {}
            mapped_actions = set()
            project_actions = defaultdict(set)
            project_visits = defaultdict(set)
            missing_mapping = set()
            missing_project = set()
            completed_visits = set()
            for action_id, month in action_months:
                mapping = action_map.get(action_id)
                if mapping is None:
                    missing_mapping.add(action_id)
                    unmapped_by_month[month].add(action_id)
                    continue
                project_id, visit_id = mapping["project_id"], mapping["visit_id"]
                if project_id is None:
                    missing_project.add(action_id)
                    unmapped_by_month[month].add(action_id)
                    continue
                directory[project_id] = {
                    "project_id": project_id,
                    "project_name": mapping["project_name"],
                    "project_type_code": mapping["project_type_code"],
                }
                group = groups[(project_id, month)]
                group["actions"].add(action_id)
                mapped_actions.add(action_id)
                project_actions[project_id].add(action_id)
                if visit_id is not None:
                    group["visits"].add(visit_id)
                    completed_visits.add((project_id, visit_id, month))
                    project_visits[project_id].add(visit_id)

            monthly = [
                {
                    "project_id": project_id,
                    "month": month,
                    "completed_action_count": len(values["actions"]),
                    "completed_visit_count": len(values["visits"]),
                }
                for (project_id, month), values in sorted(groups.items())
            ]
            visit_rows = [
                {"project_id": p, "visit_id": v, "month": m}
                for p, v, m in sorted(completed_visits)
            ]
            period_rows = [
                {
                    "project_id": project_id,
                    "completed_action_count": len(project_actions[project_id]),
                    "completed_visit_count": len(project_visits[project_id]),
                }
                for project_id in sorted(directory)
            ]
            result = {
                "window": {"w0": W0, "w1": W1, "timezone": "Europe/Moscow", "half_open": True},
                "captured_at_utc": captured_at,
                "source": "log typ=140 via type index; joins my.i -> visit.i -> proj.i",
                "totals": {
                    "event_rows": len(raw_events),
                    "distinct_actions": len(action_ids),
                    "mapped_distinct_actions": len(mapped_actions),
                    "distinct_project_visit_pairs": len({(p, v) for p, v, _m in completed_visits}),
                    "distinct_visit_ids": len({v for _p, v, _m in completed_visits}),
                    "project_count": len(directory),
                    "project_month_rows": len(monthly),
                    "missing_action_map": len(missing_mapping),
                    "missing_project": len(missing_project),
                    "missing_visit_actions": sum(1 for a in mapped_actions if action_map[a]["visit_id"] is None),
                },
                "unmapped_actions_by_month": {m: len(ids) for m, ids in sorted(unmapped_by_month.items())},
                "project_directory": [directory[k] for k in sorted(directory)],
                "project_monthly": monthly,
                "project_period": period_rows,
                "completed_visits": visit_rows,
            }
            conn.rollback()
            return result
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_json", help="secure local destination for aggregate facts and project/visit keys")
    args = parser.parse_args()
    result = extract()
    path = Path(args.output_json)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, separators=(",", ":"))
    print(json.dumps({"status": "OK", "captured_at_utc": result["captured_at_utc"], "totals": result["totals"], "output": str(path)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
