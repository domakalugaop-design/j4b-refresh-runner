from src.acquisition import Reader, acquire_project
from src.refresh import BASE_COLUMNS, merge_previous, materialize


def response(body, status=200, content_type="text/html; charset=utf-8"):
    return status, content_type, body.encode("utf-8")


class FakeSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.base_url = "https://lk.j4b.ru"
        self.last_effective_url = None

    def request(self, path, method, data=None, accept=None):
        item = next(self.responses)
        if len(item) == 4:
            status, content_type, body, self.last_effective_url = item
            return status, content_type, body
        status, content_type, body = item
        self.last_effective_url = self.base_url + path
        return status, content_type, body


def complete_edit(name="Project_Q3_0926", plan="4"):
    return f"""<!doctype html><html><body>
      <input name='name' value='{name}'>
      <input name='dt1' value='01.09.2026'><input name='dt2' value='30.09.2026'>
      <input name='visits' value='{plan}'><input name='cost' value='100'>
      <select name='client'><option selected value='1'>Client</option></select>
      <select name='user'><option selected value='2'>Manager</option></select>
      <select name='user2[]'><option selected value='3'>Coordinator</option></select>
      <select name='wave'><option selected value='4'>Wave</option></select>
      <select name='scope'><option selected value='5'>Scope</option></select>
    </body></html>"""


def test_http_200_structurally_incomplete_edit_fails_closed_and_keeps_last_good():
    name = "Project_Q3_0926"
    session = FakeSession([
        response(f"<!doctype html><html><title>{name}</title><body>{name}/visit/1</body></html>"),
        response("<!doctype html><html><body>temporarily incomplete</body></html>"),
        response("<!doctype html><html><body>actions</body></html>"),
    ])
    record, visits = acquire_project(Reader(session, 3), {"project_id": "7998", "project_name": name}, 0)
    assert visits == [{
        "project_id": "7998", "visit_id": "1",
        "raw_status": {"value": None, "state": "UNKNOWN", "provenance": {"route": "/action?project=7998"}},
        "assignment_state": {"value": "UNKNOWN", "state": "UNKNOWN", "provenance": {"route": "/action?project=7998"}},
    }]
    assert record["acquisition_state"] == "SEMANTIC_FAILURE"
    assert any(reason.startswith("edit:FIELD_NOT_EXPOSED:") for reason in record["acquisition_failure_reasons"])

    old = {field: "last-good" for field in BASE_COLUMNS}
    old.update({"project_id": "7998", "project_name": name, "plan": 22, "plan_value": 22, "client": "client", "primary_manager": "manager", "coordinators": "coord", "date_from": "2026-09-01", "date_to": "2026-09-30", "scope": "scope", "manager_payment": 100, "wave": "wave"})
    previous = [BASE_COLUMNS, [old[column] for column in BASE_COLUMNS]]
    materialized = materialize([record], visits, "2026-09-22T00:00:00+00:00")
    carried = merge_previous(materialized, previous, {"7998"}, "2026-09-22T00:00:00+00:00")[0]
    assert carried["_acquisition_state"] == "SEMANTIC_FAILURE"
    for field in ("project_name", "plan", "plan_value", "client", "primary_manager", "coordinators", "date_from", "date_to", "scope", "manager_payment", "wave"):
        assert carried[field] == old[field]


def test_complete_acquisition_is_accepted_and_applies_fresh_operational_values():
    name = "Project_Q3_0926"
    session = FakeSession([
        response(f"<!doctype html><html><title>{name}</title><body>{name}/visit/1</body></html>"),
        response(complete_edit(plan="6")),
        response("<!doctype html><html><body>actions</body></html>"),
    ])
    record, _visits = acquire_project(Reader(session, 3), {"project_id": "42", "project_name": name}, 0)
    assert record["acquisition_state"] == "ACQUIRED"
    assert record["project_identity_source"] == "project_page"
    assert record["planned_visit_count"]["value"] == 6
    rows = materialize([record], [], "2026-09-22T00:00:00+00:00")
    assert rows[0]["plan"] == 6


def test_missing_project_page_name_uses_matching_edit_form_as_scoped_fallback():
    name = "Project_Q3_0926"
    session = FakeSession([
        response("<!doctype html><html><body><a href='/visit/1'>Visit</a></body></html>"),
        response(complete_edit(name=name)),
        response("<!doctype html><html><body>actions</body></html>"),
    ])
    record, _ = acquire_project(Reader(session, 3), {"project_id": "42", "project_name": name}, 0)
    assert record["acquisition_state"] == "ACQUIRED"
    assert record["project_identity_source"] == "project_edit_fallback"
    assert record["acquisition_failure_reasons"] == []
    assert record["project_id"] == "42"
    assert record["project_name"]["value"] == name


def test_missing_project_page_name_with_conflicting_edit_name_fails():
    session = FakeSession([
        response("<!doctype html><html><body>project</body></html>"),
        response(complete_edit(name="Different_Project_0926")),
        response("<!doctype html><html><body>actions</body></html>"),
    ])
    record, _ = acquire_project(Reader(session, 3), {"project_id": "42", "project_name": "Expected_Project_0926"}, 0)
    assert record["acquisition_state"] == "SEMANTIC_FAILURE"
    assert "project:CANONICAL_NAME_NOT_PRESENT" in record["acquisition_failure_reasons"]
    assert "edit:CANONICAL_NAME_MISMATCH" in record["acquisition_failure_reasons"]


def test_missing_project_and_edit_names_fail():
    session = FakeSession([
        response("<!doctype html><html><body>project</body></html>"),
        response(complete_edit(name="")),
        response("<!doctype html><html><body>actions</body></html>"),
    ])
    record, _ = acquire_project(Reader(session, 3), {"project_id": "42", "project_name": "Expected_Project_0926"}, 0)
    assert record["acquisition_state"] == "SEMANTIC_FAILURE"
    assert "project:CANONICAL_NAME_NOT_PRESENT" in record["acquisition_failure_reasons"]
    assert "edit:PROJECT_NAME_EMPTY" in record["acquisition_failure_reasons"]


def test_project_redirect_to_different_id_fails_even_if_name_matches():
    name = "Project_Q3_0926"
    session = FakeSession([
        (*response(f"<!doctype html><html><body>{name}/visit/1</body></html>"), "https://lk.j4b.ru/proj/99"),
        response(complete_edit(name=name)),
        response("<!doctype html><html><body>actions</body></html>"),
    ])
    record, _ = acquire_project(Reader(session, 3), {"project_id": "42", "project_name": name}, 0)
    assert record["acquisition_state"] == "SEMANTIC_FAILURE"
    assert "project:IDENTITY_MISMATCH" in record["acquisition_failure_reasons"]


def test_missing_name_does_not_allow_malformed_project_or_edit_page():
    session = FakeSession([
        response("plain response with no canonical name"),
        response("plain malformed edit response"),
        response("<!doctype html><html><body>actions</body></html>"),
    ])
    record, _ = acquire_project(Reader(session, 3), {"project_id": "42", "project_name": "Expected_Project_0926"}, 0)
    assert record["acquisition_state"] == "SEMANTIC_FAILURE"
    assert "project:NOT_HTML_DOCUMENT" in record["acquisition_failure_reasons"]
    assert "edit:NOT_HTML_DOCUMENT" in record["acquisition_failure_reasons"]


def test_transport_failure_remains_failed_with_identity_fallback_enabled():
    name = "Project_Q3_0926"
    session = FakeSession([
        response("", status=503),
        response(complete_edit(name=name)),
        response("<!doctype html><html><body>actions</body></html>"),
    ])
    record, _ = acquire_project(Reader(session, 3), {"project_id": "42", "project_name": name}, 0)
    assert record["acquisition_state"] == "FAILED"
