# PORTAL-001.3 — Direct MariaDB Project Metrics (local preparation)

Status: **LOCAL MODEL AND SYNTHETIC TESTS READY; LIVE READ-ONLY VALIDATION PARTIAL**.

This work prepares a read-only candidate for the existing
`MariaDB → Analytics PostgreSQL → DataLens` path. It does not connect to either
production database and does not add a publisher, schedule, or DataLens object.
No source rows, credentials, names, or private snapshots are committed.

## Existing-path fit

`src/portal_mariadb_etl.py` is the existing bounded SQL source reader and
Analytics PostgreSQL writer. It reads project IDs, plans/types/epochs, visits,
actions and claims into `raw`; its cohort is supplied explicitly in chunks of
at most 250. It currently omits project/client/user labels and the coordinator
bridge. `src/portal_reference_etl.py` separately maintains the `reference`
labels, but obtains those labels through Portal HTML.

This candidate does not fork the scheduled ETL. The SQL contract in
`sql/portal_project_metrics_source.sql` is a proposed allowlisted extension to
the source-read phase. `src/project_metrics.py` transforms one bounded chunk
in memory. Before connecting it to the existing loader, the owner must approve
where SQL-sourced labels live and whether they replace or reconcile with the
current HTTPS reference source. The existing `raw` contract intentionally
contains technical fields; copying names into `raw` without that decision is
not assumed.

## Grain and keys

| Candidate relation | Grain / key | Main fields |
|---|---|---|
| Project | one row per `project_id` | name, client/manager IDs and labels, raw coordinator IDs, raw plan/type, source epochs |
| Visit | one row per (`project_id`, `visit_id`) | original visit ID and source epochs |
| Current action | one row per (`project_id`, `action_id`) | original action/visit IDs, raw `state_code`, source epochs |
| Project coordinator | one row per (`project_id`, `coordinator_id`) | IDs plus optional label; never joined into visit facts before aggregation |
| Project metrics | one row per `project_id` | distinct visit count, raw plan, distinct status memberships |

Source links are validated as observed ID relationships, not assumed foreign
keys. Selected visit IDs mapping to multiple projects, duplicate source keys,
missing project coverage, or malformed coordinator lists fail closed. Missing
display references retain their IDs and appear only as aggregate QA counters.

## Metric contract

- `total_visits` = distinct source visit IDs, not `proj.visits`.
- `planned_visits` = source plan field; a plan greater than or less than the
  created visit count is reported as a difference, not automatically an error.
- `assigned_visits` = distinct visits with current action state 20.
- `executed_visits_customer` = distinct visits with state 30.
- `finished_visits` = distinct union of visits with state 37, 40, or 50.
- State 39 is retained in action facts and per-state QA, but is excluded from
  `finished_visits`.
- A visit with multiple actions or several qualifying states counts once in
  each relevant state-membership set; it counts once in the finished union.
- This module does not calculate percentages, risk bands, or date filters.
  Source epochs stay raw until date semantics and timezone are approved.

## SQL execution contract

Use only bound IDs from the existing selected cohort; maximum 250 project IDs
per batch. Resolve project rows first, then fetch only distinct referenced
client/user IDs in bounded queries. Run project, visit, action, client, and user
reads in one MariaDB consistent read-only snapshot. Do not join coordinator
rows to visit/action rows. Preserve the existing source acquisition scope and
do not infer a wider universe from these queries.

The exact source schema/query has not been live revalidated in this stage.
Reports dated 2026-10-29 record the relevant columns and read-only discovery,
but also record that the successful MariaDB session had no TLS cipher. Under
the existing secure-transport gate, no live credentials were read and no SQL
was executed here. A verified TLS endpoint or separately approved protected
transport is required before live extraction.

## Parity and test plan

`compare_project_metric_rows` requires unique project IDs, reports cohort
differences, and summarizes changed metric names without returning row values.
Parity must use the same selected project IDs and the same source snapshot
cutoff. Compare counts and manager/client distribution only after the safe
source transport is qualified. Existing Google materialized values remain a
control, not the target definition. The live `projects_current` tab exposes
both legacy and qualified workflow fields; compare `assigned_visits`,
`executed_visits_customer`, and `finished_visits` separately, and do not equate
`questionnaire_filled` with a qualified workflow metric.

Synthetic tests cover unique project/visit/action keys, multi-action visits,
coordinator bridge isolation, status-code inclusion/exclusion, retained IDs,
nullable source epochs, plan-vs-created separation, and parity cohort checks.

### Historical pre-approval control baseline

This observation predates the owner's approval of the existing direct
plaintext connection. The baseline remains useful for describing the Sheet,
but the old “no live source query” status below is superseded by the live
validation section at the end of this document.

The existing `projects_current` Google tab has 8,100 populated, unique project
IDs. Its `last_refreshed` field contains three distinct raw timestamp strings
(`2026-10-19 14:08:26`, `2026-10-27 07:09:43`, `2026-10-29 14:09:58`); the
spreadsheet metadata timezone is `America/Los_Angeles`, and the timestamp
strings do not carry an explicit zone. This is not one coherent source-time
snapshot. The tab also has distinct `project_visit_count` and
`total_project_visits` fields whose aggregates differ; their exact contract
must be chosen from the live chart bindings before declaring visit parity.
Qualified workflow columns are populated for only a subset of project rows;
blank cells are not silently converted to zero. These observations make the
current workbook a useful control, but not a same-cohort/same-time baseline for
the 12-month SQL cohort.

## Not done / deployment boundary

- No full-population parity conclusion was made: 52 source projects contain
  an unqualified coordinator sentinel and the Sheet/source snapshots differ
  in time. See the live validation below.
- No PostgreSQL schema or target was written.
- No Google Sheet, DataLens, Portal, ETL schedule, or production runtime was
  changed.
- No target DDL/migration is proposed as applied. A versioned Analytics schema
  and rollback plan must be approved before integrating the candidate with
  production PostgreSQL.

## Live read-only validation — 2026-10-29

The existing protected `~/.config/j4b/mariadb.env` file was used in memory;
owner and `0600` permissions were checked. Connection identity was confirmed
as `lk.j4b.ru:3306`, database `j4b`, user `watch`; grants were read-only. No
secret values were printed or persisted. MariaDB version was
`10.11.19-MariaDB-0+deb12u1-log`; the final global-count snapshot was
`2026-10-29 20:32:50` in MariaDB `SYSTEM` timezone.

| Source | Exact rows | Distinct primary IDs | Result |
|---|---:|---:|---|
| `proj` | 8,100 | 8,100 | no duplicate project IDs |
| `visit` | 623,271 | 623,271 | no duplicate visit IDs |
| `my` | 656,850 | 656,850 | no duplicate action IDs |

The MariaDB project set and both Google tabs had identical sets of 8,100
unique IDs. The model successfully processed the 8,048 projects whose
`proj.user2` coordinator field could be parsed unambiguously, in bounded
chunks and a MariaDB consistent read-only snapshot at `2026-10-29 20:29:01`
`SYSTEM` time. It retained project, visit and action IDs and kept
project-coordinator rows separate from visit facts. Aggregate results for
that model-eligible scope:

- 576,725 visits; 607,496 actions linked to those visits.
- 3,721 projects with multiple coordinators; 18,656 coordinator links.
- State 20: 2,093 distinct visits; state 30: 415; union of 37/40/50: 500,638;
  state 39: 438, excluded from the finished union.
- 48 projects without visits; 26,997 visits without actions; 46,439 visits
  with multiple actions.
- Missing display references: 219 client references, 40 manager references,
  and 5 coordinator-user references.

The remaining 52 projects have raw `proj.user2` exactly `0`; `user.i=0` does
not exist. The parser fails closed on this value rather than guessing that it
means an empty coordinator list. Those 52 projects remain outside the model
candidate pending confirmation of the source sentinel's meaning. A separate
global relation check found 0 visits without a project, 826 `my` rows without
a matching visit, and 49 projects without a visit entity. The model's action
query joins to `visit`, so orphan actions do not enter its visit metrics.

For the whole source population, counting only actions joined to actual visit
entities, there were 2,988 distinct visits in state 20, 418 in state 30,
529,004 in the distinct union of states 37/40/50, and 812 in state 39.
The status-39 count is separate and is not included in `finished_visits`.

The 426 Google project rows with stored `last_refreshed` value
`2026-10-29 14:09:58` formed a common-ID comparison cohort. That timestamp has
no explicit zone; the spreadsheet timezone is `America/Los_Angeles`. MariaDB
was queried at approximately `20:25:52` server `SYSTEM` time. Therefore the
precise freshness lag is not established, and changing workflow counts are
not classified as formula defects.

| Metric | MariaDB candidate | Google control | Mismatched projects | Sum of absolute differences |
|---|---:|---:|---:|---:|
| Plan (`proj.visits` / `plan`) | 13,113,915 | 13,113,915 | 0 of 409 populated | 0 |
| Distinct visit entities | 19,587 | 19,452 | 13 of 426 | 135 |
| Assigned, state 20 | 1,478 | 1,448 | 51 of 426 | 244 |
| Executed, state 30 | 345 | 361 | 39 of 426 | 134 |
| Finished, union 37/40/50 | 15,503 | 15,436 | 29 of 426 | 81 |
| State 39 | 23 | 39 | 9 of 426 | 16 |

The 17 blank Sheet plan cells in that cohort correspond to source value `0`;
all nonblank plan values match. Across all 629 populated qualified rows,
`projects_current` and `Статусы проектов` agree exactly on distinct visit
count, assigned, executed and finished metrics. Both tabs have the same
8,100 project IDs, while qualified workflow fields are populated on only
629 rows; blanks on the other 7,471 rows must not be silently treated as zero.
In `projects_current`, `project_visit_count` is populated on 629 rows and
`total_project_visits` on 426; the 426 overlapping values are identical,
while 203 rows have only `project_visit_count`. Their whole-tab totals thus
have different coverage and are not directly interchangeable.

The live DataLens page was inspected read-only. The visible project table
contains labels Plan, Free visits, Assigned, Executed, Finished and Successful
visits. Visible filters are Year, Month, Client, Project and Manager, with no
selection shown. The chart-to-dataset binding and exact source expressions
were not exposed in this view; the actual controlling column for each chart
metric remains **NOT_VERIFIED**. The comparison workbook is
[`J4B Portal — DataLens Materialized Layer`](https://docs.google.com/spreadsheets/d/14PRG3HXsZaN4pVwV390PpMMqHupCW5j6RyI6HBzPQR8/edit).

The full repository suite passed: **373 passed, 3 subtests passed** via
`python3 -m pytest -q`. No pipeline code, schema, PostgreSQL, Google Sheet,
DataLens, schedule or production runtime was changed. The candidate is
validated for 8,048 projects and the 426-project comparison slice, but
full-population acceptance remains **PARTIAL** pending the 52 coordinator
sentinel interpretations, exact DataLens binding, and a comparable
same-time source snapshot.

## Final parity qualification — 2026-10-29

This addendum supplements the earlier read-only run. The MariaDB read used a
consistent read-only snapshot at `2026-10-29 20:44:31` server `SYSTEM` time
(`MSK`). MariaDB reported system timezone `MSK` and session timezone
`SYSTEM`. The Google workbook metadata reports `America/Los_Angeles`; its
latest row-level `last_refreshed` string was `2026-10-29 14:09:58` without an
offset. These are asynchronous observations, not a same-snapshot parity
proof.

### Coordinator sentinel

The current full `proj` read returned 8,100 rows and 8,100 unique IDs. The
strict local parser accepts 8,048 rows; 52 rows have `proj.user2` equal to the
literal text `0`. The other observed shapes are NULL (773), empty string
(326), and other nonempty values (6,949). The Portal form exposes `user2[]`
as a multi-select and the existing parser reads selected user IDs; inspected
Portal/ETL code does not establish what stored `0` means. No matching
`user.i=0` record exists, but that alone does not prove `0` means “no
coordinator.” The parser therefore still rejects those 52 rows rather than
converting an unqualified value to empty. Full-population parsing is not
accepted until the sentinel is authoritatively defined.

### Plan field

The Portal field inventory labels `proj.visits` as planned visits, and the
edit form labels the control “Запланировано визитов.” Its intended unit is a
count of planned visits. The field is not a reliable count of created visit
entities: among 8,100 projects it is positive on 6,882, zero on 1,218, has a
maximum of 3,100,000, and exceeds 10,000 on 53 projects. Against actual
`visit` entities, it equals the count on 3,024 projects, exceeds it on 1,370,
and is below it on 3,706. Preserve `proj.visits` as a plan source value; do
not derive it as `plan - created` or silently normalize anomalies. The live
DataLens chart displays “План”, but the exact binding among plan-like dataset
fields was not conclusively exposed, so the chart-level binding remains
unverified.

### DataLens field contract observed

The live `Проекты — реестр` chart is bound to dataset `Датасет ДФБ` in workbook
`JFB Portal — управленческий PoC`. It displays Plan, Free visits, Assigned,
Executed, Finished, Successful visits, project dates, completion, time and
risk fields. Visible filters are Year, Month, Client, Project and Manager;
the chart filter shelf was empty at inspection. The dataset editor showed
these formula expressions:

- Assigned: `SUM([assigned_visits])`
- Executed: `SUM([executed_visits_customer])`
- Finished: `SUM([finished_visits])`
- Successful visits: `SUM([successful_visits_current])`

The dataset contains `workflow_state_39_visits`, but it is not one of the
visible columns in this project registry chart. This does not establish
whether another dashboard chart uses it. The chart description and qualified
ETL contract define Finished as distinct visits in the union of states
37/40/50; status 39 remains separate. Legacy fields (`assigned`,
`questionnaire_filled`) still exist in the dataset, but their use by other
charts was not qualified in this focused review.

### Current plan comparison

MariaDB `proj.visits` contains 6,882 positive values summing to 14,207,219.
The Google `projects_current.plan` column has 6,876 populated cells summing
to 14,205,645 (maximum 3,100,000). The six-cell coverage difference and
aggregate difference of 1,574 are observed, but cannot be classified as a
formula defect because the source reads were not synchronized; the Google tab
also contains multiple `last_refreshed` values. No exact same-time per-project
parity claim is made.

### Reproducible local comparison

`src/project_metrics.py` provides `compare_project_metrics_detailed(...)`.
It requires explicit observation times and timezone labels for both inputs,
joins only on unique `project_id`, and returns both values plus absolute
numeric differences for every changed metric. It also lists IDs present on
only one side and always sets `same_snapshot_proven` to false; it does not
turn independent reads into an atomic snapshot. Names and other descriptive
fields are excluded. Regression tests cover changed values, nullable
comparisons, cohort changes, duplicate IDs and missing metric fields.

The local test suite passed **375 tests and 3 subtests** after this addition.
No production Sheet, Portal data, PostgreSQL, DataLens object, deployment or
schedule was changed. Qualification remains **PARTIAL**: coordinator sentinel
semantics, exact Plan binding and synchronized full-population parity remain
unresolved.
