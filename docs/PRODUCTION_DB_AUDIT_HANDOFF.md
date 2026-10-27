# Production DB audit handoff

## Executive result

**Read-only MariaDB access succeeded.** The production database contains relational source material for projects, visits, action/report-like `my` rows, claims, users, and log events. This is sufficient to justify a curated analytics-source design, but not to publish every requested KPI without additional semantic validation.

## Confirmed facts

- MariaDB `10.11.19`, schema `j4b`, 29 tables; `watch` grant included `SELECT, SHOW VIEW`.
- The audited session used no TLS (`Ssl_cipher` empty). No production setting was changed.
- `proj.i`, `visit.i/proj`, `my.i/visit/state/staff`, and `claim.i/proj/visit/my/staff/dt/typ/state` are present. No declared FK constraints were found.
- In the targeted October name-marker cohort: 218 project IDs, 217 present in `proj`, 215 with visits, 211 with linked `my` records; 8,265 visit rows, 8,202 joined `my` rows, 190 claim rows (80 distinct claim visits).
- Five scoped claims had `claim.proj` different from the project of `claim.visit`; claim-to-`my` visit mismatches in the tested nonzero keys: zero.
- Date-like fields are integer seconds; the business meaning/timezone for each field still requires a source contract.

## Decision

Do not connect DataLens directly to production based on this audit: the observed path was non-TLS and production load/network safety was not tested. Preferred target is a separately secured analytics store with incremental synchronization and curated semantic views. Google Sheets remains suitable for reviewed prototypes, not a long-term relational history layer.

## Security cleanup

The original untracked JSON included a grants password hash and raw user sample data; the original untracked audit script hard-coded the database password and selected raw production rows. Both were copied to a private local backup (mode `0700` directory / `0600` files) and removed from the repository worktree. No secret or personal record was committed. The original JSON must not be restored or pushed.

## Gaps before implementation

1. Validate the business meaning of `my.staff`, `claim.staff`, `my.state`, and each date column.
2. Resolve the absent October project ID and five claim/project mismatches.
3. Prove retention and deletion/archive behavior and the completeness of `log` event history.
4. Confirm DataLens connector and network/TLS requirements; assess safe query workload outside this audit.
5. Establish a reconciliation contract between MariaDB `visit`/`my`/`claim` grains and Portal/Sheets marts.

## Worktree/Git state at handoff

Audit evidence was recorded in four sanitized Markdown documents. The unsafe raw JSON and hard-coded-credential script were removed. This handoff is not a production deployment authorization. Branch: `feature/portal-001-3-mariadb-audit`.
