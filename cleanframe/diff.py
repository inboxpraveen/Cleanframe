"""Cell-level diff: exactly which values changed, and how.

Every transform is tracked. Given the original frame, the cleaned frame, and the
column lineage (which output column came from which source), :func:`compute_diff`
produces a :class:`CellDiff` recording each changed cell (``before → after``), the
columns added/removed/renamed, and the rows dropped and why. This is the lineage
that makes CleanFrame auditable — "Every changed cell is tracked."

Rows are matched by a stable integer id (the original positional index), so cells
line up correctly even after dedup removes rows.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from ._util import DEFAULT_MAX_DIFF_CHANGES
from .ops import _is_na


def _equal(a: Any, b: Any) -> bool:
    """Value equality that treats NaN==NaN as equal and any NaN/value pair as changed."""
    a_na, b_na = _is_na(a), _is_na(b)
    if a_na and b_na:
        return True
    if a_na or b_na:
        return False
    if isinstance(a, float) and isinstance(b, float):
        if math.isnan(a) and math.isnan(b):
            return True
    try:
        return bool(a == b)
    except Exception:  # noqa: BLE001 - exotic types compare unequal
        return False


def _changed_mask(before: pd.Series, after: pd.Series) -> pd.Series:
    """Vectorised element-wise 'changed?' mask with NaN==NaN treated as unchanged."""
    both_na = before.isna().to_numpy() & after.isna().to_numpy()
    try:
        equal = (before.to_numpy() == after.to_numpy())
    except Exception:  # noqa: BLE001 - dtype mismatch -> fall back element-wise
        equal = pd.Series(
            [_equal(b, a) for b, a in zip(before.tolist(), after.tolist(), strict=False)],
            index=before.index,
        ).to_numpy()
    equal = pd.Series(equal, index=before.index).fillna(False).astype(bool).to_numpy()
    return pd.Series(~(equal | both_na), index=before.index)


@dataclass
class CellChange:
    row_id: int
    column: str
    before: Any
    after: Any


@dataclass
class CellDiff:
    """A structured record of everything a recipe changed."""

    changes: list[CellChange] = field(default_factory=list)
    added_columns: list[str] = field(default_factory=list)
    removed_columns: list[str] = field(default_factory=list)
    renamed_columns: dict[str, str] = field(default_factory=dict)  # source -> output
    dropped_rows: list[tuple[int, str]] = field(default_factory=list)  # (row_id, reason)
    n_rows_before: int = 0
    n_rows_after: int = 0
    #: True when ``max_changes`` stopped recording further cell edits (summary still exact).
    truncated: bool = False
    #: Total cells that changed, including those not stored when truncated.
    total_changed_cells: int = 0

    # -- summaries -------------------------------------------------------
    @property
    def changed_cells(self) -> int:
        return self.total_changed_cells or len(self.changes)

    @property
    def changed_columns(self) -> list[str]:
        seen: list[str] = []
        for c in self.changes:
            if c.column not in seen:
                seen.append(c.column)
        return seen

    def changes_by_column(self) -> dict[str, list[CellChange]]:
        out: dict[str, list[CellChange]] = {}
        for c in self.changes:
            out.setdefault(c.column, []).append(c)
        return out

    def to_frame(self) -> pd.DataFrame:
        """A tidy DataFrame of changes: ``row_id, column, before, after``."""
        return pd.DataFrame(
            [(c.row_id, c.column, c.before, c.after) for c in self.changes],
            columns=["row_id", "column", "before", "after"],
        )

    def summary(self) -> dict[str, Any]:
        return {
            "changed_cells": self.changed_cells,
            "changed_columns": len(self.changed_columns),
            "added_columns": list(self.added_columns),
            "removed_columns": list(self.removed_columns),
            "renamed_columns": dict(self.renamed_columns),
            "rows_dropped": len(self.dropped_rows),
            "rows_before": self.n_rows_before,
            "rows_after": self.n_rows_after,
            "truncated": self.truncated,
            "stored_changes": len(self.changes),
        }

    def is_empty(self) -> bool:
        return not (
            self.changes
            or self.added_columns
            or self.removed_columns
            or self.renamed_columns
            or self.dropped_rows
        )

    # -- rendering -------------------------------------------------------
    def render(
        self, *, max_per_column: int = 8, color: bool | None = None, ascii: bool = False
    ) -> str:
        """Render a git-diff-style textual summary (also used by ``show``).

        Pass ``ascii=True`` to substitute plain ASCII for the ``→``/``∅`` glyphs so
        the text is safe to print on a non-UTF-8 console (e.g. a cp1252 Windows
        terminal). :meth:`show` selects this automatically per output stream.
        """
        return _render_text(self, max_per_column=max_per_column, color=color, ascii=ascii)

    def show(
        self,
        *,
        max_per_column: int = 8,
        color: bool | None = None,
        stream: Any = None,
        ascii: bool | None = None,
    ) -> None:
        """Print the diff, git-diff style. Never crashes on a non-UTF-8 console.

        ``ascii`` defaults to auto: ASCII glyphs are used when the target stream
        cannot encode ``→``/``∅`` (the default Windows cp1252 console), so library
        users get clean output without the CLI's stdout reconfiguration.
        """
        import sys

        out = stream if stream is not None else sys.stdout
        if ascii is None:
            ascii = not _stream_supports_unicode(out)
        text = self.render(max_per_column=max_per_column, color=color, ascii=ascii)
        try:
            print(text, file=out)
        except UnicodeEncodeError:  # last-resort backstop: force ASCII
            print(self.render(max_per_column=max_per_column, color=color, ascii=True), file=out)

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        s = self.summary()
        return (
            f"CellDiff(changed_cells={s['changed_cells']}, "
            f"columns={s['changed_columns']}, rows_dropped={s['rows_dropped']})"
        )


def compute_diff(
    original: pd.DataFrame,
    cleaned: pd.DataFrame,
    lineage: dict[str, str | None],
    *,
    dropped_rows: list[tuple[int, str]] | None = None,
    dropped_after: pd.DataFrame | None = None,
    n_rows_before: int | None = None,
    max_changes: int | None = DEFAULT_MAX_DIFF_CHANGES,
) -> CellDiff:
    """Compute a :class:`CellDiff`.

    Parameters
    ----------
    original:
        The input frame, indexed by stable row id (positional 0..n-1).
    cleaned:
        The output frame, indexed by the surviving row ids.
    lineage:
        ``{output_column: source_column_or_None}``. ``None`` means the column was
        derived/added (no "before" value).
    dropped_rows:
        ``(row_id, reason)`` pairs for rows removed during execution.
    dropped_after:
        Post-transform values (indexed by row id) of rows that were later dropped,
        so a value rewrite applied to a row before it was removed is still tracked
        as a changed cell (invariant #5) rather than vanishing with the row.
    max_changes:
        Cap on stored :class:`CellChange` entries. ``None`` stores every change
        (can OOM on large dirty frames). Counts in :attr:`CellDiff.changed_cells`
        remain exact even when the detail list is truncated.
    """
    diff = CellDiff(
        dropped_rows=list(dropped_rows or []),
        n_rows_before=n_rows_before if n_rows_before is not None else int(len(original)),
        n_rows_after=int(len(cleaned)),
    )

    source_cols = set(original.columns.astype(str))
    used_sources: set[str] = set()
    total = 0
    store_cap = max_changes  # None = unlimited

    for out_col in cleaned.columns:
        out_col = str(out_col)
        source = lineage.get(out_col, out_col if out_col in source_cols else None)
        if source is None:
            diff.added_columns.append(out_col)
            continue
        used_sources.add(source)
        if source != out_col:
            diff.renamed_columns[source] = out_col

        after_series = cleaned[out_col]
        if dropped_after is not None and out_col in dropped_after.columns:
            # Append the post-transform values of dropped rows so their edits are
            # tracked too (the rows still appear separately in dropped_rows).
            after_series = pd.concat([after_series, dropped_after[out_col]])
        before_series = original[source].reindex(after_series.index)
        changed_mask = _changed_mask(before_series, after_series)
        changed_ids = after_series.index[changed_mask.to_numpy()]
        total += int(len(changed_ids))

        if store_cap is not None and len(diff.changes) >= store_cap:
            diff.truncated = True
            continue

        for row_id in changed_ids:
            if store_cap is not None and len(diff.changes) >= store_cap:
                diff.truncated = True
                break
            diff.changes.append(
                CellChange(
                    int(row_id),
                    out_col,
                    before_series.loc[row_id],
                    after_series.loc[row_id],
                )
            )

    for src in original.columns:
        src = str(src)
        if src not in used_sources:
            diff.removed_columns.append(src)

    diff.total_changed_cells = total
    return diff


# ---------------------------------------------------------------------------
# text rendering
# ---------------------------------------------------------------------------
def _stream_supports_unicode(stream: Any) -> bool:
    """True if ``stream`` can encode the diff glyphs (``→``/``∅``/``⚠``)."""
    enc = getattr(stream, "encoding", None) or "utf-8"
    try:
        "→∅⚠".encode(enc)
        return True
    except (UnicodeEncodeError, LookupError):
        return False


def _fmt(value: Any, ascii: bool = False) -> str:
    if _is_na(value):
        return "<NA>" if ascii else "∅"
    if isinstance(value, str):
        return repr(value)
    return str(value)


def _render_text(
    diff: CellDiff, *, max_per_column: int, color: bool | None, ascii: bool = False
) -> str:
    import os
    import sys

    arrow = "->" if ascii else "→"
    if color is None:
        color = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
    red = "\x1b[31m" if color else ""
    green = "\x1b[32m" if color else ""
    dim = "\x1b[2m" if color else ""
    bold = "\x1b[1m" if color else ""
    reset = "\x1b[0m" if color else ""

    if diff.is_empty():
        return f"{dim}No changes.{reset}"

    lines: list[str] = []
    s = diff.summary()
    lines.append(
        f"{bold}CleanFrame diff{reset}  "
        f"{s['changed_cells']} cell(s) changed in {s['changed_columns']} column(s), "
        f"{s['rows_dropped']} row(s) dropped "
        f"({s['rows_before']} {arrow} {s['rows_after']} rows)"
        + (
            f"  {dim}[detail truncated to {s['stored_changes']}]{reset}"
            if s.get("truncated")
            else ""
        )
    )
    if diff.renamed_columns:
        renames = ", ".join(f"{k} {arrow} {v}" for k, v in diff.renamed_columns.items())
        lines.append(f"  {dim}renamed:{reset} {renames}")
    if diff.added_columns:
        lines.append(f"  {green}added columns:{reset} {', '.join(diff.added_columns)}")
    if diff.removed_columns:
        lines.append(f"  {red}removed columns:{reset} {', '.join(diff.removed_columns)}")

    for column, changes in diff.changes_by_column().items():
        lines.append("")
        lines.append(f"{bold}{column}{reset}  {dim}({len(changes)} changed){reset}")
        for change in changes[:max_per_column]:
            lines.append(
                f"  row {change.row_id}: "
                f"{red}- {_fmt(change.before, ascii)}{reset}  "
                f"{green}+ {_fmt(change.after, ascii)}{reset}"
            )
        if len(changes) > max_per_column:
            lines.append(f"  {dim}… and {len(changes) - max_per_column} more{reset}")

    if diff.dropped_rows:
        lines.append("")
        reasons: dict[str, int] = {}
        for _, reason in diff.dropped_rows:
            reasons[reason] = reasons.get(reason, 0) + 1
        detail = ", ".join(f"{n} ({r})" for r, n in reasons.items())
        lines.append(f"{red}dropped rows:{reset} {detail}")

    return "\n".join(lines)


__all__ = ["CellDiff", "CellChange", "compute_diff"]
