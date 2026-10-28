"""Internal Visit × workflow-predicate analytics.

This module is deliberately independent of the published Sheet schema.  It
keeps every predicate membership and only aggregates after membership-level
deduplication; a Visit may therefore match more than one predicate.
"""
from __future__ import annotations

from collections import defaultdict
import math
from typing import Any, Iterable, Mapping

WORKFLOW_STATES = {
    0: "Отправлено приглашение",
    5: "Приглашение отклонено",
    10: "Приглашение принято",
    15: "Отклонено менеджером",
    20: "Подтверждено менеджером",
    25: "Провалено пользователем",
    30: "Выполнено пользователем",
    35: "Анкета отклонена",
    37: "Анкета проверена",
    38: "Вопрос координатору",
    39: "Есть претензия",
    40: "Анкета утверждена",
    45: "В оплате отказано",
    50: "Оплачено",
}
WORKFLOW_STATE_CODES = tuple(WORKFLOW_STATES)
WORKFLOW_COUNTER_FIELDS = tuple(f"workflow_state_{code}_visits" for code in WORKFLOW_STATE_CODES)
SUCCESSFUL_CURRENT_STATE_CODES = frozenset({37, 40, 50})


def _unwrap(value: Any) -> Any:
    """Acquisition checkpoints wrap values with provenance envelopes."""
    return value.get("value") if isinstance(value, Mapping) and "value" in value else value


def _integer(value: Any) -> int | None:
    value = _unwrap(value)
    if isinstance(value, bool) or value in (None, ""):
        return None
    text = str(value).strip()
    return int(text) if text.isdigit() else None


def resolve_current_visit_states(
    project_id: str,
    visit_rows: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, frozenset[int] | None], int]:
    """Resolve current workflow predicate memberships per unique Visit.

    A Visit may match several workflow predicates on the same action. Keep that
    set, and use the highest action ID to distinguish current memberships from
    older ones. A missing status remains unresolved. An explicit free marker is
    excluded unless a later positive action is explicitly marked assigned.
    """
    grouped: dict[str, list[tuple[int | None, frozenset[int], str, str]]] = defaultdict(list)
    target = str(project_id)
    for row in visit_rows:
        row_project = _unwrap(row.get("project_id"))
        if row_project not in (None, "") and str(row_project) != target:
            continue
        visit_value = _unwrap(row.get("visit_id"))
        if visit_value in (None, ""):
            continue
        visit_id = str(visit_value)
        action_id = _integer(row.get("action_id"))
        state_values = _unwrap(row.get("current_state_codes")) if "current_state_codes" in row else None
        if state_values is None or state_values == []:
            state_value = row.get("current_state_code", row.get("raw_status"))
            state_values = [state_value]
        elif not isinstance(state_values, (list, tuple, set, frozenset)):
            state_values = [state_values]
        state_codes = frozenset(
            code for code in (_integer(value) for value in state_values)
            if code in WORKFLOW_STATES
        )
        assignment = str(_unwrap(row.get("assignment_state")) or "")
        resolution = str(_unwrap(row.get("current_state_resolution")) or "")
        grouped[visit_id].append((action_id, state_codes, assignment, resolution))

    resolved: dict[str, frozenset[int] | None] = {}
    unresolved = 0
    for visit_id, rows in grouped.items():
        free_action_ids = [
            action_id for action_id, _, assignment, resolution in rows
            if assignment == "UNASSIGNED_FREE" or resolution == "EXPLICIT_UNASSIGNED"
        ]
        latest_free_action_id = max((action_id or 0) for action_id in free_action_ids) if free_action_ids else None
        has_later_explicit_assignment = any(
            assignment == "ASSIGNED" and action_id is not None
            and action_id > (latest_free_action_id if latest_free_action_id is not None else 0)
            for action_id, _, assignment, _ in rows
        )
        if free_action_ids and not has_later_explicit_assignment:
            resolved[visit_id] = frozenset()
            continue

        ranked = [(action_id, codes) for action_id, codes, _, _ in rows
                  if action_id is not None and action_id > 0]
        if ranked:
            newest_action_id = max(action_id for action_id, _ in ranked)
            latest_states = frozenset(
                code for action_id, codes in ranked if action_id == newest_action_id
                for code in codes
            )
        else:
            latest_states = frozenset(code for _, codes, _, _ in rows for code in codes)

        if latest_states:
            resolved[visit_id] = latest_states
        else:
            resolved[visit_id] = None
            unresolved += 1
    return resolved, unresolved


def successful_visit_metrics(
    project_id: str,
    visit_rows: Iterable[Mapping[str, Any]],
    planned_visit_count: Any,
) -> dict[str, int | float | None]:
    """Calculate current successful completion on unique Visit IDs.

    Current predicate memberships 37, 40, and 50 count. State 45 is a payment
    refusal. A prior membership cannot qualify a Visit after its current action
    moved to a non-success state. Percentages are stored as ratios (1.0 == 100%)
    to preserve the Sheets/DataLens numeric contract; values over 1.0 are
    intentionally allowed.
    """
    current_states, unresolved = resolve_current_visit_states(project_id, visit_rows)
    successful = sum(bool(states and states & SUCCESSFUL_CURRENT_STATE_CODES)
                     for states in current_states.values())
    plan = _unwrap(planned_visit_count)
    execution_pct: float | None = None
    if (unresolved == 0 and isinstance(plan, (int, float)) and not isinstance(plan, bool)
            and math.isfinite(float(plan)) and plan > 0):
        execution_pct = successful / float(plan)
    return {
        "total_project_visits": len(current_states),
        "successful_visits_current": successful,
        "current_state_unresolved_visits": unresolved,
        "execution_pct_successful": execution_pct,
    }


def membership_key(membership: dict[str, Any]) -> tuple[str, str, int]:
    return (str(membership["project_id"]), str(membership["visit_id"]), int(membership["workflow_state_code"]))


def deduplicate_memberships(memberships: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[tuple[str, str, int], dict[str, Any]] = {}
    for item in memberships:
        key = membership_key(item)
        if key[2] not in WORKFLOW_STATES:
            raise ValueError(f"unknown workflow state code: {key[2]}")
        unique.setdefault(key, {**item, "project_id": key[0], "visit_id": key[1], "workflow_state_code": key[2]})
    return list(unique.values())


def project_workflow_metrics(project_id: str, canonical_visit_ids: Iterable[str], memberships: Iterable[dict[str, Any]]) -> dict[str, int]:
    visits = {str(value) for value in canonical_visit_ids}
    unique = [item for item in deduplicate_memberships(memberships) if str(item["project_id"]) == str(project_id)]
    by_visit: dict[str, set[int]] = defaultdict(set)
    for item in unique:
        visit_id = str(item["visit_id"])
        if visit_id in visits:
            by_visit[visit_id].add(int(item["workflow_state_code"]))
    counters = {
        field: len({visit_id for visit_id, codes in by_visit.items() if code in codes})
        for code, field in ((code, f"workflow_state_{code}_visits") for code in WORKFLOW_STATE_CODES)
    }
    covered = set(by_visit)
    project_visit_count = len(visits)
    workflow_covered = len(covered)
    multi_match = sum(1 for visit_id in covered if len(by_visit[visit_id]) > 1)
    unclassified = project_visit_count - workflow_covered
    if unclassified < 0:
        raise ValueError("workflow_covered_visits exceeds project_visit_count")
    counters.update({
        "project_visit_count": project_visit_count,
        "assigned_visits": counters["workflow_state_20_visits"],
        "executed_visits_customer": counters["workflow_state_30_visits"],
        "finished_visits": len({visit_id for visit_id, codes in by_visit.items() if codes & {37, 40, 50} and (not visits or visit_id in visits)}),
        # This is the legacy action-membership view of Visits that have ever
        # shown an approval/payment state. State 37 is intentionally excluded:
        # review alone is not approval under the approved business rule.
        "historically_approved_visits": len({
            visit_id for visit_id, codes in by_visit.items()
            if codes & {40, 50} and (not visits or visit_id in visits)
        }),
        "workflow_covered_visits": workflow_covered,
        "workflow_unclassified_visits": unclassified,
        "workflow_multi_match_visits": multi_match,
    })
    validate_workflow_metrics(counters)
    return counters


def validate_workflow_metrics(metrics: dict[str, int]) -> None:
    total = metrics["project_visit_count"]
    for field in WORKFLOW_COUNTER_FIELDS + (
        "assigned_visits", "executed_visits_customer", "finished_visits",
        "workflow_covered_visits", "workflow_multi_match_visits",
    ):
        if metrics[field] < 0 or metrics[field] > total:
            raise ValueError(f"{field} exceeds project_visit_count")
    if metrics["workflow_unclassified_visits"] < 0:
        raise ValueError("workflow_unclassified_visits must be non-negative")
