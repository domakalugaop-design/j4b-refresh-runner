"""Grain, denominator, quality and limitation tests for the project mart."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.project_controller_claims_mart import (  # noqa: E402
    ProjectMartError,
    build_project_mart,
)


@pytest.fixture
def source_pair():
    completion = {
        "window": {"w0": 1, "w1": 2},
        "captured_at_utc": "2026-10-27T06:00:00+00:00",
        "totals": {
            "distinct_actions": 3,
            "distinct_project_visit_pairs": 3,
            "project_count": 2,
            "missing_action_map": 1,
        },
        "project_directory": [
            {"project_id": 10, "project_name": "Alpha", "project_type_code": 1},
            {"project_id": 20, "project_name": "Zero claims", "project_type_code": 2},
        ],
        "project_monthly": [
            {"project_id": 10, "month": "2026-08", "completed_action_count": 2, "completed_visit_count": 2},
            {"project_id": 20, "month": "2026-08", "completed_action_count": 1, "completed_visit_count": 1},
        ],
        "project_period": [
            {"project_id": 10, "completed_action_count": 2, "completed_visit_count": 2},
            {"project_id": 20, "completed_action_count": 1, "completed_visit_count": 1},
        ],
        "completed_visits": [
            {"project_id": 10, "visit_id": "101", "month": "2026-08"},
            {"project_id": 10, "visit_id": "102", "month": "2026-08"},
            {"project_id": 20, "visit_id": "201", "month": "2026-08"},
        ],
    }
    claims = {
        "publication_id": "claims-pub-1",
        "captured_at_utc": "2026-10-26T16:39:04Z",
        "capture_anomaly": True,
        "rows_after_declared_capture": 1,
        "project_directory": [
            {"project_id": 10, "project_name": "Alpha", "project_type_code": 1},
            {"project_id": 30, "project_name": "Claims-only project", "project_type_code": 3},
        ],
        "episodes": [
            {"action_id": "a1", "visit_id": "101", "project_id": 10,
             "opened_at_utc": "2026-08-03T10:00:00Z", "entry_mechanism": "client_claim",
             "closure_confidence": "explicit_log"},
            {"action_id": "a2", "visit_id": "101", "project_id": 10,
             "opened_at_utc": "2026-08-03T11:00:00Z", "entry_mechanism": "client_claim",
             "closure_confidence": "closure_unknown"},
            {"action_id": "a3", "visit_id": "301", "project_id": 30,
             "opened_at_utc": "2026-09-03T11:00:00Z", "entry_mechanism": "staff_status39",
             "closure_confidence": "auto_inferred"},
        ],
    }
    return completion, claims


def test_project_grains_do_not_multiply_episodes(source_pair):
    completion, claims = source_pair
    mart = build_project_mart(completion, claims, "candidate")
    assert len(mart["report_controller_claim_links"]) == 3
    assert len({r["episode_id"] for r in mart["report_controller_claim_links"]}) == 3
    p10 = next(r for r in mart["project_period_metrics"] if r["project_id"] == 10)
    assert p10["completed_visit_count"] == 2
    assert p10["claim_episode_count"] == 2
    assert p10["claim_visit_count"] == 1
    assert p10["claim_visits_in_completed_cohort"] == 1


def test_zero_claim_project_included(source_pair):
    completion, claims = source_pair
    mart = build_project_mart(completion, claims, "candidate")
    p20 = next(r for r in mart["project_period_metrics"] if r["project_id"] == 20)
    assert p20["completed_visit_count"] == 1
    assert p20["claim_episode_count"] == 0
    assert p20["claim_rate_pct"] == ""  # source capture anomaly makes rates unavailable
    directory = next(r for r in mart["project_directory"] if r["project_id"] == 20)
    assert directory["activity_status"] == "COMPLETION_COHORT"
    assert mart["quality"]["zero_claim_projects_included"] == 1


def test_report_and_controller_metrics_explicitly_unavailable(source_pair):
    completion, claims = source_pair
    mart = build_project_mart(completion, claims, "candidate")
    assert all(r["report_count"] == "" for r in mart["project_period_metrics"])
    assert all("NOT_AVAILABLE" in r["report_count_status"] for r in mart["project_period_metrics"])
    assert all(r["controller_count"] == "" for r in mart["project_period_metrics"])
    assert all(r["controller_id"] == "" for r in mart["report_controller_claim_links"])
    assert all(r["controller_link_status"].startswith("NOT_AVAILABLE") for r in mart["report_controller_claim_links"])


def test_monthly_metrics_do_not_claim_same_month_rate(source_pair):
    completion, claims = source_pair
    mart = build_project_mart(completion, claims, "candidate")
    assert len(mart["project_monthly_metrics"]) == 3 * 4  # all projects × all four months
    assert all(r["monthly_claim_rate_pct"] == "" for r in mart["project_monthly_metrics"])
    assert all("NOT_AVAILABLE" in r["monthly_claim_rate_status"] for r in mart["project_monthly_metrics"])
    zero_aug = next(r for r in mart["project_monthly_metrics"] if r["project_id"] == 20 and r["month"] == "2026-09")
    assert zero_aug["completed_visit_count"] == 0
    assert zero_aug["claim_episode_count_opened_in_month"] == 0


def test_claim_source_capture_anomaly_blocks_rate(source_pair):
    completion, claims = source_pair
    mart = build_project_mart(completion, claims, "candidate")
    assert mart["manifest"]["claim_rate_pct"] == ""
    assert mart["manifest"]["claim_rate_status"] == "NOT_AVAILABLE_CLAIM_SOURCE_CAPTURE_TIME_PRECEDES_EVENT"
    assert mart["manifest"]["claim_rows_after_declared_capture"] == 1
    assert mart["quality"]["claim_source_capture_anomaly"] is True


def test_duplicate_episode_key_refused(source_pair):
    completion, claims = source_pair
    claims["episodes"].append(dict(claims["episodes"][0]))
    with pytest.raises(ProjectMartError, match="duplicate claim episode key"):
        build_project_mart(completion, claims, "candidate")


def test_missing_project_visit_claim_row_is_retained_and_counted(source_pair):
    completion, claims = source_pair
    claims["episodes"].append({
        "action_id": "unlinked", "visit_id": None, "project_id": None,
        "opened_at_utc": "2026-09-04T12:00:00Z", "entry_mechanism": "client_claim",
        "closure_confidence": "closure_unknown",
    })
    mart = build_project_mart(completion, claims, "candidate")
    assert len(mart["report_controller_claim_links"]) == 4
    unlinked = next(r for r in mart["report_controller_claim_links"] if r["action_id"] == "unlinked")
    assert unlinked["project_id"] == ""
    assert unlinked["visit_id"] == ""
    assert mart["manifest"]["claim_episodes"] == 4
    assert mart["manifest"]["claim_episodes_missing_project_or_visit"] == 1


def test_project_period_action_count_comes_from_distinct_period_total(source_pair):
    completion, claims = source_pair
    completion["project_monthly"].append({
        "project_id": 10, "month": "2026-09", "completed_action_count": 2, "completed_visit_count": 1,
    })
    completion["project_period"][0]["completed_action_count"] = 3
    completion["project_period"][0]["completed_visit_count"] = 3
    completion["completed_visits"].append({"project_id": 10, "visit_id": "103", "month": "2026-09"})
    mart = build_project_mart(completion, claims, "candidate")
    p10 = next(r for r in mart["project_period_metrics"] if r["project_id"] == 10)
    assert p10["completed_action_count"] == 3  # not 2 + 2, which could double-count an action
