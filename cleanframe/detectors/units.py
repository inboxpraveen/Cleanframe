"""Unit-quantity detection: ``5kg`` / ``5000 g`` / ``5 KG`` → one canonical unit."""

from __future__ import annotations

from collections import Counter

import pandas as pd

from ..issues import Issues, _cap_examples
from ..ops import UNIT_FAMILIES, parse_unit_scalar
from ..types import Op, Severity
from .base import DetectorContext, detector

# Preferred target unit per family (SI-ish, matches README examples for mass).
_PREFERRED_TARGET = {"mass": "g", "length": "m", "volume": "l"}


@detector("units", priority=46)
def detect_units(series: pd.Series, ctx: DetectorContext) -> Issues:
    """Detect mixed unit strings and propose ``normalize_unit`` to a single base."""
    issues = Issues()
    cp = ctx.column_profile
    if cp is None or cp.count == 0:
        return issues
    if cp.semantic_type not in ("unit", "text", "categorical"):
        return issues

    parsed: list[tuple[float, str]] = []
    raw_examples: list[str] = []
    for v in series.dropna().tolist():
        p = parse_unit_scalar(v)
        if p is None:
            continue
        parsed.append(p)
        raw_examples.append(str(v))

    if len(parsed) < max(2, int(0.5 * cp.count)):
        return issues

    families = Counter()
    units = Counter()
    for _, unit in parsed:
        for fam, table in UNIT_FAMILIES.items():
            if unit in table:
                families[fam] += 1
                units[unit] += 1
                break

    if not families:
        return issues
    family, _ = families.most_common(1)[0]
    distinct = sorted(u for u, _ in units.most_common() if u in UNIT_FAMILIES[family])
    if len(distinct) < 1:
        return issues

    mixed = len(distinct) > 1
    # When units are mixed, normalize to the family SI base (matches README:
    # 5kg / 5000 g → grams). A single unit keeps that unit.
    if mixed:
        target = _PREFERRED_TARGET[family]
    else:
        target = distinct[0] if distinct else _PREFERRED_TARGET[family]

    issues.add(
        "mixed_units" if mixed else "unit_format",
        (
            f"Unit quantities stored as text "
            f"({'mixed units ' + str(distinct) if mixed else 'unit ' + distinct[0]}); "
            f"normalize to {target}"
        ),
        severity=Severity.WARNING if mixed else Severity.INFO,
        confidence=0.9 if mixed else 0.75,
        evidence={
            "family": family,
            "units": distinct,
            "target": target,
            "examples": _cap_examples(raw_examples),
        },
        ops=[Op("normalize_unit", {"to": target})],
    )
    return issues


__all__ = ["detect_units"]
