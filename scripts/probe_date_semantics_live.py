"""Locate and qualify the report date field on /action/{id} and /visit/{id}.

Prints date/time values and short date labels only (no personal text).

Usage (on DFB):
    python3 probe_date_semantics_live.py <action_id> [action_id...]
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
REQUEST_INTERVAL = 1.2
TIMEOUT = 45
DATE_VALUE = re.compile(r"\d{2}[./]\d{2}[./]\d{4}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?")
DATE_LABEL = re.compile(r"дата|создан|отправ|заполн|визит|провер|отч[её]т|анкет|время|изменен", re.I)
SAFE_LABEL = re.compile(r"[0-9A-Za-zА-Яа-яЁё №\-.,:;()/ ]+")
LINK_ID = re.compile(r"/(visit|action|proj)/(\d+)(?:/|$)", re.I)


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


class _Dates(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.field_values: dict[str, str] = {}
        self.date_contexts: list[tuple[str, str]] = []
        self.linked: dict[str, set[str]] = {}
        self._recent = ""
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        a = {str(k).casefold(): v for k, v in attrs}
        if tag in {"script", "style"}:
            self._skip_depth += 1
        if tag in {"input", "select", "textarea"}:
            name = (a.get("name") or "").strip()
            value = (a.get("value") or "").strip()
            if name and value and re.search(r"dt|date|time|finish|created|updated", name, re.I):
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?|\d{2}[./]\d{2}[./]\d{4}|\d{1,2}:\d{2}", value):
                    self.field_values[name[:40]] = value[:32]
        if tag == "a":
            href = a.get("href") or ""
            for kind, identifier in LINK_ID.findall(href):
                self.linked.setdefault(kind.lower(), set()).add(identifier)

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth:
            return
        text = " ".join(data.split())
        if not text:
            return
        window = (self._recent + " " + text)[-260:]
        for match in DATE_VALUE.finditer(text):
            before = window[: window.rfind(match.group(0))]
            label = ""
            for chunk in re.split(r"[\n|]", before)[-1:]:
                candidate = chunk.strip(" :•-—\t")
                if candidate and len(candidate) <= 44 and SAFE_LABEL.fullmatch(candidate):
                    if DATE_LABEL.search(candidate):
                        label = candidate
            self.date_contexts.append((label[:44], match.group(0)))
        self._recent = window[-160:]


def analyse(html: str) -> dict:
    page = _Dates()
    page.feed(html)
    page.close()
    labels: dict[str, int] = {}
    for label, _ in page.date_contexts:
        if label:
            labels[label] = labels.get(label, 0) + 1
    return {
        "date_field_values": page.field_values,
        "date_contexts": page.date_contexts[:8],
        "date_labels": dict(sorted(labels.items(), key=lambda p: -p[1])[:6]),
        "date_occurrences": len(page.date_contexts),
        "linked": {k: sorted(v)[:4] for k, v in sorted(page.linked.items())},
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
        for index, action_id in enumerate(ids[:3]):
            if index:
                time.sleep(REQUEST_INTERVAL)
            status, ctype, body = session.request(f"/action/{action_id}", "GET")
            entry: dict = {"action_id": action_id, "action_http": status}
            if status == 200 and "html" in ctype.lower():
                info = analyse(body.decode("utf-8", errors="replace"))
                entry["action"] = info
                visit_ids = info["linked"].get("visit", [])
                if visit_ids:
                    time.sleep(REQUEST_INTERVAL)
                    v_status, v_ctype, v_body = session.request(f"/visit/{visit_ids[0]}", "GET")
                    entry["visit_id"] = visit_ids[0]
                    entry["visit_http"] = v_status
                    if v_status == 200 and "html" in v_ctype.lower():
                        entry["visit"] = analyse(v_body.decode("utf-8", errors="replace"))
            out["probes"].append(entry)
        out["request_count"] = session.requests
        rendered = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
        print(rendered if len(rendered) <= 6000 else '{"failure_category":"SUMMARY_LIMIT"}')
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
