"""Privacy-safe structural inspection of one project page."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urlparse

_ID_PATH = re.compile(r"/(action|report|visit|proj)/(\d+)(?:/|$)", re.I)
_DATE_HEADER = re.compile(r"date|дата|создан|заполн|отч[её]т", re.I)
_DATE_TYPES = (
    ("ISO_DATE", re.compile(r"^\d{4}-\d{2}-\d{2}$")),
    ("DMY_DATE", re.compile(r"^\d{2}\.\d{2}\.\d{4}$")),
    ("DMY_SLASH_DATE", re.compile(r"^\d{2}/\d{2}/\d{4}$")),
    ("DATETIME", re.compile(r"^\d{2}[./]\d{2}[./]\d{4}\s+\d{1,2}:\d{2}(?::\d{2})?$")),
)


def _path_template(raw_url: str, base_url: str) -> str | None:
    parsed = urlparse(raw_url)
    base = urlparse(base_url)
    if parsed.scheme and parsed.hostname != base.hostname:
        return None
    path = parsed.path or raw_url.split("?", 1)[0]
    path = re.sub(r"/\d+(?=/|$)", "/{id}", path)
    return path[:160]


def _date_type(value: str) -> str:
    value = " ".join(value.split())
    if not value:
        return "EMPTY"
    return next((name for name, pattern in _DATE_TYPES if pattern.fullmatch(value)), "OTHER")


class _ProjectPage(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.tables = 0
        self.rows = 0
        self.data_rows = 0
        self.ids = {key: set() for key in ("action", "report", "visit", "proj")}
        self.pagination = False
        self.headers: list[str] = []
        self.date_columns: dict[str, set[str]] = {}
        self.row_link_sets: list[set[str]] = []
        self._row: list[tuple[str, str]] | None = None
        self._cell: str | None = None
        self._cell_text: list[str] = []
        self._row_kinds: set[str] = set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        rel = (attrs.get("rel") or "").lower()
        aria = (attrs.get("aria-label") or "").lower()
        classes = (attrs.get("class") or "").lower()
        href_or_action = attrs.get("href") or attrs.get("action") or ""
        if ("next" in rel or "next" in aria or "pagination" in classes
                or re.search(r"(?:[?&](?:page|p)=\d+|/page/\d+)", href_or_action, re.I)):
            self.pagination = True
        if tag == "table":
            self.tables += 1
        elif tag == "tr":
            self._row = []
            self._row_kinds = set()
        elif tag in {"th", "td"} and self._row is not None:
            self._cell = tag
            self._cell_text = []
        elif tag in {"a", "form"}:
            key = "href" if tag == "a" else "action"
            raw = attrs.get(key) or ""
            if raw:
                route = _path_template(raw, self.base_url)
                if route is not None:
                    if tag == "a":
                        path = urlparse(raw).path or raw.split("?", 1)[0]
                        for kind, identifier in _ID_PATH.findall(path):
                            kind = kind.lower()
                            self.ids[kind].add(identifier)
                            self._row_kinds.add(kind)

    def handle_endtag(self, tag):
        if tag in {"th", "td"} and self._cell == tag and self._row is not None:
            # Text is discarded after classification; it is never returned.
            value = " ".join(" ".join(self._cell_text).split())
            self._row.append((tag, value))
            self._cell = None
            self._cell_text = []
        elif tag == "tr" and self._row is not None:
            self.rows += 1
            if self._row and all(kind == "th" for kind, _ in self._row):
                self.headers = [value[:100] for _, value in self._row]
            else:
                self.data_rows += 1
                self.row_link_sets.append(set(self._row_kinds))
                for index, (_, value) in enumerate(self._row):
                    header = self.headers[index] if index < len(self.headers) else ""
                    if _DATE_HEADER.search(header):
                        meaning = (
                            "REPORT_CANDIDATE" if re.search(r"отч[её]т|анкет|заполн", header, re.I)
                            else "VISIT_CANDIDATE" if re.search(r"визит", header, re.I)
                            else "GENERIC_DATE"
                        )
                        self.date_columns.setdefault(meaning, set()).add(_date_type(value))
            self._row = None
            self._row_kinds = set()

    def handle_data(self, data):
        if self._cell:
            self._cell_text.append(data[:256])


def inspect_project_page(html: str, *, base_url: str = "https://lk.j4b.ru") -> dict:
    parser = _ProjectPage(base_url)
    parser.feed(html)
    parser.close()
    return {
        "tables": parser.tables,
        "rows": parser.rows,
        "data_rows": parser.data_rows,
        "action_ids_count": len(parser.ids["action"]),
        "report_ids_count": len(parser.ids["report"]),
        "visit_ids_count": len(parser.ids["visit"]),
        "project_ids_count": len(parser.ids["proj"]),
        "rows_with_visit_and_action": sum(
            1 for kinds in parser.row_link_sets if {"visit", "action"} <= kinds
        ),
        "rows_with_visit_and_report": sum(
            1 for kinds in parser.row_link_sets if {"visit", "report"} <= kinds
        ),
        "date_column_classes": {
            key: tuple(sorted(value_types)) for key, value_types in sorted(parser.date_columns.items())
        },
        "pagination": parser.pagination,
    }
