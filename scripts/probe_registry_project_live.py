"""Calibrate /report/ registry completeness and extract October inventory.

1) Registry filtered to project 7986 for 30.09.2026 must reproduce the project
   page's 1124 reports (ID range 665134..677465) if the registry is complete.
2) Then the same registry for October yields the October inventory.

Usage (on DFB):
    python3 probe_registry_project_live.py 7986
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from html.parser import HTMLParser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from portal_transport import PortalSession  # noqa: E402

CREDENTIALS = "/home/maxis/j4b-refresh-deployment/credentials/portal-auth.json"
BASE_URL = "https://lk.j4b.ru"
TIMEOUT = 90
REPORT_LINK = re.compile(r"/report/(\d+)(?:/|$)", re.I)
PROJ_LINK = re.compile(r"/proj/(\d+)(?:/|$)", re.I)
DATE_VALUE = re.compile(r"\b(\d{2}[./]\d{2}[./]\d{4})\b")

SEPT_KNOWN_COUNT = 1124
SEPT_KNOWN_MIN = 665134
SEPT_KNOWN_MAX = 677465


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


class _Collect(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: set[str] = set()
        self.projects: set[str] = set()
        self.dates: set[str] = set()
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self._depth += 1
            return
        if tag == "a":
            href = dict(attrs).get("href") or ""
            for identifier in REPORT_LINK.findall(href):
                if identifier != "0":
                    self.ids.add(identifier)
            for identifier in PROJ_LINK.findall(href):
                if identifier != "0":
                    self.projects.add(identifier)

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self._depth:
            self._depth -= 1

    def handle_data(self, data):
        if self._depth == 0:
            for match in DATE_VALUE.findall(data):
                parts = match.split(".")
                if len(parts) == 3:
                    self.dates.add(f"{parts[2]}-{parts[1]}-{parts[0]}")


def query(session: PortalSession, data: dict) -> dict:
    status, ctype, body = session.request("/report/", "POST", data=data)
    parser = _Collect()
    parser.feed(body.decode("utf-8", errors="replace"))
    parser.close()
    ids = sorted(int(value) for value in parser.ids)
    return {
        "http_status": status,
        "bytes": len(body),
        "unique_report_ids": len(ids),
        "id_min": ids[0] if ids else None,
        "id_max": ids[-1] if ids else None,
        "projects": len(parser.projects),
        "_ids": [str(value) for value in ids],
    }


def main() -> int:
    project_id = sys.argv[1] if len(sys.argv) > 1 else "7986"
    out: dict = {"project_id": project_id, "calibration": {}, "october": {}}
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
        sept = query(session, {"dt1": "30.09.2026", "dt2": "30.09.2026",
                               "proj[]": [project_id], "limit": "10000"})
        sept_ids = sept.pop("_ids")
        in_range = [value for value in sept_ids if SEPT_KNOWN_MIN <= int(value) <= SEPT_KNOWN_MAX]
        out["calibration"] = {
            **sept,
            "expected_count": SEPT_KNOWN_COUNT,
            "expected_range": [SEPT_KNOWN_MIN, SEPT_KNOWN_MAX],
            "ids_in_expected_range": len(in_range),
            "count_ratio": round(len(sept_ids) / SEPT_KNOWN_COUNT, 3),
        }

        time.sleep(2.0)
        october = query(session, {"dt1": "01.10.2026", "dt2": "31.10.2026",
                                  "proj[]": [project_id], "limit": "10000"})
        oct_ids = october.pop("_ids")
        out["october"] = {**october, "sample_ids": oct_ids[:4],
                          "sample_ids_high": oct_ids[-2:]}
        out["project_count_in_result"] = len(set(oct_ids)) and None
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
