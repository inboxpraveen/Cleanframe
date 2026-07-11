"""Text hygiene: whitespace and casing.

These are the safest, most universal fixes, so they run first (low priority
number). Whitespace trimming is non-lossy and always proposed when present.
Title-casing is opinionated (it would mangle ``"McDonald"``), so it is proposed
only for name-like columns and at a confidence low enough that ``strict`` mode
declines it.
"""

from __future__ import annotations

import re

import pandas as pd

from ..issues import Issues, _cap_examples
from ..profile import _name_hint
from ..types import Op, Severity
from .base import DetectorContext, detector

_DOUBLE_WS = re.compile(r"\s{2,}")
_ALPHAWORDS = re.compile(r"^[A-Za-z][A-Za-z.'\- ]*$")


def _is_string_column(series: pd.Series) -> bool:
    return series.dtype == object or str(series.dtype) == "string"


@detector("whitespace", priority=10)
def detect_whitespace(series: pd.Series, ctx: DetectorContext) -> Issues:
    """Flag leading/trailing or repeated internal whitespace and propose a trim."""
    issues = Issues()
    cp = ctx.column_profile
    if cp is None or cp.count == 0 or not _is_string_column(series):
        return issues

    strings = [v for v in series.dropna().tolist() if isinstance(v, str)]
    trailing = [v for v in strings if v != v.strip()]
    doubles = [v for v in strings if _DOUBLE_WS.search(v)]
    if not trailing and not doubles:
        return issues

    # collapse_whitespace subsumes a plain strip, so pick one op, not both.
    if doubles:
        op = Op("collapse_whitespace")
        msg = f"{len(set(trailing) | set(doubles))} value(s) have irregular whitespace"
    else:
        op = Op("strip_whitespace")
        msg = f"{len(trailing)} value(s) have leading/trailing whitespace"

    issues.add(
        "whitespace",
        msg,
        severity=Severity.WARNING,
        confidence=1.0,
        evidence={
            "leading_trailing": len(trailing),
            "internal_doubles": len(doubles),
            "examples": _cap_examples([repr(v) for v in (doubles or trailing)]),
        },
        ops=[op],
    )
    return issues


@detector("text_case", priority=60)
def detect_text_case(series: pd.Series, ctx: DetectorContext) -> Issues:
    """Propose title-casing for inconsistently-cased *name* columns (low confidence)."""
    issues = Issues()
    cp = ctx.column_profile
    if cp is None or cp.count == 0 or cp.semantic_type != "text":
        return issues
    if not _name_hint(ctx.column or "", "date") and not _looks_like_name_column(ctx.column, series):
        return issues

    strings = [v.strip() for v in series.dropna().tolist() if isinstance(v, str)]
    if not strings:
        return issues
    inconsistent = [v for v in strings if v != v.title()]
    frac = len(inconsistent) / len(strings)
    if frac < 0.25:
        return issues

    issues.add(
        "inconsistent_case",
        f"{len(inconsistent)} value(s) are not in Title Case",
        severity=Severity.INFO,
        confidence=0.55,
        evidence={"count": len(inconsistent), "examples": _cap_examples(inconsistent)},
        ops=[Op("title_case")],
    )
    return issues


def _looks_like_name_column(column: str | None, series: pd.Series) -> bool:
    if column is None:
        return False
    hinted = any(h in str(column).lower() for h in ("name", "city", "state", "country", "title"))
    if not hinted:
        return False
    strings = [v for v in series.dropna().head(50).tolist() if isinstance(v, str)]
    if not strings:
        return False
    alpha = sum(1 for v in strings if _ALPHAWORDS.match(v.strip()))
    return alpha / len(strings) >= 0.8


__all__ = ["detect_whitespace", "detect_text_case"]
