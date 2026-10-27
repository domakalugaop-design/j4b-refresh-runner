"""Controller–Claims association builder.

Joins historical claim episodes (claims_mart_90d) with the current
controller snapshot (controller_report_snapshot) using action_id as the
primary key and visit_id as the secondary key. Produces three analytical
grains for TEST publication.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

CONTROLLER_CURRENT_FIELDS = [
    "publication_id", "captured_at_utc", "project_id", "project_name",
    "controller_id", "controller_fio", "unique_visits_observed",
    "action_rows_observed", "coverage_status",
]

CLAIMS_CONTROLLER_FIELDS = [
    "claim_publication_id", "episode_id", "action_id", "visit_id",
    "project_id", "project_name", "opened_at_utc",
    "observed_controller_id", "observed_controller_fio",
    "controller_observed_at_utc", "association_method",
    "association_status", "quality_flags",
]

COVERAGE_FIELDS = [
    "publication_id", "captured_at_utc", "population_definition",
    "projects_targeted", "projects_collected", "visits_observed",
    "visits_without_controller", "claim_episodes_considered",
    "claims_matched", "claims_unmatched", "claims_ambiguous",
    "source_timestamp", "completeness_status",
]


def _as_str(v: Any) -> str:
    if v is None:
        return ""
    return str(v)


def _as_int(v: Any) -> int | None:
    if v in (None, ""):
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


def build_controller_current(
    ctrl_snapshot: list[dict],
    proj_names: dict[str, str],
    publication_id: str,
    captured_at_utc: str,
) -> list[dict]:
    """Build controller_current_observed grain: one row per (controller, project)."""
    by_ctrl_proj: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"visits": set(), "actions": set()}
    )
    for row in ctrl_snapshot:
        ctrl_id = _as_str(row.get("corrector_id"))
        ctrl_fio = _as_str(row.get("corrector_fio"))
        proj_id = _as_str(row.get("project_id"))
        visit_id = _as_str(row.get("visit_id"))
        action_id = _as_str(row.get("action_id"))
        if not ctrl_id or not proj_id:
            continue
        key = (ctrl_id, proj_id)
        by_ctrl_proj[key]["visits"].add(visit_id)
        by_ctrl_proj[key]["actions"].add(action_id)

    rows = []
    for (ctrl_id, proj_id), vals in sorted(by_ctrl_proj.items()):
        # Find FIO for this controller_id
        ctrl_fio = ""
        for row in ctrl_snapshot:
            if _as_str(row.get("corrector_id")) == ctrl_id:
                ctrl_fio = _as_str(row.get("corrector_fio"))
                break
        proj_name = proj_names.get(proj_id, "")
        rows.append({
            "publication_id": publication_id,
            "captured_at_utc": captured_at_utc,
            "project_id": proj_id,
            "project_name": proj_name,
            "controller_id": ctrl_id,
            "controller_fio": ctrl_fio,
            "unique_visits_observed": len(vals["visits"]),
            "action_rows_observed": len(vals["actions"]),
            "coverage_status": "OBSERVED_IN_CURRENT_SNAPSHOT",
        })
    return rows


def build_claims_controller_association(
    claims: list[dict],
    ctrl_snapshot: list[dict],
    proj_names: dict[str, str],
    publication_id: str,
    captured_at_utc: str,
) -> list[dict]:
    """Build claims_controller_association grain: one row per claim episode.

    Association priority:
    1. action_id exact match in controller_report_snapshot
    2. visit_id match in controller_report_snapshot (if unique controller)
    3. UNMATCHED / AMBIGUOUS
    """
    # Build lookup maps from controller snapshot
    by_action: dict[str, dict] = {}
    by_visit: dict[str, list[dict]] = defaultdict(list)
    for row in ctrl_snapshot:
        action_id = _as_str(row.get("action_id"))
        visit_id = _as_str(row.get("visit_id"))
        if action_id:
            by_action[action_id] = row
        if visit_id:
            by_visit[visit_id].append(row)

    rows = []
    for claim in claims:
        action_id = _as_str(claim.get("action_id"))
        visit_id = _as_str(claim.get("visit_id"))
        proj_id = _as_str(claim.get("project_id"))
        episode_id = f"{action_id}|{claim.get('opened_at_utc', '')}"

        proj_name = proj_names.get(proj_id, "")
        opened_at = _as_str(claim.get("opened_at_utc"))

        # Priority 1: action_id exact match
        ctrl_row = by_action.get(action_id)
        association_method = ""
        association_status = ""
        quality_flags: list[str] = []
        ctrl_id = ""
        ctrl_fio = ""
        ctrl_observed_at = ""

        if ctrl_row:
            ctrl_id = _as_str(ctrl_row.get("corrector_id"))
            ctrl_fio = _as_str(ctrl_row.get("corrector_fio"))
            association_method = "ACTION_ID_EXACT"
            association_status = "MATCHED"
            ctrl_observed_at = _as_str(ctrl_row.get("report_date"))
        else:
            # Priority 2: visit_id match (if unique controller for that visit)
            visit_rows = by_visit.get(visit_id, [])
            if len(visit_rows) == 1:
                ctrl_row = visit_rows[0]
                ctrl_id = _as_str(ctrl_row.get("corrector_id"))
                ctrl_fio = _as_str(ctrl_row.get("corrector_fio"))
                association_method = "VISIT_ID_UNIQUE"
                association_status = "MATCHED"
                ctrl_observed_at = _as_str(ctrl_row.get("report_date"))
            elif len(visit_rows) > 1:
                # Multiple controllers for same visit - ambiguous
                fios = set(_as_str(r.get("corrector_fio")) for r in visit_rows if _as_str(r.get("corrector_fio")))
                ids = set(_as_str(r.get("corrector_id")) for r in visit_rows if _as_str(r.get("corrector_id")))
                association_method = "VISIT_ID_MULTIPLE"
                association_status = "AMBIGUOUS"
                quality_flags.append("MULTIPLE_CONTROLLERS_ON_VISIT")
                ctrl_id = "|".join(sorted(ids))
                ctrl_fio = "|".join(sorted(fios))
            else:
                association_method = "NO_CONTROLLER_SNAPSHOT"
                association_status = "UNMATCHED"
                quality_flags.append("NO_CONTROLLER_IN_SNAPSHOT")

        proj_name = proj_names.get(_as_str(claim.get("project_id")), "")

        rows.append({
            "claim_publication_id": publication_id,
            "episode_id": episode_id,
            "action_id": _as_str(claim.get("action_id")),
            "visit_id": _as_str(claim.get("visit_id")),
            "project_id": _as_str(claim.get("project_id")),
            "project_name": proj_name,
            "opened_at_utc": _as_str(claim.get("opened_at_utc")),
            "observed_controller_id": ctrl_id,
            "observed_controller_fio": ctrl_fio,
            "controller_observed_at_utc": ctrl_observed_at,
            "association_method": association_method,
            "association_status": association_status,
            "quality_flags": ";".join(sorted(quality_flags)),
        })
    return rows


def build_coverage_status(
    claims: list[dict],
    ctrl_snapshot: list[dict],
    publication_id: str,
    captured_at_utc: str,
) -> list[dict]:
    """Build controller_coverage_status grain: one summary row."""
    # Controller snapshot stats
    ctrl_visits = set(_as_str(r.get("visit_id")) for r in ctrl_snapshot if _as_str(r.get("visit_id")))
    ctrl_projects = set(_as_str(r.get("project_id")) for r in ctrl_snapshot if _as_str(r.get("project_id")))
    ctrl_controllers = set(_as_str(r.get("corrector_id")) for r in ctrl_snapshot if _as_str(r.get("corrector_id")))

    # Claims stats
    claim_visits = set(_as_str(c.get("visit_id")) for c in claims if _as_str(c.get("visit_id")))
    claim_projects = set(_as_str(c.get("project_id")) for c in claims if _as_str(c.get("project_id")))

    # Association stats
    by_action = {_as_str(r.get("action_id")): r for r in ctrl_snapshot if _as_str(r.get("action_id"))}
    by_visit = defaultdict(list)
    for r in ctrl_snapshot:
        vid = _as_str(r.get("visit_id"))
        if vid:
            by_visit[vid].append(r)

    matched = 0
    unmatched = 0
    ambiguous = 0
    for claim in claims:
        action_id = _as_str(claim.get("action_id"))
        visit_id = _as_str(claim.get("visit_id"))
        if action_id in by_action:
            matched += 1
        elif visit_id in by_visit:
            if len(by_visit[visit_id]) == 1:
                matched += 1
            else:
                ambiguous += 1
        else:
            unmatched += 1

    return [{
        "publication_id": publication_id,
        "captured_at_utc": captured_at_utc,
        "population_definition": "claims_mart_90d episodes joined to controller_report_snapshot via action_id then visit_id",
        "projects_targeted": len(claim_projects),
        "projects_collected": len(ctrl_projects),
        "visits_observed": len(ctrl_visits),
        "visits_without_controller": len(claim_visits - set(by_visit.keys())),
        "claim_episodes_considered": len(claims),
        "claims_matched": matched,
        "claims_unmatched": unmatched,
        "claims_ambiguous": ambiguous,
        "source_timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "completeness_status": "PARTIAL_CONTROLLER_SNAPSHOT_57_PROJECTS_22_CONTROLLERS",
    }]


def fingerprint(rows: list[dict], fields: list[str]) -> str:
    payload = "\n".join(
        "\x1f".join(_as_str(row.get(f, "")) for f in fields) for row in rows
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def publication_id_for(prefix: str, fp: str) -> str:
    return f"{prefix}-{fp[:16]}"