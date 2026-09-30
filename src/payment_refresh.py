"""Private resumable payment acquisition state and current-snapshot merges.

This module has no network or Sheets side effects. Callers own authentication,
request scheduling, and publication authorization. Checkpoint files contain
normalized business rows and must remain private, outside the repository.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
import copy
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.error import HTTPError, URLError

from .payment_materialization import serialize_sheet_payload

PAYMENT_STATES = {
    "PENDING", "SUCCESS_WITH_ROWS", "SUCCESS_ZERO_ROWS",
    "FAILED_TRANSPORT", "FAILED_PARSE", "FAILED_SEMANTIC",
}
SUCCESS_STATES = {"SUCCESS_WITH_ROWS", "SUCCESS_ZERO_ROWS"}
PAYMENT_WRITE_CHUNK_MAX_BYTES = 1_500_000
PAYMENT_WRITE_MAX_ATTEMPTS = 3


def normalize_payment_sheet_values(tab: str, rows: list[list[Any]]) -> list[list[Any]]:
    """Normalize Sheets API numeric values to the publication's exact types."""
    money_indices = {"Выплаты по визитам": {6, 7}, "Выплаты по проектам": {6, 7}}
    count_indices = {
        "Выплаты по визитам": {5, 8, 9},
        "Выплаты по проектам": {4, 5, 8, 9, 10, 11, 12, 13},
    }
    if tab not in money_indices or not rows:
        return [list(row) for row in rows]
    result = [list(row) for row in rows]
    id_indices = {"Выплаты по визитам": {0, 4}, "Выплаты по проектам": {0}}[tab]
    for row in result[1:]:
        for index in id_indices:
            if index < len(row) and row[index] not in (None, ""):
                row[index] = str(row[index])
        for index in money_indices[tab]:
            if index < len(row) and isinstance(row[index], float):
                row[index] = Decimal(str(row[index]))
        for index in count_indices[tab]:
            if index < len(row) and isinstance(row[index], float):
                if not row[index].is_integer():
                    raise ValueError(f"non-integral count in payment tab {tab}")
                row[index] = int(row[index])
    return result


def _encode(value: Any) -> Any:
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite Decimal in payment checkpoint")
        return {"__decimal__": format(value, "f")}
    if isinstance(value, dict):
        return {str(key): _encode(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode(item) for item in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(f"unsupported payment checkpoint value: {type(value).__name__}")


def _decode(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value) == {"__decimal__"}:
            return Decimal(value["__decimal__"])
        return {key: _decode(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode(item) for item in value]
    return value


def new_checkpoint(project_ids: Iterable[str | int], *, mode: str, year: int | None = None) -> dict[str, Any]:
    ids = [str(pid) for pid in project_ids]
    if any(not pid.isdigit() or int(pid) <= 0 for pid in ids):
        raise ValueError("payment scope contains invalid project_id")
    if len(ids) != len(set(ids)):
        raise ValueError("payment scope contains duplicate project_id")
    return {
        "version": 1,
        "mode": mode,
        "year": year,
        "scope": ids,
        "projects": {pid: {"status": "PENDING", "rows": []} for pid in ids},
    }


def record_result(checkpoint: dict[str, Any], project_id: str | int, status: str,
                  rows: Iterable[Mapping[str, Any]] = (),
                  context: Mapping[str, Any] | None = None) -> None:
    pid = str(project_id)
    if status not in PAYMENT_STATES - {"PENDING"}:
        raise ValueError("invalid terminal payment acquisition status")
    if pid not in checkpoint.get("projects", {}):
        raise ValueError("project_id is outside payment checkpoint scope")
    current = checkpoint["projects"][pid]
    if current.get("status") in SUCCESS_STATES:
        raise ValueError("accepted successful payment result is immutable on resume")
    materialized_rows = [_encode(dict(row)) for row in rows]
    if status == "SUCCESS_WITH_ROWS" and not materialized_rows:
        raise ValueError("SUCCESS_WITH_ROWS requires at least one parsed row")
    if status == "SUCCESS_ZERO_ROWS" and materialized_rows:
        raise ValueError("SUCCESS_ZERO_ROWS cannot contain rows")
    if status not in SUCCESS_STATES and materialized_rows:
        raise ValueError("failed acquisition cannot persist parsed rows")
    checkpoint["projects"][pid] = {
        "status": status,
        "rows": materialized_rows,
        "context": _encode(dict(context or {})),
    }


def checkpoint_counts(checkpoint: Mapping[str, Any]) -> dict[str, int]:
    counts = {status: 0 for status in PAYMENT_STATES}
    for result in checkpoint.get("projects", {}).values():
        status = result.get("status")
        if status not in counts:
            raise ValueError("checkpoint contains unknown payment status")
        counts[status] += 1
    return counts


def save_checkpoint(path: str | os.PathLike[str], checkpoint: Mapping[str, Any]) -> dict[str, Any]:
    target = Path(path).expanduser().absolute()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(target.parent, 0o700)
    body = _encode(dict(checkpoint))
    body.pop("integrity_sha256", None)
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    body["integrity_sha256"] = hashlib.sha256(canonical).hexdigest()
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=".payment-checkpoint-", dir=target.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        os.chmod(target, 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return {"path": str(target), "sha256": hashlib.sha256(encoded).hexdigest(), "bytes": len(encoded)}


def load_checkpoint(path: str | os.PathLike[str], expected_sha256: str | None = None) -> dict[str, Any]:
    target = Path(path).expanduser().absolute()
    if target.stat().st_mode & 0o077:
        raise ValueError("payment checkpoint permissions must be 0600 or stricter")
    payload = target.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    if expected_sha256 and digest.lower() != expected_sha256.lower():
        raise ValueError("payment checkpoint SHA-256 mismatch")
    result = _decode(json.loads(payload))
    if not isinstance(result, dict) or result.get("version") != 1:
        raise ValueError("unsupported payment checkpoint")
    claimed_integrity = result.pop("integrity_sha256", None)
    canonical = json.dumps(_encode(result), ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")
    if not isinstance(claimed_integrity, str) or not hmac.compare_digest(
        claimed_integrity, hashlib.sha256(canonical).hexdigest()
    ):
        raise ValueError("payment checkpoint internal integrity mismatch")
    scope = result.get("scope", [])
    projects = result.get("projects", {})
    if len(scope) != len(set(scope)) or set(scope) != set(projects):
        raise ValueError("payment checkpoint scope/index mismatch")
    checkpoint_counts(result)
    return result


def pending_project_ids(checkpoint: Mapping[str, Any], *, retry_failures: bool = True) -> list[str]:
    retryable = {"PENDING", "FAILED_TRANSPORT", "FAILED_PARSE", "FAILED_SEMANTIC"} if retry_failures else {"PENDING"}
    return [pid for pid in checkpoint["scope"] if checkpoint["projects"][pid]["status"] in retryable]


def deterministic_batches(project_ids: Iterable[str | int], batch_size: int) -> list[list[str]]:
    ids = [str(pid) for pid in project_ids]
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate project_id in batch scope")
    return [ids[index:index + batch_size] for index in range(0, len(ids), batch_size)]


def require_complete(checkpoint: Mapping[str, Any], expected_total: int | None = None) -> None:
    if expected_total is not None and len(checkpoint.get("scope", [])) != expected_total:
        raise ValueError("payment checkpoint scope count does not match expected total")
    counts = checkpoint_counts(checkpoint)
    if counts["PENDING"] or any(counts[state] for state in PAYMENT_STATES - SUCCESS_STATES - {"PENDING"}):
        raise ValueError("payment acquisition is incomplete; publication prohibited")


def replace_by_project(previous: Mapping[str, list[list[Any]]], incoming: Mapping[str, list[list[Any]]],
                       selected_project_ids: Iterable[str | int]) -> dict[str, list[list[Any]]]:
    """Replace selected project rows on each tab; successful zero-row projects delete stale rows."""
    selected = {str(pid) for pid in selected_project_ids}
    if set(previous) != set(incoming):
        raise ValueError("payment tab set mismatch")
    merged: dict[str, list[list[Any]]] = {}
    for tab, old in previous.items():
        new = incoming[tab]
        if not old or not new or old[0] != new[0]:
            raise ValueError(f"payment schema mismatch in {tab}")
        old_rows, new_rows = old[1:], new[1:]
        # Incoming must contain rows only for selected projects.
        if any(not row or str(row[0]) not in selected for row in new_rows):
            raise ValueError("incoming payment row is outside selected scope")
        kept = [row for row in old_rows if not row or str(row[0]) not in selected]
        merged[tab] = [list(old[0]), *kept, *[list(row) for row in new_rows]]
    return merged


def atomic_two_tab_update_requests(
    sheet_ids: Mapping[str, int],
    previous: Mapping[str, list[list[Any]]],
    candidate: Mapping[str, list[list[Any]]],
    *,
    strict_headers: bool = True,
) -> list[dict[str, Any]]:
    """Build a single spreadsheets.batchUpdate request list for both tabs.

    The caller must submit the complete list in one API call. Each tab range is
    padded to the previous/candidate dimensions so stale tails are cleared.
    Decimal is retained as an exact JSON number for the caller's Decimal-aware
    JSON serializer.
    """
    if set(sheet_ids) != set(previous) or set(previous) != set(candidate):
        raise ValueError("payment sheet ID/tab set mismatch")
    requests: list[dict[str, Any]] = []
    for tab in previous:
        old, new = previous[tab], candidate[tab]
        if not old or not new or (strict_headers and old[0] != new[0]):
            raise ValueError(f"payment schema mismatch in {tab}")
        width = len(new[0])
        if any(len(row) != width for row in new):
            raise ValueError(f"candidate row width mismatch in {tab}")
        height = max(len(old), len(new))
        padded = [list(row) + [None] * (width - len(row)) for row in new]
        padded.extend([[None] * width for _ in range(height - len(padded))])
        rows = []
        for row in padded:
            values = []
            for value in row:
                if value is None:
                    values.append({})
                elif isinstance(value, bool):
                    values.append({"userEnteredValue": {"boolValue": value}})
                elif isinstance(value, (int, Decimal)):
                    if isinstance(value, Decimal) and not value.is_finite():
                        raise ValueError("non-finite Decimal in candidate")
                    values.append({"userEnteredValue": {"numberValue": value}})
                elif isinstance(value, str):
                    values.append({"userEnteredValue": {"stringValue": value}})
                else:
                    raise TypeError(f"unsupported payment Sheet cell type: {type(value).__name__}")
            rows.append({"values": values})
        requests.append({"updateCells": {
            "range": {"sheetId": sheet_ids[tab], "startRowIndex": 0,
                      "endRowIndex": height, "startColumnIndex": 0,
                      "endColumnIndex": width},
            "rows": rows,
            "fields": "userEnteredValue",
        }})
    return requests


def _normalized_readback(rows: list[list[Any]]) -> list[list[Any]]:
    """Normalize Google values API's omitted trailing blank cells."""
    result = [[Decimal(str(value)) if isinstance(value, float) else value for value in row] for row in rows]
    while result and (not result[-1] or all(value in (None, "") for value in result[-1])):
        result.pop()
    return [row[:next((i + 1 for i in range(len(row) - 1, -1, -1) if row[i] not in (None, "")), 0)] for row in result]


def _payment_write_chunks(requests: list[dict[str, Any]], max_bytes: int) -> list[dict[str, Any]]:
    """Split updateCells requests into request bodies bounded by serialized bytes."""
    if max_bytes <= 0:
        raise ValueError("payment write chunk byte limit must be positive")
    chunks: list[dict[str, Any]] = []
    for request in requests:
        update = request.get("updateCells")
        if not isinstance(update, dict) or not isinstance(update.get("rows"), list):
            if len(serialize_sheet_payload({"requests": [request]}).encode("utf-8")) > max_bytes:
                raise ValueError("non-updateCells payment request exceeds chunk byte limit")
            chunks.append(request)
            continue
        all_rows = update["rows"]
        base_range = update.get("range", {})
        if base_range.get("startColumnIndex", 0) != 0:
            raise ValueError("payment chunk writer requires full-row ranges starting at column A")
        base_start = int(base_range.get("startRowIndex", 0))
        cursor = 0
        while cursor < len(all_rows):
            start = base_start + cursor
            selected: list[dict[str, Any]] = []
            encoded_rows_size = 0
            while cursor + len(selected) < len(all_rows):
                row = all_rows[cursor + len(selected)]
                row_size = len(serialize_sheet_payload({"values": row.get("values", [])}).encode("utf-8"))
                count = len(selected) + 1
                chunk_request = {key: value for key, value in request.items() if key != "updateCells"}
                chunk_request["updateCells"] = {
                    **update,
                    "range": {**base_range, "startRowIndex": start, "endRowIndex": start + count},
                    "rows": [],
                }
                base_size = len(serialize_sheet_payload({"requests": [chunk_request]}).encode("utf-8")) - 2
                estimated_size = base_size + encoded_rows_size + row_size + len(selected)
                if estimated_size > max_bytes:
                    if not selected:
                        raise ValueError("single payment row exceeds chunk byte limit")
                    break
                selected.append(row)
                encoded_rows_size += row_size
            result = {key: value for key, value in request.items() if key != "updateCells"}
            result["updateCells"] = {
                **update,
                "range": {**base_range, "startRowIndex": start, "endRowIndex": start + len(selected)},
                "rows": selected,
            }
            actual_size = len(serialize_sheet_payload({"requests": [result]}).encode("utf-8"))
            if actual_size > max_bytes:
                raise AssertionError("payment chunk byte estimator exceeded configured limit")
            chunks.append(result)
            cursor += len(selected)
    return chunks


def _chunk_values_match(actual: list[list[Any]], request: Mapping[str, Any]) -> bool:
    update = request["updateCells"]
    cell_range = update["range"]
    start = int(cell_range["startRowIndex"])
    end = int(cell_range["endRowIndex"])
    width = int(cell_range["endColumnIndex"])
    expected_rows: list[list[Any]] = []
    for row in update["rows"]:
        values = []
        for cell in row.get("values", []):
            entered = cell.get("userEnteredValue", {})
            if "numberValue" in entered:
                value = entered["numberValue"]
            elif "stringValue" in entered:
                value = entered["stringValue"]
            elif "boolValue" in entered:
                value = entered["boolValue"]
            else:
                value = None
            values.append(value)
        expected_rows.append(values + [None] * max(0, width - len(values)))
    actual_rows = []
    for index in range(start, end):
        row = actual[index] if index < len(actual) else []
        actual_rows.append(list(row[:width]) + [None] * max(0, width - len(row)))
    return _normalized_readback(actual_rows) == _normalized_readback(expected_rows)


def _retryable_payment_write_error(error: Exception) -> bool:
    if isinstance(error, HTTPError):
        return error.code in {408, 429, 500, 502, 503, 504}
    return isinstance(error, (URLError, TimeoutError, ConnectionError, OSError))


def _payment_tab_label(tab: str) -> str:
    normalized = tab.casefold()
    return "VISIT" if "visit" in normalized or "визит" in normalized else "PROJECT"


def publish_payment_pair(
    *,
    sheet_ids: Mapping[str, int],
    previous: Mapping[str, list[list[Any]]],
    candidate: Mapping[str, list[list[Any]]],
    write_batch: Any,
    read_tab: Any,
    max_request_bytes: int = PAYMENT_WRITE_CHUNK_MAX_BYTES,
    max_attempts: int = PAYMENT_WRITE_MAX_ATTEMPTS,
    grid_row_counts: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Publish payment tabs in bounded chunks, verify, and restore on mismatch.

    Physical writes span multiple API calls; they are accepted only after the
    complete pair readback matches. Any failed chunk restores both tabs.
    """
    if max_attempts <= 0:
        raise ValueError("payment write max_attempts must be positive")
    if set(sheet_ids) != set(previous) or set(previous) != set(candidate):
        raise ValueError("payment publication tab set mismatch")
    saved = copy.deepcopy(dict(previous))
    candidate_copy = copy.deepcopy(dict(candidate))
    requests = atomic_two_tab_update_requests(sheet_ids, saved, candidate_copy)

    def read_pair() -> dict[str, list[list[Any]]]:
        return {tab: read_tab(tab) for tab in sheet_ids}

    def matches(actual: Mapping[str, list[list[Any]]], expected: Mapping[str, list[list[Any]]]) -> bool:
        return all(_normalized_readback(actual[tab]) == _normalized_readback(expected[tab]) for tab in sheet_ids)

    chunks_written = 0
    ambiguous_write_response = False
    current_grid_rows = dict(grid_row_counts or {})

    def restore_grid_rows() -> None:
        """Remove only row capacity added by this publication after data rollback."""
        requests = []
        for tab, baseline in (grid_row_counts or {}).items():
            current = int(current_grid_rows.get(tab, baseline))
            if current > baseline:
                requests.append({"deleteDimension": {"range": {
                    "sheetId": sheet_ids[tab], "dimension": "ROWS",
                    "startIndex": baseline, "endIndex": current,
                }}})
        if requests:
            write_batch(requests)
            for tab, baseline in (grid_row_counts or {}).items():
                current_grid_rows[tab] = min(int(current_grid_rows.get(tab, baseline)), baseline)

    def write_chunked(batch_requests: list[dict[str, Any]]) -> None:
        nonlocal chunks_written, ambiguous_write_response
        chunks = _payment_write_chunks(batch_requests, max_request_bytes)
        tabs_by_sheet_id = {value: name for name, value in sheet_ids.items()}
        totals: dict[str, int] = {}
        for chunk in chunks:
            update = chunk.get("updateCells")
            tab = tabs_by_sheet_id.get(update["range"].get("sheetId"), next(iter(sheet_ids))) if update else next(iter(sheet_ids))
            totals[tab] = totals.get(tab, 0) + 1
        completed: dict[str, int] = {}
        for chunk in chunks:
            update = chunk.get("updateCells")
            if not update:
                write_batch([chunk])
                chunks_written += 1
                continue
            cell_range = update["range"]
            tab = tabs_by_sheet_id.get(cell_range.get("sheetId"), next(iter(sheet_ids)))
            start = int(cell_range["startRowIndex"])
            end = int(cell_range["endRowIndex"])
            sheet_id = cell_range["sheetId"]
            label = _payment_tab_label(tab)
            chunk_number = completed.get(tab, 0) + 1
            completed[tab] = chunk_number
            for attempt in range(1, max_attempts + 1):
                append_count = max(0, end - int(current_grid_rows.get(tab, end)))
                batch = ([{"appendDimension": {
                    "sheetId": sheet_id, "dimension": "ROWS", "length": append_count,
                }}] if append_count else []) + [chunk]
                try:
                    write_batch(batch)
                except Exception as write_error:
                    try:
                        current = read_tab(tab)
                    except Exception as verify_error:
                        raise RuntimeError("ambiguous payment chunk write; range readback failed") from verify_error
                    if _chunk_values_match(current, chunk):
                        if append_count:
                            current_grid_rows[tab] = int(current_grid_rows.get(tab, 0)) + append_count
                        ambiguous_write_response = True
                        print(f"PAYMENT_{label}_WRITE_CHUNK={chunk_number}/{totals[tab]} ROWS={start + 1}-{end} STATUS=PASS AMBIGUOUS_RESPONSE=CONFIRMED", flush=True)
                        chunks_written += 1
                        break
                    if not _retryable_payment_write_error(write_error) or attempt >= max_attempts:
                        raise
                    print(f"PAYMENT_{label}_WRITE_CHUNK={chunk_number}/{totals[tab]} ROWS={start + 1}-{end} STATUS=RETRY ATTEMPT={attempt + 1}", flush=True)
                    time.sleep(min(1.0 * (2 ** (attempt - 1)), 4.0))
                    continue
                if append_count:
                    current_grid_rows[tab] = int(current_grid_rows.get(tab, 0)) + append_count
                print(f"PAYMENT_{label}_WRITE_CHUNK={chunk_number}/{totals[tab]} ROWS={start + 1}-{end} STATUS=PASS", flush=True)
                chunks_written += 1
                break

    try:
        write_chunked(requests)
    except Exception as write_error:
        try:
            current = read_pair()
            if matches(current, saved):
                raise write_error
            if matches(current, candidate_copy):
                return {"status": "PASS", "ambiguous_write_response": True,
                        "requests": len(requests), "chunks_written": chunks_written}
            write_chunked(atomic_two_tab_update_requests(sheet_ids, current, saved, strict_headers=False))
            restore_grid_rows()
            restored = read_pair()
            if not matches(restored, saved):
                raise RuntimeError("payment rollback readback mismatch") from write_error
        except Exception as recovery_error:
            if recovery_error is write_error:
                raise
            raise RuntimeError("payment publication failed and rollback could not be verified") from recovery_error
        raise write_error

    try:
        actual = read_pair()
    except Exception as read_error:
        try:
            current = read_pair()
            if not matches(current, saved):
                write_chunked(atomic_two_tab_update_requests(sheet_ids, current, saved, strict_headers=False))
                restore_grid_rows()
                restored = read_pair()
                if not matches(restored, saved):
                    raise RuntimeError("payment rollback readback mismatch")
        except Exception as recovery_error:
            raise RuntimeError("payment readback failed and rollback could not be verified") from recovery_error
        raise read_error
    if matches(actual, candidate_copy):
        return {"status": "PASS", "ambiguous_write_response": ambiguous_write_response,
                "requests": len(requests), "chunks_written": chunks_written}
    try:
        write_chunked(atomic_two_tab_update_requests(sheet_ids, actual, saved, strict_headers=False))
        restore_grid_rows()
        restored = read_pair()
        if not matches(restored, saved):
            raise RuntimeError("payment rollback readback mismatch")
    except Exception as rollback_error:
        raise RuntimeError("payment readback mismatch and rollback failed") from rollback_error
    raise RuntimeError("payment readback mismatch; previous publication restored")
