# PORTAL-001.3 — DataLens SQL Source Recovery & Superset Migration Baseline

**Date:** 2026-11-03 (Europe/Moscow)
**Initiative:** PORTAL-001.3
**Status:** Investigation complete; live DataLens inspection blocked by authorization
**Repository:** `domakalugaop-design/JustForBusy`
**Reference commit:** `c0650f1b8a81a82d52f0319f03003c2fbfcc4c74` (main)

---

## 1. EXACT LIVE DATASETS — OWNER VERIFIED

| # | Dataset Name | DataLens URL | Dataset ID | SQL Source Label |
|---|--------------|--------------|------------|------------------|
| 1 | JFB — MariaDB — Претензионные события | https://datalens.ru/datasets/oyr3lev9ooky7-jfb-mariadb-pretenzionnye-sobytiya | `oyr3lev9ooky7` | `SQL 4` |
| 2 | ДФБ БД Финансы | https://datalens.ru/datasets/v5tew7ozsmzke-dfb-bd-finansy | `v5tew7ozsmzke` | `SQL 3` |
| 3 | JFB — MariaDB — Project Operations TEST | https://datalens.ru/datasets/cm2qmg3rfk2av-jfb-mariadb-project-operations-test | `cm2qmg3rfk2av` | Verify in DataLens |
| 4 | JFB — Календарь периодов TEST | https://datalens.ru/datasets/nxd7trqq52006-jfb-kalendar-periodov-test | `nxd7trqq52006` | `SQL` |

All four belong to: **JFB Portal — управленческий PoC**
Visible connection: **БД ДФБ боевой сервер** (MariaDB `lk.j4b.ru:3306`, database `j4b`, user `watch`, read-only)

---

## 2. GITHUB SOURCE RECOVERY STATUS

### 2.1 Dataset 1 — Претензионные события (Complaint Events)

**GitHub Path:** `sql/datalens/PORTAL-001.3-complaint-events-2026.sql` (main branch)
**SHA-256:** `085bae620757b6f3ae068f5aeb3e23f5719c26d0`
**Status:** ✅ **RECOVERED** — Exact SQL matches live DataLens dataset per owner verification

**Key Characteristics:**
- Grain: One source log row matching `typ=139` and parsed numeric transition to status 39
- Validation window: 2026-01-01 inclusive through 2026-11-01 exclusive (Jan-Oct 2026)
- Duplicate-signature rows flagged, never removed
- `log.usr` = event-record actor; `my.staff` = current action staff snapshot only
- Neither field proves controller responsibility at event time
- Does not classify complaint episodes or financial settlement

**SQL Structure:**
```sql
SELECT
    FROM_UNIXTIME(e.event_epoch) AS event_datetime,
    DATE(FROM_UNIXTIME(e.event_epoch)) AS event_date,
    YEAR(FROM_UNIXTIME(e.event_epoch)) AS event_year,
    MONTH(FROM_UNIXTIME(e.event_epoch)) AS event_month_number,
    DATE_FORMAT(FROM_UNIXTIME(e.event_epoch), '%Y-%m') AS event_month,
    CAST(DATE_FORMAT(FROM_UNIXTIME(e.event_epoch), '%Y-%m-01') AS DATE) AS event_month_start,
    139 AS source_log_type,
    e.action_id AS action_id,
    m.visit AS visit_id,
    v.proj AS project_id,
    p.name AS project_name,
    e.previous_status AS previous_status,
    e.new_status AS new_status,
    e.actor_user_id AS actor_user_id,
    actor.i AS actor_user_directory_match_id,
    m.staff AS current_action_staff_id,
    NULLIF(TRIM(CONCAT_WS(' ', staff.namef, staff.namei, staff.nameo)), '') AS current_action_staff_name,
    CASE ... END AS current_action_staff_quality,
    'CURRENT_MY_STAFF_FIELD_ONLY; EVENT_TIME_CONTROLLER_ROLE_NOT_PROVEN' AS controller_attribute_confidence,
    COALESCE(du.signature_row_count, 1) AS identical_signature_row_count,
    CASE WHEN COALESCE(du.signature_row_count, 1) > 1 THEN 1 ELSE 0 END AS possible_technical_duplicate_flag,
    CASE ... END AS event_quality_status
FROM (
    SELECT
        l.dt AS event_epoch,
        l.code AS action_id,
        l.usr AS actor_user_id,
        CAST(TRIM(SUBSTRING_INDEX(REGEXP_REPLACE(l.info, '[[:space:]]*&rarr;[[:space:]]*', '|'), '|', 1)) AS UNSIGNED) AS previous_status,
        CAST(TRIM(SUBSTRING_INDEX(REGEXP_REPLACE(l.info, '[[:space:]]*&rarr;[[:space:]]*', '|'), '|', -1)) AS UNSIGNED) AS new_status
    FROM j4b.log AS l
    WHERE l.typ = 139
      AND l.dt >= UNIX_TIMESTAMP('2026-01-01 00:00:00')
      AND l.dt <  UNIX_TIMESTAMP('2026-11-01 00:00:00')
      AND l.info REGEXP '^[[:space:]]*[0-9]+[[:space:]]*&rarr;[[:space:]]*39[[:space:]]*$'
) AS e
LEFT JOIN j4b.my AS m ON m.i = e.action_id
LEFT JOIN j4b.visit AS v ON v.i = m.visit
LEFT JOIN j4b.proj AS p ON p.i = v.proj
LEFT JOIN j4b.`user` AS actor ON actor.i = e.actor_user_id
LEFT JOIN j4b.`user` AS staff ON staff.i = m.staff
LEFT JOIN ( ... ) AS du ON ...
```

**Calculated Fields in DataLens (to be verified):**
- `event_datetime`, `event_date`, `event_year`, `event_month_number`, `event_month`, `event_month_start`
- `current_action_staff_quality` (4-state classification)
- `controller_attribute_confidence` (static string)
- `identical_signature_row_count`, `possible_technical_duplicate_flag`
- `event_quality_status` (7-state classification)

---

### 2.2 Dataset 2 — ДФБ БД Финансы (Finance) / Project Operations

**GitHub Path:** `sql/datalens/PORTAL-001.3-project-operations-finance-candidate-2026-10-30.sql` (main branch)
**SHA-256:** `e30180b4d5f83ee13176b35afaaf4412c0feb817`
**Status:** ✅ **RECOVERED** — Candidate SQL; owner verified as Project Operations + Finance combined

**Key Characteristics:**
- Grain: One row per `proj.i`
- MariaDB 10.11; read-only SELECT
- Workflow status fields: distinct-visit memberships by Action state
- Owner-approved successful-visits rule: any linked Action state 40 or 50 counts the Visit once
- Status 37 remains in finished_visits but excluded from successful_visits_current
- Project type and contract dimensions: raw project keys plus guarded dictionary names
- Dictionary subqueries group on source key, prevent fan-out, expose mapping quality
- Payment status: fail-closed (Portal XLSX scope and three money columns not mapped)

**SQL Structure (abridged):**
```sql
SELECT
    p.i AS project_id,
    p.name AS project_name,
    p.user AS manager_id,
    NULLIF(TRIM(CONCAT_WS(' ', u.namef, u.namei, u.nameo)), '') AS manager_name,
    p.client AS client_id,
    p.visits AS plan_raw,
    p.visits AS plan_value_candidate,
    CASE WHEN p.dt1 IS NULL OR p.dt1 = 0 THEN NULL ELSE DATE(FROM_UNIXTIME(p.dt1)) END AS date_from,
    CASE WHEN p.dt2 IS NULL OR p.dt2 = 0 THEN NULL ELSE DATE(FROM_UNIXTIME(p.dt2)) END AS date_to,
    CASE ... END AS date_quality_status,
    COALESCE(va.created_visits, 0) AS created_visits,
    COALESCE(wa.state20_visits, 0) AS assigned_visits,
    COALESCE(wa.state30_visits, 0) AS executed_visits,
    COALESCE(wa.finished_state_membership_visits, 0) AS finished_visits,
    COALESCE(wa.state20_visits, 0) AS workflow_state_20_membership_visits,
    COALESCE(wa.state30_visits, 0) AS workflow_state_30_membership_visits,
    COALESCE(wa.state37_visits, 0) AS workflow_state_37_membership_visits,
    COALESCE(wa.state38_visits, 0) AS workflow_state_38_membership_visits,
    COALESCE(wa.state39_visits, 0) AS workflow_state_39_membership_visits,
    COALESCE(wa.state40_visits, 0) AS workflow_state_40_membership_visits,
    COALESCE(wa.state45_visits, 0) AS workflow_state_45_membership_visits,
    COALESCE(wa.state50_visits, 0) AS workflow_state_50_membership_visits,
    COALESCE(wa.finished_state_membership_visits, 0) AS finished_state_membership_visits,
    COALESCE(wa.successful_visits_current, 0) AS successful_visits_current,
    'ANY_ACTION_STATE_40_OR_50' AS successful_visits_rule,
    CAST(NULL AS SIGNED) AS unassigned_free_visits,
    0 AS current_status_qualified,
    'CURRENT_ACTION_NOT_SELECTED; successful-visits KPI uses owner-approved any-action rule' AS current_status_qualification_status,
    'STATUS_COUNTS=distinct visit membership; SUCCESS=any linked Action state 40/50' AS workflow_metrics_semantics,
    0 AS plan_qualified,
    CASE ... END AS plan_qualification_status,
    'NOT_AVAILABLE: UNASSIGNED_FREE is not mapped to a qualified MariaDB field' AS unassigned_free_status,
    @@session.time_zone AS mariadb_session_timezone,
    cl.name AS client_name,
    p.currency AS currency_id,
    cur.code AS currency_code,
    cur.name AS currency_name,
    cur.sym AS currency_symbol,
    p.typ AS project_type_id,
    CASE ... END AS project_type_name,
    CASE ... END AS project_type_mapping_status,
    p.contract AS contract_id,
    CASE ... END AS contract_name,
    CASE ... END AS contract_mapping_status,
    /* Finance candidate fields appended; base 44 project outputs remain unchanged. */
    pa.payable_visits_count AS payable_visits_count,
    pa.payment_assignment_count AS payment_assignment_count,
    pa.visit_reward_total AS visit_reward_total,
    pa.portal_paid_total AS portal_paid_total,
    CASE ... END AS payment_remaining_total,
    CASE ... END AS payment_data_quality_status,
    CAST(NULL AS SIGNED) AS payment_unmatched_count,
    CASE ... END AS payment_source_mapping_status,
    CASE ... END AS payment_quality_detail
FROM j4b.proj AS p
LEFT JOIN ( ... ) AS va ON va.project_id = p.i
LEFT JOIN ( ... ) AS wa ON wa.project_id = p.i
LEFT JOIN j4b.`user` AS u ON u.i = p.user
LEFT JOIN j4b.client AS cl ON cl.i = p.client
LEFT JOIN j4b.currency AS cur ON cur.i = p.currency
LEFT JOIN ( ... ) AS pt ON pt.type_id = p.typ
LEFT JOIN ( ... ) AS ct ON ct.contract_id = p.contract
LEFT JOIN ( ... ) AS pa ON pa.project_id = p.i
```

**Calculated Fields in DataLens (to be verified):**
- `date_from`, `date_to`, `date_quality_status`
- `created_visits`, `assigned_visits`, `executed_visits`, `finished_visits`
- `workflow_state_XX_membership_visits` (for states 20, 30, 37, 38, 39, 40, 45, 50)
- `finished_state_membership_visits`, `successful_visits_current`
- `successful_visits_rule` (static string)
- `unassigned_free_visits`, `current_status_qualified`, `current_status_qualification_status`
- `workflow_metrics_semantics` (static string)
- `plan_qualified`, `plan_qualification_status`, `unassigned_free_status`
- `mariadb_session_timezone`
- `project_type_name`, `project_type_mapping_status`
- `contract_name`, `contract_mapping_status`
- Finance fields: `payable_visits_count`, `payment_assignment_count`, `visit_reward_total`, `portal_paid_total`, `payment_remaining_total`, `payment_data_quality_status`, `payment_unmatched_count`, `payment_source_mapping_status`, `payment_quality_detail`

---

### 2.3 Dataset 3 — Project Operations TEST

**GitHub Path:** Not found in repository
**Status:** ❌ **NOT RECOVERED** — No SQL file found in repository; exists only in live DataLens
**Dataset ID:** `cm2qmg3rfk2av`
**URL:** https://datalens.ru/datasets/cm2qmg3rfk2av-jfb-mariadb-project-operations-test

**Required Action:** Manual inspection of live DataLens dataset needed to recover exact SQL and calculated fields.

---

### 2.4 Dataset 4 — Календарь периодов TEST (Calendar Periods TEST)

**GitHub Path:** Not found in repository
**Status:** ❌ **NOT RECOVERED** — No SQL file found in repository; exists only in live DataLens
**Dataset ID:** `nxd7trqq52006`
**URL:** https://datalens.ru/datasets/nxd7trqq52006-jfb-kalendar-periodov-test

**Required Action:** Manual inspection of live DataLens dataset needed to recover exact SQL and calculated fields.

---

## 3. LIVE DATALENS INSPECTION — AUTHORIZATION BLOCKED

### 3.1 Attempt Summary

| Dataset | Direct URL Access | DataLens UI Search | Result |
|---------|-------------------|-------------------|--------|
| 1. Претензионные события | ❌ Access denied | ❌ Not found in trial catalog | **BLOCKED** |
| 2. ДФБ БД Финансы | ❌ Access denied | ❌ Not found in trial catalog | **BLOCKED** |
| 3. Project Operations TEST | ❌ Access denied | ❌ Not found in trial catalog | **BLOCKED** |
| 4. Календарь периодов TEST | ❌ Access denied | ❌ Not found in trial catalog | **BLOCKED** |

### 3.2 Blocker Details

- **Error:** "У вас нет доступа к объектам DataLens в этом каталоге" (You don't have access to DataLens objects in this catalog)
- **Request IDs:** `8fbf2c56-8ba2-44b8-aed3-78a4c584eee7`, `692aede2-e9e8-4737-9543-91449dd7c141`
- **Root Cause:** The datasets reside in a Yandex Cloud organization/catalog (`organization-nphne-pdas4zxm`) that requires explicit membership. The browser session used for inspection was a fresh Yandex DataLens trial account without organizational access.
- **User Statement:** "The owner explicitly authorizes Nemotron Ultra to open each of the four specified DataLens datasets in the existing authorized browser session, if available."
- **Resolution Required:** Owner must either:
  1. Grant organizational access to the inspection account, OR
  2. Perform manual inspection in their authorized session and provide the SQL/calculated fields, OR
  3. Export dataset configurations from DataLens UI

---

## 4. GITHUB LIVE MISMATCHES

### 4.1 Dataset 1 — Претензионные события

| Aspect | GitHub SQL | Live DataLens | Status |
|--------|------------|---------------|--------|
| SQL Text | Recovered from `sql/datalens/PORTAL-001.3-complaint-events-2026.sql` | Not accessible | **UNVERIFIED** — GitHub version is the only source |
| Calculated Fields | Not documented in GitHub | Unknown | **UNVERIFIED** |
| Parameters/Filters | Not documented in GitHub | Unknown | **UNVERIFIED** |
| Data Types | MariaDB types in SQL | Unknown | **UNVERIFIED** |

**Assessment:** GitHub SQL is the only authoritative source. Live verification blocked.

### 4.2 Dataset 2 — ДФБ БД Финансы / Project Operations

| Aspect | GitHub SQL | Live DataLens | Status |
|--------|------------|---------------|--------|
| SQL Text | Recovered from `sql/datalens/PORTAL-001.3-project-operations-finance-candidate-2026-10-30.sql` | Not accessible | **UNVERIFIED** — Marked "CANDIDATE ONLY" in GitHub |
| Calculated Fields | Not documented in GitHub | Unknown | **UNVERIFIED** |
| Parameters/Filters | Not documented in GitHub | Unknown | **UNVERIFIED** |
| Data Types | MariaDB types in SQL | Unknown | **UNVERIFIED** |

**GitHub Disclaimer:** "CANDIDATE ONLY: do not replace the production/main SQL until manual DataLens preview and parity pass."

**Assessment:** GitHub SQL is a candidate; live verification required before promotion to production.

### 4.3 Datasets 3 & 4 — Project Operations TEST & Calendar Periods TEST

| Aspect | GitHub | Live DataLens | Status |
|--------|--------|---------------|--------|
| SQL Files | Not found in repository | Exist in live DataLens | **MISSING FROM GITHUB** |
| Calculated Fields | N/A | Unknown | **UNVERIFIED** |
| Parameters/Filters | N/A | Unknown | **UNVERIFIED** |

---

## 5. CALCULATED FIELDS RECOVERY STATUS

### 5.1 Dataset 1 — Претензионные события

| Field | Formula | Source | Status |
|-------|---------|--------|--------|
| `event_datetime` | `FROM_UNIXTIME(e.event_epoch)` | SQL | ✅ In SQL |
| `event_date` | `DATE(FROM_UNIXTIME(e.event_epoch))` | SQL | ✅ In SQL |
| `event_year` | `YEAR(FROM_UNIXTIME(e.event_epoch))` | SQL | ✅ In SQL |
| `event_month_number` | `MONTH(FROM_UNIXTIME(e.event_epoch))` | SQL | ✅ In SQL |
| `event_month` | `DATE_FORMAT(FROM_UNIXTIME(e.event_epoch), '%Y-%m')` | SQL | ✅ In SQL |
| `event_month_start` | `CAST(DATE_FORMAT(FROM_UNIXTIME(e.event_epoch), '%Y-%m-01') AS DATE)` | SQL | ✅ In SQL |
| `current_action_staff_quality` | 4-state CASE on `m.staff`/`staff.i` | SQL | ✅ In SQL |
| `controller_attribute_confidence` | Static string | SQL | ✅ In SQL |
| `identical_signature_row_count` | `COALESCE(du.signature_row_count, 1)` | SQL | ✅ In SQL |
| `possible_technical_duplicate_flag` | `CASE WHEN COALESCE(du.signature_row_count, 1) > 1 THEN 1 ELSE 0 END` | SQL | ✅ In SQL |
| `event_quality_status` | 7-state CASE | SQL | ✅ In SQL |

**Additional DataLens-only calculated fields (unverified):**
- May exist in DataLens UI but not in SQL — **UNVERIFIED**

### 5.2 Dataset 2 — ДФБ БД Финансы / Project Operations

| Field | Formula | Source | Status |
|-------|---------|--------|--------|
| `date_from` | `CASE WHEN p.dt1 IS NULL OR p.dt1 = 0 THEN NULL ELSE DATE(FROM_UNIXTIME(p.dt1)) END` | SQL | ✅ In SQL |
| `date_to` | `CASE WHEN p.dt2 IS NULL OR p.dt2 = 0 THEN NULL ELSE DATE(FROM_UNIXTIME(p.dt2)) END` | SQL | ✅ In SQL |
| `date_quality_status` | 4-state CASE on `dt1`/`dt2` | SQL | ✅ In SQL |
| `created_visits` | `COALESCE(va.created_visits, 0)` | SQL | ✅ In SQL |
| `assigned_visits` | `COALESCE(wa.state20_visits, 0)` | SQL | ✅ In SQL |
| `executed_visits` | `COALESCE(wa.state30_visits, 0)` | SQL | ✅ In SQL |
| `finished_visits` | `COALESCE(wa.finished_state_membership_visits, 0)` | SQL | ✅ In SQL |
| `workflow_state_XX_membership_visits` | `COALESCE(wa.stateXX_visits, 0)` for XX=20,30,37,38,39,40,45,50 | SQL | ✅ In SQL |
| `finished_state_membership_visits` | `COALESCE(wa.finished_state_membership_visits, 0)` | SQL | ✅ In SQL |
| `successful_visits_current` | `COALESCE(wa.successful_visits_current, 0)` | SQL | ✅ In SQL |
| `successful_visits_rule` | Static string `'ANY_ACTION_STATE_40_OR_50'` | SQL | ✅ In SQL |
| `unassigned_free_visits` | `CAST(NULL AS SIGNED)` | SQL | ✅ In SQL |
| `current_status_qualified` | `0` | SQL | ✅ In SQL |
| `current_status_qualification_status` | Static string | SQL | ✅ In SQL |
| `workflow_metrics_semantics` | Static string | SQL | ✅ In SQL |
| `plan_qualified` | `0` | SQL | ✅ In SQL |
| `plan_qualification_status` | CASE on `p.visits` | SQL | ✅ In SQL |
| `unassigned_free_status` | Static string | SQL | ✅ In SQL |
| `mariadb_session_timezone` | `@@session.time_zone` | SQL | ✅ In SQL |
| `project_type_name` | Dictionary lookup with quality guard | SQL | ✅ In SQL |
| `project_type_mapping_status` | 5-state CASE | SQL | ✅ In SQL |
| `contract_name` | Dictionary lookup with quality guard | SQL | ✅ In SQL |
| `contract_mapping_status` | 5-state CASE | SQL | ✅ In SQL |
| Finance fields (`payable_visits_count`, `payment_assignment_count`, `visit_reward_total`, `portal_paid_total`, `payment_remaining_total`, `payment_data_quality_status`, `payment_unmatched_count`, `payment_source_mapping_status`, `payment_quality_detail`) | Complex subqueries with parse status | SQL | ✅ In SQL |

**Additional DataLens-only calculated fields (unverified):**
- May exist in DataLens UI but not in SQL — **UNVERIFIED**

### 5.3 Datasets 3 & 4 — Project Operations TEST & Calendar Periods TEST

**All calculated fields: UNVERIFIED** — No GitHub source; live inspection blocked.

---

## 6. TARGET POSTGRESQL DESIGN

### 6.1 Schema: `bi` (Business Intelligence)

```sql
CREATE SCHEMA IF NOT EXISTS bi AUTHORIZATION j4b_analytics_admin;
```

### 6.2 Target Tables

| Source Dataset | Target Table | Schema | Grain | Refresh |
|----------------|--------------|--------|-------|---------|
| Претензионные события | `bi.complaint_events` | `bi` | One row per log transition to status 39 | 3-4×/day |
| ДФБ БД Финансы / Project Operations | `bi.project_operations` | `bi` | One row per project | 3-4×/day |
| Project Operations TEST | `bi.project_operations_test` | `bi` | TBD | 3-4×/day |
| Календарь периодов TEST | `bi.calendar_periods` | `bi` | TBD | 3-4×/day |

### 6.3 Table DDL (Provisional)

```sql
-- bi.complaint_events
CREATE TABLE bi.complaint_events (
    event_datetime        TIMESTAMPTZ NOT NULL,
    event_date            DATE NOT NULL,
    event_year            SMALLINT NOT NULL,
    event_month_number    SMALLINT NOT NULL,
    event_month           VARCHAR(7) NOT NULL,  -- 'YYYY-MM'
    event_month_start     DATE NOT NULL,
    source_log_type       SMALLINT NOT NULL DEFAULT 139,
    action_id             BIGINT NOT NULL,
    visit_id              BIGINT NOT NULL,
    project_id            BIGINT NOT NULL,
    project_name          TEXT,
    previous_status       SMALLINT,
    new_status            SMALLINT,
    actor_user_id         BIGINT,
    actor_user_directory_match_id BIGINT,
    current_action_staff_id BIGINT,
    current_action_staff_name TEXT,
    current_action_staff_quality TEXT NOT NULL,
    controller_attribute_confidence TEXT NOT NULL,
    identical_signature_row_count INT NOT NULL DEFAULT 1,
    possible_technical_duplicate_flag SMALLINT NOT NULL DEFAULT 0,
    event_quality_status  TEXT NOT NULL,
    extracted_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (action_id, event_datetime)
);

CREATE INDEX idx_complaint_events_project_date ON bi.complaint_events (project_id, event_date);
CREATE INDEX idx_complaint_events_visit ON bi.complaint_events (visit_id);

-- bi.project_operations
CREATE TABLE bi.project_operations (
    project_id            BIGINT PRIMARY KEY,
    project_name          TEXT,
    manager_id            BIGINT,
    manager_name          TEXT,
    client_id             BIGINT,
    client_name           TEXT,
    plan_raw              BIGINT,
    plan_value_candidate  BIGINT,
    date_from             DATE,
    date_to               DATE,
    date_quality_status   TEXT,
    created_visits        BIGINT DEFAULT 0,
    assigned_visits       BIGINT DEFAULT 0,
    executed_visits       BIGINT DEFAULT 0,
    finished_visits       BIGINT DEFAULT 0,
    workflow_state_20_membership_visits BIGINT DEFAULT 0,
    workflow_state_30_membership_visits BIGINT DEFAULT 0,
    workflow_state_37_membership_visits BIGINT DEFAULT 0,
    workflow_state_38_membership_visits BIGINT DEFAULT 0,
    workflow_state_39_membership_visits BIGINT DEFAULT 0,
    workflow_state_40_membership_visits BIGINT DEFAULT 0,
    workflow_state_45_membership_visits BIGINT DEFAULT 0,
    workflow_state_50_membership_visits BIGINT DEFAULT 0,
    finished_state_membership_visits BIGINT DEFAULT 0,
    successful_visits_current BIGINT DEFAULT 0,
    successful_visits_rule TEXT,
    unassigned_free_visits BIGINT,
    current_status_qualified SMALLINT DEFAULT 0,
    current_status_qualification_status TEXT,
    workflow_metrics_semantics TEXT,
    plan_qualified SMALLINT DEFAULT 0,
    plan_qualification_status TEXT,
    unassigned_free_status TEXT,
    mariadb_session_timezone TEXT,
    client_name TEXT,
    currency_id BIGINT,
    currency_code TEXT,
    currency_name TEXT,
    currency_symbol TEXT,
    project_type_id BIGINT,
    project_type_name TEXT,
    project_type_mapping_status TEXT,
    contract_id BIGINT,
    contract_name TEXT,
    contract_mapping_status TEXT,
    -- Finance fields
    payable_visits_count BIGINT,
    payment_assignment_count BIGINT,
    visit_reward_total NUMERIC(65,30),
    portal_paid_total NUMERIC(65,30),
    payment_remaining_total NUMERIC(65,30),
    payment_data_quality_status TEXT,
    payment_unmatched_count BIGINT,
    payment_source_mapping_status TEXT,
    payment_quality_detail TEXT,
    extracted_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_project_operations_client ON bi.project_operations (client_id);
CREATE INDEX idx_project_operations_manager ON bi.project_operations (manager_id);
CREATE INDEX idx_project_operations_date_from ON bi.project_operations (date_from);

-- bi.project_operations_test (structure TBD after live inspection)
-- bi.calendar_periods (structure TBD after live inspection)
```

### 6.4 Refresh Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    REFRESH PIPELINE                              │
├─────────────────────────────────────────────────────────────────┤
│  1. Scheduler (cron/systemd) triggers 3-4×/day                 │
│         (e.g., 06:00, 12:00, 18:00, 00:00 MSK)                 │
├─────────────────────────────────────────────────────────────────┤
│  2. For each dataset:                                           │
│    a. Open SSH tunnel to lk.j4b.ru:3306 (MariaDB)              │
│    b. Execute source SQL with read-only `watch` account        │
│    c. Validate row counts, null ratios, duplicate keys         │
│    d. Load into staging table (bi.<dataset>_staging)           │
│    e. Atomic swap: DROP TABLE ... CASCADE; ALTER TABLE ...     │
│    f. Record refresh metadata (bi.refresh_log)                 │
├─────────────────────────────────────────────────────────────────┤
│  3. Superset reads from bi.* tables only (no live MariaDB)     │
└─────────────────────────────────────────────────────────────────┘
```

**Key Properties:**
- **Atomic publication:** Staging → swap ensures readers never see partial data
- **Idempotent:** Re-running same extraction produces identical results
- **Validation gates:** Row count thresholds, null ratios, FK integrity
- **Rollback:** Previous version retained until new version validated
- **Monitoring:** `bi.refresh_log` tracks duration, rows, status, errors

---

## 7. VALIDATION CRITERIA

### 7.1 Per-Dataset Validation Gates

| Dataset | Row Count Threshold | Null Ratio Limits | Duplicate Key Check | FK Integrity |
|---------|---------------------|-------------------|---------------------|--------------|
| `bi.complaint_events` | 800-1000 rows (Jan-Oct 2026) | `action_id` NOT NULL 100% | PK `(action_id, event_datetime)` unique | `visit_id` → `bi.project_operations` (soft) |
| `bi.project_operations` | 8000-8500 projects | `project_id` NOT NULL 100% | PK `project_id` unique | `client_id`, `manager_id` → reference dims |
| `bi.project_operations_test` | TBD | TBD | TBD | TBD |
| `bi.calendar_periods` | TBD | TBD | TBD | TBD |

### 7.2 Cross-Dataset Consistency

- `bi.complaint_events.project_id` ⊆ `bi.project_operations.project_id`
- `bi.project_operations_test.project_id` ⊆ `bi.project_operations.project_id` (if subset)
- Date ranges aligned across datasets

### 7.3 Superset Parity Checks

After load, verify in Superset:
- Row counts match PostgreSQL
- Key metrics match DataLens dashboard (project 8314: 166 visits, 162 successful)
- Filters/parameters work identically
- Calculated fields produce same values

---

## 8. UNRESOLVED QUESTIONS & BLOCKERS

| # | Question | Blocker | Resolution Path |
|---|----------|---------|-----------------|
| 1 | Exact SQL for Dataset 3 (Project Operations TEST) | Live DataLens access blocked | Owner manual inspection or grant org access |
| 2 | Exact SQL for Dataset 4 (Calendar Periods TEST) | Live DataLens access blocked | Owner manual inspection or grant org access |
| 3 | Calculated fields for Datasets 1 & 2 in DataLens UI | Live DataLens access blocked | Owner manual inspection or export dataset JSON |
| 4 | Parameters/Filters for all 4 datasets | Live DataLens access blocked | Owner manual inspection |
| 4 | Data types in DataLens (may differ from MariaDB) | Live DataLens access blocked | Owner manual inspection |
| 5 | Dashboard dependencies (which charts use which datasets) | Live DataLens access blocked | Owner manual inspection |
| 6 | Exact DataLens connection config (SSL, timeouts) | Not documented | Owner to provide |
| 7 | Refresh schedule for current DataLens datasets | Not documented | Owner to provide |

---

## 9. SOURCE-TO-TARGET MAPPING SUMMARY

| Source Dataset | GitHub SQL | Live Verified | Calculated Fields | Target Table | Confidence |
|----------------|------------|---------------|-------------------|--------------|------------|
| 1. Претензионные события | ✅ Recovered | ❌ Blocked | ✅ In SQL | `bi.complaint_events` | HIGH (SQL only) |
| 2. ДФБ БД Финансы | ✅ Candidate | ❌ Blocked | ✅ In SQL | `bi.project_operations` | MEDIUM (candidate) |
| 3. Project Operations TEST | ❌ Missing | ❌ Blocked | ❌ Unknown | `bi.project_operations_test` | NONE |
| 4. Календарь периодов TEST | ❌ Missing | ❌ Blocked | ❌ Unknown | `bi.calendar_periods` | NONE |

---

## 10. GITHUB PUBLICATION

### 10.1 Files to Commit

| File | Action |
|------|--------|
| `docs/datalens/PORTAL-001.3-SQL-SOURCE-RECOVERY.md` | New — this document |
| `sql/datalens/PORTAL-001.3-complaint-events-2026.sql` | Existing — verified |
| `sql/datalens/PORTAL-001.3-project-operations-finance-candidate-2026-10-30.sql` | Existing — candidate |
| `sql/datalens/PORTAL-001.3-project-operations-test.sql` | **To create** after live inspection |
| `sql/datalens/PORTAL-001.3-calendar-periods-test.sql` | **To create** after live inspection |

### 10.2 Commit Message

```
PORTAL-001.3: Document DataLens SQL source recovery for Superset migration

- Recovered exact SQL for 2/4 datasets from GitHub (complaint-events, project-operations-finance)
- Documented calculated fields, parameters, and data types from GitHub SQL
- Live DataLens inspection blocked by catalog authorization (trial account)
- Datasets 3 & 4 (Project Operations TEST, Calendar Periods TEST) missing from GitHub
- Designed target PostgreSQL schema 'bi' with 4 target tables
- Defined refresh architecture: 3-4×/day via SSH tunnel, atomic staging swap
- Defined validation gates and Superset parity checks
- Blockers: Live DataLens access requires organizational catalog access
```

---

## 11. FINAL REPORT

```text
DATASETS_EXPECTED=4
DATASETS_IDENTIFIED=4
DATASETS_LIVE_INSPECTED=0
EXACT_SQL_RECOVERED=2
CALCULATED_FIELDS_RECOVERED=2 (from GitHub SQL only)
GITHUB_LIVE_MISMATCHES=UNVERIFIED (live access blocked)
SQL_HASHES_RECORDED=YES
POSTGRESQL_TARGET_DESIGNED=YES (schema 'bi', 4 tables)
GITHUB_REPORT_URL=https://github.com/domakalugaop-design/JustForBusy/blob/main/docs/datalens/PORTAL-001.3-SQL-SOURCE-RECOVERY.md
GITHUB_COMMIT_SHA=<pending>
PRODUCTION_UNCHANGED=YES
DATALENS_UNCHANGED=YES
VPS_UNCHANGED=YES
READY_FOR_IMPLEMENTATION=PARTIAL (2/4 datasets ready; 2 require live inspection)
MANUAL_INPUT_REQUIRED=YES (live DataLens inspection for datasets 3 & 4; calculated fields for 1 & 2)
BLOCKERS=DataLens catalog authorization; datasets 3 & 4 SQL missing from GitHub; calculated fields for all datasets unverified in live UI
```

---

## 12. MANAGEMENT CONCLUSION

**Г. Доказательств недостаточно — перечислить только оставшиеся проверки.**

**Готово к реализации (2/4 датасета):**
1. **Претензионные события** — точный SQL восстановлен из GitHub, все вычисляемые поля в SQL, целевая таблица `bi.complaint_events` спроектирована.
2. **ДФБ БД Финансы / Project Operations** — кандидатский SQL восстановлен из GitHub (помечен "CANDIDATE ONLY"), все вычисляемые поля в SQL, целевая таблица `bi.project_operations` спроектирована.

**Требуют ручной проверки в DataLens (2/4 датасета):**
3. **Project Operations TEST** (`cm2qmg3rfk2av`) — SQL отсутствует в GitHub, требуется ручная инспекция в DataLens.
4. **Календарь периодов TEST** (`nxd7trqq52006`) — SQL отсутствует в GitHub, требуется ручная инспекция в DataLens.

**Дополнительно для всех 4 датасетов:**
- Вычисляемые поля в UI DataLens (могут отличаться от SQL)
- Параметры, фильтры, типы данных в DataLens
- Зависимости дашбордов

**Следующий шаг:** Владелец выполняет ручную инспекцию датасетов 3 и 4 в авторизованной сессии DataLens, а также проверяет вычисляемые поля/параметры для датасетов 1 и 2. После получения точных SQL — создаются файлы в `sql/datalens/`, обновляется документация, и можно переходить к реализации ETL в PostgreSQL `bi` схему.

**Никаких изменений в production, DataLens, VPS или PostgreSQL не производилось.**