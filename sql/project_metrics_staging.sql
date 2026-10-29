-- PORTAL-001.3 DB-backed project dashboard staging contract.
--
-- Owner-authorized controlled TEST deployment. Preflight must confirm that
-- the schema is absent before applying. This DDL and its minimal role grants
-- affect only staging_project_metrics; it does not alter existing
-- raw.*, reference.*, publish.*, or analytics objects.

CREATE SCHEMA IF NOT EXISTS staging_project_metrics;

CREATE TABLE IF NOT EXISTS staging_project_metrics.snapshot_runs (
    run_id                 uuid PRIMARY KEY,
    captured_at_utc        timestamptz NOT NULL,
    source_timezone        text NOT NULL,
    source_system          text NOT NULL CHECK (source_system = 'MariaDB j4b'),
    selection_kind         text NOT NULL,
    selected_projects      bigint NOT NULL CHECK (selected_projects >= 0),
    acquired_projects      bigint NOT NULL CHECK (acquired_projects >= 0),
    source_visits          bigint NOT NULL CHECK (source_visits >= 0),
    source_actions         bigint NOT NULL CHECK (source_actions >= 0),
    orphan_actions        bigint NOT NULL CHECK (orphan_actions >= 0),
    acquisition_complete  boolean NOT NULL,
    model_version          text NOT NULL,
    CHECK (acquired_projects <= selected_projects)
);

CREATE TABLE IF NOT EXISTS staging_project_metrics.projects (
    run_id                 uuid NOT NULL REFERENCES staging_project_metrics.snapshot_runs(run_id),
    project_id             bigint NOT NULL CHECK (project_id > 0),
    project_name           text,
    client_id              bigint,
    client_name            text,
    manager_id             bigint,
    manager_name           text,
    coordinator_ids_raw    text,
    coordinator_state      text NOT NULL CHECK (coordinator_state IN ('EMPTY', 'RESOLVED', 'UNRESOLVED')),
    plan_visits_raw        text,
    planned_visits         bigint,
    plan_extreme_gt_10000  boolean NOT NULL,
    project_type_code      integer,
    project_created_epoch  bigint,
    date_from_epoch        bigint,
    date_to_epoch          bigint,
    PRIMARY KEY (run_id, project_id)
);

CREATE TABLE IF NOT EXISTS staging_project_metrics.project_coordinators (
    run_id          uuid NOT NULL,
    project_id      bigint NOT NULL,
    coordinator_id  bigint NOT NULL CHECK (coordinator_id > 0),
    coordinator_name text,
    PRIMARY KEY (run_id, project_id, coordinator_id),
    FOREIGN KEY (run_id, project_id)
        REFERENCES staging_project_metrics.projects(run_id, project_id)
);

CREATE TABLE IF NOT EXISTS staging_project_metrics.visits (
    run_id          uuid NOT NULL,
    project_id      bigint NOT NULL,
    visit_id        bigint NOT NULL CHECK (visit_id > 0),
    visit_epoch     bigint,
    visit_start_epoch bigint,
    visit_end_epoch bigint,
    PRIMARY KEY (run_id, project_id, visit_id),
    FOREIGN KEY (run_id, project_id)
        REFERENCES staging_project_metrics.projects(run_id, project_id)
);

CREATE TABLE IF NOT EXISTS staging_project_metrics.actions (
    run_id              uuid NOT NULL,
    project_id          bigint,
    action_id           bigint NOT NULL CHECK (action_id > 0),
    visit_id            bigint NOT NULL,
    action_link_state   text NOT NULL CHECK (action_link_state IN ('LINKED', 'ORPHAN_VISIT')),
    raw_state_code      integer,
    action_epoch        bigint,
    action_visit_epoch  bigint,
    PRIMARY KEY (run_id, action_id),
    CHECK ((action_link_state = 'LINKED' AND project_id IS NOT NULL)
        OR (action_link_state = 'ORPHAN_VISIT' AND project_id IS NULL))
);

CREATE OR REPLACE VIEW staging_project_metrics.project_metrics AS
WITH visit_counts AS (
    SELECT v.run_id, v.project_id, count(DISTINCT v.visit_id)::bigint AS created_visits
    FROM staging_project_metrics.visits AS v
    GROUP BY v.run_id, v.project_id
), action_counts AS (
    SELECT a.run_id, a.project_id,
           count(DISTINCT a.visit_id) FILTER (WHERE a.raw_state_code = 20)::bigint AS assigned_visits,
           count(DISTINCT a.visit_id) FILTER (WHERE a.raw_state_code = 30)::bigint AS executed_visits_customer,
           count(DISTINCT a.visit_id) FILTER (WHERE a.raw_state_code IN (37, 40, 50))::bigint AS finished_visits,
           count(DISTINCT a.visit_id) FILTER (WHERE a.raw_state_code = 39)::bigint AS workflow_state_39_visits
    FROM staging_project_metrics.actions AS a
    GROUP BY a.run_id, a.project_id
)
SELECT p.run_id, p.project_id, p.project_name, p.client_id, p.client_name,
       p.manager_id, p.manager_name, p.planned_visits, p.plan_visits_raw,
       p.plan_extreme_gt_10000,
       coalesce(v.created_visits, 0)::bigint AS created_visits,
       coalesce(a.assigned_visits, 0)::bigint AS assigned_visits,
       coalesce(a.executed_visits_customer, 0)::bigint AS executed_visits_customer,
       coalesce(a.finished_visits, 0)::bigint AS finished_visits,
       coalesce(a.workflow_state_39_visits, 0)::bigint AS workflow_state_39_visits,
       NULL::bigint AS successful_visits_current,
       'NOT_AVAILABLE: UNASSIGNED_FREE marker is not present in the qualified MariaDB source model'
           ::text AS successful_visits_current_status,
       p.project_type_code, p.project_created_epoch, p.date_from_epoch, p.date_to_epoch,
       p.coordinator_ids_raw, p.coordinator_state
FROM staging_project_metrics.projects AS p
LEFT JOIN visit_counts AS v USING (run_id, project_id)
LEFT JOIN action_counts AS a USING (run_id, project_id);

CREATE OR REPLACE VIEW staging_project_metrics.visit_action_drilldown AS
SELECT v.run_id, v.project_id, v.visit_id, a.action_id, a.raw_state_code,
       a.action_epoch, a.action_visit_epoch, a.action_link_state,
       v.visit_epoch, v.visit_start_epoch, v.visit_end_epoch
FROM staging_project_metrics.visits AS v
LEFT JOIN staging_project_metrics.actions AS a
  ON a.run_id = v.run_id AND a.project_id = v.project_id AND a.visit_id = v.visit_id
UNION ALL
SELECT a.run_id, NULL::bigint AS project_id, a.visit_id, a.action_id, a.raw_state_code,
       a.action_epoch, a.action_visit_epoch, a.action_link_state,
       NULL::bigint AS visit_epoch, NULL::bigint AS visit_start_epoch,
       NULL::bigint AS visit_end_epoch
FROM staging_project_metrics.actions AS a
WHERE a.action_link_state = 'ORPHAN_VISIT';

CREATE OR REPLACE VIEW staging_project_metrics.data_quality_status AS
SELECT run_id, project_id, coordinator_ids_raw, coordinator_state,
       plan_visits_raw, planned_visits, plan_extreme_gt_10000,
       (coordinator_state = 'UNRESOLVED') AS coordinator_requires_review
FROM staging_project_metrics.projects;

COMMENT ON SCHEMA staging_project_metrics IS
    'Isolated PORTAL-001.3 TEST mart proposal; create only after owner approval.';
COMMENT ON COLUMN staging_project_metrics.project_metrics.finished_visits IS
    'Distinct visit IDs with source workflow state 37, 40, or 50; overlaps counted once.';
COMMENT ON COLUMN staging_project_metrics.project_metrics.workflow_state_39_visits IS
    'Separate distinct visit count for raw status 39; not included in finished_visits.';
COMMENT ON COLUMN staging_project_metrics.project_metrics.successful_visits_current IS
    'NULL until UNASSIGNED_FREE exclusion marker is available from a qualified source.';

-- Least-privilege access needed by the authorized DataLens test objects.
-- The existing read-only identity may select only the three staging views.
GRANT USAGE ON SCHEMA staging_project_metrics TO j4b_analytics_reader;
GRANT SELECT ON staging_project_metrics.project_metrics,
                staging_project_metrics.visit_action_drilldown,
                staging_project_metrics.data_quality_status
TO j4b_analytics_reader;
