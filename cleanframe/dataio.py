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


_EXCEL_SUFFIXES = (".xlsx", ".xls", ".xlsm")


def excel_sheet_names(path: str | Path) -> list[str]:
    """Return a workbook's sheet names in file order (raises CleanFrameError on error)."""
    path = Path(path)
    try:
        return list(pd.ExcelFile(path).sheet_names)
    except ImportError as exc:  # pragma: no cover - optional engine missing
        raise CleanFrameError(
            f"Reading Excel requires openpyxl: {exc}. Try `pip install cleanframe[excel]`."
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise CleanFrameError(f"Could not open workbook {path.name}: {exc}.") from exc


def _apply_row_slice(df: pd.DataFrame, nrows: int | None, skiprows) -> pd.DataFrame:
    """Post-read row selection for formats without native nrows/skiprows (parquet/json)."""
    if skiprows is not None:
        if isinstance(skiprows, int):
            df = df.iloc[skiprows:]
        else:
            keep = [i for i in range(len(df)) if i not in set(skiprows)]
            df = df.iloc[keep]
    if nrows is not None:
        df = df.head(nrows)
    return df


def read_frame(
    path: str | Path,
    *,
    sheet: str | int | None = None,
    columns: list[str] | None = None,
    nrows: int | None = None,
    skiprows: int | list[int] | None = None,
    **kwargs,
) -> pd.DataFrame:
    """Read a single dataframe, dispatching on file extension.

    Parameters
    ----------
    sheet:
        Excel only — a sheet name or 0-based index. If a workbook has more than one
        sheet and none is chosen, a :class:`~cleanframe.errors.CleanFrameError` is
        raised (never silently pick the first). Use :func:`cleanframe.clean_workbook`
        to clean every sheet.
    columns / nrows / skiprows:
        Select a subset of columns (``usecols``) and/or a row range. ``columns`` is a
        *filter*, not a reorder — output keeps file order. Under ``skiprows``/``nrows``
        the diff's ``row_id`` is relative to the loaded slice, not the physical file line.

    Any failure pandas/pyarrow would surface as a raw traceback is re-raised as a
    :class:`~cleanframe.errors.CleanFrameError` with an actionable hint.
    """
    path = Path(path)
    if not path.exists():
        raise CleanFrameError(f"Input file not found: {path}")
    if not path.is_file():
        raise CleanFrameError(f"Input path is not a file (is it a directory?): {path}")
    if path.stat().st_size == 0:
        raise CleanFrameError(f"Input file is empty (no columns to parse): {path}")
    suffix = path.suffix.lower()
    is_excel = suffix in _EXCEL_SUFFIXES
    if sheet is not None and not is_excel:
        raise CleanFrameError(f"sheet= is only valid for Excel files, not {suffix or 'this file'}.")
    try:
        if is_excel:
            if sheet is None:
                names = excel_sheet_names(path)
                if len(names) > 1:
                    raise CleanFrameError(
                        f"{path.name} has {len(names)} sheets ({names}). Pass sheet='NAME' "
                        "(or a 0-based index) to pick one, or use cleanframe.clean_workbook() "
                        "/ the `cleanframe clean` CLI, which cleans every sheet."
                    )
                sheet = 0
            xl_kwargs = dict(kwargs)
            if columns is not None:
                xl_kwargs["usecols"] = list(columns)
            if nrows is not None:
                xl_kwargs["nrows"] = nrows
            if skiprows is not None:
                xl_kwargs["skiprows"] = skiprows
            df = pd.read_excel(path, sheet_name=sheet, **xl_kwargs)
            if isinstance(df, dict):
                raise CleanFrameError("sheet must select a single sheet (a name or index).")
            return df
        if suffix == ".parquet":
            df = pd.read_parquet(path, columns=list(columns) if columns else None, **kwargs)
            return _apply_row_slice(df, nrows, skiprows)
        if suffix == ".json":
            kwargs.setdefault("encoding", "utf-8")
            df = pd.read_json(path, **kwargs)
            if columns is not None:
                df = df[[c for c in columns if c in df.columns]]
            return _apply_row_slice(df, nrows, skiprows)
        # CSV family (.csv/.txt/.tsv and unknown extensions).
        kwargs.setdefault("encoding", _CSV_READ_ENCODING)
        if suffix == ".tsv":
            kwargs.setdefault("sep", "\t")
        if columns is not None:
            kwargs["usecols"] = list(columns)
        if nrows is not None:
            kwargs["nrows"] = nrows
        if skiprows is not None:
            kwargs["skiprows"] = skiprows
        return pd.read_csv(path, **kwargs)
    except ImportError as exc:  # pragma: no cover - optional engine missing
        hint = "Try `pip install cleanframe[excel]` for Excel support."
        if suffix == ".parquet":
            hint = "Try `pip install cleanframe[parquet]` (pyarrow) for Parquet support."
        raise CleanFrameError(f"Reading {suffix} requires an extra engine: {exc}. {hint}") from exc
    except CleanFrameError:
        raise
    except UnicodeDecodeError as exc:
        raise CleanFrameError(
            f"Could not decode {path.name} as UTF-8 ({exc}). It may be saved as "
            "Latin-1 / Windows-1252 / UTF-16 (common for Excel 'Save as CSV' on "
            "Windows) — pass encoding='cp1252' (or the correct codec) to read_frame."
        ) from exc
    except Exception as exc:  # noqa: BLE001 - IO boundary: surface any parse failure cleanly
        raise CleanFrameError(
            f"Could not read {path.name}: {type(exc).__name__}: {exc}. "
            "Check the delimiter/quoting, that the file matches its extension, and "
            "that it is not truncated."
        ) from exc


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


__all__ = ["read_frame", "write_frame", "excel_sheet_names"]
