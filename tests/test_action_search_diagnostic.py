from src.action_search_diagnostic import (
    analyze_action_response,
    render_comparison,
    render_diagnostic,
    summarize_action_response,
)


def _summary(body: bytes, *, limit: int = 10_000):
    return summarize_action_response(
        http_status=200,
        content_type="text/html; charset=utf-8",
        body=body,
        final_url="https://lk.j4b.ru/action",
        expected_host="lk.j4b.ru",
        limit=limit,
    )


def test_extracts_safe_structure_and_confirmed_report_date_metadata():
    body = (
        b'<form><input name="limit" value="10000"></form>'
        b'<p>Total: 1</p><table><tr><th>Report date</th><th>Visit</th></tr>'
        b'<tr><td>2026-10-01</td><td><a href="/visit/991234567">PRIVATE-FIO</a>'
        b'<a href="/action/991234568">PRIVATE</a><a href="/proj/991234569">PRIVATE</a></td></tr></table>'
    )
    summary = _summary(body)
    rendered = render_diagnostic(summary)

    assert summary.html_parsed
    assert summary.result_tables == 1
    assert summary.result_rows == 1
    assert summary.distinct_action_ids == 1
    assert summary.distinct_visit_ids == 1
    assert summary.distinct_project_ids == 1
    assert summary.date_columns == ("Report date",)
    assert summary.date_data_types == ("ISO_DATE",)
    assert summary.date_semantics == "REPORT_DATE"
    assert summary.date_min == summary.date_max == "2026-10-01 00:00:00"
    assert summary.explicit_total_count == 1
    assert summary.configured_result_limit == 10_000
    assert not summary.limit_reached
    assert all(secret not in rendered for secret in ("PRIVATE", "991234567", "991234568", "991234569"))


def test_large_html_has_bounded_output_and_hashed_ids_only():
    row = b'<tr><td>SECRET-FIO-DO-NOT-EMIT</td><td><a href="/visit/41">x</a></td></tr>'
    body = b'<table><tr><th>Visit</th></tr>' + row * 100_000 + b'</table>'
    summary = _summary(body)
    rendered = render_diagnostic(summary)

    assert summary.response_bytes == len(body)
    assert summary.result_rows == 100_000
    assert summary.distinct_visit_ids == 1
    assert len(summary.visit_ids_sha256) == 64
    assert len(rendered) <= 2_000
    assert "SECRET" not in rendered
    assert "visit/41" not in rendered


def test_limit_and_pagination_indicators_are_detected():
    body = (
        b'<input name="limit" value="10000"><table><tr><th>Date</th></tr>'
        b'<tr><td>text</td><a href="/visit/1">x</a></tr>' * 2
        + b'<a rel="next" href="/action?page=2">next</a><p>'
        + "Всего: 25000".encode()
        + b"</p>"
    )
    summary = _summary(body, limit=2)

    assert summary.result_rows == 2
    assert summary.limit_reached
    assert summary.pagination == "YES"
    assert summary.explicit_total_count == 25_000


def _analysis(rows: tuple[tuple[str, str], ...]):
    markup = "<table>" + "".join(
        f'<tr><td><a href="/action/{action}">a</a>'
        f'<a href="/visit/{visit}">v</a><a href="/proj/77">p</a></td></tr>'
        for action, visit in rows
    ) + "</table>"
    return analyze_action_response(
        http_status=200, content_type="text/html", body=markup.encode(),
        final_url="https://lk.j4b.ru/action", expected_host="lk.j4b.ru",
    )


def test_comparison_calculates_overlap_from_private_id_fingerprints():
    first = _analysis((("123456", "111111"), ("234567", "222222")))
    second = _analysis((("234567", "222222"), ("345678", "333333")))
    rendered = render_comparison(first, second)
    assert '"action_intersection":1' in rendered
    assert '"action_only_day1":1' in rendered
    assert '"action_only_day2":1' in rendered
    assert '"action_subset_relation":"OVERLAPPING_NON_NESTED"' in rendered
    assert '"action_jaccard":0.333333' in rendered
    assert '"visit_intersection":1' in rendered
    assert "123456" not in rendered and "234567" not in rendered
    assert "111111" not in rendered and "222222" not in rendered
    assert len(rendered) <= 2_000


def test_comparison_detects_subset_and_disjoint_daily_shapes():
    day1 = _analysis((("123456", "111111"),))
    superset = _analysis((("123456", "111111"), ("234567", "222222")))
    disjoint = _analysis((("999999", "888888"),))
    assert '"action_subset_relation":"DAY1_SUBSET_OF_DAY2"' in render_comparison(day1, superset)
    assert '"date_filter_behavior":"DISJOINT_DAILY_SLICES"' in render_comparison(day1, disjoint)


def test_date_visit_semantics_does_not_claim_report_date():
    body = (
        '<table><tr><th>Дата визита</th></tr>'
        '<tr><td>01.10.2026</td></tr></table>'
    ).encode()
    summary = _summary(body)

    assert summary.date_semantics == "VISIT_DATE"
    assert summary.date_data_types == ("DMY_DATE",)
    assert summary.date_min == "2026-10-01 00:00:00"
    assert summary.date_min != summary.date_max or summary.date_semantics != "REPORT_DATE"


def test_unverified_date_heading_has_no_min_max():
    summary = _summary(
        b'<table><tr><th>Date</th></tr><tr><td>2026-10-01</td></tr></table>'
    )
    assert summary.date_semantics == "UNCONFIRMED"
    assert summary.date_min == summary.date_max == "UNKNOWN"


def test_non_html_and_empty_responses_are_sanitized():
    empty = _summary(b"")
    non_html = summarize_action_response(
        http_status=200, content_type="application/json", body=b'{"secret":"value"}',
        final_url="https://lk.j4b.ru/action", expected_host="lk.j4b.ru",
    )
    assert empty.failure_category == "EMPTY_RESPONSE"
    assert non_html.failure_category == "NON_HTML_RESPONSE"
    assert "secret" not in render_diagnostic(non_html)


def test_auth_redirect_is_classified_and_not_parsed_as_search_result():
    summary = summarize_action_response(
        http_status=200, content_type="text/html", body=b"<html>private</html>",
        final_url="https://lk.j4b.ru/login", expected_host="lk.j4b.ru",
    )
    assert summary.final_url_class == "AUTH"
    assert summary.redirect_detected == "YES"
    assert summary.failure_category == "AUTH_REDIRECT"
    assert summary.result_rows == 0


def test_non_200_response_stops_before_html_parse():
    summary = summarize_action_response(
        http_status=500, content_type="text/html", body=b"<table><tr><td>x</td></tr></table>",
        final_url="https://lk.j4b.ru/action", expected_host="lk.j4b.ru",
    )
    assert summary.failure_category == "HTTP_ERROR"
    assert not summary.html_parsed
    assert summary.result_rows == 0


def test_diagnostic_emits_no_stdout_or_stderr(capsys):
    render_diagnostic(_summary(b"<table><tr><td>PRIVATE-FIO</td></tr></table>"))
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
