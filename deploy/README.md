# Server-side Portal ETL deployment (Analytics VPS)

Deployed 2026-10-28 on the Analytics VPS (Ubuntu 24.04, systemd 255).

## Layout on the VPS

- `/home/service/app/j4b-analytics/etl/src/` — ETL code (0600, root):
  `portal_mariadb_etl.py`, `portal_reference_etl.py`, `parsers.py`,
  `portal_transport.py`, `workflow_analytics.py`, `__init__.py`
- `/home/service/app/j4b-analytics/etl/venv/` — Python 3.12 venv with
  `pymysql==1.1.1`, `psycopg2-binary==2.9.10`
- `/home/service/app/j4b-analytics/portal-etl/` — runtime (0700/0600, outside
  the code tree): `portal-mariadb-etl.ini`, `portal-reference-etl.ini`,
  `secrets/` (MariaDB watch password, portal login/password, PG loader password)
- `/home/service/app/j4b-analytics/secrets/pg_loader_password` — original
  Compose secret (0440) stays untouched; the ETL uses its own 0600 copy.

## Units

- `j4b-portal-etl-sql.service` + `.timer` — every 6 h
  (`*-*-* 00,06,12,18:00:00 Europe/Moscow`), `Persistent=true`
- `j4b-portal-etl-reference.service` + `.timer` — daily 05:30 MSK,
  `Persistent=true`

Both: `Type=oneshot`, `TimeoutStartSec=900`, `Nice=10`, logs in journald
(`journalctl -u j4b-portal-etl-*`), run journals also in PostgreSQL
(`raw.portal_mariadb_etl_runs`, `ops.reference_etl_runs`).

## Operations

```
systemctl list-timers j4b-portal-etl-*
systemctl start j4b-portal-etl-sql.service        # manual run
systemctl status j4b-portal-etl-sql.service
```

The ETL enforces its own gates: storage guard before every write batch,
exclusive flock per channel, EXPLAIN bounds, QA counters, idempotent upserts.
Credentials never appear in chat, logs, code, or the command line; they were
transferred from the macOS Keychain over SSH stdin directly into 0600 files.
