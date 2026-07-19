"""Date detection and format inference.

Given a column the profiler flagged as dates, this figures out *which* formats are
actually present (a file mixing ``31/01/2024``, ``2024-01-31``, and ``1 Jan 2024``
is the whole point) and proposes a ``parse_date`` op that normalises them to ISO.

Ambiguous day/month order (``05/06/2024``) is resolved from the data when any value
disambiguates (a component > 12); otherwise it honours the ``dayfirst`` option and
records the assumption in the issue's evidence rather than guessing silently.
"""

from __future__ import annotations

import re

import pandas as pd

from .._util import sample_non_null
from ..issues import Issues, _cap_examples
from ..profile import COMMON_DATE_FORMATS, _looks_date
from ..types import Op, Severity
from .base import DetectorContext, detector

_DAYFIRST_FORMATS = {"%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y", "%d-%m-%y"}
_MONTHFIRST_FORMATS = {"%m/%d/%Y", "%m-%d-%Y", "%m/%d/%y"}
_SLASH_DATE_RE = re.compile(r"^\s*(\d{1,2})[/\-.](\d{1,2})[/\-.]\d{2,4}\s*$")


def _infer_direction(values: list[str]) -> bool | None:
    """Derive day-first vs month-first from any value with a component > 12.

    A value like ``01/13/2024`` (second component 13 > 12) proves the column is
    month-first; ``13/01/2024`` proves day-first. Returns ``True`` (day-first),
    ``False`` (month-first), or ``None`` when nothing disambiguates or the column
    genuinely mixes both orders. Feeding this into :func:`_infer_formats` stops the
    greedy cover from producing two contradictory ``d/m`` + ``m/d`` formats that
    silently swap day and month on the ambiguous (both ≤ 12) values.
    """
    saw_dayfirst = saw_monthfirst = False
    for v in values:
        m = _SLASH_DATE_RE.match(v)
        if not m:
            continue
        a, b = int(m.group(1)), int(m.group(2))
        if a > 12 and b <= 12:
            saw_dayfirst = True
        elif b > 12 and a <= 12:
            saw_monthfirst = True
    if saw_dayfirst and not saw_monthfirst:
        return True
    if saw_monthfirst and not saw_dayfirst:
        return False
    return None


def _ordered_formats(dayfirst: bool | None) -> list[str]:
    """COMMON_DATE_FORMATS, reordered so the preferred d/m-vs-m/d variant wins ties."""
    if dayfirst is False:
        # Prefer month-first: move month-first formats ahead of their day-first twins.
        monthfirst = [f for f in COMMON_DATE_FORMATS if f in _MONTHFIRST_FORMATS]
        rest = [f for f in COMMON_DATE_FORMATS if f not in _MONTHFIRST_FORMATS]
        return monthfirst + rest
    return list(COMMON_DATE_FORMATS)


def _infer_formats(values: list[str], dayfirst: bool | None) -> tuple[list[str], int]:
    """Greedy minimal cover of ``values`` by common formats. Returns (formats, unparsed)."""
    dateish = [v for v in values if _looks_date(v)]
    if not dateish:
        return [], 0
    ser = pd.Series(dateish, dtype="object")
    covered = pd.Series(False, index=ser.index)
    kept: list[str] = []
    for fmt in _ordered_formats(dayfirst):
        pending = ser[~covered]
        if pending.empty:
            break
        parsed = pd.to_datetime(pending, format=fmt, errors="coerce")
        hit = parsed.notna()
        if hit.any():
            kept.append(fmt)
            covered.loc[pending.index[hit.to_numpy()]] = True
    unparsed = int((~covered).sum())
    return kept, unparsed


@detector("dates", priority=40)
def detect_dates(series: pd.Series, ctx: DetectorContext) -> Issues:
    """Detect mixed / non-ISO date formats and propose normalising them to ISO."""
    issues = Issues()
    cp = ctx.column_profile
    if cp is None or cp.count == 0 or cp.semantic_type not in ("date", "datetime"):
        return issues
    # Already a proper datetime dtype — nothing to normalise.
    if pd.api.types.is_datetime64_any_dtype(series.dtype):
        return issues

    values = [v if isinstance(v, str) else str(v) for v in sample_non_null(series)]
    dayfirst_opt = ctx.option("dayfirst")
    if dayfirst_opt is None:
        # Derive day/month order from the data when the user hasn't pinned it, so
        # the greedy cover uses one consistent slash format for the whole column.
        dayfirst_opt = _infer_direction(values)
    formats, unparsed = _infer_formats(values, dayfirst_opt)
    if not formats:
        # Looked like dates to the profiler but nothing parses cleanly.
        issues.add(
            "unparseable_dates",
            "Column looks date-like but matches no known format",
            severity=Severity.WARNING,
            confidence=0.5,
            evidence={"examples": _cap_examples(values)},
        )
        return issues

    already_iso = formats == ["%Y-%m-%d"] and unparsed == 0
    if already_iso:
        return issues

    dayfirst = formats[0] in _DAYFIRST_FORMATS
    ambiguous = _is_ambiguous(values)
    kind = "mixed_date_formats" if len(formats) > 1 else "nonstandard_date_format"
    sev = Severity.WARNING if (len(formats) > 1 or unparsed) else Severity.INFO
    # Preserve time-of-day if any inferred format carries one (don't truncate to date).
    output = "%Y-%m-%dT%H:%M:%S" if any("%H" in f for f in formats) else "%Y-%m-%d"

    evidence = {
        "formats_found": formats,
        "unparsed": unparsed,
        "examples": _cap_examples(values),
    }
    if ambiguous:
        evidence["ambiguous_day_month"] = True
        evidence["assumed"] = "day-first" if dayfirst else "month-first"
    if any("%y" in f for f in formats):
        # 2-digit years use strptime's 1969–2068 century pivot — surface it.
        evidence["two_digit_year_pivot"] = "1969-2068"

    issues.add(
        kind,
        _message(formats, unparsed, ambiguous, dayfirst),
        severity=sev,
        confidence=0.9 if not ambiguous else 0.75,
        evidence=evidence,
        ops=[Op("parse_date", {"formats": formats, "dayfirst": dayfirst, "output": output})],
    )
    return issues


def _is_ambiguous(values: list[str]) -> bool:
    """True if no value disambiguates day-vs-month order (all numeric components <= 12)."""
    import re

    saw_slashlike = False
    for v in values:
        m = re.match(r"^\s*(\d{1,2})[/\-.](\d{1,2})[/\-.]\d{2,4}\s*$", v)
        if not m:
            continue
        saw_slashlike = True
        a, b = int(m.group(1)), int(m.group(2))
        if a > 12 or b > 12:
            return False
    return saw_slashlike


def _message(formats: list[str], unparsed: int, ambiguous: bool, dayfirst: bool) -> str:
    if len(formats) > 1:
        base = f"{len(formats)} different date formats present"
    else:
        base = f"Dates use non-ISO format {formats[0]!r}"
    if unparsed:
        base += f"; {unparsed} value(s) unparseable"
    if ambiguous:
        base += f" (day/month order ambiguous — assuming {'day' if dayfirst else 'month'}-first)"
    return base


__all__ = ["detect_dates"]
