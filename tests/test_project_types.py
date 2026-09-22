from unittest.mock import patch

import pytest

from src.project_types import (
    PROJECT_TYPE_DICTIONARY,
    STATE_COLUMNS,
    apply_canonical_project_names,
    acquire_pending_types,
    classify_raw_type,
    is_project_type_applicable,
    materialize_project_types,
    merge_assignment,
    normalized_period_marker,
    project_type_applicable_ids,
    serialize_state,
    validate_state_rows,
)
from src.refresh import publish_project_type_refresh


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Example_Q3_0826", (2026, 8)),
        ("Example_Q3_0926", (2026, 9)),
        ("Example_0127", (2027, 1)),
        ("No marker", None),
        ("Invalid_1326", None),
    ],
)
def test_period_marker_is_validated_and_normalized(name, expected):
    assert normalized_period_marker(name) == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [("A_0826", False), ("A_0926", True), ("A_0127", True), ("A_1326", False)],
)
def test_project_type_cutoff_is_september_2026_or_later(name, expected):
    assert is_project_type_applicable(name) is expected


def test_dictionary_codes_remain_text_with_leading_zeroes():
    assert PROJECT_TYPE_DICTIONARY["0001"] == "Обычный"
    assert all(isinstance(code, str) and len(code) == 4 for code in PROJECT_TYPE_DICTIONARY)
    assert "0006" not in PROJECT_TYPE_DICTIONARY


@pytest.mark.parametrize(
    ("payload", "bucket", "code"),
    [
        ({"type": "0001"}, "VALID", "0001"),
        ({"type": 0}, "ZERO", None),
        ({"type": None}, "NULL", None),
        ({"type": ""}, "EMPTY", None),
        ({}, "MISSING", None),
        ({"type": "common"}, "UNKNOWN_CODE", None),
        ({"type": "0006"}, "UNKNOWN_CODE", None),
    ],
)
def test_only_dictionary_qualified_codes_are_assignments(payload, bucket, code):
    actual_bucket, actual_code, _name = classify_raw_type(payload)
    assert (actual_bucket, actual_code) == (bucket, code)


def test_null_zero_empty_missing_and_unknown_remain_unassigned():
    for payload in ({"type": 0}, {"type": None}, {"type": ""}, {}, {"type": "common"}):
        bucket, code, name = classify_raw_type(payload)
        assert bucket != "VALID"
        assert code is None and name is None


def test_valid_assignment_is_immutable_and_conflict_is_reported():
    state = {"9": ("0001", "Обычный")}
    updated, added, conflict = merge_assignment(state, "9", "0005", "Качественные исследования")
    assert updated == state
    assert added is False
    assert conflict is True


def test_duplicate_state_ids_and_invalid_codes_are_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        validate_state_rows([STATE_COLUMNS, ["9", "0001", "Обычный"], ["9", "0002", "Классика_ТП"]])
    with pytest.raises(ValueError, match="invalid persisted"):
        validate_state_rows([STATE_COLUMNS, ["9", "0", ""]])


def test_state_serialization_keeps_code_and_id_text():
    assert serialize_state({"9": ("0001", "Обычный")}) == [STATE_COLUMNS, ["9", "0001", "Обычный"]]


def test_materialized_join_blanks_pre_cutoff_and_unresolved_rows():
    rows = [{"project_id": "1", "project_name": "A_0926"}, {"project_id": "2", "project_name": "B_0826"}, {"project_id": "3", "project_name": "C_0926"}]
    state = {"1": ("0001", "Обычный")}
    materialize_project_types(rows, state, {"1", "3"})
    assert rows[0]["project_type_code"] == "0001"
    assert rows[0]["project_type_name"] == "Обычный"
    assert rows[1]["project_type_code"] is None
    assert rows[1]["project_type_name"] is None
    assert rows[2]["project_type_code"] is None
    assert rows[2]["project_type_name"] is None


def test_applicability_uses_one_normalized_universe_period_contract_and_canonical_name():
    universe = [
        {"project_id": "1", "project_name": "old_0826"},
        {"project_id": "2", "project_name": "current_0926"},
        {"project_id": "3", "project_name": "future_0127"},
    ]
    assert project_type_applicable_ids(universe) == {"2", "3"}
    rows = [{"project_id": "2", "project_name": "stale_0726"}]
    apply_canonical_project_names(rows, universe)
    assert rows[0]["project_name"] == "current_0926"


@pytest.mark.parametrize(
    ("project_id", "stale_name", "canonical_name"),
    [
        (
            "7598",
            "JP_Бриф 328 Оформление КАСКО и урегулирование убытков_Q3_0726",
            "JP_Бриф 328 Оформление КАСКО и урегулирование убытков_Q3_0926",
        ),
        (
            "8044",
            "JP_Лаб1 &quot;ИИ образование&quot;_Q3_9026",
            'JP_Лаб1 "ИИ образование"_Q3_0926',
        ),
    ],
)
def test_discovered_stale_names_are_replaced_from_universe_for_7598_and_8044(
    project_id, stale_name, canonical_name
):
    rows = [{"project_id": project_id, "project_name": stale_name}]
    universe = [{"project_id": project_id, "project_name": canonical_name}]
    apply_canonical_project_names(rows, universe)
    applicable = project_type_applicable_ids(universe)
    state = {project_id: ("0005", "Качественные исследования")}
    materialize_project_types(rows, state, applicable)
    assert rows[0]["project_name"] == canonical_name
    assert rows[0]["project_type_code"] == "0005"
    assert rows[0]["project_type_name"] == "Качественные исследования"


@pytest.mark.parametrize(
    ("project_id", "stale_name"),
    [
        ("7598", "JP_Бриф 328 Оформление КАСКО и урегулирование убытков_Q3_0726"),
        ("8044", "JP_Лаб1 &quot;ИИ образование&quot;_Q3_9026"),
    ],
)
def test_immutable_assignment_with_stale_period_surfaces_conflict_without_deletion(
    project_id, stale_name
):
    state = {project_id: ("0005", "Качественные исследования")}
    rows = [{"project_id": project_id, "project_name": stale_name}]
    with pytest.raises(ValueError, match=f"PERIOD_APPLICABILITY_CONFLICT.*{project_id}"):
        materialize_project_types(rows, state, set())
    assert state == {project_id: ("0005", "Качественные исследования")}
    assert "project_type_code" not in rows[0]


class FakeSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.paths = []

    def request(self, path, method, accept):
        self.paths.append((path, method, accept))
        return next(self.responses)


def test_pt2_persistence_skips_assigned_and_retries_pending_type_zero():
    universe = [
        {"project_id": "1", "project_name": "assigned_0926"},
        {"project_id": "2", "project_name": "pending_0926"},
        {"project_id": "3", "project_name": "historical_0826"},
    ]
    state = {"1": ("0001", "Обычный")}
    session = FakeSession([(200, "application/json", b'{"type":0}')])
    after_pt2, telemetry, applicable = acquire_pending_types(session, universe, state, delay=0)
    assert session.paths == [("/api/project/2", "GET", "application/json")]
    assert applicable == {"1", "2"}
    assert after_pt2 == state
    assert telemetry.already_assigned == 1
    assert telemetry.pending_before == 1
    assert telemetry.zero_results == 1
    assert telemetry.detail_get_reused == 0
    assert telemetry.detail_get_additional == 1


def test_pending_project_can_be_retried_and_assigned_on_next_refresh():
    universe = [{"project_id": "2", "project_name": "pending_0926"}]
    session = FakeSession([(200, "application/json", b'{"type":"0002"}')])
    state, telemetry, _ = acquire_pending_types(session, universe, {}, delay=0)
    assert state == {"2": ("0002", "Классика_ТП")}
    assert telemetry.valid_assignments_acquired == 1
    assert telemetry.pending_before == 1


def test_project_types_rollback_when_projects_current_publish_fails():
    previous_rows = [STATE_COLUMNS, ["1", "0001", "Обычный"]]
    with patch("src.refresh.publish_project_type_state") as write_state, patch(
        "src.refresh.publish", side_effect=RuntimeError("injected current publish failure")
    ), patch("src.refresh._write_project_type_state_rows") as restore_state, patch(
        "src.refresh.read_project_type_state_rows", return_value=previous_rows
    ):
        with pytest.raises(RuntimeError, match="injected current publish failure"):
            publish_project_type_refresh(
                "token", "workbook-1", [["project_id"]], [["project_id"]], ["project_id"],
                {"1": ("0001", "Обычный"), "2": ("0002", "Классика_ТП")}, previous_rows,
            )
    write_state.assert_called_once()
    restore_state.assert_called_once_with("token", "workbook-1", previous_rows)


def test_project_types_write_failure_does_not_publish_projects_current():
    previous_rows = [STATE_COLUMNS, ["1", "0001", "Обычный"]]
    with patch("src.refresh.publish_project_type_state", side_effect=RuntimeError("state write failed")), patch(
        "src.refresh.publish"
    ) as current_publish, patch("src.refresh._write_project_type_state_rows"), patch(
        "src.refresh.read_project_type_state_rows", return_value=previous_rows
    ):
        with pytest.raises(RuntimeError, match="state write failed"):
            publish_project_type_refresh(
                "token", "workbook-1", [["project_id"]], [["project_id"]], ["project_id"],
                {"1": ("0001", "Обычный")}, previous_rows,
            )
    current_publish.assert_not_called()
