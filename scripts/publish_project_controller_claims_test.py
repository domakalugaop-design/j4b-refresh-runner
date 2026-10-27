"""Build and publish project/claim source tables to the TEST Google workbook.

Only creates these new tabs: project_directory, project_monthly_metrics,
project_period_metrics, controller_monthly_metrics,
report_controller_claim_links, project_controller_claims_manifest.
All tabs are append-only and versioned by publication_id. Existing tabs,
including claims_mart_90d, are read-only and untouched.
The current v2 publication preserves numeric cell types for DataLens; the
earlier v1 publication remains in the same tabs as history and must not be
combined with v2 (filter by publication_id).

Requires the explicit --approve-test-write gate. Uses the owner-approved
direct MariaDB/TCP/no-TLS read-only contract only for project-name lookups
for claim-only project IDs; credentials remain in Keychain.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import subprocess
import sys
from pathlib import Path

import pymysql

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.controller_quality_sheets import GoogleSheetsGateway, MartError  # noqa: E402
from src.google_service_account import google_token  # noqa: E402
from src.project_controller_claims_mart import (  # noqa: E402
    CONTROLLER_MONTHLY_FIELDS,
    LINK_FIELDS,
    MANIFEST_FIELDS,
    PROJECT_DIRECTORY_FIELDS,
    PROJECT_MONTHLY_FIELDS,
    PROJECT_PERIOD_FIELDS,
    build_project_mart,
)

TEST_SPREADSHEET_ID = "1G_pXXNCLTwABSnSPtXxRCxF19VOCOvXi-DK21ZMzytM"
SOURCE_CLAIMS_TAB = "claims_mart_90d"
SOURCE_MANIFEST_TAB = "claims_mart_manifest"
OUTPUT_HEADERS = {
    "project_directory": PROJECT_DIRECTORY_FIELDS,
    "project_monthly_metrics": PROJECT_MONTHLY_FIELDS,
    "project_period_metrics": PROJECT_PERIOD_FIELDS,
    "controller_monthly_metrics": CONTROLLER_MONTHLY_FIELDS,
    "report_controller_claim_links": LINK_FIELDS,
    "project_controller_claims_manifest": MANIFEST_FIELDS,
}
OUTPUT_DATA = {
    "project_directory": "project_directory",
    "project_monthly_metrics": "project_monthly_metrics",
    "project_period_metrics": "project_period_metrics",
    "controller_monthly_metrics": "controller_monthly_metrics",
    "report_controller_claim_links": "report_controller_claim_links",
    "project_controller_claims_manifest": "manifest",
}
KEY_FIELDS = {
    "project_directory": ("project_id",),
    "project_monthly_metrics": ("project_id", "month"),
    "project_period_metrics": ("project_id",),
    "controller_monthly_metrics": ("project_id", "month"),
    "report_controller_claim_links": ("episode_id",),
}


def _keychain_password():
    result = subprocess.run(
        ["security", "find-generic-password", "-s", "j4b-production-db-readonly", "-w"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode or not result.stdout.strip():
        raise RuntimeError("Keychain item j4b-production-db-readonly is unavailable")
    return result.stdout.strip()


def _project_names_for_ids(ids):
    if not ids:
        return {}
    conn = pymysql.connect(
        host="lk.j4b.ru", port=3306, user="watch", password=_keychain_password(),
        database="j4b", connect_timeout=10, read_timeout=25, charset="utf8mb4",
    )
    result = {}
    try:
        with conn.cursor() as cur:
            cur.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
            for i in range(0, len(ids), 400):
                chunk = ids[i:i + 400]
                placeholders = ",".join(["%s"] * len(chunk))
                cur.execute(
                    f"SELECT i,name,typ FROM proj WHERE i IN ({placeholders})", chunk,
                )
                for project_id, name, type_code in cur.fetchall():
                    result[int(project_id)] = {
                        "project_id": int(project_id),
                        "project_name": name or "",
                        "project_type_code": int(type_code) if type_code is not None else None,
                    }
            conn.rollback()
    finally:
        conn.close()
    return result


def _read_claims(gateway):
    data = gateway.read(SOURCE_CLAIMS_TAB)
    manifest_rows = gateway.read(SOURCE_MANIFEST_TAB)
    if not data or not manifest_rows:
        raise MartError("claims source or its manifest is empty")
    headers = data[0]
    required = ("action_id", "visit_id", "project_id", "opened_at_utc", "entry_mechanism", "closure_confidence")
    if any(name not in headers for name in required):
        raise MartError("claims_mart_90d header mismatch")
    ix = {name: headers.index(name) for name in required}
    episodes = []
    for row in data[1:]:
        get = lambda name: row[ix[name]] if ix[name] < len(row) else ""
        try:
            project_value = get("project_id")
            visit_value = get("visit_id")
            episodes.append({
                "action_id": str(get("action_id")),
                "visit_id": str(visit_value) if visit_value not in ("", None) else None,
                "project_id": int(project_value) if project_value not in ("", None) else None,
                "opened_at_utc": str(get("opened_at_utc")),
                "entry_mechanism": str(get("entry_mechanism")),
                "closure_confidence": str(get("closure_confidence")),
            })
        except (TypeError, ValueError):
            raise MartError("claims_mart_90d contains a malformed row")
    mh = manifest_rows[0]
    latest = manifest_rows[-1]
    mix = {name: i for i, name in enumerate(mh)}
    for field in ("publication_id", "source_captured_at_utc"):
        if field not in mix:
            raise MartError(f"claims manifest missing field: {field}")
    claim_capture = str(latest[mix["source_captured_at_utc"]])
    try:
        capture_dt = dt.datetime.fromisoformat(claim_capture.replace("Z", "+00:00"))
    except ValueError:
        raise MartError("claims manifest capture time is invalid")
    later_rows = 0
    max_opened = None
    for ep in episodes:
        event_dt = dt.datetime.fromisoformat(ep["opened_at_utc"].replace("Z", "+00:00"))
        max_opened = max(max_opened, event_dt) if max_opened else event_dt
        if event_dt > capture_dt:
            later_rows += 1
    return {
        "publication_id": str(latest[mix["publication_id"]]),
        "captured_at_utc": claim_capture,
        "capture_anomaly": later_rows > 0,
        "rows_after_declared_capture": later_rows,
        "max_opened_at_utc": max_opened.isoformat() if max_opened else "",
        "episodes": episodes,
    }


def _stringify(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, float) and math.isfinite(v) and v.is_integer():
        return str(int(v))
    return str(v)


def _sheet_row(record, fields):
    # Preserve numeric values so Sheets stores metrics as numbers under RAW.
    # IDs that arrive as strings stay text; dates stay ISO strings.
    return [record.get(f, "") for f in fields]


def _key_from_record(record, fields):
    return tuple(_stringify(record.get(f, "")) for f in fields)


def _key_from_sheet(row, header, fields):
    indexes = [header.index(f) for f in fields]
    return tuple(_stringify(row[i] if i < len(row) else "") for i in indexes)


def _rows_match(title, existing_row, expected_row, header):
    """Compare source fields; publication creation time is first-write metadata."""
    if title == "project_controller_claims_manifest":
        ignored = {header.index("created_at_utc")}
        return all(
            _stringify(existing_row[i] if i < len(existing_row) else "")
            == _stringify(expected_row[i] if i < len(expected_row) else "")
            for i in range(len(header)) if i not in ignored
        )
    return existing_row == expected_row


def _ensure_tab(gateway, title, header, titles):
    if title not in titles:
        gateway.add_sheet(title)
        titles.add(title)
    current = gateway.read_range(title, "A1:Z1")
    if current and current[0]:
        if current[0] != header:
            raise MartError(f"REFUSED_HEADER_MISMATCH:{title}")
    else:
        gateway.ensure_headers(title, header)


def _append_versioned(gateway, title, header, records, pub_id):
    current = gateway.read(title)
    if not current or current[0] != header:
        raise MartError(f"REFUSED_HEADER_MISMATCH:{title}")
    fields = KEY_FIELDS.get(title)
    if title == "project_controller_claims_manifest":
        fields = ("publication_id",)
    if not fields:
        raise MartError(f"no grain key configured for {title}")
    current_pub_idx = header.index("publication_id")
    existing = {}
    for row in current[1:]:
        if current_pub_idx >= len(row) or str(row[current_pub_idx]) != pub_id:
            continue
        key = _key_from_sheet(row, header, fields)
        if key in existing:
            raise MartError(f"REFUSED_DUPLICATE_GRAIN:{title}:{key}")
        existing[key] = [_stringify(row[i] if i < len(row) else "") for i in range(len(header))]
    expected = {}
    expected_raw = {}
    for record in records:
        key = _key_from_record(record, fields)
        if key in expected:
            raise MartError(f"REFUSED_DUPLICATE_EXPECTED_GRAIN:{title}:{key}")
        raw_row = _sheet_row(record, header)
        expected_raw[key] = raw_row
        expected[key] = [_stringify(v) for v in raw_row]
    for key, prior in existing.items():
        if key not in expected or not _rows_match(title, prior, expected[key], header):
            raise MartError(f"REFUSED_PARTIAL_PUBLICATION_CONFLICT:{title}:{key}")
    missing = [row for key, row in expected_raw.items() if key not in existing]
    if missing:
        next_row = len(current) + 1
        gateway.ensure_grid_capacity(title, len(current) + len(missing), len(header))
        gateway.update(title, f"A{next_row}", missing)
    # independent full read-back by publication id and primary grain
    after = gateway.read(title)
    found = {}
    for row in after[1:]:
        if current_pub_idx < len(row) and str(row[current_pub_idx]) == pub_id:
            found[_key_from_sheet(row, header, fields)] = [
                _stringify(row[i] if i < len(row) else "") for i in range(len(header))
            ]
    if set(found) != set(expected) or any(
        not _rows_match(title, found[key], expected[key], header) for key in expected
    ):
        raise MartError(f"READBACK_MISMATCH:{title}")
    return {"expected": len(expected), "prior": len(existing), "added": len(missing), "readback_ok": True}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("completion_extract_json")
    parser.add_argument("--approve-test-write", action="store_true", dest="approve")
    parser.add_argument("--dry-run", action="store_true", dest="dry_run")
    args = parser.parse_args()
    if not (args.approve or args.dry_run):
        print(json.dumps({"status": "REFUSED_WRITE_NOT_APPROVED"}))
        return 2

    completion = json.loads(Path(args.completion_extract_json).read_text(encoding="utf-8"))
    gateway = GoogleSheetsGateway(TEST_SPREADSHEET_ID, google_token())
    titles = set(gateway.titles())
    claims = _read_claims(gateway)
    completed_ids = {int(p["project_id"]) for p in completion.get("project_directory", [])}
    claim_ids = {int(ep["project_id"]) for ep in claims["episodes"] if ep["project_id"] is not None}
    missing_ids = sorted(claim_ids - completed_ids)
    claim_directory = _project_names_for_ids(missing_ids)
    claims["project_directory"] = list(claim_directory.values())
    mart = build_project_mart(completion, claims, "candidate")

    print(json.dumps({
        "candidate_status": "BUILT_NOT_PUBLISHED" if args.dry_run else "READY_TO_PUBLISH",
        "publication_id": mart["publication_id"],
        "fingerprint": mart["fingerprint"],
        "manifest": mart["manifest"],
        "rows": {name: len(mart[key]) if isinstance(mart[key], list) else 1 for name, key in OUTPUT_DATA.items()},
        "quality": mart["quality"],
    }, ensure_ascii=False))

    if args.dry_run:
        print(json.dumps({"status": "DRY_RUN_NO_WRITES"}))
        return 0

    manifest_rows = gateway.read("project_controller_claims_manifest") if "project_controller_claims_manifest" in titles else []
    if manifest_rows and manifest_rows[0] == MANIFEST_FIELDS:
        mh = manifest_rows[0]
        pid_i, fp_i = mh.index("publication_id"), mh.index("fingerprint")
        for row in manifest_rows[1:]:
            if pid_i < len(row) and str(row[pid_i]) == mart["publication_id"]:
                if fp_i < len(row) and str(row[fp_i]) == mart["fingerprint"]:
                    # Per-tab verification below distinguishes complete from partial writes.
                    break
                raise MartError("REFUSED_PUBLICATION_ID_CONFLICT")

    for title, header in OUTPUT_HEADERS.items():
        _ensure_tab(gateway, title, header, titles)

    readbacks = {}
    for title, key in OUTPUT_DATA.items():
        records = [mart[key]] if key == "manifest" else mart[key]
        readbacks[title] = _append_versioned(
            gateway, title, OUTPUT_HEADERS[title], records, mart["publication_id"],
        )

    already = all(item["added"] == 0 and item["prior"] == item["expected"] for item in readbacks.values())
    print(json.dumps({
        "status": "ALREADY_PUBLISHED" if already else "PUBLISHED_AND_READBACK_VERIFIED",
        "publication_id": mart["publication_id"],
        "fingerprint": mart["fingerprint"],
        "test_sheets_url": f"https://docs.google.com/spreadsheets/d/{TEST_SPREADSHEET_ID}/edit",
        "readbacks": readbacks,
        "report_count_status": mart["manifest"]["report_denominator_status"],
        "controller_status": mart["manifest"]["controller_history_status"],
        "claim_rate_status": mart["manifest"]["claim_rate_status"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (MartError, RuntimeError, pymysql.MySQLError) as error:
        print(json.dumps({"status": "REFUSED_OR_FAILED", "reason": str(error)[:300]}))
        raise SystemExit(2)
