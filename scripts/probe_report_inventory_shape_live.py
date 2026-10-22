"""Aggregate-only live probe: report list structure and report page shape.

Runs on the DFB host with the deployment's read-only portal credentials.
Prints identifiers, field names, header labels, and coarse date classes only.
It never prints credentials, cookies, HTML, option text, or free-form values.

Usage (on DFB):
    python3 probe_report_inventory_shape_live.py [project_id]
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
from report_inventory import extract_report_inventory  # noqa: E402

REPO_SRC = "/home/maxis/j4b-refresh-deployment/repo/src"
CREDENTIALS = "/home/maxis/j4b-refresh-deployment/credentials/portal-auth.json"
BASE_URL = "https://lk.j4b.ru"
REQUEST_INTERVAL = 1.5
TIMEOUT = 45
_ID_LINK = re.compile(r"/(report|action|visit|proj)/(\d+)(?:/|$)", re.I)
_DATEISH = re.compile(r"\d{2}[./]\d{2}[./]\d{4}|\d{4}-\d{2}-\d{2}")
_STAFF = "staff"


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


class _PageShape(HTMLParser):
    """Collect field names, header labels, link templates and date classes."""

    def __init__(self, base_host: str | None) -> None:
        super().__init__(convert_charrefs=True)
        self.base_host = base_host
        self.tables = 0
        self.rows = 0
        self.headers: list[str] = []
        self.field_names: set[str] = set()
        self.field_kinds: dict[str, str] = {}
        self.link_templates: dict[str, int] = {}
        self.link_ids: dict[str, set[str]] = {}
        self.date_classes: dict[str, set[str]] = {}
        self.staff_options = 0
        self.staff_selected = False
        self.staff_selected_numeric = None
        self._in_staff = False
        self._opt_value: str | None = None
        self._opt_selected = False
        self._cell: list[str] | None = None
        self._cells: list[tuple[str, str]] = []
        self._row_open = False
        self._headers_row: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {str(k).casefold(): v for k, v in attrs}
        if tag == "table":
            self.tables += 1
        elif tag == "tr":
            self._row_open = True
            self._cells = []
        elif tag in {"th", "td"} and self._row_open:
            self._cell = []
        elif tag in {"input", "select", "textarea"}:
            name = (a.get("name") or "").strip()
            if name:
                self.field_names.add(name[:60])
                self.field_kinds.setdefault(name[:60], tag)
            if tag == "select" and name.casefold() == _STAFF:
                self._in_staff = True
        elif tag == "option" and self._in_staff:
            self.staff_options += 1
            self._opt_value = a.get("value")
            self._opt_selected = "selected" in a
        elif tag in {"a", "form"}:
            raw = a.get("href" if tag == "a" else "action") or ""
            if not raw:
                return
            parsed = urlparse(raw)
            if parsed.scheme and parsed.hostname != self.base_host:
                return
            path = parsed.path or raw.split("?", 1)[0]
            if not path.startswith("/"):
                return
            template = re.sub(r"/\d+(?=/|$)", "/{id}", path)[:80]
            self.link_templates[template] = self.link_templates.get(template, 0) + 1
            for kind, identifier in _ID_LINK.findall(path):
                self.link_ids.setdefault(kind.lower(), set()).add(identifier)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"th", "td"} and self._cell is not None:
            value = " ".join("".join(self._cell).split())
            self._cells.append((tag, value[:120]))
            self._cell = None
        elif tag == "option" and self._in_staff:
            if self._opt_selected and not self.staff_selected:
                self.staff_selected = True
                value = (self._opt_value or "").strip()
                self.staff_selected_numeric = bool(value) and value.isdigit()
            self._opt_value = None
        elif tag == "select" and self._in_staff:
            self._in_staff = False
        elif tag == "tr" and self._row_open:
            if self._cells:
                if all(k == "th" for k, _ in self._cells):
                    self._headers_row = [v[:60] for _, v in self._cells]
                    if not self.headers:
                        self.headers = self._headers_row
                else:
                    self.rows += 1
                    for index, (_, value) in enumerate(self._cells):
                        if not _DATEISH.search(value):
                            continue
                        header = self._headers_row[index] if index < len(self._headers_row) else ""
                        kind = "ISO_DATE" if re.match(r"^\d{4}-", value) else "DMY_DATE"
                        label = re.sub(r"[^0-9A-Za-zА-Яа-яЁё ]", "", header)[:40] or "UNLABELED"
                        self.date_classes.setdefault(label, set()).add(kind)
            self._row_open = False
            self._cells = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data[:200])


def page_shape(body: bytes) -> dict:
    parser = _PageShape(urlparse(BASE_URL).hostname)
    parser.feed(body.decode("utf-8", errors="replace"))
    parser.close()
    meaning = {}
    for label, kinds in parser.date_classes.items():
        low = label.casefold()
        slot = (
            "REPORT" if re.search(r"отч|анкет|заполн|создан", low)
            else "VISIT" if "визит" in low
            else "CHECK" if "провер" in low
            else "GENERIC"
        )
        meaning.setdefault(slot, set()).update(kinds)
    return {
        "tables": parser.tables,
        "rows": parser.rows,
        "header_labels": parser.headers[:14],
        "field_names": sorted(parser.field_names)[:24],
        "field_kinds": {k: parser.field_kinds[k] for k in sorted(parser.field_kinds)[:24]},
        "link_templates": dict(sorted(parser.link_templates.items(), key=lambda p: -p[1])[:10]),
        "linked_ids": {k: len(v) for k, v in sorted(parser.link_ids.items())},
        "date_classes": {k: sorted(v) for k, v in sorted(meaning.items())},
        "staff_select": {
            "present": _STAFF in parser.field_names,
            "options": parser.staff_options,
            "selected": parser.staff_selected,
            "selected_numeric": parser.staff_selected_numeric,
        },
    }


def main() -> int:
    project_id = sys.argv[1] if len(sys.argv) > 1 else "7986"
    if not project_id.isdigit():
        print('{"failure_category":"BAD_ARGUMENT"}')
        return 0
    session = None
    out: dict = {"project_id": project_id}
    try:
        with open(CREDENTIALS, encoding="utf-8") as stream:
            config = json.load(stream)
        session = PortalSession(
            timeout=TIMEOUT, base_url=BASE_URL,
            login=config["login"], password=config["password"],
        )
        quiet_login(session)
        out["credentials"] = "OK"
        out["auth_gets"] = session.auth_get_count

        status, ctype, body = session.request(f"/proj/{project_id}/report", "GET")
        out["listing"] = {"http_status": status, "is_html": "html" in ctype.lower(), "bytes": len(body)}
        if status != 200 or "html" not in ctype.lower():
            out["failure_category"] = "LISTING_UNAVAILABLE"
            print(json.dumps(out, ensure_ascii=True, separators=(",", ":")))
            return 0

        page = extract_report_inventory(body.decode("utf-8", errors="replace"), base_url=BASE_URL)
        ids = [int(value) for value in page.report_ids]
        out["listing"].update({
            "tables": page.tables,
            "rows_with_report": len(page.rows),
            "unique_report_ids": len(page.report_ids),
            "project_ids_on_page": len(page.project_ids),
            "pagination": page.pagination,
            "header_labels": list(page.header_labels[:14]),
            "date_column_classes": {k: list(v) for k, v in page.date_column_classes.items()},
            "join_profile": page.join_profile(),
            "report_id_min": ids[0] if ids else None,
            "report_id_max": ids[-1] if ids else None,
        })
        samples = [page.report_ids[0], page.report_ids[len(page.report_ids) // 2]] if page.report_ids else []
        out["samples"] = samples

        out["pages"] = []
        for report_id in samples:
            time.sleep(REQUEST_INTERVAL)
            r_status, r_ctype, r_body = session.request(f"/report/{report_id}", "GET")
            entry = {"report_id": report_id, "http_status": r_status,
                     "is_html": "html" in r_ctype.lower(), "bytes": len(r_body)}
            if r_status == 200 and "html" in r_ctype.lower():
                entry.update(page_shape(r_body))
            out["pages"].append(entry)

        out["request_count"] = session.requests
        rendered = json.dumps(out, ensure_ascii=True, separators=(",", ":"))
        print(rendered if len(rendered) <= 6000 else '{"failure_category":"SUMMARY_LIMIT"}')
        return 0
    except Exception as exc:  # never leak portal text or credentials
        out["failure_category"] = "SAFE_PROBE_ERROR"
        out["error_class"] = type(exc).__name__
        print(json.dumps(out, ensure_ascii=True, separators=(",", ":")))
        return 0
    finally:
        if session is not None:
            session.close()


if __name__ == "__main__":
    raise SystemExit(main())
