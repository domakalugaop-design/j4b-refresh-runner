import pytest

from src.report_reviewer import ReportCorrectorParseError, parse_report_corrector


def test_parses_selected_staff_id_and_decoded_fio():
    result = parse_report_corrector(
        '<form><tr><th>Проверил</th><td><select name="staff">'
        '<option value="17">Other Person</option>'
        '<option value="90001" selected>Synthetic Reviewer</option>'
        '</select></td></tr></form>'
    )
    assert result.state == "VALUE_PRESENT"
    assert result.corrector_id == "90001"
    assert result.corrector_fio == "Synthetic Reviewer"


def test_decodes_html_entities_and_preserves_id_as_text():
    result = parse_report_corrector(
        '<select name="staff"><option value="00017" selected>Reviewer &amp; QA</option></select>'
    )
    assert result.corrector_id == "00017"
    assert result.corrector_fio == "Reviewer & QA"


def test_blank_selected_staff_is_empty():
    result = parse_report_corrector(
        '<select name="staff"><option value="" selected>—</option>'
        '<option value="17">Other Person</option></select>'
    )
    assert result.state == "EMPTY"
    assert result.corrector_id is None
    assert result.corrector_fio is None


def test_no_staff_field_is_reported_as_not_exposed():
    result = parse_report_corrector('<html><body><p>Report</p></body></html>')
    assert result.state == "FIELD_NOT_EXPOSED"
    assert result.corrector_id is None
    assert result.corrector_fio is None


def test_unclosed_staff_select_fails_closed():
    with pytest.raises(ReportCorrectorParseError, match="not closed"):
        parse_report_corrector(
            '<html><body><select name="staff"><option value="17" selected>Person'
        )


def test_multiple_selected_options_fail_closed():
    with pytest.raises(ReportCorrectorParseError, match="multiple selected"):
        parse_report_corrector(
            '<select name="staff"><option value="17" selected>First</option>'
            '<option value="18" selected>Second</option></select>'
        )


def test_nonnumeric_staff_id_fails_closed():
    with pytest.raises(ReportCorrectorParseError, match="not numeric"):
        parse_report_corrector(
            '<select name="staff"><option value="person-id" selected>Person</option></select>'
        )


def test_duplicate_staff_controls_are_ambiguous():
    with pytest.raises(ReportCorrectorParseError, match="multiple staff selects"):
        parse_report_corrector(
            '<select name="staff"><option value="17" selected>First</option></select>'
            '<select name="staff"><option value="18">Second</option></select>'
        )


def test_missing_selected_display_text_fails_closed():
    with pytest.raises(ReportCorrectorParseError, match="display text is missing"):
        parse_report_corrector('<select name="staff"><option value="17" selected></option></select>')
