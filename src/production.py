from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError

from .acquisition import Reader, acquire_project, discover_universe
from .portal_transport import PortalSession
from .google_service_account import google_token
from .project_types import (
    PROJECT_TYPE_DICTIONARY,
    STATE_COLUMNS,
    apply_canonical_project_names,
    acquire_pending_types,
    materialize_project_types,
    project_type_applicable_ids,
    validate_state_rows,
)
from .refresh import (
    COLUMNS,
    BASE_COLUMNS,
    PROJECT_TYPE_COLUMNS as PROJECT_TYPE_SCHEMA,
    materialize,
    merge_previous,
    now,
    publish,
    publish_project_type_refresh,
    read_sheet,
    read_project_type_state_rows,
    select_scope,
    sheet_rows,
    summary,
    api,
)

PRODUCTION_TITLE = "J4B Portal — DataLens Materialized Layer"


def _destination_preflight(token: str, sid: str, expected_title: str = PRODUCTION_TITLE) -> dict[str, Any]:
    meta = api(
        f"https://sheets.googleapis.com/v4/spreadsheets/{sid}?fields=spreadsheetId,properties.title,sheets.properties(title,sheetId,gridProperties(columnCount,rowCount))",
        token,
    )
    title = meta.get("properties", {}).get("title")
    if meta.get("spreadsheetId") != sid or title != expected_title:
        raise RuntimeError("production destination title verification failed")
    return meta


def _plan_project_type_layout(meta: dict[str, Any]) -> dict[str, Any]:
    """Describe a required schema migration without mutating the workbook."""
    sheets = meta.get("sheets", [])
    current = next((s.get("properties", {}) for s in sheets if s.get("properties", {}).get("title") == "projects_current"), None)
    if not current:
        raise RuntimeError("projects_current tab not found")
    column_count = current.get("gridProperties", {}).get("columnCount")
    if column_count not in (len(BASE_COLUMNS), len(PROJECT_TYPE_SCHEMA)):
        raise RuntimeError(f"projects_current unexpected column count: {column_count}")
    state = next((s.get("properties", {}) for s in sheets if s.get("properties", {}).get("title") == "project_types"), None)
    requests: list[dict[str, Any]] = []
    if column_count == len(BASE_COLUMNS):
        requests.append({"appendDimension": {"sheetId": current["sheetId"], "dimension": "COLUMNS", "length": 2}})
    if not state:
        requests.append({"addSheet": {"properties": {"title": "project_types", "gridProperties": {"rowCount": 1000, "columnCount": 3, "frozenRowCount": 1}}}})
    return {
        "projects_sheet_id": current["sheetId"],
        "project_types_sheet_id": state.get("sheetId") if state else None,
        "source_column_count": column_count,
        "state_exists": state is not None,
        "requests": requests,
        "planned": (["EXPAND projects_current A:AE -> A:AG"] if column_count == len(BASE_COLUMNS) else [])
        + (["CREATE project_types"] if not state else []),
    }


def _execute_project_type_layout(token: str, sid: str, plan: dict[str, Any]) -> dict[str, Any]:
    """Execute a previously validated migration plan after write authorization."""
    if plan["requests"]:
        api(f"https://sheets.googleapis.com/v4/spreadsheets/{sid}:batchUpdate", token, {"requests": plan["requests"]})
    fresh = api(
        f"https://sheets.googleapis.com/v4/spreadsheets/{sid}?fields=spreadsheetId,properties.title,sheets.properties(title,sheetId,gridProperties(columnCount,rowCount))",
        token,
    )
    fresh_sheets = fresh.get("sheets", [])
    current = next((s.get("properties", {}) for s in fresh_sheets if s.get("properties", {}).get("title") == "projects_current"), None)
    state = next((s.get("properties", {}) for s in fresh_sheets if s.get("properties", {}).get("title") == "project_types"), None)
    if not current or current.get("gridProperties", {}).get("columnCount") != len(PROJECT_TYPE_SCHEMA) or not state:
        raise RuntimeError("Project Type schema migration failed")
    api(
        f"https://sheets.googleapis.com/v4/spreadsheets/{sid}:batchUpdate",
        token,
        {"requests": [
            {"repeatCell": {"range": {"sheetId": current["sheetId"], "startRowIndex": 1, "startColumnIndex": len(BASE_COLUMNS), "endColumnIndex": len(BASE_COLUMNS) + 1}, "cell": {"userEnteredFormat": {"numberFormat": {"type": "TEXT"}}}, "fields": "userEnteredFormat.numberFormat"}},
            {"repeatCell": {"range": {"sheetId": state["sheetId"], "startRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": 2}, "cell": {"userEnteredFormat": {"numberFormat": {"type": "TEXT"}}}, "fields": "userEnteredFormat.numberFormat"}},
            {"autoResizeDimensions": {"dimensions": {"sheetId": current["sheetId"], "dimension": "COLUMNS", "startIndex": len(BASE_COLUMNS), "endIndex": len(PROJECT_TYPE_SCHEMA)}}},
            {"autoResizeDimensions": {"dimensions": {"sheetId": state["sheetId"], "dimension": "COLUMNS", "startIndex": 0, "endIndex": 3}}},
        ]},
    )
    return {"sheets": fresh_sheets, "project_types_sheet_id": state["sheetId"]}


def _rollback_project_type_layout(token: str, sid: str, plan: dict[str, Any], previous_raw: list[list[Any]]) -> None:
    """Restore the pre-migration schema after an authorized cutover failure."""
    meta = api(
        f"https://sheets.googleapis.com/v4/spreadsheets/{sid}?fields=spreadsheetId,sheets.properties(title,sheetId,gridProperties(columnCount))",
        token,
    )
    sheets = meta.get("sheets", [])
    current = next((s.get("properties", {}) for s in sheets if s.get("properties", {}).get("title") == "projects_current"), None)
    state = next((s.get("properties", {}) for s in sheets if s.get("properties", {}).get("title") == "project_types"), None)
    if not current:
        raise RuntimeError("projects_current missing during schema rollback")
    requests: list[dict[str, Any]] = []
    if not plan["state_exists"] and state:
        requests.append({"deleteSheet": {"sheetId": state["sheetId"]}})
    if plan["source_column_count"] == len(BASE_COLUMNS):
        actual_count = current.get("gridProperties", {}).get("columnCount", 0)
        if actual_count > len(BASE_COLUMNS):
            requests.append({"deleteDimension": {"range": {"sheetId": current["sheetId"], "dimension": "COLUMNS", "startIndex": len(BASE_COLUMNS), "endIndex": actual_count}}})
    if requests:
        api(f"https://sheets.googleapis.com/v4/spreadsheets/{sid}:batchUpdate", token, {"requests": requests})
    restored_meta = api(
        f"https://sheets.googleapis.com/v4/spreadsheets/{sid}?fields=spreadsheetId,sheets.properties(title,sheetId,gridProperties(columnCount))",
        token,
    )
    restored_sheets = restored_meta.get("sheets", [])
    restored_current = next((s.get("properties", {}) for s in restored_sheets if s.get("properties", {}).get("title") == "projects_current"), None)
    restored_state = next((s.get("properties", {}) for s in restored_sheets if s.get("properties", {}).get("title") == "project_types"), None)
    expected_count = plan["source_column_count"]
    if not restored_current or restored_current.get("gridProperties", {}).get("columnCount") != expected_count:
        raise RuntimeError("schema rollback column-count readback mismatch")
    if not plan["state_exists"] and restored_state:
        raise RuntimeError("schema rollback failed to remove newly created project_types")
    rollback_columns = BASE_COLUMNS if plan["source_column_count"] == len(BASE_COLUMNS) else PROJECT_TYPE_SCHEMA
    if previous_raw and read_sheet(token, sid, columns=rollback_columns) != previous_raw:
        raise RuntimeError("schema rollback projects_current readback mismatch")


def _load_bootstrap_seed(path: str, expected_sha256: str, target_ids: set[str], universe: list[dict[str, Any]]) -> tuple[list[list[str]], dict[str, tuple[str, str]], str]:
    import stat as stat_module

    file_stat = os.stat(path)
    if stat_module.S_IMODE(file_stat.st_mode) & 0o077:
        raise ValueError("bootstrap artifact permissions must be 0600 or stricter")
    with open(path, "rb") as source:
        content = source.read()
    actual_sha256 = hashlib.sha256(content).hexdigest()
    if not expected_sha256 or actual_sha256.lower() != expected_sha256.strip().lower():
        raise ValueError("bootstrap artifact SHA-256 mismatch")
    decoded = json.loads(content.decode("utf-8"))
    if not isinstance(decoded, list):
        raise ValueError("bootstrap artifact must be a JSON list")
    rows: list[list[Any]] = [STATE_COLUMNS]
    seen: set[str] = set()
    applicable_ids = project_type_applicable_ids(universe)
    for entry in decoded:
        if not isinstance(entry, dict) or set(entry) != set(STATE_COLUMNS):
            raise ValueError("bootstrap rows must contain exactly project_id, project_type_code, project_type_name")
        pid = str(entry["project_id"])
        code = entry["project_type_code"]
        name = entry["project_type_name"]
        if not pid.isdigit() or pid in seen:
            raise ValueError(f"invalid or duplicate bootstrap project_id: {pid}")
        if pid not in target_ids:
            raise ValueError(f"bootstrap project_id missing from target: {pid}")
        if pid not in applicable_ids:
            raise ValueError(f"bootstrap project_id is pre-cutoff or has no qualified period: {pid}")
        if not isinstance(code, str) or code not in PROJECT_TYPE_DICTIONARY:
            raise ValueError(f"unknown bootstrap project type code for project_id {pid}")
        if name != PROJECT_TYPE_DICTIONARY[code]:
            raise ValueError(f"bootstrap name/dictionary mismatch for project_id {pid}")
        seen.add(pid)
        rows.append([pid, code, name])
    state = validate_state_rows(rows)
    return rows, state, actual_sha256


def _resolve_project_type_state(
    state_exists: bool,
    persisted_rows: list[list[Any]],
    seed_path: str,
    seed_sha256: str,
    target_ids: set[str],
    universe: list[dict[str, Any]],
) -> tuple[list[list[Any]], dict[str, tuple[str, str]], str, str]:
    """Use persisted state when present; bootstrap is strictly absent-state only."""
    if state_exists:
        state = validate_state_rows(persisted_rows)
        applicable_ids = project_type_applicable_ids(universe)
        if not set(state).issubset(target_ids):
            raise ValueError("persisted project_types state references project absent from target")
        if not set(state).issubset(applicable_ids):
            raise ValueError("persisted project_types state includes pre-0926 or unqualified project")
        return persisted_rows, state, "PERSISTED_STATE", "NONE"
    if not seed_path or not seed_sha256:
        raise ValueError("project_types state is absent; explicit validated bootstrap artifact is required")
    rows, state, digest = _load_bootstrap_seed(seed_path, seed_sha256, target_ids, universe)
    return rows, state, "LOCAL_VALIDATED_ARTIFACT", digest


def _upgrade_projects_rows(rows: list[list[Any]], source_columns: list[str]) -> list[list[Any]]:
    if not rows:
        raise RuntimeError("baseline sheet unavailable or schema mismatch")
    if source_columns == PROJECT_TYPE_SCHEMA:
        if rows[0] == PROJECT_TYPE_SCHEMA:
            return [list(row) + [""] * max(0, len(PROJECT_TYPE_SCHEMA) - len(row)) for row in rows]
        if rows[0] == BASE_COLUMNS:
            return [PROJECT_TYPE_SCHEMA] + [list(row) + [""] * 2 for row in rows[1:]]
        raise RuntimeError("baseline sheet unavailable or schema mismatch")
    if rows[0] != source_columns:
        raise RuntimeError("baseline sheet unavailable or schema mismatch")
    if source_columns != BASE_COLUMNS:
        raise RuntimeError("unsupported projects_current source schema")
    return [PROJECT_TYPE_SCHEMA] + [list(row) + ["", ""] for row in rows[1:]]


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


def _production_target() -> str:
    if os.environ.get("RUN_MODE", "").strip().lower() != "production":
        raise RuntimeError("production entrypoint requires RUN_MODE=production")
    sid = os.environ.get("GOOGLE_SPREADSHEET_ID", "").strip()
    if not sid:
        raise RuntimeError("missing required environment variable: GOOGLE_SPREADSHEET_ID")
    return sid


def _stage(name: str) -> None:
    print(name, flush=True)


def run() -> dict[str, Any]:
    run_mode = os.environ.get("RUN_MODE", "test").strip().lower()
    dry_run = os.environ.get("DRY_RUN", "false").strip().lower() == "true"
    if run_mode != "production":
        raise RuntimeError("RUN_MODE must be production")
    sid = _production_target()
    started_at = now()
    started_monotonic = time.monotonic()
    # Production is already on the persisted 33-column Project Type contract.
    # Keep this schema unconditional so a missing feature flag cannot silently
    # publish the legacy 31-column layout over AF:AG.
    project_type_enabled = True
    columns = PROJECT_TYPE_SCHEMA
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + hashlib.sha1(os.urandom(8)).hexdigest()[:8]

    _stage("BASELINE_READ_START")
    token = google_token()
    destination_meta = _destination_preflight(token, sid)
    _stage("PRODUCTION_DESTINATION_GUARD_PASS")

    layout_plan: dict[str, Any] = {"planned": [], "requests": [], "state_exists": False}
    previous_raw: list[list[Any]] = []
    bootstrap_source = "NONE"
    bootstrap_rows = 0
    bootstrap_sha256 = "NONE"
    if project_type_enabled and destination_meta:
        current_meta = next((s.get("properties", {}) for s in destination_meta.get("sheets", []) if s.get("properties", {}).get("title") == "projects_current"), {})
        physical_columns = current_meta.get("gridProperties", {}).get("columnCount")
        source_columns = BASE_COLUMNS if physical_columns == len(BASE_COLUMNS) else PROJECT_TYPE_SCHEMA if physical_columns == len(PROJECT_TYPE_SCHEMA) else []
        previous_raw = read_sheet(token, sid, columns=source_columns) if source_columns else []
        previous = _upgrade_projects_rows(previous_raw, source_columns)
        layout_plan = _plan_project_type_layout(destination_meta)
        _stage(f"PROJECT_TYPE_BASELINE_SCHEMA | source_columns={len(source_columns)} | rows={len(previous)-1}")
        if layout_plan["state_exists"]:
            previous_state_rows = read_project_type_state_rows(token, sid)
            state = validate_state_rows(previous_state_rows)
        else:
            previous_state_rows = []
            state = {}
    else:
        previous = read_sheet(token, sid, columns=columns)
        if not previous or previous[0] != columns:
            raise RuntimeError("baseline sheet unavailable or schema mismatch")
        previous_state_rows = []
        state = {}
    _stage(f"BASELINE_READ_PASS | rows={len(previous)-1} | unique={summary(previous)['unique']}")

    session = PortalSession()
    _stage("PORTAL_LOGIN_START")
    session.login()
    _stage("PORTAL_LOGIN_PASS")
    try:
        _stage("UNIVERSE_DISCOVERY_START")
        universe = discover_universe(session)
        _stage(f"UNIVERSE_DISCOVERY_PASS | universe={len(universe)}")

        type_telemetry = None
        applicable_ids: set[str] = set()
        if project_type_enabled:
            previous_ids = {str(r[0]) for r in previous[1:] if r and r[0] not in (None, "")}
            previous_state_rows, state, bootstrap_source, bootstrap_sha256 = _resolve_project_type_state(
                bool(layout_plan["state_exists"]),
                previous_state_rows,
                os.environ.get("PROJECT_TYPE_BOOTSTRAP_PATH", "").strip(),
                os.environ.get("PROJECT_TYPE_BOOTSTRAP_SHA256", "").strip(),
                previous_ids,
                universe,
            )
            bootstrap_rows = len(previous_state_rows) - 1 if bootstrap_source == "LOCAL_VALIDATED_ARTIFACT" else 0
            if bootstrap_source == "LOCAL_VALIDATED_ARTIFACT":
                _stage(f"PROJECT_TYPE_BOOTSTRAP_VALIDATED | rows={bootstrap_rows} | sha256={bootstrap_sha256}")
            else:
                _stage(f"PROJECT_TYPE_PERSISTED_STATE_AUTHORITATIVE | rows={len(state)}")
            _stage("PROJECT_TYPE_ACQUISITION_START")
            state, type_telemetry, applicable_ids = acquire_pending_types(
                session,
                universe,
                state,
                float(os.environ.get("PORTAL_REQUEST_DELAY", "0.15")),
            )
            _stage(
                f"PROJECT_TYPE_ACQUISITION_PASS | applicable={type_telemetry.applicable_projects} | "
                f"assigned_before={type_telemetry.already_assigned} | pending_before={type_telemetry.pending_before} | "
                f"valid={type_telemetry.valid_assignments_acquired} | http_additional={type_telemetry.detail_get_additional}"
            )

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
                semantic_failed = sum(p.get("acquisition_state") == "SEMANTIC_FAILURE" for p in projects)
                success = len(projects) - failed - semantic_failed
                elapsed = max(time.monotonic() - acquisition_started, 0.001)
                rate = len(projects) / elapsed * 60.0
                remaining = len(selected) - len(projects)
                eta_min = (remaining / rate) if rate > 0 else 0.0
                print(
                    f"ACQUISITION {len(projects)}/{len(selected)} | success={success} | failed={failed} | semantic_failed={semantic_failed} | "
                    f"http={reader.count} | elapsed={elapsed:.0f}s | {rate:.1f} proj/min | ETA={eta_min:.1f} min",
                    flush=True,
                )

        timestamp = now()
        _stage("MATERIALIZATION_START")
        rows = materialize(projects, visits, timestamp)
        merged = merge_previous(rows, previous, {str(x["project_id"]) for x in selected}, timestamp, columns=columns)
        if project_type_enabled:
            apply_canonical_project_names(merged, universe)
            applicable_ids = project_type_applicable_ids(universe)
            materialize_project_types(merged, state, applicable_ids)
        candidate = sheet_rows(merged, columns=columns)
        candidate_summary = summary(candidate)
        if candidate_summary["duplicates"] != 0:
            raise RuntimeError("candidate contains duplicate project IDs")
        if candidate_summary["unique"] < summary(previous)["unique"]:
            raise RuntimeError("candidate master shrinks previous unique project set")
        if project_type_enabled:
            candidate_ids = {str(r[0]) for r in candidate[1:] if r and r[0] not in (None, "")}
            if not set(state).issubset(candidate_ids):
                raise RuntimeError("candidate omits immutable Project Type assignments")
            if not applicable_ids.issuperset(state):
                raise RuntimeError("candidate contains pre-0926 Project Type assignment")
            layout_plan["planned"] = layout_plan.get("planned", []) + (["WRITE project_types state"] if state != validate_state_rows(previous_state_rows) else [])
            layout_plan["planned"] = layout_plan.get("planned", []) + ["WRITE projects_current A:AG"]
        _stage(f"MATERIALIZATION_PASS | rows={candidate_summary['rows']} | unique={candidate_summary['unique']}")
        _stage("CANDIDATE_VALIDATION_PASS")
        _stage("DIFF_VALIDATION_PASS")

        if dry_run:
            _stage("DRY_RUN_PASS | TARGET_SHEET_WRITES=0 | EXECUTED_SCHEMA_MUTATIONS=0")
            return {
                "RUN_ID": run_id,
                "FINAL_STATUS": "DRY_RUN_PASS",
                "DRY_RUN": True,
                "TARGET_SHEET_WRITES": 0,
                "EXECUTED_SCHEMA_MUTATIONS": 0,
                "PLANNED_SCHEMA_MUTATIONS": layout_plan.get("planned", []),
                "BOOTSTRAP_SOURCE": bootstrap_source,
                "BOOTSTRAP_ROWS": bootstrap_rows,
                "BOOTSTRAP_SHA256": bootstrap_sha256,
                "CANDIDATE_ROWS": candidate_summary["rows"],
                "CANDIDATE_UNIQUE_IDS": candidate_summary["unique"],
                "CANDIDATE_DUPLICATES": candidate_summary["duplicates"],
                "BASELINE_ROWS": len(previous) - 1,
                "BOOTSTRAP_VALID": bool(bootstrap_rows or layout_plan["state_exists"]),
            }

        # Authorization is deliberately late: all acquisition, candidate validation,
        # bootstrap validation, and diff planning above are read-only.
        if project_type_enabled:
            _require_production_gate()
            _stage("WRITE_AUTHORIZATION_PASS")
            _stage("MIGRATION_START")
            try:
                _execute_project_type_layout(token, sid, layout_plan)
                _stage("MIGRATION_PASS")
                _stage("PUBLISH_START")
                publish_project_type_refresh(token, sid, candidate, previous, columns, state, previous_state_rows)
                _stage("PUBLISH_PASS")
            except Exception:
                if layout_plan.get("planned"):
                    _rollback_project_type_layout(token, sid, layout_plan, previous_raw)
                raise
        else:
            if run_mode == "production":
                _require_production_gate()
            publish(token, sid, candidate, previous, columns=columns)
        _stage("READBACK_PASS")
        _stage("TYPE_VALIDATION=EXTERNAL_POSTCHECK_REQUIRED")

        failed = sum(p.get("acquisition_state") == "FAILED" for p in projects)
        semantic_failed = sum(p.get("acquisition_state") == "SEMANTIC_FAILURE" for p in projects)
        baseline_ids = {str(r[0]) for r in previous[1:] if r and r[0] != ""}
        selected_ids = {str(x["project_id"]) for x in selected}
        final_ids = {str(r[0]) for r in candidate[1:] if r and r[0] != ""}
        finished_at = now()
        wall = time.monotonic() - started_monotonic
        report = {
            "RUN_ID": run_id,
            "STARTED_AT": started_at,
            "FINISHED_AT": finished_at,
            "WALL_SECONDS": round(wall, 3),
            "UNIVERSE_COUNT": len(universe),
            "SELECTED_COUNT": len(selected),
            "SUCCESS_COUNT": len(projects) - failed - semantic_failed,
            "FAILED_COUNT": failed,
            "SEMANTIC_FAILURE_COUNT": semantic_failed,
            "SEMANTIC_FAILURE_REASONS": {
                str(p["project_id"]): p.get("acquisition_failure_reasons", [])
                for p in projects
                if p.get("acquisition_state") == "SEMANTIC_FAILURE"
            },
            "BASELINE_ROWS": len(previous) - 1,
            "BASELINE_UNIQUE_IDS": summary(previous)["unique"],
            "BASELINE_HAS_8110": "8110" in baseline_ids,
            "FINAL_ROWS": candidate_summary["rows"],
            "FINAL_UNIQUE_IDS": candidate_summary["unique"],
            "FINAL_DUPLICATES": candidate_summary["duplicates"],
            "FINAL_HAS_8110": "8110" in final_ids,
            "RETAINED_UNSELECTED_ROWS": len(baseline_ids - selected_ids),
            "NEW_PROJECTS": len(selected_ids - baseline_ids),
            "DROPPED_PREVIOUS_IDS": len(baseline_ids - final_ids),
            "PORTAL_HTTP_REQUEST_COUNT": session.requests,
            "PORTAL_AUTH_GETS": session.auth_get_count,
            "PORTAL_AUTH_POSTS": session.auth_post_count,
            "PORTAL_READ_FILTER_POSTS": reader.post_count,
            "PORTAL_DATA_MUTATION_POSTS": 0,
            "PROJECT_TYPE_APPLICABLE_PROJECTS": type_telemetry.applicable_projects if type_telemetry else 0,
            "PROJECT_TYPE_ALREADY_ASSIGNED": type_telemetry.already_assigned if type_telemetry else 0,
            "PROJECT_TYPE_PENDING_BEFORE": type_telemetry.pending_before if type_telemetry else 0,
            "PROJECT_TYPE_VALID_ASSIGNMENTS_ACQUIRED": type_telemetry.valid_assignments_acquired if type_telemetry else 0,
            "PROJECT_TYPE_ZERO_RESULTS": type_telemetry.zero_results if type_telemetry else 0,
            "PROJECT_TYPE_NULL_RESULTS": type_telemetry.null_results if type_telemetry else 0,
            "PROJECT_TYPE_EMPTY_RESULTS": type_telemetry.empty_results if type_telemetry else 0,
            "PROJECT_TYPE_MISSING_RESULTS": type_telemetry.missing_results if type_telemetry else 0,
            "PROJECT_TYPE_UNKNOWN_CODE_RESULTS": type_telemetry.unknown_code_results if type_telemetry else 0,
            "PROJECT_TYPE_IMMUTABILITY_CONFLICTS": type_telemetry.immutable_conflicts if type_telemetry else 0,
            "TYPE_DETAIL_GET_REUSED": type_telemetry.detail_get_reused if type_telemetry else 0,
            "TYPE_DETAIL_GET_ADDITIONAL": type_telemetry.detail_get_additional if type_telemetry else 0,
            "PROJECT_TYPE_REQUEST_FAILURES": type_telemetry.request_failures if type_telemetry else 0,
            "PROJECT_TYPES_STATE_ROWS": max(len(state), 0) if project_type_enabled else 0,
            "PROJECT_TYPE_PENDING_BEFORE_IDS": sorted(type_telemetry.pending_ids, key=int) if type_telemetry else [],
            "PROJECT_TYPE_PENDING_AFTER_IDS": sorted(applicable_ids - set(state), key=int) if project_type_enabled else [],
            "PROJECT_TYPE_APPLICABLE_IDS": sorted(applicable_ids, key=int) if project_type_enabled else [],
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
    except HTTPError as exc:
        try:
            response_body = exc.read().decode("utf-8", errors="replace")[:4000]
        except Exception:
            response_body = "<unavailable>"
        print(
            json.dumps(
                {
                    "FINAL_STATUS": "FAILED",
                    "ERROR_TYPE": type(exc).__name__,
                    "ERROR_MESSAGE": str(exc),
                    "HTTP_STATUS": exc.code,
                    "HTTP_REASON": exc.reason,
                    "HTTP_RESPONSE_BODY": response_body,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            flush=True,
        )
        return 1
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
