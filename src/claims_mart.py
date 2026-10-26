"""Historical claims mart for Portal status-39 (client claims).

Builds three analytical grains from the MariaDB ``log`` audit trail
(research contract: docs/STATUS_39_HISTORY_DISCOVERY_2026-10-26.md):

A. CLAIM EVENT  - one uniquely identified entry/exit log event.
B. CLAIM EPISODE - one continuous period during which an action sits in
   status 39.  Repeated entries without an intervening exit are additional
   entry events on the same episode; an exit (any logged transition to a
   non-39 state) closes the episode and a later entry starts a new one.
C. CLAIM AGGREGATE - calendar-period metrics (Europe/Moscow business days).

Canonical time interval is half-open ``[start, end)`` in business-local
(Europe/Moscow) time.  All counts are computed from one consistent
database snapshot; the capture timestamp travels with the data.

Closure semantics:
- explicit: a logged transition event whose parsed target state is not 39
  (typ 140 '39->40', 145 '39->45', 135 '39->35', 137 '39->37', ...).
  Duration is PRECISE only for these.
- auto-inferred: episode still open in the event stream but ``my.state``
  has moved on (40/45/50/...).  Closure time is approximated by the last
  claim reply (``claim.repdt``) or the last typ-131 submit after the
  opening entry; such durations are APPROXIMATE and are excluded from
  precise SLA metrics.
- unresolved: ``my.state`` is still 39 at capture time.
- orphan closure: an exit event with no entry event in the log coverage
  (episode opened before the log starts); opened_at is UNKNOWN.

Project attribution: ``visit.proj`` is canonical (the business project of
the visit being evaluated).  Actions whose ``claim.proj`` disagrees are
flagged and excluded from project-level KPIs while remaining in totals.
Actions with no visit attribution are flagged as well.
"""
from __future__ import annotations

import hashlib
import re
import statistics
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")

ENTRY_TYPES = {142: "client_claim", 139: "staff_status39"}
TRANSITION_TYPES = {135, 137, 139, 140, 142, 145, 150}
STATE_39 = 39

_ARROW = re.compile(r"^\s*(\d+)\s*&rarr;\s*(\d+)")


class ClaimsMartError(ValueError):
    """Raised when the event stream violates the research contract."""


def parse_transition_info(info):
    """Return (from_state, to_state) for 'X &rarr; Y' info, else None."""
    if not info:
        return None
    match = _ARROW.match(str(info))
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def msk_month(dt_unix: int) -> str:
    return datetime.fromtimestamp(dt_unix, MSK).strftime("%Y-%m")


def in_window(dt_unix: int, window: dict) -> bool:
    return window["w0"] <= dt_unix < window["w1"]


def dedupe_events(raw_events):
    """Collapse duplicate source events keyed by (code, typ, dt).

    The production log occasionally writes the same transition twice with
    different info payloads (one descriptive, one empty).  Same-key events
    are merged into one, keeping the most informative payload; the merge
    count is returned so aggregates can report duplicate volume.
    """
    seen = {}
    duplicates = 0
    for code, typ, dt, info in raw_events:
        code = str(code)
        info = "" if info is None else str(info)
        key = (code, typ, dt)
        if key in seen:
            duplicates += 1
            if len(info) > len(seen[key][2]):
                seen[key] = (typ, dt, info)
            continue
        seen[key] = (typ, dt, info)
    events = [
        (code, typ, dt, info_val)
        for (code, typ, dt), (_t, _d, info_val) in seen.items()
    ]
    return sorted(events, key=lambda e: (e[0], e[2], e[1])), duplicates


def build_episodes(action_id, events, current_state, claims, window):
    """Build claim episodes for one action.

    ``events`` is the action's sorted [(typ, dt, info)] transition/submit
    stream (typ in TRANSITION_TYPES plus 131).  ``claims`` is
    [(proj, dt, repdt, state)] claim items for the action.
    """
    episodes = []
    open_ep = None
    for typ, dt, info in events:
        parsed = parse_transition_info(info)
        if typ in ENTRY_TYPES:
            if open_ep is None:
                open_ep = {
                    "action_id": action_id,
                    "episode_no": len(episodes) + 1,
                    "opened_at": dt,
                    "entry_mechanism": ENTRY_TYPES[typ],
                    "entry_events": [(typ, dt)],
                    "closed_at": None,
                    "resolution_mechanism": None,
                    "closure_confidence": None,
                    "duration_h": None,
                    "quality_flags": [],
                }
            else:
                open_ep["entry_events"].append((typ, dt))
            continue
        if typ == 131 or parsed is None:
            continue  # submit/autosave evidence, not a state transition
        from_state, to_state = parsed
        if to_state == STATE_39:
            # a logged transition back INTO 39 (typ 139) while open is an
            # additional entry event on the open episode
            if open_ep is not None:
                open_ep["entry_events"].append((typ, dt))
            continue
        if open_ep is not None:
            open_ep["closed_at"] = dt
            open_ep["resolution_mechanism"] = f"typ{typ}->state{to_state}"
            open_ep["closure_confidence"] = "explicit_log"
            open_ep["duration_h"] = round((dt - open_ep["opened_at"]) / 3600.0, 2)
            if from_state != STATE_39:
                open_ep["quality_flags"].append("from_state_mismatch")
            episodes.append(open_ep)
            open_ep = None
        else:
            episodes.append({
                "action_id": action_id,
                "episode_no": len(episodes) + 1,
                "opened_at": None,
                "entry_mechanism": "unknown_pre_log",
                "entry_events": [],
                "closed_at": dt,
                "resolution_mechanism": f"typ{typ}->state{to_state}",
                "closure_confidence": "explicit_log_orphan_open",
                "duration_h": None,
                "quality_flags": ["opened_before_log_coverage"],
            })
    if open_ep is not None:
        if current_state == STATE_39:
            open_ep["closure_confidence"] = "unresolved_open_at_capture"
            open_ep["quality_flags"].append("open_at_capture")
        else:
            evidence = [
                repdt for _p, _dt, repdt, _s in claims
                if repdt and open_ep["opened_at"] is not None and repdt > open_ep["opened_at"]
            ]
            typ131 = [dt for t, dt, _i in events if t == 131 and dt > open_ep["opened_at"]]
            closure = max(evidence + typ131) if (evidence or typ131) else None
            if closure is not None:
                source = "auto_inferred_repdt" if evidence and max(evidence) >= (max(typ131) if typ131 else 0) else "auto_inferred_typ131"
                open_ep["closed_at"] = closure
                open_ep["resolution_mechanism"] = source
                open_ep["closure_confidence"] = "auto_inferred"
                open_ep["duration_h"] = round((closure - open_ep["opened_at"]) / 3600.0, 2)
                open_ep["quality_flags"].append("duration_approximate")
            else:
                open_ep["closed_at"] = None
                open_ep["resolution_mechanism"] = "state_moved_no_closure_evidence"
                open_ep["closure_confidence"] = "closure_unknown"
                open_ep["quality_flags"].append("closure_time_unknown")
        episodes.append(open_ep)
    return episodes


def build_mart(extract: dict) -> dict:
    """Build the full mart (events, episodes, aggregates) from an extract."""
    window = extract["window"]
    attribution = extract["attribution"]
    claims = extract["claims"]
    per_action_events = extract["events"]

    raw = []
    for code, typ, dt in extract["entries"]:
        raw.append((code, typ, dt, ""))
    for code, typ, dt, info in [
        (c, t, d, i) for c, evs in per_action_events.items() for t, d, i in evs
    ]:
        raw.append((code, typ, dt, info))
    events, duplicates = dedupe_events(raw)

    by_action = {}
    for code, typ, dt, info in events:
        by_action.setdefault(code, []).append((typ, dt, info))
    for action in by_action:
        by_action[action].sort(key=lambda e: (e[1], e[0]))

    episodes = []
    for action, evs in by_action.items():
        info = attribution.get(action)
        state = info["state"] if info else None
        action_episodes = build_episodes(action, evs, state, claims.get(action, []), window)
        for ep in action_episodes:
            if info is None:
                ep["quality_flags"].append("missing_visit_attribution")
                ep["project_id"] = None
                ep["visit_id"] = None
            else:
                ep["project_id"] = info["visit_proj"]
                ep["visit_id"] = info["visit"]
            ep["current_state"] = info["state"] if info else None
            conflict = any(
                p and info and info["visit_proj"] and p != info["visit_proj"]
                for p, _d, _r, _s in claims.get(action, [])
            )
            if conflict:
                ep["quality_flags"].append("project_attribution_conflict")
            episodes.append(ep)

    opened_in_window = [
        e for e in episodes
        if e["opened_at"] is not None and in_window(e["opened_at"], window)
    ]
    resolved_in_window = [
        e for e in episodes
        if e["closed_at"] is not None and in_window(e["closed_at"], window)
    ]
    unresolved = [e for e in episodes if e["closure_confidence"] == "unresolved_open_at_capture"]

    explicit_durations = [
        e["duration_h"] for e in opened_in_window
        if e["closure_confidence"] == "explicit_log" and e["duration_h"] is not None
    ]
    auto_durations = [
        e["duration_h"] for e in opened_in_window
        if e["closure_confidence"] == "auto_inferred" and e["duration_h"] is not None
    ]

    def period_metrics(eps):
        actions = {e["action_id"] for e in eps}
        visits = {e["visit_id"] for e in eps if e["visit_id"] is not None}
        client = sum(1 for e in eps for t, _d in e["entry_events"] if t == 142)
        staff = sum(1 for e in eps for t, _d in e["entry_events"] if t == 139)
        per_action_counts = {}
        for e in eps:
            per_action_counts[e["action_id"]] = per_action_counts.get(e["action_id"], 0) + 1
        reopened = sum(1 for n in per_action_counts.values() if n > 1)
        conflicts = sum(1 for e in eps if "project_attribution_conflict" in e["quality_flags"])
        missing_attr = sum(1 for e in eps if "missing_visit_attribution" in e["quality_flags"])
        return {
            "new_episodes": len(eps),
            "unique_actions": len(actions),
            "unique_visits": len(visits),
            "client_entry_events": client,
            "staff_entry_events": staff,
            "reopened_episodes": reopened,
            "attribution_conflicts": conflicts,
            "missing_attribution": missing_attr,
        }

    months = sorted({msk_month(e["opened_at"]) for e in opened_in_window})
    monthly = {}
    for month in months:
        eps = [e for e in opened_in_window if msk_month(e["opened_at"]) == month]
        row = period_metrics(eps)
        row["resolved_in_window"] = sum(
            1 for e in resolved_in_window if msk_month(e["closed_at"]) == month
        )
        monthly[month] = row

    total = period_metrics(opened_in_window)
    total["resolved_in_window"] = len(resolved_in_window)
    total["unresolved_open_at_capture"] = len(unresolved)
    total["explicit_resolutions"] = sum(
        1 for e in opened_in_window if e["closure_confidence"] == "explicit_log"
    )
    total["auto_resolutions_inferred"] = sum(
        1 for e in opened_in_window if e["closure_confidence"] == "auto_inferred"
    )
    total["closure_unknown"] = sum(
        1 for e in opened_in_window if e["closure_confidence"] == "closure_unknown"
    )
    total["median_resolution_h_explicit"] = (
        round(statistics.median(explicit_durations), 2) if explicit_durations else None
    )
    total["median_resolution_h_auto_approx"] = (
        round(statistics.median(auto_durations), 2) if auto_durations else None
    )
    total["entry_events_total"] = sum(
        len(e["entry_events"]) for e in opened_in_window
    )
    total["duplicate_source_events_dropped"] = duplicates

    return {
        "window": window,
        "captured_at_utc": extract["captured_at_utc"],
        "canonical_counts": extract["canonical"],
        "episodes": episodes,
        "monthly": monthly,
        "total": total,
    }


def cohort_claim_rate(mart: dict, denominator: dict) -> dict:
    """Cohort-matched claim rate.

    Denominator: distinct visits with any logged ->40 completion event in
    the window (same snapshot).  Numerator: episodes opened in the window
    on those same visits, so both grains use the identical visit cohort.
    """
    completed_visits = set(denominator["completed_visit_ids"])
    episode_visits = {
        e["visit_id"] for e in mart["episodes"]
        if e["opened_at"] is not None and in_window(e["opened_at"], mart["window"])
        and e["visit_id"] is not None
    }
    matched = episode_visits & completed_visits
    if not completed_visits:
        return {"claim_rate_available": False, "reason": "BLOCKED_DENOMINATOR_UNVERIFIED"}
    return {
        "claim_rate_available": True,
        "completed_visits_in_window": len(completed_visits),
        "episode_visits_in_window": len(episode_visits),
        "episode_visits_on_completed_cohort": len(matched),
        "claim_rate_cohort_pct": round(100.0 * len(matched) / len(completed_visits), 2),
    }


MART_ROW_FIELDS = [
    "publication_id", "action_id", "visit_id", "project_id",
    "opened_at_utc", "entry_mechanism", "closed_at_utc",
    "resolution_mechanism", "closure_confidence", "duration_h",
    "quality_flags", "current_state",
]

MANIFEST_FIELDS = [
    "publication_id", "published_at_utc", "fingerprint",
    "window_start_utc", "window_end_utc", "source_captured_at_utc",
    "new_episodes", "unique_actions", "unique_visits",
    "client_entry_events", "staff_entry_events", "reopened_episodes",
    "explicit_resolutions", "auto_resolutions_inferred", "closure_unknown",
    "unresolved_open_at_capture", "median_resolution_h_explicit",
    "median_resolution_h_auto_approx", "completed_visits_in_window",
    "claim_rate_cohort_pct", "duplicate_source_events_dropped",
    "attribution_conflicts", "missing_attribution", "status",
]


def utc_iso(ts) -> str:
    """Unix seconds -> ISO-8601 UTC string; None/'' -> ''."""
    if ts in (None, ""):
        return ""
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def episode_rows(mart: dict, publication_id: str) -> list[dict]:
    """Publication rows for episodes OPENED in the mart window.

    Orphan closures (episodes opened before log coverage) are excluded
    from the data tab; their volume is reported in the manifest.
    """
    window = mart["window"]
    rows = []
    for ep in mart["episodes"]:
        if ep["opened_at"] is None or not in_window(ep["opened_at"], window):
            continue
        rows.append({
            "publication_id": publication_id,
            "action_id": ep["action_id"],
            "visit_id": ep["visit_id"] if ep["visit_id"] is not None else "",
            "project_id": ep["project_id"] if ep["project_id"] is not None else "",
            "opened_at_utc": utc_iso(ep["opened_at"]),
            "entry_mechanism": ep["entry_mechanism"],
            "closed_at_utc": utc_iso(ep["closed_at"]),
            "resolution_mechanism": ep["resolution_mechanism"] or "",
            "closure_confidence": ep["closure_confidence"] or "",
            "duration_h": ep["duration_h"] if ep["duration_h"] is not None else "",
            "quality_flags": ";".join(ep["quality_flags"]),
            "current_state": ep["current_state"] if ep["current_state"] is not None else "",
        })
    rows.sort(key=lambda r: (r["opened_at_utc"], r["action_id"], r["closed_at_utc"]))
    return rows


def mart_fingerprint(rows: list[dict]) -> str:
    """Stable sha256 over the published row payloads (order-sensitive)."""
    payload = "\n".join(
        "\x1f".join(str(row.get(f, "")) for f in MART_ROW_FIELDS) for row in rows
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def mart_manifest(
    mart: dict, rate: dict, publication_id: str, published_at_utc: str,
    fingerprint: str, status: str = "PUBLISHED",
) -> dict:
    """One manifest record describing a claims-mart publication."""
    total = mart["total"]
    window = mart["window"]
    return {
        "publication_id": publication_id,
        "published_at_utc": published_at_utc,
        "fingerprint": fingerprint,
        "window_start_utc": utc_iso(window["w0"]),
        "window_end_utc": utc_iso(window["w1"]),
        "source_captured_at_utc": mart["captured_at_utc"],
        "new_episodes": total["new_episodes"],
        "unique_actions": total["unique_actions"],
        "unique_visits": total["unique_visits"],
        "client_entry_events": total["client_entry_events"],
        "staff_entry_events": total["staff_entry_events"],
        "reopened_episodes": total["reopened_episodes"],
        "explicit_resolutions": total["explicit_resolutions"],
        "auto_resolutions_inferred": total["auto_resolutions_inferred"],
        "closure_unknown": total["closure_unknown"],
        "unresolved_open_at_capture": total["unresolved_open_at_capture"],
        "median_resolution_h_explicit": total["median_resolution_h_explicit"]
            if total["median_resolution_h_explicit"] is not None else "",
        "median_resolution_h_auto_approx": total["median_resolution_h_auto_approx"]
            if total["median_resolution_h_auto_approx"] is not None else "",
        "completed_visits_in_window": rate.get("completed_visits_in_window", ""),
        "claim_rate_cohort_pct": rate.get("claim_rate_cohort_pct", "")
            if rate.get("claim_rate_available") else "BLOCKED_DENOMINATOR_UNVERIFIED",
        "duplicate_source_events_dropped": total["duplicate_source_events_dropped"],
        "attribution_conflicts": total["attribution_conflicts"],
        "missing_attribution": total["missing_attribution"],
        "status": status,
    }


def publication_id_for(mart: dict, fingerprint: str) -> str:
    """Deterministic id: window + source capture + fingerprint prefix."""
    window = mart["window"]
    captured = mart["captured_at_utc"].replace("-", "").replace(":", "")
    return f"cm90d-{window['w0']}-{window['w1']}-{captured}-{fingerprint[7:15]}"
