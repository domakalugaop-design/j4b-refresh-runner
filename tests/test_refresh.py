from datetime import date

from src.refresh import BASE_COLUMNS, COLUMNS, PROJECT_TYPE_COLUMNS, materialize, merge_previous, select_scope, sheet_rows, sheets_serial, summary


def test_selector_current_previous_and_new():
    previous = [COLUMNS, ["1", "Legacy 0826"] + [""] * 29]
    catalogue = [
        {"project_id": "1", "project_name": "Legacy 0826"},
        {"project_id": "2", "project_name": "Current 0926"},
        {"project_id": "3", "project_name": "Future 0127"},
    ]
    selected = select_scope(catalogue, previous, today=date(2026, 9, 4))
    assert [row["project_id"] for row in selected] == ["1", "2", "3"]


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


def test_sheet_rows_emit_full_width():
    row = {name: None for name in COLUMNS}
    row["project_id"] = "1"
    row["last_refreshed"] = "2026-09-04T10:00:00+00:00"
    values = sheet_rows([row])
    assert len(values[0]) == 33
    assert len(values[1]) == 33


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
