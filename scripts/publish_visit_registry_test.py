"""Controlled TEST publication of a visit-registry snapshot.

Safety gates:
- destination spreadsheet id is allowlisted to the TEST workbook;
- requires an explicit ``--approve-test-write`` flag;
- append-only tabs (``visits_registry_history``, ``visits_registry_manifest``,
  ``data_quality_status``, ``metric_dictionary``) only ever gain rows;
- ``visits_current`` is a current-state view: it may be replaced only by a
  snapshot whose manifest declares complete collection, and never silently
  (a conflicting incomplete snapshot aborts without writing);
- idempotent: republishing the same snapshot_id is a no-op when the stored
  fingerprint matches, and a hard refusal when it does not;
- never touches the production source spreadsheet or DataLens.

The publication plan is built by a pure function so tests can exercise every
gate against an in-memory fake gateway without network access.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.controller_quality_sheets import GoogleSheetsGateway, MartError  # noqa: E402
from src.google_service_account import google_token  # noqa: E402
from src.visit_registry import (  # noqa: E402
    MANIFEST_FIELDS,
    PARSE_FIELDS,
    VISIT_GRAIN_FIELDS,
    snapshot_fingerprint,
)

TEST_SPREADSHEET_ID = "1G_pXXNCLTwABSnSPtXxRCxF19VOCOvXi-DK21ZMzytM"

HISTORY_TAB = "visits_registry_history"
MANIFEST_TAB = "visits_registry_manifest"
CURRENT_TAB = "visits_current"
WORKLOAD_TAB = "controller_workload"
SUMMARY_TAB = "project_summary"
PERIOD_TAB = "visits_by_period"
QUALITY_TAB = "data_quality_status"
METRICS_TAB = "metric_dictionary"

HISTORY_HEADER = ("snapshot_id", "source_generation", "source_identity") + PARSE_FIELDS
MANIFEST_HEADER = MANIFEST_FIELDS
CURRENT_HEADER = (
    "snapshot_id", "captured_at_utc", "project_id", "visit_id", "action_ids",
    "workflow_status_code", "workflow_status_codes", "visit_datetime",
    "report_datetime", "controller_fio", "controller_fio_all", "visitor_user_id",
    "action_row_count", "has_unassigned_row", "source_quality", "source_url",
    "quality_flags",
)
WORKLOAD_HEADER = (
    "snapshot_id", "captured_at_utc", "project_id", "controller_fio",
    "visits_assigned", "action_rows_handled",
)
SUMMARY_HEADER = (
    "snapshot_id", "captured_at_utc", "project_id", "project_name_short",
    "visits_total", "visits_assigned", "visits_unassigned_only",
    "visits_with_claim_code", "visits_with_report_datetime",
    "visits_without_visit_datetime", "action_rows", "controllers_distinct",
)
PERIOD_HEADER = (
    "snapshot_id", "captured_at_utc", "project_id", "period", "unique_visits",
    "visits_assigned", "visits_unassigned_only", "date_semantics",
    "data_completeness", "quality_flags",
)
QUALITY_HEADER = (
    "publication_key", "schema_version", "active_generation", "refresh_status",
    "last_successful_refresh_at", "source_snapshot_at", "source_row_count",
    "published_report_count", "published_visit_count", "unmatched_records",
    "ambiguous_versions", "unknown_status_count", "coverage_status",
    "source_freshness", "status_project_count",
)
METRICS_HEADER = (
    "publication_key", "generation_id", "metric_key", "display_name",
    "definition", "source_fields", "availability", "limitation",
)

_METRIC_ROWS = (
    (
        "unique_visits", "Уникальные визиты",
        "Количество различных visit_id в проекте за наблюдение; action-строки "
        "одного визита дедуплицируются.",
        "visits_current", "AVAILABLE_IN_OBSERVED_SNAPSHOT",
        "TEST-охват: один проект; visit_id проверен в пределах проекта.",
    ),
    (
        "visits_by_period", "Визиты по периодам",
        "Уникальные визиты, сгруппированные по месяцу visit_datetime.",
        "visits_by_period", "AVAILABLE_WITH_DATE_CAVEATS",
        "visit_datetime может быть планируемой датой (включая будущие); "
        "часовой пояс не подтверждён.",
    ),
    (
        "controller_workload_visits", "Визиты у контролёра",
        "Число визитов с привязкой controller_fio в наблюдении.",
        "controller_workload", "AVAILABLE_IN_OBSERVED_SNAPSHOT",
        "Идентичность по ФИО, не по проверенному controller_id.",
    ),
    (
        "controller_action_rows", "Action-строки контролёра",
        "Число action-строк с данным controller_fio (без дедупликации).",
        "controller_workload", "AVAILABLE_IN_OBSERVED_SNAPSHOT",
        "Одному визиту может соответствовать несколько action-строк.",
    ),
    (
        "claims_current", "Текущие претензии (39)",
        "Визиты с текущим кодом состояния 39 «Есть претензия».",
        "visits_snapshot_history", "AVAILABLE_IN_OBSERVED_SNAPSHOT",
        "Только текущее состояние; время возникновения претензии UNKNOWN; "
        "претензия не является оценкой работы контролёра.",
    ),
    (
        "claim_rate", "Доля претензий",
        "Отношение претензий к визитам за период.",
        "claims_trend", "BLOCKED_COHORT_UNVERIFIED",
        "Числитель и знаменатель не имеют подтверждённого общего определения "
        "даты; расчёт заблокирован до верификации когорты.",
    ),
)


def _cell(value: Any) -> Any:
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, list):
        return "|".join(str(item) for item in value)
    return value


def _history_row(fact: dict[str, Any]) -> list[Any]:
    record = {name: _cell(fact.get(name, "")) for name in HISTORY_HEADER}
    return [record[name] for name in HISTORY_HEADER]


def _fact_from_history_row(row: list[Any]) -> dict[str, Any]:
    fact: dict[str, Any] = {}
    # Sheets API trims trailing empty cells; pad so every header field maps.
    padded = list(row) + [""] * (len(HISTORY_HEADER) - len(row))
    for name, value in zip(HISTORY_HEADER, padded):
        if name == "quality_flags":
            fact[name] = [part for part in str(value).split("|") if part] if value else []
        elif name == "workflow_state_codes":
            fact[name] = [part for part in str(value).split("|") if part] if value else []
        elif name == "participant_assigned":
            fact[name] = str(value) in ("True", "true")
        else:
            fact[name] = value
    return fact


def _visit_row(visit: dict[str, Any], snapshot_id: str) -> list[Any]:
    record = {
        "snapshot_id": snapshot_id,
        "captured_at_utc": visit["captured_at_utc"],
        "project_id": visit["project_id"],
        "visit_id": visit["visit_id"],
        "action_ids": visit["action_ids"],
        "workflow_status_code": visit["workflow_status_code"],
        "workflow_status_codes": visit["workflow_status_codes"],
        "visit_datetime": visit["visit_datetime"],
        "report_datetime": visit["report_datetime"],
        "controller_fio": visit["controller_fio"],
        "controller_fio_all": visit["controller_fio_all"],
        "visitor_user_id": visit["visitor_user_id"],
        "action_row_count": visit["action_row_count"],
        "has_unassigned_row": _cell(visit["has_unassigned_row"]),
        "source_quality": "VISIT_REGISTRY_SNAPSHOT",
        "source_url": visit["source_url"],
        "quality_flags": _cell(visit["quality_flags"]),
    }
    return [record[name] for name in CURRENT_HEADER]


def build_publication_plan(
    payload: dict[str, Any], existing: dict[str, list[list[Any]]], published_at_utc: str
) -> dict[str, Any]:
    """Pure publication planner.  ``existing`` maps tab title to its rows."""
    manifest = payload["manifest"]
    snapshot_id = manifest["snapshot_id"]
    fingerprint = payload["fingerprint"]
    facts = payload["facts"]

    def rows_of(title: str) -> list[list[Any]]:
        return existing.get(title, [])

    history_rows = rows_of(HISTORY_TAB)
    prior = [
        row for row in history_rows[1:]
        if "snapshot_id" in HISTORY_HEADER and row[HISTORY_HEADER.index("snapshot_id")] == snapshot_id
    ]
    resume = False
    if prior:
        prior_fingerprint = snapshot_fingerprint([_fact_from_history_row(row) for row in prior])
        if prior_fingerprint != fingerprint:
            return {
                "status": "REFUSED_SNAPSHOT_ID_CONFLICT",
                "snapshot_id": snapshot_id,
                "ops": [],
            }
        # Snapshot already registered: resume any partially published
        # derived views instead of duplicating history/manifest rows.
        resume = True

    completeness = str(manifest.get("collection_completeness", ""))
    current_rows = rows_of(CURRENT_TAB)
    current_data = current_rows[1:]
    if current_data:
        current_header = current_rows[0]
        if "snapshot_id" in current_header:
            idx = current_header.index("snapshot_id")
            prior_current_ids = {str(row[idx]) for row in current_data if idx < len(row)}
        else:
            prior_current_ids = {"<unknown>"}
        if prior_current_ids != {snapshot_id}:
            if prior_current_ids - {snapshot_id} and not completeness.startswith("COMPLETE"):
                return {
                    "status": "REFUSED_CURRENT_STATE_REPLACE_INCOMPLETE",
                    "snapshot_id": snapshot_id,
                    "ops": [],
                }

    ops: list[dict[str, Any]] = []

    # Header preparation: create missing tabs, verify existing headers, and
    # never touch data rows.  Only the header-only current-state tab may have
    # its schema upgraded in place (no data rows to lose).
    workload_header = ("snapshot_id", "captured_at_utc") + WORKLOAD_HEADER[2:]
    header_plan: dict[str, list[str]] = {
        HISTORY_TAB: list(HISTORY_HEADER),
        MANIFEST_TAB: list(MANIFEST_HEADER),
        CURRENT_TAB: list(CURRENT_HEADER),
        WORKLOAD_TAB: list(workload_header),
        SUMMARY_TAB: list(SUMMARY_HEADER),
        PERIOD_TAB: list(PERIOD_HEADER),
        QUALITY_TAB: list(QUALITY_HEADER),
        METRICS_TAB: list(METRICS_HEADER),
    }
    for title, header in header_plan.items():
        rows = rows_of(title)
        if not rows:
            if title not in existing:
                ops.append({"op": "add_sheet", "title": title})
            ops.append({"op": "ensure_headers", "title": title, "header": header})
            continue
        current_header = [str(cell) for cell in rows[0]]
        if current_header == [str(cell) for cell in header]:
            continue
        if title == CURRENT_TAB:
            # Header-only tab: upgrade the schema in place.  Tab with data:
            # the replace gate above already sanctioned clearing every data
            # row, so upgrading the header alongside the replace is safe.
            ops.append({
                "op": "write_rows", "title": title, "start_row": 1,
                "rows": [list(header)],
            })
            continue
        return {
            "status": "REFUSED_HEADER_MISMATCH",
            "snapshot_id": snapshot_id,
            "tab": title,
            "ops": [],
        }

    if not resume:
        ops.append({
            "op": "write_rows", "title": HISTORY_TAB,
            "start_row": max(len(history_rows), 1) + 1,
            "rows": [_history_row(fact) for fact in facts],
        })
        ops.append({
            "op": "write_rows", "title": MANIFEST_TAB,
            "start_row": max(len(rows_of(MANIFEST_TAB)), 1) + 1,
            "rows": [[_cell(manifest.get(name, "")) for name in MANIFEST_HEADER]],
        })

    # Current-state view: replace data rows with this snapshot's visit grain.
    # A rerun whose current rows are already this snapshot's grain is a no-op.
    # The row count must match too: a write interrupted after a partial
    # values.update would leave fewer rows, all correctly tagged, and must
    # not be mistaken for a complete current view.
    current_header = current_rows[0] if current_rows else []
    current_is_ours = bool(current_data) and "snapshot_id" in current_header and len(
        current_data
    ) == len(payload["visit_grain"]) and all(
        current_header.index("snapshot_id") < len(row)
        and str(row[current_header.index("snapshot_id")]) == snapshot_id
        for row in current_data
    )
    if not current_is_ours:
        if current_data:
            ops.append({
                "op": "clear_rows", "title": CURRENT_TAB,
                "start_row": 2, "count": len(current_data),
            })
        ops.append({
            "op": "write_rows", "title": CURRENT_TAB, "start_row": 2,
            "rows": [_visit_row(visit, snapshot_id) for visit in payload["visit_grain"]],
        })

    def _existing_keys(title: str, key_columns: tuple[str, ...]) -> set[tuple[str, ...]]:
        """Natural keys of rows in ``title`` already tagged with this snapshot."""
        rows = rows_of(title)
        if not rows:
            return set()
        header = [str(cell) for cell in rows[0]]
        if "snapshot_id" not in header or any(col not in header for col in key_columns):
            return set()
        sid = header.index("snapshot_id")
        idxs = [header.index(col) for col in key_columns]
        return {
            tuple(str(row[i]) for i in idxs) for row in rows[1:]
            if sid < len(row) and str(row[sid]) == snapshot_id
        }

    workload_existing = _existing_keys(WORKLOAD_TAB, ("project_id", "controller_fio"))
    workload_new = [
        [snapshot_id, manifest["captured_at_utc"], row["project_id"], row["controller_fio"],
         row["visits_assigned"], row["action_rows_handled"]]
        for row in payload["controller_workload"]
        if (row["project_id"], row["controller_fio"]) not in workload_existing
    ]
    if workload_new:
        ops.append({
            "op": "write_rows", "title": WORKLOAD_TAB,
            "start_row": max(len(rows_of(WORKLOAD_TAB)), 1) + 1,
            "rows": workload_new,
        })

    summary_existing = _existing_keys(SUMMARY_TAB, ("project_id",))
    summary_new = [
        [snapshot_id, manifest["captured_at_utc"]] + [
            row[name] for name in SUMMARY_HEADER[2:]
        ]
        for row in payload["project_summary"]
        if (row["project_id"],) not in summary_existing
    ]
    if summary_new:
        ops.append({
            "op": "write_rows", "title": SUMMARY_TAB,
            "start_row": max(len(rows_of(SUMMARY_TAB)), 1) + 1,
            "rows": summary_new,
        })

    period_existing = _existing_keys(PERIOD_TAB, ("project_id", "period"))
    period_new = [
        [snapshot_id, manifest["captured_at_utc"], row["project_id"], row["period"],
         row["unique_visits"], row["visits_assigned"], row["visits_unassigned_only"],
         manifest["date_semantics"],
         "SINGLE_SNAPSHOT_PERIODS" if row["period"] else "VISITS_WITHOUT_DATETIME",
         ""]
        for row in payload["visits_by_period"]
        if (row["project_id"], row["period"]) not in period_existing
    ]
    if period_new:
        ops.append({
            "op": "write_rows", "title": PERIOD_TAB,
            "start_row": max(len(rows_of(PERIOD_TAB)), 1) + 1,
            "rows": period_new,
        })

    quality_rows = rows_of(QUALITY_TAB)
    unknown_status_count = sum(
        1 for fact in facts if "STATUS_UNRESOLVED" in fact.get("quality_flags", [])
    )
    quality_already = any(
        len(row) > 2 and str(row[2]) == snapshot_id for row in quality_rows[1:]
    )
    if not quality_already:
        project_count = len({str(fact["project_id"]) for fact in facts})
        ops.append({
            "op": "write_rows", "title": QUALITY_TAB,
            "start_row": max(len(quality_rows), 1) + 1,
            "rows": [[
                "visit_registry", "1", snapshot_id, "PUBLISHED_TEST_SNAPSHOT",
                published_at_utc, manifest["captured_at_utc"], str(manifest["response_rows"]),
                "", str(manifest["distinct_visits"]), "0", "", str(unknown_status_count),
                manifest["coverage_scope"], "CAPTURED_" + str(manifest["captured_at_utc"])[:10],
                str(project_count),
            ]],
        })

    metrics_rows = rows_of(METRICS_TAB)
    metrics_already = any(
        len(row) > 1 and str(row[1]) == snapshot_id for row in metrics_rows[1:]
    )
    if not metrics_already:
        ops.append({
            "op": "write_rows", "title": METRICS_TAB,
            "start_row": max(len(metrics_rows), 1) + 1,
            "rows": [
                ["visit_registry", snapshot_id, key, name, definition, source, availability, limitation]
                for key, name, definition, source, availability, limitation in _METRIC_ROWS
            ],
        })

    if not ops:
        return {"status": "ALREADY_PUBLISHED", "snapshot_id": snapshot_id, "ops": []}
    return {"status": "PUBLISH", "snapshot_id": snapshot_id, "ops": ops}


def execute_plan(gateway: Any, ops: list[dict[str, Any]]) -> None:
    for op in ops:
        if op["op"] == "add_sheet":
            gateway.add_sheet(op["title"])
        elif op["op"] == "ensure_headers":
            gateway.ensure_headers(op["title"], op["header"])
        elif op["op"] == "write_rows":
            gateway.update(op["title"], f"A{op['start_row']}", op["rows"])
        elif op["op"] == "clear_rows":
            gateway.update(
                op["title"], f"A{op['start_row']}",
                [[""] * 17 for _ in range(op["count"])],
            )
        else:
            raise MartError(f"unknown publication op: {op['op']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot_json")
    parser.add_argument("--approve-test-write", action="store_true", dest="approve")
    args = parser.parse_args(argv)
    if not args.approve:
        print(json.dumps({"status": "REFUSED_WRITE_NOT_APPROVED"}))
        return 2
    payload = json.loads(Path(args.snapshot_json).read_text(encoding="utf-8"))
    if payload.get("status") != "OK":
        print(json.dumps({"status": "REFUSED_SNAPSHOT_NOT_OK"}))
        return 2

    token = google_token()
    gateway = GoogleSheetsGateway(TEST_SPREADSHEET_ID, token)
    titles = gateway.titles()
    existing = {title: gateway.read(title) for title in titles}
    published_at_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    plan = build_publication_plan(payload, existing, published_at_utc)
    if plan["status"] != "PUBLISH":
        print(json.dumps(plan, ensure_ascii=False))
        return 0 if plan["status"] == "ALREADY_PUBLISHED" else 2
    execute_plan(gateway, plan["ops"])
    print(json.dumps({
        "status": "PUBLISHED",
        "snapshot_id": plan["snapshot_id"],
        "ops": len(plan["ops"]),
        "facts": len(payload["facts"]),
        "visits": len(payload["visit_grain"]),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except MartError as error:
        print(json.dumps({"status": "REFUSED", "reason": str(error)}))
        raise SystemExit(2)
