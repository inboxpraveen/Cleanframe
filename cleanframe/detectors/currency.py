"""Currency / money-column detection.

Turns ``₹1,20,000`` / ``$1,200`` / ``1200 INR`` into a typed float. When a column
holds a single currency, the code is folded into the column name (``amount`` →
``amount_inr``, matching the README). When a column *mixes* currencies — where the
amount alone would be meaningless — it additionally splits out a ``*_currency``
column so no information is lost.
"""

from __future__ import annotations

import re

import numpy as np
import pandas as pd

from .._util import sample_non_null, snake_case, token_set
from ..issues import Issues, _cap_examples
from ..ops import _detect_currency_scalar, _parse_number_scalar
from ..types import Op, Severity
from .base import DetectorContext, detector

_EU_GROUPED_RE = re.compile(r"\d{1,3}(?:\.\d{3})+,\d+")
_US_GROUPED_RE = re.compile(r"\d{1,3}(?:,\d{3})+")
_COMMA_DECIMAL_RE = re.compile(r"\d,\d{1,2}(?!\d)")


def _decimal_convention(values: list[str]) -> dict[str, str]:
    """Infer European ``1.234,56`` grouping. Empty dict means the pandas default.

    Parsing ``€1.200,50`` with the default convention yields 1.2005 while reporting
    nothing unparseable, so the convention has to be decided from the values.
    """
    eu_grouped = sum(1 for v in values if _EU_GROUPED_RE.search(v))
    us_grouped = sum(1 for v in values if _US_GROUPED_RE.search(v))
    if us_grouped:
        return {}
    if eu_grouped:
        return {"decimal": ",", "thousands": "."}
    comma_decimal = sum(1 for v in values if _COMMA_DECIMAL_RE.search(v))
    if comma_decimal and not any("." in v for v in values):
        return {"decimal": ",", "thousands": "."}
    return {}


@detector("currency", priority=45)
def detect_currency(series: pd.Series, ctx: DetectorContext) -> Issues:
    """Detect money-as-text columns and propose parsing to a float (+ currency split)."""
    issues = Issues()
    cp = ctx.column_profile
    if cp is None or cp.count == 0 or cp.semantic_type != "currency":
        return issues

    values = [v if isinstance(v, str) else str(v) for v in sample_non_null(series)]
    codes = {c for c in (_detect_currency_scalar(v, None) for v in values) if isinstance(c, str)}

    convention = _decimal_convention(values)
    decimal = convention.get("decimal", ".")
    thousands = convention.get("thousands", ",")
    # How many non-null values fail to become a number? (report, don't hide.)
    unparsed = sum(
        1 for v in values if np.isnan(_parse_number_scalar(v, decimal, thousands, []))
    )

    snake = snake_case(ctx.column or series.name or "amount")
    ops: list[Op] = []
    rename_to: str | None = None

    if len(codes) == 1:
        code = next(iter(codes))
        # Fold the currency into the name unless it's already there.
        if code.lower() not in token_set(snake):
            rename_to = f"{snake}_{code.lower()}"
        else:
            rename_to = snake
        ops = [Op("parse_number", dict(convention))]
        currency_note = f"single currency {code}"
    else:
        rename_to = snake
        target = f"{snake}_currency"
        ops = [Op("extract_currency", {"to": target}), Op("parse_number", dict(convention))]
        currency_note = (
            f"mixed currencies {sorted(codes)} — splitting out `{target}`"
            if codes
            else "no explicit currency code"
        )

    if rename_to == (ctx.column or series.name):
        rename_to = None

    sev = Severity.WARNING if unparsed else Severity.INFO
    evidence = {
        "currencies": sorted(codes),
        "unparsed": unparsed,
        "examples": _cap_examples(values),
    }
    if convention:
        evidence["decimal_convention"] = "european"
    issues.add(
        "currency_format",
        f"Money column stored as text ({currency_note})"
        + (f"; {unparsed} value(s) unparseable" if unparsed else ""),
        severity=sev,
        confidence=0.95,
        evidence=evidence,
        ops=ops,
        rename_to=rename_to,
    )
    return issues


__all__ = ["detect_currency"]
