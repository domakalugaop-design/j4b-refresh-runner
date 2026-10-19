"""Strict calendar-period parsing shared by operational project selectors."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo


MMYY_RE = re.compile(r"(?<!\d)(0[1-9]|1[0-2])(\d{2})(?!\d)")
QUARTER_PERIOD_RE = re.compile(r"Q([1-4])[_\s-]*(0[1-9]|1[0-2])(\d{2})", re.IGNORECASE)


@dataclass(frozen=True)
class PeriodClassification:
    status: str
    marker: str | None
    quarter_mismatch: bool


def moscow_today() -> date:
    return datetime.now(ZoneInfo("Europe/Moscow")).date()


def rolling_month_markers(today: date) -> tuple[str, str, str]:
    """Return previous/current/next calendar months as MMYY, across year edges."""
    current_index = today.year * 12 + today.month - 1
    markers: list[str] = []
    for offset in (-1, 0, 1):
        year, month0 = divmod(current_index + offset, 12)
        markers.append(f"{month0 + 1:02d}{year % 100:02d}")
    return tuple(markers)  # type: ignore[return-value]


def classify_project_period(name: Any) -> PeriodClassification:
    if name is None or name == "":
        return PeriodClassification("UNRECOGNIZED_PERIOD", None, False)
    markers = {match.group(1) + match.group(2) for match in MMYY_RE.finditer(str(name))}
    if not markers:
        return PeriodClassification("UNRECOGNIZED_PERIOD", None, False)
    if len(markers) != 1:
        return PeriodClassification("AMBIGUOUS_PERIOD", None, False)
    marker = next(iter(markers))
    quarter_mismatch = any(
        int(match.group(1)) != (int(match.group(2)) - 1) // 3 + 1
        for match in QUARTER_PERIOD_RE.finditer(str(name))
    )
    return PeriodClassification("VALID", marker, quarter_mismatch)
