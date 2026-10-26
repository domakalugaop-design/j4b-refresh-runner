from __future__ import annotations

import pytest

from src.report_inventory import (
    ReportInventoryParseError,
    date_value_type,
    extract_report_inventory,
)

BASE = "https://lk.j4b.ru"

LISTING_WITH_ACTION_AND_VISIT = """
<html><body>
<table>
  <tr><th>Отчёт</th><th>Дата отчёта</th><th>Визит</th><th>Проверка</th></tr>
  <tr>
    <td><a href="/report/9001">открыть</a></td>
    <td>01.10.2026</td>
    <td><a href="/visit/555">визит</a></td>
    <td><a href="/action/777">проверка</a></td>
  </tr>
  <tr>
    <td><a href="/report/9002">открыть</a></td>
    <td>02.10.2026</td>
    <td><a href="/visit/556">визит</a></td>
    <td><a href="/action/778">проверка</a></td>
  </tr>
</table>
</body></html>
"""

LISTING_REPORT_ONLY = """
<html><body>
<table>
  <tr><th>Отчёт</th></tr>
  <tr><td><a href="/report/9001">открыть</a></td></tr>
  <tr><td><a href="/report/9002">открыть</a></td></tr>
</table>
</body></html>
"""


def test_extracts_report_ids_in_numeric_order():
    page = extract_report_inventory(LISTING_WITH_ACTION_AND_VISIT, base_url=BASE)
    assert page.report_ids == ("9001", "9002")
    assert page.tables == 1


def test_join_profile_reports_unresolved_links():
    resolved = extract_report_inventory(LISTING_WITH_ACTION_AND_VISIT, base_url=BASE)
    assert resolved.join_profile() == {
        "rows": 2,
        "rows_without_action": 0,
        "rows_without_visit": 0,
    }
    unresolved = extract_report_inventory(LISTING_REPORT_ONLY, base_url=BASE)
    assert unresolved.join_profile() == {
        "rows": 2,
        "rows_without_action": 2,
        "rows_without_visit": 2,
    }


def test_row_ids_are_exposed_only_when_unambiguous():
    page = extract_report_inventory(LISTING_WITH_ACTION_AND_VISIT, base_url=BASE)
    first = page.rows[0]
    assert first.report_id == "9001"
    assert first.single("action") == "777"
    assert first.single("visit") == "555"


def test_date_cells_are_classified_without_free_text():
    page = extract_report_inventory(LISTING_WITH_ACTION_AND_VISIT, base_url=BASE)
    first = page.rows[0]
    assert ("Дата отчёта", "DMY_DATE") in first.date_cells
    assert page.date_column_classes == {"REPORT": ("DMY_DATE",)}


def test_headers_are_captured_as_labels_only():
    page = extract_report_inventory(LISTING_WITH_ACTION_AND_VISIT, base_url=BASE)
    assert page.header_labels == ("Отчёт", "Дата отчёта", "Визит", "Проверка")


def test_ambiguous_report_links_fail_closed():
    html = """
    <table><tr><th>Отчёт</th></tr>
    <tr><td><a href="/report/1">a</a><a href="/report/2">b</a></td></tr></table>
    """
    with pytest.raises(ReportInventoryParseError):
        extract_report_inventory(html, base_url=BASE)


def test_empty_html_is_rejected():
    with pytest.raises(ReportInventoryParseError):
        extract_report_inventory("   ", base_url=BASE)


def test_external_links_are_ignored():
    html = """
    <table><tr><th>Отчёт</th></tr>
    <tr><td><a href="https://evil.invalid/report/5">x</a></td></tr></table>
    """
    page = extract_report_inventory(html, base_url=BASE)
    assert page.report_ids == ()


def test_duplicate_report_links_collapse_to_one_row():
    html = """
    <table><tr><th>Отчёт</th></tr>
    <tr><td><a href="/report/42">a</a><a href="/report/42/edit">b</a></td></tr></table>
    """
    page = extract_report_inventory(html, base_url=BASE)
    assert page.report_ids == ("42",)
    assert len(page.rows) == 1


def test_pagination_marker_is_detected():
    html = '<table><tr><td><a href="?page=2" rel="next">next</a></td></tr></table>'
    assert extract_report_inventory(html, base_url=BASE).pagination is True


@pytest.mark.parametrize(
    "value,expected",
    [
        ("01.10.2026", "DMY_DATE"),
        ("2026-10-01", "ISO_DATE"),
        ("01.10.2026 14:05", "DMY_DATETIME"),
        ("2026-10-01T14:05:00", "ISO_DATETIME"),
        ("", "EMPTY"),
        ("не дата", "OTHER"),
    ],
)
def test_date_value_type(value, expected):
    assert date_value_type(value) == expected
