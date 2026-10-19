"""Safe, project-scoped failure records and private append-only journaling."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


_ENDPOINTS = {"/api/project/{project_id}", "/proj/{project_id}",
              "/proj/{project_id}/edit", "/action", "/pay/detail"}
_ERROR_CLASSES = {
    "JSON_ERROR", "HTTP_ERROR", "TRANSPORT_ERROR", "TIMEOUT_ERROR",
    "UTF8_ERROR", "SCHEMA_ERROR", "SEMANTIC_ERROR", "PAYMENT_ERROR",
}
_SANITIZED_CODES = {
    "PROJECT_DETAIL_JSON_INVALID", "PROJECT_DETAIL_HTTP_FAILURE",
    "PROJECT_DETAIL_TRANSPORT_FAILURE", "PROJECT_DETAIL_SCHEMA_INVALID",
    "PROJECT_DETAIL_SEMANTIC_INVALID", "PROJECT_DETAIL_TYPE_UNRESOLVED", "PAYMENT_EXPORT_FAILURE",
    "PAYMENT_EXPORT_SCHEMA_INVALID", "PAYMENT_MATERIALIZATION_INVALID",
    "PROJECT_VIEW_REQUEST_FAILURE", "PROJECT_EDIT_REQUEST_FAILURE",
    "PROJECT_ACTION_REQUEST_FAILURE", "PROJECT_SEMANTIC_VALIDATION_FAILED",
}


@dataclass(frozen=True)
class ProjectFailure:
    project_id: str
    endpoint: str
    error_class: str
    sanitized_error: str

    def __post_init__(self) -> None:
        if not isinstance(self.project_id, str) or not self.project_id.isdigit():
            raise ValueError("project failure requires a numeric project_id")
        if self.endpoint not in _ENDPOINTS:
            raise ValueError("project failure endpoint is not allowlisted")
        if self.error_class not in _ERROR_CLASSES:
            raise ValueError("project failure class is not allowlisted")
        if self.sanitized_error not in _SANITIZED_CODES:
            raise ValueError("project failure code is not allowlisted")


def last_successful_timestamp(value: Any) -> str:
    """Normalize a Sheets date serial or ISO timestamp without leaking cell data."""
    if isinstance(value, bool) or value in (None, ""):
        return "UNKNOWN"
    try:
        if isinstance(value, (int, float)):
            stamp = datetime(1899, 12, 30, tzinfo=timezone.utc) + timedelta(days=float(value))
            return stamp.isoformat(timespec="seconds")
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")
    except (OverflowError, TypeError, ValueError):
        pass
    return "UNKNOWN"


def project_failure_record(
    failure: ProjectFailure,
    *,
    last_successful_refresh: Any,
    recovery_status: str,
    timestamp: datetime | None = None,
) -> dict[str, str]:
    if recovery_status not in {
        "CANDIDATE_PRESERVES_LAST_GOOD", "CANDIDATE_OMITS_WITHOUT_LAST_GOOD",
        "RECOVERED_ON_THIS_RUN",
    }:
        raise ValueError("project failure recovery status is not allowlisted")
    return {
        "timestamp": (timestamp or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds"),
        "project_id": failure.project_id,
        "endpoint": failure.endpoint,
        "error_class": failure.error_class,
        "sanitized_error": failure.sanitized_error,
        "last_successful_refresh": last_successful_timestamp(last_successful_refresh),
        "recovery_status": recovery_status,
    }


def _private_journal_path(path: str | os.PathLike[str] | None) -> Path:
    target = Path(path).expanduser() if path else Path.home() / ".local/state/j4b-refresh/project-errors.jsonl"
    target = target.absolute()
    parent = target.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = parent.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise PermissionError("project error journal directory is not private and owner-controlled")
    return target


def append_project_error_journal(
    records: Iterable[Mapping[str, str]],
    path: str | os.PathLike[str] | None = None,
) -> int:
    """Append allowlisted JSONL records with private permissions; never log exception text."""
    materialized = [dict(record) for record in records]
    if not materialized:
        return 0
    required = {"timestamp", "project_id", "endpoint", "error_class", "sanitized_error",
                "last_successful_refresh", "recovery_status"}
    for record in materialized:
        if set(record) != required:
            raise ValueError("project error journal record schema mismatch")
        ProjectFailure(record["project_id"], record["endpoint"], record["error_class"], record["sanitized_error"])
        if record["recovery_status"] not in {
            "CANDIDATE_PRESERVES_LAST_GOOD", "CANDIDATE_OMITS_WITHOUT_LAST_GOOD", "RECOVERED_ON_THIS_RUN"
        }:
            raise ValueError("project error journal recovery status is invalid")
        if record["last_successful_refresh"] != "UNKNOWN" and last_successful_timestamp(record["last_successful_refresh"]) == "UNKNOWN":
            raise ValueError("project error journal timestamp is invalid")
    target = _private_journal_path(path)
    flags = os.O_CREAT | os.O_APPEND | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(target, flags, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise PermissionError("project error journal file is not private and owner-controlled")
        os.fchmod(descriptor, 0o600)
        payload = b"".join(
            json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
            for record in materialized
        )
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return len(materialized)


def restore_failed_type_assignments(
    candidate: dict[str, tuple[str, str]],
    previous: Mapping[str, tuple[str, str]],
    failed_ids: Iterable[str],
) -> dict[str, tuple[str, str]]:
    """Keep the complete prior type bundle for any project excluded this cycle."""
    restored = dict(candidate)
    for raw_id in failed_ids:
        project_id = str(raw_id)
        if project_id in previous:
            restored[project_id] = previous[project_id]
        else:
            restored.pop(project_id, None)
    return restored


def reconcile_failed_project_bundles(
    selected: list[dict[str, Any]],
    projects: list[dict[str, Any]],
    visits: list[dict[str, Any]],
    state: dict[str, tuple[str, str]],
    previous_state: Mapping[str, tuple[str, str]],
    failures: Iterable[ProjectFailure],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, tuple[str, str]], set[str]]:
    """Exclude failed IDs from every new-data candidate while restoring last-good type state."""
    selected_ids = [str(item.get("project_id", "")) for item in selected]
    if any(not project_id.isdigit() for project_id in selected_ids) or len(selected_ids) != len(set(selected_ids)):
        raise RuntimeError("selected project scope is invalid or duplicated")
    failed_ids = {failure.project_id for failure in failures}
    if any(not project_id.isdigit() for project_id in failed_ids):
        raise RuntimeError("project failure set contains an invalid ID")
    selected_set = set(selected_ids)
    effective = selected_set - failed_ids
    filtered_projects = [row for row in projects if str(row.get("project_id", "")) in effective]
    filtered_visits = [row for row in visits if str(row.get("project_id", "")) in effective]
    if len({str(row.get("project_id", "")) for row in filtered_projects}) != len(filtered_projects):
        raise RuntimeError("project acquisition results contain duplicate IDs")
    if any(str(row.get("project_id", "")) not in selected_set for row in projects + visits):
        raise RuntimeError("project acquisition data contains an ID outside selected scope")
    restored_state = restore_failed_type_assignments(state, previous_state, failed_ids)
    filtered_selected = [item for item in selected if str(item["project_id"]) in effective]
    return filtered_selected, filtered_projects, filtered_visits, restored_state, failed_ids


def partition_project_results(
    selected_ids: Iterable[str | int],
    results: Iterable[Mapping[str, Any]],
) -> tuple[set[str], set[str]]:
    """Validate exact per-ID acquisition coverage, returning success and scoped-failure IDs.

    Missing/duplicate/unexpected records and unknown states remain global errors;
    only explicit FAILED or SEMANTIC_FAILURE outcomes are isolatable.
    """
    expected = [str(project_id) for project_id in selected_ids]
    if any(not project_id.isdigit() for project_id in expected) or len(expected) != len(set(expected)):
        raise RuntimeError("selected project IDs are invalid or duplicated")
    expected_set = set(expected)
    acquired: set[str] = set()
    failed: set[str] = set()
    seen: set[str] = set()
    for result in results:
        project_id = str(result.get("project_id", ""))
        if project_id not in expected_set or project_id in seen:
            raise RuntimeError("project acquisition result set is unexpected or duplicated")
        seen.add(project_id)
        state = result.get("acquisition_state")
        if state == "ACQUIRED":
            acquired.add(project_id)
        elif state in {"FAILED", "SEMANTIC_FAILURE"}:
            failed.add(project_id)
        else:
            raise RuntimeError("project acquisition result has an unknown state")
    if seen != expected_set:
        raise RuntimeError("project acquisition results do not cover the selected ID set")
    return acquired, failed
