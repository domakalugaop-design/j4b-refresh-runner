# Qualification status

## Runtime decision

Google Apps Script is rejected as the target autonomous runtime after the final clean benchmark failed before acquisition with no active execution owner and no continuation.

Target runtime: GitHub Actions.

## Migration status

Prepared on branch `migration/local-prod-port`:

- public-safe source inventory;
- portable environment-based Portal credentials;
- portable `curl` resolution from `PATH`;
- canonical three-request acquisition core;
- synchronous materialization/publication runner;
- TEST-only fail-closed production guard;
- manual GitHub Actions TEST workflow;
- unit/portability tests;
- repository safety workflow and scanner.

## Not yet qualified

No live GitHub Actions TEST refresh has been executed yet.

Reason: the required repository Secrets must be configured outside the repository. Secret values must never be committed or pasted into project documentation.

## Required TEST Secrets

- `PORTAL_BASE_URL`
- `PORTAL_LOGIN`
- `PORTAL_PASSWORD`
- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- `GOOGLE_REFRESH_TOKEN`
- `TEST_GOOGLE_SPREADSHEET_ID`

## Production state

- Existing local production writer remains authoritative.
- No recurring GitHub Actions production schedule exists.
- No production cutover has occurred.
