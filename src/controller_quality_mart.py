"""Development-only controller-quality mart builder and fail-closed publisher.

The input report set is the observed corrector snapshot, not a complete report
inventory. Statuses are joined only on (project_id, action_id). A visit with
multiple report versions is retained as multiple reports and its current
version remains unresolved.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import time
import uuid
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .parsers import parse_action_table
from .workflow_analytics import WORKFLOW_STATES

PUBLICATION_KEY = "controller_quality"
SCHEMA_VERSION = "1"
MAX_STATUS_PROJECTS_PER_RUN = 200
MIN_STATUS_REQUEST_INTERVAL_SECONDS = 1.5

REPORT_COLUMNS = (
    "publication_key", "generation_id", "report_id", "action_id", "visit_id",
    "project_id", "project_name", "corrector_id", "corrector_fio", "report_date",
    "workflow_state_codes", "status_35", "status_38", "status_39", "status_40",
    "status_45", "status_50", "report_url", "source_status", "source_snapshot_at",
    "data_coverage_flag",
)
VISIT_COLUMNS = (
    "publication_key", "generation_id", "project_id", "visit_id", "known_report_ids",
    "report_records_count", "known_corrector_ids", "current_report_id",
    "current_corrector_id", "version_resolution_status", "data_coverage_flag",
)
METRIC_DICTIONARY_COLUMNS = (
    "publication_key", "generation_id", "metric_key", "display_name", "definition",
    "source_fields", "availability", "limitation",
)
METRIC_DICTIONARY = (
    ("observed_reports", "Observed reports/actions", "Distinct report_id values in the observed corrector snapshot", "controller_report_snapshot.report_id", "AVAILABLE", "Partial source population; not a complete Portal report universe"),
    ("projects_by_controller", "Projects by controller", "Distinct project_id values grouped by known corrector", "controller_report_snapshot.project_id,corrector_id", "AVAILABLE", "Observed assignments only; not a complete controller workload denominator"),
    ("workflow_35", "Observed workflow membership 35", "Observed report rows with status_35=OBSERVED_PRESENT", "controller_report_snapshot.status_35", "AVAILABLE", "Current observed membership, not a transition or outcome count"),
    ("workflow_38", "Observed workflow membership 38", "Observed report rows with status_38=OBSERVED_PRESENT", "controller_report_snapshot.status_38", "AVAILABLE", "Current observed membership, not a transition or outcome count"),
    ("workflow_39", "Observed claim membership", "Observed report rows with status_39=OBSERVED_PRESENT", "controller_report_snapshot.status_39", "AVAILABLE", "Code 39 indicates claim membership; it is not a controller error count"),
    ("workflow_40", "Observed workflow membership 40", "Observed report rows with status_40=OBSERVED_PRESENT", "controller_report_snapshot.status_40", "AVAILABLE", "Current observed membership, not a transition or outcome count"),
    ("workflow_45", "Observed workflow membership 45", "Observed report rows with status_45=OBSERVED_PRESENT", "controller_report_snapshot.status_45", "AVAILABLE", "Current observed membership, not a transition or outcome count"),
    ("workflow_50", "Observed workflow membership 50", "Observed report rows with status_50=OBSERVED_PRESENT", "controller_report_snapshot.status_50", "AVAILABLE", "Current observed membership, not a transition or outcome count"),
    ("controller_error_rate", "Controller error rate", "No qualified source outcome or denominator is available", "NOT_AVAILABLE", "NOT_AVAILABLE", "Do not infer controller fault from workflow state 39 or source status"),
    ("claims_count", "Claims count", "No complete claim-level source fact is provided by this mart", "NOT_AVAILABLE", "NOT_AVAILABLE", "State 39 on observed report rows is not a complete claims count"),
    ("review_completion_rate", "Review completion rate", "No qualified review-completion timestamp or denominator is available", "NOT_AVAILABLE", "NOT_AVAILABLE", "Do not interpret collector source_status as review completion"),
    ("current_report_attribution", "Current report/corrector attribution", "Current version selection is unresolved", "controller_visit_summary.current_report_id,current_corrector_id", "NOT_AVAILABLE", "Multiple report versions are retained without selecting a current version"),
)
QUALITY_COLUMNS = (
    "publication_key", "schema_version", "active_generation", "refresh_status",
    "last_successful_refresh_at", "source_snapshot_at", "source_row_count",
    "published_report_count", "published_visit_count", "unmatched_records",
    "ambiguous_versions", "unknown_status_count", "coverage_status",
    "source_freshness", "status_project_count",
)
TAB_SCHEMAS = {
    "controller_report_snapshot": REPORT_COLUMNS,
    "controller_visit_summary": VISIT_COLUMNS,
    "metric_dictionary": METRIC_DICTIONARY_COLUMNS,
    "data_quality_status": QUALITY_COLUMNS,
}
STATUS_CODES = (35, 38, 39, 40, 45, 50)
_ID = re.compile(r"^[0-9]+$")


class MartError(ValueError):
    """Safe controller-mart contract error (contains no source payload)."""


class PublicationError(RuntimeError):
    """Safe publication failure; arbitrary API error bodies are not retained."""


class AmbiguousPublicationError(PublicationError):
    """A write outcome could not be established; never retry blindly."""


def _identifier(value: Any, field: str) -> str:
    text = "" if value is None else str(value).strip()
    if not _ID.fullmatch(text) or int(text) <= 0:
        raise MartError(f"invalid {field}")
    return str(int(text))


def _date(value: Any) -> str:
    text = "" if value is None else str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise MartError("invalid report_date") from None
    return parsed.date().isoformat()


def validate_report_source(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, str]]:
    """Normalize the existing nine-column report_reviewers source contract."""
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    required = {
        "report_id", "action_id", "visit_id", "project_id", "report_date",
        "corrector_id", "corrector_fio", "source_status", "last_successful_fetch",
    }
    for row in rows:
        if not isinstance(row, Mapping) or not required.issubset(row):
            raise MartError("report source schema mismatch")
        report_id = _identifier(row.get("report_id"), "report_id")
        if report_id in seen:
            raise MartError("duplicate report_id")
        seen.add(report_id)
        action_id = _identifier(row.get("action_id"), "action_id")
        visit_id = _identifier(row.get("visit_id"), "visit_id")
        project_id = _identifier(row.get("project_id"), "project_id")
        corrector_id = str(row.get("corrector_id") or "").strip()
        if corrector_id:
            corrector_id = _identifier(corrector_id, "corrector_id")
        fio = str(row.get("corrector_fio") or "").strip()
        source_status = str(row.get("source_status") or "").strip()
        if source_status not in {
            "CURRENT", "STALE_LAST_GOOD", "FETCH_FAILED_NO_HISTORY", "PENDING",
        }:
            raise MartError("unsupported report source_status")
        if source_status in {"CURRENT", "STALE_LAST_GOOD"} and (not corrector_id or not fio):
            raise MartError("corrector attribution incomplete")
        last_success = str(row.get("last_successful_fetch") or "").strip()
        if last_success:
            try:
                datetime.fromisoformat(last_success.replace("Z", "+00:00"))
            except ValueError:
                raise MartError("invalid last_successful_fetch") from None
        normalized.append({
            "report_id": report_id, "action_id": action_id, "visit_id": visit_id,
            "project_id": project_id, "report_date": _date(row.get("report_date")),
            "corrector_id": corrector_id, "corrector_fio": fio,
            "source_status": source_status, "last_successful_fetch": last_success,
        })
    return normalized


def validate_status_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Normalize project-scoped action records; preserve predicate overlap."""
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, int]] = set()
    for row in rows:
        try:
            project_id = _identifier(row.get("project_id"), "status project_id")
            visit_id = _identifier(row.get("visit_id"), "status visit_id")
            action_id = _identifier(row.get("action_id"), "status action_id")
            raw_codes = row.get("workflow_state_codes") or []
            if not raw_codes and row.get("workflow_state_code") not in (None, ""):
                raw_codes = [row.get("workflow_state_code")]
            if not isinstance(raw_codes, (list, tuple, set)):
                raise MartError("invalid workflow state codes")
            codes = []
            for raw_code in raw_codes:
                code = int(raw_code)
                if code not in WORKFLOW_STATES:
                    raise MartError("unknown workflow state code")
                codes.append(code)
            if not codes:
                codes = [None]
            for code in codes:
                key = (project_id, visit_id, action_id, code if code is not None else -1)
                if key in seen:
                    continue
                seen.add(key)
                result.append({
                    "project_id": project_id, "visit_id": visit_id,
                    "action_id": action_id, "workflow_state_code": code,
                    "predicate_source": "project_scoped_action_query",
                })
        except (TypeError, ValueError):
            if isinstance(row, Mapping) and "workflow_state_code" in row:
                try:
                    code = int(row.get("workflow_state_code"))
                    if code not in WORKFLOW_STATES:
                        raise MartError("unknown workflow state code")
                    project_id = _identifier(row.get("project_id"), "status project_id")
                    visit_id = _identifier(row.get("visit_id"), "status visit_id")
                    action_id = _identifier(row.get("action_id"), "status action_id")
                    key = (project_id, visit_id, action_id, code)
                    if key not in seen:
                        seen.add(key)
                        result.append({
                            "project_id": project_id, "visit_id": visit_id,
                            "action_id": action_id, "workflow_state_code": code,
                            "predicate_source": str(row.get("predicate_source") or "action_response"),
                        })
                    continue
                except (TypeError, ValueError):
                    pass
            raise MartError("invalid status membership row") from None
    return result


def build_candidate(
    report_rows: Iterable[Mapping[str, Any]],
    action_rows: Iterable[Mapping[str, Any]],
    *, queried_project_ids: Iterable[Any], generation_id: str | None = None,
    source_snapshot_at: str | None = None,
) -> dict[str, Any]:
    """Build three fact tabs plus manifest data from observed source snapshots."""
    generation = generation_id or uuid.uuid4().hex
    if not re.fullmatch(r"[0-9a-f]{32}", generation):
        raise MartError("invalid generation_id")
    snapshot = source_snapshot_at or datetime.now(timezone.utc).isoformat()
    try:
        datetime.fromisoformat(snapshot.replace("Z", "+00:00"))
    except ValueError:
        raise MartError("invalid source_snapshot_at") from None
    reports = validate_report_source(report_rows)
    if not reports:
        raise MartError("corrector report source is empty")
    status_rows = validate_status_rows(action_rows)
    queried = {_identifier(value, "queried project_id") for value in queried_project_ids}
    required_projects = {row["project_id"] for row in reports}
    if not required_projects.issubset(queried):
        raise MartError("status source is incomplete for report projects")

    status_by_action: dict[tuple[str, str, str], set[int]] = defaultdict(set)
    observed_action_keys: set[tuple[str, str, str]] = set()
    unknown_status_count = 0
    for row in status_rows:
        key = (row["project_id"], row["action_id"], row["visit_id"])
        observed_action_keys.add(key)
        if row["workflow_state_code"] is not None:
            status_by_action[key].add(row["workflow_state_code"])
            if row["workflow_state_code"] not in WORKFLOW_STATES:
                unknown_status_count += 1

    report_tab: list[list[Any]] = [list(REPORT_COLUMNS)]
    report_to_status_match: dict[tuple[str, str, str], bool] = {}
    for report in reports:
        key = (report["project_id"], report["action_id"], report["visit_id"])
        matched = report["project_id"] in queried and key in observed_action_keys
        report_to_status_match[key] = matched
        codes = sorted(status_by_action.get(key, set()))
        flags = {code: ("UNKNOWN" if not matched else "OBSERVED_PRESENT" if code in codes else "OBSERVED_ABSENT")
                 for code in STATUS_CODES}
        report_tab.append([
            PUBLICATION_KEY, generation, report["report_id"], report["action_id"], report["visit_id"],
            report["project_id"], "", report["corrector_id"], report["corrector_fio"],
            report["report_date"], ",".join(str(code) for code in codes) if matched else "",
            flags[35], flags[38], flags[39], flags[40], flags[45], flags[50],
            f"https://lk.j4b.ru/action/{report['action_id']}", report["source_status"],
            report["last_successful_fetch"] or snapshot,
            "OBSERVED_CORRECTOR_SNAPSHOT_ONLY",
        ])
        if not matched:
            unknown_status_count += 1

    visits: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for report in reports:
        visits[(report["project_id"], report["visit_id"])].append(report)
    visit_tab: list[list[Any]] = [list(VISIT_COLUMNS)]
    ambiguous_versions = 0
    for (project_id, visit_id), records in sorted(visits.items(), key=lambda item: (int(item[0][0]), int(item[0][1]))):
        report_ids = sorted({item["report_id"] for item in records}, key=int)
        corrector_ids = sorted({item["corrector_id"] for item in records if item["corrector_id"]}, key=int)
        if len(report_ids) > 1:
            ambiguous_versions += 1
        visit_tab.append([
            PUBLICATION_KEY, generation, project_id, visit_id, ",".join(report_ids), len(report_ids),
            ",".join(corrector_ids), "", "", "UNRESOLVED_CURRENT_VERSION_RULE",
            "OBSERVED_CORRECTOR_SNAPSHOT_ONLY",
        ])

    unmatched = sum(
        1 for report in reports
        if not report_to_status_match.get((report["project_id"], report["action_id"], report["visit_id"]))
    )
    # Freshness refers to today's verified collector run, not each stable
    # report's last individual Portal detail read.
    source_freshness = "FRESH_COLLECTOR_SUCCESS"
    quality_tab = [list(QUALITY_COLUMNS), [
        PUBLICATION_KEY, SCHEMA_VERSION, generation, "PUBLISHED_SUCCESS",
        datetime.now(timezone.utc).isoformat(), snapshot, len(reports), len(reports), len(visits),
        unmatched, ambiguous_versions, unknown_status_count,
        "OBSERVED_CORRECTOR_SNAPSHOT_ONLY", source_freshness, len(queried),
    ]]
    dictionary_tab = [list(METRIC_DICTIONARY_COLUMNS)] + [
        [PUBLICATION_KEY, generation, *metric] for metric in METRIC_DICTIONARY
    ]
    candidate = {
        "controller_report_snapshot": report_tab,
        "controller_visit_summary": visit_tab,
        "metric_dictionary": dictionary_tab,
        "data_quality_status": quality_tab,
    }
    validate_candidate(candidate, expected_generation=generation)
    return candidate


def validate_candidate(candidate: Mapping[str, list[list[Any]]], *, expected_generation: str) -> None:
    if set(candidate) != set(TAB_SCHEMAS):
        raise MartError("candidate tab set mismatch")
    for title, headers in TAB_SCHEMAS.items():
        rows = candidate[title]
        if not rows or tuple(rows[0]) != headers:
            raise MartError(f"candidate schema mismatch: {title}")
        if any(len(row) != len(headers) for row in rows[1:]):
            raise MartError(f"candidate row width mismatch: {title}")
        if any(row[0] != PUBLICATION_KEY for row in rows[1:]):
            raise MartError(f"candidate publication key mismatch: {title}")
        if title != "data_quality_status" and any(row[1] != expected_generation for row in rows[1:]):
            raise MartError(f"candidate generation mismatch: {title}")
    report_rows = candidate["controller_report_snapshot"][1:]
    report_ids = [str(row[2]) for row in report_rows]
    if len(report_ids) != len(set(report_ids)):
        raise MartError("duplicate report_id in candidate")
    visit_rows = candidate["controller_visit_summary"][1:]
    visit_keys = [(str(row[2]), str(row[3])) for row in visit_rows]
    if len(visit_keys) != len(set(visit_keys)):
        raise MartError("duplicate visit key in candidate")
    metric_rows = candidate["metric_dictionary"][1:]
    metric_keys = [str(row[2]) for row in metric_rows]
    if len(metric_keys) != len(set(metric_keys)):
        raise MartError("duplicate metric_key in candidate")
    quality = candidate["data_quality_status"]
    if len(quality) != 2 or quality[1][2] != expected_generation:
        raise MartError("quality status singleton contract failed")
    if any(
        flag not in {"OBSERVED_PRESENT", "OBSERVED_ABSENT", "UNKNOWN"}
        for row in report_rows for flag in row[11:17]
    ):
        raise MartError("report status flag is not tri-state")
    if quality[1][6] != len(report_rows) or quality[1][7] != len(report_rows):
        raise MartError("quality report counts do not reconcile")
    if quality[1][8] != len(visit_rows):
        raise MartError("quality visit count does not reconcile")


def extract_project_action_rows(markup: str, project_id: Any) -> list[dict[str, Any]]:
    """Validate a project-scoped /action response before extracting rows."""
    pid = _identifier(project_id, "project_id")
    lower = markup.casefold()
    if "<html" not in lower and "<!doctype" not in lower:
        raise MartError("action response is not an HTML document")
    if re.search(r"name\s*=\s*[\"']_login[\"']", lower) or re.search(
        r"type\s*=\s*[\"']password[\"']", lower
    ):
        raise MartError("action response is an authentication page")
    if any(marker in lower for marker in ("access denied", "доступ запрещ", "php parse error", "fatal error")):
        raise MartError("action response is an error page")
    project_links = set(re.findall(r"/proj/(\d+)(?:/|[\"'?#])", lower))
    if "<table" not in lower or pid not in project_links or project_links - {pid}:
        raise MartError("action response structural markers missing")
    try:
        parsed = parse_action_table(markup, pid)
    except Exception:
        raise MartError("action response parsing failed") from None
    result = []
    for row in parsed:
        action_id = row.get("action_id")
        if not action_id or action_id == "0":
            continue
        result.append({
            "project_id": pid, "visit_id": row["visit_id"], "action_id": action_id,
            "workflow_state_codes": [int(code) for code in row.get("workflow_state_codes") or []],
        })
    return result


def acquire_action_statuses(
    project_ids: Iterable[Any], fetch: Callable[[str], tuple[int, str, bytes]], *,
    interval: float = MIN_STATUS_REQUEST_INTERVAL_SECONDS, sleep: Callable[[float], None] = time.sleep,
) -> tuple[list[dict[str, Any]], set[str]]:
    """One project-scoped POST per project, paced, with no automatic retries."""
    ids = sorted({_identifier(item, "project_id") for item in project_ids}, key=int)
    if len(ids) > MAX_STATUS_PROJECTS_PER_RUN:
        raise MartError("status project request cap exceeded")
    if interval < MIN_STATUS_REQUEST_INTERVAL_SECONDS:
        raise MartError("status request interval below safety minimum")
    memberships: list[dict[str, Any]] = []
    covered: set[str] = set()
    for index, pid in enumerate(ids):
        if index:
            sleep(interval)
        try:
            status, content_type, body = fetch(pid)
        except Exception:
            raise MartError("status request failed; no publication") from None
        if status != 200 or "html" not in content_type.casefold():
            raise MartError("status response rejected; no publication")
        markup = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
        memberships.extend(extract_project_action_rows(markup, pid))
        covered.add(pid)
    return memberships, covered


@contextmanager
def shared_lock(path: str | os.PathLike[str]):
    """Acquire the existing production lock without changing its owner/mode."""
    lock_path = Path(path)
    if not lock_path.is_file():
        raise MartError("shared production lock file is missing")
    descriptor = os.open(lock_path, os.O_RDWR)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise MartError("shared production lock is held") from None
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _column_letter(number: int) -> str:
    result = ""
    while number:
        number, rem = divmod(number - 1, 26)
        result = chr(65 + rem) + result
    return result


class GenerationPublisher:
    """Append immutable generation rows, verify, then switch one manifest row."""

    FACT_TABS = (
        "controller_report_snapshot", "controller_visit_summary", "metric_dictionary",
    )

    def __init__(self, gateway: Any) -> None:
        self.gateway = gateway

    def validate_target(self) -> None:
        """Read-only qualification of the owner-created four-tab target."""
        metadata = set(self.gateway.titles())
        if metadata != set(TAB_SCHEMAS):
            raise PublicationError("target spreadsheet tab set mismatch")
        for title, headers in TAB_SCHEMAS.items():
            rows = self.gateway.read(title)
            if not rows or tuple(rows[0]) != headers:
                raise PublicationError(f"target schema mismatch: {title}")
        quality = self.gateway.read("data_quality_status")
        if len(quality) > 2:
            raise PublicationError("target has more than one active-generation row")
        if len(quality) == 2:
            active = quality[1]
            if len(active) < len(QUALITY_COLUMNS) or active[0] != PUBLICATION_KEY or not re.fullmatch(
                r"[0-9a-f]{32}", str(active[2])
            ):
                raise PublicationError("existing active-generation pointer is invalid")

    def _append_and_verify(self, title: str, rows: list[list[Any]]) -> None:
        current = self.gateway.read(title)
        headers = tuple(TAB_SCHEMAS[title])
        if not current or tuple(current[0]) != headers:
            raise PublicationError(f"target schema mismatch: {title}")
        start = len(current) + 1
        end_col = _column_letter(len(headers))
        end_row = start + len(rows) - 2
        if len(rows) == 1:
            return
        try:
            self.gateway.update(title, f"A{start}:{end_col}{end_row}", rows[1:])
        except Exception:
            raise AmbiguousPublicationError(f"write outcome unknown: {title}") from None
        actual = self.gateway.read_range(title, f"A{start}:{end_col}{end_row}")
        if _normalize(actual, len(headers)) != _normalize(rows[1:], len(headers)):
            raise PublicationError(f"generation readback mismatch: {title}")

    def publish(self, candidate: Mapping[str, list[list[Any]]], *, generation_id: str) -> str:
        validate_candidate(candidate, expected_generation=generation_id)
        self.validate_target()
        # Keep previous active pointer untouched until every fact write/readback passes.
        for title in self.FACT_TABS:
            self._append_and_verify(title, candidate[title])
        new_pointer = candidate["data_quality_status"][1]
        try:
            self.gateway.update("data_quality_status", f"A2:O2", [new_pointer])
        except Exception:
            # A write exception may be post-commit. Read once to reconcile; never retry.
            try:
                observed = self.gateway.read_range("data_quality_status", "A2:O2")
            except Exception:
                raise AmbiguousPublicationError("active generation pointer outcome unknown") from None
            if _normalize(observed, len(QUALITY_COLUMNS)) != _normalize([new_pointer], len(QUALITY_COLUMNS)):
                raise AmbiguousPublicationError("active generation pointer outcome unknown") from None
        pointer_readback = self.gateway.read_range("data_quality_status", "A2:O2")
        if _normalize(pointer_readback, len(QUALITY_COLUMNS)) != _normalize([new_pointer], len(QUALITY_COLUMNS)):
            raise PublicationError("active generation readback mismatch")
        return "PUBLISHED_AND_VERIFIED"


def _normalize(rows: Iterable[Iterable[Any]] | None, width: int) -> list[list[str]]:
    result = []
    for row in rows or []:
        values = ["" if value is None else str(value) for value in row]
        result.append((values + [""] * width)[:width])
    return result


def candidate_fingerprint(candidate: Mapping[str, list[list[Any]]]) -> str:
    encoded = json.dumps(candidate, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def validate_collector_success(evidence: Mapping[str, Any], *, now: datetime) -> str:
    """Require today's successful collector invocation; zero new rows is valid."""
    if now.tzinfo is None:
        raise MartError("current time must be timezone-aware")
    try:
        from zoneinfo import ZoneInfo
        moscow = ZoneInfo("Europe/Moscow")
    except Exception:
        raise MartError("Europe/Moscow timezone is unavailable") from None
    if str(evidence.get("result", "")).casefold() != "success":
        raise MartError("collector did not complete successfully")
    if str(evidence.get("exit_status", "")) != "0":
        raise MartError("collector exit status is not successful")
    started = evidence.get("started_at")
    if not isinstance(started, datetime) or started.tzinfo is None:
        raise MartError("collector start time is unavailable")
    if started.astimezone(moscow).date() != now.astimezone(moscow).date():
        raise MartError("collector success is not from today")
    # This field explicitly distinguishes a valid no-change run from failure.
    if evidence.get("collector_result") not in {"SUCCESS", "SUCCESS_NO_NEW_RECORDS"}:
        raise MartError("collector result marker is missing or unsuccessful")
    return str(evidence["collector_result"])


def read_systemd_collector_evidence(
    *, unit: str = "j4b-report-correctors.service",
    log_dir: str | os.PathLike[str] = "/home/maxis/j4b-report-correctors/logs",
) -> dict[str, Any]:
    """Read systemd result and private collector run-log summary; invoke no unit."""
    completed = subprocess.run(
        ["systemctl", "show", unit, "--property=Result,ExecMainStatus,ExecMainStartTimestamp"],
        check=False, capture_output=True, text=True, timeout=10,
    )
    if completed.returncode != 0:
        raise MartError("collector systemd status is unavailable")
    properties = {}
    for line in completed.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            properties[key] = value
    try:
        from zoneinfo import ZoneInfo
        moscow = ZoneInfo("Europe/Moscow")
    except Exception:
        raise MartError("Europe/Moscow timezone is unavailable") from None
    try:
        start_text = properties.get("ExecMainStartTimestamp", "")
        started = datetime.strptime(start_text.rsplit(" ", 1)[0], "%a %Y-%m-%d %H:%M:%S").replace(tzinfo=moscow)
    except ValueError:
        raise MartError("collector start timestamp is invalid") from None
    if properties.get("Result") != "success" or properties.get("ExecMainStatus") != "0":
        raise MartError("collector systemd result is unsuccessful")
    try:
        candidates = sorted(
            Path(log_dir).glob("collectors-*.log"),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
        log_path = candidates[0]
        log_stat = log_path.stat()
    except (OSError, IndexError):
        raise MartError("collector run log is unavailable") from None
    if log_stat.st_mode & 0o077:
        raise MartError("collector run log permissions are too broad")
    if datetime.fromtimestamp(log_stat.st_mtime, moscow) < started.astimezone(moscow):
        raise MartError("collector run log does not match latest systemd run")
    try:
        log_lines = log_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        raise MartError("collector run log could not be read") from None
    final_event = None
    for line in reversed(log_lines):
        try:
            event = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(event, dict) and "rows" in event and "queue_remaining" in event:
            final_event = event
            break
    if not final_event or final_event.get("status") != "OK":
        raise MartError("collector run result is unsuccessful")
    if int(final_event.get("queue_remaining", -1)) != 0:
        raise MartError("collector still has unresolved queued reports")
    batch = final_event.get("batch") or {}
    reconciliation = final_event.get("reconciliation") or {}
    no_changes = (
        final_event.get("discovered") == 0
        and batch.get("attempted", 0) == 0
        and not any(int(reconciliation.get(key, 0) or 0) for key in ("added", "updated", "removed"))
    )
    return {
        "result": properties["Result"], "exit_status": properties["ExecMainStatus"],
        "started_at": started,
        "collector_result": "SUCCESS_NO_NEW_RECORDS" if no_changes else "SUCCESS",
    }


def run_refresh(
    *, report_rows: Iterable[Mapping[str, Any]] | Callable[[], Iterable[Mapping[str, Any]]],
    fetch_action: Callable[[str], tuple[int, str, bytes]],
    queried_project_ids: Iterable[Any] | None = None,
    publisher: GenerationPublisher | None = None,
    publish_enabled: bool = False,
    collector_evidence: Mapping[str, Any],
    now: datetime,
    lock_path: str | os.PathLike[str],
) -> dict[str, Any]:
    """Run the post-collector build under the main refresh shared lock."""
    collector_result = validate_collector_success(collector_evidence, now=now)
    with shared_lock(lock_path):
        source_rows = report_rows() if callable(report_rows) else report_rows
        normalized_reports = validate_report_source(source_rows)
        project_ids = sorted(
            {_identifier(row["project_id"], "project_id") for row in normalized_reports}, key=int
        )
        action_rows: list[dict[str, Any]] = []
        covered: set[str] = set()
        if queried_project_ids is None:
            action_rows, covered = acquire_action_statuses(project_ids, fetch_action)
        else:
            # Test seam / already acquired source; production CLI never accepts this input.
            covered = {_identifier(value, "queried project_id") for value in queried_project_ids}
        candidate = build_candidate(
            normalized_reports, action_rows, queried_project_ids=covered,
            source_snapshot_at=now.astimezone(timezone.utc).isoformat(),
        )
        generation = candidate["data_quality_status"][1][2]
        result = {
            "status": "DRY_RUN_VALIDATED", "collector_result": collector_result,
            "generation_id": generation,
            "source_report_count": len(candidate["controller_report_snapshot"]) - 1,
            "visit_count": len(candidate["controller_visit_summary"]) - 1,
            "metric_dictionary_count": len(candidate["metric_dictionary"]) - 1,
            "candidate_sha256": candidate_fingerprint(candidate),
            "coverage_status": "OBSERVED_CORRECTOR_SNAPSHOT_ONLY",
        }
        if publish_enabled:
            if publisher is None:
                raise MartError("publisher is required when publication is enabled")
            result["publication"] = publisher.publish(candidate, generation_id=generation)
            result["status"] = "PUBLISHED_AND_VERIFIED"
        return result
