# Controller quality online mart — architecture

## Decision

Create a **new owner-provisioned Google spreadsheet** used only by the controller-quality mart. Keep the six existing J4B materialized tabs unchanged. The mart has four tabs: `controller_report_snapshot`, `controller_visit_summary`, `controller_status_membership`, and `data_quality_status`. All four must be in the same workbook so DataLens can join each fact tab to the generation manifest within one Google Sheets connection.

The published data is an observed sample, not the complete Portal report population. The existing `report_reviewers` source contains reports with a known positive corrector; historical observed count was 722 reports and 22 correctors at the 2026-10-25 collection. These are checkpoints, not expected totals. A documented live report is absent from the global registry/checkpoint/sheet, so completeness is explicitly `OBSERVED_CORRECTOR_SNAPSHOT_ONLY`.

## Fact grain and safe joins

| Tab | Grain | Stable key | Status semantics |
|---|---|---|---|
| `controller_report_snapshot` | One observed report/action record | `report_id`; action attribution uses `(project_id, action_id, visit_id)` | `PRESENT`, `OBSERVED_ABSENT`, or `UNKNOWN`; absence is only asserted after a structurally valid project-scoped action response contains the exact action row |
| `controller_visit_summary` | One business visit | `(project_id, visit_id)` | Keeps all known report IDs; current report/corrector remain blank with `UNRESOLVED_CURRENT_VERSION_RULE` |
| `controller_status_membership` | One report/action status predicate | `(project_id, visit_id, action_id, workflow_state_code)` | Memberships may overlap; this is a current snapshot, not a transition log |
| `data_quality_status` | One active generation | `publication_key` | Pointer and freshness/coverage metadata; written only after fact rows are staged and read back |

Statuses are joined to reports only by project, action, and visit identity, never by `visit_id` alone. Project-level status counters are not used to infer report status. Missing action-level membership is `UNKNOWN`; it is not converted to false. `35`, `38`, and `39` keep their Portal meanings; `39` is a claim membership, not proof of controller error. No status history is reconstructed from simultaneous memberships.

No universal rule identifies the current report among multiple action IDs for one visit. The mart does not select by largest ID or latest date. The visit summary stores `current_report_id` and `current_corrector_id` blank and labels the resolution as unresolved.

## Generation publication

The publisher assigns a random 32-character `generation_id`, appends immutable fact rows tagged with `publication_key=controller_quality` and that generation, reads each written range back, then writes the single `data_quality_status` row as the active-generation pointer and reads it back. If a fact write fails or has an ambiguous result, the old pointer remains active and the run stops; the write is never blindly retried. If pointer-write outcome is ambiguous, the publisher performs one read-only reconciliation and accepts only an exact pointer match. It never retries the pointer write.

This is a pointer-based publication protocol, **not a multi-tab Google Sheets transaction**. Intermediate generations physically exist in the workbook. Every DataLens fact dataset must join its fact to `data_quality_status` on `publication_key` and filter `generation_id = active_generation`. A dataset that omits this condition can see a staged or stale generation. Old generations are retained; cleanup/retention is a separate deployment decision and should occur only after an active generation is verified. Long-term spreadsheet cell limits require a retention policy before prolonged operation.

The publisher requires the target workbook to contain exactly these four pre-created tabs with exact headers. It does not create or rename spreadsheets/tabs. This avoids Drive-wide creation scopes and leaves ownership and sharing under the user’s control.

## Source-to-field lineage

| Output | Source | Transformation | Confidence |
|---|---|---|---|
| report/action/visit/project IDs, report date, corrector ID/name, source status, last successful fetch | `report_reviewers` tab in the existing materialized workbook | Exact 9-column header, positive integer IDs, unique report ID, ISO/date parse, valid status; preserve observed source values | Confirmed source contract; known partial population |
| status code and label | Project-scoped `POST /action` with all established workflow-state filters | At most one request per distinct source project, one query per project, minimum 1.5 s spacing, no automatic retry; reject non-HTML, login, PHP/error, wrong-final-URL or structurally incomplete response | Existing parser contract reused; no new live qualification in this development task |
| report status flags | Exact report/action/visit match to the project-scoped response | Emit `OBSERVED_PRESENT` / `OBSERVED_ABSENT` only if the exact action row was observed; otherwise `UNKNOWN` | Confirmed join grain; observed snapshot only |
| visit report count and IDs | Validated `report_reviewers` records | Group by `(project_id, visit_id)`, distinct IDs | Confirmed observed snapshot, not all Portal versions |
| current report/corrector | None | Always blank; `UNRESOLVED_CURRENT_VERSION_RULE` | Unknown by prior investigation |
| source freshness and coverage | Source row `last_successful_fetch`, collector systemd result and private run-log summary | Keep source snapshot time and successful refresh time separate; coverage always partial label | Confirmed semantics |

`project_name` is intentionally blank: the source snapshot does not contain it, and joining to the operational project sheet would mix snapshots and grains. DataLens can show `project_id` as the stable project dimension until a separately qualified name source is approved.

## Automation and interference controls

The prepared timer is 18:30 `Europe/Moscow`, after the current collector timer at 18:15; it is not installed or enabled here. At execution the job must verify same-day collector `systemd Result=success`, exit code zero, and the latest private collector run log with final `status=OK` and `queue_remaining=0`. A completed run with no discovered rows/no detail attempts/no reconciliation changes is classified `SUCCESS_NO_NEW_RECORDS` and is valid. A failed, refused, missing, stale-day, or unresolved-queue collector result blocks the mart.

The job uses the existing `fcntl.flock(LOCK_EX | LOCK_NB)` file `/home/maxis/j4b-refresh-deployment/locks/production.lock` for source reads, Portal status acquisition, build, and publication. If the shared lock is unavailable, the job fails closed. It does not change or trigger the collector, main refresh, checkpoints, or timers.

## Confirmed / inferred / unknown

- **Confirmed:** report/action equality holds for observed known rows; one visit can have multiple report/action IDs; the report registry can omit reports; status source is project-scoped action membership; existing 18:15 collector and shared lock exist.
- **Inferred:** the observed corrector snapshot is useful for operational drilldown when clearly marked partial.
- **Unknown:** complete Portal report universe, global omission rule, current-version selection, status transition history, report-review completion time, controller quality/error denominator, actual target spreadsheet permissions, DataLens user access, and whether the action membership source is complete for each report row.

No Portal, Sheets, DataLens, scheduler, or production data was changed by this implementation.
