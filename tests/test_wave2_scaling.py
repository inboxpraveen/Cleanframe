"""Wave 2 — scaling optimizations must preserve output byte-for-byte.

These lock the determinism/correctness contract while the executor snapshots only
op-touched columns, the diff extracts in bulk, and the profiler dedupes its work.
"""
from __future__ import annotations

import pandas as pd

import cleanframe as cf
from cleanframe.executor import execute
from cleanframe.profile import _value_counts_stable, profile_dataframe
from cleanframe.recipe import Recipe


def test_partial_snapshot_tracks_transformed_not_passthrough():
    """The op-touched-columns snapshot must diff transformed columns (incl. rows later
    dropped) and must NOT invent changes for untouched pass-through columns."""
    df = pd.DataFrame(
        {
            "name": ["  Alice ", "bob", "bob"],  # transformed; row 2 is a dup
            "note": ["x", "y", "y"],  # pass-through, unchanged
            "amt": ["$10", "$20", "$20"],  # transformed
        }
    )
    recipe = Recipe.from_dict(
        {
            "version": 1,
            "columns": {
                "name": {"ops": ["strip_whitespace", "title_case"]},
                "amt": {"ops": ["parse_number"]},
            },
            "dedup": {"subset": ["note"]},
        }
    )
    result = execute(recipe, df)
    changed = {(c.row_id, c.column) for c in result.diff.changes}
    # name + amt changed on all 3 original rows (row 2 was transformed then dropped).
    assert (2, "name") in changed and (2, "amt") in changed  # mutated-then-dropped tracked
    assert result.diff.changed_cells == 6, result.diff.summary()
    # 'note' is pass-through: it must NOT appear as changed.
    assert "note" not in result.diff.changed_columns
    assert result.diff.summary()["rows_dropped"] == 1


def test_clean_is_deterministic_and_matches_repeat():
    df = pd.DataFrame(
        {
            "City": ["BLR", "blr ", "Mumbai", "MUMBAI", "Delhi"] * 4,
            "Amount": ["₹1,200", "₹2,50,000", "(500)", "₹0", "₹99"] * 4,
        }
    )
    a = cf.clean(df, mode="auto")
    b = cf.clean(df, mode="auto")
    assert a.recipe.to_dict() == b.recipe.to_dict()
    pd.testing.assert_frame_equal(a.dataframe, b.dataframe)
    assert a.diff.changed_cells == b.diff.changed_cells


def test_value_counts_stable_deterministic_tiebreak():
    """The optimized top-N must keep the documented (-count, str(value)) tie-break."""
    s = pd.Series(["b", "a", "c", "a", "b", "c", "d"])  # a,b,c all count 2; d count 1
    out = _value_counts_stable(s, top=3)
    # highest count first; ties broken lexicographically -> a, b, c (all count 2)
    assert out == [("a", 2), ("b", 2), ("c", 2)]


def test_profiler_signals_unchanged_on_repeated_values():
    """Deduping the signal computation must not change the inferred semantic type."""
    df = pd.DataFrame({"amt": ["$1,200"] * 1000 + ["$3,400"] * 1000})
    prof = profile_dataframe(df)
    assert prof.column("amt").semantic_type == "currency"
