"""Missing values — real and disguised.

Two distinct jobs:

* **Disguised nulls** (``"NA"``, ``"-"``, ``"unknown"``) are a *fix*: we propose a
  ``to_na`` op to turn them into real NaN so downstream parsing and counts are
  honest.
* **Actually-missing values** are *reported, never imputed*. CleanFrame will tell
  you a column is 40% empty; it will not silently invent values. (Users who want
  imputation add an explicit ``fill_na`` op themselves.)
"""

from __future__ import annotations

import pandas as pd

from .._util import is_string_like, sample_non_null
from ..issues import Issues
from ..ops import DEFAULT_NA_TOKENS
from ..types import Op, Severity
from .base import DetectorContext, detector

_NA_LOOKUP = {t.strip().casefold() for t in DEFAULT_NA_TOKENS if t}
#: Tokens that are frequently *legitimate* data (an allergy of "none", a status of
#: "unknown", a placeholder "-"). We surface these for review but do NOT auto-convert
#: them below strict review, so a real category is never silently turned into NaN.
_AMBIGUOUS_NA = {"none", "nil", "-", "--", "?", "unknown"}


@detector("nulls", priority=20)
def detect_nulls(series: pd.Series, ctx: DetectorContext) -> Issues:
    """Convert disguised nulls to NaN; report genuinely-missing and structural oddities."""
    issues = Issues()
    cp = ctx.column_profile
    if cp is None:
        return issues

    # 1) Disguised nulls in string columns -> fixable.
    if is_string_like(series):
        disguised: dict[str, int] = {}
        for v in sample_non_null(series):
            if isinstance(v, str) and v.strip().casefold() in _NA_LOOKUP:
                disguised[v] = disguised.get(v, 0) + 1
        if disguised:
            unambiguous = sorted(v for v in disguised if v.strip().casefold() not in _AMBIGUOUS_NA)
            ambiguous = sorted(v for v in disguised if v.strip().casefold() in _AMBIGUOUS_NA)
            # Unambiguous null tokens ('', 'n/a', 'null', …) — safe to convert.
            if unambiguous:
                n = sum(disguised[t] for t in unambiguous)
                issues.add(
                    "disguised_nulls",
                    f"{n} value(s) are disguised nulls ({', '.join(map(repr, unambiguous[:4]))})",
                    severity=Severity.WARNING,
                    confidence=1.0,
                    evidence={"count": n, "tokens": {t: disguised[t] for t in unambiguous}},
                    ops=[Op("to_na", {"tokens": unambiguous})],
                )
            # Ambiguous tokens that may be legitimate data — report, don't auto-convert.
            if ambiguous:
                n = sum(disguised[t] for t in ambiguous)
                issues.add(
                    "disguised_nulls",
                    f"{n} value(s) may be disguised nulls "
                    f"({', '.join(map(repr, ambiguous[:4]))}) — review before converting",
                    severity=Severity.INFO,
                    confidence=0.45,  # below the review threshold: surfaced, not auto-applied
                    evidence={"count": n, "tokens": {t: disguised[t] for t in ambiguous}, "ambiguous": True},
                    ops=[Op("to_na", {"tokens": ambiguous})],
                )

    # 2) Genuinely missing values -> report only.
    if cp.null_count:
        sev = Severity.WARNING if cp.null_fraction >= 0.2 else Severity.INFO
        issues.add(
            "missing_values",
            f"{cp.null_count} missing value(s) ({cp.null_fraction:.0%} of the column)",
            severity=sev,
            confidence=1.0,
            evidence={"null_count": cp.null_count, "null_fraction": round(cp.null_fraction, 4)},
        )

    # 3) Structural oddities worth surfacing.
    if cp.count == 0:
        issues.add(
            "empty_column",
            "Column is entirely empty",
            severity=Severity.WARNING,
            confidence=1.0,
            evidence={},
        )
    elif cp.is_constant:
        only = cp.sample_values[0] if cp.sample_values else None
        issues.add(
            "constant_column",
            f"Column has a single value throughout ({only!r})",
            severity=Severity.INFO,
            confidence=1.0,
            evidence={"value": only},
        )
    return issues


__all__ = ["detect_nulls"]
