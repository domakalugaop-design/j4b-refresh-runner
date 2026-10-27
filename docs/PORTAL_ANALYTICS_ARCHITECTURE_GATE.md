# PORTAL-001.3 — Analytics architecture readiness gate

**Gate:** `PARTIAL — NOT READY TO IMPLEMENT AN ANALYTICS DATABASE`
**Changes:** no MariaDB/Portal/Sheets/DataLens/Worker/workflow changes were made.

## Evidence-based decision

### ПОДТВЕРЖДЕНО

- MariaDB has usable relational sources (`proj`, `visit`, `my`, `claim`, `user`, `log`) and selected project/action/claim joins were verified read-only.
- The 8181/8187/8188/8189 control samples reconcile Portal unique visit/action counts with MariaDB; this does not generalize to the full population, where SQL and Portal differ by 40 visit keys and 51 action IDs.
- Claims have at least two non-equivalent sources/grains: raw current `claim.i` rows and event-derived `claims_mart_90d` episodes.
- The audit connection had no TLS. DataLens network reachability, firewall policy, and database query isolation were not tested.
- The DataLens-source workbook is a partial summary/snapshot and says automatic publication is not connected. Project 8189's 38/49 (77.6%) joins a 90-day claims numerator to a visit snapshot denominator and is not a same-period rate.
- Local Worker code dispatches a GitHub workflow and does not itself publish Sheets. Local README and `wrangler.toml` conflict about whether Cron triggers are deployed. Actual deployed schedule is not verified.

### ЧАСТИЧНО ПОДТВЕРЖДЕНО

- A separate analytics database could isolate production reads, maintain curated event grains and support selectors, if an incremental source contract, completeness/retention rules, and reconciliation are established.
- The current Google Sheets materialized source can support a bounded demonstration and manual review. It does not currently demonstrate zero-claim denominator completeness or automatic publishing in the claims DataLens workbook.

### НЕ ПОДТВЕРЖДЕНО

- Direct DataLens network/connector feasibility, TLS termination options, firewall reachability, production query budget, and failover behavior.
- Safe change-data capture or a unique monotonically increasing cursor for all required tables. `visit`, `my`, and `log` lacked declared primary keys in the captured DDL; `claim.i` is primary key.
- Complete event retention, deletion/archive behavior, or a historical reviewer/controller assignment feed.

## Option gate

| Option | Decision | Basis |
|---|---|---|
| Direct DataLens → production MariaDB | **NO-GO now** | Inspected route was non-TLS; network/load safety not tested; date and claim semantics unresolved; full-cohort visit/action reconciliation incomplete. A `SELECT` grant alone is not a security or performance approval. |
| Separate analytics database | **Preferred candidate, conditional; do not build yet** | Strong potential to isolate reads, version data, define distinct project/visit/action/claim grains, and expose curated selectors. Must first establish change capture, deletion handling, consistent snapshot semantics, quality reconciliation, TLS/private networking, backup and operational ownership. |
| Existing Google Sheets/DataLens source | **Keep demo unchanged; temporary/partial only** | It offers a readable project-level summary, but the source workbook's details and period tabs showed headers only in checked ranges and its metadata says no automatic publication. Its sample ratio has mismatched periods. No replacement/refresh was attempted. |

## Gates before approving a build

1. **Population:** approve strict 2026-10 name markers (213 current candidates) and adjudicate month-only/false-substring names; resolve 8399 and the three not-attempted projects.
2. **Grain:** reconcile Portal action/visit keys to SQL `my.i`/`visit.i` for the full selected cohort, documenting the 40/51 differences.
3. **Claims:** choose raw claim row versus log-derived episode as the business incident grain; define reopen, duplicate, project-conflict and missing-visit policies.
4. **Time:** establish date meaning/timezone for project, visit, action, claim-open/close and log events; only then define day/month/year filters and rates.
5. **Denominator:** define all included projects including verified zero-claim projects; do not use a claim-only project list as the denominator.
6. **Controller:** validate role/identity and historical assignment before controller-linked claims or workload rates.
7. **Security/operations:** approve private network, TLS, least-privilege identity, workload isolation, retention, monitoring, backup, incremental cursor and delete/tombstone handling.
8. **Scheduler:** verify deployed Cloudflare/DFB schedule. Do not infer active scheduling from conflicting local files; do not run GitHub Actions.
9. **DataLens:** after approved authentication, inspect existing dataset filters and dimension grain read-only. Preserve the demonstration dashboard unchanged.

## Recommendation

Proceed with requirements and bounded reconciliations only. If gates 1–8 pass, propose a small read-only proof-of-concept in a separate analytics store. Do not connect DataLens directly to production or create infrastructure at this stage. Maintain Sheets as a clearly labelled, partial demonstration source until the warehouse/replica gate is approved.
