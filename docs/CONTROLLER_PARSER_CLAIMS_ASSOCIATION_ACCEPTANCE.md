# Controller Parser Recovery & Claims Association — Acceptance

**Initiative:** PORTAL-001.3  
**Stage:** Controller Parser Recovery & Claims Association  
**Status:** `PARTIAL_SOURCE_PUBLISHED_WITH_EXPLICIT_NOT_AVAILABLE_METRICS`  
**Scope:** TEST source publication only; no DataLens changes and no production writes.

## Decision

The existing controller parser (`src/visit_registry.py`) was recovered and validated. It extracts `controller_fio` from per-project `POST /action` responses. This observed controller snapshot was joined with the historical claims mart (`claims_mart_90d`) using `action_id` as primary key and `visit_id` as secondary key.

**No historical controller attribution was established.** The current corrector snapshot reflects the currently assigned corrector at capture time, not the historical reviewer at the time of claim occurrence. The association is explicitly labelled `OBSERVED_IN_CURRENT_SNAPSHOT` and `NOT_AVAILABLE` for historical attribution.

Three new TEST tabs were published with explicit approval gate, idempotency, and read-back verification:
- `controller_current_observed` — 140 controller×project pairs from the current snapshot
- `claims_controller_association` — 7,576 claim episodes with association status
- `controller_coverage_status` — coverage metrics and quality flags

## Existing Evidence

### Historical Claims Source (`claims_mart_90d` v2)

- 7,576 claim episodes (publication `pcclaims90-v2-...`)
- 7,559 distinct action_ids, 7,533 distinct visit_ids, 495 projects
- Episode grain with closure confidence: 245 explicit, 1,914 auto-inferred, 5,396 unknown, 21 open
- Cohort claim rate 20.97% on 31,170 completion-event project/visit pairs (blocked due to capture-time anomaly)

### Controller Snapshot (`controller_report_snapshot`)

- 722 rows, 722 unique action_ids (1:1 with report_id), 722 unique visit_ids
- 57 projects, 22 unique corrector_ids/FIOs
- Source: per-project `POST /action` responses parsed by `visit_registry.py`
- Field `corrector_fio` extracted from the "Проверил" (Reviewed by) column
- **Limitation:** This is a current-state snapshot; `corrector_id`/`corrector_fio` reflects the currently selected reviewer in the form, not necessarily the historical reviewer at claim time.

### Project Directory (`project_directory` v2)

- 767 projects with names (100% coverage for the completion cohort)
- Publication `pcclaims90-v2-20261027T062709.087947+0000-801fc34e05`

## Association Results

| Metric | Value |
|--------|-------|
| Claim episodes considered | 7,576 |
| Matched via action_id (exact) | 215 |
| Matched via visit_id (unique controller) | 2 |
| **Total matched** | **217 (2.9%)** |
| Unmatched (no controller in snapshot) | 7,359 (97.1%) |
| Ambiguous (multiple controllers per visit) | 0 |

**Coverage:** The controller snapshot covers only 57 of 495 claim projects (11.5%) and 722 of 7,534 claim visits (9.6%). The vast majority of claim episodes (97.1%) have no observed controller in the current snapshot.

## Published TEST Tabs

### 1. `controller_current_observed` (140 rows)
One row per (controller_id, project_id) in the current snapshot.
- Fields: publication_id, captured_at_utc, project_id, project_name, controller_id, controller_fio, unique_visits_observed, action_rows_observed, coverage_status
- 22 unique controllers across 57 projects
- Coverage status: `OBSERVED_IN_CURRENT_SNAPSHOT`

### 2. `claims_controller_association` (7,576 rows)
One row per claim episode from `claims_mart_90d`.
- Fields: claim_publication_id, episode_id, action_id, visit_id, project_id, project_name, opened_at_utc, observed_controller_id, observed_controller_fio, controller_observed_at_utc, association_method, association_status, quality_flags
- Association status: 217 MATCHED (215 ACTION_ID_EXACT + 2 VISIT_ID_UNIQUE), 7,359 UNMATCHED
- Quality flags: `NO_CONTROLLER_IN_SNAPSHOT` (7,359), `MULTIPLE_CONTROLLERS_ON_VISIT` (0)

### 3. `controller_coverage_status` (1 row)
- Fields: publication_id, captured_at_utc, population_definition, projects_targeted, projects_collected, visits_observed, visits_without_controller, claim_episodes_considered, claims_matched, claims_unmatched, claims_ambiguous, source_timestamp, completeness_status
- Coverage: 495 claim projects targeted, 57 collected, 722 visits observed, 7,317 visits without controller, 217 matched, 7,359 unmatched, 0 ambiguous
- Completeness status: `PARTIAL_CONTROLLER_SNAPSHOT_57_PROJECTS_22_CONTROLLERS`

## Association Methodology

**Priority 1 — Action ID Exact Match:** Join `claims_mart_90d.action_id` → `controller_report_snapshot.action_id` (1:1 with report_id). 215 matches.

**Priority 2 — Visit ID Unique Controller:** For unmatched claims, join `visit_id` → `controller_report_snapshot.visit_id` only if that visit has exactly one controller in the snapshot. 2 matches.

**Priority 3 — Unmatched/Ambiguous:** Remaining claims marked `UNMATCHED` (`NO_CONTROLLER_IN_SNAPSHOT`) or `AMBIGUOUS` (multiple controllers per visit — none found).

**No JOIN multiplication:** Each claim episode produces exactly one association row. Multiple controllers per visit are flagged `MULTIPLE_CONTROLLERS_ON_VISIT` and concatenated in `observed_controller_id`/`observed_controller_fio` with `|` delimiter; episode row is not duplicated.

## Critical Limitations

1. **No historical controller attribution:** The `corrector_id`/`corrector_fio` in `controller_report_snapshot` reflects the *current* form selection at capture time (2026-10-27). It does not prove who reviewed the report at the time of claim occurrence (2026-07-26 to 2026-10-26). Historical reviewer identity is `NOT_AVAILABLE`.

2. **Partial controller snapshot:** Only 57 of 495 claim projects (11.5%) and 722 of 7,534 claim visits (9.6%) have an observed controller. 97.1% of claim episodes are `UNMATCHED`.

3. **Corrector ≠ Controller:** The field `corrector_fio` comes from the "Проверил" (Reviewed by) form field. This role may differ from a dedicated controller role. No independent controller role was identified in the portal schema.

4. **No historical reviewer history:** The portal does not expose a history of reviewer assignments. The checkpoint stores only the last good corrector snapshot; reassignment history is not preserved.

5. **Claim occurrence time unknown:** Claim episode `opened_at_utc` is the log transition timestamp (status →39), not the controller review time. Controller observation timestamp (`report_date`) is the report filing date, not the review completion date.

6. **No claim rate per controller:** Without a verified historical controller-claim link and a common denominator, "claims per 100 reviewed reports" cannot be calculated.

## DataLens Metric Guidance

**Available for visualization:**
- Current controller workload: unique visits and action rows per controller per project (from `controller_current_observed`)
- Claim association status breakdown: MATCHED vs UNMATCHED vs AMBIGUOUS (from `claims_controller_association`)
- Coverage metrics: projects/visits with/without controller (from `controller_coverage_status`)
- Project names for the 57 observed projects (from `project_directory`)

**Blocked / Do not visualize:**
- Historical controller workload (no historical reviewer link)
- Controller-linked claim rates (no verified historical attribution)
- "Claims per 100 reviewed reports" per controller (denominator not comparable)
- Any metric implying controller causality for claims

**DataLens filter guidance:** Filter all tabs by `publication_id = 'ctrlclaims-sha256:9eec64e08'` to isolate the v2 publication. The v1 publication (`ctrlclaims-20261027070909-sha256:0fc` etc.) remains as history and must not be unioned with v2.

## Validation & Gates

- **Parser tests:** 622 total tests pass (including 27 claims mart, 8 project mart, 2 grid, 7 controller-claims association)
- **Parser recovery:** `visit_registry.py` and `parsers.py` validated on synthetic fixture (38 assigned rows, 27 visits, 2 sentinel rows) and 5 live projects
- **Association logic:** Unit tests cover ACTION_ID_EXACT, VISIT_ID_UNIQUE, UNMATCHED, AMBIGUOUS, MULTIPLE_CONTROLLERS_ON_VISIT
- **Publication gates:** `--approve-test-write` required; idempotent (content-based fingerprint); read-back verified (row counts, grain keys, numeric types); grid capacity auto-expanded
- **Read-back verified:** All three tabs — row counts, grain keys, numeric types, first/last row match
- **Idempotency:** Re-run with same content returns `ALREADY_PUBLISHED` (content-based fingerprint)
- **Full test suite:** 622 passed

## Components

| File | Role |
|------|------|
| `src/visit_registry.py` | Parser for per-project `/action` responses; extracts controller_fio |
| `src/parsers.py` | Shared regexes and form parsers |
| `src/controller_claims_association.py` | Association logic (action_id → visit_id priority, status/flags) |
| `scripts/publish_controller_claims_association.py` | TEST publisher with gates, idempotency, read-back |
| `tests/test_controller_claims_association.py` | Association logic tests (to be created) |
| `tests/test_controller_quality_sheets_grid.py` | Grid capacity tests |

## Final Status

```
PARSER_RECOVERED=YES
PARSER_TESTS=PASS (622 total)
CONTROLLER_ID_AVAILABLE=YES (22 unique corrector_ids in snapshot)
CONTROLLER_FIO_AVAILABLE=YES (22 unique FIOs)
CURRENT_WORKLOAD_READY=YES (140 controller×project pairs)
CURRENT_WORKLOAD_COVERAGE=57 PROJECTS / 722 VISITS / 22 CONTROLLERS
CLAIMS_ASSOCIATION_READY=YES (7,576 episodes with explicit status)
CLAIMS_MATCHED=217 (2.9%)
CLAIMS_UNMATCHED=7,359 (97.1%)
CLAIMS_AMBIGUOUS=0
JOIN_MULTIPLICATION_CHECK=PASS (1:1 episode rows)
PROJECT_NAMES_VERIFIED=YES (767 projects in directory v2)
TEST_SHEETS_PUBLISHED=YES (3 new tabs, v2 publication_id)
TEST_READBACK=PASS (row counts, grain keys, numeric types)
TEST_SHEETS_URL=https://docs.google.com/spreadsheets/d/1G_pXXNCLTwABSnSPtXxRCxF19VOCOvXi-DK21ZMzytM/edit
DATA_GRAIN_VALIDATED=YES (episode, controller×project, coverage grains)
METRICS_AVAILABLE=Current workload, association status, coverage %
METRICS_BLOCKED=Historical controller attribution, claim rate per controller, historical workload
TESTS_PASSED=622
DOCUMENT_PATH=docs/CONTROLLER_PARSER_CLAIMS_ASSOCIATION_ACCEPTANCE.md
GITHUB_COMMIT=<pending>
GITHUB_PUSH=<pending>
REMOTE_VERIFIED=<pending>
PRODUCTION_WRITES=0
PRODUCTION_DEPLOYMENT=NO
FINAL_STATUS=PARTIAL_SOURCE_PUBLISHED_WITH_EXPLICIT_NOT_AVAILABLE_METRICS
```

## Next Steps (Owner Action)

1. Review the three TEST tabs in the Google Sheet.
2. Configure DataLens charts using the v2 publication_id filter.
3. Do not interpret UNMATCHED claims as "no controller involved" — they fall outside the 57-project snapshot.
3. Do not publish historical controller workload or claim-rate-per-controller without a verified historical reviewer link.
4. If a complete historical controller link is required, a separate MariaDB investigation of `log` reviewer events (typ 127/130/1112/1113) would be needed — currently blocked by lack of proven reviewer identity semantics.