"""Build a visit-registry snapshot JSON from a saved per-project /action HTML.

Local, offline tool: it never contacts the Portal.  The HTML must be a
response already collected by an authorized controlled request (for example
the daily collector's per-project query or a bounded validation request).

Usage:
  python3 scripts/run_visit_registry_snapshot.py HTML_PATH \
      --project-id 8050 \
      --captured-at-utc 2026-10-26T13:57:52Z \
      [--source-url https://lk.j4b.ru/action] \
      [--source-identity "..."] [--coverage-scope "..."] \
      [--output PATH]

The output JSON contains the snapshot manifest, action-level facts, the
visit grain and derived workload/summary rows.  It contains no credentials.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.visit_registry import (  # noqa: E402
    VisitRegistryError,
    build_registry_snapshot,
    parse_action_fact_rows,
    visit_grain,
)

_ISO_RE = __import__("re").compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|\+00:00)?$")


def controller_workload(visit_rows: list[dict]) -> list[dict]:
    workload: dict[tuple[str, str], dict[str, int]] = {}
    for visit in visit_rows:
        fio = visit["controller_fio"]
        if not fio:
            continue
        key = (visit["project_id"], fio)
        bucket = workload.setdefault(key, {"visits_assigned": 0, "action_rows_handled": 0})
        bucket["visits_assigned"] += 1
        bucket["action_rows_handled"] += int(visit["action_row_count"])
    return [
        {
            "project_id": project_id,
            "controller_fio": fio,
            "visits_assigned": counts["visits_assigned"],
            "action_rows_handled": counts["action_rows_handled"],
        }
        for (project_id, fio), counts in sorted(workload.items())
    ]


def project_summary(visit_rows: list[dict], facts: list[dict]) -> list[dict]:
    by_project: dict[str, list[dict]] = {}
    for visit in visit_rows:
        by_project.setdefault(visit["project_id"], []).append(visit)
    summaries = []
    for project_id, visits in sorted(by_project.items()):
        project_facts = [fact for fact in facts if fact["project_id"] == project_id]
        claim_visits = [
            visit for visit in visits
            if "39" in (visit["workflow_status_codes"].split("|") if visit["workflow_status_codes"] else [])
        ]
        summaries.append({
            "project_id": project_id,
            "project_name_short": visits[0]["project_name_short"],
            "visits_total": len(visits),
            "visits_assigned": sum(1 for visit in visits if not visit["has_unassigned_row"] or visit["controller_fio"]),
            "visits_unassigned_only": sum(
                1 for visit in visits
                if visit["has_unassigned_row"] and not any(
                    fact["participant_assigned"] for fact in project_facts
                    if fact["visit_id"] == visit["visit_id"]
                )
            ),
            "visits_with_claim_code": len(claim_visits),
            "visits_with_report_datetime": sum(1 for visit in visits if visit["report_datetime"]),
            "visits_without_visit_datetime": sum(1 for visit in visits if not visit["visit_datetime"]),
            "action_rows": len(project_facts),
            "controllers_distinct": len({visit["controller_fio"] for visit in visits if visit["controller_fio"]}),
        })
    return summaries


def visits_by_period(visit_rows: list[dict]) -> list[dict]:
    periods: dict[tuple[str, str], dict[str, int]] = {}
    for visit in visit_rows:
        period = visit["visit_datetime"][:7] if visit["visit_datetime"] else ""
        key = (visit["project_id"], period)
        bucket = periods.setdefault(key, {"unique_visits": 0, "visits_assigned": 0, "visits_unassigned_only": 0})
        bucket["unique_visits"] += 1
        if visit["controller_fio"]:
            bucket["visits_assigned"] += 1
        if visit["has_unassigned_row"] and not visit["controller_fio"] and visit["action_row_count"] == 1:
            bucket["visits_unassigned_only"] += 1
    return [
        {
            "project_id": project_id,
            "period": period,
            "unique_visits": counts["unique_visits"],
            "visits_assigned": counts["visits_assigned"],
            "visits_unassigned_only": counts["visits_unassigned_only"],
        }
        for (project_id, period), counts in sorted(periods.items())
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("html_path")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--captured-at-utc", required=True)
    parser.add_argument("--source-url", default="https://lk.j4b.ru/action")
    parser.add_argument("--source-identity", default="")
    parser.add_argument("--coverage-scope", default="")
    parser.add_argument("--collection-completeness", default="COMPLETE_RESPONSE_PARSED")
    parser.add_argument("--output", default="")
    args = parser.parse_args(argv)

    if not _ISO_RE.match(args.captured_at_utc.strip()):
        print(json.dumps({"status": "REFUSED_BAD_CAPTURED_AT"}))
        return 2
    html_path = Path(args.html_path)
    if not html_path.is_file():
        print(json.dumps({"status": "REFUSED_HTML_UNAVAILABLE"}))
        return 2
    html = html_path.read_text(encoding="utf-8", errors="replace")
    source_identity = args.source_identity or f"per_project_action_query:{args.project_id}"
    coverage_scope = args.coverage_scope or f"SINGLE_PROJECT:{args.project_id}"
    try:
        facts = parse_action_fact_rows(
            html,
            args.project_id,
            captured_at_utc=args.captured_at_utc,
            source_url=args.source_url,
        )
        snapshot = build_registry_snapshot(
            facts,
            source_identity=source_identity,
            source_generation=f"visit_registry:{args.project_id}",
            captured_at_utc=args.captured_at_utc,
            source_url=args.source_url,
            coverage_scope=coverage_scope,
            collection_completeness=args.collection_completeness,
        )
    except VisitRegistryError as error:
        print(json.dumps({"status": "PARSE_FAILED", "reason": str(error)}, ensure_ascii=False))
        return 2

    visits = visit_grain(snapshot["facts"])
    payload = {
        "status": "OK",
        "snapshot_id": snapshot["snapshot_id"],
        "fingerprint": snapshot["fingerprint"],
        "manifest": snapshot["manifest"],
        "facts": snapshot["facts"],
        "visit_grain": visits,
        "controller_workload": controller_workload(visits),
        "project_summary": project_summary(visits, snapshot["facts"]),
        "visits_by_period": visits_by_period(visits),
    }
    rendered = json.dumps(payload, ensure_ascii=False, indent=1)
    if args.output:
        out_path = Path(args.output)
        out_path.write_text(rendered, encoding="utf-8")
        print(json.dumps({
            "status": "OK", "snapshot_id": payload["snapshot_id"],
            "output": str(out_path),
            "facts": len(payload["facts"]), "visits": len(payload["visit_grain"]),
        }, ensure_ascii=False))
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except VisitRegistryError as error:
        print(json.dumps({"status": "REFUSED", "reason": str(error)}, ensure_ascii=False))
        raise SystemExit(2)
