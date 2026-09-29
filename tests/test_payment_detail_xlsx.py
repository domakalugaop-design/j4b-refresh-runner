"""Synthetic, PII-free tests for the Portal payment XLSX contract."""

from __future__ import annotations

import sys
import unittest
import zipfile
from io import BytesIO
from email.message import Message

from src.payment_detail_xlsx import (
    PaymentFeatureDisabled,
    PaymentWorkbookError,
    acquire_project_payment_assignments,
    join_payment_assignments,
    normalize_money,
    parse_payment_detail_xlsx,
)


HEADERS = [
    "#", "ФИО ТП", "Логин", "Проект", "Координатор", "Корректор", "Адрес",
    "Оплата за визит", "Транспортные расходы", "Компенсация расходов",
    "Бонус (штраф)", "Оплачено ранее", "Бюджет проекта",
]


def _col_name(index: int) -> str:
    result = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        result = chr(65 + rem) + result
    return result


def make_xlsx(headers=HEADERS, rows=None) -> bytes:
    """Create minimal OOXML with shared strings and no real personal data."""
    rows = rows or []
    values = list(headers)
    for row in rows:
        values.extend("" if value is None else str(value) for value in row)
    shared = list(dict.fromkeys(values))
    indexes = {value: index for index, value in enumerate(shared)}
    xml_rows = []
    for row_index, row in enumerate([headers, *rows], 1):
        cells = []
        for col, value in enumerate(row):
            if value is None or value == "":
                continue
            ref = f"{_col_name(col)}{row_index}"
            content = str(value)
            if isinstance(value, (int, float)):
                cells.append(f'<c r="{ref}"><v>{content}</v></c>')
            else:
                cells.append(f'<c r="{ref}" t="s"><v>{indexes[content]}</v></c>')
        xml_rows.append(f'<row r="{row_index}">{"".join(cells)}</row>')
    worksheet = ('<?xml version="1.0" encoding="UTF-8"?>'
                 '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                 f'<sheetData>{"".join(xml_rows)}</sheetData></worksheet>')
    strings = ''.join(f'<si><t>{value}</t></si>' for value in shared)
    shared_xml = ('<?xml version="1.0" encoding="UTF-8"?>'
                  '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                  f'{strings}</sst>')
    wb = ('<?xml version="1.0" encoding="UTF-8"?>'
          '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
          '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>')
    rels = ('<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Target="worksheets/sheet1.xml" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/>'
            '</Relationships>')
    content_types = ('<?xml version="1.0" encoding="UTF-8"?>'
                     '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                     '<Default Extension="xml" ContentType="application/xml"/>'
                     '</Types>')
    output = BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("xl/workbook.xml", wb)
        archive.writestr("xl/_rels/workbook.xml.rels", rels)
        archive.writestr("xl/sharedStrings.xml", shared_xml)
        archive.writestr("xl/worksheets/sheet1.xml", worksheet)
    return output.getvalue()


def row(my_id, reward="500", transport="0", compensation="0", bonus="0", paid="500"):
    mapping = {
        "#": my_id,
        "Оплата за визит": reward,
        "Транспортные расходы": transport,
        "Компенсация расходов": compensation,
        "Бонус (штраф)": bonus,
        "Оплачено ранее": paid,
    }
    return [mapping.get(header, "synthetic") for header in HEADERS]


class PaymentParserTests(unittest.TestCase):
    def test_valid_workbook_expected_headers_and_numeric_values(self):
        parsed = parse_payment_detail_xlsx(make_xlsx(rows=[row(1, paid=125)]))
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["my_id"], "1")
        self.assertEqual(parsed[0]["portal_paid_amount_raw"], "125")
        self.assertNotIn("ФИО ТП", parsed[0])

    def test_reordered_columns_map_by_header(self):
        reordered = list(reversed(HEADERS))
        values = {"#": 22, "Оплачено ранее": 75, "Оплата за визит": 90,
                  "Транспортные расходы": 3, "Компенсация расходов": 4, "Бонус (штраф)": -1}
        parsed = parse_payment_detail_xlsx(make_xlsx(reordered, [[values.get(h, "synthetic") for h in reordered]]))
        self.assertEqual(parsed[0]["my_id"], "22")
        self.assertEqual(parsed[0]["portal_paid_amount_raw"], "75")
        self.assertEqual(parsed[0]["bonus_penalty_raw"], "-1")

    def test_missing_required_header_rejected(self):
        with self.assertRaisesRegex(PaymentWorkbookError, "missing required headers"):
            parse_payment_detail_xlsx(make_xlsx([h for h in HEADERS if h != "Оплачено ранее"], []))

    def test_empty_workbook_rejected(self):
        with self.assertRaises(PaymentWorkbookError):
            parse_payment_detail_xlsx(make_xlsx([], []))

    def test_normalize_positive_zero_negative_blank_and_malformed(self):
        self.assertEqual(normalize_money("12.50").value.as_tuple().digits, (1, 2, 5, 0))
        self.assertEqual(normalize_money("0").status, "NUMERIC_OK")
        self.assertEqual(normalize_money("0").value, 0)
        self.assertEqual(normalize_money("-3.25").value, -3.25)
        self.assertEqual(normalize_money("").status, "UNCLASSIFIED_BLANK")
        self.assertEqual(normalize_money("N/A").status, "REVIEW")
        self.assertEqual(normalize_money("1,234.50").status, "REVIEW")

    def test_blank_financial_row_without_key_is_not_dropped(self):
        parsed = parse_payment_detail_xlsx(make_xlsx(rows=[[None] * len(HEADERS)]))
        self.assertEqual(parsed, [])
        synthetic = row(None, paid="bad")
        parsed = parse_payment_detail_xlsx(make_xlsx(rows=[synthetic]))
        self.assertIsNone(parsed[0]["my_id"])
        self.assertEqual(parsed[0]["portal_paid_amount_raw"], "bad")

    def test_duplicate_my_id_is_diagnosed_not_silently_collapsed(self):
        rows = parse_payment_detail_xlsx(make_xlsx(rows=[row(7), row(7, paid=0)]))
        joined = join_payment_assignments("100", rows, [])
        self.assertEqual(len(joined["assignments"]), 2)
        self.assertEqual(joined["diagnostics"]["PAYMENT_ROWS_DUPLICATE_MY_ID"], 1)

    def test_deterministic_join_multiple_assignments_per_visit(self):
        payment = parse_payment_detail_xlsx(make_xlsx(rows=[row(1), row(2)]))
        workflow = [
            {"project_id": "100", "action_id": "1", "visit_id": "9", "workflow_state_code": 50},
            {"project_id": "100", "action_id": "1", "visit_id": "9", "workflow_state_code": 15},
            {"project_id": "100", "action_id": "2", "visit_id": "9", "workflow_state_code": 50},
        ]
        joined = join_payment_assignments(100, payment, workflow)
        self.assertEqual(joined["matched_count"], 2)
        self.assertEqual(joined["payment_only_count"], 0)
        self.assertEqual(joined["multi_assignment_visits"], 1)
        self.assertEqual(joined["max_assignments_per_visit"], 2)
        self.assertEqual(joined["assignments"][0]["workflow_state_codes"], ["15", "50"])
        self.assertEqual(joined["diagnostics"]["PAYMENT_ROWS_TOTAL"], 2)

    def test_payment_only_and_workflow_only_are_both_retained(self):
        payment = parse_payment_detail_xlsx(make_xlsx(rows=[row(1), row(2)]))
        workflow = [{"project_id": "100", "action_id": "1", "visit_id": "9", "workflow_state_code": 40},
                    {"project_id": "100", "action_id": "3", "visit_id": "10", "workflow_state_code": 50}]
        joined = join_payment_assignments(100, payment, workflow)
        self.assertEqual(joined["matched_count"], 1)
        self.assertEqual(joined["payment_only_count"], 1)
        self.assertEqual(joined["workflow_only_count"], 1)
        self.assertEqual(joined["workflow_only_keys"], [("100", "3")])

    def test_feature_gate_is_off_by_default(self):
        with self.assertRaises(PaymentFeatureDisabled):
            acquire_project_payment_assignments("100", object())

    def test_enabled_acquisition_uses_one_scoped_xlsx_get(self):
        body = make_xlsx(rows=[row(5)])

        class Response(BytesIO):
            status = 200

            def __init__(self):
                super().__init__(body)
                self.headers = Message()
                self.headers["Content-Type"] = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

        class FakeOpener:
            calls = 0

            def open(self, request, timeout):
                self.calls += 1
                self.request = request
                self.timeout = timeout
                return Response()

        session = FakeOpener()
        parsed, status = acquire_project_payment_assignments("123", session, feature_enabled=True)
        self.assertEqual(status, 200)
        self.assertEqual(session.calls, 1)
        self.assertTrue(session.request.full_url.endswith("/pay/detail?proj=123&send=send"))
        self.assertEqual(session.request.get_method(), "GET")
        self.assertEqual(parsed[0]["my_id"], "5")


if __name__ == "__main__":
    unittest.main()
