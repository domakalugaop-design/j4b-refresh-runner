# Предложение структуры датасета качества контролёров

**PORTAL-001.3 — проект, не реализован**

Dashboard, dataset, таблицы и публикации в рамках исследования не создавались.

## Принципы

1. Не публиковать report-level status или completed-review flag, пока источник/семантика не подтверждены.
2. Сохранять разные grains: snapshot отчёта — одна строка на report_id; история — одна строка на status transition.
3. Счётчики и доли строить из DISTINCT report_id в выбранной когорте; не суммировать повторно строки transition fact.
4. В drilldown показывать report_id, корректный статус/событие, его дату и URL исходного отчёта.
5. Претензия не тождественна ошибке корректора; для атрибуции нужна временная связь с завершённой проверкой и outcome претензии.
6. Не преобразовывать `last_successful_fetch` в `review_date`.

## Dataset A: `report_controller_snapshot` (предпочтительный base grain)

**Grain:** одна текущая snapshot-строка на `report_id`, с отдельным `snapshot_at`/`source_observed_at`.

| Поле | Тип/nullable | Статус | Источник/правило |
|---|---|---|---|
| `report_id` | string / required | CONFIRMED | `report_reviewers.report_id`; decimal identifier normalized as string |
| `action_id` | string / required | CONFIRMED on observed 722 | `report_reviewers.action_id`; equals report_id 722/722 in current readback |
| `visit_id` | string / required for observed rows | CONFIRMED on observed 722 | `report_reviewers.visit_id`; do not infer report status from it |
| `project_id` | string / required | CONFIRMED | `report_reviewers.project_id`; project dimension join separately validated |
| `project_name` | string / nullable | NOT IN current sheet | Could be resolved through project dimension; join has not been tested in this discovery |
| `corrector_id` | string / nullable in population; present for published set | PARTIAL | Current selected `staff` (“Проверил”), not proof of completed review |
| `corrector_fio` | string / nullable | PARTIAL | Current selected display value; restrict access and avoid unnecessary copies |
| `report_date` | date / nullable | CONFIRMED | Filing date from action page, not visit/review/claim date |
| `current_report_status_id` | integer / nullable | UNKNOWN | Do not fill with workflow state 39 or collector `source_status` |
| `current_report_status_name` | string / nullable | UNKNOWN | Requires report-level status catalog |
| `review_date` | timestamp / nullable | UNKNOWN | Requires completed review event/timestamp |
| `review_completed` | boolean / nullable | UNKNOWN | No separate completion event confirmed |
| `source_status` | string / required for source health | CONFIRMED | Collector status only (`CURRENT`, etc.); name it `corrector_source_status` to avoid semantic collision |
| `source_observed_at` | timestamp / required | CONFIRMED | `last_successful_fetch`; snapshot time, not review time |
| `report_url` | URL / nullable | PARTIAL | Existing qualified route `https://lk.j4b.ru/action/{report_id}`; verify before production dataset |

`ever_client_claim_39`, `ever_returned_to_shopper`, `shopper_return_count`, `last_status_change_at` **must not be emitted yet**: source history and report-level join remain unverified.

## Dataset B: `report_status_transition` (only if event history is confirmed)

**Grain:** one row per immutable status transition, not one row per current report.

| Поле | Требование |
|---|---|
| `transition_id` | Unique source event key; do not fabricate if absent |
| `report_id` | FK to `report_controller_snapshot.report_id` with validated join cardinality |
| `previous_status_id`, `previous_status_name` | Nullable only where source explicitly has no previous value |
| `new_status_id`, `new_status_name` | Exact catalog values from same entity level (report, not visit/project) |
| `changed_at` | Source event timestamp with timezone/precision documented |
| `changed_by_id` | Source actor ID; nullable only when source omits it |
| `source_event_version` / `source_event_id` | Needed for idempotent append-only ingestion |
| `report_url` | Link to source report/action page |

Only this event-grain dataset can support defensible `EVER_HAD_STATUS` and `STATUS_TRANSITION_COUNT`, subject to retention/completeness guarantees. If no transition log exists, show `CURRENT_STATUS` with its as-of date and label historical metrics unavailable.

## Metric formulas after acceptance gates

- `checked_reports(controller, cohort) = COUNT(DISTINCT report_id)` where a confirmed completion event exists and corrector attribution is effective at review time.
- `claim39_reports(controller, cohort) = COUNT(DISTINCT report_id)` with confirmed report-level claim event/status 39 in the specified interpretation (current vs ever).
- `shopper_returned_reports = COUNT(DISTINCT report_id)` with confirmed report-level return-to-shopper event.
- `shopper_return_count = COUNT(transition_id)` only if each return transition is represented exactly once.
- `claim_share = claim39_reports / checked_reports`; both numerator and denominator must use the same cohort/time basis.

Until a completed-review event is confirmed, these formulas are **specifications only**, not computable metrics. Do not use row count in `report_reviewers` as denominator.

## Release gates before materialization

1. Obtain approved read-only access to portal GET and the asserted PostgreSQL analytics connection; do not use authentication POST while that is prohibited.
2. Discover schema and exact status catalog for each entity; distinguish report vs visit vs project.
3. Validate bounded real cases for status 39, shopper-return, repeated transition, no-controller and ordinary completed report; no synthetic rows.
4. Confirm report-to-status join is one-to-one for current status and event-grain cardinality for history; test duplicates and missing FK.
5. Confirm `review_date` and corrector-at-event attribution; otherwise suppress “checked by controller” comparison.
6. Re-read current Google Sheets data and reconcile IDs; do not assume the 722 baseline remains current.
7. Only after these gates: implement the physical dataset and dashboard in a separately authorized task.
