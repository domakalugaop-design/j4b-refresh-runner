# PORTAL-001.3 — MariaDB snapshot failure recovery

Date: 2026-10-30

## Diagnosis

The failed full source snapshot stopped in `_fetch_named_rows` before any
PostgreSQL load began. The reader enforced `MAX_SOURCE_ROWS_PER_ENTITY =
50_000` independently for each query. The current project population contains
8,104 projects, split into 33 batches of at most 250 projects. One batch
(ordinal 7) contains 79,283 visit rows and 83,034 linked action rows, so both
entity queries exceeded the per-query materialization cap. The cap therefore
rejected a valid cohort batch; it did not identify an invalid source record.

An aggregate-only read established that the largest individual project has
7,480 visits and 8,488 linked actions, both below the cap. The bounded
reproduction on the affected project-ID batch raised `ProjectMetricsError`
from the cap check in `src/project_metrics.py` (`_fetch_named_rows`). The
original full-run wrapper retained only the exception class, so its traceback
did not preserve the exact source line. The reproduction confirms the failing
function and trigger; the current check is at line 157 of that module.

The observed population is 8,104 rather than the earlier 8,100 checkpoint.
The snapshot must preserve and report the current source population; it must
not truncate or silently exclude the additional four projects.

## Local fix and regression coverage

The source row cap remains 50,000. Fetching now stops at the first row above
the cap, and the population reader bisects the affected explicit ID list only
when it receives the specific `SOURCE_ENTITY_ROW_CAP` error. Child queries
share the same consistent read-only transaction. Other validation errors,
including a single project exceeding the cap, still fail closed. Project,
visit, action, and reference rows retain their source IDs and values.

Stage logs contain stage, outcome, exception class, safe error code, source
basename/line/function, entity or bounded ID-range label, processed row
counts, and duration. They do not include exception messages, SQL, parameters,
credentials, or source row contents.

Regression tests reproduce a capped multi-ID batch, assert recursive split,
single transaction/rollback, and exact project/visit/action ID preservation.
A separate test asserts that the cap error reports only its safe code and row
count.

## Deployment boundary and current status

Local full suite: **381 passed, 3 subtests passed**. Targeted diff check for
`src/project_metrics.py` and `tests/test_project_metrics.py`: passed. The
repository-wide `git diff --check` still reports pre-existing trailing
whitespace in unrelated `src/parsers.py:435`; that file was not changed here.

No full snapshot retry has been run. The last known state after the earlier
failed attempt was five empty tables in `staging_project_metrics`; this state
could not be independently rechecked in this session. The prior ETL runtime
configuration path is absent on `dfb`, no PostgreSQL client/profile is
available in the accessible local or server environment, and the existing
Docker socket is not accessible to the current SSH user. Therefore the target
schema emptiness and a writer limited to that schema cannot currently be
verified. No PostgreSQL data load or DataLens object creation was attempted.

Existing production MariaDB, PostgreSQL objects, Google Sheets, and DataLens
objects were not changed by this recovery work.
