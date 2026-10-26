# Controller Quality — Google Sheets inventory and refresh lineage

**PORTAL-001.3 — read-only discovery, 2026-10-26**

Spreadsheet: `14PRG3HXsZaN4pVwV390PpMMqHupCW5j6RyI6HBzPQR8`.
Read-only inventory used the existing production Google Sheets gateway and the Google Values API. Cell contents containing names/PII were summarized locally and not written to this document.

## Summary

- All six known tabs are present with the expected sheet IDs.
- The workbook is a materialized-value layer: formula-render readback found **0 formula cells** across the six used ranges.
- The exact workflow-name dictionary is encoded in `src/workflow_analytics.py` and repeated in `Статусы проектов` column headers. There is no separate status-dictionary tab.
- Workflow codes in `projects_current` and `Статусы проектов` are **project-level visit counts**, not report-level rows.
- `Выплаты по визитам.workflow_state_codes` is a payment-mapped visit snapshot; it lacks `action_id` and is not a complete report-status fact.
- `projects_current.period` is blank in the 8,088 current rows; it is not a usable cohort date. Use explicit report/visit/review/claim dates only when their event semantics are confirmed.
- `report_reviewers` contains the current controller snapshot, not a review/status event history. It currently has 722 rows, but one live October status39 report with a selected controller was absent from the registry/checkpoint/sheet, so the inventory is not complete.

## Tabs, grains, keys, schemas

Row counts below are data rows excluding the header, read back on 2026-10-26.

| Tab | sheetId | Rows × columns | Grain / unique key in readback | Main purpose |
|---|---:|---:|---|---|
| `projects_current` | 709247807 | 8,088 × 58 | one row per `project_id`, 8,088/8,088 unique | current project dimension and project-level calculated execution/workflow metrics |
| `project_types` | 2117967649 | 420 × 3 | one row per `project_id`, 420/420 unique | project type code/name dictionary |
| `Статусы проектов` | 1133607650 | 8,088 × 25 | one row per `project_id`, 8,088/8,088 unique | project-level workflow status counts and derived aggregates |
| `Выплаты по визитам` | 1500929201 | 88,864 × 12 | `(project_id, visit_id)`, 88,864/88,864 unique | payment assignment aggregates for visits with qualified payment-to-visit mapping |
| `Выплаты по проектам` | 1500929202 | 1,998 × 15 | one row per `project_id`, 1,998/1,998 unique | project-level payment aggregates and payment data-quality state |
| `report_reviewers` | 146075996 | 722 × 9 | one row per `report_id`, 722/722 unique | current selected corrector and report/action/visit/project identifiers for discovered reports with a corrector |

`visit_id` alone is not a unique key in `Выплаты по визитам`: five visit IDs occur in multiple projects. Use `(project_id, visit_id)` for this tab. `report_reviewers` has unique visit IDs in its current 722-row snapshot, but a separate bounded portal check found multiple report/action records for one visit; do not assume that this holds for the whole portal.

### Exact headers

#### `projects_current`

`project_id`, `project_name`, `period`, `plan`, `plan_value`, `plan_status`, `created`, `completed`, `unassigned`, `execution_pct`, `plan_missing_with_activity`, `has_period_marker`, `project_start`, `project_end`, `elapsed_pct`, `lag`, `risk_status`, `risk_reason`, `validation_state`, `last_refreshed`, `client`, `primary_manager`, `coordinators`, `date_from`, `date_to`, `scope`, `manager_payment`, `wave`, `assigned`, `questionnaire_filled`, `rejected`, `project_type_code`, `project_type_name`, `workflow_state_0_visits`, `workflow_state_5_visits`, `workflow_state_10_visits`, `workflow_state_15_visits`, `workflow_state_20_visits`, `workflow_state_25_visits`, `workflow_state_30_visits`, `workflow_state_35_visits`, `workflow_state_37_visits`, `workflow_state_38_visits`, `workflow_state_39_visits`, `workflow_state_40_visits`, `workflow_state_45_visits`, `workflow_state_50_visits`, `assigned_visits`, `executed_visits_customer`, `finished_visits`, `project_visit_count`, `workflow_covered_visits`, `workflow_unclassified_visits`, `workflow_multi_match_visits`, `currency_id`, `currency_code`, `currency_name`, `currency_symbol`.

#### `project_types`

`project_id`, `project_type_code`, `project_type_name`.

#### `Статусы проектов`

`project_id`, `project_name`, `client`, `manager`, `project_visit_count`, `Отправлено приглашение [0]`, `Приглашение отклонено [5]`, `Приглашение принято [10]`, `Отклонено менеджером [15]`, `Подтверждено менеджером [20]`, `Провалено пользователем [25]`, `Выполнено пользователем [30]`, `Анкета отклонена [35]`, `Анкета проверена [37]`, `Вопрос координатору [38]`, `Есть претензия [39]`, `Анкета утверждена [40]`, `В оплате отказано [45]`, `Оплачено [50]`, `Назначено`, `Выполнено`, `Завершено`, `Покрыто workflow`, `Без workflow-состояния`, `Несколько workflow-состояний`.

#### `Выплаты по визитам`

`project_id`, `project_name`, `client`, `manager`, `visit_id`, `payment_assignment_count`, `Вознаграждение за визит`, `Оплачено по данным портала`, `Количество положительных выплат`, `Количество нулевых выплат`, `workflow_state_codes`, `Статус данных выплат`.

#### `Выплаты по проектам`

`project_id`, `project_name`, `client`, `manager`, `Количество визитов с выплатами`, `Количество записей выплат`, `Вознаграждение за визиты`, `Оплачено по данным портала`, `Количество записей с положительной выплатой`, `Количество записей с нулевой выплатой`, `Количество визитов с положительной выплатой`, `Количество визитов с нулевой выплатой`, `Строки, требующие проверки`, `Несопоставленные строки`, `Статус данных выплат`.

#### `report_reviewers`

`report_id`, `action_id`, `visit_id`, `project_id`, `report_date`, `corrector_id`, `corrector_fio`, `source_status`, `last_successful_fetch`.

## Value types, formats, formulas, dictionaries

The type histograms below come from the full used values ranges in `UNFORMATTED_VALUE` mode. These are Google API value types, not a claim about a DataLens cast.

| Tab | Raw populated cell type counts | Formula cells | Format notes |
|---|---|---:|---|
| `projects_current` | string 74,458; integer 101,186; boolean 15,573; float 8,387 | 0 | Sampled effective formats include NUMBER, DATE and DATE_TIME. Date/time fields are numeric Sheets serials in unformatted readback. |
| `project_types` | string 843; integer 420 | 0 | Sampled cells have no explicit number-format type; IDs are numbers, codes/names text. |
| `Статусы проектов` | string 22,540; integer 22,790 | 0 | Sampled cells have no explicit number-format type; IDs/counts are numeric, dimension labels text. |
| `Выплаты по визитам` | string 444,293; integer 621,982 | 0 | Sampled cells have no explicit number-format type; `workflow_state_codes` is text/JSON-like list. |
| `Выплаты по проектам` | string 8,003; integer 21,914 | 0 | Sampled cells have no explicit number-format type; counts/IDs are numeric values, labels/statuses text. |
| `report_reviewers` | string 6,507 | 0 | All populated readback values are strings, including IDs, ISO report date and ISO UTC fetch timestamp. |

Format metadata was sampled on the header and first three data rows; other cells may have formatting not represented by that sample. All six tabs have zero formulas in the full formula-render range.

Status/quality values in the current readback:

- `projects_current.plan_status`: `VALID_PLAN` 6,866; `PLAN_MISSING_DATA` 1,215; `PLAN_SOURCE_ERROR` 7.
- `projects_current.risk_status`: `NOT_QUALIFIED` 6,882; `DATA_QUALITY` 1,205; `ATTENTION` 1.
- `projects_current.period`: blank in 8,088/8,088 rows; `plan_status` and `risk_status` are calculated project/data-quality states, not Portal workflow statuses.
- Workflow counters are 14 code-specific project aggregates. Only 617/8,088 project rows have any nonblank workflow metrics; the other 7,471 are blank, **not confirmed zero**. All 57 projects represented in `report_reviewers` do have workflow metrics; all 57 project IDs match `projects_current` and `Статусы проектов`.
- `Выплаты по визитам.Статус данных выплат`: `INCOMPLETE` 81,473; `REVIEW` 7,391.
- `Выплаты по проектам.Статус данных выплат`: `INCOMPLETE` 1,759; `REVIEW` 211; `COMPLETE` 28.
- `report_reviewers.source_status`: 722 `CURRENT`; this is corrector collector/source status, not report workflow status.
- `project_types` contains 420 mappings; 56/57 report-project IDs have a mapping, so one report project currently lacks a project type row.

## Refresh lineage

### Main refresh → project status sheets

1. `src/acquisition.py::acquire_project` performs `GET /proj/{project_id}`, `GET /proj/{project_id}/edit`, and project/date/status-filtered `POST /action` with `state[{code}]=on`. The latter is used as a search/filter, not a business mutation.
2. `src/parsers.py::parse_action_table` parses action-row workflow codes and visit/action IDs. `src/acquisition.py::action_memberships` emits `(project_id, visit_id, workflow_state_code, action_id)` memberships.
3. `src/refresh.py` computes `project_workflow_metrics` over the canonical visit IDs extracted from `/proj/{id}`; counts are filtered to that visit set. It materializes `workflow_state_{code}_visits`, `assigned_visits` (20), `executed_visits_customer` (30), `finished_visits` (a distinct visit matching any of 37/40/50), coverage, unclassified and multi-match counts.
4. `src/workflow_publication.py::third_tab_rows` maps workflow code to exact header label from `WORKFLOW_STATES`, e.g. `Есть претензия [39]`; `src/production.py` publishes/validates `Статусы проектов` and the primary project rows.

### Main refresh → payment sheets

1. `src/production.py` acquires `/pay/detail` payment assignments.
2. `src/payment_detail_xlsx.py::join_payment_assignments` joins payment `my_id` to workflow `action_id` within project, then obtains visit_id.
3. `src/payment_materialization.py::materialize_payment_data` includes only assignments with a qualified visit mapping in `Выплаты по визитам`, groups them by `(project_id, visit_id)`, and unions all workflow codes from those assignments. It does not retain `action_id` in the published tab.
4. Therefore this tab is payment-scoped and can combine memberships from multiple actions on a visit; it is not a universal report-status source.

### Corrector collector → `report_reviewers`

1. `scripts/run_report_corrector_collector.py` discovers a dated report registry using `POST /report/` (`dt1`/`dt2`, read-only filtering) and fetches report details via `GET /action/{report_id}`.
2. `src/report_corrector_collector.py::parse_report_detail` requires one report/action, visit and project link; it parses filing date and selected corrector. The publisher emits only positive corrector IDs.
3. The published tab has one row per discovered report with a corrector; it is a current snapshot, not a complete registry or history. The live status39 report omitted from the same-day registry is a concrete coverage failure for this source.

## Refresh frequency and readback freshness

Read-only systemd inspection on `jfb-reserve` confirmed both timers enabled and active: `j4b-refresh.timer` at 10:00 and 17:00 MSK; `j4b-report-correctors.timer` at 18:15 MSK. At inspection, last triggers were 25.10 and next triggers 26.10; the report sheet's maximum fetch timestamp was 25.10 18:15:11 MSK. No service was manually started.

## Join results and source conflicts

- `report_reviewers` → `Выплаты по визитам` by `(project_id, visit_id)`: 720/722 matches; 2 report visits are absent from payment visit output, and no duplicate composite keys matched. Live project-filtered action query found both unmatched visits and the current report/action rows; both had code 50. Their exact absence reason in the payment mapping remains unconfirmed.
- Live action search found one of those visits with two action/report IDs: old report 2026-09-24/code15 and current report 2026-10-01/code50. Joining only by visit would assign the old code15 to the current report.
- Among 720 payment-joined corrector reports, membership counts were code35=7, code38=1, code39=0, code37=0, code40=696, code50=139 (overlap allowed); all 720 had at least one code in `{37,40,50}`.
- In populated project aggregate rows, code39 sums to 32 in both `projects_current` and `Статусы проектов`; 7,471 project rows have blank workflow fields. In payment visit records, code39 appears on 50 composite visit keys. These are different grains/populations; the discrepancy is unresolved and proves they cannot be substituted for one another.
- A live status39 action/report with corrector selected was found outside the 722-row sheet/checkpoint and absent from the same-day report registry. The observed sheet is therefore incomplete even though its own IDs reconcile perfectly to the checkpoint.

## Implications for a future DataLens dataset

Use project aggregates only for project-level dashboards. For controller quality, preserve a report/action-level snapshot and a separate workflow-membership fact keyed by action/report (plus project/visit). Use `visit_id` only as the business operation key for distinct-operation aggregation and drilldown; do not join the payment visit status list blindly to reports. Keep status membership separate from transition events. Do not publish `review_date`, `ever_had_status`, return counts or a controller error rate until a complete event source and validated outcome semantics exist.

No tab, dataset, formula, DataLens configuration, production refresh or timer was changed during this investigation.
