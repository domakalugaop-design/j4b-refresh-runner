"""Inspect the project report page form and test a read-only date filter POST.

Field names and date-like values only; no tokens, no cell text.

Usage (on DFB):
    python3 probe_listing_post_filter_live.py <project_id>
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
    try:
        return date(int(match.group(3)), int(match.group(2)), int(match.group(1)))
    except ValueError:
        return None


class _Form(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.fields: list[tuple[str, str, str]] = []
        self.forms = 0

    def handle_starttag(self, tag, attrs):
        a = {str(k).casefold(): v for k, v in attrs}
        if tag == "form":
            self.forms += 1
        if tag in {"input", "select", "textarea"}:
            name = (a.get("name") or "").strip()
            if not name:
                return
            kind = (a.get("type") or tag).casefold()
            value = (a.get("value") or "").strip()
            if kind in {"hidden", "submit", "button"} or DATE_VALUE.search(value) or re.search(
                r"dt|date", name, re.I
            ):
                self.fields.append((name[:40], kind[:12], value[:24]))


class _Stats(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.report_ids: set[str] = set()
        self.dates: list[date] = []
        self._cell: list[str] | None = None
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self._depth += 1
        elif tag in {"td", "th"}:
            self._cell = []
        if tag == "a":
            for identifier in REPORT_LINK.findall(dict(attrs).get("href") or ""):
                self.report_ids.add(identifier)

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self._depth:
            self._depth -= 1
        elif tag in {"td", "th"} and self._cell is not None:
            text = " ".join("".join(self._cell).split())
            found = DATE_VALUE.search(text)
            if found:
                parsed = parse_dmy(found.group(0))
                if parsed:
                    self.dates.append(parsed)
            self._cell = None

    def handle_data(self, data):
        if self._depth == 0 and self._cell is not None:
            self._cell.append(data[:200])

    def summary(self) -> dict:
        ids = sorted(int(value) for value in self.report_ids)
        return {
            "unique_report_ids": len(ids),
            "report_id_min": ids[0] if ids else None,
            "report_id_max": ids[-1] if ids else None,
            "date_count": len(self.dates),
            "date_min": str(min(self.dates)) if self.dates else None,
            "date_max": str(max(self.dates)) if self.dates else None,
            "october_dates": sum(1 for value in self.dates if value >= date(2026, 10, 1)),
        }


def scan(body: bytes) -> dict:
    parser = _Stats()
    parser.feed(body.decode("utf-8", errors="replace"))
    parser.close()
    return parser.summary()


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
        out["get"] = {"http_status": status, **scan(body)}
        form = _Form()
        form.feed(body.decode("utf-8", errors="replace"))
        form.close()
        out["forms"] = form.forms
        out["fields"] = [{"name": name, "type": kind, "value": value} for name, kind, value in form.fields[:20]]

        out["posts"] = []
        for data in (
            {"dt1": "01.10.2026", "dt2": "01.10.2026"},
            {"dt1": "01.10.2026", "dt2": "31.10.2026"},
        ):
            time.sleep(1.5)
            p_status, p_ctype, p_body = session.request(
                f"/proj/{project_id}/report", "POST", data=data
            )
            out["posts"].append({"data": data, "http_status": p_status, **scan(p_body)})
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
