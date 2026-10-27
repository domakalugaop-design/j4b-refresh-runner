"""Tests for TEST Sheets grid growth used by append-only versioned publishers."""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.controller_quality_sheets import GoogleSheetsGateway  # noqa: E402


def test_ensure_grid_capacity_expands_rows_and_columns(monkeypatch):
    gateway = GoogleSheetsGateway("spreadsheet", "token")
    calls = []
    responses = iter([
        {"sheets": [{"properties": {"sheetId": 44, "title": "facts", "gridProperties": {"rowCount": 1000, "columnCount": 26}}}]},
        {},
    ])

    def fake_request(method, url, payload=None):
        calls.append((method, url, payload))
        return next(responses)

    monkeypatch.setattr(gateway, "_request", fake_request)
    gateway.ensure_grid_capacity("facts", 6200, 30)
    assert calls[0][0] == "GET"
    request = calls[1][2]["requests"][0]["updateSheetProperties"]
    assert request["properties"]["sheetId"] == 44
    assert request["properties"]["gridProperties"] == {"rowCount": 6200, "columnCount": 30}
    assert request["fields"] == "gridProperties.rowCount,gridProperties.columnCount"


def test_ensure_grid_capacity_is_noop_when_sheet_is_large_enough(monkeypatch):
    gateway = GoogleSheetsGateway("spreadsheet", "token")
    calls = []

    def fake_request(method, url, payload=None):
        calls.append(method)
        return {"sheets": [{"properties": {"sheetId": 44, "title": "facts", "gridProperties": {"rowCount": 7000, "columnCount": 30}}}]}

    monkeypatch.setattr(gateway, "_request", fake_request)
    gateway.ensure_grid_capacity("facts", 6200, 30)
    assert calls == ["GET"]
