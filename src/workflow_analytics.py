"""Internal Visit × workflow-predicate analytics.

This module is deliberately independent of the published Sheet schema.  It
keeps every predicate membership and only aggregates after membership-level
deduplication; a Visit may therefore match more than one predicate.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

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
