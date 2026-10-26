# PORTAL-001.3 — Portal Data Acquisition Architecture (Optimized)

Stage: Optimized Portal Data Acquisition Architecture
Mode: autonomous controlled research (5 of 6 authorized live Portal HTTP requests used)
Date of evidence: 2026-10-26
Repository HEAD at writing: `d27ac71` (branch `codex/portal-001-3-controller-quality-snapshot-20261026`)

Evidence classes used below:

- MEASURED — observed directly in saved sanitized responses or live controlled requests of this stage.
- PRIOR-VERIFIED — established in earlier authorized stages of this initiative.
- INFERRED — conclusion derived from evidence; assumptions are marked explicitly.
- UNKNOWN — not established; listed under blockers.

---

## 1. Confirmed Portal data sources

| # | Source | Grain | Verified this stage | Notes |
|---|--------|-------|---------------------|-------|
| S1 | `POST /action` global, all states | action row (visit-linked) | MEASURED (saved GET + prior POST) | Current-state registry; capped at `limit`; **date filter not enforced**; state filter enforced |
| S2 | `POST /action` per-project (`proj=["<id>"]`, all states, `limit=10000`) | action row (visit-linked) | MEASURED (live, project 8050) | **Complete per-project visit registry**: 38 action rows → 27 distinct visit_ids = exactly the 27 visits on `/proj/8050` |
| S3 | `POST /action` `state[39]=on` + `empty=on` | action row | MEASURED (live) | Complete current-claims list: **12 rows** (test-project claim excluded, see §5) |
| S4 | `/report/` registry (`POST /report/`) | report id list | PRIOR-VERIFIED | `dt1/dt2` **enforced** here (day=144, 3-day=468, month=5058 report ids); misses status-39 reports and visits without reports |
| S5 | SOURCE tab «Выплаты по визитам» | visit (project_id, visit_id) | MEASURED (Google API read) | 89 128 rows; `workflow_state_codes` per visit; **no date columns**; covers all 13 known claim visits; 54 rows contain code 39 (includes resolved/stale states) |
| S6 | `GET /proj/{id}` page | visit list | MEASURED (live, project 8050) | 27 visit rows: №, visit_id, city, code, address, count — **no visit dates** |
| S7 | TEST mart `controller_report_snapshot` (722 rows) | report/visit observation | PRIOR-VERIFIED | corrector_id + FIO from report details; partial coverage; not the Portal population |

Population bound (MEASURED): S5 holds ≥ 89 128 visit rows, so any global S1 fetch bounded by
`limit=10000` truncates. A global full-registry fetch via S1 is **infeasible**.

No server-provided total counts and no pagination metadata exist in any observed response (MEASURED).

## 2. Coverage matrix

| Domain | S1 global | S2 per-project action | S3 claims query | S4 /report/ | S5 cached tab | S6 /proj page |
|---|---|---|---|---|---|---|
| All visits of a period | No (no date filter, capped) | **Yes, after client-side date filter** (complete per project) | No | Report-grain proxy only | Visit-grain but **no dates** | Visit list, no dates |
| Status-39 claims (current) | Partial (with unassigned noise) | Yes (status per row) | **Yes, complete, 1 request** | No (misses them) | Yes (membership codes, stale-prone) | No |
| Controller workload | FIO per row, capped | **FIO per row, complete per project** | n/a | Via report details (existing pipeline) | manager ≠ controller | No |
| Period scoping by the server | **Not enforced** | Not enforced (irrelevant — full project history returned) | Not enforced | **Enforced** | n/a | n/a |

Never equate a report with a visit: S4 rows are report ids; S2 rows are action rows that map to
visit ids (multiple action rows per visit observed — 8 of 27 visits in project 8050 had 2–4 action
rows; MEASURED). Deduplicate by visit_id for visit counts.

## 3. Date-filter findings

- `dt1/dt2` on `/action` are **NOT enforced** — MEASURED, two independent responses:
  - saved GET with form defaults (2026-10-01..2026-10-26) returned rows with visit datetimes
    2023-07, 2026-06..10 and planned **2028** dates across real projects (302 Сбер: 190 rows,
    329 ННЦК: 514, ПСБ: 20 — not only the test project);
  - prior `state[39]` POST with the same window returned a 2023-07 visit and a 2026-10-30 visit.
  The filter fails under every plausible target interpretation (visit date, report date, changed
  date: the 2023 test visit with changed=2026-01-22 was present in-window).
- `dt1/dt2` on `/report/` ARE enforced (PRIOR-VERIFIED).
- Visit datetime semantics: the column holds **planned future dates** (2028 rows observed), so
  "visit date" can be a schedule date, not an performed date. Report datetime = submission
  timestamp. Claim occurrence time: UNKNOWN (current-state only; see §9).
- Consequence: an October claim rate must not be computed until numerator and denominator share a
  verified date definition. The only server-enforced period scope is S4 (report-grain).

## 4. Controller linkage findings

- Every assigned row of S1/S2 carries the controller **FIO** (no controller ID) — MEASURED
  (38/38 rows of project 8050; 648 of 905 assigned rows in the saved global GET).
- Controller IDs exist only in report details (existing `report_reviewers` pipeline, 722 rows,
  22 controllers — PRIOR-VERIFIED). Join key between S2 FIO and mart IDs is FIO text —
  normalization risk is real and must be gated (INFERRED).
- One controller FIO per action row observed; multiple controllers per visit and assignment
  history are UNKNOWN from list sources.
- One visit can produce multiple action rows (MEASURED) → workload must be defined per metric:
  visits assigned (dedupe by visit_id) vs action rows handled (raw rows). Do not mix them.

## 5. Status-filter behavior

- State filter IS enforced: the saved global GET (states 10/20/30/40/50 checked) contained only
  codes 10/20/40/50 (MEASURED). Per-status queries behave like the verified 39 query.
- `empty=on` means "Скрыть неназначенные визиты" (MEASURED, form label). With `state[39]=on` +
  `empty=on` the response is **12 rows** — the test-project claim (action 362206, project 2995
  ТЕСТ) is classified as unassigned-hidden; without `empty=on` the same query returns 13 rows
  plus 1 552 unassigned rows. For KPI purposes: 12 is the real-claim count; the test project
  (2995/3359/3360, name contains «ТЕСТ») must be excluded by project filter in any case.
- Current status is a single code per action row; membership code lists (S5) can contain several
  codes per visit — historical membership ≠ current state (54 cached rows with code 39 vs 12–13
  current claims; MEASURED).
- Secondary status names confirmed from the live filter form (MEASURED): 35 «Анкета отклонена»,
  37 «Анкета проверена», 40 «Анкета утверждена» (also 0,5,10,15,20,25,30,38,45,50 confirmed).
  No live per-status queries were spent; they are unnecessary because S2 returns all statuses at once.
- Status subsets are current states of the same visit population; they are not additive across
  different snapshots in time.

## 6. Recommended acquisition strategy

**Chosen: Option B′ — "existing collector as the base registry + one cheap claims cross-check".**

The production collector already runs daily and, per project, performs `GET /proj/{id}`,
`GET /proj/{id}/edit`, and `POST /action` with `proj=[id]`, all states, `limit=10000`
(MEASURED from `src/acquisition.py::acquire_project`). Request S2 was validated live: its
response is a complete per-project visit registry with visit datetime, report datetime, current
status, and controller FIO. The collector downloads these facts today but persists only
ids/codes — the dates and FIO are discarded.

Therefore:

1. **Base visit registry (Domain A):** extend the collector's parsing to persist, per action row:
   `project_id, visit_id, action_id, visit_datetime, report_datetime, status_code, status_label,
   controller_fio`. One SOURCE tab, action-row grain, dedupe by visit_id for visit metrics.
   This is a production collector change — **requires owner approval; not deployed in this stage.**
2. **Claims (Domain B):** keep the verified one-request global query
   `state[39]=on, empty=on, limit=10000` per refresh as the authoritative current-claims
   observation (12 rows, ~124 KB). After (1) exists, it doubles as a cross-check against the
   collector-derived 39 rows.
3. **Controller workload (Domain C):** from (1) — FIO-level visits assigned and action rows
   handled per period; join to `report_reviewers` corrector_id by normalized FIO where the join
   is unambiguous; publish unmatched-FIO rows with a quality flag instead of dropping them.
4. **Period-scoped counts before (1) is deployed:** use S4 (`/report/`, date-enforced) as a
   clearly-labeled report-grain proxy ("reports submitted in period"), never as "unique visits".
5. **Secondary statuses 35/37/40:** no extra requests — they arrive inside S2 responses; counts
   become available once (1) persists status codes. Meanings are confirmed (§5).
6. Rejected alternatives: global S1 full fetch (population ≥ 89k > any sane limit; truncation);
   per-status global queries for every status (redundant given S2); treating S5 as period source
   (no dates); treating S6 as date source (no dates).

## 7. Request/load estimates

Measured values are marked; the rest are estimates derived from measured sizes.

| Strategy | Portal requests / refresh | Response volume | Completeness |
|---|---|---|---|
| Collector per-project crawl (existing, 57+ projects) | ~3 per project ≈ 170+ (MEASURED pattern: 3/project) | per-project action ≈ 150 KB (MEASURED, 38 rows); scales with project size | Complete per project |
| Claims cross-check (S3) | 1 (MEASURED) | ~124 KB (MEASURED) | Complete current state |
| Global S1 all-states fetch | 1 | ≥ 60 MB estimated (89k+ rows × ~0.7 KB) — **rejected** | Truncated at limit |
| S4 report registry (proxy) | 1–3 (PRIOR-VERIFIED pattern) | small (id lists) | Report-grain only |
| S6 per-project pages | 1 per project | ~22 KB (MEASURED, project 8050) | No dates → insufficient alone |

Dashboard-only refresh without the collector extension: 1 request (claims) + S4 proxy; period
unique visits remain unavailable — this is the documented gap until (1) is approved.

Recovery after partial failure: per-project isolation already exists in the collector
(`project_failure_isolation.py`); the claims query is idempotent and retryable; snapshots are
append-only, so a failed component simply does not publish a new snapshot for that component.

## 8. Snapshot design

Append-only model preserved (existing `visits_snapshot_history` / `visits_snapshot_manifest`).

A snapshot must carry:

- `snapshot_id`, `captured_at_utc`, `source_identity` (source kind + project scope + query fingerprint);
- `collection_completeness` (`COMPLETE_CURRENT_STATE_FILTER_RESULT` / partial / truncated + row counts);
- `date_semantics` (`UNCONFIRMED` until a date definition is verified; `VISIT_DATETIME_INCLUDES_PLANNED`);
- `source_coverage` (projects covered / total projects; assigned vs unassigned rows; excluded test projects);
- per-component timestamps: claims observation, visit-registry collection window, workload basis
  are captured at different times and must never be merged under one timestamp silently;
- status counts and quality flags (unknown codes, FIO-join failures, stale cached codes).

Rule: components captured at different times or with different completeness are published as
separate manifest entries; no incomplete component is merged into a "complete" snapshot.

## 9. Google Sheets analytical schema (proposal — no tabs created in this stage)

Names provisional; reuse existing TEST tabs where noted.

| Table | Grain | Key | Dimensions | Metrics | Update | Timestamp semantics |
|---|---|---|---|---|---|---|
| `visits_by_period` | (period, project_id, visit_id) | period+project+visit | period, project, status_code, controller_fio | unique_visits (dedupe), action_rows | collector extension (1) | visit_datetime (includes planned — flagged) |
| `claims_by_period` | (snapshot_id, visit_id) | snapshot+visit | snapshot_id, project, action_id, observed visit_datetime | current_claims_count | S3 query per refresh | captured_at_utc; claim occurrence time UNKNOWN |
| `controller_workload` | (period, controller_fio) | period+fio | fio, matched_corrector_id (nullable) | visits_assigned, action_rows_handled, claims_linked (only where linkage verified) | collector extension (1) | period derived from visit_datetime |
| `project_summary` | (period, project_id) | period+project | project name/type | visits, claims, workload units | derived | derived |
| `data_quality` | (snapshot_id) | snapshot | component, coverage, flags | row counts, unmatched FIO count, stale-code count | per publish | captured_at_utc |

Completeness rules: a period row set is publishable only when every in-scope project contributed
a complete S2 response (or its failure is recorded in `data_quality`); test projects
(2995/3359/3360) excluded; unassigned rows excluded from controller metrics; `/action/0` rows
never counted as claims.

Existing TEST tabs `visits_current` and `claims_trend` are the natural hosts for
`visits_by_period` and `claims_by_period` once sources are validated; `visits_snapshot_history`
and `visits_snapshot_manifest` remain untouched append-only stores.

## 10. Known blockers

1. Production collector change (persist dates/FIO/status per action row) requires owner approval —
   without it, period-scoped unique visits have no complete source (the core gap).
2. `/action` date filter is broken portal-side; no client-side fix except infeasible full fetch.
3. Controller IDs are absent from list sources; FIO-join is the only bridge (normalization risk).
4. Claim occurrence time UNKNOWN — claims are current-state observations only.
5. Visit datetime includes planned future visits; "performed visit" definition needs owner confirmation.
6. Pre-existing unrelated safety-check failure
   (`portal/project_pipeline/artifacts/PAYMENT_ACQUISITION_TELEMETRY_2026-10-04.md`) — untouched,
   requires separate security review.

## 11. Implementation plan with acceptance tests

1. **Owner decision gate:** approve collector extension (1) and the visit-date semantics.
2. **Collector extension (production change, separate stage):** parse and persist the seven
   fields per action row into a new SOURCE tab; fail closed on unknown status codes.
   - Acceptance: for project 8050 the persisted set dedupes to exactly 27 visit_ids with
     non-empty visit datetimes (matches MEASURED ground truth); re-run is idempotent.
3. **Claims query integration:** scheduled single S3 request per refresh; snapshot per §8.
   - Acceptance: response row count equals distinct current claims; test project excluded;
     idempotent re-run produces `ALREADY_PUBLISHED`.
4. **Mart/schema publication:** build `visits_by_period`, `controller_workload`, `project_summary`,
   `data_quality` per §9 into TEST; owner configures DataLens manually.
   - Acceptance: every published period row set carries coverage + completeness flags; FIO-join
     failures appear in `data_quality`, never silently dropped.
5. **Cross-check gate:** collector-derived 39 rows vs S3 query must reconcile; mismatch publishes
   a quality flag, not a silent choice.
6. **Non-goals this stage:** no collector/scheduler deployment, no DataLens changes, no new tabs,
   no new data publication.
