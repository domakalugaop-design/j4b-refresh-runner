"""Publish controller–claims association tabs to TEST Google Sheets.

Three new tabs:
- controller_current_observed
- claims_controller_association
- controller_coverage_status

All append-only, versioned by publication_id, with explicit approval gate,
idempotency, and read-back verification.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.controller_claims_association import (  # noqa: E402
    CONTROLLER_CURRENT_FIELDS,
    CLAIMS_CONTROLLER_FIELDS,
    COVERAGE_FIELDS,
    build_claims_controller_association,
    build_controller_current,
    build_coverage_status,
    fingerprint,
    publication_id_for,
)
from src.controller_quality_sheets import GoogleSheetsGateway, MartError  # noqa: E402
from src.google_service_account import google_token  # noqa: E402

TEST_SPREADSHEET_ID = "1G_pXXNCLTwABSnSPtXxRCxF19VOCOvXi-DK21ZMzytM"

SOURCE_CLAIMS_TAB = "claims_mart_90d"
SOURCE_CTRL_TAB = "controller_report_snapshot"
SOURCE_PROJ_TAB = "project_directory"

OUTPUT_TABS = {
    "controller_current_observed": "controller_current_observed",
    "claims_controller_association": "claims_controller_association",
    "controller_coverage_status": "controller_coverage_status",
}

OUTPUT_HEADERS = {
    "controller_current_observed": [
        "publication_id", "captured_at_utc", "project_id", "project_name",
        "controller_id", "controller_fio", "unique_visits_observed",
        "action_rows_observed", "coverage_status",
    ],
    "claims_controller_association": [
        "claim_publication_id", "episode_id", "action_id", "visit_id",
        "project_id", "project_name", "opened_at_utc",
        "observed_controller_id", "observed_controller_fio",
        "controller_observed_at_utc", "association_method",
        "association_status", "quality_flags",
    ],
    "controller_coverage_status": [
        "publication_id", "captured_at_utc", "population_definition",
        "projects_targeted", "projects_collected", "visits_observed",
        "visits_without_controller", "claim_episodes_considered",
        "claims_matched", "claims_unmatched", "claims_ambiguous",
        "source_timestamp", "completeness_status",
    ],
}

KEY_FIELDS = {
    "controller_current_observed": ("publication_id", "controller_id", "project_id"),
    "claims_controller_association": ("episode_id",),
    "controller_coverage_status": ("publication_id",),
}

PUB_ID_FIELD = {
    "controller_current_observed": "publication_id",
    "claims_controller_association": "claim_publication_id",
    "controller_coverage_status": "publication_id",
}


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


def _ensure_tab(gateway: GoogleSheetsGateway, title: str, header: list[str], titles: set[str]):
    if title not in titles:
        gateway.add_sheet(title)
        titles.add(title)
    current = gateway.read_range(title, "A1:Z1")
    if current and current[0]:
        if current[0] != header:
            raise MartError(f"REFUSED_HEADER_MISMATCH:{title}")
    else:
        gateway.ensure_headers(title, header)


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
    if not current or current[0] != header:
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
        if key not in expected or prior != expected[key]:
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


def _rows_match(title: str, existing_row: list, expected_row: list, header: list[str]) -> bool:
    if title == "controller_coverage_status":
        ignored = {header.index("source_timestamp")}
        return all(
            _stringify(existing_row[i] if i < len(existing_row) else "")
            == _stringify(expected_row[i] if i < len(expected_row) else "")
            for i in range(len(header)) if i not in ignored
        )
    return existing_row == expected_row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approve-test-write", action="store_true", dest="approve")
    parser.add_argument("--dry-run", action="store_true", dest="dry_run")
    args = parser.parse_args()

    if not (args.approve or args.dry_run):
        print(json.dumps({"status": "REFUSED_WRITE_NOT_APPROVED"}))
        return 2

    gateway = GoogleSheetsGateway(TEST_SPREADSHEET_ID, google_token())
    titles = set(gateway.titles())

    # Read sources
    claims = _read_source(gateway, SOURCE_CLAIMS_TAB, [
        "publication_id", "action_id", "visit_id", "project_id",
        "opened_at_utc", "entry_mechanism", "closure_confidence", "current_state",
    ])
    ctrl_snapshot = _read_source(gateway, SOURCE_CTRL_TAB, [
        "corrector_id", "corrector_fio", "report_id", "action_id",
        "visit_id", "project_id", "project_name", "report_date",
        "workflow_state_codes", "source_status",
    ])
    proj_dir = _read_source(gateway, SOURCE_PROJ_TAB, [
        "project_id", "project_name",
    ])

    # Build project name map (prefer project_directory, fallback to ctrl snapshot)
    proj_names = {}
    for row in proj_dir:
        pid = str(row.get("project_id", ""))
        name = str(row.get("project_name", ""))
        if pid and name:
            proj_names[pid] = name
    for row in ctrl_snapshot:
        pid = str(row.get("project_id", ""))
        name = str(row.get("project_name", ""))
        if pid and name and pid not in proj_names:
            proj_names[pid] = name

    # Build association data
    captured_at = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ctrl_current = build_controller_current(
        ctrl_snapshot, proj_names, "candidate", captured_at
    )
    claims_assoc = build_claims_controller_association(
        claims, ctrl_snapshot, proj_names, "candidate", captured_at
    )
    coverage = build_coverage_status(claims, ctrl_snapshot, "candidate", captured_at)

    # Compute publication_id and fingerprint
    all_rows = {
        "controller_current_observed": ctrl_current,
        "claims_controller_association": claims_assoc,
        "controller_coverage_status": coverage,
    }
    # Compute content-based fingerprint (excluding timestamps for idempotency)
    def content_fingerprint(records: list[dict], fields: list[str]) -> str:
        timestamp_fields = {"captured_at_utc", "source_timestamp", "controller_observed_at_utc", "opened_at_utc"}
        payload = "\n".join(
            "\x1f".join(_stringify(record.get(f, "")) for f in fields if f not in timestamp_fields)
            for record in records
        )
        return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()

    fp = content_fingerprint(
        ctrl_current + claims_assoc + coverage,
        ["publication_id", "claim_publication_id"],
    )
    pub_id = publication_id_for("ctrlclaims", fp)

    # Rebuild with actual publication_id
    ctrl_current = build_controller_current(ctrl_snapshot, proj_names, pub_id, captured_at)
    claims_assoc = build_claims_controller_association(claims, ctrl_snapshot, proj_names, pub_id, captured_at)
    coverage = build_coverage_status(claims, ctrl_snapshot, pub_id, captured_at)

    # Recompute fingerprint with actual publication_id in rows (content-based)
    fp = content_fingerprint(
        ctrl_current + claims_assoc + coverage,
        ["publication_id", "claim_publication_id"],
    )
    pub_id = publication_id_for("ctrlclaims", fp)

    # Final rebuild with final pub_id
    ctrl_current = build_controller_current(ctrl_snapshot, proj_names, pub_id, captured_at)
    claims_assoc = build_claims_controller_association(claims, ctrl_snapshot, proj_names, pub_id, captured_at)
    coverage = build_coverage_status(claims, ctrl_snapshot, pub_id, captured_at)

    print(json.dumps({
        "candidate_status": "BUILT_NOT_PUBLISHED" if args.dry_run else "READY_TO_PUBLISH",
        "publication_id": pub_id,
        "fingerprint": fp,
        "rows": {name: len(records) for name, records in {
            "controller_current_observed": ctrl_current,
            "claims_controller_association": claims_assoc,
            "controller_coverage_status": coverage,
        }.items()},
    }, ensure_ascii=False))

    if args.dry_run:
        print(json.dumps({"status": "DRY_RUN_NO_WRITES"}))
        return 0

    # Check idempotency
    manifest_tab = "controller_coverage_status"
    if manifest_tab in titles:
        existing = gateway.read(manifest_tab)
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
        "controller_current_observed": [
            "publication_id", "captured_at_utc", "project_id", "project_name",
            "controller_id", "controller_fio", "unique_visits_observed",
            "action_rows_observed", "coverage_status",
        ],
        "claims_controller_association": [
            "claim_publication_id", "episode_id", "action_id", "visit_id",
            "project_id", "project_name", "opened_at_utc",
            "observed_controller_id", "observed_controller_fio",
            "controller_observed_at_utc", "association_method",
            "association_status", "quality_flags",
        ],
        "controller_coverage_status": [
            "publication_id", "captured_at_utc", "population_definition",
            "projects_targeted", "projects_collected", "visits_observed",
            "visits_without_controller", "claim_episodes_considered",
            "claims_matched", "claims_unmatched", "claims_ambiguous",
            "source_timestamp", "completeness_status",
        ],
    }.items():
        _ensure_tab(gateway, title, header, titles)

    # Write data
    readbacks = {}
    for title, records in {
        "controller_current_observed": ctrl_current,
        "claims_controller_association": claims_assoc,
        "controller_coverage_status": coverage,
    }.items():
        header = {
            "controller_current_observed": [
                "publication_id", "captured_at_utc", "project_id", "project_name",
                "controller_id", "controller_fio", "unique_visits_observed",
                "action_rows_observed", "coverage_status",
            ],
            "claims_controller_association": [
                "claim_publication_id", "episode_id", "action_id", "visit_id",
                "project_id", "project_name", "opened_at_utc",
                "observed_controller_id", "observed_controller_fio",
                "controller_observed_at_utc", "association_method",
                "association_status", "quality_flags",
            ],
            "controller_coverage_status": [
                "publication_id", "captured_at_utc", "population_definition",
                "projects_targeted", "projects_collected", "visits_observed",
                "visits_without_controller", "claim_episodes_considered",
                "claims_matched", "claims_unmatched", "claims_ambiguous",
                "source_timestamp", "completeness_status",
            ],
        }[title]
        # Ensure grid capacity before append
        gateway.ensure_grid_capacity(title, len(gateway.read(title)) + len(records) + 10, len(header))
        readbacks[title] = _append_versioned(gateway, title, header, records, pub_id, titles)

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
        print(json.dumps({"status": "REFUSED_OR_FAILED", "reason": str(error)[:300]}))
        raise SystemExit(2)