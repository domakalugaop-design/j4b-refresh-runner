from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from typing import Any

from .acquisition import Reader, acquire_project, discover_universe
from .portal_transport import PortalSession
from .refresh import (
    COLUMNS,
    google_token,
    materialize,
    merge_previous,
    now,
    publish,
    read_sheet,
    select_scope,
    sheet_rows,
    summary,
)


def _require_production_gate() -> str:
    if os.environ.get("RUN_MODE", "").strip().lower() != "production":
        raise RuntimeError("production entrypoint requires RUN_MODE=production")
    if os.environ.get("ALLOW_PRODUCTION_WRITE", "").strip().lower() != "true":
        raise RuntimeError("production entrypoint requires ALLOW_PRODUCTION_WRITE=true")
    if os.environ.get("PRODUCTION_CONFIRMATION", "").strip() != "J4B_PRODUCTION":
        raise RuntimeError("production entrypoint requires PRODUCTION_CONFIRMATION=J4B_PRODUCTION")
    sid = os.environ.get("GOOGLE_SPREADSHEET_ID", "").strip()
    if not sid:
        raise RuntimeError("missing required environment variable: GOOGLE_SPREADSHEET_ID")
    return sid


def _stage(name: str) -> None:
    print(name, flush=True)


def run() -> dict[str, Any]:
    sid = _require_production_gate()
    started_at = now()
    started_monotonic = time.monotonic()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + hashlib.sha1(os.urandom(8)).hexdigest()[:8]

    _stage("BASELINE_READ_START")
    token = google_token()
    previous = read_sheet(token, sid)
    if not previous or previous[0] != COLUMNS:
        raise RuntimeError("baseline sheet unavailable or schema mismatch")
    _stage(f"BASELINE_READ_PASS | rows={len(previous)-1} | unique={summary(previous)['unique']}")

    session = PortalSession()
    _stage("PORTAL_LOGIN_START")
    session.login()
    _stage("PORTAL_LOGIN_PASS")
    try:
        _stage("UNIVERSE_DISCOVERY_START")
        universe = discover_universe(session)
        _stage(f"UNIVERSE_DISCOVERY_PASS | universe={len(universe)}")

        selected = select_scope(universe, previous)
        _stage(f"SCOPE_SELECTION_PASS | selected={len(selected)}")
        reader = Reader(session, max(3 * len(selected), 3))
        projects: list[dict[str, Any]] = []
        visits: list[dict[str, Any]] = []
        acquisition_started = time.monotonic()

        for index, spec in enumerate(selected, start=1):
            project, project_visits = acquire_project(
                reader,
                spec,
                float(os.environ.get("PORTAL_REQUEST_DELAY", "0.15")),
            )
            projects.append(project)
            visits.extend(project_visits)
            if index % 10 == 0 or index == len(selected):
                failed = sum(p.get("acquisition_state") == "FAILED" for p in projects)
                success = len(projects) - failed
                elapsed = max(time.monotonic() - acquisition_started, 0.001)
                rate = len(projects) / elapsed * 60.0
                remaining = len(selected) - len(projects)
                eta_min = (remaining / rate) if rate > 0 else 0.0
                print(
                    f"ACQUISITION {len(projects)}/{len(selected)} | success={success} | failed={failed} | "
                    f"http={reader.count} | elapsed={elapsed:.0f}s | {rate:.1f} proj/min | ETA={eta_min:.1f} min",
                    flush=True,
                )

        timestamp = now()
        _stage("MATERIALIZATION_START")
        rows = materialize(projects, visits, timestamp)
        merged = merge_previous(rows, previous, {str(x["project_id"]) for x in selected}, timestamp)
        candidate = sheet_rows(merged)
        candidate_summary = summary(candidate)
        if candidate_summary["duplicates"] != 0:
            raise RuntimeError("candidate contains duplicate project IDs")
        if candidate_summary["unique"] < summary(previous)["unique"]:
            raise RuntimeError("candidate master shrinks previous unique project set")
        _stage(f"MATERIALIZATION_PASS | rows={candidate_summary['rows']} | unique={candidate_summary['unique']}")
        _stage("CANDIDATE_VALIDATION_PASS")

        _stage("PUBLISH_START")
        publish(token, sid, candidate, previous)
        _stage("PUBLISH_PASS")
        _stage("READBACK_PASS")
        _stage("TYPE_VALIDATION_PASS")

        failed = sum(p.get("acquisition_state") == "FAILED" for p in projects)
        finished_at = now()
        wall = time.monotonic() - started_monotonic
        report = {
            "RUN_ID": run_id,
            "STARTED_AT": started_at,
            "FINISHED_AT": finished_at,
            "WALL_SECONDS": round(wall, 3),
            "UNIVERSE_COUNT": len(universe),
            "SELECTED_COUNT": len(selected),
            "SUCCESS_COUNT": len(projects) - failed,
            "FAILED_COUNT": failed,
            "PORTAL_HTTP_REQUEST_COUNT": session.requests,
            "FINAL_MASTER_UNIQUE_IDS": candidate_summary["unique"],
            "FINAL_STATUS": "SUCCESS",
        }
        print("FINAL_STATUS=SUCCESS", flush=True)
        return report
    finally:
        session.close()


def main() -> int:
    try:
        report = run()
        print(json.dumps(report, ensure_ascii=False, sort_keys=True), flush=True)
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {"FINAL_STATUS": "FAILED", "ERROR_TYPE": type(exc).__name__, "ERROR_MESSAGE": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            flush=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
