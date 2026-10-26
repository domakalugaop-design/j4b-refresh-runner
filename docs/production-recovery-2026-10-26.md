# J4B Production Recovery — 2026-10-26

Status: the controlled recovery refresh passed. The next scheduled refresh at 17:00 MSK had not occurred when this record was written; incident closure remains pending its read-only verification.

## Incident and fix

The 2026-10-26 10:00 MSK refresh failed during payment publication. The payment payload-size estimator omitted the JSON rows brackets, undercounting serialized size by two bytes. Rollback of projects_current was blocked because the normal shrink guard rejected removal of project ID 8406.

Development fix: bd5cd845085c9e1dba4887cb5da126a70d6d2a5f.

Only the verified five-file patch was applied to production. Patch SHA-256: c075d9b8b14b0c4017fd95c7f90c561ff8a72ecb3c6588c77b7e36bc08327162. Production commit: feba201151115a5c7f064cbf41ddf48a108a6eb7, parent cf373bb63f2ee3b1415ee2c809e4aa7abebbe7da. The production commit was preserved on branch production/recovery-2026-10-26, whose remote SHA was verified equal to the production commit. Production checkout remained clean after deployment. Focused server tests: 85 passed.

## Restore

The verified pre-incident backup SHA-256 was 25b0fa12cf1c29aa8e0f10b920089ae216cff21ceac6ebbc112439adbd800e6c. Only projects_current was restored, with one values batch request. Its readback matched baseline fingerprint 719d5d62ce934c957b686a8215973f2c6b6a301c56836f24fbda8642a402d2d3.

A private pre-restore snapshot was retained with SHA-256 a6a8012a5b98bc96ad81194fe43046d25e83870e0b99ec99144ec74df8173d79.

## One controlled refresh

The approved wrapper ran exactly once, from 2026-10-26 13:00:17 to 13:15:07 MSK. Service result was success, exit status 0. Run ID: 20261026T100031Z-b693bfb0.

- Selected/acquired: 417/417.
- Transport failures: 0; semantic failures: 0.
- Portal data-mutation POSTs: 0.
- Materialization, persistent prepublication backup, workflow publication, payment publication, readback, non-target fingerprint, and final success markers: PASS.
- Rollback: not required.
- Private prepublication backup SHA-256: 24fb7bc0ce5bfab4d5eb8392e757e7c27ca887231fef66fa58f99605f0ab63c8.

## Independent readback

Read-only Sheets readback matched the completed run’s reported data-row counts and verified contract headers, widths, and unique keys:

| Tab | Data rows | Columns | Readback checks |
|---|---:|---:|---|
| projects_current | 8,090 | 58 | Contract header; 8,090 unique project IDs |
| project_types | 422 | 3 | Contract header; unique project IDs |
| Статусы проектов | 8,090 | 25 | Contract header; 8,090 unique project IDs |
| Выплаты по визитам | 89,128 | 12 | Contract header; 89,128 unique (project_id, visit_id) keys |
| Выплаты по проектам | 2,000 | 15 | Contract header; unique project IDs |

The runner’s internal full candidate readback passed. Project 8406 was present and had the controlled run’s last_refreshed timestamp. The selected 417 rows have last_refreshed 2026-10-26 10:09:53 UTC; retained rows preserve their prior incremental timestamps. No stale value tail was observed.

The non-target report_reviewers fingerprint matched the prepublication backup: b7f040b046e10cda3c4c06310a15f3c6c53cc424974fe75db5570ace38bb18a7. The shared production lock was free after the run.

## Scheduler and closure status

At 2026-10-26 13:34:41 MSK, j4b-refresh.timer was active/enabled and scheduled for 17:00 MSK. j4b-report-correctors.timer remained active/enabled and scheduled for 18:15 MSK. No timer, collector, or service configuration was changed.

A one-time read-only follow-up was scheduled in Codex for 17:45 Europe/Moscow to inspect only the scheduled refresh. It must not trigger, retry, or dispatch any refresh. Until its actual result is observed, scheduled verification is pending and the incident is not fully closed.

Remaining risk: this record proves the recovery publication, but does not prove the future scheduled invocation will succeed. No credentials or private row contents are recorded here.
