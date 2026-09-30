"""Pure publication planning for the Phase 2 workflow analytics.

This module accepts already-materialized rows only.  It has no Portal or
Google client dependency, making the publication and rollback contracts
testable before a controlled production run.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import tempfile
from typing import Any, Iterable

from .workflow_analytics import WORKFLOW_COUNTER_FIELDS, WORKFLOW_STATES, validate_workflow_metrics
from .technical_ids import integer_id

THIRD_TAB_NAME = "Статусы проектов"
PRIMARY_ANALYTICS_COLUMNS = list(WORKFLOW_COUNTER_FIELDS) + [
    "assigned_visits", "executed_visits_customer", "finished_visits",
    "project_visit_count", "workflow_covered_visits", "workflow_unclassified_visits",
    "workflow_multi_match_visits",
]
THIRD_TAB_COLUMNS = ["project_id", "project_name", "client", "manager", "project_visit_count"]
THIRD_TAB_COLUMNS += [f"{label} [{code}]" for code, label in WORKFLOW_STATES.items()]
THIRD_TAB_COLUMNS += ["Назначено", "Выполнено", "Завершено", "Покрыто workflow", "Без workflow-состояния", "Несколько workflow-состояний"]


def primary_columns(existing_columns: Iterable[str]) -> list[str]:
    """Return existing columns plus the 21 additive analytics fields."""
    result = list(existing_columns)
    for column in PRIMARY_ANALYTICS_COLUMNS:
        if column not in result:
            result.append(column)
    return result


def validate_materialized_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ids: list[str] = []
    for row in rows:
        pid = str(row.get("project_id", ""))
        if not pid:
            raise ValueError("materialized workflow row has empty project_id")
        ids.append(pid)
        metrics = {field: int(row.get(field, 0) or 0) for field in PRIMARY_ANALYTICS_COLUMNS if field in row}
        missing = [field for field in PRIMARY_ANALYTICS_COLUMNS if field not in row]
        if missing:
            raise ValueError(f"materialized row missing workflow fields: {','.join(missing)}")
        validate_workflow_metrics(metrics)
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate project_id in materialized workflow rows")
    return {"rows": len(ids), "unique_project_ids": len(set(ids)), "duplicates": 0}


def third_tab_rows(rows: list[dict[str, Any]]) -> list[list[Any]]:
    validate_materialized_rows(rows)
    output = [THIRD_TAB_COLUMNS]
    for row in sorted(rows, key=lambda item: (int(str(item["project_id"])) if str(item["project_id"]).isdigit() else str(item["project_id"]))):
        # Values API reads absent text as an empty cell. Serialize absent
        # project dimensions as that same explicit blank so readback remains
        # exact without broadening the comparison contract.
        project_name = row.get("project_name")
        client = row.get("client")
        manager = row.get("primary_manager", row.get("manager"))
        line: list[Any] = [integer_id(row.get("project_id"), "project_id"), project_name if project_name is not None else "",
                           client if client is not None else "", manager if manager is not None else "",
                           row.get("project_visit_count", 0)]
        line += [row.get(f"workflow_state_{code}_visits", 0) for code in WORKFLOW_STATES]
        line += [row.get("assigned_visits", 0), row.get("executed_visits_customer", 0), row.get("finished_visits", 0)]
        line += [row.get("workflow_covered_visits", 0), row.get("workflow_unclassified_visits", 0), row.get("workflow_multi_match_visits", 0)]
        output.append(line)
    return output


def shrink_clear_range(tab_name: str, old_rows: int, new_rows: int, width: int) -> dict[str, Any] | None:
    if new_rows >= old_rows:
        return None
    return {"tab": tab_name, "start_row": new_rows + 1, "end_row": old_rows, "start_column": 1, "end_column": width}


def publication_plan(rows: list[dict[str, Any]], existing_tab_names: set[str], previous_third_rows: int = 0) -> dict[str, Any]:
    validate_materialized_rows(rows)
    rendered = third_tab_rows(rows)
    return {
        "target_tab": THIRD_TAB_NAME,
        "headers": THIRD_TAB_COLUMNS,
        "row_count": len(rendered) - 1,
        "column_count": len(THIRD_TAB_COLUMNS),
        "create_tab": THIRD_TAB_NAME not in existing_tab_names,
        "clear_range": shrink_clear_range(THIRD_TAB_NAME, previous_third_rows, len(rendered), len(THIRD_TAB_COLUMNS)),
        "primary_columns_to_add": list(PRIMARY_ANALYTICS_COLUMNS),
        "backup_required": True,
        "validation": "PASS",
    }


def backup_snapshot(spreadsheet_id: str, sheets: dict[str, list[list[Any]]], metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = {"spreadsheet_id": spreadsheet_id, "metadata": metadata or {}, "sheets": sheets}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return {"payload": payload, "sha256": hashlib.sha256(encoded).hexdigest(), "bytes": len(encoded)}


def rollback_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(snapshot, dict) or "payload" not in snapshot or "sha256" not in snapshot:
        raise ValueError("invalid backup snapshot")
    payload = snapshot["payload"]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    if hashlib.sha256(encoded).hexdigest() != snapshot["sha256"]:
        raise ValueError("backup snapshot hash mismatch")
    return payload["sheets"]


def persist_private_workflow_candidate(rows: list[list[Any]]) -> dict[str, Any]:
    """Persist the exact workflow matrix outside the repository with private permissions."""
    encoded = json.dumps(rows, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    header = rows[0] if rows else []
    schema_encoded = json.dumps(header, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    directory = tempfile.mkdtemp(prefix="j4b-workflow-candidate-")
    os.chmod(directory, 0o700)
    path = os.path.join(directory, "candidate.json")
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(path, 0o600)
    with open(path, "rb") as handle:
        persisted = handle.read()
    digest = hashlib.sha256(encoded).hexdigest()
    if (
        persisted != encoded
        or hashlib.sha256(persisted).hexdigest() != digest
        or stat.S_IMODE(os.stat(directory).st_mode) != 0o700
        or stat.S_IMODE(os.stat(path).st_mode) != 0o600
    ):
        raise RuntimeError("private workflow candidate integrity mismatch")
    return {
        "path": path,
        "bytes": len(encoded),
        "rows": len(rows),
        "columns": max((len(row) for row in rows), default=0),
        "candidate_sha256": digest,
        "schema_sha256": hashlib.sha256(schema_encoded).hexdigest(),
    }


def _value_class(value: Any) -> str:
    if value is None:
        return "NULL"
    if value == "":
        return "EMPTY_STRING"
    if isinstance(value, bool):
        return "BOOLEAN"
    if isinstance(value, (int, float)):
        return "NUMBER"
    if isinstance(value, str):
        return "TEXT"
    return "OTHER"


def _safe_value_fingerprint(value: Any) -> dict[str, Any]:
    """Describe a mismatched value without exposing its contents."""
    if value is None:
        encoded = b"null"
    elif isinstance(value, str):
        encoded = value.encode("utf-8", errors="replace")
    elif isinstance(value, bool):
        encoded = b"true" if value else b"false"
    elif isinstance(value, (int, float)):
        encoded = repr(value).encode("ascii")
    else:
        encoded = repr(value).encode("utf-8", errors="replace")
    return {"class": _value_class(value), "type": type(value).__name__, "length": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest()}


def _column_name(index: int) -> str:
    result = ""
    value = index + 1
    while value:
        value, remainder = divmod(value - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _workflow_mismatch_class(expected: Any, actual: Any, *, missing_cell: bool = False) -> str:
    if missing_cell:
        return "MISSING_TRAILING_VALUE" if expected not in (None, "") else "MISSING_CELL"
    if (expected is None and actual == "") or (actual is None and expected == ""):
        return "NULL_VS_EMPTY"
    if isinstance(expected, bool) or isinstance(actual, bool):
        return "BOOLEAN_VALUE"
    if ((isinstance(expected, str) and isinstance(actual, (int, float))) or
            (isinstance(actual, str) and isinstance(expected, (int, float)))):
        return "STRING_VS_NUMBER"
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return "NUMERIC_VALUE"
    if isinstance(expected, str) or isinstance(actual, str):
        return "TEXT_VALUE"
    return "OTHER"


def _workflow_row_matches_with_trailing_blanks(expected: list[Any], actual: list[Any], width: int) -> bool:
    if expected == actual:
        return True
    return (
        len(actual) < width
        and len(expected) == width
        and expected[:len(actual)] == actual
        and all(value == "" for value in expected[len(actual):])
    )


def diagnose_workflow_readback(expected: list[list[Any]], actual: list[list[Any]]) -> dict[str, Any]:
    """Compare fixed-width rows, allowing only omitted expected-empty suffixes.

    Google Sheets Values API may trim physically blank cells at the end of a
    row. This is a directional transport normalization: a shortened actual
    row is equivalent only when every omitted expected cell is the explicit
    empty string. It does not equate None with blank, pad interior holes, or
    forgive row/schema differences. Payment tabs intentionally retain their
    separate, previously qualified nullable-money contract.
    """
    expected_rows, actual_rows = len(expected), len(actual)
    expected_columns = len(expected[0]) if expected else 0
    actual_columns = max((len(row) for row in actual), default=0)
    by_class: dict[str, int] = {}
    by_column: dict[str, int] = {}
    mismatch_row_indexes: set[int] = set()
    mismatch_samples: list[dict[str, Any]] = []
    first: dict[str, Any] | None = None
    trailing_blank_omissions_accepted = 0
    rows_with_trailing_blank_omissions: set[int] = set()
    max_rows = max(expected_rows, actual_rows)
    max_columns = max(expected_columns, actual_columns,
                      max((len(row) for row in expected), default=0),
                      max((len(row) for row in actual), default=0))

    if expected_rows != actual_rows:
        by_class["ROW_COUNT"] = abs(expected_rows - actual_rows)
    if expected_columns != actual_columns:
        by_class["COLUMN_COUNT"] = abs(expected_columns - actual_columns)

    expected_widths = {len(row) for row in expected}
    if len(expected_widths) > 1:
        by_class["EXPECTED_SCHEMA_WIDTH"] = len(expected_widths) - 1

    actual_header_width = len(actual[0]) if actual else 0
    if expected and actual and actual_header_width != expected_columns:
        by_class["SCHEMA_WIDTH"] = abs(expected_columns - actual_header_width)

    for row_index in range(max_rows):
        expected_row = expected[row_index] if row_index < expected_rows else []
        actual_row = actual[row_index] if row_index < actual_rows else []
        # Accept omissions as one complete row suffix only. Do not normalize
        # short expected rows, header/schema width, or any suffix containing
        # a non-empty or None expected value.
        valid_omitted_suffix = (
            row_index > 0
            and row_index < expected_rows
            and row_index < actual_rows
            and len(expected_row) == expected_columns
            and actual_header_width == expected_columns
            and len(expected_widths) == 1
            and len(actual_row) < expected_columns
            and all(value == "" for value in expected_row[len(actual_row):])
        )
        if valid_omitted_suffix:
            omitted = expected_columns - len(actual_row)
            trailing_blank_omissions_accepted += omitted
            rows_with_trailing_blank_omissions.add(row_index + 1)
        for column_index in range(max_columns):
            expected_present = column_index < len(expected_row)
            actual_present = column_index < len(actual_row)
            expected_value = expected_row[column_index] if expected_present else None
            actual_value = actual_row[column_index] if actual_present else None
            if valid_omitted_suffix and column_index >= len(actual_row) and column_index < expected_columns:
                continue
            if expected_present == actual_present and expected_value == actual_value:
                continue
            mismatch_row_indexes.add(row_index + 1)
            kind = _workflow_mismatch_class(
                expected_value, actual_value,
                missing_cell=(not expected_present or not actual_present),
            )
            by_class[kind] = by_class.get(kind, 0) + 1
            column = _column_name(column_index)
            header = expected[0][column_index] if expected and column_index < len(expected[0]) else None
            label = f"{column}: {header}" if isinstance(header, str) and len(header) <= 80 and not any(c in header for c in "\r\n") else column
            by_column[label] = by_column.get(label, 0) + 1
            if first is None:
                first = {
                    "row_index": row_index + 1,
                    "column_index": column_index + 1,
                    "a1": f"{column}{row_index + 1}",
                    "column": column,
                    "column_name": header if isinstance(header, str) else None,
                    "expected_type": type(expected_value).__name__,
                    "actual_type": type(actual_value).__name__,
                    "expected_value": _safe_value_fingerprint(expected_value),
                    "actual_value": _safe_value_fingerprint(actual_value),
                    "class": kind,
                }
            if len(mismatch_samples) < 25:
                mismatch_samples.append({
                    "row_index": row_index + 1,
                    "column_index": column_index + 1,
                    "a1": f"{column}{row_index + 1}",
                    "column_name": header if isinstance(header, str) else None,
                    "expected_value": _safe_value_fingerprint(expected_value),
                    "actual_value": _safe_value_fingerprint(actual_value),
                    "class": kind,
                })

    shift_count = 0
    for index, expected_row in enumerate(expected):
        if index < len(actual) and not _workflow_row_matches_with_trailing_blanks(expected_row, actual[index], expected_columns):
            if ((index > 0 and expected_row == actual[index - 1]) or
                    (index + 1 < len(actual) and expected_row == actual[index + 1])):
                shift_count += 1
    if shift_count:
        by_class["ROW_SHIFT"] = shift_count

    return {
        "matches": not by_class,
        "expected_rows": expected_rows,
        "actual_rows": actual_rows,
        "expected_columns": expected_columns,
        "actual_columns": actual_columns,
        "first_mismatch": first,
        "mismatch_samples": mismatch_samples,
        "total_mismatch_cells": sum(by_column.values()),
        "mismatch_rows": len(mismatch_row_indexes),
        "mismatch_by_column": by_column,
        "mismatch_by_class": by_class,
        "trailing_blank_omissions_accepted": trailing_blank_omissions_accepted,
        "rows_with_trailing_blank_omissions": len(rows_with_trailing_blank_omissions),
    }


def validate_readback(actual: list[list[Any]], expected: list[list[Any]]) -> dict[str, Any]:
    diagnostic = diagnose_workflow_readback(expected, actual)
    if not diagnostic["matches"]:
        raise ValueError("publication readback mismatch")
    if not actual or actual[0] != expected[0]:
        raise ValueError("publication header mismatch")
    ids = [str(row[0]) for row in actual[1:] if row and row[0] not in (None, "")]
    if len(ids) != len(set(ids)):
        raise ValueError("publication readback contains duplicate project_id")
    return {
        "status": "PASS",
        "rows": len(ids),
        "columns": len(expected[0]),
        "trailing_blank_omissions_accepted": diagnostic["trailing_blank_omissions_accepted"],
        "rows_with_trailing_blank_omissions": diagnostic["rows_with_trailing_blank_omissions"],
    }
