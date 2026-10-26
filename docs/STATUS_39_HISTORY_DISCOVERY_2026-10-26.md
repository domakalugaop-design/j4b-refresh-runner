# STATUS-39 HISTORY DISCOVERY — 2026-10-26

**Stage:** PORTAL-001.3 — Historical Status-39 Discovery & Data Readiness
**Mode:** AUTONOMOUS CONTROLLED READ-ONLY RESEARCH (production DB)
**Verdict:** `HISTORY_AVAILABLE` (full event history for the window 2026-07-26…2026-10-26; log coverage since 2022-10-24)

---

## 1. Objective

Determine whether historical events of client-claim (status 39) appearance and
completion can be recovered from the Just for Busy production database for the
window **2026-07-26 … 2026-10-26**, and assess TEST Google Sheets readiness for
the first dashboard. Deliverable: this document + decision
`HISTORY_AVAILABLE / HISTORY_PARTIAL / HISTORY_UNAVAILABLE / BLOCKED`.

## 2. Database access — verified

The owner's initial pointer ("PostgreSQL, port 5432") was corrected by the
prior research chat «Дашборд на БД» (DataLens feasibility, 2026-10-08), which
records the production connection facts:

```
DATABASE_ENGINE=MariaDB
DATABASE_VERSION=10.11.6        (actual now: 10.11.19-MariaDB-0+deb12u1-log)
DATABASE_NAME=j4b
DATABASE_HOST=lk.j4b.ru
DATABASE_PORT=3306
READ_ONLY_USER=watch
PERMISSIONS=SELECT, SHOW VIEW
```

Verification performed this stage:

- `SHOW GRANTS FOR CURRENT_USER()` → `USAGE ON *.*` + `SELECT, SHOW VIEW ON j4b.*`
  — **read-only role confirmed** (no INSERT/UPDATE/DELETE/DDL possible).
- Port 5432 accepts TCP but closes every connection (not a usable Postgres
  endpoint); port 3306 serves MariaDB. Connection from the Mac works directly
  (Clash DIRECT exclusion for `lk.j4b.ru` configured by the owner earlier).
- Password source: macOS Keychain service `j4b-production-db-readonly`
  (never printed, never persisted to disk or docs).
- Client: psycopg2 venv extended with PyMySQL 2.2.8
  (`/var/folders/.../p39/pgvenv`).

`DATABASE_ACCESS_VERIFIED=YES (read-only)`

## 3. Schema inventory (29 tables, InnoDB)

Relevant tables (row estimates from information_schema, verified counts where noted):

| table | rows | role |
|---|---|---|
| `my` | 474,256 | **per-action workflow state** (one row per action; `state`, `dt` = row creation, `visit`, `user`, `staff`, `claim` flag, `dt30`, `dt_pay`) |
| `visit` | 566,859 | visits (`proj`, `dt`, `name`, `city`, …) |
| `claim` | 19,749 (counted) | individual client-claim items (`my` FK, `visit`, `proj`, `dt`, `state` 0/5/10/15/20, `repdt`, `reply`) |
| `log` | 11,778,495 (counted) | **audit trail — the event history** (`typ`, `usr`, `code` = `my.i`, `dt`, `info`); indexed by `code`, `type`, `dt`, `user` |
| `proj` | 5,428 | projects |
| `user` / `user2` | 113,971 / 143,193 | portal accounts (not extracted — PII) |
| `comment` | 379,895 | comments (typ 21/22 in log) |
| `email` | 19.7M | mail merge store (not touched) |

`log` dt range: **2022-10-24 05:54:23 … 2026-10-26 19:17+ (live)**.

## 4. History classification

### 4.1 `my` = CURRENT STATE (mutable)

- `my.state` is **updated in place**. Proof: action `680368` was captured as 39
  by the TEST snapshot (2026-10-26 morning) and the same row now reads
  `state=40`; `my.dt` never changed (2026-10-06 09:53 = creation, matches
  `log` typ=110 creation event to the second).
- Therefore `my` alone **cannot** answer "when did the claim appear".
- 811 rows currently at state=39 span 2020-05-10 … 2026-10-24 — a mix of live
  claims and stale never-resolved ones. Row existence ≠ claim event.

### 4.2 `log` = EVENT HISTORY (append-only audit) — the answer

Every state transition is logged with `code` = action id and `dt`:

| typ | meaning | info payload | window count (07-26…10-26) |
|---|---|---|---|
| 135 | →35 | `'30 &rarr; 35'` | (present) |
| 137 | →37 (under review) | `'30→37'` 13,552; `'35→37'`, `'38→37'`, … | 14,077 |
| **139** | **→39, manual/staff** ("ЧЕРЕЗ ПРЕТЕНЗИИ") | `'40→39'`, `'37→39'` | **143** |
| 140 | →40 (resolved/approved) | `'30→40'` 15,283; `'37→40'` 13,781; **`'39→40'` 149** | 31,681 |
| **142** | **→39, client claim opened** | empty | **9,946** (7,732 distinct actions) |
| 122 | pre-review event (not a transition; semantics partially UNKNOWN) | empty | 20,447 |
| 110/112 | action creation | empty | — |
| 120 | assignment | empty | — |
| 127/130 | controller form save/submit | empty | — |
| 1112 | controller form autosave (~60 s cadence) | form payload | 5.66M total |
| 1113 + 131 | review/claim-reply form autosave + submit | form payload | 962k total |
| 80 | notification e-mails | recipient address | — |
| 21/22 | comments | text | — |

**→39 has exactly two logged paths:** `typ=142` (client opens claim) and
`typ=139` (staff moves action to 39 manually). Both carry the precise `dt`.

**→40 from 39 has two paths:** `typ=140` with info `'39 &rarr; 40'` (manual,
149 in window) and a **silent auto-transition** when the last claim is replied
(no 140 entry; the final `typ=131` claim-reply submit and `claim.repdt` serve
as the resolution marker). Resolution time is therefore recoverable to within
minutes via `claim.repdt` / last 131, exactly for the 140 path.

### 4.3 `claim` = per-issue claim items (supplementary)

19,749 rows since 2020-02-21; multiple claims per action; `state`
0/5/10/15/20; `repdt` = reply timestamp. Claim rows for an action may be
created after the action already sits at 39 (e.g. `682398`: →39 on 10-26
17:18 via typ=142, first claim row 17:49). Use `log` for action-level
history, `claim` for item-level detail.

### 4.4 SNAPSHOT HISTORY

Our own TEST workbook `visits_registry_history` (1,014 rows) — pipeline-built,
not a production source.

## 5. Reconciliation with TEST observations (exact)

The four action ids tracked during acceptance reconcile **exactly** against
`my` + `log`:

| action | visit | project (visit.proj) | TEST capture (10-26 morning) | DB now | log timeline |
|---|---|---|---|---|---|
| 680368 | 778547 | 8278 PhoBo | 39 | **40** | 137 '30→37' 10-21 20:25 → 140 '37→40' 20:30 → **142 21:06 (→39)** → claims 21:25-21:30 + 10-24 → replies 10-26 17:59-18:01 → 40 |
| 683735 | 778608 | 8237 МТС | 39 | **40** | same pattern (140 '37→40' 10-26, resolved) |
| 682398 | 781765 | 8338 Пульт | not in set | **39** | 137 '30→37' 10-20 → **140 '37→40' 10-26 16:40** → **142 17:18 (→39)** → claim 17:49 |
| 682754 | 781766 | 8338 Пульт | not in set | **39** | 137 '30→37' 10-23 → 140 '37→40' 10-26 16:40 → 142 17:18 |

This explains the acceptance observation "2 claims exited (680368, 683735),
2 entered (682398, 682754)" between captures: all four transitions happened
in the **10-26 16:40…18:01** window, fully visible in `log`.

## 6. Window aggregates (2026-07-26 … 2026-10-26, UTC)

- **→39 events:** 10,089 total = 9,946 (typ 142, client) + 143 (typ 139, manual)
- **Distinct actions with ≥1 →39 event:** 7,732 (typ 142) + 139-only actions
- **Per month (events / distinct actions):** Jul(26-31) 604/538 · Aug 2,913/2,505 · Sep 3,820/2,981 · Oct(1-26) 2,760/2,376
- **39→40 via typ=140:** 149 (manual path; auto-resolutions not counted here)
- **5 TEST projects (8050/8397/8240/8080/7591):** 242 →39 events, 183 distinct actions
- **Currently open (state=39) in TEST projects:** 8240 → 2, 8397 → 3 (others 0 — resolved between morning snapshot and now)
- Live system: new claims were being opened at 19:21–19:25 during research.

## 7. TEST workbook readiness matrix (read-only; not modified)

Spreadsheet `1G_pXXNCLTwABSnSPtXxRCxF19VOCOvXi-DK21ZMzytM`, 13 tabs
(state after morning publication `d2187f3e…`, generation `51133d5d…`):

| tab | rows (acceptance) | dashboard-ready | note |
|---|---|---|---|
| controller_report_snapshot | 974 facts | YES | action/visit/project ids + corrector |
| controller_visit_summary | 798 | YES | per-visit rollup |
| data_quality_status | 4 | YES | quality gates |
| metric_dictionary | 35 | YES | metric definitions |
| visits_snapshot_history | 1,014 | YES | snapshot accumulation |
| visits_snapshot_manifest | 3 | YES | generation manifest |
| visits_current | 799 | YES | current state |
| claims_trend | — | PARTIAL | tab exists; needs status-39 event feed (this discovery provides the source) |
| visits_registry_history | 1,014 | YES | multi-project registry history |
| visits_registry_manifest | 3 | YES | |
| controller_workload | 24 | YES | FIO-visibility caveat (168/798 without FIO) |
| project_summary | 7 | YES | |
| visits_by_period | 15 | YES | |

**Matrix verdict:** the TEST workbook is structurally ready for the first
dashboard; the missing piece is a claims-trend feed built from `log`
(typ 139/142/140) — now proven available.

## 8. Decision

```
HISTORY_CLASSIFICATION = HISTORY_AVAILABLE
```

- Event-level →39 and →40 history **is recoverable** from `log` for the whole
  window (and back to 2022-10-24).
- `my` provides current state; `claim` provides item-level detail.

## 9. Caveats / UNKNOWN (do not silently drop)

1. `log` starts **2022-10-24**; earlier transitions are unrecoverable
   (immaterial for the dashboard window).
2. Silent 39→40 auto-resolution on last claim reply (no typ=140 entry) —
   resolution time must be derived from `claim.repdt` / last typ=131.
3. `typ=122` semantics not fully determined (20,447 in window; not a state
   transition; co-occurs with review flow).
4. 811 current-39 rows include stale claims from 2020-2024 that predate `log`
   or never resolved — dashboards must filter by **event dt**, not row
   existence.
5. `visit.dt` has **no index** (confirmed by prior research: full scan ~570k
   rows); keep joins bounded, drive queries from `log` indexes (`type`, `code`, `dt`).
6. Production is live: counts move during research; snapshot semantics must
   be fixed by capture time.
7. `claim.proj` may differ from `visit.proj` (claim filed under another
   project id) — project attribution rule needs one explicit decision before
   the claims-trend feed is built.

## 10. Compliance

- Read-only role verified (`SELECT, SHOW VIEW` only); no writes, DDL, dumps,
  `EXPLAIN ANALYZE`, or unbounded scans performed.
- No PII extracted (no FIO, phones, addresses, e-mails copied into this repo;
  only ids, states, timestamps, and transition mechanics).
- Password used exclusively from macOS Keychain; never printed or stored.
- TEST workbook not modified this stage.

## 11. Checkpoint

- Branch: `codex/portal-001-3-controller-quality-snapshot-20261026`
- This document: `docs/STATUS_39_HISTORY_DISCOVERY_2026-10-26.md`
- Next stage candidate: build the claims-trend feed (log typ 139/142/140 →
  per-period →39/→40 aggregates) into the TEST publication pipeline.
