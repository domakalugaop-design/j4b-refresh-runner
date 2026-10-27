# DataLens analytics architecture decision

## Decision

**Recommendation: separate, access-controlled analytics store with incremental synchronization, conditional on a validated source contract and infrastructure/security review.** Do not connect DataLens directly to the production MariaDB on the evidence currently available. Retain Google Sheets as a controlled prototype/temporary publication path, not the long-term authoritative analytics store.

This is an architecture recommendation, not an implementation approval. No connection, database, replication, network rule, index, DataLens object, or production configuration was created or changed.

## Evidence relevant to the decision

### ПОДТВЕРЖДЕНО

- MariaDB 10.11.19 is reachable from the audited workstation using the existing read-only `watch` account.
- The inspected grants are `SELECT, SHOW VIEW` on `j4b.*`.
- The inspected connection had an empty `Ssl_cipher`: this connection was not TLS-protected.
- The database includes `proj`, `visit`, `my`, `claim`, `user`, and a large `log` table; relevant tables are InnoDB and use integer date-like fields.
- There are no declared foreign keys in the inspected schema. Some ID links were confirmed by scoped joins, but inconsistencies were observed.

### НЕ ПОДТВЕРЖДЕНО

- DataLens network reachability, supported connection configuration for this deployment, TLS requirements/availability, firewall policy, server-side workload isolation, query quotas, and production failover characteristics.
- Completeness/retention of claims and log history or an approved semantic model for dates/status transitions.

## Options comparison

| Option | Advantages | Risks/limits on current evidence | Relative initial work | Ongoing work |
|---|---|---|---|---|
| **A. Direct DataLens → production MariaDB** | No copy delay; SQL views can serve selectors and filters directly. | **Not recommended now:** audited path had no TLS; production query load and network exposure not assessed; source has no FK constraints, integer dates, and unresolved semantics. Heavy dashboards could compete with portal workload. Read-only credentials alone do not establish safe network architecture. | Low–medium if network/TLS and permissions already satisfy policy; otherwise potentially high. | Medium: query tuning, workload monitoring, schema drift, access controls, incident handling. |
| **B. Separate analytics database** | Isolates production workload; supports history, curated grains, deduplication, tests, and stable DataLens views. Can preserve snapshots and change history. | Requires defining CDC/incremental extraction, delete handling, late updates, lineage, retention, security, backup, and reconciliation. None is implemented or approved here. | Medium–high. | Medium: sync reliability, quality checks, schema evolution, backups, monitoring. |
| **C. Google Sheets materialized layer** | Already operational for controlled test data; easy owner review and manual DataLens setup. | Refresh/copy latency; spreadsheet size and API limits; multiple publication versions need filters; weak fit for large relational joins/history; current TEST writer permission blocker. | Low–medium (existing foundation). | Medium–high: publisher operation, version filters, permissions, capacity, reconciliation. |

## Recommendation and gates

1. Before implementation, confirm exact DataLens connector/network requirements and an approved TLS-protected path; this audit's non-TLS connection is not a design precedent.
2. Approve a read-only source identity and workload safeguards. No public exposure of MariaDB.
3. Define and test canonical grains and fields: project, visit, action/report, claim, staff assignment, and one business date per metric.
4. Specify source retention/deletion handling and whether `log` is complete enough for historical events.
5. Design a separate analytics store with incremental sync, immutable watermarks/change capture, deletion tombstones, reconciliation, retries, backup, and observability.
6. Keep Google Sheets only for reviewed prototype outputs until the replica is accepted.

No DataLens selectors or charts were created. For future selectors, expose project, time period, claim status, controller identity, and association quality as dimensions on curated views, with a publication/snapshot timestamp and explicit “current vs historical” labels.
