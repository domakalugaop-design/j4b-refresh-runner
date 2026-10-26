# Action/report versioning findings

**PORTAL-001.3 — read-only investigation, 2026-10-26**

**Conclusion:** multiple action/report records for one visit are confirmed; a universal “current version” selection rule is not.

## Confirmed sample behaviour

Two bounded portal samples showed more than one `action_id`/`report_id` for the same `visit_id`:

1. In the project containing the reproduced status39 report, one visit had an older report/action with code 15, `report_date=2026-10-08`, and no selected corrector; a distinct report/action on the same visit had code 39, `report_date=2026-10-17`, and a selected corrector. Each detail page linked its own report ID, and for each record `report_id == action_id`.
2. A second visit showed an older code15 report/action dated 2026-09-24 and a separate code50 report/action dated 2026-10-01. The later report had a selected corrector; the older one did not.

The project-level report listing for the first sample contained five report IDs, included the newer code39 report for that visit, and did not include the older code15 report. The individual `/action/{id}` detail route still resolved the older report separately. This is evidence of what that specific listing displayed, not a global current-version contract.

## What the evidence does and does not establish

| Question | Finding |
|---|---|
| Can one visit have several action/report IDs? | **CONFIRMED** in bounded live samples. `visit_id` is not a unique report/action key. |
| Is `action_id == report_id`? | **CONFIRMED per sampled detail records and 722/722 current `report_reviewers` rows**, not a universal assumption; validate the detail link for each source record. |
| Does a rejected action automatically create a new action ID? | **UNKNOWN.** A code15 record predates a separate action/report record, but no parent/replace event or causal link was found. |
| Are the records versions of one questionnaire or separate submissions? | **UNKNOWN.** Shared visit and separate report/action IDs are observed; a revision/attempt relation is not explicitly modeled in the inspected pages or code. |
| Which version is current globally? | **UNKNOWN.** The project report list surfaced the later report in one sample, but no explicit `is_current`, `supersedes_id`, archived flag or selection rule was found. Do not select by max ID or max date by assumption. |
| Can two versions have different correctors? | **PARTIAL.** In the two samples, one older version had no selected corrector and the later version had one. Two different non-empty controller IDs for versions of the same visit were not demonstrated. |
| Is status history preserved? | **UNKNOWN/NOT FOUND.** The action rows contain status memberships but no old/new status, transition timestamp, actor or version-parent key. |

The status15 → status39 or status15 → status50 patterns are **not** proof of “returned to shopper”, recheck, or a single report transitioning. Code15 is labelled “Отклонено менеджером”; the codes belong to distinct report/action records on a shared visit.

## Join and aggregation strategy

- Keep `report_id` and `action_id` as separate columns and validate the report detail links. In the observed current corrector sheet they matched 722/722, but the visit-level action search showed additional action IDs for the same visit.
- For report-level status membership, join by the exact action/report key plus project context, not by `visit_id` alone. The `Выплаты по визитам` tab drops `action_id` and unions workflow codes across payment-linked assignments per visit; it can merge statuses from different actions.
- Preserve one source row per `report_id`/action version for drilldown. If management metrics count the business operation once per visit, aggregate with `COUNT(DISTINCT project_id, visit_id)` only after the owner confirms which report version supplies the outcome. Keep all linked report IDs available in the detail table.
- Do not publish “current report status” as a single value when one action row carries multiple workflow memberships (e.g. observed code sets 35+40 and 38+40). Model these as memberships as-of a refresh until a separate status history/semantics contract is confirmed.

## Development-only behavior

The optional `--supplement-project-report PROJECT_ID` fallback in the collector preserves every report ID returned by one explicitly supplied project listing; it does not collapse by visit or choose a latest action. It is bounded, opt-in, refuses pagination and oversized candidate sets, and is not enabled by the daily timer. This helps reconcile a known registry omission but does not establish a universal version rule or completeness guarantee.

## Decision still required

Before treating one visit as one completed report in a metric, the process owner/portal owner must confirm:

1. whether older action/report records are archived attempts, corrections, or independent submissions;
2. which record is shown to the client and which is reviewed/paid;
3. whether a rejection generates a new report/action or updates an existing record;
4. how the chosen operative report is identified in data (flag, parent/revision link, or approved timestamp rule).

Until then, no max-ID/latest-date heuristic should be implemented.
