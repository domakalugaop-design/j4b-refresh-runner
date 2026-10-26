"""Controlled publication of a claim snapshot to the TEST Google workbook.

Safety gates:
- destination spreadsheet id is allowlisted to the TEST workbook;
- requires an explicit ``--approve-test-write`` flag;
- creates only the four agreed tabs and refuses duplicates;
- idempotent: an already published snapshot_id must match the source
  fingerprint exactly, otherwise the run aborts without writing;
- never touches the production source spreadsheet.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.claim_snapshot import (  # noqa: E402
    MANIFEST_FIELDS,
    PROVENANCE_FIELDS,
    SNAPSHOT_FIELDS,
    snapshot_fingerprint,
)
from src.controller_quality_sheets import GoogleSheetsGateway, MartError  # noqa: E402
from src.google_service_account import google_token  # noqa: E402

TEST_SPREADSHEET_ID = "1G_pXXNCLTwABSnSPtXxRCxF19VOCOvXi-DK21ZMzytM"

HISTORY_TAB = "visits_snapshot_history"
MANIFEST_TAB = "visits_snapshot_manifest"
CURRENT_TAB = "visits_current"
TREND_TAB = "claims_trend"

CURRENT_HEADER = [
    "snapshot_id", "captured_at_utc", "project_id", "visit_id", "action_id",
    "status_id", "source_quality", "action_url",
]
TREND_HEADER = [
    "snapshot_id", "captured_at_utc", "project_id", "visit_id", "action_id",
    "status_id", "query_window", "claim_event_time",
]
TREND_NOTE = (
    "claim_event_time is UNKNOWN: the /action state[39] response is a current-state "
    "observation, not a claim event timestamp."
)


def _row(record: dict, fields) -> list:
    return [record.get(name, "") for name in fields]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot_json", help="path to the local snapshot JSON produced by run_claim_snapshot.py")
    parser.add_argument("--approve-test-write", action="store_true", dest="approve")
    args = parser.parse_args()

    if not args.approve:
        print(json.dumps({"status": "REFUSED_WRITE_NOT_APPROVED"}))
        return 2

    payload = json.loads(Path(args.snapshot_json).read_text(encoding="utf-8"))
    if payload.get("status") != "OK":
        print(json.dumps({"status": "REFUSED_SNAPSHOT_NOT_OK"}))
        return 2
    records, manifest = payload["records"], payload["manifest"]
    fingerprint = snapshot_fingerprint(records)
    snapshot_id = manifest["snapshot_id"]

    token = google_token()
    gateway = GoogleSheetsGateway(TEST_SPREADSHEET_ID, token)
    titles = gateway.titles()

    # Idempotency: refuse to republish a different snapshot under the same id.
    if HISTORY_TAB in titles:
        existing = gateway.read(HISTORY_TAB)
        header = existing[0] if existing else []
        id_index = header.index("snapshot_id") if "snapshot_id" in header else None
        prior = [
            row for row in existing[1:]
            if id_index is not None and id_index < len(row) and str(row[id_index]) == snapshot_id
        ]
        if prior:
            prior_fingerprint = snapshot_fingerprint(
                [dict(zip(SNAPSHOT_FIELDS + PROVENANCE_FIELDS, row)) for row in prior]
            )
            if prior_fingerprint == fingerprint:
                print(json.dumps({"status": "ALREADY_PUBLISHED", "snapshot_id": snapshot_id}))
                return 0
            print(json.dumps({"status": "REFUSED_SNAPSHOT_ID_CONFLICT", "snapshot_id": snapshot_id}))
            return 2

    created = []
    for title, header in (
        (HISTORY_TAB, list(SNAPSHOT_FIELDS) + list(PROVENANCE_FIELDS)),
        (MANIFEST_TAB, list(MANIFEST_FIELDS)),
        (CURRENT_TAB, CURRENT_HEADER),
        (TREND_TAB, TREND_HEADER),
    ):
        if title not in titles:
            gateway.add_sheet(title)
            created.append(title)
        gateway.ensure_headers(title, header)
    if TREND_TAB in created or not gateway.read_range(TREND_TAB, "A2:A2"):
        gateway.update(TREND_TAB, "A2", [[
            "", "", "", "", "", "", "", TREND_NOTE,
        ]])

    gateway.update(HISTORY_TAB, "A2", [_row(record, SNAPSHOT_FIELDS + PROVENANCE_FIELDS) for record in records])
    gateway.update(MANIFEST_TAB, "A2", [_row(manifest, MANIFEST_FIELDS)])

    print(json.dumps({
        "status": "PUBLISHED", "snapshot_id": snapshot_id,
        "records": len(records), "created_tabs": created,
        "fingerprint": fingerprint,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except MartError as error:
        print(json.dumps({"status": "REFUSED", "reason": str(error)}))
        raise SystemExit(2)
