"""Pure publication planning for the Phase 2 workflow analytics.

This module accepts already-materialized rows only.  It has no Portal or
Google client dependency, making the publication and rollback contracts
testable before a controlled production run.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

from .workflow_analytics import WORKFLOW_COUNTER_FIELDS, WORKFLOW_STATES, validate_workflow_metrics

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
        line: list[Any] = [row.get("project_id", ""), row.get("project_name", ""), row.get("client", ""), row.get("primary_manager", row.get("manager", "")), row.get("project_visit_count", 0)]
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


def validate_readback(actual: list[list[Any]], expected: list[list[Any]]) -> dict[str, Any]:
    if actual != expected:
        raise ValueError("publication readback mismatch")
    if not actual or actual[0] != expected[0]:
        raise ValueError("publication header mismatch")
    ids = [str(row[0]) for row in actual[1:] if row and row[0] not in (None, "")]
    if len(ids) != len(set(ids)):
        raise ValueError("publication readback contains duplicate project_id")
    return {"status": "PASS", "rows": len(ids), "columns": len(actual[0])}

