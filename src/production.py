from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from urllib.error import HTTPError

from .acquisition import (
    MAX_REQUEST_RETRIES,
    Reader,
    acquire_project,
    discover_universe,
    recover_retryable_failed_projects,
)
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
    publish_project_type_state,
    read_sheet,
    read_project_type_state_rows,
    reconcile_current_rows,
    select_scope,
    regular_scope_counts,
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
    diagnose_workflow_readback,
    persist_private_workflow_candidate,
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
    with urllib.request.urlopen(req, timeout=60) as response:
        return json.load(response)


def _prepare_regular_payment_publication(
    token: str,
    sid: str,
    meta: dict[str, Any],
    session: PortalSession,
    selected: list[dict[str, Any]],
    acquired_projects: list[dict[str, Any]],
) -> tuple[dict[str, list[list[Any]]], dict[str, list[list[Any]]], dict[str, int], dict[str, int]]:
    """Acquire complete selected scope and prepare both replacement payloads before writes."""
    sheet_ids = {
        props.get("title"): props.get("sheetId")
        for sheet in meta.get("sheets", [])
        for props in [sheet.get("properties", {})]
        if props.get("title") in {PAYMENT_VISIT_TAB, PAYMENT_PROJECT_TAB}
    }
    if set(sheet_ids) != {PAYMENT_VISIT_TAB, PAYMENT_PROJECT_TAB} or any(not isinstance(x, int) for x in sheet_ids.values()):
        raise RuntimeError("payment tabs are missing from destination metadata")
    grid_row_counts = {
        props.get("title"): props.get("gridProperties", {}).get("rowCount")
        for sheet in meta.get("sheets", [])
        for props in [sheet.get("properties", {})]
        if props.get("title") in sheet_ids
    }
    if set(grid_row_counts) != set(sheet_ids) or any(not isinstance(x, int) or x <= 0 for x in grid_row_counts.values()):
        raise RuntimeError("payment tab grid row count metadata is unavailable")
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
            pid,
            session,
            feature_enabled=True,
            timeout=60,
            ordinal=index,
            total=len(selected_ids),
            telemetry=_payment_telemetry,
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
    return previous, candidate, sheet_ids, grid_row_counts


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
    # Sheets grid capacity may be larger than the populated header (for
    # example, a 54-column workflow-enabled grid with a 33-column legacy
    # header).  The header width is the source schema; capacity is only a
    # lower bound for it.
    if not isinstance(column_count, int) or column_count < len(source_columns):
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


def _prune_absent_project_type_state_rows(
    rows: list[list[Any]], authoritative_ids: set[str]
) -> list[list[Any]]:
    """Drop only current-state assignments for IDs absent from a complete universe."""
    if not rows:
        return rows
    if rows[0] != STATE_COLUMNS:
        raise ValueError("project_types header mismatch")
    ids = {str(project_id) for project_id in authoritative_ids}
    return [rows[0]] + [row for row in rows[1:] if row and str(row[0]) in ids]


def _validate_authoritative_universe(universe: list[dict[str, Any]]) -> set[str]:
    """Validate the already-fetched /api/project universe before using it for pruning."""
    if not isinstance(universe, list) or not universe:
        raise RuntimeError("authoritative project universe is empty")
    ids: set[str] = set()
    for item in universe:
        if not isinstance(item, dict):
            raise RuntimeError("authoritative project universe contains malformed record")
        project_id = str(item.get("project_id", ""))
        if not project_id.isdigit() or project_id in ids:
            raise RuntimeError("authoritative project universe contains invalid or duplicate project IDs")
        if item.get("project_name") in (None, ""):
            raise RuntimeError("authoritative project universe contains a project without a name")
        ids.add(project_id)
    return ids


def _upgrade_projects_rows(rows: list[list[Any]], source_columns: list[str], target_columns: list[str] | None = None) -> list[list[Any]]:
    target_columns = target_columns or PROJECT_TYPE_SCHEMA
    if not rows:
        raise RuntimeError("baseline sheet unavailable or schema mismatch")
    if source_columns == target_columns:
        if rows[0] != target_columns:
            raise RuntimeError("baseline sheet unavailable or schema mismatch")
        return [target_columns] + [list(row) + [""] * max(0, len(target_columns) - len(row)) for row in rows[1:]]
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
    candidate_manifest = persist_private_workflow_candidate(rendered)
    _stage(
        f"WORKFLOW_CANDIDATE_PRIVATE_PASS | rows={candidate_manifest['rows']} | "
        f"columns={candidate_manifest['columns']} | schema_sha256={candidate_manifest['schema_sha256']} | "
        f"candidate_sha256={candidate_manifest['candidate_sha256']}"
    )
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
    _validate_workflow_readback(actual, rendered, candidate_manifest=candidate_manifest)
    return rendered


def _validate_workflow_readback(
    actual: list[list[Any]],
    expected: list[list[Any]],
    *,
    candidate_manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    diagnostic = diagnose_workflow_readback(expected, actual)
    if not diagnostic["matches"]:
        if candidate_manifest:
            diagnostic["candidate_sha256"] = candidate_manifest["candidate_sha256"]
            diagnostic["schema_sha256"] = candidate_manifest["schema_sha256"]
        print("WORKFLOW_READBACK_DIAGNOSTIC=" + json.dumps(diagnostic, ensure_ascii=False, sort_keys=True), flush=True)
        raise ValueError("workflow publication readback mismatch")
    result = validate_readback(actual, expected)
    _stage(f"TRAILING_BLANK_OMISSIONS_ACCEPTED={result['trailing_blank_omissions_accepted']}")
    _stage(f"ROWS_WITH_TRAILING_BLANK_OMISSIONS={result['rows_with_trailing_blank_omissions']}")
    return result


def _rollback_workflow_tab(token: str, sid: str, backup: dict[str, Any]) -> None:
    _stage("WORKFLOW_ROLLBACK_REQUIRED=YES")
    _stage("WORKFLOW_ROLLBACK_ATTEMPTED=YES")
    readback_started = False
    def mark_readback_start() -> None:
        nonlocal readback_started
        readback_started = True
    try:
        _rollback_workflow_tab_body(token, sid, backup, on_readback_start=mark_readback_start)
    except Exception:
        _stage("WORKFLOW_ROLLBACK_READBACK=FAIL" if readback_started else "WORKFLOW_ROLLBACK_READBACK=NOT_COMPLETED")
        _stage("WORKFLOW_ROLLBACK_RESULT=FAIL")
        raise RuntimeError("WORKFLOW_ROLLBACK_INCOMPLETE") from None
    _stage("WORKFLOW_ROLLBACK_READBACK=PASS")
    _stage("WORKFLOW_ROLLBACK_RESULT=PASS")


def _rollback_workflow_tab_body(
    token: str,
    sid: str,
    backup: dict[str, Any],
    *,
    on_readback_start: Any,
) -> None:
    sheets = backup["payload"]["sheets"]
    meta = api(f"https://sheets.googleapis.com/v4/spreadsheets/{sid}?fields=sheets.properties(title,sheetId)", token)
    tab = next((s.get("properties", {}) for s in meta.get("sheets", []) if s.get("properties", {}).get("title") == THIRD_TAB_NAME), None)
    previous = sheets.get(THIRD_TAB_NAME, [])
    if not previous:
        if tab:
            api(f"https://sheets.googleapis.com/v4/spreadsheets/{sid}:batchUpdate", token, {"requests": [{"deleteSheet": {"sheetId": tab["sheetId"]}}]})
        on_readback_start()
        verified = api(f"https://sheets.googleapis.com/v4/spreadsheets/{sid}?fields=sheets.properties(title)", token)
        if any(s.get("properties", {}).get("title") == THIRD_TAB_NAME for s in verified.get("sheets", [])):
            raise RuntimeError("workflow rollback tab-removal readback mismatch")
        return
    if not previous or not previous[0]:
        raise RuntimeError("workflow rollback backup has no header")
    width = len(previous[0])
    normalized_previous: list[list[Any]] = []
    for row in previous:
        if len(row) > width:
            raise RuntimeError("workflow rollback backup row exceeds header width")
        normalized_previous.append(list(row) + [""] * (width - len(row)))
    encoded = urllib.parse.quote(f"{THIRD_TAB_NAME}!A:Z", safe="!:")
    api(f"https://sheets.googleapis.com/v4/spreadsheets/{sid}/values/{encoded}:clear", token, {}, method="POST")
    end_column = col(width - 1)
    api(f"https://sheets.googleapis.com/v4/spreadsheets/{sid}/values:batchUpdate", token, {"valueInputOption": "RAW", "data": [{"range": f"{THIRD_TAB_NAME}!A1:{end_column}{len(normalized_previous)}", "majorDimension": "ROWS", "values": normalized_previous}]})
    on_readback_start()
    actual = api(f"https://sheets.googleapis.com/v4/spreadsheets/{sid}/values/{encoded}?valueRenderOption=UNFORMATTED_VALUE", token).get("values", [])
    _validate_workflow_readback(actual, normalized_previous)


def _rollback_full_refresh(
    token: str,
    sid: str,
    *,
    columns: list[str],
    previous: list[list[Any]],
    previous_raw: list[list[Any]],
    previous_state_rows: list[list[Any]],
    workflow_backup: dict[str, Any] | None,
    payment_publication: tuple[dict[str, list[list[Any]]], dict[str, list[list[Any]]], dict[str, int], dict[str, int]] | None,
    layout_plan: dict[str, Any],
) -> None:
    """Best-effort restore every target snapshot; report any unverifiable tab."""
    errors: list[str] = []
    if payment_publication is not None:
        old_payment, _candidate, sheet_ids, _grid_rows = payment_publication
        try:
            live = {tab: _read_payment_tab(token, sid, tab) for tab in old_payment}
            if live != old_payment:
                current_meta = api(
                    f"https://sheets.googleapis.com/v4/spreadsheets/{sid}?fields=sheets.properties(title,gridProperties(rowCount))",
                    token,
                )
                current_grid_rows = {
                    item["properties"]["title"]: item["properties"].get("gridProperties", {}).get("rowCount")
                    for item in current_meta.get("sheets", [])
                    if item.get("properties", {}).get("title") in sheet_ids
                }
                if set(current_grid_rows) != set(sheet_ids) or any(not isinstance(n, int) for n in current_grid_rows.values()):
                    raise RuntimeError("payment rollback grid metadata unavailable")
                publish_payment_pair(
                    sheet_ids=sheet_ids,
                    previous=live,
                    candidate=old_payment,
                    write_batch=lambda requests: _exact_google_batch(token, sid, requests),
                    read_tab=lambda tab: _read_payment_tab(token, sid, tab),
                    grid_row_counts=current_grid_rows,
                )
            if {tab: _read_payment_tab(token, sid, tab) for tab in old_payment} != old_payment:
                raise RuntimeError("payment tabs rollback readback mismatch")
        except Exception as exc:
            errors.append(f"payment tabs: {type(exc).__name__}")
    try:
        live_current = read_sheet(token, sid, columns=columns)
        if live_current != previous:
            publish(token, sid, previous, live_current, columns=columns)
    except Exception as exc:
        errors.append(f"projects_current: {type(exc).__name__}")
    try:
        live_state_rows = read_project_type_state_rows(token, sid)
        if previous_state_rows and live_state_rows != previous_state_rows:
            publish_project_type_state(
                token, sid, validate_state_rows(previous_state_rows), live_state_rows
            )
        if previous_state_rows and read_project_type_state_rows(token, sid) != previous_state_rows:
            raise RuntimeError("project_types rollback readback mismatch")
    except Exception as exc:
        errors.append(f"project_types: {type(exc).__name__}")
    if workflow_backup is not None:
        try:
            _rollback_workflow_tab(token, sid, workflow_backup)
        except Exception as exc:
            errors.append(f"workflow tab: {type(exc).__name__}")
    if layout_plan.get("planned"):
        try:
            _rollback_project_type_layout(token, sid, layout_plan, previous_raw)
        except Exception as exc:
            errors.append(f"schema: {type(exc).__name__}")
    if errors:
        raise RuntimeError("full refresh rollback incomplete: " + "; ".join(errors))


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


def _payment_telemetry(event: str, fields: dict[str, Any]) -> None:
    """Emit allowlisted payment transport diagnostics without payloads."""
    allowed = {
        "project_id", "ordinal", "total", "attempt", "http_status", "content_type",
        "content_length", "actual_response_bytes", "duration_ms", "row_count",
        "failure_code", "failure_stage", "exception_class", "response_sha256",
        "transport_error_class", "retryable",
        "looks_like_html", "looks_like_login", "xlsx_magic_valid", "zip_valid",
        "workbook_structure_valid", "worksheet_xml_valid", "expected_headers_valid",
    }
    safe = {key: value for key, value in fields.items() if key in allowed}
    _stage("PAYMENT_TELEMETRY " + json.dumps({"event": event, **safe}, sort_keys=True, ensure_ascii=False))


def _safe_failure_class(exc: BaseException) -> str:
    """Map internal failures to a non-sensitive operational diagnostic."""
    message = str(exc).casefold()
    classifications = (
        ("source schema width mismatch", "SHEET_SOURCE_SCHEMA_WIDTH_MISMATCH"),
        ("baseline sheet header schema mismatch", "SHEET_HEADER_SCHEMA_MISMATCH"),
        ("baseline sheet unavailable or schema mismatch", "SHEET_BASELINE_SCHEMA_MISMATCH"),
        ("production destination title verification failed", "SHEET_DESTINATION_GUARD_FAILED"),
        ("missing required environment variable", "RUNTIME_SECRET_OR_CONFIG_MISSING"),
        ("project_types state is absent", "PROJECT_TYPE_STATE_MISSING"),
        ("portal", "PORTAL_FAILURE"),
    )
    for needle, classification in classifications:
        if needle in message:
            return classification
    return type(exc).__name__.upper()


def _encode_private_backup(value: Any) -> Any:
    """JSON-safe, type-preserving encoding for private prepublication backups."""
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite Decimal in private refresh backup")
        return {"__decimal__": format(value, "f")}
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite float in private refresh backup")
        return value
    if isinstance(value, dict):
        return {str(key): _encode_private_backup(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode_private_backup(item) for item in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(f"unsupported private refresh backup value: {type(value).__name__}")


def _decode_private_backup(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value) == {"__decimal__"}:
            return Decimal(value["__decimal__"])
        return {key: _decode_private_backup(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode_private_backup(item) for item in value]
    return value


def _persist_prepublication_backup(
    sid: str,
    run_id: str,
    previous_raw: list[list[Any]],
    previous_state_rows: list[list[Any]],
    workflow_backup: dict[str, Any] | None,
    payment_publication: tuple[dict[str, list[list[Any]]], dict[str, list[list[Any]]], dict[str, int], dict[str, int]] | None,
    non_target_fingerprints: dict[str, str],
) -> dict[str, Any]:
    """Persist all target-tab baselines outside Git before the first Sheet write."""
    sheets: dict[str, list[list[Any]]] = {
        "projects_current": previous_raw,
        "project_types": previous_state_rows,
    }
    metadata: dict[str, Any] = {}
    if workflow_backup is not None:
        backup_payload = workflow_backup.get("payload", {})
        sheets.update(backup_payload.get("sheets", {}))
        metadata.update(backup_payload.get("metadata", {}))
    metadata["non_target_fingerprints"] = non_target_fingerprints
    if payment_publication is not None:
        payment_previous = payment_publication[0]
        sheets.update(payment_previous)
    payload = {"version": 1, "run_id": run_id, "spreadsheet_id": sid,
               "created_at": now(), "sheets": sheets, "metadata": metadata}

    forbidden = ("password", "token", "secret", "authorization", "cookie", "session", "api_key")
    def reject_secret_keys(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if any(part in str(key).casefold() for part in forbidden):
                    raise ValueError("secret-like field rejected from private refresh backup")
                reject_secret_keys(child)
        elif isinstance(value, list):
            for child in value:
                reject_secret_keys(child)
    reject_secret_keys(payload)

    configured_dir = os.environ.get("J4B_PERSISTENT_BACKUP_DIR", "").strip()
    if os.environ.get("RUN_MODE", "").strip().lower() == "production" and not configured_dir:
        raise RuntimeError("persistent prepublication backup path is not configured")
    if configured_dir:
        target_dir = os.path.abspath(os.path.expanduser(configured_dir))
        temp_root = os.path.realpath(tempfile.gettempdir())
        if os.environ.get("RUN_MODE", "").strip().lower() == "production" and (os.path.realpath(target_dir) == temp_root or os.path.realpath(target_dir).startswith(temp_root + os.sep)):
            raise RuntimeError("persistent prepublication backup path must not be under temporary storage")
        os.makedirs(target_dir, mode=0o700, exist_ok=True)
    else:
        target_dir = tempfile.mkdtemp(prefix="j4b-production-private-backup-")
    os.chmod(target_dir, 0o700)
    target = os.path.join(target_dir, f"{run_id}.json")
    encoded = json.dumps(_encode_private_backup(payload), ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(target, 0o600)
    with open(target, "rb") as handle:
        restored = handle.read()
    if restored != encoded:
        raise RuntimeError("private prepublication backup integrity mismatch")
    required = {"projects_current", "project_types"}
    if workflow_backup is not None:
        required.add(THIRD_TAB_NAME)
    if payment_publication is not None:
        required.update({PAYMENT_VISIT_TAB, PAYMENT_PROJECT_TAB})
    if not required.issubset(sheets):
        raise RuntimeError("private prepublication backup is missing a target tab")
    return {"path": target, "bytes": len(encoded), "sha256": hashlib.sha256(encoded).hexdigest(),
            "tabs": sorted(required)}


def load_private_prepublication_backup(path: str | os.PathLike[str], expected_sha256: str | None = None) -> dict[str, Any]:
    """Load and integrity-check a private persistent pre-publication backup."""
    target = os.path.abspath(os.path.expanduser(os.fspath(path)))
    mode = os.stat(target).st_mode
    if mode & 0o077:
        raise ValueError("private prepublication backup permissions must be 0600")
    encoded = open(target, "rb").read()
    digest = hashlib.sha256(encoded).hexdigest()
    if expected_sha256 and not hmac.compare_digest(digest, expected_sha256):
        raise ValueError("private prepublication backup hash mismatch")
    payload = _decode_private_backup(json.loads(encoded))
    if not isinstance(payload, dict) or payload.get("version") != 1 or not isinstance(payload.get("sheets"), dict):
        raise ValueError("invalid private prepublication backup")
    if payload.get("spreadsheet_id") in (None, ""):
        raise ValueError("private prepublication backup missing spreadsheet identity")
    return payload


def _persistent_backup_dir() -> str:
    configured = os.environ.get("J4B_PERSISTENT_BACKUP_DIR", "").strip()
    if not configured:
        raise RuntimeError("persistent prepublication backup path is not configured")
    return os.path.abspath(os.path.expanduser(configured))


def _require_persistent_backup_upload() -> None:
    """Require the workflow's successful artifact-upload marker before writes."""
    backup_dir = _persistent_backup_dir()
    marker = os.environ.get("J4B_PERSISTENT_BACKUP_UPLOAD_MARKER", "").strip()
    if not marker:
        raise RuntimeError("persistent prepublication backup upload marker is not configured")
    marker_path = os.path.abspath(os.path.expanduser(marker))
    if not marker_path.startswith(backup_dir + os.sep):
        raise RuntimeError("persistent prepublication backup upload marker is outside backup path")
    if not os.path.isfile(marker_path) or os.stat(marker_path).st_mode & 0o077:
        raise RuntimeError("persistent prepublication backup upload marker is missing or unsafe")
    if open(marker_path, "r", encoding="utf-8").read().strip() != "PASS":
        raise RuntimeError("persistent prepublication backup upload marker is invalid")


def create_persistent_prepublication_backup() -> dict[str, Any]:
    """Read target tabs and persist the rollback baseline before publication."""
    sid = _production_target()
    token = google_token()
    meta = _destination_preflight(token, sid)
    header_url = urllib.parse.quote("projects_current!1:1", safe="!:")
    header = api_get(
        f"https://sheets.googleapis.com/v4/spreadsheets/{sid}/values/{header_url}?valueRenderOption=UNFORMATTED_VALUE",
        token,
    ).get("values", [[]])[0]
    if not header:
        raise RuntimeError("projects_current populated header is unavailable")
    previous_raw = read_sheet(token, sid, columns=[str(i) for i in range(len(header))])
    previous_state_rows = read_project_type_state_rows(token, sid)
    workflow_backup = _capture_workflow_backup(token, sid, previous_raw, previous_state_rows, meta) if _workflow_enabled() else None
    payment_tabs = {PAYMENT_VISIT_TAB, PAYMENT_PROJECT_TAB}
    payment_previous = {tab: _read_payment_tab(token, sid, tab) for tab in payment_tabs} if _payment_refresh_enabled() else {}
    payment_publication = None
    if payment_previous:
        sheet_ids = {
            props.get("title"): props.get("sheetId")
            for sheet in meta.get("sheets", [])
            for props in [sheet.get("properties", {})]
            if props.get("title") in payment_tabs
        }
        grid_rows = {
            props.get("title"): props.get("gridProperties", {}).get("rowCount")
            for sheet in meta.get("sheets", [])
            for props in [sheet.get("properties", {})]
            if props.get("title") in payment_tabs
        }
        payment_publication = (payment_previous, {}, sheet_ids, grid_rows)
    target_tabs = {"projects_current", "project_types"}
    if workflow_backup is not None:
        target_tabs.add(THIRD_TAB_NAME)
    if payment_publication is not None:
        target_tabs.update(payment_tabs)
    non_target = _fingerprint_non_target_tabs(token, sid, target_tabs)
    saved = _persist_prepublication_backup(
        sid, os.environ.get("GITHUB_RUN_ID", "preflight"), previous_raw,
        previous_state_rows, workflow_backup, payment_publication, non_target,
    )
    _stage(f"PERSISTENT_PREPUBLICATION_BACKUP_PASS | bytes={saved['bytes']} | sha256={saved['sha256']}")
    return saved


def _fingerprint_non_target_tabs(token: str, sid: str, target_tabs: set[str]) -> dict[str, str]:
    """Hash values and structural metadata for every non-target worksheet."""
    meta = api(
        f"https://sheets.googleapis.com/v4/spreadsheets/{sid}?fields=sheets.properties(title,sheetId,index,hidden,gridProperties(rowCount,columnCount,frozenRowCount))",
        token,
    )
    sheets = [item.get("properties", {}) for item in meta.get("sheets", [])]
    fingerprints: dict[str, str] = {}
    for props in sheets:
        title = props.get("title")
        if not isinstance(title, str) or title in target_tabs:
            continue
        width = props.get("gridProperties", {}).get("columnCount")
        if not isinstance(width, int) or width < 1:
            raise RuntimeError("non-target worksheet column metadata unavailable")
        escaped_title = title.replace("'", "''")
        encoded = urllib.parse.quote(f"'{escaped_title}'!A:{col(width - 1)}", safe="!:'")
        values = api_get(
            f"https://sheets.googleapis.com/v4/spreadsheets/{sid}/values/{encoded}?valueRenderOption=UNFORMATTED_VALUE",
            token,
        ).get("values", [])
        canonical = json.dumps({"properties": props, "values": values}, ensure_ascii=False,
                               sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        fingerprints[title] = hashlib.sha256(canonical).hexdigest()
    return fingerprints


def _fingerprint_summary(fingerprints: dict[str, str]) -> str:
    canonical = json.dumps(fingerprints, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _assert_prepublication_baseline_unchanged(
    token: str,
    sid: str,
    previous_raw: list[list[Any]],
    previous_state_rows: list[list[Any]],
) -> None:
    """Fail closed if core or Project Type state changed during acquisition."""
    source_columns = list(previous_raw[0]) if previous_raw else []
    if not source_columns or read_sheet(token, sid, columns=source_columns) != previous_raw:
        raise RuntimeError("prepublication projects_current baseline changed during acquisition")
    if read_project_type_state_rows(token, sid) != previous_state_rows:
        raise RuntimeError("prepublication project_types baseline changed during acquisition")


def _require_complete_operational_acquisition(
    projects: list[dict[str, Any]], selected_count: int | None = None
) -> None:
    expected = len(projects) if selected_count is None else selected_count
    acquired = sum(p.get("acquisition_state") == "ACQUIRED" for p in projects)
    if len(projects) != expected or acquired != expected:
        raise RuntimeError(
            "operational acquisition acceptance failed: "
            f"selected={expected} records={len(projects)} acquired={acquired}"
        )


def _payment_report(publication: Any, selected: list[dict[str, Any]]) -> dict[str, int | bool]:
    if publication is None:
        return {"enabled": False, "projects_refreshed": 0, "assignment_rows_refreshed": 0,
                "unmatched_visit_mappings_refreshed": 0, "visit_final_rows": 0, "project_final_rows": 0}
    _previous, candidate, _sheet_ids, _grid_rows = publication
    selected_ids = {str(row["project_id"]) for row in selected}
    project_rows = candidate[PAYMENT_PROJECT_TAB][1:]
    selected_project_rows = [row for row in project_rows if row and str(row[0]) in selected_ids]
    return {
        "enabled": True,
        "projects_refreshed": len(selected),
        "assignment_rows_refreshed": sum(int(row[5] or 0) for row in selected_project_rows),
        "unmatched_visit_mappings_refreshed": sum(int(row[13] or 0) for row in selected_project_rows),
        "visit_final_rows": len(candidate[PAYMENT_VISIT_TAB]) - 1,
        "project_final_rows": len(project_rows),
    }


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
    authoritative_ids = _validate_authoritative_universe(universe)
    selected = checkpoint["selected"]
    scope_counts = checkpoint.get("scope_counts")
    projects = checkpoint["projects"]
    visits = checkpoint["visits"]
    previous_raw = checkpoint["previous_raw"]
    previous_state_rows = checkpoint["previous_state_rows"]
    state = {str(pid): tuple(value) for pid, value in checkpoint["state"].items()}
    state = {pid: value for pid, value in state.items() if pid in authoritative_ids}
    source_columns = list(previous_raw[0]) if previous_raw else []
    previous = _upgrade_projects_rows(previous_raw, source_columns, columns)
    if os.environ.get("MATERIALIZATION_PREFLIGHT", "false").strip().lower() == "true":
        _require_complete_operational_acquisition(projects)
        rows = materialize(projects, visits, now())
        merged = merge_previous(rows, previous, {str(x["project_id"]) for x in selected}, now(), columns=columns)
        merged, stale_ids = reconcile_current_rows(
            merged, authoritative_ids, acquisition_complete=True
        )
        if workflow_enabled:
            validate_materialized_rows(merged)
        apply_canonical_project_names(merged, universe)
        materialize_project_types(merged, state, project_type_applicable_ids(universe))
        candidate = sheet_rows(merged, columns=columns)
        candidate_summary = summary(candidate)
        if candidate_summary["duplicates"]:
            raise RuntimeError("candidate validation failed: duplicate project IDs")
        _stage(f"MATERIALIZATION_PREFLIGHT_PASS | rows={candidate_summary['rows']} | unique={candidate_summary['unique']} | stale_pruned={len(stale_ids)}")
        return {"FINAL_STATUS": "MATERIALIZATION_PREFLIGHT_PASS", "CANDIDATE_ROWS": candidate_summary["rows"]}
    meta = _destination_preflight(token, sid)
    physical = next(s.get("properties", {}) for s in meta["sheets"] if s.get("properties", {}).get("title") == "projects_current")
    if physical.get("gridProperties", {}).get("columnCount", 0) < len(source_columns):
        raise RuntimeError("checkpoint baseline schema does not match destination width")
    plan = _plan_project_type_layout(meta, columns, source_columns=source_columns)
    _require_complete_operational_acquisition(projects)
    _stage("MATERIALIZATION_START")
    timestamp = now()
    rows = materialize(projects, visits, timestamp)
    merged = merge_previous(rows, previous, {str(x["project_id"]) for x in selected}, timestamp, columns=columns)
    merged, stale_ids = reconcile_current_rows(
        merged, authoritative_ids, acquisition_complete=True
    )
    validate_materialized_rows(merged)
    apply_canonical_project_names(merged, universe)
    applicable = project_type_applicable_ids(universe)
    materialize_project_types(merged, state, applicable)
    payment_publication = None
    payment_portal_requests = 0
    if _payment_refresh_enabled():
        session = PortalSession()
        try:
            _stage("PORTAL_PAYMENT_SESSION_START")
            session.login()
            _stage("PORTAL_PAYMENT_SESSION_PASS")
            payment_publication = _prepare_regular_payment_publication(
                token, sid, meta, session, selected, projects
            )
            payment_portal_requests = session.requests
        finally:
            session.close()
        _stage("PAYMENT_ACQUISITION_AND_CANDIDATE_VALIDATION_PASS")
    candidate = sheet_rows(merged, columns=columns)
    candidate_summary = summary(candidate)
    if candidate_summary["duplicates"]:
        raise RuntimeError("candidate validation failed: duplicate project IDs")
    _validate_project_type_state_materialization(merged, state, project_type_applicable_ids(universe))
    _stage(f"MATERIALIZATION_PASS | rows={candidate_summary['rows']} | unique={candidate_summary['unique']} | stale_pruned={len(stale_ids)}")
    _stage("CANDIDATE_VALIDATION_PASS")
    _require_production_gate()
    _require_persistent_backup_upload()
    backup = _capture_workflow_backup(token, sid, previous_raw, previous_state_rows, meta) if workflow_enabled else None
    _assert_prepublication_baseline_unchanged(token, sid, previous_raw, previous_state_rows)
    target_tabs = {"projects_current", "project_types"}
    if workflow_enabled:
        target_tabs.add(THIRD_TAB_NAME)
    if payment_publication is not None:
        target_tabs.update({PAYMENT_VISIT_TAB, PAYMENT_PROJECT_TAB})
    non_target_before = _fingerprint_non_target_tabs(token, sid, target_tabs)
    private_backup = _persist_prepublication_backup(
        sid, str(checkpoint.get("run_id") or "replay"), previous_raw, previous_state_rows,
        backup, payment_publication, non_target_before,
    )
    _stage(f"PRIVATE_PREPUBLICATION_BACKUP_PASS | bytes={private_backup['bytes']} | sha256={private_backup['sha256']}")
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
        if payment_publication is not None:
            old_payment, new_payment, payment_sheet_ids, payment_grid_rows = payment_publication
            _stage("PAYMENT_PUBLICATION_START | tabs=2")
            payment_result = publish_payment_pair(
                sheet_ids=payment_sheet_ids,
                previous=old_payment,
                candidate=new_payment,
                write_batch=lambda requests: _exact_google_batch(token, sid, requests),
                read_tab=lambda tab: _read_payment_tab(token, sid, tab),
                grid_row_counts=payment_grid_rows,
            )
            _stage(f"PAYMENT_PUBLICATION_{payment_result['status']} | tabs=2 | readback=PASS")
        non_target_after = _fingerprint_non_target_tabs(token, sid, target_tabs)
        if non_target_after != non_target_before:
            raise RuntimeError("non-target worksheet fingerprint changed during unified refresh")
        _stage(f"NON_TARGET_TABS_FINGERPRINT_PASS | tabs={len(non_target_after)} | sha256={_fingerprint_summary(non_target_after)}")
        _stage("PUBLISH_PASS")
    except Exception:
        _rollback_full_refresh(
            token, sid, columns=columns, previous=previous, previous_raw=previous_raw,
            previous_state_rows=previous_state_rows, workflow_backup=backup,
            payment_publication=payment_publication, layout_plan=plan,
        )
        raise
    _stage("READBACK_PASS")
    _stage("TYPE_VALIDATION=EXTERNAL_POSTCHECK_REQUIRED")
    return {
        "RUN_ID": checkpoint.get("run_id"), "FINAL_STATUS": "SUCCESS",
        "SELECTED_COUNT": len(selected), "REGULAR_SELECTOR": scope_counts,
        "SUCCESS_COUNT": sum(p.get("acquisition_state") not in {"FAILED", "SEMANTIC_FAILURE"} for p in projects),
        "SEMANTIC_FAILURE_COUNT": sum(p.get("acquisition_state") == "SEMANTIC_FAILURE" for p in projects),
        "PORTAL_HTTP_REQUEST_COUNT": payment_portal_requests,
        "CORE_PROJECTS": len(universe), "CORE_FINAL_ROWS": candidate_summary["rows"],
        "WORKFLOW_PROJECTS_REFRESHED": len(selected), "WORKFLOW_FINAL_ROWS": len(third_tab_rows(merged)) - 1,
        "PAYMENT_SUMMARY": _payment_report(payment_publication, selected),
        "PREPUBLICATION_BACKUP": private_backup,
        "NON_TARGET_TABS_UNCHANGED": len(non_target_before),
        "SERVICE_INFORMATION_REFRESH_TIMESTAMP": now(),
        "FINAL_ROWS": candidate_summary["rows"],
        "FINAL_UNIQUE_IDS": candidate_summary["unique"], "FINAL_DUPLICATES": candidate_summary["duplicates"],
    }


def run() -> dict[str, Any]:
    run_mode = os.environ.get("RUN_MODE", "test").strip().lower()
    dry_run = os.environ.get("DRY_RUN", "false").strip().lower() == "true"
    if run_mode != "production":
        raise RuntimeError("RUN_MODE must be production")
    if os.environ.get("PREPUBLICATION_BACKUP_ONLY", "false").strip().lower() == "true":
        return create_persistent_prepublication_backup()
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
        # Read the populated header using the target range, then distinguish
        # schema width from the worksheet's allocated grid capacity.
        previous_raw = read_sheet(token, sid, columns=columns)
        header = previous_raw[0] if previous_raw else []
        if header == BASE_COLUMNS:
            source_columns = BASE_COLUMNS
        elif header == PROJECT_TYPE_SCHEMA:
            source_columns = PROJECT_TYPE_SCHEMA
        elif header == columns:
            source_columns = columns
        else:
            raise RuntimeError("baseline sheet header schema mismatch")
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
        authoritative_ids = _validate_authoritative_universe(universe)
        _stage(f"UNIVERSE_DISCOVERY_PASS | universe={len(universe)} | authoritative=PASS")

        type_telemetry = None
        applicable_ids: set[str] = set()
        if project_type_enabled:
            previous_ids = {str(r[0]) for r in previous[1:] if r and r[0] not in (None, "")}
            state_rows_for_resolution = _prune_absent_project_type_state_rows(previous_state_rows, authoritative_ids)
            resolved_state_rows, state, bootstrap_source, bootstrap_sha256 = _resolve_project_type_state(
                bool(layout_plan["state_exists"]),
                state_rows_for_resolution,
                os.environ.get("PROJECT_TYPE_BOOTSTRAP_PATH", "").strip(),
                os.environ.get("PROJECT_TYPE_BOOTSTRAP_SHA256", "").strip(),
                previous_ids,
                universe,
            )
            bootstrap_rows = len(resolved_state_rows) - 1 if bootstrap_source == "LOCAL_VALIDATED_ARTIFACT" else 0
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
        moscow_today = (datetime.now(timezone.utc) + timedelta(hours=3)).date()
        scope_counts: dict[str, int] | None = None
        if workflow_enabled and backfill_scope == "2026":
            if os.environ.get("WORKFLOW_ANALYTICS_FULL_ACQUISITION", "false").strip().lower() == "true":
                raise RuntimeError("2026 backfill refuses full-universe acquisition fallback")
            selected = _select_reporting_year_scope(universe, previous, 2026)
            if not 1000 <= len(selected) <= 3000:
                raise RuntimeError(f"2026 backfill scope count outside expected order of magnitude: {len(selected)}")
            _stage(f"BACKFILL_SCOPE_GUARD_PASS | scope=2026_ONLY | projects={len(selected)} | pre2026=0")
        else:
            if os.environ.get("WORKFLOW_ANALYTICS_FULL_ACQUISITION", "false").strip().lower() == "true":
                selected = sorted((dict(item) for item in universe), key=lambda row: int(row["project_id"]))
                scope_counts = {"new": len({str(x["project_id"]) for x in universe} - {str(r[0]) for r in previous[1:] if r and r[0] not in (None, "")}), "current_month": 0, "previous_month": 0, "union": len(selected)}
            else:
                scope_counts = regular_scope_counts(universe, previous, moscow_today)
                selected = select_scope(universe, previous, today=moscow_today)
                if len(selected) != scope_counts["union"]:
                    raise RuntimeError("regular selector summary disagrees with deduplicated selection")
        _stage(f"SCOPE_SELECTION_PASS | selected={len(selected)}")
        # Budget for one full retry sequence plus one failed-project-only recovery pass.
        reader = Reader(
            session,
            max(3 * (MAX_REQUEST_RETRIES + 1) * 2 * len(selected), 3),
            expected_projects=len(selected),
        )
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

        projects, visits, recovery_ids = recover_retryable_failed_projects(
            reader,
            selected,
            projects,
            visits,
            float(os.environ.get("PORTAL_REQUEST_DELAY", "0.15")),
        )
        acquired_count = sum(p.get("acquisition_state") == "ACQUIRED" for p in projects)
        request_failed = [p for p in projects if p.get("acquisition_state") == "FAILED"]
        transport_failure_classes = {
            "TIMEOUT", "DNS_ERROR", "CONNECT_ERROR", "CONNECTION_RESET", "CONNECTION_CLOSED",
            "CONNECTION_SEND_ERROR", "HTTP_408", "HTTP_425", "HTTP_429",
            "HTTP_500", "HTTP_502", "HTTP_503", "HTTP_504",
        }
        transport_failed = [
            p for p in request_failed
            if any(
                failure.get("failure_class") in transport_failure_classes
                for failure in p.get("acquisition_request_failures", [])
            )
        ]
        semantic_failed = [p for p in projects if p.get("acquisition_state") == "SEMANTIC_FAILURE"]
        unresolved = [p for p in projects if p.get("acquisition_state") != "ACQUIRED"]
        print(
            "ACQUISITION_FINAL_SUMMARY "
            + json.dumps({
                "selected_projects": len(selected),
                "acquired_projects": acquired_count,
                "transport_failures": len(transport_failed),
                "request_failed_projects": len(request_failed),
                "semantic_failures": len(semantic_failed),
                "failed_project_ids": [str(p["project_id"]) for p in unresolved],
                "failed_project_outcomes": [
                    {
                        "project_id": str(p["project_id"]),
                        "failure_class": p.get("acquisition_failure_reasons", []),
                        "request_failures": p.get("acquisition_request_failures", []),
                        "retryable": p.get("retryable_failure", False),
                    }
                    for p in unresolved
                ],
                "failed_only_recovery_project_ids": recovery_ids,
                "request_failure_attempts": len(getattr(reader, "failure_telemetry", [])),
                "portal_http_requests": reader.count,
            }, sort_keys=True),
            flush=True,
        )
        _require_complete_operational_acquisition(projects, selected_count=len(selected))

        payment_publication: tuple[dict[str, list[list[Any]]], dict[str, list[list[Any]]], dict[str, int], dict[str, int]] | None = None
        if payment_enabled and os.environ.get("ACQUISITION_ONLY", "false").strip().lower() != "true":
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
            "scope_date_moscow": moscow_today.isoformat(),
            "scope_counts": scope_counts,
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
        merged, stale_ids = reconcile_current_rows(
            merged, authoritative_ids, acquisition_complete=True
        )
        workflow_backup: dict[str, Any] | None = None
        if workflow_enabled:
            validate_materialized_rows(merged)
        if project_type_enabled:
            apply_canonical_project_names(merged, universe)
            applicable_ids = project_type_applicable_ids(universe)
            materialize_project_types(merged, state, applicable_ids)
        candidate = sheet_rows(merged, columns=columns)
        candidate_summary = summary(candidate)
        if candidate_summary["duplicates"] != 0:
            raise RuntimeError("candidate contains duplicate project IDs")
        # A smaller current-state set is valid only after the complete,
        # fail-closed authoritative-universe reconciliation above.
        if project_type_enabled:
            candidate_ids = {str(r[0]) for r in candidate[1:] if r and r[0] not in (None, "")}
            _validate_project_type_state_materialization(merged, state, applicable_ids)
            layout_plan["planned"] = layout_plan.get("planned", []) + (["WRITE project_types state"] if state != validate_state_rows(previous_state_rows) else [])
            layout_plan["planned"] = layout_plan.get("planned", []) + ["WRITE projects_current A:AG"]
        _stage(f"LIFECYCLE_RECONCILIATION_PASS | authoritative={len(authoritative_ids)} | stale_pruned={len(stale_ids)}")
        _stage(f"MATERIALIZATION_PASS | rows={candidate_summary['rows']} | unique={candidate_summary['unique']} | stale_pruned={len(stale_ids)}")
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
                "REGULAR_SELECTOR": scope_counts,
            }

        # Authorization is deliberately late: all acquisition, candidate validation,
        # bootstrap validation, and diff planning above are read-only.
        if project_type_enabled:
            _require_production_gate()
            _require_persistent_backup_upload()
            if workflow_enabled:
                workflow_backup = _capture_workflow_backup(token, sid, previous_raw, previous_state_rows, destination_meta)
            _assert_prepublication_baseline_unchanged(token, sid, previous_raw, previous_state_rows)
            target_tabs = {"projects_current", "project_types"}
            if workflow_enabled:
                target_tabs.add(THIRD_TAB_NAME)
            if payment_publication is not None:
                target_tabs.update({PAYMENT_VISIT_TAB, PAYMENT_PROJECT_TAB})
            non_target_before = _fingerprint_non_target_tabs(token, sid, target_tabs)
            private_backup = _persist_prepublication_backup(
                sid, run_id, previous_raw, previous_state_rows, workflow_backup, payment_publication,
                non_target_before,
            )
            _stage(f"PRIVATE_PREPUBLICATION_BACKUP_PASS | bytes={private_backup['bytes']} | sha256={private_backup['sha256']}")
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
                    old_payment, new_payment, payment_sheet_ids, payment_grid_rows = payment_publication
                    _stage("PAYMENT_PUBLICATION_START | tabs=2")
                    payment_result = publish_payment_pair(
                        sheet_ids=payment_sheet_ids,
                        previous=old_payment,
                        candidate=new_payment,
                        write_batch=lambda requests: _exact_google_batch(token, sid, requests),
                        read_tab=lambda tab: _read_payment_tab(token, sid, tab),
                        grid_row_counts=payment_grid_rows,
                    )
                    _stage(f"PAYMENT_PUBLICATION_{payment_result['status']} | tabs=2 | readback=PASS")
                non_target_after = _fingerprint_non_target_tabs(token, sid, target_tabs)
                if non_target_after != non_target_before:
                    raise RuntimeError("non-target worksheet fingerprint changed during unified refresh")
                _stage(f"NON_TARGET_TABS_FINGERPRINT_PASS | tabs={len(non_target_after)} | sha256={_fingerprint_summary(non_target_after)}")
                _stage("PUBLISH_PASS")
            except Exception:
                _rollback_full_refresh(
                    token, sid, columns=columns, previous=previous, previous_raw=previous_raw,
                    previous_state_rows=previous_state_rows,
                    workflow_backup=workflow_backup if workflow_enabled else None,
                    payment_publication=payment_publication,
                    layout_plan=layout_plan,
                )
                raise
        else:
            if run_mode == "production":
                _require_production_gate()
            publish(token, sid, candidate, previous, columns=columns)
            if payment_publication is not None:
                old_payment, new_payment, payment_sheet_ids, payment_grid_rows = payment_publication
                _stage("PAYMENT_PUBLICATION_START | tabs=2")
                payment_result = publish_payment_pair(
                    sheet_ids=payment_sheet_ids,
                    previous=old_payment,
                    candidate=new_payment,
                    write_batch=lambda requests: _exact_google_batch(token, sid, requests),
                    read_tab=lambda tab: _read_payment_tab(token, sid, tab),
                    grid_row_counts=payment_grid_rows,
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
            "SERVICE_INFORMATION_REFRESH_TIMESTAMP": finished_at,
            "WALL_SECONDS": round(wall, 3),
            "UNIVERSE_COUNT": len(universe),
            "SELECTED_COUNT": len(selected),
            "REGULAR_SELECTOR": scope_counts,
            "CORE_PROJECTS": len(universe),
            "CORE_FINAL_ROWS": candidate_summary["rows"],
            "WORKFLOW_PROJECTS_REFRESHED": len(selected) if workflow_enabled else 0,
            "WORKFLOW_FINAL_ROWS": len(third_tab_rows(merged)) - 1 if workflow_enabled else 0,
            "PAYMENT_SUMMARY": _payment_report(payment_publication, selected),
            "PREPUBLICATION_BACKUP": private_backup if project_type_enabled else None,
            "NON_TARGET_TABS_UNCHANGED": len(non_target_before) if project_type_enabled else None,
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
        print(
            json.dumps(
                {
                    "FINAL_STATUS": "FAILED",
                    "ERROR_TYPE": type(exc).__name__,
                    "HTTP_STATUS": exc.code,
                    "FAILURE_CLASS": "HTTP_FAILURE",
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
                {
                    "FINAL_STATUS": "FAILED",
                    "ERROR_TYPE": type(exc).__name__,
                    "FAILURE_CLASS": _safe_failure_class(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            flush=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
