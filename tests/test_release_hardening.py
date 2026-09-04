"""Regression tests for the 0.3.0 hardening pass.

Grouped by the promise each one protects: no silent corruption, no silent input,
no traceback in the user's face, and no unsafe artifact.
"""

from __future__ import annotations

import io
import time
import warnings
from pathlib import Path

import pandas as pd
import pytest

import cleanframe as cf
from cleanframe.cli import EXIT_DRIFT, EXIT_ERROR, EXIT_OK, main
from cleanframe.errors import CleanFrameError, OutputError, RecipeError, SchemaError
from cleanframe.recipe import Recipe


def _csv(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


# ===========================================================================
# Nothing is silently corrupted
# ===========================================================================
def test_phone_column_read_as_float_keeps_its_digits(tmp_path):
    p = _csv(tmp_path, "phones.csv", "Name,Phone\nA,9876543210\nB,\nC,8005550100\n")
    out = cf.clean(p, mode="auto").dataframe
    assert out["phone"].dropna().tolist() == ["9876543210", "8005550100"]


def test_phone_extension_is_not_fused_into_the_number():
    from cleanframe.ops import normalize_phone

    assert normalize_phone(pd.Series(["080-1234-5678 ext 12"])).tolist() == ["08012345678"]


def test_timestamps_are_not_treated_as_phone_numbers():
    stamps = [f"2024-01-{i + 1:02d}T10:00:00Z" for i in range(20)]
    result = cf.clean(pd.DataFrame({"Updated At": stamps}), mode="auto")
    ops = [op.name for col in result.recipe.columns for op in col.ops]
    assert "normalize_phone" not in ops
    assert result.dataframe.iloc[0, 0] != "20240101100000"


def test_dashed_identifier_is_not_treated_as_a_phone_number():
    refs = [f"2024-{i:06d}-01" for i in range(1, 21)]
    result = cf.clean(pd.DataFrame({"Order Ref": refs}), mode="auto")
    ops = [op.name for col in result.recipe.columns for op in col.ops]
    assert "normalize_phone" not in ops


def test_european_decimals_are_parsed_with_the_right_convention():
    df = pd.DataFrame({"eu_amount": ["€1.200,50", "€2.000,00", "€999,99", "€12.345,67"]})
    out = cf.clean(df, mode="auto").dataframe.iloc[:, 0].tolist()
    assert out == [1200.5, 2000.0, 999.99, 12345.67]


def test_indian_and_us_grouping_still_parse():
    inr = cf.clean(pd.DataFrame({"Amount": ["₹1,20,000", "₹1,200", "₹50,000"]}), mode="auto")
    assert inr.dataframe.iloc[:, 0].tolist() == [120000.0, 1200.0, 50000.0]
    usd = cf.clean(pd.DataFrame({"Amount": ["$1,200", "$3,400", "$500"]}), mode="auto")
    assert usd.dataframe.iloc[:, 0].tolist() == [1200.0, 3400.0, 500.0]


def test_negated_category_is_never_merged_into_its_opposite():
    df = pd.DataFrame({"approved": ["Approved"] * 10 + ["Unapproved"] * 3})
    out = cf.clean(df, mode="auto").dataframe.iloc[:, 0]
    assert set(out) == {"Approved", "Unapproved"}


def test_genuine_typo_is_still_merged():
    df = pd.DataFrame({"city": ["Bangalore"] * 10 + ["Banglore"]})
    assert set(cf.clean(df, mode="auto").dataframe.iloc[:, 0]) == {"Bangalore"}


def test_punctuation_only_values_are_not_category_variants():
    df = pd.DataFrame({"Notes": ["a", "-", "?", "b", "c"]})
    out = cf.clean(df, mode="auto").dataframe.iloc[:, 0].tolist()
    assert "-" in out and "?" in out


def test_short_uppercase_code_wins_a_frequency_tie():
    df = pd.DataFrame({"State": ["CA", "ca", "CA", "ca", "NY", "ny", "TX", "TX", "TX"]})
    assert set(cf.clean(df, mode="auto").dataframe.iloc[:, 0]) == {"CA", "NY", "TX"}


def test_na_in_a_country_code_column_is_not_converted():
    df = pd.DataFrame({"Country": ["NA", "US", "IN", "DE", "FR"]})
    out = cf.clean(df, mode="auto").dataframe.iloc[:, 0].tolist()
    assert "NA" in out


def test_membership_check_compares_text_not_yaml_booleans():
    recipe = {"version": 1, "validate": [{"column": "n", "check": "in [Yes, No]"}]}
    result = cf.apply_recipe(pd.DataFrame({"n": ["Yes", "No", "maybe"]}), recipe, check_drift=False)
    assert result.dataframe["n"].tolist() == ["Yes", "No"]
    assert len(result.quarantine) == 1


def test_ragged_row_does_not_shift_columns(tmp_path):
    p = _csv(tmp_path, "ragged.csv", "name,date,amount\nA,2024-01-31,1200,\nB,2024-02-01,5,\n")
    out = cf.clean(p, mode="auto").dataframe
    assert out["name"].tolist() == ["A", "B"]


def test_mixed_day_and_month_first_dates_report_their_casualties():
    df = pd.DataFrame({"d": ["31/01/2024", "01/31/2024", "05/06/2024", "12/25/2024"]})
    result = cf.clean(df, mode="auto")
    evidence = [i.evidence for i in result.issues if i.detector == "dates"]
    assert evidence and evidence[0]["unparsed"] == 2


def test_unparseable_values_are_logged_not_silent():
    recipe = {"version": 1, "columns": {"Amount": {"ops": ["parse_number"]}}}
    with pytest.warns(cf.CleanFrameWarning, match="became missing"):
        result = cf.apply_recipe(
            pd.DataFrame({"Amount": ["₹1,200", "abc"]}), recipe, check_drift=False
        )
    assert any("became missing" in line for line in result.log)


def test_quarantine_reason_never_overwrites_a_user_column():
    df = pd.DataFrame({"_cf_quarantine_reason": ["u1", "u2"], "Email": ["bad", "ok@x.io"]})
    recipe = {"version": 1, "validate": [{"column": "Email", "check": "valid_email"}]}
    q = cf.apply_recipe(df, recipe, check_drift=False).quarantine
    assert q["_cf_quarantine_reason"].tolist() == ["u1"]
    assert "_cf_quarantine_reason_2" in q.columns


def test_skiprows_counts_data_rows_across_every_format(tmp_path):
    frame = pd.DataFrame({"a": [1, 2, 3, 4, 5], "b": list("vwxyz")})
    frame.to_csv(tmp_path / "t.csv", index=False)
    frame.to_json(tmp_path / "t.json", orient="records")
    frame.to_excel(tmp_path / "t.xlsx", index=False)
    for name in ("t.csv", "t.json", "t.xlsx"):
        assert cf.read_frame(tmp_path / name, skiprows=2)["a"].tolist() == [3, 4, 5], name
        assert cf.read_frame(tmp_path / name, skiprows=[1, 2])["a"].tolist() == [3, 4, 5], name


def test_negative_selection_is_refused(tmp_path):
    p = _csv(tmp_path, "t.csv", "a\n1\n2\n")
    with pytest.raises(CleanFrameError):
        cf.read_frame(p, skiprows=-1)
    with pytest.raises(CleanFrameError):
        cf.read_frame(p, nrows=-1)


def test_profiling_a_long_text_value_is_not_quadratic():
    from cleanframe.profile import _looks_date

    start = time.perf_counter()
    _looks_date("x" * 32_000)
    assert time.perf_counter() - start < 2.0


def test_unhashable_and_categorical_columns_profile():
    assert cf.clean(pd.DataFrame({"l": [[1, 2], [3], None], "s": ["a ", "b", "c"]})).dataframe.shape
    cats = pd.Categorical(["red", "blue", "green", "red", "red"])
    assert cf.clean(pd.DataFrame({"c": cats})).dataframe.shape[1] == 1


# ===========================================================================
# Nothing is silently accepted
# ===========================================================================
@pytest.mark.parametrize(
    "op",
    [
        {"parse_date": {"format": ["%d/%m/%Y"]}},
        {"parse_date": {"formats": "%d/%m/%Y"}},
        {"to_na": {"token": ["x"]}},
        {"parse_number": {"thousand": "."}},
        {"remove_symbols": 5},
        {"cast": "flaot"},
        {"normalize_unit": "furlong"},
        {"normalize_phone": {"country": "+91"}},
    ],
)
def test_unknown_or_wrongly_typed_op_parameters_are_refused(op):
    with pytest.raises(RecipeError):
        Recipe.from_dict({"version": 1, "columns": {"a": {"ops": [op]}}})


def test_round_accepts_both_documented_forms():
    for raw in ({"round": 2}, {"round": {"decimals": 2}}):
        recipe = Recipe.from_dict({"version": 1, "columns": {"a": {"ops": [raw]}}})
        assert recipe.columns[0].ops[0].params == {"decimals": 2}
    with pytest.raises(RecipeError):
        Recipe.from_dict({"version": 1, "columns": {"a": {"ops": [{"round": "two"}]}}})


def test_schema_str_alias_is_accepted():
    assert cf.Schema.from_dict({"columns": {"c": {"dtype": "str"}}}).columns[0].dtype == "string"


def test_unquoted_yaml_yes_no_still_matches_the_text():
    recipe = Recipe.from_yaml(
        "version: 1\nvalidate:\n  - {column: n, check: in, values: [Yes, No]}\n"
    )
    frame = pd.DataFrame({"n": ["Yes", "No", "maybe"]})
    result = cf.apply_recipe(frame, recipe, check_drift=False)
    assert result.dataframe["n"].tolist() == ["Yes", "No"]
    namespace: dict = {}
    exec(compile(cf.generate_code(recipe), "generated", "exec"), namespace)
    assert namespace["clean"](frame.copy())["n"].tolist() == ["Yes", "No"]


def test_real_booleans_still_match_a_membership_rule():
    recipe = {"version": 1, "validate": [{"column": "b", "check": "in", "values": [True, False]}]}
    frame = pd.DataFrame({"b": [True, False]})
    assert cf.apply_recipe(frame, recipe, check_drift=False).dataframe["b"].tolist() == [True, False]


def test_text_artifacts_are_written_atomically(tmp_path):
    from cleanframe._util import write_text

    target = tmp_path / "sub" / "note.txt"
    assert write_text(target, "hello").read_text(encoding="utf-8") == "hello"
    assert not (tmp_path / "sub" / "note.txt.cf-tmp").exists()
    with pytest.raises(OutputError):
        write_text(tmp_path, "cannot write over a directory")


def test_streaming_can_opt_into_overwriting_its_input(tmp_path):
    p = _csv(tmp_path, "s.csv", "a\n x\n y\n")
    recipe = Recipe.from_dict({"version": 1, "columns": {"a": {"ops": ["strip_whitespace"]}}})
    cf.stream_apply(recipe, p, p, check_drift=False, overwrite=True)
    assert p.read_text(encoding="utf-8").splitlines() == ["a", "x", "y"]


def test_reading_sheet_names_releases_the_workbook(tmp_path):
    book = tmp_path / "book.xlsx"
    pd.DataFrame({"a": [1]}).to_excel(book, index=False)
    assert cf.read_workbook(book, ["Sheet1"])
    # A leaked handle would make replacing the file fail on Windows.
    cf.write_frame(pd.DataFrame({"a": [2]}), book, source=book, overwrite=True)
    assert pd.read_excel(book)["a"].tolist() == [2]


def test_an_op_and_its_argument_written_as_two_list_items_are_joined():
    """Models write `["extract_currency", "cast", "float"]`; the intent is unambiguous."""
    recipe = Recipe.from_dict(
        {"version": 1, "columns": {"Amount": {"ops": ["extract_currency", "cast", "float"]}}}
    )
    assert [(op.name, op.params.get("to")) for op in recipe.columns[0].ops] == [
        ("extract_currency", None),
        ("cast", "float"),
    ]
    # Two real ops in a row are never joined.
    plain = Recipe.from_dict(
        {"version": 1, "columns": {"a": {"ops": ["strip_whitespace", "lowercase"]}}}
    )
    assert [op.name for op in plain.columns[0].ops] == ["strip_whitespace", "lowercase"]
    # An op that needs a value followed by another op stays an error.
    with pytest.raises(RecipeError):
        Recipe.from_dict({"version": 1, "columns": {"a": {"ops": ["cast", "lowercase"]}}})


def test_documented_aliases_still_load():
    recipe = Recipe.from_dict(
        {"version": 1, "columns": {"d": {"ops": [{"parse_date": {"allowed": ["%d/%m/%Y"]}}]}}}
    )
    assert recipe.columns[0].ops[0].params["formats"] == ["%d/%m/%Y"]
    phone = Recipe.from_dict(
        {"version": 1, "columns": {"p": {"ops": [{"normalize_phone": {"country_code": "+91"}}]}}}
    )
    assert phone.columns[0].ops[0].params["default_country_code"] == "+91"


def test_normalize_values_mapping_is_data_not_parameters():
    recipe = Recipe.from_dict(
        {"version": 1, "columns": {"c": {"ops": [{"normalize_values": {"BLR": "Bangalore"}}]}}}
    )
    assert recipe.columns[0].ops[0].params["map"] == {"BLR": "Bangalore"}


@pytest.mark.parametrize(
    "raw",
    [
        {"version": "abc"},
        {"version": 1, "meta": "x"},
        {"version": 1, "read": {"foo": 1}},
        {"version": 1, "columns": {"a": {"rename_to": 5}}},
        {"version": 1, "dedup": {"keep": "bogus"}},
        {"version": 1, "dedup": {"case_insensitive": True}},
        {"version": 1, "validate": [{"column": "e", "check": "valid_emial"}]},
    ],
)
def test_malformed_recipes_are_refused_at_load(raw):
    with pytest.raises(RecipeError):
        Recipe.from_dict(raw)


def test_duplicate_yaml_keys_are_refused():
    with pytest.raises(RecipeError, match="[Dd]uplicate"):
        Recipe.from_yaml("version: 1\ncolumns:\n  a: {ops: [lowercase]}\n  a: {ops: [uppercase]}\n")
    from cleanframe._util import load_yaml

    with pytest.raises(SchemaError, match="[Dd]uplicate"):
        load_yaml("columns:\n  a: string\n  a: float\n", error=SchemaError, what="schema")


def test_schema_dtype_aliases_drive_the_same_cast():
    df = pd.DataFrame({"qty": ["1", "2", "3"]})
    for alias, expected in (("int", "Int64"), ("integer", "Int64"), ("number", "float64")):
        result = cf.clean(df, target_schema={"columns": {"qty": {"dtype": alias}}}, mode="auto")
        assert str(result.dataframe["qty"].dtype) == expected, alias


def test_validator_rejects_parameters_it_does_not_accept():
    recipe = {"version": 1, "validate": [{"column": "e", "check": "not_null", "foo": 1}]}
    with pytest.raises(RecipeError, match="not_null"):
        cf.apply_recipe(pd.DataFrame({"e": ["x"]}), recipe, check_drift=False)


def test_on_drift_typo_raises_instead_of_disabling_the_guard(messy_df):
    recipe = cf.clean(messy_df, mode="auto").recipe
    with pytest.raises(CleanFrameError, match="on_drift"):
        cf.apply_recipe(messy_df, recipe, on_drift="bogus")


def test_mode_and_option_typos_raise_cleanframe_errors():
    df = pd.DataFrame({"a": [1]})
    with pytest.raises(CleanFrameError, match="Unknown mode"):
        cf.clean(df, mode="bogus")
    with pytest.raises(CleanFrameError, match="options"):
        cf.clean(df, options="notadict")
    with pytest.raises(CleanFrameError, match="planner"):
        cf.clean(df, planner="rules")


def test_duplicate_and_blank_csv_headers_are_refused(tmp_path):
    with pytest.raises(CleanFrameError, match="duplicate"):
        cf.read_frame(_csv(tmp_path, "dup.csv", "name,amount,name\nA,1,B\n"))
    with pytest.raises(CleanFrameError, match="empty column name"):
        cf.read_frame(_csv(tmp_path, "blank.csv", "name,,amount\nA,x,1\n"))


def test_unsupported_input_extension_is_refused(tmp_path):
    p = tmp_path / "x.xml"
    p.write_text("<root/>", encoding="utf-8")
    with pytest.raises(CleanFrameError, match="Unsupported input format"):
        cf.read_frame(p)


def test_lower_level_functions_reject_the_wrong_type():
    with pytest.raises(CleanFrameError):
        cf.generate_code({"version": 1})
    with pytest.raises(CleanFrameError):
        cf.write_frame([1, 2], "x.csv")


# ===========================================================================
# Errors reach the user as messages, not tracebacks
# ===========================================================================
def test_missing_and_directory_inputs_raise_clean_errors(tmp_path):
    with pytest.raises(CleanFrameError, match="not found"):
        cf.clean(tmp_path / "nope.csv")
    with pytest.raises(CleanFrameError, match="not a file"):
        cf.clean(tmp_path)
    with pytest.raises(CleanFrameError, match="not found"):
        cf.report(tmp_path / "nope.csv")


def test_a_byte_cp1252_cannot_decode_does_not_raise(tmp_path):
    p = tmp_path / "sjis.csv"
    p.write_bytes(b"name,city\nA,\x8f\x81\nB,x\n")
    assert cf.clean(p, mode="auto").dataframe.shape == (2, 2)


def test_utf16_input_is_read_not_called_binary(tmp_path):
    p = tmp_path / "u16.csv"
    p.write_bytes("name,city\nA,x\n".encode("utf-16"))
    assert cf.clean(p, mode="auto").dataframe.shape == (1, 2)


def test_malformed_recipe_and_schema_yaml_raise_named_errors(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("version: 1\ncolumns:\n  a: [unclosed\n", encoding="utf-8")
    with pytest.raises(RecipeError, match="Invalid recipe YAML"):
        cf.load_recipe(bad)
    with pytest.raises(SchemaError, match="Invalid schema YAML"):
        cf.Schema.load(bad)


def test_non_utf8_recipe_raises_a_clean_error(tmp_path):
    p = tmp_path / "latin.recipe.yaml"
    p.write_bytes(b"version: 1\nmeta: {note: caf\xe9}\n")
    with pytest.raises(CleanFrameError, match="UTF-8"):
        Recipe.load(p)


def test_output_path_problems_raise_output_errors(tmp_path):
    frame = pd.DataFrame({"a": [1]})
    with pytest.raises(OutputError, match="directory"):
        cf.write_frame(frame, tmp_path)
    with pytest.raises(OutputError, match="\\.xls"):
        cf.write_frame(frame, tmp_path / "o.xls")


def test_writing_over_the_input_is_refused_unless_asked(tmp_path):
    p = _csv(tmp_path, "in.csv", "a\n1\n")
    frame = pd.DataFrame({"a": [2]})
    with pytest.raises(OutputError, match="Refusing to overwrite"):
        cf.write_frame(frame, p, source=p)
    assert cf.write_frame(frame, p, source=p, overwrite=True) == p


def test_a_failed_write_leaves_the_previous_file_intact(tmp_path):
    p = tmp_path / "keep.xlsx"
    cf.write_frame(pd.DataFrame({"a": ["first"]}), p)
    before = p.read_bytes()
    with pytest.raises(OutputError):
        cf.write_frame(pd.DataFrame({"a": ["x" * 40_000]}), p)
    assert p.read_bytes() == before
    assert not (tmp_path / "keep.xlsx.cf-tmp").exists()


def test_excel_control_characters_are_stripped_not_fatal(tmp_path):
    p = tmp_path / "ctrl.xlsx"
    cf.write_frame(pd.DataFrame({"n": ["hello\x01world"]}), p)
    assert pd.read_excel(p)["n"].tolist() == ["helloworld"]


def test_diff_show_survives_a_cp1252_stream_with_currency_data():
    result = cf.clean(pd.DataFrame({"Amount": ["₹1,200", "₹50,000"]}), mode="auto")
    buffer = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", newline="")
    result.diff.show(stream=buffer)


def test_every_advisory_uses_the_cleanframe_warning_category(messy_df):
    recipe = cf.clean(messy_df, mode="auto").recipe
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        cf.apply_recipe(messy_df.rename(columns={"Email": "E-mail"}), recipe, on_drift="warn")
    assert caught and all(issubclass(w.category, cf.CleanFrameWarning) for w in caught)


# ===========================================================================
# Read-time fidelity
# ===========================================================================
def test_text_mode_keeps_the_file_exactly(tmp_path):
    p = _csv(tmp_path, "zip.csv", "Zip,Allergy,Code,Flag\n00123,None,1e5,TRUE\n02134,Peanuts,7,FALSE\n")
    verbatim = cf.read_frame(p, text=True)
    assert verbatim["Zip"].tolist() == ["00123", "02134"]
    assert verbatim["Allergy"].tolist() == ["None", "Peanuts"]
    assert verbatim["Code"].tolist() == ["1e5", "7"]


def test_type_inference_losses_are_reported(tmp_path):
    p = _csv(tmp_path, "zip.csv", "Zip,Allergy\n00123,None\n02134,Peanuts\n")
    with pytest.warns(cf.CleanFrameWarning, match="type inference"):
        result = cf.clean(p, mode="auto")
    assert any("type inference" in note for note in result.log)


def test_text_mode_is_recorded_and_replayed(tmp_path):
    p = _csv(tmp_path, "zip.csv", "Zip\n00123\n02134\n")
    recipe = cf.clean(p, text=True, mode="auto").recipe
    assert recipe.read and recipe.read.get("text") is True
    assert cf.apply_recipe(p, recipe, check_drift=False).dataframe["zip"].tolist() == [
        "00123",
        "02134",
    ]


# ===========================================================================
# Artifacts are safe and faithful
# ===========================================================================
def test_generated_code_cannot_be_injected_through_a_column_name():
    import os

    hostile = "x\n    import os\n    os.environ['CF_PWNED'] = 'yes'\n    #"
    code = cf.clean(pd.DataFrame({hostile: [" a"], "y": [1]})).code.to_string()
    namespace: dict = {}
    exec(compile(code, "generated", "exec"), namespace)  # noqa: S102 - the point of the test
    os.environ.pop("CF_PWNED", None)
    namespace["clean"](pd.DataFrame({hostile: ["a"], "y": [1]}))
    assert "CF_PWNED" not in os.environ


def test_generated_code_quotes_validation_labels():
    rule = cf.ValidationRule("y' + str(1) + '", "not_null", "error")
    code = cf.generate_code(Recipe(validations=[rule]))
    compile(code, "generated", "exec")


def test_codegen_refuses_a_recipe_it_cannot_reproduce():
    recipe = Recipe.from_dict({"version": 1, "columns": {"p": {"ops": ["normalize_phone"]}}})
    cf.generate_code(recipe)  # reproducible ops are fine

    @cf.register_op("only_in_python_" + "x")
    def _custom(series):
        """A custom op codegen knows nothing about."""
        return series

    custom = Recipe.from_dict({"version": 1, "columns": {"p": {"ops": ["only_in_python_x"]}}})
    with pytest.raises(CleanFrameError, match="allow_partial"):
        cf.generate_code(custom)
    assert cf.generate_code(custom, allow_partial=True)


def test_exported_code_matches_the_executor_on_messy_phones():
    """The phone fixes must exist in both the executor and the exported module."""
    frame = pd.DataFrame(
        {
            "numeric": [9876543210.0, None, 8005550100.0],
            "messy": ["080-1234-5678 ext 12", "+91 98765 43210", "N/A"],
        }
    )
    recipe = Recipe.from_dict(
        {
            "version": 1,
            "columns": {
                "numeric": {"ops": ["normalize_phone"]},
                "messy": {"ops": [{"normalize_phone": {"default_country_code": "+91"}}]},
            },
        }
    )
    engine = cf.apply_recipe(frame, recipe, check_drift=False).dataframe.reset_index(drop=True)
    namespace: dict = {}
    exec(compile(cf.generate_code(recipe), "generated", "exec"), namespace)
    exported = namespace["clean"](frame.copy()).reset_index(drop=True)
    pd.testing.assert_frame_equal(exported, engine, check_dtype=False)
    assert engine["numeric"].dropna().tolist() == ["9876543210", "8005550100"]
    assert engine["messy"].tolist()[0] == "+918012345678"


def test_export_keeps_signed_numbers_and_escapes_formulas(tmp_path):
    p = tmp_path / "san.csv"
    cf.write_frame(pd.DataFrame({"phone": ["+919876543210"], "d": ["-1.5"], "f": ["=CMD()"]}), p)
    text = p.read_text(encoding="utf-8")
    assert "+919876543210" in text and ",-1.5," in text
    assert "'=CMD()" in text


def test_export_escapes_a_formula_in_the_header(tmp_path):
    p = tmp_path / "hdr.csv"
    cf.write_frame(pd.DataFrame({"=CMD()": ["a"]}), p)
    assert p.read_text(encoding="utf-8").splitlines()[0] == "'=CMD()"


# ===========================================================================
# Streaming parity and guards
# ===========================================================================
def test_streamed_output_matches_a_whole_frame_replay(tmp_path):
    p = _csv(tmp_path, "floats.csv", "a,b\n1.5, x \n2, y\n3,z\n")
    recipe = cf.clean(p, mode="auto").recipe
    whole = cf.apply_recipe(p, recipe, check_drift=False)
    cf.write_frame(whole.dataframe, tmp_path / "whole.csv")
    cf.stream_apply(recipe, p, tmp_path / "streamed.csv", chunksize=2, check_drift=False)
    assert (tmp_path / "streamed.csv").read_text(encoding="utf-8") == (
        tmp_path / "whole.csv"
    ).read_text(encoding="utf-8")


@pytest.mark.parametrize("chunksize", [0, -1, None, "10"])
def test_streaming_refuses_a_bad_chunksize(tmp_path, chunksize):
    p = _csv(tmp_path, "s.csv", "a\n1\n2\n")
    recipe = Recipe.from_dict({"version": 1, "columns": {"a": {"ops": ["strip_whitespace"]}}})
    with pytest.raises(CleanFrameError, match="chunksize"):
        cf.stream_apply(recipe, p, tmp_path / "o.csv", chunksize=chunksize, check_drift=False)


def test_streaming_refuses_to_overwrite_its_input(tmp_path):
    p = _csv(tmp_path, "s.csv", "a\n1\n2\n")
    recipe = Recipe.from_dict({"version": 1, "columns": {"a": {"ops": ["strip_whitespace"]}}})
    with pytest.raises(OutputError, match="Refusing to overwrite"):
        cf.stream_apply(recipe, p, p, check_drift=False)


# ===========================================================================
# Drift, LLM policy, and the CLI contract
# ===========================================================================
def test_a_fingerprint_without_column_names_does_not_invent_drift():
    recipe = Recipe.from_dict(
        {
            "version": 1,
            "source_fingerprint": {"columns": 2, "hash_sample": "a41f"},
            "columns": {"Customer Name": {"rename_to": "customer_name"}},
        }
    )
    report = cf.detect_drift(pd.DataFrame({"Customer Name": ["a"], "Email": ["b"]}), recipe)
    assert [f.kind for f in report.findings if f.kind in ("new_column", "missing_column")] == []


def test_malformed_fingerprint_raises_a_clean_error():
    recipe = Recipe.from_dict({"version": 1, "source_fingerprint": "abc"})
    with pytest.raises(CleanFrameError, match="source_fingerprint"):
        cf.detect_drift(pd.DataFrame({"a": [1]}), recipe)


def test_exposure_none_makes_no_request():
    class Spy:
        model = "spy"

        def __init__(self):
            self.calls = 0

        def complete(self, system, user, *, max_tokens=2048):
            self.calls += 1
            raise AssertionError("exposure='none' must not reach a provider")

    spy = Spy()
    with pytest.warns(cf.CleanFrameWarning, match="none"):
        cf.clean(pd.DataFrame({"a": [" x"]}), llm=spy, llm_exposure="none")
    assert spy.calls == 0


def test_llm_failure_can_be_made_fatal():
    class Broken:
        model = "broken"

        def complete(self, system, user, *, max_tokens=2048):
            raise RuntimeError("network down")

    with pytest.raises(cf.LLMError):
        cf.clean(pd.DataFrame({"a": [" x"]}), llm=Broken(), llm_fallback=False)


def test_suggest_reads_the_file_the_way_the_recipe_was_planned(tmp_path):
    p = _csv(tmp_path, "semi.csv", "Name ;email\n a;A@X.com\n b ;B@Y.com\n")
    recipe = cf.clean(p, mode="auto").recipe
    _patched, drift = cf.suggest_update(p, recipe)
    assert not drift.has_drift


def test_cli_exit_codes_are_distinct(messy_df, drifted_df, tmp_path):
    src = tmp_path / "m.csv"
    messy_df.to_csv(src, index=False, encoding="utf-8")
    recipe = tmp_path / "r.recipe.yaml"
    assert main(["clean", str(src), "--recipe", str(recipe), "--mode", "auto"]) == EXIT_OK

    drifted = tmp_path / "m2.csv"
    drifted_df.to_csv(drifted, index=False, encoding="utf-8")
    assert (
        main(["apply", str(drifted), "--recipe", str(recipe), "--out", str(tmp_path / "o.csv")])
        == EXIT_DRIFT
    )
    assert main(["clean", str(tmp_path / "missing.csv")]) == EXIT_ERROR
    with pytest.raises(SystemExit) as exc:
        main(["clean", str(src), "--mode", "nonsense"])
    assert exc.value.code == 2


def test_cli_refuses_flags_it_would_otherwise_ignore(tmp_path):
    src = tmp_path / "s.csv"
    pd.DataFrame({"a": [" x", "y"]}).to_csv(src, index=False)
    recipe = tmp_path / "r.recipe.yaml"
    main(["clean", str(src), "--recipe", str(recipe), "--mode", "auto"])
    assert (
        main(
            [
                "apply", str(src), "--recipe", str(recipe), "--chunksize", "2",
                "--report", str(tmp_path / "r.html"),
            ]
        )
        == EXIT_ERROR
    )


def _workbook(tmp_path: Path) -> Path:
    path = tmp_path / "book.xlsx"
    with pd.ExcelWriter(path) as writer:
        pd.DataFrame({"Name ": [" a"]}).to_excel(writer, sheet_name="Customers", index=False)
        pd.DataFrame({"q": [1]}).to_excel(writer, sheet_name="Sample", index=False)
    return path


def test_cli_workbook_out_dir_keeps_every_sheet(tmp_path):
    book = _workbook(tmp_path)
    assert main(["clean", str(book), "--out-dir", str(tmp_path / "art"), "--mode", "auto"]) == EXIT_OK
    written = sorted(p.name for p in (tmp_path / "art").iterdir())
    assert written == ["book.clean.xlsx", "book.recipe.yaml"]
    assert pd.ExcelFile(tmp_path / "art" / "book.clean.xlsx").sheet_names == [
        "Customers",
        "Sample",
    ]


def test_cli_workbook_apply_refuses_selection_flags(tmp_path):
    book = _workbook(tmp_path)
    recipe = tmp_path / "wb.recipe.yaml"
    main(["clean", str(book), "--recipe", str(recipe), "--mode", "auto"])
    for flag in (["--sheet", "Customers"], ["--text"], ["--sep", ","]):
        assert (
            main(["apply", str(book), "--recipe", str(recipe), "--out", str(tmp_path / "o.xlsx"), *flag])
            == EXIT_ERROR
        ), flag
    assert (
        main(["apply", str(book), "--recipe", str(recipe), "--out", str(tmp_path / "ok.xlsx")])
        == EXIT_OK
    )


def test_cli_out_dir_writes_every_artifact(tmp_path):
    src = tmp_path / "s.csv"
    pd.DataFrame({"Name": [" a", "b"]}).to_csv(src, index=False)
    assert main(["clean", str(src), "--out-dir", str(tmp_path / "art"), "--mode", "auto"]) == EXIT_OK
    written = sorted(p.name for p in (tmp_path / "art").iterdir())
    assert written == ["s.clean.csv", "s.py", "s.recipe.yaml", "s.report.html"]


def test_module_entry_point_runs():
    import subprocess
    import sys

    done = subprocess.run(
        [sys.executable, "-m", "cleanframe", "--version"], capture_output=True, text=True
    )
    assert done.returncode == 0 and "cleanframe" in done.stdout
