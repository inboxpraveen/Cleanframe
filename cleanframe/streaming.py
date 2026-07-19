"""Out-of-core replay: stream a recipe over a file too large to fit in memory.

Streaming applies ONLY to recipe *replay* (not planning — profiling/detection are
whole-frame). It is correct only for **row-independent** recipes: every op and
validation must produce the same result on a chunk as on the whole frame. That
subset streams with byte-identical output; anything else is a **hard, named
refusal**, never a silent divergence — because a misclassified global op would
silently corrupt output, the exact failure the library exists to prevent.

Refused (need the whole column / cross-row state): ``dedup``; ``fill_na`` with a
mean/median/mode/ffill/bfill strategy; ``cast`` to ``category``/``datetime``/``date``;
``parse_date`` *without* explicit formats (format inference is order-dependent);
the ``unique`` validator; and any op/validator not on the streamable allow-list
(default-DENY, so an unknown custom op is refused until proven row-independent).

Row-ids in the (bounded) diff summary are global — offset by the chunk's start.
"""

from __future__ import annotations

import os
import re
import warnings
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from ._util import ensure_parent, sanitize_dataframe_for_csv
from .errors import CleanFrameError
from .executor import execute
from .recipe import Recipe
from .types import Mode

# Column/frame ops that are provably row-independent (allow-list; default-DENY).
_STREAMABLE_OPS = frozenset(
    {
        "strip_whitespace", "collapse_whitespace", "lowercase", "uppercase", "title_case",
        "capitalize", "remove_symbols", "replace", "to_na", "normalize_email",
        "normalize_phone", "parse_number", "round", "normalize_values", "extract_currency",
        "normalize_unit", "drop_columns",
    }
)
_CMP_RE = re.compile(r"^(>=|<=|==|!=|>|<)\s*-?\d+(?:\.\d+)?$")
_CSV_STREAM_SUFFIXES = {".csv", ".txt", ".tsv", ""}


def is_op_streamable(op) -> bool:
    """True if applying ``op`` chunk-by-chunk equals applying it to the whole frame."""
    if op.name == "cast":
        return str(op.params.get("to", "")).lower() not in ("category", "datetime", "date")
    if op.name == "parse_date":
        return bool(op.params.get("formats"))  # inference over the series is order-dependent
    if op.name == "fill_na":
        return op.params.get("strategy") in (None, "zero", "empty")  # constant fill only
    return op.name in _STREAMABLE_OPS


def is_validation_streamable(rule) -> bool:
    """True for row-local checks; ``unique`` (and unknown custom checks) are not."""
    check = rule.check.strip()
    if check == "unique":
        return False
    if check in ("not_null", "valid_email", "valid_url", "valid_phone"):
        return True
    if _CMP_RE.match(check):
        return True
    # Mirror validate.pass_mask membership / regex prefixes (not bare startswith("in")).
    if check == "in" or check.startswith("in ") or check.startswith("in["):
        return True
    if check.startswith("matches") or check.startswith("regex"):
        return True
    return False  # unknown custom validator -> refuse (default-DENY)


def check_streamable(recipe: Recipe) -> None:
    """Raise :class:`~cleanframe.errors.CleanFrameError` naming the first op/validation
    that cannot stream. Returns ``None`` if the whole recipe is streamable."""
    for col in recipe.columns:
        for op in col.ops:
            if not is_op_streamable(op):
                raise CleanFrameError(
                    f"Recipe is not streamable: op {op.name!r} on column {col.source!r} needs the "
                    "whole column (global state). Run it whole-frame with apply_recipe(), or split "
                    "the recipe so the streaming part is row-independent."
                )
    for op in recipe.frame_ops:
        if not is_op_streamable(op):
            raise CleanFrameError(
                f"Recipe is not streamable: frame op {op.name!r} needs all rows. "
                "Run it whole-frame with apply_recipe()."
            )
    for rule in recipe.validations:
        if not is_validation_streamable(rule):
            raise CleanFrameError(
                f"Recipe is not streamable: validation {rule.column}:{rule.check} needs all rows "
                "(e.g. 'unique'). Run it whole-frame with apply_recipe()."
            )


def _stream_read_kwargs(recipe: Recipe, *, pin_dtype: bool) -> dict:
    """Build pandas ``read_csv`` kwargs from ``recipe.read``, matching ``apply_recipe``."""
    read = dict(recipe.read or {})
    if read.get("sheet") is not None:
        raise CleanFrameError(
            "Streaming does not support Excel sheet selection (recipe.read.sheet). "
            "Use apply_recipe() / cleanframe apply without --chunksize for workbooks."
        )
    kwargs: dict = {
        "encoding": read.get("encoding", "utf-8-sig"),
    }
    if read.get("sep"):
        kwargs["sep"] = read["sep"]
    if read.get("columns") is not None:
        kwargs["usecols"] = list(read["columns"])
    if read.get("skiprows") is not None:
        kwargs["skiprows"] = read["skiprows"]
    if read.get("nrows") is not None:
        kwargs["nrows"] = read["nrows"]
    if pin_dtype:
        # Pin dtypes for the stream so representation can't drift across chunks.
        # Drift checks intentionally leave inference on so fingerprints match planning.
        kwargs["dtype"] = str
    return kwargs


@dataclass
class StreamSummary:
    """Counts from a streamed replay (no full cell-level diff is kept)."""

    out_path: Path
    rows_in: int = 0
    rows_out: int = 0
    changed_cells: int = 0
    rows_dropped: int = 0
    rows_quarantined: int = 0
    chunks: int = 0
    quarantine_path: Path | None = None

    def render(self) -> str:
        base = (
            f"Streamed {self.rows_in:,} → {self.rows_out:,} rows in {self.chunks} chunk(s); "
            f"{self.changed_cells:,} cell(s) changed, {self.rows_dropped:,} dropped."
        )
        if self.rows_quarantined:
            base += f" {self.rows_quarantined:,} quarantined → {self.quarantine_path}."
        return base


def stream_apply(
    recipe: Recipe | str | Path | dict,
    in_path: str | Path,
    out_path: str | Path,
    *,
    chunksize: int = 100_000,
    mode: Mode | str = Mode.REVIEW,
    quarantine_path: str | Path | None = None,
    check_drift: bool = True,
    on_drift: str = "error",
) -> StreamSummary:
    """Replay ``recipe`` over ``in_path`` (CSV) in chunks, writing cleaned CSV to
    ``out_path`` without ever holding the whole file in memory.

    Refuses (raises) if the recipe contains any non-row-independent op/validation, so
    the streamed output is byte-identical to a whole-frame replay (values). Column
    dtypes are pinned to string on read so representation can't drift across chunks.

    ``recipe.read`` selection (``columns`` / ``sep`` / ``encoding`` / ``skiprows`` /
    ``nrows``) is applied the same way as :func:`apply_recipe`. Output is written to a
    temporary sibling file and atomically replaced onto ``out_path`` when the stream
    completes successfully.

    Like :func:`apply_recipe`, schema drift is checked first (on a bounded head sample)
    and, by default, refuses (``on_drift="error"``) so a drifted file is never silently
    streamed with a stale recipe.
    """
    if not isinstance(recipe, Recipe):
        from .api import _resolve_recipe

        recipe = _resolve_recipe(recipe)
    check_streamable(recipe)

    in_path = Path(in_path)
    out_path = ensure_parent(out_path)
    suffix = in_path.suffix.lower()
    if suffix not in _CSV_STREAM_SUFFIXES:
        raise CleanFrameError(
            f"Streaming (--chunksize) only supports CSV-family files, not {suffix or 'this file'}. "
            "Use apply_recipe() for Excel/Parquet/JSON."
        )
    mode = Mode.coerce(mode)
    head_kwargs = _stream_read_kwargs(recipe, pin_dtype=False)
    read_kwargs = _stream_read_kwargs(recipe, pin_dtype=True)

    if check_drift and not recipe.source_fingerprint:
        warnings.warn(
            "CleanFrame: recipe has no source_fingerprint — drift check skipped. "
            "Re-plan or stamp a fingerprint for production replay.",
            stacklevel=2,
        )

    # Drift check on a bounded head (read UN-pinned so dtypes match the recipe's
    # fingerprint, which was built from the original inferred-dtype read).
    if check_drift and recipe.source_fingerprint:
        from .drift import detect_drift
        from .errors import DriftError
        from .fingerprint import DEFAULT_SAMPLE_ROWS

        head = pd.read_csv(in_path, nrows=DEFAULT_SAMPLE_ROWS, **head_kwargs)
        drift = detect_drift(head, recipe, source=str(in_path))
        if drift.has_drift and (on_drift == "error" or mode is Mode.STRICT):
            raise DriftError(drift.render(), report=drift)
        if drift.has_drift and on_drift == "warn":
            warnings.warn("CleanFrame: " + drift.render(), stacklevel=2)

    summary = StreamSummary(out_path=Path(out_path))
    q_path = Path(quarantine_path) if quarantine_path else None
    q_tmp = Path(str(q_path) + ".cf-tmp") if q_path is not None else None
    q_written = False
    first = True
    tmp_out = Path(str(out_path) + ".cf-tmp")

    try:
        reader = pd.read_csv(in_path, chunksize=chunksize, **read_kwargs)
    except Exception as exc:  # noqa: BLE001
        raise CleanFrameError(f"Could not open {in_path.name} for streaming: {exc}.") from exc

    try:
        for chunk in reader:
            # max_diff_changes=0: count every change but store none (streaming keeps counts,
            # not a full in-memory lineage — the invariant-5 cap, applied globally). The
            # per-chunk "diff truncated" warning is expected here, so silence just that one.
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", message="CleanFrame: diff detail truncated")
                result = execute(recipe, chunk, mode=mode, max_diff_changes=0)
            cleaned = sanitize_dataframe_for_csv(result.dataframe)
            cleaned.to_csv(
                tmp_out, mode="w" if first else "a", header=first, index=False,
                encoding="utf-8", lineterminator="\n",
            )
            summary.rows_in += len(chunk)
            summary.rows_out += len(cleaned)
            summary.changed_cells += result.diff.changed_cells
            summary.rows_dropped += len(result.diff.dropped_rows)
            summary.chunks += 1
            if result.has_quarantine:
                if q_path is None:
                    q_path = Path(out_path).with_suffix(".quarantine.csv")
                    q_tmp = Path(str(q_path) + ".cf-tmp")
                assert q_tmp is not None
                q = sanitize_dataframe_for_csv(result.quarantine)
                q.to_csv(
                    q_tmp, mode="w" if not q_written else "a", header=not q_written,
                    index=False, encoding="utf-8", lineterminator="\n",
                )
                q_written = True
                summary.rows_quarantined += len(result.quarantine)
            first = False

        if first:
            # Empty input — emit a header-only CSV using the recipe's column projection
            # (or the file's columns) so the output is a valid empty table, not headerless.
            cols = list(read_kwargs.get("usecols") or [])
            if not cols:
                try:
                    cols = list(pd.read_csv(in_path, nrows=0, **head_kwargs).columns)
                except Exception:  # noqa: BLE001
                    cols = [c.source for c in recipe.columns]
            pd.DataFrame(columns=cols).to_csv(
                tmp_out, index=False, encoding="utf-8", lineterminator="\n"
            )

        os.replace(tmp_out, out_path)
        if q_written and q_path is not None and q_tmp is not None:
            os.replace(q_tmp, q_path)
    except Exception:
        if tmp_out.exists():
            tmp_out.unlink(missing_ok=True)
        if q_tmp is not None and q_tmp.exists():
            q_tmp.unlink(missing_ok=True)
        raise

    summary.quarantine_path = q_path if q_written else None
    return summary


__all__ = [
    "stream_apply",
    "check_streamable",
    "is_op_streamable",
    "is_validation_streamable",
    "StreamSummary",
]
