import json
import os
import stat

import pytest

from src.payment_refresh import replace_by_project
from src.project_failure_isolation import (
    ProjectFailure,
    append_project_error_journal,
    partition_project_results,
    project_failure_record,
    reconcile_failed_project_bundles,
)
from src.project_types import acquire_current_types, validate_project_type_catalog
from src.refresh import COLUMNS, materialize, merge_previous, sheet_rows
from src.workflow_publication import primary_columns, third_tab_rows


def test_partition_requires_exact_result_coverage_and_isolates_only_explicit_states():
    assert partition_project_results(
        ["10", "20"],
        [
            {"project_id": "10", "acquisition_state": "ACQUIRED"},
            {"project_id": "20", "acquisition_state": "FAILED"},
        ],
    ) == ({"10"}, {"20"})
    for results in (
        [{"project_id": "10", "acquisition_state": "ACQUIRED"}],
        [
            {"project_id": "10", "acquisition_state": "ACQUIRED"},
            {"project_id": "10", "acquisition_state": "FAILED"},
        ],
        [
            {"project_id": "10", "acquisition_state": "ACQUIRED"},
            {"project_id": "20", "acquisition_state": "UNKNOWN"},
        ],
    ):
        with pytest.raises(RuntimeError):
            partition_project_results(["10", "20"], results)


def test_failed_project_is_excluded_as_a_whole_bundle_and_good_peer_remains():
    selected = [{"project_id": "10"}, {"project_id": "20"}]
    projects = [{"project_id": "10"}, {"project_id": "20"}]
    visits = [{"project_id": "10", "visit_id": "a"}, {"project_id": "20", "visit_id": "b"}]
    old_types = {"10": ("0002", "Classic"), "20": ("0003", "Old")}
    new_types = {"10": ("0009", "Home Test"), "20": ("0004", "Photo")}
    failure = ProjectFailure("20", "/proj/{project_id}/edit", "SEMANTIC_ERROR", "PROJECT_SEMANTIC_VALIDATION_FAILED")

    result = reconcile_failed_project_bundles(
        selected, projects, visits, new_types, old_types, [failure]
    )

    filtered_selected, filtered_projects, filtered_visits, types, failed_ids = result
    assert [row["project_id"] for row in filtered_selected] == ["10"]
    assert [row["project_id"] for row in filtered_projects] == ["10"]
    assert [row["project_id"] for row in filtered_visits] == ["10"]
    assert types == {"10": ("0009", "Home Test"), "20": ("0003", "Old")}
    assert failed_ids == {"20"}


def test_failed_new_project_does_not_create_fabricated_last_good_data():
    failure = ProjectFailure("30", "/api/project/{project_id}", "JSON_ERROR", "PROJECT_DETAIL_JSON_INVALID")
    selected, projects, visits, types, failed = reconcile_failed_project_bundles(
        [{"project_id": "30"}], [{"project_id": "30"}],
        [{"project_id": "30", "visit_id": "x"}],
        {"30": ("0009", "Home Test")}, {}, [failure],
    )
    assert selected == projects == visits == []
    assert "30" not in types
    assert failed == {"30"}


def test_payment_candidate_replaces_only_successful_project_rows():
    old_visit = ["project_id", "value"]
    old_project = ["project_id", "value"]
    previous = {
        "visits": [old_visit, ["10", "old-10"], ["20", "last-good-20"]],
        "projects": [old_project, ["10", "old-10"], ["20", "last-good-20"]],
    }
    incoming = {
        "visits": [old_visit, ["10", "new-10"]],
        "projects": [old_project, ["10", "new-10"]],
    }
    candidate = replace_by_project(previous, incoming, ["10"])
    assert candidate["visits"] == [old_visit, [20, "last-good-20"], [10, "new-10"]]
    assert candidate["projects"] == [old_project, [20, "last-good-20"], [10, "new-10"]]


def test_five_sheet_candidates_keep_failed_bundle_and_refresh_successful_peer():
    workflow_columns = primary_columns(COLUMNS)
    old_current = sheet_rows(materialize(
        [{"project_id": "20", "project_name": {"value": "LastGood_1026"},
          "planned_visit_count": {"value": 2}, "workflow_memberships": []}], [], "2026-10-18T00:00:00Z"
    ), columns=workflow_columns)
    new_current_rows = materialize(
        [{"project_id": "10", "project_name": {"value": "Fresh_1026"},
          "planned_visit_count": {"value": 3}, "workflow_memberships": []}], [], "2026-10-19T00:00:00Z"
    )
    merged = merge_previous(new_current_rows, old_current, {"10"}, "2026-10-19T00:00:00Z", workflow_columns)
    current_candidate = sheet_rows(merged, columns=workflow_columns)
    status_candidate = third_tab_rows(merged)

    types = {"10": ("0009", "Home Test"), "20": ("0004", "Photo")}
    old_types = {"20": ("0002", "Classic")}
    failure = ProjectFailure("20", "/proj/{project_id}/edit", "SEMANTIC_ERROR", "PROJECT_SEMANTIC_VALIDATION_FAILED")
    _, _, _, reconciled_types, _ = reconcile_failed_project_bundles(
        [{"project_id": "10"}, {"project_id": "20"}],
        [{"project_id": "10"}], [], types, old_types, [failure],
    )

    current_by_id = {str(row[0]): row for row in current_candidate[1:]}
    status_by_id = {str(row[0]): row for row in status_candidate[1:]}
    assert set(current_by_id) == {"10", "20"}
    assert current_by_id["20"][COLUMNS.index("project_name")] == "LastGood_1026"
    assert set(status_by_id) == {"10", "20"}
    assert status_by_id["20"][1] == "LastGood_1026"
    assert reconciled_types["20"] == ("0002", "Classic")

    old_tabs = {
        "visit": [["project_id", "value"], ["20", "last-good-visit"]],
        "project": [["project_id", "value"], ["20", "last-good-project"]],
    }
    new_tabs = {key: [rows[0]] for key, rows in old_tabs.items()}
    payment_candidate = replace_by_project(old_tabs, new_tabs, [])
    assert all(candidate[1][0] == 20 and candidate[1][1] == old[1][1]
               for candidate, old in ((payment_candidate["visit"], old_tabs["visit"]),
                                      (payment_candidate["project"], old_tabs["project"])))


def test_scoped_payment_replace_is_idempotent():
    header = ["project_id", "value"]
    old = {"tab": [header, ["10", "old"], ["20", "keep"]]}
    incoming = {"tab": [header, ["10", "new"]]}
    once = replace_by_project(old, incoming, ["10"])
    twice = replace_by_project(once, incoming, ["10"])
    assert twice == once


def test_project_type_failure_recovers_automatically_on_next_acquisition():
    catalog = validate_project_type_catalog([("0002", "Classic"), ("0009", "Home Test")])
    universe = [{"project_id": "20", "project_name": "Fixture_1026"}]

    class Session:
        def __init__(self, response):
            self.response = response

        def request(self, *_args, **_kwargs):
            return self.response

    old = {"20": ("0002", "Classic")}
    failed_state, telemetry, _ = acquire_current_types(
        Session((200, "application/json", b"{bad")), universe, old, catalog, delay=0,
    )
    assert failed_state == old
    assert len(telemetry.project_failures) == 1

    recovered_state, recovered_telemetry, _ = acquire_current_types(
        Session((200, "application/json", b'{"type":"0009"}')),
        universe, failed_state, catalog, delay=0,
    )
    assert recovered_state == {"20": ("0009", "Home Test")}
    assert recovered_telemetry.project_failures == []


def test_error_journal_is_allowlisted_sanitized_and_private(tmp_path):
    path = tmp_path / "private" / "errors.jsonl"
    os.chmod(tmp_path, 0o700)
    record = project_failure_record(
        ProjectFailure("7968", "/api/project/{project_id}", "JSON_ERROR", "PROJECT_DETAIL_JSON_INVALID"),
        last_successful_refresh="2026-10-18T17:08:13+03:00",
        recovery_status="CANDIDATE_PRESERVES_LAST_GOOD",
    )
    assert append_project_error_journal([record], path) == 1
    payload = json.loads(path.read_text())
    assert payload["project_id"] == "7968"
    assert payload["last_successful_refresh"].endswith("+00:00")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not any(secret in path.read_text() for secret in ("cookie", "token", "response body"))


def test_project_failure_journal_rejects_arbitrary_exception_text(tmp_path):
    unsafe = {
        "timestamp": "2026-10-19T00:00:00+00:00",
        "project_id": "7968",
        "endpoint": "/api/project/{project_id}",
        "error_class": "JSON_ERROR",
        "sanitized_error": "private response body must not be logged",
        "last_successful_refresh": "UNKNOWN",
        "recovery_status": "CANDIDATE_OMITS_WITHOUT_LAST_GOOD",
    }
    with pytest.raises(ValueError):
        append_project_error_journal([unsafe], tmp_path / "private" / "errors.jsonl")
