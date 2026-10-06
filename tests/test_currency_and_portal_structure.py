import html

from src.acquisition import Reader, acquire_project
from src.parsers import inspect_project_edit_structure, parse_currency_select, parse_edit
from src.payment_materialization import PROJECT_PUBLICATION_COLUMNS, VISIT_PUBLICATION_COLUMNS
from src.refresh import COLUMNS, PROJECT_TYPE_COLUMNS, materialize, sheet_rows
from src.production import _upgrade_projects_rows, primary_columns


PROJECT_ID = "999999"
PROJECT_NAME = "Fixture_Project_Q3_0926"


def edit_page(currency="<select name='currency'><option value='1' selected>рубль</option></select>", extra=""):
    return f"""<!doctype html><html><body>
    <form action='/proj/{PROJECT_ID}/edit' method='post'>
      <input name='name' value='{PROJECT_NAME}'>
      <input name='dt1' value='01.09.2026'><input name='dt2' value='30.09.2026'>
      <input name='visits' value='4'><input name='cost' value='100'>
      <select name='client'><option value='1' selected>Client</option></select>
      <select name='user'><option value='2' selected>Manager</option></select>
      <select name='user2[]'><option value='3' selected>Coordinator</option></select>
      <select name='wave'><option value='4' selected>Wave</option></select>
      <select name='scope'><option value='5' selected>Scope</option></select>
      {currency}{extra}
    </form></body></html>"""


class FakeSession:
    base_url = "https://lk.j4b.ru"

    def __init__(self, edit, project_name=PROJECT_NAME):
        self.edit = edit
        self.project_name = project_name
        self.calls = []
        self.last_retry_after = None
        self.last_effective_url = None

    def request(self, path, method, data=None, accept=None):
        self.calls.append((path, method))
        self.last_effective_url = self.base_url + path
        body = (
            f"<!doctype html><html><body>{html.escape(self.project_name)}<a href='/visit/123'>visit</a></body></html>"
            if path == f"/proj/{PROJECT_ID}"
            else self.edit
            if path == f"/proj/{PROJECT_ID}/edit"
            else "<!doctype html><html><body>action results</body></html>"
        )
        return 200, "text/html; charset=UTF-8", body.encode()


def acquired(edit, project_name=PROJECT_NAME):
    session = FakeSession(edit, project_name)
    record, visits = acquire_project(
        Reader(session, 3, max_retries=0, sleep=lambda _seconds: None),
        {"project_id": PROJECT_ID, "project_name": project_name}, 0,
    )
    return record, visits, session


def test_rub_currency_id_and_authoritative_dictionary_values_round_trip():
    parsed = parse_currency_select(edit_page())
    assert parsed == {
        "state": "VALUE_PRESENT", "value": "1", "currency_code": "RUB",
        "currency_name": "рубль", "currency_symbol": "₽", "dictionary_match": True,
    }
    record, _visits, session = acquired(edit_page())
    assert record["acquisition_state"] == "ACQUIRED"
    assert record["currency_id"]["value"] == "1"
    assert record["currency_code"]["value"] == "RUB"
    assert record["currency_name"]["value"] == "рубль"
    assert record["currency_symbol"]["value"] == "₽"
    assert len(session.calls) == 3
    assert session.calls[1] == (f"/proj/{PROJECT_ID}/edit", "GET")
    rows = materialize([record], [], "2026-10-05T00:00:00+00:00")
    matrix = sheet_rows(rows)
    assert matrix[0] == COLUMNS
    assert [matrix[1][COLUMNS.index(key)] for key in COLUMNS[-4:]] == ["1", "RUB", "рубль", "₽"]


def test_parse_edit_ignores_name_like_markup_outside_the_project_form():
    decoys = (
        "<form><input name='name' value='Men'></form>"
        "<textarea><input name='name' value='Also Men'></textarea>"
    )
    source = decoys + edit_page()

    parsed = parse_edit(source)
    assert parsed["project_name"] == {
        "state": "VALUE_PRESENT", "value": PROJECT_NAME,
    }
    record, _visits, _session = acquired(source)
    assert record["project_name"]["value"] == PROJECT_NAME
    assert record["acquisition_state"] == "ACQUIRED"


def test_real_structure_apostrophe_fix_and_html_attribute_values():
    values = [
        ('Men\'s Look', '"Men\'s Look"'),
        ('O\'Reilly', '"O\'Reilly"'),
        ('John\'s project', '"John\'s project"'),
        ('Проект без апострофа', '"Проект без апострофа"'),
        ('"quoted" project', "'\"quoted\" project'"),
        ('A & B', '"A & B"'),
        ('A < B', '"A &lt; B"'),
        ('O\'Reilly', '"O&#39;Reilly"'),
    ]
    for expected, attribute in values:
        # Mirrors the observed Portal shape: a project form with POST method,
        # no explicit action, and a double-quoted name value when apostrophes
        # occur. The placeholder remains intentionally synthetic.
        source = edit_page().replace(
            f"<form action='/proj/{PROJECT_ID}/edit' method='post'>",
            "<form method='post'>",
        ).replace(f"value='{PROJECT_NAME}'", f"value={attribute}")
        parsed = parse_edit(source)["project_name"]
        assert parsed["state"] == "VALUE_PRESENT"
        assert html.unescape(parsed["value"]) == expected

    apostrophe_source = edit_page().replace(
        f"<form action='/proj/{PROJECT_ID}/edit' method='post'>",
        "<form method='post'>",
    ).replace(f"value='{PROJECT_NAME}'", 'value="Men\'s Look store"')
    record, _visits, _session = acquired(apostrophe_source, "Men's Look store")
    assert record["project_name"]["value"] == "Men's Look store"
    assert record["acquisition_state"] == "ACQUIRED"


def test_nullable_currency_control_empty_and_missing_are_not_defaulted():
    no_explicit_selection = "<select name='currency'><option value='1'>рубль</option></select>"
    assert parse_currency_select(edit_page(no_explicit_selection))["state"] == "CONTROL_PRESENT_NO_SELECTION"
    empty_selected = "<select name='currency'><option value='' selected>Не выбрана</option></select>"
    assert parse_currency_select(edit_page(empty_selected))["state"] == "FIELD_PRESENT_EMPTY"
    assert parse_currency_select(edit_page(currency=""))["state"] == "FIELD_NOT_EXPOSED"

    record, _visits, session = acquired(edit_page(currency=""))
    assert record["acquisition_state"] == "ACQUIRED"
    assert record["currency_id"]["state"] == "FIELD_NOT_EXPOSED"
    assert all(record[key]["value"] is None for key in ("currency_id", "currency_code", "currency_name", "currency_symbol"))
    assert len(session.calls) == 3  # currency adds no request


def test_currency_dictionary_unknown_key_or_label_mismatch_fails_closed():
    unknown = parse_currency_select("<select name='currency'><option value='999' selected>Unknown</option></select>")
    assert unknown["dictionary_match"] is False
    mismatch = parse_currency_select("<select name='currency'><option value='1' selected>доллар</option></select>")
    assert mismatch["dictionary_match"] is False
    record, _visits, _session = acquired(edit_page(currency="<select name='currency'><option value='1' selected>доллар</option></select>"))
    assert record["acquisition_state"] == "SEMANTIC_FAILURE"
    assert "edit:CURRENCY_DICTIONARY_MISMATCH" in record["acquisition_failure_reasons"]


def test_currency_dictionary_decodes_portal_html_entity_symbol_only():
    parsed = parse_currency_select(
        "<select name='currency'><option value='4' selected>Лари - грузинский</option></select>"
    )
    assert parsed["dictionary_match"] is True
    assert parsed["currency_code"] == "GEL"
    assert parsed["currency_symbol"] == "₾"


def test_edit_structure_rejects_login_error_denied_and_non_form_200():
    cases = [
        ("<!doctype html><html><body><form><input name='_login'><input name='_password'></form></body></html>", "login_form_present"),
        ("<!doctype html><html><body>PHP Parse error: unexpected token</body></html>", "php_error_present"),
        ("<!doctype html><html><body>Access denied</body></html>", "access_denied_present"),
        ("<!doctype html><html><body>arbitrary response</body></html>", "project_form_missing"),
    ]
    for html, marker in cases:
        structure = inspect_project_edit_structure(html)
        if marker == "project_form_missing":
            assert structure["project_form_present"] is False
        else:
            assert structure[marker] is True
        if marker != "project_form_missing":
            assert structure["project_form_present"] is False

    rejects = [
        ("<!doctype html><html><body><form><input name='_login'><input name='_password'></form></body></html>", "edit:LOGIN_PAGE"),
        ("<!doctype html><html><body>PHP Parse error: unexpected token</body></html>", "edit:PHP_ERROR"),
        ("<!doctype html><html><body>Access denied</body></html>", "edit:ACCESS_DENIED"),
        ("<!doctype html><html><body>arbitrary response</body></html>", "edit:PROJECT_FORM_MISSING"),
    ]
    for bad_edit, expected_reason in rejects:
        record, _visits, session = acquired(bad_edit)
        assert session.calls[1] == (f"/proj/{PROJECT_ID}/edit", "GET")
        assert record["acquisition_state"] == "SEMANTIC_FAILURE"
        assert expected_reason in record["acquisition_failure_reasons"]


def test_valid_project_form_does_not_require_currency_control():
    parsed = parse_edit(edit_page(currency=""))
    structure = inspect_project_edit_structure(edit_page(currency=""))
    assert structure["project_form_present"] is True
    assert parsed["currency_id"]["state"] == "FIELD_NOT_EXPOSED"
    record, _visits, session = acquired(edit_page(currency=""))
    assert record["acquisition_state"] == "ACQUIRED"
    assert len(session.calls) == 3


def test_workflow_publication_schema_appends_currency_without_moving_existing_fields():
    workflow_columns = primary_columns(PROJECT_TYPE_COLUMNS)
    current_target = workflow_columns + [
        "currency_id", "currency_code", "currency_name", "currency_symbol",
    ]
    assert len(workflow_columns) == 54
    assert len(current_target) == 58
    legacy_row = [f"old-{index}" for index in range(len(workflow_columns))]
    upgraded = _upgrade_projects_rows(
        [workflow_columns, legacy_row], workflow_columns, current_target,
    )
    assert upgraded[0] == current_target
    assert upgraded[1][:len(workflow_columns)] == legacy_row
    assert upgraded[1][-4:] == ["", "", "", ""]


def test_legacy_33_column_rows_map_currency_and_preserve_all_prior_values():
    legacy_row = [f"legacy-{index}" for index in range(len(PROJECT_TYPE_COLUMNS))]
    upgraded = _upgrade_projects_rows(
        [PROJECT_TYPE_COLUMNS, legacy_row], PROJECT_TYPE_COLUMNS, COLUMNS,
    )
    assert upgraded[0] == COLUMNS
    assert upgraded[1][:len(PROJECT_TYPE_COLUMNS)] == legacy_row
    assert upgraded[1][-4:] == ["", "", "", ""]


def test_currency_dimension_does_not_expand_payment_tab_contracts():
    assert not set(COLUMNS[-4:]).intersection(VISIT_PUBLICATION_COLUMNS)
    assert not set(COLUMNS[-4:]).intersection(PROJECT_PUBLICATION_COLUMNS)
