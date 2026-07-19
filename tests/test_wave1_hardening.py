"""Wave 1 — Batch A: crash-hardening regression tests.

Each test captures an audit repro that today leaks a raw (non-CleanFrame) traceback
or crashes the pipeline. They must fail before the Batch-A fixes and pass after.
"""
from __future__ import annotations

import io

import pandas as pd
import pytest

import cleanframe as cf
from cleanframe.errors import CleanFrameError, RecipeError


# --- IO error hygiene: raw pandas errors must become CleanFrameError ----------
def test_non_utf8_csv_raises_cleanframe_error(tmp_path):
    """M2: a cp1252/latin-1 CSV must not leak a raw UnicodeDecodeError."""
    p = tmp_path / "latin1.csv"
    p.write_bytes("name,city\nJos\xe9,M\xfcnchen\n".encode("cp1252"))
    with pytest.raises(CleanFrameError):
        cf.read_frame(p)


def test_ragged_csv_raises_cleanframe_error(tmp_path):
    """L3: a ragged CSV must not leak a raw pandas.errors.ParserError."""
    p = tmp_path / "ragged.csv"
    p.write_text("a,b,c\n1,2,3\n4,5,6,7,8\n", encoding="utf-8")
    with pytest.raises(CleanFrameError):
        cf.read_frame(p)


def test_empty_file_raises_cleanframe_error(tmp_path):
    """L4: a 0-byte file must not leak a raw EmptyDataError."""
    p = tmp_path / "empty.csv"
    p.write_bytes(b"")
    with pytest.raises(CleanFrameError):
        cf.read_frame(p)


def test_directory_path_raises_cleanframe_error(tmp_path):
    """L5: a directory path must not leak a raw PermissionError."""
    with pytest.raises(CleanFrameError):
        cf.read_frame(tmp_path)


def test_csv_bytes_as_parquet_raises_cleanframe_error(tmp_path):
    """L5: a mislabeled file (CSV bytes as .parquet) must not leak a raw ArrowInvalid."""
    p = tmp_path / "fake.parquet"
    p.write_text("a,b\n1,2\n", encoding="utf-8")
    with pytest.raises(CleanFrameError):
        cf.read_frame(p)


# --- Recipe / op parsing hygiene ---------------------------------------------
def test_malformed_recipe_yaml_raises_recipe_error(tmp_path):
    """L9: malformed YAML must raise RecipeError, not a raw yaml.YAMLError."""
    p = tmp_path / "bad.recipe.yaml"
    p.write_text("columns: [unclosed\n", encoding="utf-8")
    with pytest.raises(RecipeError):
        cf.Recipe.load(p)


def test_replace_op_missing_pattern_raises_recipe_error():
    """L10: `replace` without `pattern` must raise RecipeError, not a raw KeyError."""
    with pytest.raises(RecipeError):
        cf.Recipe.from_dict({"version": 1, "columns": {"a": {"ops": [{"replace": {"repl": "x"}}]}}})


def test_cast_op_missing_target_raises_recipe_error():
    """M12: `cast` with a dict lacking `to` must raise RecipeError, not a raw KeyError."""
    with pytest.raises(RecipeError):
        cf.Recipe.from_dict({"version": 1, "columns": {"a": {"ops": [{"cast": {"precision": 2}}]}}})


def test_round_op_bad_value_raises_recipe_error():
    """M12: `round: two` must raise RecipeError, not a raw ValueError."""
    with pytest.raises(RecipeError):
        cf.Recipe.from_dict({"version": 1, "columns": {"a": {"ops": [{"round": "two"}]}}})


# --- Structural / column-label crashes ---------------------------------------
def test_duplicate_column_names_raise_cleanframe_error():
    """M3/L11: duplicate column labels must raise a clean CleanFrameError, not a raw TypeError."""
    df = pd.DataFrame([[1, 2, 3], [4, 5, 6]], columns=["a", "a", "b"])
    with pytest.raises(CleanFrameError):
        cf.clean(df)


def test_integer_column_names_do_not_crash():
    """M4: integer column labels must not crash clean(); they normalize to strings."""
    df = pd.DataFrame({0: ["  x  ", "y"], 1: ["a", "b"]})
    result = cf.clean(df)
    assert all(isinstance(c, str) for c in result.dataframe.columns)


def test_multiindex_columns_do_not_raise_raw_error():
    """M4: MultiIndex columns must not leak a raw TypeError from .astype(str)."""
    df = pd.DataFrame([[1, 2], [3, 4]], columns=pd.MultiIndex.from_tuples([("a", "x"), ("a", "y")]))
    # Either succeeds (flattened labels) or raises a clean CleanFrameError — never a raw TypeError.
    try:
        cf.clean(df)
    except CleanFrameError:
        pass


def test_string_index_with_name_column_does_not_crash():
    """H6: a non-integer index plus a name-like column must not crash the dedup detector."""
    df = pd.DataFrame({"customer_name": ["Alice", "Bob", "Carol"]}, index=["r1", "r2", "r3"])
    # Should complete without a raw ValueError/TypeError from int(idx).
    cf.clean(df)


# --- Windows console rendering (M9) ------------------------------------------
def test_diff_render_is_cp1252_safe():
    """M9: diff.show() must not crash on a cp1252 stdout (the default Windows console)."""
    df = pd.DataFrame({"name": ["  Alice  ", "bob"]})
    result = cf.clean(df)
    # An ASCII-only render must be encodable to cp1252 without raising.
    ascii_text = result.diff.render(ascii=True)
    ascii_text.encode("cp1252")  # must not raise
    # show() to a genuine cp1252 stream must not raise UnicodeEncodeError.
    buf = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", newline="")
    result.diff.show(stream=buf)
