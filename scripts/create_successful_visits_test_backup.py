#!/usr/bin/env python3
"""Create and verify a private Drive rollback copy for the isolated TEST workbook."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

from src.google_service_account import DRIVE_SCOPE, SHEETS_SCOPE, google_token
from src.production import col

PINNED_TEST_SPREADSHEET_ID = "13pPltgzR6HceIW4OjtLyzOrXwNdEcDQ1RBxR0L7ZCK8"
TEST_TITLE = "J4B Portal — Successful Visits — TEST — 2026-10-27"
BACKUP_MIME_TYPE = "application/vnd.google-apps.spreadsheet"
PAGE_ROWS = 250


def _request(url: str, token: str, *, method: str = "GET", body: dict[str, Any] | None = None) -> Any:
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.load(response)
    except HTTPError as exc:
        raise RuntimeError(f"Google API request failed with HTTP {exc.code}") from None


def _spreadsheet_metadata(token: str, spreadsheet_id: str) -> dict[str, Any]:
    query = urllib.parse.urlencode({
        "fields": "spreadsheetId,properties.title,sheets.properties(title,gridProperties(rowCount,columnCount))"
    })
    return _request(
        f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}?{query}", token
    )


def _values_page(token: str, spreadsheet_id: str, a1_range: str) -> list[list[Any]]:
    query = urllib.parse.urlencode({"valueRenderOption": "UNFORMATTED_VALUE"})
    encoded_range = urllib.parse.quote(a1_range, safe="!:'")
    result = _request(
        f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{encoded_range}?{query}",
        token,
    )
    return result.get("values", [])


def _fingerprint_tab(token: str, spreadsheet_id: str, title: str, rows: int, width: int) -> tuple[int, str]:
    digest = hashlib.sha256()
    returned_rows = 0
    end_column = col(width - 1)
    for start in range(1, max(rows, 1) + 1, PAGE_ROWS):
        end = min(rows, start + PAGE_ROWS - 1)
        page = _values_page(token, spreadsheet_id, f"'{title}'!A{start}:{end_column}{end}")
        returned_rows += len(page)
        for row in page:
            normalized = list(row[:width]) + [""] * max(0, width - len(row))
            payload = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
            digest.update(len(payload).to_bytes(8, "big"))
            digest.update(payload)
    return returned_rows, digest.hexdigest()


def _validate_test_target(metadata: dict[str, Any], spreadsheet_id: str) -> dict[str, Any]:
    if spreadsheet_id != PINNED_TEST_SPREADSHEET_ID:
        raise RuntimeError("rollback source is not the pinned isolated TEST spreadsheet")
    if metadata.get("spreadsheetId") != PINNED_TEST_SPREADSHEET_ID:
        raise RuntimeError("Google Sheets returned an unexpected TEST spreadsheet ID")
    if metadata.get("properties", {}).get("title") != TEST_TITLE:
        raise RuntimeError("isolated TEST spreadsheet title mismatch")
    sheets = {
        item.get("properties", {}).get("title"): item.get("properties", {})
        for item in metadata.get("sheets", [])
    }
    expected = {"projects_current": (58, 64), "Статусы проектов": (25, 31), "project_types": (3, 3)}
    for title, (before, _after) in expected.items():
        if title not in sheets or sheets[title].get("gridProperties", {}).get("columnCount") != before:
            raise RuntimeError(f"TEST prepublication schema width mismatch: {title}")
    return {title: sheets[title] for title in expected}


def _write_verified_marker(directory: Path, marker: Path) -> None:
    directory = directory.resolve()
    marker = marker.resolve()
    if not marker.is_relative_to(directory):
        raise RuntimeError("backup upload marker must be inside the private backup directory")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    descriptor = os.open(marker, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write("PASS\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(marker, 0o600)
    if stat.S_IMODE(marker.stat().st_mode) != 0o600 or marker.read_text(encoding="utf-8").strip() != "PASS":
        raise RuntimeError("private Drive backup marker did not persist safely")


def main() -> int:
    target_id = os.environ.get("GOOGLE_SPREADSHEET_ID", "").strip()
    pinned_id = os.environ.get("TEST_SPREADSHEET_ID", "").strip()
    folder_id = os.environ.get("TEST_BACKUP_FOLDER_ID", "").strip()
    if target_id != PINNED_TEST_SPREADSHEET_ID or pinned_id != PINNED_TEST_SPREADSHEET_ID:
        raise RuntimeError("TEST backup target does not match the pinned isolated workbook")
    if not folder_id:
        raise RuntimeError("TEST rollback Drive folder is not configured")

    token = google_token(scopes=[SHEETS_SCOPE, DRIVE_SCOPE])
    source_meta = _validate_test_target(_spreadsheet_metadata(token, target_id), target_id)

    run_id = os.environ.get("GITHUB_RUN_ID", "local-test")
    backup_name = f"J4B Successful Visits TEST rollback {run_id}"
    encoded_source_id = urllib.parse.quote(target_id, safe="")
    copy_url = (
        f"https://www.googleapis.com/drive/v3/files/{encoded_source_id}/copy?"
        + urllib.parse.urlencode({"fields": "id,name,mimeType,parents"})
    )
    copied = _request(
        copy_url,
        token,
        method="POST",
        body={"name": backup_name, "mimeType": BACKUP_MIME_TYPE, "parents": [folder_id]},
    )
    backup_id = str(copied.get("id", ""))
    if not backup_id or backup_id == target_id:
        raise RuntimeError("Drive did not create a distinct TEST rollback spreadsheet")
    if copied.get("mimeType") != BACKUP_MIME_TYPE or folder_id not in copied.get("parents", []):
        raise RuntimeError("TEST rollback copy was not created in the private rollback folder")

    backup_meta = _spreadsheet_metadata(token, backup_id)
    backup_sheets = {
        item.get("properties", {}).get("title"): item.get("properties", {})
        for item in backup_meta.get("sheets", [])
    }
    for title, source_properties in source_meta.items():
        backup_properties = backup_sheets.get(title)
        if not backup_properties:
            raise RuntimeError(f"rollback copy is missing affected tab: {title}")
        source_grid = source_properties.get("gridProperties", {})
        backup_grid = backup_properties.get("gridProperties", {})
        if (source_grid.get("columnCount"), source_grid.get("rowCount")) != (
            backup_grid.get("columnCount"), backup_grid.get("rowCount")
        ):
            raise RuntimeError(f"rollback copy grid metadata mismatch: {title}")
        columns = source_grid["columnCount"]
        source_count, source_hash = _fingerprint_tab(
            token, target_id, title, source_grid["rowCount"], columns
        )
        backup_count, backup_hash = _fingerprint_tab(
            token, backup_id, title, backup_grid["rowCount"], columns
        )
        if (source_count, source_hash) != (backup_count, backup_hash):
            raise RuntimeError(f"rollback copy value fingerprint mismatch: {title}")

    backup_dir = Path(os.environ["J4B_PERSISTENT_BACKUP_DIR"])
    marker = Path(os.environ["J4B_PERSISTENT_BACKUP_UPLOAD_MARKER"])
    _write_verified_marker(backup_dir, marker)
    github_output = os.environ.get("GITHUB_OUTPUT", "").strip()
    if github_output:
        with open(github_output, "a", encoding="utf-8") as handle:
            handle.write(f"backup_spreadsheet_id={backup_id}\n")
    print(json.dumps({
        "TEST_PRIVATE_DRIVE_BACKUP": "PASS",
        "backup_spreadsheet_id": backup_id,
        "backup_folder_id": folder_id,
        "tabs_verified": sorted(source_meta),
        "rollback_marker": "PASS",
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({
            "TEST_PRIVATE_DRIVE_BACKUP": "FAIL",
            "ERROR_TYPE": type(exc).__name__,
            "ERROR": str(exc),
        }, ensure_ascii=False, sort_keys=True))
        raise SystemExit(1)
