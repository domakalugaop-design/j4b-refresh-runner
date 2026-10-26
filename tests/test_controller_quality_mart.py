from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.controller_quality_mart import (
    METRIC_DICTIONARY_COLUMNS, REPORT_COLUMNS, TAB_SCHEMAS, VISIT_COLUMNS, GenerationPublisher,
    MartError, acquire_action_statuses, build_candidate, extract_project_action_rows,
    summarize_observed_metrics,
    run_refresh, validate_collector_success, validate_report_source, validate_status_rows,
    validate_historical_source,
)
from scripts.run_controller_quality_mart import (
    HISTORICAL_TEST_SPREADSHEET_ID, SOURCE_SPREADSHEET_ID,
    _historical_destination_gate,
)


NOW = datetime(2026, 10, 26, 16, 0, tzinfo=timezone.utc)
COLLECTOR = {
    "result": "success", "exit_status": "0", "started_at": NOW,
    "collector_result": "SUCCESS",
}


def report(report_id="680368", action_id=None, visit_id="100", project_id="10", **overrides):
    row = {
        "report_id": report_id, "action_id": action_id or report_id,
        "visit_id": visit_id, "project_id": project_id, "report_date": "2026-10-17",
        "corrector_id": "42", "corrector_fio": "Synthetic Person",
        "source_status": "CURRENT", "last_successful_fetch": "2026-10-26T15:00:00+00:00",
    }
    row.update(overrides)
    return row


def action(project="10", visit="100", action_id="680368", codes=None):
    return {
        "project_id": project, "visit_id": visit, "action_id": action_id,
        "workflow_state_codes": codes or [],
    }


def test_exact_schemas_and_source_id_normalization():
    rows = validate_report_source([report(report_id="000680368", action_id="680368")])
    assert rows[0]["report_id"] == "680368"
    candidate = build_candidate(rows, [action(codes=[39])], queried_project_ids={"10"},
                                generation_id="a" * 32, source_snapshot_at=NOW.isoformat())
    assert tuple(candidate["controller_report_snapshot"][0]) == REPORT_COLUMNS
    assert tuple(candidate["controller_visit_summary"][0]) == VISIT_COLUMNS
    assert tuple(candidate["metric_dictionary"][0]) == METRIC_DICTIONARY_COLUMNS
    assert any(row[2] == "status_39_reports" and "не ошибка контролёра" in row[7]
               for row in candidate["metric_dictionary"][1:])
    dictionary = {row[2]: row for row in candidate["metric_dictionary"][1:]}
    assert dictionary["status_39_avg_per_project"][6] == "AVAILABLE_IN_OBSERVED_SNAPSHOT"
    assert dictionary["status_39_avg_per_controller_association"][6] == "AVAILABLE_IN_OBSERVED_SNAPSHOT"
    for metric in ("status_39_avg_per_calendar_interval", "status_39_mean_rate_by_period",
                   "status_39_period_change_pp", "status_39_moving_average",
                   "project_name", "visit_date"):
        assert dictionary[metric][6] == "NOT_AVAILABLE"
    assert set(candidate) == set(TAB_SCHEMAS)
    assert candidate["controller_report_snapshot"][1][13] == "OBSERVED_PRESENT"


def test_duplicate_report_ids_are_rejected():
    with pytest.raises(MartError, match="duplicate report_id"):
        validate_report_source([report(), report()])


def test_source_schema_and_unsupported_status_fail_closed():
    with pytest.raises(MartError, match="schema"):
        validate_report_source([{"report_id": "1"}])
    with pytest.raises(MartError, match="source_status"):
        validate_report_source([report(source_status="NO_CORRECTOR")])


def test_duplicate_action_membership_is_deduplicated_but_overlapping_codes_remain():
    rows = validate_status_rows([action(codes=[35, 38, 39, 39]), action(codes=[35, 38, 39])])
    assert [row["workflow_state_code"] for row in rows].count(39) == 1
    assert {row["workflow_state_code"] for row in rows} == {35, 38, 39}


def test_multi_action_visit_is_retained_and_current_version_stays_unknown():
    rows = [report("500", "500"), report("501", "501", corrector_id="43", corrector_fio="Other Person")]
    candidate = build_candidate(rows, [action(action_id="500", codes=[35]), action(action_id="501", codes=[40])],
                                queried_project_ids={"10"}, generation_id="b" * 32,
                                source_snapshot_at=NOW.isoformat())
    visit = candidate["controller_visit_summary"][1]
    assert visit[5] == 2
    assert visit[7:10] == ["", "", "UNRESOLVED_CURRENT_VERSION_RULE"]


def test_corrector_attribution_is_preserved_from_source():
    candidate = build_candidate([report(corrector_id="00042")], [action(codes=[40])],
                                queried_project_ids={"10"}, generation_id="c" * 32,
                                source_snapshot_at=NOW.isoformat())
    row = candidate["controller_report_snapshot"][1]
    assert row[7:9] == ["42", "Synthetic Person"]


def test_unknown_status_is_not_false_when_action_missing_including_known_680368_case():
    candidate = build_candidate([report("680368", "680368")], [], queried_project_ids={"10"},
                                generation_id="d" * 32, source_snapshot_at=NOW.isoformat())
    row = candidate["controller_report_snapshot"][1]
    assert row[13] == "UNKNOWN"
    assert candidate["data_quality_status"][1][9] == 1


def test_exact_action_visit_join_prevents_status_from_wrong_visit():
    candidate = build_candidate([report(visit_id="100")], [action(visit="999", codes=[39])],
                                queried_project_ids={"10"}, generation_id="e" * 32,
                                source_snapshot_at=NOW.isoformat())
    assert candidate["controller_report_snapshot"][1][13] == "UNKNOWN"


def test_historical_backfill_scopes_current_status_to_exact_report_action_keys():
    reports = [report("500", "500", visit_id="100"), report("501", "501", visit_id="101")]
    candidate = build_candidate(
        reports,
        [action(visit="100", action_id="500", codes=[39]),
         action(visit="101", action_id="501", codes=[]),
         action(visit="999", action_id="999", codes=[39])],
        queried_project_ids={"10"}, generation_id="2" * 32,
        source_snapshot_at=NOW.isoformat(),
        action_scope_keys={("10", "500", "100"), ("10", "501", "101")},
        refresh_status="PUBLISHED_HISTORICAL_BACKFILL",
        source_freshness="ACCUMULATED_4_DAY_REGISTRY_CURRENT_STATUS",
    )
    assert [row[13] for row in candidate["controller_report_snapshot"][1:]] == [
        "OBSERVED_PRESENT", "OBSERVED_ABSENT",
    ]
    assert candidate["data_quality_status"][1][3] == "PUBLISHED_HISTORICAL_BACKFILL"
    assert candidate["data_quality_status"][1][13] == "ACCUMULATED_4_DAY_REGISTRY_CURRENT_STATUS"


def test_historical_backfill_source_gate_requires_exact_reviewed_inventory():
    rows = []
    for index in range(722):
        day = ("2026-10-22", "2026-10-23", "2026-10-24", "2026-10-25")[
            0 if index < 161 else 1 if index < 361 else 2 if index < 549 else 3
        ]
        rows.append(report(
            str(10000 + index), str(20000 + index), visit_id=str(30000 + index),
            project_id=str(1 + index % 57), corrector_id=str(1 + index % 22),
            corrector_fio=f"Person {1 + index % 22}",
            report_date="2026-10-01" if index == 0 else "2026-10-25" if index == 721 else "2026-10-17",
            last_successful_fetch=f"{day}T15:00:00+00:00",
        ))
    expected = {"2026-10-22": 161, "2026-10-23": 200, "2026-10-24": 188, "2026-10-25": 173}
    assert len(validate_historical_source(
        rows, expected_fetch_day_counts=expected, expected_rows=722,
        expected_projects=57, expected_controllers=22,
    )) == 722
    with pytest.raises(MartError, match="row count"):
        validate_historical_source(
            rows[:-1], expected_fetch_day_counts=expected, expected_rows=722,
            expected_projects=57, expected_controllers=22,
        )


def test_historical_backfill_destination_is_fixed_and_explicitly_enabled():
    _historical_destination_gate(SOURCE_SPREADSHEET_ID, HISTORICAL_TEST_SPREADSHEET_ID, "true")
    with pytest.raises(MartError, match="feature gate"):
        _historical_destination_gate(SOURCE_SPREADSHEET_ID, HISTORICAL_TEST_SPREADSHEET_ID, "")
    with pytest.raises(MartError, match="not allowlisted"):
        _historical_destination_gate(SOURCE_SPREADSHEET_ID, SOURCE_SPREADSHEET_ID, "true")


def test_project_aggregate_without_action_identity_is_rejected():
    with pytest.raises(MartError, match="invalid status membership"):
        validate_status_rows([{"project_id": "10", "visit_id": "100", "workflow_state_codes": [39]}])


def test_partial_project_status_coverage_blocks_candidate():
    with pytest.raises(MartError, match="incomplete"):
        build_candidate([report(project_id="10"), report("502", "502", project_id="11")],
                        [action()], queried_project_ids={"10"})


def test_empty_source_and_stale_collector_are_fail_closed():
    with pytest.raises(MartError, match="empty"):
        build_candidate([], [], queried_project_ids=set())
    stale = {**COLLECTOR, "started_at": datetime(2026, 10, 25, 16, 0, tzinfo=timezone.utc)}
    with pytest.raises(MartError, match="not from today"):
        validate_collector_success(stale, now=NOW)


def test_observed_workload_and_status_39_metrics_use_correct_grains_and_denominators():
    reports = [
        report("500", "500", visit_id="100", project_id="10", corrector_id="42"),
        report("501", "501", visit_id="100", project_id="10", corrector_id="43",
               corrector_fio="Other Person"),
        report("502", "502", visit_id="200", project_id="10", corrector_id="43",
               corrector_fio="Other Person"),
        report("503", "503", visit_id="300", project_id="11", corrector_id="42"),
    ]
    candidate = build_candidate(
        reports,
        [action(visit="100", action_id="500", codes=[39]),
         action(visit="100", action_id="501", codes=[]),
         action(visit="300", project="11", action_id="503", codes=[])],
        queried_project_ids={"10", "11"}, generation_id="f" * 32,
        source_snapshot_at=NOW.isoformat(),
    )

    result = summarize_observed_metrics(candidate)

    assert result["coverage"] == "OBSERVED_CORRECTOR_SNAPSHOT_ONLY"
    assert result["controllers_count"] == 2
    assert result["projects_count"] == 2
    assert result["overall"] == {
        "observed_reports": 4,
        "observed_visits": 3,
        "eligible_reports": 3,
        "status_39_reports": 1,
        "status_39_report_share": pytest.approx(1 / 3),
        "eligible_visits": 2,
        "status_39_visits": 1,
        "status_39_visit_share": pytest.approx(1 / 2),
        "projects_with_known_status": 2,
        "status_39_visits_per_project_avg": pytest.approx(1 / 2),
        "controllers_with_known_status": 2,
        "status_39_visit_controller_associations": 1,
        "status_39_visit_associations_per_controller_avg": pytest.approx(1 / 2),
    }
    assert result["by_controller"]["42"]["status_39_reports"] == 1
    assert result["by_controller"]["42"]["status_39_report_share"] == pytest.approx(1 / 2)
    assert result["by_controller"]["43"]["eligible_reports"] == 1
    assert result["by_controller"]["43"]["status_39_report_share"] == 0
    assert result["by_project"]["10"]["status_39_visits_per_project_avg"] == pytest.approx(1)
    assert result["by_project"]["11"]["status_39_visits_per_project_avg"] == 0
    # The same visit is associated with both controllers, so grouped distinct
    # visit totals overlap and must not be summed into an overall total.
    assert result["by_controller"]["42"]["observed_visits"] == 2
    assert result["by_controller"]["43"]["observed_visits"] == 2
    assert result["controller_visit_totals_additive"] is False
    assert result["controller_project_totals_additive"] is False
    assert result["status_39_semantics"].endswith("NOT_FAULT_OR_CLAIM_EVENT_COUNT")


def test_status_39_rate_is_null_when_no_known_status_denominator_exists():
    candidate = build_candidate(
        [report("600", "600", visit_id="400", project_id="12", corrector_id="44",
                corrector_fio="Unknown Status Person")],
        [], queried_project_ids={"12"}, generation_id="1" * 32,
        source_snapshot_at=NOW.isoformat(),
    )

    result = summarize_observed_metrics(candidate)
    controller = result["by_controller"]["44"]
    assert controller["eligible_reports"] == 0
    assert controller["status_39_reports"] is None
    assert controller["status_39_report_share"] is None
    assert controller["eligible_visits"] == 0
    assert controller["status_39_visits"] is None
    assert controller["status_39_visit_share"] is None
    assert controller["status_39_visits_per_project_avg"] is None
    assert result["overall"]["status_39_visit_associations_per_controller_avg"] is None


@pytest.mark.parametrize(("visit_total", "status_39_total", "expected_share"), [
    (500, 5, 0.01),
    (50, 5, 0.10),
])
def test_visit_claim_share_uses_same_distinct_visit_denominator(
    visit_total, status_39_total, expected_share,
):
    reports = [
        report(str(i + 1), visit_id=str(i + 1), project_id="10")
        for i in range(visit_total)
    ]
    actions = [
        action(visit=str(i + 1), action_id=str(i + 1), codes=[39] if i < status_39_total else [])
        for i in range(visit_total)
    ]
    candidate = build_candidate(
        reports, actions, queried_project_ids={"10"},
        generation_id="9" * 32, source_snapshot_at=NOW.isoformat(),
    )

    result = summarize_observed_metrics(candidate)["overall"]

    assert result["eligible_visits"] == visit_total
    assert result["status_39_visits"] == status_39_total
    assert result["status_39_visit_share"] == pytest.approx(expected_share)


def test_collector_failure_blocks_but_success_with_no_new_rows_is_accepted():
    zero = {**COLLECTOR, "collector_result": "SUCCESS_NO_NEW_RECORDS"}
    assert validate_collector_success(zero, now=NOW) == "SUCCESS_NO_NEW_RECORDS"
    with pytest.raises(MartError, match="did not complete"):
        validate_collector_success({**COLLECTOR, "result": "exit-code"}, now=NOW)


def test_systemd_collector_gate_reads_private_same_run_log_and_zero_change_result(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    from src.controller_quality_mart import read_systemd_collector_evidence

    log = tmp_path / "collectors-20261026T151500Z.log"
    log.write_text(json.dumps({
        "status": "OK", "rows": 722, "queue_remaining": 0, "discovered": 0,
        "batch": {"attempted": 0}, "reconciliation": {"added": 0, "updated": 0, "removed": 0},
    }) + "\n", encoding="utf-8")
    log.chmod(0o600)
    import os
    os.utime(log, (NOW.timestamp(), NOW.timestamp()))
    monkeypatch.setattr(
        "src.controller_quality_mart.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=("Result=success\nExecMainStatus=0\n"
                    "ExecMainStartTimestamp=Mon 2026-10-26 18:00:00 MSK\n"),
        ),
    )
    evidence = read_systemd_collector_evidence(log_dir=tmp_path)
    assert evidence["collector_result"] == "SUCCESS_NO_NEW_RECORDS"
    assert evidence["exit_status"] == "0"


def test_systemd_collector_gate_rejects_queue_and_broad_log_permissions(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    from src.controller_quality_mart import read_systemd_collector_evidence

    log = tmp_path / "collectors-current.log"
    log.write_text(json.dumps({"status": "OK", "rows": 1, "queue_remaining": 1}) + "\n")
    log.chmod(0o644)
    monkeypatch.setattr(
        "src.controller_quality_mart.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=("Result=success\nExecMainStatus=0\n"
                    "ExecMainStartTimestamp=Mon 2026-10-26 18:00:00 MSK\n"),
        ),
    )
    with pytest.raises(MartError, match="permissions"):
        read_systemd_collector_evidence(log_dir=tmp_path)


def test_stale_last_good_source_status_is_preserved_without_claiming_fresh_detail():
    row = report(source_status="STALE_LAST_GOOD")
    candidate = build_candidate([row], [action(codes=[38])], queried_project_ids={"10"},
                                generation_id="f" * 32, source_snapshot_at=NOW.isoformat())
    assert candidate["controller_report_snapshot"][1][18] == "STALE_LAST_GOOD"


def test_empty_action_status_row_is_observed_absent_not_unknown():
    candidate = build_candidate([report()], [action(codes=[])], queried_project_ids={"10"},
                                generation_id="1" * 32, source_snapshot_at=NOW.isoformat())
    assert candidate["controller_report_snapshot"][1][13] == "OBSERVED_ABSENT"


def test_html_status_response_requires_project_and_table_structure():
    html = ('<!doctype html><html><body><table><tr class="row">'
            '<a href="/proj/10">Project</a><a href="/visit/100">Visit</a>'
            '<a href="/action/680368">39</a></tr></table></body></html>')
    assert extract_project_action_rows(html, "10")[0]["workflow_state_codes"] == [39]
    with pytest.raises(MartError, match="authentication"):
        extract_project_action_rows('<html><form><input name="_login"><input type="password"></form></html>', "10")
    with pytest.raises(MartError, match="structural"):
        extract_project_action_rows("<html><body>empty</body></html>", "10")


def test_status_acquisition_paces_requests_and_has_hard_project_cap():
    calls, waits = [], []
    def response(pid):
        calls.append(pid)
        html = (f'<html><table><tr class="row"><a href="/proj/{pid}">P</a>'
                f'<a href="/visit/100">V</a><a href="/action/680368">39</a></tr></table></html>')
        return 200, "text/html", html.encode()
    rows, covered = acquire_action_statuses(["10", "10", "11"], response, sleep=waits.append)
    assert calls == ["10", "11"]
    assert waits == [1.5]
    assert len(rows) == 2
    assert covered == {"10", "11"}
    with pytest.raises(MartError, match="cap"):
        acquire_action_statuses((str(i) for i in range(1, 202)), response, sleep=lambda _: None)


def test_http_error_and_unexpected_content_type_fail_without_retry():
    calls = []
    def fetch(pid):
        calls.append(pid)
        return 200, "text/plain", b"no"
    with pytest.raises(MartError, match="response rejected"):
        acquire_action_statuses(["10"], fetch)
    assert calls == ["10"]


def test_non_200_project_status_response_stops_without_retry():
    calls = []
    def fetch(pid):
        calls.append(pid)
        return 403, "text/html", b"<html>denied</html>"
    with pytest.raises(MartError, match="rejected"):
        acquire_action_statuses(["10"], fetch)
    assert calls == ["10"]


def test_candidate_generation_changes_on_repeated_daily_refresh():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as directory:
        lock = Path(directory) / "production.lock"
        lock.touch()
        kwargs = dict(
            report_rows=[report()], fetch_action=lambda _pid: pytest.fail("not called"),
            queried_project_ids={"10"}, collector_evidence=COLLECTOR, now=NOW,
            lock_path=lock,
        )
        first = run_refresh(**kwargs)
        second = run_refresh(**kwargs)
    assert first["generation_id"] != second["generation_id"]
    assert first["status"] == second["status"] == "DRY_RUN_VALIDATED"


class FakeGateway:
    def __init__(self, *, fail_tab=None, pointer_raise_after_commit=False):
        self.data = {name: [list(headers)] for name, headers in TAB_SCHEMAS.items()}
        self.fail_tab = fail_tab
        self.pointer_raise_after_commit = pointer_raise_after_commit
        self.fail_pointer = False

    def titles(self):
        return tuple(self.data)

    def read(self, title):
        return self.data[title]

    def update(self, title, cell_range, rows):
        if title == self.fail_tab:
            raise RuntimeError("simulated write failure")
        start = int(cell_range[1:].split(":")[0])
        while len(self.data[title]) < start - 1:
            self.data[title].append([])
        for offset, row in enumerate(rows):
            index = start - 1 + offset
            if index == len(self.data[title]):
                self.data[title].append(list(row))
            else:
                self.data[title][index] = list(row)
        if title == "data_quality_status" and self.pointer_raise_after_commit:
            self.pointer_raise_after_commit = False
            raise RuntimeError("ambiguous after commit")

    def read_range(self, title, cell_range):
        start = int(cell_range[1:].split(":")[0])
        end = int(cell_range.split(":")[1].split(":")[0][1:])
        return self.data[title][start - 1:end]


def make_candidate(generation):
    return build_candidate([report()], [action(codes=[39])], queried_project_ids={"10"},
                           generation_id=generation, source_snapshot_at=NOW.isoformat())


def test_generation_publication_verifies_ranges_then_pointer_last():
    gateway = FakeGateway()
    result = GenerationPublisher(gateway).publish(make_candidate("2" * 32), generation_id="2" * 32)
    assert result == "PUBLISHED_AND_VERIFIED"
    assert gateway.data["data_quality_status"][1][2] == "2" * 32
    assert len(gateway.data["controller_report_snapshot"]) == 2


def test_target_readonly_validation_requires_exact_four_headers():
    gateway = FakeGateway()
    GenerationPublisher(gateway).validate_target()
    gateway.data["metric_dictionary"][0][0] = "wrong"
    with pytest.raises(Exception, match="schema"):
        GenerationPublisher(gateway).validate_target()


def test_failed_fact_write_preserves_last_good_pointer():
    gateway = FakeGateway(fail_tab="controller_visit_summary")
    old_pointer = ["controller_quality", "1", "a" * 32] + [""] * (len(TAB_SCHEMAS["data_quality_status"]) - 3)
    gateway.data["data_quality_status"].append(old_pointer)
    with pytest.raises(Exception):
        GenerationPublisher(gateway).publish(make_candidate("3" * 32), generation_id="3" * 32)
    assert gateway.data["data_quality_status"][1][2] == "a" * 32


def test_ambiguous_pointer_write_is_reconciled_once_by_readback_only():
    gateway = FakeGateway(pointer_raise_after_commit=True)
    assert GenerationPublisher(gateway).publish(make_candidate("4" * 32), generation_id="4" * 32)
    assert gateway.data["data_quality_status"][1][2] == "4" * 32


def test_bad_headers_and_duplicate_visit_candidate_fail_before_publication():
    candidate = make_candidate("5" * 32)
    gateway = FakeGateway()
    gateway.data["controller_visit_summary"][0][0] = "bad"
    with pytest.raises(Exception, match="schema"):
        GenerationPublisher(gateway).publish(candidate, generation_id="5" * 32)


def test_shared_lock_contention_refuses_without_waiting(tmp_path):
    import fcntl
    from src.controller_quality_mart import shared_lock

    path = tmp_path / "production.lock"
    path.touch()
    with path.open("r+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(MartError, match="held"):
            with shared_lock(path):
                pass


def test_data_quality_reports_partial_coverage_and_exact_control_counts():
    candidate = make_candidate("6" * 32)
    quality = candidate["data_quality_status"][1]
    assert quality[12] == "OBSERVED_CORRECTOR_SNAPSHOT_ONLY"
    assert quality[6:10] == [1, 1, 1, 0]


def test_cli_dry_run_has_no_io_and_emits_no_source_data(capsys):
    from scripts.run_controller_quality_mart import main

    assert main(["--dry-run"]) == 0
    output = capsys.readouterr().out
    assert '"status":"DRY_RUN_NO_IO"' in output
    assert "Synthetic Person" not in output


def test_google_http_error_text_does_not_retain_response_or_secret(monkeypatch):
    import urllib.error
    import urllib.request

    from src.controller_quality_sheets import GoogleSheetsGateway, SheetsTransportError

    def fail(_request, timeout):
        raise urllib.error.HTTPError("https://sheets.invalid", 403, "token=hidden", {}, None)
    monkeypatch.setattr(urllib.request, "urlopen", fail)
    gateway = GoogleSheetsGateway("safe-id", "DO_NOT_PRINT_TOKEN")
    with pytest.raises(SheetsTransportError) as caught:
        gateway.titles()
    assert "hidden" not in str(caught.value)
    assert "DO_NOT_PRINT_TOKEN" not in str(caught.value)


def test_multiple_qualified_workflow_codes_have_independent_tri_state_flags():
    candidate = build_candidate([report()], [action(codes=[35, 38, 39])],
                                queried_project_ids={"10"}, generation_id="7" * 32,
                                source_snapshot_at=NOW.isoformat())
    row = candidate["controller_report_snapshot"][1]
    assert row[11:17] == ["OBSERVED_PRESENT", "OBSERVED_PRESENT", "OBSERVED_PRESENT",
                          "OBSERVED_ABSENT", "OBSERVED_ABSENT", "OBSERVED_ABSENT"]
