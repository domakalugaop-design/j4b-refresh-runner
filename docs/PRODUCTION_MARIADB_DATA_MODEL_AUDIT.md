# Production MariaDB data model audit

**Initiative:** PORTAL-001.3
**Scope:** read-only, targeted audit; no production writes.
**Branch at audit:** `feature/portal-001-3-mariadb-audit`; base `d00e6e5`.
**Evidence time:** 2026-10-27 (local audit session).

## Evidence handling and limits

The first diagnostic artifact `docs/PRODUCTION_MARIADB_DATA_MODEL_AUDIT.json` and its generating script were found locally. The JSON contained a `SHOW GRANTS` password hash and sample rows from `user`, including personal/account fields; the script hard-coded a database password. Before removing either from the worktree, both were copied to a private local backup with directory mode `0700` and file mode `0600` at:

`/var/folders/9q/g_8k2bjs7jd_j4x4v7jjd5w00000gp/T/opencode/j4b-mariadb-audit-evidence-2026-10-27/`

The unsafe raw JSON and script are not included in Git. The backup is local diagnostic evidence only, not a deliverable. The JSON contained a sensitive grants hash; no hash, password, personal record, raw log payload, or user sample is reproduced here. Findings below use schema metadata and aggregate-only query results.

## Connection and permissions

### ПОДТВЕРЖДЕНО

- Database endpoint used: `lk.j4b.ru:3306`, schema `j4b`, account `watch`.
- Server reported MariaDB `10.11.19-MariaDB-0+deb12u1-log` (Debian 12).
- The diagnostic `SHOW GRANTS FOR CURRENT_USER()` returned `SELECT, SHOW VIEW` on `j4b.*`; the raw output is deliberately not retained in the repository because it included a password hash.
- `Ssl_cipher` was empty on the inspected connection: this connection did not use TLS.
- The inspected schema inventory contained 29 tables. No declared foreign keys were returned from `INFORMATION_SCHEMA.KEY_COLUMN_USAGE`.

### ЧАСТИЧНО ПОДТВЕРЖДЕНО

- No foreign-key declarations does **not** mean no relationships; application-level ID links exist in columns and were tested with selected aggregates.
- Network policy, firewall exposure, server bind configuration, and DataLens reachability were not inspected.

## Relevant tables and observed schema

The metadata-only `DESCRIBE` / `SHOW INDEX` results confirmed:

| Table | Selected columns and apparent role | Declared indexes observed |
|---|---|---|
| `proj` | `i` identifier; `name`; `client`; `user`; `visits`; `typ`; `dt`, `dt1`, `dt2` integer fields | `i`, `user` |
| `visit` | `i` identifier; `proj`; `dt`, `dt1`, `dt2`; `user`; descriptive fields | `i`, `proj`, `city`, composite `idx_visit_proj_dt(proj,dt1,dt2)` |
| `my` | `i` identifier; `visit`; `user`; `state`; `dt`; `dt_visit`; `staff`; `claim` | `i`, `visit`, `state`, `user`, `dt_visit`, composite `idx_my_visit_state(visit,state)` |
| `claim` | `i` primary key; `proj`; `visit`; `my`; `client`; `staff`; `usr`; `typ`; `state`; `dt`, `repdt` | `PRIMARY(i)`, composite `idx_claim_my_state(my,state)` |
| `user` | `i`; `state`; `roles`; name components and account/contact fields | `i`, `last`, `login`, `sid` |
| `log` | `typ`; `usr`; `code`; `dt`; `info` | indexes on `typ`, `usr`, `code`, `dt`; no declared primary key in captured DDL |

The project table is `proj`, not `project`. These are schema facts; field labels alone do not prove business semantics.

## Relationships

### ПОДТВЕРЖДЕНО (for the scoped 218-project cohort only)

The audit reconstructed the October name-marker project IDs from the local collection script and ran aggregate-only reads against that ID set:

- 218 target IDs; 217 IDs were present in `proj`.
- 8,265 `visit` rows across 215 cohort projects.
- Joining `my.visit = visit.i` yielded 8,202 `my` rows across 211 projects.
- `my.staff` had 34 distinct nonzero IDs in the joined cohort; all 34 IDs matched a `user.i` row.
- 190 `claim` rows had `claim.proj` in the cohort; these represented 80 distinct nonzero `claim.visit` IDs.
- Five of those claim rows had a `claim.proj` value differing from the linked `visit.proj`.
- For claims with a nonzero `claim.my`, the audit found no mismatch between `claim.visit` and the linked `my.visit` in this cohort.
- Eight distinct nonzero `claim.staff` IDs matched `user.i` in this cohort.

These results confirm candidate join paths `visit.proj → proj.i`, `my.visit → visit.i`, `claim.visit → visit.i`, `claim.my → my.i`, and `my.staff/claim.staff → user.i` for the tested rows. They do not establish global referential integrity: the tables declare no foreign keys, and five claim/project inconsistencies were observed.

### НЕ ПОДТВЕРЖДЕНО

- Whether `my.staff` always means the reviewer/controller in the business sense, or whether it may have other staff roles.
- Whether a `user.i` match proves that the user had the controller role at the event time.
- Whether all rows are retained, or whether soft deletion/archival is represented outside the inspected columns or in application logic.

## Dates, status, and history

### ПОДТВЕРЖДЕНО

- Relevant date-like fields (`proj.dt/dt1/dt2`, `visit.dt/dt1/dt2`, `my.dt/dt_visit/dt30/dt_pay`, `claim.dt/repdt`, `log.dt`) are `int(11)`, not native `DATE`/`DATETIME` columns.
- Selected cohort date values convert as Unix-second timestamps over a range spanning 2017-10 through 2026-11 when interpreted as UTC. The database/application timezone convention was not established.
- `my.state` is an integer field and contains current state values. In a targeted October `my.dt` read joined through the 218-project cohort, 14 rows had `state=39` and 3,545 had `state=40`; these are current-state counts, not event counts. Results were captured at a different time than the first exploratory output and should be treated as a point-in-time query, not immutable historical truth.
- `log` has no `id` column (the attempted `ORDER BY id` failed); its captured DDL contains `typ`, `usr`, `code`, `dt`, `info` and indexes on the first four fields.
- The initial all-time `log.typ` distribution included types 139, 140, and 142. A targeted October-window query over **all projects** returned 19, 6,431, and 2,831 rows respectively at finalization time. These are global counts by `typ`, not cohort attribution or proof of event meanings; counts can change as the production log grows.

### ЧАСТИЧНО ПОДТВЕРЖДЕНО

- A log table with timestamped rows can support historical event analysis in principle. The meaning, completeness, retention period, and reliable mapping of `typ` values to claim lifecycle events require a separately validated event contract.
- The `claim` table carries its own `dt`, `repdt`, `typ`, `state`, `visit`, `my`, and `staff` fields, but the audit did not prove which date is the claim-onset date or whether closed/reopened events are retained as separate rows.

### НЕ ПОДТВЕРЖДЕНО

- Complete all-time history, deletion history, or retention guarantees.
- A canonical date of visit completion, report review, claim creation, or claim resolution based solely on similarly named integer fields.
- Historical controller assignment at the moment a claim arose.

## Selected October aggregates and limits

For the 218 project IDs in the October name-marker cohort, UTC epoch bounds corresponding to 2026-10-01 through 2026-10-31 were applied:

| SQL condition / grain | Rows |
|---|---:|
| `visit.dt` in October | 6,621 |
| `visit.dt1` in October | 7,245 |
| `my.dt` in October, joined through cohort visits | 6,831 |
| `my.dt_visit` in October, joined through cohort visits | 7,629 |
| `claim.dt` in October and `claim.proj` in cohort | 190 |

These are distinct filters on different fields and are **not interchangeable**. In particular, `dt`, `dt1`, and `dt_visit` have not been semantically reconciled as the same business date. The counts are not the number of October projects, nor a directly comparable “successful visit” denominator.

## Security and operational status

- All database activity was read-only (`SELECT`, metadata `SHOW`/`DESCRIBE`). No production data, grants, or configuration were changed.
- The unsafe raw JSON was removed from the repository after private backup. No raw rows or credential material are part of the audit documents.
- The initial audit script was removed because it embedded the database password, used `SHOW GRANTS` whose output contains a password hash, and selected raw user/log data. It is not a safe reusable audit utility.
- Direct production DataLens connectivity is not approved by this audit; see the architecture decision document.
