from __future__ import annotations

import json
import html
import time
from typing import Any
from urllib.parse import urlsplit

from .parsers import parse_action_table, parse_edit, parse_visit_table, plain_text
from .workflow_analytics import WORKFLOW_STATE_CODES

ACTION_STATE_CODES = tuple(str(code) for code in WORKFLOW_STATE_CODES)
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
        self.post_count = 0
        self.failures = 0
        self.project_completed = 0
        self.project_failures = 0
        self.project_semantic_failures = 0
        self.started_monotonic = time.monotonic()

    def _reserve(self) -> None:
        if self.count >= self.cap:
            raise RuntimeError("request cap reached")
        self.count += 1

    def get(self, path: str) -> dict[str, Any]:
        self._reserve()
        try:
            status, content_type, body = self.session.request(path, "GET", accept="text/html, application/json")
            state = "VALUE_PRESENT" if body else "SOURCE_RETURNED_EMPTY_BODY"
            return {
                "state": state,
                "http_status": status,
                "content_type": content_type,
                "body": body,
                "effective_url": getattr(self.session, "last_effective_url", None),
            }
        except Exception:
            self.failures += 1
            return {"state": "REQUEST_FAILED", "http_status": None, "content_type": None, "body": b""}

    def post(self, path: str, data: dict[str, str]) -> dict[str, Any]:
        self._reserve()
        self.post_count += 1
        try:
            status, content_type, body = self.session.request(path, "POST", data, accept="text/html, application/json")
            state = "VALUE_PRESENT" if body else "SOURCE_RETURNED_EMPTY_BODY"
            return {"state": state, "http_status": status, "content_type": content_type, "body": body}
        except Exception:
            self.failures += 1
            return {"state": "REQUEST_FAILED", "http_status": None, "content_type": None, "body": b""}

    def project_done(self, acquisition_state: str) -> None:
        self.project_completed += 1
        if acquisition_state == "FAILED":
            self.project_failures += 1
        elif acquisition_state == "SEMANTIC_FAILURE":
            self.project_semantic_failures += 1
        total = max(self.cap // 3, 1)
        if self.project_completed % 10 != 0 and self.project_completed != total:
            return
        elapsed = max(time.monotonic() - self.started_monotonic, 0.001)
        rate = self.project_completed / elapsed * 60.0
        remaining = max(total - self.project_completed, 0)
        eta_minutes = remaining / rate if rate > 0 else 0.0
        print(
            f"ACQUISITION {self.project_completed}/{total} | "
            f"success={self.project_completed - self.project_failures - self.project_semantic_failures} | "
            f"failed={self.project_failures} | semantic_failed={self.project_semantic_failures} | http={self.count} | "
            f"elapsed={elapsed:.0f}s | {rate:.1f} proj/min | ETA={eta_minutes:.1f} min",
            flush=True,
        )


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


def action_memberships(markup: str, project_id: str) -> list[dict[str, Any]]:
    """Expand every predicate code in the combined /action response."""
    memberships: list[dict[str, Any]] = []
    for row in parse_action_table(markup, project_id):
        codes = row.get("workflow_state_codes") or ([row["visit_status"]] if row.get("visit_status") else [])
        for code in codes:
            memberships.append({
                "project_id": str(project_id),
                "visit_id": str(row["visit_id"]),
                "workflow_state_code": int(code),
                "action_id": row.get("action_id") or None,
                "predicate_source": "action_response",
            })
    return memberships


def _response_failure(name: str, response: dict[str, Any]) -> str | None:
    if response.get("state") == "REQUEST_FAILED":
        return f"{name}:REQUEST_FAILED"
    if response.get("http_status") != 200:
        return f"{name}:HTTP_{response.get('http_status')}"
    if not response.get("body"):
        return f"{name}:EMPTY_BODY"
    return None


def _semantic_failures(
    project_id: str,
    project: dict[str, Any],
    edit: dict[str, Any],
    action: dict[str, Any],
    edit_fields: dict[str, Any],
    canonical_name: Any,
    portal_base_url: str,
) -> list[str]:
    """Fail closed when a 200 response does not satisfy the qualified page contract."""
    failures = [
        reason
        for name, response in (("project", project), ("edit", edit), ("action", action))
        if (reason := _response_failure(name, response)) is not None
    ]
    project_html = project.get("body", b"").decode("utf-8", "replace")
    edit_html = edit.get("body", b"").decode("utf-8", "replace")
    action_html = action.get("body", b"").decode("utf-8", "replace")

    # project_id is the entity key. Reject a successful redirect to another
    # project (or origin) before considering the edit-form name fallback.
    base = urlsplit(portal_base_url)
    for name, response, expected_path in (
        ("project", project, f"/proj/{project_id}"),
        ("edit", edit, f"/proj/{project_id}/edit"),
    ):
        if response.get("http_status") == 200 and response.get("body"):
            effective_url = response.get("effective_url")
            actual = urlsplit(effective_url) if effective_url else None
            if (
                actual is None
                or actual.scheme != base.scheme
                or actual.netloc != base.netloc
                or actual.path.rstrip("/") != expected_path.rstrip("/")
            ):
                failures.append(f"{name}:IDENTITY_MISMATCH")

    if canonical_name in (None, ""):
        failures.append("universe:PROJECT_NAME_MISSING")

    expected_edit_fields = (
        "project_name", "date_from", "date_to", "planned_visit_count", "client",
        "primary_manager", "coordinators", "scope", "manager_payment", "wave",
    )
    if not edit_fields:
        failures.append("edit:PARSE_EMPTY")
    for name in expected_edit_fields:
        field_data = edit_fields.get(name)
        if not isinstance(field_data, dict) or field_data.get("state") == "FIELD_NOT_EXPOSED":
            failures.append(f"edit:FIELD_NOT_EXPOSED:{name}")
    edit_name = edit_fields.get("project_name", {}).get("value") if edit_fields else None
    canonical_name_matches_edit = (
        canonical_name not in (None, "")
        and edit_name not in (None, "")
        and html.unescape(str(edit_name)).strip() == str(canonical_name).strip()
    )
    canonical_name_on_project = (
        canonical_name not in (None, "") and canonical_name in plain_text(project_html)
    )
    if not canonical_name_on_project:
        project_is_html = "<html" in project_html.lower() or "<!doctype" in project_html.lower()
        if not project_is_html:
            failures.append("project:NOT_HTML_DOCUMENT")
        elif not canonical_name_matches_edit:
            failures.append("project:CANONICAL_NAME_NOT_PRESENT")
    if not edit_name:
        failures.append("edit:PROJECT_NAME_EMPTY")
    elif canonical_name not in (None, "") and not canonical_name_matches_edit:
        failures.append("edit:CANONICAL_NAME_MISMATCH")
    if edit_html and ("<html" not in edit_html.lower() and "<!doctype" not in edit_html.lower()):
        failures.append("edit:NOT_HTML_DOCUMENT")
    if action_html and ("<html" not in action_html.lower() and "<!doctype" not in action_html.lower()):
        failures.append("action:NOT_HTML_DOCUMENT")
    return failures


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
    memberships = action_memberships(action_html, project_id) if action_html else []
    failed = any(
        item["state"] == "REQUEST_FAILED" or item.get("http_status") != 200 or not item.get("body")
        for item in (project, edit, action)
    )
    portal_base_url = getattr(reader.session, "base_url", "https://lk.j4b.ru").rstrip("/")
    canonical_name = _text(spec.get("project_name"))
    semantic_failures = _semantic_failures(
        project_id, project, edit, action, edit_fields, canonical_name, portal_base_url
    )
    acquisition_state = "FAILED" if failed else "SEMANTIC_FAILURE" if semantic_failures else "ACQUIRED"
    edit_name = edit_fields.get("project_name", {}).get("value")
    if not semantic_failures and canonical_name and canonical_name in plain_text(project_html):
        identity_source = "project_page"
    elif not semantic_failures and canonical_name and edit_name:
        identity_source = "project_edit_fallback"
    else:
        identity_source = None

    record = {
        "project_id": project_id,
        "project_identity_source": identity_source,
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
        "acquisition_state": acquisition_state,
        "acquisition_failure_reasons": semantic_failures,
        "workflow_memberships": memberships,
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
    reader.project_done(acquisition_state)
    return record, visits


def discover_universe(session: Any, attempts: int = 3) -> list[dict[str, Any]]:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        status, content_type, body = session.request("/api/project", "GET", accept="application/json")
        if status != 200:
            last_error = RuntimeError(f"project universe request failed: HTTP {status}")
        elif not body:
            last_error = RuntimeError("project universe response body is empty")
        else:
            try:
                payload = json.loads(body.decode("utf-8", "replace"), strict=False)
            except json.JSONDecodeError:
                last_error = RuntimeError(
                    f"project universe returned non-JSON body: content_type={content_type or 'unknown'} bytes={len(body)}"
                )
            else:
                if not isinstance(payload, dict):
                    last_error = RuntimeError("project universe response is not an object")
                else:
                    rows = []
                    for project_id, raw in payload.items():
                        if not str(project_id).isdigit():
                            continue
                        name = raw.get("name") if isinstance(raw, dict) else raw
                        rows.append({"project_id": str(project_id), "project_name": str(name) if name not in (None, "") else None})
                    return sorted(rows, key=lambda row: int(row["project_id"]))
        if attempt < attempts:
            print(f"UNIVERSE_DISCOVERY_REAUTH | attempt={attempt + 1}/{attempts}", flush=True)
            session.close()
            time.sleep(float(attempt))
            session.login()
    raise last_error or RuntimeError("project universe discovery failed")
