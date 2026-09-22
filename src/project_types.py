from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any


PROJECT_TYPE_COLUMNS = ["project_type_code", "project_type_name"]
STATE_COLUMNS = ["project_id", "project_type_code", "project_type_name"]
PROJECT_TYPE_DICTIONARY = {
    "0001": "Обычный",
    "0002": "Классика_ТП",
    "0003": "Такси_ТП",
    "0004": "Фотоаудит",
    "0005": "Качественные исследования",
    "0007": "B2B_тайный покупатель",
    "0008": "Тестирование цифровых продуктов",
}
PERIOD_MARKER_RE = re.compile(r"(?<!\d)(0[1-9]|1[0-2])(\d{2})(?!\d)")
APPLICABILITY_CUTOFF = (2026, 9)


def normalized_period_marker(project_name: Any) -> tuple[int, int] | None:
    """Return the first qualified MMYY marker normalized to (YYYY, MM)."""
    if project_name in (None, ""):
        return None
    match = PERIOD_MARKER_RE.search(str(project_name))
    if not match:
        return None
    month = int(match.group(1))
    year = 2000 + int(match.group(2))
    return year, month


def is_project_type_applicable(project_name: Any) -> bool:
    period = normalized_period_marker(project_name)
    return period is not None and period >= APPLICABILITY_CUTOFF


def project_type_applicable_ids(universe: list[dict[str, Any]]) -> set[str]:
    """Use the current /api/project universe name as the canonical period source."""
    return {
        str(item["project_id"])
        for item in universe
        if is_project_type_applicable(item.get("project_name"))
    }


def canonical_project_names(universe: list[dict[str, Any]]) -> dict[str, str]:
    """Return usable canonical project names from the already-fetched universe."""
    return {
        str(item["project_id"]): str(item["project_name"])
        for item in universe
        if item.get("project_name") not in (None, "")
    }


def apply_canonical_project_names(
    rows: list[dict[str, Any]], universe: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    names = canonical_project_names(universe)
    for row in rows:
        name = names.get(str(row.get("project_id", "")))
        if name is not None:
            row["project_name"] = name
    return rows


def classify_raw_type(payload: Any) -> tuple[str, str | None, str | None]:
    """Return (bucket, qualified code, display name) without coercing codes."""
    if not isinstance(payload, dict) or "type" not in payload:
        return "MISSING", None, None
    raw = payload["type"]
    if raw is None:
        return "NULL", None, None
    if type(raw) is int and raw == 0:
        return "ZERO", None, None
    if raw == "":
        return "EMPTY", None, None
    if isinstance(raw, str) and raw in PROJECT_TYPE_DICTIONARY:
        return "VALID", raw, PROJECT_TYPE_DICTIONARY[raw]
    return "UNKNOWN_CODE", None, None


def validate_state_rows(rows: list[list[Any]]) -> dict[str, tuple[str, str]]:
    if not rows:
        return {}
    if rows[0] != STATE_COLUMNS:
        raise ValueError("project_types header mismatch")
    state: dict[str, tuple[str, str]] = {}
    for row in rows[1:]:
        if not row or row[0] in (None, ""):
            continue
        padded = list(row) + [""] * (len(STATE_COLUMNS) - len(row))
        project_id, code, name = (str(padded[i]) if padded[i] is not None else "" for i in range(3))
        if project_id in state:
            raise ValueError(f"duplicate project_types project_id: {project_id}")
        if not code or code not in PROJECT_TYPE_DICTIONARY:
            raise ValueError(f"invalid persisted project type code for project_id {project_id}")
        if name != PROJECT_TYPE_DICTIONARY[code]:
            raise ValueError(f"project type name/dictionary mismatch for project_id {project_id}")
        state[project_id] = (code, name)
    return state


def merge_assignment(
    state: dict[str, tuple[str, str]], project_id: str, code: str, name: str
) -> tuple[dict[str, tuple[str, str]], bool, bool]:
    """Keep valid existing assignments immutable; report conflicts explicitly."""
    existing = state.get(str(project_id))
    if existing:
        return state, False, existing != (code, name)
    if code not in PROJECT_TYPE_DICTIONARY or name != PROJECT_TYPE_DICTIONARY[code]:
        raise ValueError("only dictionary-qualified code/name assignments may be persisted")
    updated = dict(state)
    updated[str(project_id)] = (code, name)
    return updated, True, False


def serialize_state(state: dict[str, tuple[str, str]]) -> list[list[str]]:
    return [STATE_COLUMNS] + [
        [project_id, code, name]
        for project_id, (code, name) in sorted(state.items(), key=lambda item: int(item[0]))
    ]


def materialize_project_types(
    rows: list[dict[str, Any]],
    state: dict[str, tuple[str, str]],
    applicable_ids: set[str],
) -> list[dict[str, Any]]:
    for row in rows:
        project_id = str(row.get("project_id", ""))
        assignment = state.get(project_id)
        if assignment and project_id not in applicable_ids:
            raise ValueError(f"PERIOD_APPLICABILITY_CONFLICT: project_id={project_id}")
        row["project_type_code"] = assignment[0] if assignment else None
        row["project_type_name"] = assignment[1] if assignment else None
    return rows


@dataclass
class ProjectTypeTelemetry:
    applicable_projects: int = 0
    already_assigned: int = 0
    pending_before: int = 0
    valid_assignments_acquired: int = 0
    zero_results: int = 0
    null_results: int = 0
    empty_results: int = 0
    missing_results: int = 0
    unknown_code_results: int = 0
    immutable_conflicts: int = 0
    detail_get_reused: int = 0
    detail_get_additional: int = 0
    request_failures: int = 0
    pending_ids: set[str] = field(default_factory=set)
    request_elapsed_seconds: float = 0.0


def acquire_pending_types(
    session: Any,
    universe: list[dict[str, Any]],
    state: dict[str, tuple[str, str]],
    delay: float = 0.15,
) -> tuple[dict[str, tuple[str, str]], ProjectTypeTelemetry, set[str]]:
    applicable = project_type_applicable_ids(universe)
    telemetry = ProjectTypeTelemetry(
        applicable_projects=len(applicable),
        already_assigned=len(applicable & set(state)),
    )
    pending = sorted(applicable - set(state), key=int)
    telemetry.pending_before = len(pending)
    telemetry.pending_ids = set(pending)
    started = time.monotonic()
    next_state = state
    for index, project_id in enumerate(pending, start=1):
        try:
            status, _content_type, body = session.request(
                f"/api/project/{project_id}", "GET", accept="application/json"
            )
            telemetry.detail_get_additional += 1
            if status != 200 or not body:
                telemetry.request_failures += 1
                bucket = "REQUEST_FAILED"
                code = name = None
            else:
                try:
                    import json

                    payload = json.loads(body.decode("utf-8", "replace"), strict=False)
                    bucket, code, name = classify_raw_type(payload)
                except (ValueError, TypeError):
                    bucket, code, name = "MISSING", None, None
        except Exception:
            telemetry.detail_get_additional += 1
            telemetry.request_failures += 1
            bucket, code, name = "REQUEST_FAILED", None, None

        if bucket == "VALID" and code and name:
            next_state, added, conflict = merge_assignment(next_state, project_id, code, name)
            telemetry.valid_assignments_acquired += int(added)
            telemetry.immutable_conflicts += int(conflict)
        elif bucket == "ZERO":
            telemetry.zero_results += 1
        elif bucket == "NULL":
            telemetry.null_results += 1
        elif bucket == "EMPTY":
            telemetry.empty_results += 1
        elif bucket == "MISSING":
            telemetry.missing_results += 1
        elif bucket == "UNKNOWN_CODE":
            telemetry.unknown_code_results += 1

        if index % 10 == 0 or index == len(pending):
            print(
                f"PROJECT_TYPE {index}/{len(pending)} | valid={telemetry.valid_assignments_acquired} | "
                f"pending={index - telemetry.valid_assignments_acquired} | "
                f"http_additional={telemetry.detail_get_additional}",
                flush=True,
            )
        if delay:
            time.sleep(delay)
    telemetry.request_elapsed_seconds = time.monotonic() - started
    return next_state, telemetry, applicable
