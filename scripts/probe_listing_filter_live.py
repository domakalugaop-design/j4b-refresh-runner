"""Test whether the project report list honours dt1/dt2 filters.

Compares baseline vs filtered listing: report counts, ID ranges, date ranges.
Prints no cell text.

Usage (on DFB):
    python3 probe_listing_filter_live.py <project_id> "<query1>" ["<query2>" ...]
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import date
from html.parser import HTMLParser
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from portal_transport import PortalSession  # noqa: E402

CREDENTIALS = "/home/maxis/j4b-refresh-deployment/credentials/portal-auth.json"
BASE_URL = "https://lk.j4b.ru"
TIMEOUT = 45
DATE_VALUE = re.compile(r"\d{2}[./]\d{2}[./]\d{4}")
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


def parse_dmy(value: str) -> date | None:
    match = re.match(r"(\d{2})[./](\d{2})[./](\d{4})", value)
    if not match:
        return None
    day, month, year = (int(part) for part in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


class _Scan(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.report_ids: set[str] = set()
        self.dates: dict[int, list[date]] = {}
        self._depth = 0
        self._cell_index = -1
        self._cell_text: list[str] | None = None
        self._row_cells = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self._depth += 1
        elif tag == "tr":
            self._cell_index = -1
            self._row_cells = 0
        elif tag in {"td", "th"}:
            self._cell_index += 1
            self._cell_text = []
        if tag == "a":
            href = dict(attrs).get("href") or ""
            for identifier in REPORT_LINK.findall(href):
                self.report_ids.add(identifier)

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self._depth:
            self._depth -= 1
        elif tag in {"td", "th"} and self._cell_text is not None:
            text = " ".join("".join(self._cell_text).split())
            found = DATE_VALUE.search(text)
            if found:
                parsed = parse_dmy(found.group(0))
                if parsed:
                    self.dates.setdefault(self._cell_index, []).append(parsed)
            self._cell_text = None

    def handle_data(self, data):
        if self._depth == 0 and self._cell_text is not None:
            self._cell_text.append(data[:200])

    def summary(self) -> dict:
        ids = sorted(int(value) for value in self.report_ids)
        best = max(self.dates.items(), key=lambda pair: len(pair[1])) if self.dates else (None, [])
        return {
            "unique_report_ids": len(ids),
            "report_id_min": ids[0] if ids else None,
            "report_id_max": ids[-1] if ids else None,
            "dominant_date_column": best[0],
            "dominant_date_count": len(best[1]),
            "dominant_min": str(min(best[1])) if best[1] else None,
            "dominant_max": str(max(best[1])) if best[1] else None,
            "october_dates": sum(1 for value in best[1] if value >= date(2026, 10, 1)),
        }


def main() -> int:
    if len(sys.argv) < 3:
        print('{"failure_category":"BAD_ARGUMENT"}')
        return 0
    project_id = sys.argv[1]
    queries = sys.argv[2:]
    out: dict = {"project_id": project_id, "variants": []}
    session = None
    try:
        with open(CREDENTIALS, encoding="utf-8") as stream:
            config = json.load(stream)
        session = PortalSession(
            timeout=TIMEOUT, base_url=BASE_URL,
            login=config["login"], password=config["password"],
        )
        quiet_login(session)
        for index, query in enumerate(queries[:4]):
            if index:
                time.sleep(1.5)
            path = f"/proj/{project_id}/report"
            if query and query != "baseline":
                path += "?" + query
            status, ctype, body = session.request(path, "GET")
            entry = {"variant": query, "http_status": status, "bytes": len(body)}
            if status == 200:
                scan = _Scan()
                scan.feed(body.decode("utf-8", errors="replace"))
                scan.close()
                entry.update(scan.summary())
            out["variants"].append(entry)
        out["request_count"] = session.requests
        print(json.dumps(out, ensure_ascii=False, separators=(",", ":"))[:5000])
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
