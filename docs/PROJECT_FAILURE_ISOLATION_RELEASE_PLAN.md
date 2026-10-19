# Project-level failure isolation release plan

## Scope and release boundary

This development release combines the existing three-calendar-month selector
with per-project acquisition isolation. It does not change the five worksheet
schemas, calculation rules, payment formulas, systemd schedule, or DataLens
configuration. A release/deployment and the one controlled refresh require
separate owner approval; this document authorizes neither.

## Pre-deployment gates

1. Confirm the exact approved commit and reviewed diff against the currently
   deployed commit; verify the checkout is clean and the target branch is the
   intended development branch.
2. Run the complete test suite, compile check, and `git diff --check` on the
   exact commit. Confirm no secret/configuration files are included.
3. On DFB, read-only verify hostname, installed commit, active service/lock,
   timer enabled state and next trigger, deployment mechanism, free space, and
   that the controlled window extends through post-refresh checks and rollback.
   Do not move or disable the timer to manufacture a window.
   Verify that the already-configured workflow and payment publication paths
   are enabled for a five-tab bundle run; do not change feature flags as part
   of deployment. If either is disabled, treat five-tab consistency as
   unqualified and stop for an owner decision.
4. Immediately before a separately approved deployment, snapshot the current
   checkout and every executable/wrapper file that the installation method
   changes. Record SHA-256, owner/mode, and verify a restore into a separate
   temporary location.
5. Confirm the normal pre-publication snapshot can read and preserve all five
   target tabs: `projects_current`, `project_types`, `Статусы проектов`,
   `Выплаты по визитам`, and `Выплаты по проектам`. A fresh snapshot must be
   created for the approved run; an older backup is not a substitute.

Any failed or unverifiable gate is NO-GO, with no production mutation.

## Deployment and single-run sequence (future approval required)

1. Recheck no active refresh/lock and no timer conflict.
2. Capture and verify the fresh code snapshot and all-five-tab
   pre-publication snapshot.
3. Install only the approved commit using the already-qualified deployment
   mechanism; do not update dependencies, service units, or other DFB services.
4. Verify installed commit and hashes for changed runner files; verify config,
   secrets, permissions, timer state, and wrapper are unchanged.
5. Recheck timer/lock and execute exactly one approved controlled refresh via
   the existing wrapper. Do not retry automatically after failure.
6. Verify stage outcomes, project failure counts, all five tab contents and
   row/key reconciliation, `last_refreshed`, DataLens rendering, and timer
   state independently. A runner exit code alone is insufficient.

## Project-failure semantics

Only explicitly classified project-scoped transport, response/schema, or
semantic failures are isolated. The failed ID is omitted from every new-data
candidate for that cycle; its prior current/workflow rows, type assignment,
and payment rows remain untouched. If no prior bundle exists, no placeholder
or synthetic financial row is created. Successful peers can proceed. There is
no persistent exclusion list: the next normal cycle selects the ID again, and
a successful acquisition automatically replaces its last-good bundle.

Authentication/authorization failures, catalog or universe failures,
selection/result-set integrity failures, unknown programming errors,
publication/readback failures, and inability to verify rollback remain global
fail-closed conditions. Global publication failures invoke the existing
all-target rollback path.

Project error records use allowlisted endpoint/class/code fields and a private
append-only local journal. They must not include exception text, response
bodies, credentials, cookies, headers, personal data, or payment values.

## Rollback decision tree

- Failure before any Sheet write: stop; verify the installed code; restore the
  code snapshot only if required by the approved runbook.
- Failure during publication with verified runner rollback and verified
  equality of all five tabs to the pre-publication snapshot: restore the code
  snapshot, retain diagnostics and backups, and stop without another refresh.
- Partial publication or uncertain rollback: do not perform a blind data
  restore. Compare each affected tab with its fresh snapshot, then use only the
  existing verified restore mechanism under owner direction. Restore the code
  only after the data state is understood; do not rerun.
- Any unverifiable tab, hash, or rollback result: stop all writes and escalate.

## Acceptance

GO requires target commit/hash verification, fresh recoverable snapshots,
zero deployment/timer conflicts, all tests passing, one controlled refresh
with the expected publication stages, last-good preservation for any isolated
IDs, and independent verification of all five sheets and DataLens. The
development work in this branch has not performed these production checks.
