"""Test the global /report registry as a strict, date-filtered inventory source.

Row-scoped: each row's own date, report ID, project ID. Aggregate output only.

Usage (on DFB):
    python3 probe_report_registry_live.py 01.10.2026 [dd.mm.yyyy ...]
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
REPORT_LINK = re.compile(r"/report/(?:proj/)?(\d+)(?:/|$)", re.I)
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
        self._reports: set[str] = set()
        self._projs: set[str] = set()

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self._depth += 1
            return
        if tag == "tr":
            self._in_row = True
            self._cells = []
            self._reports = set()
            self._projs = set()
        elif tag in {"td", "th"} and self._in_row:
            self._cell = []
        if tag == "a" and self._in_row:
            href = dict(attrs).get("href") or ""
            for identifier in REPORT_LINK.findall(href):
                if identifier != "0":
                    self._reports.add(identifier)
            for identifier in PROJ_LINK.findall(href):
                if identifier != "0":
                    self._projs.add(identifier)

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self._depth:
            self._depth -= 1
        elif tag in {"td", "th"} and self._cell is not None:
            self._cells.append(" ".join("".join(self._cell).split())[:160])
            self._cell = None
        elif tag == "tr" and self._in_row:
            self._in_row = False
            if self._reports:
                cell_dates = [dmy(cell) for cell in self._cells]
                self.rows.append({
                    "report_ids": sorted(self._reports),
                    "date": next((str(value) for value in cell_dates if value), None),
                    "projects": sorted(self._projs)[:2],
                })
            self._in_row = False
            self._cells = []

    def handle_data(self, data):
        if self._depth == 0 and self._cell is not None:
            self._cell.append(data[:200])


def analyse(html: str) -> dict:
    parser = _Rows()
    parser.feed(html)
    parser.close()
    dates = Counter(row["date"] for row in parser.rows if row["date"])
    ids = sorted({rid for row in parser.rows for rid in row["report_ids"]}, key=int)
    projects = {pid for row in parser.rows for pid in row["projects"]}
    return {
        "rows_with_report": len(parser.rows),
        "unique_report_ids": len(ids),
        "id_min": int(ids[0]) if ids else None,
        "id_max": int(ids[-1]) if ids else None,
        "project_count": len(projects),
        "top_dates": dict(dates.most_common(4)),
        "_ids": ids,
    }


def main() -> int:
    days = [value for value in sys.argv[1:] if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", value)]
    out: dict = {"probes": []}
    session = None
    try:
        with open(CREDENTIALS, encoding="utf-8") as stream:
            config = json.load(stream)
        session = PortalSession(
            timeout=TIMEOUT, base_url=BASE_URL,
            login=config["login"], password=config["password"],
        )
        quiet_login(session)

        time.sleep(1.0)
        status, ctype, body = session.request("/report/", "GET")
        base = analyse(body.decode("utf-8", errors="replace"))
        base.pop("_ids", None)
        out["baseline_get"] = {"http_status": status, **base}

        collected: dict[str, list[str]] = {}
        for index, day in enumerate(days[:3]):
            time.sleep(1.5)
            p_status, p_ctype, p_body = session.request(
                "/report/", "POST", data={"dt1": day, "dt2": day, "limit": "10000"}
            )
            info = analyse(p_body.decode("utf-8", errors="replace"))
            ids = info.pop("_ids")
            collected[day] = ids
            on_day = sum(count for when, count in info["top_dates"].items() if when == _iso(day))
            out["probes"].append({
                "requested_day": day,
                "http_status": p_status,
                "bytes": len(p_body),
                **info,
                "rows_on_requested_day_top": on_day,
                "sample_ids": ids[:3],
            })
        if len(collected) >= 2:
            first, second = list(collected.items())[:2]
            overlap = len(set(first[1]) & set(second[1]))
            out["day_overlap"] = {
                "day_a": first[0], "day_b": second[0],
                "a_count": len(first[1]), "b_count": len(second[1]),
                "overlap": overlap,
                "jaccard": round(overlap / max(1, len(set(first[1]) | set(second[1]))), 4),
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


def _iso(day: str) -> str:
    parts = day.split(".")
    return f"{parts[2]}-{parts[1]}-{parts[0]}"


if __name__ == "__main__":
    raise SystemExit(main())
