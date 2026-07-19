"""Wave 1 — Batch E: codegen must reproduce the executor exactly (invariant #6).

Each test drives a recipe through BOTH the executor and the generated standalone
code and asserts the primary output frames are identical. These cover the whole
family of codegen divergences the audit found (H7–H16, M8, M10, M13).
"""
from __future__ import annotations

import pandas as pd

from cleanframe.codegen import generate_code
from cleanframe.executor import execute
from cleanframe.recipe import Recipe


def _run_generated(recipe: Recipe, df: pd.DataFrame) -> pd.DataFrame:
    code = generate_code(recipe)
    ns: dict = {}
    exec(compile(code, "generated.py", "exec"), ns)  # noqa: S102 - testing generated code
    return ns["clean"](df.copy()).reset_index(drop=True)


def _assert_match(recipe_dict: dict, df: pd.DataFrame) -> None:
    recipe = Recipe.from_dict({"version": 1, **recipe_dict})
    ref = execute(recipe, df, mode="auto").dataframe.reset_index(drop=True)
    gen = _run_generated(recipe, df)
    pd.testing.assert_frame_equal(gen, ref)


def test_codegen_currency_full_symbols_and_known_codes():
    df = pd.DataFrame({"amount": ["₩5000", "₽100", "$50", "ABC 42", "¢99"]})
    _assert_match({"columns": {"amount": {"ops": [{"extract_currency": "cur"}]}}}, df)


def test_codegen_parse_number_edge_cases():
    df = pd.DataFrame({"n": ["−1200", "12-34", "1.5e3", "(500)", "1234.56-", "1,20,000"]})
    _assert_match({"columns": {"n": {"ops": ["parse_number"]}}}, df)


def test_codegen_to_na_empty_string():
    df = pd.DataFrame({"c": ["", "   ", "n/a", "keep", "x"]})
    _assert_match({"columns": {"c": {"ops": [{"to_na": {"tokens": ["", "n/a"]}}]}}}, df)


def test_codegen_to_na_default_tokens():
    df = pd.DataFrame({"c": ["", "NA", "value", "-", "unknown"]})
    _assert_match({"columns": {"c": {"ops": ["to_na"]}}}, df)


def test_codegen_normalize_unit_bare_numbers_and_aliases():
    df = pd.DataFrame({"w": ["5", "500 grams", "2 kilos", "1 yd", "3.0"]})
    _assert_match({"columns": {"w": {"ops": [{"normalize_unit": "g"}]}}}, df)


def test_codegen_cast_bool_and_aliases():
    for target in ("bool", "boolean", "int64", "number", "text", "date"):
        df = pd.DataFrame({"v": ["yes", "no", "1", "0", "maybe"]})
        _assert_match({"columns": {"v": {"ops": [{"cast": target}]}}}, df)


def test_codegen_parse_date_output_aliases_and_yearfirst():
    df = pd.DataFrame({"d": ["2024-01-31", "31/01/2024", "bad", "2024-02-01"]})
    _assert_match({"columns": {"d": {"ops": [{"parse_date": {"output": "iso"}}]}}}, df)
    _assert_match({"columns": {"d": {"ops": [{"parse_date": {"output": "date"}}]}}}, df)


def test_codegen_validation_quarantine_row_filtering():
    df = pd.DataFrame({"email": ["a@b.com", "bad", "c@d.com", "nope", "e@f.com"], "x": [1, 2, 3, 4, 5]})
    _assert_match(
        {"validate": [{"column": "email", "check": "valid_email", "on_fail": "quarantine"}]}, df
    )


def test_codegen_ignore_case_dedup():
    df = pd.DataFrame({"g": ["Alice", "alice", "ALICE", "Bob", "bob"], "x": [1, 2, 3, 4, 5]})
    _assert_match({"dedup": {"subset": ["g"], "ignore_case": True}}, df)


def test_codegen_multi_rule_validation_unique_union():
    """Multiple rules must be evaluated against ONE snapshot then unioned — not applied
    sequentially — so `unique` matches the executor (which sees all rows)."""
    df = pd.DataFrame(
        {"email": ["a@b.com", "a@b.com", "c@d.com", "bad", "e@f.com"], "x": [1, 2, 3, 4, 5]}
    )
    _assert_match(
        {
            "validate": [
                {"column": "email", "check": "valid_email", "on_fail": "drop"},
                {"column": "email", "check": "unique", "on_fail": "drop"},
            ]
        },
        df,
    )


def test_codegen_comparison_validation():
    df = pd.DataFrame({"age": ["5", "-3", "40", "-1", "22"]})
    _assert_match(
        {
            "columns": {"age": {"ops": [{"cast": "int"}]}},
            "validate": [{"column": "age", "check": ">= 0", "on_fail": "drop"}],
        },
        df,
    )
