"""The executor: replay a recipe on a dataframe, deterministically.

This is the "Pandas executes" half of the promise. It runs a recipe in fixed
phases — column ops → renames → frame ops → validation — tracking column lineage
and a stable row id throughout so a complete :class:`~cleanframe.diff.CellDiff`
can be computed at the end. No AI, no network, no randomness: the same recipe and
the same frame always produce the same output, the same diff, and the same
quarantine.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field

import pandas as pd

from ._util import DEFAULT_MAX_DIFF_CHANGES
from .diff import CellDiff, compute_diff
from .errors import ExecutionError
from .ops import apply_column_op, apply_frame_op
from .recipe import Recipe
from .types import Mode
from .validate import ValidationResult, apply_validations


@dataclass
class ExecutionResult:
    """Everything replaying a recipe produced."""

    dataframe: pd.DataFrame
    diff: CellDiff
    quarantine: pd.DataFrame = field(default_factory=pd.DataFrame)
    validation_results: list[ValidationResult] = field(default_factory=list)
    lineage: dict[str, str | None] = field(default_factory=dict)
    log: list[str] = field(default_factory=list)

    @property
    def has_quarantine(self) -> bool:
        return not self.quarantine.empty


def execute(
    recipe: Recipe,
    df: pd.DataFrame,
    *,
    mode: Mode | str = Mode.REVIEW,
    max_diff_changes: int | None = DEFAULT_MAX_DIFF_CHANGES,
) -> ExecutionResult:
    """Apply ``recipe`` to ``df`` and return the cleaned frame plus full lineage.

    Parameters
    ----------
    max_diff_changes:
        Cap on stored cell-level diff entries (default 100_000). Pass ``None`` to
        store every change. Counts remain exact when truncated.
    """
    mode = Mode.coerce(mode)
    # Stable positional row id (survives renames and row drops for the diff).
    work = df.reset_index(drop=True)
    original = work.copy()
    log: list[str] = []
    dropped_rows: list[tuple[int, str]] = []

    # source_of[current_column] -> original source column, or None if derived.
    source_of: dict[str, str | None] = {str(c): str(c) for c in work.columns}

    # -- Phase 1: column ops --------------------------------------------
    for col_recipe in recipe.columns:
        src = col_recipe.source
        if src not in work.columns:
            msg = f"recipe references column {src!r}, which is not in the data"
            if mode is Mode.STRICT:
                raise ExecutionError(msg + " (strict mode)")
            log.append("skipped: " + msg)
            warnings.warn(
                f"CleanFrame: skipped recipe column {src!r} — not present in the data. "
                "Use mode='strict' to fail instead, or re-plan / suggest_update for drift.",
                stacklevel=2,
            )
            continue

        series = work[src]
        emitted: dict[str, pd.Series] = {}
        for op in col_recipe.ops:
            result = apply_column_op(op, series)
            series = result.series
            for name, extra in result.emit.items():
                emitted[name] = extra
        work[src] = series
        for name, extra in emitted.items():
            work[name] = extra.reindex(work.index)
            source_of[name] = None  # derived column, no "before"
            log.append(f"{src}: emitted derived column {name!r}")

    # -- Phase 2: renames -----------------------------------------------
    rename_map = {
        c.source: c.rename_to
        for c in recipe.columns
        if c.rename_to and c.source in work.columns
    }
    if rename_map:
        targets = list(rename_map.values())
        survivors = [str(c) for c in work.columns if c not in rename_map]
        clash = (set(targets) & set(survivors)) | {t for t in targets if targets.count(t) > 1}
        if clash:
            raise ExecutionError(f"Recipe renames collide on output name(s): {sorted(clash)}")
        work = work.rename(columns=rename_map)
        for src, dst in rename_map.items():
            source_of[dst] = source_of.pop(src, src)

    # -- Phase 3: frame ops (dedup, drop_columns, …) --------------------
    for op in recipe.frame_ops:
        before = set(work.index)
        work = apply_frame_op(op, work)
        dropped = before - set(work.index)
        for rid in sorted(dropped):
            dropped_rows.append((int(rid), op.name))
        # a frame op can also remove columns (drop_columns); keep lineage tidy
        for name in list(source_of):
            if name not in work.columns:
                source_of.pop(name, None)
        if dropped:
            log.append(f"{op.name}: dropped {len(dropped)} row(s)")

    # -- Phase 4: validation --------------------------------------------
    outcome = apply_validations(work, recipe.validations, mode)
    work = outcome.dataframe
    dropped_rows.extend(outcome.removed_rows)
    log.extend(outcome.log)

    # -- Diff -----------------------------------------------------------
    diff = compute_diff(
        original,
        work,
        source_of,
        dropped_rows=dropped_rows,
        n_rows_before=int(len(original)),
        max_changes=max_diff_changes,
    )
    if diff.truncated:
        msg = (
            f"diff detail truncated to {len(diff.changes)} of {diff.changed_cells} "
            "changed cells (raise max_diff_changes or pass None for a full lineage)"
        )
        log.append(msg)
        warnings.warn("CleanFrame: " + msg, stacklevel=2)

    return ExecutionResult(
        dataframe=work,
        diff=diff,
        quarantine=outcome.quarantine,
        validation_results=outcome.results,
        lineage=source_of,
        log=log,
    )


__all__ = ["execute", "ExecutionResult"]
