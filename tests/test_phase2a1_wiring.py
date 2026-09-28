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

