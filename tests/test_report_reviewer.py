import pytest

from src.report_reviewer import ReportReviewerParseError, parse_report_reviewer


def test_parses_selected_staff_id_and_decoded_fio():
    result = parse_report_reviewer(
        '<form><tr><th>Проверил</th><td><select name="staff">'
        '<option value="17">Other Person</option>'
        '<option value="90001" selected>Synthetic Reviewer</option>'
        '</select></td></tr></form>'
    )
    assert result.state == "VALUE_PRESENT"
    assert result.reviewer_id == "90001"
    assert result.reviewer_fio == "Synthetic Reviewer"


def test_blank_selected_staff_is_empty():
    result = parse_report_reviewer(
        '<select name="staff"><option value="" selected>—</option>'
        '<option value="17">Other Person</option></select>'
    )
    assert result.state == "EMPTY"
    assert result.reviewer_id is None
    assert result.reviewer_fio is None


def test_no_staff_field_is_reported_as_not_exposed():
    result = parse_report_reviewer('<html><body><p>Report</p></body></html>')
    assert result.state == "FIELD_NOT_EXPOSED"
    assert result.reviewer_id is None


def test_unclosed_staff_select_fails_closed():
    with pytest.raises(ReportReviewerParseError, match="not closed"):
        parse_report_reviewer(
            '<html><body><select name="staff"><option value="17" selected>Person'
        )


def test_multiple_selected_options_fail_closed():
    with pytest.raises(ReportReviewerParseError, match="multiple selected"):
        parse_report_reviewer(
            '<select name="staff"><option value="17" selected>First</option>'
            '<option value="18" selected>Second</option></select>'
        )


def test_nonnumeric_staff_id_fails_closed():
    with pytest.raises(ReportReviewerParseError, match="not numeric"):
        parse_report_reviewer(
            '<select name="staff"><option value="person-id" selected>Person</option></select>'
        )


def test_duplicate_staff_controls_are_ambiguous():
    with pytest.raises(ReportReviewerParseError, match="multiple staff selects"):
        parse_report_reviewer(
            '<select name="staff"><option value="17" selected>First</option></select>'
            '<select name="staff"><option value="18">Second</option></select>'
        )


def test_missing_selected_display_text_fails_closed():
    with pytest.raises(ReportReviewerParseError, match="display text is missing"):
        parse_report_reviewer('<select name="staff"><option value="17" selected></option></select>')
