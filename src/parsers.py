from __future__ import annotations

import html as html_lib
import re
from typing import Any

from .workflow_analytics import WORKFLOW_STATES

TAG_RE = re.compile(r"<[^>]+>")
INPUT_RE = re.compile(r"<input\b([^>]*)>", re.IGNORECASE)
SELECT_RE = re.compile(r"<select\b([^>]*)>(.*?)</select\s*>", re.IGNORECASE | re.DOTALL)
TEXTAREA_RE = re.compile(r"<textarea\b([^>]*)>(.*?)</textarea\s*>", re.IGNORECASE | re.DOTALL)
OPTION_OPEN_RE = re.compile(r"<option\b([^>]*)>", re.IGNORECASE)
NAME_RE = re.compile(r"\bname\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
VALUE_RE = re.compile(r"\bvalue\s*=\s*[\"']([^\"']*)[\"']", re.IGNORECASE)
VISIT_LINK_RE = re.compile(r"/visit/(\d+)", re.IGNORECASE)
ACTION_LINK_RE = re.compile(r'<a\b[^>]*href=["\']/action/(\d+)["\'][^>]*>(.*?)</a\s*>', re.IGNORECASE | re.DOTALL)

# The action page contains both the workflow-state link text and unrelated
# numeric action identifiers.  Only the former are admitted to analytics.
_WORKFLOW_LABELS = {" ".join(str(label).split()).casefold() for label in WORKFLOW_STATES.values()}
_WORKFLOW_LABELS.update({
    "отчет выполнен", "отчет принят", "оплачено", "ожидает оплату",
    "анкета подтверждена", "есть претензия",
})


def plain_text(fragment: str) -> str:
    return " ".join(html_lib.unescape(TAG_RE.sub(" ", fragment)).split())


def _number_or_text(value: str | None) -> int | str | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return value


def input_field_state(html: str, field_name: str) -> tuple[str, str | None]:
    for attrs in INPUT_RE.findall(html):
        name = NAME_RE.search(attrs)
        if not name or name.group(1) != field_name:
            continue
        value = VALUE_RE.search(attrs)
        if value and value.group(1).strip():
            return "VALUE_PRESENT", value.group(1).strip()
        return "FIELD_PRESENT_EMPTY", None
    return "FIELD_NOT_EXPOSED", None


def select_field_state(html: str, field_name: str) -> tuple[str, list[dict[str, str]]]:
    for attrs, options in SELECT_RE.findall(html):
        name = NAME_RE.search(attrs)
        if not name or name.group(1) != field_name:
            continue
        selected: list[dict[str, str]] = []
        option_matches = list(OPTION_OPEN_RE.finditer(options))
        for index, match in enumerate(option_matches):
            option_attrs = match.group(1)
            start = match.end()
            end = option_matches[index + 1].start() if index + 1 < len(option_matches) else len(options)
            close_option = options.lower().find("</option", start)
            close_select = options.lower().find("</select", start)
            if close_option != -1 and close_option < end:
                end = close_option
            if close_select != -1 and close_select < end:
                end = close_select
            if re.search(r"\bselected\b", option_attrs, re.IGNORECASE):
                raw = VALUE_RE.search(option_attrs)
                selected.append({
                    "value": raw.group(1).strip() if raw and raw.group(1) else "",
                    "label": plain_text(options[start:end]),
                })
        return ("VALUE_PRESENT", selected) if selected else ("FIELD_PRESENT_EMPTY", [])
    return "FIELD_NOT_EXPOSED", []


def textarea_field_state(html: str, field_name: str) -> tuple[str, str | None]:
    for attrs, body in TEXTAREA_RE.findall(html):
        name = NAME_RE.search(attrs)
        if not name or name.group(1) != field_name:
            continue
        value = plain_text(body)
        return ("VALUE_PRESENT", value) if value else ("FIELD_PRESENT_EMPTY", None)
    return "FIELD_NOT_EXPOSED", None


def parse_edit(html: str) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for html_name, canonical_name in (
        ("name", "project_name"),
        ("dt1", "date_from"),
        ("dt2", "date_to"),
        ("visits", "planned_visit_count"),
        ("cost", "manager_payment"),
    ):
        state, raw = input_field_state(html, html_name)
        value: Any = _number_or_text(raw) if canonical_name in {"planned_visit_count", "manager_payment"} else raw
        fields[canonical_name] = {"state": state, "value": value}

    for html_name, canonical_name, multiple in (
        ("client", "client", False),
        ("user", "primary_manager", False),
        ("user2[]", "coordinators", True),
        ("wave", "wave", False),
        ("scope", "scope", False),
    ):
        state, selected = select_field_state(html, html_name)
        value = [item["label"] for item in selected] if multiple else (selected[0]["label"] if selected else None)
        fields[canonical_name] = {
            "state": state,
            "selected_count": len(selected),
            "selected_values": selected,
            "value": value,
        }
    return fields


def parse_visit_table(html: str) -> list[dict[str, str]]:
    return [{"visit_id": visit_id} for visit_id in sorted(set(VISIT_LINK_RE.findall(html)), key=int)]


def parse_action_table(html: str, target_project_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    row_pattern = re.compile(r'<tr[^>]*class=["\']([^"\']*)["\'][^>]*>.*?</tr>', re.IGNORECASE | re.DOTALL)
    for match in row_pattern.finditer(html):
        row = match.group(0)
        row_class = match.group(1)
        visit_match = re.search(r"/visit/(\d+)", row)
        if not visit_match or f"/proj/{target_project_id}" not in row:
            continue
        action_match = re.search(r"/action/(\d+)", row)
        action_id = action_match.group(1) if action_match else ""
        links = [(identifier, plain_text(body)) for identifier, body in ACTION_LINK_RE.findall(row)]
        numeric = [text for _, text in links if text.isdigit()]
        codes = [text for text in numeric if int(text) in WORKFLOW_STATES]
        labels = [text for _, text in links if not text.isdigit()]
        # A structurally valid state control is represented by an action link
        # with a visible state label.  If such a control has no canonical code,
        # fail closed; bare numeric action IDs and other numeric controls are
        # ignored.  When a canonical code is present, unrelated IDs in the
        # same row remain metadata and must not poison the row.
        normalized_labels = {" ".join(label.split()).casefold() for label in labels}
        if not codes and labels and (normalized_labels & _WORKFLOW_LABELS):
            unknown = next((text for text in numeric if int(text) not in WORKFLOW_STATES), None)
            if unknown is not None:
                raise ValueError(f"unknown workflow state code: {unknown}")
        status_label = ""
        for label in reversed(labels):
            label = label.strip()
            if label and not label.isdigit():
                status_label = label
                break
        rows.append({
            "visit_id": visit_match.group(1),
            "action_id": action_id,
            "participant_assigned": "table-red" not in row_class or bool(action_id),
            "visit_status": codes[-1] if codes else "",
            "workflow_state_codes": codes,
            "visit_status_label": status_label,
        })
    return rows
