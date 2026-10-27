# Successful Visits — Isolated TEST Runbook

This TEST path exercises the feature branch's production materializer against a private copy of the production workbook. It does not call MariaDB, write to the Portal, or invoke the production scheduler.

## Pinned target

- Workbook title: `J4B Portal — Successful Visits — TEST — 2026-10-27`
- Spreadsheet ID: `13pPltgzR6HceIW4OjtLyzOrXwNdEcDQ1RBxR0L7ZCK8`
- Prepublication schema: `projects_current` 58 columns; `Статусы проектов` 25 columns.
- Expected schema: `projects_current` 64 columns; `Статусы проектов` 31 columns. Existing columns stay in place; six approved fields append to each sheet.
- The Controller Quality TEST workbook is not referenced by the workflow.

The workflow and both verification scripts pin this exact TEST ID and title. The TEST write gate also requires `RUN_MODE=test`, an explicit `J4B_ISOLATED_TEST` confirmation, and equality between `GOOGLE_SPREADSHEET_ID` and `TEST_SPREADSHEET_ID`. It rejects production write flags and production spreadsheet-ID variables.

## GitHub Actions configuration

The workflow is `.github/workflows/successful-visits-isolated-test.yml`. It reuses the existing `GOOGLE_SERVICE_ACCOUNT_JSON` secret and read-only Portal secrets. Owner authorization permits this account for the isolated TEST operation; its pre-existing Google permissions are not narrowed by this workflow. The workflow itself pins all spreadsheet API operations to the TEST ID and does not configure a production target.

`TEST_SUCCESSFUL_VISITS_BACKUP_FOLDER_ID` points to a private Drive folder. Before the first Sheet mutation, the workflow creates a full workbook copy there, fingerprints the affected tabs against the TEST source, and only then writes the local backup-upload marker required by the runner. The rollback copy is kept in Drive, not uploaded as a GitHub Actions artifact.

## Execution gates and checks

1. Public-repository safety check and full unit suite pass.
2. Read-only target check confirms the pinned ID/title, exact 58/25 source headers, and matching unique project IDs across the two project tabs.
3. Portal acquisition writes a private temporary checkpoint; materialization preflight validates the full candidate and refuses to remove any baseline project ID.
4. Private Drive rollback copy is created and its affected-tab values are fingerprint-verified.
5. `src.production` runs in TEST mode and publishes only to the pinned isolated workbook. Payment refresh is not enabled.
6. Independent readback checks the 64/31 schemas, unchanged legacy-column order, row preservation, unique project identifiers, numeric metric types, project-type state, and the control project.

Control-project assertions are reconciliation checks only; the runner obtains the values from its Portal source snapshot and does not write hardcoded metrics.

## Recovery

The TEST workflow retains a private rollback copy in the Drive folder. The runner also performs its built-in transactional rollback and readback checks on publication failures. If the workflow reports rollback incomplete or an independent readback failure, stop and restore from the identified Drive copy before another TEST attempt. Production is not touched by this runbook.
