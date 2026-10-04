# J4B Payment Acquisition Telemetry — 2026-10-04

## Scope

This change adds diagnostics only to the existing payment XLSX acquisition
request and parser. It does not add retries, alter authentication, change
timeouts, change publication gates, or change scheduler behavior.

## Contract

Each payment request emits safe lifecycle events:

- `PAYMENT_REQUEST_START`
- `PAYMENT_RESPONSE`
- `PAYMENT_REQUEST_SUCCESS`
- `PAYMENT_REQUEST_FAILURE`

Events contain only project identifier, bounded ordinal/total/attempt metadata,
HTTP status, normalized content type, content-length metadata, actual response
byte count, duration, row count on success, and an allowlisted failure class.
Failure events may include a SHA-256 digest and structural booleans. Response
bodies, XLSX rows, cookies, authorization headers, credentials, and query
secrets are never emitted.

Stable failure codes are:

`HTTP_STATUS`, `CONTENT_TYPE`, `EMPTY_RESPONSE`, `XLSX_SIGNATURE`,
`ZIP_CONTAINER`, `WORKBOOK_STRUCTURE`, `WORKSHEET_XML`, `HEADER_MISSING`,
`HEADER_DUPLICATE`, `HEADER_SCHEMA`, `ROW_SCHEMA`, `VALUE_PARSE`, and `OTHER`.

The existing `PaymentWorkbookError`/`PaymentTransportError` exceptions remain
the external failure contract. Telemetry is best-effort: an exception in the
diagnostic sink is swallowed and cannot mask the primary acquisition error.

## Verification

- One payment project still performs exactly one XLSX GET; no retry or recovery
  behavior was introduced.
- Existing strict incomplete-acquisition behavior remains fail-closed before
  any publication/readback write path.
- Local suite: `232 passed, 3 subtests passed`.
- `git diff --check`: PASS.
- Production refresh: NOT RUN.
- Google Sheet writes: 0.
- Portal production data changes: 0.

## Deployment

The telemetry-only commit is intended for the already qualified `production-cutover`
runtime. Deployment must update the server code checkout without starting the
service or timer. The scheduled execution state is unchanged.
