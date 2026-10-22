"""Resolve which portal route renders one report, and how report links to action.

Aggregate/structural output only: route names, counts, titles, field names.
No credentials, cookies, option text, or free-form record values are printed.

Usage (on DFB):
    python3 probe_report_route_live.py listing 7986
    python3 probe_report_route_live.py object <id>
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from html.parser import HTMLParser
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from portal_transport import PortalSession  # noqa: E402
from report_inventory import extract_report_inventory  # noqa: E402

CREDENTIALS = "/home/maxis/j4b-refresh-deployment/credentials/portal-auth.json"
BASE_URL = "https://lk.j4b.ru"
REQUEST_INTERVAL = 1.5
TIMEOUT = 45
_ID_LINK = re.compile(r"/(report|action|visit|proj)/(\d+)(?:/|$)", re.I)
_TITLE_OK = re.compile(r"[0-9A-Za-zА-Яа-яЁё №\-–—.,:;()/«»\"' ]+$")


def quiet_login(session: PortalSession) -> None:
    saved_out, saved_err = os.dup(1), os.dup(2)
    null_fd = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(null_fd, 1)
        os.dup2(null_fd, 2)
        session.login()
    finally:
        os.dup2(saved_out, 1)
        os.dup2(saved_err, 2)
        os.close(saved_out)
        os.close(saved_err)
        os.close(null_fd)


class _Shape(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables = 0
        self.rows = 0
        self.templates: dict[str, int] = {}
        self.linked: dict[str, set[str]] = {}
        self.fields: set[str] = set()
        self.selects: dict[str, int] = {}
        self.staff_options = 0
        self.staff_selected = False
        self.staff_numeric = None
        self.option_text_lengths: list[int] = []
        self._in_staff = False
        self._sel_name: str | None = None
        self._opt_selected = False
        self._opt_value: str | None = None
        self._opt_text: list[str] | None = None
        self._title: list[str] | None = None
        self._in_title = False
        self._row_open = False
        self._cells: list[str] = []
        self._cell: list[str] | None = None
        self._label_checked: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = {str(k).casefold(): v for k, v in attrs}
        if tag == "title":
            self._in_title = True
            self._title = []
        elif tag == "table":
            self.tables += 1
        elif tag == "tr":
            self._row_open = True
            self._cells = []
        elif tag in {"th", "td"} and self._row_open:
            self._cell = []
        elif tag in {"input", "select", "textarea"}:
            name = (a.get("name") or "").strip()
            if name:
                self.fields.add(name[:60])
                if tag == "select":
                    self.selects[name[:60]] = self.selects.get(name[:60], 0) + 1
            if tag == "select" and name.casefold() == "staff":
                self._in_staff = True
                self._sel_name = name
        elif tag == "option":
            if self._in_staff:
                self.staff_options += 1
                self._opt_text = []
            self._opt_selected = "selected" in a
            self._opt_value = a.get("value")

    def handle_endtag(self, tag):
        if tag == "title" and self._in_title:
            self._in_title = False
        elif tag in {"th", "td"} and self._cell is not None:
            self._cells.append(" ".join("".join(self._cell).split())[:120])
            self._cell = None
        elif tag == "option":
            if self._in_staff and self._opt_text is not None:
                self.option_text_lengths.append(len(" ".join("".join(self._opt_text).split())))
                if self._opt_selected and not self.staff_selected:
                    self.staff_selected = True
                    value = (self._opt_value or "").strip()
                    self.staff_numeric = bool(value) and value.isdigit()
                self._opt_text = None
        elif tag == "select" and self._in_staff:
            self._in_staff = False
        elif tag == "tr" and self._row_open:
            if self._cells:
                self.rows += 1
            self._row_open = False
            self._cells = []

    def handle_data(self, data):
        if self._in_title and self._title is not None:
            self._title.append(data)
        if self._cell is not None:
            self._cell.append(data[:200])
        if self._opt_text is not None and self._in_staff:
            self._opt_text.append(data[:200])

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    @property
    def title(self) -> str:
        raw = " ".join("".join(self._title or []).split())[:80]
        return raw if raw and _TITLE_OK.fullmatch(raw) else ("<nontextual>" if raw else "")


class _Links(HTMLParser):
    def __init__(self, base_host: str | None) -> None:
        super().__init__(convert_charrefs=True)
        self.host = base_host
        self.templates: dict[str, int] = {}
        self.linked: dict[str, set[str]] = {}

    def handle_starttag(self, tag, attrs):
        if tag not in {"a", "form"}:
            return
        a = {str(k).casefold(): v for k, v in attrs}
        raw = a.get("href" if tag == "a" else "action") or ""
        if not raw:
            return
        parsed = urlparse(raw)
        if parsed.scheme and parsed.hostname != self.host:
            return
        path = parsed.path or raw.split("?", 1)[0]
        if not path.startswith("/"):
            return
        template = re.sub(r"/\d+(?=/|$)", "/{id}", path)[:80]
        self.templates[template] = self.templates.get(template, 0) + 1
        for kind, identifier in _ID_LINK.findall(path):
            self.linked.setdefault(kind.lower(), set()).add(identifier)


def shape_of(body: bytes) -> dict:
    page = _Shape()
    page.feed(body.decode("utf-8", errors="replace"))
    page.close()
    links = _Links(urlparse(BASE_URL).hostname)
    links.feed(body.decode("utf-8", errors="replace"))
    links.close()
    return {
        "tables": page.tables,
        "rows": page.rows,
        "title": page.title,
        "fields": sorted(page.fields)[:20],
        "selects": dict(sorted(page.selects.items())[:12]),
        "link_templates": dict(sorted(links.templates.items(), key=lambda p: -p[1])[:8]),
        "linked_ids": {k: len(v) for k, v in sorted(links.linked.items())},
        "staff": {
            "present": "staff" in page.fields,
            "options": page.staff_options,
            "selected": page.staff_selected,
            "selected_numeric": page.staff_numeric,
            "option_text_lengths_min": min(page.option_text_lengths) if page.option_text_lengths else None,
        },
    }


def fetch(session: PortalSession, path: str) -> dict:
    status, ctype, body = session.request(path, "GET")
    entry = {
        "path": re.sub(r"/\d+", "/{id}", path),
        "http_status": status,
        "is_html": "html" in ctype.lower(),
        "bytes": len(body),
        "effective_path": re.sub(r"/\d+", "/{id}", urlparse(session.last_effective_url or "").path or "")[:80],
    }
    if status == 200 and "html" in ctype.lower():
        entry.update(shape_of(body))
    return entry


def main() -> int:
    if len(sys.argv) < 3:
        print('{"failure_category":"BAD_ARGUMENT"}')
        return 0
    stage, target = sys.argv[1], sys.argv[2]
    out: dict = {"stage": stage, "target": target}
    session = None
    try:
        with open(CREDENTIALS, encoding="utf-8") as stream:
            config = json.load(stream)
        session = PortalSession(
            timeout=TIMEOUT, base_url=BASE_URL,
            login=config["login"], password=config["password"],
        )
        quiet_login(session)

        if stage == "listing":
            status, ctype, body = session.request(f"/proj/{target}/report", "GET")
            page = extract_report_inventory(body.decode("utf-8", errors="replace"), base_url=BASE_URL)
            shape = shape_of(body)
            out["listing"] = {
                "http_status": status,
                "rows_with_report": len(page.rows),
                "unique_report_ids": len(page.report_ids),
                "templates": shape["link_templates"],
                "linked_ids": shape["linked_ids"],
                "title": shape["title"],
                "rows_with_multiple_report_ids": sum(
                    1 for row in page.rows if len(row.ids.get("report", ())) > 1
                ),
                "sample_report_ids": list(page.report_ids[:3]),
            }
        elif stage == "object":
            out["objects"] = []
            for path in (f"/report/{target}", f"/action/{target}", f"/visit/{target}"):
                time.sleep(REQUEST_INTERVAL)
                out["objects"].append(fetch(session, path))
        out["request_count"] = session.requests
        rendered = json.dumps(out, ensure_ascii=True, separators=(",", ":"))
        print(rendered if len(rendered) <= 5000 else '{"failure_category":"SUMMARY_LIMIT"}')
        return 0
    except Exception as exc:
        out["failure_category"] = "SAFE_PROBE_ERROR"
        out["error_class"] = type(exc).__name__
        print(json.dumps(out, ensure_ascii=True, separators=(",", ":")))
        return 0
    finally:
        if session is not None:
            session.close()


if __name__ == "__main__":
    raise SystemExit(main())
