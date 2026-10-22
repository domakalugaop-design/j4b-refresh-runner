"""Google Sheets adapter for the ``report_reviewers`` tab.

The publication transaction itself lives in ``report_corrector_pipeline``:
it validates rows, snapshots the previous tab, writes, reads back, verifies
that the five protected tabs are untouched, and rolls back on any mismatch.

This module only supplies the transport callbacks, so it is fully testable
without a live spreadsheet and it never runs on import. Existing tabs are
never written: the target tab is created only when absent.
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from .report_corrector_pipeline import SHEET_COLUMNS, SHEET_NAME, publish_new_sheet

PROTECTED_TABS = (
    "projects_current",
    "project_types",
    "Статусы проектов",
    "Выплаты по визитам",
    "Выплаты по проектам",
)
COLUMN_LETTER = "A"
LAST_COLUMN_LETTER = "Z"


class SheetTabMissing(RuntimeError):
    """Raised by the transport when a tab does not exist (HTTP 404)."""


class SheetAdapterError(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"report_reviewers sheet adapter failed: {reason}")


@dataclass(frozen=True)
class SheetMetadata:
    titles: tuple[str, ...]
    sheet_ids: Mapping[str, int] = field(default_factory=dict)


def _quote(title: str) -> str:
    return urllib.parse.quote(f"'{title}'!{COLUMN_LETTER}1:{LAST_COLUMN_LETTER}", safe="!:'")


def _quote_values(title: str) -> str:
    return urllib.parse.quote(f"'{title}'!{COLUMN_LETTER}:{LAST_COLUMN_LETTER}", safe="!:'")


def normalize_rows(values: list[list[Any]] | None) -> list[list[str]]:
    """Normalize a Sheets read back to the 9-column string contract."""
    width = len(SHEET_COLUMNS)
    normalized: list[list[str]] = []
    for row in values or []:
        cells = ["" if cell is None else str(cell) for cell in row]
        cells += [""] * (width - len(cells))
        normalized.append(cells[:width])
    return normalized


def fingerprint(values: list[list[Any]] | None) -> str:
    payload = json.dumps(normalize_rows(values), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class SheetsGateway:
    """Read/write primitives for one spreadsheet, with injected HTTP transport."""

    def __init__(
        self,
        spreadsheet_id: str,
        *,
        get: Callable[[str], dict[str, Any]],
        mutate: Callable[[str, dict[str, Any]], Any],
        put_values: Callable[[str, list[list[str]]], Any] | None = None,
        allow_mutation: bool = True,
    ) -> None:
        if not spreadsheet_id.strip():
            raise SheetAdapterError("spreadsheet id is empty")
        self._sid = spreadsheet_id
        self._get = get
        self._mutate = mutate
        self._put = put_values
        self._allow_mutation = allow_mutation

    # -- reads ---------------------------------------------------------
    def metadata(self) -> SheetMetadata:
        payload = self._get(
            f"https://sheets.googleapis.com/v4/spreadsheets/{self._sid}"
            "?fields=spreadsheetId,sheets.properties(title,sheetId)"
        )
        sheets = payload.get("sheets") or []
        titles = []
        ids: dict[str, int] = {}
        for sheet in sheets:
            properties = (sheet or {}).get("properties") or {}
            title = properties.get("title")
            if not isinstance(title, str) or not title:
                raise SheetAdapterError("sheet metadata has no title")
            titles.append(title)
            sheet_id = properties.get("sheetId")
            if isinstance(sheet_id, int):
                ids[title] = sheet_id
        return SheetMetadata(titles=tuple(titles), sheet_ids=ids)

    def read(self, title: str) -> list[list[str]] | None:
        """Return tab values, or None when the tab does not exist."""
        try:
            payload = self._get(
                f"https://sheets.googleapis.com/v4/spreadsheets/{self._sid}/values/"
                f"{_quote(title)}?valueRenderOption=UNFORMATTED_VALUE"
            )
        except SheetTabMissing:
            return None
        return normalize_rows(payload.get("values"))

    def fingerprints(self) -> dict[str, str]:
        return {title: fingerprint(self.read(title)) for title in PROTECTED_TABS}

    # -- writes --------------------------------------------------------
    def create(self, title: str) -> None:
        self._guard()
        self._mutate(
            f"https://sheets.googleapis.com/v4/spreadsheets/{self._sid}:batchUpdate",
            {"requests": [{"addSheet": {"properties": {"title": title}}}]},
        )

    def write(self, title: str, rows: list[list[Any]]) -> None:
        self._guard()
        if self._put is None:
            raise SheetAdapterError("value writer is not configured")
        self._put(
            f"https://sheets.googleapis.com/v4/spreadsheets/{self._sid}/values/"
            f"{_quote_values(title)}?valueInputOption=RAW",
            [[str(cell) for cell in row] for row in rows],
        )

    def delete(self, title: str) -> None:
        self._guard()
        metadata = self.metadata()
        sheet_id = metadata.sheet_ids.get(title)
        if sheet_id is None:
            raise SheetAdapterError("target tab id is unknown")
        self._mutate(
            f"https://sheets.googleapis.com/v4/spreadsheets/{self._sid}:batchUpdate",
            {"requests": [{"deleteSheet": {"sheetId": sheet_id}}]},
        )

    def _guard(self) -> None:
        if not self._allow_mutation:
            raise SheetAdapterError("mutation is disabled in this mode")


def write_backup(backup_dir: str, payload: Mapping[str, Any]) -> bool:
    """Write a private, verified snapshot and confirm it reads back identically."""
    os.makedirs(backup_dir, mode=0o700, exist_ok=True)
    path = os.path.join(backup_dir, "report_reviewers_backup.json")
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        stream.write(body)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    os.chmod(path, 0o600)
    with open(path, encoding="utf-8") as stream:
        return stream.read() == body


def publish(
    rows: list[list[Any]], gateway: SheetsGateway, *, backup_dir: str,
) -> str:
    """Publish the tab through the transactional publisher; return the outcome."""
    metadata = gateway.metadata()
    read_sheet = lambda: gateway.read(SHEET_NAME)  # noqa: E731
    publish_new_sheet(
        rows,
        existing_titles=set(metadata.titles),
        read_sheet=read_sheet,
        create_sheet=lambda: gateway.create(SHEET_NAME),
        write_sheet=lambda payload: gateway.write(SHEET_NAME, payload),
        delete_sheet=lambda: gateway.delete(SHEET_NAME),
        save_backup=lambda payload: write_backup(backup_dir, payload),
        readback=read_sheet,
        non_target_fingerprints=gateway.fingerprints,
    )
    return "PUBLISHED"


def plan(rows: list[list[Any]], gateway: SheetsGateway) -> dict[str, Any]:
    """Describe what publication would do without mutating anything."""
    metadata = gateway.metadata()
    existing = SHEET_NAME in set(metadata.titles)
    missing = [title for title in PROTECTED_TABS if title not in set(metadata.titles)]
    return {
        "target_tab": SHEET_NAME,
        "target_exists": existing,
        "action": "REPLACE" if existing else "CREATE",
        "rows": len(rows) - 1,
        "columns": len(SHEET_COLUMNS),
        "protected_tabs_present": len(PROTECTED_TABS) - len(missing),
        "protected_tabs_missing": missing,
        "failure": "MISSING_PROTECTED_TABS" if missing else None,
    }
