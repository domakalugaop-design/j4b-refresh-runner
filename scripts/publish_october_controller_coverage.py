"""Publish October 2026 controller coverage tabs to TEST Google Sheets.

Four new tabs:
- oct2026_controller_visits
- oct2026_controller_workload
- oct2026_claims_association
- oct2026_coverage_manifest

All append-only, versioned by publication_id, with explicit approval gate,
idempotency, and read-back verification.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.controller_quality_sheets import GoogleSheetsGateway, MartError  # noqa: E402
from src.google_service_account import google_token  # noqa: E402
from src.visit_registry import (  # noqa: E402
    PARSE_FIELDS,
    VISIT_GRAIN_FIELDS,
    build_registry_snapshot,
    parse_action_fact_rows,
    visit_grain,
)

TEST_SPREADSHEET_ID = "1G_pXXNCLTwABSnSPtXxRCxF19VOCOvXi-DK21ZMzytM"
PROD_SPREADSHEET_ID = "14PRG3HXsZaN4pVwV390PpMMqHupCW5j6RyI6HBzPQR8"
PORTAL_BASE_URL = "https://lk.j4b.ru"

OCTOBER_MARKERS = ("_1026", "_10.26", "_10_26", "1026", "oct", "окт", "10-26", "10_26", "q4_1026", "q4_10.26")

OUTPUT_TABS = {
    "oct2026_controller_visits": "oct2026_controller_visits",
    "oct2026_controller_workload": "oct2026_controller_workload",
    "oct2026_claims_association": "oct2026_claims_association",
    "oct2026_coverage_manifest": "oct2026_coverage_manifest",
}

VISITS_HEADER = [
    "publication_id", "captured_at_utc", "project_id", "project_name",
    "visit_id", "action_id", "workflow_status_code", "workflow_status_label",
    "visit_datetime", "report_datetime", "controller_id", "controller_fio",
    "coverage_status",
]

WORKLOAD_HEADER = [
    "publication_id", "captured_at_utc", "project_id", "project_name",
    "controller_id", "controller_fio", "unique_visits_observed",
    "action_rows_observed", "coverage_status",
]

CLAIMS_HEADER = [
    "publication_id", "claim_publication_id", "episode_id", "action_id", "visit_id",
    "project_id", "project_name", "opened_at_utc",
    "observed_controller_id", "observed_controller_fio",
    "controller_observed_at_utc", "association_method",
    "association_status", "quality_flags",
]

COVERAGE_HEADER = [
    "publication_id", "captured_at_utc", "population_definition",
    "projects_targeted", "projects_collected", "visits_observed",
    "visits_without_controller", "claim_episodes_considered",
    "claims_matched", "claims_unmatched", "claims_ambiguous",
    "source_timestamp", "completeness_status",
]

KEY_FIELDS = {
    "oct2026_controller_visits": ("publication_id", "project_id", "visit_id", "action_id"),
    "oct2026_controller_workload": ("publication_id", "project_id", "controller_id"),
    "oct2026_claims_association": ("publication_id", "episode_id"),
    "oct2026_coverage_manifest": ("publication_id",),
}

PUB_ID_FIELD = {
    "oct2026_controller_visits": "publication_id",
    "oct2026_controller_workload": "publication_id",
    "oct2026_claims_association": "publication_id",
    "oct2026_coverage_manifest": "publication_id",
}


def _as_str(v):
    if v is None:
        return ""
    return str(v)


def _stringify(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _sheet_row(record: dict, fields: list[str]) -> list:
    return [_stringify(record.get(f, "")) for f in fields]


def _key_from_record(record: dict, fields: tuple[str, ...]) -> tuple:
    return tuple(_stringify(record.get(f, "")) for f in fields)


def _key_from_sheet(row: list, header: list[str], fields: tuple[str, ...]) -> tuple:
    indexes = [header.index(f) for f in fields]
    return tuple(_stringify(row[i] if i < len(row) else "") for i in indexes)


def _rows_match(title: str, existing_row: list, expected_row: list, header: list[str]) -> bool:
    if title == "oct2026_coverage_manifest":
        ignored = {header.index("source_timestamp"), header.index("captured_at_utc")}
        return all(
            _stringify(existing_row[i] if i < len(existing_row) else "")
            == _stringify(expected_row[i] if i < len(expected_row) else "")
            for i in range(len(header)) if i not in ignored
        )
    if title == "oct2026_controller_workload":
        ignored = {header.index("captured_at_utc")}
        return all(
            _stringify(existing_row[i] if i < len(existing_row) else "")
            == _stringify(expected_row[i] if i < len(expected_row) else "")
            for i in range(len(header)) if i not in ignored
        )
    return existing_row == expected_row


def _ensure_tab(gateway: GoogleSheetsGateway, title: str, header: list[str], titles: set[str]):
    if title not in titles:
        gateway.add_sheet(title)
        titles.add(title)
    current = gateway.read_range(title, "A1:Z1")
    if current and current[0]:
        if current[0] != header:
            gateway.update(title, "A1", [header])
    else:
        gateway.ensure_headers(title, header)


def _validate_record_keys(records: list[dict], key_fields: tuple[str, ...], title: str) -> None:
    """Pre-write validation: ensure no record has empty key fields."""
    for i, record in enumerate(records):
        key = _key_from_record(record, key_fields)
        if any(not k for k in key):
            raise MartError(
                f"PRE_WRITE_VALIDATION_FAILED:{title}:record_index={i}:empty_key_field:key={key}:record={json.dumps(record, ensure_ascii=False)[:200]}"
            )
        # Check for duplicate keys within the batch
        # (handled later in _append_versioned but good to catch early)
    # Check for duplicates within batch
    seen = set()
    for i, record in enumerate(records):
        key = _key_from_record(record, key_fields)
        if key in seen:
            raise MartError(
                f"PRE_WRITE_VALIDATION_FAILED:{title}:record_index={i}:duplicate_key_in_batch:key={key}"
            )
        seen.add(key)


def _append_versioned(
    gateway: GoogleSheetsGateway,
    title: str,
    header: list[str],
    records: list[dict],
    pub_id: str,
    titles: set[str],
) -> dict:
    _ensure_tab(gateway, title, header, titles)
    current = gateway.read(title)
    if not current:
        raise MartError(f"REFUSED_HEADER_MISMATCH:{title}")

    fields = KEY_FIELDS[title]
    current_pub_idx = header.index(PUB_ID_FIELD[title])
    existing = {}
    for row in current[1:]:
        if current_pub_idx >= len(row) or str(row[current_pub_idx]) != pub_id:
            continue
        key = _key_from_sheet(row, header, KEY_FIELDS[title])
        if key in existing:
            raise MartError(f"REFUSED_DUPLICATE_GRAIN:{title}:{key}")
        existing[key] = [_stringify(row[i] if i < len(row) else "") for i in range(len(header))]

    expected = {}
    expected_raw = {}
    for record in records:
        key = _key_from_record(record, KEY_FIELDS[title])
        if key in expected:
            raise MartError(f"REFUSED_DUPLICATE_EXPECTED_GRAIN:{title}:{key}")
        raw_row = [_stringify(record.get(f, "")) for f in header]
        expected_raw[key] = raw_row
        expected[key] = [_stringify(v) for v in raw_row]

    for key, prior in existing.items():
        if key not in expected or not _rows_match(title, prior, expected[key], header):
            raise MartError(f"REFUSED_PARTIAL_PUBLICATION_CONFLICT:{title}:{key}")

    missing = [row for key, row in expected_raw.items() if key not in existing]
    if missing:
        next_row = len(current) + 1
        gateway.update(title, f"A{next_row}", missing)

    # Read-back verification
    after = gateway.read(title)
    found = {}
    for row in after[1:]:
        if current_pub_idx < len(row) and str(row[current_pub_idx]) == pub_id:
            found[_key_from_sheet(row, header, KEY_FIELDS[title])] = [
                _stringify(row[i] if i < len(row) else "") for i in range(len(header))
            ]
    if set(found) != set(expected) or any(
        not _rows_match(title, found[key], expected[key], header) for key in expected
    ):
        raise MartError(f"READBACK_MISMATCH:{title}")

    return {"expected": len(expected), "prior": len(existing), "added": len(missing), "readback_ok": True}


def _read_source(gateway: GoogleSheetsGateway, tab: str, required: list[str]) -> list[dict]:
    rows = gateway.read(tab)
    if not rows:
        raise MartError(f"source tab {tab} is empty")
    header = rows[0]
    ix = {name: header.index(name) for name in required if name in header}
    missing = [name for name in required if name not in header]
    if missing:
        raise MartError(f"source tab {tab} missing required columns: {missing}")
    out = []
    for row in rows[1:]:
        rec = {}
        for name, idx in ix.items():
            rec[name] = row[idx] if idx < len(row) else ""
        out.append(rec)
    return out


def _is_october_project(name: str) -> bool:
    name_lower = name.lower()
    return any(marker.lower() in name_lower for marker in OCTOBER_MARKERS)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approve-test-write", action="store_true", dest="approve")
    parser.add_argument("--dry-run", action="store_true", dest="dry_run")
    parser.add_argument("--max-projects", type=int, default=0, help="Limit projects for testing (0 = all)")
    args = parser.parse_args()

    if not (args.approve or args.dry_run):
        print(json.dumps({"status": "REFUSED_WRITE_NOT_APPROVED"}))
        return 2

    token = google_token()
    prod_gateway = GoogleSheetsGateway(PROD_SPREADSHEET_ID, token)
    test_gateway = GoogleSheetsGateway(TEST_SPREADSHEET_ID, token)
    titles = set(test_gateway.titles())

    # Step 1: Identify October projects from production projects_current
    prod_projects = _read_source(prod_gateway, "projects_current", [
        "project_id", "project_name",
    ])
    october_projects = []
    for row in prod_projects:
        pid = _as_str(row.get("project_id"))
        name = _as_str(row.get("project_name"))
        if pid and _is_october_project(name):
            october_projects.append({"project_id": pid, "project_name": name})

    if args.max_projects > 0:
        october_projects = october_projects[:args.max_projects]

    print(json.dumps({
        "step": "identify_october_projects",
        "total_production_projects": len(prod_projects),
        "october_projects_found": len(october_projects),
        "project_ids": [p["project_id"] for p in october_projects],
    }, ensure_ascii=False))

    # Step 2: Read existing controller snapshot and claims for these projects
    ctrl_snapshot = _read_source(test_gateway, "controller_report_snapshot", [
        "corrector_id", "corrector_fio", "report_id", "action_id",
        "visit_id", "project_id", "project_name", "report_date",
        "workflow_state_codes", "source_status",
    ])
    claims = _read_source(test_gateway, "claims_mart_90d", [
        "publication_id", "action_id", "visit_id", "project_id",
        "opened_at_utc", "entry_mechanism", "closure_confidence", "current_state",
    ])

    # Filter to October projects
    oct_project_ids = {p["project_id"] for p in october_projects}
    oct_ctrl = [r for r in ctrl_snapshot if _as_str(r.get("project_id")) in oct_project_ids]
    oct_claims = [r for r in claims if _as_str(r.get("project_id")) in oct_project_ids]

    print(json.dumps({
        "step": "filter_existing_data",
        "controller_snapshot_rows": len(oct_ctrl),
        "claims_rows": len(oct_claims),
    }, ensure_ascii=False))

    # Step 3: Build project name map
    proj_names = {p["project_id"]: p["project_name"] for p in october_projects}
    for row in oct_ctrl:
        pid = _as_str(row.get("project_id"))
        name = _as_str(row.get("project_name"))
        if pid and name and pid not in proj_names:
            proj_names[pid] = name

    # Step 4: Build controller_current for October (from existing snapshot)
    captured_at = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    
    # Build controller_current_observed equivalent for October
    by_ctrl_proj: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"visits": set(), "actions": set(), "controller_fio": ""}
    )
    for row in oct_ctrl:
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
        if ctrl_fio and not by_ctrl_proj[key]["controller_fio"]:
            by_ctrl_proj[key]["controller_fio"] = ctrl_fio

    ctrl_current_rows = []
    for (ctrl_id, proj_id), vals in sorted(by_ctrl_proj.items()):
        proj_name = proj_names.get(proj_id, "")
        ctrl_current_rows.append({
            "publication_id": "candidate",
            "captured_at_utc": captured_at,
            "project_id": proj_id,
            "project_name": proj_name,
            "controller_id": ctrl_id,
            "controller_fio": vals["controller_fio"],
            "unique_visits_observed": len(vals["visits"]),
            "action_rows_observed": len(vals["actions"]),
            "coverage_status": "OBSERVED_IN_CURRENT_SNAPSHOT",
        })

    # Step 5: Build claims association for October
    by_action: dict[str, dict] = {}
    by_visit: dict[str, list[dict]] = defaultdict(list)
    for row in oct_ctrl:
        action_id = _as_str(row.get("action_id"))
        visit_id = _as_str(row.get("visit_id"))
        if action_id:
            by_action[action_id] = row
        if visit_id:
            by_visit[visit_id].append(row)

    claims_assoc_rows = []
    claims_pub_id = oct_claims[0].get("publication_id", "") if oct_claims else ""
    for claim in oct_claims:
        action_id = _as_str(claim.get("action_id"))
        visit_id = _as_str(claim.get("visit_id"))
        proj_id = _as_str(claim.get("project_id"))
        episode_id = f"{action_id}|{claim.get('opened_at_utc', '')}"

        proj_name = proj_names.get(proj_id, "")
        opened_at = _as_str(claim.get("opened_at_utc"))

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
            visit_rows = by_visit.get(visit_id, [])
            if len(visit_rows) == 1:
                ctrl_row = visit_rows[0]
                ctrl_id = _as_str(ctrl_row.get("corrector_id"))
                ctrl_fio = _as_str(ctrl_row.get("corrector_fio"))
                association_method = "VISIT_ID_UNIQUE"
                association_status = "MATCHED"
                ctrl_observed_at = _as_str(ctrl_row.get("report_date"))
            elif len(visit_rows) > 1:
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

        claims_assoc_rows.append({
            "publication_id": "candidate",
            "claim_publication_id": claims_pub_id,
            "episode_id": episode_id,
            "action_id": action_id,
            "visit_id": visit_id,
            "project_id": proj_id,
            "project_name": proj_name,
            "opened_at_utc": opened_at,
            "observed_controller_id": ctrl_id,
            "observed_controller_fio": ctrl_fio,
            "controller_observed_at_utc": ctrl_observed_at,
            "association_method": association_method,
            "association_status": association_status,
            "quality_flags": ";".join(sorted(quality_flags)),
        })

    # Step 6: Build coverage status
    ctrl_visits = set(_as_str(r.get("visit_id")) for r in oct_ctrl if _as_str(r.get("visit_id")))
    ctrl_projects = set(_as_str(r.get("project_id")) for r in oct_ctrl if _as_str(r.get("project_id")))
    ctrl_controllers = set(_as_str(r.get("corrector_id")) for r in oct_ctrl if _as_str(r.get("corrector_id")))

    claim_visits = set(_as_str(c.get("visit_id")) for c in oct_claims if _as_str(c.get("visit_id")))
    claim_projects = set(_as_str(c.get("project_id")) for c in oct_claims if _as_str(c.get("project_id")))

    matched = 0
    unmatched = 0
    ambiguous = 0
    for claim in oct_claims:
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

    coverage_rows = [{
        "publication_id": "candidate",
        "captured_at_utc": captured_at,
        "population_definition": "October 2026 projects from projects_current joined to controller_report_snapshot and claims_mart_90d via action_id then visit_id",
        "projects_targeted": len(oct_project_ids),
        "projects_collected": len(ctrl_projects),
        "visits_observed": len(ctrl_visits),
        "visits_without_controller": len(claim_visits - set(by_visit.keys())),
        "claim_episodes_considered": len(oct_claims),
        "claims_matched": matched,
        "claims_unmatched": unmatched,
        "claims_ambiguous": ambiguous,
        "source_timestamp": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "completeness_status": f"PARTIAL_CONTROLLER_SNAPSHOT_{len(ctrl_projects)}_PROJECTS_{len(ctrl_controllers)}_CONTROLLERS",
    }]

    # Step 7: Build detailed visits from controller snapshot
    facts = []
    for row in oct_ctrl:
        facts.append({
            "project_id": _as_str(row.get("project_id")),
            "visit_id": _as_str(row.get("visit_id")),
            "action_id": _as_str(row.get("action_id")),
            "participant_assigned": True,
            "visit_datetime_raw": "",
            "visit_datetime": "",
            "report_datetime_raw": "",
            "report_datetime": _as_str(row.get("report_date")),
            "workflow_status_code": _as_str(row.get("workflow_state_codes", "")).split("|")[0] if _as_str(row.get("workflow_state_codes")) else "",
            "workflow_status_label": "",
            "workflow_state_codes": _as_str(row.get("workflow_state_codes", "")),
            "controller_fio": _as_str(row.get("corrector_fio")),
            "visitor_user_id": _as_str(row.get("corrector_id")),
            "project_name_short": _as_str(row.get("project_name")),
            "source_url": f"{PORTAL_BASE_URL}/action/{_as_str(row.get('action_id'))}",
            "captured_at_utc": captured_at,
            "quality_flags": [],
        })

    # Build visit grain
    visits = visit_grain(facts)
    
    # Build detailed visits rows
    visits_rows = []
    for visit in visits:
        proj_id = visit["project_id"]
        proj_name = proj_names.get(proj_id, "")
        # Find controller for this visit
        visit_ctrl_id = ""
        visit_ctrl_fio = ""
        for fact in facts:
            if fact["project_id"] == proj_id and fact["visit_id"] == visit["visit_id"] and fact["controller_fio"]:
                visit_ctrl_id = fact["visitor_user_id"]
                visit_ctrl_fio = fact["controller_fio"]
                break
        
        visits_rows.append({
            "publication_id": "candidate",
            "captured_at_utc": captured_at,
            "project_id": proj_id,
            "project_name": proj_name,
            "visit_id": visit["visit_id"],
            "action_id": visit["action_ids"].split("|")[0] if visit["action_ids"] else "",
            "workflow_status_code": visit["workflow_status_code"],
            "workflow_status_label": visit.get("workflow_status_label", ""),
            "visit_datetime": visit["visit_datetime"],
            "report_datetime": visit["report_datetime"],
            "controller_id": visit_ctrl_id,
            "controller_fio": visit_ctrl_fio,
            "coverage_status": "OBSERVED_IN_CURRENT_SNAPSHOT" if visit_ctrl_id else "NO_CONTROLLER_IN_SNAPSHOT",
        })

    # Step 8: Compute publication_id and fingerprint
    all_records = ctrl_current_rows + claims_assoc_rows + coverage_rows + visits_rows
    
    def content_fingerprint(records: list[dict], fields: list[str]) -> str:
        timestamp_fields = {"captured_at_utc", "source_timestamp", "controller_observed_at_utc", "opened_at_utc"}
        payload = "\n".join(
            "\x1f".join(_stringify(record.get(f, "")) for f in fields if f not in timestamp_fields)
            for record in records
        )
        return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()

    fp = content_fingerprint(
        all_records,
        ["publication_id", "claim_publication_id"],
    )
    pub_id = f"oct2026ctrl-v2-{fp[:16]}"

    # Rebuild with actual publication_id
    for row in ctrl_current_rows:
        row["publication_id"] = pub_id
    for row in claims_assoc_rows:
        row["publication_id"] = pub_id
    for row in coverage_rows:
        row["publication_id"] = pub_id
    for row in visits_rows:
        row["publication_id"] = pub_id

    # Recompute fingerprint with actual publication_id
    fp = content_fingerprint(
        ctrl_current_rows + claims_assoc_rows + coverage_rows + visits_rows,
        ["publication_id", "claim_publication_id"],
    )
    pub_id = f"oct2026ctrl-v2-{fp[:16]}"

    # Final rebuild with final pub_id
    for row in ctrl_current_rows:
        row["publication_id"] = pub_id
    for row in claims_assoc_rows:
        row["publication_id"] = pub_id
    for row in coverage_rows:
        row["publication_id"] = pub_id
    for row in visits_rows:
        row["publication_id"] = pub_id

    # PRE-WRITE VALIDATION: Check all key fields are populated
    print(json.dumps({"step": "pre_write_validation", "tables": {}}, ensure_ascii=False))
    _validate_record_keys(visits_rows, KEY_FIELDS["oct2026_controller_visits"], "oct2026_controller_visits")
    _validate_record_keys(ctrl_current_rows, KEY_FIELDS["oct2026_controller_workload"], "oct2026_controller_workload")
    _validate_record_keys(claims_assoc_rows, KEY_FIELDS["oct2026_claims_association"], "oct2026_claims_association")
    _validate_record_keys(coverage_rows, KEY_FIELDS["oct2026_coverage_manifest"], "oct2026_coverage_manifest")
    print(json.dumps({"step": "pre_write_validation", "status": "PASSED"}, ensure_ascii=False))

    print(json.dumps({
        "candidate_status": "BUILT_NOT_PUBLISHED" if args.dry_run else "READY_TO_PUBLISH",
        "publication_id": pub_id,
        "fingerprint": fp,
        "rows": {
            "oct2026_controller_visits": len(visits_rows),
            "oct2026_controller_workload": len(ctrl_current_rows),
            "oct2026_claims_association": len(claims_assoc_rows),
            "oct2026_coverage_manifest": len(coverage_rows),
        },
        "october_projects": len(october_projects),
        "projects_with_controller_data": len(ctrl_projects),
        "projects_with_claims": len(claim_projects),
        "coverage_pct": round(100 * len(ctrl_projects) / len(oct_project_ids), 1) if oct_project_ids else 0,
    }, ensure_ascii=False))

    if args.dry_run:
        print(json.dumps({"status": "DRY_RUN_NO_WRITES"}))
        return 0

    # Check idempotency
    manifest_tab = "oct2026_coverage_manifest"
    if manifest_tab in titles:
        existing = test_gateway.read(manifest_tab)
        if existing and existing[0]:
            hdr = existing[0]
            pid_idx = hdr.index("publication_id") if "publication_id" in hdr else -1
            fp_idx = hdr.index("fingerprint") if "fingerprint" in hdr else -1
            for row in existing[1:]:
                if pid_idx >= 0 and fp_idx >= 0 and pid_idx < len(row) and fp_idx < len(row):
                    if str(row[pid_idx]) == pub_id:
                        if str(row[fp_idx]) == fp:
                            print(json.dumps({"status": "ALREADY_PUBLISHED", "publication_id": pub_id}))
                            return 0
                        raise MartError("REFUSED_PUBLICATION_ID_CONFLICT")

    # Ensure tabs exist
    for title, header in {
        "oct2026_controller_visits": VISITS_HEADER,
        "oct2026_controller_workload": WORKLOAD_HEADER,
        "oct2026_claims_association": CLAIMS_HEADER,
        "oct2026_coverage_manifest": COVERAGE_HEADER,
    }.items():
        _ensure_tab(test_gateway, title, header, titles)

    # Write data
    readbacks = {}
    for title, records in {
        "oct2026_controller_visits": visits_rows,
        "oct2026_controller_workload": ctrl_current_rows,
        "oct2026_claims_association": claims_assoc_rows,
        "oct2026_coverage_manifest": coverage_rows,
    }.items():
        header = {
            "oct2026_controller_visits": VISITS_HEADER,
            "oct2026_controller_workload": WORKLOAD_HEADER,
            "oct2026_claims_association": CLAIMS_HEADER,
            "oct2026_coverage_manifest": COVERAGE_HEADER,
        }[title]
        test_gateway.ensure_grid_capacity(title, len(test_gateway.read(title)) + len(records) + 10, len(header))
        readbacks[title] = _append_versioned(test_gateway, title, header, records, pub_id, titles)

    print(json.dumps({
        "status": "PUBLISHED_AND_READBACK_VERIFIED",
        "publication_id": pub_id,
        "fingerprint": fp,
        "test_sheets_url": f"https://docs.google.com/spreadsheets/d/{TEST_SPREADSHEET_ID}/edit",
        "readbacks": readbacks,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (MartError, RuntimeError) as error:
        print(json.dumps({"status": "REFUSED_OR_FAILED", "reason": str(error)[:500]}))
        raise SystemExit(2)
