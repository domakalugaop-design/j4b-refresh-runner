"""Controlled publication of the 90-day historical claims mart to the TEST workbook.

Safety gates:
- destination spreadsheet id is allowlisted to the TEST workbook;
- requires an explicit ``--approve-test-write`` flag;
- creates only the two agreed tabs (claims_mart_90d, claims_mart_manifest);
- idempotent: an already published publication_id must match the source
  fingerprint exactly, otherwise the run aborts without writing;
- the data tab is replaced in a single values.update call; the manifest
  tab is append-only, so prior publications stay readable;
- never touches the production source spreadsheet.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.claims_mart import (  # noqa: E402
    MANIFEST_FIELDS,
    MART_ROW_FIELDS,
    build_mart,
    cohort_claim_rate,
    episode_rows,
    mart_fingerprint,
    mart_manifest,
    publication_id_for,
)
from src.controller_quality_sheets import GoogleSheetsGateway, MartError  # noqa: E402
from src.google_service_account import google_token  # noqa: E402

TEST_SPREADSHEET_ID = "1G_pXXNCLTwABSnSPtXxRCxF19VOCOvXi-DK21ZMzytM"

DATA_TAB = "claims_mart_90d"
MANIFEST_TAB = "claims_mart_manifest"


def _row(record: dict, fields) -> list:
    return [record.get(name, "") for name in fields]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("extract_json", help="path to the local extract JSON produced by the claims-mart extraction script")
    parser.add_argument("denominator_json", help="path to the denominator JSON (completed visit ids, same snapshot)")
    parser.add_argument("--approve-test-write", action="store_true", dest="approve")
    args = parser.parse_args()

    if not args.approve:
        print(json.dumps({"status": "REFUSED_WRITE_NOT_APPROVED"}))
        return 2

    extract = json.loads(Path(args.extract_json).read_text(encoding="utf-8"))
    denominator = json.loads(Path(args.denominator_json).read_text(encoding="utf-8"))

    mart = build_mart(extract)
    rate = cohort_claim_rate(mart, denominator)
    rows = episode_rows(mart, publication_id="")  # fingerprint over payload sans id
    fingerprint = mart_fingerprint(rows)
    publication_id = publication_id_for(mart, fingerprint)
    rows = episode_rows(mart, publication_id)
    manifest = mart_manifest(
        mart, rate, publication_id,
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        fingerprint,
    )

    token = google_token()
    gateway = GoogleSheetsGateway(TEST_SPREADSHEET_ID, token)
    titles = gateway.titles()

    # Idempotency: refuse to republish a different mart under the same id.
    if MANIFEST_TAB in titles:
        existing = gateway.read(MANIFEST_TAB)
        header = existing[0] if existing else []
        id_index = header.index("publication_id") if "publication_id" in header else None
        fp_index = header.index("fingerprint") if "fingerprint" in header else None
        for row in existing[1:]:
            if id_index is not None and fp_index is not None \
                    and id_index < len(row) and fp_index < len(row) \
                    and str(row[id_index]) == publication_id:
                if str(row[fp_index]) == fingerprint:
                    print(json.dumps({"status": "ALREADY_PUBLISHED", "publication_id": publication_id}))
                    return 0
                print(json.dumps({"status": "REFUSED_PUBLICATION_ID_CONFLICT", "publication_id": publication_id}))
                return 2

    created = []
    for title, header in ((DATA_TAB, list(MART_ROW_FIELDS)), (MANIFEST_TAB, list(MANIFEST_FIELDS))):
        if title not in titles:
            gateway.add_sheet(title)
            created.append(title)
        gateway.ensure_headers(title, header)

    # Data tab: replace in one call (atomic per request).
    gateway.update(DATA_TAB, "A2", [_row(row, MART_ROW_FIELDS) for row in rows])
    # Manifest tab: append-only.
    existing_manifest = gateway.read(MANIFEST_TAB)
    next_row = len(existing_manifest) + 1
    gateway.update(MANIFEST_TAB, f"A{next_row}", [_row(manifest, MANIFEST_FIELDS)])

    # Read-back verification.
    read_back = gateway.read(DATA_TAB)
    data_ok = (
        len(read_back) == len(rows) + 1
        and read_back[1] == _row(rows[0], MART_ROW_FIELDS)
        and read_back[-1] == _row(rows[-1], MART_ROW_FIELDS)
    )
    manifest_back = gateway.read(MANIFEST_TAB)
    manifest_ok = any(
        len(r) > 2 and str(r[0]) == publication_id and str(r[2]) == fingerprint
        for r in manifest_back[1:]
    )

    print(json.dumps({
        "status": "PUBLISHED" if (data_ok and manifest_ok) else "READBACK_MISMATCH",
        "publication_id": publication_id,
        "rows": len(rows),
        "created_tabs": created,
        "fingerprint": fingerprint,
        "data_readback_ok": data_ok,
        "manifest_readback_ok": manifest_ok,
        "claim_rate_available": rate.get("claim_rate_available", False),
    }, ensure_ascii=False))
    return 0 if (data_ok and manifest_ok) else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except MartError as error:
        print(json.dumps({"status": "REFUSED", "reason": str(error)}))
        raise SystemExit(2)
