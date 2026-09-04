# j4b-refresh-runner

Public execution shell for a scheduled, read-only data refresh pipeline.

## Purpose

This repository is intentionally limited to **safe, generic execution mechanics**. Its purpose is to run an already-qualified refresh pipeline on GitHub-hosted runners so that daily execution does not depend on a personal computer being powered on and connected to the Internet.

The repository must not become a storage location for corporate data, credentials, internal documentation, or production artifacts.

## Intended runtime

Target execution model:

```text
GitHub Actions scheduled job
        ↓
read-only source acquisition
        ↓
materialization / validation
        ↓
Google Sheets publication
        ↓
post-publication validation
```

The design principle is deliberately simple: **one runner, one process, one end-to-end job**. Do not reproduce Google Apps Script continuation-trigger orchestration unless a concrete runtime constraint makes it unavoidable.

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
- internal URLs or identifiers when disclosure is not necessary;
- production spreadsheet IDs or other private resource identifiers;
- logs containing secrets or business payloads.

Runtime-specific values are supplied through GitHub Actions Secrets/Variables or another protected runtime mechanism.

## Configuration contract

Application code should consume configuration from environment variables. Exact variables will be finalized during migration. The public `.env.example` contains placeholders only.

## Production safety

Until an explicit cutover decision is made, the existing qualified production writer remains authoritative. This repository must not be enabled as a recurring production writer in parallel with another production writer.

Migration sequence:

1. Port the already-working local pipeline without redesigning business logic.
2. Run isolated test-target qualification in GitHub Actions.
3. Verify data correctness, types, id-set preservation, and publication semantics.
4. Measure end-to-end runtime on the hosted runner.
5. Only after explicit approval, perform a separate production cutover.

## Status

Repository initialized as a safe public runner shell. Production pipeline code has **not** yet been migrated and no scheduled production workflow is enabled.
