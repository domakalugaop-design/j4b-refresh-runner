# PORTAL-001.3 — Checkpoint / New Chat & OpenWork Handoff
Date: 2026-10-26
Stage: GitHub checkpoint and context transfer ONLY
Next stage (NOT executed): Multi-Project Visit Registry Acceptance
Evidence boundary: previously observed/test-published results + verified remote repository and TEST workbook metadata. No new Portal acquisition, multi-project test, production write, or local Git test is claimed.

## 1. Repository, architecture and execution boundary

- Repository: https://github.com/domakalugaop-design/j4b-refresh-runner (public execution shell; **no sensitive business payloads**).
- Feature branch: codex/portal-001-3-controller-quality-snapshot-20261026.
- Remote branch HEAD immediately before this handoff: fbe1055eec664eeb4674b93f6454082921f23ce5 (compared against remote, identical).
- Owner-reported local workspace: ~/Development/j4b-controller-quality-publication. Its branch, HEAD, dirty state and configured origin **were not inspected in this GitHub-only operation**; do not conflate remote state with local state.
- Production branch is separate (production-cutover). No production deployment, scheduled workflow dispatch or production Google Sheets mutation is authorized here.
- Existing collector: per-project POST https://lk.j4b.ru/action, already acquired by project collection. Captured response -> strict visit/action enrichment -> content-addressed snapshot -> gated TEST Google Sheets publication. Existing production collector does **not** yet persist all newly extracted visit/report datetimes and controller FIO; integration requires a separate change.
- Core implementation: [parser and builder](../src/visit_registry.py), [offline snapshot runner](../scripts/run_visit_registry_snapshot.py), [TEST publisher](../scripts/publish_visit_registry_test.py), [tests](../tests/test_visit_registry.py).
- Source documentation: [Acquisition architecture](PORTAL_ACQUISITION_ARCHITECTURE.md) and [Visit Registry Pipeline](VISIT_REGISTRY_PIPELINE.md).
- Last reported test suite: **584 passed, 0 failed**. Not rerun during this checkpoint.

## 2. Business definitions and data grains

| Grain | Key / proof | Meaning and restrictions |
| --- | --- | --- |
| VISIT | (project_id, visit_id) | Unique visit at project scope. This is the distinct-visit denominator, not the action count. |
| REPORT | report_id **only if independently identified** | Report/questionnaire; not always one-to-one with visit or action. |
| ACTION | action_id or another independently proven key | Row observed in /action. An action row is **not proved** to be a transition event, work effort, or a newly created report. |
| CONTROLLER ASSOCIATION | Proven controller-to-visit/report/action relation | Controller FIO is free text in /action, not a stable controller ID. Multiple rows and controller associations must be represented separately. |

Never assume 1 visit = 1 report = 1 action, row count = number of status changes, or row count = actual labor time. Visits associated with controllers, action-row volume, and multiple-controller cases are three separate workload measures. All period metrics require an explicit time definition and comparable population.

### Status semantics and source coverage

- **Status 39 — «Есть претензия»**: client claim about the service result, **not** a controller remark or proof of a controller error.
- Previously observed native current-state filter POST /action with state[39]=on AND empty=on: **12 real-current-claim visits**. Without empty=on the observation was 13, including a test-project visit. The filter label describes hiding unassigned visits. This is a **point-in-time** observation, not historical claim incidence.
- Exclude test projects only after verifying their project classification. Do not infer classification from an arbitrary row and do not treat the 12 count as a permanently current count.
- Claim occurrence timestamp: **UNKNOWN**. Status membership only proves membership at observation time. Do not construct a month-of-claim trend or claim rate by pairing unmatched dates.
- Global /action dt1/dt2 is **NOT RELIABLE** for period selection. Per-project /action can enumerate rows for that project; any client-side date filtering must preserve the underlying semantics and completeness. /report/ has separately verified date filtering, but is report-grain and misses some populations.
- Visit datetime may be scheduled/planned rather than completed; report datetime is a report-submission field. Both are distinct from source capture time and status-change time.
- Production payment/project source and other caches may have stale status membership; the native current-state status-39 query is the relevant reconciliation comparator, not a historical event log.

## 3. Project 8050 observations — do not normalize away the difference

| Evidence | Observed action rows | Distinct visits |
| --- | ---: | ---: |
| Earlier live observation | 38 | 27 |
| Later enriched TEST snapshot | 39 assigned rows | 27 |

- The later registry snapshot was captured 2026-10-26T13:57:52Z and published to TEST: snapshot_id **2e6037b4e5ab521f27738faf7a2d40d12fbac3788c6663e794def4555106f301**. Last published workload summary in source documentation: two controller-FIO groups, 21 visits/29 rows and 6 visits/10 rows. The raw FIOs are deliberately not retained in GitHub.
- Different capture times or transformations can explain the difference. Both observations remain valid as measurements of their own acquisitions; **ROOT_CAUSE=UNCONFIRMED**.
- Parser notes: action links distinguish assigned rows; no action link or /action/0 on a red row is an unassigned sentinel. Red styling alone is not an unassigned marker. Controller and date fields are extracted relative to anchored visit-link cells; missing/ambiguous values produce quality flags or fail-closed errors, not fabricated values.

## 4. Explicit owner hypothesis (NOT VERIFIED)

**CLASSIFICATION: UNVERIFIED OWNER HYPOTHESIS.**

The owner suspects that extra /action rows can arise from a report/questionnaire moving among workflow statuses, illustrating 30 -> 37 -> 40. Owner's *preliminary* meanings: 30 = submitted for review; 37 = reviewed; 40 = approved. The report may remain in 37 and may pass among statuses repeatedly.

**None of the following is established:** whether an /action row represents a transition, current action registry, assignment registry, or mixed structure; whether repeat transitions add rows; whether the extra 8050 row reflects a new workflow transition, assignment, refresh, duplicate, parser/classification change; whether the illustrative path exhausts legal transitions.

Investigate 38 versus 39 first by capture timestamps, raw rows, parser revisions, sentinel classification, action keys, duplication and assignment classification. Only then test the owner's workflow interpretation, and only with independently corroborated evidence. No controller-effort or status-transition KPI may be derived from action counts in the meantime.

## 5. TEST analytical workbook and verified sheet layout

TEST workbook (read-only inspection in this checkpoint): https://docs.google.com/spreadsheets/d/1G_pXXNCLTwABSnSPtXxRCxF19VOCOvXi-DK21ZMzytM/edit
Title: JFB — Controller Quality Mart — TEST. As of 2026-10-26 connector metadata and header-row readback: **13 tabs**.

| Tab | Header-based interpretation / contract |
| --- | --- |
| controller_report_snapshot | report/action/visit/project ids, corrector ID/FIO, report date and workflow flags |
| controller_visit_summary | per-project visit/report associations, known/current report and corrector, resolution status |
| data_quality_status | publication_key, generation, refresh/source timestamps, coverage/quality indicators |
| metric_dictionary | publication_key, generation_id, metric_key, definition, availability, limitation |
| visits_snapshot_history | previous visit observation history; includes source/capture provenance |
| visits_snapshot_manifest | prior snapshot provenance and completeness fields |
| visits_registry_history | action/association history with snapshot, project, visit, action, dates, status, controller, capture and quality flags |
| visits_registry_manifest | registry snapshot ID, capture time, counts, claims membership, flags, collection_completeness, coverage_scope |
| visits_current | current visit view: 17 columns; replace-only with independent population-completeness gate |
| visits_by_period | project × period (visit-date semantics caveated) |
| controller_workload | snapshot × project × controller FIO: visits_assigned and action_rows_handled, not labor time |
| project_summary | snapshot × project: visit, assignment, report-date, claim-membership, action-row and controller counts |
| claims_trend | claim observation fields include query_window and claim_event_time; **no verified occurrence timestamp** |

The **actual** registry/history/workload and current-view headers match the documented TEST publisher contract as observed in header-row readback. This list includes visits_registry_manifest, omitted in the owner's illustrative list. Existence/header verification is NOT a multi-project data-content acceptance or a write-permission check.

## 6. Snapshot/publication safety contracts (implemented; larger-scope acceptance pending)

- Snapshot includes immutable observation identity, captured_at_utc and fact fingerprint; replay of identical facts is idempotent. Different fingerprint for same ID fails closed.
- Append-only history and manifest tabs must retain earlier observations and metadata. Row-level deduplication allows resume after partial TEST publication; a fully published snapshot previously returned ALREADY_PUBLISHED.
- visits_current is replace-only, unlike historical tables. Existing code gates replacement of other-snapshot rows on a collection_completeness value starting with COMPLETE; **THIS DOES NOT PROVE COMPLETENESS OF THE INTENDED MULTI-PROJECT POPULATION**. A complete single-project capture could still be an incomplete global view. Treat this as a critical next-stage acceptance risk.
- Header mismatch, missing approval, wrong account/destination, invalid snapshot and conflicts must refuse writes. TEST publishing uses explicit --approve-test-write; no production credentials or destinations authorized.
- Capture times for independent components (claims query, per-project visit registry, report and workload observations) must remain separate. Combining components into one false “current” snapshot is prohibited.
- Claim snapshot previously reported: **076a3519dee3433284b553cbc839f472**. Registry snapshot full ID above. Preserve prior histories; do not overwrite/delete any tab or rows.
- Available metrics are scoped to **observed snapshots**, not an all-project universe. Claim rate is BLOCKED_COHORT_UNVERIFIED until numerator/denominator cohort and dates are proved.

## 7. Access and security references

- Portal authentication contract: macOS Keychain service/name **j4b-web-login**. Secret values must remain in protected local storage.
- Google credential directory (owner environment): ~/Library/Mobile Documents/com~apple~CloudDocs/Documents/JSON. This is a locator only; NEVER copy its JSON/key contents to repo, logs or chat.
- TEST writer previously confirmed: **id-j4b-portal-refresh@quick-function-287207.iam.gserviceaccount.com**.
- TEST reader previously confirmed: **id-finance-bot@quick-function-287207.iam.gserviceaccount.com** (read-only).
- Reconfirm actual permission before the next write; historical permissions are not proof of current access. No password, session cookie, token, key, service-account JSON, customer, employee or respondent payload belongs in this public repository.
- Google Sheet was **not modified** in this handoff. All Portal/production sources remained read-only/not contacted.

## 8. NEXT TASK — Multi-Project Visit Registry Acceptance

**MODE: CONTROLLED TEST VALIDATION. This task has NOT been executed by this checkpoint.**
**OBJECTIVE:** prove the enriched visit-level pipeline on several differently sized projects, including at least one project with native current status-39 membership, before any production readiness decision. Avoid broad Portal scans.

### A. Project 8050 discrepancy
Compare capture timestamps, raw source rows, parser versions, assigned/unassigned sentinel rules, actual action identifiers, duplicate and controller-assignment classifications between the 38 and 39 observations. Preserve each original capture. Then, and only then, investigate transition-based hypotheses using independent evidence. Classify a genuinely new source row vs classification/parser change vs duplicate vs another evidenced cause; otherwise **ROOT_CAUSE=UNCONFIRMED**. Do not coerce counts.

### B. Multi-project source validation
Select explicitly bounded projects of different sizes and at least one status-39 current-claim project. Validate row/visit/report counts, all relevant datetimes and meanings, statuses, controller associations, missing data, action keys, duplicates, sentinel treatment, and per-project completeness. Keep evidence by capture; do not infer a global complete universe from successful parsers.

### C. visits_current global-population safety
Test publishing a complete **single-project** snapshot when visits_current also contains another project. Prove there is no cross-project deletion/replacement, or refuse the operation with zero writes. Define completeness against the **intended target population** (project scope vs full registry); “all parsed rows” alone is insufficient.

### D. Controller workload semantics
Verify distinct associated visits, assigned action records and multiple-controller associations separately, including ambiguity and FIO normalization; do not equate action count with labor time or with status-transition count.

### E. Current claim reconciliation
Compare project-level enriched status-39 memberships against the native POST /action state[39]=on + empty=on capture within bounded/time-stamped observations. Exclude only confirmed test projects. Surface conflicts/incompleteness, preserve unknown claim event time, and never manufacture historical claim series.

### F. History/recovery acceptance
Exercise append-only retention, snapshot ID/fingerprint idempotency, partial-write retry/recovery, independent component timestamps, header/schema refusal, and no loss of previous snapshots. Read back actual TEST effects.

### G. Decision and evidence package
Return exactly one recommendation: **READY_FOR_PRODUCTION** or **NOT_READY_FOR_PRODUCTION**, with tested scopes, source timestamps, acceptance evidence, unresolved blockers and explicit production risks. A READY recommendation does **not** authorize deployment. **NO production deployment** in this task.

## 9. Checkpoint disposition and resume protocol

- **Implemented / previously tested:** visit-level parser enrichment; visit-grain vs association-grain handling; date/status/controller fields; immutable append-only snapshots; gated, idempotent TEST Sheets publication and readback; fail-closed gates.
- **Not established:** behavioral meaning of individual action rows; reason for 38→39; exhaustive multi-project coverage; safe multi-project current-view replacement; labor intensity; claim occurrence time; global period-matched claim rate; production readiness.
- **Do not modify:** production workflow/deployment, old TEST history, other project snapshots, non-P0/PORTAL-001.3 files, master initiative registry.
- **Before next task:** inspect actual local branch/HEAD/origin/upstream/dirty worktree; preserve unrelated files and resolve divergence without reset/rebase/force; verify credentials by reference only; read [Visit Registry Pipeline](VISIT_REGISTRY_PIPELINE.md); check live workbook tabs/headers; bound Portal traffic and record independent timestamps.
- This public-repository handoff updates **technical status only**. Managerial initiative status remains in the existing “Реестр инициатив Just for Busy”; do not create a competing registry.
