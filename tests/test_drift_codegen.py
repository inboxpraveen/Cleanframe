"""Drift detection scenarios and codegen faithfulness."""

from __future__ import annotations

import pandas as pd

from cleanframe.codegen import generate_code
from cleanframe.drift import detect_drift
from cleanframe.executor import execute
from cleanframe.planner import plan_recipe


def test_drift_detects_rename_missing_and_format(messy_df, drifted_df):
    recipe = plan_recipe(messy_df, mode="auto")
    report = detect_drift(drifted_df, recipe, source="month2.csv")
    kinds = {f.kind for f in report.findings}
    assert "renamed_column" in kinds  # Amt (INR) ~ amount_inr
    assert "missing_column" in kinds  # Amount gone
    assert "format_drift" in kinds  # "Jan 5, 2026" unmatched
    assert report.has_drift


def test_no_drift_on_same_schema(messy_df):
    recipe = plan_recipe(messy_df, mode="auto")
    report = detect_drift(messy_df, recipe)
    assert not report.has_drift


def test_rename_finding_has_confident_match(messy_df, drifted_df):
    recipe = plan_recipe(messy_df, mode="auto")
    report = detect_drift(drifted_df, recipe)
    rename = next(f for f in report.by_kind("renamed_column") if f.column == "Amt (INR)")
    assert rename.evidence["match"] == "amount_inr"
    assert rename.evidence["score"] >= 0.6


def test_codegen_matches_executor(messy_df):
    # use auto mode (no email quarantine drops) so outputs align 1:1
    df = messy_df.drop(columns=["Email", "Phone"])
    recipe = plan_recipe(df, mode="auto")
    code = generate_code(recipe)
    ns: dict = {}
    exec(compile(code, "generated.py", "exec"), ns)
    gen = ns["clean"](df).reset_index(drop=True)
    ref = execute(recipe, df, mode="auto").dataframe.reset_index(drop=True)
    pd.testing.assert_frame_equal(gen, ref)


def test_generated_code_is_importable_and_pure(messy_df):
    recipe = plan_recipe(messy_df, mode="auto")
    code = generate_code(recipe)
    # no runtime dependency on cleanframe
    assert not any(
        line.startswith(("import cleanframe", "from cleanframe")) for line in code.splitlines()
    )
    compile(code, "g.py", "exec")  # syntactically valid
