"""Wave 2 — the vectorised _apply_str fast-path must equal the elementwise path.

The fast-path only fires when a column is provably all-string; it must never change
a value or a dtype vs the safe elementwise map (which the executor relies on).
"""
from __future__ import annotations

import pandas as pd

from cleanframe.executor import execute
from cleanframe.ops import apply_column_op
from cleanframe.recipe import Recipe
from cleanframe.types import Op

_TEXT_OPS = [
    "strip_whitespace",
    "collapse_whitespace",
    "lowercase",
    "uppercase",
    "title_case",
    "capitalize",
    "normalize_email",
]

_TRICKY = [
    "  Alice  ",
    "o'brien",
    "3RD STREET",
    "new   york",
    "café RESToré",
    "MCDONALD's",
    "已经",
    "a@B.COM",
]


def _elementwise(op_name, series):
    """The reference: force the elementwise path by disabling the fast-path."""
    import cleanframe.ops as ops

    orig = ops._is_pure_string
    ops._is_pure_string = lambda s: False
    try:
        return apply_column_op(Op(op_name), series).series
    finally:
        ops._is_pure_string = orig


def test_vectorized_matches_elementwise_on_pure_string():
    s = pd.Series(_TRICKY, dtype="object")
    for name in _TEXT_OPS:
        vec = apply_column_op(Op(name), s).series
        ref = _elementwise(name, s)
        pd.testing.assert_series_equal(vec, ref, check_dtype=True, obj=name)


def test_vectorized_matches_elementwise_with_nan_and_string_dtype():
    for dtype in ("object", "string"):
        s = pd.Series(["  X ", None, "y  Z"], dtype=dtype)
        for name in _TEXT_OPS:
            vec = apply_column_op(Op(name), s).series
            ref = _elementwise(name, s)
            pd.testing.assert_series_equal(vec, ref, check_dtype=True, obj=f"{name}/{dtype}")


def test_mixed_object_column_is_not_vectorized_and_preserves_non_strings():
    # A stray int in an object column must survive (the reason _apply_str is elementwise).
    s = pd.Series(["  a  ", 5, None, "B"], dtype="object")
    out = apply_column_op(Op("strip_whitespace"), s).series
    assert out.tolist()[0] == "a" and out.tolist()[1] == 5


def test_executor_output_unchanged_by_fastpath():
    df = pd.DataFrame({"name": ["  Alice  ", "BOB", "new  york"] * 50})
    recipe = Recipe.from_dict(
        {"version": 1, "columns": {"name": {"ops": ["strip_whitespace", "title_case"]}}}
    )
    out = execute(recipe, df).dataframe["name"].tolist()
    assert out[0] == "Alice" and out[2] == "New York"
