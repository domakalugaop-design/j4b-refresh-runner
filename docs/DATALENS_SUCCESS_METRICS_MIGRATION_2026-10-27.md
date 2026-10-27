# PORTAL-001.3 — DataLens migration plan for successful-visit metrics

**Status:** prepared only; production Sheets/DataLens remain unchanged
**Scope:** source contract and downstream field mapping
**Data handling:** no production IDs or customer/employee data are included.

## 1. Source fields prepared by the feature branch

The source runner appends these project fields after the existing project, type, and currency columns (no existing column is moved):

| Field | Use in DataLens |
|---|---|
| `successful_visits_current` | Approved “Успешно завершено” numerator |
| `execution_pct_successful` | Approved per-project execution ratio; format as percent |
| `total_project_visits` | “Всего визитов” denominator-independent count of unique Visits |
| `historically_approved_visits` | Separate historical diagnostic (ever observed state 40/50; excludes 37); not the current-success KPI |
| `current_state_unresolved_visits` | Data-quality indicator; when nonzero, source execution ratio is null |
| `gap_pp` | Signed current execution-minus-elapsed gap in percentage points; null without both inputs |

Existing fields remain present for compatibility:

- `completed` and `execution_pct` are source-level compatibility aliases to the corrected current-success fields.
- `finished_visits` and existing `workflow_state_*` fields retain their old membership semantics and are not safe replacements for `successful_visits_current`.
- `project_visit_count` remains and must equal `total_project_visits`.

`Статусы проектов` also receives appended fields for current successful visits, historical approvals, total unique visits, unresolved current states, execution ratio, and gap. Its existing state breakdown and legacy `Завершено` column are retained pending a reviewed workbook migration.

## 2. DataLens mapping plan

| Dashboard item | Source / formula after migration | Note |
|---|---|---|
| **Успешно завершено** | `successful_visits_current` | Use latest resolved action state per Visit; states 40/50 only |
| **Выполнение плана, % (project)** | `execution_pct_successful` | Stored ratio, display with percent format |
| **Выполнение плана, % (portfolio)** | `SUM(successful_visits_current) / SUM(plan)` | Do not average project percentages; exclude null/zero plans from numerator/denominator and expose a data-quality count |
| **Всего визитов** | `total_project_visits` | Includes refused, unassigned, unresolved, and non-success visits |
| **Когда-либо утверждено/оплачено** | `historically_approved_visits` | Historical diagnostic only; not the current plan numerator |
| Legacy **Завершено** | Replace the current chart binding with `successful_visits_current`; rename the old `finished_visits` union to “Исторически завершено (legacy)” or hide it after validating dependent charts | No production chart is changed in this task |
| **Risk / schedule status** | `gap_pp`, `risk_status` from source | Same 10/20 pp thresholds; no status when inputs are missing |

Existing chart filters, joins, and selectors should be preserved. This task did not directly inspect the saved DataLens dataset or formula because no authenticated DataLens browser tab/API was available; do not claim that its current formula has been read back.

## 3. Workbook schema compatibility

The code keeps the old 31-column base, 33-column project-type, 37-column currency, and workflow-enabled schemas readable. New fields are appended; prior columns keep their order. The preflight schema upgrader maps known prior headers into the expanded target with blanks for newly added fields. Publication remains behind the existing gates and is not run by this branch.

Before any future cutover, verify:

1. the actual `projects_current` header equals the intended expanded schema after an explicit test run;
2. `Статусы проектов` receives the expected appended headers and one row per project;
3. values read back match the generated payload, with no row movement or other-tab writes;
4. the production DataLens dataset binds the new fields and retains the existing 14 status breakdowns;
5. charts using old `finished_visits` are deliberately migrated rather than silently left on the historical union; surface a warning/count when `current_state_unresolved_visits > 0`.

## 4. Rollout sequence — not executed

1. Review `PROJECT_SUCCESSFUL_VISITS_BUSINESS_RULE.md` and the `current_state_unresolved_visits` behavior.
2. Run local synthetic tests and the isolated TEST dry-run from an approved production read-only acquisition snapshot.
3. Capture the existing Sheets baseline and validate the additive header/data contract in an isolated test workbook; do not use the production workbook for the test.
4. Obtain separate explicit approval for a production Sheets refresh and later a DataLens dataset/chart update.
5. Update source fields first, read back and reconcile; only then migrate DataLens bindings and verify project-level and portfolio-level formulas.
6. Roll back by restoring the prepublication workbook backup and prior dataset bindings if the readback or formula validation fails.

No scheduler, production Sheet, DataLens object, Portal DB, or deployment was changed while preparing this plan.

## 5. Outstanding qualification

The feature branch changes the source runner to choose one current action state by the highest action ID exposed for a Visit, and fails closed on a conflicting state tie. A bounded production SQL comparison qualified this ordering for the controlled set; the live `/action` HTTP payload was not replayed in this implementation task. The direct current `my.state` table and published workflow counts are not a one-to-one match for all statuses, so state 37/45/50 mapping remains a release gate. If the isolated TEST acquisition does not show the expected current state, do not deploy or compensate in DataLens; stop with `BLOCKED_BY_SOURCE`.
