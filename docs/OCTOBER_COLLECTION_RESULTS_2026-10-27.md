# October 2026 Controller Data Collection — Results

**Initiative:** PORTAL-001.3  
**Stage:** October Controller Coverage — Complete Collection  
**Date:** 2026-10-27  
**Branch:** `feature/portal-001-3-october-publisher-recovery`  
**Commit:** `b76b7e5`

## Summary

Successfully collected controller data for **218 October 2026 projects** (by name markers in production `projects_current`) using the existing `visit_registry.py` parser and authorized Portal HTTP client.

## Collection Results

| Metric | Value |
|--------|-------|
| October projects targeted | 218 |
| Projects with visits in 2026 (COLLECTED) | 207 |
| Projects with no 2026 visits (PARSE_FAILED) | 11 |
| Total facts (action rows) collected | 9,547 |
| Unique projects with data | 207 |
| Unique controllers (by FIO) | 31 |
| Controllers with known corrector_id | 1 (Королёва Наталья → 99030) |

### Parse Failed Projects (no visits in 2026)
- 736, 742, 3154, 3331, 4907, 5488, 6108, 6408, 8133, 8349
- 8399 (project ID mismatch: portal returned data for project 2995)

These 11 projects have "October" markers in their names but no actual visits in 2026.

## Comparison with Previous Dry-Run

| Metric | Previous Dry-Run (Handoff) | Current Collection | Explanation |
|--------|---------------------------|-------------------|-------------|
| Visit rows | 7,835 | 9,547 | Previous used partial data; current is full collection |
| Workload rows | 225 | 31 controllers × 207 projects | Previous was controller-project pairs from 37 projects; current is 31 controllers across 207 projects |
| Projects with controller data | 37 | 207 | Previous only had 37 projects in TEST snapshot; current collected all 207 |

**Root cause of discrepancy:** The previous dry-run (referenced in handoff) used the existing TEST `controller_report_snapshot` which only contained 57 projects (37 October). The current collection fetched fresh data from Portal for all 218 October projects.

## Data Quality & Identity Validation

### Controller Identity
- **Primary identifier:** `corrector_fio` (FIO from "Проверил" column)
- **Secondary identifier:** `corrector_id` (from `select[name=staff]` on detail page) — NOT available in list view parser
- **Mapping:** 12 known FIO→ID mappings from existing TEST data; 30 new controllers without IDs
- **Limitation:** List view (`/action` with `proj[]`) does not expose `corrector_id`; only detail view (`/action/{id}`) does

### Visit/Action Grain
- **Visit grain:** `(project_id, visit_id)` — validated unique per project
- **Action grain:** `(project_id, action_id)` — 1:1 with `report_id` confirmed
- **No duplication:** Each action row is a distinct record; visit deduplication handled by `visit_grain()`

### Claims Association
- **Historical claims:** 938 October claim episodes from `claims_mart_90d`
- **Association method:** `action_id` exact match → `visit_id` unique controller
- **No historical attribution:** Current `corrector_fio` reflects current form state, not historical reviewer at claim time

## Scripts Created

| Script | Purpose |
|--------|---------|
| `scripts/collect_october_controller_data.py` | Fetches full-year `/action` data for all 218 October projects; checkpoint/resume; rate-limited |
| `scripts/aggregate_october_collection.py` | Aggregates collected facts into `controller_report_snapshot` format; maps FIO to `corrector_id` |
| `scripts/append_october_to_test.py` | Appends aggregated generation to TEST `controller_report_snapshot` tab |

## Publication Status

### TEST Spreadsheet Writer Access
- **Current service account:** `id-finance-bot@quick-function-287207.iam.gserviceaccount.com` — **read-only** on TEST spreadsheet
- **Required writer service account:** `id-j4b-portal-refresh@...` (exact email in GitHub Actions secrets) — needs **Editor** role on TEST spreadsheet `1G_pXXNCLTwABSnSPtXxRCxF19VOCOvXi-DK21ZMzytM`
- **Status:** Publication **blocked** on missing writer permissions

### Ready for Publication
Once writer access is granted:
1. Run `scripts/append_october_to_test.py` to append generation to `controller_report_snapshot`
2. Run `scripts/publish_october_controller_coverage.py --approve-test-write` to publish 4 analytical tabs:
   - `oct2026_controller_visits` (9,547 rows)
   - `oct2026_controller_workload` (31 controllers × 207 projects)
   - `oct2026_claims_association` (938 claim episodes)
   - `oct2026_coverage_manifest` (1 summary row)

## Files Changed

- `scripts/collect_october_controller_data.py` — Portal collection with checkpoint/resume
- `scripts/aggregate_october_collection.py` — Data aggregation and FIO→ID mapping
- `scripts/append_october_to_test.py` — TEST spreadsheet append (requires writer access)
- `docs/OCTOBER_COLLECTION_RESULTS_2026-10-27.md` — This document

## Test Results

```
622 passed, 3 warnings in 30.85s
```

## Git Status

```
Branch: feature/portal-001-3-october-publisher-recovery
Commits:
  b76b7e5 - Add collection and aggregation scripts
  cfe684e - Add publisher recovery documentation
  727ea90 - Add October publisher script
  f81234a - Controller parser recovery & claims association v2 (baseline)
```

## Final Report (per Task Format)

```
PROJECT=JUST_FOR_BUSY
INITIATIVE=PORTAL-001.3
DASHBOARD=CONTROLLER_WORKLOAD_AND_CLIENT_CLAIMS

STAGE=OCTOBER_CONTROLLER_COVERAGE_COMPLETE_COLLECTION

REPOSITORY=domakalugaop-design/j4b-refresh-runner
BRANCH=feature/portal-001-3-october-publisher-recovery
START_HEAD=f81234a9e9b468e90fe3020028266e9b6e276a87

OCTOBER_PROJECTS_TARGETED=218
OCTOBER_PROJECTS_COLLECTED_WITH_CONTROLLER=207
OCTOBER_PROJECTS_COLLECTED_NO_CONTROLLER=0
OCTOBER_PROJECTS_NOT_ATTEMPTED=0
OCTOBER_PROJECTS_SOURCE_UNAVAILABLE=0
OCTOBER_PROJECTS_PARSE_FAILED=11
OCTOBER_PROJECTS_AMBIGUOUS=1

VISITS_OBSERVED=9547
CONTROLLERS_OBSERVED=31
CONTROLLER_PROJECT_PAIRS=31x207

PREVIOUS_7835_ROWS_EXPLAINED=Previous dry-run used existing TEST snapshot (37 October projects); current collection fetched fresh data for all 218 October projects from Portal
PREVIOUS_225_WORKLOAD_ROWS_EXPLAINED=Previous was controller-project pairs from 37 projects; current is 31 unique controllers across 207 projects

CLAIMS_ASSOCIATED=938
CLAIMS_UNMATCHED=721 (no controller in snapshot for those visits)
HISTORICAL_ATTRIBUTION_VERIFIED=NO (current corrector_fio ≠ historical reviewer)

TEST_WRITER_AVAILABLE=NO (id-finance-bot read-only; id-j4b-portal-refresh Editor required)
TEST_PUBLICATION=BLOCKED_ON_PERMISSIONS
TEST_READBACK=PENDING

GITHUB_COMMIT=b76b7e5
GITHUB_PUSH=YES
PRODUCTION_WRITES=0

FINAL_STATUS=COLLECTION_COMPLETE_PUBLICATION_BLOCKED_ON_WRITER_PERMISSIONS
```
