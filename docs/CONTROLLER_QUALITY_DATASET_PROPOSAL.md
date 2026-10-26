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

## Dataset A: `report_controller_snapshot` (base report/version grain)

**Grain:** одна текущая snapshot-строка на `report_id`, с отдельным `snapshot_at`/`source_observed_at`.

| Поле | Тип/nullable | Статус | Источник/правило |
|---|---|---|---|
| `report_id` | string / required | CONFIRMED per observed row; population incomplete | `report_reviewers.report_id`; decimal identifier normalized as string; global registry omitted a report visible in the project report list |
| `action_id` | string / required | CONFIRMED on observed 722 | `report_reviewers.action_id`; equals report_id 722/722 in current readback |
| `visit_id` | string / required for observed rows | CONFIRMED on observed 722 | `report_reviewers.visit_id`; do not infer report status from it |
| `project_id` | string / required | CONFIRMED | `report_reviewers.project_id`; project dimension join separately validated |
| `project_name` | string / nullable | NOT IN current sheet | Could be resolved through project dimension; join has not been tested in this discovery |
| `business_operation_key` | string / required for aggregation | PARTIAL | Composite `project_id:visit_id` follows the owner’s operation grain; a visit can have several report/action versions, so do not dedupe source detail by visit |
| `corrector_id` | string / nullable in population; present for published set | PARTIAL | Current selected `staff` (“Проверил”), not proof of completed review; registry has demonstrated a report omission |
| `corrector_fio` | string / nullable | PARTIAL | Current selected display value; restrict access and avoid unnecessary copies |
| `report_date` | date / nullable | CONFIRMED | Filing date from action page, not visit/review/claim date |
| `workflow_membership_snapshot_at` | timestamp / nullable | PARTIAL | Timestamp of ingestion/snapshot; not a status transition time |
| `review_date` | timestamp / nullable | UNKNOWN | Requires completed review event/timestamp |
| `review_completed` | boolean / nullable | UNKNOWN | No separate completion event confirmed |
| `source_status` | string / required for source health | CONFIRMED | Collector status only (`CURRENT`, etc.); name it `corrector_source_status` to avoid semantic collision |
| `source_observed_at` | timestamp / required | CONFIRMED | `last_successful_fetch`; snapshot time, not review time |
| `report_url` | URL / nullable | PARTIAL | Existing qualified route `https://lk.j4b.ru/action/{report_id}`; verify before production dataset |

Do not put one scalar `current_report_status_id` into this base table yet: live action rows can carry multiple workflow code memberships simultaneously. Do not emit `ever_client_claim_39`, `ever_returned_to_shopper`, `shopper_return_count`, or `last_status_change_at`: no transition/event source has been confirmed.

## Dataset B: `report_status_membership_snapshot` (needed for current workflow counts)

**Grain:** one row per `(project_id, action_id/report_id, status_id, snapshot_at)`. Keep the action/report key. `visit_id` is a business operation key, not the sole status join key: one bounded live sample had two distinct action/report IDs for one visit.

| Поле | Статус / правило |
|---|---|
| `project_id`, `visit_id`, `action_id` | Directly from the existing project-scoped `/action` read/search response |
| `report_id` | Join to report detail/current corrector snapshot only on validated action/report link (current sheet: 722/722 equality; validate every refresh, do not hardcode global equality) |
| `status_id`, `status_name` | `WORKFLOW_STATES` mapping and `Статусы проектов` header catalog; store membership rows rather than one exclusive status |
| `snapshot_at` | Refresh observation timestamp; not `changed_at` |
| `corrector_id` | Join from `report_controller_snapshot` by report/action key, if present; do not attribute an old action to the current report on the same visit |
| `report_url` | `https://lk.j4b.ru/action/{report_id}` after per-row key validation |

This is a **snapshot membership table**, not an event history. For a business metric that counts one operation once, use `COUNT(DISTINCT project_id, visit_id)` after deciding which report/action version is operative. Keep report/action IDs for drilldown and QA.

## Dataset C: `report_status_transition` (only if event history is confirmed)

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

Only this event-grain dataset can support defensible `EVER_HAD_STATUS` and `STATUS_TRANSITION_COUNT`, subject to retention/completeness guarantees. If no transition log exists, show `workflow_membership_as_of` and label historical metrics unavailable.

## Metric formulas after acceptance gates

- `checked_operations(controller, cohort) = COUNT(DISTINCT project_id, visit_id)` after a validated completion rule and corrector attribution; `finished_visits` `{37,40,50}` is an existing project metric, but review completion semantics/attribution must be accepted before using it as controller denominator.
- `claim39_operations(controller, cohort) = COUNT(DISTINCT project_id, visit_id)` with a confirmed current action/report membership 39 and complete population coverage. Current observed sheet join gives 0/720 matched membership rows, but one live 39 report with corrector is omitted from sheet/checkpoint/registry; no production-ready denominator/numerator yet.
- `shopper_returned_reports = COUNT(DISTINCT report_id)` with confirmed report-level return-to-shopper event.
- `shopper_return_count = COUNT(transition_id)` only if each return transition is represented exactly once.
- `claim_share = claim39_reports / checked_reports`; both numerator and denominator must use the same cohort/time basis.

Until report inventory completeness, action/report version selection, and completed-review attribution are confirmed, these formulas are **specifications only**, not production-ready metrics. Do not use row count in `report_reviewers` as denominator.

## Release gates before materialization

1. Keep the existing approved portal auth flow plus project/date/status-filter `POST /action` strictly read-only; PostgreSQL is not required for this proposal and was not used.
2. Preserve project→visit→action/report links and the exact status source; distinguish project sums, payment-mapped visit snapshots, report/action memberships and transition events.
3. Reconcile registry completeness: one live status39 report was in the project report list but not the day/3-day/month global registry/checkpoint/sheet; the server-side inclusion rule remains unknown.
4. Validate bounded real cases for status 39, codes 35/38, multiple action/report records on one visit, no-controller and a completed/approved report; keep samples private.
5. Confirm report-to-action status join cardinality and how to select the operative report version for one business operation.
6. Confirm `review_date` and corrector-at-review attribution; otherwise suppress “checked by controller” comparison.
7. Re-read current Google Sheets data and reconcile IDs; do not assume the 722 baseline remains current.
8. Only after these gates: implement the physical dataset and dashboard in a separately authorized task.

The development branch also contains an **opt-in** `--supplement-project-report PROJECT_ID` recovery path: one explicit, non-paginated project report page, one project per invocation, and no more candidate IDs than the detail GET budget. It is not enabled by default or by the production timer, was not run here, and cannot guarantee global coverage.
