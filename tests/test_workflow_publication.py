import json
import os
import stat
from pathlib import Path

import pytest

from src.refresh import COLUMNS
from src.workflow_publication import (
    PRIMARY_ANALYTICS_COLUMNS,
    THIRD_TAB_COLUMNS,
    THIRD_TAB_NAME,
    backup_snapshot,
    diagnose_workflow_readback,
    persist_private_workflow_candidate,
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


def test_missing_project_dimensions_are_written_as_explicit_blanks_for_exact_readback():
    rendered = third_tab_rows([materialized("1", project_name=None, client=None, primary_manager=None)])
    assert rendered[1][1:4] == ["", "", ""]
    assert validate_readback(rendered, rendered)["status"] == "PASS"
    mismatched = [list(rendered[0]), list(rendered[1])]
    mismatched[1][2] = None
    with pytest.raises(ValueError, match="readback mismatch"):
        validate_readback(mismatched, rendered)


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


def test_workflow_readback_diagnostics_are_strict_and_report_first_cell_without_values():
    expected = [["project_id", "client"], ["8110", "private client"]]
    actual = [["project_id", "client"], ["8110", "different private client"]]
    diagnostic = diagnose_workflow_readback(expected, actual)
    assert not diagnostic["matches"]
    assert diagnostic["expected_rows"] == diagnostic["actual_rows"] == 2
    assert diagnostic["expected_columns"] == diagnostic["actual_columns"] == 2
    assert diagnostic["first_mismatch"]["a1"] == "B2"
    assert diagnostic["first_mismatch"]["column_name"] == "client"
    assert diagnostic["first_mismatch"]["expected_type"] == "str"
    assert diagnostic["first_mismatch"]["actual_type"] == "str"
    assert diagnostic["first_mismatch"]["class"] == "TEXT_VALUE"
    encoded = __import__("json").dumps(diagnostic)
    assert "private client" not in encoded
    assert diagnostic["total_mismatch_cells"] == 1
    assert diagnostic["mismatch_rows"] == 1
    assert diagnostic["mismatch_by_column"] == {"B: client": 1}
    assert diagnostic["mismatch_by_class"] == {"TEXT_VALUE": 1}


def test_production_readback_failure_logs_safe_diagnostic(capsys):
    from src.production import _validate_workflow_readback

    with pytest.raises(ValueError, match="workflow publication readback mismatch"):
        _validate_workflow_readback([["client"], ["redacted actual"]], [["client"], ["redacted expected"]])
    output = capsys.readouterr().out
    assert output.startswith("WORKFLOW_READBACK_DIAGNOSTIC=")
    assert "redacted actual" not in output
    assert "redacted expected" not in output


def test_workflow_candidate_sha_is_deterministic_and_file_is_private(tmp_path, monkeypatch):
    first_dir = tmp_path / "candidate-one"
    second_dir = tmp_path / "candidate-two"
    first_dir.mkdir(mode=0o700)
    second_dir.mkdir(mode=0o700)
    directories = iter([str(first_dir), str(second_dir)])
    monkeypatch.setattr("src.workflow_publication.tempfile.mkdtemp", lambda **_kwargs: next(directories))
    candidate = [["project_id", "project_name"], ["1", "private business name"]]

    first = persist_private_workflow_candidate(candidate)
    second = persist_private_workflow_candidate(candidate)

    assert first["candidate_sha256"] == second["candidate_sha256"]
    assert first["schema_sha256"] == second["schema_sha256"]
    assert first["rows"] == 2
    assert first["columns"] == 2
    assert stat.S_IMODE(os.stat(first["path"]).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(Path(first["path"]).parent).st_mode) == 0o700
    assert json.loads(Path(first["path"]).read_text()) == candidate


@pytest.mark.parametrize(("expected", "actual", "kind"), [
    ([["id"], ["1"]], [["id"]], "ROW_COUNT"),
    ([["id", "value"], ["1", "x"]], [["id"], ["1"]], "COLUMN_COUNT"),
])
def test_workflow_dimension_mismatch_has_structured_diagnostics(expected, actual, kind):
    diagnostic = diagnose_workflow_readback(expected, actual)
    assert not diagnostic["matches"]
    assert diagnostic["mismatch_by_class"][kind] >= 1


def test_workflow_diagnostic_samples_fingerprint_text_without_pii(capsys):
    private_a = "Sensitive customer/project value A"
    private_b = "Sensitive customer/project value B"
    from src.production import _validate_workflow_readback

    with pytest.raises(ValueError, match="workflow publication readback mismatch"):
        _validate_workflow_readback([["client"], [private_b]], [["client"], [private_a]])
    output = capsys.readouterr().out
    assert "mismatch_samples" in output
    assert private_a not in output
    assert private_b not in output


def _rollback_fixture(expected, actual):
    from src.workflow_publication import backup_snapshot

    backup = backup_snapshot("sheet", {THIRD_TAB_NAME: expected})

    def api(url, _token, body=None, method=None):
        if "fields=sheets.properties(title,sheetId)" in url:
            return {"sheets": [{"properties": {"title": THIRD_TAB_NAME, "sheetId": 123}}]}
        if ":clear" in url or ":batchUpdate" in url:
            return {}
        if "/values/" in url:
            return {"values": actual}
        raise AssertionError("unexpected rollback test request")

    return backup, api


def test_workflow_rollback_success_is_explicit(monkeypatch, capsys):
    from src import production

    expected = [["project_id", "client"], ["1", "private name"]]
    backup, fake_api = _rollback_fixture(expected, expected)
    monkeypatch.setattr(production, "api", fake_api)
    production._rollback_workflow_tab("token-unused", "sheet", backup)
    output = capsys.readouterr().out
    assert "WORKFLOW_ROLLBACK_REQUIRED=YES" in output
    assert "WORKFLOW_ROLLBACK_ATTEMPTED=YES" in output
    assert "WORKFLOW_ROLLBACK_READBACK=PASS" in output
    assert "WORKFLOW_ROLLBACK_RESULT=PASS" in output


def test_workflow_rollback_failure_is_explicit_and_unambiguous(monkeypatch, capsys):
    from src import production

    expected = [["project_id", "client"], ["1", "private name"]]
    actual = [["project_id", "client"], ["1", "different private name"]]
    backup, fake_api = _rollback_fixture(expected, actual)
    monkeypatch.setattr(production, "api", fake_api)
    with pytest.raises(RuntimeError, match="WORKFLOW_ROLLBACK_INCOMPLETE"):
        production._rollback_workflow_tab("token-unused", "sheet", backup)
    output = capsys.readouterr().out
    assert "WORKFLOW_ROLLBACK_READBACK=FAIL" in output
    assert "WORKFLOW_ROLLBACK_RESULT=FAIL" in output
    assert "private name" not in output


@pytest.mark.parametrize(("expected_value", "actual_value", "kind"), [
    (None, "", "NULL_VS_EMPTY"),
    ("3", 3, "STRING_VS_NUMBER"),
    (3, 4, "NUMERIC_VALUE"),
    ("left", "right", "TEXT_VALUE"),
    (True, False, "BOOLEAN_VALUE"),
])
def test_workflow_readback_diagnostic_mismatch_classes(expected_value, actual_value, kind):
    diagnostic = diagnose_workflow_readback([["value"], [expected_value]], [["value"], [actual_value]])
    assert diagnostic["mismatch_by_class"] == {kind: 1}
    assert diagnostic["first_mismatch"]["class"] == kind


def test_workflow_readback_diagnostic_counts_dimensions_trailing_blanks_and_row_shift():
    trailing = diagnose_workflow_readback([["id", "note"], ["1", ""]], [["id", "note"], ["1"]])
    assert not trailing["matches"]
    assert trailing["first_mismatch"]["a1"] == "B2"
    assert trailing["mismatch_by_class"] == {"MISSING_TRAILING_EMPTY": 1}

    shifted = diagnose_workflow_readback([["id"], ["a"], ["b"]], [["id"], ["b"], ["a"]])
    assert shifted["mismatch_by_class"]["ROW_SHIFT"] == 2

    dimensions = diagnose_workflow_readback([["id", "x"], ["a", "b"], ["c", "d"]], [["id"], ["a"]])
    assert dimensions["mismatch_by_class"]["ROW_COUNT"] == 1
    assert dimensions["mismatch_by_class"]["COLUMN_COUNT"] == 1


def test_shrink_plan_clears_obsolete_tail_rows():
    old = publication_plan([materialized(str(i)) for i in range(1, 11)], {THIRD_TAB_NAME})
    new = publication_plan([materialized(str(i)) for i in range(1, 7)], {THIRD_TAB_NAME}, previous_third_rows=old["row_count"] + 1)
    clear = new["clear_range"]
    assert clear["start_row"] == 8
    assert clear["end_row"] == 11


def test_publication_plan_is_pure_and_does_not_reacquire_portal():
    assert publication_plan([materialized("1")], set())["row_count"] == 1
