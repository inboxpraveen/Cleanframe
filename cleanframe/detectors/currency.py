"""Currency / money-column detection.

Turns ``₹1,20,000`` / ``$1,200`` / ``1200 INR`` into a typed float. When a column
holds a single currency, the code is folded into the column name (``amount`` →
``amount_inr``, matching the README). When a column *mixes* currencies — where the
amount alone would be meaningless — it additionally splits out a ``*_currency``
column so no information is lost.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .._util import sample_non_null, snake_case, token_set
from ..issues import Issues, _cap_examples
from ..ops import _detect_currency_scalar, _parse_number_scalar
from ..types import Op, Severity
from .base import DetectorContext, detector


@detector("currency", priority=45)
def detect_currency(series: pd.Series, ctx: DetectorContext) -> Issues:
    """Detect money-as-text columns and propose parsing to a float (+ currency split)."""
    issues = Issues()
    cp = ctx.column_profile
    if cp is None or cp.count == 0 or cp.semantic_type != "currency":
        return issues

    values = [v if isinstance(v, str) else str(v) for v in sample_non_null(series)]
    codes = {c for c in (_detect_currency_scalar(v, None) for v in values) if isinstance(c, str)}

    # How many non-null values fail to become a number? (report, don't hide.)
    unparsed = sum(1 for v in values if np.isnan(_parse_number_scalar(v, ".", ",", [])))

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
        ops = [Op("parse_number")]
        currency_note = f"single currency {code}"
    else:
        rename_to = snake
        target = f"{snake}_currency"
        ops = [Op("extract_currency", {"to": target}), Op("parse_number")]
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
