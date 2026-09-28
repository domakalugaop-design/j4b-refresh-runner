import pytest

from src.acquisition import ACTION_STATE_CODES, Reader, acquire_project
from src.parsers import parse_action_table
from src.workflow_analytics import WORKFLOW_STATE_CODES, deduplicate_memberships, project_workflow_metrics


def response(body, status=200, content_type="text/html; charset=utf-8"):
    return status, content_type, body.encode("utf-8")


def action_row(visit, codes, project="42", action="100"):
    links = "".join(f'<a href="/action/{action}">{code}</a>' for code in codes)
    return f'<tr class=""><td><a href="/proj/{project}">P</a></td><td><a href="/visit/{visit}">V</a>{links}</td></tr>'


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)

    def request(self, path, method, data=None, accept=None):
        return next(self.responses)


def test_canonical_dictionary_has_exactly_fourteen_codes():
    assert set(WORKFLOW_STATE_CODES) == {0, 5, 10, 15, 20, 25, 30, 35, 37, 38, 39, 40, 45, 50}


def test_combined_action_request_contains_all_states_and_stays_one_post():
    name = "Project_Q3_0926"
    html = "<!doctype html><html><body>" + action_row("1", [15, 20]) + "</body></html>"
    session = Session([
        response(f"<!doctype html><html><body>{name}/visit/1</body></html>"),
        response(f"<!doctype html><html><body><input name='name' value='{name}'><input name='dt1' value='01.09.2026'><input name='dt2' value='30.09.2026'><input name='visits' value='1'><input name='cost' value='1'><select name='client'><option selected>Client</option></select><select name='user'><option selected>Manager</option></select><select name='user2[]'><option selected>Coordinator</option></select><select name='wave'><option selected>Wave</option></select><select name='scope'><option selected>Scope</option></select></body></html>"),
        response(html),
    ])
    reader = Reader(session, 3)
    acquire_project(reader, {"project_id": "42", "project_name": name}, 0)
    assert ACTION_STATE_CODES == tuple(str(code) for code in WORKFLOW_STATE_CODES)
    assert reader.post_count == 1


def test_parser_and_aggregation_preserve_valid_15_20_overlap():
    html = "<!doctype html><html><body>" + action_row("1", [15, 20]) + "</body></html>"
    rows = parse_action_table(html, "42")
    assert rows[0]["workflow_state_codes"] == ["15", "20"]
    memberships = [{"project_id": "42", "visit_id": "1", "workflow_state_code": int(code)} for code in rows[0]["workflow_state_codes"]]
    metrics = project_workflow_metrics("42", ["1"], memberships)
    assert metrics["workflow_state_15_visits"] == 1
    assert metrics["workflow_state_20_visits"] == 1
    assert metrics["assigned_visits"] == 1
    assert metrics["workflow_multi_match_visits"] == 1


def test_duplicate_membership_deduplicates_by_project_visit_state():
    memberships = [
        {"project_id": "42", "visit_id": "1", "workflow_state_code": 20},
        {"project_id": "42", "visit_id": "1", "workflow_state_code": 20},
    ]
    assert len(deduplicate_memberships(memberships)) == 1
    assert project_workflow_metrics("42", ["1"], memberships)["assigned_visits"] == 1


def test_finished_is_distinct_union_and_coverage_does_not_sum_states():
    memberships = [
        {"project_id": "42", "visit_id": "1", "workflow_state_code": 37},
        {"project_id": "42", "visit_id": "1", "workflow_state_code": 40},
        {"project_id": "42", "visit_id": "2", "workflow_state_code": 50},
        {"project_id": "42", "visit_id": "3", "workflow_state_code": 30},
    ]
    metrics = project_workflow_metrics("42", ["1", "2", "3", "4"], memberships)
    assert metrics["finished_visits"] == 2
    assert metrics["project_visit_count"] == 4
    assert metrics["workflow_covered_visits"] == 3
    assert metrics["workflow_unclassified_visits"] == 1
    assert metrics["workflow_multi_match_visits"] == 1


def test_state_39_is_valid_membership_and_unknown_state_fails_closed():
    assert project_workflow_metrics("42", ["1"], [{"project_id": "42", "visit_id": "1", "workflow_state_code": 39}])["workflow_state_39_visits"] == 1
    with pytest.raises(ValueError, match="unknown workflow state"):
        deduplicate_memberships([{ "project_id": "42", "visit_id": "1", "workflow_state_code": 99 }])

