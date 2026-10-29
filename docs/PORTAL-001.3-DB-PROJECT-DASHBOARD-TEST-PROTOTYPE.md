# PORTAL-001.3 — DB-backed project dashboard TEST prototype

Status: **LOCAL MODEL AND STAGING PACKAGE PREPARED; LIVE DASHBOARD BLOCKED AT THE OWNER-APPROVAL WRITE BOUNDARY**.

This stage prepares an isolated project mart contract for the existing
`MariaDB → Analytics PostgreSQL → Yandex DataLens` path. No PostgreSQL,
Google Sheets, DataLens, Portal, schedule, or production runtime was changed.
The proposed PostgreSQL DDL is in
[`../sql/project_metrics_staging.sql`](../sql/project_metrics_staging.sql)
and has **not** been applied.

## Local model changes

- A project with an unsupported non-empty coordinator value is retained. The
  source value is preserved as `coordinator_ids_raw`, and the quality state is
  `UNRESOLVED`; no unknown value is guessed to mean “no coordinator”.
- Empty and parseable coordinator values are explicitly classified as
  `EMPTY` and `RESOLVED`.
- The raw plan value is retained. Values above 10,000 are flagged for review;
  the flag is triage metadata and does not normalize or alter the plan.
- Synthetic full-population regression covers 8,100 project IDs, including 52
  unresolved sentinels, and checks that all projects remain in the candidate.
- These tests validate model behavior. They do not claim a new 8,100-row live
  extraction in this stage.

## Proposed isolated mart

The unapplied DDL creates one new schema, five base tables, and three views:

1. `snapshot_runs` — source snapshot time/timezone, scope, counts, completeness,
   and model version.
2. `projects` — one row per `(run_id, project_id)`, preserving IDs, names,
   dimensions, raw coordinator value/state, raw and parsed plan, plan review
   flag, type, and date epochs.
3. `project_coordinators` — separate project/coordinator bridge.
4. `visits` — one row per `(run_id, project_id, visit_id)`.
5. `actions` — one row per `(run_id, action_id)`, retaining visit ID and
   original status code. An action whose visit is absent remains in this table
   with `project_id IS NULL` and `action_link_state='ORPHAN_VISIT'`; it cannot
   affect project metrics.
6. `project_metrics` — distinct visit counts for created, state 20 assigned,
   state 30 executed, and the 37/40/50 finished union; state 39 stays separate.
7. `visit_action_drilldown` — visit and action IDs/statuses for DASHBOARD-REQ-01.
8. `data_quality_status` — coordinator and plan review flags at project grain.

The aggregate view calculates project visits and linked actions in separate
grouped subqueries to prevent fanout. The drilldown includes orphan actions
with their source IDs/statuses and an explicit link state. `successful_visits_current` is deliberately
`NULL`/`NOT_AVAILABLE` because the qualified MariaDB source does not contain
the `UNASSIGNED_FREE` marker needed for the existing customer-facing
definition. No status 39 is counted as finished.

## Existing Analytics PostgreSQL — read-only inventory

The existing database is `j4b_analytics`. It already has `raw`, `reference`,
`publish`, `analytics`, and `ops` schemas. The scheduled SQL ETL currently
maintains a rolling project cohort, not the full 8,100-project population.
At the read-only inventory point during this stage it contained:

| Relation | Rows |
|---|---:|
| `raw.portal_mariadb_proj` | 2,470 |
| `raw.portal_mariadb_visit` | 131,440 |
| `raw.portal_mariadb_my` | 140,215 |
| `reference.project_references` | 2,470 |
| `reference.project_coordinators` | 5,695 |

The raw project relation does not currently include project names, client or
manager labels, or raw `user2`; the live reference project relation also
lacks `project_name`. Thus the existing rolling ETL cannot, as currently
observed, directly supply the requested full registry and unresolved raw
coordinator evidence. A separate approved staging snapshot would need to
carry those fields without changing existing ETL contracts.

The existing PostgreSQL connection object in DataLens is of type PostgreSQL
and uses database `j4b_analytics` and the read-only account, but the browser
inspection did not establish that its configured endpoint resolves to this
same Analytics PostgreSQL instance. That identity must be proven before a new
dataset can be built on it.

## DataLens and parity status

- Existing management project/workflow dataset inspected in this stage is
  backed by the Google Sheets materialized layer.
- The current Google workbook remains the control, not an atomic same-time
  parity baseline.
- The existing `БД на ДФБ` PostgreSQL connection exists, but its endpoint-to-
  server identity was not established. No new connection was created.
- No new dataset or dashboard was created. There is no test dashboard URL.
- The previously confirmed qualified metric formulas remain assigned = state
  20, executed = state 30, finished = distinct union of 37/40/50. The local
  aggregate model preserves these definitions and raw IDs. Same-snapshot
  parity remains unproven.

## Exact approval boundary

To continue to a working prototype, authorize these writes explicitly:

1. In the existing `j4b_analytics` PostgreSQL database, create only the new
   `staging_project_metrics` schema, its five base tables and three views from
   `sql/project_metrics_staging.sql`, then load one complete, read-only-source
   MariaDB snapshot into that schema. This transfers project/client/employee
   labels and all project/visit/action IDs, including unlinked actions, into
   the isolated test schema. Grant `j4b_analytics_reader` schema usage and
   SELECT on only the three staging views so the existing DataLens connection
   can read the test data. Do not
   alter any existing schema, ETL, service, timer, or `publish.*` object.
2. After verifying which DataLens PostgreSQL connection reaches this database,
   create a separate test dataset and a private dashboard named
   `JFB Portal — Проекты (DB TEST)` using only the new staging views. Do not
   alter existing DataLens objects.

No approval is requested to write to MariaDB, the existing Google workbook,
or production dashboard. The test dashboard should not claim successful
visits until its exclusion-marker source is qualified. Date filters should
remain unavailable until the epoch and timezone behavior is explicitly
verified for the dashboard contract.

## Verification in this stage

Targeted model tests cover coordinator-state handling, raw plan preservation,
distinct visit/status logic, and the synthetic 8,100-project retention case.
The full test-suite result is recorded in the task completion report. Any
unrelated pre-existing dirty files remain untouched and are not part of this
prototype package.
