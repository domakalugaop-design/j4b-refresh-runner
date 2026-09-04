from datetime import date

from src.refresh import COLUMNS, materialize, select_scope, sheet_rows, sheets_serial, summary


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


def test_schema_is_31_columns_and_unique_summary():
    assert len(COLUMNS) == 31
    rows = [COLUMNS, ["1"] + [""] * 30, ["2"] + [""] * 30]
    assert summary(rows) == {"rows": 2, "unique": 2, "duplicates": 0}


def test_sheet_rows_emit_full_width():
    row = {name: None for name in COLUMNS}
    row["project_id"] = "1"
    row["last_refreshed"] = "2026-09-04T10:00:00+00:00"
    values = sheet_rows([row])
    assert len(values[0]) == 31
    assert len(values[1]) == 31
