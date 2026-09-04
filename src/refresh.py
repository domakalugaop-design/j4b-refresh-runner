from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from http.client import IncompleteRead, RemoteDisconnected
from pathlib import Path
from typing import Any
from urllib.error import URLError

from .acquisition import COMPLETED_CODES, Reader, acquire_project, discover_universe
from .portal_transport import PortalSession

SHEET_NAME = os.environ.get("GOOGLE_WORKSHEET", "projects_current")
COLUMNS = ["project_id","project_name","period","plan","plan_value","plan_status","created","completed","unassigned","execution_pct","plan_missing_with_activity","has_period_marker","project_start","project_end","elapsed_pct","lag","risk_status","risk_reason","validation_state","last_refreshed","client","primary_manager","coordinators","date_from","date_to","scope","manager_payment","wave","assigned","questionnaire_filled","rejected"]


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def value(field: Any) -> Any:
    return field.get("value") if isinstance(field, dict) else field


def text(field: Any) -> str | None:
    v = value(field)
    if isinstance(v, list):
        return ", ".join(str(x) for x in v) if v else None
    return None if v in (None, "") else str(v)


def iso_date(field: Any) -> str | None:
    v = text(field)
    if not v:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(v, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def _required(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise RuntimeError(f"missing required environment variable: {name}")
    return val


def google_token() -> str:
    client_id = _required("GOOGLE_CLIENT_ID")
    client_secret = _required("GOOGLE_CLIENT_SECRET")
    refresh_token = _required("GOOGLE_REFRESH_TOKEN")
    body = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=body, method="POST")
    with urllib.request.urlopen(req, timeout=60) as response:
        return json.load(response)["access_token"]


def api(url: str, token: str, body: Any = None, method: str | None = None) -> Any:
    req = urllib.request.Request(
        url,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method=method or ("POST" if body is not None else "GET"),
    )
    with urllib.request.urlopen(req, timeout=120) as response:
        return json.load(response)


def api_get(url: str, token: str, attempts: int = 3) -> Any:
    for attempt in range(1, attempts + 1):
        try:
            return api(url, token)
        except (IncompleteRead, ConnectionResetError, RemoteDisconnected, URLError):
            if attempt == attempts:
                raise
            time.sleep(0.2 * attempt)
    raise AssertionError("unreachable")


def col(index: int) -> str:
    out = ""
    n = index + 1
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


def select_scope(catalogue: list[dict[str, Any]], current_rows: list[list[Any]], today: date | None = None) -> list[dict[str, Any]]:
    today = today or datetime.now(timezone.utc).date()
    cur = f"{today.month:02d}{today.year % 100:02d}"
    prev_year = today.year if today.month > 1 else today.year - 1
    prev_month = (today.month - 2) % 12 + 1
    prev = f"{prev_month:02d}{prev_year % 100:02d}"
    baseline = {str(r[0]): r for r in current_rows[1:] if r and r[0] != ""}
    baseline_ids = set(baseline)
    catalogue_ids = {str(row["project_id"]) for row in catalogue}
    new_ids = catalogue_ids - baseline_ids
    selected: dict[str, dict[str, Any]] = {}
    for item in catalogue:
        pid = str(item["project_id"])
        old = baseline.get(pid)
        name = str(old[1] if old and len(old) > 1 else item.get("project_name") or "")
        marker = re.search(r"(?:^|[^0-9])(0[1-9]|1[0-2])(\d{2})(?!\d)", name)
        active = bool(marker and marker.group(1) + marker.group(2) in {cur, prev})
        if pid in new_ids or active:
            selected[pid] = dict(item)
    return sorted(selected.values(), key=lambda row: int(row["project_id"]))


def materialize(projects: list[dict[str, Any]], visits: list[dict[str, Any]], timestamp: str) -> list[dict[str, Any]]:
    rows = []
    for project in projects:
        pid = str(project["project_id"])
        pv = [v for v in visits if str(v.get("project_id")) == pid]
        plan = value(project.get("planned_visit_count"))
        codes = [text(v.get("raw_status")) for v in pv]
        row = {k: None for k in COLUMNS}
        row.update({
            "project_id": pid,
            "project_name": text(project.get("project_name")),
            "plan": plan,
            "plan_value": plan,
            "plan_status": "PLAN_MISSING_DATA" if plan is None else "VALID_PLAN",
            "created": len(pv),
            "completed": sum(code in COMPLETED_CODES for code in codes),
            "execution_pct": (sum(code in COMPLETED_CODES for code in codes) / plan if isinstance(plan, (int, float)) and plan > 0 else None),
            "plan_missing_with_activity": plan is None and bool(pv),
            "risk_status": "DATA_QUALITY" if plan is None and pv else "NOT_QUALIFIED",
            "risk_reason": "PLAN_MISSING_DATA" if plan is None and pv else None,
            "validation_state": "PENDING",
            "last_refreshed": timestamp,
            "client": text(project.get("client")),
            "primary_manager": text(project.get("primary_manager")),
            "coordinators": text(project.get("coordinators")),
            "date_from": iso_date(project.get("date_from")),
            "date_to": iso_date(project.get("date_to")),
            "scope": text(project.get("scope")),
            "manager_payment": value(project.get("manager_payment")),
            "wave": text(project.get("wave")),
            "assigned": sum(code == "20" for code in codes),
            "questionnaire_filled": sum(code == "30" for code in codes),
            "rejected": sum(code == "35" for code in codes),
            "_acquisition_state": project.get("acquisition_state"),
        })
        rows.append(row)
    return rows


def sheets_serial(raw: Any) -> Any:
    if raw in (None, ""):
        return ""
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return raw
    parsed = str(raw).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(parsed)
        dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid date value: {raw!r}") from exc
    return (dt - datetime(1899, 12, 30, tzinfo=timezone.utc)).total_seconds() / 86400


def sheet_rows(rows: list[dict[str, Any]]) -> list[list[Any]]:
    out = [COLUMNS]
    for row in rows:
        line = [row.get(c) if row.get(c) is not None else "" for c in COLUMNS]
        for name in ("date_from", "date_to", "last_refreshed"):
            line[COLUMNS.index(name)] = sheets_serial(line[COLUMNS.index(name)])
        out.append(line)
    return out


def read_sheet(token: str, sid: str, rows: int = 10000) -> list[list[Any]]:
    rng = urllib.parse.quote(f"{SHEET_NAME}!A1:{col(len(COLUMNS)-1)}{rows}", safe="!:")
    data = api_get(f"https://sheets.googleapis.com/v4/spreadsheets/{sid}/values/{rng}?valueRenderOption=UNFORMATTED_VALUE", token)
    return data.get("values", [])


def _pad(row: list[Any]) -> list[Any]:
    return list(row) + [""] * (len(COLUMNS) - len(row))


def merge_previous(rows: list[dict[str, Any]], previous: list[list[Any]], selected_ids: set[str], timestamp: str) -> list[dict[str, Any]]:
    old = {str(r[0]): _pad(r) for r in previous[1:] if r and r[0] != ""}
    merged: dict[str, dict[str, Any]] = {}
    for row in rows:
        pid = str(row["project_id"])
        prior = old.get(pid)
        if row.get("_acquisition_state") == "FAILED" and prior:
            carried = {c: prior[i] for i, c in enumerate(COLUMNS)}
            carried["last_refreshed"] = timestamp
            carried["_acquisition_state"] = "FAILED"
            merged[pid] = carried
        else:
            merged[pid] = row
    for pid, prior in old.items():
        if pid not in selected_ids:
            carried = {c: prior[i] for i, c in enumerate(COLUMNS)}
            carried["last_refreshed"] = timestamp
            merged[pid] = carried
    return [merged[k] for k in sorted(merged, key=int)]


def summary(rows: list[list[Any]]) -> dict[str, int]:
    ids = [str(r[0]) for r in rows[1:] if r and r[0] != ""]
    return {"rows": len(ids), "unique": len(set(ids)), "duplicates": len(ids) - len(set(ids))}


def publish(token: str, sid: str, candidate: list[list[Any]], previous: list[list[Any]]) -> None:
    before = summary(previous)
    after = summary(candidate)
    if after["duplicates"] != 0:
        raise RuntimeError("candidate contains duplicate project IDs")
    if after["unique"] < before["unique"]:
        raise RuntimeError("candidate master shrinks previous unique project set")
    meta = api(f"https://sheets.googleapis.com/v4/spreadsheets/{sid}?fields=sheets.properties(title,sheetId)", token)
    target = next((s["properties"] for s in meta.get("sheets", []) if s.get("properties", {}).get("title") == SHEET_NAME), None)
    if not target:
        raise RuntimeError("target worksheet not found")
    rng = f"{SHEET_NAME}!A1:{col(len(COLUMNS)-1)}{len(candidate)}"
    encoded = urllib.parse.quote(rng, safe="!:")
    previous_copy = previous
    try:
        api(f"https://sheets.googleapis.com/v4/spreadsheets/{sid}/values/{encoded}:clear", token, {}, method="POST")
        api(f"https://sheets.googleapis.com/v4/spreadsheets/{sid}/values:batchUpdate", token, {"valueInputOption": "RAW", "data": [{"range": rng, "majorDimension": "ROWS", "values": candidate}]})
        date_cols = [COLUMNS.index("date_from"), COLUMNS.index("date_to"), COLUMNS.index("last_refreshed")]
        api(f"https://sheets.googleapis.com/v4/spreadsheets/{sid}:batchUpdate", token, {"requests": [{"repeatCell": {"range": {"sheetId": target["sheetId"], "startRowIndex": 1, "endRowIndex": len(candidate), "startColumnIndex": c, "endColumnIndex": c + 1}, "cell": {"userEnteredFormat": {"numberFormat": {"type": "DATE_TIME" if c == date_cols[-1] else "DATE", "pattern": "yyyy-mm-dd hh:mm:ss" if c == date_cols[-1] else "yyyy-mm-dd"}}}, "fields": "userEnteredFormat.numberFormat"}} for c in date_cols]})
        actual = read_sheet(token, sid, len(candidate) + 10)
        if summary(actual) != after or actual[:1] != candidate[:1]:
            raise RuntimeError("readback summary mismatch")
    except Exception:
        restore_rng = f"{SHEET_NAME}!A1:{col(len(COLUMNS)-1)}{len(previous_copy)}"
        api(f"https://sheets.googleapis.com/v4/spreadsheets/{sid}/values/{urllib.parse.quote(restore_rng, safe='!:')}:clear", token, {}, method="POST")
        api(f"https://sheets.googleapis.com/v4/spreadsheets/{sid}/values:batchUpdate", token, {"valueInputOption": "RAW", "data": [{"range": restore_rng, "majorDimension": "ROWS", "values": previous_copy}]})
        raise


def run() -> dict[str, Any]:
    run_mode = os.environ.get("RUN_MODE", "test").strip().lower()
    if run_mode != "test":
        raise RuntimeError("public runner currently permits RUN_MODE=test only")
    if os.environ.get("ALLOW_PRODUCTION_WRITE", "false").strip().lower() == "true":
        raise RuntimeError("production write is forbidden in the qualification runner")

    started_at = now()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + hashlib.sha1(os.urandom(8)).hexdigest()[:8]
    sid = _required("GOOGLE_SPREADSHEET_ID")
    token = google_token()
    previous = read_sheet(token, sid)
    if not previous or previous[0] != COLUMNS:
        raise RuntimeError("baseline sheet unavailable or schema mismatch")

    session = PortalSession()
    session.login()
    try:
        universe = discover_universe(session)
        selected = select_scope(universe, previous)
        reader = Reader(session, max(3 * len(selected), 3))
        projects: list[dict[str, Any]] = []
        visits: list[dict[str, Any]] = []
        for spec in selected:
            project, project_visits = acquire_project(reader, spec, float(os.environ.get("PORTAL_REQUEST_DELAY", "0.15")))
            projects.append(project)
            visits.extend(project_visits)
        timestamp = now()
        rows = materialize(projects, visits, timestamp)
        merged = merge_previous(rows, previous, {str(x["project_id"]) for x in selected}, timestamp)
        candidate = sheet_rows(merged)
        publish(token, sid, candidate, previous)
        failed = sum(p.get("acquisition_state") == "FAILED" for p in projects)
        finished_at = now()
        wall = (datetime.fromisoformat(finished_at) - datetime.fromisoformat(started_at)).total_seconds()
        return {
            "RUN_ID": run_id,
            "STARTED_AT": started_at,
            "FINISHED_AT": finished_at,
            "WALL_SECONDS": wall,
            "UNIVERSE_COUNT": len(universe),
            "SELECTED_COUNT": len(selected),
            "SUCCESS_COUNT": len(projects) - failed,
            "FAILED_COUNT": failed,
            "PORTAL_HTTP_REQUEST_COUNT": session.requests,
            "FINAL_MASTER_UNIQUE_IDS": summary(candidate)["unique"],
            "FINAL_STATUS": "SUCCESS",
        }
    finally:
        session.close()


def main() -> int:
    try:
        report = run()
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"FINAL_STATUS": "FAILED", "ERROR_TYPE": type(exc).__name__, "ERROR_MESSAGE": str(exc)}, ensure_ascii=False, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
