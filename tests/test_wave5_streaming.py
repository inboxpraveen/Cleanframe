"""Wave 5 — out-of-core streamed replay: byte-identical for streamable recipes,
loud refusal for global ops."""
from __future__ import annotations

import io

import pandas as pd
import pytest

import cleanframe as cf
from cleanframe.errors import CleanFrameError, DriftError
from cleanframe.executor import execute
from cleanframe.recipe import Recipe


def _messy_csv(tmp_path, n=25):
    p = tmp_path / "big.csv"
    rows = {
        "name": [f"  Person{i % 7} " for i in range(n)],
        "amount": [["$1,200", "$3,400", "(500)"][i % 3] for i in range(n)],
        "note": [f"n{i}" for i in range(n)],  # pass-through text column
    }
    pd.DataFrame(rows).to_csv(p, index=False)
    return p


_STREAMABLE = {
    "version": 1,
    "columns": {
        "name": {"ops": ["strip_whitespace", "title_case"]},
        "amount": {"ops": ["parse_number"]},
    },
}


def test_stream_apply_byte_identical_to_wholeframe(tmp_path):
    p = _messy_csv(tmp_path, n=25)
    recipe = Recipe.from_dict(_STREAMABLE)
    # Reference: whole-frame replay reading everything as strings (isolate chunking
    # from dtype inference), rendered to CSV the same way.
    ref = execute(recipe, pd.read_csv(p, dtype=str)).dataframe.reset_index(drop=True)

    out = tmp_path / "streamed.csv"
    summary = cf.stream_apply(
        recipe, p, out, chunksize=4, check_drift=False
    )  # tiny chunks; no fingerprint on hand-built recipe
    got = pd.read_csv(out, dtype=str).reset_index(drop=True)

    pd.testing.assert_frame_equal(
        got, pd.read_csv(io.StringIO(ref.to_csv(index=False)), dtype=str).reset_index(drop=True)
    )
    assert summary.rows_in == 25 and summary.rows_out == 25 and summary.chunks == 7


def test_stream_refuses_dedup(tmp_path):
    p = _messy_csv(tmp_path)
    recipe = Recipe.from_dict({**_STREAMABLE, "dedup": True})
    with pytest.raises(CleanFrameError, match="dedup"):
        cf.stream_apply(recipe, p, tmp_path / "o.csv")


def test_stream_refuses_unique_validation(tmp_path):
    p = _messy_csv(tmp_path)
    recipe = Recipe.from_dict(
        {**_STREAMABLE, "validate": [{"column": "note", "check": "unique"}]}
    )
    with pytest.raises(CleanFrameError, match="unique"):
        cf.stream_apply(recipe, p, tmp_path / "o.csv")


def test_stream_refuses_formatless_parse_date(tmp_path):
    p = _messy_csv(tmp_path)
    recipe = Recipe.from_dict({"version": 1, "columns": {"note": {"ops": [{"parse_date": {}}]}}})
    with pytest.raises(CleanFrameError, match="parse_date"):
        cf.stream_apply(recipe, p, tmp_path / "o.csv")


def test_stream_allows_parse_date_with_formats(tmp_path):
    recipe = Recipe.from_dict(
        {"version": 1, "columns": {"d": {"ops": [{"parse_date": {"formats": ["%d/%m/%Y"]}}]}}}
    )
    cf.check_streamable(recipe)  # must not raise


def test_stream_checks_drift_by_default(tmp_path):
    """Streaming must not silently lose apply's refuse-on-drift guarantee."""
    p1 = tmp_path / "v1.csv"
    pd.DataFrame({"name": ["  a ", "  b ", "  c "]}).to_csv(p1, index=False)
    recipe = cf.clean(p1, mode="auto").recipe  # carries a source_fingerprint
    # A drifted file (the 'name' column is gone) must refuse by default.
    p2 = tmp_path / "v2.csv"
    pd.DataFrame({"label": ["x", "y"]}).to_csv(p2, index=False)
    with pytest.raises(DriftError):
        cf.stream_apply(recipe, p2, tmp_path / "o.csv")
    # Opting out of the drift check streams anyway (no raise).
    cf.stream_apply(recipe, p2, tmp_path / "o.csv", check_drift=False)


def test_stream_quarantine_sidecar(tmp_path):
    p = tmp_path / "emails.csv"
    pd.DataFrame({"email": ["a@b.com", "bad", "c@d.com", "nope", "e@f.com"]}).to_csv(p, index=False)
    recipe = Recipe.from_dict(
        {"version": 1, "validate": [{"column": "email", "check": "valid_email", "on_fail": "quarantine"}]}
    )
    out = tmp_path / "clean.csv"
    summary = cf.stream_apply(recipe, p, out, chunksize=2, check_drift=False)
    assert summary.rows_out == 3 and summary.rows_quarantined == 2
    assert summary.quarantine_path is not None and summary.quarantine_path.exists()
