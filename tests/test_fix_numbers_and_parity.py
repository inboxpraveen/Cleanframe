"""Regressions for the 2026-09 review: silent wrong numbers and executor/codegen parity.

Every case here produced a wrong result with no warning before the fix.
"""

from __future__ import annotations

import math
import warnings

import numpy as np
import pandas as pd
import pytest

import cleanframe as cf
from cleanframe._numparse import parse_number_text, parse_plain_number, split_number_unit
from cleanframe.recipe import Recipe


def _recipe(ops_yaml: str, col: str = "v") -> Recipe:
    return Recipe.from_yaml(f"version: 1\ncolumns:\n  {col}: {{ops: {ops_yaml}}}\n")


def _exec_generated(recipe: Recipe, df: pd.DataFrame) -> pd.DataFrame:
    ns: dict = {}
    exec(compile(cf.generate_code(recipe), "<generated>", "exec"), ns)
    return ns["clean"](df)


def _same(a: pd.Series, b: pd.Series) -> bool:
    def eq(x, y):
        if pd.isna(x) or pd.isna(y):
            return bool(pd.isna(x) and pd.isna(y))
        return x == y

    return len(a) == len(b) and all(eq(x, y) for x, y in zip(a.tolist(), b.tolist(), strict=False))


# --------------------------------------------------------------------------- parser
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("-$5", -5.0),
        ("$-5", -5.0),
        ("-€3.50", -3.5),
        ("−$7", -7.0),  # unicode minus
        ("-₹500", -500.0),
        ("$(8)", -8.0),
        ("($9)", -9.0),
        ("(1,200)", -1200.0),
        ("1200-", -1200.0),
        ("₹1,20,000", 120000.0),
        ("$1,000", 1000.0),
        ("１２３", 123.0),  # full-width digits
        ("1 234", None),  # NBSP is not the default thousands separator
        ("5​0", 50.0),  # zero-width space inside a number
        ("1200 INR", 1200.0),
        ("12ab34", None),
        ("10-12", None),
        ("2,5", None),  # not valid grouping: refuse, do not read as 25
        ("1,2,3", None),
        ("1.234,56", None),  # wrong convention for the default: refuse
        ("", None),
    ],
)
def test_parse_number_text(text, expected):
    got = parse_number_text(text)
    if expected is None:
        assert math.isnan(got), got
    else:
        assert got == expected


def test_parse_number_european_convention():
    assert parse_number_text("€1.200,50", decimal=",", thousands=".") == 1200.5
    assert parse_number_text("2,5", decimal=",", thousands=".") == 2.5
    assert math.isnan(parse_number_text("1,200.50", decimal=",", thousands="."))


def test_plain_number_refuses_ambiguity():
    assert parse_plain_number("1,500") is None  # 1500 or 1.5?
    assert parse_plain_number("1,5") == 1.5
    assert parse_plain_number("12.5") == 12.5
    assert parse_plain_number("0.500") == 0.5  # a leading zero cannot be a group
    assert parse_plain_number("2 lbs") is None


def test_split_number_unit_ambiguity():
    assert split_number_unit("5kg") == (5.0, "kg")
    assert split_number_unit("1,5 kg") == (1.5, "kg")
    assert split_number_unit("1,500 g") is None


# ------------------------------------------------------------- planner end to end
def test_signed_money_survives_the_planned_path():
    df = pd.DataFrame({"price": ["-$5", "$10", "-€3.50", "€4", "-₹500", "₹300", "$1,000"]})
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        out = cf.clean(df).dataframe.iloc[:, 0].tolist()
    assert out == [-5.0, 10.0, -3.5, 4.0, -500.0, 300.0, 1000.0]


def test_european_column_is_read_as_european_not_as_thousands():
    df = pd.DataFrame({"price": ["€1,234", "€2,5", "€15", "€999,99"]})
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        out = cf.clean(df).dataframe.iloc[:, 0].tolist()
    assert out == [1.234, 2.5, 15.0, 999.99]  # never 25.0 / 99999.0


def test_mixed_conventions_become_visible_nulls_not_wrong_numbers():
    df = pd.DataFrame({"price": ["$1,234.50", "€2,5", "$3.25"]})
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        out = cf.clean(df).dataframe
    values = out[[c for c in out.columns if "price" in c][0]].tolist()
    assert 25.0 not in values
    assert any("could not parse" in str(w.message) for w in caught)


def test_dollar_family_currencies_are_not_all_usd():
    from cleanframe.ops import _detect_currency_scalar

    got = {v: _detect_currency_scalar(v, None) for v in ("R$ 10", "A$4", "C$5", "HK$5", "NZ$ 3", "$9")}
    assert got == {
        "R$ 10": "BRL", "A$4": "AUD", "C$5": "CAD", "HK$5": "HKD", "NZ$ 3": "NZD", "$9": "USD",
    }


# ---------------------------------------------------------- categories never merge numbers
def test_categories_do_not_merge_different_numbers_in_strict_mode():
    df = pd.DataFrame(
        {
            "amount": ["1,200", "-1,200", "300", "-300", "1,200", "12.00", "1,200", "300", "450", "2,000"],
            "sku": ["A-1", "A1", "A-1", "B2", "B-2", "B2", "C3", "D4", "B2", "C3"],
        }
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = cf.clean(df, mode="strict")
    yaml_text = result.recipe.to_yaml()
    assert "-1,200" not in yaml_text.split("columns:")[1] or "normalize_values" not in yaml_text
    assert result.dataframe.shape[0] == 10  # nothing silently dropped
    assert result.dataframe["amount"].tolist() == df["amount"].tolist()


def test_categories_still_merge_case_and_typo_variants():
    df = pd.DataFrame({"city": ["Pune", "pune", "PUNE", "Delhi", "delhi", "Pune", "Pune", "Delhi"]})
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        out = cf.clean(df, mode="auto").dataframe["city"].tolist()
    assert set(out) == {"Pune", "Delhi"}


# ------------------------------------------------------------------ drift (A1)
def test_new_number_format_stops_a_replay():
    train = pd.DataFrame({"Amount": ["$1,200.50", "$3.25", "$40.00"]})
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        recipe = cf.clean(train).recipe
    incoming = pd.DataFrame({"Amount": ["€1.200,50", "€3,25", "€40,00"]})
    with pytest.raises(cf.DriftError) as exc:
        cf.apply_recipe(incoming, recipe)
    assert "number format" in str(exc.value)
    assert exc.value.report.by_kind("number_format_drift")


def test_a_stray_junk_value_is_info_not_a_stop():
    train = pd.DataFrame({"Amount": ["$1,200.50", "$3.25", "$40.00"] * 40})
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        recipe = cf.clean(train).recipe
    incoming = pd.DataFrame({"Amount": ["$1,200.50", "$3.25", "$40.00"] * 40 + ["12ab34"]})
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        cf.apply_recipe(incoming, recipe)  # must not raise


# ------------------------------------------------------------------- diff (F-08)
def test_diff_reports_int_to_float_precision_loss():
    df = pd.DataFrame({"v": [9007199254740993, 2**63 - 1, 5]})
    result = cf.apply_recipe(df, _recipe("[{cast: float}]"), check_drift=False)
    changed = {c.before for c in result.diff.changes}
    assert changed == {9007199254740993, 2**63 - 1}


def test_diff_reports_bool_cast_of_ones_and_zeros():
    df = pd.DataFrame({"v": [1, 0, 1]}).astype(object)
    result = cf.apply_recipe(df, _recipe("[{cast: bool}]"), check_drift=False)
    assert result.diff.changed_cells == 3


def test_cast_int_is_exact_for_large_integer_text():
    df = pd.DataFrame({"v": ["9007199254740993", "12", "abc", None]})
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        out = cf.apply_recipe(df, _recipe("[{cast: int}]"), check_drift=False).dataframe["v"]
    assert out.iloc[0] == 9007199254740993
    assert out.iloc[1] == 12
    assert pd.isna(out.iloc[2]) and pd.isna(out.iloc[3])


# --------------------------------------------------------------- units (F-12)
def test_units_refuse_ambiguous_grouping_instead_of_guessing():
    df = pd.DataFrame({"w": ["5kg", "1,500 g", "1.5e3 g", "2 lbs", "1,5 kg", "0.500 kg", "7", 7.0]})
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        out = cf.apply_recipe(df, _recipe("[{normalize_unit: g}]", "w"), check_drift=False)
    got = out.dataframe["w"].tolist()
    assert got[0] == 5000.0 and math.isnan(got[1]) and got[2] == 1500.0
    assert got[3] == pytest.approx(907.18474)  # "2 lbs" is a unit spelling, not garbage
    assert got[4] == 1500.0 and got[5] == 500.0
    assert got[6] == 7.0 and got[7] == 7.0


# ------------------------------------------------ codegen == executor (F-04 / F-10)
PARITY_CASES = [
    ("[parse_date]", ["05/01/2024", "2024-12-31", "Jan 3 2024", "3/4/24", None, "2024-01-01T02:00:00+05:30"]),
    ("[{cast: datetime}]", ["05/01/2024", "2024-12-31", None]),
    ("[{parse_date: {formats: ['%Y-%m-%dT%H:%M:%S%z'], output: datetime}}]",
     ["2024-01-01T10:00:00+05:30", "2024-01-02T10:00:00+05:30"]),
    ("[{to_na: {tokens: [na], case_insensitive: false}}]", ["NA", "na", " na ", "x"]),
    ("[{normalize_values: {map: {'5': five}, case_insensitive: true}}]", [5, "5", "x", None]),
    ("[{normalize_values: {'1': one}}]", [1, 2, 3]),
    ("[parse_number]", ["-$5", "$(8)", "1,200", "2,5", "1.234,56", "１２３", None]),
    ("[{cast: int}]", ["9007199254740993", "12", "abc", None]),
    ("[{normalize_unit: g}]", ["5kg", "1,500 g", "1,5 kg", "0.500 kg", "2 lbs", "7"]),
]


@pytest.mark.parametrize(("ops", "values"), PARITY_CASES)
def test_generated_code_matches_the_executor(ops, values):
    df = pd.DataFrame({"v": values})
    rec = _recipe(ops)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        expected = cf.apply_recipe(df, rec, check_drift=False).dataframe["v"]
        got = _exec_generated(rec, df)["v"]
    assert _same(expected, got), (expected.tolist(), got.tolist())


def test_generated_code_handles_integer_and_missing_columns():
    rec = Recipe.from_yaml(
        "version: 1\ncolumns:\n  '0': {ops: [strip_whitespace]}\n  ghost: {ops: [lowercase]}\n"
        "validate:\n  - {column: nowhere, check: not_null, on_fail: quarantine}\n"
    )
    df = pd.DataFrame({0: [" a ", "b"], 1: [1, 2]})
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        expected = cf.apply_recipe(df, rec, check_drift=False).dataframe
        got = _exec_generated(rec, df)
    assert expected.columns.tolist() == got.columns.tolist()
    assert expected["0"].tolist() == got["0"].tolist()


def test_naive_wallclock_is_kept_for_offset_timestamps():
    df = pd.DataFrame({"v": ["2024-01-01T02:00:00+05:30", "2024-03-10T00:30:00+05:30"]})
    out = cf.apply_recipe(df, _recipe("[parse_date]"), check_drift=False).dataframe["v"]
    assert out.tolist() == ["2024-01-01", "2024-03-10"]  # not shifted to the UTC day


def test_nan_is_still_nan():
    assert np.isnan(parse_number_text(None)) and np.isnan(parse_number_text(float("nan")))
    assert np.isnan(parse_number_text(pd.NA)) and np.isnan(parse_number_text(True))


# ----------------------------------------------------- dedup / title / phone (F-19..21)
def test_dedup_does_not_treat_booleans_as_the_numbers_they_equal():
    df = pd.DataFrame({"flag": [1, True, 0, False, 1.0], "k": ["a", "a", "b", "b", "a"]})
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = cf.clean(df)
    # only (1, 'a') vs (1.0, 'a') is a genuine duplicate; True and False are kept
    assert len(result.dataframe) == 4
    assert result.diff.summary()["rows_dropped"] == 1


def test_single_column_frames_are_not_deduplicated_automatically():
    df = pd.DataFrame({"reading": [1, 0, 1, 1, 0, 0, 1]})
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        assert len(cf.clean(df).dataframe) == 7


def test_title_case_survives_ordinals_and_apostrophes():
    df = pd.DataFrame({"t": ["3rd street", "o'neil mcdonald", "new  YORK", "don't", "jean-luc", None]})
    rec = _recipe("[title_case]", "t")
    out = cf.apply_recipe(df, rec, check_drift=False).dataframe["t"].tolist()
    assert out == ["3rd Street", "O'Neil Mcdonald", "New York", "Don't", "Jean-Luc", None]
    assert _same(pd.Series(out), _exec_generated(rec, df)["t"])


def test_phone_international_prefix_and_short_numbers():
    df = pd.DataFrame({"p": ["0044 20 7946 0958", "12345", "98765 43210", "+1 415 555 0100"]})
    rec = _recipe("[{normalize_phone: '+91'}]", "p")
    out = cf.apply_recipe(df, rec, check_drift=False).dataframe["p"].tolist()
    assert out == ["+442079460958", "12345", "+919876543210", "+14155550100"]
    assert _same(pd.Series(out), _exec_generated(rec, df)["p"])


# ------------------------------------------------- recipe loading strictness (F-15/F-17)
@pytest.mark.parametrize("key", ["yes", "no", "on", "off", "null", "~", "010", "0x10", "12:30", "1_000"])
def test_plain_yaml_keys_stay_column_names(key):
    rec = Recipe.from_yaml(f"version: 1\ncolumns:\n  {key}:\n    ops: [strip_whitespace]\n")
    assert rec.columns[0].source == key


def test_a_yaml_bool_token_in_to_na_is_refused_with_a_hint():
    with pytest.raises(cf.RecipeError, match="quote it"):
        Recipe.from_yaml("version: 1\ncolumns:\n  a:\n    ops:\n      - to_na: [no, null]\n")


@pytest.mark.parametrize(
    "ops",
    [
        "[{parse_number: {decimal: 5}}]",
        "[{parse_number: {decimal: ',', thousands: ','}}]",
        "[{replace: {pattern: yes, repl: x}}]",
        "[{replace: {pattern: '(a)', repl: '\\2'}}]",
    ],
)
def test_bad_op_parameters_fail_at_load(ops):
    with pytest.raises(cf.RecipeError):
        _recipe(ops)


def test_ops_may_be_a_single_name_and_version_may_not_be_a_bool():
    rec = Recipe.from_yaml("version: 1\ncolumns:\n  a:\n    ops: strip_whitespace\n")
    assert [o.name for o in rec.columns[0].ops] == ["strip_whitespace"]
    with pytest.raises(cf.RecipeError):
        Recipe.from_yaml("version: true\ncolumns: {}\n")


def test_unknown_option_key_warns_with_a_suggestion():
    df = pd.DataFrame({"a": ["1", "2"]})
    with pytest.warns(cf.CleanFrameWarning, match="did you mean 'dayfirst'"):
        cf.clean(df, options={"dayfrist": True})


# ------------------------------------------------------------------------------ CLI
def test_cli_clean_refuses_to_replace_an_edited_recipe(tmp_path, capsys):
    from cleanframe.cli import main

    src = tmp_path / "d.csv"
    src.write_text("Name,City\nAnn,pune\nBob,Delhi\n", encoding="utf-8")
    assert main(["clean", str(src)]) == 0
    recipe = tmp_path / "d.recipe.yaml"
    assert main(["clean", str(src)]) == 0  # unchanged input regenerates the same bytes
    recipe.write_text(recipe.read_text(encoding="utf-8") + "# MY HAND EDIT\n", encoding="utf-8")
    assert main(["clean", str(src)]) == 1
    assert "MY HAND EDIT" in recipe.read_text(encoding="utf-8")
    assert main(["clean", str(src), "--overwrite"]) == 0
    assert "MY HAND EDIT" not in recipe.read_text(encoding="utf-8")


def test_cli_header_row_flag(tmp_path):
    from cleanframe.cli import main

    src = tmp_path / "t.csv"
    src.write_text("Quarterly report\nName,City\nAnn,Pune\nBob,Delhi\n", encoding="utf-8")
    out = tmp_path / "o.csv"
    assert main(["clean", str(src), "--header-row", "1", "--out", str(out)]) == 0
    assert out.read_text(encoding="utf-8").splitlines()[0] == "name,city"


# ------------------------------------------------------- misc review fixes (A2, A9, F-25/26)
def test_suggest_update_learns_the_new_format_not_a_competing_reading():
    train = pd.DataFrame({"signup_date": ["01/02/2026", "15/03/2026", "03/03/2026"]})
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        recipe = cf.clean(train).recipe
        new = pd.DataFrame({"signup_date": ["01/02/2026", "03/03/2026", "Jan 5, 26"]})
        patched, _ = cf.suggest_update(new, recipe)
    assert "%b %d, %y" in patched.columns[0].ops[0].params["formats"]
    assert "%m/%d/%Y" not in patched.columns[0].ops[0].params["formats"]
    assert not cf.detect_drift(new, patched).has_drift


def test_recipe_equals_its_own_round_trip():
    df = pd.read_csv("examples/messy_customers.csv")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        recipe = cf.clean(df).recipe
    assert Recipe.from_yaml(recipe.to_yaml()) == recipe


def test_generate_code_rejects_a_bad_function_name():
    rec = _recipe("[strip_whitespace]")
    for bad in ("1 bad; import os", "class", "a-b"):
        with pytest.raises(cf.CleanFrameError):
            cf.generate_code(rec, func_name=bad)


def test_emitting_over_an_existing_column_warns():
    df = pd.DataFrame({"price": ["$5", "€6"], "cur": ["keep", "me"]})
    rec = _recipe("[{extract_currency: cur}]", "price")
    with pytest.warns(cf.CleanFrameWarning, match="overwrites the existing column"):
        cf.apply_recipe(df, rec, check_drift=False)


def test_a_lone_sign_is_not_escaped_but_a_formula_is():
    from cleanframe._util import sanitize_csv_value

    assert sanitize_csv_value("-") == "-" and sanitize_csv_value("+") == "+"
    assert sanitize_csv_value("-5") == "-5"
    assert sanitize_csv_value("=SUM(A1)") == "'=SUM(A1)"
    assert sanitize_csv_value("-2+3") == "'-2+3"


def test_generated_module_leads_with_the_readable_function():
    src = cf.generate_code(_recipe("[strip_whitespace]"))
    assert src.index("def clean(") < src.index("Helpers: the same parsing code")
    ns: dict = {}
    exec(compile(src, "<generated>", "exec"), ns)  # helpers defined below still resolve at call time
    assert ns["clean"](pd.DataFrame({"v": [" a "]}))["v"].tolist() == ["a"]
