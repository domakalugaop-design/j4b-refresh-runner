# DataLens runbook — controller quality mart

## Prerequisite and access

The owner must create a **new native Google spreadsheet** and share it with the existing J4B service account as Editor. Give DataLens access using the organization-approved Google OAuth identity. Do not make the workbook public or share it broadly: `corrector_fio` is employee personal data. The target ID must be configured as `CONTROLLER_QUALITY_SPREADSHEET_ID` and must differ from the existing J4B materialized spreadsheet.

Create exactly these tabs with the exact header rows from `src/controller_quality_mart.py`:

1. `controller_report_snapshot`
2. `controller_visit_summary`
3. `controller_status_membership`
4. `data_quality_status`

Do not add sample production rows. A new blank workbook can contain headers only; the first active generation is created by a later separately approved deployment.

Yandex documents that one Google Sheets connection can include multiple tabs as separate tables and supports joins between tables from the same source. Use one connection to this workbook; keep each dataset’s generation filter within that same source. See [Google Sheets connection setup](https://yandex.cloud/ru/docs/datalens/operations/connection/create-google-sheets) and [dataset joins](https://yandex.cloud/ru/docs/datalens/concepts/data-join). Validate the current UI’s field types and syntax before saving the dataset.

## Dataset pattern

Create a dataset per fact grain. Join that fact to `data_quality_status` using `publication_key`, then filter the fact’s `generation_id` to the single manifest `active_generation`. Do not join report and status facts directly unless the dataset uses the exact project/action/visit key and handles one-to-many status rows intentionally.

Fields:

- report fact: report/action/visit/project IDs, report date, corrector ID/FIO, status flags, report URL, source status, coverage flag.
- visit fact: project/visit IDs, all observed report IDs, report-record count, known corrector IDs, unresolved-current-version label.
- status fact: project/visit/action IDs, numeric state code, Portal state label, observation timestamp.
- quality manifest: source and published counts, unmatched rows, ambiguous versions, unknown statuses, source freshness, refresh status, active generation, coverage status.

## Suggested charts and measures

Use only the filtered active generation:

- **Known report/action records by corrector:** distinct count of `report_id`, grouped by `corrector_fio`; label as “observed reports with known corrector”.
- **Known visits by corrector:** distinct count of composite `(project_id, visit_id)`; because one visit can have several correctors/versions, mark as attribution-overlap-capable and do not sum across correctors as a total.
- **Observed workflow membership by code:** distinct `report_id` where a status flag equals `OBSERVED_PRESENT`, grouped by status or use the status fact. Label each as a current observed membership, not event count or outcome.
- **Multi-version visits:** count visits with `report_records_count > 1`; show `version_resolution_status`.
- **Unknown status matches:** report count with status flags `UNKNOWN`; this is a data coverage indicator.
- **Freshness/coverage:** show `last_successful_refresh_at`, `source_snapshot_at`, `source_freshness`, `coverage_status`, and `refresh_status` from the manifest.

Example calculated-field patterns (confirm function availability and null handling in the current DataLens editor):

```text
Known report records = COUNTD([report_id])
Status 39 observed reports = COUNTD(IF([status_39] = "OBSERVED_PRESENT", [report_id], NULL))
Status 39 unknown reports = COUNTD(IF([status_39] = "UNKNOWN", [report_id], NULL))
Composite visit key = CONCAT(STR([project_id]), "|", STR([visit_id]))
Known visits = COUNTD([Composite visit key])
```

Do not publish rates or rankings. The source does not provide a proven complete denominator, corrector error outcome, review completion timestamp, or current-version selection rule. Status 39 means a claim exists; it does not establish controller fault.

## Dashboard construction

1. Open Yandex DataLens and create a Google Sheets connection to the owner-provisioned workbook.
2. Verify all four tabs appear as separate tables and headers are recognized.
3. Create report, visit, status, and quality datasets with the active-generation filter.
4. Verify IDs remain dimensions (string or integer consistently), dates parse as dates, status flags remain strings, and counts are measures.
5. Add filters for corrector, project ID, report date, status membership, and coverage label.
6. Add a drilldown link from `report_url`; preserve exact action/report ID.
7. Display coverage and freshness prominently. Never label the dashboard as a complete company-wide audit.
8. Compare control totals with `data_quality_status`; ensure counts match the generation rows.
9. Keep this as a separate dashboard. Do not modify the existing customer dashboard in this task.

Google Sheets connection refresh and DataLens cache timing can delay visible changes; freshness should be read from the manifest, not inferred from when a chart happens to refresh. DataLens configuration and publication were not performed here.
