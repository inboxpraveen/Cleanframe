"""Regressions from the second verification pass (the re-run of the adversarial probes)."""

from __future__ import annotations

import math
import warnings

import pandas as pd
import pytest

import cleanframe as cf
from cleanframe._numparse import parse_int_text, parse_number_text
from cleanframe.ops import _detect_currency_scalar
from cleanframe.recipe import Recipe


def _recipe(ops_yaml: str, col: str = "v") -> Recipe:
    return Recipe.from_yaml(f"version: 1\ncolumns:\n  {col}: {{ops: {ops_yaml}}}\n")


def _replay(df, rec):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return cf.apply_recipe(df, rec, check_drift=False)


# N-01: a numeric YAML key must still find the float a NaN turned an int column into
@pytest.mark.parametrize("dtype", ["float", "Int64"])
def test_numeric_normalize_values_keys_match_integral_floats(dtype):
    values = [1, 2, None, 3]
    col = pd.Series(values, dtype="Float64" if dtype == "float" else dtype).astype(
        "float64" if dtype == "float" else "Int64"
    )
    out = _replay(pd.DataFrame({"a": col}), _recipe("[{normalize_values: {1: one, 2: two}}]", "a"))
    assert out.dataframe["a"].tolist()[:2] == ["one", "two"]
    assert pd.isna(out.dataframe["a"].iloc[2]) and out.dataframe["a"].iloc[3] in (3, 3.0)


# N-02: parentheses and hyphens only mean "negative" around a bare number
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("N/A (2023)", 2023.0),
        ("Widget (5 pack)", 5.0),
        ("5 - see note", 5.0),
        ("Plan-B 3", 3.0),
        ("abc-5", -5.0),
        ("USD -5", -5.0),
        ("-$5", -5.0),
        ("$(8)", -8.0),
        ("(1,200)", -1200.0),
        ("1200-", -1200.0),
    ],
)
def test_sign_needs_bare_notation(text, expected):
    assert parse_number_text(text) == expected


# N-03
def test_overflow_and_negative_zero_and_python_literals():
    assert math.isnan(parse_number_text("9" * 400)) and math.isnan(parse_number_text("1e999"))
    assert str(parse_number_text("-0")) == "0.0" and str(parse_number_text("(0)")) == "0.0"
    assert parse_int_text("1_0") is None and parse_int_text("12") == 12


# N-04: 1.234 is 1234 in German data and 1.234 in Irish data: report it, do not guess
def test_dot_grouped_money_is_reported_not_guessed():
    df = pd.DataFrame({"price": ["€1.234", "€2.000", "€15", "€999"]})
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = cf.clean(df)
    assert [i.kind for i in result.issues] == ["ambiguous_number_format"]
    assert result.dataframe["price"].tolist() == df["price"].tolist()  # untouched


def test_other_evidence_still_resolves_dot_shapes():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        us = cf.clean(pd.DataFrame({"p": ["$1.234", "$12.50"]})).dataframe.iloc[:, 0].tolist()
        eu = cf.clean(pd.DataFrame({"p": ["€1.234", "€12,5"]})).dataframe.iloc[:, 0].tolist()
        small = cf.clean(pd.DataFrame({"p": ["$0.500", "$3.499"]})).dataframe.iloc[:, 0].tolist()
    assert us == [1.234, 12.5] and eu == [1234.0, 12.5] and small == [0.5, 3.499]


# N-05
@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("R$ 10", "BRL"), ("A$4", "AUD"), ("AR$ 3", "ARS"), ("MX$ 5", "MXN"), ("NT$5", "TWD"),
        ("$5 MXN", "MXN"), ("MXN 9", "MXN"), ("USD5", "USD"), ("CHF5", "CHF"), ("Rs. 500", "INR"),
        ("US$4", "USD"), ("$9", "USD"), ("Price€5", "EUR"),
    ],
)
def test_currency_code_detection(text, code):
    assert _detect_currency_scalar(text, None) == code


@pytest.mark.parametrize("text", ["XY$5", "kr 5", "Doors 5", "N/A"])
def test_unknown_currency_is_no_currency_not_usd(text):
    assert pd.isna(_detect_currency_scalar(text, None))


# N-07: the derived column name must not depend on which op ran first
def test_extract_currency_name_survives_a_preceding_unit_op():
    df = pd.DataFrame({"c0": ["5kg", "7kg"]})
    rec = _recipe("[{normalize_unit: kg}, extract_currency]", "c0")
    out = _replay(df, rec).dataframe
    assert "c0_currency" in out.columns and "None_currency" not in out.columns


# N-11
def test_one_odd_value_in_a_small_file_does_not_stop_a_replay():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        recipe = cf.clean(pd.DataFrame({"A": ["$1,200.50", "$3.25", "$40.00"] * 7})).recipe
    incoming = pd.DataFrame({"A": ["$1,200.50", "$3.25", "$40.00"] * 6 + ["$10-20"] * 1 + ["$5.00"] * 3})
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        cf.apply_recipe(incoming, recipe)  # 1 of 22 is 4.5%: below both the rate and the count gate


# N-12
def test_percent_and_currency_symbols_never_merge_into_bare_numbers():
    from cleanframe.detectors.categories import _cluster

    counts = {"10 %": 3, "10": 3, "$5": 2, "5": 2, "5 €": 1}
    assert _cluster(counts, None) == {}
    # ...while genuine spelling variants of a unit still merge
    assert _cluster({"10 kg": 5, "10 KG": 1}, None) == {"10 KG": "10 kg"}


# N-14
def test_yaml_null_on_fail_says_to_quote_it():
    with pytest.raises(cf.RecipeError, match='"null"'):
        Recipe.from_yaml(
            "version: 1\ncolumns: {}\nvalidate:\n  - {column: a, check: not_null, on_fail: null}\n"
        )


# F-15 leftover: a bare 2024 in a list is a column name
def test_numeric_looking_names_in_dedup_and_drop_lists():
    df = pd.DataFrame({"2024": [1, 1, 2], "x": ["a", "a", "b"]})
    rec = Recipe.from_yaml("version: 1\ncolumns: {}\ndedup: {subset: [2024]}\nframe_ops:\n  - drop_columns: [x]\n")
    out = _replay(df, rec).dataframe
    assert out.columns.tolist() == ["2024"] and len(out) == 2
    with pytest.raises(cf.RecipeError, match="quote it"):
        Recipe.from_yaml("version: 1\ncolumns: {}\ndedup: {subset: [yes]}\n")


# F-24 leftover
def test_excel_export_says_when_it_strips_control_characters(tmp_path):
    pytest.importorskip("openpyxl")
    df = pd.DataFrame({"t": ["ok", "bad\x07value"]})
    with pytest.warns(cf.CleanFrameWarning, match="control characters"):
        cf.write_frame(df, tmp_path / "o.xlsx")


# F-26 leftover: a deliberate to_na is not "could not parse"
def test_to_na_is_not_reported_as_a_parse_failure():
    df = pd.DataFrame({"v": ["a", "N/A", "b", "-"]})
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        cf.apply_recipe(df, _recipe("[to_na]"), check_drift=False)
    assert not any("could not parse" in str(w.message) for w in caught)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        cf.apply_recipe(pd.DataFrame({"v": ["1", "x"]}), _recipe("[parse_number]"), check_drift=False)
    assert any("could not parse" in str(w.message) for w in caught)


def test_more_unit_spellings():
    df = pd.DataFrame({"w": ["2 lbs", "3 pounds", "2 kgs", "12 ounces", "5 kilograms"]})
    got = _replay(df, _recipe("[{normalize_unit: g}]", "w")).dataframe["w"].tolist()
    assert got == pytest.approx([907.18474, 1360.77711, 2000.0, 340.19427750, 5000.0])


# N-08: pandas' chunked reader silently truncates an over-wide row; streaming must not
@pytest.mark.parametrize("chunk", [1, 100])
def test_streaming_refuses_a_row_wider_than_the_header(tmp_path, chunk):
    src = tmp_path / "wide.csv"
    src.write_text("name,amt,note\nn1,1,x\nn2,2,x,EXTRA\nn3,3,x\n", encoding="utf-8")
    rec = Recipe.from_yaml("version: 1\ncolumns:\n  name: {ops: [strip_whitespace]}\n")
    with pytest.raises(cf.CleanFrameError, match="4 fields but the header has 3"):
        cf.stream_apply(rec, src, tmp_path / "o.csv", chunksize=chunk, check_drift=False)
    with pytest.raises(cf.CleanFrameError):
        cf.apply_recipe(src, rec, check_drift=False)  # the whole-frame path refuses it too


def test_streaming_does_not_mistake_quoted_delimiters_for_extra_fields(tmp_path):
    src = tmp_path / "q.csv"
    src.write_text('name,note\nn1,"a, b, c"\nn2,"line one\nline two"\n', encoding="utf-8")
    rec = Recipe.from_yaml("version: 1\ncolumns:\n  name: {ops: [strip_whitespace]}\n")
    summary = cf.stream_apply(rec, src, tmp_path / "o.csv", chunksize=1, check_drift=False)
    assert summary.rows_in == 2


# N-10: a delimiter mismatch is reported as one, not as "the header is on line 2"
def test_read_frame_says_delimiter_not_title_row(tmp_path):
    src = tmp_path / "semi.csv"
    src.write_text("name;note\nAnn;hello, world\nBob;hi, there\n", encoding="utf-8")
    with pytest.raises(cf.CleanFrameError, match="semicolon-delimited"):
        cf.read_frame(src)


# N-09: the --json contract
def test_json_contract_gaps(tmp_path, capsys):
    import json

    from cleanframe.cli import main

    (tmp_path / "d.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        main(["clean", str(tmp_path / "d.csv"), "--mode", "nonsense", "--json"])
    assert exc.value.code == 2
    assert json.loads(capsys.readouterr().out)["status"] == "usage_error"

    assert main(["ops", "--json"]) == 0
    listing = json.loads(capsys.readouterr().out)
    assert any(o["name"] == "parse_number" for o in listing["ops"])
    assert main(["detectors", "--json"]) == 0
    assert any(d["name"] == "currency" for d in json.loads(capsys.readouterr().out)["detectors"])


# F-26 leftovers: invisible characters and NFC/NFD forms
def test_unicode_hygiene_is_detected_planned_exported_and_streamable(tmp_path):
    nfc, nfd = "caf\u00e9", "cafe\u0301"
    df = pd.DataFrame(
        {"city": [nfc, nfd, "Paris\u200b", "\ufeffLyon", "New\u00a0York", nfc, nfd, "Paris"] * 4,
         "n": range(32)}
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = cf.clean(df, mode="auto")
    assert any(i.kind == "unicode_hygiene" for i in result.issues)
    out = result.dataframe["city"].tolist()
    assert set(out) == {nfc, "Paris", "Lyon", "New York"}
    assert nfd not in out and not any("\u200b" in v or "\ufeff" in v or "\u00a0" in v for v in out)

    rec = _recipe("[normalize_unicode]", "city")
    ns: dict = {}
    exec(compile(cf.generate_code(rec), "<generated>", "exec"), ns)
    assert ns["clean"](df)["city"].tolist() == _replay(df, rec).dataframe["city"].tolist()

    cf.check_streamable(rec)
    src = tmp_path / "u.csv"
    df.to_csv(src, index=False, encoding="utf-8")
    cf.stream_apply(rec, src, tmp_path / "o.csv", chunksize=5, check_drift=False)
    assert pd.read_csv(tmp_path / "o.csv", encoding="utf-8")["city"].tolist() == _replay(df, rec).dataframe["city"].tolist()


def test_unicode_op_keeps_zero_width_joiners_and_validates_the_form():
    family = "\U0001f468\u200d\U0001f469\u200d\U0001f467"  # emoji ZWJ sequence
    out = _replay(pd.DataFrame({"t": [family]}), _recipe("[normalize_unicode]", "t")).dataframe["t"]
    assert out.tolist() == [family]
    with pytest.raises(cf.RecipeError, match="NFC"):
        _recipe("[{normalize_unicode: {form: nope}}]")
    assert _recipe("[{normalize_unicode: NFKC}]").columns[0].ops[0].params == {"form": "NFKC"}
