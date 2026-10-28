import json

import pytest

from src import production


def test_pre_materialization_acquisition_failure_logs_safe_context_and_reraises(monkeypatch, capsys):
    hidden_message = "do-not-log-value-marker"

    def fail_acquire(*_args, **_kwargs):
        raise ValueError(hidden_message)

    monkeypatch.setattr(production, "acquire_project", fail_acquire)
    spec = {"project_id": "sensitive-id-marker", "project_name": "sensitive-name-marker"}

    with pytest.raises(ValueError, match=hidden_message):
        production._acquire_project_with_diagnostics(object(), spec, 0.15, 10, 425)

    output = capsys.readouterr().out.strip()
    prefix, encoded = output.split(" ", 1)
    diagnostic = json.loads(encoded)
    assert prefix == "PRE_MATERIALIZATION_VALIDATION_EXCEPTION"
    assert diagnostic["stage"] == "PRE_MATERIALIZATION_VALIDATION"
    assert diagnostic["validation_step"] == "OPERATIONAL_PROJECT_ACQUISITION"
    assert diagnostic["exception_type"] == "ValueError"
    assert diagnostic["function"] == "fail_acquire"
    assert diagnostic["file"] == "tests/test_pre_materialization_diagnostics.py"
    assert isinstance(diagnostic["line"], int)
    assert diagnostic["input_classes"] == {
        "project_spec": "dict",
        "reader": "object",
        "request_delay": "float",
    }
    assert diagnostic["project_index"] == 10
    assert diagnostic["project_total"] == 425
    assert hidden_message not in output
    assert "sensitive-id-marker" not in output
    assert "sensitive-name-marker" not in output


def test_pre_materialization_diagnostic_reports_types_and_counts_only():
    try:
        raise ValueError("payload must not appear")
    except ValueError as exc:
        diagnostic = production._pre_materialization_exception_diagnostic(
            exc,
            "READER_VALIDATION",
            {"candidate": [{"private": "row"}], "delay": 0.15},
            0,
            425,
        )
    encoded = json.dumps(diagnostic, sort_keys=True)
    assert diagnostic["input_classes"] == {"candidate": "list", "delay": "float"}
    assert diagnostic["project_index"] == 0
    assert diagnostic["project_total"] == 425
    assert "payload must not appear" not in encoded
    assert "private" not in encoded
