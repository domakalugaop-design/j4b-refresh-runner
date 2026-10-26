"""Bounded, privacy-preserving diagnostics for /action search HTML."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urlparse

MAX_DIAGNOSTIC_CHARS = 2000
IDENTIFIER_RE = re.compile(r"/(action|visit|proj|report)/(\d+)(?:[/?#]|$)", re.I)
DATE_HEADER_RE = re.compile(r"date|дата|создан|врем", re.I)
DATE_FORMATS = (
    ("ISO_DATE", re.compile(r"^\d{4}-\d{2}-\d{2}$")),
    ("DMY_DATE", re.compile(r"^\d{2}\.\d{2}\.\d{4}$")),
    ("DMY_SLASH_DATE", re.compile(r"^\d{2}/\d{2}/\d{4}$")),
    ("DATETIME", re.compile(r"^\d{2}[./]\d{2}[./]\d{4}\s+\d{1,2}:\d{2}(?::\d{2})?$")),
)
TOTAL_RE = re.compile(r"(?:total|всего|найдено|из)\s*[:№]?\s*(\d{1,9})", re.I)
PAGE_RE = re.compile(r"(?:^|[?&])(?:page|p)=\d+", re.I)


@dataclass(frozen=True)
class ActionSearchSummary:
    http_status: int
    response_bytes: int
    final_url_class: str
    redirect_detected: str
    html_parsed: bool
    result_tables: int
    result_rows: int
    distinct_action_ids: int
    distinct_visit_ids: int
    distinct_project_ids: int
    report_links: int
    action_ids_sha256: str
    visit_ids_sha256: str
    project_ids_sha256: str
    date_columns: tuple[str, ...]
    date_data_types: tuple[str, ...]
    date_min: str
    date_max: str
    date_semantics: str
    pagination: str
    explicit_total_count: int | None
    configured_result_limit: int | None
    limit_reached: bool
    failure_category: str


@dataclass(frozen=True)
class ActionSearchAnalysis:
    summary: ActionSearchSummary
    action_fingerprints: frozenset[str]
    visit_fingerprints: frozenset[str]
    project_fingerprints: frozenset[str]


def _id_fingerprints(kind: str, values: set[str]) -> frozenset[str]:
    return frozenset(hashlib.sha256(f"{kind}:{value}".encode("ascii")).hexdigest() for value in values)


def _digest(values: set[str]) -> str:
    canonical = "\n".join(sorted(values, key=lambda value: (len(value), value)))
    return hashlib.sha256(canonical.encode("ascii")).hexdigest() if values else "EMPTY"


def _cell_type(value: str) -> str:
    value = " ".join(value.split())
    if not value:
        return "EMPTY"
    for kind, pattern in DATE_FORMATS:
        if pattern.fullmatch(value):
            return kind
    return "OTHER"


def _date_semantics(header: str) -> str:
    normalized = " ".join(header.casefold().split())
    if re.search(r"дата\s*(?:визита|посещения)|visit\s*date", normalized):
        return "VISIT_DATE"
    if re.search(r"дата\s*(?:отч[её]та|анкеты)|report\s*date", normalized):
        return "REPORT_DATE"
    if re.search(r"дата\s*(?:создания|добавления)|created\s*date", normalized):
        return "CREATED_DATE"
    return "UNCONFIRMED"


class _ActionDOM(HTMLParser):
    """Retains only IDs for hashing and date-cell format metadata."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.table_stack: list[dict] = []
        self.table_count = 0
        self.result_rows = 0
        self.ids = {kind: set() for kind in ("action", "visit", "proj", "report")}
        self.pagination = False
        self.total_count: int | None = None
        self.configured_limit: int | None = None
        self._cell_tag: str | None = None
        self._cell_text: list[str] = []
        self._current_row: list[tuple[str, str]] | None = None
        self._row_link_kinds: set[str] = set()
        self._headers: list[str] = []
        self._table_rows = 0
        self._date_types: set[str] = set()
        self._dates: list[datetime] = []
        self._date_columns: set[str] = set()
        self._date_meanings: set[str] = set()
        self._text_tail = ""
        self.parse_error = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        if tag == "table":
            self.table_count += 1
            self.table_stack.append({"rows": 0})
        elif tag == "tr" and self.table_stack:
            self._current_row = []
            self._row_link_kinds = set()
        elif tag in {"th", "td"} and self._current_row is not None:
            self._cell_tag = tag
            self._cell_text = []
        elif tag == "a":
            href = attrs_dict.get("href") or ""
            for kind, identifier in IDENTIFIER_RE.findall(href):
                normalized_kind = kind.lower()
                self.ids[normalized_kind].add(identifier)
                self._row_link_kinds.add(normalized_kind)
            rel = (attrs_dict.get("rel") or "").lower().split()
            if "next" in rel or PAGE_RE.search(href):
                self.pagination = True
            marker = " ".join((attrs_dict.get(key) or "") for key in ("class", "aria-label", "title"))
            if re.search(r"next|впер[её]д|следующ", marker, re.I):
                self.pagination = True
        elif tag in {"input", "button"}:
            name = attrs_dict.get("name") or ""
            value = attrs_dict.get("value") or ""
            if name.lower() == "limit" and value.isdigit():
                self.configured_limit = int(value)
            marker = " ".join((name, attrs_dict.get("id") or "", attrs_dict.get("class") or "", value))
            if re.search(r"next|впер[её]д|следующ", marker, re.I):
                self.pagination = True

    def handle_endtag(self, tag: str) -> None:
        if tag in {"th", "td"} and self._cell_tag == tag and self._current_row is not None:
            value = " ".join(" ".join(self._cell_text).split())
            self._current_row.append((tag, value))
            self._cell_tag = None
            self._cell_text = []
        elif tag == "tr" and self._current_row is not None:
            if self.table_stack:
                self.table_stack[-1]["rows"] += 1
            cells = self._current_row
            if cells and all(kind == "th" for kind, _ in cells):
                self._headers = [value[:120] for _, value in cells]
            elif cells:
                if self._row_link_kinds.intersection({"action", "visit", "proj"}):
                    self.result_rows += 1
                for index, (_, value) in enumerate(cells):
                    header = self._headers[index] if index < len(self._headers) else ""
                    if DATE_HEADER_RE.search(header):
                        kind = _cell_type(value)
                        self._date_types.add(kind)
                        self._date_columns.add(header[:120] or f"COLUMN_{index + 1}")
                        meaning = _date_semantics(header)
                        self._date_meanings.add(meaning)
                        if meaning != "UNCONFIRMED":
                            parsed = self._parse_date(value)
                            if parsed is not None:
                                self._dates.append(parsed)
            self._current_row = None
            self._row_link_kinds = set()
        elif tag == "table" and self.table_stack:
            self.table_stack.pop()

    def handle_data(self, data: str) -> None:
        if self._cell_tag:
            # Cell text is transiently held only until </th> or </td> and never emitted.
            self._cell_text.append(data[:512])
        self._text_tail = (self._text_tail + data)[-256:]
        match = TOTAL_RE.search(self._text_tail)
        if match:
            self.total_count = int(match.group(1))

    @staticmethod
    def _parse_date(value: str) -> datetime | None:
        for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%d.%m.%Y %H:%M", "%d.%m.%Y %H:%M:%S"):
            try:
                return datetime.strptime(" ".join(value.split()), fmt)
            except ValueError:
                continue
        return None

    def close(self) -> None:
        try:
            super().close()
        except Exception:
            self.parse_error = True


def analyze_action_response(
    *, http_status: int, content_type: str, body: bytes, final_url: str | None,
    expected_host: str, limit: int = 10000,
) -> ActionSearchAnalysis:
    parsed_url = urlparse(final_url or "")
    host = (parsed_url.hostname or "").lower()
    path = parsed_url.path.rstrip("/") or "/"
    if host != expected_host.lower():
        final_url_class, redirected, url_failure = "OTHER_HOST", "YES", "UNEXPECTED_FINAL_URL"
    elif path == "/action":
        final_url_class, redirected, url_failure = "ACTION", "UNKNOWN", "NONE"
    elif path == "/" or re.search(r"login|auth", path, re.I):
        final_url_class, redirected, url_failure = "AUTH", "YES", "AUTH_REDIRECT"
    else:
        final_url_class, redirected, url_failure = "OTHER_PATH", "YES", "UNEXPECTED_FINAL_URL"
    empty = ActionSearchSummary(
        http_status=int(http_status) if str(http_status).isdigit() else 0,
        response_bytes=len(body), final_url_class=final_url_class,
        redirect_detected=redirected, html_parsed=False, result_tables=0, result_rows=0,
        distinct_action_ids=0, distinct_visit_ids=0, distinct_project_ids=0,
        report_links=0, action_ids_sha256="EMPTY", visit_ids_sha256="EMPTY",
        project_ids_sha256="EMPTY", date_columns=(), date_data_types=(), date_min="UNKNOWN",
        date_max="UNKNOWN", date_semantics="UNCONFIRMED", pagination="UNKNOWN",
        explicit_total_count=None, configured_result_limit=None, limit_reached=False,
        failure_category="NONE",
    )
    if url_failure != "NONE":
        return _analysis(_replace(empty, failure_category=url_failure))
    if int(http_status) != 200:
        return _analysis(_replace(empty, failure_category="HTTP_ERROR"))
    if not body:
        return _analysis(_replace(empty, failure_category="EMPTY_RESPONSE"))
    if "html" not in content_type.lower():
        return _analysis(_replace(empty, failure_category="NON_HTML_RESPONSE"))
    try:
        parser = _ActionDOM()
        parser.feed(body.decode("utf-8"))
        parser.close()
    except Exception:
        return _analysis(_replace(empty, failure_category="HTML_PARSE_ERROR"))
    if parser.parse_error:
        return _analysis(_replace(empty, failure_category="HTML_PARSE_ERROR"))
    date_semantics = next(iter(parser._date_meanings)) if len(parser._date_meanings) == 1 else "UNCONFIRMED"
    date_min = min(parser._dates).isoformat(sep=" ") if parser._dates and date_semantics != "UNCONFIRMED" else "UNKNOWN"
    date_max = max(parser._dates).isoformat(sep=" ") if parser._dates and date_semantics != "UNCONFIRMED" else "UNKNOWN"
    rows = parser.result_rows
    action_fingerprints = _id_fingerprints("action", parser.ids["action"])
    visit_fingerprints = _id_fingerprints("visit", parser.ids["visit"])
    project_fingerprints = _id_fingerprints("project", parser.ids["proj"])
    summary = ActionSearchSummary(
        **{
            **asdict(empty), "html_parsed": True, "result_tables": parser.table_count,
            "result_rows": rows, "distinct_action_ids": len(parser.ids["action"]),
            "distinct_visit_ids": len(parser.ids["visit"]),
            "distinct_project_ids": len(parser.ids["proj"]), "report_links": len(parser.ids["report"]),
            "action_ids_sha256": _digest(set(action_fingerprints)),
            "visit_ids_sha256": _digest(set(visit_fingerprints)),
            "project_ids_sha256": _digest(set(project_fingerprints)),
            "date_columns": tuple(sorted(name[:64] for name in parser._date_columns)[:6]),
            "date_data_types": tuple(sorted(parser._date_types)),
            "date_min": date_min, "date_max": date_max, "date_semantics": date_semantics,
            "pagination": "YES" if parser.pagination else "NO",
            "explicit_total_count": parser.total_count,
            "configured_result_limit": parser.configured_limit,
            "limit_reached": bool(limit and rows >= limit),
        }
    )
    return ActionSearchAnalysis(summary, action_fingerprints, visit_fingerprints, project_fingerprints)


def summarize_action_response(**kwargs) -> ActionSearchSummary:
    """Compatibility wrapper that returns only aggregate summary fields."""
    return analyze_action_response(**kwargs).summary


def _analysis(summary: ActionSearchSummary) -> ActionSearchAnalysis:
    return ActionSearchAnalysis(summary, frozenset(), frozenset(), frozenset())


def render_diagnostic(summary: ActionSearchSummary) -> str:
    return _bounded_json(_summary_payload(summary))


def render_comparison(first: ActionSearchAnalysis, second: ActionSearchAnalysis) -> str:
    action_intersection = first.action_fingerprints & second.action_fingerprints
    action_union = first.action_fingerprints | second.action_fingerprints
    visit_intersection = first.visit_fingerprints & second.visit_fingerprints
    if first.action_fingerprints == second.action_fingerprints:
        subset_relation = "EQUAL"
        behavior = "EQUAL_SETS"
    elif first.action_fingerprints <= second.action_fingerprints:
        subset_relation = "DAY1_SUBSET_OF_DAY2"
        behavior = "CUMULATIVE_NESTED"
    elif second.action_fingerprints <= first.action_fingerprints:
        subset_relation = "DAY2_SUBSET_OF_DAY1"
        behavior = "CUMULATIVE_NESTED"
    elif not action_intersection:
        subset_relation = "DISJOINT"
        behavior = "DISJOINT_DAILY_SLICES"
    else:
        subset_relation = "OVERLAPPING_NON_NESTED"
        behavior = "OVERLAPPING_NON_NESTED"
    result = {
        "day1": _summary_payload(first.summary), "day2": _summary_payload(second.summary),
        "action_intersection": len(action_intersection),
        "action_only_day1": len(first.action_fingerprints - second.action_fingerprints),
        "action_only_day2": len(second.action_fingerprints - first.action_fingerprints),
        "action_subset_relation": subset_relation,
        "action_jaccard": round(len(action_intersection) / len(action_union), 6) if action_union else 1.0,
        "visit_intersection": len(visit_intersection),
        "date_filter_behavior": behavior,
    }
    return _bounded_json(result)


def render_single(analysis: ActionSearchAnalysis) -> str:
    return _bounded_json({"day1": _summary_payload(analysis.summary), "failure_category": analysis.summary.failure_category})


def _summary_payload(summary: ActionSearchSummary) -> dict:
    values = asdict(summary)
    # Whole-set fingerprints stay internal too; only overlap counts are reported.
    for key in ("action_ids_sha256", "visit_ids_sha256", "project_ids_sha256"):
        values.pop(key, None)
    return values


def _bounded_json(value: dict) -> str:
    rendered = json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    return rendered if len(rendered) <= MAX_DIAGNOSTIC_CHARS else '{"failure_category":"DIAGNOSTIC_OUTPUT_LIMIT"}'


def _replace(summary: ActionSearchSummary, **changes: object) -> ActionSearchSummary:
    values = asdict(summary)
    values.update(changes)
    return ActionSearchSummary(**values)
