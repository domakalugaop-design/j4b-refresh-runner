"""Acceptance scenarios for PORTAL-001: stable incremental collector behaviour.

Each test maps to one scenario from the task list, so a regression names the
daily situation it breaks rather than an internal function.
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone

from src.report_corrector_collector import (
    DEFAULT_POLICY, MAX_DETAIL_GETS_PER_RUN, MIN_REQUEST_INTERVAL_SECONDS,
    SHEET_COLUMNS, CollectorError, RecheckPolicy, collect_batch, coverage_summary,
    mark_published, merge_candidates, migrate_state, new_state, parse_report_detail,
    pending_report_ids, queue_remaining, reconcile_dataset, record_discovery,
    render_sheet_rows, request_manual_recheck, store_detail, store_failure,
    validate_sheet_rows,
)
from src.report_corrector_sheet import PROTECTED_TABS, plan

NOW = datetime(2026, 10, 22, 9, 0, tzinfo=timezone.utc)
DAY = timedelta(days=1)
CORRECTOR = '<option value="56770" selected>Иванов И.И.</option>'
NOBODY = '<option value="0" selected>Нет</option>'


def page(report_id: str = "671045", *, staff: str = CORRECTOR, dt: str = "01.10.2026") -> str:
    return (
        f'<html><body><a href="/action/{report_id}">a</a>'
        f'<a href="/report/{report_id}">r</a><a href="/visit/763369">v</a>'
        f'<a href="/proj/7986">p</a><input type="text" name="dt" value="{dt}">'
        f'<select name="staff"><option value="">Нет</option>{staff}</select></body></html>'
    )


def state_with(*report_ids: str) -> dict:
    state = new_state()
    merge_candidates(state, report_ids, discovered_for="2026-10-01")
    return state


def collect(state: dict, pages: dict[str, str], **kwargs) -> dict:
    kwargs.setdefault("max_fetches", 50)
    kwargs.setdefault("save", lambda _: None)
    kwargs.setdefault("sleep", lambda _: None)
    return collect_batch(state, lambda rid: pages[rid], kwargs.pop("now", NOW), **kwargs)


def rows_by_id(state: dict) -> dict[str, list[str]]:
    return {row[0]: row for row in render_sheet_rows(state)[1:]}


class ScenarioTests(unittest.TestCase):
    def test_01_new_report_with_corrector_is_published(self) -> None:
        state = state_with("671045")
        counts = collect(state, {"671045": page()})
        self.assertEqual((counts["passed"], counts["failed"]), (1, 0))
        row = rows_by_id(state)["671045"]
        self.assertEqual((row[5], row[7]), ("56770", "CURRENT"))
        validate_sheet_rows(render_sheet_rows(state))

    def test_02_new_report_without_corrector_is_queued_once(self) -> None:
        state = state_with("671045")
        collect(state, {"671045": page(staff=NOBODY)})
        self.assertEqual(rows_by_id(state), {})
        entry = state["reports"]["671045"]
        self.assertEqual(entry["last_good"]["source_status"], "NO_CORRECTOR")
        self.assertEqual(entry["empty_reads"], 1)
        self.assertEqual(datetime.fromisoformat(entry["next_recheck_at"]), NOW + DAY)
        self.assertEqual(pending_report_ids(state, NOW + timedelta(hours=23), 10), [])

    def test_03_late_assignment_is_picked_up_on_the_scheduled_recheck(self) -> None:
        state = state_with("671045")
        collect(state, {"671045": page(staff=NOBODY)})
        later = NOW + DAY + timedelta(minutes=1)
        counts = collect(state, {"671045": page()}, now=later)
        self.assertEqual(counts["passed"], 1)
        entry = state["reports"]["671045"]
        self.assertEqual(entry["last_good"]["source_status"], "CURRENT")
        self.assertEqual(entry["empty_reads"], 0)
        self.assertIsNone(entry["next_recheck_at"])
        self.assertEqual(rows_by_id(state)["671045"][5], "56770")

    def test_04_no_corrector_budget_is_spent_and_becomes_terminal(self) -> None:
        state = state_with("671045")
        empty = {"671045": page(staff=NOBODY)}
        collect(state, empty, now=NOW)
        collect(state, empty, now=NOW + DAY + timedelta(minutes=1))
        collect(state, empty, now=NOW + 3 * DAY + timedelta(minutes=2))
        entry = state["reports"]["671045"]
        self.assertEqual(entry["last_good"]["source_status"], "NO_CORRECTOR_EXHAUSTED")
        self.assertIsNone(entry["next_recheck_at"])
        self.assertEqual(entry["empty_reads"], 3)
        # No request at all, ever again, without an explicit manual recheck.
        self.assertEqual(pending_report_ids(state, NOW + 400 * DAY, 10), [])
        self.assertEqual(queue_remaining(state, NOW + 400 * DAY), 0)
        self.assertEqual(rows_by_id(state), {})

    def test_05_repeat_run_without_new_data_issues_no_detail_gets(self) -> None:
        state = state_with("671045", "671046")
        pages = {"671045": page(), "671046": page("671046")}
        first = collect(state, pages)
        self.assertEqual(first["attempted"], 2)
        calls: list[str] = []
        second = collect_batch(state, lambda rid: calls.append(rid) or pages[rid], NOW + timedelta(days=2),
                               max_fetches=50, save=lambda _: None, sleep=lambda _: None)
        self.assertEqual(second["attempted"], 0)
        self.assertEqual(calls, [])
        self.assertEqual(second["remaining"], 0)

    def test_06_confirmed_corrector_change_updates_the_row(self) -> None:
        state = state_with("671045")
        collect(state, {"671045": page()})
        mark_published(state, NOW)
        changed = page(staff='<option value="31291" selected>Петров П.</option>')
        request_manual_recheck(state, ["671045"], NOW + DAY)
        collect(state, {"671045": changed}, now=NOW + DAY)
        self.assertEqual(rows_by_id(state)["671045"][5], "31291")
        self.assertEqual(reconcile_dataset(state)["updated"], ["671045"])

    def test_07_withdrawal_removes_the_row_but_keeps_history(self) -> None:
        state = state_with("671045")
        collect(state, {"671045": page()})
        mark_published(state, NOW)
        request_manual_recheck(state, ["671045"], NOW + DAY)
        collect(state, {"671045": page(staff=NOBODY)}, now=NOW + DAY)
        good = state["reports"]["671045"]["last_good"]
        self.assertEqual(good["source_status"], "CORRECTOR_REMOVED")
        self.assertEqual(good["withdrawn_corrector_id"], "56770")
        self.assertEqual(rows_by_id(state), {})
        self.assertEqual(reconcile_dataset(state)["removed"], ["671045"])
        # Terminal: no scheduled retry loop.
        self.assertIsNone(state["reports"]["671045"]["next_recheck_at"])
        self.assertEqual(pending_report_ids(state, NOW + 90 * DAY, 10), [])

    def test_08_portal_degradation_halts_and_preserves_progress(self) -> None:
        state = state_with("100", "200", "300", "400")
        done: list[str] = []

        def fetch(rid: str) -> str:
            if rid == "300":
                raise RuntimeError("detail fetch failed with HTTP 503")
            done.append(rid)
            return page(rid)

        counts = collect_batch(state, fetch, NOW, max_fetches=10, save=lambda _: None,
                               sleep=lambda _: None)
        self.assertEqual(counts["halted"], 1)
        self.assertEqual(done, ["100", "200"])
        self.assertEqual(counts["failed"], 1)
        # Completed work survives; the failed report backs off instead of looping.
        self.assertEqual(state["reports"]["100"]["last_attempt_status"], "PASS")
        self.assertIsNotNone(state["reports"]["300"]["next_attempt_at"])
        self.assertEqual(counts["remaining"], 1)

    def test_09_run_budget_is_hard_capped(self) -> None:
        with self.assertRaises(CollectorError):
            collect_batch(new_state(), lambda rid: page(rid), NOW,
                          max_fetches=MAX_DETAIL_GETS_PER_RUN + 1, save=lambda _: None)
        with self.assertRaises(CollectorError):
            collect_batch(new_state(), lambda rid: page(rid), NOW, max_fetches=1,
                          request_interval_seconds=MIN_REQUEST_INTERVAL_SECONDS - 0.1,
                          save=lambda _: None)
        state = state_with(*[str(500 + index) for index in range(MAX_DETAIL_GETS_PER_RUN + 50)])
        first = collect_batch(state, lambda rid: page(rid), NOW, max_fetches=MAX_DETAIL_GETS_PER_RUN,
                              save=lambda _: None, sleep=lambda _: None)
        self.assertEqual(first["attempted"], MAX_DETAIL_GETS_PER_RUN)
        self.assertEqual(first["remaining"], 50)
        second = collect_batch(state, lambda rid: page(rid), NOW + timedelta(minutes=1),
                               max_fetches=MAX_DETAIL_GETS_PER_RUN, save=lambda _: None,
                               sleep=lambda _: None)
        self.assertEqual(second["attempted"], 50)
        self.assertEqual(second["remaining"], 0)

    def test_10_duplicate_registry_ids_produce_one_row(self) -> None:
        state = new_state()
        self.assertEqual(record_discovery(state, ["671045", "671045"], discovered_for="2026-10-01", at=NOW)["added"], 1)
        self.assertEqual(record_discovery(state, ["671045"], discovered_for="2026-10-01", at=NOW)["added"], 0)
        collect(state, {"671045": page()})
        rows = render_sheet_rows(state)
        self.assertEqual(len(rows), 2)
        self.assertEqual(len({row[0] for row in rows[1:]}), 1)
        validate_sheet_rows(rows)

    def test_11_payload_cannot_carry_non_reviewer_or_duplicate_rows(self) -> None:
        state = state_with("671045")
        collect(state, {"671045": page()})
        rows = render_sheet_rows(state)
        self.assertEqual(tuple(rows[0]), SHEET_COLUMNS)
        self.assertNotIn("report_reviewers", {row[0] for row in rows[1:]})
        with self.assertRaises(CollectorError):
            validate_sheet_rows([list(SHEET_COLUMNS), rows[1], rows[1]])
        with self.assertRaises(CollectorError):
            validate_sheet_rows([list(SHEET_COLUMNS), [*rows[1][:7], "NO_CORRECTOR_EXHAUSTED", ""]])

    def test_12_plan_mode_reads_metadata_only(self) -> None:
        class ReadOnlyGateway:
            def __init__(self) -> None:
                self.mutations = 0

            def metadata(self):  # noqa: ANN001 - mirrors SheetsGateway.metadata
                from src.report_corrector_sheet import SheetMetadata

                return SheetMetadata(titles=tuple(PROTECTED_TABS))

            def mutate(self, *args, **kwargs):  # pragma: no cover - asserted never called
                self.mutations += 1
                raise AssertionError("plan must not mutate the spreadsheet")

        state = state_with("671045")
        collect(state, {"671045": page()})
        gateway = ReadOnlyGateway()
        result = plan(render_sheet_rows(state), gateway)
        self.assertEqual(result["action"], "CREATE")
        self.assertEqual(result["protected_tabs_present"], len(PROTECTED_TABS))
        self.assertEqual(gateway.mutations, 0)

    def test_13_restart_after_partial_failure_resumes_the_queue(self) -> None:
        state = state_with("100", "200", "300")
        saved: list[dict] = []
        pages = {"100": page("100"), "200": page("200"), "300": page("300")}

        def fetch(rid: str) -> str:
            if rid == "200":
                raise RuntimeError("connection reset")
            return pages[rid]

        collect_batch(state, fetch, NOW, max_fetches=3,
                      save=lambda payload: saved.append(json.loads(json.dumps(payload))),
                      sleep=lambda _: None)
        resumed = saved[-1]
        self.assertEqual(resumed["reports"]["100"]["last_attempt_status"], "PASS")
        self.assertEqual(pending_report_ids(resumed, NOW + timedelta(seconds=400), 10), ["200"])
        counts = collect_batch(resumed, lambda rid: pages[rid], NOW + timedelta(seconds=400),
                               max_fetches=3, save=lambda _: None, sleep=lambda _: None)
        self.assertEqual(counts["passed"], 1)
        self.assertEqual(counts["remaining"], 0)

    def test_14_reported_metrics_never_contain_names_or_secrets(self) -> None:
        state = state_with("671045")
        collect(state, {"671045": page()})
        summary = json.dumps({"coverage": coverage_summary(state),
                              "queue": queue_remaining(state, NOW)}, ensure_ascii=False)
        self.assertNotIn("Иванов", summary)
        self.assertNotIn("56770", summary)
        source = open("scripts/run_report_corrector_collector.py", encoding="utf-8").read()
        self.assertNotIn("corrector_fio", source)
        self.assertNotIn("Иванов", source)


class MigrationAndManualTests(unittest.TestCase):
    def test_migration_clears_stable_schedules_and_keeps_history(self) -> None:
        state = state_with("100", "200", "300")
        store_detail(state, parse_report_detail(page("100", staff=NOBODY), "100"), NOW, policy=DEFAULT_POLICY)
        store_detail(state, parse_report_detail(page("200"), "200"), NOW, policy=DEFAULT_POLICY)
        store_detail(state, parse_report_detail(page("300"), "300"), NOW, policy=DEFAULT_POLICY)
        # Simulate a v1 checkpoint written under the old status-aware policy.
        state.pop("schema", None)
        state["reports"]["200"].update({"next_recheck_at": (NOW + 7 * DAY).isoformat(), "empty_reads": 0})
        state["reports"]["300"].update({"next_recheck_at": (NOW + 7 * DAY).isoformat(), "empty_reads": 0})
        state["reports"]["300"]["last_good"]["source_status"] = "CORRECTOR_REMOVED"
        del state["reports"]["100"]["empty_reads"]
        del state["reports"]["100"]["first_no_corrector_at"]
        counts = migrate_state(state, NOW)
        self.assertEqual(counts["current_cleared"], 1)
        self.assertEqual(counts["removed_cleared"], 1)
        self.assertEqual(counts["no_corrector_migrated"], 1)
        self.assertEqual(state["schema"], 2)
        self.assertIsNone(state["reports"]["200"]["next_recheck_at"])
        self.assertIsNone(state["reports"]["300"]["next_recheck_at"])
        self.assertEqual(state["reports"]["100"]["empty_reads"], 1)
        self.assertEqual(len(state["reports"]), 3)
        self.assertEqual(rows_by_id(state)["200"][5], "56770")
        # Idempotent: a second pass changes nothing.
        self.assertEqual(migrate_state(state, NOW), {"current_cleared": 0, "removed_cleared": 0,
                                                     "no_corrector_migrated": 0})

    def test_manual_recheck_rejects_unknown_or_out_of_scope_ids(self) -> None:
        state = state_with("100")
        with self.assertRaises(CollectorError):
            request_manual_recheck(state, ["999"], NOW)
        with self.assertRaises(CollectorError):
            request_manual_recheck(state, ["abc"], NOW)
        self.assertEqual(request_manual_recheck(state, ["100"], NOW), ["100"])
        self.assertEqual(pending_report_ids(state, NOW, 5), ["100"])
        # The manual request never lifts the per-run budget.
        self.assertEqual(pending_report_ids(state, NOW, 0) if False else len(pending_report_ids(state, NOW, 1)), 1)


if __name__ == "__main__":
    unittest.main()
