# j4b-refresh-runner

Public execution shell for a scheduled, read-only data refresh pipeline.

## Purpose

This repository is intentionally limited to **safe, generic execution mechanics**. Its purpose is to run an already-qualified refresh pipeline on GitHub-hosted runners so that daily execution does not depend on a personal computer being powered on and connected to the Internet.

The repository must not become a storage location for corporate data, credentials, internal documentation, or production artifacts.

## Runtime model

```text
GitHub Actions job
        ↓
read-only source acquisition
        ↓
materialization / validation
        ↓
Google Sheets publication
        ↓
post-publication validation
```

The design principle is deliberately simple: **one runner, one process, one end-to-end job**. Google Apps Script continuation/state orchestration is intentionally not reproduced.

## Security boundary

Public repository content may include:

- generic Python/runtime code;
- tests;
- workflow definitions;
- configuration schemas;
- environment-variable names;
- non-sensitive examples and fixtures;
- operational documentation that does not expose the private system.

Public repository content must not include:

- access tokens, passwords, cookies, API keys, OAuth refresh tokens, service-account keys;
- real customer, employee, project, visit, financial, or operational records;
- production exports, backups, cached responses, request/response dumps;
- production spreadsheet IDs or other private resource identifiers;
- logs containing secrets or business payloads.

Runtime-specific values are supplied through GitHub Actions Secrets/Variables.

## Current qualification branch

The portable migration lives on `migration/local-prod-port` until it passes isolated TEST qualification.

Current portable components:

- env-based Portal authentication and curl transport;
- canonical project universe discovery;
- canonical three-request project acquisition;
- project/action parsers required by production refresh;
- synchronous materialization and Google Sheets publication path;
- fail-closed TEST-only runtime guard;
- unit/portability tests;
- manual `workflow_dispatch` TEST workflow.

The source inventory and sanitization boundary are documented in `docs/SOURCE_INVENTORY.md`.

## Required GitHub Actions Secrets for TEST

- `PORTAL_BASE_URL`
- `PORTAL_LOGIN`
- `PORTAL_PASSWORD`
- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- `GOOGLE_REFRESH_TOKEN`
- `TEST_GOOGLE_SPREADSHEET_ID`

No secret value belongs in the repository.

## Production safety

The current workflow is intentionally **manual TEST only**. Code rejects `RUN_MODE != test` and rejects `ALLOW_PRODUCTION_WRITE=true` during qualification.

This code-only cutover does not change a workflow or scheduler and does not
enable a second recurring production writer.

## Production Project Type contract

The production materialized schema is 33 columns. The original 31 operational
columns remain in A:AE, followed by `project_type_code` and
`project_type_name` in AF:AG. The runner reads and maintains immutable qualified
assignments in the persisted `project_types` tab; it does not issue a type
detail request for an already assigned project. New or pending projects are
eligible only when a valid MMYY marker in `project_name`, normalized to
`(YYYY, MM)`, is September 2026 or later. A raw Portal type value of zero stays
pending and may be retried. The code-to-name mapping is validated against the
qualified dictionary in `src/project_types.py`.

Publication is guarded: source acquisition, state validation, candidate
validation, and diff planning happen before the production write authorization
check. Project Type state and `projects_current` publication have rollback and
readback safeguards, while semantic operational acquisition failures retain
last-good materialized values.

Migration sequence:

1. Port the already-working local pipeline without redesigning business logic.
2. Run isolated TEST qualification in GitHub Actions.
3. Verify data correctness, types, ID-set preservation, and publication semantics.
4. Measure end-to-end runtime on the hosted runner.
5. Qualify one unattended scheduled TEST run.
6. Only after explicit approval, perform a separate production cutover.

## Status

The production-cutover runner contains the 33-column Project Type contract.
This code change does not execute or push a live refresh and does not enable a
scheduled production workflow.
