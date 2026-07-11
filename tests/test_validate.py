"""Validators and failure policies (quarantine / error / null / drop / strict)."""

from __future__ import annotations

import pandas as pd
import pytest

from cleanframe.errors import ValidationFailure
from cleanframe.recipe import ValidationRule
from cleanframe.validate import apply_validations, evaluate, list_validators, validator


def df():
    return pd.DataFrame(
        {"email": ["a@b.com", "bad", "c@d.com"], "amount": [10, -5, 20]}
    ).reset_index(drop=True)


def test_valid_email_check():
    res = evaluate(ValidationRule("email", "valid_email"), df())
    assert res.n_failed == 1 and res.failed_row_ids == [1]


def test_comparison_check():
    res = evaluate(ValidationRule("amount", ">= 0"), df())
    assert res.failed_row_ids == [1]


def test_quarantine_moves_rows_aside():
    rules = [ValidationRule("email", "valid_email", "quarantine")]
    out = apply_validations(df(), rules, mode="review")
    assert len(out.dataframe) == 2
    assert len(out.quarantine) == 1
    assert "_cf_quarantine_reason" in out.quarantine.columns


def test_error_policy_raises():
    rules = [ValidationRule("amount", ">= 0", "error")]
    with pytest.raises(ValidationFailure):
        apply_validations(df(), rules, mode="review")


@pytest.mark.parametrize("policy", ["quarantine", "warn", "drop", "null"])
def test_strict_escalates_every_policy_to_error(policy):
    # strict = zero tolerance: even drop/null must raise, not silently lose data.
    rules = [ValidationRule("amount", ">= 0", policy)]
    with pytest.raises(ValidationFailure):
        apply_validations(df(), rules, mode="strict")


def test_null_policy_blanks_cell_not_row():
    rules = [ValidationRule("amount", ">= 0", "null")]
    out = apply_validations(df(), rules, mode="review")
    assert len(out.dataframe) == 3  # no rows removed
    assert pd.isna(out.dataframe.loc[1, "amount"])


def test_drop_policy_removes_rows():
    rules = [ValidationRule("email", "valid_email", "drop")]
    out = apply_validations(df(), rules, mode="review")
    assert len(out.dataframe) == 2 and out.quarantine.empty


def test_membership_check():
    data = pd.DataFrame({"city": ["NYC", "LA", "Mars"]})
    res = evaluate(ValidationRule("city", "in", params={"values": ["NYC", "LA"]}), data)
    assert res.failed_row_ids == [2]


def test_missing_column_is_skipped_not_crashed():
    res = evaluate(ValidationRule("nope", "not_null"), df())
    assert res.found is False and res.n_failed == 0


def test_quarantine_preserves_original_value_under_null_and_quarantine():
    # Regression: a row failing both a null rule and a quarantine rule must be
    # quarantined with its ORIGINAL value, not blanked to NaN.
    data = pd.DataFrame({"a": [1, -5, 3], "b": ["x", None, "z"]})
    rules = [ValidationRule("a", ">= 0", "null"), ValidationRule("b", "not_null", "quarantine")]
    out = apply_validations(data, rules, mode="review")
    assert out.quarantine.loc[1, "a"] == -5  # original preserved for inspection


def test_custom_validator_plugin():
    @validator("is_short")
    def _is_short(series):
        return series.isna() | (series.astype(str).str.len() <= 3)

    try:
        assert "is_short" in list_validators()
        data = pd.DataFrame({"code": ["ab", "toolong", "xy"]})
        res = evaluate(ValidationRule("code", "is_short"), data)
        assert res.failed_row_ids == [1]
    finally:
        from cleanframe.validate import VALIDATOR_REGISTRY

        VALIDATOR_REGISTRY.pop("is_short", None)
