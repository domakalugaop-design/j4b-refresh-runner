from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any
from urllib.error import HTTPError, URLError

from .portal_transport import PortalTransportError
from .technical_ids import integer_id


PROJECT_TYPE_COLUMNS = ["project_type_code", "project_type_name"]
STATE_COLUMNS = ["project_id", "project_type_code", "project_type_name"]
PROJECT_TYPE_DICTIONARY = {
    "0001": "Обычный",
    "0002": "Классика_ТП",
    "0003": "Такси_ТП",
    "0004": "Фотоаудит",
    "0005": "Качественные исследования",
    "0007": "B2B_тайный покупатель",
    "0008": "Тестирование цифровых продуктов",
}
PERIOD_MARKER_RE = re.compile(r"(?<!\d)(0[1-9]|1[0-2])(\d{2})(?!\d)")
APPLICABILITY_CUTOFF = (2026, 9)
PROJECT_TYPE_DIRECTORY_PATH = "/admin/proj-type"
VERIFIED_DIRECTORY_CODES = {"0002", "0003", "0004", "0005", "0007", "0008", "0009"}


class ProjectTypeAcquisitionError(RuntimeError):
    """Fail-closed acquisition error carrying only allowlisted diagnostics."""

    _STAGES = {
        "PROJECT_DETAIL_REQUEST",
        "PROJECT_DETAIL_HTTP_STATUS",
        "PROJECT_DETAIL_RESPONSE",
        "PROJECT_DETAIL_UTF8",
        "PROJECT_DETAIL_JSON",
        "PROJECT_DETAIL_TYPE_CLASSIFICATION",
    }
    _CATEGORIES = {
        "TRANSPORT_ERROR", "HTTP_ERROR", "UTF8_ERROR", "JSON_ERROR",
        "UNRESOLVED_TYPE", "INTERNAL_ERROR",
    }

    def __init__(
        self,
        *,
        stage: str,
        project_index: int | str,
        project_total: int | str,
        project_id: Any,
        failure_category: str,
        http_status: int | str,
    ) -> None:
        super().__init__("project type acquisition is incomplete")
        if type(project_index) is not int or project_index < 1:
            project_index = "UNKNOWN"
        if type(project_total) is not int or project_total < 1:
            project_total = "UNKNOWN"
        if project_index != "UNKNOWN" and project_total != "UNKNOWN" and project_index > project_total:
            project_index = "UNKNOWN"
        if stage not in self._STAGES:
            stage = "PROJECT_DETAIL_REQUEST"
        if failure_category not in self._CATEGORIES:
            failure_category = "INTERNAL_ERROR"
        self._stage = stage
        self._project_index = project_index
        self._project_total = project_total
        self._project_id = _safe_project_id(project_id)
        self._failure_category = failure_category
        self._http_status = _safe_http_status(http_status)

    @property
    def diagnostic(self) -> dict[str, Any]:
        return {
            "STAGE": self._stage,
            "PROJECT_INDEX": self._project_index,
            "PROJECT_TOTAL": self._project_total,
            "PROJECT_ID": self._project_id,
            "FAILURE_CATEGORY": self._failure_category,
            "HTTP_STATUS": self._http_status,
        }


def _safe_project_id(project_id: Any) -> str:
    if type(project_id) is int and project_id >= 0:
        return str(project_id)
    if type(project_id) is str and re.fullmatch(r"\d+", project_id):
        return project_id
    return "UNKNOWN"


def _safe_http_status(status: Any) -> int | str:
    if type(status) is int and 100 <= status <= 599:
        return status
    return "UNKNOWN"


class _ProjectTypeDirectoryHTML(HTMLParser):
    """Extract code/name rows from the verified three-column admin directory."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._table = False
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self.pagination = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key.lower(): value or "" for key, value in attrs}
        if tag == "table":
            self._table = True
            self.tables.append([])
        elif tag == "tr" and self._table:
            self._row = []
        elif tag in {"td", "th"} and self._table and self._row is not None:
            self._cell = []
        elif tag == "a":
            rel = attrs_dict.get("rel", "").lower()
            href = attrs_dict.get("href", "").lower()
            classes = attrs_dict.get("class", "").lower()
            aria = attrs_dict.get("aria-label", "").lower()
            if "next" in rel or "pagination" in classes or "page=" in href or "next" in aria:
                self.pagination = True
        elif tag in {"nav", "ul", "div"} and "pagination" in attrs_dict.get("class", "").lower():
            self.pagination = True

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None and self._table:
            self.tables[-1].append(self._row)
            self._row = None
        elif tag == "table":
            self._table = False


def parse_project_type_catalog_html(body: bytes) -> dict[str, str]:
    """Parse the live admin directory and reject incomplete/ambiguous pages.

    The seven codes shown in the owner-provided directory and independently
    returned by the live route are completeness sentinels. Additional rows
    are allowed, so newly introduced types are discovered automatically.
    """
    try:
        html = body.decode("utf-8", "strict")
    except (AttributeError, UnicodeDecodeError) as exc:
        raise ValueError("project type directory encoding is invalid") from exc
    lowered = html.lower()
    if "<html" not in lowered or "</html>" not in lowered or "php parse error" in lowered:
        raise ValueError("project type directory document is incomplete")
    parser = _ProjectTypeDirectoryHTML()
    parser.feed(html)
    if parser.pagination:
        raise ValueError("project type directory pagination is not fully acquired")

    candidate_tables: list[list[tuple[str, str]]] = []
    for table in parser.tables:
        pairs: list[tuple[str, str]] = []
        for row in table:
            code_cells = [index for index, cell in enumerate(row) if re.fullmatch(r"\d{4}", cell)]
            if not code_cells:
                continue
            if len(code_cells) != 1 or len(row) != 3 or code_cells[0] != 1:
                raise ValueError("project type directory row structure is ambiguous")
            code_index = code_cells[0]
            if not row[code_index + 1]:
                raise ValueError("project type directory contains an empty type name")
            pairs.append((row[code_index], row[code_index + 1]))
        if pairs:
            candidate_tables.append(pairs)
    if len(candidate_tables) != 1:
        raise ValueError("project type directory table is missing or ambiguous")
    catalog = validate_project_type_catalog(candidate_tables[0])
    if not VERIFIED_DIRECTORY_CODES.issubset(catalog):
        raise ValueError("project type directory does not contain the verified complete catalog")
    return catalog


def acquire_project_type_catalog(session: Any) -> dict[str, str]:
    """Read the verified Portal admin directory without mutating Portal state."""
    status, content_type, body = session.request(
        PROJECT_TYPE_DIRECTORY_PATH, "GET", accept="text/html"
    )
    if status != 200 or "text/html" not in content_type.lower():
        raise RuntimeError("project type directory request failed")
    return parse_project_type_catalog_html(body)


def normalized_period_marker(project_name: Any) -> tuple[int, int] | None:
    """Return the first qualified MMYY marker normalized to (YYYY, MM)."""
    if project_name in (None, ""):
        return None
    match = PERIOD_MARKER_RE.search(str(project_name))
    if not match:
        return None
    month = int(match.group(1))
    year = 2000 + int(match.group(2))
    return year, month


def is_project_type_applicable(project_name: Any) -> bool:
    period = normalized_period_marker(project_name)
    return period is not None and period >= APPLICABILITY_CUTOFF


def project_type_applicable_ids(universe: list[dict[str, Any]]) -> set[str]:
    """Use the current /api/project universe name as the canonical period source."""
    return {
        str(item["project_id"])
        for item in universe
        if is_project_type_applicable(item.get("project_name"))
    }


def canonical_project_names(universe: list[dict[str, Any]]) -> dict[str, str]:
    """Return usable canonical project names from the already-fetched universe."""
    return {
        str(item["project_id"]): str(item["project_name"])
        for item in universe
        if item.get("project_name") not in (None, "")
    }


def apply_canonical_project_names(
    rows: list[dict[str, Any]], universe: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    names = canonical_project_names(universe)
    for row in rows:
        name = names.get(str(row.get("project_id", "")))
        if name is not None:
            row["project_name"] = name
    return rows


def classify_raw_type(
    payload: Any, catalog: dict[str, str] | None = None
) -> tuple[str, str | None, str | None]:
    """Return (bucket, qualified code, display name) without coercing codes."""
    if not isinstance(payload, dict) or "type" not in payload:
        return "MISSING", None, None
    raw = payload["type"]
    if raw is None:
        return "NULL", None, None
    if type(raw) is int and raw == 0:
        return "ZERO", None, None
    if raw == "":
        return "EMPTY", None, None
    active_catalog = catalog if catalog is not None else PROJECT_TYPE_DICTIONARY
    if isinstance(raw, str) and raw in active_catalog:
        return "VALID", raw, active_catalog[raw]
    return "UNKNOWN_CODE", None, None


def validate_state_rows(
    rows: list[list[Any]], *, allow_dynamic_catalog: bool = False
) -> dict[str, tuple[str, str]]:
    if not rows:
        return {}
    if rows[0] != STATE_COLUMNS:
        raise ValueError("project_types header mismatch")
    state: dict[str, tuple[str, str]] = {}
    for row in rows[1:]:
        if not row or row[0] in (None, ""):
            continue
        padded = list(row) + [""] * (len(STATE_COLUMNS) - len(row))
        project_id = str(integer_id(padded[0], "project_id"))
        code, name = (str(padded[i]) if padded[i] is not None else "" for i in (1, 2))
        if project_id in state:
            raise ValueError(f"duplicate project_types project_id: {project_id}")
        if not code or (allow_dynamic_catalog and not re.fullmatch(r"\d{4}", code)):
            raise ValueError(f"invalid persisted project type code for project_id {project_id}")
        if not allow_dynamic_catalog and code not in PROJECT_TYPE_DICTIONARY:
            raise ValueError(f"invalid persisted project type code for project_id {project_id}")
        if not name or (not allow_dynamic_catalog and name != PROJECT_TYPE_DICTIONARY[code]):
            raise ValueError(f"project type name/dictionary mismatch for project_id {project_id}")
        state[project_id] = (code, name)
    return state


def validate_project_type_catalog(rows: list[tuple[Any, Any]]) -> dict[str, str]:
    """Validate a catalog payload without assuming its Portal transport/shape.

    The caller must obtain these code/name pairs from a verified source. Codes
    stay strings so leading zeroes are preserved. Empty, partial, duplicate, or
    contradictory catalogs are rejected rather than treated as authoritative.
    """
    if not isinstance(rows, list) or not rows:
        raise ValueError("project type catalog is empty or malformed")
    catalog: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, (tuple, list)) or len(row) != 2:
            raise ValueError("project type catalog row is malformed")
        code, name = row
        if not isinstance(code, str) or not code.strip() or code != code.strip():
            raise ValueError("project type catalog code is invalid")
        if not re.fullmatch(r"\d{4}", code):
            raise ValueError("project type catalog code format is invalid")
        if not isinstance(name, str) or not name.strip() or name != name.strip():
            raise ValueError("project type catalog name is invalid")
        if code in catalog:
            raise ValueError("project type catalog contains a duplicate code")
        catalog[code] = name
    return catalog


def reconcile_project_type_assignments(
    existing: dict[str, tuple[str, str]],
    observations: list[tuple[Any, Any]],
    expected_project_ids: set[str],
    catalog_rows: list[tuple[Any, Any]],
) -> tuple[dict[str, tuple[str, str]], dict[str, int]]:
    """Apply a complete, catalog-qualified observation set by stable project ID.

    This pure reconciliation layer deliberately knows nothing about Portal
    endpoints. The caller must supply a verified complete catalog and exactly
    one current type-code observation for every expected applicable project.
    """
    catalog = validate_project_type_catalog(catalog_rows)
    expected = {str(integer_id(project_id, "project_id")) for project_id in expected_project_ids}
    observed: dict[str, str] = {}
    for row in observations:
        if not isinstance(row, (tuple, list)) or len(row) != 2:
            raise ValueError("project type observation is malformed")
        raw_project_id, code = row
        project_id = str(integer_id(raw_project_id, "project_id"))
        if project_id in observed:
            raise ValueError("duplicate project type observation")
        if not isinstance(code, str) or not code or code != code.strip():
            raise ValueError("project type observation code is invalid")
        observed[project_id] = code
    if set(observed) != expected:
        raise ValueError("project type observations are incomplete or unexpected")

    updated_state = dict(existing)
    added = changed = unchanged = 0
    for project_id in sorted(expected, key=int):
        code = observed[project_id]
        if code not in catalog:
            raise ValueError("project type code is absent from verified catalog")
        assignment = (code, catalog[code])
        previous = existing.get(project_id)
        if previous is None:
            added += 1
        elif previous != assignment:
            changed += 1
        else:
            unchanged += 1
        updated_state[project_id] = assignment
    return updated_state, {"added": added, "changed": changed, "unchanged": unchanged}


def state_rows_equivalent(left: list[list[Any]], right: list[list[Any]]) -> bool:
    """Compare persisted state semantically while validating both snapshots."""
    try:
        return validate_state_rows(left, allow_dynamic_catalog=True) == validate_state_rows(
            right, allow_dynamic_catalog=True
        )
    except Exception:
        return False


def merge_assignment(
    state: dict[str, tuple[str, str]], project_id: str, code: str, name: str
) -> tuple[dict[str, tuple[str, str]], bool, bool]:
    """Keep valid existing assignments immutable; report conflicts explicitly."""
    existing = state.get(str(project_id))
    if existing:
        return state, False, existing != (code, name)
    if code not in PROJECT_TYPE_DICTIONARY or name != PROJECT_TYPE_DICTIONARY[code]:
        raise ValueError("only dictionary-qualified code/name assignments may be persisted")
    updated = dict(state)
    updated[str(project_id)] = (code, name)
    return updated, True, False


def serialize_state(state: dict[str, tuple[str, str]]) -> list[list[Any]]:
    return [STATE_COLUMNS] + [
        [integer_id(project_id, "project_id"), code, name]
        for project_id, (code, name) in sorted(state.items(), key=lambda item: int(item[0]))
    ]


def materialize_project_types(
    rows: list[dict[str, Any]],
    state: dict[str, tuple[str, str]],
    applicable_ids: set[str],
) -> list[dict[str, Any]]:
    for row in rows:
        project_id = str(row.get("project_id", ""))
        assignment = state.get(project_id)
        if assignment and project_id not in applicable_ids:
            raise ValueError(f"PERIOD_APPLICABILITY_CONFLICT: project_id={project_id}")
        row["project_type_code"] = assignment[0] if assignment else None
        row["project_type_name"] = assignment[1] if assignment else None
    return rows


@dataclass
class ProjectTypeTelemetry:
    applicable_projects: int = 0
    already_assigned: int = 0
    pending_before: int = 0
    valid_assignments_acquired: int = 0
    zero_results: int = 0
    null_results: int = 0
    empty_results: int = 0
    missing_results: int = 0
    unknown_code_results: int = 0
    immutable_conflicts: int = 0
    added_assignments: int = 0
    changed_assignments: int = 0
    unchanged_assignments: int = 0
    detail_get_reused: int = 0
    detail_get_additional: int = 0
    request_failures: int = 0
    pending_ids: set[str] = field(default_factory=set)
    request_elapsed_seconds: float = 0.0


def acquire_pending_types(
    session: Any,
    universe: list[dict[str, Any]],
    state: dict[str, tuple[str, str]],
    delay: float = 0.15,
) -> tuple[dict[str, tuple[str, str]], ProjectTypeTelemetry, set[str]]:
    applicable = project_type_applicable_ids(universe)
    telemetry = ProjectTypeTelemetry(
        applicable_projects=len(applicable),
        already_assigned=len(applicable & set(state)),
    )
    pending = sorted(applicable - set(state), key=int)
    telemetry.pending_before = len(pending)
    telemetry.pending_ids = set(pending)
    started = time.monotonic()
    next_state = state
    for index, project_id in enumerate(pending, start=1):
        try:
            status, _content_type, body = session.request(
                f"/api/project/{project_id}", "GET", accept="application/json"
            )
            telemetry.detail_get_additional += 1
            if status != 200 or not body:
                telemetry.request_failures += 1
                bucket = "REQUEST_FAILED"
                code = name = None
            else:
                try:
                    import json

                    payload = json.loads(body.decode("utf-8", "replace"), strict=False)
                    bucket, code, name = classify_raw_type(payload)
                except (ValueError, TypeError):
                    bucket, code, name = "MISSING", None, None
        except Exception:
            telemetry.detail_get_additional += 1
            telemetry.request_failures += 1
            bucket, code, name = "REQUEST_FAILED", None, None

        if bucket == "VALID" and code and name:
            next_state, added, conflict = merge_assignment(next_state, project_id, code, name)
            telemetry.valid_assignments_acquired += int(added)
            telemetry.immutable_conflicts += int(conflict)
        elif bucket == "ZERO":
            telemetry.zero_results += 1
        elif bucket == "NULL":
            telemetry.null_results += 1
        elif bucket == "EMPTY":
            telemetry.empty_results += 1
        elif bucket == "MISSING":
            telemetry.missing_results += 1
        elif bucket == "UNKNOWN_CODE":
            telemetry.unknown_code_results += 1

        if index % 10 == 0 or index == len(pending):
            print(
                f"PROJECT_TYPE {index}/{len(pending)} | valid={telemetry.valid_assignments_acquired} | "
                f"pending={index - telemetry.valid_assignments_acquired} | "
                f"http_additional={telemetry.detail_get_additional}",
                flush=True,
            )
        if delay:
            time.sleep(delay)
    telemetry.request_elapsed_seconds = time.monotonic() - started
    return next_state, telemetry, applicable


def acquire_current_types(
    session: Any,
    universe: list[dict[str, Any]],
    existing: dict[str, tuple[str, str]],
    catalog: dict[str, str],
    delay: float = 0.15,
) -> tuple[dict[str, tuple[str, str]], ProjectTypeTelemetry, set[str]]:
    """Refresh every applicable project assignment against the live catalog.

    Any transport, payload, type, or coverage uncertainty aborts before
    publication. Reconciliation is keyed only by project_id and preserves
    non-applicable assignments; payment data is outside this function.
    """
    catalog = validate_project_type_catalog(list(catalog.items()))
    applicable = project_type_applicable_ids(universe)
    telemetry = ProjectTypeTelemetry(
        applicable_projects=len(applicable),
        already_assigned=len(applicable & set(existing)),
        pending_before=len(applicable - set(existing)),
    )
    observations: list[tuple[str, str]] = []
    started = time.monotonic()
    ordered_project_ids = sorted(applicable, key=int)
    project_total = len(ordered_project_ids)
    for index, project_id in enumerate(ordered_project_ids, start=1):
        safe_pid = _safe_project_id(project_id)

        def fail(category: str, failure_stage: str, status: int | str = "UNKNOWN") -> None:
            telemetry.request_failures += 1
            telemetry.request_elapsed_seconds = time.monotonic() - started
            raise ProjectTypeAcquisitionError(
                stage=failure_stage,
                project_index=index,
                project_total=project_total,
                project_id=safe_pid,
                failure_category=category,
                http_status=status,
            ) from None

        try:
            status, _content_type, body = session.request(
                f"/api/project/{project_id}", "GET", accept="application/json"
            )
            telemetry.detail_get_additional += 1
        except Exception as exc:
            if isinstance(exc, HTTPError):
                fail("HTTP_ERROR", "PROJECT_DETAIL_HTTP_STATUS", _safe_http_status(exc.code))
            if isinstance(exc, (PortalTransportError, TimeoutError, ConnectionError, URLError)):
                fail("TRANSPORT_ERROR", "PROJECT_DETAIL_REQUEST")
            fail("INTERNAL_ERROR", "PROJECT_DETAIL_REQUEST")

        http_status = _safe_http_status(status)
        if status != 200:
            fail("HTTP_ERROR", "PROJECT_DETAIL_HTTP_STATUS", http_status)

        if not body:
            fail("UNRESOLVED_TYPE", "PROJECT_DETAIL_RESPONSE", http_status)

        try:
            payload_text = body.decode("utf-8", "strict")
        except UnicodeDecodeError:
            fail("UTF8_ERROR", "PROJECT_DETAIL_UTF8", http_status)
        except Exception:
            fail("INTERNAL_ERROR", "PROJECT_DETAIL_UTF8", http_status)

        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            fail("JSON_ERROR", "PROJECT_DETAIL_JSON", http_status)
        except Exception:
            fail("INTERNAL_ERROR", "PROJECT_DETAIL_JSON", http_status)

        try:
            bucket, code, _name = classify_raw_type(payload, catalog)
        except Exception:
            fail("INTERNAL_ERROR", "PROJECT_DETAIL_TYPE_CLASSIFICATION", http_status)
        if bucket != "VALID" or code is None:
            fail("UNRESOLVED_TYPE", "PROJECT_DETAIL_TYPE_CLASSIFICATION", http_status)
        observations.append((project_id, code))
        if delay:
            time.sleep(delay)

    updated, counts = reconcile_project_type_assignments(
        existing,
        observations,
        applicable,
        list(catalog.items()),
    )
    telemetry.valid_assignments_acquired = counts["added"] + counts["changed"]
    telemetry.added_assignments = counts["added"]
    telemetry.changed_assignments = counts["changed"]
    telemetry.unchanged_assignments = counts["unchanged"]
    telemetry.request_elapsed_seconds = time.monotonic() - started
    return updated, telemetry, applicable
