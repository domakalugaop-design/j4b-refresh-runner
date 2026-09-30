"""Strict normalization for technical identifiers written to materialized Sheets."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

_DIGITS = re.compile(r"^[0-9]+$")


def integer_id(value: Any, field: str) -> int:
    """Return a positive integer ID; reject lossy or ambiguous coercions."""
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a positive integer")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and _DIGITS.fullmatch(value):
        result = int(value)
    elif isinstance(value, Decimal) and value.is_finite() and value == value.to_integral_value():
        result = int(value)
    else:
        raise ValueError(f"{field} must be a positive integer")
    if result <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return result


def normalize_id_columns(headers: list[Any], rows: list[list[Any]]) -> list[list[Any]]:
    """Normalize populated project_id/visit_id fields in a headered payload."""
    indexes = {name: headers.index(name) for name in ("project_id", "visit_id") if name in headers}
    normalized = [list(row) for row in rows]
    for row in normalized:
        for name, index in indexes.items():
            if index < len(row) and row[index] not in (None, ""):
                row[index] = integer_id(row[index], name)
    return normalized
