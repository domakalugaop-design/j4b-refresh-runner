# Multi-Project Visit Registry Acceptance — 2026-10-26

CONTROLLED TEST VALIDATION of the visit-level registry pipeline
(`src/visit_registry.py`, `scripts/run_visit_registry_snapshot.py`,
`scripts/publish_visit_registry_test.py`) before any production decision.
Production deployment was NOT performed in this stage.

Live Portal budget: 1 authenticated session, 5 per-project `POST /action`
queries (8050 re-capture, 8397, 8240, 8080, 7591), 2 native `state[39]=on`
queries (empty-dt and October-range). Raw HTML kept in 0600 temp files
outside the repository; no personal data committed.

## A. Root cause of the 38-vs-39 row discrepancy (8050)

**Classification: CLASSIFICATION/COUNTING ARTIFACT of the earlier ad-hoc
analysis — strongly supported. Not a new source row.**

Evidence:

1. The only saved per-project 8050 capture (2026-10-26T13:57:52Z) contains
   **39** visit-linked rows — verified four independent ways (raw href count,
   regex row split, strict parser, legacy `parse_action_table`).
2. The architecture doc recording "38 action rows" was committed 2 minutes
   after that capture (16:59:42 local) — same file, under-counted.
3. A fresh re-capture at 2026-10-26T15:41:25Z returned an **identical set of
   39 action ids** (set equality, not just count). The source did not change
   between captures ~1h45m apart, so no new row can explain the delta.
4. The implementation-stage record documents a red-row classification bug
   that was found and fixed, after which all rows classify as assigned —
   consistent with an earlier 38-assigned + 1-misclassified count.

Residual uncertainty: the earlier ad-hoc counting code was not preserved, so
the exact misclassified row cannot be named. The conclusion is therefore
"strongly supported", not "confirmed to certainty".

The doc's "38/38 rows carry controller FIO" is also corrected: all 39 rows
carry the FIO **field**, but only 28/39 have it **populated** (intermediate
workflow rows 5/15/25/45 leave it empty). `PORTAL_ACQUISITION_ARCHITECTURE.md`
lines 22/66/131 updated with pointers to this document.

## B. Multi-project parser validation (5 projects, live captures)

| project | rows | assigned | unassigned | visits (grain) | multi-row visits | status-39 rows |
|---|---|---|---|---|---|---|
| 8050 | 39 | 39 | 0 | 27 | 8 | 0 |
| 8397 | 99 | 73 | 26 | 65+unassigned-only | 8 | 3 |
| 8240 | 236 | 179 | 57 | 166 | 32 | 2 |
| 8080 | 569 | 558 | 11 | 493+unassigned-only | 63 | 0 |
| 7591 | 31 | 31 | 0 | 28 | 3 | 0 |

Combined snapshot `d2187f3e…`: 974 rows, 880 assigned, 94 unassigned,
798 distinct visits, 5 projects, 5 claim visits. No parse exceptions on any
project.

Findings:

- **Row layouts vary between projects** (15 vs 16 cells; 8080 adds a place
  cell and a membership-code cell). The `cells[-2]` controller-cell heuristic
  held for every observed layout; the visitor cell (with `/user/` link) is
  cell [8] in all layouts.
- **Unassigned rows are `/action/0` sentinel rows** — visits with no action
  created yet ("0 приглашений"). Sentinel semantics are correct; 94 such
  rows across 8397/8240/8080.
- **Project 7591 records no controller FIO at all** (31/31 rows,
  `CONTROLLER_FIO_ABSENT`). Real data finding, correctly flagged — not a
  parser bug (the visitor FIO in cell [8] is a different person).
- **384/880 assigned rows (44%) carry annotated visit datetimes** (e.g.
  «13:04  копир», «срочная оплата»); normalization strips annotations
  correctly (`VISIT_DATETIME_ANNOTATED` flag + clean ISO value).
- Statuses beyond the 8050 set observed: 10, 20, 30, 35, 37, 38, 45, 50.
- All 8 multi-row visits of 8050 are monotonic in action_id: intermediate
  statuses (5/15/25/45, no FIO, often 00:00 timestamps) precede the final
  status-40 row (FIO, precise timestamp). Consistent with rows accumulating
  as the report moves through the workflow; exact per-row semantics remain
  UNVERIFIED (no independent corroboration).

## C. `visits_current` safety under partial publication

**Gap found and fixed.** `current_is_ours` checked only the snapshot_id tag,
not the row count: a write interrupted after a partial update would leave
fewer rows, all correctly tagged, and the resume plan would accept the
current view as complete (verified empirically: 10/27 rows → no rewrite
scheduled). Fix: the plan now also requires `len(current_data) ==
len(visit_grain)`; regression test
`test_publication_rewrites_partially_written_current` added (suite 585
passed).

Other current-state behaviors re-verified: foreign rows + incomplete
snapshot → `REFUSED_CURRENT_STATE_REPLACE_INCOMPLETE`; foreign rows +
complete snapshot → sanctioned replace; interrupted after clear → rewrite
on resume. Real-TEST replace 28 → 799 rows verified with read-back (all
rows tagged with the new snapshot_id).

## D. Controller workload semantics

- Grain: per `(project_id, controller_fio)`:
  `visits_assigned` = visits whose single controller FIO matches;
  `action_rows_handled` = sum of the visits' action-row counts.
- **0 multi-controller visits** observed in 798 (the `controller_fio_all`
  field handles the case; no `|` values occurred).
- **168/798 visits (21%) have no controller FIO** and are invisible in the
  workload grain (7591: 28/28 = 100% invisible; 8240: 94; 8397: 46).
  Workload is therefore "recorded-controller workload", not "project visit
  count" — documented limitation.
- Cross-project controllers (Гринцевич Екатерина: 8080/8240/8397) are
  tracked per project; no cross-project aggregation in this grain.

## E. Native status-39 query semantics and reconciliation

- `state[39]=on` + October dt range → **13 real claim rows + 1655 no-action
  visits** (`/action/0`, "0 приглашений") across 105 projects. The no-action
  population rides along with the state filter; it is not a separate
  `empty=on` parameter.
- **Empty dt1/dt2 → 0 rows** (empty table). The query requires dt bounds;
  "empty = all time" is false. The earlier fixture named
  `action_39_empty_on.html` was in fact made with the October range (its
  form defaults show dt1=2026-10-01, dt2=2026-10-26) — the filename was
  misleading.
- Claims are dynamic: between 13:32 and 15:44 UTC two claims left the
  status-39 population (680368, 683735) and two entered (682398, 682754).
- Reconciliation on the captured scope: registry current-39 extraction vs
  native query — **8397: 3/3 match, 8240: 2/2 match** (action ids identical);
  8050/8080/7591 have no claims in either source. CONSISTENT.

## F. History / idempotency / retry acceptance on real TEST

Sequence against the TEST workbook (writer service account):

1. Pre-check: publication plan for the already-published 8050 snapshot
   (`2e6037b4…`) against live tabs → `ALREADY_PUBLISHED`, 0 ops.
2. Multi-project snapshot `d2187f3e…` published with `--approve-test-write`
   → `PUBLISHED`, 9 ops, 974 facts, 798 visits.
3. Read-back: `visits_registry_history` 1014 rows (40 + 974 ✓, append-only),
   `visits_registry_manifest` 3, `visits_current` 799 (header + 798, every
   row tagged with the new snapshot_id ✓), `controller_workload` 24 (3+21 ✓),
   `project_summary` 7 (2+5 ✓), `visits_by_period` 15, `data_quality_status`
   4, `metric_dictionary` 35 (29+6 ✓).
4. Rerun → `ALREADY_PUBLISHED`, 0 ops.

Retry/resume paths are covered by unit tests (history-only interruption,
partially written current, header-only upgrade) and by the earlier real
partial-publication incident that motivated them.

## G. Recommendation

**READY_FOR_PRODUCTION** — for the visit-registry pipeline as validated:
parser (5 diverse projects, varying layouts, no failures), snapshot/grain
builders, gated TEST publication (append-only history, replace gate,
idempotency, resume), and claim reconciliation.

Conditions and separate approvals (not blockers for this recommendation):

- The production collector extension (persist visit/report datetime and
  controller FIO from per-project responses) still requires its own owner
  approval; this stage changed nothing in production.
- Workload invisibility of no-FIO visits (21% here; 100% for 7591) must be
  communicated to dashboard consumers.
- `visits_current` includes never-actioned visits (94 unassigned rows /
  48 unassigned-only visits in this capture) — visible as rows with empty
  controller/status.
- Claims are a point-in-time population; the registry records claim
  membership at capture time, not a claim ledger.
