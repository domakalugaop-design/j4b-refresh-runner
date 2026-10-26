# Controller quality mart — deployment and rollback plan

## Owner provisioning (manual, one time)

1. Create a new private Google spreadsheet named `J4B — Качество работы контролёров`.
2. Create the four exact tabs and header rows documented in `CONTROLLER_QUALITY_ONLINE_ARCHITECTURE.md` / `src/controller_quality_mart.py`.
3. Share the workbook with the existing J4B service account as Editor; do not share publicly. Give DataLens access via the approved Google OAuth identity.
4. Provide the new spreadsheet ID through the protected server environment file. Never place service-account JSON, Portal credentials, or tokens in that file; it contains file paths only.
5. Validate the service account can read existing source and read the pre-created target, and can update target tabs. Do not grant Drive-wide create scope.

The target workbook ID and service-account access were not provisioned or verified by this development task. Do not deploy until owner provisioning is complete.

## Prepared isolated service/timer

Files under `deployment/controller-quality/` are preparation artifacts only; they are not installed, enabled, or started. The prepared timer is 18:30 `Europe/Moscow`, after the corrector collector’s 18:15 schedule. The script independently requires a same-day successful collector systemd result and its latest private run-log summary with `queue_remaining=0`, so a timer firing after a failed/refused/incomplete collector cannot publish. It accepts a completed no-new-records run separately from failure.

The service runs as unprivileged `maxis` from an isolated checkout/venv. It uses the existing Portal/Google protected credential file paths and the existing shared lock. It does not alter, invoke, or wrap the collector or main refresh. It does not change Cloudflare, M1, existing timers, or the production checkout.

Before install: verify exact source commit/hash, private config permissions, venv dependencies, target tabs/permissions, read-only `--plan`, lock contention refusal, collector failure/no-new-records gate, and publication/readback tests. The first `--publish` must be separately authorized after those gates.

## Deployment outline (not executed)

1. Provision the workbook and protected environment file.
2. Deploy this development commit to a new server-owned checkout, not the existing production checkout.
3. Create isolated venv from pinned `requirements.txt`; run full tests.
4. Install `j4b-controller-quality.service` and `.timer` from this directory only after approval.
5. Run read-only `--plan` after a successful collector event; verify tab contracts, Portal status response contract, candidate counts and shared lock.
6. Obtain explicit one-run publication approval. Verify the generation pointer and DataLens source filter after publication.
7. Enable timer only through a separately authorized cutover.

## Rollback and last-good behavior

Before the pointer changes, all prior active rows remain untouched and visible; any failure leaves the previous manifest pointer active. Failed generations are retained but inactive. If the pointer write outcome is ambiguous, do one readback only; do not retry a write. If it is confirmed to point to the new generation, verify all target ranges; otherwise stop with state unresolved. Do not delete rows or attempt an automatic rollback write. Revert visibility by separately authorized pointer update only after reviewing the verified previous generation. This implementation does not currently automate pointer rollback.

Because DataLens can read intermediate state if its dataset omits the active-generation join/filter, dashboard configuration must be reviewed before timer activation. Append-only generations require a separately approved retention/compaction plan before the workbook approaches Google Sheets size limits.

## Current production observation (read-only)

At audit on 2026-10-26, `j4b-refresh.timer` and `j4b-report-correctors.timer` were active (10:00/17:00 and 18:15 MSK). The corrector service’s last result was success on 2026-10-25. The main refresh service failed after starting payment publication on 2026-10-26. Its logs indicated rollback and readback success for payment/status tabs; the current `projects_current` read differs from that run’s private pre-publication backup by 416 rows and one data-row count. This task did not modify or repair production. Resolve the production consistency incident and qualify the source freshness before any deployment/production publication.

## Acceptance checklist

- [ ] Owner-created separate target ID differs from source.
- [ ] Four headers and workbook permissions verified read-only.
- [ ] Same-day collector gate passes for both normal and zero-change success.
- [ ] Shared lock is the same inode/path contract used by existing jobs.
- [ ] Status request cap/pacing and fail-closed markers pass.
- [ ] All four schemas, counts, uniqueness, and source coverage validate.
- [ ] Generation ranges read back before pointer switch.
- [ ] DataLens datasets join/filter active generation.
- [ ] Old generations retained until separate retention decision.
- [ ] Explicit approval before service/timer installation, first publication, or timer enablement.
