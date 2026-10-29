# PORTAL-001.3 — Project Metrics Qualification Handoff

**Date:** 2026-10-29

**Status:** LOCAL QUALIFICATION PARTIAL; NOT READY FOR POSTGRES STAGING

## A. Architecture and safety boundary

Target architecture is `MariaDB → Analytics PostgreSQL → Yandex DataLens`.
The existing Google Sheets workbook remains the comparison control until
source definitions and synchronized parity are accepted. This work added a
local MariaDB candidate and read-only comparison support only. Production
PostgreSQL, Google Sheets, DataLens objects, Portal data, deployments and
schedules were not changed.

## B. Results established

- MariaDB source `j4b`: `proj` has 8,100 rows / unique project IDs;
  `visit` has 623,271 rows / unique visit IDs; `my` has 656,850 rows /
  unique action IDs.
- The local candidate keeps project, visit and action grains separate,
  retains source IDs and status codes, and counts distinct visits so multiple
  actions do not multiply visit facts.
- The strict model processed 8,048 projects. It rejected 52 projects whose
  raw `proj.user2` value is `0`; that value's meaning is not established.
- Live DataLens dataset formulas were verified for the project registry:
  - Assigned = `SUM([assigned_visits])`
  - Executed = `SUM([executed_visits_customer])`
  - Finished = `SUM([finished_visits])`
  - Successful visits = `SUM([successful_visits_current])`
- Finished workflow contract remains the unique-visit union of states
  `37/40/50`; state `39` is separate and must not be silently included.
- Local test suite: **375 passed; 3 subtests passed**.
- A detailed project-ID comparison helper records read timestamps/timezones,
  both values and absolute differences, while explicitly refusing to claim
  same-snapshot parity for independent reads.

The implementation and SQL are in `src/project_metrics.py` and
`sql/portal_project_metrics_source.sql`; tests are in
`tests/test_project_metrics.py`. The evidence and qualification details are in
`PORTAL-001.3-DIRECT-MARIADB-PROJECT-METRICS.md`.

## C. Open limitations

1. **Coordinator sentinel:** 52 projects have `proj.user2="0"`. Existing
   Portal/ETL code reviewed so far does not prove whether this means no
   coordinator or something else. Keep fail-closed behavior; do not map it to
   empty without authoritative evidence.
2. **Plan values:** `proj.visits` is labelled as planned visits and is
   intended as a visit count, but extreme values (maximum 3,100,000; 53
   projects above 10,000) remain unexplained. Preserve the source value and
   do not recalculate it from created visits.
3. **DataLens Plan binding:** the registry displays “План”, but the exact
   binding to `plan` versus other plan-like dataset fields is not confirmed.
4. **Parity:** MariaDB and Sheet reads were asynchronous. The Sheet has
   multiple row-level `last_refreshed` values and its timestamp strings have
   no explicit timezone. Observed aggregate differences therefore do not
   prove formula defects or same-snapshot parity.
5. **Finance:** financial values remain dependent on the existing Portal
   XLSX flow; this local project-metrics model does not replace it.
6. **Claims:** claims analytics, including the meaning and coverage of status
   `39`, is a separate qualification stage.
7. Legacy dataset fields such as `assigned` and `questionnaire_filled` still
   exist. Their use in charts outside the inspected project registry has not
   been qualified.

## D. Next actions

1. Obtain authoritative evidence for the stored `proj.user2="0"` sentinel.
2. Trace the origin and intended unit/validity of extreme `proj.visits`
   values without normalizing source data.
3. Verify the exact DataLens field binding for “План”.
4. Plan a comparable source/Sheet read window and run ID-level QA using the
   detailed comparator; report timing and timezone uncertainty explicitly.
5. Resolve the above blockers before preparing an Analytics PostgreSQL
   staging schema or loading a full-population candidate.
6. Require ID-level QA before any DataLens cutover.

## E. Acceptance controls

- **DASHBOARD-REQ-01:** aggregate status metrics must be traceable to source
  project, visit and action IDs and raw status codes.
- Preserve current dashboards until replacement data is accepted.
- No production writes or publication without separate owner authorization.
- Never put credentials, tokens, cookies, private payloads or sensitive
  production records in the repository or this handoff.

## Reproducibility

Run the local suite with `python3 -m pytest -q`. The direct comparison helper
is `compare_project_metrics_detailed(...)` in `src/project_metrics.py`; it
requires explicit observation times and timezone labels, checks unique
`project_id` values, reports unmatched IDs, and returns per-field source/Sheet
values with absolute numeric differences. It always reports
`same_snapshot_proven=false` because separate system reads are not atomic.

No production migration, PostgreSQL write, Google Sheets write, DataLens
change, deployment, or schedule change was performed for this checkpoint.
