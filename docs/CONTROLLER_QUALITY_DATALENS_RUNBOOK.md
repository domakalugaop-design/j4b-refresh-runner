# DataLens runbook — controller quality mart

## Prerequisite and access

The owner must create a **new native Google spreadsheet** and share it with the existing J4B service account as Editor. Give DataLens access using the organization-approved Google OAuth identity. Do not make the workbook public or share it broadly: `corrector_fio` is employee personal data. The target ID must be configured as `CONTROLLER_QUALITY_SPREADSHEET_ID` and must differ from the existing J4B materialized spreadsheet.

Create exactly these tabs with the exact header rows from `src/controller_quality_mart.py`:

1. `controller_report_snapshot`
2. `controller_visit_summary`
3. `data_quality_status`
4. `metric_dictionary`

Do not add sample production rows. A new blank workbook can contain headers only; the first active generation is created by a later separately approved deployment.

Yandex documents that one Google Sheets connection can include multiple tabs as separate tables and supports joins between tables from the same source. Use one connection to this workbook; keep each dataset’s generation filter within that same source. See [Google Sheets connection setup](https://yandex.cloud/ru/docs/datalens/operations/connection/create-google-sheets) and [dataset joins](https://yandex.cloud/ru/docs/datalens/concepts/data-join). Validate the current UI’s field types and syntax before saving the dataset.

## Dataset pattern

Create a dataset per fact grain. Join that fact to `data_quality_status` using `publication_key`, then filter the fact’s `generation_id` to the single manifest `active_generation`.

Fields:

- report fact: report/action/visit/project IDs, report date, corrector ID/FIO, status flags, report URL, source status, coverage flag.
- visit fact: project/visit IDs, all observed report IDs, report-record count, known corrector IDs, unresolved-current-version label.
- workflow status: report-level tri-state flags for supported codes; current observed memberships, not a transition log.
- metric dictionary: metric key, display name, definition, source fields, availability, and limitation.
- quality manifest: source and published counts, unmatched rows, ambiguous versions, unknown statuses, source freshness, refresh status, active generation, coverage status.

## Suggested charts and measures

Use only the filtered active generation. Label counts as **observed snapshot** measures; the source is not the complete Portal population and does not establish completed controller reviews.

- **Summary:** observed reports, composite observed visits, controllers, projects, reports with current observed status 39, visits with current observed status 39, and status-39 share among status-known observed entities.
- **Controller associations:** distinct reports, distinct composite visits, distinct projects, status-39 reports/visits, and same-grain observed status-39 shares by `corrector_id`/`corrector_fio`. These are associations in this source, not completed-review workload or performance. Visit/project totals may overlap across controllers and are non-additive.
- **Project associations:** project ID, associated controller count, observed composite visits/reports, current status-39 counts/shares. Project names are blank in the current schema; do not infer them.
- **Detail:** controller, project ID, visit ID, report/action ID, report URL, observed status flags, report date, source status and coverage flag.
- **Version and coverage:** multi-report visits and unresolved current version; unknown exact status matches; generation freshness and source coverage.

Status-39 membership is current observed `/action` membership. It is not a historical transition, a count of individual claims, a substantiation result, or evidence of fault. Label rates “доля наблюдаемых … с известным статусом 39”, never as a Portal-wide claim rate.

Calculated fields (verify the current DataLens UI accepts the syntax before saving):

```text
Composite visit key = CONCAT(STR([project_id]), "|", STR([visit_id]))
Observed reports = COUNTD([report_id])
Observed visits = COUNTD([Composite visit key])
Known-status reports = COUNTD(IF([status_39] = "OBSERVED_PRESENT" OR [status_39] = "OBSERVED_ABSENT", [report_id], NULL))
Status-39 reports = COUNTD(IF([status_39] = "OBSERVED_PRESENT", [report_id], NULL))
Status-39 report share = [Status-39 reports] / [Known-status reports]
Known-status visits = COUNTD(IF([status_39] = "OBSERVED_PRESENT" OR [status_39] = "OBSERVED_ABSENT", [Composite visit key], NULL))
Status-39 visits = COUNTD(IF([status_39] = "OBSERVED_PRESENT", [Composite visit key], NULL))
Status-39 visit share = [Status-39 visits] / [Known-status visits]
Unknown-status reports = COUNTD(IF([status_39] = "UNKNOWN", [report_id], NULL))
```

Guard each ratio against a zero denominator and preserve NULL, not 0. Report-grain rates use distinct `report_id`; visit-grain rates use distinct `(project_id, visit_id)`. For a visit with several report/action rows, numerator membership is 1 if any associated row is `OBSERVED_PRESENT`; denominator membership exists if any associated row has a known status. Grouped visit/project counts can overlap, so never sum groups into an overall total. Do not publish an “unusually high claim rate” flag until a threshold, minimum sample size, and complete comparable population are approved.

## Dashboard construction

1. Open Yandex DataLens and create a Google Sheets connection to the owner-provisioned workbook.
2. Verify all four tabs appear as separate tables and headers are recognized.
3. Create report, visit, quality, and metric-dictionary datasets; apply active-generation filtering to generated data.
4. Verify IDs remain dimensions (string or integer consistently), dates parse as dates, status flags remain strings, and counts are measures.
5. Add filters for corrector, project ID, report date, status membership, and coverage label. Date is report filing date, not visit or review date.
6. Add a drilldown link from `report_url`; preserve exact action/report ID.
7. Display coverage and freshness prominently. Never label the dashboard as a complete company-wide audit.
8. Compare control totals with `data_quality_status`; ensure counts match the generation rows. Independently validate report and composite-visit counts/rates against `summarize_observed_metrics` from the same candidate generation.
9. Keep this as a separate dashboard. Do not modify the existing customer dashboard in this task.

Google Sheets connection refresh and DataLens cache timing can delay visible changes; freshness should be read from the manifest, not inferred from when a chart happens to refresh.

## Test publication and DataLens status (2026-10-26)

The initial connection failure described in earlier revisions is resolved. The owner-approved historical backfill populated the private TEST workbook with real accumulated source data; the existing DataLens connection then loaded the four tabs. Independent readback verified the four schemas, data rows, IDs, generation consistency, and publication counts. No synthetic rows were used. The private TEST dashboard and its report/visit datasets are live in the owner's DataLens account. See the dated implementation record below for exact URLs and limits.

Yandex documentation: [Google Sheets connection requirements](https://yandex.cloud/ru/docs/datalens/operations/connection/create-google-sheets) and [troubleshooting “Could not upload file”](https://yandex.cloud/en/docs/troubleshooting/datalens/known-issues/unable-to-load-file-when-using-gsheets-as-data-source).

## Owner-approved four-day publication

The observed `report_reviewers` source is an accumulated current registry, not four independent daily snapshots. Its four observed `last_successful_fetch` dates are source-row provenance only. Do not duplicate rows into daily history or infer historical status transitions. The isolated `--historical-backfill` mode accepts only the reviewed source and fixed TEST workbook, validates the reviewed row/project/controller counts and fetch-date coverage, and labels the manifest `PUBLISHED_HISTORICAL_BACKFILL` / `ACCUMULATED_4_DAY_REGISTRY_CURRENT_STATUS`. It queries current `/action` membership only for the source's 57 project IDs and only publishes exact report/action/visit join keys. Consequently, status 39 is current as of the generation timestamp; the report date is the only verified business-date filter. This mode does not relax the normal same-day collector gate for `--publish`.

## Dashboard business requirements and delivered scope — 2026-10-26

The owner-approved TEST mart is an accumulated report registry, not four independent daily snapshots. The four `last_successful_fetch` dates are row provenance, not a status-history axis. Therefore no trend, baseline average, or moving average of claims is supportable from this generation. The only date filter is `report_date` (report filing date); it is not visit date, review date, or claim date. Separate year/month/day selectors and a visit-date trend are not available from the source.

Status 39 (`Есть претензия`) is a client-facing workflow membership. It does not count individual claim events and does not attribute fault to a controller. The qualified share is distinct composite visits with any associated status-39 row divided by distinct composite visits with at least one known status membership (`OBSERVED_PRESENT` or `OBSERVED_ABSENT`). `UNKNOWN` is excluded, and zero denominators remain NULL. Numerator and denominator use the same `(project_id, visit_id)` grain. For this generation there are 719 visits with known status and 3 with unknown status; no status-39 membership was observed, so the observed share is 0/719. This is a current observed sample statistic, not a Portal-wide claim rate.

The private dashboard currently contains a total-observed-visits indicator (722 for this generation), controller report distribution, current status-39 observation, project visit distribution, a report-detail table, and explanatory coverage text. Filters for controller, project, report filing date, and status-39 observation were exercised and reset. The detail table exposes report/action identifiers and report URL, but direct link navigation was not part of readback. The current source supports 722 distinct reports / 722 distinct visits, 22 associated controllers, and 57 represented projects, with report dates 2026-10-01 through 2026-10-25. Project names are not present in the source and must not be inferred.

The dashboard is intentionally a bounded operational preview, not a full company-wide quality scorecard. Per-controller/per-project averages can be shown only as associations within this sample and must not be presented as rankings or causal attribution. Average per interval, trend, baseline/moving average, complete Portal claim totals, and individual claim-event counts remain `NOT_AVAILABLE` until an authoritative history/event source and complete population are qualified. Synthetic examples belong in unit tests only.
