import json
from unittest.mock import patch
from urllib.error import HTTPError
from datetime import date

import pytest

from src.project_types import (
    PROJECT_TYPE_DICTIONARY,
    ProjectTypeAcquisitionError,
    STATE_COLUMNS,
    apply_canonical_project_names,
    acquire_pending_types,
    acquire_current_types,
    classify_raw_type,
    is_project_type_applicable,
    materialize_project_types,
    parse_project_type_catalog_html,
    merge_assignment,
    normalized_period_marker,
    project_type_applicable_ids,
    reconcile_project_type_assignments,
    serialize_state,
    state_rows_equivalent,
    validate_project_type_catalog,
    validate_state_rows,
)
from src import production
from src.refresh import PublicationRollbackError, publish_project_type_refresh, publish_project_type_state
from src.portal_transport import PortalTransportError


DIRECTORY_HTML = b"""<!doctype html><html><body><table>
<tr><th>ID</th><th>Code</th><th>Name</th></tr>
<tr><td>2</td><td>0002</td><td>Classic</td></tr>
<tr><td>3</td><td>0003</td><td>Taxi</td></tr>
<tr><td>4</td><td>0004</td><td>Photo</td></tr>
<tr><td>5</td><td>0005</td><td>Qualitative</td></tr>
<tr><td>7</td><td>0007</td><td>B2B</td></tr>
<tr><td>8</td><td>0008</td><td>Digital</td></tr>
<tr><td>9</td><td>0009</td><td>Home Test</td></tr>
<tr><td>10</td><td>0010</td><td>New type</td></tr>
</table></body></html>"""


def test_live_directory_parser_preserves_codes_and_discovers_new_types():
    catalog = parse_project_type_catalog_html(DIRECTORY_HTML)
    assert len(catalog) == 8
    assert catalog["0009"] == "Home Test"
    assert catalog["0010"] == "New type"


@pytest.mark.parametrize(
    "html",
    [
        b"<html><body><table><tr><td>2</td><td>0002</td><td>x</td></tr></table></body></html>",
        DIRECTORY_HTML.replace(b"<td>0009</td>", b"<td>0002</td>"),
        DIRECTORY_HTML.replace(b"<td>New type</td>", b"<td></td>"),
        DIRECTORY_HTML.replace(b"<table>", b"<table><nav class='pagination'>next</nav>"),
        b"<html><body>PHP Parse error</body></html>",
    ],
)
def test_live_directory_parser_fails_closed_on_incomplete_or_ambiguous_pages(html):
    with pytest.raises(ValueError):
        parse_project_type_catalog_html(html)


def test_current_type_acquisition_updates_existing_assignments_and_type_names():
    class Session:
        def __init__(self):
            self.calls = []

        def request(self, path, method, accept):
            self.calls.append((path, method, accept))
            project_id = path.rsplit("/", 1)[-1]
            payload = {"type": {"1": "0009", "2": "0010", "3": "0009"}[project_id]}
            return 200, "application/json", json.dumps(payload).encode()

    session = Session()
    existing = {
        "1": ("0002", "old label"),
        "2": ("0003", "old type"),
        "3": ("0009", "old name"),
    }
    catalog = validate_project_type_catalog([
        ("0002", "Classic"), ("0003", "Taxi"), ("0004", "Photo"),
        ("0005", "Qualitative"), ("0007", "B2B"), ("0008", "Digital"),
        ("0009", "Home Test"), ("0010", "New type"),
    ])
    universe = [{"project_id": str(i), "project_name": f"P{i}_0926"} for i in range(1, 4)]
    state, telemetry, applicable = acquire_current_types(session, universe, existing, catalog, delay=0)
    assert applicable == {"1", "2", "3"}
    assert state == {
        "1": ("0009", "Home Test"),
        "2": ("0010", "New type"),
        "3": ("0009", "Home Test"),
    }
    assert telemetry.valid_assignments_acquired == 3
    assert telemetry.changed_assignments == 3
    assert telemetry.immutable_conflicts == 0
    assert len(session.calls) == 3


def test_current_type_acquisition_isolates_unknown_type_but_duplicate_ids_remain_global():
    class Session:
        def __init__(self, payloads):
            self.payloads = iter(payloads)

        def request(self, *_args, **_kwargs):
            return 200, "application/json", json.dumps(next(self.payloads)).encode()

    catalog = validate_project_type_catalog([
        (code, name) for code, name in [
            ("0002", "A"), ("0003", "B"), ("0004", "C"), ("0005", "D"),
            ("0007", "E"), ("0008", "F"), ("0009", "Home Test"),
        ]
    ])
    universe = [{"project_id": "1", "project_name": "P_0926"}]
    state, telemetry, applicable = acquire_current_types(
        Session([{"type": "0010"}]), universe, {"1": ("0002", "A")}, catalog, delay=0
    )
    assert applicable == {"1"}
    assert state == {"1": ("0002", "A")}
    assert len(telemetry.project_failures) == 1
    assert telemetry.project_failures[0].diagnostic["FAILURE_CATEGORY"] == "UNRESOLVED_TYPE"
    with pytest.raises(ValueError, match="duplicate project type observation"):
        reconcile_project_type_assignments(
            {}, [("1", "0009"), ("1", "0009")], {"1"}, list(catalog.items())
        )


_DIAGNOSTIC_CATALOG = {"0002": "Classic", "0009": "Home Test"}
_DIAGNOSTIC_UNIVERSE = [
    {"project_id": str(i), "project_name": f"Fixture_{i}_0926"}
    for i in range(1, 4)
]


class _DiagnosticSession:
    def __init__(self, fail_at, failure=None, response=None):
        self.fail_at = fail_at
        self.failure = failure
        self.response = response or (200, "application/json", b'{"type":"0002"}')
        self.calls = []

    def request(self, path, method, accept):
        self.calls.append((path, method, accept))
        if len(self.calls) == self.fail_at and self.failure is not None:
            raise self.failure
        if len(self.calls) == self.fail_at and self.response is not None:
            return self.response
        return 200, "application/json", b'{"type":"0002"}'


def _acquire_result(session, existing=None):
    return acquire_current_types(
        session, _DIAGNOSTIC_UNIVERSE, existing or {}, _DIAGNOSTIC_CATALOG, delay=0
    )


@pytest.mark.parametrize(
    ("failure", "category"),
    [
        (TimeoutError("timeout text must not be logged"), "TRANSPORT_ERROR"),
        (ConnectionError("connection text must not be logged"), "TRANSPORT_ERROR"),
        (PortalTransportError("TIMEOUT", True), "TRANSPORT_ERROR"),
    ],
)
def test_project_type_acquisition_isolates_project_transport_diagnostics(failure, category):
    session = _DiagnosticSession(1, failure=failure, response=None)
    state, telemetry, _applicable = _acquire_result(session, {"1": ("0002", "Classic")})
    error = telemetry.project_failures[0]
    assert error.diagnostic == {
        "STAGE": "PROJECT_DETAIL_REQUEST",
        "PROJECT_INDEX": 1,
        "PROJECT_TOTAL": 3,
        "PROJECT_ID": "1",
        "FAILURE_CATEGORY": category,
        "HTTP_STATUS": "UNKNOWN",
    }
    assert len(session.calls) == 3
    assert state["1"] == ("0002", "Classic")
    assert state["2"] == ("0002", "Classic")


def test_project_type_acquisition_http_diagnostics_isolate_project_500():
    _state, telemetry, _applicable = _acquire_result(
        _DiagnosticSession(1, response=(500, "text/plain", b"sensitive body"))
    )
    error = telemetry.project_failures[0]
    assert error.diagnostic["FAILURE_CATEGORY"] == "HTTP_ERROR"
    assert error.diagnostic["HTTP_STATUS"] == 500
    assert error.diagnostic["STAGE"] == "PROJECT_DETAIL_HTTP_STATUS"


def test_project_type_acquisition_http_exception_uses_numeric_status_without_message():
    _state, telemetry, _applicable = _acquire_result(
        _DiagnosticSession(
            1,
            failure=HTTPError("https://example.invalid/?token=DO_NOT_LOG", 500, "PRIVATE BODY", None, None),
            response=None,
        )
    )
    error = telemetry.project_failures[0]
    assert error.diagnostic["FAILURE_CATEGORY"] == "HTTP_ERROR"
    assert error.diagnostic["HTTP_STATUS"] == 500
    assert "DO_NOT_LOG" not in json.dumps(error.diagnostic)
    assert "PRIVATE BODY" not in json.dumps(error.diagnostic)


@pytest.mark.parametrize(
    ("body", "category", "stage"),
    [
        (bytes([255]), "UTF8_ERROR", "PROJECT_DETAIL_UTF8"),
        (b'{"password":"SENSITIVE_RESPONSE_BODY"', "JSON_ERROR", "PROJECT_DETAIL_JSON"),
        (b'{"type":"9999","token":"SENSITIVE_RESPONSE_BODY"}', "UNRESOLVED_TYPE", "PROJECT_DETAIL_TYPE_CLASSIFICATION"),
    ],
)
def test_project_type_acquisition_payload_diagnostics_are_sanitized(body, category, stage):
    _state, telemetry, _applicable = _acquire_result(
        _DiagnosticSession(1, response=(200, "application/json", body))
    )
    error = telemetry.project_failures[0]
    assert error.diagnostic["FAILURE_CATEGORY"] == category
    assert error.diagnostic["STAGE"] == stage
    assert error.diagnostic["HTTP_STATUS"] == 200


@pytest.mark.parametrize(("failure_index", "project_id"), [(1, "1"), (3, "3")])
def test_project_type_acquisition_reports_first_and_last_failure(failure_index, project_id):
    _state, telemetry, _applicable = _acquire_result(
        _DiagnosticSession(failure_index, failure=TimeoutError(), response=None)
    )
    error = next(item for item in telemetry.project_failures if item.diagnostic["PROJECT_INDEX"] == failure_index)
    assert error.diagnostic["PROJECT_INDEX"] == failure_index
    assert error.diagnostic["PROJECT_TOTAL"] == 3
    assert error.diagnostic["PROJECT_ID"] == project_id


def test_project_type_acquisition_diagnostics_never_include_sensitive_inputs(monkeypatch, capsys):
    secret_text = "login=PRIVATE password=PRIVATE cookie=PRIVATE token=PRIVATE"
    body_text = '{"client":"PRIVATE","token":"PRIVATE"'
    _state, telemetry, _applicable = _acquire_result(
        _DiagnosticSession(1, failure=TimeoutError(secret_text), response=None)
    )
    diagnostic = telemetry.project_failures[0].diagnostic
    assert not any(value in json.dumps(diagnostic) for value in (secret_text, body_text, "PRIVATE"))

    monkeypatch.setattr(production, "run", lambda: (_ for _ in ()).throw(ProjectTypeAcquisitionError(
        stage=diagnostic["STAGE"], project_index=diagnostic["PROJECT_INDEX"],
        project_total=diagnostic["PROJECT_TOTAL"], project_id=diagnostic["PROJECT_ID"],
        failure_category=diagnostic["FAILURE_CATEGORY"], http_status=diagnostic["HTTP_STATUS"],
    )))
    assert production.main() == 1
    output = capsys.readouterr().out
    parsed = json.loads(output)
    assert parsed["FAILURE_CATEGORY"] == "TRANSPORT_ERROR"
    assert parsed["PROJECT_ID"] == "1"
    assert not any(value in output for value in (secret_text, body_text, "PRIVATE", "Traceback"))


def test_project_type_acquisition_rejects_non_numeric_diagnostic_id():
    error = ProjectTypeAcquisitionError(
        stage="PROJECT_DETAIL_REQUEST",
        project_index=1,
        project_total=1,
        project_id="42?token=secret",
        failure_category="INTERNAL_ERROR",
        http_status="UNKNOWN",
    )
    assert error.diagnostic["PROJECT_ID"] == "UNKNOWN"


def test_project_type_acquisition_keeps_last_good_for_failed_id_and_updates_peers():
    session = _DiagnosticSession(2, failure=ConnectionError("private"), response=None)
    state, telemetry, _applicable = acquire_current_types(
        session, _DIAGNOSTIC_UNIVERSE, {"1": ("0009", "Home Test"), "2": ("0003", "Old")},
        _DIAGNOSTIC_CATALOG, delay=0,
    )
    assert len(session.calls) == 3
    assert [call[0] for call in session.calls] == ["/api/project/1", "/api/project/2", "/api/project/3"]
    assert state == {"1": ("0002", "Classic"), "2": ("0003", "Old"), "3": ("0002", "Classic")}
    assert [failure.diagnostic["PROJECT_ID"] for failure in telemetry.project_failures] == ["2"]


def test_project_type_unexpected_internal_error_remains_global():
    session = _DiagnosticSession(1, failure=RuntimeError("internal failure"), response=None)
    with pytest.raises(RuntimeError, match="internal failure"):
        acquire_current_types(session, _DIAGNOSTIC_UNIVERSE, {}, _DIAGNOSTIC_CATALOG, delay=0)


def test_multiple_project_failures_are_isolated_and_successful_peer_updates():
    class Session:
        def __init__(self):
            self.index = 0

        def request(self, *_args, **_kwargs):
            self.index += 1
            if self.index == 1:
                return 200, "application/json", b"{malformed"
            if self.index == 2:
                raise TimeoutError("private transport detail")
            return 200, "application/json", b'{"type":"0009"}'

    previous = {"1": ("0002", "Classic"), "2": ("0003", "Taxi")}
    state, telemetry, _ = acquire_current_types(
        Session(), _DIAGNOSTIC_UNIVERSE, previous, _DIAGNOSTIC_CATALOG, delay=0
    )
    assert state == {"1": previous["1"], "2": previous["2"], "3": ("0009", "Home Test")}
    assert [item.diagnostic["PROJECT_ID"] for item in telemetry.project_failures] == ["1", "2"]


def test_project_type_auth_failure_remains_global():
    session = _DiagnosticSession(1, response=(403, "text/plain", b"private"))
    with pytest.raises(RuntimeError, match="authentication"):
        acquire_current_types(session, _DIAGNOSTIC_UNIVERSE, {}, _DIAGNOSTIC_CATALOG, delay=0)


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
    [("A_0826", False), ("A_0926", True), ("A_1026", True), ("A_1126", True),
     ("A_1226", False), ("A_0127", False), ("A_1326", False)],
)
def test_project_type_period_recognition_requires_one_valid_marker(name, expected):
    assert is_project_type_applicable(name, today=date(2026, 10, 19)) is expected


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
    with pytest.raises(ValueError, match="project_id"):
        validate_state_rows([STATE_COLUMNS, ["9.5", "0001", "Обычный"]])


def test_state_serialization_uses_numeric_project_id_and_text_code():
    assert serialize_state({"9": ("0001", "Обычный")}) == [STATE_COLUMNS, [9, "0001", "Обычный"]]
    assert type(serialize_state({"9": ("0001", "Обычный")})[1][0]) is int


def test_state_rows_equivalence_normalizes_numeric_ids_but_rejects_semantic_changes():
    numeric = [STATE_COLUMNS, [9, "0001", "Обычный"]]
    stringified = [STATE_COLUMNS, ["9", "0001", "Обычный"]]
    changed = [STATE_COLUMNS, ["9", "0002", "Классика_ТП"]]
    malformed = [STATE_COLUMNS, ["9", "9999", "unknown"]]

    assert state_rows_equivalent(numeric, stringified)
    assert not state_rows_equivalent(numeric, changed)
    assert not state_rows_equivalent(numeric, malformed)


def test_dynamic_catalog_accepts_new_type_and_preserves_leading_zero_codes():
    catalog = validate_project_type_catalog([
        ("0001", "Обычный"),
        ("0009", "Home Test"),
        ("0010", "A & B <Research>"),
    ])
    assert catalog == {"0001": "Обычный", "0009": "Home Test", "0010": "A & B <Research>"}


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [("0001", "Обычный"), ("0001", "Обычный")],
        [("0001", "Обычный"), ("0001", "Different")],
        [(1, "Обычный")],
        [("0001", " ")],
        [("0001", "Обычный"), ("0002", " Классика")],
    ],
)
def test_dynamic_catalog_rejects_empty_malformed_or_duplicate_codes(rows):
    with pytest.raises(ValueError):
        validate_project_type_catalog(rows)


def test_reconciliation_updates_existing_type_and_type_rename_by_project_id():
    existing = {
        "10": ("0007", "B2B_тайный покупатель"),
        "11": ("0005", "Old label"),
    }
    state, counts = reconcile_project_type_assignments(
        existing,
        [(10, "0009"), (11, "0005")],
        {"10", "11"},
        [("0005", "Качественные исследования"), ("0007", "B2B_тайный покупатель"), ("0009", "Home Test")],
    )
    assert state["10"] == ("0009", "Home Test")
    assert state["11"] == ("0005", "Качественные исследования")
    assert counts == {"added": 0, "changed": 2, "unchanged": 0}


def test_reconciliation_adds_new_project_without_duplicate_on_repeat():
    catalog = [("0009", "Home Test")]
    state, counts = reconcile_project_type_assignments({}, [(12, "0009")], {"12"}, catalog)
    repeated, repeated_counts = reconcile_project_type_assignments(state, [(12, "0009")], {"12"}, catalog)
    assert state == repeated == {"12": ("0009", "Home Test")}
    assert counts == {"added": 1, "changed": 0, "unchanged": 0}
    assert repeated_counts == {"added": 0, "changed": 0, "unchanged": 1}


def test_reconciliation_corrects_eleven_existing_assignments_without_changing_sheet_contract():
    existing = {str(project_id): ("0007", "Old label") for project_id in range(1, 12)}
    observations = [(project_id, "0009") for project_id in range(1, 12)]
    state, counts = reconcile_project_type_assignments(
        existing, observations, set(existing), [("0009", "Home Test")]
    )
    assert all(state[str(project_id)] == ("0009", "Home Test") for project_id in range(1, 12))
    assert counts == {"added": 0, "changed": 11, "unchanged": 0}
    assert serialize_state(state)[0] == STATE_COLUMNS


@pytest.mark.parametrize(
    ("observations", "expected", "catalog"),
    [
        ([(1, "0001")], {"1", "2"}, [("0001", "Обычный")]),
        ([(1, "0001"), (2, "0001")], {"1"}, [("0001", "Обычный")]),
        ([(1, "0001"), (1, "0001")], {"1"}, [("0001", "Обычный")]),
        ([(1, "0009")], {"1"}, [("0001", "Обычный")]),
        ([(1, "0001")], {"1"}, []),
    ],
)
def test_reconciliation_fails_closed_on_incomplete_duplicate_or_unknown_data(observations, expected, catalog):
    with pytest.raises(ValueError):
        reconcile_project_type_assignments({}, observations, expected, catalog)


def test_reconciliation_preserves_unobserved_assignments_and_does_not_touch_payments():
    existing = {"1": ("0007", "B2B_тайный покупатель"), "99": ("0005", "Качественные исследования")}
    state, counts = reconcile_project_type_assignments(
        existing, [(1, "0009")], {"1"}, [("0009", "Home Test")]
    )
    assert state == {"1": ("0009", "Home Test"), "99": existing["99"]}
    assert counts == {"added": 0, "changed": 1, "unchanged": 0}


def test_project_type_state_shrink_clears_obsolete_tail():
    previous_rows = [
        STATE_COLUMNS,
        [1, "0001", "Обычный"],
        [2, "0002", "Классика_ТП"],
        [3, "0005", "Качественные исследования"],
    ]
    candidate_rows = [STATE_COLUMNS, [1, "0001", "Обычный"]]
    with patch("src.refresh._write_project_type_state_rows") as write_rows, patch(
        "src.refresh._clear_project_type_state_tail"
    ) as clear_tail, patch("src.refresh.read_project_type_state_rows", return_value=candidate_rows):
        from src.refresh import publish_project_type_state

        result = publish_project_type_state(
            "token", "workbook-1", {"1": ("0001", "Обычный")}, previous_rows
        )

    assert result == candidate_rows
    write_rows.assert_called_once_with("token", "workbook-1", candidate_rows)
    clear_tail.assert_called_once_with("token", "workbook-1", 3, 4)


def test_project_type_state_readback_accepts_sheets_string_ids():
    previous_rows = [STATE_COLUMNS, [1, "0002", "Классика_ТП"]]
    actual_rows = [STATE_COLUMNS, ["1", "0001", "Обычный"]]
    with patch("src.refresh._write_project_type_state_rows"), patch(
        "src.refresh._clear_project_type_state_tail"
    ), patch("src.refresh.read_project_type_state_rows", return_value=actual_rows):
        from src.refresh import publish_project_type_state

        result = publish_project_type_state(
            "token", "workbook-1", {"1": ("0001", "Обычный")}, previous_rows
        )

    assert result == [STATE_COLUMNS, [1, "0001", "Обычный"]]


@pytest.mark.parametrize(
    "actual_rows",
    [
        [STATE_COLUMNS],
        [STATE_COLUMNS, ["1", "0002", "Классика_ТП"]],
        [STATE_COLUMNS, ["1", "0001", "Обычный"], ["1", "0001", "Обычный"]],
    ],
)
def test_project_type_state_readback_rejects_real_state_differences(actual_rows):
    previous_rows = [STATE_COLUMNS, [1, "0002", "Классика_ТП"]]
    with patch("src.refresh._write_project_type_state_rows"), patch(
        "src.refresh._clear_project_type_state_tail"
    ), patch("src.refresh.read_project_type_state_rows", side_effect=[actual_rows, previous_rows]):
        from src.refresh import publish_project_type_state

        with pytest.raises(RuntimeError, match="project_types readback mismatch"):
            publish_project_type_state(
                "token", "workbook-1", {"1": ("0001", "Обычный")}, previous_rows
            )


def test_project_type_state_primary_failure_with_successful_rollback_preserves_exception():
    previous = [STATE_COLUMNS, [1, "0002", "Классика_ТП"]]
    primary = RuntimeError("injected state write failure")
    with patch("src.refresh._write_project_type_state_rows", side_effect=[primary, None]), patch(
        "src.refresh.read_project_type_state_rows", return_value=previous
    ):
        with pytest.raises(RuntimeError) as caught:
            publish_project_type_state("token", "sheet", {"1": ("0001", "Обычный")}, previous)

    assert caught.value is primary
    assert primary._j4b_publication_diagnostic["primary_stage"] == "PROJECT_TYPES_WRITE"
    assert primary._j4b_publication_diagnostic["rollback_attempted"] is True
    assert primary._j4b_publication_diagnostic["rollback_status"] == "PASS"


def test_project_type_state_primary_and_rollback_failures_are_both_preserved():
    previous = [STATE_COLUMNS, [1, "0002", "Классика_ТП"]]
    primary = RuntimeError("primary state write failure")
    rollback = OSError("external detail must not be logged")
    with patch("src.refresh._write_project_type_state_rows", side_effect=[primary, rollback]):
        with pytest.raises(PublicationRollbackError) as caught:
            publish_project_type_state("token", "sheet", {"1": ("0001", "Обычный")}, previous)

    assert caught.value.primary_error is primary
    assert caught.value.rollback_error is rollback
    assert caught.value.__cause__ is primary
    assert caught.value.diagnostic["rollback_status"] == "FAIL"


def test_project_type_readback_mismatch_and_failed_rollback_are_both_preserved():
    previous = [STATE_COLUMNS, [1, "0002", "Классика_ТП"]]
    rollback = OSError("sensitive external error details")
    wrong_readback = [STATE_COLUMNS, [1, "0003", "Такси_ТП"]]
    with patch("src.refresh._write_project_type_state_rows", side_effect=[None, rollback]), patch(
        "src.refresh._clear_project_type_state_tail"
    ), patch("src.refresh.read_project_type_state_rows", return_value=wrong_readback):
        with pytest.raises(PublicationRollbackError) as caught:
            publish_project_type_state("token", "sheet", {"1": ("0001", "Обычный")}, previous)

    assert str(caught.value.primary_error) == "project_types readback mismatch"
    assert caught.value.rollback_error is rollback
    assert caught.value.diagnostic["primary_stage"] == "PROJECT_TYPES_READBACK"


def test_project_type_readback_mismatch_and_rollback_readback_mismatch_are_distinguished():
    previous = [STATE_COLUMNS, [1, "0002", "Классика_ТП"]]
    wrong = [STATE_COLUMNS, [1, "0003", "Такси_ТП"]]
    with patch("src.refresh._write_project_type_state_rows"), patch(
        "src.refresh._clear_project_type_state_tail"
    ), patch("src.refresh.read_project_type_state_rows", side_effect=[wrong, wrong]):
        with pytest.raises(PublicationRollbackError) as caught:
            publish_project_type_state("token", "sheet", {"1": ("0001", "Обычный")}, previous)

    assert str(caught.value.primary_error) == "project_types readback mismatch"
    assert str(caught.value.rollback_error) == "project_types rollback readback mismatch"
    assert caught.value.diagnostic["rollback_failure_stage"] == "PROJECT_TYPES_RESTORE_READBACK"


def test_project_type_refresh_primary_failure_with_successful_rollback_preserves_exception():
    previous = [STATE_COLUMNS, [1, "0001", "Обычный"]]
    primary = RuntimeError("projects_current API failure")
    with patch("src.refresh.publish_project_type_state"), patch(
        "src.refresh.publish", side_effect=primary
    ), patch("src.refresh._write_project_type_state_rows"), patch(
        "src.refresh.read_project_type_state_rows", return_value=previous
    ):
        with pytest.raises(RuntimeError) as caught:
            publish_project_type_refresh("token", "sheet", [], [], [], {"1": ("0001", "Обычный")}, previous)

    assert caught.value is primary
    assert primary._j4b_publication_diagnostic["primary_stage"] == "PROJECTS_CURRENT_PUBLISH"
    assert primary._j4b_publication_diagnostic["rollback_status"] == "PASS"


def test_project_type_refresh_primary_and_rollback_failures_are_both_preserved():
    previous = [STATE_COLUMNS, [1, "0001", "Обычный"]]
    primary = RuntimeError("primary current publication failure")
    rollback = OSError("private rollback API detail")
    with patch("src.refresh.publish_project_type_state"), patch(
        "src.refresh.publish", side_effect=primary
    ), patch("src.refresh._write_project_type_state_rows", side_effect=rollback):
        with pytest.raises(PublicationRollbackError) as caught:
            publish_project_type_refresh("token", "sheet", [], [], [], {"1": ("0001", "Обычный")}, previous)

    assert caught.value.primary_error is primary
    assert caught.value.rollback_error is rollback
    assert caught.value.diagnostic["rollback_status"] == "FAIL"


def test_project_type_refresh_retains_readback_and_first_rollback_failures():
    previous = [STATE_COLUMNS, [1, "0002", "Классика_ТП"]]
    primary = RuntimeError("project_types readback mismatch")
    first_rollback = OSError("first state restore failed")
    outer_rollback = OSError("second state restore failed")
    diagnostic = {
        "primary_error": primary,
        "primary_stage": "PROJECT_TYPES_READBACK",
        "rollback_attempted": True,
        "rollback_status": "FAIL",
        "rollback_failure_stage": "PROJECT_TYPES_RESTORE",
        "rollback_error": first_rollback,
    }
    inner = PublicationRollbackError(diagnostic)
    with patch("src.refresh.publish_project_type_state", side_effect=inner), patch(
        "src.refresh._write_project_type_state_rows", side_effect=outer_rollback
    ):
        with pytest.raises(PublicationRollbackError) as caught:
            publish_project_type_refresh("token", "sheet", [], [], [], {}, previous)

    assert caught.value.primary_error is primary
    assert caught.value.rollback_error is first_rollback
    assert caught.value.diagnostic["primary_stage"] == "PROJECT_TYPES_READBACK"


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


def test_applicability_uses_live_names_and_three_month_calendar_window():
    universe = [
        {"project_id": "1", "project_name": "old_0826"},
        {"project_id": "2", "project_name": "previous_0926"},
        {"project_id": "3", "project_name": "current_1026"},
        {"project_id": "4", "project_name": "next_1126"},
        {"project_id": "5", "project_name": "outside_1226"},
    ]
    assert project_type_applicable_ids(universe, today=date(2026, 10, 19)) == {"2", "3", "4"}
    rows = [{"project_id": "2", "project_name": "stale_0726"}]
    apply_canonical_project_names(rows, universe, {"2"})
    assert rows[0]["project_name"] == "previous_0926"


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
    applicable = project_type_applicable_ids(universe, today=date(2026, 9, 1))
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
def test_historical_assignment_is_preserved_when_project_is_outside_active_window(
    project_id, stale_name
):
    state = {project_id: ("0005", "Качественные исследования")}
    rows = [{"project_id": project_id, "project_name": stale_name}]
    materialize_project_types(rows, state, set())
    assert state == {project_id: ("0005", "Качественные исследования")}
    assert rows[0]["project_type_code"] == "0005"


def test_canonical_rename_is_applied_only_to_selected_live_ids():
    rows = [
        {"project_id": "1", "project_name": "old_1026"},
        {"project_id": "2", "project_name": "historical_old_0826"},
    ]
    universe = [
        {"project_id": "1", "project_name": "renamed_1026"},
        {"project_id": "2", "project_name": "renamed_historical_0826"},
    ]
    apply_canonical_project_names(rows, universe, {"1"})
    assert rows[0]["project_name"] == "renamed_1026"
    assert rows[1]["project_name"] == "historical_old_0826"


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
