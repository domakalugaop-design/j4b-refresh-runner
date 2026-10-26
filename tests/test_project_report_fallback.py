from __future__ import annotations

import unittest
from contextlib import redirect_stdout
from io import StringIO

from scripts.run_report_corrector_collector import fetch_project_report_supplement, main
from src.report_corrector_collector import (
    MAX_DETAIL_GETS_PER_RUN,
    CollectorError,
    bounded_project_report_candidates,
)
from src.report_inventory import ReportInventoryPage, ReportInventoryRow


class FakePortalSession:
    def __init__(self, body: str, *, status: int = 200, content_type: str = "text/html") -> None:
        self.body = body.encode("utf-8")
        self.status = status
        self.content_type = content_type
        self.requests = 1
        self.calls: list[tuple[str, str, object]] = []

    def request(self, path: str, method: str, data=None, *, accept="text/html"):
        self.calls.append((path, method, data))
        self.requests += 1
        return self.status, self.content_type, self.body


PROJECT_REPORT_HTML = (
    "<html><body><table>"
    "<tr><th>Report</th><th>Project</th><th>Date</th></tr>"
    '<tr><td><a href="/report/100">Report</a></td>'
    '<td><a href="/proj/900">Project</a></td><td>17.10.2026</td></tr>'
    '<tr><td><a href="/report/101">Report</a></td>'
    '<td><a href="/proj/900">Project</a></td><td>18.10.2026</td></tr>'
    "</table></body></html>"
)


def project_page(
    report_ids: tuple[str, ...] = ("100", "101"),
    *,
    project_id: str = "900",
    pagination: bool = False,
    page_project_ids: tuple[str, ...] | None = None,
) -> ReportInventoryPage:
    rows = tuple(
        ReportInventoryRow(
            report_id=report_id,
            project_id=project_id,
            ids={"report": (report_id,), "proj": (project_id,)},
            date_cells=(),
        )
        for report_id in report_ids
    )
    return ReportInventoryPage(
        rows=rows,
        tables=1,
        header_labels=(),
        date_column_classes={},
        pagination=pagination,
        project_ids=page_project_ids if page_project_ids is not None else (project_id,),
    )


class ProjectReportFallbackTests(unittest.TestCase):
    def test_live_fallback_path_uses_one_read_only_project_get_and_respects_pacing(self) -> None:
        session = FakePortalSession(PROJECT_REPORT_HTML)
        sleeps: list[float] = []
        page, candidates = fetch_project_report_supplement(
            session, "900", {"101"}, max_candidates=2,
            interval=1.5, sleep=sleeps.append,
        )
        self.assertEqual(candidates, ("100",))
        self.assertEqual(len(page.report_ids), 2)
        self.assertEqual(session.calls, [("/proj/900/report", "GET", None)])
        self.assertEqual(sleeps, [1.5])

    def test_live_fallback_path_refuses_a_paginated_page(self) -> None:
        session = FakePortalSession(
            PROJECT_REPORT_HTML.replace(
                "</body>", '<a rel="next" href="?page=2">Next</a></body>',
            ),
        )
        with self.assertRaisesRegex(CollectorError, "paginated"):
            fetch_project_report_supplement(
                session, "900", set(), max_candidates=2, sleep=lambda _: None,
            )
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(session.calls[0][1], "GET")

    def test_live_fallback_path_fails_closed_on_non_html_response(self) -> None:
        session = FakePortalSession("not html", status=500, content_type="text/plain")
        with self.assertRaisesRegex(CollectorError, "did not return an HTML page"):
            fetch_project_report_supplement(
                session, "900", set(), max_candidates=2, sleep=lambda _: None,
            )
        self.assertEqual(session.calls[0][1], "GET")

    def test_explicit_project_listing_recovers_an_id_missing_from_registry(self) -> None:
        registry_ids = {"101"}
        page = project_page(("100", "101"))
        result = bounded_project_report_candidates(
            page, "900", registry_ids, max_candidates=2,
        )
        self.assertEqual(result, ("100",))
        self.assertEqual(registry_ids, {"101"})

    def test_returns_only_new_distinct_ids_in_numeric_order(self) -> None:
        page = project_page(("101", "100", "101"))
        self.assertEqual(
            bounded_project_report_candidates(
                page, "900", {"101"}, max_candidates=2,
            ),
            ("100",),
        )

    def test_multiple_reports_for_one_visit_are_not_collapsed(self) -> None:
        page = ReportInventoryPage(
            rows=(
                ReportInventoryRow("100", "900", {"report": ("100",), "visit": ("700",)}, ()),
                ReportInventoryRow("101", "900", {"report": ("101",), "visit": ("700",)}, ()),
            ),
            tables=1,
            header_labels=(),
            date_column_classes={},
            pagination=False,
            project_ids=("900",),
        )
        self.assertEqual(
            bounded_project_report_candidates(page, "900", set(), max_candidates=2),
            ("100", "101"),
        )

    def test_existing_candidates_are_idempotent(self) -> None:
        page = project_page(("100", "101"))
        self.assertEqual(
            bounded_project_report_candidates(
                page, "900", {"100", "101"}, max_candidates=2,
            ),
            (),
        )

    def test_rejects_pagination_instead_of_accepting_a_partial_page(self) -> None:
        with self.assertRaisesRegex(CollectorError, "paginated"):
            bounded_project_report_candidates(
                project_page(pagination=True), "900", set(), max_candidates=2,
            )

    def test_rejects_project_metadata_mismatch(self) -> None:
        with self.assertRaisesRegex(CollectorError, "different project"):
            bounded_project_report_candidates(
                project_page(page_project_ids=("901",)), "900", set(), max_candidates=2,
            )

    def test_rejects_missing_project_scope_metadata(self) -> None:
        with self.assertRaisesRegex(CollectorError, "different project"):
            bounded_project_report_candidates(
                project_page(page_project_ids=()), "900", set(), max_candidates=2,
            )

    def test_rejects_a_non_table_response(self) -> None:
        page = project_page()
        page = ReportInventoryPage(
            rows=page.rows, tables=0, header_labels=page.header_labels,
            date_column_classes=page.date_column_classes, pagination=False,
            project_ids=page.project_ids,
        )
        with self.assertRaisesRegex(CollectorError, "has no table"):
            bounded_project_report_candidates(page, "900", set(), max_candidates=2)

    def test_rejects_foreign_project_row(self) -> None:
        page = project_page()
        foreign = ReportInventoryRow(
            report_id="102", project_id="901",
            ids={"report": ("102",), "proj": ("901",)}, date_cells=(),
        )
        page = ReportInventoryPage(
            rows=(*page.rows, foreign), tables=page.tables,
            header_labels=page.header_labels, date_column_classes=page.date_column_classes,
            pagination=False, project_ids=("900",),
        )
        with self.assertRaisesRegex(CollectorError, "different project"):
            bounded_project_report_candidates(page, "900", set(), max_candidates=3)

    def test_refuses_candidate_count_above_remaining_detail_budget(self) -> None:
        with self.assertRaisesRegex(CollectorError, "bounded candidate limit"):
            bounded_project_report_candidates(
                project_page(("100", "101", "102")), "900", set(), max_candidates=2,
            )

    def test_refuses_budget_above_global_detail_cap(self) -> None:
        with self.assertRaisesRegex(CollectorError, "outside the detail GET cap"):
            bounded_project_report_candidates(
                project_page(), "900", set(), max_candidates=MAX_DETAIL_GETS_PER_RUN + 1,
            )

    def test_refuses_invalid_project_id(self) -> None:
        with self.assertRaisesRegex(CollectorError, "invalid project report fallback project_id"):
            bounded_project_report_candidates(
                project_page(), "../900", set(), max_candidates=2,
            )

    def test_cli_refuses_project_supplement_with_skip_discovery_before_io(self) -> None:
        output = StringIO()
        with redirect_stdout(output):
            code = main([
                "--state", "/tmp/collector-state-must-not-be-read.json",
                "--skip-discovery", "--supplement-project-report", "900",
            ])
        self.assertEqual(code, 2)
        self.assertIn("REFUSED_PROJECT_SUPPLEMENT_WITH_SKIP_DISCOVERY", output.getvalue())

    def test_cli_refuses_more_than_one_project_before_io(self) -> None:
        output = StringIO()
        with redirect_stdout(output):
            code = main([
                "--state", "/tmp/collector-state-must-not-be-read.json",
                "--supplement-project-report", "900",
                "--supplement-project-report", "901",
            ])
        self.assertEqual(code, 2)
        self.assertIn("REFUSED_TOO_MANY_PROJECT_SUPPLEMENTS", output.getvalue())

    def test_cli_refuses_mixing_project_supplement_with_manual_recheck(self) -> None:
        output = StringIO()
        with redirect_stdout(output):
            code = main([
                "--state", "/tmp/collector-state-must-not-be-read.json",
                "--supplement-project-report", "900", "--recheck", "100",
            ])
        self.assertEqual(code, 2)
        self.assertIn("REFUSED_PROJECT_SUPPLEMENT_WITH_MANUAL_RECHECK", output.getvalue())


if __name__ == "__main__":
    unittest.main()
