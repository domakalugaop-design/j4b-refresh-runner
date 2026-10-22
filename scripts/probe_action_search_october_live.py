"""Validate /action search as the October report inventory and its date semantics.

Checks: row counts, unique action IDs per day, membership of known report IDs,
and the dt value of one sampled action from each day.

Usage (on DFB):
    python3 probe_action_search_october_live.py
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
TIMEOUT = 60
ACTION_LINK = re.compile(r"/action/(\d+)(?:/|$)", re.I)
DT_FIELD = re.compile(r'name="dt"[^>]*value="([0-9.]{10})"', re.I)

KNOWN_30_SEP = {"665134", "671045", "677465"}


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


class _Ids(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: set[str] = set()

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href") or ""
            for identifier in ACTION_LINK.findall(href):
                self.ids.add(identifier)


def search(session: PortalSession, dt1: str, dt2: str) -> dict:
    status, ctype, body = session.request(
        "/action", "POST", data={"dt1": dt1, "dt2": dt2, "limit": "10000"}
    )
    html = body.decode("utf-8", errors="replace")
    parser = _Ids()
    parser.feed(html)
    parser.close()
    ids = sorted(int(value) for value in parser.ids)
    entry = {
        "dt1": dt1,
        "dt2": dt2,
        "http_status": status,
        "bytes": len(body),
        "unique_action_ids": len(ids),
        "id_min": ids[0] if ids else None,
        "id_max": ids[-1] if ids else None,
        "limit_hit": len(ids) >= 10000,
    }
    return entry, ids


def action_dt(session: PortalSession, action_id: str) -> dict:
    status, ctype, body = session.request(f"/action/{action_id}", "GET")
    html = body.decode("utf-8", errors="replace")
    found = DT_FIELD.search(html)
    return {
        "action_id": action_id,
        "http_status": status,
        "dt_value": found.group(1) if found else None,
        "bytes": len(body),
    }


def main() -> int:
    out: dict = {"probes": [], "membership": {}}
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
        sep, sep_ids = search(session, "30.09.2026", "30.09.2026")
        out["probes"].append(sep)
        out["membership"]["30_sep_contains_known"] = sorted(KNOWN_30_SEP & {str(v) for v in sep_ids})
        out["membership"]["30_sep_known_present"] = len(KNOWN_30_SEP & {str(v) for v in sep_ids})

        time.sleep(1.5)
        oct1, oct_ids = search(session, "01.10.2026", "01.10.2026")
        out["probes"].append(oct1)
        out["membership"]["oct1_overlap_with_sep30"] = len(set(oct_ids) & set(sep_ids))
        out["membership"]["oct1_min_id"] = min(oct_ids) if oct_ids else None

        if sep_ids:
            time.sleep(1.2)
            out["sep_sample"] = action_dt(session, str(max(sep_ids)))
        if oct_ids:
            time.sleep(1.2)
            out["oct_sample"] = action_dt(session, str(min(oct_ids)))

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
