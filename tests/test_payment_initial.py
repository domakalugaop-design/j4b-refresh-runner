import json
from unittest.mock import patch

import pytest

from src.google_service_account import _service_account_info
from src.payment_initial import _keychain_credential, materialize_complete_checkpoint
from src.payment_materialization import PROJECT_PUBLICATION_COLUMNS, VISIT_PUBLICATION_COLUMNS
from src.payment_refresh import load_checkpoint, new_checkpoint, record_result, save_checkpoint


def test_initial_checkpoint_reconstructs_zero_row_project_without_network(tmp_path):
    checkpoint = new_checkpoint(["10"], mode="INITIAL_2026", year=2026)
    record_result(checkpoint, "10", "SUCCESS_ZERO_ROWS", [], {
        "project": {"project_name": {"value": "Project_2026"}, "client": {"value": "Client"}},
        "workflow_memberships": [],
    })
    saved = save_checkpoint(tmp_path / "private" / "checkpoint.json", checkpoint)
    restored = load_checkpoint(saved["path"], saved["sha256"])
    payload = materialize_complete_checkpoint(restored, expected_total=1)
    assert payload["Выплаты по визитам"] == [list(VISIT_PUBLICATION_COLUMNS)]
    assert payload["Выплаты по проектам"][0] == list(PROJECT_PUBLICATION_COLUMNS)
    assert payload["Выплаты по проектам"][1][0:4] == ["10", "Project_2026", "Client", None]
    assert payload["Выплаты по проектам"][1][5] == 0


def test_initial_checkpoint_requires_all_1780_selected_projects_before_materialization():
    checkpoint = new_checkpoint(["10"], mode="INITIAL_2026", year=2026)
    record_result(checkpoint, "10", "SUCCESS_ZERO_ROWS", [], {"project": {}, "workflow_memberships": []})
    with pytest.raises(ValueError, match="scope count"):
        materialize_complete_checkpoint(checkpoint, expected_total=1780)


def test_checkpoint_internal_hash_detects_tampering(tmp_path):
    path = tmp_path / "checkpoint.json"
    saved = save_checkpoint(path, new_checkpoint(["1"], mode="INITIAL_2026", year=2026))
    document = json.loads(path.read_text())
    document["scope"][0] = "2"
    path.write_text(json.dumps(document))
    path.chmod(0o600)
    with pytest.raises(ValueError, match="integrity"):
        load_checkpoint(path)


def test_google_service_account_can_load_private_file_without_printing_it(tmp_path, monkeypatch):
    path = tmp_path / "service-account.json"
    path.write_text(json.dumps({"type": "service_account", "private_key": "private"}))
    path.chmod(0o600)
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON", raising=False)
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON_FILE", str(path))
    assert _service_account_info() == {"type": "service_account", "private_key": "private"}


def test_google_service_account_rejects_broad_file_permissions(tmp_path, monkeypatch):
    path = tmp_path / "service-account.json"
    path.write_text(json.dumps({"type": "service_account"}))
    path.chmod(0o644)
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON", raising=False)
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON_FILE", str(path))
    with pytest.raises(RuntimeError, match="permissions"):
        _service_account_info()


def test_keychain_error_does_not_include_captured_credential():
    with patch("src.payment_initial.subprocess.run", return_value=type(
        "Result", (), {"returncode": 1, "stdout": "SECRET", "stderr": "SECRET"}
    )()):
        with pytest.raises(RuntimeError) as error:
            _keychain_credential("password")
    assert "SECRET" not in str(error.value)
