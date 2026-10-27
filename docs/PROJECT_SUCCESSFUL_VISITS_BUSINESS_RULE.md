# PORTAL-001.3 — Successful Visit and Plan Execution Contract

**Status:** owner-approved rule; source implementation prepared on a feature branch
**Scope:** project-level source materialization; no production rollout
**Data handling:** this public-repository document contains no production project, Visit, action, customer, or employee identifiers.

## 1. Approved business rule

> Project plan execution is the number of unique Visits with a valid, currently successful outcome divided by the planned Visit count.

The numerator is based on the current resolved action state, not on a status the Visit merely reached at some earlier point.

| Current state | Workflow meaning | Current successful numerator |
|---:|---|---:|
| 37 | Анкета проверена | No: review alone is not approval |
| 40 | Анкета утверждена | Yes |
| 45 | В оплате отказано | No, even if an earlier state was 40 |
| 50 | Оплачено | Yes. This meaning is confirmed by the workflow dictionary label and the production status-sheet contract |
| Unknown / conflicting | Current state cannot be resolved safely | Never assume success; count as unresolved and leave execution ratio null |

The approved current-success set is `{40, 50}`. State 37 is not successful by itself. The status dictionary and production comparison establish state 50 as “Оплачено”, so current state 50 is included.

## 2. Current-state source contract

The Portal `/action` response may contain multiple action rows for one Visit. The source parser now retains the action-ID/state-code pairing and resolves one current state per Visit:

1. select the state belonging to the greatest action ID for that Visit;
2. the target production qualification found that action-ID order matched action creation-time order across its bounded Visit set;
3. when the highest action ID has conflicting state codes, mark current state unresolved and do not count it as successful;
4. an explicit `/action/0` `UNASSIGNED_FREE` row is non-success only when no later positive-ID state-bearing action exists; this current-success resolver is separate from the legacy assignment metric;
5. never fall back to an older successful action after a later current action is state 45 or another non-success state.

Duplicate rows for the same `(project_id, visit_id, action_id, state)` are collapsed. Distinct action memberships are retained for diagnostics, but their historical/parallel states are not summed into current completion.

If a future Portal response changes action-ID ordering or provides a better explicit current-state field, requalify this rule before changing the resolver. The live `/action` HTML was not replayed in this task; code-level fixtures and bounded SQL evidence are separate and are identified as such in the migration plan.

## 3. Metric fields and compatibility

| Field | Contract |
|---|---|
| `successful_visits_current` | Unique Visit IDs whose resolved current state is 40 or 50 |
| `current_state_unresolved_visits` | Unique Visits with no usable current state or a conflicting latest-action state |
| `historically_approved_visits` | Unique Visits with an observed state-40 or state-50 workflow membership; this is a historical diagnostic, not the current numerator |
| `finished_visits` | Legacy union of unique Visit IDs in observed states `{37,40,50}`; retained for compatibility, but not the approved execution numerator |
| `total_project_visits` | Unique Visit IDs in the project, including refused, unassigned, unresolved, and other non-success visits |
| `project_visit_count` | Existing distinct-Visit workflow count; must equal `total_project_visits` |
| `completed` | Compatibility alias to `successful_visits_current` |
| `execution_pct_successful` | `successful_visits_current / plan`; stored as a ratio (`1.0` = 100%) for the existing Sheets/DataLens percentage format; not capped at 1.0 |
| `execution_pct` | Compatibility alias to `execution_pct_successful` |
| `gap_pp` | `(execution_pct_successful - elapsed_pct) * 100`, in percentage points, only when both inputs are valid |

`historically_approved_visits` intentionally excludes state 37, while the retained legacy `finished_visits` continues to include it. The legacy action-page membership relation is not claimed to be a complete immutable event log; the historical field means “ever observed in the available membership source.”

`total_project_visits` is distinct by Visit ID. A project plan is the configured planned Visit count, not the current number of Visit rows. A project may have more actual Visits than plan.

## 4. Plan denominator and missing values

- If the plan is missing or zero, `execution_pct_successful` is null.
- If any Visit’s current state is unresolved, `execution_pct_successful` is null rather than silently treating the unknown Visit as failed or successful.
- Otherwise, divide the unique current-success count by plan.
- Values above 100% are valid; do not clamp them.
- No missing date, status, or plan is imputed.

## 5. Project health status

The code preserves the stated 10/20 percentage-point thresholds and only evaluates them when a valid current execution ratio and an existing valid `elapsed_pct` are both available:

```text
gap_pp = (execution_pct_successful - elapsed_pct) * 100
lag_pp = max(0, -gap_pp)

GREEN  if lag_pp < 10
YELLOW if 10 <= lag_pp < 20
RED    if lag_pp >= 20
```

`elapsed_pct` is not recalculated from dates in this change because the local source contract does not specify exact inclusive-date/timezone semantics. The existing value is reused only when present; otherwise `gap_pp` is null and `risk_status=NOT_QUALIFIED`. Missing-plan-with-activity remains `DATA_QUALITY`.

## 6. Validation invariants

- `0 <= successful_visits_current <= total_project_visits`.
- `0 <= historically_approved_visits <= finished_visits <= total_project_visits`.
- `total_project_visits == project_visit_count`.
- `completed == successful_visits_current` and `execution_pct == execution_pct_successful`.
- Any unresolved current state blocks a numeric execution ratio.
- With a positive plan and no unresolved current states, `execution_pct_successful == successful_visits_current / plan`.
- Workflow counters (`assigned_visits`, `executed_visits_customer`, `workflow_state_*`, `workflow_multi_match_visits`) retain their existing definitions and are not reinterpreted by this change.

## 7. Synthetic regression cases

The committed test suite uses synthetic identifiers only and covers:

- current state 40 success;
- current state 50 paid success;
- current state 37 review-only exclusion;
- current state 45 exclusion after a prior 40;
- max-action-ID selection for multiple action rows;
- ambiguous conflicting codes for the same latest action ID fail closed;
- duplicate actions and duplicate workflow memberships do not double-count a Visit;
- explicit free Visit, unknown state, missing plan, zero plan, and >100% completion;
- preserved legacy/history metric versus current-success metric;
- the fixed 10/20 percentage-point health bands.

No production identifiers or real action/log records are stored in these fixtures.
