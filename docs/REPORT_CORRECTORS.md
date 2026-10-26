# Report correctors (`report_reviewers`)

Collects, for every portal report filed on or after **2026-10-01**, the report's
corrector ("Проверил") together with its project, visit and filing date, and
publishes them into the `report_reviewers` tab of the materialized-layer
spreadsheet.

Status: collection and publication path have been verified against the live
portal and spreadsheet. A 2026-10-26 readback showed 722 unique `report_id`
rows, all reconciled to the schema-2 checkpoint. This is a corrector-only
materialized set, not a completeness guarantee for all portal reports: a later
bounded check reproduced a report absent from the global registry/checkpoint/tab.

## Verified portal model

| Question | Answer | Evidence |
| --- | --- | --- |
| Detail page for a report | `/action/{id}` | `/report/{id}` redirects to `/report/`; three sampled IDs returned the identical listing page |
| Report/action join | `report_id == action_id` | current Sheets readback 722/722 plus limited live detail samples; each sampled action page links one report, project and visit |
| Corrector field | `select[name=staff]` on the action page | parsed to `VALUE_PRESENT` with a numeric employee ID and a 3-word FIO |
| Report filing date | `input[name=dt]` on the action page | `/proj/7986/report` lists each report under exactly that date |
| October inventory | `POST /report/` with `dt1 = dt2 = day` | see coverage calibration below |

### Why the registry, not the project page

`/proj/{id}/report` is project-scoped and its observed period behavior is not
uniform across projects. Project 7986 showed 1124 reports dated
2026-09-01..2026-09-30 and 0 October rows at the earlier probe; it had no date
or month filter (`textarea[name=log-body-25]` was the only field). In the new
bounded check, another project's page contained five report IDs including a
17-October report omitted by the global registry. Therefore a project journal
can be a cross-check for a known project, but it is not established as a
complete global inventory or a universal replacement for `/report/`.

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

The registry is therefore a cross-project superset in that calibration, with
1123/1124 observed project reports present; one report was absent. **99.91% is a
historical sample result, not a guaranteed completeness bound.** A later bounded
check found a live status39 report filed 2026-10-17 (corrector selected and
detail route valid) that was not in the day-filtered registry response, the
checkpoint, or `report_reviewers`; the same project report listing did contain
it. Day, 3-day and October-range global registry reads returned 144, 468 and
5,058 IDs respectively without the sample. The responses showed no pagination
controls, and the sample ID was absent from raw registry HTML, so the collector
parser did not discard a received link. The source inclusion rule remains
unknown; October registry completeness is known to be imperfect, with total
missed count unknown.

## Semantics

- `report_id` — portal report ID; equals `action_id` by verified construction.
- `action_id` — same value, kept as a separate column because the detail page is
  an action page.
- `visit_id`, `project_id` — taken from the action page's single visit/project
  link. A page that resolves to zero or several of either fails closed.
- One visit may have multiple distinct action/report IDs in the portal. The
  collector keeps report IDs separate and does not collapse them by visit; the
  business rule for selecting one operative version per visit is not yet
  established.
- `report_date` — the action page's `dt`, i.e. the date the portal files the
  report under. It is **not** the visit date and **not** a corrector timestamp.
- `corrector_id` / `corrector_fio` — the selected `staff` option, only when the
  selection is present. `NO_CORRECTOR` means the report carries no corrector:
  the portal marks that with the same `value="0"` placeholder option used for
  the empty first row (verified on a real report: 87 options, `value="0"`
  selected, no label). A zero or blank id is therefore "not assigned", not a
  malformed control.
- `source_status` — `CURRENT` (fresh read with a corrector), `NO_CORRECTOR`
  (fresh read, nothing assigned), `CORRECTOR_REMOVED` (a corrector that was
  previously recorded is no longer selected), `STALE_LAST_GOOD` (refresh
  failed, previous values kept), `FETCH_FAILED_NO_HISTORY` (failed, no previous
  values), `PENDING`.
- `last_successful_fetch` — UTC ISO-8601 timestamp of the last successful read.

### Visit/action versioning

`report_id == action_id` is confirmed for each tested detail record and 722/722
current sheet rows, but it does **not** imply one action/report per visit. A
bounded live sample found two separate report/action IDs on one visit (older
code 15 with no selected corrector; later code 39 with a selected corrector).
Do not deduplicate reports by `visit_id` or assume the highest ID/date is the
current version. A limited project report listing surfaced the later report and
not the older one in that sample, but no global current-version contract was
found. See `docs/ACTION_VERSIONING_FINDINGS.md`.

## Corrector-only dataset

The published tab contains only reports with a real corrector: a positive
numeric `corrector_id`. Everything discovered is retained in the checkpoint, so
nothing is lost, but `NO_CORRECTOR` and `CORRECTOR_REMOVED` reports are never
written to the sheet. This is enforced at three points, not by convention:

1. `render_sheet_rows` skips any row without a positive corrector id;
2. `validate_sheet_rows` rejects a published row whose `corrector_id` is not a
   positive integer or whose status is `NO_CORRECTOR`/`CORRECTOR_REMOVED`;
3. the sheet is replaced from that rendered set, so a row that loses its
   corrector disappears from the tab instead of remaining as a stale entry.

### Reconciliation

`reconcile_dataset` compares the current publishable set with what the tab last
received (recorded by `mark_published` after a verified publication) and returns
`added`, `updated`, `removed` and `unchanged`. `removed` is the explicit
reconciliation signal for a corrector that disappeared: the report leaves the
target set, keeps its withdrawn id and timestamp in the checkpoint for audit, and
is reported in the run JSON. Repeated runs converge — the second run after a
publication reports `added=0, updated=0, removed=0`.

### Recheck policy

A corrector assignment is treated as **stable**, not as something to rediscover
daily. Re-reading a resolved report is a deliberate act with a cost, so the
schedule is deliberately sparse and bounded:

| State | Scheduled re-read | Reason |
| --- | --- | --- |
| `CURRENT` | none | a published corrector is stable; a mass weekly re-read would spend the whole GET budget to confirm what is already known |
| `NO_CORRECTOR` | up to 2, inside 7 days | this is the pool that later gains a corrector: 48% of pilot reports had none on filing day |
| `NO_CORRECTOR_EXHAUSTED` | none | the two-recheck budget is spent; the row is terminal and costs nothing |
| `CORRECTOR_REMOVED` | none | terminal; a restoration is found only by an explicit manual recheck |

`RecheckPolicy` holds the two delays (`no_corrector_delays`, default +1 d then
+2 d) and the window (7 days, measured from the first empty read). The third
empty read marks the report `NO_CORRECTOR_EXHAUSTED`. A confirmed non-empty read
resets the counter and makes the row `CURRENT`.

Re-reading a stable row is an operator decision:

```bash
python3 scripts/run_report_corrector_collector.py --state ... \
    --max-fetches 20 --recheck 671045 --recheck 671046,671047
```

A manual request clears the backoff, sets the report due, and is still subject
to the per-run budget and the request interval. It never changes a corrector
without a confirmed page read, and `CURRENT` rows keep their previous value in
`last_good` until that read succeeds.

### Checkpoint schema and migration

`schema` is `2`. Loading a v1 checkpoint calls `migrate_state`, which is
idempotent and never deletes history: it clears scheduled rechecks on
`CURRENT`/`CORRECTOR_REMOVED` entries and gives a `NO_CORRECTOR` entry the
unspent part of its two-recheck budget, anchored on its first successful read.
A migration is reported in the run JSON as `migration`.

### Discovery coverage

Registry coverage is not assumed complete. `record_discovery` stores per-day
`runs`, `unique` and `late_adds` counts, and `coverage_summary` reports how many
discovered days gained IDs after their first scan. This keeps drift visible (a
day that first yielded 459 IDs later yielded 412 for the same filter) instead of
silently trusting a single pass. Discovery is idempotent, so a late arrival is
simply a new unresolved row on a later run.

## Operation

```bash
# incremental: re-discover the last 3 days, fetch up to 50 details
# (hard ceiling: 200 detail GETs, minimum interval 1.5 s)
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
(09:55-10:20 and 16:55-17:20 MSK), refuses `--max-fetches` above 200, refuses an
interval below 1.5 s, and stops the batch immediately on HTTP 429/5xx or after
three consecutive failures, leaving the queue and the checkpoint intact.

The tab is written only by this collector. It must never be written from the
production refresh transaction: that transaction fingerprints every non-target
tab and rolls back on any mismatch.

| Concern | Owner |
| --- | --- |
| `projects_current`, `project_types` | production refresh |
| `report_reviewers` | this collector, separate job |
| schedule | 18:15 MSK, after the 16:55-17:20 window; one job at a time |

Publication is isolated end to end: its own checkpoint, backup directory,
readback and rollback. A collector failure cannot abort or roll back the
production refresh, and a refresh failure cannot corrupt the review dataset.

## Safety invariants

- The upstream portal is read-only here: only `GET` and the registry's filter
  `POST` are used, never a state-changing submission.
- One bad page never corrupts the sheet: parsing fails closed, and a failed
  refresh keeps the last good values marked `STALE_LAST_GOOD`.
- The checkpoint contains employee names, so it lives outside the repository in
  a 0700 directory with 0600 file permissions.
- Pre-cutover reports are recorded as out-of-scope and neither retried nor
  published.

## Daily operation

Recommended mode for a daily run (design only; no timer is installed):

1. **Discovery window** — the last 3 calendar days, re-queried every run. A
   report filed late, or one the registry omitted on the day it was filed,
   appears in a later pass because discovery is idempotent and the window is
   wider than one day.
2. **Top-up of missed reports** — every ID already in the checkpoint but still
   unresolved is fetched, regardless of when it was discovered. Nothing is
   dropped because a previous run hit its GET cap.
3. **Bounded recheck of unresolved rows** — only reports that have never had a
   corrector are re-read on a schedule, twice at most inside seven days, because
   a corrector is commonly assigned after filing. In the pilot, 70 of 145
   reports (48%) had no corrector on filing day, so this bounded recheck is what
   fills the sheet — without a weekly mass re-read of the 111 already-resolved
   reports. `CURRENT` and `CORRECTOR_REMOVED` rows cost nothing until an operator
   asks for one with `--recheck`.
4. **Last-good preservation** — a failed re-read keeps the previous values and
   marks the row `STALE_LAST_GOOD`; a fresh report that fails has no history and
   is marked `FETCH_FAILED_NO_HISTORY`.
5. **Request limits** — hard cap of 200 detail GETs per run, minimum 1.5 s
   between requests, exponential backoff per report (300 s doubling to a 24 h
   ceiling), and an immediate stop on HTTP 429/5xx. The remainder stays queued
   and is continued by the next run.
6. **Refresh protection** — the runner refuses to start between 09:55-10:20 and
   16:55-17:20 MSK, and also refuses when `j4b-refresh.service` is active.

### Recommended schedule

| Run | Time (MSK) | Purpose | Cap |
| --- | --- | --- | --- |
| Primary | 18:15 daily | full day's reports after the 17:00 refresh finishes | 300 GETs |
| Optional catch-up | 09:30 daily | late-evening filings before the 10:00 refresh | 100 GETs |

The primary run is deliberately placed 75 minutes after the 17:00 refresh: the
2026-10-16 incident showed that publish phase can run past 17:15, and the guard
alone only covers the window, not an overrunning job.

Measured pilot rate is 2.1 s per report (1.5 s interval plus request latency).
Expected daily load, using the October average of ~166 reports filed per day:
~166 new-report reads, plus the recheck queues (≈48% of the inventory without a
corrector re-read every 3 days, and every published row every 7 days). On a
one-month inventory that is roughly 800-1,200 detail GETs, i.e. 30-45 minutes at
the measured rate — an evening job, not a per-hour one.

### Integration with the production refresh (read-only study)

Integrating the corrector collection *inside* `src.production.run()` is
technically possible but materially risky, and is not done:

- `target_tabs` is exactly `{"projects_current", "project_types"}`
  (`src/production.py:1083`, `:1408`, `:1876`). Every other worksheet is hashed
  by `_fingerprint_non_target_tabs` (`:1097`) before and after publication, and
  any difference raises `RuntimeError` (`:1453-1455`), which runs
  `_rollback_full_refresh` (`:1456-1470`). Creating or writing
  `report_reviewers` during that window would therefore abort and roll back the
  whole five-tab publication — the same failure class as 2026-10-16.
- Making the corrector tab a target would require changing the protected set,
  the rollback contract, and the persistent backup coverage: a production change
  well outside a collector task.
- Data reuse is negligible. The refresh does already fetch the per-project
  `/action` listing for the whole year (`src/acquisition.py:372-383`), but
  `parse_action_table` keeps only visit/action ids and workflow states
  (`src/parsers.py:421-429`) — it has no date and no corrector, so no detail GET
  is saved and October rows cannot be isolated from it reliably.

Conclusion: run the collector as a separate post-refresh process (18:15 MSK).
It shares credentials, the venv and the spreadsheet contract, but owns its tab,
its transaction and its checkpoint, so a corrector failure can never touch the
five production tabs.

Do not schedule at 10:00 or 17:00, and do not run the pilot's full-month
discovery as a daily job: the daily mode needs only the 3-day window.
