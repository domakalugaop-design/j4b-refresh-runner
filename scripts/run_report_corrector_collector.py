"""Deployment entry point for the report -> corrector collection.

Runs, in order: optional October re-discovery, bounded detail acquisition,
row rendering, validation, and (only when explicitly enabled) publication of
the ``report_reviewers`` tab.

Safety defaults: nothing is published without ``--publish`` *and*
``REPORT_REVIEWERS_PUBLICATION_ENABLED=true``; the run refuses to start inside
the production refresh windows; the checkpoint is private and outside the repo.

Usage:
    python3 scripts/run_report_corrector_collector.py --state /path/state.json \
        [--max-fetches 50] [--days-back 3] [--recheck ID] [--plan] [--publish]

Hard limits: at most 200 detail GETs per run and at least 1.5 s between them.
Stable reports (a known corrector, or a removed one) are never re-read on a
schedule; pass ``--recheck`` to re-read chosen IDs once.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.portal_transport import PortalSession  # noqa: E402
from src.report_corrector_collector import (  # noqa: E402
    DEFAULT_POLICY, MAX_DETAIL_GETS_PER_RUN, MIN_REQUEST_INTERVAL_SECONDS,
    START_DATE, collect_batch, coverage_summary, extract_registry_report_ids,
    mark_published, migrate_state, new_state, queue_remaining, reconcile_dataset,
    record_discovery, render_sheet_rows, request_manual_recheck, validate_sheet_rows,
)

SPREADSHEET_ID = "14PRG3HXsZaN4pVwV390PpMMqHupCW5j6RyI6HBzPQR8"
REFRESH_WINDOWS_MSK = ((9 * 60 + 55, 10 * 60 + 20), (16 * 60 + 55, 17 * 60 + 20))
BASE_URL = "https://lk.j4b.ru"


def _load_state(path: Path) -> dict:
    if not path.exists():
        return new_state()
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(state, stream, ensure_ascii=False, separators=(",", ":"))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def _moscow_now() -> datetime:
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("Europe/Moscow"))
    except Exception:
        return datetime.now(timezone.utc).astimezone()


def _in_refresh_window(now: datetime) -> bool:
    minutes = now.hour * 60 + now.minute
    return any(start <= minutes <= end for start, end in REFRESH_WINDOWS_MSK)


def _build_gateway():
    from src.google_service_account import google_token
    from src.report_corrector_sheet import SheetsGateway

    token = google_token()

    def get(url: str) -> dict:
        import urllib.error
        import urllib.request

        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                from src.report_corrector_sheet import SheetTabMissing

                raise SheetTabMissing("tab not found") from None
            raise

    def mutate(url: str, body: dict) -> dict:
        import urllib.request

        if os.environ.get("DRY_RUN", "false").strip().lower() == "true":
            raise RuntimeError("DRY_RUN blocked report_reviewers mutation")
        request = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)

    def put(url: str, values: list[list[str]]) -> dict:
        import urllib.request

        if os.environ.get("DRY_RUN", "false").strip().lower() == "true":
            raise RuntimeError("DRY_RUN blocked report_reviewers write")
        request = urllib.request.Request(
            url, data=json.dumps({"values": values}, ensure_ascii=False).encode("utf-8"),
            method="PUT",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)

    allow = os.environ.get("REPORT_REVIEWERS_PUBLICATION_ENABLED", "false").lower() in {"1", "true", "yes"}
    return SheetsGateway(SPREADSHEET_ID, get=get, mutate=mutate, put_values=put,
                         allow_mutation=allow)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="collect report -> corrector rows")
    parser.add_argument("--state", required=True, help="private checkpoint path")
    parser.add_argument("--max-fetches", type=int, default=50,
                        help=f"detail GETs this run (1..{MAX_DETAIL_GETS_PER_RUN})")
    parser.add_argument("--days-back", type=int, default=3,
                        help="re-discover the last N days (0 = whole month)")
    parser.add_argument("--interval", type=float, default=MIN_REQUEST_INTERVAL_SECONDS,
                        help=f"seconds between requests (>= {MIN_REQUEST_INTERVAL_SECONDS})")
    parser.add_argument("--recheck", action="append", default=[], metavar="REPORT_ID",
                        help="re-read this report id once (repeatable, comma separated)")
    parser.add_argument("--skip-discovery", action="store_true")
    parser.add_argument("--plan", action="store_true", help="read-only publication plan")
    parser.add_argument("--publish", action="store_true", help="publish the tab")
    args = parser.parse_args(argv)
    if not 0 <= args.max_fetches <= MAX_DETAIL_GETS_PER_RUN:
        print(json.dumps({"status": "REFUSED_MAX_FETCHES",
                          "limit": MAX_DETAIL_GETS_PER_RUN}, ensure_ascii=False))
        return 2
    if args.interval < MIN_REQUEST_INTERVAL_SECONDS:
        print(json.dumps({"status": "REFUSED_INTERVAL",
                          "minimum": MIN_REQUEST_INTERVAL_SECONDS}, ensure_ascii=False))
        return 2

    now_local = _moscow_now()
    if _in_refresh_window(now_local):
        print(json.dumps({"status": "REFUSED_PRODUCTION_WINDOW"}, ensure_ascii=False))
        return 0
    if args.plan and args.publish:
        print(json.dumps({"status": "REFUSED_AMBIGUOUS_MODE"}, ensure_ascii=False))
        return 2

    state_path = Path(args.state).expanduser()
    state = _load_state(state_path)
    report: dict = {"status": "OK", "requests": 0}
    # A v1 checkpoint is migrated in place: no history is dropped, only stale
    # scheduled rechecks of stable reports are cleared.
    report["migration"] = migrate_state(state, datetime.now(timezone.utc))
    _save_state(state_path, state)

    manual = [item.strip() for chunk in args.recheck for item in chunk.split(",") if item.strip()]
    if manual:
        report["manual_recheck"] = request_manual_recheck(
            state, manual, datetime.now(timezone.utc),
        )
        _save_state(state_path, state)

    needs_portal = (not args.skip_discovery) or args.max_fetches > 0
    credentials_path = os.environ.get("PORTAL_CREDENTIALS_FILE", "").strip()
    if needs_portal and not credentials_path:
        print(json.dumps({"status": "BLOCKED_MISSING_PORTAL_CREDENTIALS"}))
        return 2

    session = None
    if not args.skip_discovery:
        with open(credentials_path, encoding="utf-8") as stream:
            config = json.load(stream)
        session = PortalSession(timeout=90, base_url=BASE_URL,
                                login=config["login"], password=config["password"])
        session.login()

        today = now_local.date()
        first = START_DATE if args.days_back <= 0 else max(START_DATE, today - timedelta(days=args.days_back))
        added = 0
        late = 0
        day = first
        try:
            while day <= today:
                if session.requests:
                    import time

                    time.sleep(args.interval)
                ddmm = day.strftime("%d.%m.%Y")
                status, _, body = session.request(
                    "/report/", "POST", data={"dt1": ddmm, "dt2": ddmm, "limit": "10000"},
                )
                if status != 200:
                    raise RuntimeError(f"registry discovery failed with HTTP {status}")
                counts = record_discovery(
                    state, extract_registry_report_ids(body.decode("utf-8", errors="replace")),
                    discovered_for=day.isoformat(), at=datetime.now(timezone.utc),
                )
                added += counts["added"]
                late += counts["late_adds"]
                day += timedelta(days=1)
            _save_state(state_path, state)
            report["discovered"] = added
            report["late_ids"] = late
        finally:
            if session is not None:
                session.close()

    fetch_session = None
    try:
        if args.max_fetches > 0:
            with open(credentials_path, encoding="utf-8") as stream:
                config = json.load(stream)
            fetch_session = PortalSession(timeout=90, base_url=BASE_URL,
                                          login=config["login"], password=config["password"])
            fetch_session.login()

            def fetch(report_id: str) -> str:
                status, _, body = fetch_session.request(f"/action/{report_id}", "GET")
                if status != 200:
                    raise RuntimeError(f"detail fetch failed with HTTP {status}")
                return body.decode("utf-8", errors="replace")

            report["batch"] = collect_batch(
                state, fetch, datetime.now(timezone.utc), max_fetches=args.max_fetches,
                policy=DEFAULT_POLICY,
                save=lambda payload: _save_state(state_path, payload),
                request_interval_seconds=args.interval,
            )
    finally:
        if fetch_session is not None:
            fetch_session.close()

    rows = render_sheet_rows(state)
    validate_sheet_rows(rows)
    report["rows"] = len(rows) - 1
    report["unresolved"] = sum(
        1 for entry in state["reports"].values()
        if entry.get("detail") is None and entry.get("last_attempt_status") != "DATE_OUT_OF_RANGE"
    )
    # Corrector-only accounting: everything discovered but not published stays
    # in the checkpoint for later re-discovery and is reported, never dropped.
    def _status_count(status: str) -> int:
        return sum(
            1 for entry in state["reports"].values()
            if (entry.get("last_good") or {}).get("source_status") == status
        )

    report["no_corrector_excluded"] = _status_count("NO_CORRECTOR")
    report["no_corrector_exhausted"] = _status_count("NO_CORRECTOR_EXHAUSTED")
    report["recheck_pending"] = sum(
        1 for entry in state["reports"].values()
        if entry.get("next_recheck_at")
    )
    report["queue_remaining"] = queue_remaining(state, datetime.now(timezone.utc))
    report["coverage"] = coverage_summary(state)
    report["corrector_removed"] = sum(
        1 for entry in state["reports"].values()
        if (entry.get("last_good") or {}).get("source_status") == "CORRECTOR_REMOVED"
    )
    report["reconciliation"] = {
        key: value if isinstance(value, int) else len(value)
        for key, value in reconcile_dataset(state).items()
    }

    if args.plan or args.publish:
        from src.report_corrector_sheet import plan as plan_publication
        from src.report_corrector_sheet import publish

        gateway = _build_gateway()
        if args.plan:
            report["plan"] = plan_publication(rows, gateway)
        else:
            if len(rows) <= 1:
                report["status"] = "REFUSED_EMPTY_PUBLICATION"
                print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
                return 0
            report["publication"] = publish(
                rows, gateway, backup_dir=str(state_path.parent / "backups"),
            )
            mark_published(state, datetime.now(timezone.utc))
            _save_state(state_path, state)

    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
