# PORTAL Shared Analytics Foundation

PROJECT=PORTAL-001.3 · TASK=HYBRID_ETL_CONTROLLED_DEPLOYMENT · 2026-10-28

This document defines the shared data foundation that the Just for Busy portal
ETL feeds and that future dashboards (DataLens or otherwise) must build on.
It is the contract between the ETL channels and every consumer of the
Analytics PostgreSQL database.

## 1. Systems and roles

| System | Role |
| --- | --- |
| `lk.j4b.ru` MariaDB 10.11 (`j4b` db) | Production source of record. Read-only `watch` account; never modified by the ETL. |
| `lk.j4b.ru` HTTPS portal | Source of reference (client/manager/coordinator) labels via authenticated project edit pages. |
| VPS `200.169.190.247`, `/home/service/app/j4b-analytics` | Analytics PostgreSQL 16 (Docker Compose project `j4b-analytics`, internal network, no published ports). |
| Local Mac (OpenWork Desktop) | ETL executor and Automation runner. Holds all credentials (Keychain + `~/.j4b-etl`, 0600). |

## 2. ETL channels

| Channel | Module | Transport | Cadence | Scope |
| --- | --- | --- | --- | --- |
| A — SQL Operational | `src/portal_mariadb_etl.py` | MariaDB legacy-plain (owner-approved, double opt-in, `mysql_native_password` challenge-response verified) → SSH tunnel → PostgreSQL | every 6 h (00:00/06:00/12:00/18:00 MSK) | projects 8181, 8187, 8188, 8189 |
| B — HTTPS Reference | `src/portal_reference_etl.py` | Authenticated HTTPS (Keychain creds) → SSH tunnel → PostgreSQL | daily 05:30 MSK | same 4 projects |

Both channels: storage-guard check before every write batch, exclusive
per-channel file lock (no parallel runs), bounded EXPLAIN validation on the
source, QA counters, run journals, idempotent upserts.

## 3. Entities, grain, and keys

### raw schema (Channel A — technical IDs, statuses, dates, counters only)

| Table | Grain | Key | Columns |
| --- | --- | --- | --- |
| `raw.portal_mariadb_proj` | one row per project | `source_id` | source_id, planned_visits, raw_type, dt, dt1, dt2 |
| `raw.portal_mariadb_visit` | one row per visit | (`project_id`, `source_id`) | project_id, source_id, dt, dt1, dt2 |
| `raw.portal_mariadb_my` | one row per visit detail | (`project_id`, `source_id`) | project_id, source_id, visit_id, raw_state, dt, dt_visit |
| `raw.portal_mariadb_claim` | one row per claim | (`source_id`) | source_id, claim_project_id, linked_visit_project_id, visit_id, my_id, raw_type, raw_state, dt, repdt |
| `raw.portal_mariadb_etl_runs` | one row per ETL run | `run_id` (UUID) | counts per entity (read/written/skipped), error_count, qa |

Link rules (validated every run, QA counters must stay 0):
- `my.project_id + my.visit_id` → `visit.project_id + visit.source_id`
- `claim.linked_visit_project_id + claim.visit_id` → `visit.project_id + visit.source_id`
- `claim.claim_project_id` must be one of the selected projects.

### reference schema (Channel B — labels)

| Table | Grain | Key |
| --- | --- | --- |
| `reference.portal_clients` | one row per client label | `client_id` |
| `reference.portal_users` | one row per user label (managers + coordinators) | `user_id` |
| `reference.project_references` | one row per project | `project_id` (client_id, primary_manager_id, coordinator_count) |
| `reference.project_coordinators` | per-project coordinator membership | (`project_id`, `coordinator_id`) |

Label states: `VALUE_PRESENT` (label captured), `MISSING` (field exists, empty),
`ABSENT` (field not on the form). `content_hash` in `ops.reference_sync_state`
skips re-fetch within a 24 h TTL.

### ops schema (journals)

`ops.reference_sync_state` (per-project freshness), `ops.reference_etl_runs`
(Channel B run journal). Channel A journal lives in
`raw.portal_mariadb_etl_runs`.

## 4. Known limitations and caveats

- **Historical depth**: the ETL loads the current state of the 4 selected
  projects only. There is no change history; `dt`/`dt1`/`dt2` are source
  timestamps, not an audit trail.
- **`proj.visits` counter drifts** in the source; always `COUNT(*)` rows,
  never trust the counter column.
- **`claim` has no project index** in production MariaDB; the ETL uses a
  bounded full scan (validated ≤ 50 000 estimated rows every run; actual
  ~14.8k). Do not copy this pattern to larger tables.
- **Reference labels are point-in-time**: refreshed daily; a label change
  propagates within one day.
- **Scope is strictly 8181, 8187, 8188, 8189.** Extending the project list is
  an owner decision, not an ETL setting change.

## 5. Access model

- `j4b_analytics_loader` — ETL writer (owns `reference` and `ops`, CREATE on `raw`).
- `j4b_analytics_reader` — SELECT on `raw` only (5 tables). `reference`/`ops`
  are closed to reader and PUBLIC until the owner decides to open them.
- `j4b_analytics_monitor`, `j4b_analytics_backup` — operational roles.
- PostgreSQL is reachable only through the SSH tunnel; no published ports.

## 6. Dashboards: what to build on this foundation

Dashboards must read from `raw` (via `j4b_analytics_reader`) and join labels
from `reference` only through an owner-approved view. Do not widen the
allowlist for future dashboards without an owner decision. Reconciliation
against the portal UI (counts per project/status) is required before any
dashboard is published.

## 7. Operations

- Run journals: `raw.portal_mariadb_etl_runs`, `ops.reference_etl_runs`.
- A run that fails QA or the storage guard exits non-zero and writes nothing
  beyond its journal.
- Backups: `j4b-analytics-backup-rotate.sh` / restore test on the VPS.
- Schedules: OpenWork Desktop Automations (proposals rendered 2026-10-28);
  they run only while the signed-in desktop runner is connected.
