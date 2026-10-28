import json

import pytest

from src import production


def test_project_type_acquisition_failure_logs_safe_location_and_reraises(monkeypatch, capsys):
    hidden_message = "do-not-log-value-marker"

    def fail(*_args, **_kwargs):
        raise ValueError(hidden_message)

    monkeypatch.setattr(production, "acquire_current_types", fail)

    with pytest.raises(ValueError, match=hidden_message):
        production._acquire_current_types_with_diagnostics(
            object(), [{"project_id": "not-logged"}], {}, {"1": "Type"}, "0.15", production.date.today()
        )

    output = capsys.readouterr().out.strip()
    prefix, encoded = output.split(" ", 1)
    diagnostic = json.loads(encoded)
    assert prefix == "PROJECT_TYPE_ACQUISITION_EXCEPTION"
    assert diagnostic["stage"] == "PROJECT_TYPE_ACQUISITION"
    assert diagnostic["exception_type"] == "ValueError"
    assert diagnostic["function"] == "fail"
    assert diagnostic["file"] == "tests/test_project_type_failure_diagnostic.py"
    assert isinstance(diagnostic["line"], int)
    assert diagnostic["input_classes"] == {
        "persisted_state": "dict",
        "request_delay_config": "str",
        "session": "object",
        "today": "date",
        "type_catalog": "dict",
        "universe": "list",
    }
    assert hidden_message not in output
    assert "not-logged" not in output


def test_project_type_request_delay_conversion_failure_is_instrumented(capsys):
    with pytest.raises(ValueError):
        production._acquire_current_types_with_diagnostics(
            object(), [], {}, {}, "invalid-delay-value-marker", production.date.today()
        )

    output = capsys.readouterr().out
    assert "PROJECT_TYPE_ACQUISITION_EXCEPTION" in output
    assert '"exception_type": "ValueError"' in output
    assert "invalid-delay-value-marker" not in output
    assert '"request_delay_config": "str"' in output
