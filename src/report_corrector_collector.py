"""Report -> corrector collection: registry discovery, detail enrichment, checkpoint.

Verified portal model (live checks, 2026-10-22):

* ``POST /report/`` with ``dt1``/``dt2`` returns the report registry; its day
  filter was 96-100% precise, and for 2026-09-30 it contained 1123 of the 1124
  reports independently listed by ``/proj/7986/report`` (99.91% coverage), so
  it is used as the cross-project discovery source.
* ``/report/{id}`` is not a detail page (it redirects to ``/report/``).
  ``/action/{id}`` is the detail page, and for every sampled report the action
  page linked ``/report/{same id}``, i.e. ``report_id == action_id``.
* The corrector is ``select[name=staff]`` on that action page.
* The report's filing date is the action page's ``dt`` value: the project report
  journal lists each report under exactly that date.

Nothing here guesses a join: a detail page whose report/action IDs disagree is
rejected rather than stored.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any, Callable, Collection, Iterable, Mapping

from .report_reviewer import ReportCorrectorParseError, parse_report_corrector
from .report_inventory import ReportInventoryPage

START_DATE = date(2026, 10, 1)
REGISTRY_PATH = "/report/"
SHEET_NAME = "report_reviewers"
SHEET_COLUMNS = (
    "report_id", "action_id", "visit_id", "project_id", "report_date",
    "corrector_id", "corrector_fio", "source_status", "last_successful_fetch",
)
STATE_VERSION = 1
FAILURE_CATEGORIES = {
    "TRANSPORT_ERROR", "HTTP_ERROR", "PARSE_ERROR", "JOIN_MISMATCH",
    "DATE_OUT_OF_RANGE", "INTERNAL_ERROR",
}
SOURCE_STATUSES = {
    "PENDING", "CURRENT", "NO_CORRECTOR", "NO_CORRECTOR_EXHAUSTED",
    "STALE_LAST_GOOD", "FETCH_FAILED_NO_HISTORY", "CORRECTOR_REMOVED",
}
# Hard per-run budget and pacing for detail GETs. The budget is deliberately
# below the portal's tolerance so a run can always be interrupted safely.
MAX_DETAIL_GETS_PER_RUN = 200
MIN_REQUEST_INTERVAL_SECONDS = 1.5
# A report that has never had a corrector is re-read at most twice, inside a
# seven-day window measured from its first empty read.
NO_CORRECTOR_MAX_RECHECKS = 2
NO_CORRECTOR_WINDOW = timedelta(days=7)


@dataclass(frozen=True)
class RecheckPolicy:
    """When a resolved report is re-read on its own, without an operator.

    ``CURRENT`` and ``CORRECTOR_REMOVED`` are stable by default: they are never
    re-read on a schedule, only on an explicit manual request. A report that has
    never had a corrector is re-read up to ``NO_CORRECTOR_MAX_RECHECKS`` times
    inside ``NO_CORRECTOR_WINDOW`` and then becomes terminal
    (``NO_CORRECTOR_EXHAUSTED``) instead of being retried forever.
    """

    no_corrector_delays: tuple[timedelta, ...] = (
        timedelta(days=1), timedelta(days=2),
    )
    window: timedelta = NO_CORRECTOR_WINDOW

    def validated(self) -> "RecheckPolicy":
        if len(self.no_corrector_delays) > NO_CORRECTOR_MAX_RECHECKS:
            raise CollectorError("too many no-corrector rechecks configured")
        for delay in self.no_corrector_delays:
            if delay.total_seconds() <= 0:
                raise CollectorError("recheck interval must be positive")
        if self.window.total_seconds() <= 0:
            raise CollectorError("recheck window must be positive")
        return self

    def no_corrector_delay(self, empty_reads: int) -> timedelta | None:
        """Delay before the next recheck, or ``None`` when the budget is spent."""
        index = max(int(empty_reads) - 1, 0)
        if index >= len(self.no_corrector_delays):
            return None
        return self.no_corrector_delays[index]


DEFAULT_POLICY = RecheckPolicy()

_REPORT_LINK = re.compile(r"/report/(\d+)(?:/|$)", re.I)
_ACTION_LINK = re.compile(r"/action/(\d+)(?:/|$)", re.I)
_VISIT_LINK = re.compile(r"/visit/(\d+)(?:/|$)", re.I)
_PROJ_LINK = re.compile(r"/proj/(\d+)(?:/|$)", re.I)
_DT_INPUT = re.compile(r"""name=["']dt["'][^>]*value=["'](\d{2}\.\d{2}\.\d{4})["']""", re.I)
_DT_INPUT_REVERSED = re.compile(r"""value=["'](\d{2}\.\d{2}\.\d{4})["'][^>]*name=["']dt["']""", re.I)


class CollectorError(ValueError):
    """A structural or validation error carrying no portal text."""


@dataclass(frozen=True)
class ReportDetail:
    """One report as read from its action page."""

    report_id: str
    visit_id: str
    project_id: str
    report_date: str
    corrector_state: str
    corrector_id: str | None = None
    corrector_fio: str | None = None

    def validated(self) -> "ReportDetail":
        for field_name in ("report_id", "visit_id", "project_id"):
            value = str(getattr(self, field_name))
            if not value.isdigit() or int(value) <= 0:
                raise CollectorError(f"invalid {field_name}")
        try:
            parsed = date.fromisoformat(self.report_date)
        except (TypeError, ValueError):
            raise CollectorError("invalid report_date") from None
        if self.corrector_state not in {"VALUE_PRESENT", "EMPTY"}:
            raise CollectorError("corrector state is not resolved")
        if self.corrector_state == "VALUE_PRESENT":
            if not (self.corrector_id or "").isdigit():
                raise CollectorError("corrector id is not numeric")
            if not (self.corrector_fio or "").strip():
                raise CollectorError("corrector name is missing")
        return ReportDetail(
            self.report_id, self.visit_id, self.project_id, parsed.isoformat(),
            self.corrector_state,
            self.corrector_id if self.corrector_state == "VALUE_PRESENT" else None,
            self.corrector_fio if self.corrector_state == "VALUE_PRESENT" else None,
        )


class _RegistryIds(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        href = dict(attrs).get("href") or ""
        for identifier in _REPORT_LINK.findall(href):
            if identifier != "0":
                self.ids.add(identifier)


def extract_registry_report_ids(html: str) -> tuple[str, ...]:
    """Return the report IDs linked by one registry response."""
    if not isinstance(html, str) or not html.strip():
        raise CollectorError("registry HTML is empty")
    parser = _RegistryIds()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        raise CollectorError("registry HTML is malformed") from None
    return tuple(sorted(parser.ids, key=int))


class _DetailPage(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.reports: set[str] = set()
        self.actions: set[str] = set()
        self.visits: set[str] = set()
        self.projects: set[str] = set()
        self.dt: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {str(key).casefold(): value for key, value in attrs}
        if tag == "a":
            href = attributes.get("href") or ""
            for kind, pattern in (
                ("report", _REPORT_LINK), ("action", _ACTION_LINK),
                ("visit", _VISIT_LINK), ("proj", _PROJ_LINK),
            ):
                target = getattr(self, {"report": "reports", "action": "actions",
                                        "visit": "visits", "proj": "projects"}[kind])
                for identifier in pattern.findall(href):
                    if identifier != "0":
                        target.add(identifier)
        elif tag == "input" and (attributes.get("name") or "").casefold() == "dt":
            value = (attributes.get("value") or "").strip()
            if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", value):
                self.dt = value


def parse_report_detail(html: str, report_id: str) -> ReportDetail:
    """Parse one action page into a :class:`ReportDetail`, failing closed."""
    if not isinstance(html, str) or not html.strip():
        raise CollectorError("action page HTML is empty")
    page = _DetailPage()
    try:
        page.feed(html)
        page.close()
    except Exception:
        raise CollectorError("action page HTML is malformed") from None

    if report_id not in page.actions or report_id not in page.reports:
        raise CollectorError("report/action join mismatch")
    if page.reports - {report_id} or page.actions - {report_id}:
        raise CollectorError("action page links an unexpected report")
    if len(page.visits) != 1 or len(page.projects) != 1:
        raise CollectorError("action page does not resolve one visit and project")
    if page.dt is None:
        raise CollectorError("action page has no dt value")

    day, month, year = (int(part) for part in page.dt.split("."))
    try:
        report_date = date(year, month, day).isoformat()
    except ValueError:
        raise CollectorError("action page dt is not a valid date") from None

    parsed = parse_report_corrector(html)
    if parsed.state == "FIELD_NOT_EXPOSED":
        raise CollectorError("corrector field is not exposed")
    return ReportDetail(
        report_id=report_id,
        visit_id=next(iter(page.visits)),
        project_id=next(iter(page.projects)),
        report_date=report_date,
        corrector_state=parsed.state,
        corrector_id=parsed.corrector_id,
        corrector_fio=parsed.corrector_fio,
    ).validated()


def new_state() -> dict[str, Any]:
    return {"version": STATE_VERSION, "start_date": START_DATE.isoformat(), "reports": {}}


def _require_state(state: Mapping[str, Any]) -> None:
    if state.get("version") != STATE_VERSION or state.get("start_date") != START_DATE.isoformat():
        raise CollectorError("unsupported collector state")
    if not isinstance(state.get("reports"), dict):
        raise CollectorError("collector state has no reports map")


def merge_candidates(state: dict[str, Any], report_ids: Iterable[str], *, discovered_for: str) -> int:
    """Add newly discovered report IDs without altering existing entries."""
    _require_state(state)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", discovered_for):
        raise CollectorError("invalid discovery date")
    added = 0
    for raw in report_ids:
        report_id = str(raw)
        if not report_id.isdigit() or int(report_id) <= 0:
            raise CollectorError("invalid report id")
        if report_id in state["reports"]:
            continue
        state["reports"][report_id] = {
            "discovered_for": discovered_for,
            "detail": None,
            "last_good": None,
            "last_attempt_at": None,
            "last_attempt_status": "PENDING",
            "failure_count": 0,
            "next_attempt_at": None,
            "next_recheck_at": None,
            "empty_reads": 0,
            "first_no_corrector_at": None,
            "last_checked_at": None,
        }
        added += 1
    return added


def bounded_project_report_candidates(
    page: ReportInventoryPage,
    project_id: str,
    existing_report_ids: Collection[str],
    *,
    max_candidates: int,
) -> tuple[str, ...]:
    """Validate one explicit project-page supplement without assuming completeness.

    The global ``/report/`` registry has a reproduced omission. This optional
    fallback is restricted to a caller-specified project, one non-paginated
    project report page, and a candidate ceiling no larger than the remaining
    detail-GET budget. It fails closed on pagination or a cross-project row; it
    is not a substitute for a complete global registry.
    """
    pid = str(project_id).strip()
    if not pid.isdigit() or int(pid) <= 0:
        raise CollectorError("invalid project report fallback project_id")
    if not 0 <= max_candidates <= MAX_DETAIL_GETS_PER_RUN:
        raise CollectorError("project report fallback budget is outside the detail GET cap")
    if page.tables < 1:
        raise CollectorError("project report listing has no table")
    if page.pagination:
        raise CollectorError("project report listing is paginated; fallback is incomplete")
    if set(page.project_ids) != {pid}:
        raise CollectorError("project report listing contains a different project")
    if any(row.project_id and row.project_id != pid for row in page.rows):
        raise CollectorError("project report row belongs to a different project")

    page_ids = set(page.report_ids)
    if len(page_ids) > max_candidates:
        raise CollectorError("project report listing exceeds the bounded candidate limit")
    if any(not report_id.isdigit() or int(report_id) <= 0 for report_id in page_ids):
        raise CollectorError("project report listing contains an invalid report id")
    candidates = tuple(sorted(page_ids - {str(value) for value in existing_report_ids}, key=int))
    if len(candidates) > max_candidates:
        raise CollectorError("project report listing exceeds the remaining detail GET budget")
    return candidates


def record_discovery(
    state: dict[str, Any], report_ids: Iterable[str], *, discovered_for: str, at: datetime,
) -> dict[str, int]:
    """Merge a registry read and record coverage diagnostics for that day.

    Registry coverage is not assumed complete: a day can yield new IDs after it
    was first scanned (observed drift: 459 -> 412 for one October day). Those
    late arrivals are counted per day so drifting coverage stays visible
    instead of being silently trusted.
    """
    _require_state(state)
    if at.tzinfo is None:
        raise CollectorError("invalid discovery timestamp")
    timestamp = at.astimezone(timezone.utc).isoformat()
    before = {rid for rid, entry in state["reports"].items() if entry.get("discovered_for") == discovered_for}
    added = merge_candidates(state, report_ids, discovered_for=discovered_for)
    after = {rid for rid, entry in state["reports"].items() if entry.get("discovered_for") == discovered_for}
    late = len(after - before) if before else 0
    days = state.setdefault("discovery", {})
    if not isinstance(days, dict):
        raise CollectorError("collector state has no discovery map")
    record = days.get(discovered_for) or {"runs": 0, "late_adds": 0}
    record["runs"] = int(record.get("runs", 0)) + 1
    record["late_adds"] = int(record.get("late_adds", 0)) + late
    record["unique"] = len(after)
    record["last_run_at"] = timestamp
    days[discovered_for] = record
    # Keep the diagnostic bounded: a year of daily records is not needed.
    if len(days) > 60:
        for stale in sorted(days)[:-60]:
            days.pop(stale, None)
    return {"added": added, "late_adds": late, "unique": len(after)}


def coverage_summary(state: Mapping[str, Any]) -> dict[str, int]:
    """Aggregate discovery coverage so a run can report it without listing IDs."""
    _require_state(state)
    days = state.get("discovery") or {}
    return {
        "days_tracked": len(days),
        "days_with_late_ids": sum(1 for record in days.values() if int(record.get("late_adds", 0)) > 0),
        "late_ids_total": sum(int(record.get("late_adds", 0)) for record in days.values()),
    }


def pending_report_ids(
    state: Mapping[str, Any], now: datetime, max_items: int, *,
    scope: Collection[str] | None = None,
) -> list[str]:
    """Return unresolved IDs first, then entries whose recheck is due.

    A resolved entry is due only when it carries an explicit ``next_recheck_at``
    in the past. ``CURRENT`` and ``CORRECTOR_REMOVED`` entries normally carry
    none, so a stable dataset costs no requests; an operator can make a chosen
    report due again with :func:`request_manual_recheck`.
    """
    _require_state(state)
    if max_items <= 0:
        raise CollectorError("max_items must be positive")
    if now.tzinfo is None:
        raise CollectorError("now must be timezone-aware")
    allowed = None if scope is None else {str(value) for value in scope}
    now_text = now.astimezone(timezone.utc).isoformat()
    fresh: list[str] = []
    due: list[tuple[str, str]] = []
    for report_id, entry in state["reports"].items():
        if allowed is not None and report_id not in allowed:
            continue
        if entry.get("last_attempt_status") == "DATE_OUT_OF_RANGE":
            continue
        if entry.get("last_attempt_status") == "NO_CORRECTOR_EXHAUSTED":
            continue
        next_attempt = entry.get("next_attempt_at")
        if next_attempt and next_attempt > now_text:
            continue
        if entry.get("detail") is None:
            fresh.append(report_id)
            continue
        recheck = entry.get("next_recheck_at")
        if recheck is not None and recheck <= now_text:
            due.append((recheck, report_id))
    ordered = sorted(fresh, key=int)[:max_items]
    remaining = max_items - len(ordered)
    if remaining > 0:
        ordered += [report_id for _, report_id in sorted(due)[:remaining]]
    return ordered


def queue_remaining(state: Mapping[str, Any], now: datetime) -> int:
    """Count what is still queued without fetching anything."""
    _require_state(state)
    if now.tzinfo is None:
        raise CollectorError("now must be timezone-aware")
    now_text = now.astimezone(timezone.utc).isoformat()
    queued = 0
    for entry in state["reports"].values():
        if entry.get("last_attempt_status") in {"DATE_OUT_OF_RANGE", "NO_CORRECTOR_EXHAUSTED"}:
            continue
        next_attempt = entry.get("next_attempt_at")
        if next_attempt and next_attempt > now_text:
            continue
        if entry.get("detail") is None:
            queued += 1
            continue
        recheck = entry.get("next_recheck_at")
        if recheck is not None and recheck <= now_text:
            queued += 1
    return queued


def request_manual_recheck(state: dict[str, Any], report_ids: Iterable[str], at: datetime) -> list[str]:
    """Make explicitly chosen reports due on the next run.

    This is the only way a stable ``CURRENT`` or ``CORRECTOR_REMOVED`` entry is
    re-read. It never bypasses the per-run budget or the request interval.
    """
    _require_state(state)
    if at.tzinfo is None:
        raise CollectorError("invalid manual recheck timestamp")
    timestamp = at.astimezone(timezone.utc)
    accepted: list[str] = []
    for raw in report_ids:
        report_id = str(raw).strip()
        if not report_id.isdigit() or report_id not in state["reports"]:
            raise CollectorError("manual recheck target is outside inventory")
        entry = state["reports"][report_id]
        if entry.get("last_attempt_status") == "DATE_OUT_OF_RANGE":
            raise CollectorError("manual recheck target is out of scope")
        entry.update({
            "next_attempt_at": None,
            "next_recheck_at": timestamp.isoformat(),
            "manual_recheck_requested_at": timestamp.isoformat(),
        })
        accepted.append(report_id)
    return accepted


def store_detail(
    state: dict[str, Any], detail: ReportDetail, fetched_at: datetime, *,
    policy: RecheckPolicy = DEFAULT_POLICY,
) -> None:
    """Store a resolved read and schedule the next check it actually deserves.

    ``CURRENT`` and ``CORRECTOR_REMOVED`` are terminal by default. A report that
    has never had a corrector is read at most ``NO_CORRECTOR_MAX_RECHECKS`` more
    times inside a seven-day window; afterwards it becomes
    ``NO_CORRECTOR_EXHAUSTED`` so the pool cannot produce unbounded traffic.
    """
    _require_state(state)
    policy.validated()
    if detail.report_id not in state["reports"]:
        raise CollectorError("report id is outside inventory")
    if fetched_at.tzinfo is None:
        raise CollectorError("invalid fetch time")
    timestamp = fetched_at.astimezone(timezone.utc)
    entry = state["reports"][detail.report_id]
    previous = entry.get("last_good") or {}
    previous_id = previous.get("corrector_id")

    empty_reads = int(entry.get("empty_reads", 0))
    first_empty_text = entry.get("first_no_corrector_at")
    first_empty = datetime.fromisoformat(first_empty_text) if first_empty_text else None
    next_recheck: str | None = None

    if detail.corrector_state == "VALUE_PRESENT":
        source_status = "CURRENT"
        empty_reads = 0
        first_empty_text = None
    elif previous_id:
        # A confirmed empty read after a real assignment: withdraw, keep history,
        # and stop asking. A restoration is found only by a manual recheck.
        source_status = "CORRECTOR_REMOVED"
    else:
        empty_reads += 1
        first_empty = first_empty or timestamp
        first_empty_text = first_empty.isoformat()
        delay = policy.no_corrector_delay(empty_reads)
        expired = (timestamp - first_empty) >= policy.window
        if delay is None or expired or (timestamp + delay - first_empty) > policy.window:
            source_status = "NO_CORRECTOR_EXHAUSTED"
        else:
            source_status = "NO_CORRECTOR"
            next_recheck = (timestamp + delay).isoformat()

    last_good = {
        "corrector_id": detail.corrector_id,
        "corrector_fio": detail.corrector_fio,
        "source_status": source_status,
        "last_successful_fetch": timestamp.isoformat(),
    }
    if source_status == "CORRECTOR_REMOVED":
        last_good["withdrawn_corrector_id"] = previous_id
        last_good["withdrawn_fio"] = previous.get("corrector_fio")
        last_good["withdrawn_at"] = timestamp.isoformat()
        last_good["withdrawn_seen_from"] = previous.get("last_successful_fetch")

    entry.update({
        "detail": asdict(detail),
        "last_good": last_good,
        "last_attempt_at": timestamp.isoformat(),
        "last_attempt_status": "PASS",
        "failure_count": 0,
        "next_attempt_at": None,
        "next_recheck_at": next_recheck,
        "empty_reads": empty_reads,
        "first_no_corrector_at": first_empty_text,
        "last_checked_at": timestamp.isoformat(),
    })


def store_failure(state: dict[str, Any], report_id: str, category: str, attempted_at: datetime) -> None:
    _require_state(state)
    if report_id not in state["reports"]:
        raise CollectorError("report id is outside inventory")
    if category not in FAILURE_CATEGORIES or attempted_at.tzinfo is None:
        raise CollectorError("invalid failure category or timestamp")
    entry = state["reports"][report_id]
    failures = int(entry.get("failure_count", 0)) + 1
    delay = min(300 * (2 ** min(failures - 1, 10)), 86_400)
    attempt = attempted_at.astimezone(timezone.utc)
    entry.update({
        "last_attempt_at": attempt.isoformat(),
        "last_attempt_status": category,
        "failure_count": failures,
        "next_attempt_at": (attempt + timedelta(seconds=delay)).isoformat(),
    })


def store_out_of_scope(state: dict[str, Any], report_id: str, seen_at: datetime) -> None:
    """Record a report filed before the cutover: terminal, never published, never retried."""
    _require_state(state)
    if report_id not in state["reports"]:
        raise CollectorError("report id is outside inventory")
    if seen_at.tzinfo is None:
        raise CollectorError("invalid timestamp")
    entry = state["reports"][report_id]
    entry.update({
        "detail": None,
        "last_attempt_at": seen_at.astimezone(timezone.utc).isoformat(),
        "last_attempt_status": "DATE_OUT_OF_RANGE",
        "next_attempt_at": None,
        "next_recheck_at": None,
    })


_DEGRADATION = re.compile(r"HTTP (?:429|5\d\d)\b")


def _degradation_signal(exc: BaseException) -> str | None:
    """Return the portal degradation signal carried by an exception, if any."""
    match = _DEGRADATION.search(str(exc))
    return match.group(0) if match else None


def migrate_state(state: dict[str, Any], at: datetime) -> dict[str, int]:
    """Bring a v1 checkpoint onto the stable-assignment policy without data loss.

    No history is removed. Only scheduling fields change:
    ``CURRENT`` and ``CORRECTOR_REMOVED`` lose their scheduled recheck (they are
    re-read only on request), and a report that has never had a corrector keeps
    whatever part of its two-recheck budget is still unspent.
    """
    _require_state(state)
    if at.tzinfo is None:
        raise CollectorError("invalid migration timestamp")
    timestamp = at.astimezone(timezone.utc)
    counts = {"current_cleared": 0, "removed_cleared": 0, "no_corrector_migrated": 0}
    for entry in state["reports"].values():
        entry.setdefault("empty_reads", 0)
        entry.setdefault("first_no_corrector_at", None)
        entry.setdefault("last_checked_at", None)
        good = entry.get("last_good") or {}
        status = good.get("source_status")
        if status in {"CURRENT", "CORRECTOR_REMOVED"} and entry.get("next_recheck_at"):
            entry["next_recheck_at"] = None
            counts["removed_cleared" if status == "CORRECTOR_REMOVED" else "current_cleared"] += 1
        elif status == "NO_CORRECTOR":
            before = (entry.get("empty_reads"), entry.get("first_no_corrector_at"),
                      entry.get("next_recheck_at"))
            reads = max(int(entry.get("empty_reads", 0)), 1)
            entry["empty_reads"] = reads
            if not entry.get("first_no_corrector_at"):
                entry["first_no_corrector_at"] = good.get("last_successful_fetch") or timestamp.isoformat()
            if not entry.get("last_checked_at"):
                entry["last_checked_at"] = good.get("last_successful_fetch")
            delay = DEFAULT_POLICY.no_corrector_delay(reads)
            entry["next_recheck_at"] = None if delay is None else (
                datetime.fromisoformat(entry["first_no_corrector_at"]) + delay
            ).isoformat()
            after = (entry.get("empty_reads"), entry.get("first_no_corrector_at"),
                     entry.get("next_recheck_at"))
            if after != before:
                counts["no_corrector_migrated"] += 1
    state["schema"] = 2
    return counts


def collect_batch(
    state: dict[str, Any], fetch_html: Callable[[str], str], now: datetime, *,
    max_fetches: int, policy: RecheckPolicy = DEFAULT_POLICY,
    save: Callable[[Mapping[str, Any]], None],
    request_interval_seconds: float = MIN_REQUEST_INTERVAL_SECONDS,
    sleep: Callable[[float], None] | None = None,
    scope: Collection[str] | None = None,
) -> dict[str, int]:
    """Fetch due report detail pages, persisting after every terminal result.

    The batch stops immediately on a portal degradation signal (HTTP 429/5xx) or
    after three consecutive failures, leaving the queue intact for the next run.
    """
    import time as _time

    sleep = sleep or _time.sleep
    policy.validated()
    if not 0 < max_fetches <= MAX_DETAIL_GETS_PER_RUN:
        raise CollectorError(f"max_fetches must be between 1 and {MAX_DETAIL_GETS_PER_RUN}")
    if request_interval_seconds < MIN_REQUEST_INTERVAL_SECONDS:
        raise CollectorError(f"request interval must be at least {MIN_REQUEST_INTERVAL_SECONDS}s")
    selected = pending_report_ids(state, now, max_fetches, scope=scope)
    counts = {
        "attempted": 0, "passed": 0, "failed": 0, "below_start": 0,
        "halted": 0, "remaining": 0,
    }
    consecutive_failures = 0
    for report_id in selected:
        if counts["attempted"]:
            sleep(request_interval_seconds)
        counts["attempted"] += 1
        try:
            detail = parse_report_detail(fetch_html(report_id), report_id)
            if date.fromisoformat(detail.report_date) < START_DATE:
                # Pre-cutover reports stay tracked but are never published.
                store_out_of_scope(state, report_id, now)
                counts["below_start"] += 1
            else:
                store_detail(state, detail, now, policy=policy)
                counts["passed"] += 1
            consecutive_failures = 0
        except CollectorError as exc:
            category = "JOIN_MISMATCH" if "join" in str(exc) else "PARSE_ERROR"
            store_failure(state, report_id, category, now)
            counts["failed"] += 1
            consecutive_failures += 1
        except ReportCorrectorParseError:
            store_failure(state, report_id, "PARSE_ERROR", now)
            counts["failed"] += 1
            consecutive_failures += 1
        except Exception as exc:
            signal = _degradation_signal(exc)
            store_failure(state, report_id, "HTTP_ERROR" if signal else "INTERNAL_ERROR", now)
            counts["failed"] += 1
            consecutive_failures += 1
            if signal:
                counts["halted"] = 1
                save(state)
                break
        save(state)
        if consecutive_failures >= 3:
            counts["halted"] = 1
            break
    counts["remaining"] = queue_remaining(state, now)
    return counts


def render_sheet_rows(state: Mapping[str, Any], *, require_corrector: bool = True) -> list[list[Any]]:
    """Render publishable rows.

    The published dataset is corrector-only: a report must carry a real
    ``corrector_id`` (a positive portal staff id). Reports without a corrector
    stay in the checkpoint for later re-discovery but are never published, so
    ``NO_CORRECTOR`` and ``CORRECTOR_REMOVED`` reports cannot appear in the tab.
    """
    _require_state(state)
    rows: list[list[Any]] = [list(SHEET_COLUMNS)]
    for report_id, entry in sorted(state["reports"].items(), key=lambda pair: int(pair[0])):
        detail = entry.get("detail")
        if not detail:
            continue
        if date.fromisoformat(detail["report_date"]) < START_DATE:
            continue
        good = entry.get("last_good") or {}
        passed = entry.get("last_attempt_status") == "PASS"
        if good:
            source_status = good["source_status"] if passed else "STALE_LAST_GOOD"
        else:
            source_status = "FETCH_FAILED_NO_HISTORY"
        corrector_id = str(good.get("corrector_id") or "")
        if require_corrector and (not corrector_id.isdigit() or int(corrector_id) <= 0):
            continue
        rows.append([
            detail["report_id"], detail["report_id"], detail["visit_id"],
            detail["project_id"], detail["report_date"],
            corrector_id, good.get("corrector_fio") or "",
            source_status, good.get("last_successful_fetch") or "",
        ])
    return rows


def reconcile_dataset(state: Mapping[str, Any]) -> dict[str, Any]:
    """Compare the current publishable set with what the tab already holds.

    Publication is incremental: the tab is the union of every corrector-bearing
    report, so an add/update/remove plan is what keeps it idempotent instead of
    rewritten. A report that lost its corrector appears in ``removed`` and must
    be deleted from the tab rather than left behind as a stale row.
    """
    _require_state(state)
    current = {row[0]: str(row[5]) for row in render_sheet_rows(state)[1:]}
    published: dict[str, str] = {}
    for report_id, entry in state["reports"].items():
        record = entry.get("published") or {}
        if record.get("corrector_id"):
            published[report_id] = str(record["corrector_id"])
    added = sorted((rid for rid in current if rid not in published), key=int)
    updated = sorted(
        (rid for rid in current if rid in published and published[rid] != current[rid]), key=int
    )
    removed = sorted((rid for rid in published if rid not in current), key=int)
    return {
        "publishable": len(current),
        "added": added,
        "updated": updated,
        "removed": removed,
        "unchanged": len(current) - len(added) - len(updated),
    }


def mark_published(state: dict[str, Any], published_at: datetime) -> None:
    """Record the tab's new content after a successful publication."""
    _require_state(state)
    if published_at.tzinfo is None:
        raise CollectorError("invalid publication time")
    timestamp = published_at.astimezone(timezone.utc).isoformat()
    rows = render_sheet_rows(state)
    live = {row[0] for row in rows[1:]}
    for row in rows[1:]:
        state["reports"][row[0]]["published"] = {
            "corrector_id": str(row[5]),
            "corrector_fio": row[6],
            "published_at": timestamp,
        }
    for report_id, entry in state["reports"].items():
        record = entry.get("published") or {}
        if record.get("corrector_id") and report_id not in live:
            entry["published"] = {
                "corrector_id": None,
                "corrector_fio": None,
                "withdrawn_corrector_id": record["corrector_id"],
                "withdrawn_fio": record.get("corrector_fio"),
                "withdrawn_at": timestamp,
                "published_at": record.get("published_at"),
            }


def validate_sheet_rows(rows: list[list[Any]]) -> None:
    if not rows or list(rows[0]) != list(SHEET_COLUMNS):
        raise CollectorError("report_reviewers schema mismatch")
    seen: set[str] = set()
    for row in rows[1:]:
        if len(row) != len(SHEET_COLUMNS):
            raise CollectorError("report_reviewers row width mismatch")
        report_id = str(row[0])
        if not report_id.isdigit() or int(report_id) <= 0 or report_id in seen:
            raise CollectorError("report_reviewers report_id is invalid or duplicated")
        seen.add(report_id)
        try:
            if date.fromisoformat(str(row[4])) < START_DATE:
                raise CollectorError("report_reviewers row predates collection start")
        except ValueError:
            raise CollectorError("report_reviewers report_date is invalid") from None
        if str(row[7]) not in SOURCE_STATUSES:
            raise CollectorError("report_reviewers source_status is invalid")
        if str(row[7]) in {"NO_CORRECTOR", "NO_CORRECTOR_EXHAUSTED", "CORRECTOR_REMOVED"}:
            raise CollectorError("report_reviewers publishes a report without a corrector")
        if not str(row[5]).isdigit() or int(str(row[5])) <= 0:
            raise CollectorError("report_reviewers corrector_id is invalid")
        if row[8]:
            try:
                datetime.fromisoformat(str(row[8]))
            except ValueError:
                raise CollectorError("report_reviewers fetch timestamp is invalid") from None
