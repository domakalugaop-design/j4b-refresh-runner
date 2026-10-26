"""Safety-gate tests for the controlled TEST publication script."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER = REPO_ROOT / "scripts" / "publish_claim_snapshot_test.py"


def _snapshot(tmp_path):
    payload = {
        "status": "OK",
        "fingerprint": "f" * 64,
        "manifest": {"snapshot_id": "snap1", "captured_at_utc": "t"},
        "records": [{
            "snapshot_id": "snap1", "captured_at_utc": "t", "source_generation": "g",
            "report_id": "", "visit_id": "780001", "project_id": "8200",
            "controller_id": "", "status_id": 39, "report_date": "", "visit_date": "",
            "source_quality": "OBSERVED_CURRENT_STATE_39", "published_at_utc": "",
            "action_id": "681001", "action_url": "https://lk.j4b.ru/action/681001",
            "observed_datetimes": "", "date_semantics": "UNCONFIRMED",
            "response_sha256": "x", "query_window": "w", "date_filter_enforced": "NO",
            "unassigned_rows": 0, "response_rows": 1,
        }],
    }
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_refuses_without_explicit_approval(tmp_path):
    result = subprocess.run(
        [sys.executable, str(RUNNER), str(_snapshot(tmp_path))],
        capture_output=True, text=True,
    )
    assert result.returncode == 2
    assert json.loads(result.stdout)["status"] == "REFUSED_WRITE_NOT_APPROVED"


def test_refuses_snapshot_not_ok(tmp_path):
    path = _snapshot(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["status"] = "VALIDATION_FAILED"
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(RUNNER), str(path), "--approve-test-write"],
        capture_output=True, text=True,
    )
    assert result.returncode == 2
    assert json.loads(result.stdout)["status"] == "REFUSED_SNAPSHOT_NOT_OK"
