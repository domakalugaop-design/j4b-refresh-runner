from copy import deepcopy
from urllib.parse import unquote

import pytest

from src import production, refresh
from src.payment_refresh import publish_payment_pair
from src.project_types import STATE_COLUMNS


class SheetsEmulator:
    """Small in-memory Google Sheets Values API emulator for publication tests."""

    def __init__(self, *, fail_current_read=False, fail_current_write=False, fail_current_restore=False, fail_current_format=False, fail_metadata=False, missing_current_sheet=False, current_mutation=None, current_restore_mutation=None, type_mutation=None, type_restore_mutation=None):
        self.previous_current = [refresh.COLUMNS, self.row(10)]
        self.candidate_current = [refresh.COLUMNS, self.row(10), self.row(11)]
        self.previous_types = [STATE_COLUMNS, [10, "0001", "Обычный"]]
        self.candidate_types = [STATE_COLUMNS, [10, "0001", "Обычный"], [11, "0002", "Классика_ТП"]]
        self.current = deepcopy(self.previous_current)
        self.project_types = deepcopy(self.previous_types)
        self.fail_current_read = fail_current_read
        self.fail_current_write = fail_current_write
        self.fail_current_restore = fail_current_restore
        self.fail_current_format = fail_current_format
        self.fail_metadata = fail_metadata
        self.missing_current_sheet = missing_current_sheet
        self.current_mutation = current_mutation
        self.current_restore_mutation = current_restore_mutation
        self.type_mutation = type_mutation
        self.type_restore_mutation = type_restore_mutation
        self.current_reads = 0
        self.type_reads = 0
        self.events = []
        self.format_requests = []

    @staticmethod
    def row(project_id):
        return [project_id] + [""] * (len(refresh.COLUMNS) - 1)

    @staticmethod
    def values_url(url):
        return unquote(url.split("/values/", 1)[1].split("?", 1)[0])

    def get(self, url, token):
        if "/values/" not in url:
            raise AssertionError(f"unexpected GET: {url}")
        range_name = self.values_url(url)
        if range_name.startswith("project_types!"):
            self.type_reads += 1
            self.events.append("read_project_types")
            if self.type_mutation and self.type_reads == 1:
                return {"values": self.type_mutation(deepcopy(self.project_types))}
            if self.type_restore_mutation and self.events.count("write_project_types") > 1:
                return {"values": self.type_restore_mutation(deepcopy(self.project_types))}
            # Sheets may return numeric IDs as strings and omit trailing empty cells.
            rows = deepcopy(self.project_types)
            for row in rows[1:]:
                row[0] = str(row[0])
            return {"values": rows}
        if range_name.startswith("projects_current!"):
            self.current_reads += 1
            self.events.append("read_projects_current")
            if self.fail_current_read and self.current_reads == 1:
                raise RuntimeError("injected current readback API failure")
            rows = deepcopy(self.current)
            if self.current_mutation and self.current_reads == 1:
                rows = self.current_mutation(rows)
            return {"values": [self.trim_trailing_blanks(row) for row in rows]}
        raise AssertionError(f"unexpected values range: {range_name}")

    @staticmethod
    def trim_trailing_blanks(row):
        trimmed = list(row)
        while trimmed and trimmed[-1] in (None, ""):
            trimmed.pop()
        return trimmed

    def call(self, url, token, body=None, method=None):
        decoded = unquote(url)
        if "/values:batchUpdate" in url:
            range_name = body["data"][0]["range"]
            rows = deepcopy(body["data"][0]["values"])
            if range_name.startswith("project_types!"):
                self.project_types = rows
                self.events.append("write_project_types")
                if self.type_restore_mutation and self.events.count("write_project_types") == 2:
                    self.project_types = self.type_restore_mutation(deepcopy(self.project_types))
            elif range_name.startswith("projects_current!"):
                self.current = rows
                self.events.append("write_projects_current")
                if self.fail_current_write and self.events.count("write_projects_current") == 1:
                    self.current = rows[:2]
                    raise RuntimeError("injected partial current write failure")
                if self.fail_current_restore and self.events.count("write_projects_current") == 2:
                    self.current = rows[:1]
                    raise RuntimeError("injected current rollback write failure")
                if self.current_restore_mutation and self.events.count("write_projects_current") == 2:
                    self.current = self.current_restore_mutation(deepcopy(self.current))
            else:
                raise AssertionError(f"unexpected batch range: {range_name}")
            return {}
        if ":clear" in decoded:
            self.events.append("clear_project_types" if "project_types!" in decoded else "clear_projects_current")
            if "project_types!" in decoded:
                self.project_types = []
            elif "projects_current!" in decoded:
                self.current = []
            else:
                raise AssertionError(f"unexpected clear: {decoded}")
            return {}
        if "?fields=sheets.properties(title,sheetId)" in url:
            if self.fail_metadata:
                raise RuntimeError("injected worksheet metadata failure")
            if self.missing_current_sheet:
                return {"sheets": []}
            return {"sheets": [{"properties": {"title": "projects_current", "sheetId": 7}}]}
        if url.endswith(":batchUpdate"):
            self.events.append("format_projects_current")
            self.format_requests.extend(deepcopy(body["requests"]))
            if self.fail_current_format:
                raise RuntimeError("injected current date-format failure")
            return {}
        raise AssertionError(f"unexpected API call: {url}")


@pytest.fixture
def emulated_sheets(monkeypatch):
    def install(emulator):
        monkeypatch.setattr(refresh, "api", emulator.call)
        monkeypatch.setattr(refresh, "api_get", emulator.get)
        return emulator

    return install


def publish(emulator, authoritative_stale_ids=None):
    return refresh.publish_project_type_refresh(
        "not-a-real-token",
        "isolated-test-sheet",
        emulator.candidate_current,
        emulator.previous_current,
        refresh.COLUMNS,
        {"10": ("0001", "Обычный"), "11": ("0002", "Классика_ТП")},
        emulator.previous_types,
        authoritative_stale_ids=authoritative_stale_ids,
    )


def test_full_publication_accepts_project_type_string_ids_and_omitted_trailing_blanks(emulated_sheets):
    emulator = emulated_sheets(SheetsEmulator())

    publish(emulator)

    assert emulator.current == emulator.candidate_current
    assert emulator.project_types == emulator.candidate_types
    assert emulator.events.index("write_project_types") < emulator.events.index("write_projects_current")
    assert emulator.events.index("read_projects_current") > emulator.events.index("write_projects_current")


def test_api_omission_never_prunes_previous_project_from_current_snapshot(emulated_sheets):
    emulator = emulated_sheets(SheetsEmulator())
    source_rows = [{"project_id": str(pid)} for pid in (10, 11, 12, 13, 14, 15, 16)]
    reconciled, stale_ids = refresh.reconcile_current_rows(
        source_rows, {"10", "11", "15", "16"}, acquisition_complete=True
    )
    emulator.previous_current = [refresh.COLUMNS, *(emulator.row(pid) for pid in (10, 11, 12, 13, 14))]
    emulator.candidate_current = [refresh.COLUMNS, *(emulator.row(row["project_id"]) for row in reconciled)]
    emulator.current = deepcopy(emulator.previous_current)

    assert stale_ids == {"12", "13", "14"}
    assert len(emulator.candidate_current) - 1 > len(emulator.previous_current) - 1
    publish(emulator, authoritative_stale_ids=stale_ids)

    assert emulator.current == emulator.candidate_current
    assert emulator.project_types == emulator.candidate_types
    assert "write_projects_current" in emulator.events
    assert {str(row[0]) for row in emulator.current[1:]} >= {"12", "13", "14"}


@pytest.mark.parametrize("authoritative_stale_ids", [None, {"99"}, {"10", "12"}])
def test_unapproved_shrink_is_rejected_with_precise_stage_before_mutation(emulated_sheets, authoritative_stale_ids):
    emulator = emulated_sheets(SheetsEmulator())
    previous = [refresh.COLUMNS, emulator.row(10), emulator.row(12)]
    candidate = [refresh.COLUMNS, emulator.row(10)]

    with pytest.raises(RuntimeError, match="candidate master shrinks") as caught:
        refresh.publish(
            "token",
            "isolated-test-sheet",
            candidate,
            previous,
            authoritative_stale_ids=authoritative_stale_ids,
        )

    assert caught.value._j4b_publication_diagnostic["primary_stage"] == "PROJECTS_CURRENT_BASELINE_GUARD"
    assert emulator.events == []


@pytest.mark.parametrize(
    ("options", "expected_stage", "message"),
    [
        ({"fail_metadata": True}, "PROJECTS_CURRENT_WORKSHEET_METADATA", "worksheet metadata"),
        ({"missing_current_sheet": True}, "PROJECTS_CURRENT_WORKSHEET_METADATA", "target worksheet not found"),
        ({"fail_current_format": True}, "PROJECTS_CURRENT_FORMAT", "date-format"),
    ],
)
def test_projects_current_failure_stages_are_safe_and_rollback(emulated_sheets, options, expected_stage, message):
    emulator = emulated_sheets(SheetsEmulator(**options))

    with pytest.raises(RuntimeError, match=message) as caught:
        publish(emulator)

    diagnostic = caught.value._j4b_publication_diagnostic
    assert diagnostic["primary_stage"] == expected_stage
    assert diagnostic["rollback_status"] == "PASS"
    assert emulator.current == emulator.previous_current
    assert emulator.project_types == emulator.previous_types


def test_large_projects_current_range_and_date_formats_are_preserved(emulated_sheets):
    emulator = emulated_sheets(SheetsEmulator())
    emulator.previous_current = [refresh.COLUMNS, *(emulator.row(pid) for pid in range(1, 1001))]
    emulator.candidate_current = [refresh.COLUMNS, *(emulator.row(pid) for pid in range(1, 1201))]
    emulator.current = deepcopy(emulator.previous_current)

    refresh.publish("token", "isolated-test-sheet", emulator.candidate_current, emulator.previous_current)

    assert emulator.current == emulator.candidate_current
    assert len(emulator.format_requests) == 3
    formats = [request["repeatCell"]["cell"]["userEnteredFormat"]["numberFormat"] for request in emulator.format_requests]
    assert [item["type"] for item in formats] == ["DATE", "DATE", "DATE_TIME"]
    assert emulator.events.index("format_projects_current") < emulator.events.index("read_projects_current")


@pytest.mark.parametrize(
    "mutation",
    [
        lambda rows: [rows[0], ["999"] + rows[1][1:], *rows[2:]],
        lambda rows: [rows[0], rows[1]],
        lambda rows: [rows[0], rows[2], rows[1]],
        lambda rows: [rows[0], rows[1], [*rows[2][:-1], "changed"]],
    ],
    ids=["changed-id", "missing-row", "reordered-rows", "changed-value"],
)
def test_current_readback_real_differences_fail_and_restore_both_tabs(emulated_sheets, mutation):
    emulator = emulated_sheets(SheetsEmulator(current_mutation=mutation))

    with pytest.raises(RuntimeError, match="projects_current full readback mismatch"):
        publish(emulator)

    assert emulator.current == emulator.previous_current
    assert emulator.project_types == emulator.previous_types
    assert "clear_projects_current" in emulator.events
    assert emulator.events[-1] == "read_project_types"


def test_current_readback_api_failure_preserves_original_error_and_restores_tabs(emulated_sheets):
    emulator = emulated_sheets(SheetsEmulator(fail_current_read=True))

    with pytest.raises(RuntimeError, match="injected current readback API failure") as caught:
        publish(emulator)

    assert emulator.current == emulator.previous_current
    assert emulator.project_types == emulator.previous_types
    assert caught.value._j4b_publication_diagnostic["rollback_status"] == "PASS"


def test_partial_current_write_is_restored_and_project_types_rolled_back(emulated_sheets):
    emulator = emulated_sheets(SheetsEmulator(fail_current_write=True))

    with pytest.raises(RuntimeError, match="injected partial current write failure"):
        publish(emulator)

    assert emulator.current == emulator.previous_current
    assert emulator.project_types == emulator.previous_types


def test_project_type_readback_real_difference_stops_before_current_publication(emulated_sheets):
    emulator = emulated_sheets(
        SheetsEmulator(type_mutation=lambda rows: [rows[0], rows[1]])
    )

    with pytest.raises(RuntimeError, match="project_types readback mismatch"):
        publish(emulator)

    assert emulator.current == emulator.previous_current
    assert emulator.project_types == emulator.previous_types
    assert "write_projects_current" not in emulator.events


def test_numeric_representation_change_on_current_readback_is_not_silently_coerced(emulated_sheets):
    emulator = emulated_sheets(
        SheetsEmulator(current_mutation=lambda rows: [[*rows[0]], [str(rows[1][0]), *rows[1][1:]], *rows[2:]])
    )

    with pytest.raises(RuntimeError, match="projects_current full readback mismatch"):
        publish(emulator)

    assert emulator.current == emulator.previous_current


def test_publish_preserves_readback_and_rollback_failures(emulated_sheets):
    emulator = emulated_sheets(
        SheetsEmulator(
            fail_current_restore=True,
            current_mutation=lambda rows: [rows[0], ["999"] + rows[1][1:], *rows[2:]],
        )
    )

    with pytest.raises(refresh.PublicationRollbackError) as caught:
        refresh.publish("token", "isolated-test-sheet", emulator.candidate_current, emulator.previous_current)

    error = caught.value
    assert "projects_current full readback mismatch" in str(error.primary_error)
    assert "injected current rollback write failure" in str(error.rollback_error)
    assert error.__cause__ is error.primary_error
    assert error.diagnostic["primary_stage"] == "PROJECTS_CURRENT_SEMANTIC_COMPARE"
    assert error.diagnostic["rollback_status"] == "FAIL"
    assert error.diagnostic["rollback_failure_stage"] == "PROJECTS_CURRENT_RESTORE_WRITE"


def test_publish_preserves_api_failure_and_failed_rollback(emulated_sheets):
    emulator = emulated_sheets(
        SheetsEmulator(fail_current_read=True, fail_current_restore=True)
    )

    with pytest.raises(refresh.PublicationRollbackError) as caught:
        refresh.publish("token", "isolated-test-sheet", emulator.candidate_current, emulator.previous_current)

    assert str(caught.value.primary_error) == "injected current readback API failure"
    assert str(caught.value.rollback_error) == "injected current rollback write failure"
    assert caught.value.diagnostic["primary_stage"] == "PROJECTS_CURRENT_READBACK"
    assert caught.value.diagnostic["rollback_status"] == "FAIL"


def test_publish_readback_mismatch_and_rollback_readback_mismatch_are_distinguished(emulated_sheets):
    emulator = emulated_sheets(
        SheetsEmulator(
            current_mutation=lambda rows: [rows[0], ["999"] + rows[1][1:], *rows[2:]],
            current_restore_mutation=lambda rows: [rows[0], ["998"] + rows[1][1:]],
        )
    )

    with pytest.raises(refresh.PublicationRollbackError) as caught:
        refresh.publish("token", "isolated-test-sheet", emulator.candidate_current, emulator.previous_current)

    assert "projects_current full readback mismatch" in str(caught.value.primary_error)
    assert "projects_current rollback readback mismatch" in str(caught.value.rollback_error)
    assert caught.value.diagnostic["rollback_failure_stage"] == "PROJECTS_CURRENT_RESTORE_READBACK"


def test_candidate_duplicate_and_shrink_guards_fail_before_any_sheet_mutation(emulated_sheets):
    emulator = emulated_sheets(SheetsEmulator())
    duplicated = [emulator.candidate_current[0], emulator.candidate_current[1], emulator.candidate_current[1]]

    with pytest.raises(RuntimeError, match="candidate contains duplicate project IDs"):
        refresh.publish("token", "isolated-test-sheet", duplicated, emulator.previous_current)

    with pytest.raises(RuntimeError, match="candidate master shrinks"):
        refresh.publish("token", "isolated-test-sheet", emulator.previous_current, emulator.candidate_current)

    assert emulator.events == []


def _run_full_refresh_rollback(emulator, previous, candidate, *, trigger_payment_failure=False):
    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(refresh, "api", emulator.call)
        patcher.setattr(refresh, "api_get", emulator.get)
        patcher.setattr(production, "read_sheet", lambda *_args, **_kwargs: deepcopy(emulator.current))
        patcher.setattr(production, "read_project_type_state_rows", lambda *_args: [])
        refresh.publish("token", "isolated-test-sheet", candidate, previous)
        if trigger_payment_failure:
            payment_previous = {
                "visits": [["id", "value"], ["old", "ok"]],
                "projects": [["id"], ["old"]],
            }
            payment_candidate = {
                "visits": [["id", "value"], ["new", "x" * 500]],
                "projects": [["id"], ["new"]],
            }
            payment_writes = []
            with pytest.raises(ValueError, match="single payment row exceeds chunk byte limit"):
                publish_payment_pair(
                    sheet_ids={"visits": 20, "projects": 21},
                    previous=payment_previous,
                    candidate=payment_candidate,
                    write_batch=lambda requests: payment_writes.append(requests),
                    read_tab=lambda tab: payment_previous[tab],
                    max_request_bytes=250,
                )
            assert payment_writes == []
        production._rollback_full_refresh(
            "token", "isolated-test-sheet", columns=list(refresh.COLUMNS),
            previous=previous, previous_raw=previous, previous_state_rows=[],
            workflow_backup=None, payment_publication=None, layout_plan={"planned": []},
            failed_candidate=candidate,
        )


def test_full_refresh_payment_failure_rolls_back_new_project_8406_exactly():
    previous = [refresh.COLUMNS, SheetsEmulator.row(10)]
    candidate = [refresh.COLUMNS, SheetsEmulator.row(10), SheetsEmulator.row(8406)]
    emulator = SheetsEmulator()
    emulator.previous_current = deepcopy(previous)
    emulator.candidate_current = deepcopy(candidate)
    emulator.current = deepcopy(previous)

    _run_full_refresh_rollback(emulator, previous, candidate, trigger_payment_failure=True)

    assert emulator.current == previous
    assert [row[0] for row in emulator.current[1:]] == [10]
    assert "clear_projects_current" in emulator.events
    assert emulator.events.count("write_projects_current") == 2
    assert production._projects_current_fingerprint(emulator.current, list(refresh.COLUMNS)) == production._projects_current_fingerprint(previous, list(refresh.COLUMNS))


def test_full_refresh_rollback_fails_closed_on_readback_mismatch():
    previous = [refresh.COLUMNS, SheetsEmulator.row(10)]
    candidate = [refresh.COLUMNS, SheetsEmulator.row(10), SheetsEmulator.row(8406)]
    emulator = SheetsEmulator()
    emulator.previous_current = deepcopy(previous)
    emulator.candidate_current = deepcopy(candidate)
    emulator.current = deepcopy(previous)
    emulator.current_restore_mutation = lambda rows: [rows[0], [999, *rows[1][1:]]]

    with pytest.raises(RuntimeError, match="full refresh rollback incomplete"):
        _run_full_refresh_rollback(emulator, previous, candidate)

    assert emulator.current != previous


def test_full_refresh_rollback_does_not_remove_id_outside_failed_candidate():
    previous = [refresh.COLUMNS, SheetsEmulator.row(10)]
    candidate = [refresh.COLUMNS, SheetsEmulator.row(10), SheetsEmulator.row(8406)]
    emulator = SheetsEmulator()
    emulator.previous_current = deepcopy(previous)
    emulator.candidate_current = deepcopy(candidate)
    emulator.current = [refresh.COLUMNS, SheetsEmulator.row(10), SheetsEmulator.row(999)]

    with pytest.raises(RuntimeError, match="full refresh rollback incomplete"):
        with pytest.MonkeyPatch.context() as patcher:
            patcher.setattr(production, "read_sheet", lambda *_args, **_kwargs: deepcopy(emulator.current))
            patcher.setattr(production, "read_project_type_state_rows", lambda *_args: [])
            production._rollback_full_refresh(
                "token", "isolated-test-sheet", columns=list(refresh.COLUMNS),
                previous=previous, previous_raw=previous, previous_state_rows=[],
                workflow_backup=None, payment_publication=None, layout_plan={"planned": []},
                failed_candidate=candidate,
            )

    assert not any(event.startswith("clear_projects_current") for event in emulator.events)
