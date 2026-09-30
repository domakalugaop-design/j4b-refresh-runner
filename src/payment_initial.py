"""One-shot, local, resumable INITIAL_2026 payment enrichment.

The checkpoint is normalized data only and must be stored outside every Git
working tree. This module intentionally does not run at import time.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any, Mapping
from urllib.error import URLError

from . import production
from .acquisition import Reader, acquire_project, discover_universe
from .google_service_account import google_token
from .payment_detail_xlsx import PaymentTransportError, PaymentWorkbookError, acquire_project_payment_assignments
from .payment_materialization import (
    PROJECT_PUBLICATION_COLUMNS,
    VISIT_PUBLICATION_COLUMNS,
    build_publication_payloads,
    materialize_payment_data,
    serialize_sheet_payload,
)
from .payment_refresh import (
    PAYMENT_STATES,
    SUCCESS_STATES,
    checkpoint_counts,
    load_checkpoint,
    new_checkpoint,
    pending_project_ids,
    publish_payment_pair,
    record_result,
    replace_by_project,
    require_complete,
    save_checkpoint,
)
from .portal_transport import PortalSession
from .refresh import PROJECT_TYPE_COLUMNS, api_get, read_sheet

EXPECTED_PROJECTS = 1780
YEAR = 2026
BATCH_SIZE = 25
CHECKPOINT_PATH = Path.home() / "Library/Application Support/J4B/payment-initial-2026/checkpoint.json"
BACKUP_PATH = Path.home() / "Library/Application Support/J4B/payment-initial-2026/prepublication-tabs.json"
PAYMENT_TABS = (production.PAYMENT_VISIT_TAB, production.PAYMENT_PROJECT_TAB)
PORTAL_KEYCHAIN_SERVICE = "j4b-web-login"


def _keychain_credential(account: str) -> str:
    """Read a Portal credential without exposing it in argv, logs, or errors."""
    result = subprocess.run(
        ["/usr/bin/security", "find-generic-password", "-s", PORTAL_KEYCHAIN_SERVICE,
         "-a", account, "-w"],
        capture_output=True,
        text=True,
        check=False,
    )
    value = result.stdout.rstrip("\r\n")
    if result.returncode != 0 or not value:
        raise RuntimeError(
            f"Portal Keychain credential unavailable (service={PORTAL_KEYCHAIN_SERVICE}, account={account})"
        )
    return value


def _initial_portal_session() -> PortalSession:
    if os.environ.get("PORTAL_LOGIN") and os.environ.get("PORTAL_PASSWORD"):
        return PortalSession()
    return PortalSession(
        base_url=os.environ.get("PORTAL_BASE_URL", "https://lk.j4b.ru"),
        login=_keychain_credential("login"),
        password=_keychain_credential("password"),
    )
NON_PAYMENT_TABS = {
    "projects_current": "A:AG",
    "project_types": "A:C",
    "Статусы проектов": "A:Y",
}


def _private_atomic_json(path: Path, value: Any) -> dict[str, Any]:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                         default=lambda item: str(item)).encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=".payment-private-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    reread = path.read_bytes()
    if reread != payload:
        raise RuntimeError("private payment snapshot local verification failed")
    return {"sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload), "path": str(path)}


def _assert_outside_git(path: Path) -> None:
    resolved = path.expanduser().resolve()
    for parent in (resolved, *resolved.parents):
        if (parent / ".git").exists():
            raise RuntimeError("payment checkpoint must remain outside Git repositories")


def _checkpoint_for_scope(path: Path, project_ids: list[str]) -> dict[str, Any]:
    _assert_outside_git(path)
    if path.exists():
        state = load_checkpoint(path)
        if state.get("mode") != "INITIAL_2026" or state.get("year") != YEAR:
            raise RuntimeError("existing local payment checkpoint mode/year mismatch")
        if state.get("scope") != project_ids:
            raise RuntimeError("existing local payment checkpoint universe differs from qualified scope")
        return state
    state = new_checkpoint(project_ids, mode="INITIAL_2026", year=YEAR)
    save_checkpoint(path, state)
    return state


def materialize_complete_checkpoint(checkpoint: Mapping[str, Any], expected_total: int = EXPECTED_PROJECTS) -> dict[str, list[list[Any]]]:
    """Build both complete incoming tabs from saved normalized state only."""
    require_complete(checkpoint, expected_total=expected_total)
    visits: list[list[Any]] = []
    projects: list[list[Any]] = []
    for pid in checkpoint["scope"]:
        result = checkpoint["projects"][pid]
        if result["status"] not in SUCCESS_STATES:
            raise RuntimeError("complete checkpoint contains non-success project")
        context = result.get("context") or {}
        data = materialize_payment_data(pid, context.get("project", {}), result.get("rows", []),
                                        context.get("workflow_memberships", []))
        if not all(data["invariants"].values()):
            raise RuntimeError(f"payment materialization invariant failure for project_id={pid}")
        payload = build_publication_payloads(data)
        visits.extend(payload[production.PAYMENT_VISIT_TAB][1:])
        projects.extend(payload[production.PAYMENT_PROJECT_TAB][1:])
    return {
        production.PAYMENT_VISIT_TAB: [list(VISIT_PUBLICATION_COLUMNS), *visits],
        production.PAYMENT_PROJECT_TAB: [list(PROJECT_PUBLICATION_COLUMNS), *projects],
    }


def _fingerprint_nonpayment_tabs(token: str, sid: str) -> dict[str, str]:
    meta = production.api(
        f"https://sheets.googleapis.com/v4/spreadsheets/{sid}?fields=sheets.properties(title,sheetId,gridProperties(rowCount,columnCount,frozenRowCount))",
        token,
    )
    properties = {
        sheet.get("properties", {}).get("title"): sheet.get("properties", {})
        for sheet in meta.get("sheets", [])
    }
    result: dict[str, str] = {}
    for title, columns in NON_PAYMENT_TABS.items():
        if title not in properties:
            raise RuntimeError(f"protected non-payment tab is missing: {title}")
        encoded = urllib.parse.quote(f"'{title}'!{columns}", safe="!:'")
        rows = api_get(
            f"https://sheets.googleapis.com/v4/spreadsheets/{sid}/values/{encoded}?valueRenderOption=UNFORMATTED_VALUE",
            token,
        ).get("values", [])
        canonical = json.dumps({"properties": properties[title], "values": rows}, ensure_ascii=False,
                               sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        result[title] = hashlib.sha256(canonical).hexdigest()
    return result


def _capture_payment_tabs(token: str, sid: str, meta: dict[str, Any]) -> tuple[dict[str, int], dict[str, list[list[Any]]], dict[str, int]]:
    original_ids = {
        sheet["properties"]["title"]: sheet["properties"]["sheetId"]
        for sheet in meta.get("sheets", [])
        if sheet.get("properties", {}).get("title") in PAYMENT_TABS
    }
    if set(original_ids) != set(PAYMENT_TABS):
        raise RuntimeError("expected payment tabs are missing")
    fresh = production.api(
        f"https://sheets.googleapis.com/v4/spreadsheets/{sid}?fields=properties.title,sheets.properties(title,sheetId,gridProperties(rowCount,columnCount))",
        token,
    )
    fresh_ids = {
        sheet["properties"]["title"]: sheet["properties"]["sheetId"]
        for sheet in fresh.get("sheets", [])
        if sheet.get("properties", {}).get("title") in PAYMENT_TABS
    }
    if fresh.get("properties", {}).get("title") != production.PRODUCTION_TITLE or fresh_ids != original_ids:
        raise RuntimeError("payment workbook/title/tab identity changed during INITIAL_2026 run")
    grid_row_counts = {
        sheet.get("properties", {}).get("title"): sheet.get("properties", {}).get("gridProperties", {}).get("rowCount")
        for sheet in fresh.get("sheets", [])
        if sheet.get("properties", {}).get("title") in PAYMENT_TABS
    }
    if set(grid_row_counts) != set(PAYMENT_TABS) or any(not isinstance(rows, int) or rows <= 0 for rows in grid_row_counts.values()):
        raise RuntimeError("payment tab grid row count metadata is unavailable")
    previous = {title: production._read_payment_tab(token, sid, title) for title in PAYMENT_TABS}
    expected = {
        production.PAYMENT_VISIT_TAB: list(VISIT_PUBLICATION_COLUMNS),
        production.PAYMENT_PROJECT_TAB: list(PROJECT_PUBLICATION_COLUMNS),
    }
    if any(not previous[title] or previous[title][0] != expected[title] for title in PAYMENT_TABS):
        raise RuntimeError("current payment tab header/schema mismatch")
    return fresh_ids, previous, grid_row_counts


def _validate_complete_candidate(candidate: Mapping[str, list[list[Any]]], scope: list[str],
                                 expected_project_ids: set[str]) -> None:
    expected = {
        production.PAYMENT_VISIT_TAB: list(VISIT_PUBLICATION_COLUMNS),
        production.PAYMENT_PROJECT_TAB: list(PROJECT_PUBLICATION_COLUMNS),
    }
    if set(candidate) != set(expected):
        raise RuntimeError("payment candidate tab set mismatch")
    for tab, headers in expected.items():
        rows = candidate[tab]
        if not rows or rows[0] != headers or any(len(row) != len(headers) for row in rows):
            raise RuntimeError(f"payment candidate schema mismatch: {tab}")
        if any(isinstance(value, float) for row in rows[1:] for value in row):
            raise RuntimeError("binary float in payment candidate")
    project_ids = [str(row[0]) for row in candidate[production.PAYMENT_PROJECT_TAB][1:]]
    if len(project_ids) != len(set(project_ids)) or set(project_ids) != expected_project_ids:
        raise RuntimeError("payment project candidate lost/duplicated projects outside selected scope")
    if not set(scope).issubset(set(project_ids)):
        raise RuntimeError("payment project candidate omitted a selected project")
    visit_keys = [(str(row[0]), str(row[4])) for row in candidate[production.PAYMENT_VISIT_TAB][1:]]
    if len(visit_keys) != len(set(visit_keys)):
        raise RuntimeError("duplicate visit grain in complete payment candidate")
    if any(pid not in expected_project_ids for pid, _ in visit_keys):
        raise RuntimeError("visit candidate contains a project outside its preserved project scope")


def _publish_complete_snapshot(token: str, sid: str, meta: dict[str, Any], incoming: dict[str, list[list[Any]]]) -> dict[str, Any]:
    sheet_ids, previous, grid_row_counts = _capture_payment_tabs(token, sid, meta)
    selected_ids = [row[0] for row in incoming[production.PAYMENT_PROJECT_TAB][1:]]
    # Replace all applicable project IDs, including successful zero-row projects.
    selected_scope = set(incoming.get("_scope", []))
    if not selected_scope:
        selected_scope = {str(pid) for pid in selected_ids}
    # Empty projects still belong to the replacement scope. Caller sets _scope.
    replacement_input = {tab: incoming[tab] for tab in PAYMENT_TABS}
    candidate = replace_by_project(previous, replacement_input, selected_scope)
    if candidate[production.PAYMENT_VISIT_TAB][0] != list(VISIT_PUBLICATION_COLUMNS):
        raise RuntimeError("visit payment payload schema mismatch")
    if candidate[production.PAYMENT_PROJECT_TAB][0] != list(PROJECT_PUBLICATION_COLUMNS):
        raise RuntimeError("project payment payload schema mismatch")
    old_project_ids = {str(row[0]) for row in previous[production.PAYMENT_PROJECT_TAB][1:] if row and row[0] not in (None, "")}
    expected_project_ids = (old_project_ids - selected_scope) | selected_scope
    _validate_complete_candidate(candidate, sorted(selected_scope, key=int), expected_project_ids)
    payload_hashes = {
        tab: hashlib.sha256(serialize_sheet_payload(candidate[tab]).encode("utf-8")).hexdigest()
        for tab in PAYMENT_TABS
    }
    backup = _private_atomic_json(BACKUP_PATH, {
        "version": 1,
        "spreadsheet_id": sid,
        "tabs": previous,
        "sheet_ids": sheet_ids,
        "grid_row_counts": grid_row_counts,
    })
    if backup["bytes"] <= 0:
        raise RuntimeError("payment tab backup is empty")
    before = _fingerprint_nonpayment_tabs(token, sid)
    result = publish_payment_pair(
        sheet_ids=sheet_ids,
        previous=previous,
        candidate=candidate,
        write_batch=lambda requests: production._exact_google_batch(token, sid, requests),
        read_tab=lambda title: production._read_payment_tab(token, sid, title),
        grid_row_counts=grid_row_counts,
    )
    try:
        after = _fingerprint_nonpayment_tabs(token, sid)
    except Exception as fingerprint_error:
        # A publication with an unverifiable protected-tab state is not a
        # successful transaction. Restore both owned payment tabs from the
        # pre-write snapshot and verify through the same atomic/readback path.
        restored = publish_payment_pair(
            sheet_ids=sheet_ids,
            previous=candidate,
            candidate=previous,
            write_batch=lambda requests: production._exact_google_batch(token, sid, requests),
            read_tab=lambda title: production._read_payment_tab(token, sid, title),
        )
        raise RuntimeError(
            f"non-payment fingerprint verification failed; payment rollback={restored['status']}"
        ) from fingerprint_error
    if before != after:
        # Non-payment mutation is outside contract. Restore the two owned tabs.
        restored = publish_payment_pair(
            sheet_ids=sheet_ids,
            previous=candidate,
            candidate=previous,
            write_batch=lambda requests: production._exact_google_batch(token, sid, requests),
            read_tab=lambda title: production._read_payment_tab(token, sid, title),
        )
        raise RuntimeError(f"non-payment tab fingerprint changed; payment rollback={restored['status']}")
    return {"publication": result, "backup": backup, "non_payment_fingerprints_unchanged": True,
            "rows": {tab: len(candidate[tab]) - 1 for tab in PAYMENT_TABS},
            "columns": {tab: len(candidate[tab][0]) for tab in PAYMENT_TABS},
            "payload_sha256": payload_hashes}


def run_initial_2026(*, checkpoint_path: Path = CHECKPOINT_PATH, batch_size: int = BATCH_SIZE,
                     expected_total: int = EXPECTED_PROJECTS) -> dict[str, Any]:
    """Run one complete local acquisition, then publish only after the full gate."""
    if batch_size <= 0:
        raise ValueError("INITIAL_2026 checkpoint interval must be positive")
    if expected_total != EXPECTED_PROJECTS:
        raise ValueError("INITIAL_2026 expected scope is fixed at 1,780 projects")
    sid = os.environ.get("GOOGLE_SPREADSHEET_ID", "").strip()
    if not sid:
        raise RuntimeError("INITIAL_2026 requires the existing production workbook ID from GOOGLE_SPREADSHEET_ID")
    token = google_token()
    meta = production._destination_preflight(token, sid)
    project_rows = read_sheet(token, sid, columns=list(PROJECT_TYPE_COLUMNS))
    # Project universe must come from the authenticated Portal session; resolve it before checkpoint validation.
    session = _initial_portal_session()
    try:
        session.login()
        catalogue = discover_universe(session)
        selected = production._select_reporting_year_scope(catalogue, project_rows, YEAR)
        selected_ids = [str(row["project_id"]) for row in selected]
        if len(selected_ids) != expected_total or len(selected_ids) != len(set(selected_ids)):
            raise RuntimeError(f"qualified INITIAL_2026 scope mismatch: expected={expected_total}, actual={len(selected_ids)}")
        checkpoint = _checkpoint_for_scope(checkpoint_path, selected_ids)
        already = len(selected_ids) - len(pending_project_ids(checkpoint))
        print(f"INITIAL_2026_SCOPE={len(selected_ids)} ALREADY_ACCEPTED={already}", flush=True)
        selected_by_id = {str(row["project_id"]): row for row in selected}
        retry_ids = pending_project_ids(checkpoint)
        reader = Reader(session, max(3 * len(retry_ids), 3))
        for batch in (retry_ids[index:index + batch_size] for index in range(0, len(retry_ids), batch_size)):
            for pid in batch:
                spec = selected_by_id[pid]
                try:
                    project, _ = acquire_project(reader, spec, float(os.environ.get("PORTAL_REQUEST_DELAY", "0.15")))
                    if project.get("acquisition_state") == "FAILED":
                        record_result(checkpoint, pid, "FAILED_TRANSPORT")
                        continue
                    if project.get("acquisition_state") != "ACQUIRED":
                        record_result(checkpoint, pid, "FAILED_SEMANTIC")
                        continue
                    payment_rows, status = acquire_project_payment_assignments(
                        pid, session, feature_enabled=True, timeout=60
                    )
                    context = {
                        "project": {key: project.get(key) for key in (
                            "project_id", "project_name", "client", "primary_manager"
                        )},
                        "workflow_memberships": [
                            {key: row.get(key) for key in ("project_id", "action_id", "visit_id", "workflow_state_code")}
                            for row in project.get("workflow_memberships", [])
                        ],
                    }
                    record_result(checkpoint, pid,
                                  "SUCCESS_WITH_ROWS" if payment_rows else "SUCCESS_ZERO_ROWS",
                                  payment_rows, context)
                except PaymentTransportError:
                    record_result(checkpoint, pid, "FAILED_TRANSPORT")
                except PaymentWorkbookError:
                    record_result(checkpoint, pid, "FAILED_PARSE")
                except (URLError, TimeoutError, OSError):
                    record_result(checkpoint, pid, "FAILED_TRANSPORT")
                except Exception:
                    record_result(checkpoint, pid, "FAILED_TRANSPORT")
            saved = save_checkpoint(checkpoint_path, checkpoint)
            counts = checkpoint_counts(checkpoint)
            print(f"CHECKPOINT rows={len(checkpoint['scope'])} accepted={counts['SUCCESS_WITH_ROWS'] + counts['SUCCESS_ZERO_ROWS']} pending={counts['PENDING']} failed={sum(counts[x] for x in PAYMENT_STATES - SUCCESS_STATES - {'PENDING'})} sha256={saved['sha256']}", flush=True)

        require_complete(checkpoint, expected_total=expected_total)
        print("INITIAL_2026_ACQUISITION_GATE=PASS", flush=True)
        incoming = materialize_complete_checkpoint(checkpoint, expected_total)
        incoming["_scope"] = list(checkpoint["scope"])
        publication = _publish_complete_snapshot(token, sid, meta, incoming)
        result = {"status": "PASS", "scope": len(selected_ids), "counts": checkpoint_counts(checkpoint),
                  "publication": publication}
        print("INITIAL_2026_FINAL_STATUS=PASS", flush=True)
        return result
    finally:
        session.close()


def main() -> int:
    try:
        result = run_initial_2026()
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str), flush=True)
        return 0
    except Exception as exc:
        print(json.dumps({"status": "INCOMPLETE_OR_FAILED", "error_type": type(exc).__name__,
                          "error": str(exc)}, ensure_ascii=False), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
