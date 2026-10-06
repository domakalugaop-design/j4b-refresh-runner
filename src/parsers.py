from __future__ import annotations

import html as html_lib
import re
from typing import Any
from html.parser import HTMLParser

from .workflow_analytics import WORKFLOW_STATES

TAG_RE = re.compile(r"<[^>]+>")
INPUT_RE = re.compile(r"<input\b([^>]*)>", re.IGNORECASE)
SELECT_RE = re.compile(r"<select\b([^>]*)>(.*?)</select\s*>", re.IGNORECASE | re.DOTALL)
TEXTAREA_RE = re.compile(r"<textarea\b([^>]*)>(.*?)</textarea\s*>", re.IGNORECASE | re.DOTALL)
OPTION_OPEN_RE = re.compile(r"<option\b([^>]*)>", re.IGNORECASE)
NAME_RE = re.compile(r"\bname\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
VALUE_RE = re.compile(r"\bvalue\s*=\s*[\"']([^\"']*)[\"']", re.IGNORECASE)
VISIT_LINK_RE = re.compile(r"/visit/(\d+)", re.IGNORECASE)
ACTION_LINK_RE = re.compile(r'<a\b[^>]*href=["\']/action/(\d+)["\'][^>]*>(.*?)</a\s*>', re.IGNORECASE | re.DOTALL)

# The action page contains both the workflow-state link text and unrelated
# numeric action identifiers.  Only the former are admitted to analytics.
_WORKFLOW_LABELS = {" ".join(str(label).split()).casefold() for label in WORKFLOW_STATES.values()}
_WORKFLOW_LABELS.update({
    "отчет выполнен", "отчет принят", "оплачено", "ожидает оплату",
    "анкета подтверждена", "есть претензия",
})

# Read-only snapshot of the Portal currency dictionary on dfb, used only to
# resolve stable dictionary keys emitted by the already-acquired project edit
# form. Unknown/new keys fail closed in acquisition rather than guessing.
# Values are (code, name, raw Portal sym); html entities in sym are decoded
# before materialization for a usable display symbol.
PORTAL_CURRENCY_DICTIONARY: dict[str, tuple[str, str, str]] = {
    "1": ("RUB", "рубль", "₽"),
    "2": ("ARM", "Драм - армянский", "֏"),
    "3": ("AZN", "Манат - азейбарджаский", "₼"),
    "4": ("GEL", "Лари - грузинский", "&#8382;"),
    "5": ("BYN", "Белорусский рубль", "Б"),
    "6": ("KZT", "Казахский тенге", "₸"),
    "7": ("KGS", "Киргизский сом", "с"),
    "8": ("USDT", "долар - крипта", "$"),
    "9": ("UZS", "Узбекский сум", "UZS"),
    "10": ("XOF", "Западноафриканский франк", "₣"),
}


class _EditFormInspector(HTMLParser):
    """Collect only form/control structure; never retain response text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[set[str]] = []
        self._form: set[str] | None = None
        self.currency_selects: list[dict[str, Any]] = []
        self._currency: dict[str, Any] | None = None
        self.login_form = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        tag = tag.lower()
        if tag == "form":
            self._form = set()
            self.forms.append(self._form)
        name = attributes.get("name")
        if tag in {"input", "select", "textarea", "button"} and name:
            if self._form is not None:
                self._form.add(name)
            if name in {"_login", "_password"}:
                self.login_form = True
        if tag == "select" and name == "currency":
            self._currency = {
                "multiple": "multiple" in attributes,
                "options": [],
            }
            self.currency_selects.append(self._currency)
        elif tag == "option" and self._currency is not None:
            self._currency["options"].append({
                "value": attributes.get("value"),
                "selected": "selected" in attributes,
                "text": "",
            })

    def handle_data(self, data: str) -> None:
        if self._currency is not None and self._currency["options"]:
            self._currency["options"][-1]["text"] += data

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "select":
            self._currency = None
        elif tag.lower() == "form":
            self._form = None


def parse_currency_select(html: str) -> dict[str, Any]:
    """Parse the selected raw Portal currency key and its dictionary values.

    An absent control, empty selection, or control without an explicit
    selected option remains nullable. Browser fallback selection of the first
    option is deliberately not treated as a persisted project value.
    """
    inspector = _EditFormInspector()
    inspector.feed(html)
    if not inspector.currency_selects:
        return {
            "state": "FIELD_NOT_EXPOSED", "value": None,
            "currency_code": None, "currency_name": None,
            "currency_symbol": None, "dictionary_match": None,
        }
    if len(inspector.currency_selects) != 1:
        return {
            "state": "CONTROL_AMBIGUOUS", "value": None,
            "currency_code": None, "currency_name": None,
            "currency_symbol": None, "dictionary_match": False,
        }
    control = inspector.currency_selects[0]
    selected = [option for option in control["options"] if option["selected"]]
    if len(selected) > 1 or control["multiple"]:
        return {
            "state": "SELECTION_AMBIGUOUS", "value": None,
            "currency_code": None, "currency_name": None,
            "currency_symbol": None, "dictionary_match": False,
        }
    if not selected:
        return {
            "state": "CONTROL_PRESENT_NO_SELECTION", "value": None,
            "currency_code": None, "currency_name": None,
            "currency_symbol": None, "dictionary_match": None,
        }
    option = selected[0]
    raw_id = option["value"]
    if raw_id in (None, ""):
        return {
            "state": "FIELD_PRESENT_EMPTY", "value": None,
            "currency_code": None, "currency_name": None,
            "currency_symbol": None, "dictionary_match": None,
        }
    raw_id = str(raw_id).strip()
    dictionary = PORTAL_CURRENCY_DICTIONARY.get(raw_id)
    if dictionary is None:
        return {
            "state": "VALUE_PRESENT", "value": raw_id,
            "currency_code": None, "currency_name": None,
            "currency_symbol": None, "dictionary_match": False,
        }
    code, name, symbol = dictionary
    selected_text = " ".join(html_lib.unescape(option["text"]).split())
    dictionary_match = selected_text == " ".join(name.split())
    return {
        "state": "VALUE_PRESENT", "value": raw_id,
        "currency_code": code, "currency_name": name,
        "currency_symbol": html_lib.unescape(symbol),
        "dictionary_match": dictionary_match,
    }


def inspect_project_edit_structure(html: str) -> dict[str, Any]:
    """Return safe structural markers for an expected project edit resource."""
    inspector = _EditFormInspector()
    inspector.feed(html)
    required = {"name", "dt1", "dt2", "visits", "client", "user"}
    form_present = any(required.issubset(form) for form in inspector.forms)
    lower = html.lower()
    text = plain_text(html).casefold()
    php_error = bool(re.search(r"php\s+(?:parse|fatal)\s+error|parse error:|fatal error:|uncaught exception", text))
    access_denied = bool(re.search(r"access denied|доступ запрещ[её]н|нет доступа|http\s*403", text))
    return {
        "html_document": "<!doctype" in lower or "<html" in lower,
        "form_present": bool(inspector.forms),
        "project_form_present": form_present,
        "login_form_present": inspector.login_form,
        "php_error_present": php_error,
        "access_denied_present": access_denied,
        "currency_control_count": len(inspector.currency_selects),
    }


def plain_text(fragment: str) -> str:
    return " ".join(html_lib.unescape(TAG_RE.sub(" ", fragment)).split())


def _number_or_text(value: str | None) -> int | str | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return value


def input_field_state(html: str, field_name: str) -> tuple[str, str | None]:
    for attrs in INPUT_RE.findall(html):
        name = NAME_RE.search(attrs)
        if not name or name.group(1) != field_name:
            continue
        value = VALUE_RE.search(attrs)
        if value and value.group(1).strip():
            return "VALUE_PRESENT", value.group(1).strip()
        return "FIELD_PRESENT_EMPTY", None
    return "FIELD_NOT_EXPOSED", None


def select_field_state(html: str, field_name: str) -> tuple[str, list[dict[str, str]]]:
    for attrs, options in SELECT_RE.findall(html):
        name = NAME_RE.search(attrs)
        if not name or name.group(1) != field_name:
            continue
        selected: list[dict[str, str]] = []
        option_matches = list(OPTION_OPEN_RE.finditer(options))
        for index, match in enumerate(option_matches):
            option_attrs = match.group(1)
            start = match.end()
            end = option_matches[index + 1].start() if index + 1 < len(option_matches) else len(options)
            close_option = options.lower().find("</option", start)
            close_select = options.lower().find("</select", start)
            if close_option != -1 and close_option < end:
                end = close_option
            if close_select != -1 and close_select < end:
                end = close_select
            if re.search(r"\bselected\b", option_attrs, re.IGNORECASE):
                raw = VALUE_RE.search(option_attrs)
                selected.append({
                    "value": raw.group(1).strip() if raw and raw.group(1) else "",
                    "label": plain_text(options[start:end]),
                })
        return ("VALUE_PRESENT", selected) if selected else ("FIELD_PRESENT_EMPTY", [])
    return "FIELD_NOT_EXPOSED", []


def textarea_field_state(html: str, field_name: str) -> tuple[str, str | None]:
    for attrs, body in TEXTAREA_RE.findall(html):
        name = NAME_RE.search(attrs)
        if not name or name.group(1) != field_name:
            continue
        value = plain_text(body)
        return ("VALUE_PRESENT", value) if value else ("FIELD_PRESENT_EMPTY", None)
    return "FIELD_NOT_EXPOSED", None


class _ProjectEditFormLocator(HTMLParser):
    """Locate forms by their project-edit controls, ignoring input-like text elsewhere."""

    REQUIRED_FIELDS = {"name", "dt1", "dt2", "visits", "client", "user"}

    def __init__(self, source: str):
        super().__init__(convert_charrefs=True)
        self.source = source
        self.line_offsets = [0]
        for match in re.finditer("\n", source):
            self.line_offsets.append(match.end())
        self.forms: list[dict[str, Any]] = []
        self.current: dict[str, Any] | None = None

    def _offset(self) -> int:
        line, column = self.getpos()
        return self.line_offsets[line - 1] + column

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "form":
            self.current = {"start": self._offset(), "end": None, "fields": set()}
            self.forms.append(self.current)
            return
        if self.current is not None and tag in {"input", "select", "textarea", "button"}:
            name = dict(attrs).get("name")
            if name:
                self.current["fields"].add(name)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() == "form":
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "form" and self.current is not None:
            offset = self._offset()
            end_tag = re.match(r"</\s*form\s*>", self.source[offset:], re.IGNORECASE)
            self.current["end"] = offset + (len(end_tag.group(0)) if end_tag else len("</form>"))
            self.current = None

    def project_form(self) -> str | None:
        candidates = [
            form for form in self.forms
            if self.REQUIRED_FIELDS.issubset(form["fields"])
            and form["end"] is not None
        ]
        if len(candidates) != 1:
            return None
        form = candidates[0]
        return self.source[form["start"]:form["end"]]


def _project_edit_form(html: str) -> str | None:
    locator = _ProjectEditFormLocator(html)
    locator.feed(html)
    return locator.project_form()


def parse_edit(html: str) -> dict[str, Any]:
    # Portal pages may contain input-like fragments outside the project form
    # (for example template/search markup). Regex parsing the full document can
    # select one of those before the actual project name field.
    project_form = _project_edit_form(html)
    if project_form is None:
        html = ""
    else:
        html = project_form

    fields: dict[str, Any] = {}
    for html_name, canonical_name in (
        ("name", "project_name"),
        ("dt1", "date_from"),
        ("dt2", "date_to"),
        ("visits", "planned_visit_count"),
        ("cost", "manager_payment"),
    ):
        state, raw = input_field_state(html, html_name)
        value: Any = _number_or_text(raw) if canonical_name in {"planned_visit_count", "manager_payment"} else raw
        fields[canonical_name] = {"state": state, "value": value}

    for html_name, canonical_name, multiple in (
        ("client", "client", False),
        ("user", "primary_manager", False),
        ("user2[]", "coordinators", True),
        ("wave", "wave", False),
        ("scope", "scope", False),
    ):
        state, selected = select_field_state(html, html_name)
        value = [item["label"] for item in selected] if multiple else (selected[0]["label"] if selected else None)
        fields[canonical_name] = {
            "state": state,
            "selected_count": len(selected),
            "selected_values": selected,
            "value": value,
        }
    currency = parse_currency_select(html)
    fields["currency_id"] = {
        "state": currency["state"], "value": currency["value"],
        "dictionary_match": currency["dictionary_match"],
    }
    for name in ("currency_code", "currency_name", "currency_symbol"):
        fields[name] = {
            "state": currency["state"] if currency["value"] is not None else currency["state"],
            "value": currency[name],
            "dictionary_match": currency["dictionary_match"],
        }
    return fields


def parse_visit_table(html: str) -> list[dict[str, str]]:
    return [{"visit_id": visit_id} for visit_id in sorted(set(VISIT_LINK_RE.findall(html)), key=int)]


def parse_action_table(html: str, target_project_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    row_pattern = re.compile(r'<tr[^>]*class=["\']([^"\']*)["\'][^>]*>.*?</tr>', re.IGNORECASE | re.DOTALL)
    for match in row_pattern.finditer(html):
        row = match.group(0)
        row_class = match.group(1)
        visit_match = re.search(r"/visit/(\d+)", row)
        if not visit_match or f"/proj/{target_project_id}" not in row:
            continue
        action_match = re.search(r"/action/(\d+)", row)
        action_id = action_match.group(1) if action_match else ""
        explicitly_unassigned = action_id == "0" and "table-red" in row_class
        links = [(identifier, plain_text(body)) for identifier, body in ACTION_LINK_RE.findall(row)]
        numeric = [text for _, text in links if text.isdigit()]
        # In the red unassigned branch /action/0 is a sentinel link, not a
        # my.state=0 membership (state-0 invitation rows use their my.i ID).
        codes = [] if explicitly_unassigned else [text for text in numeric if int(text) in WORKFLOW_STATES]
        labels = [text for _, text in links if not text.isdigit()]
        # A structurally valid state control is represented by an action link
        # with a visible state label.  If such a control has no canonical code,
        # fail closed; bare numeric action IDs and other numeric controls are
        # ignored.  When a canonical code is present, unrelated IDs in the
        # same row remain metadata and must not poison the row.
        normalized_labels = {" ".join(label.split()).casefold() for label in labels}
        if not codes and labels and (normalized_labels & _WORKFLOW_LABELS):
            unknown = next((text for text in numeric if int(text) not in WORKFLOW_STATES), None)
            if unknown is not None:
                raise ValueError(f"unknown workflow state code: {unknown}")
        status_label = ""
        for label in reversed(labels):
            label = label.strip()
            if label and not label.isdigit():
                status_label = label
                break
        # The action page's optional unassigned Visit branch is represented
        # by a red row linked to /action/0.  Keep this structural marker
        # distinct from raw workflow-state predicates.
        participant_assigned = (
            False if explicitly_unassigned else
            (action_id not in ("", "0") if "table-red" in row_class else True)
        )
        rows.append({
            "visit_id": visit_match.group(1),
            "action_id": action_id,
            "participant_assigned": participant_assigned,
            "assignment_state": "UNASSIGNED_FREE" if explicitly_unassigned else None,
            "visit_status": codes[-1] if codes else "",
            "workflow_state_codes": codes,
            "visit_status_label": status_label,
        })
    return rows
