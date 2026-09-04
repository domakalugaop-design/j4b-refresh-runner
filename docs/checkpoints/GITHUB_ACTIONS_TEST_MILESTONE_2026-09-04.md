# GitHub Actions TEST qualification milestone — 2026-09-04

## Scope

This checkpoint records the proven state of the migration from the qualified local macOS production refresh to an autonomous GitHub Actions runtime. It does **not** declare production cutover readiness.

## Architecture decision

- Google Apps Script runtime is rejected for production refresh orchestration after the clean qualification failure.
- Current cloud runtime target is GitHub Actions.
- Migration principle: port the already-qualified local acquisition/materialization/publication contract; do not create a new ETL architecture.
- Current production writer remains `LOCAL_PROD` until cloud qualification and explicit cutover.
- Production Google Sheet and DataLens are not written by the current TEST workflow.

## Public execution repository

Repository: `domakalugaop-design/j4b-refresh-runner`

Migration branch: `migration/local-prod-port`

Draft PR: #2 `Port local refresh pipeline to GitHub Actions TEST runner`

The public repository contains execution-safe code only. Credentials are supplied through GitHub Actions Secrets and are not stored in source, documentation, logs, or committed runtime artifacts.

## Secret contract

The repository Actions secret contract contains seven required values:

- `PORTAL_BASE_URL`
- `PORTAL_LOGIN`
- `PORTAL_PASSWORD`
- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- `GOOGLE_REFRESH_TOKEN`
- `TEST_GOOGLE_SPREADSHEET_ID`

Secret values are not recorded here.

## First manual qualification run

Run ID: `33863604491`

Result: FAILED before live refresh.

Proven facts:

- GitHub-hosted Ubuntu runner started correctly.
- Migration branch checkout succeeded.
- Python 3.11 and dependencies installed successfully.
- Public-repository safety check passed.
- All seven secrets were injected and masked by GitHub Actions.
- Failure occurred during pytest collection: `ModuleNotFoundError: No module named 'src'`.
- Portal login, Google auth and live ETL were not reached.
- Failure classification: runner/test invocation portability defect, not Portal/Google/data logic.

Fix applied to manual dispatcher on `main`: invoke tests using `python -m pytest -q`.

Dispatcher fix commit: `5594db294bc66260c139e7857c9b81354bb32d49`.

## Second manual qualification run

Run ID: `33863735316`

Run started: `2026-09-04T10:32:39Z`.

State at this checkpoint: **IN PROGRESS**.

Already proven PASS in this run:

1. Set up job.
2. Checkout migration branch.
3. Python setup.
4. Dependency installation.
5. Public repository safety check.
6. Unit and portability tests.
7. Required runtime secret validation.

Current active step at checkpoint creation:

- `Run isolated TEST refresh` — `in_progress`.

This is the first GitHub-hosted run to reach the live Portal/Google execution path.

No qualification conclusion may be recorded until the live refresh completes and TEST Google Sheet readback is validated.

## Canonical production logic preserved

The port retains the qualified three-request acquisition contract per successfully acquired project:

1. `GET /proj/{project_id}`
2. `GET /proj/{project_id}/edit`
3. filtered `POST /action`

No mass `/visit/{visit_id}` acquisition is part of the production path.

The TEST runner remains fail-closed:

- `RUN_MODE=test` only;
- production write explicitly forbidden;
- candidate duplicate IDs forbidden;
- candidate master cannot shrink the previous unique project set;
- previous sheet data is restored on publication/readback failure;
- failed selected-project acquisition carries forward the previous last-good row;
- unselected rows are preserved;
- `date_from`, `date_to`, `last_refreshed` are written as Google Sheets date/date/datetime serial values with explicit number formats.

## Expected qualification gate after run completion

A green GitHub Actions job alone is insufficient. The run must also prove via TEST spreadsheet readback:

- full master unique count is preserved or increased;
- duplicates = 0;
- selected project data is refreshed;
- unselected project rows are preserved;
- failed acquisitions preserve last-good business values;
- header/schema remains intact;
- `date_from` is native DATE;
- `date_to` is native DATE;
- `last_refreshed` is native DATE_TIME;
- successful publication has one consistent refresh timestamp across the master;
- no production spreadsheet write occurred.

Only after this gate may production cutover be considered.

## Observability backlog

Current defect: `src.refresh.run()` emits only the final JSON report. During the acquisition loop GitHub Actions exposes the whole refresh as one long-running step with no internal progress signal.

Add bounded, secret-safe progress logging without changing acquisition/data semantics.

Target progress line, emitted periodically (recommended every 10 projects and at completion):

```text
ACQUISITION 50/258 | success=50 | failed=0 | http=150 | 21.8 proj/min | ETA=9.5 min
```

Required fields:

- completed selected projects / total selected projects;
- success count;
- failed count;
- Portal HTTP request count;
- elapsed wall time;
- projects/minute;
- rolling or cumulative ETA;
- optional current project ID only if useful for diagnostics.

Required stage boundary events:

- `BASELINE_READ_START/PASS`
- `PORTAL_LOGIN_START/PASS`
- `UNIVERSE_DISCOVERY_START/PASS` with universe count
- `SCOPE_SELECTION_PASS` with selected count
- periodic `ACQUISITION` progress
- `MATERIALIZATION_START/PASS`
- `CANDIDATE_VALIDATION_PASS` with rows/unique/duplicates
- `PUBLISH_START/PASS`
- `READBACK_START/PASS`
- `TYPE_VALIDATION_PASS`
- `FINAL_STATUS` with wall time and counts

Safety requirements for progress logs:

- never print credentials, tokens, cookies, authorization headers, request bodies containing sensitive business payloads, or secret environment values;
- never dump raw HTML or full Google Sheet rows;
- logging must not alter retry, failure, rollback, selection, acquisition, or publication behavior;
- progress output is observability only.

## Schedule/cutover backlog

Do not enable a recurring production schedule yet.

After isolated TEST qualification:

1. qualify TEST spreadsheet readback and physical types;
2. record the successful qualification checkpoint;
3. merge/normalize the qualified runner code;
4. add the production target under an explicit production-write guard;
5. enable one production writer only;
6. configure daily GitHub Actions schedule matching the approved production refresh time;
7. perform and verify first scheduled production run through DataLens;
8. only then retire/disable the local recurring production writer.
