# Controller quality online pipeline

## Components

- `src/controller_quality_mart.py`: source normalization, exact-grain joins, grouping, validation, project-scoped status requests, shared lock, generation publication coordinator, and collector-success gate.
- `src/controller_quality_sheets.py`: Google Sheets API v4 read/update adapter and exact-header source reader. It uses the existing `google_token()` implementation; no new auth mechanism or scope was added.
- `scripts/run_controller_quality_mart.py`: explicit `--dry-run`, read-only `--plan`, and gated `--publish` entry points. Importing modules has no network or write side effects.

## Run modes

```sh
python3 scripts/run_controller_quality_mart.py --dry-run
python3 scripts/run_controller_quality_mart.py --plan
python3 scripts/run_controller_quality_mart.py --publish
```

`--dry-run` performs no I/O. `--plan` requires configured Google/Portal credentials and reads the collector result, source workbook, project-scoped Portal action responses, and target schema; it cannot write. `--publish` additionally requires `CONTROLLER_QUALITY_PUBLICATION_ENABLED=true`, an explicit separate `CONTROLLER_QUALITY_SPREADSHEET_ID`, matching target schemas, same-day successful collector evidence, and the shared lock. Target ID equal to the existing production source ID is rejected. There is no implicit mode.

`--historical-backfill` is an isolated exception for the owner-approved, already accumulated four-day `report_reviewers` registry. It requires `CONTROLLER_QUALITY_HISTORICAL_BACKFILL_ENABLED=true`, the exact known source spreadsheet ID, and the fixed TEST workbook ID in `CONTROLLER_QUALITY_SPREADSHEET_ID`; any other source or destination fails closed. It does not inspect or invoke systemd collector evidence and does not change the normal `--publish` same-day freshness gate. The reviewed source contract is exactly 722 unique reports, 57 projects, 22 controllers, report dates 2026-10-01 through 2026-10-25, and row-level last-successful-fetch dates/counts 2026-10-22:161, 2026-10-23:200, 2026-10-24:188, 2026-10-25:173. If the source differs, publication is blocked for re-review.

This historical publication still performs bounded current-state `/action` reads for only the 57 project IDs represented by the source, then limits status membership rows to the exact observed `(project_id, action_id, visit_id)` keys. It has no retries and does not read a broader project universe. `status_39` describes current membership observed during generation, not a four-day status history or transitions. Report dates remain the business-date filter; row `last_successful_fetch` remains source provenance and is not a daily snapshot dimension.

The same `PORTAL_CREDENTIALS_FILE` and `GOOGLE_SERVICE_ACCOUNT_JSON_FILE` contracts already used by J4B are reused. Portal credential file permissions must be 0600 or stricter. Tokens, response bodies, names, and IDs are never logged. CLI output is an allowlisted aggregate summary or a safe exception class.

## Status acquisition

The status query uses existing `ACTION_STATE_CODES` and the same 2026 project-scoped `/action` query shape as the qualified production reader. It performs no more than 200 project requests per run, waits at least 1.5 seconds between requests, and does not retry. The request has to return HTTP 200, HTML, the expected Portal origin/`/action` final URL, an exact project link, and a parseable table. Login, access-denied, PHP-error, non-document, malformed, and structurally incomplete responses stop the run before publication. A status query for every distinct project represented in the report source is required; otherwise candidate creation fails.

Known status code memberships can overlap. An exact matched action row with no code has no memberships; an absent/mismatched action row is `UNKNOWN`. New/unrecognized codes fail closed rather than being dropped.

## Publication details

1. Verify same-day collector systemd status and private run-log summary with `queue_remaining=0`; zero-change success is accepted.
2. Acquire the shared non-blocking flock before reading source data.
3. Read and validate the exact `report_reviewers` 9-column header.
4. Acquire one project-scoped status response per distinct report project.
5. Build all tabs in memory and validate schemas, widths, uniqueness, source coverage, and exact join keys.
6. Append fact rows for a new generation; verify every written range.
7. Update the single active-generation row last and verify it by readback.
8. Release the shared lock.

Each generation is immutable. A failed fact write leaves the previous pointer untouched. An ambiguous write is not retried. Old staged generations are inert only when DataLens enforces the active-generation filter. No rollback write is needed because the last good pointer is preserved until all fact writes verify.

## Structured output

Success emits counts, generation ID, candidate SHA-256, and fixed coverage label. Failure emits only `FAILED_CLOSED`, a safe exception class, and `writes=0`. It never emits source records, report/visit IDs, corrector names, credentials, raw HTML, or Google error response bodies.

## Current operational gate

Read-only production audit found the main `j4b-refresh.service` failed after starting payment publication on 2026-10-26. Its workflow reports rollback/readback PASS, and the two payment tabs plus `Статусы проектов` matched the run’s pre-publication backup. `projects_current` differs from that backup: it has one additional data row and 416 changed rows. This task did not repair or rerun the main refresh. Before deploying the new service, separately restore/qualify overall production consistency and confirm the source `report_reviewers` tab remains current.
