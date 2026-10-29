# PORTAL-001.3 — DB-backed project dashboard TEST prototype

Status: **STAGING OBJECTS CREATED; SOURCE SNAPSHOT DID NOT COMPLETE; NO ROWS LOADED; DASHBOARD NOT CREATED**.

This stage prepares an isolated project mart for the existing
`MariaDB → Analytics PostgreSQL → Yandex DataLens` path. The new isolated
PostgreSQL schema and its objects were created after preflight; no row data
was loaded. No existing PostgreSQL schema, Google Sheets, DataLens object,
Portal data, schedule, or production runtime was changed. The DDL is in
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

The DDL creates one new schema, five base tables, and three views:

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

## Controlled deployment result — 2026-10-29

The server-side full-population read began through the existing configured
MariaDB `watch` connection and the read-only consistent-snapshot reader. It
terminated with the safe exception class `ProjectMetricsError` before the
loader reached PostgreSQL DML. The outer one-off runner intentionally did not
emit the exception message or a traceback, so the precise failing validation
step is **NOT_CAPTURED**. No retry was made.

An independent PostgreSQL readback confirmed all five staging tables contain
zero rows. The one-off staging scripts were removed from the server. Thus the
DDL and grants are present, but the snapshot and dashboard are not.

The DataLens connection detail shows PostgreSQL database `j4b_analytics`,
read-only account `j4b_analytics_reader`, and host
`j4b-analytics.duckdns.org:15432`. DNS resolution on `dfb` maps that host to
the server's external address. The connection was not exercised from DataLens
against the new views; this remains **PARTIAL**, not a read-access PASS.

### Rollback of only the created PostgreSQL objects

No row data or DataLens object exists. If the owner later chooses to remove
this empty staging layer, first verify it is still the only schema with this
name, then execute `DROP SCHEMA staging_project_metrics CASCADE` in a
transaction. That removes only this stage's schema, tables, views, and its
schema-scoped grants. This rollback was prepared but not run.

## DataLens and parity status

- Existing management project/workflow dataset inspected in this stage is
  backed by the Google Sheets materialized layer.
- The current Google workbook remains the control, not an atomic same-time
  parity baseline.
- The existing `БД на ДФБ` PostgreSQL connection exists and is configured for
  the Analytics database; DNS from `dfb` resolves its host to that server.
  DataLens query access to the new views is not verified. No new connection
  was created.
- No new dataset or dashboard was created. There is no test dashboard URL.
- The previously confirmed qualified metric formulas remain assigned = state
  20, executed = state 30, finished = distinct union of 37/40/50. The local
  aggregate model preserves these definitions and raw IDs. Same-snapshot
  parity remains unproven.

## Remaining work

The owner has already authorized creation of the staging objects and a
private test dashboard. The load stopped before PostgreSQL DML due the
uncaptured `ProjectMetricsError`; it must not be retried without first
instrumenting that failure safely and confirming the precise cause. After
that:

1. Capture a safe diagnostic location (exception source file/line/function
   and symbolic source-validation step, never message or row content) during a
   single authorized retry after the failure cause is established.
2. Complete one full read-only source snapshot and load only into the existing
   `staging_project_metrics` schema. Verify all source IDs, row counts,
   aggregate parity against the local candidate, unresolved coordinator
   count, orphan-action preservation, and nullable successful KPI.
3. Exercise the existing DataLens connection against the new views. Only if
   that succeeds, create the separate dataset(s) and private dashboard
   `JFB Portal — Проекты (DB TEST)` using only the staging views.

No additional approval is requested for the already authorized isolated
staging and new private dashboard. No writes to MariaDB, the existing Google
workbook, or production dashboard are authorized. The test dashboard should not claim successful
visits until its exclusion-marker source is qualified. Date filters should
remain unavailable until the epoch and timezone behavior is explicitly
verified for the dashboard contract.

## Verification in this stage

Targeted model tests cover coordinator-state handling, raw plan preservation,
distinct visit/status logic, and the synthetic 8,100-project retention case.
The full test-suite result is recorded in the task completion report. Any
unrelated pre-existing dirty files remain untouched and are not part of this
prototype package.
