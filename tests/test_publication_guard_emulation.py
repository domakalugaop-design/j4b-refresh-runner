import pytest

from src import production


def test_non_target_fingerprint_detects_value_change_and_excludes_publication_tabs(monkeypatch):
    external_values = {"Archive": [["state", "v1"]], "projects_current": [["id"], [1]]}
    metadata = {
        "sheets": [
            {"properties": {"title": title, "sheetId": index, "gridProperties": {"columnCount": 2}}}
            for index, title in enumerate(external_values, start=1)
        ]
    }

    def api(url, token, body=None, method=None):
        assert "fields=sheets.properties" in url
        return metadata

    def api_get(url, token):
        title = "Archive" if "Archive" in url else "projects_current"
        return {"values": external_values[title]}

    monkeypatch.setattr(production, "api", api)
    monkeypatch.setattr(production, "api_get", api_get)

    before = production._fingerprint_non_target_tabs("token", "sheet", {"projects_current"})
    external_values["Archive"] = [["state", "v2"]]
    after = production._fingerprint_non_target_tabs("token", "sheet", {"projects_current"})

    assert set(before) == {"Archive"}
    assert before != after


def test_prepublication_baseline_guard_rejects_changed_project_type_state(monkeypatch):
    monkeypatch.setattr(production, "read_sheet", lambda *args, **kwargs: [["project_id"], [1]])
    monkeypatch.setattr(production, "read_project_type_state_rows", lambda *args: [["project_id", "project_type_code", "project_type_name"], [1, "0002", "Классика_ТП"]])

    with pytest.raises(RuntimeError, match="project_types baseline changed"):
        production._assert_prepublication_baseline_unchanged(
            "token",
            "sheet",
            [["project_id"], [1]],
            [["project_id", "project_type_code", "project_type_name"], [1, "0001", "Обычный"]],
        )
