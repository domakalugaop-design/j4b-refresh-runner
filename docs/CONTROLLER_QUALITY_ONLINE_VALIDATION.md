# Controller quality online validation contract

## Input checks

- Source tab header equals the existing `report_reviewers` contract exactly.
- `report_id`, `action_id`, `visit_id`, `project_id`, and any corrector ID are positive integer identifiers; IDs are canonicalized without leading zeroes for joins.
- `report_id` is unique. A repeated ID is a hard failure.
- Report dates and source timestamps parse as dates/timestamps.
- Corrector ID/name are both present for `CURRENT` and `STALE_LAST_GOOD`; other stored source statuses are not silently reinterpreted.
- Status source covers every project in the observed report input. A status response is never interpreted as an empty result unless its HTML, origin, project scope, and table structure pass.
- Status membership is unique at `(project_id, visit_id, action_id, workflow_state_code)`; overlapping codes are allowed.

## Candidate checks

- Exactly four candidate tabs, exact header tuples, and exact row widths.
- One row per report ID and per `(project_id, visit_id)` visit key.
- One row per exact status membership.
- Every row is tagged with `publication_key=controller_quality`; fact rows carry one generation ID.
- Every report status flag is tri-state. Only an exact action/visit row match can create observed-present or observed-absent values; otherwise it is `UNKNOWN`.
- Current-version fields are empty unless a future independently approved source rule is added. Current implementation always sets `UNRESOLVED_CURRENT_VERSION_RULE`.
- `data_quality_status` is a singleton row, `coverage_status=OBSERVED_CORRECTOR_SNAPSHOT_ONLY`, with explicit source freshness and count metadata.

## Source and publication gates

- Same-day collector systemd result is `success`, exit status `0`, with a latest private collector run log written after that service start, final JSON `status=OK`, and `queue_remaining=0`.
- No-new-records success is distinct from failure and accepted only when discovery, attempted detail reads, and reconciliation changes are all zero.
- Source fetches, build, and publication run under the existing shared non-blocking production lock.
- Maximum 200 project-level status requests; minimum 1.5-second spacing; no retries.
- All required target tabs exist with exact schemas. No target workbook or tab is auto-created.
- Readback of each generation range must match before the active pointer is moved.
- The active pointer is written last and independently read back. Ambiguous pointer writes get at most one read-only reconciliation; never a second write attempt.
- Failed source/schema/validation/write/readback leaves the previous active-generation pointer untouched.

## Measures eligible for DataLens

For a selected corrector, `Known report/action records` is the distinct count of `report_id` within the observed snapshot. `Known visits` is the distinct composite key `(project_id, visit_id)`; it is not the report count. Status 35/38/39 report counts include only `OBSERVED_PRESENT` records. Status `UNKNOWN` rows must be excluded from both present and absent denominators and displayed as a separate coverage count.

Do not derive company-wide claim rate, error rate, return count, review completion, review time, current report by maximum ID/date, or corrector ranking. There is no proven full denominator, transition history, review timestamp, or current-version rule.

## Expected controls

- Source `report_reviewers` rows read = `source_row_count` in the active generation; this is an observed-source count, not total Portal reports.
- Published report count = report snapshot data-row count.
- Published visit count = unique `(project_id, visit_id)` summary row count.
- Status membership count = unique exact status fact keys.
- Unmatched report status count remains visible and is not converted to a false status.
- Ambiguous versions = visits with more than one observed report/action ID.
- Unknown status count includes reports without an exact action/visit observation.
- Source freshness is based on source successful-fetch timestamps; it is separate from mart publication time.

All tests use synthetic data. No private production source records are committed.
