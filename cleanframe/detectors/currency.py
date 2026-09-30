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

_NUMBER_RUN_RE = re.compile(r"\d[\d., ' ]*\d|\d")


def _convention_votes(values: list[str]) -> tuple[int, int, int]:
    """Count values that are *unambiguously* EU vs US, and the ambiguous ``1.234`` shape.

    ``1,234`` is ambiguous (US thousands or EU decimal) but so common as US thousands
    that it votes for neither and the dot-decimal default stands. ``1.234`` is the
    dangerous mirror image: US decimal ``1.234`` or EU thousands ``1234``. It is counted
    separately so the detector can refuse to guess. Indian ``1,20,000`` and
    ``1,234,567`` can only be grouping, so they vote US.
    """
    eu = us = ambiguous = 0
    for v in values:
        for run in _NUMBER_RUN_RE.findall(v):
            run = run.strip(" ., '")
            dots, commas = run.count("."), run.count(",")
            if dots and commas:
                if run.rfind(",") > run.rfind("."):
                    eu += 1
                else:
                    us += 1
            elif commas:
                tail = run.rsplit(",", 1)[1]
                if commas == 1 and (len(tail) in (1, 2) or len(tail) >= 4):
                    eu += 1
                elif commas >= 2:
                    us += 1
            elif dots:
                head, _, tail = run.rpartition(".")
                if dots >= 2:
                    eu += 1  # 1.234.567
                elif len(tail) == 3 and 1 <= len(head) <= 3 and not head.startswith("0"):
                    ambiguous += 1  # 1.234 / 2.000: thousands or decimals?
                else:
                    us += 1  # 12.5 / 1200.75 / 0.500 / 1234.567
    return eu, us, ambiguous


def _decimal_convention(values: list[str]) -> dict[str, str]:
    """Infer European ``1.234,56`` numbers. Empty dict means the dot-decimal default.

    Parsing ``€1.200,50`` with the default convention used to yield 1.2005 while
    reporting nothing unparseable, so the convention is decided from the values.
    Only unambiguous evidence counts. If both conventions appear in one column the
    default is kept, so the values that do not fit it become visibly unparseable
    rather than silently wrong.
    """
    eu, us, _ambiguous = _convention_votes(values)
    if eu and not us:
        return {"decimal": ",", "thousands": "."}
    return {}


def _only_ambiguous_grouping(values: list[str]) -> bool:
    """Every separator-bearing value is ``1.234``-shaped and nothing says which it is."""
    eu, us, ambiguous = _convention_votes(values)
    return bool(ambiguous) and not eu and not us


@detector("currency", priority=45)
def detect_currency(series: pd.Series, ctx: DetectorContext) -> Issues:
    """Detect money-as-text columns and propose parsing to a float (+ currency split)."""
    issues = Issues()
    cp = ctx.column_profile
    if cp is None or cp.count == 0 or cp.semantic_type != "currency":
        return issues

    values = [v if isinstance(v, str) else str(v) for v in sample_non_null(series)]
    codes = {c for c in (_detect_currency_scalar(v, None) for v in values) if isinstance(c, str)}

    if _only_ambiguous_grouping(values):
        # "€1.234" is 1234 in German data and 1.234 in Irish data. A 1000x error is worse
        # than a column left as text, so report it and propose nothing.
        issues.add(
            "ambiguous_number_format",
            "Money values such as "
            f"{next(v for v in values if '.' in v)!r} could be thousands (1.234 = 1234) or "
            "decimals (1.234 = 1.234). Not guessed: add a parse_number op with "
            "decimal/thousands set for your data.",
            severity=Severity.WARNING,
            column=ctx.column,
            confidence=0.9,
            evidence={"examples": _cap_examples(values)},
        )
        return issues

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
