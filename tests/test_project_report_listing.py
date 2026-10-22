from src.project_report_listing import inspect_project_page
import json


def test_project_page_inventory_extracts_link_mappings_and_date_shape_only():
    html = """<!doctype html><html><body>
      <table><tr><th>Дата визита</th><th>Статус</th></tr>
      <tr><td>03.10.2026</td><td><a href='/visit/91001'>PRIVATE-FIO</a>
      <a href='/action/92001'>PRIVATE-COMMENT</a><a href='/proj/93001'>PROJECT</a></td></tr>
      <tr><td></td><td><a href='/visit/91002'>x</a></td></tr></table>
      <form action='/action'><input value='secret'></form>
    </body></html>"""
    result = inspect_project_page(html)

    assert result["tables"] == 1
    assert result["rows"] == 3
    assert result["visit_ids_count"] == 2
    assert result["action_ids_count"] == 1
    assert result["project_ids_count"] == 1
    assert result["report_ids_count"] == 0
    assert result["rows_with_visit_and_action"] == 1
    assert result["date_column_classes"] == {
        "VISIT_CANDIDATE": ("DMY_DATE", "EMPTY")
    }
    assert "91001" not in str(result)
    assert "PRIVATE" not in str(result)


def test_project_page_does_not_trust_external_routes_or_emit_query_values():
    html = """<a href='https://evil.example/report/123?token=secret'>x</a>
    <a href='/report/456?name=private'>x</a>"""
    result = inspect_project_page(html)

    assert result["report_ids_count"] == 1
    assert "evil.example" not in str(result)
    assert "token" not in str(result)
    assert "private" not in str(result)


def test_large_html_produces_small_aggregate_json():
    rows = "".join(
        f"<tr><td>2026-10-01</td><td><a href='/action/{i}'>PRIVATE-FIO-{i}</a>"
        f"<a href='/visit/{i}'>PRIVATE-COMMENT-{i}</a></td></tr>"
        for i in range(25000)
    )
    html = "<table><tr><th>Дата отчёта</th><th>Результат</th></tr>" + rows + "</table>"
    result = inspect_project_page(html)
    rendered = json.dumps(result, ensure_ascii=True, separators=(",", ":"))

    assert result["data_rows"] == 25000
    assert result["action_ids_count"] == 25000
    assert result["visit_ids_count"] == 25000
    assert len(rendered) < 2000
    assert "PRIVATE" not in rendered
    assert "FIO" not in rendered
