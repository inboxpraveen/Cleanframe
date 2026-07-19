"""Wave 3b — multi-sheet workbook: clean all tabs, keep the rest, write back safely."""
from __future__ import annotations

import pandas as pd
import pytest

import cleanframe as cf
from cleanframe.errors import CleanFrameError


def _book(tmp_path, name="book.xlsx"):
    p = tmp_path / name
    with pd.ExcelWriter(p) as xl:
        pd.DataFrame({"Name": ["  alice ", "BOB"], "Amount": ["₹1,200", "₹2,400"]}).to_excel(
            xl, sheet_name="Customers", index=False
        )
        pd.DataFrame({"sku": ["a", "a", "b"], "qty": [1, 1, 2]}).to_excel(
            xl, sheet_name="Orders", index=False
        )
        pd.DataFrame({"note": ["keep me"]}).to_excel(xl, sheet_name="Meta", index=False)
    return p


def test_clean_workbook_cleans_every_sheet(tmp_path):
    p = _book(tmp_path)
    result = cf.clean_workbook(p, mode="auto")
    assert set(result.sheets) == {"Customers", "Orders", "Meta"}
    assert result.sheet_order == ["Customers", "Orders", "Meta"]
    # Customers got cleaned (name renamed + trimmed, amount parsed to a number).
    cust = result.sheets["Customers"].dataframe
    assert "name" in cust.columns and cust["name"].iloc[0] == "alice"


def test_workbook_write_back_roundtrips_all_sheets(tmp_path):
    p = _book(tmp_path)
    result = cf.clean_workbook(p, mode="auto")
    out = tmp_path / "cleaned.xlsx"
    result.save_data(out)
    from cleanframe.dataio import excel_sheet_names

    assert excel_sheet_names(out) == ["Customers", "Orders", "Meta"]
    assert cf.read_frame(out, sheet="Meta")["note"].tolist() == ["keep me"]


def test_workbook_save_refuses_inplace_overwrite(tmp_path):
    p = _book(tmp_path)
    result = cf.clean_workbook(p, mode="auto")
    with pytest.raises(CleanFrameError, match="in place"):
        result.save_data(p)  # same as source
    result.save_data(p, overwrite=True)  # explicit opt-in is allowed


def test_clean_workbook_subset_keeps_others_untouched(tmp_path):
    p = _book(tmp_path)
    result = cf.clean_workbook(p, sheets=["Customers"], mode="auto")
    assert set(result.sheets) == {"Customers"}
    assert set(result.untouched) == {"Orders", "Meta"}
    # write-back still includes all three sheets in order
    out = tmp_path / "partial.xlsx"
    result.save_data(out)
    from cleanframe.dataio import excel_sheet_names

    assert excel_sheet_names(out) == ["Customers", "Orders", "Meta"]


def test_workbook_recipe_roundtrip_and_apply(tmp_path):
    p = _book(tmp_path)
    result = cf.clean_workbook(p, mode="auto")
    rp = tmp_path / "wb.recipe.yaml"
    result.save_recipe(rp)
    # load_recipe detects a workbook recipe
    loaded = cf.load_recipe(rp)
    assert isinstance(loaded, cf.WorkbookRecipe)
    assert set(loaded.sheets) == {"Customers", "Orders", "Meta"}
    # apply_workbook replays deterministically
    applied = cf.apply_workbook(p, loaded, check_drift=False)
    assert applied.sheets["Customers"].dataframe["name"].iloc[0] == "alice"


def test_load_recipe_detects_single_vs_workbook(tmp_path):
    single = cf.clean(pd.DataFrame({"a": ["  x "]}), mode="auto")
    sp = tmp_path / "single.recipe.yaml"
    single.recipe.save(sp)
    assert isinstance(cf.load_recipe(sp), cf.Recipe)
