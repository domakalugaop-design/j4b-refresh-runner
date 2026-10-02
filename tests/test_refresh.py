from datetime import date
import pytest

from src.refresh import BASE_COLUMNS, COLUMNS, PROJECT_TYPE_COLUMNS, materialize, merge_previous, reconcile_current_rows, regular_scope_counts, select_scope, sheet_rows, sheets_serial, summary
from src.production import load_acquisition_checkpoint, serialize_acquisition_checkpoint


def test_selector_current_previous_and_new():
    previous = [COLUMNS, ["1", "Legacy 0826"] + [""] * 29]
    catalogue = [
        {"project_id": "1", "project_name": "Legacy 0826"},
        {"project_id": "2", "project_name": "Current 0926"},
        {"project_id": "3", "project_name": "Future 0127"},
    ]
    selected = select_scope(catalogue, previous, today=date(2026, 9, 4))
    assert [row["project_id"] for row in selected] == ["1", "2", "3"]


def test_regular_selector_counts_new_current_previous_union_and_deduplicates():
    previous = [COLUMNS, ["1", "Legacy_0826"] + [""] * 31, ["2", "Current_0926"] + [""] * 31]
    catalogue = [
        {"project_id": "1", "project_name": "changed name"},
        {"project_id": "2", "project_name": "Current_0926"},
        {"project_id": "3", "project_name": "New_0926"},
        {"project_id": "4", "project_name": "New_0826"},
        {"project_id": "5", "project_name": "Historical_0726"},
    ]
    counts = regular_scope_counts(catalogue, previous, today=date(2026, 9, 30))
    selected = select_scope(catalogue, previous, today=date(2026, 9, 30))
    assert counts == {"new": 3, "current_month": 2, "previous_month": 2, "union": 5}
    assert [item["project_id"] for item in selected] == ["1", "2", "3", "4", "5"]


def test_regular_selector_checks_all_period_markers_and_deduplicates_union():
    previous = [COLUMNS, ["1", "Old_0726_then_0926"] + [""] * 31,
                ["2", "Contains_0926_and_0826"] + [""] * 31]
    catalogue = [
        {"project_id": "1", "project_name": "Old_0726_then_0926"},
        {"project_id": "2", "project_name": "Contains_0926_and_0826"},
    ]
    counts = regular_scope_counts(catalogue, previous, today=date(2026, 9, 30))
    selected = select_scope(catalogue, previous, today=date(2026, 9, 30))
    assert counts == {"new": 0, "current_month": 2, "previous_month": 1, "union": 2}
    assert [row["project_id"] for row in selected] == ["1", "2"]


def test_plan_zero_is_real_zero():
    projects = [{"project_id": "1", "planned_visit_count": {"value": 0}, "acquisition_state": "ACQUIRED"}]
    row = materialize(projects, [], "2026-09-04T10:00:00+00:00")[0]
    assert row["plan"] == 0
    assert row["plan_status"] == "VALID_PLAN"


def test_missing_plan_is_not_zero():
    projects = [{"project_id": "1", "planned_visit_count": {"value": None}, "acquisition_state": "ACQUIRED"}]
    visits = [{"project_id": "1", "raw_status": {"value": "40"}}]
    row = materialize(projects, visits, "2026-09-04T10:00:00+00:00")[0]
    assert row["plan"] is None
    assert row["plan_missing_with_activity"] is True


def test_native_serial_conversion():
    assert isinstance(sheets_serial("2026-09-04"), float)
    assert isinstance(sheets_serial("2026-09-04T10:00:00+00:00"), float)
    assert sheets_serial(46267.0) == 46267.0


def test_schema_is_33_columns_and_unique_summary():
    assert len(COLUMNS) == 33
    rows = [COLUMNS, ["1"] + [""] * 32, ["2"] + [""] * 32]
    assert summary(rows) == {"rows": 2, "unique": 2, "duplicates": 0}


def test_lifecycle_reconciliation_prunes_ids_absent_from_complete_universe():
    rows = [{"project_id": "100"}, {"project_id": "101"}, {"project_id": "102"}]
    current, stale = reconcile_current_rows(rows, {"100", "101"}, acquisition_complete=True)
    assert [row["project_id"] for row in current] == ["100", "101"]
    assert stale == {"102"}


def test_lifecycle_reconciliation_blocks_pruning_on_incomplete_acquisition():
    with pytest.raises(RuntimeError, match="incomplete"):
        reconcile_current_rows([{"project_id": "102"}], {"100"}, acquisition_complete=False)


def test_lifecycle_reconciliation_is_id_based_for_duplicate_names():
    rows = [{"project_id": "8183", "project_name": "same"}, {"project_id": "8184", "project_name": "same"}]
    current, stale = reconcile_current_rows(rows, {"8183", "8184"}, acquisition_complete=True)
    assert len(current) == 2 and not stale


def test_lifecycle_reconciliation_removes_old_id_but_keeps_new_same_name_id():
    rows = [{"project_id": "8062", "project_name": "same"}, {"project_id": "8183", "project_name": "same"}]
    current, stale = reconcile_current_rows(rows, {"8183"}, acquisition_complete=True)
    assert [row["project_id"] for row in current] == ["8183"]
    assert stale == {"8062"}


def test_lifecycle_reconciliation_keeps_authoritative_row_even_with_null_metrics():
    rows = [{"project_id": "8184", "completed": None, "execution_pct": None}]
    current, stale = reconcile_current_rows(rows, {"8184"}, acquisition_complete=True)
    assert current == rows and not stale


def test_lifecycle_reconciliation_keeps_hidden_related_entity_when_authoritative():
    rows = [{"project_id": "8184"}, {"project_id": "8185"}]
    current, stale = reconcile_current_rows(rows, {"8184", "8185"}, acquisition_complete=True)
    assert {row["project_id"] for row in current} == {"8184", "8185"}
    assert not stale


def test_lifecycle_reconciliation_fails_closed_on_unexpected_major_drop():
    rows = [{"project_id": str(i)} for i in range(100)]
    with pytest.raises(RuntimeError, match="suspicious stale-project count"):
        reconcile_current_rows(rows, {"0"}, acquisition_complete=True)


def test_sheet_rows_emit_full_width():
    row = {name: None for name in COLUMNS}
    row["project_id"] = "1"
    row["last_refreshed"] = "2026-09-04T10:00:00+00:00"
    values = sheet_rows([row])
    assert len(values[0]) == 33
    assert len(values[1]) == 33
    assert values[1][0] == 1 and type(values[1][0]) is int
    assert values[1][1] == ""  # textual dimensions remain text


def test_sheet_row_builder_normalizes_digit_string_ids_and_rejects_bad_ids():
    row = {name: None for name in COLUMNS}
    row.update({"project_id": "7890", "project_name": "5920", "client": "123"})
    output = sheet_rows([row])[1]
    assert output[0] == 7890 and type(output[0]) is int
    assert output[1] == "5920" and output[20] == "123"
    row["project_id"] = "78x"
    with pytest.raises(ValueError, match="project_id"):
        sheet_rows([row])


def test_project_type_schema_appends_only_two_columns_after_original_contract():
    assert COLUMNS == PROJECT_TYPE_COLUMNS
    assert PROJECT_TYPE_COLUMNS[: len(BASE_COLUMNS)] == BASE_COLUMNS
    assert PROJECT_TYPE_COLUMNS[-2:] == ["project_type_code", "project_type_name"]
    assert len(PROJECT_TYPE_COLUMNS) == 33
    row = {name: None for name in PROJECT_TYPE_COLUMNS}
    row.update({"project_id": "7975", "project_name": "4 Лапы_Москва_Q3_0926", "project_type_code": "0001", "project_type_name": "Обычный"})
    values = sheet_rows([row], columns=PROJECT_TYPE_COLUMNS)
    assert values[0] == PROJECT_TYPE_COLUMNS
    assert values[1][31:] == ["0001", "Обычный"]


def test_selected_refresh_preserves_previously_unmapped_fields_and_recalculates_marker():
    prior = {name: "" for name in BASE_COLUMNS}
    prior.update({"project_id": "1", "project_name": "Example_0926", "period": "", "unassigned": 4, "has_period_marker": True, "elapsed_pct": 0.25, "lag": 2})
    previous = [BASE_COLUMNS, [prior[name] for name in BASE_COLUMNS]]
    incoming = {"project_id": "1", "project_name": "Example_0926", "_acquisition_state": "ACQUIRED", "plan": 5}
    merged = merge_previous([incoming], previous, {"1"}, "2026-09-22T00:00:00+00:00")[0]
    assert merged["unassigned"] == 4
    assert merged["elapsed_pct"] == 0.25
    assert merged["lag"] == 2
    assert merged["period"] == ""
    assert merged["has_period_marker"] is True


def test_selected_refresh_preserves_project_type_assignment_without_state_reenrichment():
    prior = {name: "" for name in COLUMNS}
    prior.update({
        "project_id": "7975",
        "project_name": "4 Лапы_Москва_Q3_0926",
        "project_type_code": "0005",
        "project_type_name": "Тестовый подтвержденный тип",
    })
    previous = [COLUMNS, [prior[name] for name in COLUMNS]]
    incoming = {
        "project_id": "7975",
        "project_name": "4 Лапы_Москва_Q3_0926",
        "_acquisition_state": "ACQUIRED",
    }
    merged = merge_previous([incoming], previous, {"7975"}, "2026-09-22T00:00:00+00:00")[0]
    assert merged["project_type_code"] == "0005"
    assert merged["project_type_name"] == "Тестовый подтвержденный тип"


def test_merge_previous_carries_last_good_fields_on_semantic_failure():
    prior = {name: "last-good" for name in BASE_COLUMNS}
    prior.update({"project_id": "7", "project_name": "Project_0926", "plan": 22, "plan_value": 22, "client": "Client"})
    previous = [BASE_COLUMNS, [prior[name] for name in BASE_COLUMNS]]
    incoming = {"project_id": "7", "project_name": None, "_acquisition_state": "SEMANTIC_FAILURE", "plan": None}
    merged = merge_previous([incoming], previous, {"7"}, "2026-09-22T00:00:00+00:00")[0]
    assert merged["_acquisition_state"] == "SEMANTIC_FAILURE"
    assert merged["project_name"] == prior["project_name"]
    assert merged["plan"] == prior["plan"]
    assert merged["client"] == prior["client"]


def test_materializer_does_not_redefine_unmapped_has_period_marker():
    row = materialize([{"project_id": "1", "project_name": {"value": "Example_0926"}}], [], "2026-09-22T00:00:00+00:00")[0]
    assert row["has_period_marker"] is None


def test_acquisition_checkpoint_roundtrip_and_secret_scan(tmp_path):
    path = tmp_path / "checkpoint.json"
    payload = {
        "version": 1, "universe": [{"project_id": "1"}], "selected": [{"project_id": "1"}],
        "projects": [{"project_id": "1", "acquisition_state": "ACQUIRED"}], "visits": [],
        "previous_raw": [], "previous_state_rows": [], "state": {},
    }
    result = serialize_acquisition_checkpoint(str(path), payload)
    assert result["bytes"] > 0
    assert load_acquisition_checkpoint(str(path))["projects"][0]["project_id"] == "1"
    assert path.stat().st_mode & 0o077 == 0


def test_acquisition_checkpoint_rejects_secret_like_fields(tmp_path):
    with pytest.raises(ValueError, match="secret-like"):
        serialize_acquisition_checkpoint(str(tmp_path / "bad.json"), {"version": 1, "token": "must-not-persist"})
