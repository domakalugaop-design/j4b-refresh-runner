from copy import deepcopy
from urllib.parse import unquote

import pytest

from src import refresh
from src.project_types import STATE_COLUMNS


class SheetsEmulator:
    """Small in-memory Google Sheets Values API emulator for publication tests."""

    def __init__(self, *, fail_current_read=False, fail_current_write=False, current_mutation=None, type_mutation=None):
        self.previous_current = [refresh.COLUMNS, self.row(10)]
        self.candidate_current = [refresh.COLUMNS, self.row(10), self.row(11)]
        self.previous_types = [STATE_COLUMNS, [10, "0001", "Обычный"]]
        self.candidate_types = [STATE_COLUMNS, [10, "0001", "Обычный"], [11, "0002", "Классика_ТП"]]
        self.current = deepcopy(self.previous_current)
        self.project_types = deepcopy(self.previous_types)
        self.fail_current_read = fail_current_read
        self.fail_current_write = fail_current_write
        self.current_mutation = current_mutation
        self.type_mutation = type_mutation
        self.current_reads = 0
        self.type_reads = 0
        self.events = []

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
            elif range_name.startswith("projects_current!"):
                self.current = rows
                self.events.append("write_projects_current")
                if self.fail_current_write and self.events.count("write_projects_current") == 1:
                    self.current = rows[:2]
                    raise RuntimeError("injected partial current write failure")
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
            return {"sheets": [{"properties": {"title": "projects_current", "sheetId": 7}}]}
        if url.endswith(":batchUpdate"):
            self.events.append("format_projects_current")
            return {}
        raise AssertionError(f"unexpected API call: {url}")


@pytest.fixture
def emulated_sheets(monkeypatch):
    def install(emulator):
        monkeypatch.setattr(refresh, "api", emulator.call)
        monkeypatch.setattr(refresh, "api_get", emulator.get)
        return emulator

    return install


def publish(emulator):
    return refresh.publish_project_type_refresh(
        "not-a-real-token",
        "isolated-test-sheet",
        emulator.candidate_current,
        emulator.previous_current,
        refresh.COLUMNS,
        {"10": ("0001", "Обычный"), "11": ("0002", "Классика_ТП")},
        emulator.previous_types,
    )


def test_full_publication_accepts_project_type_string_ids_and_omitted_trailing_blanks(emulated_sheets):
    emulator = emulated_sheets(SheetsEmulator())

    publish(emulator)

    assert emulator.current == emulator.candidate_current
    assert emulator.project_types == emulator.candidate_types
    assert emulator.events.index("write_project_types") < emulator.events.index("write_projects_current")
    assert emulator.events.index("read_projects_current") > emulator.events.index("write_projects_current")


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

    with pytest.raises(RuntimeError, match="injected current readback API failure"):
        publish(emulator)

    assert emulator.current == emulator.previous_current
    assert emulator.project_types == emulator.previous_types


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


def test_candidate_duplicate_and_shrink_guards_fail_before_any_sheet_mutation(emulated_sheets):
    emulator = emulated_sheets(SheetsEmulator())
    duplicated = [emulator.candidate_current[0], emulator.candidate_current[1], emulator.candidate_current[1]]

    with pytest.raises(RuntimeError, match="candidate contains duplicate project IDs"):
        refresh.publish("token", "isolated-test-sheet", duplicated, emulator.previous_current)

    with pytest.raises(RuntimeError, match="candidate master shrinks"):
        refresh.publish("token", "isolated-test-sheet", emulator.previous_current, emulator.candidate_current)

    assert emulator.events == []
