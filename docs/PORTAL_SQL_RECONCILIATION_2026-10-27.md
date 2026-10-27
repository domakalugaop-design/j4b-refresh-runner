# PORTAL-001.3 — SQL / Portal / Sheets reconciliation

**Stage:** Business Metrics Reconciliation & Analytics Architecture Validation
**Base audit commit:** `a464b55e33a153ae6b008d097d47a82d23e9bd54`
**Working branch:** `feature/portal-001-3-sql-portal-reconciliation-20261027`
**Scope:** read-only; no portal collection, Sheets writes, DataLens edits, or production changes.

## Cohort correction

### ПОДТВЕРЖДЕНО

- The checked `projects_current` source returned **221** projects under the publisher's broad substring rule (including any `oct` or `окт` substring).
- A stricter month-and-year rule (e.g. `10.26`, `10_26`, `1026`, `October/октябрь 2026`) returned **213** candidate October-2026 IDs. This is the preferred provisional cohort; it is not a business-approved cohort contract.
- The previous local collection script held **218** IDs: 210 strict year-coded matches plus eight false-positive/ambiguous broad-only matches. Three strict year-coded projects present in current `projects_current` were omitted from that collection list: **8298, 8415, 8416**.
- Current SQL counts for the 213 strict IDs: 212 present in `proj`; 8,206 visit rows, 8,132 joined `my` rows, and 190 raw claim rows. The missing `proj` ID is 8399.
- The eight broad-only IDs are `736, 742, 3154, 3331, 4907, 5488, 6108, 6408`. Several match because `oct/окт` occurs inside other words; one is marked Q4-2025; two have an October word without a year. Those are not sufficient evidence for October 2026 membership.
- `proj` contains **212/213** strict-cohort IDs. The absent ID is **8399**. A saved Portal collection attempt for 8399 was rejected by the existing parser because the response contained a row for a different project ID (2995). Treat this as **AMBIGUOUS**, not zero activity.
- The three omitted strict projects have SQL rows: 8298 has 4 visits / 6 `my` rows; 8415 has 3 / 3; 8416 has 2 / 2. These projects were **NOT_ATTEMPTED** by the saved HTML collection.

### ЧАСТИЧНО ПОДТВЕРЖДЕНО

The prior Portal checkpoint has 207 projects with parsed facts, 9,547 fact rows, and 11 parse failures from the 218-ID static list. Of those failures, 8133 and 8349 are present in `proj` but have zero `visit` rows and zero joined `my` rows in the bounded SQL check. The remaining failure 8399 is the project-ID mismatch above. The eight broad-only IDs are excluded from the strict 2026 cohort; seven have no 2026 `visit.dt`/`visit.dt1` rows, while 6408 has 13 2026-dated rows but its project marker is not an October-2026 marker.

**Provisional status manifest for 213 strict IDs:** 138 projects have at least one parsed controller FIO; 69 have parsed facts but no controller FIO; 2 had an empty/failed Portal parse and zero SQL visit rows; 1 is ambiguous (8399); 3 were not attempted. HTTP/network failures in the saved 218-project run: zero. “No FIO” means no FIO in this observation, not proof that no controller ever existed.

## Portal fact grain versus SQL grain

The saved Portal checkpoint is a full-year-2026 `/action` response collection for projects selected by project-name markers. It does not contain a historical claim register. The existing parser emits one fact per HTML row, including rows with no action ID; these facts are not all unique visits or unique actions.

### ПОДТВЕРЖДЕНО — totals on the 207 collected project IDs

| Source/grain | Rows or keys |
|---|---:|
| Portal parsed HTML facts | 9,547 fact rows |
| Portal facts with blank `action_id` | 1,477 rows |
| Portal distinct `(project_id, action_id)` | 8,070 |
| Portal distinct `(project_id, visit_id)` | 8,157 |
| Portal fact rows with controller FIO | 4,325 |
| Projects with any controller FIO / without FIO | 138 / 69 |
| MariaDB `visit` rows / distinct composite project-visit keys | 8,197 / 8,197 |
| MariaDB `my` rows / distinct action IDs | 8,121 / 8,121 |
| MariaDB distinct project-visit keys represented in `my` | 7,084 |

The SQL cohort rows were evaluated at a later live read than the Portal checkpoint. The current difference is **40** more SQL project-visit keys than Portal keys and **51** more SQL action IDs than Portal action IDs. The precise omitted/extra key set was not materialized or reconciled at full-cohort key level; do not attribute the difference to time drift, filtering, or parser behavior without that proof.

The previously reported **7,835** Portal rows and **225** workload rows have no saved source payload/capture manifest in the repository or private checkpoint examined for this reconciliation. They cannot be exactly reproduced or proven to share the present project cohort/grain. The current local facts aggregate has **264** distinct `(project_id,controller_fio)` pairs. `PREVIOUS_7835_EXPLAINED=NO_EXACT_REPRODUCTION`; `PREVIOUS_225_EXPLAINED=NO_EXACT_REPRODUCTION`.

### Bounded control sample

| Project ID | Portal fact rows | Portal rows without action ID | Portal distinct visits | Portal distinct actions | SQL visit rows | SQL `my` actions | Portal action IDs found in SQL same project |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 8181 | 195 | 69 | 175 | 126 | 175 | 126 | 126 |
| 8187 | 192 | 1 | 168 | 191 | 168 | 191 | 191 |
| 8188 | 102 | 0 | 82 | 102 | 82 | 102 | 102 |
| 8189 | 69 | 0 | 49 | 69 | 49 | 69 | 69 |

### ПОДТВЕРЖДЕНО

- In these four projects, every tested Portal action ID matched a MariaDB `my.i` row within the same project; Portal visit IDs matched SQL visit IDs by count.
- This supports the **candidate** ID mapping `Portal action_id → my.i` and `Portal visit_id → visit.i` for this sample. It does not prove complete matching across all 207 projects.
- In project 8181, 69 of 195 Portal fact rows have no action ID; SQL has 126 `my` actions. This directly demonstrates why Portal fact-row counts are not action counts or visit counts.
- Project 736 returned no Portal fact rows. SQL contains 13 visit rows for it, all dated in 2017, and zero `visit.dt` or `visit.dt1` rows in 2026. The broad `окт` match is not evidence of an October-2026 operating cohort.

### Даты в контрольном проекте 8189

The SQL visit table has 49 visits for project 8189; Portal has 49 distinct visit IDs; the DataLens-source project summary also shows 49 total visits. However, among 38 Portal visits with a date, only 3 Portal calendar dates matched SQL `visit.dt1` converted to UTC date; none matched `visit.dt` or `visit.dt2` under that direct conversion, and 35 did not match those SQL dates. Among 45 Portal action IDs with a report date, all IDs were found in SQL `my`, but Portal report dates matched SQL `my.dt` on 1/45 and `my.dt_visit` on 19/45 by UTC calendar date.

These date comparisons are **not a field-semantic validation**: Portal dates are parsed as page values and the database/application timezone convention is unknown. Do not select `dt`, `dt1`, `dt_visit`, or `my.dt` as one universal date axis based on these results.

## Claims reconciliation

### ПОДТВЕРЖДЕНО

For the 221 broad-name cohort in the current scoped read, MariaDB has **190 `claim` rows**, representing **80 distinct `(proj, visit)` keys**. Five rows have a `claim.proj` differing from the linked `visit.proj`; `claim.my`/`my.visit` mismatches in the checked nonzero links: zero. The earlier 218-ID cohort and current live values are different snapshots; the count is not a stable baseline.

For project 8189, applying the claims mart's **numeric timestamp bounds** `[1785013200,1793048400)` to `claim.dt` and comparing to its declared 90-day episode publication:

| Source | Records/episodes | Distinct claim visits |
|---|---:|---:|
| MariaDB `claim` rows selected by those numeric `claim.dt` bounds | 41 | 22 |
| TEST `claims_mart_90d` episode source | 38 | 38 |
| DataLens-source `Показатели проектов` summary | 38 claim rows/count | 38 visits with claims |

The 22 SQL claim-visit keys are all present in the 38 TEST/DataLens claim-visit set; there are 16 TEST/DataLens visit keys not present in the raw `claim` rows selected by that `claim.dt` filter, and no SQL-only visit keys in this project comparison. The raw `claim.dt` business meaning has not been proven equal to episode `opened_at_utc`; this is an ID-set reconciliation, not proof of a common event-time window. The TEST claim episode source is log-derived and has a different grain/history from the raw `claim` table. Do not equate either table's row count with independent customer incidents without a canonical definition.

The DataLens-source summary calculates `38 / 49 = 77.6%`, but its own metadata says claims are from 26.07–26.10 while “all visits” are a snapshot. This ratio is mathematically correct but **not a same-period incidence rate**.

## Existing sources and DataLens readback

### ПОДТВЕРЖДЕНО

- Production `projects_current` has a single project row for the sample project 8189; its period field is blank in the sampled row. The separate DataLens-source workbook's “Показатели проектов” row for 8189 reports 49 visits, 38 visits with claims, 38 claims, 77.6%, and explicitly labels coverage partial/different periods.
- The DataLens-source workbook's “О витрине” tab says the first claims period is 26.07.2026–26.10.2026, visit data is a snapshot, status is partial, and automatic publication is not connected.
- The “Показатели проектов” tab has 495 project IDs in its inspected ID column. This is a claim-project summary population, not a validated all-project denominator.
- The TEST workbook's prior accepted `project_controller_claims` v2 source has 767 union projects (including zero-claim projects), but its claims source has 36 episode timestamps later than the declared source capture; its project claim-rate fields are consequently blank/`NOT_AVAILABLE`.
- In the DataLens-source workbook, the first 20 rows inspected for `Проекты`, `Визиты`, `Претензии`, `Контролёры`, and the first 10 for `Показатели по периодам` returned headers only. This confirms no detail data in those inspected ranges; the entire tabs' row counts were not queried.

### НЕ ПОДТВЕРЖДЕНО

- DataLens dataset/chart filter expressions and current dashboard calculations. Opening the supplied DataLens dataset URL redirected to Yandex SSO and requested SMS code/CAPTCHA; no sign-in attempt or dashboard modification was made.
- Automatic refresh of the DataLens-source workbook. Its metadata note explicitly says automatic publication is not connected.
- A full 0-claim project population in that DataLens source, or daily/monthly data in its `Показатели по периодам` tab.

## Scheduler/source publication context

The local `j4b-cloudflare-dispatcher` files describe a Worker that dispatches GitHub `workflow_dispatch`; the README describes two Cron Triggers, while the local `wrangler.toml` says `crons=[]` and comments that production scheduling moved to a DFB systemd timer. This directory has no Git metadata and the deployed Worker/DFB state was not queried. The Worker code shown in the local source contains GitHub API dispatch logic, not Google credentials or Sheets publication/readback logic. Therefore the active scheduler and actual publication mechanism remain **НЕ ПОДТВЕРЖДЕНО**. No workflow or schedule was run.

## Conclusion

`PROJECT_JOIN_VERIFIED=PARTIAL` and `VISIT_ACTION_JOIN_VERIFIED=PARTIAL` on the listed control samples. `CLAIM_EVENT_GRAIN_RECONCILED=NO`; `DATE_AXIS_RECONCILED=NO`; `SAME_PERIOD_VISIT_CLAIM_RATE=NOT_AVAILABLE`; `ALL_PROJECT_CLAIM_RATE=NOT_AVAILABLE`; `HISTORICAL_CONTROLLER_ATTRIBUTION=NOT_AVAILABLE`. The 9,547 Portal facts, 8,197 SQL visits (207-project set), and 38 claim episodes for the DataLens control project are different grains and sources. Some control joins agree exactly; full-cohort row-key reconciliation and the business definitions remain incomplete.
