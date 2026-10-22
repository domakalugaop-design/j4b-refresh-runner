"""Validate corrector parsing and locate the report date on /action/{id}.

Uses the repository's own parse_report_corrector. Prints the corrector ID and
display-name *metadata* only; personal text stays in the private run output.

Usage (on DFB):
    python3 probe_corrector_date_live.py <id> [id...]
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
from report_reviewer import ReportCorrectorParseError, parse_report_corrector  # noqa: E402

CREDENTIALS = "/home/maxis/j4b-refresh-deployment/credentials/portal-auth.json"
BASE_URL = "https://lk.j4b.ru"
REQUEST_INTERVAL = 1.5
TIMEOUT = 45
DATE_VALUE = re.compile(r"\d{2}[./]\d{2}[./]\d{4}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?")
DATE_LABEL = re.compile(r"дата|создан|отправ|заполн|визит|провер|отч[её]т|анкет|время", re.I)
CYRILLIC_LABEL = re.compile(r"[0-9A-Za-zА-Яа-яЁё №\-.,:;()/ ]+")


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


class _LabelledDates(HTMLParser):
    """Collect (label, date) pairs from simple label/value table rows."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.pairs: list[tuple[str, str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._cells: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
            self._cells = []
        elif tag in {"th", "td"} and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in {"th", "td"} and self._cell is not None:
            self._cells.append(" ".join("".join(self._cell).split())[:200])
            self._cell = None
        elif tag == "tr" and self._row is not None:
            cells = self._cells
            self._row = None
            self._cells = []
            if len(cells) < 2:
                return
            for index, value in enumerate(cells):
                if not DATE_VALUE.search(value):
                    continue
                label = cells[index - 1] if index else ""
                label = label.strip(" :")
                if not label or len(label) > 40 or not CYRILLIC_LABEL.fullmatch(label):
                    continue
                if not DATE_LABEL.search(label):
                    continue
                found = DATE_VALUE.search(value)
                self.pairs.append((label, found.group(0) if found else ""))

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data[:200])


def summarise_fio(value: str | None) -> dict:
    if not value:
        return {"present": False}
    words = value.split()
    letters_only = all(word.replace(".", "").isalpha() for word in words)
    return {
        "present": True,
        "length": len(value),
        "word_count": len(words),
        "letters_only": letters_only,
        "initials_style": any(word.endswith(".") for word in words),
    }


def main() -> int:
    ids = [value for value in sys.argv[1:] if value.isdigit()]
    if not ids:
        print('{"failure_category":"BAD_ARGUMENT"}')
        return 0
    out: dict = {"probes": [], "date_labels_seen": {}}
    session = None
    try:
        with open(CREDENTIALS, encoding="utf-8") as stream:
            config = json.load(stream)
        session = PortalSession(
            timeout=TIMEOUT, base_url=BASE_URL,
            login=config["login"], password=config["password"],
        )
        quiet_login(session)
        for index, action_id in enumerate(ids[:6]):
            if index:
                time.sleep(REQUEST_INTERVAL)
            status, ctype, body = session.request(f"/action/{action_id}", "GET")
            entry = {"action_id": action_id, "http_status": status}
            if status != 200 or "html" not in ctype.lower():
                entry["failure_category"] = "PAGE_UNAVAILABLE"
                out["probes"].append(entry)
                continue
            html = body.decode("utf-8", errors="replace")
            try:
                parsed = parse_report_corrector(html)
                entry["corrector"] = {
                    "state": parsed.state,
                    "corrector_id": parsed.corrector_id,
                    "fio": summarise_fio(parsed.corrector_fio),
                }
            except ReportCorrectorParseError as exc:
                entry["corrector"] = {"state": "PARSE_ERROR", "reason": str(exc)[:80]}
            dates = _LabelledDates()
            dates.feed(html)
            dates.close()
            entry["date_pairs"] = dates.pairs[:10]
            for label, _ in dates.pairs:
                out["date_labels_seen"][label] = out["date_labels_seen"].get(label, 0) + 1
            out["probes"].append(entry)
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
