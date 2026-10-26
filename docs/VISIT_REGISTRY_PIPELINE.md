# Visit Registry Pipeline (PORTAL-001.3)

Visit-level collector enrichment: strict parsing of per-project `POST /action`
responses into an append-only visit registry with TEST-only publication.

## What this pipeline does

Reuses the per-project `POST /action` response already downloaded by
`acquire_project` (see `docs/PORTAL_ACQUISITION_ARCHITECTURE.md`, Option B′)
and extracts, per project:

- **Association grain** — one record per action row (no deduplication).
- **Visit grain** — one record per `(project_id, visit_id)`, deduplicated.
- **Controller workload** — visits and action rows per controller FIO.
- **Project summary** — per-project visit/claim/quality counters.
- **Visits by period** — unique visits per `YYYY-MM` of visit datetime.

All records are derived from a single captured HTML response; nothing is
fetched live by these scripts.

## Components

| File | Role |
| --- | --- |
| `src/visit_registry.py` | Strict parser, grains, snapshot builder, reconciliation |
| `scripts/run_visit_registry_snapshot.py` | Offline HTML → snapshot JSON runner |
| `scripts/publish_visit_registry_test.py` | Gated TEST publication planner/executor |
| `tests/test_visit_registry.py` | Parser, grain, snapshot, gate and runner tests |

## Parsing contract (`parse_action_fact_rows`)

Fail-closed: any unexpected structure raises `VisitRegistryError` instead of
guessing. Validated against the real project-8050 response (39 assigned action
rows → 27 visits, 100% field coverage).

- Row classification: a row with an `/action/` link is an assigned action row;
  a `table-red` row with **no** action link (or `/action/0`) is a sentinel
  unassigned row. Red styling with a real action link is a normal assigned row.
- Field chain is anchored on the `/visit/` link cell (index `vi`):
  `vi+1` visit datetime, `vi+2` price, `vi+3` currency, `vi+4` report datetime;
  `cells[-2]` controller FIO, `cells[-1]` project short name. This survives
  variable control-block layouts (colspan vs split cells).
- Datetimes: `DD.MM.YYYY HH:MM` → ISO `YYYY-MM-DDTHH:MM`. Annotations such as
  «копир» / «срочная оплата» are preserved in `visit_datetime_raw` and flagged
  `VISIT_DATETIME_ANNOTATED`. Malformed dates raise.
- Statuses: numeric labels in `WORKFLOW_STATES` (all 14 codes); unknown codes
  raise; no codes → `STATUS_UNRESOLVED` flag. Status extraction agrees with the
  legacy `parse_action_table` (regression-tested).
- Missing values: absent report datetime → `REPORT_DATETIME_ABSENT`; absent
  controller FIO → `CONTROLLER_FIO_ABSENT`; no visitor link → flag; empty
  response, visit link without action link, or unexpected cell content raise.

## Grains and snapshot

- `visit_grain` deduplicates to `(project_id, visit_id)`; multi-action visits
  keep `action_ids` joined and `action_row_count` (assigned rows only).
  Conflicts surface as flags, never silent choices:
  `MULTIPLE_CONTROLLERS` (visit FIO becomes empty, all FIOs kept in
  `controller_fio_all`), `MULTIPLE_CURRENT_STATUSES`, `VISIT_DATETIME_CONFLICT`,
  `REPORT_DATETIME_CONFLICT`.
- `association_grain` returns every action row unchanged.
- `build_registry_snapshot` produces a deterministic, content-addressed
  snapshot: `snapshot_id` (stable identity) + `fingerprint` (sha256 over the
  canonical fact set). Rebuilding the same facts yields the same ids
  (append-only semantics). Empty fact sets are refused.
- `reconcile_claims` compares current-snapshot claim visits (explicit status-39
  membership) against a native query and a stored snapshot; reports
  `CONSISTENT` / `DIFFERENCES_FOUND` with `only_in_*` lists. Membership code 39
  is treated as current-status evidence only via explicit membership; claim
  occurrence time stays `UNKNOWN` (no verified shared date).
- `verify_test_projects` verifies test-project exclusion by name
  («тест» substring) before any exclusion is applied.

## Snapshot runner

```
python3 scripts/run_visit_registry_snapshot.py RESPONSE.html \
    --project-id 8050 --captured-at-utc 2026-10-26T13:57:52Z \
    --output snapshot.json
```

- `captured_at_utc` must be a real UTC ISO timestamp of the capture (mtime of
  the response file); it is validated and refused otherwise
  (`REFUSED_BAD_CAPTURED_AT`, `REFUSED_HTML_UNAVAILABLE`).
- Output JSON: `status`, `snapshot_id`, `fingerprint`, `manifest`, `facts`
  (association grain), `visit_grain`, `controller_workload`,
  `project_summary`, `visits_by_period`.

## TEST publication

```
GOOGLE_SERVICE_ACCOUNT_JSON_FILE=... python3 scripts/publish_visit_registry_test.py \
    snapshot.json --approve-test-write
```

Target: TEST spreadsheet `1G_pXXNCLTwABSnSPtXxRCxF19VOCOvXi-DK21ZMzytM`
(JFB — Controller Quality Mart — TEST). Production deployment is forbidden.

Gates (all refusal statuses return exit code 2 with a JSON body):

- `--approve-test-write` required (`REFUSED_WRITE_NOT_APPROVED`).
- `payload.status == "OK"` required (`REFUSED_SNAPSHOT_NOT_OK`).
- Writer service account required: `id-j4b-portal-refresh@…` has Editor on the
  TEST spreadsheet; `id-finance-bot@…` is read-only there.
- Same `snapshot_id` with matching fingerprint → resume/idempotent path; with
  a different fingerprint → `REFUSED_SNAPSHOT_ID_CONFLICT`.
- Append-only tabs (`visits_registry_history`, `visits_registry_manifest`,
  `controller_workload`, `project_summary`, `visits_by_period`,
  `data_quality_status`, `metric_dictionary`) only ever append; header
  mismatch on an existing tab → `REFUSED_HEADER_MISMATCH`.
- `visits_current` is replace-only; replacement of foreign-snapshot rows is
  allowed only when `collection_completeness` starts with `COMPLETE`
  (`REFUSED_CURRENT_STATE_REPLACE_INCOMPLETE` otherwise). A header-only tab
  may be upgraded to the current 17-column schema in place.
- Row-level deduplication: already-published rows for the same snapshot are
  skipped, so a partially published snapshot is resumed without duplicates and
  a fully published one returns `ALREADY_PUBLISHED` with no ops.

### Tab layout (TEST)

| Tab | Grain | Semantics |
| --- | --- | --- |
| `visits_registry_history` | association (action row) | append-only fact history |
| `visits_registry_manifest` | snapshot | one row per snapshot |
| `controller_workload` | project × controller | append-only |
| `project_summary` | project | append-only |
| `visits_by_period` | project × period | append-only; `date_semantics = VISIT_DATETIME_MAY_BE_PLAN` |
| `visits_current` | visit | replace-only current-state view (17 columns) |
| `data_quality_status` | publication | shared tab; `publication_key = visit_registry` |
| `metric_dictionary` | metric | shared tab; 6 visit-registry metrics |

### Published metrics

`unique_visits`, `visits_by_period` (with date caveats), 
`controller_workload_visits`, `controller_action_rows`, `claims_current` —
`AVAILABLE_IN_OBSERVED_SNAPSHOT*`; `claim_rate` — `BLOCKED_COHORT_UNVERIFIED`
(numerator and denominator have no verified common date definition; visit
datetime may be a planned future date and the claim occurrence time is
unknown).

## Verified publication (2026-10-26)

Snapshot `2e6037b4e5ab521f27738faf7a2d40d12fbac3788c6663e794def4555106f301`
(project 8050, captured `2026-10-26T13:57:52Z`): 39 assigned action rows →
27 visits; workload: 2 controllers with 21 visits / 29 rows and 6 visits /
10 rows respectively; periods 2026-09 = 12 visits, 2026-10 = 15; quality row
and 6 metrics appended; rerun returns `ALREADY_PUBLISHED`. Controller names
are published only to the TEST spreadsheet, not to this repository.

## Known limitations

- Coverage is per observed project snapshot (`SINGLE_PROJECT:<id>`); a global
  visit registry requires iterating all projects (no server-side global query;
  see the architecture doc).
- Visit datetime may be a planned date (`VISIT_DATETIME_MAY_BE_PLAN`); period
  attribution is therefore caveated.
- Controller identity is a free-text FIO, not an ID; ambiguous multi-controller
  visits are excluded from workload and flagged.
- The production collector does not yet persist visit datetime / report
  datetime / controller FIO from the per-project response; extending
  `acquire_project` storage is a separate owner-approved change.

## 2026-10-26 checkpoint and handoff

The complete technical checkpoint, verified facts, the owner's explicit **UNVERIFIED** hypothesis, unresolved items, and not-yet-executed acceptance plan are in [HANDOFF_2026-10-26_PORTAL_001_3.md](HANDOFF_2026-10-26_PORTAL_001_3.md).

**NEXT TASK:** Multi-Project Visit Registry Acceptance, CONTROLLED TEST VALIDATION. Investigate 8050's 38 vs 39 rows without forced reconciliation, prove cross-project safety of partial `visits_current` publication, validate controller associations and status-39 claim membership, check history/recovery, and issue a documented READY_FOR_PRODUCTION or NOT_READY_FOR_PRODUCTION recommendation. **Do not perform production deployment.**

## 2026-10-26 acceptance result

The acceptance was executed the same day; full evidence in
[ACCEPTANCE_2026-10-26_MULTIPROJECT.md](ACCEPTANCE_2026-10-26_MULTIPROJECT.md).
Summary: the 38-vs-39 discrepancy is a classification/counting artifact of the
earlier ad-hoc analysis (39 is correct; identical action-id sets across two
captures); the parser validated on 5 live projects with varying row layouts;
a `visits_current` partial-write gap was found and fixed (row-count check +
regression test); native `state[39]=on` semantics established (claims +
no-action visits; dt range required); real-TEST publication, read-back and
idempotent rerun verified. **Recommendation: READY_FOR_PRODUCTION** for the
pipeline as validated, with documented limitations (workload invisibility of
no-FIO visits, never-actioned visits in `visits_current`, point-in-time
claims) and the production collector extension still requiring separate
owner approval.

This file is the technical pipeline status index; the existing management initiative register remains authoritative.

## 2026-10-26 claims mart stage

The historical claims mart (status-39 episodes from the MariaDB `log` audit
trail, 90-day window, single consistent snapshot) was built, validated and
published to the TEST workbook: 10,097 entry events reconciled with the
discovery baseline, 7,576 in-window episodes, cohort claim rate 20.97% on a
31,147-visit completed cohort; new TEST tabs `claims_mart_90d` and
`claims_mart_manifest` with gated, idempotent, read-back-verified
publication. Full evidence in
[CLAIMS_MART_90D_ACCEPTANCE_2026-10-26.md](CLAIMS_MART_90D_ACCEPTANCE_2026-10-26.md);
research contract in
[STATUS_39_HISTORY_DISCOVERY_2026-10-26.md](STATUS_39_HISTORY_DISCOVERY_2026-10-26.md).
No production writes or deployment.
