"""Streaming reliability: no raw pandas errors, whole-file drift, byte parity."""

from __future__ import annotations

import warnings

import pandas as pd
import pytest

import cleanframe as cf
from cleanframe.errors import CleanFrameError, DriftError
from cleanframe.recipe import Recipe


def _plan_recipe(tmp_path, n=300):
    plan = pd.DataFrame({"qty": list(range(n)), "name": [f"  n{i}  " for i in range(n)]})
    p = tmp_path / "plan.csv"
    plan.to_csv(p, index=False)
    recipe = cf.clean(p).recipe
    assert recipe.source_fingerprint["dtypes"]["qty"] == "int64"
    return recipe


def _write(tmp_path, name, qty, n=300):
    p = tmp_path / name
    pd.DataFrame({"qty": qty, "name": [f"  n{i}  " for i in range(n)]}).to_csv(p, index=False)
    return p


def _quiet(fn, *a, **k):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return fn(*a, **k)


@pytest.mark.parametrize("bad", ["abc", 1.5, 2**70], ids=["text", "float", "overflow"])
def test_late_chunk_mismatch_is_never_a_raw_pandas_error(tmp_path, bad):
    recipe = _plan_recipe(tmp_path)
    qty = list(range(300))
    qty[245] = bad
    p = _write(tmp_path, "late.csv", qty)
    out = tmp_path / "out.csv"
    for kwargs in ({}, {"on_drift": "warn"}, {"check_drift": False}):
        try:
            _quiet(cf.stream_apply, recipe, p, out, chunksize=100, **kwargs)
        except CleanFrameError:
            pass  # a named error is the contract; anything else fails the test
        assert not (tmp_path / "out.csv.cf-tmp").exists()


def test_late_text_matches_whole_frame_when_drift_is_off(tmp_path):
    recipe = _plan_recipe(tmp_path)
    qty = list(range(300))
    qty[245] = "abc"
    p = _write(tmp_path, "late.csv", qty)
    out = tmp_path / "out.csv"
    _quiet(cf.stream_apply, recipe, p, out, chunksize=100, check_drift=False)
    whole = _quiet(cf.apply_recipe, p, recipe, check_drift=False)
    assert out.read_text(encoding="utf-8") == whole.dataframe.to_csv(
        index=False, lineterminator="\n"
    )


def test_blank_beyond_drift_head_raises_like_apply_recipe(tmp_path):
    recipe = _plan_recipe(tmp_path)
    qty: list = list(range(300))
    qty[245] = None  # beyond the 200-row head; int64 -> float64
    p = _write(tmp_path, "blank.csv", qty)
    out = tmp_path / "out.csv"
    with pytest.raises(DriftError):
        cf.apply_recipe(p, recipe)
    with pytest.raises(DriftError):
        cf.stream_apply(recipe, p, out, chunksize=50)
    assert not out.exists()  # nothing partial is left behind


@pytest.mark.parametrize("kwargs", [{"check_drift": False}, {"on_drift": "warn"}])
def test_blank_renders_identically_to_whole_frame(tmp_path, kwargs):
    recipe = _plan_recipe(tmp_path)
    qty: list = list(range(300))
    qty[245] = None
    p = _write(tmp_path, "blank.csv", qty)
    out = tmp_path / "out.csv"
    _quiet(cf.stream_apply, recipe, p, out, chunksize=3, **kwargs)
    whole = _quiet(cf.apply_recipe, p, recipe, **kwargs)
    text = out.read_text(encoding="utf-8")
    assert text == whole.dataframe.to_csv(index=False, lineterminator="\n")
    assert "\n2.0," in text  # whole-frame renders floats; so must the stream


def test_negative_int_with_late_blank_renders_as_float(tmp_path):
    p = tmp_path / "n.csv"
    pd.DataFrame({"v": ["-1", "2", "3", "4", "5", "6", "", "8"]}).to_csv(p, index=False)
    recipe = Recipe.from_dict({"version": 1, "columns": {"v": {"ops": ["strip_whitespace"]}}})
    out = tmp_path / "o.csv"
    cf.stream_apply(recipe, p, out, chunksize=3, check_drift=False)
    whole = cf.apply_recipe(p, recipe, check_drift=False)
    assert out.read_text(encoding="utf-8") == whole.dataframe.to_csv(
        index=False, lineterminator="\n"
    )


def test_late_format_drift_is_caught_beyond_head(tmp_path):
    p = tmp_path / "d.csv"
    dates = ["2024-01-01"] * 250 + ["Jan 5, 26"]
    pd.DataFrame({"d": dates}).to_csv(p, index=False)
    recipe = Recipe.from_dict(
        {
            "version": 1,
            "source_fingerprint": {"column_names": ["d"], "dtypes": {"d": "object"}},
            "columns": {"d": {"ops": [{"parse_date": {"formats": ["%Y-%m-%d"]}}]}},
        }
    )
    with pytest.raises(DriftError):
        cf.apply_recipe(p, recipe)
    with pytest.raises(DriftError):
        cf.stream_apply(recipe, p, tmp_path / "o.csv", chunksize=50)


def test_bool_with_blank_across_chunks_is_refused_not_guessed(tmp_path):
    p = tmp_path / "b.csv"
    pd.DataFrame({"f": ["True", "False", "True", "", "False", "True"]}).to_csv(p, index=False)
    recipe = Recipe.from_dict({"version": 1, "columns": {"f": {"ops": ["strip_whitespace"]}}})
    with pytest.raises(CleanFrameError, match="booleans"):
        cf.stream_apply(recipe, p, tmp_path / "o.csv", chunksize=2, check_drift=False)


def test_quarantine_file_only_written_when_requested(tmp_path):
    p = tmp_path / "e.csv"
    pd.DataFrame({"email": ["a@b.com", "bad", "c@d.com"]}).to_csv(p, index=False)
    recipe = Recipe.from_dict(
        {"version": 1, "validate": [{"column": "email", "check": "valid_email", "on_fail": "quarantine"}]}
    )
    out = tmp_path / "o.csv"
    with pytest.warns(cf.CleanFrameWarning, match="quarantined"):
        s = cf.stream_apply(recipe, p, out, check_drift=False)
    assert s.rows_quarantined == 1 and s.quarantine_path is None
    assert sorted(x.name for x in tmp_path.iterdir()) == ["e.csv", "o.csv"]


def test_stream_overwrite_flag_is_honoured(tmp_path):
    p = tmp_path / "e.csv"
    pd.DataFrame({"a": [" x "]}).to_csv(p, index=False)
    recipe = Recipe.from_dict({"version": 1, "columns": {"a": {"ops": ["strip_whitespace"]}}})
    with pytest.raises(CleanFrameError):
        cf.stream_apply(recipe, p, p, check_drift=False)
    cf.stream_apply(recipe, p, p, check_drift=False, overwrite=True)
    assert "x" in p.read_text(encoding="utf-8")
