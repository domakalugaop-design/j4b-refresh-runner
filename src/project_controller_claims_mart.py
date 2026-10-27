"""Project/report/controller/claim source mart with explicit evidence states.

The inputs have different grains and capture times.  This module keeps the
project-visit denominator separate from report/action rows and claim episodes;
it never broadcasts visit counts across actions or imputes a controller.
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone

MONTHS = ("2026-07", "2026-08", "2026-09", "2026-10")

PROJECT_DIRECTORY_FIELDS = [
    "publication_id", "project_id", "project_name", "project_type_code",
    "activity_status", "project_name_status",
]
PROJECT_MONTHLY_FIELDS = [
    "publication_id", "project_id", "project_name", "month",
    "completed_visit_count", "completed_action_count",
    "claim_visit_count_opened_in_month", "claim_episode_count_opened_in_month",
    "claim_visits_linked_to_window_completion_cohort",
    "report_count", "report_count_status", "monthly_claim_rate_pct",
    "monthly_claim_rate_status", "source_completeness",
]
PROJECT_PERIOD_FIELDS = [
    "publication_id", "project_id", "project_name",
    "completed_visit_count", "completed_action_count",
    "claim_visit_count", "claim_episode_count",
    "claim_visits_in_completed_cohort", "claim_rate_pct",
    "claim_rate_status", "report_count", "report_count_status",
    "controller_count", "controller_count_status", "source_completeness",
]
CONTROLLER_MONTHLY_FIELDS = [
    "publication_id", "month", "project_id", "controller_id", "controller_fio",
    "reviewed_report_count", "reviewed_visit_count", "claim_linked_report_count",
    "claim_linked_visit_count", "controller_history_status", "reason",
]
LINK_FIELDS = [
    "publication_id", "episode_id", "action_id", "report_id", "visit_id",
    "project_id", "claim_opened_at_utc", "entry_mechanism",
    "closure_confidence", "action_link_status", "report_link_status",
    "controller_id", "controller_fio", "controller_link_status",
]
MANIFEST_FIELDS = [
    "publication_id", "created_at_utc", "fingerprint", "window_start_utc",
    "window_end_utc", "claims_source_publication_id", "claims_source_capture_utc",
    "completion_source_capture_utc", "completed_actions", "completed_project_visit_pairs",
    "projects_in_completion_cohort", "projects_in_union", "claim_episodes",
    "claim_actions", "claim_project_visit_pairs", "claim_visits_in_completed_cohort",
    "claim_episodes_missing_project_or_visit",
    "claim_rate_pct", "claim_rate_status", "unmapped_completion_actions",
    "claim_rows_after_declared_capture", "projects_with_zero_claims", "project_name_coverage_pct",
    "controller_history_status", "report_denominator_status", "status",
]


class ProjectMartError(ValueError):
    pass


def _as_int(value):
    if value in (None, ""):
        return None
    return int(value)


def _fields_by_project(rows):
    return {int(r["project_id"]): r for r in rows if r.get("project_id") not in (None, "")}


def build_project_mart(completion_source: dict, claims_source: dict,
                       publication_id: str, created_at_utc: str | None = None) -> dict:
    """Build publication rows from aggregated completion facts and episode rows.

    completion_source schema is produced by the bounded extractor:
      project_directory[{project_id, project_name, project_type_code}],
      project_monthly[{project_id, month, completed_action_count,
                       completed_visit_count}],
      completed_visits[{project_id, visit_id, month}],
      totals and source capture metadata.

    claims_source carries rows from TEST `claims_mart_90d` and its manifest.
    """
    claims = claims_source["episodes"]
    if not claims:
        raise ProjectMartError("claims source is empty")
    base_projects = _fields_by_project(completion_source.get("project_directory", []))
    for p in claims_source.get("project_directory", []):
        pid = _as_int(p.get("project_id"))
        if pid is not None and pid not in base_projects:
            base_projects[pid] = p

    completed_keys = {
        (int(r["project_id"]), str(r["visit_id"]))
        for r in completion_source.get("completed_visits", [])
        if r.get("project_id") not in (None, "") and r.get("visit_id") not in (None, "")
    }
    episodes = []
    seen_episode_ids = set()
    claim_by_project = defaultdict(list)
    claim_month_by_project = defaultdict(list)
    claim_visit_month_sets = defaultdict(set)
    for source_row in claims:
        action = str(source_row.get("action_id", ""))
        visit_value = source_row.get("visit_id")
        visit = str(visit_value) if visit_value not in (None, "") else ""
        project = _as_int(source_row.get("project_id"))
        opened = str(source_row.get("opened_at_utc", ""))
        if not action or not opened:
            raise ProjectMartError("claim episode missing action or opened date")
        month = opened[:7]
        episode_id = f"{action}|{opened}"
        if episode_id in seen_episode_ids:
            raise ProjectMartError(f"duplicate claim episode key: {episode_id}")
        seen_episode_ids.add(episode_id)
        pinfo = base_projects.get(project, {"project_id": project, "project_name": ""}) if project is not None else {"project_name": ""}
        ep = {
            "publication_id": publication_id,
            "episode_id": episode_id,
            "action_id": action,
            "report_id": "",
            "visit_id": visit,
            "project_id": project if project is not None else "",
            "claim_opened_at_utc": opened,
            "entry_mechanism": str(source_row.get("entry_mechanism", "")),
            "closure_confidence": str(source_row.get("closure_confidence", "")),
            "action_link_status": "VERIFIED_ACTION_ID_FROM_CLAIMS_MART",
            "report_link_status": "NOT_AVAILABLE_UNIVERSAL_REPORT_ID_EQUIVALENCE_UNPROVEN",
            "controller_id": "",
            "controller_fio": "",
            "controller_link_status": "NOT_AVAILABLE_HISTORICAL_REVIEWER_NOT_PROVEN",
            "project_name": pinfo.get("project_name", ""),
        }
        episodes.append(ep)
        if project is not None:
            claim_by_project[project].append(ep)
            claim_month_by_project[(project, month)].append(ep)
            if visit:
                claim_visit_month_sets[(project, month)].add(visit)

    completion_month = {}
    for row in completion_source.get("project_monthly", []):
        pid, month = int(row["project_id"]), str(row["month"])
        completion_month[(pid, month)] = row
    action_period_count = {
        int(r["project_id"]): int(r["completed_action_count"])
        for r in completion_source.get("project_period", [])
    }
    visit_period_count = defaultdict(set)
    for pid, visit in completed_keys:
        visit_period_count[pid].add(visit)

    episode_period_count = {pid: len(eps) for pid, eps in claim_by_project.items()}
    claim_visit_period = {pid: {e["visit_id"] for e in eps} for pid, eps in claim_by_project.items()}
    matched_claim_visits = {
        pid: (claim_visit_period.get(pid, set()) & visit_period_count.get(pid, set()))
        for pid in base_projects
    }

    directory_rows = []
    for pid, pinfo in sorted(base_projects.items()):
        has_completion = pid in visit_period_count
        has_claim = pid in claim_by_project
        directory_rows.append({
            "publication_id": publication_id,
            "project_id": pid,
            "project_name": pinfo.get("project_name") or "",
            "project_type_code": pinfo.get("project_type_code") if pinfo.get("project_type_code") is not None else "",
            "activity_status": "COMPLETION_COHORT" if has_completion else ("CLAIM_ONLY_OUTSIDE_COMPLETION_COHORT" if has_claim else "NO_ACTIVITY"),
            "project_name_status": "AVAILABLE" if pinfo.get("project_name") else "NOT_AVAILABLE_PROJECT_NAME_MISSING",
        })

    period_rows = []
    for pid, pinfo in sorted(base_projects.items()):
        denom = len(visit_period_count.get(pid, set()))
        matched = len(matched_claim_visits.get(pid, set()))
        capture_anomaly = bool(claims_source.get("capture_anomaly"))
        period_rows.append({
            "publication_id": publication_id,
            "project_id": pid,
            "project_name": pinfo.get("project_name") or "",
            "completed_visit_count": denom,
            "completed_action_count": action_period_count.get(pid, 0),
            "claim_visit_count": len(claim_visit_period.get(pid, set())),
            "claim_episode_count": episode_period_count.get(pid, 0),
            "claim_visits_in_completed_cohort": matched,
            "claim_rate_pct": round(100 * matched / denom, 2) if denom and not capture_anomaly else "",
            "claim_rate_status": (
                "NOT_AVAILABLE_CLAIM_SOURCE_CAPTURE_TIME_PRECEDES_EVENT"
                if capture_anomaly else
                ("AVAILABLE_SAME_WINDOW_PROJECT_VISIT_COHORT" if denom else "NOT_AVAILABLE_NO_COMPLETED_VISIT_DENOMINATOR")
            ),
            "report_count": "",
            "report_count_status": "NOT_AVAILABLE_REPORT_COMPLETION_AND_REPORT_ID_GRAIN_UNPROVEN",
            "controller_count": "",
            "controller_count_status": "NOT_AVAILABLE_HISTORICAL_CONTROLLER_ASSOCIATION_UNPROVEN",
            "source_completeness": "PROJECT_VISIT_COHORT_AND_CLAIMS_EVENT_SOURCE; REPORT_AND_CONTROLLER_GAPS_REMAIN",
        })

    # Monthly rows show distinct visit/action counts by their own event dates.
    # A monthly rate is deliberately blank: a claim-open month is not the same
    # cohort as the month of a later/earlier status-40 completion.
    monthly_keys = {(pid, month) for pid in base_projects for month in MONTHS}
    monthly_rows = []
    controller_rows = []
    for pid, month in sorted(monthly_keys):
        pinfo = base_projects.get(pid, {})
        cm = completion_month.get((pid, month), {})
        eps = claim_month_by_project.get((pid, month), [])
        monthly_rows.append({
            "publication_id": publication_id,
            "project_id": pid,
            "project_name": pinfo.get("project_name") or "",
            "month": month,
            "completed_visit_count": int(cm.get("completed_visit_count", 0)),
            "completed_action_count": int(cm.get("completed_action_count", 0)),
            "claim_visit_count_opened_in_month": len(claim_visit_month_sets.get((pid, month), set())),
            "claim_episode_count_opened_in_month": len(eps),
            "claim_visits_linked_to_window_completion_cohort": len(
                claim_visit_month_sets.get((pid, month), set()) & visit_period_count.get(pid, set())
            ),
            "report_count": "",
            "report_count_status": "NOT_AVAILABLE_REPORT_COMPLETION_AND_REPORT_ID_GRAIN_UNPROVEN",
            "monthly_claim_rate_pct": "",
            "monthly_claim_rate_status": "NOT_AVAILABLE_CLAIM_OPEN_MONTH_AND_COMPLETION_MONTH_ARE_DIFFERENT_COHORTS",
            "source_completeness": "EVENT_COUNTS_ONLY; REPORT_COUNT_AND_MONTHLY_RATE_NOT_VERIFIED",
        })
        controller_rows.append({
            "publication_id": publication_id,
            "month": month,
            "project_id": pid,
            "controller_id": "",
            "controller_fio": "",
            "reviewed_report_count": "",
            "reviewed_visit_count": "",
            "claim_linked_report_count": "",
            "claim_linked_visit_count": "",
            "controller_history_status": "NOT_AVAILABLE",
            "reason": "Available corrector snapshot is current/partial; historical reviewer event semantics and reassignment history are not proven.",
        })

    def without_publication_id(rows):
        return [{k: v for k, v in row.items() if k != "publication_id"} for row in rows]

    fingerprint_payload = {
        "schema_version": 2,
        "completion_capture": completion_source.get("captured_at_utc"),
        "claims_publication": claims_source.get("publication_id"),
        "directory": without_publication_id(directory_rows),
        "monthly": without_publication_id(monthly_rows),
        "period": without_publication_id(period_rows),
        "controllers": without_publication_id(controller_rows),
        "links": [{k: e[k] for k in LINK_FIELDS if k != "publication_id"} for e in episodes],
    }
    fingerprint = hashlib.sha256(json.dumps(fingerprint_payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    capture_tag = (completion_source.get("captured_at_utc") or "unknown").replace(":", "").replace("-", "")
    publication_id = f"pcclaims90-v2-{capture_tag}-{fingerprint[:10]}"
    # Ensure publication_id in each row equals the content-addressed id.
    for rows in (directory_rows, period_rows, monthly_rows, controller_rows, episodes):
        for row in rows:
            row["publication_id"] = publication_id

    claim_visits_total = sum(len(v) for v in claim_visit_period.values())
    matched_total = sum(len(v) for v in matched_claim_visits.values())
    total_denominator = sum(len(v) for v in visit_period_count.values())
    capture_anomaly = bool(claims_source.get("capture_anomaly"))
    claim_rate = round(100 * matched_total / total_denominator, 2) if total_denominator and not capture_anomaly else ""
    names_available = sum(bool(p.get("project_name")) for p in base_projects.values())
    zero_claim_projects = sum(1 for pid in base_projects if not claim_by_project.get(pid))
    created_at = created_at_utc or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest = {
        "publication_id": publication_id,
        "created_at_utc": created_at,
        "fingerprint": fingerprint,
        "window_start_utc": "2026-07-25T21:00:00Z",
        "window_end_utc": "2026-10-26T21:00:00Z",
        "claims_source_publication_id": claims_source.get("publication_id", ""),
        "claims_source_capture_utc": claims_source.get("captured_at_utc", ""),
        "completion_source_capture_utc": completion_source.get("captured_at_utc", ""),
        "completed_actions": int(completion_source.get("totals", {}).get("distinct_actions", 0)),
        "completed_project_visit_pairs": total_denominator,
        "projects_in_completion_cohort": int(completion_source.get("totals", {}).get("project_count", 0)),
        "projects_in_union": len(base_projects),
        "claim_episodes": len(episodes),
        "claim_actions": len({e["action_id"] for e in episodes}),
        "claim_project_visit_pairs": claim_visits_total,
        "claim_visits_in_completed_cohort": matched_total,
        "claim_episodes_missing_project_or_visit": sum(1 for e in episodes if not e["project_id"] or not e["visit_id"]),
        "claim_rate_pct": claim_rate if claim_rate != "" else "",
        "claim_rate_status": (
            "NOT_AVAILABLE_CLAIM_SOURCE_CAPTURE_TIME_PRECEDES_EVENT"
            if capture_anomaly else
            ("AVAILABLE_SAME_WINDOW_PROJECT_VISIT_COHORT;SOURCE_CAPTURES_DIFFER" if total_denominator else "NOT_AVAILABLE_NO_DENOMINATOR")
        ),
        "unmapped_completion_actions": int(completion_source.get("totals", {}).get("missing_action_map", 0)),
        "claim_rows_after_declared_capture": int(claims_source.get("rows_after_declared_capture", 0)),
        "projects_with_zero_claims": zero_claim_projects,
        "project_name_coverage_pct": round(100 * names_available / len(base_projects), 2) if base_projects else 0,
        "controller_history_status": "NOT_AVAILABLE_HISTORICAL_REVIEWER_NOT_PROVEN",
        "report_denominator_status": "NOT_AVAILABLE_REPORT_COMPLETION_AND_ID_GRAIN_UNPROVEN;TRANSITION_40_ACTIONS_AVAILABLE_AS_PROXY",
        "status": "PARTIAL_SOURCE_WITH_EXPLICIT_NOT_AVAILABLE_METRICS",
    }
    return {
        "publication_id": publication_id,
        "fingerprint": fingerprint,
        "project_directory": directory_rows,
        "project_monthly_metrics": monthly_rows,
        "project_period_metrics": period_rows,
        "controller_monthly_metrics": controller_rows,
        "report_controller_claim_links": episodes,
        "manifest": manifest,
        "quality": {
            "completion_visit_month_keys_unique": len({(int(r["project_id"]), str(r["visit_id"]), str(r["month"])) for r in completion_source.get("completed_visits", [])}) == len(completion_source.get("completed_visits", [])),
            "claim_episode_keys_unique": len(seen_episode_ids) == len(episodes),
            "report_count_available": False,
            "controller_history_available": False,
            "project_names_available": names_available,
            "project_names_total": len(base_projects),
            "zero_claim_projects_included": zero_claim_projects,
            "claim_source_capture_anomaly": capture_anomaly,
            "claim_rows_after_declared_capture": int(claims_source.get("rows_after_declared_capture", 0)),
            "claim_episodes_missing_project_or_visit": sum(1 for e in episodes if not e["project_id"] or not e["visit_id"]),
        },
    }
