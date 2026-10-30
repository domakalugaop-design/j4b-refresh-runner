from __future__ import annotations

import pytest

from src.project_metrics import (
    FINISHED_STATUS_CODES,
    ProjectMetricsError,
    action_select_sql,
    build_project_metrics_candidate,
    compare_project_metric_rows,
    compare_project_metrics_detailed,
    merge_project_metrics_candidates,
    parse_coordinator_field,
    parse_coordinator_ids,
    project_distribution_counts,
    project_select_sql,
    read_project_metrics_chunk,
    read_project_metrics_population,
    user_select_sql,
    visit_select_sql,
)


def sample_rows():
    projects = [{
        "project_id": 8314,
        "project_name": "Synthetic project",
        "client_id": 10,
        "manager_id": 20,
        "coordinator_ids_raw": "21, 22;23",
        "plan_visits": 2,
        "project_type_code": 4,
        "project_created_epoch": 100,
        "date_from_epoch": 200,
        "date_to_epoch": 300,
    }]
    visits = [
        {"project_id": 8314, "visit_id": 501, "visit_epoch": 1000, "visit_start_epoch": 1010, "visit_end_epoch": 1020},
        {"project_id": 8314, "visit_id": 502, "visit_epoch": 1001, "visit_start_epoch": None, "visit_end_epoch": None},
        {"project_id": 8314, "visit_id": 503, "visit_epoch": 1002, "visit_start_epoch": None, "visit_end_epoch": None},
    ]
    actions = [
        {"project_id": 8314, "action_id": 601, "visit_id": 501, "state_code": 20, "action_epoch": 1, "action_visit_epoch": 2},
        {"project_id": 8314, "action_id": 602, "visit_id": 501, "state_code": 30, "action_epoch": 3, "action_visit_epoch": 4},
        {"project_id": 8314, "action_id": 603, "visit_id": 502, "state_code": 37, "action_epoch": 5, "action_visit_epoch": 6},
        {"project_id": 8314, "action_id": 604, "visit_id": 502, "state_code": 40, "action_epoch": 7, "action_visit_epoch": 8},
        {"project_id": 8314, "action_id": 605, "visit_id": 502, "state_code": 50, "action_epoch": 9, "action_visit_epoch": 10},
        {"project_id": 8314, "action_id": 606, "visit_id": 503, "state_code": 39, "action_epoch": 11, "action_visit_epoch": 12},
    ]
    clients = [{"client_id": 10, "client_name": "Synthetic client"}]
    users = [
        {"user_id": 20, "name_first": "Manager", "name_middle": "M", "name_last": "One"},
        {"user_id": 21, "name_first": "Coord", "name_middle": None, "name_last": "One"},
        {"user_id": 22, "name_first": "Coord", "name_middle": None, "name_last": "Two"},
        {"user_id": 23, "name_first": "Coord", "name_middle": None, "name_last": "Three"},
    ]
    return projects, visits, actions, clients, users


def build(*overrides):
    rows = list(sample_rows())
    for index, value in enumerate(overrides):
        rows[index] = value
    return build_project_metrics_candidate((8314,), *rows)


def test_sql_contract_is_allowlisted_bounded_and_preserves_source_ids():
    assert "p.name AS project_name" in project_select_sql(2)
    assert "p.user2 AS coordinator_ids_raw" in project_select_sql(2)
    assert "v.i AS visit_id" in visit_select_sql(2)
    assert "m.i AS action_id" in action_select_sql(2)
    assert "m.state AS state_code" in action_select_sql(2)
    assert "SELECT *" not in action_select_sql(2)
    assert "u.i AS user_id" in user_select_sql(2)
    with pytest.raises(ProjectMetricsError):
        project_select_sql(251)


def test_coordinator_ids_parse_as_a_distinct_bridge_and_reject_unknown_format():
    assert parse_coordinator_ids("21, 22;23") == (21, 22, 23)
    assert parse_coordinator_ids(None) == ()
    assert parse_coordinator_ids(" ") == ()
    with pytest.raises(ProjectMetricsError):
        parse_coordinator_ids("21/name")
    with pytest.raises(ProjectMetricsError):
        parse_coordinator_ids("21,21")
    assert parse_coordinator_field(None) == ((), "EMPTY")
    assert parse_coordinator_field("21;22") == ((21, 22), "RESOLVED")
    assert parse_coordinator_field("0") == ((), "UNRESOLVED")
    assert parse_coordinator_field("21/bad") == ((), "UNRESOLVED")


def test_project_metrics_use_distinct_visits_and_do_not_fan_out_for_actions_or_coordinators():
    candidate = build()
    assert candidate.qa == {
        "projects": 1,
        "visits": 3,
        "actions": 6,
        "coordinator_links": 3,
        "projects_missing_client_label": 0,
        "projects_missing_manager_label": 0,
        "coordinator_links_missing_user_label": 0,
        "projects_with_empty_coordinators": 0,
        "projects_with_unresolved_coordinators": 0,
        "projects_with_extreme_plans": 0,
    }
    metric = candidate.metrics[0]
    assert metric["project_id"] == 8314
    assert metric["total_visits"] == 3
    assert metric["assigned_visits"] == 1
    assert metric["executed_visits_customer"] == 1
    assert metric["finished_visits"] == 1
    assert metric["workflow_state_visit_counts"][37] == 1
    assert metric["workflow_state_visit_counts"][40] == 1
    assert metric["workflow_state_visit_counts"][50] == 1
    assert len(candidate.visits) == 3
    assert len(candidate.project_coordinators) == 3
    assert {row["visit_id"] for row in candidate.visits} == {501, 502, 503}
    assert candidate.projects[0]["coordinator_ids"] == (21, 22, 23)



def test_unknown_coordinator_sentinel_preserves_project_and_visit_metrics():
    projects, visits, actions, clients, users = sample_rows()
    projects[0] = {**projects[0], "coordinator_ids_raw": "0"}
    candidate = build(projects, visits, actions, clients, users)
    project = candidate.projects[0]
    assert candidate.qa["projects"] == 1
    assert candidate.qa["visits"] == 3
    assert candidate.qa["projects_with_unresolved_coordinators"] == 1
    assert candidate.qa["projects_with_empty_coordinators"] == 0
    assert project["coordinator_ids_raw"] == "0"
    assert project["coordinator_ids"] == ()
    assert project["coordinator_state"] == "UNRESOLVED"
    assert candidate.metrics[0]["total_visits"] == 3
    assert candidate.metrics[0]["finished_visits"] == 1
    assert candidate.project_coordinators == ()


def test_full_population_candidate_keeps_all_projects_with_52_unresolved_sentinels():
    project_ids = tuple(range(1, 8101))
    chunks = []
    for start in range(0, len(project_ids), 250):
        ids = project_ids[start:start + 250]
        projects = [{
            "project_id": project_id,
            "project_name": None,
            "client_id": None,
            "manager_id": None,
            "coordinator_ids_raw": "0" if project_id <= 52 else "",
            "plan_visits": 0,
        } for project_id in ids]
        chunks.append(build_project_metrics_candidate(ids, projects, [], [], [], []))
    candidate = merge_project_metrics_candidates(chunks)
    assert candidate.qa["projects"] == 8100
    assert candidate.qa["projects_with_unresolved_coordinators"] == 52
    assert candidate.qa["projects_with_empty_coordinators"] == 8048
    assert len({row["project_id"] for row in candidate.projects}) == 8100
    assert len(candidate.metrics) == 8100


def test_population_reader_uses_one_read_only_snapshot_and_retains_unresolved_project():
    class Cursor:
        def __init__(self, connection):
            self.connection = connection
            self.description = None
            self.rows = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params=()):
            assert isinstance(params, tuple)
            if sql.startswith("START TRANSACTION"):
                self.connection.started += 1
                return
            if "UTC_TIMESTAMP(6)" in sql:
                columns, rows = ("captured_at_utc", "source_timezone"), [("2026-10-29 00:00:00", "SYSTEM")]
            elif "SELECT p.i AS project_id FROM proj AS p ORDER BY p.i" in sql:
                columns, rows = ("project_id",), [(8314,)]
            elif "FROM proj AS p WHERE" in sql:
                columns = (
                    "project_id", "project_name", "client_id", "manager_id",
                    "coordinator_ids_raw", "plan_visits", "project_type_code",
                    "project_created_epoch", "date_from_epoch", "date_to_epoch",
                )
                rows = [(8314, "Synthetic", None, None, "0", 13, 4, 100, 200, 300)]
            elif "FROM visit AS v WHERE" in sql:
                columns = ("visit_id", "project_id", "visit_epoch", "visit_start_epoch", "visit_end_epoch")
                rows = [(501, 8314, 1000, None, None)]
            elif "FROM `my` AS m INNER JOIN visit" in sql:
                columns = ("action_id", "project_id", "visit_id", "state_code", "action_epoch", "action_visit_epoch")
                rows = [(601, 8314, 501, 40, 1001, 1002)]
            elif "LEFT JOIN visit AS v ON v.i=m.visit WHERE v.i IS NULL" in sql:
                columns = ("action_id", "project_id", "visit_id", "state_code", "action_epoch", "action_visit_epoch")
                rows = [(602, None, 999, 39, 1003, 1004)]
            elif "FROM client AS c" in sql:
                columns, rows = ("client_id", "client_name"), []
            elif "FROM `user` AS u" in sql:
                columns = ("user_id", "name_first", "name_middle", "name_last")
                rows = []
            else:
                raise AssertionError("unexpected source query")
            self.description = tuple((name,) for name in columns)
            self.rows = list(rows)

        def fetchmany(self, size):
            batch, self.rows = self.rows[:size], self.rows[size:]
            return batch

    class Connection:
        def __init__(self):
            self.started = 0
            self.rolled_back = 0

        def cursor(self):
            return Cursor(self)

        def rollback(self):
            self.rolled_back += 1

    connection = Connection()
    population = read_project_metrics_population(connection)
    candidate = population.candidate
    assert connection.started == 1
    assert connection.rolled_back == 1
    assert candidate.qa["projects"] == 1
    assert candidate.qa["projects_with_unresolved_coordinators"] == 1
    assert candidate.projects[0]["coordinator_ids_raw"] == "0"
    assert candidate.metrics[0]["finished_visits"] == 1
    assert population.orphan_actions == ({
        "action_id": 602, "project_id": None, "visit_id": 999,
        "state_code": 39, "action_epoch": 1003, "action_visit_epoch": 1004,
    },)
    assert population.source_timezone == "SYSTEM"


def test_population_reader_bisects_only_row_cap_batch_and_preserves_all_rows(monkeypatch, caplog):
    from src import project_metrics

    class Connection:
        started = 0
        rolled_back = 0

        def cursor(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params=()):
            assert sql.startswith("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
            self.started += 1

        def rollback(self):
            self.rolled_back += 1

    calls = []

    def fetch(_connection, sql, params):
        calls.append((sql, tuple(params)))
        if "UTC_TIMESTAMP(6)" in sql:
            return ({"captured_at_utc": "2026-10-30 00:00:00", "source_timezone": "SYSTEM"},)
        if "SELECT p.i AS project_id FROM proj AS p ORDER BY p.i" in sql:
            return ({"project_id": 1}, {"project_id": 2})
        ids = tuple(params)
        if "FROM proj AS p WHERE" in sql:
            return tuple({
                "project_id": project_id, "project_name": None, "client_id": None,
                "manager_id": None, "coordinator_ids_raw": "", "plan_visits": 0,
                "project_type_code": None, "project_created_epoch": None,
                "date_from_epoch": None, "date_to_epoch": None,
            } for project_id in ids)
        if "FROM visit AS v WHERE" in sql:
            if len(ids) > 1:
                raise ProjectMetricsError(
                    "source entity exceeded the local candidate row cap",
                    code="SOURCE_ENTITY_ROW_CAP", processed_rows=50_001,
                )
            return ({"visit_id": 100 + ids[0], "project_id": ids[0], "visit_epoch": None,
                     "visit_start_epoch": None, "visit_end_epoch": None},)
        if "FROM `my` AS m INNER JOIN visit" in sql:
            if len(ids) > 1:
                raise ProjectMetricsError(
                    "source entity exceeded the local candidate row cap",
                    code="SOURCE_ENTITY_ROW_CAP", processed_rows=50_001,
                )
            return ({"action_id": 200 + ids[0], "project_id": ids[0], "visit_id": 100 + ids[0],
                     "state_code": 40, "action_epoch": None, "action_visit_epoch": None},)
        if "FROM client AS c" in sql or "FROM `user` AS u" in sql:
            return ()
        if "LEFT JOIN visit AS v ON v.i=m.visit WHERE v.i IS NULL" in sql:
            return ()
        raise AssertionError("unexpected source query")

    monkeypatch.setattr(project_metrics, "_fetch_named_rows", fetch)
    connection = Connection()
    population = read_project_metrics_population(connection)

    assert connection.started == connection.rolled_back == 1
    assert population.candidate.qa["projects"] == 2
    assert population.candidate.qa["visits"] == 2
    assert population.candidate.qa["actions"] == 2
    assert {row["project_id"] for row in population.candidate.projects} == {1, 2}
    assert {row["visit_id"] for row in population.candidate.visits} == {101, 102}
    assert {row["action_id"] for row in population.candidate.actions} == {201, 202}
    visit_batch_sizes = [len(params) for sql, params in calls if "FROM visit AS v WHERE" in sql]
    action_batch_sizes = [len(params) for sql, params in calls if "FROM `my` AS m INNER JOIN visit" in sql]
    assert visit_batch_sizes == [2, 1, 1]
    assert action_batch_sizes == [2, 1, 1]
    assert "SOURCE_ENTITY_ROW_CAP" in caplog.text
    assert "Synthetic" not in caplog.text


def test_source_row_cap_error_exposes_only_safe_classification():
    class Cursor:
        description = (("id",),)
        remaining = 51_000

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, _sql, _params):
            pass

        def fetchmany(self, size):
            if self.remaining == 0:
                return []
            count = min(size, self.remaining)
            start = 51_000 - self.remaining
            self.remaining -= count
            return [(index,) for index in range(start, start + count)]

    class Connection:
        def cursor(self):
            return Cursor()

    from src.project_metrics import _fetch_named_rows
    with pytest.raises(ProjectMetricsError) as caught:
        _fetch_named_rows(Connection(), "SELECT id", ())
    assert caught.value.code == "SOURCE_ENTITY_ROW_CAP"
    assert caught.value.processed_rows == 50_001

def test_status_39_is_retained_as_raw_membership_but_excluded_from_finished():
    candidate = build()
    metric = candidate.metrics[0]
    assert FINISHED_STATUS_CODES == {37, 40, 50}
    assert metric["workflow_state_visit_counts"][39] == 1
    assert metric["finished_visits"] == 1
    assert candidate.actions[-1]["state_code"] == 39


def test_plan_and_created_visits_are_independent_source_values():
    candidate = build()
    assert candidate.metrics[0]["planned_visits"] == 2
    assert candidate.metrics[0]["total_visits"] == 3
    assert candidate.projects[0]["planned_visits"] == 2
    assert candidate.projects[0]["plan_visits_raw"] == 2
    assert candidate.projects[0]["plan_extreme_gt_10000"] is False



def test_extreme_plan_is_preserved_and_flagged_without_normalization():
    projects, visits, actions, clients, users = sample_rows()
    projects[0] = {**projects[0], "plan_visits": 3_100_000}
    candidate = build(projects, visits, actions, clients, users)
    assert candidate.projects[0]["plan_visits_raw"] == 3_100_000
    assert candidate.projects[0]["planned_visits"] == 3_100_000
    assert candidate.projects[0]["plan_extreme_gt_10000"] is True
    assert candidate.qa["projects_with_extreme_plans"] == 1

def test_raw_ids_epochs_and_nullable_dates_are_preserved():
    candidate = build()
    assert candidate.projects[0]["project_id"] == 8314
    assert candidate.projects[0]["date_from_epoch"] == 200
    assert candidate.projects[0]["date_to_epoch"] == 300
    assert candidate.visits[1]["visit_start_epoch"] is None
    assert candidate.actions[0]["action_id"] == 601
    assert candidate.actions[0]["state_code"] == 20


def test_duplicate_project_visit_action_and_reference_keys_fail_closed():
    projects, visits, actions, clients, users = sample_rows()
    with pytest.raises(ProjectMetricsError, match="duplicate project source"):
        build(projects + projects)
    with pytest.raises(ProjectMetricsError, match="duplicate project-visit"):
        build(projects, visits + visits[:1])
    with pytest.raises(ProjectMetricsError, match="duplicate project-action"):
        build(projects, visits, actions + actions[:1])
    with pytest.raises(ProjectMetricsError, match="duplicate reference"):
        build(projects, visits, actions, clients + clients, users)


def test_visit_id_cannot_map_to_multiple_selected_projects():
    projects, visits, actions, clients, users = sample_rows()
    second_project = {**projects[0], "project_id": 8315, "coordinator_ids_raw": ""}
    second_visit = {"project_id": 8315, "visit_id": 501, "visit_epoch": None,
                    "visit_start_epoch": None, "visit_end_epoch": None}
    with pytest.raises(ProjectMetricsError, match="multiple selected projects"):
        build_project_metrics_candidate((8314, 8315), projects + [second_project],
                                         visits + [second_visit], actions, clients, users)


def test_actions_cannot_multiply_or_orphan_visit_facts():
    projects, visits, actions, clients, users = sample_rows()
    actions = actions + [{**actions[0], "action_id": 607}]
    candidate = build(projects, visits, actions, clients, users)
    assert candidate.qa["actions"] == 7
    assert candidate.qa["visits"] == 3
    actions[-1] = {**actions[-1], "visit_id": 999}
    with pytest.raises(ProjectMetricsError, match="outside the selected cohort"):
        build(projects, visits, actions, clients, users)


def test_missing_reference_names_keep_ids_and_are_reported_as_aggregate_qa():
    projects, visits, actions, clients, users = sample_rows()
    candidate = build(projects, visits, actions, [], users[:1])
    assert candidate.projects[0]["client_id"] == 10
    assert candidate.projects[0]["client_name"] is None
    assert candidate.projects[0]["manager_id"] == 20
    assert candidate.projects[0]["manager_name"] == "Manager M One"
    assert candidate.qa["projects_missing_client_label"] == 1
    assert candidate.qa["coordinator_links_missing_user_label"] == 3


def test_parity_summary_requires_same_cohort_and_reports_only_metric_names():
    baseline = [{
        "project_id": 8314, "total_visits": 3, "planned_visits": 2,
        "assigned_visits": 1, "executed_visits_customer": 1, "finished_visits": 1,
    }]
    candidate = [dict(baseline[0], finished_visits=2)]
    result = compare_project_metric_rows(baseline, candidate)
    assert result["same_cohort"] is True
    assert result["changed_project_count"] == 1
    assert result["changed_metric_names"] == ("finished_visits",)
    assert "changed_project_ids" not in result


def test_parity_cohort_mismatch_is_explicit():
    baseline = [{"project_id": 1}]
    candidate = [{"project_id": 2}]
    result = compare_project_metric_rows(baseline, candidate)
    assert result["same_cohort"] is False
    assert result["missing_project_ids"] == (1,)
    assert result["extra_project_ids"] == (2,)


def test_detailed_parity_reports_values_absolute_differences_and_read_times():
    source = [
        {"project_id": 1, "planned_visits": 12, "finished_visits": None},
        {"project_id": 2, "planned_visits": 5, "finished_visits": 2},
    ]
    sheet = [
        {"project_id": 1, "planned_visits": 9, "finished_visits": 1},
        {"project_id": 3, "planned_visits": 7, "finished_visits": 0},
    ]
    result = compare_project_metrics_detailed(
        source, sheet, metric_fields=("planned_visits", "finished_visits"),
        source_observed_at="2026-10-29T20:44:31",
        source_timezone="MSK",
        sheet_observed_at="2026-10-29 14:09:58",
        sheet_timezone="America/Los_Angeles spreadsheet timezone; raw timestamp unzoned",
    )
    assert result["same_snapshot_proven"] is False
    assert result["common_project_count"] == 1
    assert result["source_only_project_ids"] == (2,)
    assert result["sheet_only_project_ids"] == (3,)
    assert result["differences"] == (
        {"project_id": 1, "field": "planned_visits", "source_value": 12,
         "sheet_value": 9, "absolute_difference": 3},
        {"project_id": 1, "field": "finished_visits", "source_value": None,
         "sheet_value": 1, "absolute_difference": None},
    )


def test_detailed_parity_rejects_duplicate_ids_and_missing_metric_fields():
    source = [{"project_id": 1, "planned_visits": 2}]
    args = dict(metric_fields=("planned_visits",), source_observed_at="t1",
                source_timezone="MSK", sheet_observed_at="t2", sheet_timezone="UTC")
    with pytest.raises(ProjectMetricsError, match="duplicate comparison"):
        compare_project_metrics_detailed(source + source, source, **args)
    with pytest.raises(ProjectMetricsError, match="metric field is missing"):
        compare_project_metrics_detailed(source, [{"project_id": 1}], **args)


class _ReadOnlyCursor:
    def __init__(self, connection):
        self.connection = connection
        self.description = ()
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=()):
        self.connection.statements.append(sql)
        lowered = sql.lower()
        if lowered.startswith("start transaction"):
            self.description, self.rows = (), []
            return
        if "from proj as p" in lowered:
            rows = self.connection.data["projects"]
        elif "from visit as v" in lowered:
            rows = self.connection.data["visits"]
        elif "from `my` as m" in lowered:
            rows = self.connection.data["actions"]
        elif "from client as c" in lowered:
            rows = self.connection.data["clients"]
        elif "from `user` as u" in lowered:
            rows = self.connection.data["users"]
        else:
            raise AssertionError("unexpected test SQL shape")
        self.description = tuple((key,) for key in rows[0]) if rows else ()
        self.rows = [tuple(row[key] for key in rows[0]) for row in rows] if rows else []

    def fetchmany(self, size):
        batch, self.rows = self.rows[:size], self.rows[size:]
        return batch


class _ReadOnlyConnection:
    def __init__(self):
        projects, visits, actions, clients, users = sample_rows()
        self.data = {"projects": projects, "visits": visits, "actions": actions,
                     "clients": clients, "users": users}
        self.statements = []
        self.rollbacks = 0

    def cursor(self):
        return _ReadOnlyCursor(self)

    def rollback(self):
        self.rollbacks += 1


def test_local_source_reader_uses_one_readonly_snapshot_and_only_selects():
    connection = _ReadOnlyConnection()
    candidate = read_project_metrics_chunk(connection, (8314,))
    assert candidate.metrics[0]["finished_visits"] == 1
    assert connection.statements[0] == "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY"
    assert all(statement.lstrip().lower().startswith(("start transaction", "select"))
               for statement in connection.statements)
    assert connection.rollbacks == 1


def test_local_source_reader_closes_snapshot_when_validation_fails():
    connection = _ReadOnlyConnection()
    connection.data["visits"].append(dict(connection.data["visits"][0]))
    with pytest.raises(ProjectMetricsError, match="duplicate project-visit"):
        read_project_metrics_chunk(connection, (8314,))
    assert connection.rollbacks == 1


def test_merge_rechecks_keys_across_chunks_and_keeps_facts_separate():
    first = build()
    with pytest.raises(ProjectMetricsError, match="duplicate global project key"):
        merge_project_metrics_candidates((first, first))

    projects, visits, actions, clients, users = sample_rows()
    second_project = {**projects[0], "project_id": 8315, "coordinator_ids_raw": ""}
    second_visits = [{**row, "project_id": 8315, "visit_id": row["visit_id"] + 1000} for row in visits]
    second_actions = [{**row, "project_id": 8315, "visit_id": row["visit_id"] + 1000,
                       "action_id": row["action_id"] + 1000} for row in actions]
    second = build_project_metrics_candidate((8315,), [second_project], second_visits,
                                             second_actions, clients, users)
    merged = merge_project_metrics_candidates((first, second))
    assert merged.qa["projects"] == 2
    assert merged.qa["visits"] == 6
    assert len(merged.metrics) == 2
    distribution = project_distribution_counts(merged)
    assert distribution["by_client_id"] == ({"client_id": 10, "project_count": 2},)
    assert distribution["by_manager_id"] == ({"manager_id": 20, "project_count": 2},)
