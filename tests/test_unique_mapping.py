"""Evaluating a pure op once per distinct value must be indistinguishable from per cell."""

from __future__ import annotations

import warnings

import numpy as np
import pandas as pd
import pytest

import cleanframe as cf
from cleanframe import _util
from cleanframe.recipe import Recipe

POOL = [
    "  Alice Smith ", "bob jones", "₹1,20,000", "$1,200.50", "(5)", "-$5", "€1.200,50", "2,5",
    "5kg", "1,500 g", "1.5e3 g", "2 lbs", "+91 98765 43210", "0044 20 7946 0958", "12345",
    "a@x.com", "not-an-email", "https://x.io", "N/A", "", "  ", "Bengaluru", "BLR", "1 Jan 2024",
    "31/01/2024", "2024-12-31", "05/06/2024", "x",
]

OPS = [
    "parse_number",
    "{normalize_unit: g}",
    "{normalize_phone: '+91'}",
    "{extract_currency: cur}",
    "{normalize_values: {BLR: Bengaluru, blr: Bengaluru}}",
    "{normalize_values: {map: {blr: Bengaluru}, case_insensitive: true}}",
    "{parse_date: {formats: ['%Y-%m-%d', '%d/%m/%Y', '%d %b %Y']}}",
]


def _frame(n: int = 6000, seed: int = 7, missing: bool = True) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    values = rng.choice(np.array(POOL, dtype=object), size=n)
    if missing:
        values[rng.random(n) < 0.05] = None
    return pd.DataFrame({"v": pd.Series(values, dtype=object)})


def _replay(df, op):
    rec = Recipe.from_yaml(f"version: 1\ncolumns:\n  v: {{ops: [{op}]}}\n")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return cf.apply_recipe(df, rec, check_drift=False)


@pytest.mark.parametrize("op", OPS)
def test_unique_path_equals_per_cell_path(op, monkeypatch):
    df = _frame()
    fast = _replay(df, op)
    monkeypatch.setattr(_util, "_UNIQUE_MAP_MIN_ROWS", 10**9)  # force the per-cell path
    slow = _replay(df, op)
    pd.testing.assert_frame_equal(fast.dataframe, slow.dataframe)
    assert fast.diff.changed_cells == slow.diff.changed_cells


def test_unique_path_is_taken_for_repetitive_text_and_skipped_otherwise(monkeypatch):
    calls = []
    series = pd.Series(["a", "b"] * 3000, dtype=object)
    _util.map_pure(series, lambda v: calls.append(v) or v)
    assert len(calls) == 3  # "a", "b" and the trailing fn(NaN)

    calls.clear()
    near_unique = pd.Series([f"v{i}" for i in range(6000)], dtype=object)
    _util.map_pure(near_unique, lambda v: calls.append(v) or v)
    assert len(calls) == 6000  # nothing to share: plain per-cell map

    calls.clear()
    mixed = pd.Series([1, True, 1.0, "1"] * 1000, dtype=object)  # 1 == True == 1.0
    _util.map_pure(mixed, lambda v: calls.append(v) or v)
    assert len(calls) == 4000  # never collapse cells that only *compare* equal


def test_missing_cells_use_the_function_result_for_nan():
    series = pd.Series(["a", None, "a", None] * 1000, dtype=object)
    out = _util.map_pure(series, lambda v: "NA-cell" if v != v else v.upper())
    assert out.tolist()[:4] == ["A", "NA-cell", "A", "NA-cell"]
    assert out.index.equals(series.index)


def test_validators_agree_on_large_columns(monkeypatch):
    df = pd.DataFrame({"e": pd.Series((["a@x.com", "bad", None, "b@y.org"]) * 1500, dtype=object)})
    rec = Recipe.from_yaml(
        "version: 1\ncolumns: {}\nvalidate:\n  - {column: e, check: valid_email, on_fail: quarantine}\n"
    )
    fast = cf.apply_recipe(df, rec, check_drift=False)
    monkeypatch.setattr(_util, "_UNIQUE_MAP_MIN_ROWS", 10**9)
    slow = cf.apply_recipe(df, rec, check_drift=False)
    pd.testing.assert_frame_equal(fast.dataframe, slow.dataframe)
    pd.testing.assert_frame_equal(fast.quarantine, slow.quarantine)
