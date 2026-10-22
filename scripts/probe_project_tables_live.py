"""Describe the /proj/{id}/report page: tables, period controls, field names.

Usage (on DFB):
    python3 probe_project_tables_live.py 7986 [project_id ...]
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import date
from html.parser import HTMLParser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from portal_transport import PortalSession  # noqa: E402

CREDENTIALS = "/home/maxis/j4b-refresh-deployment/credentials/portal-auth.json"
BASE_URL = "https://lk.j4b.ru"
TIMEOUT = 60
DATE_VALUE = re.compile(r"\b(\d{2}[./]\d{2}[./]\d{4})\b")
REPORT_LINK = re.compile(r"/report/(\d+)(?:/|$)", re.I)


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


def dmy(value: str) -> date | None:
    match = DATE_VALUE.search(value)
    if not match:
        return None
    day, month, year = (int(part) for part in re.split(r"[./]", match.group(1)))
    try:
        return date(year, month, year)
    except ValueError:
        return None


class _Tables(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[dict] = []
        self.fields: list[tuple[str, str, int]] = []
        self.period_links: dict[str, int] = {}
        self._depth = 0
        self._tbl: dict | None = None
        self._in_row = False
        self._cell: list[str] | None = None
        self._row_reports = 0
        self._row_dates: list[date] = []
        self._select_name: str | None = None
        self._select_options = 0

    def handle_starttag(self, tag, attrs):
        a = {str(k).casefold(): v for k, v in attrs}
        if tag in {"script", "style"}:
            self._depth += 1
            return
        if tag == "table":
            self._tbl = {"rows": 0, "report_rows": 0, "dates": [], "link_row_shape": []}
        elif tag == "tr" and self._tbl is not None:
            self._in_row = True
            self._row_reports = 0
            self._row_dates = []
        elif tag in {"td", "th"} and self._in_row:
            self._cell = []
        elif tag in {"input", "select", "textarea"}:
            name = (a.get("name") or "").strip()
            if name:
                self.fields.append((name[:40], (a.get("type") or tag).casefold()[:10], 0))
            if tag == "select":
                self._select_name = name[:40]
                self._select_options = 0
        elif tag == "option" and self._select_name is not None:
            self._select_options += 1
        if tag == "a":
            href = a.get("href") or ""
            if re.search(r"/proj/\d+/report(?:/|\?|$)", href, re.I):
                template = re.sub(r"\d+", "{id}", href)[:70]
                self.period_links[template] = self.period_links.get(template, 0) + 1
            if self._in_row:
                self._row_reports += len(REPORT_LINK.findall(href))

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self._depth:
            self._depth -= 1
        elif tag in {"td", "th"} and self._cell is not None:
            text = " ".join("".join(self._cell).split())
            found = DATE_VALUE.search(text)
            if found:
                parsed = dmy(found.group(0))
                if parsed:
                    self._row_dates.append(parsed)
            self._cell = None
        elif tag == "tr" and self._in_row and self._tbl is not None:
            self._in_row = False
            self._tbl["rows"] += 1
            if self._row_reports:
                self._tbl["report_rows"] += 1
            self._tbl["dates"].extend(self._row_dates)
            self._row_dates = []
        elif tag == "table" and self._tbl is not None:
            self.tables.append(self._tbl)
            self._tbl = None
        elif tag == "select" and self._select_name is not None:
            self.fields.append((self._select_name, "select", self._select_options))
            self._select_name = None

    def handle_data(self, data):
        if self._depth == 0 and self._cell is not None:
            self._cell.append(data[:200])


def main() -> int:
    project_ids = [value for value in sys.argv[1:] if value.isdigit()] or ["7986"]
    out: dict = {"projects": []}
    session = None
    try:
        with open(CREDENTIALS, encoding="utf-8") as stream:
            config = json.load(stream)
        session = PortalSession(
            timeout=TIMEOUT, base_url=BASE_URL,
            login=config["login"], password=config["password"],
        )
        quiet_login(session)
        for index, project_id in enumerate(project_ids[:3]):
            if index:
                time.sleep(1.5)
            status, ctype, body = session.request(f"/proj/{project_id}/report", "GET")
            entry: dict = {"project_id": project_id, "http_status": status, "bytes": len(body)}
            if status == 200:
                page = _Tables()
                page.feed(body.decode("utf-8", errors="replace"))
                page.close()
                entry["tables"] = [
                    {
                        "index": position,
                        "rows": table["rows"],
                        "report_rows": table["report_rows"],
                        "date_count": len(table["dates"]),
                        "date_min": str(min(table["dates"])) if table["dates"] else None,
                        "date_max": str(max(table["dates"])) if table["dates"] else None,
                    }
                    for position, table in enumerate(page.tables)
                ]
                entry["fields"] = [{"name": n, "type": t, "options": o} for n, t, o in page.fields[:14]]
                entry["period_links"] = dict(sorted(page.period_links.items(), key=lambda p: -p[1])[:6])
            out["projects"].append(entry)
        out["request_count"] = session.requests
        print(json.dumps(out, ensure_ascii=False, separators=(",", ":"))[:5500])
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
