# Project, Report, and Controller Claims Mart — 90-Day Acceptance

**Initiative:** PORTAL-001.3  
**Status:** `BLOCKED_INCOMPLETE_HISTORICAL_SOURCE`  
**Scope:** source qualification only; no DataLens changes and no production writes.

## Decision

The requested historical source is **not ready for publication**. The existing
TEST claims mart can support episode-level historical counts, but the available
TEST report/controller snapshot is a limited observation and does not establish
the complete report universe, project denominators, or historical controller
attribution. No new tabs were published because that would present incomplete
coverage as a management mart.

The required production MariaDB source could not be queried over a verified
TLS connection. The approved Keychain-backed connection attempt required both
certificate and hostname verification and failed with PyMySQL client error
`2026` (`OperationalError`, TLS connection failure). No SQL statement was
executed. No plaintext fallback was attempted.

## Existing evidence

### Historical claims source

The existing `claims_mart_90d` tab in the TEST workbook contains 7,576 episode
rows (7,577 including its header), 7,559 distinct actions, 7,533 distinct
`(project_id, visit_id)` pairs, and 495 distinct project IDs. Its snapshot
publication ID is `cm90d-1785013200-1793048400-20261026T163904.687610+0000-7a3520c7`.
The existing source manifest records 10,097 status-39 entry events, including
9,954 client entries and 143 staff entries, in the declared 90-day window.

The current source capture is `2026-10-26T16:39:04Z`, while the declared
half-open window ends at `2026-10-26T21:00:00Z` (2026-10-27 00:00 MSK). Thus,
the published extract predates the end of its declared window by about 4h21m.
It remains valid evidence for its capture, but not a complete observation at
the requested cutoff.

Episode rows by `opened_at_utc` month in the existing TEST source:

| Month | Episodes | Distinct project/visit pairs | Projects represented |
|---|---:|---:|---:|
| 2026-07 (from Jul 26) | 426 | 424 | 75 |
| 2026-08 | 2,361 | 2,352 | 202 |
| 2026-09 | 2,622 | 2,609 | 208 |
| 2026-10 (through capture) | 2,167 | 2,158 | 220 |

These are **claim-episode numerators only**. They do not represent all visits,
all reports, or all projects active in each month. Projects with zero claims are
not represented by this fact source.

### Available report/controller data

The existing `controller_report_snapshot` tab contains 722 rows. Its source is
documented as a partial corrector/report observation, not complete historical
controller history. A bounded comparison of its `action_id` values with the
7,559 claim-mart actions found 215 matching IDs (2.84%). This overlap is a
cross-source diagnostic only: the sources have different population and capture
scope, and the field is `corrector_id`, not a proven historical controller
association. It must not be interpreted as historical attribution coverage.

The existing multi-project visit registry covers five selected projects and is
not a complete company-wide historical project or report directory. Its current
snapshot and visit dates are not a substitute for a historical event-time
source.

## Required source chain and current status

| Requirement | Available evidence | Acceptance |
|---|---|---|
| Complete historical project directory including zero-claim projects | No complete project/activity extract in existing TEST sources | `NOT_AVAILABLE` |
| Canonical project names for the full 90-day cohort | Claims fact carries IDs; selected-project sheets cover only a subset | `NOT_AVAILABLE` |
| All visits and reports by project/month | Not present in the claims mart; no complete history extract available | `NOT_AVAILABLE` |
| Claim episodes by project/month | Existing `claims_mart_90d`, with source-capture cutoff noted above | `PARTIAL_SOURCE_AVAILABLE` |
| Claim visits on a same-definition denominator | Prior 31,147 count is a specific `my`/`log` completion cohort, not all company visits; no project/month denominator extract | `NOT_AVAILABLE_FOR_REQUESTED_MART` |
| Historical report-to-controller association | Current corrector snapshot is incomplete and not a temporal association history | `NOT_AVAILABLE` |
| Controller monthly workload | Requires all reports/visits and proven historical controller links | `NOT_AVAILABLE` |
| Controller-linked claims | No verified historical controller join; no blame or causal metric can be supported | `NOT_AVAILABLE` |
| Historical claims trend | Episode opening month is available for the captured extract only | `PARTIAL_SOURCE_AVAILABLE` |

No inference was made from `my.staff`, `my.user`, `log.usr`, or current
`corrector_id` to a controller identity. No one-to-many joins were performed.

## Intended TEST schema, pending source qualification

The four proposed tabs remain a design only; they have not been created or
written.

1. `project_directory` — one row per project in the complete activity cohort;
   `project_id`, canonical `project_name`, project classification when
   supported, activity indicator, `snapshot_id`, and completeness flag.
2. `project_monthly_metrics` — one row per `(project_id, month, publication_id)`;
   distinct visits, distinct reports/actions under a proven report rule,
   claim visits, claim episodes, denominator definition, rate, and quality flag.
3. `controller_monthly_metrics` — one row per
   `(controller_id, project_id, month, publication_id)`; report and visit
   workload only after controller role and historical event-time association
   are proven. Claim associations must be explicitly labelled associative, not
   controller errors.
4. `report_controller_claim_links` — minimal report/action-to-visit/project
   links, claim episode keys, controller association evidence, attribution
   confidence, and source-generation identifiers. Omit customer text and
   unnecessary personal data.

All future fact tables must preserve their grain and use a consistent
publication/snapshot identifier. Missing controller or project attribution
must remain missing and be counted in quality coverage, not imputed.

## Acceptance gates before TEST publication

1. Restore a verifiable TLS connection to `lk.j4b.ru:3306` using the existing
   read-only `watch` account and Keychain service. Verify `Ssl_cipher`,
   hostname/certificate validation, and read-only grants before querying.
2. In one repeatable-read, read-only snapshot, inspect schema and estimate
   indexed query cost before extracting bounded project, visit, action/report,
   event, and controller-association aggregates for the exact half-open window
   `[2026-07-26 00:00 MSK, 2026-10-27 00:00 MSK)`.
3. Prove the semantic meaning of the controller actor/assignment from source
   fields and bounded examples; do not conflate controllers with correctors or
   assign the latest/current person retrospectively.
4. Reconcile project and report populations, include zero-claim projects,
   verify unique grains and join cardinalities, and calculate denominators from
   the same snapshot/window as numerators.
5. Capture only the minimum data needed, validate a complete candidate locally,
   then use the existing explicit TEST-only approval/idempotency/read-back
   publication pattern. Production remains out of scope.

## Work performed for this assessment

- Verified local feature branch `codex/portal-001-3-controller-quality-snapshot-20261026`,
  baseline `8a82af073027aa44cd3590eee93ce66b35e9c017`, clean worktree, and the
  matching remote branch head.
- Read TEST workbook metadata and bounded headers/rows for the claims mart,
  its manifest, controller report snapshot, project summary, and visit registry.
- Aggregated the existing claims rows in bounded reads; no row contents or
  personal data were emitted into this report.
- Attempted a Keychain-backed MariaDB connection with TLS certificate and
  hostname verification. Connection failed before a session was established;
  therefore database queries performed: **0**.
- No Portal requests, TEST Sheets writes, DataLens edits, production writes,
  refreshes, or deployments were performed.

## Final status

`PROJECT_DIRECTORY_READY=NO`  
`HISTORICAL_REPORTS_COVERAGE=NOT_PROVEN`  
`CONTROLLER_ASSOCIATION_COVERAGE=NOT_PROVEN`  
`MONTHLY_PROJECT_METRICS_READY=NO`  
`MONTHLY_WORKLOAD_READY=NO`  
`TEST_SHEETS_PUBLISHED=NO`  
`BLOCKER=Verified TLS connection to the approved read-only MariaDB source is unavailable; existing TEST sources do not contain complete historical project/report/controller denominators.`
