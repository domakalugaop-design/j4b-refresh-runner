"""Bounded, resumable report-corrector acquisition primitives.

This module is deliberately not wired into the regular refresh. A caller must
provide an independently validated report inventory (with distinct report_id
and action_id) and an explicit per-run GET cap. Checkpoints contain employee
names and therefore must be stored privately outside the repository.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .report_reviewer import ReportCorrectorParseError, parse_report_corrector

START_DATE = date(2026, 10, 1)
SHEET_NAME = "report_reviewers"
SHEET_COLUMNS = (
    "report_id", "action_id", "visit_id", "project_id", "report_date",
    "corrector_id", "corrector_fio", "source_status", "last_successful_fetch",
)
FETCH_FAILURES = {"TRANSPORT_ERROR", "HTTP_ERROR", "PARSE_ERROR", "INTERNAL_ERROR"}
SOURCE_STATUSES = {"PENDING", "CURRENT", "NO_CORRECTOR", "STALE_LAST_GOOD", "FETCH_FAILED_NO_HISTORY"}


@dataclass(frozen=True)
class ReportInventoryItem:
    report_id: str
    action_id: str
    visit_id: str
    project_id: str
    report_date: str

    def validated(self) -> "ReportInventoryItem":
        for field_name in ("report_id", "action_id", "visit_id", "project_id"):
            value = str(getattr(self, field_name))
            if not value.isdigit() or int(value) <= 0:
                raise ValueError(f"invalid {field_name}")
        try:
            parsed_date = date.fromisoformat(self.report_date)
        except (TypeError, ValueError):
            raise ValueError("invalid report_date") from None
        if parsed_date < START_DATE:
            raise ValueError("report is before configured collection start")
        return ReportInventoryItem(
            str(self.report_id), str(self.action_id), str(self.visit_id),
            str(self.project_id), parsed_date.isoformat(),
        )


class ReportFetchFailure(Exception):
    """Safe fetch failure; category must be one of the fixed codes."""

    def __init__(self, category: str):
        if category not in FETCH_FAILURES:
            category = "INTERNAL_ERROR"
        self.category = category
        super().__init__(category)


def new_checkpoint(items: Iterable[ReportInventoryItem]) -> dict[str, Any]:
    reports: dict[str, Any] = {}
    for raw_item in items:
        item = raw_item.validated()
        rid = item.report_id
        if rid in reports:
            if reports[rid]["inventory"] != asdict(item):
                raise ValueError("conflicting report inventory identity")
            continue
        reports[rid] = {
            "inventory": asdict(item),
            "last_good": None,
            "last_attempt_at": None,
            "last_attempt_status": "PENDING",
            "failure_count": 0,
            "next_attempt_at": None,
            "next_recheck_at": None,
        }
    return {"version": 1, "start_date": START_DATE.isoformat(), "reports": reports}


def merge_inventory(checkpoint: dict[str, Any], items: Iterable[ReportInventoryItem]) -> int:
    if checkpoint.get("version") != 1 or checkpoint.get("start_date") != START_DATE.isoformat():
        raise ValueError("unsupported report-corrector checkpoint")
    added = 0
    for raw_item in items:
        item = raw_item.validated()
        rid = item.report_id
        current = checkpoint["reports"].get(rid)
        if current is None:
            checkpoint["reports"][rid] = {
                "inventory": asdict(item), "last_good": None,
                "last_attempt_at": None, "last_attempt_status": "PENDING",
                "failure_count": 0, "next_attempt_at": None, "next_recheck_at": None,
            }
            added += 1
        elif current["inventory"] != asdict(item):
            raise ValueError("report identity mapping changed")
    return added


def due_report_ids(checkpoint: Mapping[str, Any], now: datetime, max_gets: int) -> list[str]:
    if max_gets <= 0:
        raise ValueError("max_gets must be positive")
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    now_text = now.astimezone(timezone.utc).isoformat()
    pending: list[str] = []
    due: list[tuple[str, str]] = []
    for rid, entry in checkpoint.get("reports", {}).items():
        next_attempt = entry.get("next_attempt_at")
        if next_attempt and next_attempt > now_text:
            continue
        if entry.get("last_good") is None:
            pending.append(rid)
        else:
            recheck = entry.get("next_recheck_at")
            if recheck is not None and recheck <= now_text:
                due.append((recheck, rid))
    return sorted(pending, key=int)[:max_gets] + [rid for _, rid in sorted(due)[:max(0, max_gets-len(pending[:max_gets]) )]]


def record_success(
    checkpoint: dict[str, Any], report_id: str, parsed: Any, fetched_at: datetime,
    *, recheck_after: timedelta,
) -> None:
    if report_id not in checkpoint.get("reports", {}):
        raise ValueError("report_id is outside inventory")
    if fetched_at.tzinfo is None or recheck_after.total_seconds() <= 0:
        raise ValueError("invalid fetch time or recheck interval")
    if parsed.state not in {"VALUE_PRESENT", "EMPTY"}:
        raise ValueError("report corrector field is not safely resolved")
    timestamp = fetched_at.astimezone(timezone.utc)
    source_status = "CURRENT" if parsed.state == "VALUE_PRESENT" else "NO_CORRECTOR"
    checkpoint["reports"][report_id].update({
        "last_good": {
            "corrector_id": parsed.corrector_id,
            "corrector_fio": parsed.corrector_fio,
            "source_status": source_status,
            "last_successful_fetch": timestamp.isoformat(),
        },
        "last_attempt_at": timestamp.isoformat(),
        "last_attempt_status": "PASS",
        "failure_count": 0,
        "next_attempt_at": None,
        "next_recheck_at": (timestamp + recheck_after).isoformat(),
    })


def record_failure(checkpoint: dict[str, Any], report_id: str, category: str, attempted_at: datetime) -> None:
    if report_id not in checkpoint.get("reports", {}):
        raise ValueError("report_id is outside inventory")
    if category not in FETCH_FAILURES or attempted_at.tzinfo is None:
        raise ValueError("invalid safe failure category or timestamp")
    entry = checkpoint["reports"][report_id]
    failures = int(entry.get("failure_count", 0)) + 1
    # Bounded exponential retry delay: 5 minutes, capped at 24 hours.
    delay = min(300 * (2 ** min(failures - 1, 8)), 86_400)
    attempt = attempted_at.astimezone(timezone.utc)
    entry.update({
        "last_attempt_at": attempt.isoformat(),
        "last_attempt_status": category,
        "failure_count": failures,
        "next_attempt_at": (attempt + timedelta(seconds=delay)).isoformat(),
    })


def acquire_batch(
    checkpoint: dict[str, Any], fetch_html: Callable[[str], str], now: datetime,
    *, max_gets: int, recheck_after: timedelta, checkpoint_path: str | os.PathLike[str],
    request_interval_seconds: float = 1.0, sleep: Callable[[float], None] = time.sleep,
) -> dict[str, int]:
    if request_interval_seconds <= 0:
        raise ValueError("request interval must be positive")
    selected = due_report_ids(checkpoint, now, max_gets)
    # Persist scope and pending work before any GET, then each terminal result
    # independently so interruption never discards completed items.
    save_checkpoint(checkpoint_path, checkpoint)
    counts = {"attempted": 0, "passed": 0, "failed": 0}
    for rid in selected:
        if counts["attempted"]:
            sleep(request_interval_seconds)
        counts["attempted"] += 1
        try:
            html = fetch_html(checkpoint["reports"][rid]["inventory"]["action_id"])
            parsed = parse_report_corrector(html)
            if parsed.state == "FIELD_NOT_EXPOSED":
                raise ReportCorrectorParseError("corrector field not exposed")
            record_success(checkpoint, rid, parsed, now, recheck_after=recheck_after)
            counts["passed"] += 1
        except ReportCorrectorParseError:
            record_failure(checkpoint, rid, "PARSE_ERROR", now)
            counts["failed"] += 1
        except ReportFetchFailure as exc:
            record_failure(checkpoint, rid, exc.category, now)
            counts["failed"] += 1
        except Exception:
            # Never retain or print arbitrary transport/API exception text.
            record_failure(checkpoint, rid, "INTERNAL_ERROR", now)
            counts["failed"] += 1
        save_checkpoint(checkpoint_path, checkpoint)
    return counts


def render_sheet_rows(checkpoint: Mapping[str, Any]) -> list[list[Any]]:
    rows: list[list[Any]] = [list(SHEET_COLUMNS)]
    for rid, entry in sorted(checkpoint.get("reports", {}).items(), key=lambda pair: int(pair[0])):
        inventory = entry["inventory"]
        good = entry.get("last_good")
        if good:
            source_status = good["source_status"] if entry["last_attempt_status"] == "PASS" else "STALE_LAST_GOOD"
            corrector_id, corrector_fio = good["corrector_id"], good["corrector_fio"]
            last_successful_fetch = good["last_successful_fetch"]
        else:
            source_status = "FETCH_FAILED_NO_HISTORY" if entry["last_attempt_status"] not in {"PENDING", "PASS"} else "PENDING"
            corrector_id = corrector_fio = last_successful_fetch = ""
        rows.append([
            inventory["report_id"], inventory["action_id"], inventory["visit_id"],
            inventory["project_id"], inventory["report_date"], corrector_id,
            corrector_fio, source_status, last_successful_fetch,
        ])
    return rows


def save_checkpoint(path: str | os.PathLike[str], checkpoint: Mapping[str, Any]) -> str:
    target = Path(path).expanduser().resolve()
    repo_root = Path(__file__).resolve().parents[1]
    if target.is_relative_to(repo_root):
        raise ValueError("corrector checkpoint must be outside repository")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if target.parent.stat().st_mode & 0o077:
        raise ValueError("corrector checkpoint directory permissions must be 0700 or stricter")
    payload = json.dumps(checkpoint, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    envelope = {"payload": json.loads(payload), "sha256": hashlib.sha256(payload).hexdigest()}
    encoded = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    fd, temp_name = tempfile.mkstemp(prefix=".corrector-checkpoint-", dir=target.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
        os.chmod(target, 0o600)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return hashlib.sha256(encoded).hexdigest()


def load_checkpoint(path: str | os.PathLike[str], expected_sha256: str | None = None) -> dict[str, Any]:
    target = Path(path).expanduser().resolve()
    if target.stat().st_mode & 0o077:
        raise ValueError("corrector checkpoint permissions must be 0600 or stricter")
    encoded = target.read_bytes()
    digest = hashlib.sha256(encoded).hexdigest()
    if expected_sha256 and not hmac.compare_digest(digest, expected_sha256):
        raise ValueError("corrector checkpoint SHA-256 mismatch")
    envelope = json.loads(encoded)
    payload = envelope.get("payload")
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if not hmac.compare_digest(envelope.get("sha256", ""), hashlib.sha256(canonical).hexdigest()):
        raise ValueError("corrector checkpoint integrity mismatch")
    if not isinstance(payload, dict) or payload.get("version") != 1 or not isinstance(payload.get("reports"), dict):
        raise ValueError("invalid corrector checkpoint")
    return payload


class CorrectorSheetPublicationError(RuntimeError):
    def __init__(self, rollback_status: str):
        self.rollback_status = rollback_status
        super().__init__(f"report_reviewers publication failed; rollback={rollback_status}")


def publish_new_sheet(
    rows: list[list[Any]], *, existing_titles: set[str], read_sheet: Callable[[], list[list[Any]] | None],
    create_sheet: Callable[[], None], write_sheet: Callable[[list[list[Any]]], None],
    delete_sheet: Callable[[], None], save_backup: Callable[[dict[str, Any]], None],
    readback: Callable[[], list[list[Any]] | None],
    non_target_fingerprints: Callable[[], Mapping[str, str]],
) -> None:
    """Publish only the new tab; require durable snapshot and verify rollback.

    All callbacks are injected so this operation is testable without Sheets.
    Callers must not invoke it until request volume and source inventory are
    approved. Existing five tabs are protected by before/after fingerprints.
    """
    _validate_sheet_rows(rows)
    if SHEET_NAME in existing_titles:
        previous = read_sheet()
        if previous is None or not previous or previous[0] != list(SHEET_COLUMNS):
            raise ValueError("existing report_reviewers tab is not safely restorable")
        existed = True
    else:
        previous = None
        existed = False
    before = dict(non_target_fingerprints())
    try:
        backup_verified = save_backup({"tab": SHEET_NAME, "existed": existed, "rows": previous})
    except Exception:
        raise RuntimeError("report_reviewers durable backup failed") from None
    if backup_verified is not True:
        raise RuntimeError("report_reviewers durable backup was not verified")
    created = False
    try:
        if not existed:
            created = True
            create_sheet()
        write_sheet(rows)
        if readback() != rows:
            raise RuntimeError("readback mismatch")
        if dict(non_target_fingerprints()) != before:
            raise RuntimeError("non-target tab fingerprint changed")
    except Exception:
        try:
            if existed:
                write_sheet(previous or [])
                if readback() != previous:
                    raise RuntimeError
            elif created:
                delete_sheet()
                if readback() is not None:
                    raise RuntimeError
            if dict(non_target_fingerprints()) != before:
                raise RuntimeError
        except Exception:
            raise CorrectorSheetPublicationError("FAIL") from None
        raise CorrectorSheetPublicationError("PASS") from None


def _validate_sheet_rows(rows: list[list[Any]]) -> None:
    if not rows or rows[0] != list(SHEET_COLUMNS):
        raise ValueError("report_reviewers schema mismatch")
    seen: set[str] = set()
    for row in rows[1:]:
        if len(row) != len(SHEET_COLUMNS):
            raise ValueError("report_reviewers row width mismatch")
        report_id = str(row[0])
        if not report_id.isdigit() or int(report_id) <= 0 or report_id in seen:
            raise ValueError("report_reviewers report_id is invalid or duplicated")
        seen.add(report_id)
        if row[8]:
            try:
                datetime.fromisoformat(str(row[8]))
            except ValueError:
                raise ValueError("report_reviewers fetch timestamp is invalid") from None
        if row[7] not in SOURCE_STATUSES:
            raise ValueError("report_reviewers source_status is invalid")
