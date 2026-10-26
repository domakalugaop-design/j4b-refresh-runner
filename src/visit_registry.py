"""Visit-level registry extraction from per-project Portal /action responses.

The production collector already downloads these responses daily
(``acquire_project`` posts ``/action`` with ``proj=[id]``, every workflow
state selected and ``limit=10000``).  This module parses the same HTML into
strict, fail-closed fact rows and derives the two analytical grains:

- visit grain: one record per ``(project_id, visit_id)``;
- association grain: one record per action row (preserves multiple action
  rows and controller associations per visit).

Design constraints (evidence: docs/PORTAL_ACQUISITION_ARCHITECTURE.md):

- action rows are NOT visits; deduplication by visit_id is mandatory;
- visit datetime may be a PLANNED future date; timezone semantics are
  unconfirmed; claim occurrence time is UNKNOWN;
- controller identity is a FIO string only (no verified controller id in
  list responses); it must never be treated as a globally unique id;
- unexpected HTML must raise instead of producing valid-looking records.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from typing import Any, Iterable

from .parsers import ACTION_LINK_RE, VISIT_LINK_RE
from .workflow_analytics import WORKFLOW_STATES

__all__ = [
    "VisitRegistryError",
    "PARSE_FIELDS",
    "VISIT_GRAIN_FIELDS",
    "MANIFEST_FIELDS",
    "parse_action_fact_rows",
    "visit_grain",
    "association_grain",
    "build_registry_snapshot",
    "snapshot_fingerprint",
    "reconcile_claims",
    "verify_test_projects",
    "TEST_PROJECT_CANDIDATES",
]

TEST_PROJECT_CANDIDATES = ("2995", "3359", "3360")

_DATE_TIME_RE = re.compile(r"^(\d{2})\.(\d{2})\.(\d{4}) (\d{2}):(\d{2})")
_FIO_RE = re.compile(r"^[А-Яа-ёЁ][А-Яа-ёЁ-]+ [А-Яа-ёЁ][А-Яа-ёЁ-]+( [А-Яа-ёЁ][А-Яа-ёЁ-]+)?$")
_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.IGNORECASE | re.DOTALL)
_ROW_RE = re.compile(r"<tr[^>]*>.*?</tr>", re.IGNORECASE | re.DOTALL)
_USER_LINK_RE = re.compile(r"/user/(\d+)", re.IGNORECASE)
_PROJ_LINK_RE = re.compile(r"/proj/(\d+)", re.IGNORECASE)

PARSE_FIELDS = (
    "project_id",
    "visit_id",
    "action_id",
    "participant_assigned",
    "visit_datetime_raw",
    "visit_datetime",
    "report_datetime_raw",
    "report_datetime",
    "workflow_status_code",
    "workflow_status_label",
    "workflow_state_codes",
    "controller_fio",
    "visitor_user_id",
    "project_name_short",
    "source_url",
    "captured_at_utc",
    "quality_flags",
)

VISIT_GRAIN_FIELDS = (
    "project_id",
    "visit_id",
    "visit_datetime",
    "report_datetime",
    "workflow_status_code",
    "workflow_status_codes",
    "controller_fio",
    "controller_fio_all",
    "visitor_user_id",
    "action_row_count",
    "action_ids",
    "has_unassigned_row",
    "project_name_short",
    "captured_at_utc",
    "source_url",
    "quality_flags",
)

MANIFEST_FIELDS = (
    "snapshot_id",
    "captured_at_utc",
    "source_generation",
    "source_identity",
    "source_url",
    "response_rows",
    "assigned_rows",
    "unassigned_rows",
    "distinct_visits",
    "distinct_projects",
    "project_ids",
    "status_code_counts",
    "claim_visit_count",
    "quality_flag_counts",
    "collection_completeness",
    "coverage_scope",
    "date_semantics",
)

DATE_SEMANTICS = (
    "VISIT_DATETIME_MAY_BE_PLANNED_FUTURE; TZ_UNCONFIRMED; "
    "REPORT_DATETIME_IS_SUBMISSION; CLAIM_OCCURRENCE_TIME_UNKNOWN"
)


class VisitRegistryError(RuntimeError):
    """Raised when response HTML does not match the verified structure."""


def _text(cell: str) -> str:
    return re.sub(r"<[^>]+>", " ", cell).replace("&nbsp;", " ").strip()


def _cells(row: str) -> list[str]:
    return [m.group(1) for m in _TD_RE.finditer(row)]


def _normalize_datetime(raw: str) -> str:
    match = _DATE_TIME_RE.match(raw)
    if not match:
        raise VisitRegistryError(f"malformed portal datetime: {raw[:32]}")
    day, month, year, hour, minute = (int(part) for part in match.groups())
    if not (1 <= month <= 12 and 1 <= day <= 31 and hour < 24 and minute < 60):
        raise VisitRegistryError(f"malformed portal datetime: {raw[:32]}")
    return f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}"


def _status_extraction(row: str) -> tuple[list[str], str]:
    """Mirror ``parse_action_table`` workflow-state extraction rules."""
    links = [(identifier, _text(body)) for identifier, body in ACTION_LINK_RE.findall(row)]
    numeric = [text for _, text in links if text.isdigit()]
    codes = [text for text in numeric if int(text) in WORKFLOW_STATES]
    labels = [text for _, text in links if not text.isdigit()]
    normalized_labels = {" ".join(label.split()).casefold() for label in labels}
    known_labels = {" ".join(str(label).split()).casefold() for label in WORKFLOW_STATES.values()}
    if not codes and labels and (normalized_labels & known_labels):
        unknown = next((text for text in numeric if int(text) not in WORKFLOW_STATES), None)
        if unknown is not None:
            raise VisitRegistryError(f"unknown workflow state code: {unknown}")
    status_label = ""
    for label in reversed(labels):
        label = label.strip()
        if label and not label.isdigit():
            status_label = label
            break
    return codes, status_label


def _parse_assigned_row(
    row: str, cells: list[str], project_id: str, captured_at_utc: str, source_url: str
) -> dict[str, Any]:
    visit_match = VISIT_LINK_RE.search(row)
    if not visit_match:
        raise VisitRegistryError("assigned row without visit anchor")
    visit_id = visit_match.group(1)
    action_match = ACTION_LINK_RE.search(row)
    if not action_match:
        raise VisitRegistryError(f"assigned row without action link (visit {visit_id})")
    action_id = action_match.group(1)
    proj_match = _PROJ_LINK_RE.search(row)
    if not proj_match:
        raise VisitRegistryError(f"assigned row without project link (visit {visit_id})")
    if proj_match.group(1) != project_id:
        raise VisitRegistryError(
            f"row project {proj_match.group(1)} does not match query project {project_id}"
        )
    visit_index = next((i for i, cell in enumerate(cells) if VISIT_LINK_RE.search(cell)), None)
    if visit_index is None:
        raise VisitRegistryError(f"visit link cell not found (visit {visit_id})")

    flags: list[str] = []
    visit_raw = _text(cells[visit_index + 1]) if visit_index + 1 < len(cells) else ""
    if visit_raw and not _DATE_TIME_RE.match(visit_raw):
        raise VisitRegistryError(f"unexpected visit datetime cell (visit {visit_id}): {visit_raw[:32]}")
    price_raw = _text(cells[visit_index + 2]) if visit_index + 2 < len(cells) else ""
    if price_raw and not price_raw.isdigit():
        raise VisitRegistryError(f"unexpected price cell (visit {visit_id}): {price_raw[:32]}")
    report_raw = _text(cells[visit_index + 4]) if visit_index + 4 < len(cells) else ""
    if report_raw and not _DATE_TIME_RE.match(report_raw):
        raise VisitRegistryError(f"unexpected report datetime cell (visit {visit_id}): {report_raw[:32]}")

    codes, status_label = _status_extraction(row)
    if not codes:
        flags.append("STATUS_UNRESOLVED")
    if visit_raw and not _DATE_TIME_RE.fullmatch(visit_raw):
        flags.append("VISIT_DATETIME_ANNOTATED")
    if not visit_raw:
        flags.append("VISIT_DATETIME_ABSENT")
    if not report_raw:
        flags.append("REPORT_DATETIME_ABSENT")

    controller_raw = _text(cells[-2]) if len(cells) >= 2 else ""
    controller_fio = ""
    if not controller_raw:
        flags.append("CONTROLLER_FIO_ABSENT")
    elif _FIO_RE.match(controller_raw):
        controller_fio = " ".join(controller_raw.split())
    else:
        flags.append("CONTROLLER_FIO_UNPARSED")

    user_match = _USER_LINK_RE.search(row)
    if not user_match:
        flags.append("VISITOR_LINK_ABSENT")

    return {
        "project_id": project_id,
        "visit_id": visit_id,
        "action_id": action_id,
        "participant_assigned": True,
        "visit_datetime_raw": visit_raw,
        "visit_datetime": _normalize_datetime(visit_raw) if visit_raw else "",
        "report_datetime_raw": report_raw,
        "report_datetime": _normalize_datetime(report_raw) if report_raw else "",
        "workflow_status_code": codes[-1] if codes else "",
        "workflow_status_label": status_label,
        "workflow_state_codes": list(codes),
        "controller_fio": controller_fio,
        "visitor_user_id": user_match.group(1) if user_match else "",
        "project_name_short": _text(cells[-1]) if cells else "",
        "source_url": source_url,
        "captured_at_utc": captured_at_utc,
        "quality_flags": sorted(set(flags)),
    }


def _parse_unassigned_row(
    row: str, cells: list[str], project_id: str, captured_at_utc: str, source_url: str
) -> dict[str, Any]:
    visit_match = VISIT_LINK_RE.search(row)
    if not visit_match:
        raise VisitRegistryError("unassigned row without visit anchor")
    visit_id = visit_match.group(1)
    dates = [cell for cell in (_text(c) for c in cells) if _DATE_TIME_RE.match(cell)]
    flags = ["UNASSIGNED_ROW"]
    if dates:
        # Unassigned rows are not expected to carry dates; if one appears,
        # keep it but flag the anomaly instead of silently using it.
        flags.append("UNASSIGNED_ROW_CARRIES_DATE")
    codes, status_label = _status_extraction(row)
    if codes:
        flags.append("UNASSIGNED_ROW_CARRIES_STATE_CODE")
    return {
        "project_id": project_id,
        "visit_id": visit_id,
        "action_id": "",
        "participant_assigned": False,
        "visit_datetime_raw": "",
        "visit_datetime": "",
        "report_datetime_raw": "",
        "report_datetime": "",
        "workflow_status_code": "",
        "workflow_status_label": "",
        "workflow_state_codes": [],
        "controller_fio": "",
        "visitor_user_id": "",
        "project_name_short": _text(cells[-1]) if cells else "",
        "source_url": source_url,
        "captured_at_utc": captured_at_utc,
        "quality_flags": sorted(set(flags)),
    }


def parse_action_fact_rows(
    html: str,
    project_id: str,
    *,
    captured_at_utc: str,
    source_url: str,
) -> list[dict[str, Any]]:
    """Parse one per-project ``POST /action`` response into fact rows.

    Fail-closed: any row that does not match the verified structure raises
    ``VisitRegistryError`` instead of producing a partial record.
    """
    if not project_id:
        raise VisitRegistryError("project_id is required")
    facts: list[dict[str, Any]] = []
    for row_match in _ROW_RE.finditer(html):
        row = row_match.group(0)
        has_visit = VISIT_LINK_RE.search(row) is not None
        has_user = _USER_LINK_RE.search(row) is not None
        if not has_visit:
            if has_user:
                raise VisitRegistryError("row with visitor link but without visit anchor")
            continue
        cells = _cells(row)
        red = "table-red" in row
        action_match = ACTION_LINK_RE.search(row)
        # Red styling alone does not mean unassigned: per-project responses
        # render ordinary rows (e.g. status 15 rejections) red.  The
        # unassigned branch is reserved for sentinel rows without a real
        # action link (or with the /action/0 sentinel), matching the
        # ``parse_action_table`` semantics.
        sentinel_unassigned = red and (
            action_match is None or action_match.group(1) == "0"
        )
        if sentinel_unassigned:
            facts.append(_parse_unassigned_row(row, cells, project_id, captured_at_utc, source_url))
        else:
            facts.append(_parse_assigned_row(row, cells, project_id, captured_at_utc, source_url))
    if not facts:
        raise VisitRegistryError("no visit rows found in response")
    return facts


def visit_grain(facts: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate action facts to one record per ``(project_id, visit_id)``."""
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for fact in facts:
        grouped[(str(fact["project_id"]), str(fact["visit_id"]))].append(fact)
    grains: list[dict[str, Any]] = []
    for (project_id, visit_id), rows in sorted(grouped.items()):
        flags: set[str] = set()
        for row in rows:
            flags.update(row["quality_flags"])

        visit_dates = sorted({row["visit_datetime"] for row in rows if row["visit_datetime"]})
        if len(visit_dates) > 1:
            flags.add("VISIT_DATETIME_CONFLICT")
        report_dates = sorted({row["report_datetime"] for row in rows if row["report_datetime"]})
        if len(report_dates) > 1:
            flags.add("REPORT_DATETIME_CONFLICT")

        current_codes = sorted({row["workflow_status_code"] for row in rows if row["workflow_status_code"]})
        if len(current_codes) > 1:
            flags.add("MULTIPLE_CURRENT_STATUSES")
        all_codes: list[str] = []
        for row in rows:
            for code in row["workflow_state_codes"]:
                if code not in all_codes:
                    all_codes.append(code)

        fios = sorted({row["controller_fio"] for row in rows if row["controller_fio"]})
        if len(fios) > 1:
            flags.add("MULTIPLE_CONTROLLERS")

        grains.append({
            "project_id": project_id,
            "visit_id": visit_id,
            "visit_datetime": visit_dates[-1] if visit_dates else "",
            "report_datetime": report_dates[-1] if report_dates else "",
            "workflow_status_code": current_codes[0] if len(current_codes) == 1 else "",
            "workflow_status_codes": "|".join(current_codes),
            "controller_fio": fios[0] if len(fios) == 1 else "",
            "controller_fio_all": "|".join(fios),
            "visitor_user_id": next((row["visitor_user_id"] for row in rows if row["visitor_user_id"]), ""),
            "action_row_count": sum(1 for row in rows if row["participant_assigned"]),
            "action_ids": "|".join(str(row["action_id"]) for row in rows if row["action_id"]),
            "has_unassigned_row": any(not row["participant_assigned"] for row in rows),
            "project_name_short": rows[0]["project_name_short"],
            "captured_at_utc": max(row["captured_at_utc"] for row in rows),
            "source_url": rows[0]["source_url"],
            "quality_flags": sorted(flags),
        })
    return grains


def association_grain(facts: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Association grain: one record per action row (no deduplication)."""
    return [dict(fact) for fact in facts]


def snapshot_fingerprint(facts: Iterable[dict[str, Any]]) -> str:
    canonical = json.dumps(
        sorted(json.dumps(fact, sort_keys=True, ensure_ascii=False) for fact in facts),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_registry_snapshot(
    facts: Iterable[dict[str, Any]],
    *,
    source_identity: str,
    source_generation: str,
    captured_at_utc: str,
    source_url: str,
    coverage_scope: str,
    collection_completeness: str = "COMPLETE_RESPONSE_PARSED",
) -> dict[str, Any]:
    fact_list = [dict(fact) for fact in facts]
    if not fact_list:
        raise VisitRegistryError("cannot build a snapshot from an empty fact set")
    snapshot_id = hashlib.sha256(
        json.dumps(
            {
                "facts": sorted(
                    json.dumps(fact, sort_keys=True, ensure_ascii=False) for fact in fact_list
                ),
                "source_identity": source_identity,
                "captured_at_utc": captured_at_utc,
            },
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    for fact in fact_list:
        fact["snapshot_id"] = snapshot_id
        fact["source_generation"] = source_generation
        fact["source_identity"] = source_identity

    assigned = [fact for fact in fact_list if fact["participant_assigned"]]
    visits = visit_grain(fact_list)
    status_counts = Counter(
        fact["workflow_status_code"] for fact in assigned if fact["workflow_status_code"]
    )
    flag_counts = Counter(flag for fact in fact_list for flag in fact["quality_flags"])
    claim_visits = {
        visit["visit_id"]
        for visit in visits
        if "39" in (visit["workflow_status_codes"].split("|") if visit["workflow_status_codes"] else [])
    }
    manifest = {
        "snapshot_id": snapshot_id,
        "captured_at_utc": captured_at_utc,
        "source_generation": source_generation,
        "source_identity": source_identity,
        "source_url": source_url,
        "response_rows": len(fact_list),
        "assigned_rows": len(assigned),
        "unassigned_rows": len(fact_list) - len(assigned),
        "distinct_visits": len({(v["project_id"], v["visit_id"]) for v in visits}),
        "distinct_projects": len({fact["project_id"] for fact in fact_list}),
        "project_ids": "|".join(sorted({fact["project_id"] for fact in fact_list})),
        "status_code_counts": json.dumps(dict(sorted(status_counts.items())), ensure_ascii=False),
        "claim_visit_count": len(claim_visits),
        "quality_flag_counts": json.dumps(dict(sorted(flag_counts.items())), ensure_ascii=False),
        "collection_completeness": collection_completeness,
        "coverage_scope": coverage_scope,
        "date_semantics": DATE_SEMANTICS,
    }
    return {
        "snapshot_id": snapshot_id,
        "manifest": manifest,
        "facts": fact_list,
        "visit_grain": visits,
        "fingerprint": snapshot_fingerprint(fact_list),
    }


def verify_test_projects(
    name_by_id: dict[str, str], candidates: Iterable[str] = TEST_PROJECT_CANDIDATES
) -> tuple[list[str], list[str]]:
    """Verify test-project candidates by observed project naming.

    Returns ``(verified_ids, rejected_candidates)``.  Only projects whose
    observed name contains the test marker may be excluded from KPI scopes.
    """
    verified: list[str] = []
    rejected: list[str] = []
    for candidate in candidates:
        name = name_by_id.get(str(candidate), "")
        if name and "тест" in name.casefold():
            verified.append(str(candidate))
        else:
            rejected.append(str(candidate))
    return verified, rejected


def reconcile_claims(
    current_facts: Iterable[dict[str, Any]],
    native_facts: Iterable[dict[str, Any]],
    stored_rows: Iterable[dict[str, Any]],
    *,
    exclude_project_ids: Iterable[str] = (),
) -> dict[str, Any]:
    """Reconcile three status-39 observation sets.

    - ``current_facts``: facts parsed from per-project /action responses
      (current workflow membership per visit);
    - ``native_facts``: facts parsed from the native ``state[39]`` filtered
      query response;
    - ``stored_rows``: rows from the append-only claim snapshot store
      (dicts with at least ``visit_id``, ``project_id``, ``status_id``).

    Membership of code 39 in a cached or current response is NOT equivalent
    to a current status-39 observation; the reconciliation only compares
    visit-id sets and reports differences instead of resolving them.
    """
    exclude = {str(pid) for pid in exclude_project_ids}

    def _current_claim_visits(facts: Iterable[dict[str, Any]]) -> set[str]:
        visits: set[str] = set()
        for fact in facts:
            if str(fact.get("project_id")) in exclude:
                continue
            codes = fact.get("workflow_state_codes") or []
            if isinstance(codes, str):
                codes = [code for code in codes.split("|") if code]
            if "39" in {str(code) for code in codes}:
                visits.add(str(fact["visit_id"]))
        return visits

    current_visits = _current_claim_visits(current_facts)
    native_visits = _current_claim_visits(native_facts)
    stored_visits = {
        str(row["visit_id"])
        for row in stored_rows
        if str(row.get("project_id")) not in exclude and str(row.get("status_id")) == "39"
    }
    only_current = sorted(current_visits - native_visits - stored_visits)
    only_native = sorted(native_visits - current_visits - stored_visits)
    only_stored = sorted(stored_visits - current_visits - native_visits)
    consistent = current_visits == native_visits == stored_visits
    return {
        "reconciliation_status": "CONSISTENT" if consistent else "DIFFERENCES_FOUND",
        "matched_visits": sorted(current_visits & native_visits & stored_visits),
        "only_in_current_response": only_current,
        "only_in_native_query": only_native,
        "only_in_stored_snapshot": only_stored,
        "excluded_project_ids": sorted(exclude),
        "claim_occurrence_time": "UNKNOWN",
    }
