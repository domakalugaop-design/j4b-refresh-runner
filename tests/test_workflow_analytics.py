import pytest

from src.acquisition import ACTION_STATE_CODES, Reader, action_index, action_memberships, acquire_project, current_state_index
from src.parsers import parse_action_table
from src.workflow_analytics import WORKFLOW_STATE_CODES, deduplicate_memberships, project_workflow_metrics


def response(body, status=200, content_type="text/html; charset=utf-8"):
    return status, content_type, body.encode("utf-8")


def action_row(visit, codes, project="42", action="100"):
    links = "".join(f'<a href="/action/{action}">{code}</a>' for code in codes)
    return f'<tr class=""><td><a href="/proj/{project}">P</a></td><td><a href="/visit/{visit}">V</a>{links}</td></tr>'


def labeled_action_row(visit, code, label, project="42", action="100"):
    return (
        f'<tr class="table-success"><td><a href="/proj/{project}">P</a></td>'
        f'<td><a href="/visit/{visit}">V</a>'
        f'<a href="/action/{action}">{code}</a>'
        f'<a href="/action/{action}">{label}</a></td></tr>'
    )


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
    assert rows[0]["current_state_codes"] == ["15", "20"]
    assert rows[0]["current_state_code"] == ""
    assert rows[0]["current_state_resolution"] == "RESOLVED_LATEST_ACTION_MEMBERSHIPS"
    memberships = [{"project_id": "42", "visit_id": "1", "workflow_state_code": int(code)} for code in rows[0]["workflow_state_codes"]]
    metrics = project_workflow_metrics("42", ["1"], memberships)
    assert metrics["workflow_state_15_visits"] == 1
    assert metrics["workflow_state_20_visits"] == 1
    assert metrics["assigned_visits"] == 1
    assert metrics["workflow_multi_match_visits"] == 1


def test_action_index_uses_the_state_link_for_the_highest_action_id():
    html = (
        labeled_action_row("9", 40, "Анкета утверждена", action="100")
        + labeled_action_row("9", 45, "В оплате отказано", action="200")
    )
    indexed = action_index(html, "42")["9"]
    assert indexed["action_id"] == "200"
    assert indexed["current_state_code"] == "45"
    assert indexed["raw_status"] == "45"
    assert indexed["current_state_resolution"] == "RESOLVED_LATEST_ACTION"


def test_state_code_stays_paired_with_its_action_id_inside_one_portal_row():
    html = (
        '<tr class="table-success"><td><a href="/proj/42">P</a><a href="/visit/9">V</a>'
        '<a href="/action/100">40</a><a href="/action/100">Анкета утверждена</a>'
        '<a href="/action/200">45</a><a href="/action/200">В оплате отказано</a></td></tr>'
    )
    parsed = parse_action_table(html, "42")[0]
    assert parsed["current_action_id"] == "200"
    assert parsed["current_state_code"] == "45"
    assert parsed["workflow_state_action_pairs"] == [
        {"action_id": "100", "workflow_state_code": 40},
        {"action_id": "200", "workflow_state_code": 45},
    ]


def test_action_row_with_multiple_codes_on_same_latest_action_preserves_current_memberships():
    html = "<!doctype html><html><body>" + action_row("9", [40, 45], action="200") + "</body></html>"
    indexed = action_index(html, "42")["9"]
    assert indexed["action_id"] == "200"
    assert indexed["current_state_code"] is None
    assert indexed["current_state_codes"] == ["40", "45"]
    assert indexed["raw_status"] == "45"  # legacy membership remains available but is not current success
    assert indexed["current_state_resolution"] == "RESOLVED_LATEST_ACTION_MEMBERSHIPS"


def test_current_state_index_uses_later_real_action_after_legacy_free_assignment_marker():
    html = (
        '<tr class="table-red"><td><a href="/proj/42">P</a>'
        '<a href="/visit/9">V</a><a href="/action/0">0</a></td></tr>'
        + labeled_action_row("9", 40, "Анкета утверждена", action="200")
    )
    # Preserve the separately-qualified assignment contract while current
    # outcome resolves from the later action.
    assert action_index(html, "42")["9"]["assignment_state"] == "UNASSIGNED_FREE"
    current = current_state_index(html, "42")["9"]
    assert current == {
        "action_id": "200",
        "current_state_code": 40,
        "current_state_codes": [40],
        "current_state_resolution": "RESOLVED_LATEST_ACTION",
    }


def test_current_state_index_keeps_all_codes_from_the_latest_action():
    html = (
        labeled_action_row("9", 40, "Анкета утверждена", action="200")
        + labeled_action_row("9", 50, "Оплачено", action="200")
        + labeled_action_row("9", 45, "В оплате отказано", action="100")
    )
    current = current_state_index(html, "42")["9"]
    assert current["action_id"] == "200"
    assert current["current_state_codes"] == [40, 50]
    assert current["current_state_code"] is None
    assert current["current_state_resolution"] == "RESOLVED_LATEST_ACTION_MEMBERSHIPS"


def test_memberships_keep_each_action_id_without_changing_visit_state_deduplication():
    html = (
        labeled_action_row("9", 40, "Анкета утверждена", action="100")
        + labeled_action_row("9", 45, "В оплате отказано", action="200")
    )
    memberships = action_memberships(html, "42")
    assert {(item["action_id"], item["workflow_state_code"]) for item in memberships} == {
        ("100", 40), ("200", 45)
    }


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


def test_unrelated_numeric_action_id_362206_is_not_a_workflow_state():
    html = (
        '<tr class="table-success"><td><a href="/proj/42">P</a>'
        '<a href="/visit/1">V</a><a href="/action/362206">362206</a></td></tr>'
    )
    row = parse_action_table(html, "42")[0]
    assert row["action_id"] == "362206"
    assert row["workflow_state_codes"] == []


def test_all_fourteen_canonical_states_parse_as_state_controls():
    html = "".join(labeled_action_row(str(code), code, label) for code, label in {
        0: "Отправлено приглашение", 5: "Приглашение отклонено",
        10: "Приглашение принято", 15: "Отклонено менеджером",
        20: "Подтверждено менеджером", 25: "Провалено пользователем",
        30: "Выполнено пользователем", 35: "Анкета отклонена",
        37: "Анкета проверена", 38: "Вопрос координатору",
        39: "Есть претензия", 40: "Анкета утверждена",
        45: "В оплате отказано", 50: "Оплачено",
    }.items())
    rows = parse_action_table(html, "42")
    assert {int(row["workflow_state_codes"][0]) for row in rows} == set(WORKFLOW_STATE_CODES)


def test_unknown_code_in_state_control_fails_closed():
    html = labeled_action_row("1", 362206, "Оплачено")
    with pytest.raises(ValueError, match="unknown workflow state code: 362206"):
        parse_action_table(html, "42")


def test_unknown_latest_state_does_not_fall_back_to_an_older_success():
    html = (
        labeled_action_row("1", 40, "Анкета утверждена", action="100")
        + '<tr class="table-success"><td><a href="/proj/42">P</a><a href="/visit/1">V</a>'
        '<a href="/action/200">362206</a><a href="/action/200">Оплачено</a></td></tr>'
    )
    with pytest.raises(ValueError, match="unknown workflow state code: 362206"):
        parse_action_table(html, "42")


def test_unrelated_numeric_link_does_not_poison_a_row_with_known_state_code():
    html = (
        '<tr class="table-success"><td><a href="/proj/42">P</a>'
        '<a href="/visit/1">V</a>'
        '<a href="/action/900">40</a>'
        '<a href="/action/888">2026</a><a href="/action/888">Дополнительная ссылка</a>'
        '</td></tr>'
    )
    parsed = parse_action_table(html, "42")[0]
    assert parsed["workflow_state_codes"] == ["40"]
    assert parsed["current_state_code"] == "40"
    assert parsed["current_state_resolution"] == "RESOLVED_LATEST_ACTION"


def test_unrelated_numeric_control_with_non_state_label_is_ignored():
    html = (
        '<tr class="table-success"><td><a href="/proj/42">P</a>'
        '<a href="/visit/1">V</a><a href="/action/362206">362206</a>'
        '<a href="/action/362206">Открыть действие</a></td></tr>'
    )
    assert parse_action_table(html, "42")[0]["workflow_state_codes"] == []


def test_action_zero_red_row_is_explicit_unassigned_not_state_or_assigned():
    html = (
        '<tr class="table-red"><td><a href="/proj/42">P</a>'
        '<a href="/visit/9">V</a><a href="/action/0">0</a></td></tr>'
    )
    row = parse_action_table(html, "42")[0]
    assert row["action_id"] == "0"
    assert row["participant_assigned"] is False
    assert row["assignment_state"] == "UNASSIGNED_FREE"
    assert row["workflow_state_codes"] == []
    indexed = action_index(html, "42")["9"]
    assert indexed["action_id"] == "0"
    assert indexed["assignment_state"] == "UNASSIGNED_FREE"


def test_explicit_unassigned_marker_wins_over_invitation_event_for_same_visit():
    html = (
        labeled_action_row("9", 0, "Отправлено приглашение", action="222")
        + '<tr class="table-red"><td><a href="/proj/42">P</a>'
        '<a href="/visit/9">V</a><a href="/action/0">0</a></td></tr>'
    )
    indexed = action_index(html, "42")["9"]
    assert indexed["action_id"] == "0"
    assert indexed["assignment_state"] == "UNASSIGNED_FREE"
