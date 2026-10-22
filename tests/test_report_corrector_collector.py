from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone

from src.report_corrector_collector import (
    SHEET_COLUMNS,
    START_DATE,
    CollectorError,
    ReportDetail,
    collect_batch,
    extract_registry_report_ids,
    merge_candidates,
    new_state,
    parse_report_detail,
    pending_report_ids,
    render_sheet_rows,
    store_detail,
    store_failure,
    store_out_of_scope,
    validate_sheet_rows,
)

NOW = datetime(2026, 10, 22, 9, 0, tzinfo=timezone.utc)
RECHECK = timedelta(hours=6)


def action_html(
    report_id: str = "671045",
    *,
    action_id: str | None = None,
    report_link: str | None = None,
    visits: tuple[str, ...] = ("763369",),
    projects: tuple[str, ...] = ("7986",),
    dt: str | None = "01.10.2026",
    staff: str = '<select name="staff"><option value="">Нет</option>'
                 '<option value="56770" selected>Иванов Иван Иванович</option></select>',
) -> str:
    parts = ["<html><body>"]
    parts.append(f'<a href="/action/{action_id or report_id}">a</a>')
    parts.append(f'<a href="/report/{report_link or report_id}">r</a>')
    for visit in visits:
        parts.append(f'<a href="/visit/{visit}">v</a>')
    for project in projects:
        parts.append(f'<a href="/proj/{project}">p</a>')
    if dt is not None:
        parts.append(f'<input type="text" name="dt" value="{dt}">')
    parts.append(staff)
    parts.append("</body></html>")
    return "".join(parts)


def ready_state(*report_ids: str) -> dict:
    state = new_state()
    merge_candidates(state, report_ids, discovered_for="2026-10-01")
    return state


class RegistryExtractionTests(unittest.TestCase):
    def test_reads_and_deduplicates_report_ids(self) -> None:
        html = (
            '<a href="/report/671045">a</a><a href="/report/665134">b</a>'
            '<a href="/report/671045/pdf">c</a><a href="/report/665134">d</a>'
        )
        self.assertEqual(extract_registry_report_ids(html), ("665134", "671045"))

    def test_ignores_non_numeric_and_placeholder_links(self) -> None:
        html = (
            '<a href="/report/proj/7518">p</a><a href="/report/new">n</a>'
            '<a href="/report/txt">t</a><a href="/report/0">z</a>'
            '<a href="/proj/7986/report">x</a>'
        )
        self.assertEqual(extract_registry_report_ids(html), ())

    def test_sorts_numerically_not_lexically(self) -> None:
        html = '<a href="/report/900">a</a><a href="/report/1000">b</a>'
        self.assertEqual(extract_registry_report_ids(html), ("900", "1000"))

    def test_empty_page_fails_closed(self) -> None:
        with self.assertRaises(CollectorError):
            extract_registry_report_ids("   ")


class DetailParsingTests(unittest.TestCase):
    def test_reads_join_date_visit_project_and_corrector(self) -> None:
        detail = parse_report_detail(action_html(), "671045")
        self.assertEqual(detail.report_id, "671045")
        self.assertEqual(detail.visit_id, "763369")
        self.assertEqual(detail.project_id, "7986")
        self.assertEqual(detail.report_date, "2026-10-01")
        self.assertEqual(detail.corrector_state, "VALUE_PRESENT")
        self.assertEqual(detail.corrector_id, "56770")
        self.assertEqual(detail.corrector_fio, "Иванов Иван Иванович")

    def test_missing_corrector_is_recorded_as_empty(self) -> None:
        detail = parse_report_detail(
            action_html(staff='<select name="staff"><option value="">Нет</option></select>'),
            "671045",
        )
        self.assertEqual(detail.corrector_state, "EMPTY")
        self.assertIsNone(detail.corrector_id)

    def test_report_action_id_mismatch_is_rejected(self) -> None:
        with self.assertRaises(CollectorError):
            parse_report_detail(action_html(action_id="999999"), "671045")

    def test_foreign_report_link_is_rejected(self) -> None:
        with self.assertRaises(CollectorError):
            parse_report_detail(action_html(report_link="111111"), "671045")

    def test_ambiguous_visit_or_project_is_rejected(self) -> None:
        with self.assertRaises(CollectorError):
            parse_report_detail(action_html(visits=("1", "2")), "671045")
        with self.assertRaises(CollectorError):
            parse_report_detail(action_html(projects=("7986", "7987")), "671045")

    def test_missing_or_impossible_date_is_rejected(self) -> None:
        with self.assertRaises(CollectorError):
            parse_report_detail(action_html(dt=None), "671045")
        with self.assertRaises(CollectorError):
            parse_report_detail(action_html(dt="31.02.2026"), "671045")

    def test_unexposed_corrector_field_is_rejected(self) -> None:
        with self.assertRaises(CollectorError):
            parse_report_detail(action_html(staff=""), "671045")


class InventoryStateTests(unittest.TestCase):
    def test_merge_is_idempotent_and_rejects_bad_ids(self) -> None:
        state = new_state()
        self.assertEqual(merge_candidates(state, ["671045", "665134"], discovered_for="2026-10-01"), 2)
        self.assertEqual(merge_candidates(state, ["671045"], discovered_for="2026-10-02"), 0)
        self.assertEqual(state["reports"]["671045"]["discovered_for"], "2026-10-01")
        with self.assertRaises(CollectorError):
            merge_candidates(state, ["abc"], discovered_for="2026-10-01")

    def test_pending_prefers_unresolved_then_due_rechecks(self) -> None:
        state = ready_state("100", "200", "300")
        store_detail(state, parse_report_detail(action_html("100"), "100"), NOW, recheck_after=RECHECK)
        due = pending_report_ids(state, NOW + timedelta(hours=7), 5, recheck_after=RECHECK)
        self.assertIn("100", due)
        self.assertIn("200", due)
        fresh_only = pending_report_ids(state, NOW, 5, recheck_after=RECHECK)
        self.assertNotIn("100", fresh_only)
        self.assertEqual(fresh_only[:2], ["200", "300"])

    def test_recheck_respects_interval_and_limit(self) -> None:
        state = ready_state("100", "200")
        store_detail(state, parse_report_detail(action_html("100"), "100"), NOW, recheck_after=RECHECK)
        self.assertEqual(pending_report_ids(state, NOW + timedelta(hours=1), 5, recheck_after=RECHECK), ["200"])
        self.assertEqual(len(pending_report_ids(state, NOW, 1, recheck_after=RECHECK)), 1)

    def test_fresh_entries_appear_once(self) -> None:
        state = ready_state("100", "200")
        store_detail(state, parse_report_detail(action_html("100"), "100"), NOW, recheck_after=RECHECK)
        self.assertEqual(pending_report_ids(state, NOW, 5, recheck_after=RECHECK), ["200"])

    def test_rejects_naive_timestamps_and_bad_intervals(self) -> None:
        state = ready_state("100")
        with self.assertRaises(CollectorError):
            pending_report_ids(state, datetime(2026, 10, 22, 9, 0), 1, recheck_after=RECHECK)
        with self.assertRaises(CollectorError):
            pending_report_ids(state, NOW, 1, recheck_after=timedelta(0))

    def test_detail_stores_current_status_and_recheck_time(self) -> None:
        state = ready_state("671045")
        store_detail(state, parse_report_detail(action_html(), "671045"), NOW, recheck_after=RECHECK)
        entry = state["reports"]["671045"]
        self.assertEqual(entry["last_good"]["source_status"], "CURRENT")
        self.assertEqual(entry["last_attempt_status"], "PASS")
        self.assertEqual(entry["next_recheck_at"], (NOW + RECHECK).isoformat())

    def test_empty_corrector_maps_to_no_corrector_status(self) -> None:
        state = ready_state("671045")
        detail = parse_report_detail(
            action_html(staff='<select name="staff"><option value="">Нет</option></select>'),
            "671045",
        )
        store_detail(state, detail, NOW, recheck_after=RECHECK)
        self.assertEqual(state["reports"]["671045"]["last_good"]["source_status"], "NO_CORRECTOR")

    def test_failure_backoff_grows_and_is_capped(self) -> None:
        state = ready_state("671045")
        store_failure(state, "671045", "TRANSPORT_ERROR", NOW)
        first = datetime.fromisoformat(state["reports"]["671045"]["next_attempt_at"])
        self.assertEqual(first, NOW + timedelta(seconds=300))
        for _ in range(20):
            store_failure(state, "671045", "TRANSPORT_ERROR", NOW)
        entry = state["reports"]["671045"]
        self.assertEqual(entry["failure_count"], 21)
        capped = datetime.fromisoformat(entry["next_attempt_at"])
        self.assertEqual(capped, NOW + timedelta(seconds=86_400))

    def test_backoff_blocks_selection_until_due(self) -> None:
        state = ready_state("671045")
        store_failure(state, "671045", "HTTP_ERROR", NOW)
        self.assertEqual(pending_report_ids(state, NOW + timedelta(seconds=60), 5, recheck_after=RECHECK), [])
        self.assertEqual(pending_report_ids(state, NOW + timedelta(seconds=301), 5, recheck_after=RECHECK), ["671045"])

    def test_out_of_scope_is_terminal(self) -> None:
        state = ready_state("665134")
        store_out_of_scope(state, "665134", NOW)
        self.assertEqual(pending_report_ids(state, NOW + timedelta(days=30), 5, recheck_after=RECHECK), [])

    def test_merge_rejects_unknown_state_version(self) -> None:
        state = new_state()
        state["version"] = 2
        with self.assertRaises(CollectorError):
            merge_candidates(state, ["671045"], discovered_for="2026-10-01")


class BatchTests(unittest.TestCase):
    def test_collects_pages_and_persists_after_each_attempt(self) -> None:
        state = ready_state("671045", "665134", "680000")
        pages = {
            "671045": action_html("671045", dt="01.10.2026"),
            "665134": action_html("665134", dt="30.09.2026"),
            "680000": action_html("680000", dt="20.10.2026"),
        }
        saves: list[int] = []
        sleeps: list[float] = []
        counts = collect_batch(
            state, lambda rid: pages[rid], NOW, max_fetches=3, recheck_after=RECHECK,
            save=lambda payload: saves.append(len(payload["reports"])),
            request_interval_seconds=1.5, sleep=sleeps.append,
        )
        self.assertEqual(counts, {"attempted": 3, "passed": 2, "failed": 0, "below_start": 1})
        self.assertEqual(len(saves), 3)
        self.assertEqual(sleeps, [1.5, 1.5])
        self.assertEqual(state["reports"]["665134"]["last_attempt_status"], "DATE_OUT_OF_RANGE")
        self.assertIsNone(state["reports"]["665134"]["detail"])

    def test_fetch_exception_is_recorded_not_raised(self) -> None:
        state = ready_state("671045")

        def boom(_: str) -> str:
            raise RuntimeError("network down")

        counts = collect_batch(
            state, boom, NOW, max_fetches=1, recheck_after=RECHECK,
            save=lambda payload: None, request_interval_seconds=1.0, sleep=lambda _: None,
        )
        self.assertEqual(counts["failed"], 1)
        self.assertEqual(state["reports"]["671045"]["last_attempt_status"], "INTERNAL_ERROR")

    def test_batch_respects_budget(self) -> None:
        state = ready_state("1", "2", "3", "4")
        counts = collect_batch(
            state, lambda rid: action_html(rid), NOW, max_fetches=2, recheck_after=RECHECK,
            save=lambda payload: None, request_interval_seconds=1.0, sleep=lambda _: None,
        )
        self.assertEqual(counts["attempted"], 2)


class SheetRenderingTests(unittest.TestCase):
    def test_header_matches_contract(self) -> None:
        rows = render_sheet_rows(new_state())
        self.assertEqual(tuple(rows[0]), SHEET_COLUMNS)

    def test_publishes_only_resolved_october_rows(self) -> None:
        state = ready_state("671045", "665134", "680000")
        store_detail(state, parse_report_detail(action_html(), "671045"), NOW, recheck_after=RECHECK)
        store_out_of_scope(state, "665134", NOW)
        rows = render_sheet_rows(state)
        self.assertEqual([row[0] for row in rows[1:]], ["671045"])
        self.assertEqual(rows[1][1], "671045")
        self.assertEqual(rows[1][4], "2026-10-01")
        self.assertEqual(rows[1][7], "CURRENT")

    def test_failed_refresh_marks_stale_last_good(self) -> None:
        state = ready_state("671045")
        store_detail(state, parse_report_detail(action_html(), "671045"), NOW, recheck_after=RECHECK)
        store_failure(state, "671045", "TRANSPORT_ERROR", NOW + timedelta(hours=7))
        rows = render_sheet_rows(state)
        self.assertEqual(rows[1][7], "STALE_LAST_GOOD")
        self.assertEqual(rows[1][5], "56770")

    def test_failure_without_history_is_marked(self) -> None:
        state = ready_state("671045")
        state["reports"]["671045"].update({
            "detail": {"report_id": "671045", "report_date": "2026-10-05", "visit_id": "1", "project_id": "2"},
        })
        store_failure(state, "671045", "TRANSPORT_ERROR", NOW)
        rows = render_sheet_rows(state)
        self.assertEqual(rows[1][7], "FETCH_FAILED_NO_HISTORY")

    def test_row_validation_rejects_schema_and_duplicates(self) -> None:
        state = ready_state("671045")
        store_detail(state, parse_report_detail(action_html(), "671045"), NOW, recheck_after=RECHECK)
        rows = render_sheet_rows(state)
        validate_sheet_rows(rows)
        with self.assertRaises(CollectorError):
            validate_sheet_rows([["report_id"]])
        with self.assertRaises(CollectorError):
            validate_sheet_rows([list(SHEET_COLUMNS), rows[1], rows[1]])

    def test_row_validation_rejects_pre_start_date(self) -> None:
        row = ["671045", "671045", "763369", "7986", "2026-09-30", "", "", "CURRENT", ""]
        with self.assertRaises(CollectorError):
            validate_sheet_rows([list(SHEET_COLUMNS), row])

    def test_detail_validation_rejects_non_numeric_ids(self) -> None:
        with self.assertRaises(CollectorError):
            ReportDetail("671045", "x", "7986", "2026-10-01", "EMPTY").validated()

    def test_start_date_is_october_first(self) -> None:
        self.assertEqual(START_DATE, date(2026, 10, 1))


if __name__ == "__main__":
    unittest.main()
