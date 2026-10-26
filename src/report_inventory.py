"""Structural extraction of report inventory rows from a project report list.

The project report list (``/proj/{id}/report``) is the confirmed source of
report identifiers. This module extracts *structure* only: identifiers, which
identifier kinds co-occur in a row, and which columns look like dates. It never
returns free-form personal text, credentials, or raw markup.

Nothing here assumes that a report id equals an action id; the join is taken
from what the row actually links to, and unresolved rows are reported rather
than guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urlparse

ID_KINDS = ("report", "action", "visit", "proj")
_ID_PATH = re.compile(r"/(report|action|visit|proj)/(\d+)(?:/|$)", re.I)
_DATE_VALUE_PATTERNS = (
    ("ISO_DATE", re.compile(r"^\d{4}-\d{2}-\d{2}$")),
    ("DMY_DATE", re.compile(r"^\d{2}\.\d{2}\.\d{4}$")),
    ("DMY_SLASH_DATE", re.compile(r"^\d{2}/\d{2}/\d{4}$")),
    ("DMY_DATETIME", re.compile(r"^\d{2}[./]\d{2}[./]\d{4}\s+\d{1,2}:\d{2}(?::\d{2})?$")),
    ("ISO_DATETIME", re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?$")),
)


class ReportInventoryParseError(ValueError):
    """Structural error with no portal text in the message."""


@dataclass(frozen=True)
class ReportInventoryRow:
    """One report row, with only identifiers and coarse date typing kept."""

    report_id: str
    project_id: str
    ids: dict[str, tuple[str, ...]] = field(default_factory=dict)
    date_cells: tuple[tuple[str, str], ...] = ()

    def single(self, kind: str) -> str | None:
        """Return the row's value for ``kind`` only when it is unambiguous."""
        values = self.ids.get(kind, ())
        return values[0] if len(values) == 1 else None


@dataclass(frozen=True)
class ReportInventoryPage:
    rows: tuple[ReportInventoryRow, ...]
    tables: int
    header_labels: tuple[str, ...]
    date_column_classes: dict[str, tuple[str, ...]]
    pagination: bool
    project_ids: tuple[str, ...]

    @property
    def report_ids(self) -> tuple[str, ...]:
        return tuple(sorted({row.report_id for row in self.rows}, key=int))

    def join_profile(self) -> dict[str, int]:
        profile = {"rows": len(self.rows), "rows_without_action": 0, "rows_without_visit": 0}
        for row in self.rows:
            if row.single("action") is None:
                profile["rows_without_action"] += 1
            if row.single("visit") is None:
                profile["rows_without_visit"] += 1
        return profile


def date_value_type(value: str) -> str:
    text = " ".join(value.split())
    if not text:
        return "EMPTY"
    for name, pattern in _DATE_VALUE_PATTERNS:
        if pattern.fullmatch(text):
            return name
    return "OTHER"


def _path_template(raw: str, base_host: str | None) -> str | None:
    parsed = urlparse(raw)
    if parsed.scheme and parsed.hostname != base_host:
        return None
    path = parsed.path or raw.split("?", 1)[0]
    if not path.startswith("/"):
        return None
    return re.sub(r"/\d+(?=/|$)", "/{id}", path)[:120]


class _ReportListParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_host = urlparse(base_url).hostname
        self.tables = 0
        self.pagination = False
        self.header_labels: list[str] = []
        self.date_column_classes: dict[str, set[str]] = {}
        self.project_ids: set[str] = set()
        self._row: list[str] | None = None
        self._row_ids: dict[str, set[str]] = {}
        self._cell_text: list[str] | None = None
        self._cells: list[tuple[str, ...]] = []
        self._rows: list[ReportInventoryRow] = []
        self._headers: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {str(key).casefold(): value for key, value in attrs}
        if tag == "table":
            self.tables += 1
        elif tag == "tr":
            self._row = []
            self._row_ids = {kind: set() for kind in ID_KINDS}
            self._cells = []
        elif tag in {"th", "td"} and self._row is not None:
            self._cell_text = []
        elif tag in {"a", "form"}:
            raw = attributes.get("href" if tag == "a" else "action") or ""
            if not raw:
                return
            rel = (attributes.get("rel") or "").lower()
            aria = (attributes.get("aria-label") or "").lower()
            if (
                "next" in rel
                or "next" in aria
                or re.search(r"(?:[?&](?:page|p)=\d+|/page/\d+)", raw, re.I)
            ):
                self.pagination = True
            # Same-origin links only; external hosts must not contribute IDs.
            if _path_template(raw, self.base_host) is None:
                return
            path = urlparse(raw).path or raw.split("?", 1)[0]
            for kind, identifier in _ID_PATH.findall(path):
                kind = kind.lower()
                if self._row_ids is not None and self._row is not None:
                    self._row_ids[kind].add(identifier)
                if kind == "proj":
                    self.project_ids.add(identifier)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"th", "td"} and self._cell_text is not None and self._row is not None:
            value = " ".join("".join(self._cell_text).split())
            self._cells.append((tag, value))
            self._cell_text = None
        elif tag == "tr" and self._row is not None:
            self._finish_row()
        # A table end also closes an unterminated final row.
        elif tag == "table" and self._row is not None:
            self._finish_row()

    def handle_data(self, data: str) -> None:
        if self._cell_text is not None:
            self._cell_text.append(data[:256])

    def _finish_row(self) -> None:
        cells = self._cells
        row_ids = self._row_ids
        self._row = None
        self._row_ids = {}
        self._cells = []
        if not cells:
            return
        if all(kind == "th" for kind, _ in cells):
            self._headers = [value[:100] for _, value in cells]
            if not self.header_labels:
                self.header_labels = self._headers
            return
        date_cells: list[tuple[str, str]] = []
        for index, (_, value) in enumerate(cells):
            header = self._headers[index] if index < len(self._headers) else ""
            value_type = date_value_type(value)
            if value_type == "OTHER":
                continue
            date_cells.append((header[:60], value_type))
            meaning = (
                "REPORT" if re.search(r"отч[её]т|анкет|заполн|создан", header, re.I)
                else "VISIT" if re.search(r"визит", header, re.I)
                else "CHECK" if re.search(r"провер", header, re.I)
                else "GENERIC"
            )
            self.date_column_classes.setdefault(meaning, set()).add(value_type)
        reports = row_ids.get("report", set())
        if not reports:
            return
        if len(reports) > 1:
            raise ReportInventoryParseError("row links to multiple reports")
        self._rows.append(
            ReportInventoryRow(
                report_id=next(iter(reports)),
                project_id=next(iter(row_ids.get("proj", set())), "") or "",
                ids={kind: tuple(sorted(values, key=int)) for kind, values in row_ids.items()},
                date_cells=tuple(date_cells),
            )
        )


def extract_report_inventory(
    html: str, *, base_url: str = "https://lk.j4b.ru"
) -> ReportInventoryPage:
    """Extract report inventory structure without returning free-form text."""
    if not isinstance(html, str) or not html.strip():
        raise ReportInventoryParseError("report list HTML is empty")
    parser = _ReportListParser(base_url)
    try:
        parser.feed(html)
        parser.close()
    except ReportInventoryParseError:
        raise
    except Exception:
        raise ReportInventoryParseError("report list HTML is malformed") from None
    if parser._row is not None:
        parser._finish_row()
    return ReportInventoryPage(
        rows=tuple(parser._rows),
        tables=parser.tables,
        header_labels=tuple(parser.header_labels),
        date_column_classes={
            meaning: tuple(sorted(types)) for meaning, types in sorted(parser.date_column_classes.items())
        },
        pagination=parser.pagination,
        project_ids=tuple(sorted(parser.project_ids, key=int)),
    )
