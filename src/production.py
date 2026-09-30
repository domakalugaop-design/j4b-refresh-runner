from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.error import HTTPError

from .acquisition import Reader, acquire_project, discover_universe
from .payment_detail_xlsx import acquire_project_payment_assignments
from .payment_materialization import (
    PROJECT_PUBLICATION_COLUMNS,
    VISIT_PUBLICATION_COLUMNS,
    build_publication_payloads,
    materialize_payment_data,
    serialize_sheet_payload,
)
from .payment_refresh import normalize_payment_sheet_values, publish_payment_pair, replace_by_project
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
    api_get,
    summary,
    api,
    col,
)
from .workflow_publication import (
    PRIMARY_ANALYTICS_COLUMNS,
    THIRD_TAB_NAME,
    backup_snapshot,
    primary_columns,
    third_tab_rows,
    validate_materialized_rows,
    validate_readback,
)

PRODUCTION_TITLE = "J4B Portal — DataLens Materialized Layer"
PAYMENT_VISIT_TAB = "Выплаты по визитам"
PAYMENT_PROJECT_TAB = "Выплаты по проектам"


def _payment_refresh_enabled() -> bool:
    return os.environ.get("PAYMENT_REFRESH_ENABLED", "false").strip().lower() in {"1", "true", "yes"}


def _read_payment_tab(token: str, sid: str, title: str) -> list[list[Any]]:
    encoded = urllib.parse.quote(f"'{title}'!A:Z", safe="!:'")
    values = api_get(
        f"https://sheets.googleapis.com/v4/spreadsheets/{sid}/values/{encoded}?valueRenderOption=UNFORMATTED_VALUE",
        token,
    ).get("values", [])
    return normalize_payment_sheet_values(title, values)


def _exact_google_batch(token: str, sid: str, requests: list[dict[str, Any]]) -> Any:
    if os.environ.get("DRY_RUN", "false").strip().lower() == "true":
        raise RuntimeError("DRY_RUN blocked payment Sheet mutation")
    body = serialize_sheet_payload({"requests": requests}).encode("utf-8")
    req = urllib.request.Request(
        f"https://sheets.googleapis.com/v4/spreadsheets/{sid}:batchUpdate",
        data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as response:
        return json.load(response)


def _prepare_regular_payment_publication(
    token: str,
    sid: str,
    meta: dict[str, Any],
    session: PortalSession,
    selected: list[dict[str, Any]],
    acquired_projects: list[dict[str, Any]],
) -> tuple[dict[str, list[list[Any]]], dict[str, list[list[Any]]], dict[str, int]]:
    """Acquire complete selected scope and prepare both replacement payloads before writes."""
    sheet_ids = {
        props.get("title"): props.get("sheetId")
        for sheet in meta.get("sheets", [])
        for props in [sheet.get("properties", {})]
        if props.get("title") in {PAYMENT_VISIT_TAB, PAYMENT_PROJECT_TAB}
    }
    if set(sheet_ids) != {PAYMENT_VISIT_TAB, PAYMENT_PROJECT_TAB} or any(not isinstance(x, int) for x in sheet_ids.values()):
        raise RuntimeError("payment tabs are missing from destination metadata")
    expected_headers = {
        PAYMENT_VISIT_TAB: list(VISIT_PUBLICATION_COLUMNS),
        PAYMENT_PROJECT_TAB: list(PROJECT_PUBLICATION_COLUMNS),
    }
    projects_by_id = {str(row["project_id"]): row for row in acquired_projects}
    selected_ids = [str(row["project_id"]) for row in selected]
    if len(selected_ids) != len(set(selected_ids)) or set(selected_ids) != set(projects_by_id):
        raise RuntimeError("payment selected/acquired project set mismatch")
    failed = [pid for pid, row in projects_by_id.items() if row.get("acquisition_state") != "ACQUIRED"]
    if failed:
        raise RuntimeError(f"payment acquisition blocked by incomplete operational project set: {len(failed)}")
    visit_rows: list[list[Any]] = []
    project_rows: list[list[Any]] = []
    for index, pid in enumerate(selected_ids, start=1):
        payment_rows, status = acquire_project_payment_assignments(
            pid, session, feature_enabled=True, timeout=60
        )
        if status != 200:
            raise RuntimeError("payment XLSX acquisition returned non-200 status")
        record = projects_by_id[pid]
        materialized = materialize_payment_data(pid, record, payment_rows, record.get("workflow_memberships", []))
        if not all(materialized["invariants"].values()):
            raise RuntimeError(f"payment materialization invariant failure for project_id={pid}")
        payload = build_publication_payloads(materialized)
        visit_rows.extend(payload[PAYMENT_VISIT_TAB][1:])
        project_rows.extend(payload[PAYMENT_PROJECT_TAB][1:])
        if index % 25 == 0 or index == len(selected_ids):
            _stage(f"PAYMENT_ACQUISITION {index}/{len(selected_ids)} | success={index}")
    # Read/reconstruct the previous accepted snapshot only after every selected
    # project's Portal and payment acquisition has succeeded.
    previous = {PAYMENT_VISIT_TAB: _read_payment_tab(token, sid, PAYMENT_VISIT_TAB),
                PAYMENT_PROJECT_TAB: _read_payment_tab(token, sid, PAYMENT_PROJECT_TAB)}
    for tab, headers in expected_headers.items():
        if not previous[tab] or previous[tab][0] != headers:
            raise RuntimeError(f"existing payment schema mismatch: {tab}")
    incoming = {
        PAYMENT_VISIT_TAB: [expected_headers[PAYMENT_VISIT_TAB], *visit_rows],
        PAYMENT_PROJECT_TAB: [expected_headers[PAYMENT_PROJECT_TAB], *project_rows],
    }
    candidate = replace_by_project(previous, incoming, selected_ids)
    return previous, candidate, sheet_ids


def _destination_preflight(token: str, sid: str, expected_title: str = PRODUCTION_TITLE) -> dict[str, Any]:
    meta = api(
        f"https://sheets.googleapis.com/v4/spreadsheets/{sid}?fields=spreadsheetId,properties.title,sheets.properties(title,sheetId,gridProperties(columnCount,rowCount))",
        token,
    )
    title = meta.get("properties", {}).get("title")
    if meta.get("spreadsheetId") != sid or title != expected_title:
        raise RuntimeError("production destination title verification failed")
    return meta


def _plan_project_type_layout(
    meta: dict[str, Any],
    target_columns: list[str] | None = None,
    source_columns: list[str] | None = None,
) -> dict[str, Any]:
    """Describe a required schema migration without mutating the workbook."""
    sheets = meta.get("sheets", [])
    current = next((s.get("properties", {}) for s in sheets if s.get("properties", {}).get("title") == "projects_current"), None)
    if not current:
        raise RuntimeError("projects_current tab not found")
    column_count = current.get("gridProperties", {}).get("columnCount")
    target_columns = list(target_columns or PROJECT_TYPE_SCHEMA)
    if source_columns is None:
        if column_count == len(BASE_COLUMNS):
            source_columns = list(BASE_COLUMNS)
        elif column_count == len(PROJECT_TYPE_SCHEMA):
            source_columns = list(PROJECT_TYPE_SCHEMA)
        else:
            raise RuntimeError("source columns are required for an arbitrary baseline width")
    else:
        source_columns = list(source_columns)
    if column_count != len(source_columns):
        raise RuntimeError("projects_current source schema width mismatch")
    state = next((s.get("properties", {}) for s in sheets if s.get("properties", {}).get("title") == "project_types"), None)
    requests: list[dict[str, Any]] = []
    if column_count < len(target_columns):
        requests.append({"appendDimension": {"sheetId": current["sheetId"], "dimension": "COLUMNS", "length": len(target_columns) - column_count}})
    if not state:
        requests.append({"addSheet": {"properties": {"title": "project_types", "gridProperties": {"rowCount": 1000, "columnCount": 3, "frozenRowCount": 1}}}})
    return {
        "projects_sheet_id": current["sheetId"],
        "project_types_sheet_id": state.get("sheetId") if state else None,
        "source_column_count": len(source_columns),
        "source_columns": source_columns,
        "state_exists": state is not None,
        "requests": requests,
        "target_column_count": len(target_columns),
        "planned": ([("EXPAND projects_current A:AE -> A:AG" if len(target_columns) == len(PROJECT_TYPE_SCHEMA) else f"EXPAND projects_current to {len(target_columns)} columns")] if column_count < len(target_columns) else [])
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
    if not current or current.get("gridProperties", {}).get("columnCount") != plan["target_column_count"] or not state:
        raise RuntimeError("Project Type schema migration failed")
    api(
        f"https://sheets.googleapis.com/v4/spreadsheets/{sid}:batchUpdate",
        token,
        {"requests": [
            {"repeatCell": {"range": {"sheetId": current["sheetId"], "startRowIndex": 1, "startColumnIndex": len(BASE_COLUMNS), "endColumnIndex": len(BASE_COLUMNS) + 1}, "cell": {"userEnteredFormat": {"numberFormat": {"type": "TEXT"}}}, "fields": "userEnteredFormat.numberFormat"}},
            {"repeatCell": {"range": {"sheetId": state["sheetId"], "startRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": 2}, "cell": {"userEnteredFormat": {"numberFormat": {"type": "TEXT"}}}, "fields": "userEnteredFormat.numberFormat"}},
            {"autoResizeDimensions": {"dimensions": {"sheetId": current["sheetId"], "dimension": "COLUMNS", "startIndex": len(BASE_COLUMNS), "endIndex": plan["target_column_count"]}}},
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
    if plan["source_column_count"] < plan["target_column_count"]:
        actual_count = current.get("gridProperties", {}).get("columnCount", 0)
        source_count = plan["source_column_count"]
        if actual_count > source_count:
            requests.append({"deleteDimension": {"range": {"sheetId": current["sheetId"], "dimension": "COLUMNS", "startIndex": source_count, "endIndex": actual_count}}})
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
    rollback_columns = plan.get("source_columns")
    if not rollback_columns:
        raise RuntimeError("schema rollback missing saved source columns")
    if previous_raw and previous_raw[0] != rollback_columns:
        raise RuntimeError("schema rollback saved header mismatch")
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


def _upgrade_projects_rows(rows: list[list[Any]], source_columns: list[str], target_columns: list[str] | None = None) -> list[list[Any]]:
    target_columns = target_columns or PROJECT_TYPE_SCHEMA
    if not rows:
        raise RuntimeError("baseline sheet unavailable or schema mismatch")
    if source_columns == PROJECT_TYPE_SCHEMA:
        if rows[0] == target_columns:
            return [list(row) + [""] * max(0, len(target_columns) - len(row)) for row in rows]
        if rows[0] == PROJECT_TYPE_SCHEMA:
            return [target_columns] + [list(row) + [""] * (len(target_columns) - len(PROJECT_TYPE_SCHEMA)) for row in rows[1:]]
        if rows[0] == BASE_COLUMNS:
            return [target_columns] + [list(row) + [""] * (len(target_columns) - len(BASE_COLUMNS)) for row in rows[1:]]
        raise RuntimeError("baseline sheet unavailable or schema mismatch")
    if rows[0] != source_columns:
        raise RuntimeError("baseline sheet unavailable or schema mismatch")
    if source_columns != BASE_COLUMNS:
        raise RuntimeError("unsupported projects_current source schema")
    return [target_columns] + [list(row) + [""] * (len(target_columns) - len(BASE_COLUMNS)) for row in rows[1:]]


def _workflow_enabled() -> bool:
    return os.environ.get("ENABLE_WORKFLOW_ANALYTICS_PUBLICATION", "false").strip().lower() == "true"


def _select_reporting_year_scope(catalogue: list[dict[str, Any]], current_rows: list[list[Any]], year: int) -> list[dict[str, Any]]:
    """Select projects whose persisted date interval intersects the reporting year.

    This is the same closed-interval year rule used by the dashboard cache:
    a project is in scope when its known [date_from, date_to] interval
    intersects January 1 through December 31 of ``year``.  Projects without
    usable persisted date evidence are excluded rather than guessed in.
    """
    if not current_rows:
        raise RuntimeError("2026 backfill requires a non-empty materialized baseline")
    header = current_rows[0]
    try:
        id_index = header.index("project_id")
        start_index = header.index("date_from")
        end_index = header.index("date_to")
    except ValueError as exc:
        raise RuntimeError("2026 backfill baseline lacks date interval columns") from exc

    def parse_day(raw: Any) -> date | None:
        if raw in (None, ""):
            return None
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            try:
                return date(1899, 12, 30) + timedelta(days=float(raw))
            except (OverflowError, ValueError):
                return None
        text = str(raw).strip()
        for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
            try:
                return datetime.strptime(text[:10], fmt).date()
            except ValueError:
                continue
        return None

    year_start = date(year, 1, 1)
    year_end = date(year, 12, 31)
    evidence: dict[str, tuple[date | None, date | None]] = {}
    for row in current_rows[1:]:
        if len(row) <= max(id_index, start_index, end_index) or row[id_index] in (None, ""):
            continue
        evidence[str(row[id_index])] = (parse_day(row[start_index]), parse_day(row[end_index]))

    selected: list[dict[str, Any]] = []
    for item in catalogue:
        pid = str(item["project_id"])
        start, end = evidence.get(pid, (None, None))
        if start is None and end is None:
            continue
        start = start or end
        end = end or start
        if start <= year_end and end >= year_start:
            selected.append(dict(item))
    return sorted(selected, key=lambda row: int(row["project_id"]))


def _capture_workflow_backup(token: str, sid: str, previous_raw: list[list[Any]], previous_state_rows: list[list[Any]], meta: dict[str, Any]) -> dict[str, Any]:
    """Capture affected tabs before the first Phase-2 mutation."""
    existing = {s.get("properties", {}).get("title") for s in meta.get("sheets", [])}
    current = next((s.get("properties", {}) for s in meta.get("sheets", []) if s.get("properties", {}).get("title") == "projects_current"), {})
    third: list[list[Any]] = []
    if THIRD_TAB_NAME in existing:
        encoded = urllib.parse.quote(f"{THIRD_TAB_NAME}!A:Z", safe="!:")
        third = api(f"https://sheets.googleapis.com/v4/spreadsheets/{sid}/values/{encoded}?valueRenderOption=UNFORMATTED_VALUE", token).get("values", [])
    return backup_snapshot(
        sid,
        {"projects_current": previous_raw, "project_types": previous_state_rows, THIRD_TAB_NAME: third},
        {
            "existing_tabs": sorted(existing),
            "projects_current_headers": list(previous_raw[0]) if previous_raw else [],
            "projects_current_width": len(previous_raw[0]) if previous_raw else 0,
            "projects_current_grid": current.get("gridProperties", {}),
        },
    )


def _validate_project_type_state_materialization(
    rows: list[dict[str, Any]],
    state: dict[str, tuple[str, str]],
    applicable_ids: set[str],
) -> None:
    """Validate assignments present in the candidate without requiring full-universe grain.

    The persisted state is universe-scoped, while a workflow candidate may be
    a bounded operational scope.  Therefore an applicable state row may be
    absent from the candidate; any assignment that is present must still match
    the immutable persisted value.
    """
    if not applicable_ids.issuperset(state):
        invalid = sorted(set(state) - applicable_ids, key=int)
        raise RuntimeError(f"candidate contains pre-0926 Project Type assignment: {','.join(invalid)}")
    for row in rows:
        project_id = str(row.get("project_id", ""))
        assignment = state.get(project_id)
        if not assignment:
            continue
        actual = (row.get("project_type_code"), row.get("project_type_name"))
        if actual != assignment:
            raise RuntimeError(f"candidate changes immutable Project Type assignment: {project_id}")


def _publish_workflow_tab(token: str, sid: str, rows: list[dict[str, Any]], backup: dict[str, Any]) -> list[list[Any]]:
    rendered = third_tab_rows(rows)
    meta = api(f"https://sheets.googleapis.com/v4/spreadsheets/{sid}?fields=sheets.properties(title,sheetId,gridProperties(rowCount,columnCount))", token)
    tab = next((s.get("properties", {}) for s in meta.get("sheets", []) if s.get("properties", {}).get("title") == THIRD_TAB_NAME), None)
    if not tab:
        api(f"https://sheets.googleapis.com/v4/spreadsheets/{sid}:batchUpdate", token, {"requests": [{"addSheet": {"properties": {"title": THIRD_TAB_NAME, "gridProperties": {"rowCount": max(1000, len(rendered)), "columnCount": len(rendered[0]), "frozenRowCount": 1}}}}]})
        meta = api(f"https://sheets.googleapis.com/v4/spreadsheets/{sid}?fields=sheets.properties(title,sheetId,gridProperties(rowCount,columnCount))", token)
        tab = next(s.get("properties", {}) for s in meta.get("sheets", []) if s.get("properties", {}).get("title") == THIRD_TAB_NAME)
    old_rows = int(tab.get("gridProperties", {}).get("rowCount", len(rendered)))
    end_column = col(len(rendered[0]) - 1)
    encoded = urllib.parse.quote(f"{THIRD_TAB_NAME}!A1:{end_column}{max(len(rendered), 1)}", safe="!:")
    api(f"https://sheets.googleapis.com/v4/spreadsheets/{sid}/values/{encoded}:clear", token, {}, method="POST")
    api(f"https://sheets.googleapis.com/v4/spreadsheets/{sid}/values:batchUpdate", token, {"valueInputOption": "RAW", "data": [{"range": f"{THIRD_TAB_NAME}!A1:{end_column}{len(rendered)}", "majorDimension": "ROWS", "values": rendered}]})
    if old_rows > len(rendered):
        tail = urllib.parse.quote(f"{THIRD_TAB_NAME}!A{len(rendered)+1}:{end_column}{old_rows}", safe="!:")
        api(f"https://sheets.googleapis.com/v4/spreadsheets/{sid}/values/{tail}:clear", token, {}, method="POST")
    actual = api(f"https://sheets.googleapis.com/v4/spreadsheets/{sid}/values/{encoded}?valueRenderOption=UNFORMATTED_VALUE", token).get("values", [])
    validate_readback(actual, rendered)
    return rendered


def _rollback_workflow_tab(token: str, sid: str, backup: dict[str, Any]) -> None:
    sheets = backup["payload"]["sheets"]
    meta = api(f"https://sheets.googleapis.com/v4/spreadsheets/{sid}?fields=sheets.properties(title,sheetId)", token)
    tab = next((s.get("properties", {}) for s in meta.get("sheets", []) if s.get("properties", {}).get("title") == THIRD_TAB_NAME), None)
    previous = sheets.get(THIRD_TAB_NAME, [])
    if not previous:
        if tab:
            api(f"https://sheets.googleapis.com/v4/spreadsheets/{sid}:batchUpdate", token, {"requests": [{"deleteSheet": {"sheetId": tab["sheetId"]}}]})
        return
    encoded = urllib.parse.quote(f"{THIRD_TAB_NAME}!A:Z", safe="!:")
    api(f"https://sheets.googleapis.com/v4/spreadsheets/{sid}/values/{encoded}:clear", token, {}, method="POST")
    end_column = col(len(previous[0]) - 1)
    api(f"https://sheets.googleapis.com/v4/spreadsheets/{sid}/values:batchUpdate", token, {"valueInputOption": "RAW", "data": [{"range": f"{THIRD_TAB_NAME}!A1:{end_column}{len(previous)}", "majorDimension": "ROWS", "values": previous}]})
    actual = api(f"https://sheets.googleapis.com/v4/spreadsheets/{sid}/values/{encoded}?valueRenderOption=UNFORMATTED_VALUE", token).get("values", [])
    validate_readback(actual, previous)


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


CHECKPOINT_FORBIDDEN_KEYS = {
    "password", "token", "secret", "authorization", "cookie", "session",
    "refresh_token", "client_secret", "api_key",
}


def serialize_acquisition_checkpoint(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Write a replayable, secret-free acquisition payload and verify it."""
    def scan(value: Any, key: str = "") -> None:
        lowered = key.casefold()
        if any(part in lowered for part in CHECKPOINT_FORBIDDEN_KEYS):
            raise ValueError(f"checkpoint secret-like field rejected: {key}")
        if isinstance(value, dict):
            for child_key, child_value in value.items():
                scan(child_value, str(child_key))
        elif isinstance(value, list):
            for child in value:
                scan(child, key)

    scan(payload)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    target = os.path.abspath(path)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "wb") as handle:
        handle.write(encoded)
    os.chmod(target, 0o600)
    with open(target, "rb") as handle:
        restored = json.load(handle)
    restored_encoded = json.dumps(restored, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(encoded).hexdigest()
    if restored_encoded != encoded or hashlib.sha256(restored_encoded).hexdigest() != digest:
        raise ValueError("acquisition checkpoint roundtrip mismatch")
    return {"path": target, "bytes": len(encoded), "sha256": digest, "payload": restored}


def load_acquisition_checkpoint(path: str) -> dict[str, Any]:
    with open(path, "rb") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError("unsupported acquisition checkpoint")
    for required in ("universe", "selected", "projects", "visits", "previous_raw", "previous_state_rows", "state"):
        if required not in payload:
            raise ValueError(f"acquisition checkpoint missing {required}")
    return payload


def run_from_acquisition_checkpoint() -> dict[str, Any]:
    """Replay downstream production processing without Portal acquisition."""
    checkpoint = load_acquisition_checkpoint(os.environ.get("ACQUISITION_CHECKPOINT_PATH", "/tmp/j4b-acquisition-checkpoint.json"))
    sid = _production_target()
    token = google_token()
    workflow_enabled = _workflow_enabled()
    columns = primary_columns(PROJECT_TYPE_SCHEMA) if workflow_enabled else PROJECT_TYPE_SCHEMA
    universe = checkpoint["universe"]
    selected = checkpoint["selected"]
    projects = checkpoint["projects"]
    visits = checkpoint["visits"]
    previous_raw = checkpoint["previous_raw"]
    previous_state_rows = checkpoint["previous_state_rows"]
    state = {str(pid): tuple(value) for pid, value in checkpoint["state"].items()}
    meta = _destination_preflight(token, sid)
    physical = next(s.get("properties", {}) for s in meta["sheets"] if s.get("properties", {}).get("title") == "projects_current")
    source_columns = list(previous_raw[0]) if previous_raw else []
    if physical.get("gridProperties", {}).get("columnCount") != len(source_columns):
        raise RuntimeError("checkpoint baseline schema does not match destination width")
    previous = _upgrade_projects_rows(previous_raw, source_columns, columns)
    plan = _plan_project_type_layout(meta, columns, source_columns=source_columns)
    _stage("MATERIALIZATION_START")
    timestamp = now()
    rows = materialize(projects, visits, timestamp)
    merged = merge_previous(rows, previous, {str(x["project_id"]) for x in selected}, timestamp, columns=columns)
    validate_materialized_rows(merged)
    backup = _capture_workflow_backup(token, sid, previous_raw, previous_state_rows, meta)
    apply_canonical_project_names(merged, universe)
    applicable = project_type_applicable_ids(universe)
    materialize_project_types(merged, state, applicable)
    candidate = sheet_rows(merged, columns=columns)
    candidate_summary = summary(candidate)
    if candidate_summary["duplicates"] or candidate_summary["unique"] < summary(previous)["unique"]:
        raise RuntimeError("candidate validation failed")
    _validate_project_type_state_materialization(merged, state, project_type_applicable_ids(universe))
    _stage(f"MATERIALIZATION_PASS | rows={candidate_summary['rows']} | unique={candidate_summary['unique']}")
    _stage("CANDIDATE_VALIDATION_PASS")
    _require_production_gate()
    _stage("WRITE_AUTHORIZATION_PASS")
    try:
        _stage("MIGRATION_START")
        _execute_project_type_layout(token, sid, plan)
        _stage("MIGRATION_PASS")
        _stage("PUBLISH_START")
        _stage("WORKFLOW_PUBLICATION_START")
        _publish_workflow_tab(token, sid, merged, backup)
        _stage("WORKFLOW_PUBLICATION_PASS")
        publish_project_type_refresh(token, sid, candidate, previous, columns, state, previous_state_rows)
        _stage("PUBLISH_PASS")
    except Exception:
        try:
            _rollback_workflow_tab(token, sid, backup)
        finally:
            if plan.get("planned"):
                _rollback_project_type_layout(token, sid, plan, previous_raw)
        raise
    _stage("READBACK_PASS")
    _stage("TYPE_VALIDATION=EXTERNAL_POSTCHECK_REQUIRED")
    return {
        "RUN_ID": checkpoint.get("run_id"), "FINAL_STATUS": "SUCCESS",
        "SELECTED_COUNT": len(selected), "SUCCESS_COUNT": sum(p.get("acquisition_state") not in {"FAILED", "SEMANTIC_FAILURE"} for p in projects),
        "SEMANTIC_FAILURE_COUNT": sum(p.get("acquisition_state") == "SEMANTIC_FAILURE" for p in projects),
        "PORTAL_HTTP_REQUEST_COUNT": 0, "FINAL_ROWS": candidate_summary["rows"],
        "FINAL_UNIQUE_IDS": candidate_summary["unique"], "FINAL_DUPLICATES": candidate_summary["duplicates"],
    }


def run() -> dict[str, Any]:
    run_mode = os.environ.get("RUN_MODE", "test").strip().lower()
    dry_run = os.environ.get("DRY_RUN", "false").strip().lower() == "true"
    if run_mode != "production":
        raise RuntimeError("RUN_MODE must be production")
    if os.environ.get("REPLAY_ACQUISITION_CHECKPOINT", "false").strip().lower() == "true":
        return run_from_acquisition_checkpoint()
    sid = _production_target()
    started_at = now()
    started_monotonic = time.monotonic()
    # Production is already on the persisted 33-column Project Type contract.
    # Keep this schema unconditional so a missing feature flag cannot silently
    # publish the legacy 31-column layout over AF:AG.
    project_type_enabled = True
    workflow_enabled = _workflow_enabled()
    payment_enabled = _payment_refresh_enabled()
    columns = primary_columns(PROJECT_TYPE_SCHEMA) if workflow_enabled else PROJECT_TYPE_SCHEMA
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
        source_columns = BASE_COLUMNS if physical_columns == len(BASE_COLUMNS) else columns if physical_columns == len(columns) else PROJECT_TYPE_SCHEMA if physical_columns == len(PROJECT_TYPE_SCHEMA) else []
        previous_raw = read_sheet(token, sid, columns=source_columns) if source_columns else []
        previous = _upgrade_projects_rows(previous_raw, source_columns, columns)
        layout_plan = _plan_project_type_layout(destination_meta, columns, source_columns=source_columns)
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

        backfill_scope = os.environ.get("BACKFILL_SCOPE", "").strip()
        if workflow_enabled and backfill_scope == "2026":
            if os.environ.get("WORKFLOW_ANALYTICS_FULL_ACQUISITION", "false").strip().lower() == "true":
                raise RuntimeError("2026 backfill refuses full-universe acquisition fallback")
            selected = _select_reporting_year_scope(universe, previous, 2026)
            if not 1000 <= len(selected) <= 3000:
                raise RuntimeError(f"2026 backfill scope count outside expected order of magnitude: {len(selected)}")
            _stage(f"BACKFILL_SCOPE_GUARD_PASS | scope=2026_ONLY | projects={len(selected)} | pre2026=0")
        else:
            selected = universe if workflow_enabled and os.environ.get("WORKFLOW_ANALYTICS_FULL_ACQUISITION", "false").strip().lower() == "true" else select_scope(universe, previous)
            if workflow_enabled and len(selected) != len(universe):
                raise RuntimeError("workflow publication requires WORKFLOW_ANALYTICS_FULL_ACQUISITION=true")
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

        payment_publication: tuple[dict[str, list[list[Any]]], dict[str, list[list[Any]]], dict[str, int]] | None = None
        if payment_enabled:
            _stage("PAYMENT_STAGE_START | mode=REGULAR | cross_run_checkpoint=NO")
            payment_publication = _prepare_regular_payment_publication(
                token, sid, destination_meta, session, selected, projects
            )
            _stage("PAYMENT_ACQUISITION_AND_CANDIDATE_VALIDATION_PASS")

        checkpoint_path = os.environ.get("ACQUISITION_CHECKPOINT_PATH", "/tmp/j4b-acquisition-checkpoint.json")
        checkpoint = serialize_acquisition_checkpoint(checkpoint_path, {
            "version": 1,
            "run_id": run_id,
            "scope": "2026" if workflow_enabled and os.environ.get("BACKFILL_SCOPE", "").strip() == "2026" else "normal",
            "universe": universe,
            "selected": selected,
            "projects": projects,
            "visits": visits,
            "previous_raw": previous_raw,
            "previous_state_rows": previous_state_rows,
            "state": state,
            "layout_plan": layout_plan,
            "columns": columns,
        })
        _stage(
            f"ACQUISITION_CHECKPOINT_PASS | path={checkpoint['path']} | projects={len(projects)} | "
            f"bytes={checkpoint['bytes']} | sha256={checkpoint['sha256']}"
        )
        if os.environ.get("ACQUISITION_ONLY", "false").strip().lower() == "true":
            _stage("ACQUISITION_ONLY_PASS")
            return {
                "RUN_ID": run_id,
                "FINAL_STATUS": "ACQUISITION_CHECKPOINT_PASS",
                "ACQUISITION_CHECKPOINT": checkpoint,
                "SELECTED_COUNT": len(selected),
                "PORTAL_HTTP_REQUEST_COUNT": session.requests,
            }

        timestamp = now()
        _stage("MATERIALIZATION_START")
        rows = materialize(projects, visits, timestamp)
        merged = merge_previous(rows, previous, {str(x["project_id"]) for x in selected}, timestamp, columns=columns)
        workflow_backup: dict[str, Any] | None = None
        if workflow_enabled:
            validate_materialized_rows(merged)
            workflow_backup = _capture_workflow_backup(token, sid, previous_raw, previous_state_rows, destination_meta)
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
            _validate_project_type_state_materialization(merged, state, applicable_ids)
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
                "WORKFLOW_PUBLICATION": workflow_enabled,
                "PAYMENT_PUBLICATION": bool(payment_publication),
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
                if workflow_enabled:
                    _stage("WORKFLOW_PUBLICATION_START")
                    _publish_workflow_tab(token, sid, merged, workflow_backup or {})
                    _stage("WORKFLOW_PUBLICATION_PASS")
                publish_project_type_refresh(token, sid, candidate, previous, columns, state, previous_state_rows)
                if payment_publication is not None:
                    old_payment, new_payment, payment_sheet_ids = payment_publication
                    _stage("PAYMENT_PUBLICATION_START | tabs=2")
                    payment_result = publish_payment_pair(
                        sheet_ids=payment_sheet_ids,
                        previous=old_payment,
                        candidate=new_payment,
                        write_batch=lambda requests: _exact_google_batch(token, sid, requests),
                        read_tab=lambda tab: _read_payment_tab(token, sid, tab),
                    )
                    _stage(f"PAYMENT_PUBLICATION_{payment_result['status']} | tabs=2 | readback=PASS")
                _stage("PUBLISH_PASS")
            except Exception:
                if workflow_enabled and workflow_backup is not None:
                    try:
                        _rollback_workflow_tab(token, sid, workflow_backup)
                    except Exception as rollback_exc:
                        raise RuntimeError("workflow publication rollback failed") from rollback_exc
                if layout_plan.get("planned"):
                    _rollback_project_type_layout(token, sid, layout_plan, previous_raw)
                raise
        else:
            if run_mode == "production":
                _require_production_gate()
            publish(token, sid, candidate, previous, columns=columns)
            if payment_publication is not None:
                old_payment, new_payment, payment_sheet_ids = payment_publication
                _stage("PAYMENT_PUBLICATION_START | tabs=2")
                payment_result = publish_payment_pair(
                    sheet_ids=payment_sheet_ids,
                    previous=old_payment,
                    candidate=new_payment,
                    write_batch=lambda requests: _exact_google_batch(token, sid, requests),
                    read_tab=lambda tab: _read_payment_tab(token, sid, tab),
                )
                _stage(f"PAYMENT_PUBLICATION_{payment_result['status']} | tabs=2 | readback=PASS")
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
            "PAYMENT_REFRESH_ENABLED": payment_enabled,
            "PAYMENT_PUBLICATION": bool(payment_publication),
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
