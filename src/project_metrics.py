"""Local, read-only project-metrics candidate built from allowlisted MariaDB rows.

This module contains no database connection and performs no PostgreSQL writes.
Callers supply an explicit project cohort and the result rows from a single
read-only source snapshot. Raw IDs and workflow codes remain available for
reconciliation; project aggregates are built from distinct visit keys rather
than a fan-out join across visits, actions, and coordinator memberships.
"""
from __future__ import annotations

import logging
import re
import time
import traceback
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Sequence, Tuple


MAX_PROJECT_IDS_PER_QUERY = 250
MAX_SOURCE_ROWS_PER_ENTITY = 50_000
FETCH_BATCH_SIZE = 1_000
# Triage threshold based on the observed source distribution; it does not
# change or normalize the business plan value.
PLAN_EXTREME_THRESHOLD = 10_000
FINISHED_STATUS_CODES = frozenset({37, 40, 50})
ASSIGNED_STATUS_CODE = 20
EXECUTED_STATUS_CODE = 30
_LOG = logging.getLogger(__name__)

# Descriptive labels only. Unknown codes remain raw and are not discarded.
WORKFLOW_STATUS_LABELS: Mapping[int, str] = {
    0: "Не назначено",
    5: "Ожидает",
    10: "Новый визит",
    15: "Назначено",
    20: "Подтверждено менеджером",
    25: "На проверке",
    30: "Выполнено пользователем",
    35: "Ожидает проверки",
    37: "Анкета проверена",
    38: "Отклонено",
    39: "Есть претензия",
    40: "Анкета утверждена",
    45: "Возврат",
    50: "Оплачено",
}

_COORDINATOR_DELIMITERS = re.compile(r"[\s,;|]+")
_CANONICAL_ID = re.compile(r"[1-9][0-9]*\Z")


class ProjectMetricsError(ValueError):
    """Safe validation error with no source row values in the message."""

    def __init__(self, message: str, *, code: str | None = None, processed_rows: int | None = None):
        super().__init__(message)
        self.code = code
        self.processed_rows = processed_rows


@contextmanager
def _snapshot_stage(stage: str, progress: Mapping[str, int], *, entity: str, entity_id: str):
    started = time.monotonic()
    try:
        yield
    except Exception as exc:
        origin = traceback.extract_tb(exc.__traceback__)[-1]
        rows = sum(progress.values()) + (getattr(exc, "processed_rows", 0) or 0)
        _LOG.error(
            "project_metrics_snapshot stage=%s outcome=FAIL exception_class=%s error_code=%s "
            "source_file=%s source_line=%d source_function=%s entity=%s entity_id=%s "
            "processed_rows=%d duration_ms=%d",
            stage, type(exc).__name__, getattr(exc, "code", None) or "UNCLASSIFIED",
            Path(origin.filename).name, origin.lineno, origin.name, entity, entity_id,
            rows, int((time.monotonic() - started) * 1000),
        )
        raise
    else:
        _LOG.info(
            "project_metrics_snapshot stage=%s outcome=PASS entity=%s entity_id=%s "
            "processed_rows=%d duration_ms=%d",
            stage, entity, entity_id, sum(progress.values()),
            int((time.monotonic() - started) * 1000),
        )


def _marks(count: int) -> str:
    if isinstance(count, bool) or count < 1 or count > MAX_PROJECT_IDS_PER_QUERY:
        raise ProjectMetricsError("query requires 1..250 explicit project IDs")
    return ",".join(["%s"] * count)


def project_select_sql(project_count: int) -> str:
    marks = _marks(project_count)
    return (
        "SELECT p.i AS project_id, p.name AS project_name, p.client AS client_id, "
        "p.user AS manager_id, p.user2 AS coordinator_ids_raw, "
        "p.visits AS plan_visits, p.typ AS project_type_code, p.dt AS project_created_epoch, "
        "p.dt1 AS date_from_epoch, p.dt2 AS date_to_epoch "
        "FROM proj AS p WHERE p.i IN (" + marks + ")"
    )


def visit_select_sql(project_count: int) -> str:
    marks = _marks(project_count)
    return (
        "SELECT v.i AS visit_id, v.proj AS project_id, v.dt AS visit_epoch, "
        "v.dt1 AS visit_start_epoch, v.dt2 AS visit_end_epoch "
        "FROM visit AS v WHERE v.proj IN (" + marks + ")"
    )


def action_select_sql(project_count: int) -> str:
    marks = _marks(project_count)
    return (
        "SELECT m.i AS action_id, v.proj AS project_id, m.visit AS visit_id, "
        "m.state AS state_code, m.dt AS action_epoch, m.dt_visit AS action_visit_epoch "
        "FROM `my` AS m INNER JOIN visit AS v ON v.i=m.visit "
        "WHERE v.proj IN (" + marks + ")"
    )


def client_select_sql(client_count: int) -> str:
    marks = _marks(client_count)
    return "SELECT c.i AS client_id, c.name AS client_name FROM client AS c WHERE c.i IN (" + marks + ")"


def user_select_sql(user_count: int) -> str:
    marks = _marks(user_count)
    return (
        "SELECT u.i AS user_id, u.namef AS name_first, u.namei AS name_middle, "
        "u.nameo AS name_last FROM `user` AS u WHERE u.i IN (" + marks + ")"
    )


def _chunks(items: Sequence[int], size: int = MAX_PROJECT_IDS_PER_QUERY):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _fetch_named_rows(connection: Any, sql: str, params: Sequence[int]) -> Tuple[Mapping[str, Any], ...]:
    """Execute one fixed SELECT and cap materialized rows without logging them."""
    result = []
    with connection.cursor() as cursor:
        cursor.execute(sql, tuple(params))
        names = tuple(str(column[0]) for column in cursor.description)
        while True:
            remaining_through_cap = MAX_SOURCE_ROWS_PER_ENTITY + 1 - len(result)
            batch = cursor.fetchmany(min(FETCH_BATCH_SIZE, remaining_through_cap))
            if not batch:
                break
            result.extend(dict(zip(names, values)) for values in batch)
            if len(result) > MAX_SOURCE_ROWS_PER_ENTITY:
                raise ProjectMetricsError(
                    "source entity exceeded the local candidate row cap",
                    code="SOURCE_ENTITY_ROW_CAP",
                    processed_rows=len(result),
                )
    return tuple(result)


def _fetch_partitioned_rows(
    connection: Any,
    sql_builder: Any,
    ids: Sequence[int],
    *,
    stage: str,
    entity: str,
) -> Tuple[Mapping[str, Any], ...]:
    """Read a bounded ID set, bisecting only on the explicit row-cap error."""
    selected = tuple(ids)
    if not selected:
        return ()
    label = f"id_count={len(selected)};first={selected[0]};last={selected[-1]}"
    progress: Dict[str, int] = {}
    with _snapshot_stage(stage, progress, entity=entity, entity_id=label):
        try:
            rows = _fetch_named_rows(connection, sql_builder(len(selected)), selected)
            progress["rows"] = len(rows)
            return rows
        except ProjectMetricsError as exc:
            if exc.code != "SOURCE_ENTITY_ROW_CAP" or len(selected) == 1:
                raise
            midpoint = len(selected) // 2
            _LOG.warning(
                "project_metrics_snapshot stage=batch_split outcome=RETRY reason=SOURCE_ENTITY_ROW_CAP "
                "entity=%s parent_id_count=%d left_id_count=%d right_id_count=%d",
                entity, len(selected), midpoint, len(selected) - midpoint,
            )
            return (
                _fetch_partitioned_rows(
                    connection, sql_builder, selected[:midpoint], stage=stage, entity=entity
                )
                + _fetch_partitioned_rows(
                    connection, sql_builder, selected[midpoint:], stage=stage, entity=entity
                )
            )


def read_project_metrics_chunk(connection: Any, project_ids: Sequence[int]) -> "ProjectMetricsCandidate":
    """Read one explicit cohort chunk in a MariaDB read-only snapshot.

    Requires a fresh source connection configured through the repository's
    existing verified transport path. This function issues only fixed SELECTs
    and finishes with ROLLBACK to release the read snapshot; it has no target
    database dependency and performs no writes.
    """
    selected = tuple(_required_int({"id": value}, "id") for value in project_ids)
    if not selected or len(selected) > MAX_PROJECT_IDS_PER_QUERY:
        raise ProjectMetricsError("read requires 1..250 explicit project IDs")
    if len(set(selected)) != len(selected):
        raise ProjectMetricsError("selected project IDs contain duplicates")

    with connection.cursor() as cursor:
        cursor.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
    try:
        projects = _fetch_named_rows(connection, project_select_sql(len(selected)), selected)
        if len(projects) != len(selected):
            raise ProjectMetricsError("source project count does not match selected IDs")
        client_ids = tuple(sorted({
            parsed for row in projects
            if (parsed := _optional_int(row.get("client_id"))) is not None
        }))
        user_ids = set()
        for row in projects:
            manager_id = _optional_int(row.get("manager_id"))
            if manager_id is not None:
                user_ids.add(manager_id)
            coordinator_ids, _ = parse_coordinator_field(row.get("coordinator_ids_raw"))
            user_ids.update(coordinator_ids)

        visits = _fetch_named_rows(connection, visit_select_sql(len(selected)), selected)
        actions = _fetch_named_rows(connection, action_select_sql(len(selected)), selected)
        clients = tuple(
            row for batch in _chunks(client_ids)
            for row in _fetch_named_rows(connection, client_select_sql(len(batch)), batch)
        )
        users = tuple(
            row for batch in _chunks(tuple(sorted(user_ids)))
            for row in _fetch_named_rows(connection, user_select_sql(len(batch)), batch)
        )
        return build_project_metrics_candidate(selected, projects, visits, actions, clients, users)
    finally:
        connection.rollback()


def read_project_metrics_population(connection: Any) -> "ProjectMetricsPopulation":
    """Read the complete current project population in one consistent snapshot.

    Project-dependent queries are still split into at most 250 IDs per batch,
    but every batch shares the same MariaDB read-only transaction. This is for
    a single explicitly approved staging snapshot, not a scheduled publisher.
    """
    started = time.monotonic()
    progress = {"project_ids": 0, "projects": 0, "visits": 0, "actions": 0, "orphan_actions": 0}
    try:
        with connection.cursor() as cursor:
            cursor.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
        metadata_progress: Dict[str, int] = {}
        with _snapshot_stage("snapshot_metadata", metadata_progress, entity="metadata", entity_id="global"):
            snapshot_rows = _fetch_named_rows(
                connection,
                "SELECT UTC_TIMESTAMP(6) AS captured_at_utc, @@system_time_zone AS source_timezone",
                (),
            )
            metadata_progress["rows"] = len(snapshot_rows)
        if len(snapshot_rows) != 1:
            raise ProjectMetricsError("source snapshot metadata is unavailable")
        snapshot_metadata = snapshot_rows[0]
        universe_progress: Dict[str, int] = {}
        with _snapshot_stage("project_universe", universe_progress, entity="project", entity_id="global"):
            id_rows = _fetch_named_rows(
                connection,
                "SELECT p.i AS project_id FROM proj AS p ORDER BY p.i",
                (),
            )
            universe_progress["rows"] = len(id_rows)
        project_ids = tuple(_required_int(row, "project_id") for row in id_rows)
        if not project_ids or len(set(project_ids)) != len(project_ids):
            raise ProjectMetricsError("source project population is empty or contains duplicate IDs")
        progress["project_ids"] = len(project_ids)

        candidates = []
        for batch_index, selected in enumerate(_chunks(project_ids), start=1):
            projects = _fetch_partitioned_rows(
                connection, project_select_sql, selected, stage="project_rows", entity="project"
            )
            visits = _fetch_partitioned_rows(
                connection, visit_select_sql, selected, stage="visit_rows", entity="visit"
            )
            actions = _fetch_partitioned_rows(
                connection, action_select_sql, selected, stage="action_rows", entity="action"
            )
            progress["projects"] += len(projects)
            progress["visits"] += len(visits)
            progress["actions"] += len(actions)
            client_ids = tuple(sorted({
                parsed for row in projects
                if (parsed := _optional_int(row.get("client_id"))) is not None
            }))
            user_ids = set()
            for row in projects:
                manager_id = _optional_int(row.get("manager_id"))
                if manager_id is not None:
                    user_ids.add(manager_id)
                coordinator_ids, _ = parse_coordinator_field(row.get("coordinator_ids_raw"))
                user_ids.update(coordinator_ids)
            clients = tuple(
                row for batch in _chunks(client_ids)
                for row in _fetch_partitioned_rows(
                    connection, client_select_sql, batch, stage="client_rows", entity="client"
                )
            )
            users = tuple(
                row for batch in _chunks(tuple(sorted(user_ids)))
                for row in _fetch_partitioned_rows(
                    connection, user_select_sql, batch, stage="user_rows", entity="user"
                )
            )
            with _snapshot_stage(
                "candidate_build", {"projects": len(projects), "visits": len(visits), "actions": len(actions)},
                entity="project_batch", entity_id=f"batch={batch_index};project_count={len(selected)}",
            ):
                candidates.append(build_project_metrics_candidate(selected, projects, visits, actions, clients, users))

        # Keep source action IDs whose visit does not resolve. They are not
        # assigned to a project and therefore must not affect project metrics.
        orphan_progress: Dict[str, int] = {}
        with _snapshot_stage("orphan_actions", orphan_progress, entity="action", entity_id="unmatched_visit"):
            orphan_actions = _fetch_named_rows(
                connection,
                "SELECT m.i AS action_id, NULL AS project_id, m.visit AS visit_id, "
                "m.state AS state_code, m.dt AS action_epoch, m.dt_visit AS action_visit_epoch "
                "FROM `my` AS m LEFT JOIN visit AS v ON v.i=m.visit WHERE v.i IS NULL",
                (),
            )
            orphan_progress["rows"] = len(orphan_actions)
        progress["orphan_actions"] = len(orphan_actions)
        with _snapshot_stage(
            "candidate_merge", {"candidate_batches": len(candidates)}, entity="project_population", entity_id="global"
        ):
            merged = merge_project_metrics_candidates(candidates)
        population = ProjectMetricsPopulation(
            merged, orphan_actions, snapshot_metadata["captured_at_utc"], str(snapshot_metadata["source_timezone"])
        )
        _LOG.info(
            "project_metrics_snapshot stage=population_snapshot outcome=PASS project_ids=%d projects=%d "
            "visits=%d actions=%d orphan_actions=%d duration_ms=%d",
            progress["project_ids"], progress["projects"], progress["visits"], progress["actions"],
            progress["orphan_actions"], int((time.monotonic() - started) * 1000),
        )
        return population
    except Exception as exc:
        origin = traceback.extract_tb(exc.__traceback__)[-1]
        _LOG.error(
            "project_metrics_snapshot stage=population_snapshot outcome=FAIL exception_class=%s "
            "error_code=%s source_file=%s source_line=%d source_function=%s entity=population entity_id=global "
            "project_ids=%d projects=%d visits=%d actions=%d orphan_actions=%d duration_ms=%d",
            type(exc).__name__, getattr(exc, "code", None) or "UNCLASSIFIED",
            Path(origin.filename).name, origin.lineno, origin.name,
            progress["project_ids"], progress["projects"], progress["visits"], progress["actions"],
            progress["orphan_actions"], int((time.monotonic() - started) * 1000),
        )
        raise
    finally:
        connection.rollback()


def parse_coordinator_ids(value: Any) -> Tuple[int, ...]:
    """Parse the observed numeric ID-list field; reject ambiguous payloads."""
    if value is None or str(value).strip() == "":
        return ()
    text = str(value).strip()
    parts = _COORDINATOR_DELIMITERS.split(text)
    if not parts or any(not _CANONICAL_ID.fullmatch(part) for part in parts):
        raise ProjectMetricsError("coordinator ID list has an unsupported format")
    parsed = tuple(int(part) for part in parts)
    if any(identifier > (2**63 - 1) for identifier in parsed):
        raise ProjectMetricsError("coordinator ID is outside the supported integer range")
    if len(set(parsed)) != len(parsed):
        raise ProjectMetricsError("coordinator ID list contains duplicates")
    return parsed


def parse_coordinator_field(value: Any) -> Tuple[Tuple[int, ...], str]:
    """Parse known IDs while explicitly classifying empty and unknown values.

    Unsupported non-empty values (including ``"0"``) are not guessed to mean
    no coordinator. The caller retains the original source value and may keep
    the project and its visit metrics with an UNRESOLVED quality state.
    """
    if value is None or str(value).strip() == "":
        return (), "EMPTY"
    try:
        return parse_coordinator_ids(value), "RESOLVED"
    except ProjectMetricsError:
        return (), "UNRESOLVED"


def _required_int(row: Mapping[str, Any], key: str) -> int:
    value = row.get(key)
    if isinstance(value, bool) or value is None:
        raise ProjectMetricsError("required identifier is missing or invalid")
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ProjectMetricsError("required identifier is invalid") from None
    if parsed <= 0 or parsed > (2**63 - 1) or str(parsed) != str(value).strip():
        raise ProjectMetricsError("required identifier is not a canonical positive integer")
    return parsed


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ProjectMetricsError("optional numeric source field is invalid")
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ProjectMetricsError("optional numeric source field is invalid") from None
    if parsed < 0 or parsed > (2**63 - 1):
        raise ProjectMetricsError("optional numeric source field is outside the supported range")
    return parsed


def _reference_map(rows: Sequence[Mapping[str, Any]], id_key: str) -> Dict[int, Mapping[str, Any]]:
    result: Dict[int, Mapping[str, Any]] = {}
    for row in rows:
        identifier = _required_int(row, id_key)
        if identifier in result:
            raise ProjectMetricsError("duplicate reference identifier")
        result[identifier] = row
    return result


def _display_name(row: Mapping[str, Any] | None) -> str | None:
    if row is None:
        return None
    parts = [str(row.get(key) or "").strip() for key in ("name_first", "name_middle", "name_last")]
    label = " ".join(part for part in parts if part)
    return label or None


@dataclass(frozen=True)
class ProjectMetricsCandidate:
    projects: Tuple[Mapping[str, Any], ...]
    visits: Tuple[Mapping[str, Any], ...]
    actions: Tuple[Mapping[str, Any], ...]
    project_coordinators: Tuple[Mapping[str, int], ...]
    metrics: Tuple[Mapping[str, Any], ...]
    qa: Mapping[str, int]


@dataclass(frozen=True)
class ProjectMetricsPopulation:
    candidate: ProjectMetricsCandidate
    orphan_actions: Tuple[Mapping[str, Any], ...]
    captured_at_utc: Any
    source_timezone: str


def build_project_metrics_candidate(
    project_ids: Sequence[int],
    project_rows: Sequence[Mapping[str, Any]],
    visit_rows: Sequence[Mapping[str, Any]],
    action_rows: Sequence[Mapping[str, Any]],
    client_rows: Sequence[Mapping[str, Any]],
    user_rows: Sequence[Mapping[str, Any]],
) -> ProjectMetricsCandidate:
    """Build a deterministic in-memory candidate from one explicit cohort.

    Inputs use the aliases returned by the SQL helpers above. The function
    keeps project, visit, action, and coordinator grains separate. It does not
    derive a percentage or reinterpret dates; source epochs and plan values are
    passed through as-is for an owner-approved downstream formula.
    """
    selected = tuple(_required_int({"id": value}, "id") for value in project_ids)
    if not selected or len(selected) > MAX_PROJECT_IDS_PER_QUERY:
        raise ProjectMetricsError("candidate must be built from one bounded explicit cohort chunk")
    if len(set(selected)) != len(selected):
        raise ProjectMetricsError("selected project IDs contain duplicates")

    projects_by_id: Dict[int, Mapping[str, Any]] = {}
    client_map = _reference_map(client_rows, "client_id")
    user_map = _reference_map(user_rows, "user_id")
    for row in project_rows:
        project_id = _required_int(row, "project_id")
        if project_id in projects_by_id:
            raise ProjectMetricsError("duplicate project source ID")
        projects_by_id[project_id] = row
    if set(projects_by_id) != set(selected):
        raise ProjectMetricsError("project rows do not match the explicit selected cohort")

    projects = []
    coordinator_rows = []
    missing_clients = 0
    missing_managers = 0
    missing_coordinators = 0
    unresolved_coordinators = 0
    empty_coordinators = 0
    extreme_plans = 0
    for project_id in selected:
        source = projects_by_id[project_id]
        client_id = _optional_int(source.get("client_id"))
        manager_id = _optional_int(source.get("manager_id"))
        coordinator_raw = source.get("coordinator_ids_raw")
        coordinator_ids, coordinator_state = parse_coordinator_field(coordinator_raw)
        unresolved_coordinators += int(coordinator_state == "UNRESOLVED")
        empty_coordinators += int(coordinator_state == "EMPTY")
        plan_raw = source.get("plan_visits")
        planned_visits = _optional_int(plan_raw)
        plan_extreme = planned_visits is not None and planned_visits > PLAN_EXTREME_THRESHOLD
        extreme_plans += int(plan_extreme)
        client = client_map.get(client_id) if client_id is not None else None
        manager = user_map.get(manager_id) if manager_id is not None else None
        missing_clients += int(client_id is not None and client is None)
        missing_managers += int(manager_id is not None and manager is None)
        projects.append({
            "project_id": project_id,
            "project_name": source.get("project_name"),
            "client_id": client_id,
            "client_name": client.get("client_name") if client else None,
            "manager_id": manager_id,
            "manager_name": _display_name(manager),
            "plan_visits_raw": plan_raw,
            "planned_visits": planned_visits,
            "plan_extreme_gt_10000": plan_extreme,
            "project_type_code": _optional_int(source.get("project_type_code")),
            "project_created_epoch": _optional_int(source.get("project_created_epoch")),
            "date_from_epoch": _optional_int(source.get("date_from_epoch")),
            "date_to_epoch": _optional_int(source.get("date_to_epoch")),
            "coordinator_ids_raw": coordinator_raw,
            "coordinator_ids": coordinator_ids,
            "coordinator_state": coordinator_state,
        })
        for coordinator_id in coordinator_ids:
            coordinator = user_map.get(coordinator_id)
            missing_coordinators += int(coordinator is None)
            coordinator_rows.append({
                "project_id": project_id,
                "coordinator_id": coordinator_id,
                "coordinator_name": _display_name(coordinator),
            })

    visit_by_key: Dict[Tuple[int, int], Mapping[str, Any]] = {}
    visit_owner: Dict[int, int] = {}
    for row in visit_rows:
        project_id = _required_int(row, "project_id")
        visit_id = _required_int(row, "visit_id")
        if project_id not in projects_by_id:
            raise ProjectMetricsError("visit references a project outside the selected cohort")
        key = (project_id, visit_id)
        if key in visit_by_key:
            raise ProjectMetricsError("duplicate project-visit source key")
        if visit_id in visit_owner and visit_owner[visit_id] != project_id:
            raise ProjectMetricsError("visit ID maps to multiple selected projects")
        visit_owner[visit_id] = project_id
        visit_by_key[key] = row

    action_by_key: Dict[Tuple[int, int], Mapping[str, Any]] = {}
    actions = []
    states_by_project_visit: Dict[Tuple[int, int], set[int]] = {}
    for row in action_rows:
        project_id = _required_int(row, "project_id")
        action_id = _required_int(row, "action_id")
        visit_id = _required_int(row, "visit_id")
        if project_id not in projects_by_id:
            raise ProjectMetricsError("action references a project outside the selected cohort")
        if (project_id, visit_id) not in visit_by_key:
            raise ProjectMetricsError("action references a visit outside the selected cohort")
        key = (project_id, action_id)
        if key in action_by_key:
            raise ProjectMetricsError("duplicate project-action source key")
        action_by_key[key] = row
        state_code = _optional_int(row.get("state_code"))
        action = {
            "project_id": project_id,
            "action_id": action_id,
            "visit_id": visit_id,
            "state_code": state_code,
            "action_epoch": _optional_int(row.get("action_epoch")),
            "action_visit_epoch": _optional_int(row.get("action_visit_epoch")),
        }
        actions.append(action)
        if state_code is not None:
            states_by_project_visit.setdefault((project_id, visit_id), set()).add(state_code)

    visit_rows_out = tuple({
        "project_id": project_id,
        "visit_id": visit_id,
        "visit_epoch": _optional_int(row.get("visit_epoch")),
        "visit_start_epoch": _optional_int(row.get("visit_start_epoch")),
        "visit_end_epoch": _optional_int(row.get("visit_end_epoch")),
    } for (project_id, visit_id), row in sorted(visit_by_key.items()))

    metrics = []
    for project_id in selected:
        project_visits = {visit_id for owner, visit_id in visit_by_key if owner == project_id}
        state_visits: Dict[int, set[int]] = {}
        for (owner, visit_id), codes in states_by_project_visit.items():
            if owner == project_id:
                for code in codes:
                    state_visits.setdefault(code, set()).add(visit_id)
        finished = set().union(*(state_visits.get(code, set()) for code in FINISHED_STATUS_CODES))
        metrics.append({
            "project_id": project_id,
            "total_visits": len(project_visits),
            "planned_visits": next(row["planned_visits"] for row in projects if row["project_id"] == project_id),
            "assigned_visits": len(state_visits.get(ASSIGNED_STATUS_CODE, set())),
            "executed_visits_customer": len(state_visits.get(EXECUTED_STATUS_CODE, set())),
            "finished_visits": len(finished),
            "workflow_state_visit_counts": {code: len(ids) for code, ids in sorted(state_visits.items())},
        })

    return ProjectMetricsCandidate(
        projects=tuple(projects),
        visits=visit_rows_out,
        actions=tuple(actions),
        project_coordinators=tuple(coordinator_rows),
        metrics=tuple(metrics),
        qa={
            "projects": len(projects),
            "visits": len(visit_rows_out),
            "actions": len(actions),
            "coordinator_links": len(coordinator_rows),
            "projects_missing_client_label": missing_clients,
            "projects_missing_manager_label": missing_managers,
            "coordinator_links_missing_user_label": missing_coordinators,
            "projects_with_empty_coordinators": empty_coordinators,
            "projects_with_unresolved_coordinators": unresolved_coordinators,
            "projects_with_extreme_plans": extreme_plans,
        },
    )


def compare_project_metric_rows(
    baseline_rows: Sequence[Mapping[str, Any]], candidate_rows: Sequence[Mapping[str, Any]]
) -> Mapping[str, Any]:
    """Compare same-cohort aggregate rows by project ID without returning PII."""
    keys = ("total_visits", "planned_visits", "assigned_visits", "executed_visits_customer", "finished_visits")

    def index(rows: Sequence[Mapping[str, Any]]) -> Dict[int, Mapping[str, Any]]:
        result = {}
        for row in rows:
            project_id = _required_int(row, "project_id")
            if project_id in result:
                raise ProjectMetricsError("duplicate aggregate project ID")
            result[project_id] = row
        return result

    baseline = index(baseline_rows)
    candidate = index(candidate_rows)
    changed = {}
    for project_id in sorted(set(baseline) & set(candidate)):
        different = [key for key in keys if baseline[project_id].get(key) != candidate[project_id].get(key)]
        if different:
            changed[project_id] = tuple(different)
    return {
        "baseline_project_count": len(baseline),
        "candidate_project_count": len(candidate),
        "missing_project_ids": tuple(sorted(set(baseline) - set(candidate))),
        "extra_project_ids": tuple(sorted(set(candidate) - set(baseline))),
        "changed_project_count": len(changed),
        "changed_metric_names": tuple(sorted({key for changed_keys in changed.values() for key in changed_keys})),
        "same_cohort": set(baseline) == set(candidate),
    }


def compare_project_metrics_detailed(
    source_rows: Sequence[Mapping[str, Any]],
    sheet_rows: Sequence[Mapping[str, Any]],
    *,
    metric_fields: Sequence[str] = (
        "total_visits", "planned_visits", "assigned_visits",
        "executed_visits_customer", "finished_visits",
    ),
    source_observed_at: str,
    source_timezone: str,
    sheet_observed_at: str,
    sheet_timezone: str,
) -> Mapping[str, Any]:
    """Return ID-level source/Sheet differences and the supplied read times.

    This is a comparison aid, not proof that two independently read systems
    represent one atomic snapshot. Callers must provide observation times and
    timezone labels; the result always leaves same-snapshot proof false.
    Project names and other descriptive fields are deliberately excluded.
    """
    fields = tuple(metric_fields)
    if not fields or any(not isinstance(field, str) or not field for field in fields):
        raise ProjectMetricsError("comparison requires explicit metric field names")

    def index(rows: Sequence[Mapping[str, Any]]) -> Dict[int, Mapping[str, Any]]:
        result = {}
        for row in rows:
            project_id = _required_int(row, "project_id")
            if project_id in result:
                raise ProjectMetricsError("duplicate comparison project ID")
            if any(field not in row for field in fields):
                raise ProjectMetricsError("comparison metric field is missing")
            result[project_id] = row
        return result

    def decimal_or_none(value: Any) -> Decimal | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            number = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
        return number if number.is_finite() else None

    source = index(source_rows)
    sheet = index(sheet_rows)
    differences = []
    for project_id in sorted(set(source) & set(sheet)):
        for field in fields:
            source_value = source[project_id][field]
            sheet_value = sheet[project_id][field]
            if source_value == sheet_value:
                continue
            source_number = decimal_or_none(source_value)
            sheet_number = decimal_or_none(sheet_value)
            absolute_difference = (
                abs(source_number - sheet_number)
                if source_number is not None and sheet_number is not None
                else None
            )
            differences.append({
                "project_id": project_id,
                "field": field,
                "source_value": source_value,
                "sheet_value": sheet_value,
                "absolute_difference": absolute_difference,
            })

    return {
        "source_observed_at": source_observed_at,
        "source_timezone": source_timezone,
        "sheet_observed_at": sheet_observed_at,
        "sheet_timezone": sheet_timezone,
        "same_snapshot_proven": False,
        "source_project_count": len(source),
        "sheet_project_count": len(sheet),
        "common_project_count": len(set(source) & set(sheet)),
        "source_only_project_ids": tuple(sorted(set(source) - set(sheet))),
        "sheet_only_project_ids": tuple(sorted(set(sheet) - set(source))),
        "differences": tuple(differences),
    }


def merge_project_metrics_candidates(
    candidates: Sequence[ProjectMetricsCandidate],
) -> ProjectMetricsCandidate:
    """Combine bounded chunks while rechecking global key uniqueness."""
    if not candidates:
        raise ProjectMetricsError("at least one chunk candidate is required")
    projects = tuple(row for chunk in candidates for row in chunk.projects)
    visits = tuple(row for chunk in candidates for row in chunk.visits)
    actions = tuple(row for chunk in candidates for row in chunk.actions)
    coordinators = tuple(row for chunk in candidates for row in chunk.project_coordinators)
    metrics = tuple(row for chunk in candidates for row in chunk.metrics)

    def require_unique(keys: Iterable[Tuple[Any, ...]], label: str) -> None:
        observed = set()
        for key in keys:
            if key in observed:
                raise ProjectMetricsError("duplicate global " + label + " key across chunks")
            observed.add(key)

    require_unique(((row["project_id"],) for row in projects), "project")
    require_unique(((row["project_id"], row["visit_id"]) for row in visits), "project-visit")
    require_unique(((row["visit_id"],) for row in visits), "visit ID")
    require_unique(((row["project_id"], row["action_id"]) for row in actions), "project-action")
    require_unique(((row["project_id"], row["coordinator_id"]) for row in coordinators), "coordinator")
    require_unique(((row["project_id"],) for row in metrics), "project metric")
    qa_keys = tuple(candidates[0].qa)
    qa = {key: sum(chunk.qa[key] for chunk in candidates) for key in qa_keys}
    return ProjectMetricsCandidate(projects, visits, actions, coordinators, metrics, qa)


def project_distribution_counts(
    candidate: ProjectMetricsCandidate,
) -> Mapping[str, Tuple[Mapping[str, Any], ...]]:
    """Return project counts by source client/manager IDs, including NULL groups."""
    client_counts = Counter(row["client_id"] for row in candidate.projects)
    manager_counts = Counter(row["manager_id"] for row in candidate.projects)
    return {
        "by_client_id": tuple(
            {"client_id": key, "project_count": count}
            for key, count in sorted(client_counts.items(), key=lambda item: (-1 if item[0] is None else item[0]))
        ),
        "by_manager_id": tuple(
            {"manager_id": key, "project_count": count}
            for key, count in sorted(manager_counts.items(), key=lambda item: (-1 if item[0] is None else item[0]))
        ),
    }
