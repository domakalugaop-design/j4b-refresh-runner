#!/usr/bin/env python3
"""Fail-closed schema, row-preservation, and TEST readback checks."""
from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

from src.google_service_account import google_token
from src.production import col
from src.project_types import STATE_COLUMNS, validate_state_rows
from src.refresh import CURRENCY_COLUMNS, PROJECT_TYPE_COLUMNS, SUCCESS_METRIC_COLUMNS
from src.workflow_publication import THIRD_TAB_COLUMNS, primary_columns

PINNED_TEST_SPREADSHEET_ID = "13pPltgzR6HceIW4OjtLyzOrXwNdEcDQ1RBxR0L7ZCK8"
CONTROLLER_QUALITY_TEST_SPREADSHEET_ID = "1G_pXXNCLTwABSnSPtXxRCxF19VOCOvXi-DK21ZMzytM"
TEST_TITLE = "J4B Portal — Successful Visits — TEST — 2026-10-27"
PROJECT_TAB = "projects_current"
STATUS_TAB = "Статусы проектов"
PROJECT_TYPES_TAB = "project_types"
PAGE_ROWS = 500

BASE_PROJECT_HEADERS = primary_columns(PROJECT_TYPE_COLUMNS) + CURRENCY_COLUMNS
TARGET_PROJECT_HEADERS = BASE_PROJECT_HEADERS + SUCCESS_METRIC_COLUMNS
BASE_STATUS_HEADERS = THIRD_TAB_COLUMNS[:-6]


def _api(url: str, token: str) -> Any:
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.load(response)
    except HTTPError as exc:
        raise RuntimeError(f"Google Sheets API read failed with HTTP {exc.code}") from None


def _spreadsheet(token: str, spreadsheet_id: str) -> dict[str, Any]:
    fields = "spreadsheetId,properties.title,sheets.properties(title,sheetId,gridProperties(rowCount,columnCount))"
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}?{urllib.parse.urlencode({'fields': fields})}"
    return _api(url, token)


def _values(token: str, spreadsheet_id: str, a1_range: str) -> list[list[Any]]:
    encoded = urllib.parse.quote(a1_range, safe="!:'")
    query = urllib.parse.urlencode({"valueRenderOption": "UNFORMATTED_VALUE"})
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{encoded}?{query}"
    return _api(url, token).get("values", [])


def _pages(token: str, spreadsheet_id: str, tab: str, start_col: str, end_col: str, end_row: int) -> list[list[Any]]:
    result: list[list[Any]] = []
    for start in range(1, max(end_row, 1) + 1, PAGE_ROWS):
        end = min(end_row, start + PAGE_ROWS - 1)
        result.extend(_values(token, spreadsheet_id, f"'{tab}'!{start_col}{start}:{end_col}{end}"))
    return result


def _metadata_sheets(metadata: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        sheet.get("properties", {}).get("title"): sheet.get("properties", {})
        for sheet in metadata.get("sheets", [])
    }


def _check_identity(token: str) -> tuple[str, dict[str, Any], dict[str, dict[str, Any]]]:
    actual_id = os.environ.get("GOOGLE_SPREADSHEET_ID", "").strip()
    pinned_id = os.environ.get("TEST_SPREADSHEET_ID", "").strip()
    if actual_id != PINNED_TEST_SPREADSHEET_ID or pinned_id != PINNED_TEST_SPREADSHEET_ID:
        raise RuntimeError("TEST target differs from the hard-pinned isolated workbook")
    production_id = os.environ.get("PRODUCTION_SPREADSHEET_ID", "").strip()
    if not production_id:
        raise RuntimeError("production ID is required for the read-only target inequality check")
    if actual_id in {production_id, CONTROLLER_QUALITY_TEST_SPREADSHEET_ID}:
        raise RuntimeError("TEST spreadsheet ID collides with a forbidden workbook")
    metadata = _spreadsheet(token, actual_id)
    if metadata.get("spreadsheetId") != PINNED_TEST_SPREADSHEET_ID:
        raise RuntimeError("Sheets API returned a non-pinned spreadsheet ID")
    if metadata.get("properties", {}).get("title") != TEST_TITLE:
        raise RuntimeError("isolated TEST workbook title mismatch")
    return actual_id, metadata, _metadata_sheets(metadata)


def _identifiers(rows: list[list[Any]]) -> list[str]:
    ids: list[str] = []
    for row in rows:
        if not row or row[0] in (None, ""):
            continue
        value = row[0]
        if isinstance(value, bool):
            raise RuntimeError("project_id contains a boolean value")
        if isinstance(value, float) and not value.is_integer():
            raise RuntimeError("project_id contains a non-integral numeric value")
        ids.append(str(int(value)) if isinstance(value, (int, float)) else str(value))
    if len(ids) != len(set(ids)):
        raise RuntimeError("duplicate project_id values found in a TEST tab")
    return ids


def _header(token: str, spreadsheet_id: str, tab: str, width: int) -> list[Any]:
    end = col(width - 1)
    rows = _values(token, spreadsheet_id, f"'{tab}'!A1:{end}1")
    return rows[0] if rows else []


def _baseline(token: str, spreadsheet_id: str, sheets: dict[str, dict[str, Any]]) -> dict[str, Any]:
    for title, expected_width in ((PROJECT_TAB, 58), (STATUS_TAB, 25), (PROJECT_TYPES_TAB, 3)):
        props = sheets.get(title)
        if not props or props.get("gridProperties", {}).get("columnCount") != expected_width:
            raise RuntimeError(f"unexpected prepublication grid width: {title}")
    if _header(token, spreadsheet_id, PROJECT_TAB, 58) != BASE_PROJECT_HEADERS:
        raise RuntimeError("projects_current baseline header is not the established 58-column schema")
    if _header(token, spreadsheet_id, STATUS_TAB, 25) != BASE_STATUS_HEADERS:
        raise RuntimeError("Статусы проектов baseline header is not the established 25-column schema")

    project_rows = _pages(
        token, spreadsheet_id, PROJECT_TAB, "A", "A",
        sheets[PROJECT_TAB].get("gridProperties", {}).get("rowCount", 1),
    )
    status_rows = _pages(
        token, spreadsheet_id, STATUS_TAB, "A", "A",
        sheets[STATUS_TAB].get("gridProperties", {}).get("rowCount", 1),
    )
    type_rows = _pages(
        token, spreadsheet_id, PROJECT_TYPES_TAB, "A", "C",
        sheets[PROJECT_TYPES_TAB].get("gridProperties", {}).get("rowCount", 1),
    )
    project_ids = _identifiers(project_rows[1:])
    status_ids = _identifiers(status_rows[1:])
    type_state = validate_state_rows(type_rows)
    if set(project_ids) != set(status_ids) or len(project_ids) != len(status_ids):
        raise RuntimeError("prepublication project IDs differ between target tabs")
    if not set(type_state).issubset(set(project_ids)):
        raise RuntimeError("project_types contains an ID absent from projects_current baseline")
    if not project_ids:
        raise RuntimeError("isolated TEST baseline contains no projects")

    baseline_path = Path(os.environ["J4B_TEST_BASELINE_IDS_PATH"]).resolve()
    baseline = {
        "project_ids": sorted(project_ids),
        "project_types": {key: list(value) for key, value in sorted(type_state.items())},
    }
    payload = json.dumps(baseline, separators=(",", ":"), sort_keys=True).encode("utf-8")
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(baseline_path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(baseline_path, 0o600)
    digest = hashlib.sha256(payload).hexdigest()
    return {
        "baseline_project_rows": len(project_ids),
        "baseline_project_type_rows": len(type_state),
        "baseline_id_sha256": digest,
    }


def _numeric(value: Any, label: str, *, allow_blank: bool = False) -> float | int | None:
    if allow_blank and value in (None, ""):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuntimeError(f"readback numeric type check failed: {label}")
    if isinstance(value, float) and not math.isfinite(value):
        raise RuntimeError(f"readback non-finite number: {label}")
    return value


def _readback(token: str, spreadsheet_id: str, sheets: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if sheets.get(PROJECT_TAB, {}).get("gridProperties", {}).get("columnCount") != 64:
        raise RuntimeError("projects_current grid is not 64 columns after publication")
    if sheets.get(STATUS_TAB, {}).get("gridProperties", {}).get("columnCount") != 31:
        raise RuntimeError("Статусы проектов grid is not 31 columns after publication")
    if sheets.get(PROJECT_TYPES_TAB, {}).get("gridProperties", {}).get("columnCount") != 3:
        raise RuntimeError("project_types grid changed unexpectedly")
    project_header = _header(token, spreadsheet_id, PROJECT_TAB, 64)
    status_header = _header(token, spreadsheet_id, STATUS_TAB, 31)
    if project_header != TARGET_PROJECT_HEADERS:
        raise RuntimeError("projects_current readback schema/order mismatch")
    if project_header[:58] != BASE_PROJECT_HEADERS or project_header[58:] != SUCCESS_METRIC_COLUMNS:
        raise RuntimeError("projects_current did not append only the six approved metrics")
    if status_header != THIRD_TAB_COLUMNS:
        raise RuntimeError("Статусы проектов readback schema/order mismatch")
    if status_header[:25] != BASE_STATUS_HEADERS or status_header[25:] != THIRD_TAB_COLUMNS[25:]:
        raise RuntimeError("Статусы проектов did not append only the six approved metrics")

    project_values = _pages(
        token, spreadsheet_id, PROJECT_TAB, "A", "BL",
        sheets[PROJECT_TAB].get("gridProperties", {}).get("rowCount", 1),
    )
    status_values = _pages(
        token, spreadsheet_id, STATUS_TAB, "A", "AE",
        sheets[STATUS_TAB].get("gridProperties", {}).get("rowCount", 1),
    )
    type_values = _pages(
        token, spreadsheet_id, PROJECT_TYPES_TAB, "A", "C",
        sheets[PROJECT_TYPES_TAB].get("gridProperties", {}).get("rowCount", 1),
    )
    project_ids = _identifiers(project_values[1:])
    status_ids = _identifiers(status_values[1:])
    if set(project_ids) != set(status_ids) or len(project_ids) != len(status_ids):
        raise RuntimeError("postpublication project IDs differ between target tabs")
    baseline_path = Path(os.environ["J4B_TEST_BASELINE_IDS_PATH"])
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    baseline_ids = set(baseline["project_ids"])
    if not baseline_ids.issubset(set(project_ids)):
        raise RuntimeError("postpublication readback lost one or more baseline project IDs")
    type_state = validate_state_rows(type_values)
    baseline_types = baseline["project_types"]
    if not set(baseline_types).issubset(set(type_state)):
        raise RuntimeError("postpublication readback lost a persisted project_types assignment")
    if any(list(type_state[key]) != value for key, value in baseline_types.items()):
        raise RuntimeError("postpublication readback changed an immutable project_types assignment")
    if not set(type_state).issubset(set(project_ids)):
        raise RuntimeError("postpublication project_types contains an unknown project ID")

    indices = {name: project_header.index(name) for name in (
        "project_id", "plan", "completed", "execution_pct", "finished_visits",
        "project_visit_count", "successful_visits_current", "historically_approved_visits",
        "total_project_visits", "current_state_unresolved_visits", "execution_pct_successful",
    )}
    count_fields = (
        "successful_visits_current", "historically_approved_visits",
        "total_project_visits", "current_state_unresolved_visits",
    )
    for row_number, row in enumerate(project_values[1:], start=2):
        if not row or row[0] in (None, ""):
            continue
        values = {field: row[indices[field]] for field in indices if indices[field] < len(row)}
        for field in count_fields:
            if field not in values:
                raise RuntimeError(f"readback is missing {field} at row {row_number}")
            _numeric(values[field], f"{field} row {row_number}")
        ratio = values.get("execution_pct_successful", "")
        ratio_value = _numeric(ratio, f"execution_pct_successful row {row_number}", allow_blank=True)
        if values.get("completed") != values["successful_visits_current"]:
            raise RuntimeError(f"completed alias mismatch at row {row_number}")
        if values.get("project_visit_count") != values["total_project_visits"]:
            raise RuntimeError(f"project_visit_count alias mismatch at row {row_number}")
        legacy_ratio = values.get("execution_pct", "")
        if legacy_ratio in (None, "") and ratio in (None, ""):
            pass
        else:
            legacy_ratio_value = _numeric(legacy_ratio, f"execution_pct row {row_number}")
            if not math.isclose(float(legacy_ratio_value), float(ratio_value), rel_tol=0, abs_tol=1e-12):
                raise RuntimeError(f"execution_pct alias mismatch at row {row_number}")

    status_metrics = {
        "Успешно завершено сейчас": status_header.index("Успешно завершено сейчас"),
        "Когда-либо достигало успешного статуса": status_header.index("Когда-либо достигало успешного статуса"),
        "Всего уникальных визитов": status_header.index("Всего уникальных визитов"),
        "Состояние не разрешено": status_header.index("Состояние не разрешено"),
        "Выполнение плана (успешные визиты)": status_header.index("Выполнение плана (успешные визиты)"),
        "Разница с графиком, п.п.": status_header.index("Разница с графиком, п.п."),
    }
    for row_number, row in enumerate(status_values[1:], start=2):
        if not row or row[0] in (None, ""):
            continue
        for field in list(status_metrics)[:4]:
            index = status_metrics[field]
            if index >= len(row):
                raise RuntimeError(f"status readback is missing {field} at row {row_number}")
            _numeric(row[index], f"{field} row {row_number}")
        for field in list(status_metrics)[4:]:
            index = status_metrics[field]
            value = row[index] if index < len(row) else ""
            _numeric(value, f"{field} row {row_number}", allow_blank=True)

    control: list[Any] | None = None
    for row in project_values[1:]:
        if row and str(int(row[0]) if isinstance(row[0], (int, float)) and not isinstance(row[0], bool) else row[0]) == "8314":
            control = row
            break
    if control is None:
        raise RuntimeError("project 8314 is absent from TEST readback")
    expected_control = {
        "plan": 162,
        "total_project_visits": 166,
        "successful_visits_current": 162,
        "historically_approved_visits": 164,
        "current_state_unresolved_visits": 0,
    }
    for field, expected in expected_control.items():
        actual = _numeric(control[indices[field]], field)
        if actual != expected:
            raise RuntimeError(f"project 8314 readback mismatch: {field}")
    ratio = _numeric(control[indices["execution_pct_successful"]], "execution_pct_successful")
    if not math.isclose(float(ratio), 1.0, rel_tol=0, abs_tol=1e-12):
        raise RuntimeError("project 8314 readback execution is not 100 percent")
    baseline_path.unlink(missing_ok=True)
    return {
        "projects_current_rows": len(project_ids),
        "status_rows": len(status_ids),
        "project_types_rows": len(type_state),
        "baseline_rows_preserved": len(baseline_ids),
        "unique_project_ids": len(set(project_ids)),
        "projects_current_columns": len(project_header),
        "status_columns": len(status_header),
        "project_8314": {
            "plan": expected_control["plan"],
            "total_visits": expected_control["total_project_visits"],
            "successful_visits_current": expected_control["successful_visits_current"],
            "historically_approved_visits": expected_control["historically_approved_visits"],
            "execution_pct": 100.0,
        },
    }


def main() -> int:
    phase = os.environ.get("J4B_TEST_VALIDATION_PHASE", "").strip().lower()
    if phase not in {"preflight", "readback"}:
        raise RuntimeError("J4B_TEST_VALIDATION_PHASE must be preflight or readback")
    token = google_token()
    spreadsheet_id, metadata, sheets = _check_identity(token)
    result = _baseline(token, spreadsheet_id, sheets) if phase == "preflight" else _readback(token, spreadsheet_id, sheets)
    print(json.dumps({"phase": phase, "TEST_READBACK": "PASS", **result}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({
            "TEST_READBACK": "FAIL",
            "ERROR_TYPE": type(exc).__name__,
            "ERROR": str(exc),
        }, ensure_ascii=False, sort_keys=True))
        raise SystemExit(1)
