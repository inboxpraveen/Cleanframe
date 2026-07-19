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
    from ._util import ensure_string_columns

    mode = Mode.coerce(mode)
    # Stable positional row id (survives renames and row drops for the diff).
    work = ensure_string_columns(df).reset_index(drop=True)
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
                if name in emitted:
                    raise ExecutionError(
                        f"Column {src!r} emits {name!r} more than once; rename one target."
                    )
                emitted[name] = extra
        work[src] = series
        for name, extra in emitted.items():
            if name in source_of and source_of[name] is None:
                # Another op this run already emitted this derived column — two ops
                # writing the same output name would silently clobber each other
                # (the rename phase already guards this class of collision).
                raise ExecutionError(
                    f"Two ops emit the same derived column {name!r}; rename one target."
                )
            work[name] = extra.reindex(work.index)
            if name not in source_of:
                source_of[name] = None  # brand-new derived column, no "before"
            # else: `name` is an existing source column being overwritten — keep its
            # lineage so every clobbered cell is tracked in the diff, and an
            # idempotent re-emit of identical values registers as no change.
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

    # Post-transform values of rows that later get dropped, so a value rewrite on a
    # row that dedup/validation removes is still tracked in the diff (invariant #5).
    # Only the dropped rows are snapshotted, not the whole frame.
    dropped_snaps: list[pd.DataFrame] = []

    # -- Phase 3: frame ops (dedup, drop_columns, …) --------------------
    for op in recipe.frame_ops:
        prev = work
        work = apply_frame_op(op, prev)
        dropped = set(prev.index) - set(work.index)
        if dropped:
            ids = sorted(dropped)
            for rid in ids:
                dropped_rows.append((int(rid), op.name))
            dropped_snaps.append(prev.loc[ids])
            log.append(f"{op.name}: dropped {len(dropped)} row(s)")
        # a frame op can also remove columns (drop_columns); keep lineage tidy
        for name in list(source_of):
            if name not in work.columns:
                source_of.pop(name, None)

    # -- Phase 4: validation --------------------------------------------
    pre_validation = work
    outcome = apply_validations(work, recipe.validations, mode)
    work = outcome.dataframe
    if outcome.removed_rows:
        present = [rid for rid, _ in outcome.removed_rows if rid in pre_validation.index]
        if present:
            dropped_snaps.append(pre_validation.loc[present])
    dropped_rows.extend(outcome.removed_rows)
    log.extend(outcome.log)

    dropped_after = pd.concat(dropped_snaps) if dropped_snaps else None

    # -- Diff -----------------------------------------------------------
    diff = compute_diff(
        original,
        work,
        source_of,
        dropped_rows=dropped_rows,
        dropped_after=dropped_after,
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
