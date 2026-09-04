# Migration plan: local refresh → GitHub Actions

## Objective

Move the already-qualified local refresh pipeline to a GitHub-hosted Linux runner without changing its business semantics. The migration target is operational independence from a continuously powered personal computer.

This is **not** a rewrite project.

## Architectural rule

Prefer one ordinary end-to-end process:

```text
scheduled workflow
  → acquire
  → materialize
  → validate candidate
  → backup target
  → publish
  → read back
  → validate published result
  → exit
```

Do not introduce continuation triggers, distributed cursors, multi-run state machines, or recovery orchestration unless the hosted runner actually requires them.

## Source-system contract to preserve

The existing qualified source acquisition contract must be ported, not reinvented. The source side remains read-only.

Migration must preserve the established semantics for:

- dynamic project selection rather than hardcoded historical counts;
- missing data being distinct from numeric zero;
- preservation of last-good rows after per-project acquisition failures where that is part of the existing production contract;
- duplicate-ID prevention;
- non-shrinking master-set validation;
- preservation of unselected rows;
- publication backup/readback validation;
- physical date/date-time types in the Google target;
- one uniform successful-sync timestamp for the completed publication.

Exact private endpoint paths, resource identifiers, credentials, and real datasets do not belong in this public repository documentation.

## Migration phases

### Phase 1 — import safe runtime code

Copy only the generic executable pipeline code and tests required for the refresh. Before commit, inspect every imported file for:

- embedded URLs;
- tokens/cookies/credentials;
- spreadsheet or Drive IDs;
- real project/customer/user data;
- captured responses;
- private fixture payloads;
- internal names that should not be public.

Private configuration must be replaced by environment-variable lookups.

### Phase 2 — Linux portability

Run the pipeline on `ubuntu-latest` and remove assumptions tied to macOS, Keychain, launchd, local paths, or interactive browser sessions.

No source semantics should change merely to make the runtime portable.

### Phase 3 — isolated TEST qualification

Use a non-production Google target and protected runtime secrets.

Qualify:

- source acquisition count and failures;
- duplicate/reacquisition behavior;
- materialized row count and unique-ID count;
- schema contract;
- type contract;
- backup contract;
- readback;
- semantic validation;
- master non-shrink;
- unselected-row preservation;
- end-to-end wall time.

Do not enable a recurring production schedule in this phase.

### Phase 4 — autonomous scheduled TEST

Enable a temporary schedule against the TEST target and prove at least one unattended scheduled execution. Confirm that no local computer participates in the run.

### Phase 5 — explicit cutover

Cutover is a separate management action. Before it:

1. confirm the GitHub runner is qualified;
2. confirm secrets are installed and scoped correctly;
3. confirm production target configuration;
4. ensure only one production writer will remain active;
5. retain a documented rollback path.

Only then enable the GitHub Actions production schedule and disable the former production scheduler.

## Failure policy

A failed job must terminate visibly with a non-zero exit status and enough sanitized diagnostics to identify the failing stage. Do not silently publish partial/invalid output.

A source acquisition failure must never be disguised as successful zero-valued business data.

## Logging contract

Recommended public-safe execution summary:

```text
RUN_ID=
STARTED_AT=
FINISHED_AT=
WALL_SECONDS=
SELECTED_COUNT=
SUCCESS_COUNT=
FAILED_COUNT=
HTTP_REQUEST_COUNT=
FINAL_ROWS=
FINAL_UNIQUE_IDS=
DUPLICATE_IDS=
SCHEMA_CONTRACT=
TYPE_CONTRACT=
BACKUP_CONTRACT=
READBACK=
SEMANTIC_VALIDATION=
MASTER_NON_SHRINK=
UNSELECTED_PRESERVATION=
FINAL_STATUS=
```

Do not print private payloads or secrets.

## Repository status

At repository initialization this plan is documentation only. No production credentials, private source configuration, production schedule, or business data are present here.
