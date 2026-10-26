"""Visit-registry parser, grain, snapshot and publication-gate tests.

The synthetic fixture mirrors the verified structure of the real per-project
``POST /action`` response for project 8050 (38 assigned action rows over 27
visits, plus sentinel unassigned rows) without embedding any personal data.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.parsers import parse_action_table  # noqa: E402
from src.visit_registry import (  # noqa: E402
    MANIFEST_FIELDS,
    PARSE_FIELDS,
    VisitRegistryError,
    association_grain,
    build_registry_snapshot,
    parse_action_fact_rows,
    reconcile_claims,
    snapshot_fingerprint,
    verify_test_projects,
    visit_grain,
)

RUNNER = REPO_ROOT / "scripts" / "run_visit_registry_snapshot.py"
PUBLISHER = REPO_ROOT / "scripts" / "publish_visit_registry_test.py"

PROJECT = "8050"
CAPTURED = "2026-10-26T13:57:52Z"
SOURCE_URL = "https://lk.j4b.ru/action"


def assigned_row(
    action_id: str,
    visit_id: str,
    *,
    visit_dt: str = "21.08.2026 00:00",
    annotation: str = "",
    report_dt: str = "",
    controller: str = "Тестов Тест Тестович",
    status: str = "40",
    label: str = "Анкета утверждена",
    red: bool = False,
    split_controls: bool = False,
    user_id: str = "1001",
) -> str:
    row_class = "table-red" if red else "table-lime"
    controls = (
        f'<td colspan=6><a href="/action/{action_id}"></a></td>'
        if not split_controls
        else (
            f'<td><a href="/action/{action_id}"></a></td>'
            f'<td><a href="/action/{action_id}">20</a></td>'
            f'<td><a href="/action/{action_id}">Подтверждено менеджером</a></td>'
        )
    )
    return (
        f'<tr class="{row_class}">'
        f'<td class="center"><a href="/action/{action_id}">{action_id}</a></td>'
        f'<td><a href="/proj/{PROJECT}">Проект {PROJECT}</a> <span class="badge">Q3</span></td>'
        f'<td><a href="/visit/{visit_id}">3001600300 адрес тестовый</a></td>'
        f'<td>{visit_dt}{annotation}</td>'
        f'<td>500</td><td>₽</td>'
        f'<td>{report_dt}</td>'
        f'{controls}'
        f'<td><a href="/user/{user_id}" class="ul2">Тестов Тест Тестович</a></td>'
        f'<td><a href="/mail/new/{user_id}">user{user_id}@example.test</a></td>'
        f'<td>79000000000</td>'
        f'<td><a href="/action/{action_id}">{status}</a></td>'
        f'<td><a href="/action/{action_id}">{label}</a></td>'
        f'<td>{controller}</td>'
        f'<td>157 Тест проект</td>'
        f'</tr>'
    )


def sentinel_row(visit_id: str) -> str:
    return (
        f'<tr class="table-red">'
        f'<td><a href="/visit/{visit_id}">{visit_id}</a></td>'
        f'<td><a href="/proj/{PROJECT}">Проект {PROJECT}</a></td>'
        f'<td>3001600300 адрес тестовый</td>'
        f'<td></td><td>500</td><td>₽</td><td></td>'
        f'<td></td><td></td><td></td><td></td><td></td>'
        f'<td>0 приглашений</td><td></td>'
        f'<td>157 Тест проект</td>'
        f'</tr>'
    )


def build_fixture_html() -> str:
    """38 assigned action rows over 27 visits + 2 sentinel unassigned rows."""
    rows: list[str] = []
    action = 660100
    # 19 single-row visits (one without controller FIO)
    for visit in range(745331, 745350):
        action += 1
        rows.append(assigned_row(
            str(action), str(visit),
            controller="" if visit == 745340 else "Тестов Тест Тестович",
        ))
    # 6 double-row visits
    for visit in range(745350, 745356):
        for _ in range(2):
            action += 1
            rows.append(assigned_row(str(action), str(visit)))
    # 1 triple-row visit with annotated datetime and missing report datetime
    for offset, (annotation, report) in enumerate((
        ("  копир", ""), ("", "01.09.2026 10:00"), ("  срочная оплата", ""),
    )):
        action += 1
        rows.append(assigned_row(
            str(action), "745356", annotation=annotation, report_dt=report,
        ))
    # 1 quadruple-row visit: two controllers, two current statuses (40 and 39)
    for index, (controller, status, label) in enumerate((
        ("Первов Первый Отчествич", "40", "Анкета утверждена"),
        ("Второв Второй Отчествич", "39", "Есть претензия"),
        ("Первов Первый Отчествич", "40", "Анкета утверждена"),
        ("Второв Второй Отчествич", "39", "Есть претензия"),
    )):
        action += 1
        rows.append(assigned_row(
            str(action), "745357", controller=controller, status=status, label=label,
            split_controls=(index == 0), red=(index == 1),
        ))
    # 2 sentinel unassigned rows for visits that also have assigned rows
    rows.append(sentinel_row("745331"))
    rows.append(sentinel_row("745350"))
    return "<html><body><table>" + "".join(rows) + "</table></body></html>"


@pytest.fixture(scope="module")
def fixture_html() -> str:
    return build_fixture_html()


@pytest.fixture(scope="module")
def facts(fixture_html: str) -> list[dict]:
    return parse_action_fact_rows(
        fixture_html, PROJECT, captured_at_utc=CAPTURED, source_url=SOURCE_URL
    )


# ------------------------------------------------------------------ parser


def test_parser_counts_and_fields(facts: list[dict]):
    # 38 assigned action rows + 2 sentinel rows = 40 fact rows.
    assert len(facts) == 40
    assigned = [fact for fact in facts if fact["participant_assigned"]]
    assert len(assigned) == 38
    assert all(set(PARSE_FIELDS) <= set(fact) for fact in facts)
    assert {fact["project_id"] for fact in facts} == {PROJECT}
    sample = assigned[0]
    assert sample["visit_id"] == "745331"
    assert sample["visit_datetime"] == "2026-08-21T00:00"
    assert sample["visit_datetime_raw"] == "21.08.2026 00:00"
    assert sample["workflow_status_code"] == "40"
    assert sample["controller_fio"] == "Тестов Тест Тестович"
    assert sample["visitor_user_id"] == "1001"
    assert sample["project_name_short"] == "157 Тест проект"
    assert sample["source_url"] == SOURCE_URL
    assert sample["captured_at_utc"] == CAPTURED


def test_missing_field_behavior(facts: list[dict]):
    triple_rows = [f for f in facts if f["visit_id"] == "745356"]
    assert len(triple_rows) == 3
    annotated = next(f for f in triple_rows if f["visit_datetime_raw"].endswith("копир"))
    assert annotated["visit_datetime"] == "2026-08-21T00:00"
    assert "VISIT_DATETIME_ANNOTATED" in annotated["quality_flags"]
    assert annotated["report_datetime"] == ""
    assert "REPORT_DATETIME_ABSENT" in annotated["quality_flags"]
    filled = next(f for f in triple_rows if f["report_datetime"])
    assert filled["report_datetime"] == "2026-09-01T10:00"


def test_sentinel_unassigned_rows(facts: list[dict]):
    sentinels = [f for f in facts if not f["participant_assigned"]]
    assert len(sentinels) == 2
    assert all(f["action_id"] == "" for f in sentinels)
    assert all(f["visit_datetime"] == "" for f in sentinels)
    assert all("UNASSIGNED_ROW" in f["quality_flags"] for f in sentinels)
    # red styling with a real action link is NOT unassigned
    red_assigned = [
        f for f in facts
        if f["participant_assigned"] and f["workflow_status_code"] == "39"
    ]
    assert red_assigned, "red assigned-style row must stay assigned"


def test_date_parsing_malformed_raises(fixture_html: str):
    broken = fixture_html.replace("21.08.2026 00:00", "21.13.2026 00:00", 1)
    with pytest.raises(VisitRegistryError):
        parse_action_fact_rows(broken, PROJECT, captured_at_utc=CAPTURED, source_url=SOURCE_URL)


def test_status_parsing_unknown_code_raises(fixture_html: str):
    broken = fixture_html.replace(
        '<td><a href="/action/660101">40</a></td>',
        '<td><a href="/action/660101">77</a></td>', 1,
    )
    with pytest.raises(VisitRegistryError):
        parse_action_fact_rows(broken, PROJECT, captured_at_utc=CAPTURED, source_url=SOURCE_URL)


def test_status_unresolved_flag(fixture_html: str):
    # A row whose only action links carry no workflow-state labels.
    broken = fixture_html.replace(
        '<td><a href="/action/660101">40</a></td><td><a href="/action/660101">Анкета утверждена</a></td>',
        '<td></td><td></td>', 1,
    )
    parsed = parse_action_fact_rows(broken, PROJECT, captured_at_utc=CAPTURED, source_url=SOURCE_URL)
    target = next(f for f in parsed if f["action_id"] == "660101")
    assert target["workflow_status_code"] == ""
    assert "STATUS_UNRESOLVED" in target["quality_flags"]


def test_unexpected_structures_raise(fixture_html: str):
    # visit datetime cell replaced by arbitrary text
    broken = fixture_html.replace("<td>21.08.2026 00:00</td>", "<td>не дата</td>", 1)
    with pytest.raises(VisitRegistryError):
        parse_action_fact_rows(broken, PROJECT, captured_at_utc=CAPTURED, source_url=SOURCE_URL)
    # visitor link without visit anchor
    with pytest.raises(VisitRegistryError):
        parse_action_fact_rows(
            '<table><tr><td><a href="/user/1">Иванов Иван</a></td></tr></table>',
            PROJECT, captured_at_utc=CAPTURED, source_url=SOURCE_URL,
        )
    # non-red row with a visit link but no action link anywhere
    no_action = (
        '<table><tr class="table-lime">'
        '<td><a href="/visit/745331">745331</a></td>'
        '<td><a href="/proj/8050">Проект 8050</a></td>'
        '<td>21.08.2026 00:00</td><td>500</td><td>₽</td><td></td>'
        '<td></td><td></td><td></td>'
        '<td>Тестов Тест Тестович</td><td>157 Тест проект</td>'
        '</tr></table>'
    )
    with pytest.raises(VisitRegistryError):
        parse_action_fact_rows(no_action, PROJECT, captured_at_utc=CAPTURED, source_url=SOURCE_URL)
    # empty response
    with pytest.raises(VisitRegistryError):
        parse_action_fact_rows("<table></table>", PROJECT, captured_at_utc=CAPTURED, source_url=SOURCE_URL)


def test_controller_fio_variants(fixture_html: str):
    parsed = parse_action_fact_rows(fixture_html, PROJECT, captured_at_utc=CAPTURED, source_url=SOURCE_URL)
    absent = [f for f in parsed if "CONTROLLER_FIO_ABSENT" in f["quality_flags"]]
    assert absent, "expected at least one row without controller FIO"
    assert all(f["controller_fio"] == "" for f in absent)


# ------------------------------------------------------------------ grains


def test_visit_deduplication(facts: list[dict]):
    grains = visit_grain(facts)
    assert len(grains) == 27
    keys = [(g["project_id"], g["visit_id"]) for g in grains]
    assert len(set(keys)) == 27


def test_multiple_action_rows_preserved(facts: list[dict]):
    grains = visit_grain(facts)
    multi = [g for g in grains if g["action_row_count"] > 1]
    assert len(multi) == 8  # 6 doubles + 1 triple + 1 quadruple
    quad = next(g for g in grains if g["visit_id"] == "745357")
    assert quad["action_row_count"] == 4
    assert len(quad["action_ids"].split("|")) == 4
    assert association_grain(facts) == facts  # association grain keeps every row


def test_multiple_controller_association(facts: list[dict]):
    quad = next(g for g in visit_grain(facts) if g["visit_id"] == "745357")
    assert "MULTIPLE_CONTROLLERS" in quad["quality_flags"]
    assert quad["controller_fio"] == ""  # ambiguous at visit grain
    assert set(quad["controller_fio_all"].split("|")) == {
        "Первов Первый Отчествич", "Второв Второй Отчествич",
    }
    # association grain preserves both associations independently
    quad_facts = [f for f in facts if f["visit_id"] == "745357"]
    assert {f["controller_fio"] for f in quad_facts} == {
        "Первов Первый Отчествич", "Второв Второй Отчествич",
    }


def test_multiple_current_statuses_flag(facts: list[dict]):
    quad = next(g for g in visit_grain(facts) if g["visit_id"] == "745357")
    assert "MULTIPLE_CURRENT_STATUSES" in quad["quality_flags"]
    assert quad["workflow_status_code"] == ""
    assert set(quad["workflow_status_codes"].split("|")) == {"39", "40"}


def test_status_extraction_matches_legacy_parser(fixture_html: str):
    legacy = [row for row in parse_action_table(fixture_html, PROJECT) if row["participant_assigned"]]
    modern = [f for f in parse_action_fact_rows(
        fixture_html, PROJECT, captured_at_utc=CAPTURED, source_url=SOURCE_URL
    ) if f["participant_assigned"]]
    legacy_index = {(row["visit_id"], row["action_id"]): row for row in legacy}
    assert len(legacy_index) == len(modern)
    for fact in modern:
        twin = legacy_index[(fact["visit_id"], fact["action_id"])]
        assert fact["workflow_status_code"] == twin["visit_status"]
        assert fact["workflow_state_codes"] == twin["workflow_state_codes"]


# ---------------------------------------------------------------- snapshot


def test_snapshot_deterministic_and_append_only(facts: list[dict]):
    first = build_registry_snapshot(
        facts, source_identity="per_project_action_query:8050",
        source_generation="visit_registry:8050", captured_at_utc=CAPTURED,
        source_url=SOURCE_URL, coverage_scope="SINGLE_PROJECT:8050",
    )
    second = build_registry_snapshot(
        facts, source_identity="per_project_action_query:8050",
        source_generation="visit_registry:8050", captured_at_utc=CAPTURED,
        source_url=SOURCE_URL, coverage_scope="SINGLE_PROJECT:8050",
    )
    assert first["snapshot_id"] == second["snapshot_id"]
    assert first["fingerprint"] == second["fingerprint"]
    assert set(MANIFEST_FIELDS) <= set(first["manifest"])
    manifest = first["manifest"]
    assert manifest["response_rows"] == 40
    assert manifest["assigned_rows"] == 38
    assert manifest["unassigned_rows"] == 2
    assert manifest["distinct_visits"] == 27
    assert manifest["claim_visit_count"] == 1  # visit 745357 carries code 39
    assert json.loads(manifest["status_code_counts"])["39"] == 2
    # snapshot facts carry the snapshot identity without mutating inputs
    assert all(fact["snapshot_id"] == first["snapshot_id"] for fact in first["facts"])
    assert "snapshot_id" not in facts[0]


def test_snapshot_rejects_empty():
    with pytest.raises(VisitRegistryError):
        build_registry_snapshot(
            [], source_identity="x", source_generation="g",
            captured_at_utc=CAPTURED, source_url=SOURCE_URL, coverage_scope="s",
        )


# ---------------------------------------------------------- reconciliation


def _fact(visit_id: str, project_id: str, codes: list[str]) -> dict:
    return {
        "project_id": project_id, "visit_id": visit_id,
        "workflow_state_codes": codes, "quality_flags": [],
    }


def test_reconciliation_consistent():
    result = reconcile_claims(
        [_fact("1", "8050", ["39"])],
        [_fact("1", "8050", ["39"])],
        [{"visit_id": "1", "project_id": "8050", "status_id": "39"}],
    )
    assert result["reconciliation_status"] == "CONSISTENT"
    assert result["matched_visits"] == ["1"]


def test_reconciliation_differences_and_test_exclusion():
    result = reconcile_claims(
        [_fact("1", "8050", ["39"])],
        [_fact("1", "8050", ["39"]), _fact("2", "8050", ["39"])],
        [
            {"visit_id": "1", "project_id": "8050", "status_id": "39"},
            {"visit_id": "3", "project_id": "8050", "status_id": "39"},
            {"visit_id": "9", "project_id": "2995", "status_id": "39"},
        ],
        exclude_project_ids=("2995",),
    )
    assert result["reconciliation_status"] == "DIFFERENCES_FOUND"
    assert result["only_in_native_query"] == ["2"]
    assert result["only_in_stored_snapshot"] == ["3"]
    assert "9" not in result["only_in_stored_snapshot"]  # excluded test project
    assert result["claim_occurrence_time"] == "UNKNOWN"


def test_reconciliation_membership_is_not_current_status():
    # code 39 in a membership list of a visit whose current status is 40
    # must NOT be counted as a current claim by the native-query semantics
    # used here; only explicit 39 membership counts, and the reconciliation
    # reports differences instead of resolving them.
    result = reconcile_claims(
        [_fact("1", "8050", ["40", "39"])],
        [_fact("1", "8050", ["39"])],
        [{"visit_id": "1", "project_id": "8050", "status_id": "39"}],
    )
    assert result["reconciliation_status"] == "CONSISTENT"


def test_verify_test_projects():
    verified, rejected = verify_test_projects({
        "2995": "Зоомагазин_тестовый пример №1",
        "3359": "Зоомагазин_тестовый пример №2",
        "3360": "Реальный проект",
    })
    assert verified == ["2995", "3359"]
    assert rejected == ["3360"]


# ------------------------------------------------------------- publication


def _payload(facts: list[dict], completeness: str = "COMPLETE_RESPONSE_PARSED") -> dict:
    snapshot = build_registry_snapshot(
        facts, source_identity="per_project_action_query:8050",
        source_generation="visit_registry:8050", captured_at_utc=CAPTURED,
        source_url=SOURCE_URL, coverage_scope="SINGLE_PROJECT:8050",
        collection_completeness=completeness,
    )
    from scripts.run_visit_registry_snapshot import (
        controller_workload, project_summary, visits_by_period,
    )
    visits = visit_grain(snapshot["facts"])
    return {
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


def _existing_with_history(payload: dict) -> dict[str, list[list]]:
    from scripts.publish_visit_registry_test import HISTORY_HEADER, _history_row
    return {
        "visits_registry_history": [list(HISTORY_HEADER)] + [
            _history_row(fact) for fact in payload["facts"]
        ],
    }


def _apply(existing: dict[str, list[list]], ops: list[dict]) -> None:
    """Minimal in-memory executor mirroring execute_plan semantics."""
    for op in ops:
        title = op["title"]
        rows = existing.setdefault(title, [])
        if op["op"] == "add_sheet":
            continue
        elif op["op"] == "ensure_headers":
            if not rows:
                rows.append(list(op["header"]))
        elif op["op"] == "write_rows":
            start = op["start_row"] - 1
            while len(rows) < start:
                rows.append([])
            for offset, row in enumerate(op["rows"]):
                target = start + offset
                while len(rows) <= target:
                    rows.append([])
                rows[target] = list(row)
        elif op["op"] == "clear_rows":
            start = op["start_row"] - 1
            for offset in range(op["count"]):
                if start + offset < len(rows):
                    rows[start + offset] = [""] * 17
        else:
            raise AssertionError(f"unknown op {op['op']}")


def test_publication_idempotent_rerun(facts: list[dict]):
    from scripts.publish_visit_registry_test import build_publication_plan
    payload = _payload(facts)
    existing: dict[str, list[list]] = {}
    first = build_publication_plan(payload, existing, "2026-10-26T14:00:00Z")
    assert first["status"] == "PUBLISH"
    _apply(existing, first["ops"])
    second = build_publication_plan(payload, existing, "2026-10-26T14:10:00Z")
    assert second["status"] == "ALREADY_PUBLISHED"
    assert second["ops"] == []


def test_publication_resumes_partial_publication(facts: list[dict]):
    from scripts.publish_visit_registry_test import build_publication_plan
    payload = _payload(facts)
    # Prior session published only history + manifest.
    partial: dict[str, list[list]] = {}
    full = build_publication_plan(payload, partial, "2026-10-26T14:00:00Z")
    _apply(partial, [
        op for op in full["ops"] if op["title"] in (
            "visits_registry_history", "visits_registry_manifest",
        )
    ])
    plan = build_publication_plan(payload, partial, "2026-10-26T14:10:00Z")
    assert plan["status"] == "PUBLISH"
    titles_touched = {op["title"] for op in plan["ops"] if op["op"] == "write_rows"}
    assert "visits_registry_history" not in titles_touched
    assert "visits_registry_manifest" not in titles_touched
    assert "controller_workload" in titles_touched
    assert "visits_current" in titles_touched


def test_publication_refuses_conflicting_same_id(facts: list[dict]):
    from scripts.publish_visit_registry_test import build_publication_plan
    payload = _payload(facts)
    existing = _existing_with_history(payload)
    tampered = json.loads(json.dumps(payload))
    tampered["facts"][0]["controller_fio"] = "Изменённый Контролёр"
    tampered["fingerprint"] = snapshot_fingerprint(tampered["facts"])
    plan = build_publication_plan(tampered, existing, "2026-10-26T14:00:00Z")
    assert plan["status"] == "REFUSED_SNAPSHOT_ID_CONFLICT"
    assert plan["ops"] == []


def test_publication_appends_after_existing_rows(facts: list[dict]):
    from scripts.publish_visit_registry_test import (
        HISTORY_HEADER,
        MANIFEST_HEADER,
        METRICS_HEADER,
        PERIOD_HEADER,
        QUALITY_HEADER,
        SUMMARY_HEADER,
        WORKLOAD_HEADER,
        build_publication_plan,
    )
    payload = _payload(facts)
    workload_header = ("snapshot_id", "captured_at_utc") + WORKLOAD_HEADER[2:]
    existing = {
        "visits_registry_history": [list(HISTORY_HEADER), ["old", "row"]],
        "visits_registry_manifest": [list(MANIFEST_HEADER), ["old"]],
        "controller_workload": [list(workload_header), ["old"]],
        "project_summary": [list(SUMMARY_HEADER), ["old"]],
        "visits_by_period": [list(PERIOD_HEADER), ["old"]],
        "data_quality_status": [list(QUALITY_HEADER), ["old"]],
        "metric_dictionary": [list(METRICS_HEADER), ["old"]],
        "visits_current": [["snapshot_id"], ["old-snapshot", "x"]],
    }
    plan = build_publication_plan(payload, existing, "2026-10-26T14:00:00Z")
    assert plan["status"] == "PUBLISH"
    ops = plan["ops"]
    writes = {op["title"]: op for op in ops if op["op"] == "write_rows"}
    assert writes["visits_registry_history"]["start_row"] == 3
    assert writes["visits_registry_manifest"]["start_row"] == 3
    assert writes["controller_workload"]["start_row"] == 3
    assert writes["project_summary"]["start_row"] == 3
    assert writes["visits_by_period"]["start_row"] == 3
    assert writes["data_quality_status"]["start_row"] == 3
    assert writes["metric_dictionary"]["start_row"] == 3
    # current-state view is replaced, with old rows cleared first
    assert any(op["op"] == "clear_rows" and op["title"] == "visits_current" for op in ops)
    current_write = next(
        op for op in ops if op["op"] == "write_rows" and op["title"] == "visits_current"
        and op["start_row"] == 2
    )
    assert current_write["start_row"] == 2
    assert len(current_write["rows"]) == 27


def test_publication_refuses_incomplete_current_replace(facts: list[dict]):
    from scripts.publish_visit_registry_test import build_publication_plan
    payload = _payload(facts, completeness="PARTIAL_RESPONSE_PARSED")
    existing = {"visits_current": [["snapshot_id"], ["old-snapshot", "x"]]}
    plan = build_publication_plan(payload, existing, "2026-10-26T14:00:00Z")
    assert plan["status"] == "REFUSED_CURRENT_STATE_REPLACE_INCOMPLETE"
    assert plan["ops"] == []


def test_publication_allows_complete_current_replace(facts: list[dict]):
    from scripts.publish_visit_registry_test import build_publication_plan
    payload = _payload(facts)
    existing = {"visits_current": [["snapshot_id"], ["old-snapshot", "x"]]}
    plan = build_publication_plan(payload, existing, "2026-10-26T14:00:00Z")
    assert plan["status"] == "PUBLISH"


def test_publication_refuses_header_mismatch(facts: list[dict]):
    from scripts.publish_visit_registry_test import build_publication_plan
    payload = _payload(facts)
    existing = {"visits_registry_history": [["wrong", "header"], ["old", "row"]]}
    plan = build_publication_plan(payload, existing, "2026-10-26T14:00:00Z")
    assert plan["status"] == "REFUSED_HEADER_MISMATCH"
    assert plan["tab"] == "visits_registry_history"
    assert plan["ops"] == []


def test_publication_upgrades_header_only_current_tab(facts: list[dict]):
    from scripts.publish_visit_registry_test import CURRENT_HEADER, build_publication_plan
    payload = _payload(facts)
    existing = {"visits_current": [["snapshot_id", "old_col"]]}
    plan = build_publication_plan(payload, existing, "2026-10-26T14:00:00Z")
    assert plan["status"] == "PUBLISH"
    header_write = next(
        op for op in plan["ops"]
        if op["op"] == "write_rows" and op["title"] == "visits_current"
        and op["start_row"] == 1
    )
    assert header_write["rows"][0] == list(CURRENT_HEADER)


def test_publication_skips_ensure_headers_for_existing_tabs(facts: list[dict]):
    from scripts.publish_visit_registry_test import (
        HISTORY_HEADER,
        METRICS_HEADER,
        QUALITY_HEADER,
        build_publication_plan,
    )
    payload = _payload(facts)
    existing = {
        "visits_registry_history": [list(HISTORY_HEADER), ["old", "row"]],
        "data_quality_status": [list(QUALITY_HEADER), ["old"]],
        "metric_dictionary": [list(METRICS_HEADER), ["old"]],
    }
    plan = build_publication_plan(payload, existing, "2026-10-26T14:00:00Z")
    assert plan["status"] == "PUBLISH"
    assert not any(
        op["op"] == "ensure_headers" and op["title"] == "visits_registry_history"
        for op in plan["ops"]
    )
    history_write = next(
        op for op in plan["ops"]
        if op["op"] == "write_rows" and op["title"] == "visits_registry_history"
    )
    assert history_write["start_row"] == 3


def test_history_round_trip_preserves_empty_trailing_fields(facts: list[dict]):
    from scripts.publish_visit_registry_test import (
        HISTORY_HEADER,
        _fact_from_history_row,
        _history_row,
    )
    from src.visit_registry import build_registry_snapshot
    snapshot = build_registry_snapshot(
        facts, source_identity="per_project_action_query:8050",
        source_generation="visit_registry:8050", captured_at_utc=CAPTURED,
        source_url=SOURCE_URL, coverage_scope="SINGLE_PROJECT:8050",
    )
    empty_flag = next(f for f in snapshot["facts"] if not f["quality_flags"])
    row = _history_row(empty_flag)
    # simulate the Sheets API trimming trailing empty cells
    trimmed = list(row)
    while trimmed and (trimmed[-1] == "" or trimmed[-1] is None):
        trimmed.pop()
    restored = _fact_from_history_row(trimmed)
    assert restored["quality_flags"] == []
    assert restored["workflow_state_codes"] == empty_flag["workflow_state_codes"]
    assert restored == empty_flag


def test_execute_plan_against_fake_gateway(facts: list[dict]):
    from scripts.publish_visit_registry_test import build_publication_plan, execute_plan
    payload = _payload(facts)
    plan = build_publication_plan(payload, {}, "2026-10-26T14:00:00Z")
    assert plan["status"] == "PUBLISH"

    class FakeGateway:
        def __init__(self) -> None:
            self.sheets: dict[str, list[list]] = {}
            self.calls: list[str] = []

        def add_sheet(self, title: str) -> None:
            self.calls.append(f"add:{title}")
            self.sheets.setdefault(title, [])

        def ensure_headers(self, title: str, header: list[str]) -> None:
            self.calls.append(f"headers:{title}")
            rows = self.sheets.setdefault(title, [])
            if not rows:
                rows.append(list(header))
            else:
                rows[0] = list(header)

        def update(self, title: str, cell_range: str, rows) -> None:
            self.calls.append(f"update:{title}:{cell_range}")
            sheet = self.sheets.setdefault(title, [])
            start = int(cell_range[1:]) - 1
            while len(sheet) < start:
                sheet.append([])
            for offset, row in enumerate(rows):
                target = start + offset
                while len(sheet) <= target:
                    sheet.append([])
                sheet[target] = list(row)

    fake = FakeGateway()
    execute_plan(fake, plan["ops"])
    # every published cell must be a scalar (Sheets rejects nested lists)
    for rows in fake.sheets.values():
        for row in rows:
            assert all(not isinstance(cell, list) for cell in row)
    history = fake.sheets["visits_registry_history"]
    assert history[0][0] == "snapshot_id"
    assert len(history) == 1 + len(payload["facts"])
    assert len(fake.sheets["visits_current"]) == 1 + 27
    assert len(fake.sheets["controller_workload"]) == 1 + len(payload["controller_workload"])
    assert fake.sheets["data_quality_status"][1][0] == "visit_registry"
    metric_keys = {row[2] for row in fake.sheets["metric_dictionary"][1:]}
    assert "claim_rate" in metric_keys


# ------------------------------------------------------------ runner gates


def test_runner_refuses_bad_captured_at(tmp_path):
    html_path = tmp_path / "response.html"
    html_path.write_text(build_fixture_html(), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(RUNNER), str(html_path), "--project-id", PROJECT,
         "--captured-at-utc", "not-a-date"],
        capture_output=True, text=True,
    )
    assert result.returncode == 2
    assert json.loads(result.stdout)["status"] == "REFUSED_BAD_CAPTURED_AT"


def test_runner_refuses_missing_file(tmp_path):
    result = subprocess.run(
        [sys.executable, str(RUNNER), str(tmp_path / "nope.html"),
         "--project-id", PROJECT, "--captured-at-utc", CAPTURED],
        capture_output=True, text=True,
    )
    assert result.returncode == 2
    assert json.loads(result.stdout)["status"] == "REFUSED_HTML_UNAVAILABLE"


def test_runner_end_to_end(tmp_path):
    html_path = tmp_path / "response.html"
    html_path.write_text(build_fixture_html(), encoding="utf-8")
    out_path = tmp_path / "snapshot.json"
    result = subprocess.run(
        [sys.executable, str(RUNNER), str(html_path), "--project-id", PROJECT,
         "--captured-at-utc", CAPTURED, "--output", str(out_path)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["status"] == "OK"
    assert len(payload["facts"]) == 40
    assert len(payload["visit_grain"]) == 27
    assert payload["manifest"]["assigned_rows"] == 38
    periods = {row["period"]: row["unique_visits"] for row in payload["visits_by_period"]}
    assert periods["2026-08"] == 27  # all fixture visits share the August date
    # Only unambiguous controllers are counted: the visit with two FIOs is
    # excluded, and the visit without any FIO is skipped.
    assert payload["controller_workload"] == [
        {"project_id": PROJECT, "controller_fio": "Тестов Тест Тестович",
         "visits_assigned": 25, "action_rows_handled": 33},
    ]


def test_publisher_refuses_without_approval(tmp_path):
    payload = _payload(parse_action_fact_rows(
        build_fixture_html(), PROJECT, captured_at_utc=CAPTURED, source_url=SOURCE_URL
    ))
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(PUBLISHER), str(path)], capture_output=True, text=True,
    )
    assert result.returncode == 2
    assert json.loads(result.stdout)["status"] == "REFUSED_WRITE_NOT_APPROVED"


def test_publisher_refuses_snapshot_not_ok(tmp_path):
    payload = _payload(parse_action_fact_rows(
        build_fixture_html(), PROJECT, captured_at_utc=CAPTURED, source_url=SOURCE_URL
    ))
    payload["status"] = "VALIDATION_FAILED"
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(PUBLISHER), str(path), "--approve-test-write"],
        capture_output=True, text=True,
    )
    assert result.returncode == 2
    assert json.loads(result.stdout)["status"] == "REFUSED_SNAPSHOT_NOT_OK"
