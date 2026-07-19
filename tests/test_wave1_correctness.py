"""Wave 1 — Batch C: detector / op correctness regression tests."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import cleanframe as cf
from cleanframe.errors import SchemaError
from cleanframe.executor import execute
from cleanframe.ops import _parse_number_scalar
from cleanframe.recipe import Recipe


def _apply(recipe: dict, df: pd.DataFrame) -> pd.DataFrame:
    return execute(Recipe.from_dict({"version": 1, **recipe}), df).dataframe


# --- H3: ambiguous DD/MM vs MM/DD must not silently swap day/month ------------
def test_month_first_dates_not_swapped():
    """H3: a provably month-first column must parse every value as month-first."""
    df = pd.DataFrame({"signup_date": ["01/13/2024", "01/02/2024", "01/09/2024", "01/25/2024"]})
    out = cf.clean(df, mode="auto").dataframe["signup_date"].tolist()
    # All are January; 01/02 must be Jan 2 (not Feb 1) and 01/09 must be Jan 9.
    assert out == ["2024-01-13", "2024-01-02", "2024-01-09", "2024-01-25"]


# --- M5: datetime with a time component must not be truncated to date ----------
def test_datetime_time_component_preserved():
    """M5: a column with time-of-day must not be silently truncated to date-only."""
    df = pd.DataFrame(
        {"ts": ["2024-01-31 12:34:56", "2024-02-01 08:00:00", "2024-03-15 23:59:59"]}
    )
    out = cf.clean(df, mode="auto").dataframe["ts"].tolist()
    assert "12:34:56" in out[0], out


# --- format-less parse_date must be deterministic & keep valid dates -----------
def test_formatless_parse_date_keeps_all_valid_dates():
    """A hand-written parse_date with no formats must not null valid ISO dates."""
    df = pd.DataFrame({"d": ["31/01/2024", "2024-01-31", "2024/12/01", "07/08/2024"]})
    out = _apply({"columns": {"d": {"ops": [{"parse_date": {}}]}}}, df)["d"].tolist()
    assert out[0] == "2024-01-31"
    assert out[1] == "2024-01-31"
    assert out[2] == "2024-12-01"  # was silently nulled before the fix
    assert not any(pd.isna(v) for v in out), out


# --- H4: fuzzy category clustering must not merge both-frequent antonyms -------
def test_frequent_antonyms_not_merged():
    """H4: 'insured' and 'uninsured' (both frequent) must stay distinct."""
    df = pd.DataFrame({"status": ["insured", "uninsured"] * 5})
    out = set(cf.clean(df, mode="auto").dataframe["status"])
    assert {"insured", "uninsured"} <= out, out


def test_rare_typo_still_merged():
    """H4 guard must still merge a rare typo into its frequent canonical."""
    df = pd.DataFrame({"city": ["Bangalore"] * 19 + ["Banglore"]})
    out = set(cf.clean(df, mode="auto").dataframe["city"])
    assert out == {"Bangalore"}, out


# --- M6: parse_number must reject fused digit groups & handle scientific -------
def test_parse_number_scientific_notation():
    assert _parse_number_scalar("1.5e3", ".", ",", []) == 1500.0
    assert _parse_number_scalar("2.3E+05", ".", ",", []) == 230000.0


def test_parse_number_rejects_fused_digit_groups():
    assert np.isnan(_parse_number_scalar("12ab34", ".", ",", []))
    assert np.isnan(_parse_number_scalar("10-12", ".", ",", []))


def test_parse_number_preserves_existing_cases():
    assert _parse_number_scalar("₹1,20,000", ".", ",", []) == 120000.0
    assert _parse_number_scalar("1200 INR", ".", ",", []) == 1200.0
    assert _parse_number_scalar("1234.56-", ".", ",", []) == -1234.56
    assert _parse_number_scalar("(500)", ".", ",", []) == -500.0


# --- M7: ambiguous disguised-null tokens must not auto-convert in review -------
def test_ambiguous_null_token_not_auto_converted():
    """M7: 'none' (a legit value) must survive review-mode clean; 'n/a' still nulls."""
    df = pd.DataFrame({"allergy": ["peanuts", "none", "shellfish", "n/a", "dairy"]})
    out = cf.clean(df, mode="review").dataframe["allergy"].tolist()
    assert "none" in out, out  # ambiguous token preserved
    assert pd.isna(out[3]), out  # unambiguous 'n/a' converted


# --- M16: unknown schema dtype must raise, not silently no-op ------------------
def test_unknown_schema_dtype_raises():
    with pytest.raises(SchemaError):
        cf.Schema.from_dict({"version": 1, "columns": {"amount": {"dtype": "flaot"}}})


# --- M11: ReDoS alternation shapes must be rejected ---------------------------
def test_redos_overlapping_alternation_rejected():
    from cleanframe._util import safe_compile_regex

    with pytest.raises(ValueError):
        safe_compile_regex("(a|aa)+$")
    # A safe, non-overlapping alternation must still compile.
    safe_compile_regex("(cat|dog)s?")
