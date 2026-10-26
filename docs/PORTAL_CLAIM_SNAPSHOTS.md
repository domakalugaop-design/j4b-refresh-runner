# Portal Status 39 Investigation and Claim Snapshots (2026-10-26)

## Business semantics

Portal status 39 («Есть претензия») is a client claim concerning the company's
service result. It is NOT a controller remark, NOT proof of controller fault,
and NOT a count of separate claim messages. One visit in status 39 counts as
one observed visit with a claim indicator.

## Controlled Portal query (read-only search)

- Endpoint: `POST https://lk.j4b.ru/action` (read-only search, same semantics
  as the Portal UI filter; no mutation endpoints were submitted).
- Filter parameters confirmed from the live form: `dt1`, `dt2` (HTML date
  inputs), `user`, `place`, `state[0..50]` checkboxes, `empty`, `limit`
  (default 1000). The portal-wide form has no project selector; omitting
  `proj` is the native all-projects semantics.
- Query: `state[39]=on`, window `2026-10-01..2026-10-26`, `limit=10000`,
  `send=send`. Two runs (ISO and `DD.MM.YYYY` date formats) produced the
  same status-39 result set.
- Result: 13 visits with current status 39 across 10 projects; the response
  also contains ~1550 unassigned visits (`/action/0`, no workflow status),
  which the portal includes in state-filtered results. They are excluded
  from claim observations.
- No pagination markers; `limit=10000` exceeds the row count, so the result
  is complete for this filter.

## Date filter semantics

`dt1`/`dt2` are NOT enforced on the status-39 subset: the result includes a
2023 test-project visit and a visit dated 2026-10-30 (outside the submitted
window). Both date formats behave identically. The submitted window is
therefore recorded as a query label, not as a guaranteed coverage bound.
Observed row datetimes are stored with `date_semantics=UNCONFIRMED`; they
are never relabelled as visit/report/claim-event dates.

## Reconciliation with Google Sheets

- Source `report_reviewers` (722 rows, all `source_status=CURRENT`): 0 of 13
  portal status-39 action IDs present; 0 of 13 visit IDs present.
- TEST mart `controller_report_snapshot` (722 rows): 0 hits; `status_39`
  column holds 719 `OBSERVED_ABSENT` + 3 `UNKNOWN`.
- Verdict: the mart's zero status-39 count is a confirmed false zero caused
  by source coverage. The collector's discovery path (global `/report/`
  registry) does not return status-39 reports, while the native `/action`
  `state[39]` filter returns all of them. This matches the previously
  proven report 680368 registry gap.

## Historical snapshot design

Append-only snapshots; every collection carries its own `snapshot_id`;
repeated report/visit IDs across snapshots are expected; nothing is
overwritten. Grain of one observation:
`(snapshot_id, project_id, visit_id, action_id, status_id)`.

Required fields: `snapshot_id`, `captured_at_utc`, `source_generation`,
`report_id`, `visit_id`, `project_id`, `controller_id`, `status_id`,
`report_date`, `visit_date`, `source_quality`, optional
`published_at_utc`. For this source `report_id`, `controller_id`,
`report_date` and `visit_date` stay empty: the `/action` response does not
prove `action_id == report_id`, provides controller names without stable
IDs, and its datetime columns have unconfirmed semantics.

Provenance extras: `action_id`, `action_url`, `observed_datetimes`,
`date_semantics`, `response_sha256`, `query_window`,
`date_filter_enforced`, `unassigned_rows`, `response_rows`.

Snapshot manifest (one row per collection): response fingerprint, row
counts, distinct visits/projects, pagination markers, collection
completeness, collection mode, warnings.

## TEST workbook storage

Created in the TEST workbook `1G_pXXNCLTwABSnSPtXxRCxF19VOCOvXi-DK21ZMzytM`
(portal-refresh service account, Editor):

- `visits_snapshot_history` — append-only observations; first snapshot
  published: `076a3519dee3433284b553cbc839f472`, 13 records.
- `visits_snapshot_manifest` — one manifest row per snapshot.
- `visits_current` — schema prepared (header only; population belongs to
  the future daily pipeline).
- `claims_trend` — schema prepared with an explicit
  `claim_event_time=UNKNOWN` note; report-date cohorts, snapshot
  observation times and claim event times must never be conflated.

Publication is idempotent per `snapshot_id`: a re-run with identical content
returns `ALREADY_PUBLISHED`; a conflicting snapshot under the same id is
refused without writing.

## Local pipeline

- `src/claim_snapshot.py` — fail-closed parsing, snapshot record building,
  grain/idempotency validation.
- `scripts/run_claim_snapshot.py` — local HTML file → snapshot JSON
  (no network, no writes).
- `scripts/publish_claim_snapshot_test.py` — controlled TEST publication
  with allowlist, explicit approval flag and idempotency gates.
- Tests: `tests/test_claim_snapshot.py`,
  `tests/test_claim_snapshot_publication_gates.py`.

## Limitations

- The status-39 result is the complete current-state filter result, not a
  transition history; historical claim events remain unknown.
- The ~1550 unassigned visits in the filtered response are a portal filter
  quirk; they carry no workflow status and are excluded.
- Date semantics of the observed row datetimes are unconfirmed.
- Controller attribution is not available from this source.
- The daily refresh dependency (collector 18:15 MSK → validation →
  snapshot → publication) is designed but not deployed; production
  collectors and timers were not modified.
