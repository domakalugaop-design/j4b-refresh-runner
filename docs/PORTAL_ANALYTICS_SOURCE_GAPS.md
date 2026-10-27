# PORTAL-001.3 — Analytics source gaps

## 1. October project cohort

### ПОДТВЕРЖДЕНО

- Current `projects_current` yields 221 hits under the old broad substring matcher but only 213 with an explicit 2026-10 marker.
- The broad-only eight include no-year/2025 markers and substring false positives (e.g. `oct` embedded in unrelated words or `окт` inside another word). These must not automatically be treated as October 2026 projects.
- The prior static collection set had 218 IDs; it missed strict-marker projects 8298, 8415, 8416. Those three have SQL records (9 visits, 11 `my` rows combined) and were not attempted by the saved Portal collection.
- The earlier reported 7,835 Portal rows and 225 workload rows have no saved source payload/capture manifest; exact cohort and grain cannot be reproduced. The current Portal facts aggregate has 264 distinct FIO×project pairs.
- Of 213 strict IDs, 212 exist in `proj`. The absent ID 8399 had a Portal response whose project ID did not match the requested one.
- The saved Portal collection successfully parsed 207 projects, of which 138 had at least one controller FIO and 69 had facts but no FIO. Two strict IDs (8133, 8349) have empty Portal parse results and zero SQL visit/my rows in the checked source; 8399 is an ID-scope mismatch; three strict projects were not attempted.

### НЕ ПОДТВЕРЖДЕНО

- Owner-approved handling for month-only names without year.
- Whether `projects_current` is the complete active/archived universe and how its `period` field is populated. Example project 8189 had a blank `period` in the sampled row despite its name marker.

## 2. Visit/action reconciliation

- Portal snapshot (207 parsed projects): 9,547 HTML fact rows; 1,477 had blank `action_id`; 8,070 distinct project/action keys; 8,157 distinct project/visit keys.
- Current MariaDB reads for those same 207 project IDs: 8,197 `visit` rows/keys, 8,121 `my` rows/action IDs, and 7,084 project/visit keys represented in `my`.
- Therefore current SQL exceeds the saved Portal snapshot by 40 visit keys and 51 action IDs. The aggregate key difference was not materialized. The SQL reads occurred later than the sequential Portal captures; live drift is possible but has not been proven to explain the differences.
- Four control projects (8181, 8187, 8188, 8189) reconcile exactly by counts, and their Portal action IDs match SQL `my.i`. This sample does not certify full-cohort completeness.
- For 8189, 49 Portal visit IDs, 49 MariaDB visits, and the DataLens summary's 49 visits agree. Portal action IDs (69) all match SQL `my.i` in that project. Yet 38/45 sampled date values fail to match candidate SQL visit dates, and Portal report-date values match `my.dt` only 1/45 / `my.dt_visit` 19/45 by UTC calendar date. Date semantics/timezone remain unverified.
- Portal full fact rows are not visits: the 8181 sample contains 195 fact rows, including 69 blank-action rows, 126 action IDs, and 175 visits.

## 3. Claims reconciliation

- MariaDB `claim` is a row source keyed by `claim.i`; the accepted `claims_mart_90d` is an event-episode source derived from `log`. They are different source grains.
- For 8189, the numeric `claim.dt` 90-day filter returns 41 raw rows / 22 distinct claim visits; the log-derived TEST source has 38 episodes / 38 distinct visits. The 22 SQL claim-visit keys are a subset of the 38 episode keys; 16 episode visit keys are not present in the raw claim rows selected by that filter. `claim.dt` is not proven to mean the same thing as episode `opened_at_utc`.
- Current broad October-name SQL read: 190 raw claim rows, 80 distinct project/visit keys, and five `claim.proj`/`visit.proj` inconsistencies. A previous 90-day event extraction has its own capture anomaly; it is not an all-time complete incident register.
- The DataLens source summary for 8189 shows 38 visits with claims and 38 claims. It is consistent with the 90-day episode summary, not with raw `claim` row grain.

## 4. Dates, history, and controller role

- Relevant SQL dates are integer epoch-like fields, but the business meanings of `visit.dt`, `visit.dt1/dt2`, `my.dt`, `my.dt_visit`, `claim.dt/repdt`, and `log.dt` are not validated against the Portal fields.
- The SQL `log` table and historical claims mart can support event-time analysis in a bounded period. Complete all-time retention/deletion guarantees were not established; the current DataLens claims source says its initial window is 26 July–26 October 2026.
- `my.staff`/`claim.staff` join to `user.i` in selected rows, but staff role semantics are not verified as controller/reviewer. The Portal FIO parser is current-state observation. Historical controller assignment at claim time remains unavailable.
- The DataLens-source `Визиты`, `Претензии`, `Проекты`, `Контролёры`, and `Показатели по периодам` tabs returned header-only content in the inspected top rows. Full tab row counts were not retrieved. `О витрине` says automatic publication is not connected.

## 5. DataLens and scheduler gaps

- The supplied dataset URL redirected to Yandex SSO with an SMS-code/CAPTCHA step; no dashboard/dataset filters or charts were read or changed. Existing source-sheet data is the limit of this reconciliation.
- Local Cloudflare dispatcher README describes two Cron Triggers dispatching GitHub `workflow_dispatch`; local `wrangler.toml` has `crons=[]` and says production scheduling moved to DFB systemd. The local Worker source dispatches a workflow; it does not contain Google credentials or Sheets publication/readback logic. The Worker folder has no Git metadata and deployed Worker/DFB state was not queried. Current scheduler is therefore unverified.

## Recommended next evidence gates

1. Approve the strict 213-ID year-coded cohort; classify month-only names; explicitly include 8298/8415/8416 and resolve 8399.
2. Collect only the three newly identified projects with the existing bounded client once authorized; record per-project capture status. Do not rerun the full HTML collection.
3. Materialize anonymized key-overlap counts for all 207 Portal projects to locate the 40 visit-key and 51 action-ID discrepancy; preserve unresolved keys as exceptions.
4. Approve canonical event/date/claim definitions and project-attribution treatment.
5. Validate controller role and historical assignment semantics before controller KPIs.
6. Verify deployed scheduler and DataLens authentication/field filters through approved read-only access; do not edit or run publication.
