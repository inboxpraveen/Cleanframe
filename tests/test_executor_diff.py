"""Executor + cell-level diff: correctness, lineage, quarantine, determinism."""

from __future__ import annotations

import pandas as pd

from cleanframe.executor import execute
from cleanframe.planner import plan_recipe
from cleanframe.recipe import Recipe


def test_end_to_end_clean(messy_df):
    recipe = plan_recipe(messy_df, mode="review", options={"phone_country_code": "+91"})
    result = execute(recipe, messy_df, mode="review")
    df = result.dataframe
    assert "amount_inr" in df.columns
    assert df["amount_inr"].tolist()[0] == 120000.0
    # the invalid-email row is quarantined, not in the clean output
    assert not result.quarantine.empty
    assert "not-an-email" in result.quarantine["email"].tolist()
    assert "not-an-email" not in df["email"].tolist()


def test_diff_tracks_every_change(messy_df):
    recipe = plan_recipe(messy_df, mode="review")
    result = execute(recipe, messy_df, mode="review")
    diff = result.diff
    assert diff.changed_cells > 0
    assert diff.renamed_columns["Amount"] == "amount_inr"
    # dropped row recorded with a reason
    assert any("valid_email" in reason for _, reason in diff.dropped_rows)


def test_diff_row_ids_survive_dedup():
    df = pd.DataFrame({"x": ["a", "a", "b"], "y": [1, 1, 2]})
    recipe = Recipe.from_yaml("version: 1\ndedup: {}\n")
    result = execute(recipe, df, mode="auto")
    assert len(result.dataframe) == 2
    assert result.diff.dropped_rows == [(1, "dedup")]


def test_execution_is_deterministic(messy_df):
    recipe = plan_recipe(messy_df, mode="auto")
    a = execute(recipe, messy_df, mode="auto").dataframe
    b = execute(recipe, messy_df, mode="auto").dataframe
    pd.testing.assert_frame_equal(a, b)


def test_recipe_generation_is_deterministic(messy_df):
    assert plan_recipe(messy_df, mode="auto").to_yaml() == plan_recipe(messy_df, mode="auto").to_yaml()


def test_save_reload_replay_identical(messy_df, tmp_path):
    recipe = plan_recipe(messy_df, mode="auto")
    path = tmp_path / "r.yaml"
    recipe.save(path)
    reloaded = Recipe.load(path)
    a = execute(recipe, messy_df, mode="auto").dataframe
    b = execute(reloaded, messy_df, mode="auto").dataframe
    pd.testing.assert_frame_equal(a, b)


def test_missing_source_column_skipped_in_non_strict():
    df = pd.DataFrame({"present": [1, 2]})
    recipe = Recipe.from_yaml("version: 1\ncolumns:\n  absent: {ops: [strip_whitespace]}\n")
    result = execute(recipe, df, mode="review")  # should not raise
    assert "present" in result.dataframe.columns


def test_added_column_from_currency_split():
    df = pd.DataFrame({"amt": ["₹5", "$3", "€2"]})  # mixed currencies -> split
    recipe = plan_recipe(df, mode="auto")
    result = execute(recipe, df, mode="auto")
    added = result.diff.added_columns
    assert any(c.endswith("_currency") for c in added)
