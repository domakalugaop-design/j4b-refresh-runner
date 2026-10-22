"""Row-scoped extraction of the /action search result table.

For each result row: its own date cell, its action ID, and its visit/project
links. Prints aggregates plus a few technical IDs, never cell text.

Usage (on DFB):
    python3 probe_action_rows_live.py 01.10.2026 [dd.mm.yyyy ...]
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
ACTION_LINK = re.compile(r"/action/(\d+)(?:/|$)", re.I)
VISIT_LINK = re.compile(r"/visit/(\d+)(?:/|$)", re.I)
PROJ_LINK = re.compile(r"/proj/(\d+)(?:/|$)", re.I)


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
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict] = []
        self._depth = 0
        self._in_row = False
        self._cells: list[str] = []
        self._cell: list[str] | None = None
        self._links: dict[str, set[str]] = {}

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self._depth += 1
            return
        if tag == "tr":
            self._in_row = True
            self._cells = []
            self._links = {"action": set(), "visit": set(), "proj": set()}
        elif tag in {"td", "th"} and self._in_row:
            self._cell = []
        if tag == "a" and self._in_row:
            href = dict(attrs).get("href") or ""
            for kind, pattern in (("action", ACTION_LINK), ("visit", VISIT_LINK), ("proj", PROJ_LINK)):
                for identifier in pattern.findall(href):
                    if identifier != "0":
                        self._links[kind].add(identifier)

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self._depth:
            self._depth -= 1
        elif tag in {"td", "th"} and self._cell is not None:
            self._cells.append(" ".join("".join(self._cell).split())[:160])
            self._cell = None
        elif tag == "tr" and self._in_row:
            self._in_row = False
            if self._cells or any(self._links.values()):
                self.rows.append({"cells": self._cells, "links": {k: sorted(v) for k, v in self._links.items()}})
            self._cells = []
            self._links = {"action": set(), "visit": set(), "proj": set()}

    def handle_data(self, data):
        if self._depth == 0 and self._cell is not None:
            self._cell.append(data[:200])


def analyse(html: str) -> dict:
    parser = _Rows()
    parser.feed(html)
    parser.close()
    rows = parser.rows
    with_action = [row for row in rows if row["links"]["action"]]
    row_dates = Counter()
    matched: list[dict] = []
    for row in with_action:
        cell_dates = [dmy(cell) for cell in row["cells"]]
        row_date = next((value for value in cell_dates if value), None)
        if row_date:
            row_dates[str(row_date)] += 1
        matched.append({
            "action_id": row["links"]["action"][0],
            "date": str(row_date) if row_date else None,
            "visits": row["links"]["visit"][:2],
            "projects": row["links"]["proj"][:2],
            "cell_count": len(row["cells"]),
        })
    return {
        "total_rows": len(rows),
        "rows_with_action": len(with_action),
        "rows_with_date": sum(1 for item in matched if item["date"]),
        "top_row_dates": dict(row_dates.most_common(4)),
        "sample": matched[:3],
        "rows": matched,
    }


def main() -> int:
    days = [value for value in sys.argv[1:] if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", value)]
    if not days:
        print('{"failure_category":"BAD_ARGUMENT"}')
        return 0
    out: dict = {"days": []}
    session = None
    try:
        with open(CREDENTIALS, encoding="utf-8") as stream:
            config = json.load(stream)
        session = PortalSession(
            timeout=TIMEOUT, base_url=BASE_URL,
            login=config["login"], password=config["password"],
        )
        quiet_login(session)
        for index, day in enumerate(days[:3]):
            if index:
                time.sleep(1.5)
            status, ctype, body = session.request(
                "/action", "POST", data={"dt1": day, "dt2": day, "limit": "10000"}
            )
            info = analyse(body.decode("utf-8", errors="replace"))
            sample_rows = info.pop("rows")
            unique_on_day = {
                item["action_id"] for item in sample_rows if item["date"] == _iso(day)
            }
            entry = {
                "requested_day": day,
                "http_status": status,
                **info,
                "unique_action_ids_on_requested_day": len(unique_on_day),
                "sample_ids_on_day": sorted(unique_on_day, key=int)[:4],
            }
            out["days"].append(entry)
            out.setdefault("_collector", {})[day] = sorted(unique_on_day, key=int)
        ids_today = out["_collector"].get(days[0], [])
        out["first_day_id_min"] = int(ids_today[0]) if ids_today else None
        out["first_day_id_max"] = int(ids_today[-1]) if ids_today else None
        out.pop("_collector", None)
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


def _iso(day: str) -> str:
    parts = day.split(".")
    return f"{parts[2]}-{parts[1]}-{parts[0]}"


if __name__ == "__main__":
    raise SystemExit(main())
