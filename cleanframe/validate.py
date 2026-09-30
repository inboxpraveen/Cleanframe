"""Post-clean validation with per-rule failure policies.

Validation runs *after* the recipe's transforms. Each rule computes a pass/fail
mask over a column; the rule's ``on_fail`` decides what happens to failing rows:

``quarantine`` (default)
    Move the row into a side ``quarantine`` frame with a reason — never silently
    dropped, never corrupting the clean output.
``error``
    Raise :class:`~cleanframe.errors.ValidationFailure`. ``strict`` mode promotes
    *every* policy to ``error``.
``warn`` / ``drop`` / ``null``
    Log only / discard the row / blank just the offending cell.

Checks are pluggable, like detectors::

    @cf.validator("valid_iban")
    def _(series): return series.isna() | series.str.match(IBAN_RE)
"""

from __future__ import annotations

import re
import warnings
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd
import yaml

from ._util import map_pure, safe_compile_regex
from .checks import (
    _CMP_RE,
    VALIDATOR_REGISTRY,
    check_is_known,
    list_validators,
    validator,
)
from .errors import CleanFrameWarning, RecipeError, ValidationFailure
from .types import Mode

if TYPE_CHECKING:  # annotations only — importing it at runtime would cycle
    from .recipe import ValidationRule

# ---------------------------------------------------------------------------
# Expression checks (comparisons / membership / regex)
# ---------------------------------------------------------------------------
_CMP_OPS: dict[str, Callable[[Any, float], Any]] = {
    ">=": lambda s, t: s >= t,
    "<=": lambda s, t: s <= t,
    ">": lambda s, t: s > t,
    "<": lambda s, t: s < t,
    "==": lambda s, t: s == t,
    "!=": lambda s, t: s != t,
}


def _comparison_mask(series: pd.Series, op: str, threshold: float) -> pd.Series:
    numeric = pd.to_numeric(series, errors="coerce")
    orig_na = series.isna()
    satisfies = _CMP_OPS[op](numeric, threshold)
    satisfies = satisfies.fillna(False).astype(bool)
    return orig_na | (numeric.notna() & satisfies)


def _membership_values(rule: ValidationRule) -> list[Any]:
    values = rule.params.get("values")
    if values is not None:
        return [values] if isinstance(values, str) else list(values)
    rest = rule.check[2:].strip()  # drop leading "in"
    # BaseLoader keeps every scalar a string: YAML 1.1 would read `in [Yes, No]`
    # as booleans and then reject every literal "Yes"/"No" in the data.
    parsed = yaml.load(rest, Loader=yaml.BaseLoader) if rest else []
    return list(parsed) if isinstance(parsed, (list, tuple)) else [parsed]


#: YAML 1.1 reads a bare ``Yes``/``No``/``On``/``Off`` as a boolean, so a rule written
#: ``values: [Yes, No]`` arrives here as ``[True, False]``. Accept every spelling of
#: the boolean rather than rejecting the literal text the data actually holds.
_BOOL_SPELLINGS: dict[bool, tuple[str, ...]] = {
    True: ("True", "true", "TRUE", "Yes", "yes", "YES", "On", "on", "Y", "y", "1"),
    False: ("False", "false", "FALSE", "No", "no", "NO", "Off", "off", "N", "n", "0"),
}


def _membership_mask(series: pd.Series, values: list[Any]) -> pd.Series:
    as_str: set[str] = set()
    for value in values:
        if isinstance(value, bool):
            as_str.update(_BOOL_SPELLINGS[value])
        else:
            as_str.add(str(value))
    return series.isna() | series.isin(values) | series.astype(str).isin(as_str)


def _regex_mask(series: pd.Series, pattern: str) -> pd.Series:
    try:
        compiled = safe_compile_regex(pattern)
    except ValueError as exc:
        raise RecipeError(str(exc)) from exc
    return series.isna() | map_pure(series, lambda v: bool(compiled.search(str(v))))


def pass_mask(rule: ValidationRule, series: pd.Series) -> pd.Series:
    """Compute the boolean pass-mask (True = passes) for ``rule`` over ``series``."""
    check = rule.check.strip()
    if check in VALIDATOR_REGISTRY:
        try:
            mask = VALIDATOR_REGISTRY[check](series, **rule.params)
        except TypeError as exc:
            raise RecipeError(
                f"Validation {check!r} on column {rule.column!r} does not accept "
                f"parameter(s) {sorted(rule.params)}: {exc}"
            ) from exc
        return mask.astype(bool)

    cmp = _CMP_RE.match(check)
    if cmp:
        return _comparison_mask(series, cmp.group(1), float(cmp.group(2))).astype(bool)

    if check == "in" or check.startswith("in ") or check.startswith("in["):
        return _membership_mask(series, _membership_values(rule)).astype(bool)

    if check.startswith("matches") or check.startswith("regex"):
        pattern = rule.params.get("pattern") or re.sub(r"^(matches|regex):?\s*", "", check)
        return _regex_mask(series, pattern).astype(bool)

    raise RecipeError(
        f"Unknown validation check {check!r}. Known: {', '.join(list_validators())}, "
        "comparisons (>= 0), 'in [...]', 'matches: <regex>'."
    )


# ---------------------------------------------------------------------------
# Results + application
# ---------------------------------------------------------------------------
@dataclass
class ValidationResult:
    column: str
    check: str
    on_fail: str
    n_failed: int
    failed_row_ids: list[int]
    found: bool = True

    @property
    def passed(self) -> bool:
        return self.n_failed == 0


@dataclass
class ValidationOutcome:
    dataframe: pd.DataFrame
    quarantine: pd.DataFrame
    results: list[ValidationResult]
    removed_rows: list[tuple[int, str]] = field(default_factory=list)
    log: list[str] = field(default_factory=list)


QUARANTINE_REASON_COL = "_cf_quarantine_reason"


def _reason_column(df: pd.DataFrame) -> str:
    """A free name for the quarantine reason, so a user column is never overwritten."""
    name = QUARANTINE_REASON_COL
    suffix = 2
    while name in df.columns:
        name = f"{QUARANTINE_REASON_COL}_{suffix}"
        suffix += 1
    return name


def evaluate(rule: ValidationRule, df: pd.DataFrame) -> ValidationResult:
    if rule.column not in df.columns:
        return ValidationResult(rule.column, rule.check, rule.on_fail, 0, [], found=False)
    series = df[rule.column]
    ok = pass_mask(rule, series)
    fail_mask = ~ok.to_numpy()
    failed_ids = [int(i) for i in series.index[fail_mask]]
    return ValidationResult(rule.column, rule.check, rule.on_fail, len(failed_ids), failed_ids)


def _effective_policy(on_fail: str, mode: Mode) -> str:
    # strict = zero tolerance: ANY failure raises, including drop/null which would
    # otherwise silently discard rows or blank cells — the opposite of "fail loud".
    if mode is Mode.STRICT and on_fail != "error":
        return "error"
    return on_fail


def apply_validations(
    df: pd.DataFrame, rules: list[ValidationRule], mode: Mode | str = Mode.REVIEW
) -> ValidationOutcome:
    """Evaluate all rules against a snapshot, then apply their failure policies."""
    mode = Mode.coerce(mode)
    work = df.copy()
    results = [evaluate(rule, work) for rule in rules]

    error_failures: list[ValidationResult] = []
    null_actions: list[tuple[str, list[int]]] = []
    quarantine_ids: set[int] = set()
    drop_ids: set[int] = set()
    reasons: dict[int, list[str]] = {}
    log: list[str] = []

    for rule, res in zip(rules, results, strict=True):
        if not res.found:
            if mode is Mode.STRICT:
                raise ValidationFailure(
                    f"Validation failed under strict policy: column {res.column!r} "
                    f"for check {res.check!r} is missing from the frame.",
                    failures=[res],
                )
            log.append(f"validation skipped: column {res.column!r} not found")
            warnings.warn(
                f"CleanFrame: validation {res.check!r} skipped — column {res.column!r} is "
                "not in the data. Use mode='strict' to fail instead.",
                CleanFrameWarning,
                stacklevel=2,
            )
            continue
        if res.passed:
            continue
        action = _effective_policy(rule.on_fail, mode)
        label = f"{res.column}:{res.check}"
        if action == "error":
            error_failures.append(res)
        elif action == "warn":
            log.append(f"warn: {res.n_failed} row(s) failed {label}")
        elif action == "null":
            null_actions.append((res.column, res.failed_row_ids))
            log.append(f"null: blanked {res.n_failed} cell(s) failing {label}")
        elif action == "drop":
            drop_ids.update(res.failed_row_ids)
            for rid in res.failed_row_ids:
                reasons.setdefault(rid, []).append(label)
            log.append(f"drop: removed {res.n_failed} row(s) failing {label}")
        else:  # quarantine (default)
            quarantine_ids.update(res.failed_row_ids)
            for rid in res.failed_row_ids:
                reasons.setdefault(rid, []).append(label)
            log.append(f"quarantine: held {res.n_failed} row(s) failing {label}")

    if error_failures:
        detail = "; ".join(f"{r.column}:{r.check} ({r.n_failed} failed)" for r in error_failures)
        raise ValidationFailure(
            f"Validation failed under {mode.value} policy: {detail}", failures=error_failures
        )

    # Snapshot quarantine BEFORE null-blanking so a row that fails both a null rule
    # and a quarantine rule is preserved with its original (failing) value for the
    # human to inspect — not blanked to NaN.
    quarantine_df = pd.DataFrame()
    if quarantine_ids:
        ordered = sorted(quarantine_ids)
        quarantine_df = work.loc[ordered].copy()
        quarantine_df[_reason_column(work)] = ["; ".join(reasons[i]) for i in ordered]

    for col, ids in null_actions:
        work.loc[ids, col] = np.nan

    removed = quarantine_ids | drop_ids
    removed_rows = [(rid, "; ".join(reasons.get(rid, ["validation"]))) for rid in sorted(removed)]
    if removed:
        work = work.drop(index=sorted(removed))

    return ValidationOutcome(
        dataframe=work,
        quarantine=quarantine_df,
        results=results,
        removed_rows=removed_rows,
        log=log,
    )


__all__ = [
    "ValidationResult",
    "ValidationOutcome",
    "validator",
    "list_validators",
    "check_is_known",
    "evaluate",
    "apply_validations",
    "pass_mask",
    "QUARANTINE_REASON_COL",
    "VALIDATOR_REGISTRY",
]
