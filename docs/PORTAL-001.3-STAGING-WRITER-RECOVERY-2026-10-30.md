# PORTAL-001.3 — PostgreSQL staging writer recovery

Date: 2026-10-30

## Outcome

One full MariaDB snapshot was read and committed to the existing
`j4b_analytics.staging_project_metrics` schema. The transaction wrote only the
five approved staging tables. No existing PostgreSQL objects, production data,
roles, grants, schedules, Google Sheets, or DataLens objects were changed.

The historical writer mechanism was recovered on the analytics host
`spb-3-vm-40g1`: SSH alias `j4b-ai-validation-key`, the existing PostgreSQL
container `j4b-analytics-postgres-1`, database `j4b_analytics`, and database
role `j4b_analytics_admin`. That role is a superuser and owns the staging
objects. The separate `j4b_analytics_loader` role does not have staging write
privileges. No role or privilege changes were made. The one-off loader
statically allowlisted `COPY` destinations to the five tables in
`staging_project_metrics`, rechecked the schema and emptiness, locked those
tables, and performed the writes in one transaction. Thus the operation was
script-scoped to staging; the database role itself is not privilege-scoped to
staging.

The snapshot manifest and independently read table counts agree:

| Object | Rows |
| --- | ---: |
| Projects | 8,104 |
| Project coordinator links | 18,660 |
| Visits | 623,397 |
| Actions, including orphan actions | 657,140 |
| Orphan actions | 826 |
| Project metrics view | 8,104 |

The snapshot metadata records 8,104 selected/acquired projects, 623,397 source
visits, 657,140 source actions, and a complete acquisition. There are 52
projects marked `UNRESOLVED`, 53 extreme-plan flags, and all 8,104 rows retain
`successful_visits_current IS NULL`.

Primary keys are present on project, visit-composite, action, coordinator-link,
and snapshot-run grains. The status-code distribution read from the loaded raw
action field was:

| Status | Actions |
| ---: | ---: |
| 0 | 4 |
| 5 | 480 |
| 10 | 6,655 |
| 15 | 72,759 |
| 20 | 3,465 |
| 25 | 35,039 |
| 30 | 535 |
| 35 | 911 |
| 37 | 1,246 |
| 38 | 140 |
| 39 | 808 |
| 40 | 52,243 |
| 45 | 7,012 |
| 50 | 475,843 |

## Readback failure and limits

The loader's transaction committed. Its subsequent aggregate readback then
failed when a query containing `COUNT(DISTINCT visit_id)` exceeded the
PostgreSQL `temp_file_limit` of 32 MB. Repeating the same read-only aggregate
confirmed that server limit. The loader's outer error handling reported only
`RuntimeError`; it did not expose the SQL diagnostic. Independent bounded
queries verified table counts, the manifest, the project-metrics view count,
the NULL contract, unresolved and extreme-plan counts, raw status counts, and
the presence of primary-key constraints. The reader role successfully read
`project_metrics` (8,104 rows), `data_quality_status` (8,104 rows), and a
project-filtered result from `visit_action_drilldown`.

The full unfiltered drilldown count and exact source-to-target ID-set digest
were not independently completed: the post-commit check aborted before those
comparisons, and the in-memory source candidate was not persisted. Therefore
the snapshot is fully loaded and aggregate-checked, but full ID-level parity
remains unproven. Do not rerun the MariaDB snapshot just to repair this
readback. Any follow-up should use bounded/index-aware validation that avoids
large distinct sorts under the current temporary-file limit.

The DataLens browser session had expired. Although the database reader role
can read the staging views, the actual existing DataLens connection target
was not verified in the UI, and no dataset or dashboard was created. Reopen an
authorized DataLens session, verify the existing `БД на ДФБ` connection points
to this PostgreSQL instance, then continue with new private test objects only.

## Verification summary

```ini
WRITER_PATH=EXISTING_ANALYTICS_HOST_AND_CONTAINER
DATABASE_IDENTITY=j4b_analytics / j4b_analytics_admin
ROLE_PRIVILEGE_SCOPE=SUPERUSER; OPERATION_SQL_ALLOWLIST=STAGING_ONLY
STAGING_EMPTY_BEFORE_LOAD=YES
SNAPSHOT_ATTEMPTS=1
SNAPSHOT_PROJECTS=8104
PROJECTS_LOADED=8104
VISITS_LOADED=623397
ACTIONS_LOADED=657140
POSTGRES_AGGREGATE_QA=PARTIAL
READER_ROLE_VIEW_ACCESS=PASS
DATALENS_UI_READBACK=BLOCKED_SESSION_EXPIRED
DATALENS_TEST_OBJECTS=NOT_CREATED
PRODUCTION_OR_EXISTING_OBJECTS_CHANGED=NO
```
