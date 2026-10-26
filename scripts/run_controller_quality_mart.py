#!/usr/bin/env python3
"""Prepare or explicitly publish the separate controller-quality mart.

No mode is selected implicitly. ``--dry-run`` performs no I/O; ``--plan``
reads the collector result, source Sheet and Portal statuses but cannot write;
``--publish`` additionally requires CONTROLLER_QUALITY_PUBLICATION_ENABLED=true.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.acquisition import ACTION_STATE_CODES  # noqa: E402
from src.controller_quality_mart import (  # noqa: E402
    GenerationPublisher, MartError, read_systemd_collector_evidence, run_refresh,
    validate_historical_source, build_candidate, acquire_action_statuses,
    summarize_observed_metrics,
)
from src.controller_quality_sheets import GoogleSheetsGateway, read_report_reviewer_source  # noqa: E402
from src.google_service_account import google_token  # noqa: E402
from src.portal_transport import PortalSession  # noqa: E402

SOURCE_SPREADSHEET_ID = "14PRG3HXsZaN4pVwV390PpMMqHupCW5j6RyI6HBzPQR8"
HISTORICAL_TEST_SPREADSHEET_ID = "1G_pXXNCLTwABSnSPtXxRCxF19VOCOvXi-DK21ZMzytM"
PORTAL_BASE_URL = "https://lk.j4b.ru"
LOCK_DEFAULT = "/home/maxis/j4b-refresh-deployment/locks/production.lock"
PORTAL_KEYCHAIN_SERVICE = "j4b-web-login"


def _portal_credentials() -> tuple[str, str]:
    path_text = os.environ.get("PORTAL_CREDENTIALS_FILE", "").strip()
    if not path_text:
        return _portal_keychain_credentials()
    path = Path(path_text).expanduser()
    try:
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise MartError("portal credential file permissions are too broad")
        config = json.loads(path.read_text(encoding="utf-8"))
    except MartError:
        raise
    except Exception:
        raise MartError("portal credential file is unavailable or invalid") from None
    login, password = config.get("login"), config.get("password")
    if not isinstance(login, str) or not login or not isinstance(password, str) or not password:
        raise MartError("portal credential file schema is invalid")
    return login, password


def _portal_keychain_credentials() -> tuple[str, str]:
    """Reuse the established macOS Keychain contract without exposing values."""
    values: list[str] = []
    for account in ("login", "password"):
        try:
            result = subprocess.run(
                ["/usr/bin/security", "find-generic-password", "-s", PORTAL_KEYCHAIN_SERVICE,
                 "-a", account, "-w"],
                capture_output=True, text=True, check=False, timeout=10,
            )
        except Exception:
            raise MartError("Portal Keychain credential is unavailable") from None
        value = result.stdout.rstrip("\r\n")
        if result.returncode != 0 or not value:
            raise MartError("Portal Keychain credential is unavailable")
        values.append(value)
    return values[0], values[1]


def _source_and_target_gate() -> tuple[str, str]:
    target_id = os.environ.get("CONTROLLER_QUALITY_SPREADSHEET_ID", "").strip()
    if not target_id:
        raise MartError("owner-provisioned target spreadsheet id is required")
    source_id = os.environ.get("J4B_SOURCE_SPREADSHEET_ID", SOURCE_SPREADSHEET_ID).strip()
    if target_id == source_id:
        raise MartError("target must be a separate spreadsheet")
    return source_id, target_id


def _historical_destination_gate(source_id: str, target_id: str, enabled: str) -> None:
    if enabled.casefold() not in {"1", "true", "yes"}:
        raise MartError("historical backfill feature gate is disabled")
    if source_id != SOURCE_SPREADSHEET_ID or target_id != HISTORICAL_TEST_SPREADSHEET_ID:
        raise MartError("historical backfill source or TEST destination is not allowlisted")


def _collector_evidence() -> dict:
    return read_systemd_collector_evidence()


def _portal_fetcher(session: PortalSession):
    def fetch(project_id: str):
        from urllib.parse import urlsplit

        data = {
            "proj": json.dumps([project_id]),
            "dt1": f"01.01.{datetime.now().year}",
            "dt2": f"31.12.{datetime.now().year}",
            "limit": "10000", "send": "send", "user": "", "place": "", "city": "",
        }
        data.update({f"state[{code}]": "on" for code in ACTION_STATE_CODES})
        status, content_type, body = session.request(
            "/action", "POST", data=data, accept="text/html, application/json",
        )
        effective = urlsplit(session.last_effective_url or "")
        base = urlsplit(PORTAL_BASE_URL)
        if effective.scheme != base.scheme or effective.netloc != base.netloc or effective.path.rstrip("/") != "/action":
            raise MartError("Portal action response final URL is invalid")
        return status, content_type, body
    return fetch


def _emit(payload: dict) -> None:
    # Caller supplies allowlisted aggregate fields only.
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="controller-quality separate online mart")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="no network, filesystem or Sheets I/O")
    mode.add_argument("--plan", action="store_true", help="read-only source/Portal/target qualification")
    mode.add_argument("--publish", action="store_true", help="explicit generation-based target publication")
    mode.add_argument(
        "--historical-backfill", action="store_true",
        help="explicitly publish the reviewed accumulated four-day registry to the fixed TEST workbook",
    )
    args = parser.parse_args(argv)
    if args.dry_run:
        _emit({"status": "DRY_RUN_NO_IO", "writes": 0})
        return 0
    try:
        if args.historical_backfill:
            source_id = os.environ.get("J4B_SOURCE_SPREADSHEET_ID", SOURCE_SPREADSHEET_ID).strip()
            target_id = os.environ.get("CONTROLLER_QUALITY_SPREADSHEET_ID", "").strip()
            _historical_destination_gate(
                source_id, target_id,
                os.environ.get("CONTROLLER_QUALITY_HISTORICAL_BACKFILL_ENABLED", ""),
            )
            token = google_token()
            source_gateway = GoogleSheetsGateway(source_id, token)
            target_gateway = GoogleSheetsGateway(target_id, token)
            publisher = GenerationPublisher(target_gateway)
            publisher.validate_target()
            source_rows = read_report_reviewer_source(source_gateway)
            reports = validate_historical_source(
                source_rows,
                expected_fetch_day_counts={
                    "2026-10-22": 161, "2026-10-23": 200,
                    "2026-10-24": 188, "2026-10-25": 173,
                },
                expected_rows=722, expected_projects=57, expected_controllers=22,
            )
            login, password = _portal_credentials()
            session = PortalSession(timeout=90, base_url=PORTAL_BASE_URL, login=login, password=password)
            try:
                try:
                    session.login()
                except Exception:
                    raise MartError("Portal login failed") from None
                project_ids = sorted({row["project_id"] for row in reports}, key=int)
                action_rows, covered = acquire_action_statuses(
                    project_ids, _portal_fetcher(session),
                )
                action_scope = {
                    (row["project_id"], row["action_id"], row["visit_id"])
                    for row in reports
                }
                generated_at = datetime.now(timezone.utc)
                generation = uuid.uuid4().hex
                candidate = build_candidate(
                    reports, action_rows, queried_project_ids=covered,
                    generation_id=generation,
                    source_snapshot_at=generated_at.isoformat(),
                    action_scope_keys=action_scope,
                    refresh_status="PUBLISHED_HISTORICAL_BACKFILL",
                    source_freshness="ACCUMULATED_4_DAY_REGISTRY_CURRENT_STATUS",
                )
                if len(candidate["controller_report_snapshot"]) - 1 != 722:
                    raise MartError("historical backfill candidate report count changed")
                result = publisher.publish(candidate, generation_id=generation)
                metrics = summarize_observed_metrics(candidate)
                _emit({
                    "status": result, "mode": "HISTORICAL_BACKFILL_TEST_ONLY",
                    "generation_id": generation,
                    "source_reports": 722, "unique_visits": metrics["overall"]["observed_visits"],
                    "controllers": metrics["controllers_count"], "projects": metrics["projects_count"],
                    "status_39_reports": metrics["overall"]["status_39_reports"],
                    "status_39_visits": metrics["overall"]["status_39_visits"],
                    "status_39_known_reports": metrics["overall"]["eligible_reports"],
                    "status_39_share": metrics["overall"]["status_39_report_share"],
                    "status_39_visit_share": metrics["overall"]["status_39_visit_share"],
                    "unknown_status_records": candidate["data_quality_status"][1][11],
                    "portal_project_requests": len(project_ids), "google_source_writes": 0,
                    "production_writes": 0,
                })
            finally:
                session.close()
            return 0
        if args.publish and os.environ.get("CONTROLLER_QUALITY_PUBLICATION_ENABLED", "").casefold() not in {"1", "true", "yes"}:
            raise MartError("publication feature gate is disabled")
        collector = _collector_evidence()
        source_id, target_id = _source_and_target_gate()
        token = google_token()
        source_gateway = GoogleSheetsGateway(source_id, token)
        target_gateway = GoogleSheetsGateway(target_id, token)
        target_publisher = GenerationPublisher(target_gateway)
        target_publisher.validate_target()
        login, password = _portal_credentials()
        session = PortalSession(timeout=90, base_url=PORTAL_BASE_URL, login=login, password=password)
        try:
            try:
                session.login()
            except Exception:
                raise MartError("Portal login failed") from None
            publisher = target_publisher if args.publish else None
            result = run_refresh(
                report_rows=lambda: read_report_reviewer_source(source_gateway),
                fetch_action=_portal_fetcher(session), publisher=publisher,
                publish_enabled=args.publish, collector_evidence=collector,
                now=datetime.now(timezone.utc),
                lock_path=os.environ.get("J4B_SHARED_LOCK_PATH", LOCK_DEFAULT),
            )
            _emit(result)
        finally:
            session.close()
        return 0
    except Exception as exc:
        _emit({"status": "FAILED_CLOSED", "failure_class": type(exc).__name__, "writes": 0})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
