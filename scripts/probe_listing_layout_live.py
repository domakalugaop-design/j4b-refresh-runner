"""Inspect the project report-list row layout and date distribution.

Prints per-cell type and aggregate date ranges only, never cell text.

Usage (on DFB):
    python3 probe_listing_layout_live.py [project_id]
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
TIMEOUT = 45
DATE_VALUE = re.compile(r"\d{2}[./]\d{2}[./]\d{4}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?")
LINK_ID = re.compile(r"/(report|action|visit|proj)/(\d+)(?:/|$)", re.I)


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


class _Rows(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict] = []
        self._open = False
        self._cells: list[dict] = []
        self._cell: dict | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._open = True
            self._cells = []
        elif tag in {"th", "td"} and self._open:
            self._cell = {"links": [], "text_len": 0, "date": None, "tag": tag}
        if tag == "a" and self._cell is not None:
            for kind, identifier in LINK_ID.findall((dict(attrs).get("href") or "")):
                self._cell["links"].append(f"{kind.lower()}:{identifier}")

    def handle_endtag(self, tag):
        if tag in {"th", "td"} and self._cell is not None:
            self._cells.append(self._cell)
            self._cell = None
        elif tag == "tr" and self._open:
            self._open = False
            if self._cells:
                self.rows.append({"cells": self._cells})
            self._cells = []

    def handle_data(self, data):
        if self._cell is not None:
            text = " ".join(data.split())
            if text:
                self._cell["text_len"] += len(text)
                found = DATE_VALUE.search(text)
                if found and self._cell["date"] is None:
                    self._cell["date"] = found.group(0)


def cell_kind(cell: dict) -> str:
    if cell["date"]:
        return "DATE"
    if cell["links"]:
        return "LINK(" + ",".join(kind.split(":")[0] for kind in cell["links"][:2]) + ")"
    if cell["text_len"] == 0:
        return "EMPTY"
    return f"TEXT{min(cell['text_len'] // 10 * 10, 90)}"


def main() -> int:
    project_id = sys.argv[1] if len(sys.argv) > 1 else "7986"
    out: dict = {"project_id": project_id}
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
        if status != 200:
            print('{"failure_category":"LISTING_UNAVAILABLE"}')
            return 0
        parser = _Rows()
        parser.feed(body.decode("utf-8", errors="replace"))
        parser.close()
        rows = parser.rows
        out["total_rows"] = len(rows)
        out["layouts"] = []
        for row in rows[:10]:
            out["layouts"].append([cell_kind(cell) for cell in row["cells"]])

        # Aggregate: for each cell position, date range and October count.
        by_position: dict[int, dict] = {}
        for row in rows:
            for index, cell in enumerate(row["cells"]):
                if not cell["date"]:
                    continue
                parsed = parse_dmy(cell["date"])
                if parsed is None:
                    continue
                bucket = by_position.setdefault(index, {"count": 0, "min": None, "max": None, "october": 0})
                bucket["count"] += 1
                bucket["min"] = str(parsed) if bucket["min"] is None or str(parsed) < bucket["min"] else bucket["min"]
                bucket["max"] = str(parsed) if bucket["max"] is None or str(parsed) > bucket["max"] else bucket["max"]
                if parsed >= date(2026, 10, 1):
                    bucket["october"] += 1
        out["date_positions"] = {str(k): v for k, v in sorted(by_position.items())}
        out["request_count"] = session.requests
        rendered = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
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
