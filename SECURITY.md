# Security Policy

This is a **public** repository. Treat every committed byte as permanently public.

## Never commit

- API keys, access tokens, refresh tokens, passwords, session cookies, private keys, service-account JSON;
- `.env` files containing values;
- real production data or exports;
- backups or snapshots;
- raw HTTP request/response captures from private systems;
- customer, employee, project, financial, visit, or other operational records;
- private Google Sheet IDs, Drive IDs, internal resource IDs, or private endpoint locations unless explicitly classified as safe;
- debug logs that may contain Authorization headers, cookies, query parameters, payloads, or personal/business data.

## Secrets

Secrets must be provided at runtime through GitHub Actions Secrets or another protected secret store. Code and documentation may contain only the **names** of required variables and clearly fake placeholders.

Never print secret values to workflow logs. Avoid shell tracing (`set -x`) around authentication/configuration code.

## Public-safe logging

Logs should contain operational metadata only, such as:

- run identifier;
- stage name;
- counts;
- elapsed time;
- pass/fail validation results;
- sanitized error class/message.

Do not log business payloads by default.

## Production writes

The runner must remain read-only against the source system. Production Google Sheets publication is allowed only after explicit cutover approval and only when this runner is the sole production writer.

## Incident rule

If a secret or private payload is ever committed, rotating/revoking the exposed credential is mandatory. Removing the file from the latest commit is not sufficient because Git history may retain it.
