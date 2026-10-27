# Portal analytics source coverage

Status labels: **ПОДТВЕРЖДЕНО**, **ЧАСТИЧНО ПОДТВЕРЖДЕНО**, **НЕ ПОДТВЕРЖДЕНО**. Scope of SQL aggregate evidence is the 218 October name-marker project IDs; figures must not be generalized to every historical project without a further validated cohort.

| KPI/source question | Status | Evidence and required definition |
|---|---|---|
| Project IDs and project directory | ЧАСТИЧНО ПОДТВЕРЖДЕНО | `proj.i` exists; 217/218 sampled October IDs were present. `proj.name`, `client`, `visits`, `typ`, `dt/dt1/dt2` exist. Archive/active semantics for `typ` not decoded. |
| Total visits | ЧАСТИЧНО ПОДТВЕРЖДЕНО | `visit.i` and `visit.proj` observed; 8,265 rows for 215 sampled projects. Multiple visit rows per business visit or soft-deleted rows not ruled out globally. |
| Visit/action relationship | ЧАСТИЧНО ПОДТВЕРЖДЕНО | `my.visit` joins `visit.i`; 8,202 rows joined for 211 sampled projects. `my.i` is a candidate action/report key, but report equivalence and event semantics need validation. |
| Total claims | ЧАСТИЧНО ПОДТВЕРЖДЕНО | `claim.i` primary key; 190 rows for the sampled cohort, 80 distinct nonzero `claim.visit` IDs. Multiple claim rows per visit are present in aggregate. Claim row lifecycle and onset date not established. |
| Claims by date | НЕ ПОДТВЕРЖДЕНО | `claim.dt` and `claim.repdt` are integer epoch-like fields. Field meaning, timezone and onset semantics are unknown. October `claim.dt` count (190) is a column-filter count, not a validated “claims received in October” KPI. |
| Visits with claims | ЧАСТИЧНО ПОДТВЕРЖДЕНО | `claim.visit → visit.i` is a candidate link; five sampled claim rows had claim project different from visit project. Deduplicate by visit ID and define treatment of inconsistent rows. |
| Project claim incidence / denominator | ЧАСТИЧНО ПОДТВЕРЖДЕНО | `proj` has a project universe independent of claims; 217 sampled IDs existed. Need an approved definition of included projects (active, archived, created, scheduled, or name cohort) and consistent time basis. |
| Monthly/daily/yearly trend | ЧАСТИЧНО ПОДТВЕРЖДЕНО | Integer date-like fields and indexed `log.dt` exist. Historical completeness and timestamp meanings have not been established. Do not trend mixed `dt` fields. |
| Current controller/staff link | ЧАСТИЧНО ПОДТВЕРЖДЕНО | `my.staff` and `claim.staff` join to `user.i` in tested rows. Whether `staff` denotes the controller/reviewer role is not established by schema. |
| Controller workload | ЧАСТИЧНО ПОДТВЕРЖДЕНО | Can count distinct `(project, visit)` / action rows by `my.staff` after validating report-action grain and role semantics. Current counts are not historical assignments. |
| Historical claims by controller | НЕ ПОДТВЕРЖДЕНО | No proven history tying controller assignment to the claim event time. Current `my.staff` is not evidence of historical reviewer responsibility. |
| Claim count vs visits with claims vs projects with claims | ЧАСТИЧНО ПОДТВЕРЖДЕНО | The `claim` table supports separate candidate grains (`claim.i`, `claim.visit`, `claim.proj`); exact KPI semantics, inclusion rules, and inconsistency policy remain to be signed off. |
| Delete/archive handling | НЕ ПОДТВЕРЖДЕНО | No confirmed deletion marker/retention contract. Similar-looking fields are not sufficient evidence. |

## October cohort reconciliation

### ПОДТВЕРЖДЕНО

- The local October collection script defines 218 distinct name-marker project IDs.
- In targeted SQL reads, 217 IDs exist in `proj`; 215 have at least one `visit` row; 211 have a joined `my` row.
- Scoped row counts: 8,265 visits; 8,202 joined `my` rows; 190 claim rows (80 distinct claim visits).
- October filters produce different counts: `visit.dt` 6,621; `visit.dt1` 7,245; joined `my.dt` 6,831; joined `my.dt_visit` 7,629; `claim.dt` 190.

### ЧАСТИЧНО ПОДТВЕРЖДЕНО

Earlier Portal HTML collection evidence in the local checkpoint was 207 projects and 9,547 parsed facts. That extraction has a different source and grain from MariaDB `visit` and `my`. The counts therefore do not reconcile one-to-one. Reasons consistent with the evidence include differing grains/filters and parser sentinel/action rows; definitive row-level reconciliation was not completed. Do not use 9,547 as a SQL visit count or 8,265 as a count of checked reports.

### НЕ ПОДТВЕРЖДЕНО

- Earlier dialog-only values (e.g. prior 7,835 rows or 938 claim associations) are not backed by a saved SQL result artifact and are excluded as SQL evidence.
- The 218-name cohort equals all business-active October projects; one candidate ID is absent from `proj` at audit time.

## DataLens selectors

Date, project, and controller selectors are technically implementable only after a stable semantic view is defined. Use one explicit, documented date field per dataset; expose project ID/name and controller ID/name only after role and name joins are validated. Claim selectors must distinguish claim rows, distinct claim visits, and projects with claims. No “controller fault” or historical claim-attribution selector is supported by this audit.
