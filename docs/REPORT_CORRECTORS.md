# Report correctors (`report_reviewers`)

Collects, for every portal report filed on or after **2026-10-01**, the report's
corrector ("Проверил") together with its project, visit and filing date, and
publishes them into the `report_reviewers` tab of the materialized-layer
spreadsheet.

Status: collection path and publication path are verified end-to-end against the
live portal and live spreadsheet metadata. The `report_reviewers` tab has **not**
been created or written yet.

## Verified portal model

| Question | Answer | Evidence |
| --- | --- | --- |
| Detail page for a report | `/action/{id}` | `/report/{id}` redirects to `/report/`; three sampled IDs returned the identical listing page |
| Report/action join | `report_id == action_id` | 3/3 sampled IDs: the action page links `/report/{same id}`, `/proj/{expected}` and one `/visit/{id}` |
| Corrector field | `select[name=staff]` on the action page | parsed to `VALUE_PRESENT` with a numeric employee ID and a 3-word FIO |
| Report filing date | `input[name=dt]` on the action page | `/proj/7986/report` lists each report under exactly that date |
| October inventory | `POST /report/` with `dt1 = dt2 = day` | see coverage calibration below |

### Why the registry, not the project page

`/proj/{id}/report` is a closed-month journal: project 7986 showed 1124 reports,
all dated 2026-09-01..2026-09-30, with 0 October rows. The page has no date,
month or period control (`forms: ["", ""]`, the only field is
`textarea[name=log-body-25]`), and `dt1`/`dt2` are ignored on `GET` and `POST`
alike. It also does not change when the registry session state changes
(byte-identical responses), so it cannot be steered to October.

The registry (`/report/`) does filter by date, at day granularity with 96-100%
precision (2026-10-02: 100% of returned rows on the requested day).

### Coverage calibration

Independent cross-check on a closed day — project 7986's own journal versus the
registry for the same day:

| Metric | Value |
| --- | --- |
| Project journal unique reports (2026-09-30) | 1124 |
| Registry unique reports (2026-09-30) | 2148 |
| Project reports present in registry | **1123 / 1124 (99.91%)** |
| Registry reports from other projects | 1025 |

The registry is therefore a cross-project superset with 99.91% coverage; one
report of the 1124 was absent. October coverage cannot be cross-checked the same
way because no project page exposes October yet, so October completeness carries
that residual ~0.1% risk plus day-to-day registry variance.

## Semantics

- `report_id` — portal report ID; equals `action_id` by verified construction.
- `action_id` — same value, kept as a separate column because the detail page is
  an action page.
- `visit_id`, `project_id` — taken from the action page's single visit/project
  link. A page that resolves to zero or several of either fails closed.
- `report_date` — the action page's `dt`, i.e. the date the portal files the
  report under. It is **not** the visit date and **not** a corrector timestamp.
- `corrector_id` / `corrector_fio` — the selected `staff` option, only when the
  selection is present. `NO_CORRECTOR` means the field exists but nothing is
  selected (or the selection has an empty value).
- `source_status` — `CURRENT` (fresh read), `NO_CORRECTOR`, `STALE_LAST_GOOD`
  (refresh failed, previous values kept), `FETCH_FAILED_NO_HISTORY` (failed, no
  previous values), `PENDING`.
- `last_successful_fetch` — UTC ISO-8601 timestamp of the last successful read.

## Operation

```bash
# incremental: re-discover the last 7 days, fetch up to 50 details
PORTAL_CREDENTIALS_FILE=/path/portal-auth.json \
  python3 scripts/run_report_corrector_collector.py \
    --state ~/.cache/j4b-report-reviewers/state.json --max-fetches 50

# read-only publication plan against the real spreadsheet
GOOGLE_SERVICE_ACCOUNT_JSON_FILE=/path/google-service-account.json \
  python3 scripts/run_report_corrector_collector.py \
    --state ~/.cache/j4b-report-reviewers/state.json --max-fetches 0 --plan
```

Publication requires **both** `--publish` and
`REPORT_REVIEWERS_PUBLICATION_ENABLED=true`. It creates the tab only when it is
absent, snapshots the previous contents to a private 0600 backup, writes, reads
back, and verifies that the five protected tabs
(`projects_current`, `project_types`, `Статусы проектов`, `Выплаты по визитам`,
`Выплаты по проектам`) are byte-identical; any mismatch triggers a verified
rollback.

The runner refuses to start inside the production refresh windows
(09:55-10:20 and 16:55-17:20 MSK) and defaults to a 1.5 s request interval.

## Safety invariants

- The upstream portal is read-only here: only `GET` and the registry's filter
  `POST` are used, never a state-changing submission.
- One bad page never corrupts the sheet: parsing fails closed, and a failed
  refresh keeps the last good values marked `STALE_LAST_GOOD`.
- The checkpoint contains employee names, so it lives outside the repository in
  a 0700 directory with 0600 file permissions.
- Pre-cutover reports are recorded as out-of-scope and neither retried nor
  published.
