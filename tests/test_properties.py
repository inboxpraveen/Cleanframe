"""Property-based tests for CleanFrame's stated invariants (see CONTRIBUTING.md).

Example-based tests check the cases somebody thought of; these generate the rest.
Each property is one of the load-bearing promises: the parser never crashes and never
flips a sign, the generated code equals the executor, recipes round-trip, replay is
deterministic, and streaming equals whole-frame replay.
"""

from __future__ import annotations

import os
import warnings

import pandas as pd
import pytest

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

import cleanframe as cf  # noqa: E402
from cleanframe._numparse import parse_number_text  # noqa: E402
from cleanframe.recipe import Recipe  # noqa: E402

# CI runs a small deterministic sample. To hunt for new bugs locally:
#   CLEANFRAME_PROP_EXAMPLES=2000 CLEANFRAME_PROP_RANDOM=1 pytest tests/test_properties.py
PROFILE = settings(
    max_examples=int(os.environ.get("CLEANFRAME_PROP_EXAMPLES", "80")),
    deadline=None,
    derandomize=not os.environ.get("CLEANFRAME_PROP_RANDOM"),
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)

# Text that is hostile on purpose: unicode, whitespace, digits, signs, separators, quotes.
_ALPHABET = st.text(
    alphabet=st.sampled_from(
        list("abcXYZ019 ,.-+()$€₹£%'\"")
        + ["\t", "\n", "\u00a0", "\u200b", "\u2212", "٣", "１", "２", "é", "\u0301"]
    ),
    max_size=14,
)
TEXT = st.one_of(_ALPHABET, st.sampled_from(["NA", "N/A", "-", "n/a", "1,234", "€1.200,50", "(5)"]))
CELL = st.one_of(st.none(), TEXT, st.integers(-10**6, 10**6), st.floats(allow_nan=True, width=32))


def _exec_generated(recipe: Recipe, df: pd.DataFrame) -> pd.DataFrame:
    ns: dict = {}
    exec(compile(cf.generate_code(recipe), "<generated>", "exec"), ns)
    return ns["clean"](df)


def _same(a: pd.Series, b: pd.Series) -> bool:
    def eq(x, y):
        if pd.isna(x) or pd.isna(y):
            return bool(pd.isna(x) and pd.isna(y))
        return x == y

    return len(a) == len(b) and all(eq(x, y) for x, y in zip(a.tolist(), b.tolist(), strict=True))


# ----------------------------------------------------------------- number parser
@PROFILE
@given(TEXT)
def test_parse_number_never_raises_and_returns_a_float(text):
    got = parse_number_text(text)
    assert isinstance(got, float)


@PROFILE
@given(
    st.integers(-10**9, 10**9),
    st.sampled_from(["", "$", "€", "₹", "£"]),
    st.sampled_from(["minus_first", "minus_after_symbol", "parens", "trailing"]),
)
def test_formatted_integers_round_trip_with_the_right_sign(n, symbol, style):
    body = f"{abs(n):,}"
    if n >= 0:
        text = symbol + body
    elif style == "minus_first":
        text = "-" + symbol + body
    elif style == "minus_after_symbol":
        text = symbol + "-" + body
    elif style == "parens":
        text = symbol + "(" + body + ")"
    else:
        text = symbol + body + "-"
    assert parse_number_text(text) == float(n), text


@PROFILE
@given(st.integers(0, 10**9), st.integers(0, 99))
def test_european_and_us_formats_agree_when_the_convention_is_declared(whole, cents):
    us = f"{whole:,}.{cents:02d}"
    eu = f"{whole:,}".replace(",", ".") + f",{cents:02d}"
    assert parse_number_text(us) == parse_number_text(eu, decimal=",", thousands=".")


# --------------------------------------------------- generated code == executor
_TEXT_OPS = [
    "strip_whitespace", "collapse_whitespace", "lowercase", "uppercase", "title_case",
    "capitalize", "normalize_email", "parse_number", "to_na", "normalize_phone",
    "{normalize_unit: g}", "{cast: float}", "{cast: int}", "{cast: bool}",
    "{normalize_values: {map: {a: b}, case_insensitive: true}}",
    "{round: 1}", "{remove_symbols: ['$', ',']}", "normalize_unicode", "{normalize_unicode: NFKC}",
    "{extract_currency: cur}",
]


@PROFILE
@given(st.lists(CELL, min_size=1, max_size=12), st.sampled_from(_TEXT_OPS))
def test_generated_code_equals_the_executor(cells, op):
    df = pd.DataFrame({"v": pd.Series(cells, dtype="object")})
    rec = Recipe.from_yaml(f"version: 1\ncolumns:\n  v: {{ops: [{op}]}}\n")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        expected = cf.apply_recipe(df, rec, check_drift=False).dataframe["v"]
        got = _exec_generated(rec, df)["v"]
    assert _same(expected, got), (op, cells, expected.tolist(), got.tolist())


@PROFILE
@given(
    st.lists(
        st.sampled_from(
            ["05/01/2024", "2024-12-31", "Jan 3 2024", "3/4/24", "31/01/2024", "1 Jan 2024", "", "x"]
        ),
        min_size=1,
        max_size=8,
    ),
    st.booleans(),
)
def test_generated_date_code_equals_the_executor(cells, dayfirst):
    df = pd.DataFrame({"v": cells})
    rec = Recipe.from_yaml(
        f"version: 1\ncolumns:\n  v: {{ops: [{{parse_date: {{dayfirst: {str(dayfirst).lower()}}}}}]}}\n"
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        expected = cf.apply_recipe(df, rec, check_drift=False).dataframe["v"]
        got = _exec_generated(rec, df)["v"]
    assert _same(expected, got), (cells, expected.tolist(), got.tolist())


# ------------------------------------------------ planning: determinism + round trip
_FRAMES = st.lists(TEXT, min_size=3, max_size=25)


@PROFILE
@given(_FRAMES, _FRAMES)
def test_clean_is_deterministic_and_its_recipe_round_trips(a, b):
    n = min(len(a), len(b))
    df = pd.DataFrame({"a": a[:n], "b": b[:n]})
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        first = cf.clean(df)
        second = cf.clean(df)
    assert first.recipe.to_yaml() == second.recipe.to_yaml()
    assert first.dataframe.equals(second.dataframe)
    reloaded = Recipe.from_yaml(first.recipe.to_yaml())
    assert reloaded == first.recipe
    assert reloaded.to_yaml() == first.recipe.to_yaml()


@PROFILE
@given(_FRAMES, _FRAMES)
def test_replaying_the_recipe_reproduces_the_clean(a, b):
    n = min(len(a), len(b))
    df = pd.DataFrame({"a": a[:n], "b": b[:n]})
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        planned = cf.clean(df)
        replayed = cf.apply_recipe(df, planned.recipe, check_drift=False)
    assert planned.dataframe.reset_index(drop=True).equals(replayed.dataframe.reset_index(drop=True))


@PROFILE
@given(_FRAMES, _FRAMES)
def test_nothing_is_silently_dropped(a, b):
    """Every input row is either kept, or listed in the diff/quarantine with a reason."""
    n = min(len(a), len(b))
    df = pd.DataFrame({"a": a[:n], "b": b[:n]})
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = cf.clean(df, mode="auto")
    accounted = len(result.dataframe) + len(result.diff.dropped_rows)
    assert accounted == len(df), (len(df), len(result.dataframe), result.diff.dropped_rows)


# --------------------------------------------------------- streaming == whole frame
@PROFILE
@given(st.lists(TEXT, min_size=4, max_size=30), st.integers(1, 7))
def test_streaming_equals_whole_frame_replay(tmp_path_factory, cells, chunk):
    tmp = tmp_path_factory.mktemp("s")
    src = tmp / "in.csv"
    df = pd.DataFrame({"a": cells, "b": ["x"] * len(cells)})
    df.to_csv(src, index=False)
    rec = Recipe.from_yaml(
        "version: 2\nread: {text: true}\ncolumns:\n  a: {ops: [strip_whitespace, lowercase]}\n"
    )
    out = tmp / "out.csv"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        whole = cf.apply_recipe(src, rec, check_drift=False).dataframe
        cf.stream_apply(rec, src, out, chunksize=chunk, check_drift=False, overwrite=True)
    streamed = pd.read_csv(out, dtype=str, keep_default_na=False)
    # The streamed CSV is written through the formula-injection guard; so is the reference.
    from cleanframe._util import sanitize_dataframe_for_csv

    expected = sanitize_dataframe_for_csv(
        whole.astype(str).replace({"nan": ""}).reset_index(drop=True)
    )
    assert streamed.reset_index(drop=True).equals(expected)
