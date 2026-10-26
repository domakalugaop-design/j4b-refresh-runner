"""Local, read-only runner for Portal status-39 claim snapshots.

Input:  a saved ``/action`` ``state[39]`` search response (HTML file).
Output: snapshot records + manifest as JSON on local disk.

This runner never contacts the Portal, never writes to Google Sheets, and
never mutates production state.  Google publication is a separate,
explicitly authorized step.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.claim_snapshot import (  # noqa: E402
    ClaimSnapshotError,
    build_snapshot_records,
    parse_claim_search,
    snapshot_fingerprint,
    validate_snapshot_records,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("html_file", help="path to the saved /action state[39] HTML response")
    parser.add_argument("--output", required=True, help="path for the JSON snapshot output")
    parser.add_argument(
        "--query-window", default="UNSPECIFIED",
        help="label of the submitted filter window, e.g. 2026-10-01..2026-10-26",
    )
    parser.add_argument("--snapshot-id", default="", help="reuse an existing snapshot id (idempotent re-run)")
    args = parser.parse_args()

    path = Path(args.html_file).expanduser()
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            print(json.dumps({"status": "REFUSED_INPUT_PERMISSIONS_TOO_BROAD"}))
            return 2
        markup = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        print(json.dumps({"status": "REFUSED_INPUT_UNAVAILABLE"}))
        return 2

    try:
        search = parse_claim_search(markup)
    except ClaimSnapshotError as error:
        print(json.dumps({"status": "REFUSED_INVALID_RESPONSE", "reason": str(error)}))
        return 2

    snapshot_id = args.snapshot_id or uuid.uuid4().hex
    captured_at_utc = datetime.now(timezone.utc).isoformat()
    source_generation = f"action_state39_filter:{search.response_sha256[:16]}"
    # Idempotent re-run: an explicit snapshot id reuses the original capture
    # metadata so the same snapshot id always yields the same records.
    output_path = Path(args.output).expanduser()
    if args.snapshot_id and output_path.exists():
        try:
            previous = json.loads(output_path.read_text(encoding="utf-8"))
            if previous.get("manifest", {}).get("snapshot_id") == snapshot_id:
                captured_at_utc = previous["manifest"]["captured_at_utc"]
                source_generation = previous["manifest"]["source_generation"]
        except (OSError, ValueError, KeyError):
            pass
    records, manifest = build_snapshot_records(
        search, snapshot_id=snapshot_id, captured_at_utc=captured_at_utc,
        source_generation=source_generation, query_window=args.query_window,
    )
    try:
        validate_snapshot_records(records)
    except ClaimSnapshotError as error:
        print(json.dumps({"status": "VALIDATION_FAILED", "reason": str(error)}))
        return 2

    payload = {
        "status": "OK",
        "fingerprint": snapshot_fingerprint(records),
        "manifest": manifest,
        "records": records,
    }
    output = output_path
    output.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=1)
    print(json.dumps({
        "status": "OK", "snapshot_id": snapshot_id,
        "records": len(records), "fingerprint": payload["fingerprint"],
        "manifest": manifest,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
