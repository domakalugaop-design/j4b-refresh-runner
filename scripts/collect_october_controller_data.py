"""Collect October 2026 controller data from Portal for all 219 October projects.

Uses existing parser (visit_registry.py) and portal client.
Saves checkpoint locally for resume capability.
Fetches full year data for each project (portal date filter is by visit/report date).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.portal_transport import PortalSession  # noqa: E402
from src.visit_registry import (  # noqa: E402
    VisitRegistryError,
    parse_action_fact_rows,
)
from src.acquisition import ACTION_STATE_CODES  # noqa: E402

PORTAL_BASE_URL = "https://lk.j4b.ru"
PORTAL_KEYCHAIN_SERVICE = "j4b-web-login"
CHECKPOINT_DIR = Path.home() / ".j4b-october-collection"
CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
CHECKPOINT_FILE = CHECKPOINT_DIR / "october_collection_checkpoint.json"
RAW_RESPONSES_DIR = CHECKPOINT_DIR / "raw_responses"
RAW_RESPONSES_DIR.mkdir(parents=True, exist_ok=True)


def _get_portal_credentials() -> tuple[str, str]:
    """Get portal credentials from macOS Keychain."""
    values = []
    for account in ("login", "password"):
        result = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", PORTAL_KEYCHAIN_SERVICE,
             "-a", account, "-w"],
            capture_output=True, text=True, check=False, timeout=10,
        )
        value = result.stdout.rstrip("\r\n")
        if result.returncode != 0 or not value:
            raise RuntimeError(f"Portal Keychain credential unavailable for {account}")
        values.append(value)
    return values[0], values[1]


def _get_october_project_ids() -> list[dict[str, str]]:
    """Get October project IDs from production spreadsheet (by name markers)."""
    october_project_ids = [
        "736", "742", "3154", "3331", "4907", "5488", "6108", "6408", "7759", "8044",
        "8068", "8069", "8113", "8133", "8135", "8141", "8142", "8149", "8150", "8155",
        "8167", "8174", "8175", "8176", "8177", "8181", "8186", "8187", "8188", "8189",
        "8190", "8191", "8192", "8193", "8195", "8196", "8199", "8200", "8202", "8203",
        "8204", "8205", "8206", "8207", "8208", "8209", "8210", "8213", "8214", "8215",
        "8216", "8217", "8218", "8219", "8220", "8221", "8222", "8223", "8224", "8225",
        "8226", "8227", "8228", "8231", "8232", "8233", "8234", "8235", "8236", "8237",
        "8238", "8239", "8240", "8241", "8242", "8243", "8244", "8245", "8246", "8247",
        "8248", "8249", "8250", "8254", "8255", "8257", "8258", "8259", "8260", "8261",
        "8262", "8263", "8264", "8265", "8266", "8267", "8268", "8269", "8270", "8271",
        "8272", "8273", "8274", "8275", "8276", "8277", "8278", "8279", "8280", "8281",
        "8282", "8283", "8285", "8286", "8288", "8289", "8290", "8291", "8292", "8293",
        "8294", "8295", "8296", "8297", "8300", "8309", "8310", "8311", "8312", "8313",
        "8314", "8315", "8316", "8317", "8318", "8319", "8322", "8323", "8324", "8325",
        "8326", "8327", "8328", "8329", "8330", "8331", "8332", "8333", "8334", "8335",
        "8337", "8338", "8339", "8340", "8341", "8342", "8343", "8344", "8346", "8347",
        "8348", "8349", "8350", "8351", "8352", "8353", "8354", "8355", "8356", "8357",
        "8358", "8359", "8360", "8361", "8362", "8363", "8364", "8365", "8366", "8367",
        "8368", "8369", "8372", "8373", "8374", "8376", "8377", "8378", "8379", "8380",
        "8381", "8382", "8383", "8384", "8385", "8386", "8387", "8388", "8389", "8390",
        "8391", "8392", "8393", "8394", "8397", "8398", "8399", "8400",
        "8401", "8402", "8403", "8404", "8405", "8406", "8407", "8408", "8411", "8414"
    ]
    seen = set()
    unique = []
    for pid in october_project_ids:
        if pid not in seen:
            seen.add(pid)
            unique.append({"project_id": pid, "project_name": ""})
    return unique


def _load_checkpoint() -> dict[str, Any]:
    if CHECKPOINT_FILE.exists():
        with open(CHECKPOINT_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {
        "version": 1,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "projects": {},
        "stats": {"total": 0, "collected": 0, "failed": 0, "parse_failed": 0, "no_data": 0},
    }


def _save_checkpoint(checkpoint: dict[str, Any]) -> None:
    tmp = CHECKPOINT_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(checkpoint, f, ensure_ascii=False, indent=1)
    tmp.replace(CHECKPOINT_FILE)


def _fetch_project_actions(session: PortalSession, project_id: str) -> tuple[int, str, bytes]:
    """Fetch per-project /action response for FULL YEAR 2026."""
    import json as json_lib
    data = {
        "proj": json_lib.dumps([project_id]),
        "dt1": "01.01.2026",
        "dt2": "31.12.2026",
        "limit": "10000", "send": "send", "user": "", "place": "", "city": "",
    }
    data.update({f"state[{code}]": "on" for code in ACTION_STATE_CODES})
    status, content_type, body = session.request(
        "/action", "POST", data=data, accept="text/html, application/json",
    )
    return status, content_type, body


def _parse_response(html: str, project_id: str, captured_at: str) -> list[dict[str, Any]]:
    """Parse HTML response using visit_registry parser."""
    facts = parse_action_fact_rows(
        html,
        project_id,
        captured_at_utc=captured_at,
        source_url=f"{PORTAL_BASE_URL}/action",
    )
    return facts


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect October 2026 controller data from Portal (full year fetch)")
    parser.add_argument("--max-projects", type=int, default=0, help="Limit projects for testing (0 = all)")
    parser.add_argument("--interval", type=float, default=1.5, help="Seconds between requests")
    parser.add_argument("--resume", action="store_true", help="Resume from checkpoint")
    parser.add_argument("--project", action="append", help="Specific project ID to collect (repeatable)")
    args = parser.parse_args()

    if args.interval < 1.5:
        print(json.dumps({"status": "REFUSED_INTERVAL", "minimum": 1.5}))
        return 2

    # Get project list
    if args.project:
        projects = [{"project_id": pid, "project_name": ""} for pid in args.project]
    else:
        projects = _get_october_project_ids()
        if args.max_projects > 0:
            projects = projects[:args.max_projects]

    print(json.dumps({
        "status": "STARTING",
        "total_projects": len(projects),
        "project_ids": [p["project_id"] for p in projects],
    }, ensure_ascii=False))

    # Load checkpoint
    checkpoint = _load_checkpoint()
    if not args.resume:
        checkpoint = {
            "version": 1,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "projects": {},
            "stats": {"total": len(projects), "collected": 0, "failed": 0, "parse_failed": 0, "no_data": 0},
        }

    # Get credentials
    try:
        login, password = _get_portal_credentials()
    except RuntimeError as e:
        print(json.dumps({"status": "CREDENTIALS_UNAVAILABLE", "error": str(e)}))
        return 2

    # Create session
    session = PortalSession(timeout=90, base_url=PORTAL_BASE_URL, login=login, password=password)
    try:
        session.login()
        print(json.dumps({"status": "LOGIN_SUCCESS"}))
    except Exception as e:
        print(json.dumps({"status": "LOGIN_FAILED", "error": str(e)}))
        return 2

    try:
        # Process projects
        for index, project in enumerate(projects):
            pid = project["project_id"]
            
            # Skip if already collected
            if pid in checkpoint["projects"] and checkpoint["projects"][pid].get("status") == "COLLECTED":
                print(json.dumps({"status": "SKIP_ALREADY_COLLECTED", "project_id": pid}))
                continue

            if index > 0:
                time.sleep(args.interval)

            print(json.dumps({"status": "FETCHING", "project_id": pid, "progress": f"{index+1}/{len(projects)}"}))

            try:
                status, content_type, body = _fetch_project_actions(session, pid)
                
                if status != 200 or "html" not in content_type.lower():
                    checkpoint["projects"][pid] = {
                        "status": "SOURCE_UNAVAILABLE",
                        "http_status": status,
                        "content_type": content_type,
                        "attempted_at": datetime.now(timezone.utc).isoformat(),
                    }
                    checkpoint["stats"]["failed"] += 1
                    _save_checkpoint(checkpoint)
                    print(json.dumps({"status": "SOURCE_UNAVAILABLE", "project_id": pid, "http_status": status}))
                    continue

                # Save raw response
                raw_file = RAW_RESPONSES_DIR / f"{pid}.html"
                raw_file.write_bytes(body)
                
                # Parse
                captured_at = datetime.now(timezone.utc).isoformat()
                try:
                    facts = _parse_response(body.decode("utf-8", errors="replace"), pid, captured_at)
                    
                    if not facts:
                        checkpoint["projects"][pid] = {
                            "status": "NO_DATA",
                            "raw_file": str(raw_file),
                            "attempted_at": captured_at,
                        }
                        checkpoint["stats"]["no_data"] += 1
                        _save_checkpoint(checkpoint)
                        print(json.dumps({"status": "NO_DATA", "project_id": pid}))
                        continue

                    # Save parsed facts
                    facts_file = RAW_RESPONSES_DIR / f"{pid}_facts.json"
                    facts_data = {
                        "project_id": pid,
                        "captured_at": captured_at,
                        "facts": facts,
                        "fact_count": len(facts),
                    }
                    facts_file.write_text(json.dumps(facts_data, ensure_ascii=False), encoding="utf-8")

                    checkpoint["projects"][pid] = {
                        "status": "COLLECTED",
                        "raw_file": str(raw_file),
                        "facts_file": str(facts_file),
                        "fact_count": len(facts),
                        "collected_at": captured_at,
                    }
                    checkpoint["stats"]["collected"] += 1
                    _save_checkpoint(checkpoint)
                    print(json.dumps({"status": "COLLECTED", "project_id": pid, "facts": len(facts)}))

                except VisitRegistryError as e:
                    checkpoint["projects"][pid] = {
                        "status": "PARSE_FAILED",
                        "raw_file": str(raw_file),
                        "error": str(e),
                        "attempted_at": captured_at,
                    }
                    checkpoint["stats"]["parse_failed"] += 1
                    _save_checkpoint(checkpoint)
                    print(json.dumps({"status": "PARSE_FAILED", "project_id": pid, "error": str(e)}))

            except Exception as e:
                checkpoint["projects"][pid] = {
                    "status": "FETCH_FAILED",
                    "error": str(e),
                    "attempted_at": datetime.now(timezone.utc).isoformat(),
                }
                checkpoint["stats"]["failed"] += 1
                _save_checkpoint(checkpoint)
                print(json.dumps({"status": "FETCH_FAILED", "project_id": pid, "error": str(e)}))

    finally:
        session.close()

    # Final summary
    checkpoint["completed_at"] = datetime.now(timezone.utc).isoformat()
    _save_checkpoint(checkpoint)

    print(json.dumps({
        "status": "COMPLETED",
        "stats": checkpoint["stats"],
        "checkpoint_file": str(CHECKPOINT_FILE),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        print(json.dumps({"status": "ERROR", "error": str(e)}))
        raise SystemExit(2)
