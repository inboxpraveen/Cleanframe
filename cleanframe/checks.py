"""The validation check registry: named checks and the load-time name guard.

Split out of :mod:`cleanframe.validate` for one reason. A recipe rejects an unknown
check the moment it loads, so :mod:`cleanframe.recipe` needs :func:`check_is_known`;
but ``validate`` needs :class:`~cleanframe.recipe.ValidationRule` to do its work.
Keeping the registry here means the dependency runs one way only — ``recipe`` and
``validate`` both import ``checks``, and neither imports the other at runtime.

A check is a function of a Series returning a boolean pass-mask, where ``True``
means the value is acceptable::

    @cf.validator("valid_iban")
    def _(series): return series.isna() | series.str.match(IBAN_RE)

NaN should usually pass; ``not_null`` is the check for missingness.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import pandas as pd

from ._util import map_pure
from .errors import RecipeError
from .profile import EMAIL_RE, URL_RE

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
#: name -> function(series, **params) -> boolean pass-mask (True = ok).
VALIDATOR_REGISTRY: dict[str, Callable[..., pd.Series]] = {}


def validator(name: str) -> Callable[[Callable], Callable]:
    """Register a named validation check. The function returns a pass-mask."""

    def decorator(func: Callable) -> Callable:
        if name in VALIDATOR_REGISTRY:
            raise ValueError(f"Validator {name!r} is already registered.")
        VALIDATOR_REGISTRY[name] = func
        return func

    return decorator


def list_validators() -> list[str]:
    """Every registered check name, sorted."""
    return sorted(VALIDATOR_REGISTRY)


# ---------------------------------------------------------------------------
# Built-in checks
# ---------------------------------------------------------------------------
_PHONE_DIGITS = re.compile(r"\D")


@validator("not_null")
def _not_null(series: pd.Series) -> pd.Series:
    return series.notna()


@validator("unique")
def _unique(series: pd.Series) -> pd.Series:
    duplicated = series.duplicated(keep=False) & series.notna()
    return ~duplicated


@validator("valid_email")
def _valid_email(series: pd.Series) -> pd.Series:
    def ok(v: Any) -> bool:
        return bool(EMAIL_RE.match(str(v).strip().lower()))

    return series.isna() | map_pure(series, ok)


@validator("valid_url")
def _valid_url(series: pd.Series) -> pd.Series:
    return series.isna() | map_pure(series, lambda v: bool(URL_RE.match(str(v).strip())))


@validator("valid_phone")
def _valid_phone(series: pd.Series) -> pd.Series:
    def ok(v: Any) -> bool:
        return 7 <= len(_PHONE_DIGITS.sub("", str(v))) <= 15

    return series.isna() | map_pure(series, ok)


# ---------------------------------------------------------------------------
# Load-time name guard
# ---------------------------------------------------------------------------
#: A comparison expression, e.g. ``">= 0"``. Shared with the mask builder.
_CMP_RE = re.compile(r"^(>=|<=|==|!=|>|<)\s*(-?\d+(?:\.\d+)?)$")

#: Expression forms that are checks in their own right rather than a registry name.
_EXPRESSION_PREFIXES = ("in ", "in[", "matches", "regex")


def check_is_known(check: str) -> None:
    """Raise :class:`RecipeError` for a check that is neither registered nor an expression.

    Called at recipe *load* time so a typo like ``valid_emial`` fails before the
    recipe is applied to production data.
    """
    text = str(check).strip()
    if not text:
        raise RecipeError("A validation rule needs a non-empty 'check'.")
    if text in VALIDATOR_REGISTRY or _CMP_RE.match(text) or text == "in":
        return
    if text.startswith(_EXPRESSION_PREFIXES):
        return
    raise RecipeError(
        f"Unknown validation check {check!r}. Known: {', '.join(list_validators())}, "
        "comparisons (>= 0), 'in [...]', 'matches: <regex>'. Register a custom check "
        "with @cleanframe.validator before loading the recipe."
    )


__all__ = [
    "VALIDATOR_REGISTRY",
    "validator",
    "list_validators",
    "check_is_known",
]
