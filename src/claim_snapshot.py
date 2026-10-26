"""Bounded, append-only claim-observation snapshots from the Portal /action
status-39 search.

The response of ``POST /action`` with ``state[39]=on`` is a current-state
search result.  It is NOT a transition history and NOT a count of separate
claim messages: one visit in status 39 is one observed visit with a claim
indicator.

Grain of one snapshot observation: ``(snapshot_id, project_id, visit_id,
action_id, status_id)``.  Unassigned ``/action/0`` rows carry no workflow
status; they are counted separately and are never published as claim
observations.  ``report_id`` is never invented: this source does not prove
that ``action_id`` equals ``report_id``.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

CLAIM_STATUS_ID = 39
CLAIM_STATUS_LABEL = "Есть претензия"
UNASSIGNED_ACTION_ID = "0"

SNAPSHOT_FIELDS = (
    "snapshot_id",
    "captured_at_utc",
    "source_generation",
    "report_id",
    "visit_id",
    "project_id",
    "controller_id",
    "status_id",
    "report_date",
    "visit_date",
    "source_quality",
    "published_at_utc",
)

PROVENANCE_FIELDS = (
    "action_id",
    "action_url",
    "observed_datetimes",
    "date_semantics",
    "response_sha256",
    "query_window",
    "date_filter_enforced",
    "unassigned_rows",
    "response_rows",
)

MANIFEST_FIELDS = (
    "snapshot_id",
    "captured_at_utc",
    "source_generation",
    "response_sha256",
    "response_rows",
    "unassigned_rows",
    "claim_observation_count",
    "distinct_visits",
    "distinct_projects",
    "query_window",
    "date_filter_enforced",
    "pagination_markers",
    "collection_completeness",
    "collection_mode",
    "warnings",
)

_ROW_RE = re.compile(r"<tr[^>]*>.*?</tr>", re.IGNORECASE | re.DOTALL)
_CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.IGNORECASE | re.DOTALL)
_ACTION_LINK_RE = re.compile(r"/action/(\d+)")
_VISIT_LINK_RE = re.compile(r"/visit/(\d+)")
_PROJECT_LINK_RE = re.compile(r"/proj/(\d+)")
_DATETIME_RE = re.compile(r"\b\d{2}\.\d{2}\.\d{4}(?: \d{2}:\d{2})?\b")
_CLASS_RE = re.compile(r'class="([^"]*)"')


class ClaimSnapshotError(Exception):
    """Fail-closed error for claim snapshot extraction."""


@dataclass(frozen=True)
class ClaimObservation:
    action_id: str
    visit_id: str
    project_id: str
    observed_datetimes: tuple[str, ...]
    status_code_seen: bool
    status_label_seen: bool
    row_class: str


@dataclass(frozen=True)
class ClaimSearch:
    observations: tuple[ClaimObservation, ...]
    unassigned_rows: int
    response_rows: int
    response_sha256: str
    pagination_markers: tuple[str, ...]
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def distinct_visits(self) -> int:
        return len({(o.project_id, o.visit_id) for o in self.observations})

    @property
    def distinct_projects(self) -> int:
        return len({o.project_id for o in self.observations})


def _validate_document(markup: str) -> None:
    lower = markup.casefold()
    if "<html" not in lower and "<!doctype" not in lower:
        raise ClaimSnapshotError("response is not an HTML document")
    if re.search(r"name\s*=\s*[\"']_login[\"']", lower) or re.search(
        r"type\s*=\s*[\"']password[\"']", lower
    ):
        raise ClaimSnapshotError("response is an authentication page")
    if any(marker in lower for marker in ("access denied", "доступ запрещ", "php parse error", "fatal error")):
        raise ClaimSnapshotError("response is an error page")
    if "<table" not in lower:
        raise ClaimSnapshotError("response structural markers missing")


def _cells(row: str) -> list[str]:
    return [
        re.sub(r"<[^>]+>", " ", cell).replace("&nbsp;", " ").strip()
        for cell in _CELL_RE.findall(row)
    ]


def parse_claim_search(markup: str) -> ClaimSearch:
    """Parse a portal-wide ``/action`` state[39] search response.

    Fail closed: rows without a real action link, without visit/project
    links, or without unambiguous status-39 membership are excluded and
    reported as warnings.  Membership is never defaulted to ``False``.
    """
    _validate_document(markup)
    digest = hashlib.sha256(markup.encode("utf-8", "replace")).hexdigest()
    observations: list[ClaimObservation] = []
    warnings: list[str] = []
    unassigned = 0
    total = 0
    seen: set[tuple[str, str, str]] = set()
    pagination = tuple(sorted(set(re.findall(
        r"/action\?(?:[^\"'>]*)(?:page|start|offset|p)=(\d+)", markup, re.IGNORECASE))))

    for match in _ROW_RE.finditer(markup):
        row = match.group(0)
        action_match = _ACTION_LINK_RE.search(row)
        if not action_match:
            continue
        total += 1
        action_id = action_match.group(1)
        if action_id == UNASSIGNED_ACTION_ID:
            unassigned += 1
            continue
        visit_match = _VISIT_LINK_RE.search(row)
        project_match = _PROJECT_LINK_RE.search(row)
        if not visit_match or not project_match:
            warnings.append(f"row_without_visit_or_project_links:{action_id}")
            continue
        visit_id, project_id = visit_match.group(1), project_match.group(1)
        key = (project_id, visit_id, action_id)
        if key in seen:
            warnings.append(f"duplicate_row:{action_id}")
            continue
        seen.add(key)
        cells = _cells(row)
        code_seen = any(cell.split(" ")[0] == str(CLAIM_STATUS_ID) for cell in cells)
        label_seen = CLAIM_STATUS_LABEL in row
        if not code_seen and not label_seen:
            warnings.append(f"row_without_status39_membership:{action_id}")
            continue
        datetimes = tuple(_DATETIME_RE.findall(" ".join(cells)))
        row_class = _CLASS_RE.search(row).group(1) if _CLASS_RE.search(row) else ""
        observations.append(ClaimObservation(
            action_id=action_id, visit_id=visit_id, project_id=project_id,
            observed_datetimes=datetimes, status_code_seen=code_seen,
            status_label_seen=label_seen, row_class=row_class,
        ))

    return ClaimSearch(
        observations=tuple(observations), unassigned_rows=unassigned,
        response_rows=total, response_sha256=digest,
        pagination_markers=pagination, warnings=tuple(warnings),
    )


def build_snapshot_records(
    search: ClaimSearch,
    *,
    snapshot_id: str,
    captured_at_utc: str,
    source_generation: str,
    query_window: str,
    collection_mode: str = "MANUAL_CONTROLLED_QUERY",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build append-only snapshot records plus the snapshot manifest row.

    ``report_id``, ``controller_id``, ``report_date`` and ``visit_date``
    stay empty: this source does not confirm their values or semantics.
    Observed datetimes are kept separately with ``date_semantics`` marked
    ``UNCONFIRMED``; they are never relabelled as business dates.
    """
    records = []
    for observation in search.observations:
        records.append({
            "snapshot_id": snapshot_id,
            "captured_at_utc": captured_at_utc,
            "source_generation": source_generation,
            "report_id": "",
            "visit_id": observation.visit_id,
            "project_id": observation.project_id,
            "controller_id": "",
            "status_id": CLAIM_STATUS_ID,
            "report_date": "",
            "visit_date": "",
            "source_quality": "OBSERVED_CURRENT_STATE_39",
            "published_at_utc": "",
            "action_id": observation.action_id,
            "action_url": f"https://lk.j4b.ru/action/{observation.action_id}",
            "observed_datetimes": " | ".join(observation.observed_datetimes),
            "date_semantics": "UNCONFIRMED",
            "response_sha256": search.response_sha256,
            "query_window": query_window,
            "date_filter_enforced": "NO",
            "unassigned_rows": search.unassigned_rows,
            "response_rows": search.response_rows,
        })
    manifest = {
        "snapshot_id": snapshot_id,
        "captured_at_utc": captured_at_utc,
        "source_generation": source_generation,
        "response_sha256": search.response_sha256,
        "response_rows": search.response_rows,
        "unassigned_rows": search.unassigned_rows,
        "claim_observation_count": len(records),
        "distinct_visits": search.distinct_visits,
        "distinct_projects": search.distinct_projects,
        "query_window": query_window,
        "date_filter_enforced": "NO",
        "pagination_markers": len(search.pagination_markers),
        "collection_completeness": (
            "COMPLETE_CURRENT_STATE_FILTER_RESULT"
            if not search.pagination_markers else "PAGINATED_RESULT_INCOMPLETE"
        ),
        "collection_mode": collection_mode,
        "warnings": " | ".join(search.warnings),
    }
    return records, manifest


def validate_snapshot_records(records: list[dict[str, Any]]) -> None:
    """Fail closed on schema, grain, or attribution violations."""
    if not records:
        raise ClaimSnapshotError("snapshot has no records")
    snapshot_ids = {record["snapshot_id"] for record in records}
    if len(snapshot_ids) != 1:
        raise ClaimSnapshotError("records span multiple snapshot_id values")
    keys = set()
    for record in records:
        missing = [name for name in SNAPSHOT_FIELDS if name not in record]
        if missing:
            raise ClaimSnapshotError(f"record missing required fields: {missing}")
        if record["status_id"] != CLAIM_STATUS_ID:
            raise ClaimSnapshotError("record status_id is not the claim status")
        if record["report_id"] not in ("", None):
            raise ClaimSnapshotError("report_id must stay empty for this source")
        if record["source_quality"] != "OBSERVED_CURRENT_STATE_39":
            raise ClaimSnapshotError("unexpected source_quality value")
        key = (record["project_id"], record["visit_id"], record["action_id"])
        if key in keys:
            raise ClaimSnapshotError(f"duplicate observation grain: {key}")
        keys.add(key)


def snapshot_fingerprint(records: list[dict[str, Any]]) -> str:
    """Canonical fingerprint used for idempotent re-publication checks.

    Values are stringified before hashing: Google Sheets stores text, so a
    published snapshot and its source JSON must fingerprint identically.
    """
    canonical = json.dumps(
        [
            {name: str(record.get(name, "")) for name in SNAPSHOT_FIELDS}
            for record in records
        ],
        ensure_ascii=False, sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
