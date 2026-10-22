"""Safe, report-scoped extraction of the selected ``staff`` field.

The Portal labels this field "Проверил".  That label is not sufficient to
assert the business role "corrector", so the public names intentionally use
reviewer rather than corrector.  This parser is not wired into acquisition or
publication until report-level cardinality and the business meaning are
approved.
"""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
import re
from typing import Literal


ReviewerState = Literal["VALUE_PRESENT", "EMPTY", "FIELD_NOT_EXPOSED"]


class ReportReviewerParseError(ValueError):
    """A structural or validation error with no portal text in the message."""


@dataclass(frozen=True)
class ReportReviewer:
    state: ReviewerState
    reviewer_id: str | None = None
    reviewer_fio: str | None = None


@dataclass
class _Option:
    value: str | None
    selected: bool
    text_parts: list[str]


class _StaffSelectParser(HTMLParser):
    """Read only select[name=staff] and its option metadata/text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.select_count = 0
        self.select_closed = True
        self.options: list[_Option] = []
        self._in_staff_select = False
        self._option: _Option | None = None

    def _finish_option(self) -> None:
        if self._option is not None:
            self.options.append(self._option)
            self._option = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        attributes = {str(key).casefold(): value for key, value in attrs}

        if tag == "select":
            if self._in_staff_select:
                raise ReportReviewerParseError("malformed staff select structure")
            if (attributes.get("name") or "").casefold() == "staff":
                self.select_count += 1
                self._in_staff_select = True
                self.select_closed = False

        elif tag == "option" and self._in_staff_select:
            # HTML permits an omitted </option> before the next option.
            self._finish_option()
            self._option = _Option(
                value=attributes.get("value"),
                selected="selected" in attributes,
                text_parts=[],
            )

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag == "option" and self._in_staff_select:
            self._finish_option()
        elif tag == "select" and self._in_staff_select:
            self._finish_option()
            self._in_staff_select = False
            self.select_closed = True

    def handle_data(self, data: str) -> None:
        if self._in_staff_select and self._option is not None:
            self._option.text_parts.append(data)


def _normalized_text(parts: list[str]) -> str:
    return " ".join("".join(parts).split())


def parse_report_reviewer(html: str) -> ReportReviewer:
    """Parse the report's selected reviewer without logging source values.

    A missing field or an explicitly blank/no selection is represented as a
    state. Ambiguous controls, malformed target markup, nonnumeric IDs, and
    selected options without display text fail closed.
    """
    if not isinstance(html, str) or not html.strip():
        raise ReportReviewerParseError("report reviewer HTML is empty")

    parser = _StaffSelectParser()
    try:
        parser.feed(html)
        parser.close()
    except ReportReviewerParseError:
        raise
    except Exception:
        raise ReportReviewerParseError("report reviewer HTML is malformed") from None

    if parser._in_staff_select or not parser.select_closed:
        raise ReportReviewerParseError("staff select is not closed")
    if parser.select_count > 1:
        raise ReportReviewerParseError("multiple staff selects are ambiguous")
    if parser.select_count == 0:
        return ReportReviewer("FIELD_NOT_EXPOSED")

    selected = [option for option in parser.options if option.selected]
    if len(selected) > 1:
        raise ReportReviewerParseError("multiple selected staff options are ambiguous")
    if not selected:
        return ReportReviewer("EMPTY")

    option = selected[0]
    employee_id = (option.value or "").strip()
    display_name = _normalized_text(option.text_parts)
    if not employee_id:
        return ReportReviewer("EMPTY")
    if not re.fullmatch(r"[0-9]+", employee_id):
        raise ReportReviewerParseError("selected staff ID is not numeric")
    if not display_name:
        raise ReportReviewerParseError("selected staff display text is missing")

    return ReportReviewer(
        state="VALUE_PRESENT",
        reviewer_id=employee_id,
        reviewer_fio=display_name,
    )
