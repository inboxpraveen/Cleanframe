"""Reading and writing dataframes by file extension.

A thin, predictable wrapper so the CLI and API accept a path anywhere a dataframe
is expected. CSV/TSV/Excel/Parquet/JSON are dispatched by suffix. Reading uses
pandas' default NA handling (blank/``NA``/``null`` → NaN); CleanFrame's detectors
then catch the *disguised* nulls pandas leaves behind (``unknown``, ``-``, ``?``).

Cross-platform defaults:

* Paths go through :class:`pathlib.Path` (Windows / POSIX alike).
* CSV/TSV reads use ``utf-8-sig`` so Excel/Notepad BOMs on Windows don't break.
* CSV/TSV writes use UTF-8 + ``\\n`` line endings (not OS-dependent ``\\r\\n``).
* Parent directories are created automatically on write.
* Formula-like cells are sanitised on CSV/TSV export by default.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from ._util import ensure_parent, sanitize_dataframe_for_csv
from .errors import CleanFrameError

#: Encoding for CSV/TSV. ``utf-8-sig`` accepts a BOM and writes plain UTF-8 when
#: pandas strips the sig on read; we still pass ``encoding="utf-8"`` on write.
_CSV_READ_ENCODING = "utf-8-sig"
_CSV_WRITE_ENCODING = "utf-8"


def read_frame(path: str | Path, **kwargs) -> pd.DataFrame:
    """Read a dataframe, dispatching on file extension."""
    path = Path(path)
    if not path.exists():
        raise CleanFrameError(f"Input file not found: {path}")
    suffix = path.suffix.lower()
    try:
        if suffix in (".csv", ".txt"):
            kwargs.setdefault("encoding", _CSV_READ_ENCODING)
            return pd.read_csv(path, **kwargs)
        if suffix == ".tsv":
            kwargs.setdefault("encoding", _CSV_READ_ENCODING)
            return pd.read_csv(path, sep="\t", **kwargs)
        if suffix in (".xlsx", ".xls", ".xlsm"):
            return pd.read_excel(path, **kwargs)
        if suffix == ".parquet":
            return pd.read_parquet(path, **kwargs)
        if suffix == ".json":
            kwargs.setdefault("encoding", "utf-8")
            return pd.read_json(path, **kwargs)
        kwargs.setdefault("encoding", _CSV_READ_ENCODING)
        return pd.read_csv(path, **kwargs)
    except ImportError as exc:  # pragma: no cover - optional engine missing
        hint = "Try `pip install cleanframe[excel]` for Excel support."
        if suffix == ".parquet":
            hint = "Try `pip install cleanframe[parquet]` (pyarrow) for Parquet support."
        raise CleanFrameError(f"Reading {suffix} requires an extra engine: {exc}. {hint}") from exc


def write_frame(
    df: pd.DataFrame,
    path: str | Path,
    *,
    sanitize_csv: bool = True,
    **kwargs,
) -> Path:
    """Write a dataframe, dispatching on file extension. Never writes the index.

    Parameters
    ----------
    sanitize_csv:
        When ``True`` (default), string cells that look like spreadsheet formulas
        (leading ``=``, ``+``, ``-``, ``@``, …) are escaped before CSV/TSV export.
        Set ``False`` only when you intentionally need raw formula cells.
    """
    path = ensure_parent(path)
    suffix = path.suffix.lower()

    if suffix in (".csv", ".txt"):
        out = sanitize_dataframe_for_csv(df) if sanitize_csv else df
        kwargs.setdefault("encoding", _CSV_WRITE_ENCODING)
        kwargs.setdefault("lineterminator", "\n")
        out.to_csv(path, index=False, **kwargs)
    elif suffix == ".tsv":
        out = sanitize_dataframe_for_csv(df) if sanitize_csv else df
        kwargs.setdefault("encoding", _CSV_WRITE_ENCODING)
        kwargs.setdefault("lineterminator", "\n")
        out.to_csv(path, sep="\t", index=False, **kwargs)
    elif suffix in (".xlsx", ".xls", ".xlsm"):
        try:
            df.to_excel(path, index=False, **kwargs)
        except ImportError as exc:  # pragma: no cover
            raise CleanFrameError(
                f"Writing {suffix} requires openpyxl. Try `pip install cleanframe[excel]`."
            ) from exc
    elif suffix == ".parquet":
        try:
            df.to_parquet(path, index=False, **kwargs)
        except ImportError as exc:  # pragma: no cover
            raise CleanFrameError(
                "Writing .parquet requires pyarrow. Try `pip install cleanframe[parquet]`."
            ) from exc
    elif suffix == ".json":
        kwargs.setdefault("force_ascii", False)
        df.to_json(path, orient="records", indent=2, **kwargs)
    else:
        out = sanitize_dataframe_for_csv(df) if sanitize_csv else df
        kwargs.setdefault("encoding", _CSV_WRITE_ENCODING)
        kwargs.setdefault("lineterminator", "\n")
        out.to_csv(path, index=False, **kwargs)
    return path


__all__ = ["read_frame", "write_frame"]
