"""Tests for the historical claims mart (src/claims_mart.py).

Fixtures are synthetic event streams following the research contract in
docs/STATUS_39_HISTORY_DISCOVERY_2026-10-26.md.  Window: half-open
[2026-07-26 00:00 MSK, 2026-10-27 00:00 MSK).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.claims_mart import (  # noqa: E402
    ClaimsMartError,
    build_episodes,
    build_mart,
    cohort_claim_rate,
    dedupe_events,
    episode_rows,
    mart_fingerprint,
    mart_manifest,
    parse_transition_info,
    publication_id_for,
    utc_iso,
)

W0 = 1785013200  # 2026-07-26 00:00 MSK
W1 = 1793048400  # 2026-10-27 00:00 MSK
WINDOW = {"w0": W0, "w1": W1}
DAY = 86400


def _extract(entries, events, attribution=None, claims=None, canonical=None):
    return {
        "window": WINDOW,
        "captured_at_utc": "2026-10-26T16:39:04.687610+00:00",
        "canonical": canonical or {"total": len(entries), "typ_split": {}, "monthly": {}},
        "entries": entries,
        "events": events,
        "attribution": attribution or {},
        "claims": claims or {},
        "exits_explicit": [],
        "denominator": {},
        "sample_events": {},
    }


def _attr(visit=100, proj=7789, state=40):
    return {"visit": visit, "visit_proj": proj, "state": state}


# ---------------------------------------------------------------- parsing

def test_parse_transition_info():
    assert parse_transition_info("39 &rarr; 40") == (39, 40)
    assert parse_transition_info("30 &rarr; 35") == (30, 35)
    assert parse_transition_info("") is None
    assert parse_transition_info(None) is None
    assert parse_transition_info("garbage") is None


def test_utc_iso():
    assert utc_iso(0) == "1970-01-01T00:00:00Z"
    assert utc_iso(None) == ""
    assert utc_iso("") == ""


# ---------------------------------------------------------------- dedupe

def test_dedupe_merges_identical_events():
    raw = [(1, 142, 100, ""), (1, 142, 100, ""), (2, 139, 200, "")]
    events, dups = dedupe_events(raw)
    assert len(events) == 2
    assert dups == 1


def test_dedupe_keeps_richer_info_from_production_double_write():
    # production writes the same transition twice, one with payload one empty
    raw = [(5, 150, 300, "40 &rarr; 50"), (5, 150, 300, "")]
    events, dups = dedupe_events(raw)
    assert events == [("5", 150, 300, "40 &rarr; 50")]
    assert dups == 1


def test_dedupe_normalizes_none_info_and_string_codes():
    raw = [(7, 131, 10, None), ("7", 131, 10, "")]
    events, dups = dedupe_events(raw)
    assert events == [("7", 131, 10, "")]
    assert dups == 1


# ---------------------------------------------------------------- episodes

def test_explicit_closure_via_140():
    opened = W0 + DAY
    closed = opened + 2 * DAY
    events = [(142, opened, ""), (140, closed, "39 &rarr; 40")]
    eps = build_episodes("a1", events, 40, [], WINDOW)
    assert len(eps) == 1
    ep = eps[0]
    assert ep["opened_at"] == opened and ep["closed_at"] == closed
    assert ep["closure_confidence"] == "explicit_log"
    assert ep["entry_mechanism"] == "client_claim"
    assert ep["duration_h"] == 48.0
    assert ep["quality_flags"] == []


def test_repeated_entries_without_exit_are_one_episode():
    opened = W0 + DAY
    events = [(142, opened, ""), (142, opened + 3600, ""), (139, opened + 7200, "40 &rarr; 39")]
    eps = build_episodes("a1", events, 39, [], WINDOW)
    assert len(eps) == 1
    assert len(eps[0]["entry_events"]) == 3
    assert eps[0]["closure_confidence"] == "unresolved_open_at_capture"
    assert "open_at_capture" in eps[0]["quality_flags"]


def test_reopen_after_explicit_exit_starts_new_episode():
    e1_open, e1_close = W0 + DAY, W0 + 3 * DAY
    e2_open, e2_close = W0 + 4 * DAY, W0 + 5 * DAY
    events = [
        (142, e1_open, ""), (140, e1_close, "39 &rarr; 40"),
        (142, e2_open, ""), (140, e2_close, "39 &rarr; 40"),
    ]
    eps = build_episodes("a1", events, 40, [], WINDOW)
    assert [e["episode_no"] for e in eps] == [1, 2]
    assert all(e["closure_confidence"] == "explicit_log" for e in eps)


def test_auto_closure_uses_latest_evidence_and_labels_source():
    opened = W0 + DAY
    # repdt later than typ131 -> repdt is the closure estimate
    repdt = opened + 9 * DAY
    t131_early = opened + 5 * DAY
    events = [(142, opened, ""), (131, t131_early, None)]
    claims = [(7789, opened, repdt, 20)]
    eps = build_episodes("a1", events, 40, claims, WINDOW)
    assert eps[0]["closure_confidence"] == "auto_inferred"
    assert eps[0]["closed_at"] == repdt
    assert eps[0]["resolution_mechanism"] == "auto_inferred_repdt"
    assert "duration_approximate" in eps[0]["quality_flags"]
    # typ131 later than repdt -> typ131 is the closure estimate
    repdt_early = opened + 2 * DAY
    t131_late = opened + 6 * DAY
    events = [(142, opened, ""), (131, t131_late, None)]
    claims = [(7789, opened, repdt_early, 20)]
    eps = build_episodes("a1", events, 40, claims, WINDOW)
    assert eps[0]["closed_at"] == t131_late
    assert eps[0]["resolution_mechanism"] == "auto_inferred_typ131"


def test_auto_closure_falls_back_to_typ131():
    opened = W0 + DAY
    t131 = opened + 4 * DAY
    events = [(142, opened, ""), (131, t131, None)]
    eps = build_episodes("a1", events, 40, [], WINDOW)
    assert eps[0]["closure_confidence"] == "auto_inferred"
    assert eps[0]["closed_at"] == t131
    assert eps[0]["resolution_mechanism"] == "auto_inferred_typ131"


def test_closure_unknown_when_action_moved_on_without_evidence():
    opened = W0 + DAY
    events = [(142, opened, "")]
    eps = build_episodes("a1", events, 50, [], WINDOW)
    assert eps[0]["closure_confidence"] == "closure_unknown"
    assert eps[0]["closed_at"] is None
    assert "closure_time_unknown" in eps[0]["quality_flags"]


def test_unresolved_when_current_state_is_39():
    opened = W0 + DAY
    events = [(142, opened, "")]
    eps = build_episodes("a1", events, 39, [], WINDOW)
    assert eps[0]["closure_confidence"] == "unresolved_open_at_capture"


def test_orphan_exit_before_any_entry():
    events = [(140, W0 + DAY, "37 &rarr; 40")]
    eps = build_episodes("a1", events, 40, [], WINDOW)
    assert eps[0]["opened_at"] is None
    assert eps[0]["closure_confidence"] == "explicit_log_orphan_open"
    assert "opened_before_log_coverage" in eps[0]["quality_flags"]


def test_typ131_and_typ122_never_close_or_open_episodes():
    opened = W0 + DAY
    events = [(131, opened - DAY, None), (122, opened - 3600, None),
              (142, opened, ""), (131, opened + DAY, None), (122, opened + DAY + 1, None)]
    eps = build_episodes("a1", events, 39, [], WINDOW)
    assert len(eps) == 1
    assert eps[0]["opened_at"] == opened
    assert eps[0]["closure_confidence"] == "unresolved_open_at_capture"


def test_from_state_mismatch_flagged():
    opened = W0 + DAY
    events = [(142, opened, ""), (140, opened + DAY, "37 &rarr; 40")]
    eps = build_episodes("a1", events, 40, [], WINDOW)
    assert "from_state_mismatch" in eps[0]["quality_flags"]


# ---------------------------------------------------------------- mart

def _full_extract():
    a = W0 + 100
    b = W0 + 2 * DAY
    c = W0 + 3 * DAY
    entries = [(11, 142, a), (12, 142, b), (13, 139, c)]
    events = {
        "11": [(142, a, ""), (140, a + DAY, "39 &rarr; 40")],
        "12": [(142, b, ""), (131, b + DAY, None)],
        "13": [(139, c, "40 &rarr; 39")],
    }
    attribution = {
        "11": _attr(visit=100, proj=7789, state=40),
        "12": _attr(visit=101, proj=7789, state=40),
        "13": _attr(visit=102, proj=8001, state=39),
    }
    claims = {"12": [(7789, b, b + DAY, 20)]}
    return _extract(entries, events, attribution, claims)


def test_build_mart_totals_and_reconciliation():
    mart = build_mart(_full_extract())
    total = mart["total"]
    assert total["new_episodes"] == 3
    assert total["unique_actions"] == 3
    assert total["unique_visits"] == 3
    assert total["client_entry_events"] == 2
    assert total["staff_entry_events"] == 1
    assert total["explicit_resolutions"] == 1
    assert total["auto_resolutions_inferred"] == 1
    assert total["unresolved_open_at_capture"] == 1
    assert total["closure_unknown"] == 0


def test_build_mart_window_boundaries_are_half_open():
    extract = _full_extract()
    # episode opened exactly at w0 counts; at w1 does not
    extract["entries"] = [(21, 142, W0), (22, 142, W1 - 1), (23, 142, W1)]
    extract["events"] = {
        "21": [(142, W0, "")], "22": [(142, W1 - 1, "")], "23": [(142, W1, "")],
    }
    mart = build_mart(extract)
    assert mart["total"]["new_episodes"] == 2
    assert {e["action_id"] for e in mart["episodes"] if e["opened_at"] == W1} == {"23"}


def test_build_mart_monthly_split_msk():
    mart = build_mart(_full_extract())
    assert sum(row["new_episodes"] for row in mart["monthly"].values()) == 3
    assert all(m in ("2026-07", "2026-08", "2026-09", "2026-10") for m in mart["monthly"])


def test_build_mart_attribution_flags():
    extract = _full_extract()
    # conflicting claim.proj on action 11; action 12 loses attribution
    extract["claims"]["11"] = [(9999, W0 + 100, None, 0)]
    del extract["attribution"]["12"]
    mart = build_mart(extract)
    flags = {e["action_id"]: e["quality_flags"] for e in mart["episodes"]}
    assert "project_attribution_conflict" in flags["11"]
    assert "missing_visit_attribution" in flags["12"]
    assert mart["total"]["attribution_conflicts"] == 1
    assert mart["total"]["missing_attribution"] == 1


def test_build_mart_duplicate_entries_collapsed():
    extract = _full_extract()
    # same entry event arrives from both pulls
    extract["events"]["11"] = [(142, W0 + 100, ""), (140, W0 + 100 + DAY, "39 &rarr; 40")]
    mart = build_mart(extract)
    assert mart["total"]["new_episodes"] == 3
    assert mart["total"]["duplicate_source_events_dropped"] >= 1


# ---------------------------------------------------------------- cohort rate

def test_cohort_claim_rate_matches_visits():
    mart = build_mart(_full_extract())
    denominator = {"completed_visit_ids": [100, 101, 102, 103]}
    rate = cohort_claim_rate(mart, denominator)
    assert rate["claim_rate_available"] is True
    assert rate["completed_visits_in_window"] == 4
    assert rate["episode_visits_on_completed_cohort"] == 3
    assert rate["claim_rate_cohort_pct"] == 75.0


def test_cohort_claim_rate_blocked_without_denominator():
    mart = build_mart(_full_extract())
    rate = cohort_claim_rate(mart, {"completed_visit_ids": []})
    assert rate == {"claim_rate_available": False, "reason": "BLOCKED_DENOMINATOR_UNVERIFIED"}


# ---------------------------------------------------------------- publication

def test_episode_rows_only_opened_in_window():
    mart = build_mart(_full_extract())
    rows = episode_rows(mart, "pub1")
    assert len(rows) == 3
    assert all(r["publication_id"] == "pub1" for r in rows)
    assert rows[0]["opened_at_utc"] <= rows[-1]["opened_at_utc"]


def test_fingerprint_stable_and_sensitive():
    mart = build_mart(_full_extract())
    rows1 = episode_rows(mart, "pub1")
    rows2 = episode_rows(mart, "pub1")
    assert mart_fingerprint(rows1) == mart_fingerprint(rows2)
    changed = [dict(rows1[0], closure_confidence="x")] + rows1[1:]
    assert mart_fingerprint(changed) != mart_fingerprint(rows1)


def test_publication_id_deterministic():
    mart = build_mart(_full_extract())
    rows = episode_rows(mart, "")
    fp = mart_fingerprint(rows)
    assert publication_id_for(mart, fp) == publication_id_for(mart, fp)
    assert publication_id_for(mart, fp).startswith("cm90d-1785013200-1793048400-")


def test_manifest_record_fields():
    mart = build_mart(_full_extract())
    rate = cohort_claim_rate(mart, {"completed_visit_ids": [100, 101, 102]})
    rows = episode_rows(mart, "")
    fp = mart_fingerprint(rows)
    manifest = mart_manifest(mart, rate, "pub1", "2026-10-26T20:00:00Z", fp)
    assert manifest["publication_id"] == "pub1"
    assert manifest["fingerprint"] == fp
    assert manifest["new_episodes"] == 3
    assert manifest["claim_rate_cohort_pct"] == 100.0
    assert manifest["window_start_utc"] == utc_iso(W0)


def test_manifest_blocked_rate_is_explicit():
    mart = build_mart(_full_extract())
    rate = cohort_claim_rate(mart, {"completed_visit_ids": []})
    rows = episode_rows(mart, "")
    manifest = mart_manifest(mart, rate, "pub1", "2026-10-26T20:00:00Z", mart_fingerprint(rows))
    assert manifest["claim_rate_cohort_pct"] == "BLOCKED_DENOMINATOR_UNVERIFIED"
