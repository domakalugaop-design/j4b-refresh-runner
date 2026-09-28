from unittest.mock import patch

from src import production
from src.refresh import BASE_COLUMNS, PROJECT_TYPE_COLUMNS
from src.workflow_publication import PRIMARY_ANALYTICS_COLUMNS


def test_workflow_feature_gate_is_off_by_default_and_explicitly_on():
    with patch.dict("os.environ", {}, clear=True):
        assert production._workflow_enabled() is False
    with patch.dict("os.environ", {"ENABLE_WORKFLOW_ANALYTICS_PUBLICATION": "true"}, clear=True):
        assert production._workflow_enabled() is True


def test_workflow_schema_upgrade_is_additive_from_existing_33_columns():
    target = PROJECT_TYPE_COLUMNS + PRIMARY_ANALYTICS_COLUMNS
    previous = [PROJECT_TYPE_COLUMNS, ["1"] + [""] * (len(PROJECT_TYPE_COLUMNS) - 1)]
    upgraded = production._upgrade_projects_rows(previous, PROJECT_TYPE_COLUMNS, target)
    assert upgraded[0] == target
    assert len(upgraded[1]) == len(target)
    assert upgraded[1][: len(PROJECT_TYPE_COLUMNS)] == previous[1]
    assert upgraded[1][-21:] == [""] * 21


def test_legacy_schema_upgrade_remains_33_columns_when_gate_off():
    previous = [BASE_COLUMNS, ["1"] + [""] * (len(BASE_COLUMNS) - 1)]
    upgraded = production._upgrade_projects_rows(previous, BASE_COLUMNS, PROJECT_TYPE_COLUMNS)
    assert upgraded[0] == PROJECT_TYPE_COLUMNS
    assert len(upgraded[1]) == len(PROJECT_TYPE_COLUMNS)


def test_2026_scope_uses_persisted_interval_and_excludes_pre_and_future():
    header = PROJECT_TYPE_COLUMNS
    def row(pid, start, end):
        values = [""] * len(header)
        values[header.index("project_id")] = str(pid)
        values[header.index("date_from")] = start
        values[header.index("date_to")] = end
        return values
    baseline = [
        header,
        row(1, "2025-01-01", "2025-12-31"),
        row(2, "2026-01-01", "2026-12-31"),
        row(3, "2027-01-01", "2027-12-31"),
        row(4, "", ""),
    ]
    catalogue = [{"project_id": str(i), "project_name": f"P{i}"} for i in range(1, 5)]
    selected = production._select_reporting_year_scope(catalogue, baseline, 2026)
    assert [item["project_id"] for item in selected] == ["2"]


def test_2026_scope_is_closed_interval_and_handles_one_bound():
    header = PROJECT_TYPE_COLUMNS
    def row(pid, start, end):
        values = [""] * len(header)
        values[header.index("project_id")] = str(pid)
        values[header.index("date_from")] = start
        values[header.index("date_to")] = end
        return values
    baseline = [header, row(10, "2026-12-31", ""), row(11, "", "2026-01-01")]
    catalogue = [{"project_id": "10"}, {"project_id": "11"}]
    assert [item["project_id"] for item in production._select_reporting_year_scope(catalogue, baseline, 2026)] == ["10", "11"]


def test_2026_scope_accepts_google_sheets_date_serials():
    header = PROJECT_TYPE_COLUMNS
    def row(pid, start, end):
        values = [""] * len(header)
        values[header.index("project_id")] = str(pid)
        values[header.index("date_from")] = start
        values[header.index("date_to")] = end
        return values
    baseline = [header, row(20, 46023, 46388)]  # 2026-01-01 .. 2026-12-31
    assert [item["project_id"] for item in production._select_reporting_year_scope([{ "project_id": "20" }], baseline, 2026)] == ["20"]
