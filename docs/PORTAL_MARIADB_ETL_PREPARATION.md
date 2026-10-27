# PORTAL-001.3 MariaDB ETL — preparation only

## State

This branch contains code and synthetic tests only. No MariaDB connection, PostgreSQL connection, ETL run, Docker command, schedule, or source-data export was performed while preparing it. The current MariaDB route was previously observed without TLS; no independently trusted SSH host key / approved DB tunnel or owner-only MariaDB credential source has been verified in this runtime. Therefore the live path is fail-closed until those inputs are supplied and verified.

The previously described 213-project strict-marker set remains provisional. There is no project-ID default in the code; a future operator must pass an explicitly approved list. No list is embedded in this branch.

## Privacy-minimal source contract

Queries use fixed table/column identifiers and bound ID parameters. The extractor does not request project names, clients, users/staff, descriptive text, attachments, logs, or document fields (project names may contain personal names). It preserves raw integer IDs, status/type values, and integer date values without converting dates or calculating business KPIs.

| Source relation | Selected source columns | Target table |
|---|---|---|
| `proj` | `i, visits, typ, dt, dt1, dt2` | `raw.portal_mariadb_proj` |
| `visit` | `i, proj, dt, dt1, dt2` | `raw.portal_mariadb_visit` |
| `my` joined to selected `visit` rows | `i, visit, state, dt, dt_visit` | `raw.portal_mariadb_my` |
| `claim` plus linked visit project | `i, proj, visit, my, typ, state, dt, repdt` | `raw.portal_mariadb_claim` |

No status aliases or questionnaire/claim business meanings are inferred. `claim.proj` and the linked `visit.proj` are kept separately; mismatches become QA counts, not rewritten values. Claims whose project or linked visit enters the explicit cohort are included so conflicts can be observed. The source model does not provide a project key on `my`; only `my` rows with a visit link into the selected cohort can be attributed safely.

## Gates and bounds

- Project IDs are mandatory CLI input, positive, unique, capped at 250 per run. Batch size is capped at 5,000 (default 500); each source entity is capped at 50,000 rows before target writes. SQL parameters are bound; only the number of placeholder tokens varies.
- Before extraction, the code verifies the direct account's effective metadata grants using `SELECT` from `INFORMATION_SCHEMA`; it allows only `SELECT`/`SHOW VIEW` within `j4b` plus global `USAGE`, with no grant option or active role. It checks the required columns, requires InnoDB tables for a consistent read-only snapshot, and runs `EXPLAIN` on each bounded query. Full scans, unknown access types, missing estimates, and estimates above 50,000 rows are rejected before extraction or target writes.
- TLS mode uses a certificate-verifying `SSLContext` with hostname checks. The driver subclass aborts before sending an authentication packet if the MariaDB handshake does not advertise TLS. There is no plaintext fallback.
- SSH mode uses only a pre-existing `known_hosts` entry whose fingerprint set must match independently supplied SHA256 fingerprints. `StrictHostKeyChecking=yes`, `BatchMode=yes`, and forwarding-agent disabled; unknown keys are never accepted or written. The SSH endpoint must terminate on the DB host; an optional jump chain is separately fingerprint-checked.
- PostgreSQL is reached only by SSH-forwarding to the `j4b-analytics-postgres-1` container's address on `j4b_analytics_internal`. Before opening the tunnel, the code verifies that the Docker network is internal, the container is healthy, its address is within that network, and `docker port` reports no published port. This is read-only inspection; no Docker configuration is modified.
- The storage guard is called before target DDL, the run-log row, every data batch, and run finalization. It is executed on the VPS over the same verified SSH identity. Anything other than a successful `STORAGE_GUARD=ALLOW` response stops the next write. The guard uses `docker compose run --rm`, so its helper container is ephemeral; it does not change the Compose file or create a schedule.
- Target tables are confined to the existing `raw` schema. IDs are constrained by primary/unique keys and loaded with idempotent upserts; unchanged rows count as skipped. `raw.portal_mariadb_etl_runs` records counts and QA metrics without source row values. If the guard blocks a later operation, no subsequent target write is attempted; an interrupted run may remain marked `RUNNING` because updating its status would itself require an `ALLOW` guard result.

## Running contract (not run here)

The CLI requires a protected external INI plus explicit `--project-id` arguments. Without `--run-live`, it only validates setup and reports that it did not connect. Live mode requires source credentials in an owner-only file outside the repository, a verified source transport, target SSH known-host entries with independently approved fingerprints, and a protected PostgreSQL loader password file. No such configuration or password file was created for this task.

The code is not a cohort selector. It will not discover projects by name, default to the provisional 213 IDs, or broaden to portal history. Do not run `--run-live` until the cohort and both SSH/TLS and credential gates are approved. The target runner is designed to execute from the authorized workstation and reach the VPS through the fingerprint-verified SSH route; it does not add a PostgreSQL host port or alter Docker networking.

## Synthetic tests

`tests/test_portal_mariadb_etl.py` uses only in-memory fake source/target objects and temporary synthetic files. It covers source grant rejection, no-TLS fail-closed behavior, explicit bounded IDs, parameterized allowlisted queries, raw dates/statuses, privacy column exclusion, visit/action/claim relations, claim-project mismatch preservation, duplicate rejection, rerun idempotency, and storage-guard stop behavior. No production or Analytics database credentials are read by these tests.
