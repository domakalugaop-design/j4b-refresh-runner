#!/usr/bin/env python3
"""Offline parser and feature-gated reader for the Portal payment XLSX export.

The XLSX parser is intentionally HTTP-free and uses only the Python standard
library. Payment rows remain at (project_id, my_id) grain; no PII columns are
materialized and no money is converted through binary floating point.
"""

from __future__ import annotations

import http.cookiejar
import os
import re
import urllib.parse
import urllib.request
import zipfile
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable, Mapping
from xml.etree import ElementTree as ET

BASE_URL = "https://lk.j4b.ru"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
REQUIRED_HEADERS = (
    "#",
    "Оплата за визит",
    "Транспортные расходы",
    "Компенсация расходов",
    "Бонус (штраф)",
    "Оплачено ранее",
)
MONEY_HEADERS = {
    "Оплата за визит": "visit_reward_raw",
    "Транспортные расходы": "transport_expense_raw",
    "Компенсация расходов": "expense_compensation_raw",
    "Бонус (штраф)": "bonus_penalty_raw",
    "Оплачено ранее": "portal_paid_amount_raw",
}
NORMALIZED_FIELDS = {
    "visit_reward_raw": "visit_reward",
    "transport_expense_raw": "transport_expense",
    "expense_compensation_raw": "expense_compensation",
    "bonus_penalty_raw": "bonus_penalty",
    "portal_paid_amount_raw": "portal_paid_amount",
}
_NS = {
    "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "rel": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "pkg": "http://schemas.openxmlformats.org/package/2006/relationships",
}


class PaymentWorkbookError(ValueError):
    """The export is not a structurally valid payment workbook."""


class PaymentFeatureDisabled(RuntimeError):
    """Raised when the explicitly opt-in acquisition path is not enabled."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Do not let a business-data request silently turn into another GET."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class AmountNormalization:
    value: Decimal | None
    status: str
    reason: str | None = None


def _as_bytes(source: bytes | bytearray | memoryview | str | os.PathLike[str]) -> bytes:
    if isinstance(source, (bytes, bytearray, memoryview)):
        return bytes(source)
    return Path(source).read_bytes()


def _column_index(cell_ref: str) -> int:
    letters = re.match(r"[A-Za-z]+", cell_ref)
    if not letters:
        raise PaymentWorkbookError("invalid XLSX cell reference")
    result = 0
    for char in letters.group(0).upper():
        result = result * 26 + ord(char) - 64
    return result - 1


def _read_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    values = []
    for si in root.findall("main:si", _NS):
        values.append("".join(node.text or "" for node in si.findall(".//main:t", _NS)))
    return values


def _first_sheet_path(archive: zipfile.ZipFile) -> str:
    try:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        sheet = workbook.find("main:sheets/main:sheet", _NS)
        rel_id = sheet.attrib[f"{{{_NS['rel']}}}id"] if sheet is not None else None
        rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    except (KeyError, ET.ParseError) as exc:
        raise PaymentWorkbookError("invalid XLSX workbook structure") from exc
    if not rel_id:
        raise PaymentWorkbookError("workbook has no worksheet")
    for rel in rels.findall("pkg:Relationship", _NS):
        if rel.attrib.get("Id") == rel_id:
            target = rel.attrib.get("Target", "")
            path = target.lstrip("/") if target.startswith("/") else "xl/" + target
            # Normalize relationship paths without permitting traversal.
            parts: list[str] = []
            for part in path.split("/"):
                if part == "..":
                    if not parts:
                        raise PaymentWorkbookError("worksheet path escapes XLSX root")
                    parts.pop()
                elif part not in ("", "."):
                    parts.append(part)
            return "/".join(parts)
    raise PaymentWorkbookError("first worksheet relationship is missing")


def _cell_value(cell: ET.Element, shared: list[str]) -> str | None:
    if cell.find("main:f", _NS) is not None:
        raise PaymentWorkbookError("formula cells are not accepted in payment exports")
    kind = cell.attrib.get("t")
    if kind == "inlineStr":
        inline = cell.find("main:is", _NS)
        return "" if inline is None else "".join(n.text or "" for n in inline.findall(".//main:t", _NS))
    value = cell.find("main:v", _NS)
    if value is None:
        return None
    raw = value.text or ""
    if kind == "s":
        try:
            return shared[int(raw)]
        except (ValueError, IndexError) as exc:
            raise PaymentWorkbookError("invalid shared-string reference") from exc
    # Keep OOXML numeric lexemes as text: Decimal normalization must not pass
    # through Python float, and source representation remains inspectable.
    return raw


def parse_payment_detail_xlsx(source: bytes | bytearray | memoryview | str | os.PathLike[str]) -> list[dict[str, str | None]]:
    """Parse the first worksheet into raw assignment records (never performs HTTP).

    Display-only columns, including participant names/logins, are intentionally
    ignored. A row with amount data but no assignment key is retained with an
    empty ``my_id`` so diagnostics can classify it rather than dropping it.
    """
    payload = _as_bytes(source)
    try:
        archive = zipfile.ZipFile(BytesIO(payload))
    except (zipfile.BadZipFile, OSError) as exc:
        raise PaymentWorkbookError("input is not a readable XLSX ZIP package") from exc
    with archive:
        shared = _read_shared_strings(archive)
        sheet_path = _first_sheet_path(archive)
        try:
            root = ET.fromstring(archive.read(sheet_path))
        except (KeyError, ET.ParseError) as exc:
            raise PaymentWorkbookError("worksheet is missing or malformed") from exc
        rows: list[dict[int, str | None]] = []
        for row in root.findall(".//main:sheetData/main:row", _NS):
            parsed: dict[int, str | None] = {}
            for cell in row.findall("main:c", _NS):
                parsed[_column_index(cell.attrib.get("r", ""))] = _cell_value(cell, shared)
            if any(value not in (None, "") for value in parsed.values()):
                rows.append(parsed)
    if not rows:
        raise PaymentWorkbookError("workbook contains no non-empty header row")

    header_row = rows[0]
    header_to_column: dict[str, int] = {}
    for col, value in header_row.items():
        header = (value or "").strip()
        if header:
            if header in header_to_column:
                raise PaymentWorkbookError(f"duplicate required/recognized header: {header}")
            header_to_column[header] = col
    missing = [header for header in REQUIRED_HEADERS if header not in header_to_column]
    if missing:
        raise PaymentWorkbookError("missing required headers: " + ", ".join(missing))

    records: list[dict[str, str | None]] = []
    for row in rows[1:]:
        record: dict[str, str | None] = {
            "my_id": (row.get(header_to_column["#"]) or "").strip() or None,
        }
        for header, field in MONEY_HEADERS.items():
            raw = row.get(header_to_column[header])
            record[field] = None if raw is None else raw.strip()
        # Ignore fully blank rows; preserve rows with financial content but an
        # absent key so the downstream diagnostics can expose the issue.
        if record["my_id"] is not None or any(record[field] not in (None, "") for field in MONEY_HEADERS.values()):
            records.append(record)
    return records


def normalize_money(raw: Any) -> AmountNormalization:
    """Normalize a raw money cell safely; blank is not converted to zero."""
    if raw is None:
        return AmountNormalization(None, "UNCLASSIFIED_BLANK", "blank source value")
    if isinstance(raw, bool) or isinstance(raw, float):
        return AmountNormalization(None, "REVIEW", "binary float/boolean is not accepted")
    text = str(raw).strip()
    if not text:
        return AmountNormalization(None, "UNCLASSIFIED_BLANK", "blank source value")
    text = text.replace("\u00a0", " ").replace("\u202f", " ")
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"(?:₽|руб(?:\.)?)$", "", text, flags=re.IGNORECASE)
    # Accept canonical numeric cells and a single comma decimal separator.
    # Ambiguous mixed/grouped separators are deliberately sent for review.
    if "," in text and "." not in text:
        text = text.replace(",", ".")
    if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", text):
        return AmountNormalization(None, "REVIEW", "malformed or ambiguous amount")
    try:
        number = Decimal(text)
    except InvalidOperation:
        return AmountNormalization(None, "REVIEW", "invalid decimal")
    if not number.is_finite():
        return AmountNormalization(None, "REVIEW", "non-finite decimal")
    return AmountNormalization(number, "NUMERIC_OK")


def normalize_assignment(record: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy with Decimal candidate fields and per-field diagnostics."""
    normalized = dict(record)
    diagnostics: dict[str, str] = {}
    for raw_field, field in NORMALIZED_FIELDS.items():
        result = normalize_money(record.get(raw_field))
        normalized[field] = result.value
        diagnostics[field] = result.status
    normalized["money_diagnostics"] = diagnostics
    normalized["numeric_review"] = any(value == "REVIEW" for value in diagnostics.values())
    return normalized


def join_payment_assignments(
    project_id: str | int,
    payment_rows: Iterable[Mapping[str, Any]],
    workflow_rows: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Pure join by (project_id, my_id), preserving states and unmatched rows."""
    pid = str(project_id)
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    ambiguous: set[tuple[str, str]] = set()
    workflow_unique: set[tuple[str, str]] = set()
    visits: Counter[str] = Counter()
    for workflow in workflow_rows:
        if str(workflow.get("project_id", pid)) != pid:
            continue
        my_id = workflow.get("action_id", workflow.get("my_id"))
        if my_id in (None, ""):
            continue
        key = (pid, str(my_id))
        workflow_unique.add(key)
        visit_id = workflow.get("visit_id")
        state = workflow.get("workflow_state_code", workflow.get("state_code"))
        current = by_key.get(key)
        if current is None:
            current = {"visit_id": None if visit_id is None else str(visit_id), "workflow_state_codes": set()}
            by_key[key] = current
        elif visit_id not in (None, "") and current["visit_id"] not in (None, str(visit_id)):
            ambiguous.add(key)
        elif current["visit_id"] is None and visit_id not in (None, ""):
            current["visit_id"] = str(visit_id)
        if state not in (None, ""):
            current["workflow_state_codes"].add(str(state))

    payments: list[dict[str, Any]] = []
    seen_payment: set[tuple[str, str]] = set()
    duplicate_count = 0
    for payment in payment_rows:
        row = dict(payment)
        my_id = row.get("my_id")
        key = (pid, "" if my_id is None else str(my_id))
        if my_id not in (None, "") and key in seen_payment:
            duplicate_count += 1
        elif my_id not in (None, ""):
            seen_payment.add(key)
        workflow = by_key.get(key) if my_id not in (None, "") else None
        ambiguous_key = key in ambiguous
        row.update({"project_id": pid, "visit_id": None, "workflow_state_codes": [],
                    "join_status": "PAYMENT_ONLY"})
        if workflow and not ambiguous_key:
            row["visit_id"] = workflow["visit_id"]
            row["workflow_state_codes"] = sorted(workflow["workflow_state_codes"], key=lambda x: (not x.isdigit(), int(x) if x.isdigit() else x))
            row["join_status"] = "MATCHED"
        elif ambiguous_key:
            row["join_status"] = "PAYMENT_ONLY"
            row["join_diagnostic"] = "workflow key maps to multiple visit IDs"
        payments.append(row)
        if row["visit_id"]:
            visits[row["visit_id"]] += 1

    payment_unique = {key for key in seen_payment}
    matched = sum(1 for row in payments if row["join_status"] == "MATCHED")
    workflow_only = workflow_unique - payment_unique
    diagnostics = Counter()
    numeric_ok = numeric_review = blank_rows = 0
    for row in payments:
        if row.get("my_id") in (None, ""):
            diagnostics["PAYMENT_ROWS_WITHOUT_MY_ID"] += 1
        statuses = list((row.get("money_diagnostics") or {}).values())
        if "REVIEW" in statuses:
            numeric_review += 1
        elif "UNCLASSIFIED_BLANK" in statuses:
            blank_rows += 1
        else:
            numeric_ok += 1
    diagnostics["PAYMENT_ROWS_TOTAL"] = len(payments)
    diagnostics["PAYMENT_ROWS_NUMERIC_OK"] = numeric_ok
    diagnostics["PAYMENT_ROWS_NUMERIC_REVIEW"] = numeric_review
    diagnostics["PAYMENT_ROWS_WITH_UNCLASSIFIED_BLANK"] = blank_rows
    diagnostics["PAYMENT_ROWS_DUPLICATE_MY_ID"] = duplicate_count
    diagnostics["PAYMENT_ROWS_WITHOUT_WORKFLOW_JOIN"] = sum(row["join_status"] != "MATCHED" for row in payments)
    return {
        "project_id": pid,
        "assignments": payments,
        "workflow_only_keys": sorted(workflow_only),
        "diagnostics": dict(diagnostics),
        "matched_count": matched,
        "payment_only_count": sum(1 for row in payments if row["join_status"] != "MATCHED"),
        "workflow_only_count": len(workflow_only),
        "payment_unique_my_ids": len(payment_unique),
        "multi_assignment_visits": sum(count > 1 for count in visits.values()),
        "max_assignments_per_visit": max(visits.values(), default=0),
    }


def acquire_project_payment_assignments(
    project_id: str | int,
    authenticated_session: Any,
    *,
    feature_enabled: bool = False,
    timeout: int = 60,
) -> tuple[list[dict[str, str | None]], int]:
    """Perform exactly one XLSX GET using an existing authenticated cookie jar.

    The feature is OFF unless the caller explicitly enables it. The helper
    does not authenticate, retry, or persist response bytes.
    """
    if not feature_enabled:
        raise PaymentFeatureDisabled("payment XLSX acquisition is disabled by default")
    project = str(project_id)
    if not project.isdigit() or int(project) <= 0:
        raise ValueError("project_id must be a positive integer")
    route = "/pay/detail?" + urllib.parse.urlencode({"proj": project, "send": "send"})
    request = urllib.request.Request(
        BASE_URL + route,
        headers={"Accept": XLSX_MIME},
        method="GET",
    )
    if hasattr(authenticated_session, "open"):
        opener = authenticated_session
    else:
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(authenticated_session), _NoRedirect()
        )
    with opener.open(request, timeout=timeout) as response:
        status = int(response.status)
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        body = response.read()
    if status != 200 or content_type != XLSX_MIME:
        raise PaymentWorkbookError(f"payment export response contract failed (HTTP {status}, content type mismatch)")
    return parse_payment_detail_xlsx(body), status
