"""Verify the report -> action join and locate the selected corrector value.

Prints technical identifiers only. Corrector display text is reduced to
metadata (length, has-space) so no personal data reaches the terminal.

Usage (on DFB):
    python3 probe_action_join_live.py 665134 671045 <id3>
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

CREDENTIALS = "/home/maxis/j4b-refresh-deployment/credentials/portal-auth.json"
BASE_URL = "https://lk.j4b.ru"
REQUEST_INTERVAL = 1.5
TIMEOUT = 45
_ID_LINK = re.compile(r"/(report|action|visit|proj|user)/(\d+)(?:/|$)", re.I)


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


class _ActionPage(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.linked: dict[str, set[str]] = {}
        self.staff_options: list[tuple[str | None, bool, int]] = []
        self._in_staff = False
        self._in_opt = False
        self._opt_value: str | None = None
        self._opt_selected = False
        self._opt_len = 0
        self.hidden_numeric: list[tuple[str, str]] = []
        self.staff_name_attr: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = {str(k).casefold(): v for k, v in attrs}
        if tag == "select" and (a.get("name") or "").casefold() == "staff":
            self._in_staff = True
            self.staff_name_attr.append(a.get("name") or "")
        elif tag == "option" and self._in_staff:
            self._in_opt = True
            self._opt_value = a.get("value")
            self._opt_selected = "selected" in a
            self._opt_len = 0
        elif tag == "input" and (a.get("type") or "").casefold() == "hidden":
            value = (a.get("value") or "").strip()
            name = (a.get("name") or "").strip()
            if name and value.isdigit():
                self.hidden_numeric.append((name[:40], value[:24]))
        if tag == "a":
            raw = a.get("href") or ""
            path = urlparse(raw).path or ""
            for kind, identifier in _ID_LINK.findall(path):
                self.linked.setdefault(kind.lower(), set()).add(identifier)

    def handle_endtag(self, tag):
        if tag == "option" and self._in_opt:
            self.staff_options.append((self._opt_value, self._opt_selected, self._opt_len))
            self._in_opt = False
        elif tag == "select" and self._in_staff:
            self._in_staff = False

    def handle_data(self, data):
        if self._in_opt:
            self._opt_len += len(" ".join(data.split()))


def analyse(body: bytes) -> dict:
    page = _ActionPage()
    page.feed(body.decode("utf-8", errors="replace"))
    page.close()
    selected = [opt for opt in page.staff_options if opt[1]]
    filled = [opt for opt in page.staff_options if (opt[0] or "").strip() and opt[2] > 0]
    return {
        "linked_ids": {k: sorted(v)[:6] for k, v in sorted(page.linked.items())},
        "staff": {
            "select_name": page.staff_name_attr,
            "options_total": len(page.staff_options),
            "options_with_text": len(filled),
            "selected_count": len(selected),
            "selected_ids": [opt[0] for opt in selected][:3],
            "selected_text_len": [opt[2] for opt in selected][:3],
            "first_option_value": page.staff_options[0][0] if page.staff_options else None,
            "first_option_text_len": page.staff_options[0][2] if page.staff_options else None,
        },
        "hidden_numeric_field_count": len(page.hidden_numeric),
        "hidden_numeric_names": sorted({name for name, _ in page.hidden_numeric})[:10],
    }


def main() -> int:
    ids = [value for value in sys.argv[1:] if value.isdigit()]
    if not ids:
        print('{"failure_category":"BAD_ARGUMENT"}')
        return 0
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
        for index, report_id in enumerate(ids[:4]):
            if index:
                time.sleep(REQUEST_INTERVAL)
            status, ctype, body = session.request(f"/action/{report_id}", "GET")
            entry = {
                "report_id": report_id,
                "http_status": status,
                "is_html": "html" in ctype.lower(),
                "bytes": len(body),
                "effective_path": re.sub(r"/\d+", "/{id}", urlparse(session.last_effective_url or "").path or ""),
            }
            if status == 200 and "html" in ctype.lower():
                entry.update(analyse(body))
                entry["project_matches_listing"] = "7986" in entry["linked_ids"].get("proj", [])
                entry["report_link_equals_self"] = entry["linked_ids"].get("report") == [report_id]
            out["probes"].append(entry)
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
