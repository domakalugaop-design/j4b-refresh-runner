# Claims Mart — 90-Day Validation & TEST Publication (2026-10-26)

Stage: PORTAL-001.3 "Just for Busy" — historical claims mart built from the
MariaDB `log` audit trail (research contract:
`docs/STATUS_39_HISTORY_DISCOVERY_2026-10-26.md`), validated and published to
the TEST workbook only. No production writes, no production deployment.

## Canonical window and source

- Window (half-open, Europe/Moscow business time):
  `[2026-07-26 00:00 MSK, 2026-10-27 00:00 MSK)` = `[1785013200, 1793048400)`.
- Source: one consistent-snapshot transaction on `lk.j4b.ru/j4b` (MariaDB,
  read-only user `watch`), captured `2026-10-26T16:39:04Z`. All numbers below
  come from that single extract; live counters drift and are not comparable.
- Extraction scripts and raw extract live outside the repo
  (`/var/folders/.../p39/claims_mart/`); no PII or claim texts are committed.

## Reconciliation with the discovery-stage baseline

| Metric | Discovery stage | This stage | Status |
| --- | --- | --- | --- |
| Total →39 entries in window | 10,089 (live drift) | **10,097** | reconciled |
| typ 142 (client) | 9,946 → 9,954 (drift) | **9,954** | reconciled |
| typ 139 (staff) | 143 | **143** | reconciled |
| Monthly 2026-07 / 08 / 09 / 10 | 604 / 2,913 / 3,820 / 2,760 | same | reconciled |

The earlier 10,089 vs 10,097 gap was live-system drift between queries, not a
boundary artifact; the single-snapshot extract settles it at 10,097.

## Data-quality findings (new)

1. **Duplicate log rows.** The production `log` occasionally writes the same
   transition twice with different `info` payloads (one descriptive, one
   empty), e.g. code 552831 typ 150. The mart merges duplicates by
   `(code, typ, dt)` keeping the richer payload: 10,170 duplicates dropped =
   10,097 pull-overlap (entries also present in per-action streams) + 73
   genuine production double-writes.
2. **typ 122 neutrality verified.** typ 122 (form/review marker) never opens
   or closes episodes: 25/25 sampled full streams (incl. typ 122) produce
   identical episodes with and without typ 122.
3. **Auto-closure evidence.** For episodes still open in the event stream but
   with `my.state` moved on, closure time is estimated by the latest logged
   evidence after the opening entry — `claim.repdt` (claim reply) or typ-131
   submit — and flagged `duration_approximate`. Verified on samples: chosen
   closure always equals the max post-opening evidence timestamp.
4. **Silent exits are common.** 5,396 of 7,576 in-window episodes (71%) have
   no logged exit transition and no closure evidence; the action demonstrably
   left 39 (current state 40/50) but the moment is unknown
   (`closure_time_unknown`). These are excluded from precise SLA metrics.

## Episode grain (opened in window): 7,576 episodes / 7,559 actions / 7,533 visits

| Closure confidence | Episodes | Share |
| --- | --- | --- |
| explicit_log (logged exit transition) | 245 | 3.2% |
| auto_inferred (repdt/typ-131, approximate) | 1,914 | 25.3% |
| closure_unknown (left 39, moment unknown) | 5,396 | 71.2% |
| unresolved_open_at_capture (still 39) | 21 | 0.3% |

- Median resolution: explicit **91.51 h** (precise), auto-inferred
  **107.80 h** (approximate; excluded from precise SLA metrics).
- Entry mix: 9,613 client (typ 142) + 143 staff (typ 139) entry events;
  17 actions reopened (second episode) within the window.
- Repeated entries without an intervening exit stay on one episode as
  additional entry events.
- Orphan closures (exit logged, entry predates log coverage 2022-10-24):
  11,072 episodes excluded from the in-window grain, reported here only.
- Quality flags across in-window episodes: from_state_mismatch 101,
  project_attribution_conflict 22 actions (58 episode flags),
  missing_visit_attribution 7 actions (9 episode flags).

## Project attribution decision

`visit.proj` is canonical. Actions where `claim.proj` disagrees (22) are
flagged `project_attribution_conflict` and excluded from project-level KPIs
while remaining in totals. Actions with no visit attribution (7) are flagged
`missing_visit_attribution`. No silent attribution choice is made.

## Denominator and claim rate

- Denominator: distinct visits with any logged →40 completion event in the
  same snapshot window: **31,147 visits** (31,182 actions).
- Numerator: in-window episode visits matched to that cohort: 6,532 of 7,533.
- **Cohort claim rate: 20.97%** (6,532 / 31,147). Basis: same-snapshot visit
  cohort with a logged completion in the window; not comparable to rates
  computed on other denominators.

## TEST publication

- Workbook: TEST `1G_pXXNCLTwABSnSPtXxRCxF19VOCOvXi-DK21ZMzytM` only.
- New tabs: `claims_mart_90d` (7,576 episode rows + header) and
  `claims_mart_manifest` (append-only publication log). Existing tabs,
  including `claims_trend`, untouched.
- Gates verified live: write refused without `--approve-test-write`
  (`REFUSED_WRITE_NOT_APPROVED`); re-run with the same source returns
  `ALREADY_PUBLISHED` without rewriting (idempotency); a conflicting
  fingerprint under the same publication_id would abort
  (`REFUSED_PUBLICATION_ID_CONFLICT`).
- publication_id `cm90d-1785013200-1793048400-20261026T163904.687610+0000-7a3520c7`,
  fingerprint `sha256:7a3520c7…79a04`.
- Read-back verified: row count 7,577 (incl. header), first/last row match
  the source payload; manifest row present with matching fingerprint.

## Components

| File | Role |
| --- | --- |
| `src/claims_mart.py` | Pure mart: parsing, dedupe, episodes, aggregates, cohort rate, publication rows/manifest/fingerprint |
| `scripts/publish_claims_mart_test.py` | Gated TEST publisher (allowlist, approval flag, idempotency, read-back) |
| `tests/test_claims_mart.py` | 27 tests: boundaries, dedupe, closures, attribution, cohort rate, publication invariants |

## Final report

- CLAIMS_SOURCE: MariaDB `log` audit trail, single consistent snapshot
- PERIOD_START/END: 2026-07-26 00:00 MSK / 2026-10-27 00:00 MSK (half-open)
- TOTAL_EVENTS: 10,097 (9,954 typ 142 + 143 typ 139)
- MONTHLY_TOTAL_RECONCILED: yes (604/2,913/3,820/2,760)
- UNIQUE_ACTIONS: 7,559 with episodes (7,882 affected by transitions)
- UNIQUE_VISITS: 7,533
- CLAIM_EPISODES: 7,576 opened in window (18,978 incl. pre-log orphans)
- REOPENED_EPISODES: 17
- EXPLICIT_RESOLUTIONS: 245
- AUTO_RESOLUTIONS_VERIFIED: 1,914 (evidence-based, approximate; sample-verified)
- UNRESOLVED_EPISODES: 21 open at capture; 5,396 closure_unknown
- PROJECT_ATTRIBUTION: visit.proj canonical; 22 conflicts + 7 missing flagged
- CLAIM_RATE_AVAILABLE: yes — 20.97% on 31,147-visit completed cohort
- TEST_PUBLICATION: claims_mart_90d + claims_mart_manifest (TEST workbook)
- TEST_READBACK: OK (row count, first/last row, manifest fingerprint)
- TESTS_PASSED: 612 (585 prior + 27 new)
- PRODUCTION_WRITES: 0
- PRODUCTION_DEPLOYMENT: NO
- FINAL_STATUS: STAGE_COMPLETE
