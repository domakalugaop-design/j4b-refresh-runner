"""Append October 2026 collection data to TEST spreadsheet controller_report_snapshot tab."""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.google_service_account import google_token  # noqa: E402
from src.controller_quality_sheets import GoogleSheetsGateway  # noqa: E402

TEST_SPREADSHEET_ID = "1G_pXXNCLTwABSnSPtXxRCxF19VOCOvXi-DK21ZMzytM"
CHECKPOINT_DIR = Path.home() / ".j4b-october-collection"
SNAPSHOT_FILE = CHECKPOINT_DIR / "october_controller_report_snapshot.json"

REPORT_COLUMNS = (
    "publication_key", "generation_id", "report_id", "action_id", "visit_id",
    "project_id", "project_name", "corrector_id", "corrector_fio", "report_date",
    "workflow_state_codes", "status_35", "status_38", "status_39", "status_40",
    "status_45", "status_50", "report_url", "source_status", "source_snapshot_at",
    "data_coverage_flag",
)


def main() -> int:
    # Load aggregated data
    with open(SNAPSHOT_FILE, encoding="utf-8") as f:
        data = json.load(f)

    rows = data["rows"]
    print(f"Loaded {len(rows)} rows to append")

    # Convert to sheet format
    sheet_rows = []
    for row in rows:
        sheet_row = [str(row.get(col, "")) for col in REPORT_COLUMNS]
        sheet_rows.append(sheet_row)

    # Connect to TEST spreadsheet
    token = google_token()
    gateway = GoogleSheetsGateway(TEST_SPREADSHEET_ID, token)

    # Read current state
    current = gateway.read("controller_report_snapshot")
    print(f"Current rows in controller_report_snapshot: {len(current) - 1} (including header)")

    # Append new rows
    start_row = len(current) + 1
    print(f"Appending {len(sheet_rows)} rows starting at row {start_row}")

    # Ensure grid capacity
    gateway.ensure_grid_capacity("controller_report_snapshot", len(current) + len(sheet_rows) + 10, len(REPORT_COLUMNS))

    # Append in batches (Google Sheets API has limits)
    batch_size = 500
    for i in range(0, len(sheet_rows), batch_size):
        batch = sheet_rows[i:i+batch_size]
        batch_start = start_row + i
        gateway.update("controller_report_snapshot", f"A{batch_start}", batch)
        print(f"  Appended batch {i//batch_size + 1}: {len(batch)} rows")

    # Verify
    after = gateway.read("controller_report_snapshot")
    print(f"Rows after append: {len(after) - 1}")
    print(f"Expected: {len(current) - 1 + len(sheet_rows)}")

    # Check generation_id in new rows
    gen_id = data["generation_id"]
    gen_count = sum(1 for row in after[1:] if len(row) > 1 and row[1] == gen_id)
    print(f"Rows with generation_id {gen_id}: {gen_count}")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        raise SystemExit(2)
