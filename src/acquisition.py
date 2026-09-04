from __future__ import annotations

import json
import time
from typing import Any

from .parsers import parse_action_table, parse_edit, parse_visit_table

ACTION_STATE_CODES = ("20", "30", "35", "37", "39", "40", "50")
COMPLETED_CODES = {"37", "40", "50"}


def field(value: Any, state: str, route: str) -> dict[str, Any]:
    return {"value": value, "state": state, "provenance": {"route": route}}


def _text(entry: Any) -> Any:
    if not isinstance(entry, dict):
        return entry
    return entry.get("value")


class Reader:
    def __init__(self, session: Any, cap: int):
        self.session = session
        self.cap = cap
        self.count = 0
        self.failures = 0

    def _reserve(self) -> None:
        if self.count >= self.cap:
            raise RuntimeError("request cap reached")
        self.count += 1

    def get(self, path: str) -> dict[str, Any]:
        self._reserve()
        try:
            status, content_type, body = self.session.request(path, "GET", accept="text/html, application/json")
            state = "VALUE_PRESENT" if body else "SOURCE_RETURNED_EMPTY_BODY"
            return {"state": state, "http_status": status, "content_type": content_type, "body": body}
        except Exception:
            self.failures += 1
            return {"state": "REQUEST_FAILED", "http_status": None, "content_type": None, "body": b""}

    def post(self, path: str, data: dict[str, str]) -> dict[str, Any]:
        self._reserve()
        try:
            status, content_type, body = self.session.request(path, "POST", data, accept="text/html, application/json")
            state = "VALUE_PRESENT" if body else "SOURCE_RETURNED_EMPTY_BODY"
            return {"state": state, "http_status": status, "content_type": content_type, "body": body}
        except Exception:
            self.failures += 1
            return {"state": "REQUEST_FAILED", "http_status": None, "content_type": None, "body": b""}


def action_index(markup: str, project_id: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in parse_action_table(markup, project_id):
        existing = result.get(row["visit_id"])
        if existing and int(row.get("action_id") or 0) <= int(existing.get("action_id") or 0):
            continue
        result[row["visit_id"]] = {
            "raw_status": row.get("visit_status") or None,
            "status_label": row.get("visit_status_label") or None,
            "assignment_state": "ASSIGNED" if row.get("participant_assigned") else "UNKNOWN",
            "action_id": row.get("action_id") or None,
        }
    return result


def acquire_project(reader: Reader, spec: dict[str, Any], delay: float) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    project_id = str(spec["project_id"])
    project = reader.get(f"/proj/{project_id}")
    time.sleep(delay)
    edit = reader.get(f"/proj/{project_id}/edit")
    time.sleep(delay)
    action_data = {
        "proj": json.dumps([project_id]),
        "dt1": "2026-01-01",
        "dt2": "2026-12-31",
        "limit": "10000",
        "send": "send",
        "user": "",
        "place": "",
        "city": "",
    }
    action_data.update({f"state[{code}]": "on" for code in ACTION_STATE_CODES})
    action = reader.post("/action", action_data)

    project_html = project["body"].decode("utf-8", "replace") if project["body"] else ""
    edit_html = edit["body"].decode("utf-8", "replace") if edit["body"] else ""
    action_html = action["body"].decode("utf-8", "replace") if action["body"] else ""
    edit_fields = parse_edit(edit_html) if edit_html else {}
    plan = edit_fields.get("planned_visit_count", {"value": None, "state": edit["state"]})
    visit_ids = [row["visit_id"] for row in parse_visit_table(project_html)] if project_html else []
    actions = action_index(action_html, project_id) if action_html else {}
    failed = any(item["state"] == "REQUEST_FAILED" for item in (project, edit, action))

    record = {
        "project_id": project_id,
        "project_name": edit_fields.get("project_name") or field(spec.get("project_name"), "VALUE_PRESENT" if spec.get("project_name") else "FIELD_PRESENT_EMPTY", f"/proj/{project_id}/edit"),
        "date_from": edit_fields.get("date_from") or field(spec.get("date_from"), "FIELD_NOT_EXPOSED", f"/proj/{project_id}/edit"),
        "date_to": edit_fields.get("date_to") or field(spec.get("date_to"), "FIELD_NOT_EXPOSED", f"/proj/{project_id}/edit"),
        "planned_visit_count": field(plan.get("value"), plan.get("state", "UNKNOWN"), f"/proj/{project_id}/edit"),
        "client": edit_fields.get("client", {"value": None, "state": "FIELD_NOT_EXPOSED"}),
        "primary_manager": edit_fields.get("primary_manager", {"value": None, "state": "FIELD_NOT_EXPOSED"}),
        "coordinators": edit_fields.get("coordinators", {"value": None, "state": "FIELD_NOT_EXPOSED"}),
        "scope": edit_fields.get("scope", {"value": None, "state": "FIELD_NOT_EXPOSED"}),
        "manager_payment": edit_fields.get("manager_payment", {"value": None, "state": "FIELD_NOT_EXPOSED"}),
        "wave": edit_fields.get("wave", {"value": None, "state": "FIELD_NOT_EXPOSED"}),
        "acquisition_state": "FAILED" if failed else "ACQUIRED",
    }

    visits: list[dict[str, Any]] = []
    for visit_id in visit_ids:
        action_row = actions.get(visit_id, {})
        raw = action_row.get("raw_status")
        visits.append({
            "project_id": project_id,
            "visit_id": visit_id,
            "raw_status": field(raw, "VALUE_PRESENT" if raw else "UNKNOWN", f"/action?project={project_id}"),
            "assignment_state": field(action_row.get("assignment_state", "UNKNOWN"), "VALUE_PRESENT" if visit_id in actions else "UNKNOWN", f"/action?project={project_id}"),
        })
    return record, visits


def discover_universe(session: Any) -> list[dict[str, Any]]:
    status, _content_type, body = session.request("/api/project", "GET", accept="application/json")
    if status != 200:
        raise RuntimeError(f"project universe request failed: HTTP {status}")
    payload = json.loads(body.decode("utf-8", "replace"), strict=False)
    if not isinstance(payload, dict):
        raise RuntimeError("project universe response is not an object")
    rows = []
    for project_id, raw in payload.items():
        if not str(project_id).isdigit():
            continue
        name = raw.get("name") if isinstance(raw, dict) else raw
        rows.append({"project_id": str(project_id), "project_name": str(name) if name not in (None, "") else None})
    return sorted(rows, key=lambda row: int(row["project_id"]))
