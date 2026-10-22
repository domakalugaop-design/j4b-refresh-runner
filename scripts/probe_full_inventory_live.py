"""Full-October discovery and bounded enrichment (incremental, resumable).

Loads the checkpoint if present, discovers every October day up to today through
the registry, then enriches a bounded number of report detail pages. Refuses to
run inside the production refresh windows (09:55-10:20, 16:55-17:20 MSK).

Usage (on DFB): python3 probe_full_inventory_live.py [max_fetches]
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, "/tmp/j4b-probe")

from pkg.portal_transport import PortalSession  # noqa: E402
from pkg.report_corrector_collector import (  # noqa: E402
    START_DATE, CollectorError, collect_batch, extract_registry_report_ids,
    merge_candidates, new_state, render_sheet_rows, validate_sheet_rows,
)

CREDENTIALS = "/home/maxis/j4b-refresh-deployment/credentials/portal-auth.json"
BASE_URL = "https://lk.j4b.ru"
STATE_DIR = os.path.expanduser("~/.cache/j4b-report-reviewers")
STATE_PATH = os.path.join(STATE_DIR, "state.json")
MSK = ZoneInfo("Europe/Moscow")


def quiet_login(session: PortalSession) -> None:
    saved_out, saved_err = os.dup(1), os.dup(2)
    null_fd = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(null_fd, 1); os.dup2(null_fd, 2)
        session.login()
    finally:
        os.dup2(saved_out, 1); os.dup2(saved_err, 2)
        os.close(saved_out); os.close(saved_err); os.close(null_fd)


def in_refresh_window(now: datetime) -> bool:
    local = now.astimezone(MSK)
    minutes = local.hour * 60 + local.minute
    return 9 * 60 + 55 <= minutes <= 10 * 60 + 20 or 16 * 60 + 55 <= minutes <= 17 * 60 + 20


def load_state() -> dict:
    if not os.path.exists(STATE_PATH):
        return new_state()
    with open(STATE_PATH, encoding="utf-8") as stream:
        return json.load(stream)


def save_state(state: dict) -> None:
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    temporary = STATE_PATH + ".tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(state, stream, ensure_ascii=False, separators=(",", ":"))
    os.replace(temporary, STATE_PATH)
    os.chmod(STATE_PATH, 0o600)


def main() -> int:
    budget = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    now = datetime.now(timezone.utc)
    out: dict = {"guard": {}, "discovery": {}, "batch": {}, "rows": {}, "requests": 0}
    if in_refresh_window(now):
        out["guard"] = {"decision": "REFUSED_PRODUCTION_WINDOW"}
        print(json.dumps(out, separators=(",", ":")))
        return 0

    state = load_state()
    before = len(state["reports"])
    session = None
    try:
        with open(CREDENTIALS, encoding="utf-8") as stream:
            config = json.load(stream)
        session = PortalSession(timeout=90, base_url=BASE_URL,
                                login=config["login"], password=config["password"])
        quiet_login(session)

        today = now.astimezone(MSK).date()
        day = START_DATE
        while day <= today:
            if session.requests:
                time.sleep(1.5)
            ddmm = day.strftime("%d.%m.%Y")
            status, _, body = session.request(
                "/report/", "POST",
                data={"dt1": ddmm, "dt2": ddmm, "limit": "10000"},
            )
            ids = extract_registry_report_ids(body.decode("utf-8", errors="replace"))
            added = merge_candidates(state, ids, discovered_for=day.isoformat())
            out["discovery"][day.isoformat()] = {"ids": len(ids), "new": added}
            day += timedelta(days=1)
        save_state(state)

        def fetch(report_id: str) -> str:
            status, _, body = session.request(f"/action/{report_id}", "GET")
            if status != 200:
                raise CollectorError("action page status is not 200")
            return body.decode("utf-8", errors="replace")

        out["batch"] = collect_batch(
            state, fetch, datetime.now(timezone.utc),
            max_fetches=budget, recheck_after=timedelta(hours=6),
            save=save_state, request_interval_seconds=1.5, sleep=time.sleep,
        )

        rows = render_sheet_rows(state)
        validate_sheet_rows(rows)
        dates = Counter(row[4] for row in rows[1:])
        statuses = Counter(row[7] for row in rows[1:])
        projects = {row[3] for row in rows[1:]}
        out["rows"] = {
            "total": len(rows) - 1,
            "projects": len(projects),
            "date_min": min(dates) if dates else None,
            "date_max": max(dates) if dates else None,
            "statuses": dict(statuses),
            "with_corrector": sum(1 for row in rows[1:] if row[5]),
            "without_corrector": sum(1 for row in rows[1:] if not row[5]),
        }
        out["inventory"] = {"before": before, "after": len(state["reports"]),
                            "unresolved": sum(1 for e in state["reports"].values()
                                              if e.get("detail") is None
                                              and e.get("last_attempt_status") != "DATE_OUT_OF_RANGE")}
        out["requests"] = session.requests
        out["checkpoint_bytes"] = os.path.getsize(STATE_PATH)
        print(json.dumps(out, ensure_ascii=False, separators=(",", ":"))[:4200])
        return 0
    except Exception as exc:
        out["failure_category"] = "SAFE_PROBE_ERROR"
        out["error_class"] = type(exc).__name__
        out["error_message"] = str(exc)[:160]
        print(json.dumps(out, ensure_ascii=True, separators=(",", ":"))[:3000])
        return 0
    finally:
        if session is not None:
            session.close()


if __name__ == "__main__":
    raise SystemExit(main())
