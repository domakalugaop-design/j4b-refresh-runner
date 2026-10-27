"""Google Sheets API v4 transport for the separate controller-quality workbook."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterable

from .controller_quality_mart import MartError


class SheetsTransportError(RuntimeError):
    """Sanitized Sheets transport error; response bodies are never retained."""


class GoogleSheetsGateway:
    def __init__(self, spreadsheet_id: str, token: str, *, timeout: float = 60) -> None:
        if not spreadsheet_id or not token:
            raise MartError("spreadsheet id or Google authentication is missing")
        self.spreadsheet_id = spreadsheet_id
        self._token = token
        self.timeout = timeout

    def _request(self, method: str, url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url, data=body, method=method,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            # Never surface the Google error body; it can contain request details.
            raise SheetsTransportError(f"google_sheets_http_{error.code}") from None
        except Exception as error:
            name = type(error).__name__.casefold()
            category = "timeout" if "timeout" in name else "network_or_protocol"
            raise SheetsTransportError(f"google_sheets_{category}") from None

    def titles(self) -> tuple[str, ...]:
        url = (
            f"https://sheets.googleapis.com/v4/spreadsheets/{self.spreadsheet_id}"
            "?fields=sheets.properties(title)"
        )
        payload = self._request("GET", url)
        result = []
        for sheet in payload.get("sheets") or []:
            title = ((sheet or {}).get("properties") or {}).get("title")
            if not isinstance(title, str) or not title:
                raise MartError("Google Sheets metadata schema mismatch")
            result.append(title)
        return tuple(result)

    @staticmethod
    def _range(title: str, cell_range: str) -> str:
        escaped = title.replace("'", "''")
        return urllib.parse.quote(f"'{escaped}'!{cell_range}", safe="!:$'")

    def read_range(self, title: str, cell_range: str) -> list[list[Any]]:
        url = (
            f"https://sheets.googleapis.com/v4/spreadsheets/{self.spreadsheet_id}/values/"
            f"{self._range(title, cell_range)}?valueRenderOption=UNFORMATTED_VALUE"
        )
        payload = self._request("GET", url)
        return payload.get("values") or []

    def read(self, title: str) -> list[list[Any]]:
        return self.read_range(title, "A:Z")

    def update(self, title: str, cell_range: str, rows: Iterable[Iterable[Any]]) -> None:
        url = (
            f"https://sheets.googleapis.com/v4/spreadsheets/{self.spreadsheet_id}/values/"
            f"{self._range(title, cell_range)}?valueInputOption=RAW"
        )
        self._request("PUT", url, {"values": [list(row) for row in rows]})

    def add_sheet(self, title: str) -> None:
        """Create a new tab; refuse when an equivalent tab already exists."""
        if title in self.titles():
            raise MartError(f"tab already exists: {title}")
        url = (
            f"https://sheets.googleapis.com/v4/spreadsheets/{self.spreadsheet_id}"
            ":batchUpdate"
        )
        self._request("POST", url, {"requests": [{"addSheet": {"properties": {"title": title}}}]})

    def ensure_grid_capacity(self, title: str, min_rows: int, min_columns: int) -> None:
        """Grow a sheet's grid when append-only data exceeds its current bounds."""
        url = (
            f"https://sheets.googleapis.com/v4/spreadsheets/{self.spreadsheet_id}"
            "?fields=sheets.properties(sheetId,title,gridProperties(rowCount,columnCount))"
        )
        payload = self._request("GET", url)
        target = None
        for sheet in payload.get("sheets") or []:
            props = (sheet or {}).get("properties") or {}
            if props.get("title") == title:
                target = props
                break
        if target is None:
            raise MartError(f"sheet not found while checking grid capacity: {title}")
        grid = target.get("gridProperties") or {}
        current_rows = int(grid.get("rowCount", 1000))
        current_columns = int(grid.get("columnCount", 26))
        new_rows = max(current_rows, int(min_rows))
        new_columns = max(current_columns, int(min_columns))
        if new_rows == current_rows and new_columns == current_columns:
            return
        self._request(
            "POST",
            f"https://sheets.googleapis.com/v4/spreadsheets/{self.spreadsheet_id}:batchUpdate",
            {"requests": [{
                "updateSheetProperties": {
                    "properties": {
                        "sheetId": int(target["sheetId"]),
                        "gridProperties": {"rowCount": new_rows, "columnCount": new_columns},
                    },
                    "fields": "gridProperties.rowCount,gridProperties.columnCount",
                }
            }]},
        )

    def ensure_headers(self, title: str, header: list[str]) -> None:
        """Write the header row only when the tab is empty."""
        existing = self.read_range(title, "A1:Z1")
        if existing and existing[0]:
            raise MartError(f"tab is not empty: {title}")
        self.update(title, "A1", [header])


def read_report_reviewer_source(gateway: GoogleSheetsGateway) -> list[dict[str, str]]:
    """Read the source tab and enforce its exact header before mapping rows."""
    rows = gateway.read("report_reviewers")
    expected = (
        "report_id", "action_id", "visit_id", "project_id", "report_date",
        "corrector_id", "corrector_fio", "source_status", "last_successful_fetch",
    )
    if not rows or tuple(str(value) for value in rows[0]) != expected:
        raise MartError("report_reviewers source header mismatch")
    result = []
    for row in rows[1:]:
        padded = list(row) + [""] * (len(expected) - len(row))
        result.append(dict(zip(expected, padded[:len(expected)])))
    return result
