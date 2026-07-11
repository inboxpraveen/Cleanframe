"""Email and phone detectors.

Emails get the safe, universally-correct normalisation (trim + lowercase) plus an
invalid-address count. Phones get best-effort separator/country-code normalisation
driven by the ``phone_country_code`` option. Neither drops data — validation rules
(added by the planner for these semantic types) decide what happens to values that
still fail.
"""

from __future__ import annotations

import re

import pandas as pd

from ..issues import Issues, _cap_examples
from ..profile import EMAIL_RE, _name_hint
from ..types import Op, Severity
from .base import DetectorContext, detector


@detector("emails", priority=45)
def detect_emails(series: pd.Series, ctx: DetectorContext) -> Issues:
    """Normalise emails (trim + lowercase) and flag invalid addresses."""
    issues = Issues()
    cp = ctx.column_profile
    if cp is None or cp.count == 0:
        return issues
    if cp.semantic_type != "email" and not _name_hint(ctx.column or "", "email"):
        return issues

    values = [v for v in series.dropna().tolist() if isinstance(v, str)]
    if not values:
        return issues

    invalid = [v for v in values if not EMAIL_RE.match(v.strip().lower())]
    needs_norm = [v for v in values if v != v.strip().lower()]

    if needs_norm:
        issues.add(
            "email_normalization",
            f"{len(needs_norm)} email(s) need trimming/lowercasing",
            severity=Severity.INFO,
            confidence=0.95,
            evidence={"count": len(needs_norm), "examples": _cap_examples(needs_norm)},
            ops=[Op("normalize_email")],
        )
    if invalid:
        issues.add(
            "invalid_emails",
            f"{len(invalid)} value(s) are not valid email addresses",
            severity=Severity.ERROR if len(invalid) / len(values) > 0.02 else Severity.WARNING,
            confidence=1.0,
            evidence={"count": len(invalid), "examples": _cap_examples(invalid)},
        )
    return issues


@detector("phones", priority=45)
def detect_phones(series: pd.Series, ctx: DetectorContext) -> Issues:
    """Normalise phone-number formatting and flag implausible digit counts."""
    issues = Issues()
    cp = ctx.column_profile
    if cp is None or cp.count == 0:
        return issues
    if cp.semantic_type != "phone" and not _name_hint(ctx.column or "", "phone"):
        return issues

    values = [v if isinstance(v, str) else str(v) for v in series.dropna().tolist()]
    if not values:
        return issues

    digit_counts = [len(re.sub(r"\D", "", v)) for v in values]
    invalid = [v for v, d in zip(values, digit_counts, strict=False) if not (7 <= d <= 15)]
    # "Needs normalisation" = contains separators or lacks a leading +.
    messy = [v for v in values if re.search(r"[ ()\-.]", v) or not v.strip().startswith("+")]

    country_code = ctx.option("phone_country_code") or ctx.option("region")
    if messy:
        params = {"default_country_code": country_code} if country_code else {}
        issues.add(
            "phone_normalization",
            f"{len(messy)} phone number(s) have inconsistent formatting",
            severity=Severity.INFO,
            confidence=0.8,
            evidence={
                "count": len(messy),
                "examples": _cap_examples(messy),
                "country_code": country_code,
            },
            ops=[Op("normalize_phone", params)],
        )
    if invalid:
        issues.add(
            "invalid_phones",
            f"{len(invalid)} value(s) have an implausible number of digits",
            severity=Severity.WARNING,
            confidence=0.9,
            evidence={"count": len(invalid), "examples": _cap_examples(invalid)},
        )
    return issues


__all__ = ["detect_emails", "detect_phones"]
