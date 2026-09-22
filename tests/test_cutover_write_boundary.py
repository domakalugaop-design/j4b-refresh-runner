import hashlib
import json
import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from src import production
from src.project_types import PROJECT_TYPE_DICTIONARY, STATE_COLUMNS
from src.refresh import BASE_COLUMNS, PROJECT_TYPE_COLUMNS, api


def _meta(columns=31, include_state=False):
    sheets = [{"properties": {"title": "projects_current", "sheetId": 17, "gridProperties": {"columnCount": columns, "rowCount": 1000}}}]
    if include_state:
        sheets.append({"properties": {"title": "project_types", "sheetId": 18, "gridProperties": {"columnCount": 3, "rowCount": 1000}}})
    return {"spreadsheetId": "sheet-1", "properties": {"title": production.PRODUCTION_TITLE}, "sheets": sheets}


def _seed_file(tmp_path: Path, entries):
    path = tmp_path / "project-type-bootstrap.json"
    content = json.dumps(entries, ensure_ascii=False, separators=(",", ":")).encode()
    path.write_bytes(content)
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return path, hashlib.sha256(content).hexdigest()


def _universe(pid="8110", name="4 Лапы_0926"):
    return [{"project_id": pid, "project_name": name}]


def _entry(pid="8110", code="0005"):
    return {"project_id": pid, "project_type_code": code, "project_type_name": PROJECT_TYPE_DICTIONARY.get(code, "invalid")}


def test_prevalidation_write_regression_31_columns_and_missing_state_only_plans():
    with patch("src.production.api") as sheet_api:
        plan = production._plan_project_type_layout(_meta())
    assert plan["state_exists"] is False
    assert plan["planned"] == ["EXPAND projects_current A:AE -> A:AG", "CREATE project_types"]
    assert plan["requests"]
    sheet_api.assert_not_called()


def test_invalid_bootstrap_fails_closed_before_any_sheet_mutation(tmp_path):
    invalid = _entry(pid="8111", code="9999")
    path, digest = _seed_file(tmp_path, [invalid])
    assert path.stat().st_size > 0
    universe = _universe() + [{"project_id": "8111", "project_name": "4 Лапы_0926"}]
    with pytest.raises(ValueError, match="unknown bootstrap"):
        production._resolve_project_type_state(False, [], str(path), digest, {"8110", "8111"}, universe)


def test_valid_bootstrap_dry_run_candidate_has_type_and_migration_plan(tmp_path):
    path, digest = _seed_file(tmp_path, [_entry()])
    rows, state, source, actual_digest = production._resolve_project_type_state(
        False, [], str(path), digest, {"8110"}, _universe()
    )
    assert source == "LOCAL_VALIDATED_ARTIFACT"
    assert rows[0] == STATE_COLUMNS
    assert actual_digest == digest
    candidate = [{"project_id": "8110", "project_name": "4 Лапы_0926"}]
    production.materialize_project_types(candidate, state, {"8110"})
    assert candidate[0]["project_type_code"] == "0005"
    assert candidate[0]["project_type_name"] == PROJECT_TYPE_DICTIONARY["0005"]
    plan = production._plan_project_type_layout(_meta())
    assert plan["planned"]


def test_state_present_is_authoritative_and_never_reads_seed(tmp_path):
    rows = [STATE_COLUMNS, ["8110", "0005", PROJECT_TYPE_DICTIONARY["0005"]]]
    with patch("src.production._load_bootstrap_seed", side_effect=AssertionError("must not reseed")):
        actual_rows, state, source, digest = production._resolve_project_type_state(
            True, rows, str(tmp_path / "missing-seed.json"), "bad-hash", {"8110"}, _universe()
        )
    assert actual_rows == rows
    assert state == {"8110": ("0005", PROJECT_TYPE_DICTIONARY["0005"])}
    assert source == "PERSISTED_STATE"
    assert digest == "NONE"


def test_bootstrap_rejects_pre_cutoff_unknown_target_and_name_mismatch(tmp_path):
    cases = [
        (_entry(), set(), _universe()),
        (_entry(), {"8110"}, _universe(name="4 Лапы_0826")),
        ({**_entry(), "project_type_name": "wrong"}, {"8110"}, _universe()),
    ]
    for entry, ids, universe in cases:
        path, digest = _seed_file(tmp_path, [entry])
        with pytest.raises(ValueError):
            production._resolve_project_type_state(False, [], str(path), digest, ids, universe)


def test_dry_run_api_guard_blocks_google_sheets_mutation(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "true")
    with patch("src.refresh.urllib.request.urlopen") as urlopen:
        with pytest.raises(RuntimeError, match="DRY_RUN blocked"):
            api("https://sheets.googleapis.com/v4/spreadsheets/sheet-1:batchUpdate", "token", {"requests": []})
    urlopen.assert_not_called()


def test_dry_run_api_guard_allows_google_sheets_reads(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "true")
    with patch("src.refresh.urllib.request.urlopen") as urlopen:
        urlopen.return_value.__enter__.return_value.read.return_value = b'{"ok":true}'
        # json.load reads the response object; give it a file-like context manager.
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self, *args):
                return b'{"ok":true}'
        urlopen.return_value = Response()
        assert api("https://sheets.googleapis.com/v4/spreadsheets/sheet-1", "token") == {"ok": True}


def test_full_production_dry_run_uses_candidate_path_and_never_mutates(tmp_path, monkeypatch):
    path, digest = _seed_file(tmp_path, [_entry()])
    baseline = [BASE_COLUMNS, ["8110"] + [""] * (len(BASE_COLUMNS) - 1)]
    row = {column: "" for column in PROJECT_TYPE_COLUMNS}
    row.update({column: "" for column in BASE_COLUMNS})
    row.update({"project_id": "8110", "project_name": "4 Лапы_0926"})

    class Session:
        requests = auth_get_count = auth_post_count = 0
        def login(self):
            pass
        def close(self):
            pass

    class FakeReader:
        count = post_count = 0
        def __init__(self, *_args):
            pass

    for key, value in {
        "RUN_MODE": "production",
        "DRY_RUN": "true",
        "GOOGLE_SPREADSHEET_ID": "sheet-1",
        "PROJECT_TYPE_BOOTSTRAP_PATH": str(path),
        "PROJECT_TYPE_BOOTSTRAP_SHA256": digest,
        "PORTAL_REQUEST_DELAY": "0",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("ALLOW_PRODUCTION_WRITE", raising=False)
    monkeypatch.delenv("PRODUCTION_CONFIRMATION", raising=False)

    with patch("src.production.google_token", return_value="opaque"), \
         patch("src.production._destination_preflight", return_value=_meta()), \
         patch("src.production.read_sheet", return_value=baseline), \
         patch("src.production.PortalSession", return_value=Session()), \
         patch("src.production.discover_universe", return_value=_universe()), \
         patch("src.production.Reader", FakeReader), \
         patch("src.production.select_scope", return_value=[]), \
         patch("src.production.materialize", return_value=[]), \
         patch("src.production.merge_previous", return_value=[row]), \
         patch("src.production._execute_project_type_layout") as migrate, \
         patch("src.production.publish_project_type_refresh") as publish, \
         patch("src.production.api") as sheet_api:
        result = production.run()
    assert result["FINAL_STATUS"] == "DRY_RUN_PASS"
    assert result["TARGET_SHEET_WRITES"] == 0
    assert result["EXECUTED_SCHEMA_MUTATIONS"] == 0
    assert result["CANDIDATE_ROWS"] == 1
    assert result["CANDIDATE_DUPLICATES"] == 0
    assert result["BOOTSTRAP_ROWS"] == 1
    migrate.assert_not_called()
    publish.assert_not_called()
    assert all("sheets.googleapis.com" not in str(call.args[0]) or (call.kwargs.get("method") or ("POST" if len(call.args) > 2 else "GET")) == "GET" for call in sheet_api.call_args_list)


def test_schema_rollback_removes_only_new_tab_and_expansion():
    plan = production._plan_project_type_layout(_meta())
    previous = [BASE_COLUMNS, ["8110"] + [""] * (len(BASE_COLUMNS) - 1)]
    expanded = {"sheets": [
        {"properties": {"title": "projects_current", "sheetId": 17, "gridProperties": {"columnCount": 33}}},
        {"properties": {"title": "project_types", "sheetId": 18, "gridProperties": {"columnCount": 3}}},
    ]}
    restored = _meta(columns=31)
    with patch("src.production.api", side_effect=[expanded, {}, restored]) as sheet_api, patch(
        "src.production.read_sheet", return_value=previous
    ):
        production._rollback_project_type_layout("token", "sheet-1", plan, previous)
    mutation_body = sheet_api.call_args_list[1].args[2]
    requests = mutation_body["requests"]
    assert {next(iter(request)) for request in requests} == {"deleteSheet", "deleteDimension"}
