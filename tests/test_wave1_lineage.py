"""Wave 1 — Batch B: executor lineage / silent-corruption regression tests.

Covers the "nothing silently dropped" + "every changed cell tracked" invariants:
emitted-column overwrites (H1/H5/L7) and mutated-then-dropped provenance (H2).
"""
from __future__ import annotations

import pandas as pd

import cleanframe as cf
from cleanframe.executor import execute
from cleanframe.recipe import Recipe


def _recipe(d: dict) -> Recipe:
    return Recipe.from_dict({"version": 1, **d})


def test_emit_overwriting_existing_column_is_tracked_in_diff():
    """H1/H5: extract_currency emitting onto a pre-existing column must record the
    overwritten cells in the diff (not vanish as add+remove)."""
    df = pd.DataFrame({"amount": ["$5", "€6"], "amount_currency": ["keep-A", "keep-B"]})
    recipe = _recipe({"columns": {"amount": {"ops": [{"extract_currency": "amount_currency"}]}}})
    result = execute(recipe, df)
    # The destroyed cells (keep-A/keep-B -> USD/EUR) must appear in the diff.
    changed = {(c.row_id, c.column): (c.before, c.after) for c in result.diff.changes}
    assert (0, "amount_currency") in changed
    assert changed[(0, "amount_currency")] == ("keep-A", "USD")
    # And it must NOT be reported as both added and removed.
    assert "amount_currency" not in result.diff.added_columns
    assert "amount_currency" not in result.diff.removed_columns


def test_two_ops_emitting_same_column_raise_execution_error():
    """L7: two column recipes emitting the same derived name must fail loud, like renames."""
    df = pd.DataFrame({"a": ["$5"], "b": ["€6"]})
    recipe = _recipe(
        {
            "columns": {
                "a": {"ops": [{"extract_currency": "cur"}]},
                "b": {"ops": [{"extract_currency": "cur"}]},
            }
        }
    )
    try:
        execute(recipe, df)
        raise AssertionError("expected an ExecutionError for colliding emit targets")
    except cf.ExecutionError:
        pass


def test_extract_currency_reapply_is_idempotent():
    """H1 guard must not break idempotency: re-emitting the same column is a no-op."""
    df = pd.DataFrame({"amount": ["$5", "€6"]})
    recipe = _recipe({"columns": {"amount": {"ops": [{"extract_currency": "amount_currency"}]}}})
    once = execute(recipe, df).dataframe
    twice = execute(recipe, once)
    assert twice.diff.is_empty(), f"re-apply should be a no-op, got {twice.diff.summary()}"


def test_mutated_then_dropped_rows_keep_their_change_provenance():
    """H2: a value rewrite on a row later removed by dedup must still be tracked."""
    df = pd.DataFrame({"state": ["activated"] * 3 + ["deactivated"] * 5})
    recipe = _recipe(
        {
            "columns": {"state": {"ops": [{"normalize_values": {"activated": "deactivated"}}]}},
            "dedup": True,
        }
    )
    result = execute(recipe, df)
    # 3 rows were 'activated' -> 'deactivated' (real changes), then dedup kept 1 row.
    assert result.diff.changed_cells >= 3, result.diff.summary()
    assert len(result.dataframe) == 1
    # The dropped rows are still recorded as dropped.
    assert result.diff.summary()["rows_dropped"] == 7
