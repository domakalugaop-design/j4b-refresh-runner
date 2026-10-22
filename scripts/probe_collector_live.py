"""Bounded live validation of the report->corrector collector.

Discovers three October days from the registry, then enriches a small sample
through the action pages. Writes a checkpoint outside the repository.

Usage (on DFB): python3 probe_collector_live.py [max_fetches]
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, "/tmp/j4b-probe")

from pkg.portal_transport import PortalSession  # noqa: E402
from pkg.report_corrector_collector import (  # noqa: E402
    CollectorError, collect_batch, extract_registry_report_ids, merge_candidates,
    new_state, render_sheet_rows, validate_sheet_rows,
)

CREDENTIALS = "/home/maxis/j4b-refresh-deployment/credentials/portal-auth.json"
BASE_URL = "https://lk.j4b.ru"
STATE_DIR = os.path.expanduser("~/.cache/j4b-report-reviewers")
STATE_PATH = os.path.join(STATE_DIR, "state.json")
DAYS = ("01.10.2026", "02.10.2026", "03.10.2026")


def quiet_login(session: PortalSession) -> None:
    saved_out, saved_err = os.dup(1), os.dup(2)
    null_fd = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(null_fd, 1); os.dup2(null_fd, 2)
        session.login()
    finally:
        os.dup2(saved_out, 1); os.dup2(saved_err, 2)
        os.close(saved_out); os.close(saved_err); os.close(null_fd)


def save_state(state: dict) -> None:
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    temporary = STATE_PATH + ".tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(state, stream, ensure_ascii=False, separators=(",", ":"))
    os.replace(temporary, STATE_PATH)
    os.chmod(STATE_PATH, 0o600)


def main() -> int:
    budget = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    out: dict = {"discovery": {}, "batch": {}, "rows": {}, "requests": 0}
    state = new_state()
    session = None
    try:
        with open(CREDENTIALS, encoding="utf-8") as stream:
            config = json.load(stream)
        session = PortalSession(timeout=90, base_url=BASE_URL,
                                login=config["login"], password=config["password"])
        quiet_login(session)

        for index, day in enumerate(DAYS):
            if index:
                time.sleep(1.5)
            status, _, body = session.request(
                "/report/", "POST",
                data={"dt1": day, "dt2": day, "limit": "10000"},
            )
            ids = extract_registry_report_ids(body.decode("utf-8", errors="replace"))
            iso_day = "%s-%s-%s" % (day[6:], day[3:5], day[0:2])
            added = merge_candidates(state, ids, discovered_for=iso_day)
            out["discovery"][day] = {"status": status, "ids": len(ids), "new": added}

        def fetch(report_id: str) -> str:
            status, _, body = session.request(f"/action/{report_id}", "GET")
            if status != 200:
                raise CollectorError("action page status is not 200")
            return body.decode("utf-8", errors="replace")

        counts = collect_batch(
            state, fetch, datetime.now(timezone.utc),
            max_fetches=budget, recheck_after=timedelta(hours=6),
            save=save_state, request_interval_seconds=1.5, sleep=time.sleep,
        )
        out["batch"] = counts
        out["requests"] = session.requests

        rows = render_sheet_rows(state)
        validate_sheet_rows(rows)
        sample = []
        for row in rows[1:]:
            sample.append({
                "report_id": row[0] == row[1],
                "project_id": row[3],
                "report_date": row[4],
                "corrector_present": bool(row[5]),
                "fio_len": len(row[6]),
                "fio_words": len(row[6].split()),
                "fio_letters_only": all(ch.isalpha() or ch in " -." for ch in row[6]) if row[6] else None,
                "source_status": row[7],
            })
        out["rows"] = {"total": len(rows) - 1, "sample": sample[:6]}
        out["checkpoint"] = {"path_suffix": "cache/j4b-report-reviewers/state.json",
                             "bytes": os.path.getsize(STATE_PATH) if os.path.exists(STATE_PATH) else 0}
        print(json.dumps(out, ensure_ascii=False, separators=(",", ":"))[:4500])
        return 0
    except Exception as exc:
        out["failure_category"] = "SAFE_PROBE_ERROR"
        out["error_class"] = type(exc).__name__
        out["error_message"] = str(exc)[:160]
        print(json.dumps(out, ensure_ascii=True, separators=(",", ":"))[:2500])
        return 0
    finally:
        if session is not None:
            session.close()


if __name__ == "__main__":
    raise SystemExit(main())
