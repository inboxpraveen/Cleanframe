"""Outlier detection — flagged with evidence, never auto-"fixed".

Uses a classic Tukey IQR fence on numeric columns. Findings carry the fence
bounds and example values so a human (or a downstream validator) can decide;
no ops are attached, matching the README / CONTRIBUTING invariant that outliers
are detected, never silently corrected.
"""

from __future__ import annotations

import pandas as pd

from ..issues import Issues, _cap_examples
from ..types import Severity
from .base import DetectorContext, detector

_MIN_VALUES = 8
_IQR_K = 1.5


@detector("outliers", priority=70)
def detect_outliers(series: pd.Series, ctx: DetectorContext) -> Issues:
    """Flag numeric outliers via the IQR fence. Report only — never propose a fix."""
    issues = Issues()
    cp = ctx.column_profile
    if cp is None or cp.count < _MIN_VALUES:
        return issues
    if cp.semantic_type not in ("integer", "float", "currency"):
        # Also accept already-numeric dtypes even if semantic type is currency-as-float.
        if not (
            pd.api.types.is_numeric_dtype(series.dtype)
            and not pd.api.types.is_bool_dtype(series.dtype)
        ):
            return issues

    numeric = pd.to_numeric(series, errors="coerce").dropna()
    if len(numeric) < _MIN_VALUES:
        return issues

    q1 = float(numeric.quantile(0.25))
    q3 = float(numeric.quantile(0.75))
    iqr = q3 - q1
    if iqr == 0:
        return issues
    low = q1 - _IQR_K * iqr
    high = q3 + _IQR_K * iqr
    mask = (numeric < low) | (numeric > high)
    n = int(mask.sum())
    if n == 0:
        return issues

    examples = numeric[mask].tolist()
    issues.add(
        "outliers",
        f"{n} outlier value(s) outside IQR fence [{low:.4g}, {high:.4g}]",
        severity=Severity.INFO,
        confidence=0.85,
        evidence={
            "count": n,
            "low": low,
            "high": high,
            "q1": q1,
            "q3": q3,
            "examples": _cap_examples(examples),
        },
        # Intentionally no ops — outliers are never auto-fixed.
    )
    return issues


__all__ = ["detect_outliers"]
