#!/usr/bin/env python3
"""Pure payment assignment/visit/project materialization and Sheet row builders.

No network, credential, Google Sheets, or DataLens operations live here.
Money arithmetic is Decimal-only; raw values and diagnostics are retained.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from decimal import Decimal
from typing import Any, Iterable, Mapping

from .payment_detail_xlsx import NORMALIZED_FIELDS, join_payment_assignments, normalize_assignment
from .technical_ids import integer_id

ASSIGNMENT_COLUMNS = (
    "project_id", "project_name", "client", "manager", "visit_id", "my_id",
    "workflow_state_codes", "visit_reward_raw", "visit_reward",
    "transport_expense_raw", "transport_expense", "expense_compensation_raw",
    "expense_compensation", "bonus_penalty_raw", "bonus_penalty",
    "portal_paid_amount_raw", "portal_paid_amount", "payment_join_status",
    "payment_numeric_status", "money_diagnostics",
)

VISIT_PUBLICATION_COLUMNS = (
    "project_id", "project_name", "client", "manager", "visit_id",
    "payment_assignment_count", "Вознаграждение за визит",
    "Оплачено по данным портала", "Количество положительных выплат",
    "Количество нулевых выплат", "workflow_state_codes", "Статус данных выплат",
)

PROJECT_PUBLICATION_COLUMNS = (
    "project_id", "project_name", "client", "manager",
    "Количество визитов с выплатами", "Количество записей выплат",
    "Вознаграждение за визиты", "Оплачено по данным портала",
    "Количество записей с положительной выплатой",
    "Количество записей с нулевой выплатой",
    "Количество визитов с положительной выплатой",
    "Количество визитов с нулевой выплатой",
    "Строки, требующие проверки", "Несопоставленные строки",
    "Статус данных выплат",
)

FORBIDDEN_PUBLICATION_HEADERS = {
    "my_id", "action_id", "ФИО ТП", "Логин", "Адрес", "Телефон", "Email",
    "email", "phone", "participant_name", "participant_login",
}


def _state_value(value: Any) -> int | str:
    text = str(value)
    return int(text) if text.isdigit() else text


def _state_sort(value: int | str) -> tuple[int, int | str]:
    return (0, value) if isinstance(value, int) else (1, value)


def _state_codes_json(values: Iterable[Any]) -> str:
    clean = {_state_value(value) for value in values if value not in (None, "")}
    return json.dumps(sorted(clean, key=_state_sort), ensure_ascii=False, separators=(",", ":"))


def _context(project_id: str | int, project_metadata: Mapping[str, Any]) -> dict[str, Any]:
    def metadata_text(key: str, fallback: str | None = None) -> str | None:
        value = project_metadata.get(key)
        if value is None and fallback:
            value = project_metadata.get(fallback)
        # Saved workflow checkpoint metadata uses {state, value} envelopes.
        if isinstance(value, Mapping) and "value" in value:
            if value.get("state") not in (None, "VALUE_PRESENT"):
                return None
            value = value.get("value")
        return value if isinstance(value, str) else None

    return {
        "project_id": integer_id(project_id, "project_id"),
        "project_name": metadata_text("project_name"),
        "client": metadata_text("client"),
        # The workflow snapshot names this already-qualified dimension primary_manager.
        "manager": metadata_text("manager", "primary_manager"),
    }


def _amount_status(row: Mapping[str, Any]) -> str:
    statuses = (row.get("money_diagnostics") or {}).values()
    if "REVIEW" in statuses:
        return "REVIEW"
    if "UNCLASSIFIED_BLANK" in statuses:
        return "INCOMPLETE"
    return "OK"


def _sum_decimals(values: Iterable[Decimal | None]) -> Decimal | None:
    present = [value for value in values if isinstance(value, Decimal)]
    return sum(present, Decimal(0)) if present else None


def _unique_key_diagnostics(assignments: list[Mapping[str, Any]], workflows: Iterable[Mapping[str, Any]]) -> tuple[int, int]:
    counts = Counter((str(row.get("project_id") or ""), str(row.get("my_id") or "")) for row in assignments)
    duplicate_primary_keys = sum(count - 1 for count in counts.values() if count > 1)
    visits_by_key: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in workflows:
        my_id, visit_id = row.get("action_id", row.get("my_id")), row.get("visit_id")
        if my_id not in (None, "") and visit_id not in (None, ""):
            visits_by_key[(str(row.get("project_id") or ""), str(my_id))].add(str(visit_id))
    multi_visit_conflicts = sum(len(visit_ids) > 1 for visit_ids in visits_by_key.values())
    return duplicate_primary_keys, multi_visit_conflicts


def materialize_payment_data(
    project_id: str | int,
    project_metadata: Mapping[str, Any],
    payment_rows: Iterable[Mapping[str, Any]],
    workflow_rows: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build assignment, visit, and project layers for one project.

    Source payment rows are preserved one-for-one, including duplicate or
    unmatched keys. Every assignment contributes to its project aggregate.
    Only assignments with a qualified, nonblank visit mapping contribute to
    visit aggregates; unmapped rows remain visible in project diagnostics and
    make project data status INCOMPLETE. Their relationship to visits is
    unknown, not erroneous or payment-only.
    """
    pid = str(project_id)
    payments = [dict(row) for row in payment_rows]
    workflows = [dict(row) for row in workflow_rows if str(row.get("project_id", pid)) == pid]
    joined = join_payment_assignments(pid, payments, workflows)
    context = _context(pid, project_metadata)

    assignments: list[dict[str, Any]] = []
    for joined_row in joined["assignments"]:
        row = normalize_assignment(joined_row)
        row.update(context)
        row["workflow_state_codes_list"] = list(row.get("workflow_state_codes") or [])
        row["workflow_state_codes"] = _state_codes_json(row["workflow_state_codes_list"])
        row["payment_join_status"] = row.pop("join_status")
        row["payment_numeric_status"] = _amount_status(row)
        row["project_id"] = context["project_id"]
        if row.get("visit_id") not in (None, ""):
            row["visit_id"] = integer_id(row["visit_id"], "visit_id")
        row["money_diagnostics"] = json.dumps(row["money_diagnostics"], sort_keys=True, separators=(",", ":"))
        assignments.append(row)

    duplicate_primary_keys, my_id_multi_visit_conflicts = _unique_key_diagnostics(assignments, workflows)
    by_visit: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in assignments:
        if row.get("payment_join_status") == "MATCHED" and row.get("visit_id") not in (None, ""):
            by_visit[str(row["visit_id"])].append(row)

    visit_rows: list[dict[str, Any]] = []
    visit_reward_conflicts = 0
    for visit_id, rows in sorted(by_visit.items(), key=lambda item: item[0]):
        rewards = {row["visit_reward"] for row in rows if isinstance(row.get("visit_reward"), Decimal)}
        conflict = len(rewards) > 1
        visit_reward_conflicts += int(conflict)
        visit_reward = next(iter(rewards)) if len(rewards) == 1 else None
        paid_values = [row.get("portal_paid_amount") for row in rows]
        paid_complete = all(isinstance(value, Decimal) for value in paid_values)
        paid_total = sum(paid_values, Decimal(0)) if paid_complete else None
        positive_count = sum(isinstance(value, Decimal) and value > 0 for value in paid_values)
        zero_count = sum(isinstance(value, Decimal) and value == 0 for value in paid_values)
        statuses = {row["payment_numeric_status"] for row in rows}
        data_status = "REVIEW" if conflict or "REVIEW" in statuses else "INCOMPLETE" if "INCOMPLETE" in statuses else "COMPLETE"
        state_values = {code for row in rows for code in row["workflow_state_codes_list"]}
        visit_rows.append({
            **context,
            "visit_id": integer_id(visit_id, "visit_id"),
            "payment_assignment_count": len(rows),
            "visit_reward": visit_reward,
            "visit_reward_conflict": conflict,
            "visit_reward_conflict_code": "VISIT_REWARD_CONFLICT" if conflict else None,
            "portal_paid_total": paid_total,
            "portal_paid_total_complete": paid_complete,
            "positive_paid_assignment_count": positive_count,
            "zero_paid_assignment_count": zero_count,
            "workflow_state_codes": _state_codes_json(state_values),
            "payment_data_status": data_status,
        })

    review_rows = sum(row["payment_numeric_status"] == "REVIEW" for row in assignments)
    incomplete_rows = sum(row["payment_numeric_status"] == "INCOMPLETE" for row in assignments)
    visit_mapped_rows = [
        row for row in assignments
        if row["payment_join_status"] == "MATCHED" and row.get("visit_id") not in (None, "")
    ]
    unmatched_rows = len(assignments) - len(visit_mapped_rows)
    paid_decimals = [row.get("portal_paid_amount") for row in assignments]
    valid_paid = [value for value in paid_decimals if isinstance(value, Decimal)]
    visit_rewards = [row["visit_reward"] for row in visit_rows if isinstance(row["visit_reward"], Decimal) and not row["visit_reward_conflict"]]
    positive_assignment_count = sum(value > 0 for value in valid_paid)
    zero_assignment_count = sum(value == 0 for value in valid_paid)
    positive_visit_count = sum(row["portal_paid_total_complete"] and isinstance(row["portal_paid_total"], Decimal) and row["portal_paid_total"] > 0 for row in visit_rows)
    zero_visit_count = sum(row["portal_paid_total_complete"] and isinstance(row["portal_paid_total"], Decimal) and row["portal_paid_total"] == 0 for row in visit_rows)
    project_status = "REVIEW" if review_rows or visit_reward_conflicts else "INCOMPLETE" if incomplete_rows or unmatched_rows else "COMPLETE"
    project_row = {
        **context,
        "visit_count_with_payment_assignments": len(visit_rows),
        "payment_assignment_count": len(assignments),
        "total_visit_reward": _sum_decimals(visit_rewards),
        "total_portal_paid": _sum_decimals(valid_paid),
        "positive_paid_assignment_count": positive_assignment_count,
        "zero_paid_assignment_count": zero_assignment_count,
        "visits_with_positive_portal_paid": positive_visit_count,
        "visits_with_zero_portal_paid": zero_visit_count,
        "payment_rows_numeric_review": review_rows,
        "payment_rows_incomplete": incomplete_rows,
        "payment_rows_unmatched": unmatched_rows,
        "visit_reward_conflicts": visit_reward_conflicts,
        "payment_data_status": project_status,
    }

    diagnostics = {
        "assignment_rows": len(assignments),
        "matched_assignments": sum(row["payment_join_status"] == "MATCHED" for row in assignments),
        "unmatched_assignments": unmatched_rows,
        "visit_mapped_assignments": len(visit_mapped_rows),
        "unmatched_visit_mapping_assignments": unmatched_rows,
        "workflow_only_relevant": joined["workflow_only_count"],
        "unique_my_ids": len({row["my_id"] for row in assignments if row.get("my_id") not in (None, "")}),
        "unique_visits": len(visit_rows),
        "multi_assignment_visits": sum(row["payment_assignment_count"] > 1 for row in visit_rows),
        "max_assignments_per_visit": max((row["payment_assignment_count"] for row in visit_rows), default=0),
        "numeric_rows_ok": sum(row["payment_numeric_status"] == "OK" for row in assignments),
        "numeric_rows_review": review_rows,
        "numeric_rows_incomplete": incomplete_rows,
        "negative_payment_rows": sum(isinstance(value, Decimal) and value < 0 for value in valid_paid),
        "zero_payment_rows": zero_assignment_count,
        "positive_payment_rows": positive_assignment_count,
        "visit_reward_conflicts": visit_reward_conflicts,
        "assignment_duplicate_primary_keys": duplicate_primary_keys,
        "my_id_multi_visit_conflicts": my_id_multi_visit_conflicts,
    }
    invariants = evaluate_invariants(assignments, visit_rows, [project_row], diagnostics, len(payments))
    return {
        "assignment_rows": assignments,
        "visit_payment_aggregate": visit_rows,
        "project_payment_aggregate": [project_row],
        "diagnostics": diagnostics,
        "invariants": invariants,
        "workflow_only_keys": joined["workflow_only_keys"],
    }


def evaluate_invariants(
    assignments: list[Mapping[str, Any]],
    visits: list[Mapping[str, Any]],
    projects: list[Mapping[str, Any]],
    diagnostics: Mapping[str, int],
    source_payment_row_count: int,
) -> dict[str, bool]:
    matched = sum(row.get("payment_join_status") == "MATCHED" for row in assignments)
    unmatched = len(assignments) - matched
    visit_mapped_assignments = [
        row for row in assignments
        if row.get("payment_join_status") == "MATCHED" and row.get("visit_id") not in (None, "")
    ]
    unmatched_visit_mapping_count = len(assignments) - len(visit_mapped_assignments)
    keys = [(str(row.get("project_id") or ""), str(row.get("my_id") or "")) for row in assignments]
    unique = len(set(keys))
    project_count = sum(int(row.get("payment_assignment_count", 0)) for row in projects)
    visit_count = sum(int(row.get("payment_assignment_count", 0)) for row in visits)
    visit_by_id = Counter(str(row.get("visit_id") or "") for row in visit_mapped_assignments)
    materialized_visit_by_id = {
        str(row.get("visit_id") or ""): int(row.get("payment_assignment_count", 0)) for row in visits
    }
    paid_values = [row.get("portal_paid_amount") for row in assignments if isinstance(row.get("portal_paid_amount"), Decimal)]
    expected_paid = _sum_decimals(paid_values)
    actual_paid = _sum_decimals(row.get("total_portal_paid") for row in projects)
    expected_reward = _sum_decimals(
        row.get("visit_reward") for row in visits
        if isinstance(row.get("visit_reward"), Decimal) and not row.get("visit_reward_conflict")
    )
    actual_reward = _sum_decimals(row.get("total_visit_reward") for row in projects)
    zero_source = sum(isinstance(row.get("portal_paid_amount"), Decimal) and row["portal_paid_amount"] == 0 for row in assignments)
    negative_source = sum(isinstance(row.get("portal_paid_amount"), Decimal) and row["portal_paid_amount"] < 0 for row in assignments)
    zero_project = sum(int(row.get("zero_paid_assignment_count", 0)) for row in projects)
    negative_count = int(diagnostics.get("negative_payment_rows", 0))
    return {
        "A": len(assignments) == matched + unmatched and len(assignments) == source_payment_row_count,
        "B": unique == len(assignments) and all(row.get("my_id") not in (None, "") for row in assignments),
        "C": project_count == len(assignments),
        "D1_PROJECT_COMPLETENESS": project_count == len(assignments) == source_payment_row_count,
        "D2_VISIT_PROVENANCE": (
            visit_count == len(visit_mapped_assignments)
            and all(row.get("visit_id") not in (None, "") for row in visits)
            and len(materialized_visit_by_id) == len(visits)
            and materialized_visit_by_id == dict(visit_by_id)
        ),
        "D2_RECONCILIATION": (
            len(assignments) == len(visit_mapped_assignments) + unmatched_visit_mapping_count
            and int(diagnostics.get("visit_mapped_assignments", len(visit_mapped_assignments))) == len(visit_mapped_assignments)
            and int(diagnostics.get("unmatched_visit_mapping_assignments", unmatched_visit_mapping_count)) == unmatched_visit_mapping_count
        ),
        "E": actual_paid == expected_paid,
        "F": actual_reward == expected_reward,
        "G": len(assignments) == source_payment_row_count and unique == len(assignments),
        "H": zero_project == zero_source and len(assignments) >= zero_source,
        "I": negative_count == negative_source,
    }


def _visit_publication_row(row: Mapping[str, Any]) -> list[Any]:
    return [
        row["project_id"], row.get("project_name"), row.get("client"), row.get("manager"),
        row["visit_id"], row["payment_assignment_count"], row.get("visit_reward"),
        row.get("portal_paid_total"), row["positive_paid_assignment_count"],
        row["zero_paid_assignment_count"], row["workflow_state_codes"], row["payment_data_status"],
    ]


def _project_publication_row(row: Mapping[str, Any]) -> list[Any]:
    return [
        row["project_id"], row.get("project_name"), row.get("client"), row.get("manager"),
        row["visit_count_with_payment_assignments"], row["payment_assignment_count"],
        row.get("total_visit_reward"), row.get("total_portal_paid"),
        row["positive_paid_assignment_count"], row["zero_paid_assignment_count"],
        row["visits_with_positive_portal_paid"], row["visits_with_zero_portal_paid"],
        row["payment_rows_numeric_review"] + row["payment_rows_incomplete"],
        row["payment_rows_unmatched"], row["payment_data_status"],
    ]


def _validate_payload(headers: tuple[str, ...], rows: list[list[Any]], unique_indices: tuple[int, ...]) -> None:
    if any(header in FORBIDDEN_PUBLICATION_HEADERS for header in headers):
        raise ValueError("publication payload contains a prohibited identity field")
    if any(len(row) != len(headers) for row in rows):
        raise ValueError("publication row length does not match column contract")
    keys = [tuple(row[index] for index in unique_indices) for row in rows]
    if len(keys) != len(set(keys)):
        raise ValueError("publication grain contains duplicate keys")
    for row in rows:
        for value in row:
            if isinstance(value, float):
                raise TypeError("binary float is forbidden in payment publication payloads")
            if isinstance(value, Decimal) and not value.is_finite():
                raise ValueError("non-finite Decimal in publication payload")


def _validate_column_types(headers: tuple[str, ...], rows: list[list[Any]], expected: tuple[Any, ...]) -> None:
    if len(headers) != len(expected):
        raise ValueError("publication type contract width does not match headers")
    for row in rows:
        for index, (value, accepted) in enumerate(zip(row, expected)):
            if value is None and type(None) in (accepted if isinstance(accepted, tuple) else (accepted,)):
                continue
            accepted_types = accepted if isinstance(accepted, tuple) else (accepted,)
            if int in accepted_types and isinstance(value, bool):
                raise TypeError(f"boolean at numeric count column {headers[index]}")
            if not isinstance(value, accepted_types):
                raise TypeError(f"invalid type at publication column {headers[index]}")


def build_publication_payloads(materialized: Mapping[str, Any]) -> dict[str, list[list[Any]]]:
    """Build local-only values payloads (header row + rows), with no Sheets I/O."""
    visits = materialized["visit_payment_aggregate"]
    projects = materialized["project_payment_aggregate"]
    visit_rows = [_visit_publication_row(row) for row in visits]
    project_rows = [_project_publication_row(row) for row in projects]
    _validate_payload(VISIT_PUBLICATION_COLUMNS, visit_rows, (0, 4))
    _validate_payload(PROJECT_PUBLICATION_COLUMNS, project_rows, (0,))
    _validate_column_types(
        VISIT_PUBLICATION_COLUMNS,
        visit_rows,
        (int, (str, type(None)), (str, type(None)), (str, type(None)), int,
         int, (Decimal, type(None)), (Decimal, type(None)), int, int, str, str),
    )
    _validate_column_types(
        PROJECT_PUBLICATION_COLUMNS,
        project_rows,
        (int, (str, type(None)), (str, type(None)), (str, type(None)),
         int, int, (Decimal, type(None)), (Decimal, type(None)), int, int,
         int, int, int, int, str),
    )
    return {
        "Выплаты по визитам": [list(VISIT_PUBLICATION_COLUMNS), *visit_rows],
        "Выплаты по проектам": [list(PROJECT_PUBLICATION_COLUMNS), *project_rows],
    }


def _json_value(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite Decimal cannot be serialized")
        return format(value, "f")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        raise TypeError("binary float is forbidden in payment payload serialization")
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_json_value(item) for item in value) + "]"
    if isinstance(value, Mapping):
        return "{" + ",".join(json.dumps(str(key), ensure_ascii=False) + ":" + _json_value(item) for key, item in value.items()) + "}"
    raise TypeError(f"unsupported payload type: {type(value).__name__}")


def serialize_sheet_payload(rows: list[list[Any]]) -> str:
    """Serialize exact JSON numeric literals (including Decimal) without I/O."""
    return _json_value(rows)


def publication_dry_run(materialized: Mapping[str, Any]) -> dict[str, Any]:
    payloads = build_publication_payloads(materialized)
    return {
        name: {
            "columns": len(rows[0]),
            "data_rows": len(rows) - 1,
            "payload": rows,
            "json_payload": serialize_sheet_payload(rows),
            "pass": all(len(row) == len(rows[0]) for row in rows[1:]),
        }
        for name, rows in payloads.items()
    }
