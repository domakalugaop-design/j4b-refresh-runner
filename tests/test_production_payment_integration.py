from decimal import Decimal
from urllib.parse import unquote
from unittest.mock import patch

import pytest

from src import production
from src.payment_materialization import PROJECT_PUBLICATION_COLUMNS, VISIT_PUBLICATION_COLUMNS


def _old_tabs():
    return {
        production.PAYMENT_VISIT_TAB: [list(VISIT_PUBLICATION_COLUMNS), ["1", "Old", None, None, "9", 1, Decimal("1"), Decimal("0"), 0, 1, "[]", "COMPLETE"]],
        production.PAYMENT_PROJECT_TAB: [list(PROJECT_PUBLICATION_COLUMNS), ["1", "Old", None, None, 1, 1, Decimal("1"), Decimal("0"), 0, 1, 0, 1, 0, 0, "COMPLETE"]],
    }


def test_payment_feature_gate_is_off_unless_explicitly_enabled(monkeypatch):
    monkeypatch.delenv("PAYMENT_REFRESH_ENABLED", raising=False)
    assert production._payment_refresh_enabled() is False
    monkeypatch.setenv("PAYMENT_REFRESH_ENABLED", "true")
    assert production._payment_refresh_enabled() is True


def test_read_payment_tab_uses_authenticated_values_reader_and_normalizes_types():
    values = [list(VISIT_PUBLICATION_COLUMNS), [123, "Project", None, None, 456, 1.0, 500.0, 250.0, 1.0, 0.0, "[]", "COMPLETE"]]
    with patch.object(production, "api_get", return_value={"values": values}) as read:
        result = production._read_payment_tab("token", "sheet", production.PAYMENT_VISIT_TAB)
    assert read.call_count == 1
    assert "'Выплаты по визитам'!A:Z" in unquote(read.call_args.args[0])
    assert result[1][0] == "123"
    assert result[1][4] == "456"
    assert result[1][5] == 1
    assert result[1][6] == Decimal("500.0")


def test_regular_payment_preparation_replaces_only_selected_project_rows():
    old = _old_tabs()
    selected = [{"project_id": "1", "project_name": "New_0926"}]
    projects = [{
        "project_id": "1",
        "acquisition_state": "ACQUIRED",
        "project_name": {"value": "New_0926"},
        "client": {"value": "Client"},
        "primary_manager": {"value": "Manager"},
        "workflow_memberships": [{"project_id": "1", "action_id": "42", "visit_id": "7", "workflow_state_code": 50}],
    }]
    metadata = {"sheets": [{"properties": {"title": production.PAYMENT_VISIT_TAB, "sheetId": 20}},
                            {"properties": {"title": production.PAYMENT_PROJECT_TAB, "sheetId": 21}}]}
    fake_payment = [{"my_id": "42", "visit_reward_raw": "10", "transport_expense_raw": None,
                     "expense_compensation_raw": None, "bonus_penalty_raw": None, "portal_paid_amount_raw": "5"}]
    with patch.object(production, "_read_payment_tab", side_effect=lambda _token, _sid, tab: old[tab]), \
         patch.object(production, "acquire_project_payment_assignments", return_value=(fake_payment, 200)):
        _previous, candidate, sheet_ids = production._prepare_regular_payment_publication(
            "token", "sheet", metadata, object(), selected, projects
        )
    assert sheet_ids == {production.PAYMENT_VISIT_TAB: 20, production.PAYMENT_PROJECT_TAB: 21}
    assert candidate[production.PAYMENT_VISIT_TAB][0] == list(VISIT_PUBLICATION_COLUMNS)
    assert candidate[production.PAYMENT_PROJECT_TAB][0] == list(PROJECT_PUBLICATION_COLUMNS)
    assert all(str(row[0]) == "1" for rows in candidate.values() for row in rows[1:])
    assert candidate[production.PAYMENT_PROJECT_TAB][1][0] == "1"
    assert candidate[production.PAYMENT_PROJECT_TAB][1][7] == Decimal("5")


def test_regular_payment_incomplete_operational_acquisition_blocks_before_export_or_sheet_read():
    selected = [{"project_id": "1", "project_name": "New_0926"}]
    projects = [{"project_id": "1", "acquisition_state": "SEMANTIC_FAILURE"}]
    metadata = {"sheets": [
        {"properties": {"title": production.PAYMENT_VISIT_TAB, "sheetId": 20}},
        {"properties": {"title": production.PAYMENT_PROJECT_TAB, "sheetId": 21}},
    ]}
    with patch.object(production, "_read_payment_tab", side_effect=AssertionError("must not read old tabs")), \
         patch.object(production, "acquire_project_payment_assignments", side_effect=AssertionError("must not export")):
        with pytest.raises(RuntimeError, match="incomplete operational"):
            production._prepare_regular_payment_publication(
                "token", "sheet", metadata, object(), selected, projects
            )
