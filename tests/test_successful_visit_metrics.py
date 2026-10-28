from src.acquisition import Reader, acquire_project
from src.refresh import COLUMNS, materialize, merge_previous
from src.workflow_publication import validate_materialized_rows
from src.workflow_analytics import successful_visit_metrics


def visit(visit_id, action_id, state, project_id="42", **extra):
    codes = list(state) if isinstance(state, (list, tuple, set)) else ([] if state is None else [state])
    scalar = codes[0] if len(codes) == 1 else None
    row = {
        "project_id": project_id,
        "visit_id": str(visit_id),
        "action_id": {"value": str(action_id)},
        "current_state_code": {"value": str(scalar) if scalar is not None else None},
        "current_state_codes": {"value": [str(code) for code in codes]},
    }
    row.update(extra)
    return row


class PortalSession:
    base_url = "https://lk.j4b.ru"

    def __init__(self, responses):
        self.responses = iter(responses)
        self.last_effective_url = None
        self.last_retry_after = None

    def request(self, path, method, data=None, accept=None):
        self.last_effective_url = self.base_url + path
        return next(self.responses)


def test_current_states_37_40_and_50_are_successful_but_45_is_not():
    result = successful_visit_metrics("42", [
        visit(1, 101, 40),
        visit(2, 102, 50),
        visit(3, 103, 37),
        visit(4, 104, 45),
    ], 2)
    assert result["total_project_visits"] == 4
    assert result["successful_visits_current"] == 3
    assert result["execution_pct_successful"] == 1.5
    assert result["current_state_unresolved_visits"] == 0


def test_transition_40_to_45_uses_latest_action_not_historical_approval():
    result = successful_visit_metrics("42", [
        visit(1, 100, 40),
        visit(1, 200, 45),
        visit(2, 201, 40),
    ], 2)
    assert result["total_project_visits"] == 2
    assert result["successful_visits_current"] == 1
    assert result["execution_pct_successful"] == 0.5


def test_multiple_predicates_for_same_action_are_unioned_without_false_unresolved():
    result = successful_visit_metrics("42", [
        visit(1, 200, 40),
        visit(1, 200, 40),  # duplicate current action row
        visit(2, 201, [40, 45]),  # same action exposes overlapping predicates
    ], 2)
    assert result["total_project_visits"] == 2
    assert result["successful_visits_current"] == 2
    assert result["current_state_unresolved_visits"] == 0


def test_unknown_or_missing_current_state_is_not_successful():
    result = successful_visit_metrics("42", [
        visit(1, 100, 99),
        visit(2, 101, None),
    ], 2)
    assert result["total_project_visits"] == 2
    assert result["successful_visits_current"] == 0
    assert result["current_state_unresolved_visits"] == 2
    assert result["execution_pct_successful"] is None


def test_empty_membership_list_falls_back_to_legacy_scalar_when_present():
    row = visit(1, 100, 40)
    row["current_state_codes"] = {"value": []}
    result = successful_visit_metrics("42", [row], 1)
    assert result["successful_visits_current"] == 1
    assert result["current_state_unresolved_visits"] == 0


def test_later_assigned_action_supersedes_an_older_explicit_free_marker():
    result = successful_visit_metrics("42", [
        visit(1, 0, None, assignment_state={"value": "UNASSIGNED_FREE"}),
        visit(1, 100, 40, assignment_state={"value": "ASSIGNED"}),
    ], 1)
    assert result["total_project_visits"] == 1
    assert result["successful_visits_current"] == 1


def test_older_assignment_does_not_supersede_newer_free_marker():
    result = successful_visit_metrics("42", [
        visit(1, 100, 40, assignment_state={"value": "ASSIGNED"}),
        visit(1, 200, [40, 50], assignment_state={"value": "UNASSIGNED_FREE"}),
    ], 1)
    assert result["successful_visits_current"] == 0


def test_unassigned_free_marker_excludes_success_membership_without_assignment_evidence():
    result = successful_visit_metrics("sample", [
        visit("free-a", 50001, [45, 50], project_id="sample", assignment_state={"value": "UNASSIGNED_FREE"}),
        visit("free-b", 50002, [45, 50], project_id="sample", assignment_state={"value": "UNASSIGNED_FREE"}),
        visit("assigned", 50003, [40, 45], project_id="sample", assignment_state={"value": "ASSIGNED"}),
    ], 2)
    assert result["successful_visits_current"] == 1
    assert result["current_state_unresolved_visits"] == 0


def test_sanitized_166_visit_status_distribution_reconciles_to_plan():
    rows = [visit(f"resolved-{i}", 1000 + i, [40], project_id="sample") for i in range(106)]
    # Anonymized aggregate shape of the 58 multi-predicate latest actions:
    # 56 include 40, 42 include 50, their unique union is 58.
    rows.extend(visit(f"multi-40-{i}", 2000 + i, [40], project_id="sample") for i in range(16))
    rows.extend(visit(f"multi-40-50-{i}", 2100 + i, [40, 45, 50] if i < 3 else [40, 50], project_id="sample") for i in range(38))
    rows.extend([
        visit("free-success-a", 50004, [40, 45, 50], project_id="sample", assignment_state={"value": "UNASSIGNED_FREE"}),
        visit("free-success-b", 50005, [40, 45, 50], project_id="sample", assignment_state={"value": "UNASSIGNED_FREE"}),
    ])
    rows.extend(visit(f"multi-50-{i}", 2200 + i, [50], project_id="sample") for i in range(2))
    rows.extend([
        visit("other-1", 50006, [45], project_id="sample", assignment_state={"value": "UNASSIGNED_FREE"}),
        visit("other-2", 50007, [45], project_id="sample", assignment_state={"value": "UNASSIGNED_FREE"}),
    ])
    result = successful_visit_metrics("sample", rows, 162)
    assert result["total_project_visits"] == 166
    assert result["successful_visits_current"] == 162
    assert result["current_state_unresolved_visits"] == 0
    assert result["execution_pct_successful"] == 1.0


def test_current_explicit_free_visit_is_not_successful():
    result = successful_visit_metrics("42", [
        visit(1, 0, None, assignment_state={"value": "UNASSIGNED_FREE"}),
    ], 1)
    assert result["total_project_visits"] == 1
    assert result["successful_visits_current"] == 0
    assert result["execution_pct_successful"] == 0.0


def test_missing_or_zero_plan_has_no_execution_ratio():
    rows = [visit(1, 100, 40)]
    assert successful_visit_metrics("42", rows, None)["execution_pct_successful"] is None
    assert successful_visit_metrics("42", rows, 0)["execution_pct_successful"] is None


def test_successful_execution_is_not_capped_at_one_hundred_percent():
    result = successful_visit_metrics("42", [visit(1, 100, 40), visit(2, 101, 50)], 1)
    assert result["execution_pct_successful"] == 2.0


def test_materializer_keeps_legacy_membership_union_separate_from_current_success():
    project = {
        "project_id": "42",
        "planned_visit_count": {"value": 2},
        "acquisition_state": "ACQUIRED",
        "workflow_memberships": [
            {"project_id": "42", "visit_id": "1", "workflow_state_code": 40},
            {"project_id": "42", "visit_id": "1", "workflow_state_code": 45},
            {"project_id": "42", "visit_id": "2", "workflow_state_code": 40},
            {"project_id": "42", "visit_id": "3", "workflow_state_code": 37},
            {"project_id": "42", "visit_id": "4", "workflow_state_code": 50},
        ],
    }
    visits = [
        visit(1, 100, 40),
        visit(1, 200, 45),  # current state supersedes historical 40
        visit(2, 201, 40),
        visit(3, 202, 37),  # owner-qualified successful state
        visit(4, 203, 50),  # paid is success
    ]
    row = materialize([project], visits, "2026-10-27T00:00:00+00:00")[0]
    assert row["total_project_visits"] == row["project_visit_count"] == 4
    assert row["successful_visits_current"] == 3
    assert row["completed"] == 0  # legacy raw-status metric remains independent
    assert row["historically_approved_visits"] == 3  # states 40/50; state 37 excluded
    assert row["finished_visits"] == 4  # legacy union still includes state 37
    assert row["execution_pct_successful"] == 1.5
    assert row["execution_pct"] == 0.0


def test_portal_acquisition_to_materializer_does_not_count_a_visit_after_40_to_45():
    project_name = "SyntheticProject_Q4_1026"
    project_html = (
        f"<!doctype html><html><body>{project_name}"
        "<a href='/visit/1'>Visit 1</a><a href='/visit/2'>Visit 2</a></body></html>"
    )
    edit_html = f"""<!doctype html><html><body>
      <form action='/proj/42/edit' method='post'>
      <input name='name' value='{project_name}'>
      <input name='dt1' value='05.10.2026'><input name='dt2' value='23.10.2026'>
      <input name='visits' value='2'><input name='cost' value='100'>
      <select name='client'><option selected value='1'>Client</option></select>
      <select name='user'><option selected value='2'>Manager</option></select>
      <select name='user2[]'><option selected value='3'>Coordinator</option></select>
      <select name='wave'><option selected value='4'>Wave</option></select>
      <select name='scope'><option selected value='5'>Scope</option></select>
      </form></body></html>"""
    action_html = """<!doctype html><html><body>
      <tr class='table-success'><td><a href='/proj/42'>P</a><a href='/visit/1'>V1</a>
      <a href='/action/100'>40</a><a href='/action/100'>Анкета утверждена</a></td></tr>
      <tr class='table-success'><td><a href='/proj/42'>P</a><a href='/visit/1'>V1</a>
      <a href='/action/200'>45</a><a href='/action/200'>В оплате отказано</a></td></tr>
      <tr class='table-success'><td><a href='/proj/42'>P</a><a href='/visit/2'>V2</a>
      <a href='/action/300'>40</a><a href='/action/300'>Анкета утверждена</a></td></tr>
      </body></html>"""
    session = PortalSession([
        (200, "text/html", project_html.encode()),
        (200, "text/html", edit_html.encode()),
        (200, "text/html", action_html.encode()),
    ])
    project, visits = acquire_project(
        Reader(session, 3), {"project_id": "42", "project_name": project_name}, 0,
        track_progress=False,
    )
    row = materialize([project], visits, "2026-10-27T00:00:00+00:00")[0]
    current_by_visit = {item["visit_id"]: item["current_state_code"]["value"] for item in visits}
    assert current_by_visit == {"1": 45, "2": 40}
    assert row["total_project_visits"] == 2
    assert row["successful_visits_current"] == 1
    assert row["completed"] == 1  # legacy source-row count remains separate
    assert row["execution_pct"] == 0.5
    assert row["historically_approved_visits"] == 2
    assert row["execution_pct_successful"] == 0.5


def test_materializer_uses_unique_visit_count_and_preserves_100_plus_completion():
    project = {"project_id": "42", "planned_visit_count": {"value": 1}, "acquisition_state": "ACQUIRED"}
    visits = [visit(1, 100, 40), visit(1, 100, 40), visit(2, 101, 50)]
    row = materialize([project], visits, "2026-10-27T00:00:00+00:00")[0]
    assert row["created"] == 3  # preserve legacy raw-row count
    assert row["total_project_visits"] == 2  # new metric counts unique Visit IDs
    assert row["successful_visits_current"] == 2
    assert row["execution_pct_successful"] == 2.0


def test_project_health_adds_success_gap_without_redefining_legacy_risk_status():
    project = {"project_id": "42", "planned_visit_count": {"value": 10}, "acquisition_state": "ACQUIRED"}
    visits = [visit(i, 100 + i, 40) for i in range(1, 5)]
    materialized_row = materialize([project], visits, "2026-10-27T00:00:00+00:00")[0]
    previous_row = {column: "" for column in COLUMNS}
    previous_row.update({"project_id": "42", "elapsed_pct": 0.50})
    merged = merge_previous([materialized_row], [COLUMNS, [previous_row[column] for column in COLUMNS]],
                            {"42"}, "2026-10-27T00:00:00+00:00", columns=COLUMNS)[0]
    assert merged["execution_pct_successful"] == 0.4
    assert merged["gap_pp"] == -10.0
    assert merged["risk_status"] == "NOT_QUALIFIED"
    assert validate_materialized_rows([merged])["unique_project_ids"] == 1


def test_success_gap_never_changes_legacy_risk_status_and_missing_elapsed_stays_null():
    from src.refresh import _recalculate_project_health

    def row(elapsed):
        return {
            "plan": 10,
            "plan_status": "VALID_PLAN",
            "execution_pct_successful": 0.4,
            "elapsed_pct": elapsed,
            "risk_status": "NOT_QUALIFIED",
            "gap_pp": None,
        }

    near = _recalculate_project_health(row(0.499))
    assert near["risk_status"] == "NOT_QUALIFIED"
    assert near["gap_pp"] == -9.9
    boundary = _recalculate_project_health(row(0.5))
    assert boundary["risk_status"] == "NOT_QUALIFIED"
    assert boundary["gap_pp"] == -10.0
    lower = _recalculate_project_health(row(0.6))
    assert lower["risk_status"] == "NOT_QUALIFIED"
    assert lower["gap_pp"] == -20.0
    no_elapsed = _recalculate_project_health(row(None))
    assert no_elapsed["risk_status"] == "NOT_QUALIFIED"
    assert no_elapsed["gap_pp"] is None
