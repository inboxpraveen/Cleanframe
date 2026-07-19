"""Wave 3a — selective ingestion (sheet/columns/nrows/skiprows) + recipe read: section."""
from __future__ import annotations

import pandas as pd
import pytest

import cleanframe as cf
from cleanframe.errors import CleanFrameError, RecipeError
from cleanframe.recipe import Recipe


def _wide_csv(tmp_path):
    p = tmp_path / "wide.csv"
    pd.DataFrame(
        {"id": range(10), "name": [f"n{i}" for i in range(10)], "amt": [f"${i}" for i in range(10)]}
    ).to_csv(p, index=False)
    return p


def _workbook(tmp_path):
    p = tmp_path / "book.xlsx"
    with pd.ExcelWriter(p) as xl:
        pd.DataFrame({"a": [1, 2]}).to_excel(xl, sheet_name="Alpha", index=False)
        pd.DataFrame({"b": [3, 4, 5]}).to_excel(xl, sheet_name="Beta", index=False)
    return p


# --- read_frame selection -----------------------------------------------------
def test_read_frame_column_and_row_selection(tmp_path):
    p = _wide_csv(tmp_path)
    assert list(cf.read_frame(p, columns=["id", "amt"]).columns) == ["id", "amt"]
    assert len(cf.read_frame(p, nrows=3)) == 3
    assert len(cf.read_frame(p, skiprows=[1, 2])) == 8  # lines 1-2 (first 2 data rows) skipped


def test_read_frame_multisheet_without_sheet_raises(tmp_path):
    p = _workbook(tmp_path)
    with pytest.raises(CleanFrameError) as exc:
        cf.read_frame(p)
    assert "Alpha" in str(exc.value) and "Beta" in str(exc.value)


def test_read_frame_sheet_selection(tmp_path):
    p = _workbook(tmp_path)
    assert list(cf.read_frame(p, sheet="Beta").columns) == ["b"]
    assert len(cf.read_frame(p, sheet="Beta")) == 3


def test_read_frame_sheet_on_csv_raises(tmp_path):
    p = _wide_csv(tmp_path)
    with pytest.raises(CleanFrameError):
        cf.read_frame(p, sheet="X")


# --- selection is recorded in the recipe and replays --------------------------
def test_clean_records_read_binding_and_apply_replays(tmp_path):
    p = _wide_csv(tmp_path)
    result = cf.clean(p, columns=["id", "amt"], nrows=5, mode="auto")
    assert result.recipe.read == {"columns": ["id", "amt"], "nrows": 5}
    # v2 on save, and re-loadable
    rp = tmp_path / "r.recipe.yaml"
    result.recipe.save(rp)
    loaded = Recipe.load(rp)
    assert loaded.read == {"columns": ["id", "amt"], "nrows": 5}
    # apply re-reads the same slice (5 rows, 2 columns) without passing selection again
    applied = cf.apply_recipe(p, loaded, check_drift=False)
    assert applied.dataframe.shape[0] == 5
    assert set(applied.dataframe.columns) >= {"id"}


def test_clean_dataframe_with_sheet_raises():
    with pytest.raises(CleanFrameError):
        cf.clean(pd.DataFrame({"a": [1]}), sheet="X")


def test_apply_recorded_binding_on_dataframe_warns():
    recipe = Recipe.from_dict({"version": 2, "read": {"sheet": "Beta"}, "columns": {}})
    with pytest.warns(UserWarning, match="cannot"):
        cf.apply_recipe(pd.DataFrame({"a": [1, 2]}), recipe, check_drift=False)


# --- recipe v1/v2 handling ----------------------------------------------------
def test_plain_recipe_stays_v1_and_read_recipe_is_v2():
    plain = Recipe.from_dict({"version": 1, "columns": {"a": {"ops": ["strip_whitespace"]}}})
    assert plain.to_dict()["version"] == 1
    with_read = Recipe.from_dict({"version": 2, "read": {"nrows": 3}, "columns": {}})
    assert with_read.to_dict()["version"] == 2
    assert with_read.to_dict()["read"] == {"nrows": 3}


def test_workbook_recipe_rejected_by_recipe_from_dict():
    with pytest.raises(RecipeError, match="workbook"):
        Recipe.from_dict({"version": 2, "sheets": {"Alpha": {"columns": {}}}})
