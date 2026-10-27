# Project, Report, and Controller Claims Mart — 90-Day Acceptance

**Initiative:** PORTAL-001.3  
**Status:** `PARTIAL_SOURCE_PUBLISHED_WITH_EXPLICIT_NOT_AVAILABLE_METRICS`
**Scope:** TEST source publication only; no DataLens changes and no production writes.

## Decision

The full requested report/controller mart is **not available**: no verified
historical reviewer link or report-completion grain was found. A partial,
carefully labelled TEST source was published: complete named project dimension
for the observed completion-event cohort, project-month completion transitions,
claims episode links at action grain, zero-claim projects, and explicit
`NOT_AVAILABLE` fields for report/controller measures. It does not claim full
report-universe coverage or controller accountability.

The earlier TLS connection attempt used the wrong connection contract. After
the owner confirmed direct MariaDB TCP without TLS or SSH, the Keychain-backed
connection succeeded. `CURRENT_USER()=watch@%`, MariaDB
`10.11.19-MariaDB-0+deb12u1-log`, `Ssl_cipher` empty, grants exactly
`SELECT, SHOW VIEW ON j4b.*`. All production queries were read-only; no
production writes or schema changes occurred.

## Existing evidence

### Historical claims source

The existing `claims_mart_90d` tab in the TEST workbook contains 7,576 episode
rows (7,577 including its header), 7,559 distinct actions, 7,533 distinct
`(project_id, visit_id)` pairs, and 495 distinct project IDs. Its snapshot
publication ID is `cm90d-1785013200-1793048400-20261026T163904.687610+0000-7a3520c7`.
The existing source manifest records 10,097 status-39 entry events, including
9,954 client entries and 143 staff entries, in the declared 90-day window.

The claims manifest declares capture `2026-10-26T16:39:04.687610Z`. However,
36 of the 7,576 episode rows have `opened_at_utc` later than that capture; the
latest is `2026-10-26T19:25:24Z` (2h46m after the declared capture). The
event-time rows remain available, but the capture timestamp cannot be used as
a reliable completeness cutoff. Therefore the project-level cohort rate is
published blank with status `NOT_AVAILABLE_CLAIM_SOURCE_CAPTURE_TIME_PRECEDES_EVENT`;
the earlier 20.97% must not be treated as a universal or newly reconciled rate.

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
| Named projects in the observed →40 completion cohort | `proj.i/name` lookup: 739 completion-cohort projects plus 28 claim-only projects; all 767 names resolved | `AVAILABLE_FOR_DEFINED_COHORT` |
| Zero-claim projects | 272 of the 739 completion-cohort projects have no episode row; retained | `AVAILABLE` |
| Project/month completed visits and actions | 31,709 typ-140 rows; 31,212 distinct actions; 31,206 mapped actions; 31,170 `(project_id,visit_id)` pairs; six action IDs lack `my` mapping | `AVAILABLE_AS_LOG_TRANSITION_COHORT; NOT_ALL_VISITS/REPORTS` |
| All relevant reports by project/month | `my.i` action IDs and `typ=140` transitions are available, but universal report-ID/completion semantics are not proven | `NOT_AVAILABLE_REPORT_COUNT` |
| Claim episodes by project/month | 7,576 episode rows from `claims_mart_90d`; seven lack visit/project attribution | `AVAILABLE_WITH_7_UNATTRIBUTED` |
| Claim visits on a same-definition denominator | 6,532 claim project/visit pairs intersect the 31,170 project/visit completion cohort; claims capture anomaly blocks rate | `COUNT_AVAILABLE; RATE_NOT_AVAILABLE` |
| Historical report-to-controller association | Current corrector snapshot is incomplete and not a temporal association history | `NOT_AVAILABLE` |
| Controller monthly workload | Requires all reports/visits and proven historical controller links | `NOT_AVAILABLE` |
| Controller-linked claims | No verified historical controller join; no blame or causal metric can be supported | `NOT_AVAILABLE` |
| Historical claims trend | Episode opening month is available for the captured extract only | `PARTIAL_SOURCE_AVAILABLE` |

No historical controller attribution was published. On the 722-row current
corrector snapshot, `my.staff` matched `corrector_id` on 718 rows, but this is
current-state evidence only. In 2,504 typ-127/130 event records on that sample,
only 61 event actors matched the current corrector ID; actors have varied user
roles. These facts do not establish controller identity at review time. No
inference was made from `my.staff`, `my.user`, `log.usr`, or current
`corrector_id` to a historical controller. The project/action/visit joins were
checked for duplicate mappings; no join multiplication was found.

The earlier same-rule completion cohort was 31,147 visit IDs. This extraction
is later and contains 31,170 project/visit keys (23 more); the production log
is live and the captures are not identical. Do not silently overwrite the
earlier figure or describe either population as all company visits.

## Published TEST schema

The following six new tabs were created in the existing TEST workbook. All
include a stable `publication_id`, fixed headers and explicit quality/status
fields. The existing `claims_mart_90d` tab was read only and left unchanged.
The first publication (v1) stored numeric values as text; v2 corrected numeric
cell typing and is the DataLens-ready version. **Filter all six tabs to the v2
publication_id below; never union v1 and v2.** V1 remains as publication history.

1. `project_directory` — 767 project rows (739 in the completion cohort and
   28 claim-only); `project_id`, `project_name`, raw uninterpreted
   `project_type_code`, activity status, name status. Name coverage 100%.
2. `project_monthly_metrics` — 3,068 rows, one per project × month for all four
   months. `completed_visit_count` and `completed_action_count` use log typ=140
   transition time; claim visits/episodes use claim-open time. Monthly rate is
   blank because these are different event-month cohorts. `report_count` is
   blank with an explicit status. Read-back sums: completed action IDs per
   month = 3,459 / 12,957 / 8,728 / 6,090; attributed claim episodes per month
   = 426 / 2,359 / 2,620 / 2,164. Seven episodes with missing project/visit
   remain only in the episode link tab. Monthly distinct action/visit counts
   can repeat across months when the same action or visit has another
   type-140 transition; do not sum month counts to derive unique period totals.
3. `project_period_metrics` — 767 rows with full-window distinct project/visit
   denominator, claim visits, episodes and matched claim-visit numerator.
   `claim_rate_pct` is blank due the claims capture-time anomaly above.
4. `controller_monthly_metrics` — 3,068 project/month quality rows. Controller
   ID/FIO and workload figures are blank and explicitly `NOT_AVAILABLE`; this
   is not a person-level workload table.
5. `report_controller_claim_links` — 7,576 episode-grain rows, action/visit/
   project/date/closure fields, no customer text. Seven rows retain blank
   visit/project attribution. `action_id` is linked; universal `report_id`
   equivalence and historical controller link are explicitly unavailable.
6. `project_controller_claims_manifest` — one append-only publication record
   with source IDs/captures, row populations, quality counts and metric status.

Current v2 publication: `pcclaims90-v2-20261027T062709.087947+0000-801fc34e05`;
fingerprint `sha256:801fc34e053111c1251f742d1d406d821b5e14f00172790a042312248d915955`.
The v2 read-back checked numeric column types (zero type errors), row counts,
grain-key uniqueness and manifest fingerprint. An identical rerun returned
`ALREADY_PUBLISHED` and added zero rows.

All future fact tables must preserve their grain and use a consistent
publication/snapshot identifier. Missing controller or project attribution
must remain missing and be counted in quality coverage, not imputed.

### DataLens metric guidance

- Available: project names/IDs for this defined cohort; monthly and full-window
  project/visit counts with the type-140 event rule; claim episode counts and
  claim visit counts by opening month; project/visit matching counts; quality
  flags and closure-confidence categories in the detail source.
- Blocked: all-report counts and per-100-report claim rate (no proven report
  universe/complete-review definition); controller workload and
  controller-linked claims (no historical reviewer association); universal
  claim rate (36 episode rows occur after the source manifest capture time);
  exact average episodes per project (seven episodes lack project attribution).
- Do not label typ-140 action transitions as reviewed reports, do not treat the
  current corrector field as historical controller, and do not present claim
  association as controller error or causation.
- The `project_type_code` column is a raw source code; no classification
  semantics were inferred. Filter rate fields by their explicit status rather
  than converting blank to zero.

## Acceptance gates before TEST publication

1. Use the owner-confirmed direct TCP/no-TLS/no-SSH contract with read-only
   `watch`; verify `CURRENT_USER()`, server version, empty `Ssl_cipher`, and
   `SELECT, SHOW VIEW` grants before querying. **Done.**
2. In one repeatable-read, read-only snapshot, inspect schema and query plans;
   use the `log.type` index for typ=140 events and indexed `my.i`, `visit.i`,
   `proj.i` lookups. The exact half-open window is
   `[2026-07-26 00:00 MSK, 2026-10-27 00:00 MSK)`. **Done.**
3. Prove the semantic meaning of the controller actor/assignment from source
   fields and bounded examples; do not conflate controllers with correctors or
   assign the latest/current person retrospectively.
4. Reconcile project and event populations, include zero-claim projects,
   verify unique grains and join cardinalities. The claim rate remains blocked
   because the claims source capture metadata is inconsistent with its event
   timestamps; no rate was fabricated.
5. Capture only the minimum data needed, validate a complete candidate locally,
   then use the existing explicit TEST-only approval/idempotency/read-back
   publication pattern. Production remains out of scope.

## Work performed for this assessment

- Resumed from branch `codex/portal-001-3-controller-quality-snapshot-20261026`,
  HEAD `f941658ade744625e04e9d9ce3f7483038f58841`, clean worktree, matching
  remote; the prior claims milestone `8a82af0` is an ancestor.
- Read TEST workbook metadata and bounded headers/rows for the claims mart,
  its manifest, controller report snapshot, project summary, and visit registry.
- Aggregated the existing claims rows in bounded reads; no row contents or
  personal data were emitted into this report.
- Connected directly to MariaDB without TLS or SSH, using the Keychain item;
  verified user/version/grants. The PyMySQL extractor used a read-only,
  repeatable-read snapshot, `log` type index, and bounded ID joins. A broad
  multi-type count timed out and was not repeated; narrower indexed queries
  were used instead.
- Verified 31,709 typ-140 events; 31,212 action IDs, six without `my` mapping;
  31,206 mapped action IDs, 31,170 project/visit pairs, 739 projects and 990
  active project-months. Mapping joins had no duplicate action rows.
- Compared 722 current corrector snapshot rows with `my.staff` and bounded
  `log` actors. This did not establish historical controller identity.
- Published the six new TEST tabs with explicit approval; read back every row
  and verified grain-key uniqueness. Numeric typing was corrected in a second
  append-only v2 publication; its read-back returned no numeric type errors.
  Same-source v2 rerun returned `ALREADY_PUBLISHED` with zero rows added.
- Read-back reconciliation: 767 project period rows sum to 31,170 distinct
  project/visit completion keys; 7,569 attributed claim episodes plus seven
  un-attributed episode rows reconcile to the 7,576 detail rows; the matched
  claim-visit count is 6,532. No claim-rate cell is populated.
- Full test suite: **622 passed**. No DataLens charts/edits, production writes,
  refreshes, or deployment.

## Final status

`PROJECT_DIRECTORY_READY=YES_FOR_767_PROJECT_UNION; 100% NAMES`
`HISTORICAL_VISITS_COVERAGE=31,170 PROJECT/VISIT PAIRS WITH LOG TYPE-140 TRANSITION; NOT ALL COMPANY VISITS`
`HISTORICAL_REPORTS_COVERAGE=REPORT_COUNT_NOT_AVAILABLE; 31,206 MAPPED ACTIONS ARE TRANSITION-40 PROXY ONLY`
`CONTROLLER_HISTORY_AVAILABLE=NO; CURRENT CORRECTOR SNAPSHOT IS NOT HISTORICAL REVIEWER HISTORY`
`CONTROLLER_ASSOCIATION_COVERAGE=NOT_AVAILABLE`
`CLAIM_TO_REPORT_LINKAGE=ACTION_ID AVAILABLE; UNIVERSAL REPORT_ID EQUIVALENCE UNPROVEN`
`CLAIM_TO_CONTROLLER_LINKAGE=NOT_AVAILABLE`
`MONTHLY_PROJECT_METRICS_READY=YES_FOR_EVENT_COUNTS; RATE BLOCKED`
`MONTHLY_WORKLOAD_READY=NO; HISTORICAL CONTROLLER ATTRIBUTION NOT PROVEN`
`PROJECTS_WITH_ZERO_CLAIMS=272 INCLUDED`
`CLAIM_DENOMINATOR_VERIFIED=31,170 COMPLETION-EVENT PROJECT/VISIT PAIRS; RATE NOT AVAILABLE DUE TO 36 CLAIM ROWS AFTER MANIFEST CAPTURE`
`TEST_SHEETS_PUBLISHED=YES; 6 NEW VERSIONED TABS; CURRENT PUBLICATION=pcclaims90-v2-20261027T062709.087947+0000-801fc34e05`
`TEST_READBACK=PASS; v2 row counts, numeric types, grain keys; idempotent rerun added 0 rows; claims_mart_90d unchanged`
`TESTS_PASSED=622`
`PRODUCTION_WRITES=0`
`PRODUCTION_DEPLOYMENT=NO`
`FINAL_STATUS=PARTIAL_SOURCE_PUBLISHED; REPORT AND CONTROLLER METRICS EXPLICITLY NOT_AVAILABLE`
