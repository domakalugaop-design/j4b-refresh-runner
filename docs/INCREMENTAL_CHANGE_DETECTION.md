# Incremental Change Detection — Technical Specification

## Overview

This document describes the incremental change detection mechanism for the Portal SQL ETL, implemented in `src/portal_change_detector.py` and integrated via `--incremental` flag in `src/portal_mariadb_etl.py`.

**Status:** Safety hardening complete. **NOT approved for production activation** — requires explicit owner decision after review of documented limitations.

## Change Detection Sources

### 1. MariaDB Event Log (`log` table)

The `log` table (11.8M rows) is the primary change signal. Key log types mapped to ETL entities:

| Log Type | Business Event | `log.code` References | Project Path | Coverage |
|----------|----------------|----------------------|--------------|----------|
| 140 | Status change | visit + my | visit.proj / my→visit.proj | ✅ High |
| 1112 | Form submit | visit + my | visit.proj / my→visit.proj | ✅ High |
| 1113 | Form submit | visit + my | visit.proj / my→visit.proj | ✅ High |
| 21 | Comment/feedback | visit + my | visit.proj / my→visit.proj | ✅ High |
| 22 | Report review | visit + my | visit.proj / my→visit.proj | ✅ High |
| 24 | Experience/qualif | visit + my | visit.proj / my→visit.proj | ✅ High |
| 25 | Claim review | claim + proj | claim.proj / direct | ✅ High |
| 26 | Chat/message | visit + my | visit.proj / my→visit.proj | ✅ High |
| 28 | Claim-related | visit + claim + proj | multiple | ✅ High |
| 31 | Unknown | visit + my | visit.proj / my→visit.proj | ✅ High |
| 80 | Unknown | visit + my | visit.proj / my→visit.proj | ✅ High |
| 81 | Unknown | visit + my | visit.proj / my→visit.proj | ✅ High |

**Critical finding:** `log.code` space **overlaps** between `visit` and `my` tables (same ID ranges). Both paths must be joined and unioned to avoid missing changes.

**Quantified ambiguity (sample 200 codes per type):**
- 85.5% of codes in key change-detecting types are ambiguous (exist in both `visit` and `my` tables)
- Type 140: 175/200 ambiguous (87.5%)
- Type 1112: 161/200 ambiguous (80.5%)
- Type 1113: 167/200 ambiguous (83.5%)
- Type 21: 150/200 ambiguous (75%)
- Type 22: 190/200 ambiguous (95%)
- Type 24: 199/200 ambiguous (99.5%)
- Type 26: 198/200 ambiguous (99%)
- Type 31: 113/200 ambiguous (56.5%)
- Type 80: 190/200 ambiguous (95%)
- Type 81: 168/200 ambiguous (84%)

This confirms the dual-join approach is necessary and correct.

### 2. New Project Detection

Projects with `proj.dt` (creation timestamp) in the detection window are automatically included, regardless of log activity.

### 3. Auxiliary Tables (Not Currently Used)

| Table | Rows | Potential Signal | Limitation |
|-------|------|------------------|------------|
| `comment` | 380k | `theme`='my-{id}' tracks my events | Indirect via my→visit→proj |
| `uc` | 247k | User-city mapping | No project linkage |

## Detection Algorithm

```python
def detect_changed_projects(source, window_start, window_end, overlap_seconds=300):
    effective_start = max(0, window_start - overlap_seconds)
    
    # 1. New projects
    new_projects = SELECT i FROM proj WHERE dt > effective_start AND dt <= window_end
    
    # 2. Log-based changes (visit/my overlap)
    codes_vm = SELECT DISTINCT code FROM log 
        WHERE typ IN (140,1112,1113,21,22,24,26,31,80,81) 
        AND dt > effective_start AND dt <= window_end
    visit_projects = SELECT DISTINCT proj FROM visit WHERE i IN codes_vm
    my_projects = SELECT DISTINCT v.proj FROM my m JOIN visit v ON v.i=m.visit WHERE m.i IN codes_vm
    
    # 3. Log-based changes (claim types)
    codes_claim = SELECT DISTINCT code FROM log 
        WHERE typ IN (25,28) 
        AND dt > effective_start AND dt <= window_end
    claim_projects = SELECT DISTINCT proj FROM claim WHERE i IN codes_claim
    proj_projects = SELECT DISTINCT i FROM proj WHERE i IN codes_claim
    visit_from_claim = SELECT DISTINCT proj FROM visit WHERE i IN codes_claim
    
    # 4. Union all
    return (new_projects ∪ visit_projects ∪ my_projects ∪ 
            claim_projects ∪ proj_projects ∪ visit_from_claim)
```

**Safe overlap:** 300 seconds (5 min) extends window backwards to handle clock skew and transaction boundaries.

## Checkpoint Mechanism

Table: `ops.etl_checkpoint`

| Column | Type | Purpose |
|--------|------|---------|
| channel | TEXT PK | 'sql' or 'reference' |
| last_successful_run_ts | INTEGER | Unix timestamp of last SUCCEEDED run |
| last_full_refresh_ts | INTEGER | Unix timestamp of last full cohort refresh |
| cohort_hash | TEXT | Hash of current cohort definition |
| updated_at | TIMESTAMPTZ | Auto-updated |

**Update rules:**
- Updated ONLY on SUCCEEDED run
- `last_full_refresh_ts` updated only on explicit full refresh
- Not updated on failure, skipped runs, or detection errors
- **Checkpoint uses `window_end` (fixed before detection), not `now()` after processing**

## Incremental CLI

```bash
python -m src.portal_mariadb_etl \
  --config /path/to/config.ini \
  --incremental \
  --allow-legacy-plain \
  --run-live
```

**Mutually exclusive with:** `--project-id`, `--cohort-dynamic`

**Behavior:**
1. Reads checkpoint for channel 'sql'
2. Captures `window_end = now()` **before** detection
3. Detects changes since `last_successful_run_ts` with 300s overlap
4. If no changes → updates checkpoint to `window_end`, exits 0 (logs `skipped_no_changes`)
5. If changes ≤ 1000 projects → runs ETL on changed projects only
6. If changes > 1000 projects → **returns THRESHOLD_EXCEEDED error, does NOT run full cohort**
7. On detection/ETL failure → returns ERROR status, does NOT update checkpoint
8. On SUCCEEDED → updates checkpoint to `window_end`

## Fallback to Full Cohort — DISABLED

**Automatic fallback to full cohort is DISABLED.**

Previously triggered when:
- Change detection fails (exception)
- Detected project count > 1000 (configurable `MAX_INCREMENTAL_PROJECTS`)
- Log query times out or returns error

**Now:** All these conditions return explicit error status (`THRESHOLD_EXCEEDED` or `ERROR`), checkpoint is NOT updated, and NO full cohort ETL is run. Full cohort refresh requires explicit `--cohort-dynamic` invocation.

## Storage Guard Impact

Current: ~2,800 guard calls per full run (10 chunks × ~280 batches)

Incremental (typical 50-200 projects): ~60-240 guard calls (1 chunk × ~6-24 batches)

**No guard weakening** — guard still runs before every write batch.

## Limitations & Uncovered Changes

| Gap | Impact | Mitigation |
|-----|--------|------------|
| No `updated_at` on core tables | Cannot detect direct SQL updates bypassing app | Weekly full refresh |
| Log completeness unproven | Bulk/admin/backdated changes may not log | Weekly full refresh |
| `log.code` overlap visit/my | Requires dual join (implemented) | Verified in tests (85.5% ambiguity) |
| **30+ unmapped log types (4.1M events)** | **May miss changes** | **Weekly full refresh required** |
| Types 1,2,3,4,5,8,15,73,75,94,95,99,100,101,105,110,115,120,122,125,127,130,131,132,135,137,138,139,142,145,150,190,1001 | Not mapped — may miss changes | Audit needed; weekly full refresh catches |
| Deletions | `log` may not capture hard deletes | Weekly full refresh + referential integrity checks |
| Backdated changes | `log.dt` = event time, not commit time | Overlap window (300s) helps; weekly full refresh |

**Critical finding:** 4.1M events across 30+ unmapped log types, many high-volume (types 3, 100, 101, 112, 122, 127, 130, 131, 137, 150). These may represent changes not detected by incremental mode.

## Weekly Full Refresh — REQUIRED

Scheduled separately (e.g., Sunday 03:00 MSK):
- Runs full cohort discovery + chunked ETL (`--cohort-dynamic`)
- Updates `last_full_refresh_ts`
- Validates incremental detection completeness
- Catches any missed changes from unmapped log types

**Weekly full refresh is NOT optional** — it is the safety net for unmapped log types and log completeness gaps.

## Testing

Run tests:
```bash
python3 -m pytest tests/test_portal_change_detector.py -v
```

Test coverage (27 tests):
- ✅ No changes detected
- ✅ One changed project
- ✅ Multiple changes to same project (deduplication)
- ✅ Overlapping IDs across visit/my entities
- ✅ Late event in overlap window
- ✅ Log read error handling
- ✅ Threshold exceeded (no fallback)
- ✅ ETL interruption (checkpoint not advanced)
- ✅ Retry after interruption
- ✅ Deletion/cancellation events
- ✅ No checkpoint (first run)
- ✅ Overlapping window reprocessing
- ✅ Checkpoint semantics (window_end fixed before detection)
- ✅ Window end fixed before detection
- ✅ Checkpoint store upsert
- ✅ Result serialization format
- ✅ Integration concepts (threshold, skip, checkpoint update)

## Deployment Checklist

- [ ] Create `ops.etl_checkpoint` table in production PostgreSQL
- [ ] Verify log type mapping with DBA
- [ ] Run incremental mode in dry-run (`--incremental` without `--run-live`)
- [ ] Monitor first 5 incremental runs for correctness
- [ ] **Schedule weekly full refresh timer (mandatory)**
- [ ] Document rollback procedure (delete checkpoint row → next run does full cohort)

## Rollback Procedure

If incremental mode causes issues:
1. Delete checkpoint row: `DELETE FROM ops.etl_checkpoint WHERE channel='sql';`
2. Next scheduled run (or manual) will fall back to full cohort
3. No data loss — idempotent upserts guarantee consistency

## Files

- `src/portal_change_detector.py` — Core detection logic
- `src/portal_mariadb_etl.py` — `--incremental` CLI integration, `run_incremental_live`
- `tests/test_portal_change_detector.py` — Unit tests (27 tests)
- `docs/INCREMENTAL_CHANGE_DETECTION.md` — This document