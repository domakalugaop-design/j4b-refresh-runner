import pytest

from src.refresh import COLUMNS
from src.workflow_publication import (
    PRIMARY_ANALYTICS_COLUMNS,
    THIRD_TAB_COLUMNS,
    THIRD_TAB_NAME,
    backup_snapshot,
    primary_columns,
    publication_plan,
    rollback_snapshot,
    third_tab_rows,
    validate_materialized_rows,
    validate_readback,
)


def materialized(pid, **overrides):
    row = {field: 0 for field in PRIMARY_ANALYTICS_COLUMNS}
    row.update({"project_id": str(pid), "project_name": f"Project {pid}", "client": "Client", "primary_manager": "Manager"})
    row.update(overrides)
    row["project_visit_count"] = max(row.get("project_visit_count", 0), 1)
    row["workflow_covered_visits"] = min(row.get("workflow_covered_visits", 0), row["project_visit_count"])
    row["workflow_unclassified_visits"] = row["project_visit_count"] - row["workflow_covered_visits"]
    return row


def test_third_tab_has_exactly_fourteen_human_readable_workflow_columns_and_business_diagnostics():
    assert len([column for column in THIRD_TAB_COLUMNS if "[" in column]) == 14
    assert ["Назначено", "Выполнено", "Завершено"] == THIRD_TAB_COLUMNS[19:22]
    assert THIRD_TAB_COLUMNS[-3:] == ["Покрыто workflow", "Без workflow-состояния", "Несколько workflow-состояний"]


def test_primary_fields_are_additive_and_existing_order_is_preserved():
    columns = primary_columns(COLUMNS)
    assert columns[: len(COLUMNS)] == COLUMNS
    assert columns[-21:] == PRIMARY_ANALYTICS_COLUMNS


def test_third_tab_is_one_row_per_project_and_deterministically_sorted():
    rows = third_tab_rows([materialized("10", project_visit_count=2), materialized("2")])
    assert [row[0] for row in rows[1:]] == ["2", "10"]
    assert len(rows) == 3


def test_duplicate_project_ids_are_rejected():
    with pytest.raises(ValueError, match="duplicate project_id"):
        third_tab_rows([materialized("1"), materialized("1")])


def test_absent_and_existing_tab_plans_are_idempotent():
    rows = [materialized("1")]
    absent = publication_plan(rows, set())
    present = publication_plan(rows, {THIRD_TAB_NAME}, previous_third_rows=3)
    assert absent["create_tab"] is True
    assert present["create_tab"] is False
    assert absent["headers"] == present["headers"]
    assert present["clear_range"] is not None
    assert publication_plan(rows, {THIRD_TAB_NAME}) == publication_plan(rows, {THIRD_TAB_NAME})


def test_backup_and_rollback_restore_exact_logical_state():
    original = {"projects_current": [["project_id"], ["1"]]}
    snapshot = backup_snapshot("sheet", original, {"tabs": ["projects_current"]})
    assert rollback_snapshot(snapshot) == original


def test_readback_validator_rejects_stale_or_changed_rows():
    expected = third_tab_rows([materialized("1")])
    assert validate_readback(expected, expected)["status"] == "PASS"
    with pytest.raises(ValueError, match="readback mismatch"):
        validate_readback(expected + [["stale"]], expected)


def test_shrink_plan_clears_obsolete_tail_rows():
    old = publication_plan([materialized(str(i)) for i in range(1, 11)], {THIRD_TAB_NAME})
    new = publication_plan([materialized(str(i)) for i in range(1, 7)], {THIRD_TAB_NAME}, previous_third_rows=old["row_count"] + 1)
    clear = new["clear_range"]
    assert clear["start_row"] == 8
    assert clear["end_row"] == 11


def test_publication_plan_is_pure_and_does_not_reacquire_portal():
    assert publication_plan([materialized("1")], set())["row_count"] == 1
