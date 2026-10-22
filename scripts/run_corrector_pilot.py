"""Three-day corrector collection pilot (read-only, bounded).

Discovers a fixed window, enriches it within a hard GET budget, validates the
result locally, proves merge idempotency without a second portal pass, and runs
a read-only Sheets publication plan.

Never mutates the portal, the spreadsheet, or production state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.report_corrector_collector import (  # noqa: E402
    SHEET_COLUMNS, START_DATE, collect_batch, extract_registry_report_ids,
    merge_candidates, new_state, render_sheet_rows, validate_sheet_rows,
)

SPREADSHEET_ID = "14PRG3HXsZaN4pVwV390PpMMqHupCW5j6RyI6HBzPQR8"
BASE_URL = "https://lk.j4b.ru"
REFRESH_WINDOWS_MSK = ((9 * 60 + 55, 10 * 60 + 20), (16 * 60 + 55, 17 * 60 + 20))


def msk_now() -> datetime:
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("Europe/Moscow"))


def in_refresh_window(now: datetime) -> bool:
    minutes = now.hour * 60 + now.minute
    return any(start <= minutes <= end for start, end in REFRESH_WINDOWS_MSK)


def service_state() -> dict:
    def run(*args: str) -> str:
        try:
            return subprocess.run(args, capture_output=True, text=True, timeout=20).stdout.strip()
        except Exception:
            return "unknown"

    return {
        "service_active": run("systemctl", "is-active", "j4b-refresh.service"),
        "timer_enabled": run("systemctl", "is-enabled", "j4b-refresh.timer"),
    }


def load_state(path: Path) -> dict:
    if not path.exists():
        return new_state()
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(state, stream, ensure_ascii=False, separators=(",", ":"))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def state_hash(state: dict) -> str:
    payload = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def window_entries(state: dict, days: list[str]) -> dict:
    return {
        report_id: entry for report_id, entry in state["reports"].items()
        if entry.get("discovered_for") in days
    }


def per_day_summary(state: dict, days: list[str]) -> dict:
    counts = {day: {"discovered": 0, "processed": 0, "successful": 0, "errors": 0,
                    "remaining": 0, "corrector_ids": set(), "projects": set()} for day in days}
    for entry in state["reports"].values():
        day = entry.get("discovered_for")
        if day not in counts:
            continue
        bucket = counts[day]
        bucket["discovered"] += 1
        detail = entry.get("detail")
        status = entry.get("last_attempt_status")
        if detail is not None:
            bucket["processed"] += 1
            if status == "PASS":
                bucket["successful"] += 1
                if detail.get("corrector_id"):
                    bucket["corrector_ids"].add(detail["corrector_id"])
                bucket["projects"].add(detail["project_id"])
            else:
                bucket["errors"] += 1
        elif status == "DATE_OUT_OF_RANGE":
            bucket["processed"] += 1
        elif status and status != "PENDING":
            bucket["errors"] += 1
        else:
            bucket["remaining"] += 1
    return {
        day: {
            "discovered": value["discovered"],
            "processed": value["processed"],
            "successful": value["successful"],
            "errors": value["errors"],
            "remaining": value["remaining"],
            "correctors_unique": len(value["corrector_ids"]),
            "projects_unique": len(value["projects"]),
        }
        for day, value in counts.items()
    }


def validate_window(state: dict, days: list[str]) -> dict:
    entries = window_entries(state, days)
    resolved = {rid: e for rid, e in entries.items() if e.get("detail")}
    issues = {
        "join_mismatch": 0, "date_outside_window": 0, "duplicate_report_id": 0,
        "corrector_value_without_id": 0, "conflicting_id": 0, "non_numeric_id": 0,
        "missing_visit_or_project": 0, "no_corrector_assigned": 0,
    }
    visit_owners: dict[str, set[str]] = {}
    for report_id, entry in sorted(resolved.items(), key=lambda pair: int(pair[0])):
        detail = entry["detail"]
        # action_id is not stored separately: the parser already rejects any
        # page whose action link differs from the report id, so both columns
        # are the same verified value.
        action_id = detail.get("action_id", report_id)
        if detail["report_id"] != report_id or action_id != report_id:
            issues["join_mismatch"] += 1
        if not (days[0] <= detail["report_date"] <= days[-1]):
            issues["date_outside_window"] += 1
        if detail["corrector_state"] == "EMPTY":
            # Portal sentinel value="0": the report exists but carries no corrector.
            issues["no_corrector_assigned"] += 1
        elif not detail.get("corrector_id"):
            issues["corrector_value_without_id"] += 1
        if not all(str(detail[key]).isdigit() for key in ("report_id", "visit_id", "project_id")):
            issues["non_numeric_id"] += 1
        if not detail.get("visit_id") or not detail.get("project_id"):
            issues["missing_visit_or_project"] += 1
        visit_owners.setdefault(detail["visit_id"], set()).add(report_id)

    issues["conflicting_id"] = sum(1 for owners in visit_owners.values() if len(owners) > 1)

    rows = [list(SHEET_COLUMNS)]
    for report_id, entry in sorted(resolved.items(), key=lambda pair: int(pair[0])):
        if entry.get("last_attempt_status") != "PASS":
            continue
        detail = entry["detail"]
        good = entry.get("last_good") or {}
        rows.append([
            detail["report_id"], detail.get("action_id", detail["report_id"]),
            detail["visit_id"],
            detail["project_id"], detail["report_date"],
            detail.get("corrector_id") or "", detail.get("corrector_fio") or "",
            good.get("source_status", "PENDING"), good.get("last_successful_fetch", ""),
        ])

    duplicates = len(rows) - 1 - len({row[0] for row in rows[1:]})
    schema_ok = True
    try:
        validate_sheet_rows(rows)
    except Exception:
        schema_ok = False
    blocking = ("join_mismatch", "duplicate_report_id", "date_outside_window",
                "non_numeric_id", "conflicting_id", "corrector_value_without_id",
                "missing_visit_or_project")
    return {
        "rows_ready": len(rows) - 1,
        "resolved": len(resolved),
        "issues": issues,
        "schema_ok": schema_ok,
        "duplicate_rows": duplicates,
        "publishable": (
            schema_ok and duplicates == 0 and len(rows) > 1
            and not any(issues[key] for key in blocking)
        ),
    }


def sheets_dry_run(rows: list[list]) -> dict:
    from src.google_service_account import google_token
    from src.report_corrector_sheet import (
        PROTECTED_TABS, SheetTabMissing, SheetsGateway, plan,
    )
    import urllib.error
    import urllib.request

    token = google_token()

    def get(url: str) -> dict:
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                raise SheetTabMissing("tab not found") from None
            raise

    def blocked(*args, **kwargs):
        raise RuntimeError("mutation must not occur in dry-run")

    gateway = SheetsGateway(SPREADSHEET_ID, get=get, mutate=blocked, put_values=blocked,
                            allow_mutation=False)
    meta = gateway.metadata()
    before = gateway.fingerprints()
    outcome = plan(rows, gateway)
    after = gateway.fingerprints()
    return {
        **outcome,
        "tab_count": len(meta.titles),
        "protected_tabs": len(PROTECTED_TABS),
        "protected_all_present": all(title in meta.titles for title in PROTECTED_TABS),
        "protected_fingerprints_stable": before == after,
        "mutation_blocked": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="three-day corrector pilot")
    parser.add_argument("--state", required=True)
    parser.add_argument("--from", dest="start", default="2026-10-20")
    parser.add_argument("--to", dest="end", default="2026-10-22")
    parser.add_argument("--max-fetches", type=int, default=300)
    parser.add_argument("--interval", type=float, default=1.5)
    parser.add_argument("--deadline-minutes", type=float, default=40.0)
    parser.add_argument("--sheets-dry-run", action="store_true")
    args = parser.parse_args(argv)

    started = time.monotonic()
    now_local = msk_now()
    report: dict = {"pilot_period": [args.start, args.end]}

    days = []
    day = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    while day <= end:
        days.append(day.isoformat())
        day += timedelta(days=1)
    if not days:
        print(json.dumps({"status": "REFUSED_EMPTY_WINDOW"}))
        return 2
    report["precheck"] = {
        "in_refresh_window": in_refresh_window(now_local),
        "msk_time": now_local.isoformat(timespec="seconds"),
        **service_state(),
        "deadline_minutes": args.deadline_minutes,
        "max_fetches": args.max_fetches,
        "interval_seconds": args.interval,
    }
    if report["precheck"]["in_refresh_window"]:
        report["status"] = "REFUSED_PRODUCTION_WINDOW"
        print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
        return 0
    if report["precheck"]["service_active"] == "active":
        report["status"] = "REFUSED_PRODUCTION_RUNNING"
        print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
        return 0

    state_path = Path(args.state).expanduser()
    state = load_state(state_path)
    report["precheck"]["checkpoint_entries_before"] = len(state["reports"])

    from src.portal_transport import PortalSession

    credentials_path = os.environ["PORTAL_CREDENTIALS_FILE"]
    with open(credentials_path, encoding="utf-8") as stream:
        config = json.load(stream)

    session = PortalSession(timeout=90, base_url=BASE_URL,
                            login=config["login"], password=config["password"])
    session.login()
    discovery: dict[str, list[str]] = {}
    try:
        for index, day_text in enumerate(days):
            if index:
                time.sleep(args.interval)
            ddmm = date.fromisoformat(day_text).strftime("%d.%m.%Y")
            status, _, body = session.request(
                "/report/", "POST", data={"dt1": ddmm, "dt2": ddmm, "limit": "10000"},
            )
            if status != 200:
                raise RuntimeError(f"discovery failed with HTTP {status}")
            found = extract_registry_report_ids(body.decode("utf-8", errors="replace"))
            discovery[day_text] = list(found)
            added = merge_candidates(state, found, discovered_for=day_text)
            save_state(state_path, state)
            report.setdefault("discovery", {})[day_text] = {
                "ids": len(found), "new": added,
            }

        scope = {rid for rid, entry in state["reports"].items()
                 if entry.get("discovered_for") in days}
        report["scope"] = len(scope)
        report["precheck"]["checkpoint_entries_after_discovery"] = len(state["reports"])

        elapsed = time.monotonic() - started
        remaining_seconds = max(0.0, args.deadline_minutes * 60 - elapsed)
        budget = min(args.max_fetches, int(remaining_seconds // (args.interval + 2.6)))
        report["budget"] = {"max_fetches": args.max_fetches, "deadline_limited": budget}

        def fetch(report_id: str) -> str:
            status, _, body = session.request(f"/action/{report_id}", "GET")
            if status != 200:
                raise RuntimeError(f"detail failed with HTTP {status}")
            return body.decode("utf-8", errors="replace")

        report["batch"] = collect_batch(
            state, fetch, datetime.now(timezone.utc), max_fetches=budget,
            recheck_after=timedelta(hours=6),
            save=lambda payload: save_state(state_path, payload),
            request_interval_seconds=args.interval, sleep=time.sleep, scope=scope,
        )
        report["total_http_requests"] = session.requests
    finally:
        session.close()

    report["per_day"] = per_day_summary(state, days)
    report["validation"] = validate_window(state, days)
    report["checkpoint"] = {
        "entries": len(state["reports"]),
        "window_entries": len(window_entries(state, days)),
        "bytes": state_path.stat().st_size,
        "mode": oct(state_path.stat().st_mode & 0o777),
        "dir_mode": oct(state_path.parent.stat().st_mode & 0o777),
    }

    # Idempotency: replay the same discovery set without any portal traffic.
    hash_before = state_hash(state)
    replayed = sum(merge_candidates(state, discovery[day_text], discovered_for=day_text)
                   for day_text in days)
    hash_after = state_hash(state)
    from src.report_corrector_collector import pending_report_ids

    still_pending = pending_report_ids(state, datetime.now(timezone.utc), 10_000,
                                       recheck_after=timedelta(hours=6), scope=scope)
    report["idempotency"] = {
        "replay_added": replayed,
        "state_unchanged": hash_before == hash_after,
        "window_pending_after_run": len(still_pending),
    }

    if args.sheets_dry_run:
        rows = render_sheet_rows(state)
        window_rows = [list(SHEET_COLUMNS)]
        for row in rows[1:]:
            if row[4] >= days[0] and row[4] <= days[-1]:
                window_rows.append(row)
        validate_sheet_rows(window_rows)
        report["sheets_dry_run"] = sheets_dry_run(window_rows)
        report["sheets_dry_run"]["rows_in_plan"] = len(window_rows) - 1

    duration = time.monotonic() - started
    processed = sum(1 for entry in window_entries(state, days).values()
                    if entry.get("detail") is not None)
    report["metrics"] = {
        "total_duration_seconds": round(duration, 1),
        "average_seconds_per_report": round(duration / max(1, processed), 2),
        "requests_per_report": round(report.get("total_http_requests", 0) / max(1, processed), 2),
    }
    report["status"] = "OK"
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
