# Report registry coverage and bounded fallback

**PORTAL-001.3 — read-only investigation, 2026-10-26**

**Root cause classification:** source-level omission confirmed for one real report; the portal's underlying inclusion rule remains unknown.
**Production impact:** none. No collector run, checkpoint write, Sheets write, timer change or production deployment was performed.

## Reproduced omission

A project-scoped read-only workflow search found a report/action with workflow code 39. Its authenticated action detail returned one report link equal to the action path, one visit/project link, `report_date=2026-10-17`, and a positive selected `staff` (corrector) field. The report was absent from the current `report_reviewers` readback and schema-2 checkpoint.

The same sample was checked against the global registry:

| Read-only query | Unique report IDs | Sample included? |
|---|---:|---|
| `POST /report/`, `dt1=dt2=17.10.2026`, `limit=10000` | 144 | no |
| `POST /report/`, `16.10.2026`–`18.10.2026`, `limit=10000` | 468 | no |
| `POST /report/`, `01.10.2026`–`31.10.2026`, `limit=10000` | 5,058 | no |
| `POST /report/` with project filter and October date range | returned set did not include sample | no |
| `GET /proj/{project_id}/report` for the same project | 5 unique report IDs | **yes** |

The sample ID was absent from the raw date-filtered registry HTML, so `extract_registry_report_ids` did not drop a link it had received. The project report listing did contain it. Project-filtered day/month registry queries also omitted the sample. The project report list also excluded an earlier action/report on the same visit, described in [ACTION_VERSIONING_FINDINGS.md](ACTION_VERSIONING_FINDINGS.md).

The raw report ID, visit ID, project ID and controller identity are deliberately omitted from this repository document. The exact report ID is available in the task execution record.

## Collector path and eligibility

`scripts/run_report_corrector_collector.py` currently:

1. Makes one `POST /report/` per requested calendar day, using `dt1=dt2=DD.MM.YYYY` and `limit=10000`.
2. Parses report IDs linked in that single response with `extract_registry_report_ids`.
3. Merges only IDs returned by the registry into checkpoint schema 2.
4. Fetches eligible detail pages via `GET /action/{report_id}` within the 200-detail-GET cap and 1.5-second minimum interval.
5. Renders only reports whose detail has a positive `corrector_id` and whose action page `report_date >= 2026-10-01`.

The reproduced report satisfies the date and corrector eligibility conditions. It has **no checkpoint entry**, so it was not excluded by checkpoint state, recheck policy or `render_sheet_rows`; it was never seeded by the global registry response.

`extract_registry_report_ids` only consumes links in the response. The sample report ID was absent from the raw global registry HTML; this is not an ID-normalization or parser-regex miss. Registry retrieval has no workflow-state filter in the collector's request body.

## Hypothesis assessment

| Hypothesis | Result | Evidence / remaining uncertainty |
|---|---|---|
| A. Collector's selected calendar window excludes the report | **REJECTED as the sole cause** | Detail `dt` is 17.10.2026; exact-day, 3-day and October-wide ranges all included that date yet omitted the sample. |
| B. Registry filters by a different date than action `dt` | **UNKNOWN** | The visible `dt` matches the requested interval, but no registry-side creation/update timestamp is exposed to explain the omission. A late/backdated insertion cannot be excluded. |
| C. Pagination or result limit hides the sample | **NO EVIDENCE for this sample** | Day/3-day/month responses returned 144/468/5,058 IDs, all below 10,000. The sampled HTML had no page/offset inputs or next-page links. The sample ID was absent from raw registry HTML. Hidden server-side truncation cannot be ruled out globally. |
| D. Collector sends a status filter that excludes code 39 | **REJECTED for the collector request** | Registry requests contain date/limit fields only, no workflow-state filter. Project-filtered registry query also omitted the sample. Hidden server-side eligibility rules remain unknown. |
| E. Multiple action IDs for one visit caused this report to be dropped | **NOT the demonstrated cause of this omission** | The current code39 report is linked on the project report listing and has a valid detail page. Multiple actions do create a separate version/join risk; see the companion findings. |
| F. Report was created/changed after the collector's discovery window | **UNKNOWN** | `report_date` is known; created/updated/last-status-change time is not. Collector's rolling discovery does not revisit older filing dates every day. |
| G. Schema-2 checkpoint or publisher eligibility excluded the report | **REJECTED as the first failing stage** | No checkpoint record exists; source merge only sees registry IDs. If fetched, this report's date and positive corrector satisfy current publication eligibility. |

**Confirmed root-cause class:** the sole global discovery source is not complete for at least this real report. **Underlying portal-side reason:** not yet proven (e.g. hidden server eligibility, indexing delay, or report creation/update semantics remain possible).

## Development-only mitigation prepared

An opt-in `--supplement-project-report PROJECT_ID` route is implemented in the development branch. It performs one explicit `GET /proj/{id}/report` and supplements registry IDs only when:

- the caller explicitly supplies exactly one numeric project ID;
- the project listing has no pagination indicator and no cross-project rows;
- the unique IDs on that one page do not exceed the remaining detail-GET budget (maximum 200);
- the collector is not invoked with `--skip-discovery` and has a positive fetch budget.

Default scheduled behavior is unchanged. The fallback preserves distinct report IDs (it does not deduplicate by visit), uses the existing schema-2 checkpoint, and is not enabled in either systemd unit. It is a **bounded recovery path for a known project**, not a global coverage guarantee. Regression tests cover the missing-ID supplement, pagination refusal, project scope, candidate budget and CLI fail-closed guards. The feature was not run against production and did not modify the checkpoint or sheet. Any future live invocation must go through the existing collector wrapper so it retains the shared `production.lock`; do not invoke the CLI directly.

**Why no broader automatic fallback:** enumerating every `/proj/{id}/report` would be a project-wide scan and these pages do not have a proven global completeness/pagination contract. The sample project listing can recover this report, but one sample does not establish it as a complete replacement for `/report/`. No speculative schedule change or backfill was made.

## Next minimal verification

Ask the portal owner/maintainer to explain the missing report's server-side visibility/indexing rule and whether the project report journal is authoritative for the currently displayed report version. Then compare a small set of known missing IDs against both sources. Do not claim a coverage percentage until the global denominator is independently qualified.
