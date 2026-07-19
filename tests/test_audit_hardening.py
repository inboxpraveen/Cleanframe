"""Regression tests for audit fixes: drift content, streaming read binding, Excel sanitize, etc."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

import cleanframe as cf
from cleanframe.drift import detect_drift
from cleanframe.errors import DriftError, ValidationFailure
from cleanframe.fingerprint import fingerprint_dataframe
from cleanframe.llm import _finalize_llm_recipe, get_client
from cleanframe.recipe import ColumnRecipe, Recipe, ValidationRule
from cleanframe.types import Mode, Op
from cleanframe.validate import apply_validations


def test_content_hash_and_row_count_are_reported():
    df1 = pd.DataFrame({"a": ["x", "y"], "b": [1, 2]})
    df2 = pd.DataFrame({"a": ["TOTALLY", "DIFFERENT"], "b": [99, 100]})
    recipe = Recipe(
        columns=[ColumnRecipe(source="a", ops=[Op("strip_whitespace")])],
        source_fingerprint=fingerprint_dataframe(df1),
    )
    report = detect_drift(df2, recipe)
    kinds = {f.kind for f in report.findings}
    assert "content_hash_change" in kinds
    assert "row_count_change" not in kinds  # same length
    # Content findings are informational — monthly value changes must not stop apply.
    assert all(f.severity.name == "INFO" for f in report.by_kind("content_hash_change"))
    assert not report.has_drift


def test_dtype_change_stops_apply_when_families_differ():
    df = pd.DataFrame({"a": ["x", "y"], "b": [1, 2]})
    recipe = Recipe(
        columns=[ColumnRecipe(source="a", ops=[Op("strip_whitespace")])],
        source_fingerprint=fingerprint_dataframe(df),
    )
    drifted = df.copy()
    drifted["b"] = drifted["b"].astype("float64")
    report = detect_drift(drifted, recipe)
    assert any(f.kind == "dtype_change" for f in report.findings)
    assert report.has_drift
    with pytest.raises(DriftError):
        cf.apply_recipe(drifted, recipe, check_drift=True, on_drift="error")


def test_object_vs_str_dtype_is_not_drift():
    df = pd.DataFrame({"a": ["x", "y"]})
    fp = fingerprint_dataframe(df)
    fp["dtypes"] = {"a": "object"}
    recipe = Recipe(
        columns=[ColumnRecipe(source="a", ops=[Op("strip_whitespace")])],
        source_fingerprint=fp,
    )
    as_string = pd.DataFrame({"a": pd.Series(["x", "y"], dtype="string")})
    report = detect_drift(as_string, recipe)
    assert not any(f.kind == "dtype_change" for f in report.findings)


def test_new_column_is_warning_drift():
    df = pd.DataFrame({"a": ["x"]})
    recipe = Recipe(
        columns=[ColumnRecipe(source="a", ops=[Op("strip_whitespace")])],
        source_fingerprint=fingerprint_dataframe(df),
    )
    wider = pd.DataFrame({"a": ["x"], "extra": [1]})
    report = detect_drift(wider, recipe)
    finding = next(f for f in report.findings if f.kind == "new_column")
    assert finding.severity.name == "WARNING"
    assert report.has_drift


def test_stream_respects_recipe_read_columns(tmp_path: Path):
    src = tmp_path / "in.csv"
    out = tmp_path / "out.csv"
    pd.DataFrame({"keep": [" a ", " b "], "dropme": ["x", "y"]}).to_csv(src, index=False)
    recipe = Recipe.from_dict(
        {
            "version": 1,
            "read": {"columns": ["keep"]},
            "columns": {"keep": {"ops": ["strip_whitespace"]}},
        }
    )
    cf.stream_apply(recipe, src, out, check_drift=False)
    assert list(pd.read_csv(out).columns) == ["keep"]
    assert cf.apply_recipe(src, recipe, check_drift=False).dataframe.columns.tolist() == ["keep"]


def test_stream_refuses_non_csv(tmp_path: Path):
    xlsx = tmp_path / "w.xlsx"
    pd.DataFrame({"a": [1]}).to_excel(xlsx, index=False)
    recipe = Recipe.from_dict({"version": 1, "columns": {"a": {"ops": ["strip_whitespace"]}}})
    with pytest.raises(cf.CleanFrameError, match="CSV-family"):
        cf.stream_apply(recipe, xlsx, tmp_path / "o.csv", check_drift=False)


def test_excel_formula_cells_are_sanitized(tmp_path: Path):
    df = pd.DataFrame({"a": ["=1+1", "+cmd", "safe"], "b": [1, 2, 3]})
    out = tmp_path / "out.xlsx"
    cf.write_frame(df, out)
    got = pd.read_excel(out)
    assert got["a"].tolist()[0] == "'=1+1"
    assert got["a"].tolist()[1] == "'+cmd"
    assert got["a"].tolist()[2] == "safe"


def test_on_fail_typo_rejected_at_load():
    with pytest.raises(cf.RecipeError, match="on_fail"):
        ValidationRule.from_dict({"column": "a", "check": "not_null", "on_fail": "erro"})


def test_strict_missing_validation_column_raises():
    df = pd.DataFrame({"a": [1, 2]})
    rules = [ValidationRule(column="missing", check="not_null", on_fail="quarantine")]
    with pytest.raises(ValidationFailure, match="missing"):
        apply_validations(df, rules, mode=Mode.STRICT)


def test_openai_base_url_does_not_hijack_openrouter(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://evil.example/v1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    client = get_client("openrouter/google/gemma-4-26b-a4b-it")
    assert "openrouter.ai" in (client._base_url or "")
    assert "evil.example" not in (client._base_url or "")


def test_openai_base_url_still_applies_to_openai(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://proxy.example/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    client = get_client("openai/gpt-4o-mini")
    assert client._base_url == "https://proxy.example/v1"


def test_finalize_llm_recipe_orders_and_strips_fill_na():
    recipe = Recipe(
        columns=[
            ColumnRecipe(
                source="name",
                ops=[
                    Op("title_case"),
                    Op("strip_whitespace"),
                    Op("fill_na", {"strategy": "empty"}),
                ],
            )
        ]
    )
    out = _finalize_llm_recipe(recipe, mode=Mode.AUTO)
    names = [op.name for op in out.columns[0].ops]
    assert "fill_na" not in names
    assert names.index("strip_whitespace") < names.index("title_case")


def test_stream_validation_interest_not_mistaken_for_in():
    from cleanframe.streaming import is_validation_streamable

    rule = ValidationRule(column="x", check="interest", on_fail="quarantine")
    assert is_validation_streamable(rule) is False


def test_codegen_fill_na_strategies_match_executor():
    from cleanframe.codegen import generate_code
    from cleanframe.executor import execute

    df = pd.DataFrame({"n": [1.0, None, 3.0, None, 5.0]})
    for strategy in ("mean", "median", "zero", "empty", "ffill", "bfill"):
        recipe = Recipe.from_dict(
            {"version": 1, "columns": {"n": {"ops": [{"fill_na": {"strategy": strategy}}]}}}
        )
        ref = execute(recipe, df, mode="auto").dataframe.reset_index(drop=True)
        ns: dict = {}
        exec(compile(generate_code(recipe), "g.py", "exec"), ns)
        gen = ns["clean"](df.copy()).reset_index(drop=True)
        pd.testing.assert_frame_equal(gen, ref)


def test_codegen_normalize_unit_emit_column():
    from cleanframe.codegen import generate_code
    from cleanframe.executor import execute

    df = pd.DataFrame({"w": ["5kg", "500 g", "2"]})
    recipe = Recipe.from_dict(
        {
            "version": 1,
            "columns": {
                "w": {"ops": [{"normalize_unit": {"to": "g", "emit_unit_column": "unit"}}]}
            },
        }
    )
    ref = execute(recipe, df, mode="auto").dataframe.reset_index(drop=True)
    ns: dict = {}
    exec(compile(generate_code(recipe), "g.py", "exec"), ns)
    gen = ns["clean"](df.copy()).reset_index(drop=True)
    pd.testing.assert_frame_equal(gen, ref)
    assert "unit" in gen.columns


def test_codegen_rejects_redos_replace():
    from cleanframe.codegen import generate_code

    recipe = Recipe.from_dict(
        {"version": 1, "columns": {"a": {"ops": [{"replace": {"pattern": "(a+)+", "repl": "x"}}]}}}
    )
    with pytest.raises(ValueError, match="ReDoS|exceeds|Invalid|quantifier"):
        generate_code(recipe)


def test_ambiguous_slash_formats_not_both_kept():
    from cleanframe.detectors.dates import _reconcile_slash_formats

    both = ["%d/%m/%Y", "%m/%d/%Y", "%Y-%m-%d"]
    assert _reconcile_slash_formats(both, True) == ["%d/%m/%Y", "%Y-%m-%d"]
    assert _reconcile_slash_formats(both, False) == ["%m/%d/%Y", "%Y-%m-%d"]
    assert _reconcile_slash_formats(both, None) == ["%d/%m/%Y", "%Y-%m-%d"]


def test_parse_dates_reconciles_conflicting_slash_formats():
    from cleanframe.ops import parse_dates_to_datetime

    s = pd.Series(["05/06/2024", "07/08/2024"])
    day = parse_dates_to_datetime(s, ["%d/%m/%Y", "%m/%d/%Y"], dayfirst=True)
    month = parse_dates_to_datetime(s, ["%d/%m/%Y", "%m/%d/%Y"], dayfirst=False)
    assert day.iloc[0].day == 5 and day.iloc[0].month == 6
    assert month.iloc[0].day == 6 and month.iloc[0].month == 5


def test_missing_columns_projection_raises():
    df = pd.DataFrame({"a": [1]})
    with pytest.raises(cf.CleanFrameError, match="not found"):
        cf.clean(df, columns=["a", "nope"], mode="auto")


def test_fill_na_unknown_strategy_rejected_at_load():
    with pytest.raises(cf.RecipeError, match="strategy"):
        Recipe.from_dict(
            {"version": 1, "columns": {"a": {"ops": [{"fill_na": {"strategy": "magic"}}]}}}
        )


def test_llm_json_extract_ignores_trailing_braces():
    from cleanframe.llm import parse_recipe_json

    text = (
        'Here is the plan: {"version": 1, "columns": {"a": {"ops": ["strip_whitespace"]}}} '
        'and also {"note": "ignore"}'
    )
    recipe = parse_recipe_json(text)
    assert recipe.columns[0].source == "a"


def test_apply_warns_without_fingerprint(recwarn):
    df = pd.DataFrame({"a": [" x "]})
    recipe = Recipe.from_dict({"version": 1, "columns": {"a": {"ops": ["strip_whitespace"]}}})
    cf.apply_recipe(df, recipe, check_drift=True)
    assert any("source_fingerprint" in str(w.message) for w in recwarn)
