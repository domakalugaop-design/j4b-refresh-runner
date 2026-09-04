# Source inventory for local production migration

Source repository: `domakalugaop-design/JustForBusy`

Source branch: `agent/local-production-refresh`

Source commit observed during migration: `e3b6bed5328f1c179eeca74f04f3a8f8a618f37e`

## Public-safe mechanics reused or reimplemented

- `tools/portal/production_refresh.py` — authoritative synchronous local production flow and business/publication contracts.
- `tools/portal/curl_transport.py` — qualified HTTP/1.1 curl transport pattern; macOS Keychain and absolute curl path were removed.
- `tools/portal/operational_mvp_acquisition.py` — canonical three-request project acquisition contract.
- `tools/portal/sample_project_enrichment.py` — project edit-field parsing contract.
- `tools/portal/visit_control_probe.py` — visit/action parsing logic required by the canonical acquisition path.
- `tools/portal/test_production_refresh.py` — regression contracts used to seed portable tests.

## Not imported

The following source classes are intentionally not copied into this public repository:

- launchd plist and shell wrapper;
- macOS notification code;
- Keychain access code;
- local absolute filesystem paths;
- captured JSON outputs;
- runtime artifacts and backups;
- historical diagnostic collectors not required by the production refresh;
- Apps Script orchestration/state-machine code.

## Adaptations for GitHub-hosted Linux

- Secrets are read from environment variables supplied by GitHub Actions Secrets.
- `curl` is resolved from `PATH` rather than `/usr/bin/curl`.
- Google OAuth refresh credentials are environment variables; no local OAuth client file or Keychain lookup is used.
- The initial workflow is `workflow_dispatch` only and hard-locked to `RUN_MODE=test`.
- Production mode is rejected by code while this branch is under qualification.

## Current limitation

The migrated runner is prepared for isolated TEST qualification, but has not yet been live-qualified on GitHub Actions because repository Secrets must be configured first. The existing local production writer remains authoritative until an explicit cutover.
