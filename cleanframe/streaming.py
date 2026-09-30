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

import logging
import os
import re
import warnings
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from ._util import check_output_target, sanitize_dataframe_for_csv
from .errors import CleanFrameError, CleanFrameWarning
from .executor import execute
from .recipe import Recipe
from .types import Mode

logger = logging.getLogger(__name__)

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
    if op.name in _STREAMABLE_OPS:
        return True
    from .ops import OP_REGISTRY

    spec = OP_REGISTRY.get(op.name)
    return bool(spec and spec.streamable)  # a plugin op must opt in explicitly


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


#: dtype names a chunked scan can merge. Anything else (uint64, datetimes, ...) has no
#: chunk-independent whole-frame equivalent, so it is refused rather than guessed.
_MERGEABLE = frozenset({"int64", "float64", "bool", "object", "str", "string"})


def _merge_dtype(column: str, seen: set[str]) -> str:
    """Whole-file dtype pandas would infer, from the per-chunk inferred dtypes.

    ``int64`` + ``float64`` (or an int column with a blank somewhere) is ``float64``;
    text anywhere makes the column text; booleans only stay booleans if every chunk is.
    """
    names = {"object" if n in ("str", "string") else n for n in seen}
    unsupported = names - {"int64", "float64", "bool", "object"}
    if unsupported:
        raise CleanFrameError(
            f"Cannot stream: column {column!r} is read as {sorted(unsupported)[0]} in some "
            "chunk, which has no chunk-independent whole-frame equivalent. Run it "
            "whole-frame with apply_recipe()."
        )
    if "bool" in names and len(names) > 1:
        raise CleanFrameError(
            f"Cannot stream: column {column!r} mixes booleans with other values or blanks "
            "across chunks, so a chunked read cannot reproduce the whole-frame result. "
            "Run it whole-frame with apply_recipe()."
        )
    if names == {"bool"}:
        return "bool"
    if "object" in names:
        return "object"
    if "float64" in names:
        return "float64"
    return "int64"


def _pin_for(merged: str) -> Any:
    return str if merged == "object" else merged


def _iter_chunks(reader: Any, path: Path, chunksize: int):
    """Yield chunks, turning any pandas read failure into a CleanFrameError."""
    n = 0
    while True:
        try:
            chunk = next(reader)
        except StopIteration:
            return
        except Exception as exc:  # noqa: BLE001 - IO/parse boundary
            raise CleanFrameError(
                f"Could not read {path.name} near data row {n * chunksize + 1:,} "
                f"(chunk {n + 1}): {exc}."
            ) from exc
        yield chunk
        n += 1


def _check_row_widths(in_path: Path, kwargs: dict) -> None:
    """Refuse a record with more fields than the header, as a whole-frame read does.

    pandas' chunked reader silently *truncates* such a row (whichever chunk it lands in),
    so ``stream_apply`` would drop data ``apply_recipe`` refuses. The stdlib ``csv``
    reader counts fields exactly, in C, at a small fraction of the parse cost.
    """
    import csv
    import itertools

    skip = kwargs.get("skiprows")
    if callable(skip):
        return  # cannot mirror an arbitrary callable cheaply; pandas' own checks still apply
    skip_first, skip_lines = 0, set()
    if isinstance(skip, int) and not isinstance(skip, bool):
        skip_first = skip
    elif isinstance(skip, (list, tuple, set)):
        skip_lines = {int(i) for i in skip}
    limit = kwargs.get("nrows")
    delimiter = kwargs.get("sep") or ","
    if len(delimiter) != 1:
        return  # a regex separator is the python engine's business
    try:
        with open(in_path, encoding=kwargs.get("encoding", "utf-8-sig"), newline="") as handle:
            lines: Iterable[str] = itertools.islice(handle, skip_first, None)
            if skip_lines:
                lines = (ln for i, ln in enumerate(lines, skip_first) if i not in skip_lines)
            width = None
            for number, row in enumerate(csv.reader(lines, delimiter=delimiter)):
                if not row:
                    continue
                if width is None:
                    width = len(row)
                    continue
                if limit is not None and number > limit:
                    return
                if len(row) > width:
                    raise CleanFrameError(
                        f"{in_path.name}: data row {number} has {len(row)} fields but the header "
                        f"has {width}. Streaming would silently drop the extra field(s); fix the "
                        "row, or use apply_recipe(), which refuses such a file."
                    )
    except (UnicodeDecodeError, OSError, csv.Error):
        return  # the pandas pass reports unreadable input with its own, better message


@dataclass
class _Scan:
    dtypes: dict[str, str]
    rows: int
    format_findings: list


def _scan(recipe: Recipe, in_path: Path, chunksize: int, kwargs: dict, *, formats: bool) -> _Scan:
    """Pass 1: one bounded-memory read to learn what a whole-frame read would infer.

    Streaming used to trust the dtypes recorded in the recipe fingerprint; a later chunk
    that did not fit crashed with a raw pandas error, and a blank beyond the drift head
    made the output differ from ``apply_recipe`` (``2`` vs ``2.0``). Scanning first
    fixes both: dtypes are the ones the whole file infers, and drift covers every row.
    """
    from .drift import DriftReport, _detect_format_drift

    seen: dict[str, set[str]] = {}
    rows = 0
    fmt = DriftReport()
    _check_row_widths(in_path, kwargs)
    scan_kwargs = {k: v for k, v in kwargs.items() if k != "dtype"}
    if kwargs.get("dtype") is str:  # text=True reads everything as text
        scan_kwargs["dtype"] = str
    try:
        reader = pd.read_csv(in_path, chunksize=chunksize, **scan_kwargs)
    except Exception as exc:  # noqa: BLE001
        raise CleanFrameError(f"Could not open {in_path.name} for streaming: {exc}.") from exc
    for chunk in _iter_chunks(reader, in_path, chunksize):
        rows += len(chunk)
        for col in chunk.columns:
            seen.setdefault(str(col), set()).add(str(chunk[col].dtype))
        if formats:
            _detect_format_drift(chunk, recipe, fmt)
    dtypes = {col: _merge_dtype(col, names) for col, names in seen.items()}
    return _Scan(dtypes=dtypes, rows=rows, format_findings=fmt.findings)


def _whole_file_drift(head_report: Any, scan: _Scan, recipe: Recipe, source: str) -> Any:
    """Head-sample drift, with dtype / row-count / format findings widened to every row."""
    from ._util import canonicalize_dtype
    from .drift import DriftFinding, DriftReport
    from .llm import _sketch
    from .types import Severity

    widened = {"dtype_change", "format_drift", "row_count_change"}
    report = DriftReport(
        findings=[f for f in head_report.findings if f.kind not in widened], source=source
    )
    fp = recipe.source_fingerprint or {}
    expected = {str(k): str(v) for k, v in (fp.get("dtypes") or {}).items()}
    for col, now in scan.dtypes.items():
        was = expected.get(col)
        if was is None or canonicalize_dtype(was) == canonicalize_dtype(now):
            continue
        report.findings.append(
            DriftFinding(
                kind="dtype_change",
                message=f'Column "{col}" changed dtype ({was} → {now})',
                severity=Severity.WARNING,
                column=col,
                suggestion="Re-plan or update casts if ops assume the old dtype.",
                evidence={"was": was, "now": now},
            )
        )
    by_col: dict[str, list[DriftFinding]] = {}
    for f in scan.format_findings:
        by_col.setdefault(str(f.column), []).append(f)
    for col, group in by_col.items():
        examples = [e for f in group for e in f.evidence.get("examples", [])][:3]
        sketches = sorted({_sketch(e) for e in examples})
        count = sum(int(f.evidence.get("count", 0)) for f in group)
        report.findings.append(
            DriftFinding(
                kind="format_drift",
                message=(
                    f'{count} value(s) in "{col}" match no allowed date format '
                    f"(new: {examples[0]!r})"
                ),
                severity=Severity.WARNING,
                column=col,
                suggestion=f"New pattern {sketches[0]!r}; add its format to the recipe.",
                evidence={"count": count, "examples": examples, "sketches": sketches},
            )
        )
    expected_rows = fp.get("row_count")
    try:
        if expected_rows is not None and int(expected_rows) != scan.rows:
            report.findings.append(
                DriftFinding(
                    kind="row_count_change",
                    message=f"Row count changed ({expected_rows} → {scan.rows})",
                    severity=Severity.INFO,
                    evidence={"was": int(expected_rows), "now": scan.rows},
                )
            )
    except (TypeError, ValueError):
        pass
    return report


def _stream_read_kwargs(recipe: Recipe) -> dict:
    """Build pandas ``read_csv`` kwargs from ``recipe.read``, matching ``apply_recipe``."""
    from .dataio import _pandas_skiprows, _text_kwargs

    read = dict(recipe.read or {})
    if read.get("sheet") is not None:
        raise CleanFrameError(
            "Streaming does not support Excel sheet selection (recipe.read.sheet). "
            "Use apply_recipe() / cleanframe apply without --chunksize for workbooks."
        )
    kwargs: dict = {
        "encoding": read.get("encoding", "utf-8-sig"),
        "index_col": False,
    }
    if read.get("sep"):
        kwargs["sep"] = read["sep"]
    if read.get("columns") is not None:
        kwargs["usecols"] = list(read["columns"])
    skiprows = _pandas_skiprows(read.get("skiprows"), int(read.get("blank_lines") or 0))
    if skiprows is not None:
        kwargs["skiprows"] = skiprows
    if read.get("nrows") is not None:
        kwargs["nrows"] = read["nrows"]
    if read.get("text"):
        kwargs.update(_text_kwargs())
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
            where = f" → {self.quarantine_path}" if self.quarantine_path else " (not saved)"
            base += f" {self.rows_quarantined:,} quarantined{where}."
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
    overwrite: bool = False,
) -> StreamSummary:
    """Replay ``recipe`` over ``in_path`` (CSV) in chunks, writing cleaned CSV to
    ``out_path`` without ever holding the whole file in memory.

    Refuses (raises) if the recipe contains any non-row-independent op/validation, so
    the streamed output is byte-identical to a whole-frame replay. The file is read
    twice: a first bounded-memory pass infers the column types a whole-frame read would
    (so ``2`` renders as ``2.0`` when a blank appears later) and checks drift over every
    row; the second pass cleans and writes. Quarantined rows are written only when
    ``quarantine_path`` is given (otherwise counted and warned about, like ``apply``).

    ``recipe.read`` selection (``columns`` / ``sep`` / ``encoding`` / ``skiprows`` /
    ``nrows``) is applied the same way as :func:`apply_recipe`. Output is written to a
    temporary sibling file and atomically replaced onto ``out_path`` when the stream
    completes successfully.

    Like :func:`apply_recipe`, schema drift is checked first (over every row) and, by default, refuses (``on_drift="error"``) so a drifted file is never silently
    streamed with a stale recipe.
    """
    if not isinstance(recipe, Recipe):
        from .api import _resolve_recipe

        recipe = _resolve_recipe(recipe)
    check_streamable(recipe)

    if isinstance(chunksize, bool) or not isinstance(chunksize, int) or chunksize < 1:
        raise CleanFrameError(
            f"chunksize must be a positive number of rows, got {chunksize!r}."
        )
    in_path = Path(in_path)
    if not in_path.exists():
        raise CleanFrameError(f"Input file not found: {in_path}")
    if not in_path.is_file():
        raise CleanFrameError(f"Input path is not a file (is it a directory?): {in_path}")
    out_path = check_output_target(out_path, in_path, overwrite=overwrite)
    suffix = in_path.suffix.lower()
    if suffix not in _CSV_STREAM_SUFFIXES:
        raise CleanFrameError(
            f"Streaming (--chunksize) only supports CSV-family files, not {suffix or 'this file'}. "
            "Use apply_recipe() for Excel/Parquet/JSON."
        )
    mode = Mode.coerce(mode)
    if on_drift not in ("error", "warn", "ignore"):
        raise CleanFrameError(
            f"on_drift must be one of ['error', 'warn', 'ignore'], got {on_drift!r}."
        )
    read_kwargs = _stream_read_kwargs(recipe)
    has_fp = bool(recipe.source_fingerprint)
    if check_drift and not has_fp:
        warnings.warn(
            "CleanFrame: recipe has no source_fingerprint — drift check skipped. "
            "Re-plan or stamp a fingerprint for production replay.",
            CleanFrameWarning,
            stacklevel=2,
        )
    do_drift = check_drift and has_fp

    # Pass 1: learn the whole-file dtypes and (if checking) drift over EVERY row, before
    # any output exists — so a drifted file leaves nothing behind.
    scan = _scan(recipe, in_path, chunksize, read_kwargs, formats=do_drift)
    if not read_kwargs.get("dtype"):
        read_kwargs["dtype"] = {col: _pin_for(m) for col, m in scan.dtypes.items()}

    if do_drift:
        from .drift import detect_drift
        from .errors import DriftError
        from .fingerprint import DEFAULT_SAMPLE_ROWS

        head_kwargs = {k: v for k, v in read_kwargs.items() if k != "dtype"}
        head_kwargs["nrows"] = min(DEFAULT_SAMPLE_ROWS, int(read_kwargs.get("nrows") or 10**12))
        try:
            head = pd.read_csv(in_path, **head_kwargs)
        except Exception as exc:  # noqa: BLE001 - IO boundary
            raise CleanFrameError(
                f"Could not read {in_path.name} to check for drift: {exc}."
            ) from exc
        drift = _whole_file_drift(
            detect_drift(head, recipe, source=str(in_path)), scan, recipe, str(in_path)
        )
        if drift.has_drift and (on_drift == "error" or mode is Mode.STRICT):
            raise DriftError(drift.render(), report=drift)
        if drift.has_drift and on_drift == "warn":
            warnings.warn("CleanFrame: " + drift.render(), CleanFrameWarning, stacklevel=2)

    logger.info("streaming %s -> %s in chunks of %d row(s)", in_path, out_path, chunksize)
    summary = StreamSummary(out_path=Path(out_path))
    q_path = (
        check_output_target(quarantine_path, in_path, overwrite=overwrite)
        if quarantine_path
        else None
    )
    q_tmp = Path(str(q_path) + ".cf-tmp") if q_path is not None else None
    q_written = False
    first = True
    tmp_out = Path(str(out_path) + ".cf-tmp")

    try:
        reader = pd.read_csv(in_path, chunksize=chunksize, **read_kwargs)
    except Exception as exc:  # noqa: BLE001
        raise CleanFrameError(f"Could not open {in_path.name} for streaming: {exc}.") from exc

    try:
        for chunk in _iter_chunks(reader, in_path, chunksize):
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
            logger.debug("chunk %d done (%d row(s) in so far)", summary.chunks, summary.rows_in)
            if result.has_quarantine:
                summary.rows_quarantined += len(result.quarantine)
            if result.has_quarantine and q_tmp is not None:
                q = sanitize_dataframe_for_csv(result.quarantine)
                q.to_csv(
                    q_tmp, mode="w" if not q_written else "a", header=not q_written,
                    index=False, encoding="utf-8", lineterminator="\n",
                )
                q_written = True
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
    if summary.rows_quarantined and q_path is None:
        warnings.warn(
            f"CleanFrame: {summary.rows_quarantined:,} row(s) quarantined by validation "
            "and not saved — pass quarantine_path (CLI --quarantine FILE) to keep them.",
            CleanFrameWarning,
            stacklevel=2,
        )
    return summary


__all__ = [
    "stream_apply",
    "check_streamable",
    "is_op_streamable",
    "is_validation_streamable",
    "StreamSummary",
]
