"""Test whether a /report/ date filter changes the /proj/{id}/report scope.

Sequence: baseline project page -> POST /report/ with dt range -> project page
again. Compares report-ID sets and row dates.

Usage (on DFB):
    python3 probe_session_scope_live.py 7986 01.10.2026
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from collections import Counter
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
        return date(year, month, day)
    except ValueError:
        return None


class _Rows(HTMLParser):
    """Collect every <tr> regardless of table nesting."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict] = []
        self._depth = 0
        self._in_row = False
        self._cells: list[str] = []
        self._cell: list[str] | None = None
        self._reports: set[str] = set()

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self._depth += 1
            return
        if tag == "tr":
            self._in_row = True
            self._cells = []
            self._reports = set()
        elif tag in {"td", "th"} and self._in_row:
            self._cell = []
        if tag == "a" and self._in_row:
            for identifier in REPORT_LINK.findall(dict(attrs).get("href") or ""):
                if identifier != "0":
                    self._reports.add(identifier)

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self._depth:
            self._depth -= 1
        elif tag in {"td", "th"} and self._cell is not None:
            self._cells.append(" ".join("".join(self._cell).split())[:160])
            self._cell = None
        elif tag == "tr" and self._in_row:
            self._in_row = False
            cells, reports = self._cells, self._reports
            self._cells, self._reports = [], set()
            if not reports:
                return
            dates = [dmy(cell) for cell in cells]
            self.rows.append({
                "reports": sorted(reports),
                "date": next((value for value in dates if value), None),
            })

    def handle_data(self, data):
        if self._depth == 0 and self._cell is not None:
            self._cell.append(data[:200])


def stats(body: bytes) -> dict:
    parser = _Rows()
    parser.feed(body.decode("utf-8", errors="replace"))
    parser.close()
    ids = sorted({rid for row in parser.rows for rid in row["reports"]}, key=int)
    dates = Counter(str(row["date"]) for row in parser.rows if row["date"])
    return {
        "rows_with_report": len(parser.rows),
        "unique_report_ids": len(ids),
        "id_min": int(ids[0]) if ids else None,
        "id_max": int(ids[-1]) if ids else None,
        "top_dates": dict(dates.most_common(4)),
        "_ids": set(ids),
    }


def main() -> int:
    project_id = sys.argv[1] if len(sys.argv) > 1 else "7986"
    day = sys.argv[2] if len(sys.argv) > 2 else "01.10.2026"
    out: dict = {"project_id": project_id, "filter_day": day}
    session = None
    try:
        with open(CREDENTIALS, encoding="utf-8") as stream:
            config = json.load(stream)
        session = PortalSession(
            timeout=TIMEOUT, base_url=BASE_URL,
            login=config["login"], password=config["password"],
        )
        quiet_login(session)

        status, ctype, body = session.request(f"/proj/{project_id}/report", "GET")
        before = stats(body)
        before_ids = before.pop("_ids")
        out["before"] = {"http_status": status, **before}

        time.sleep(1.2)
        p_status, p_ctype, p_body = session.request(
            "/report/", "POST", data={"dt1": day, "dt2": day, "limit": "10000"}
        )
        out["filter_post"] = {"http_status": p_status, "bytes": len(p_body)}

        time.sleep(1.2)
        status2, ctype2, body2 = session.request(f"/proj/{project_id}/report", "GET")
        after = stats(body2)
        after_ids = after.pop("_ids")
        out["after"] = {"http_status": status2, **after}
        out["comparison"] = {
            "same_bytes": len(body) == len(body2),
            "shared_ids": len(before_ids & after_ids),
            "only_before": len(before_ids - after_ids),
            "only_after": len(after_ids - before_ids),
        }
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
