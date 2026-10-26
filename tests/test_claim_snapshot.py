"""Tests for the Portal status-39 claim snapshot schema and parser."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.claim_snapshot import (  # noqa: E402
    CLAIM_STATUS_ID,
    ClaimSnapshotError,
    build_snapshot_records,
    parse_claim_search,
    snapshot_fingerprint,
    validate_snapshot_records,
)

RUNNER = REPO_ROOT / "scripts" / "run_claim_snapshot.py"


def _row(action_id, visit_id, project_id, *, status="39", label="Есть претензия",
         datetimes=("17.10.2026 19:07", "18.10.2026 09:23", "24.10.2026 15:44"),
         annotation="", extra_cell=None):
    cells = [
        f'<td><a href="/action/{action_id}">{action_id}</a></td>',
        f'<td><a href="/proj/{project_id}">Проект пример</a></td>',
        "<td>Адрес пример</td>",
        f"<td>{datetimes[0]}{annotation}</td>",
        "<td>500</td>",
        "<td>₽</td>",
        f"<td>{datetimes[1]}</td>",
        "<td></td>",
        "<td>Пользователь Пример</td>",
        "<td>user@example.com</td>",
        "<td>79000000000</td>",
        f"<td>{status}</td>",
        f"<td>{label}</td>",
        f"<td>{datetimes[2]}</td>",
        "<td>Контролёр Пример</td>",
        f"<td><a href=\"/visit/{visit_id}\">999 ТЕСТ</a></td>",
    ]
    if extra_cell is not None:
        cells.append(f"<td>{extra_cell}</td>")
    return "<tr class=\"table-purple black\">" + "".join(cells) + "</tr>"


def _unassigned_row(visit_id, project_id):
    return (
        "<tr class=\"table-red\">"
        f"<td><a href=\"/action/0\">{visit_id}</a></td>"
        "<td><a href=\"/proj/8203\">Проект пример</a></td>"
        "<td>Адрес пример</td>"
        "<td></td><td>500</td><td>₽</td><td></td><td></td>"
        "<td></td><td></td><td></td>"
        "<td>&nbsp;</td>"
        "<td>0 приглашений</td>"
        "<td></td><td></td><td>999 ТЕСТ</td>"
        "</tr>"
    )


@pytest.fixture
def search_markup():
    rows = [
        _row("681001", "780001", "8200"),
        # shifted layout: annotation text appended to the datetime cell
        _row("681002", "780002", "8201", annotation=" копир Пример"),
        # extra trailing cell (layout drift)
        _row("681003", "780003", "8202", extra_cell="заметка"),
        _unassigned_row("780004", "8203"),
        _unassigned_row("780005", "8203"),
    ]
    return (
        "<!doctype html><html><head></head><body>"
        "<form method=\"POST\" action=\"\">"
        "<input type=\"checkbox\" name=\"state[39]\" CHECKED>"
        "</form>"
        "<table class=\"table table-bordered table-sm\">"
        + "".join(rows) +
        "</table></body></html>"
    )


def test_parse_extracts_only_claim_observations(search_markup):
    search = parse_claim_search(search_markup)
    assert len(search.observations) == 3
    assert search.unassigned_rows == 2
    assert search.response_rows == 5
    assert {(o.action_id, o.visit_id, o.project_id) for o in search.observations} == {
        ("681001", "780001", "8200"),
        ("681002", "780002", "8201"),
        ("681003", "780003", "8202"),
    }
    assert search.distinct_visits == 3
    assert search.distinct_projects == 3
    assert search.pagination_markers == ()
    assert search.warnings == ()


def test_parse_rejects_non_html_and_auth_pages():
    with pytest.raises(ClaimSnapshotError):
        parse_claim_search("<div>not a document</div>")
    auth = "<html><form><input name=\"_login\" type=\"text\"></form></html>"
    with pytest.raises(ClaimSnapshotError):
        parse_claim_search(auth)
    with pytest.raises(ClaimSnapshotError):
        parse_claim_search("<html><p>доступ запрещён</p></html>")


def test_parse_fails_closed_on_missing_membership():
    markup = (
        "<!doctype html><html><body><table>"
        "<tr><td><a href=\"/action/681004\">681004</a></td>"
        "<td><a href=\"/visit/780006\">780006</a></td>"
        "<td><a href=\"/proj/8204\">8204</a></td>"
        "<td>40</td></tr>"
        "</table></body></html>"
    )
    search = parse_claim_search(markup)
    assert search.observations == ()
    assert any("row_without_status39_membership" in w for w in search.warnings)


def test_snapshot_records_schema_and_grain(search_markup):
    search = parse_claim_search(search_markup)
    records, manifest = build_snapshot_records(
        search, snapshot_id="snap1", captured_at_utc="2026-10-26T18:30:00+00:00",
        source_generation="action_state39_filter:test", query_window="2026-10-01..2026-10-26",
    )
    validate_snapshot_records(records)
    assert len(records) == 3
    for record in records:
        assert record["status_id"] == CLAIM_STATUS_ID
        assert record["report_id"] == ""
        assert record["controller_id"] == ""
        assert record["report_date"] == ""
        assert record["visit_date"] == ""
        assert record["date_semantics"] == "UNCONFIRMED"
        assert record["source_quality"] == "OBSERVED_CURRENT_STATE_39"
        assert record["action_url"].endswith(record["action_id"])
    assert manifest["claim_observation_count"] == 3
    assert manifest["unassigned_rows"] == 2
    assert manifest["distinct_visits"] == 3
    assert manifest["collection_completeness"] == "COMPLETE_CURRENT_STATE_FILTER_RESULT"


def test_snapshot_idempotent_fingerprint(search_markup):
    search = parse_claim_search(search_markup)
    first, _ = build_snapshot_records(
        search, snapshot_id="snap1", captured_at_utc="2026-10-26T18:30:00+00:00",
        source_generation="gen", query_window="w",
    )
    second, _ = build_snapshot_records(
        search, snapshot_id="snap1", captured_at_utc="2026-10-26T18:30:00+00:00",
        source_generation="gen", query_window="w",
    )
    assert snapshot_fingerprint(first) == snapshot_fingerprint(second)


def test_validate_rejects_fanout_and_invented_report_id(search_markup):
    search = parse_claim_search(search_markup)
    records, _ = build_snapshot_records(
        search, snapshot_id="snap1", captured_at_utc="t", source_generation="g", query_window="w",
    )
    duplicated = [dict(records[0]) for _ in range(2)]
    with pytest.raises(ClaimSnapshotError):
        validate_snapshot_records(duplicated)
    invented = [dict(records[0], report_id="681001") for _ in [records[0]]]
    with pytest.raises(ClaimSnapshotError):
        validate_snapshot_records(invented)
    wrong_status = [dict(records[0], status_id=40)]
    with pytest.raises(ClaimSnapshotError):
        validate_snapshot_records(wrong_status)


def test_runner_end_to_end(tmp_path, search_markup):
    source = tmp_path / "response.html"
    source.write_text(search_markup, encoding="utf-8")
    source.chmod(0o600)
    output = tmp_path / "snapshot.json"
    result = subprocess.run(
        [sys.executable, str(RUNNER), str(source), "--output", str(output),
         "--query-window", "2026-10-01..2026-10-26", "--snapshot-id", "snapX"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "OK"
    assert payload["snapshot_id"] == "snapX"
    assert payload["records"] == 3
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved["manifest"]["snapshot_id"] == "snapX"
    assert len(saved["records"]) == 3
    # idempotent re-run with the same snapshot id
    repeat = subprocess.run(
        [sys.executable, str(RUNNER), str(source), "--output", str(output),
         "--query-window", "2026-10-01..2026-10-26", "--snapshot-id", "snapX"],
        capture_output=True, text=True,
    )
    assert repeat.returncode == 0
    assert json.loads(repeat.stdout)["fingerprint"] == payload["fingerprint"]


def test_runner_refuses_broad_permissions(tmp_path, search_markup):
    source = tmp_path / "response.html"
    source.write_text(search_markup, encoding="utf-8")
    source.chmod(0o644)
    output = tmp_path / "snapshot.json"
    result = subprocess.run(
        [sys.executable, str(RUNNER), str(source), "--output", str(output)],
        capture_output=True, text=True,
    )
    assert result.returncode == 2
    assert json.loads(result.stdout)["status"] == "REFUSED_INPUT_PERMISSIONS_TOO_BROAD"
