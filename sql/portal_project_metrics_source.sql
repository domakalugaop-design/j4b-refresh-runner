-- Prepared SELECT contract for a future, explicitly authorized read-only
-- MariaDB snapshot. Every :id placeholder must be bound by the caller; split
-- each list into batches of at most 250 IDs. Execute all queries in one
-- START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY transaction.
-- This file is documentation/source only; it has not been executed.

-- One row per selected project. Keep IDs and raw epoch/type/plan values.
SELECT p.i AS project_id, p.name AS project_name, p.client AS client_id,
       p.user AS manager_id, p.user2 AS coordinator_ids_raw,
       p.visits AS plan_visits, p.typ AS project_type_code,
       p.dt AS project_created_epoch, p.dt1 AS date_from_epoch,
       p.dt2 AS date_to_epoch
FROM proj AS p
WHERE p.i IN (:project_id_1 /*, ... bound IDs */);

-- One row per visit; do not join dimensions/actions into this result.
SELECT v.i AS visit_id, v.proj AS project_id, v.dt AS visit_epoch,
       v.dt1 AS visit_start_epoch, v.dt2 AS visit_end_epoch
FROM visit AS v
WHERE v.proj IN (:project_id_1 /*, ... bound IDs */);

-- One row per action/current state. The visit link assigns project grain;
-- source action IDs and raw status codes are retained.
SELECT m.i AS action_id, v.proj AS project_id, m.visit AS visit_id,
       m.state AS state_code, m.dt AS action_epoch,
       m.dt_visit AS action_visit_epoch
FROM `my` AS m
INNER JOIN visit AS v ON v.i = m.visit
WHERE v.proj IN (:project_id_1 /*, ... bound IDs */);

-- Reference lookup IDs are derived from the selected project result only.
SELECT c.i AS client_id, c.name AS client_name
FROM client AS c
WHERE c.i IN (:client_id_1 /*, ... bounded distinct linked IDs */);

-- Manager/coordinator labels use the employee identity table `user`.
-- Do not use user2.i as a unique identity; earlier source checks found it was
-- not unique for every coordinator association.
SELECT u.i AS user_id, u.namef AS name_first, u.namei AS name_middle,
       u.nameo AS name_last
FROM `user` AS u
WHERE u.i IN (:user_id_1 /*, ... bounded distinct linked IDs */);
