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

import csv as _csv
import os
import re as _re
import warnings
from pathlib import Path
from typing import Any

import pandas as pd

from ._util import (
    check_output_target,
    looks_like_code_values,
    sanitize_dataframe_for_csv,
    sanitize_dataframe_for_spreadsheet,
)
from .errors import CleanFrameError, CleanFrameWarning, OutputError

#: Encoding for CSV/TSV. ``utf-8-sig`` accepts a BOM and writes plain UTF-8 when
#: pandas strips the sig on read; we still pass ``encoding="utf-8"`` on write.
_CSV_READ_ENCODING = "utf-8-sig"
_CSV_WRITE_ENCODING = "utf-8"


_EXCEL_SUFFIXES = (".xlsx", ".xls", ".xlsm")
#: Read as delimited text. Anything else is refused rather than parsed as CSV.
_CSV_SUFFIXES = (".csv", ".txt", ".tsv", ".dat", "")
_TYPED_SUFFIXES = (".parquet", ".json")


def _detail(exc: BaseException) -> str:
    return str(exc).strip().rstrip(".")


def _text_kwargs() -> dict[str, Any]:
    """Read every field verbatim: no numeric coercion, no invented nulls.

    Only an empty field becomes NaN, so leading zeros, ``NA``/``None`` tokens and
    ``1e5`` survive read-time inference and stay visible in the diff.
    """
    return {"dtype": str, "keep_default_na": False, "na_values": [""]}


def _pandas_skiprows(skiprows: int | list[int] | None, blank_lines: int):
    """Translate public ``skiprows`` into pandas line indices.

    Public semantics are data rows, never file lines: ``skiprows=2`` drops the first
    two records and keeps the header (a bare pandas ``skiprows=2`` would eat it), and
    a list holds 1-based data-row numbers. ``blank_lines`` are the empty lines the
    format corrector found above the header.
    """
    lines = list(range(blank_lines))
    if skiprows is None:
        return lines or None
    if isinstance(skiprows, bool):
        raise CleanFrameError("skiprows must be a row count or a list of row numbers.")
    if isinstance(skiprows, int):
        if skiprows < 0:
            raise CleanFrameError(f"skiprows must be 0 or more, got {skiprows}.")
        lines += [blank_lines + i for i in range(1, skiprows + 1)]
    elif isinstance(skiprows, (list, tuple)):
        bad = [r for r in skiprows if isinstance(r, bool) or not isinstance(r, int) or r < 1]
        if bad:
            raise CleanFrameError(
                f"skiprows entries must be data-row numbers of 1 or more, got {bad}."
            )
        lines += [blank_lines + int(r) for r in skiprows]
    else:
        raise CleanFrameError(
            f"skiprows must be a row count or a list of row numbers, got "
            f"{type(skiprows).__name__}."
        )
    return lines or None


def _header_hint(path: Path, encoding: str, sep: str | None, blank_lines: int) -> str:
    """`` Try header_row=N`` when title rows plausibly sit above the header, else ``""``."""
    from .readfix import suggest_header_row

    try:
        with open(path, encoding=encoding, newline="") as fh:
            lines = [fh.readline().rstrip("\r\n") for _ in range(60)]
    except (OSError, UnicodeDecodeError, LookupError):
        return ""
    at = suggest_header_row(lines, sep or ",")
    if at is None or at <= blank_lines:
        return ""
    return (
        f" The real header may be on line {at + 1}: pass header_row={at} "
        f"(CLI --header-row {at}) to skip the {at} line(s) above it."
    )


_DELIMITER_NAMES = {";": "semicolon", "\t": "tab", "|": "pipe", ",": "comma"}


def _delimiter_hint(path: Path, encoding: str, sep: str | None, blank_lines: int) -> str:
    """A hint when the file is consistently split by a delimiter other than ``sep``."""
    try:
        with open(path, encoding=encoding, newline="") as fh:
            for _ in range(blank_lines):
                fh.readline()
            lines = [ln for ln in (fh.readline() for _ in range(6)) if ln.strip()]
    except (OSError, UnicodeDecodeError, LookupError):
        return ""
    current = sep or ","
    for delim in (";", "\t", "|", ","):
        if delim == current:
            continue
        counts = [ln.count(delim) for ln in lines]
        if counts and counts[0] >= 1 and len(set(counts)) == 1:
            return (
                f" The file looks {_DELIMITER_NAMES[delim]}-delimited: pass sep={delim!r} "
                f"(CLI --sep) - clean() and report() detect the delimiter for you."
            )
    return ""


def _check_csv_header(path: Path, encoding: str, sep: str | None, blank_lines: int) -> None:
    """Refuse duplicate or blank CSV headers instead of letting pandas rename them.

    pandas turns a repeated ``name`` into ``name.1`` and a blank one into
    ``Unnamed: 3``, so the recipe would key lineage off a label the file never had.
    A title row above the real header is the usual cause, so the error suggests
    ``header_row=`` when it can find the header.
    """
    try:
        with open(path, encoding=encoding, newline="") as fh:
            for _ in range(blank_lines):
                fh.readline()
            line = fh.readline()
    except (OSError, UnicodeDecodeError, LookupError):
        return
    if not line.strip():
        return
    fields = next(_csv.reader([line.rstrip("\r\n")], delimiter=sep or ","), None)
    if not fields or len(fields) < 2:
        hint = _header_hint(path, encoding, sep, blank_lines)
        if hint:
            # "The header is on line 2" would be the wrong advice for a file that is simply
            # split by another delimiter: say that first.
            delimiter = _delimiter_hint(path, encoding, sep, blank_lines)
            if delimiter:
                raise CleanFrameError(
                    f"{path.name} reads as a single column with sep={sep or ','!r}." + delimiter
                )
            raise CleanFrameError(
                f"The first row of {path.name} has a single field, but the rows below are "
                f"wider - it looks like a title, not a header.{hint}"
            )
        return
    names = [f.strip() for f in fields]
    if any(not n for n in names):
        raise CleanFrameError(
            f"{path.name} has an empty column name in its header row "
            f"(position {names.index('') + 1} of {len(names)}). Name every column, or "
            "select the ones you need with columns=."
            + _header_hint(path, encoding, sep, blank_lines)
        )
    seen: dict[str, int] = {}
    for n in names:
        seen[n] = seen.get(n, 0) + 1
    dups = sorted(n for n, c in seen.items() if c > 1)
    if dups:
        raise CleanFrameError(
            f"{path.name} has duplicate column name(s) {dups} in its header row. "
            "CleanFrame needs unique names - rename them in the file, or select a "
            "subset with columns=." + _header_hint(path, encoding, sep, blank_lines)
        )


#: Text-file scans run over at most this many records (bounded, O(1) memory).
_SCAN_ROWS = 100_000
_NUL_CHUNK = 1 << 20
_FOOTER_RE = _re.compile(r"(?:grand\s+total|sub[\s-]?total|total)s?\s*:?", _re.IGNORECASE)
#: Workbooks above this size skip the (advisory) formula/merged-cell scan.
_STRUCTURE_SCAN_BYTES = 25 * 1024 * 1024


def _refuse_nul_bytes(path: Path, encoding: str) -> None:
    """pandas' C parser silently truncates a field at a NUL byte (``a<NUL>b`` -> ``a``).

    ``clean`` has always refused such a file; ``read_frame`` now does too, instead of
    handing back quietly shortened values. UTF-16/32 text legitimately contains NULs.
    """
    if encoding.lower().replace("_", "-").startswith(("utf-16", "utf-32", "utf16", "utf32")):
        return
    try:
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(_NUL_CHUNK)
                if not chunk:
                    return
                if b"\x00" in chunk:
                    break
    except OSError:
        return
    raise CleanFrameError(
        f"{path.name} contains NUL bytes, which the CSV parser would silently use to "
        "truncate values. It is either binary or UTF-16/32 text: pass an explicit "
        "encoding= (e.g. 'utf-16') if it is text, or clean the NULs out of the file."
    )


def _warn_csv_shape(path: Path, encoding: str, sep: str | None, blank_lines: int) -> None:
    """Warn - never fix - about short (NaN-padded) rows and a trailing totals row."""
    header_len = 0
    header_no = 0
    short: list[int] = []
    n_short = 0
    last: list[str] | None = None
    reached_eof = True
    try:
        with open(path, encoding=encoding, newline="") as fh:
            reader = _csv.reader(fh, delimiter=sep or ",")
            seen_header = False
            skipped = 0
            for rec_no, row in enumerate(reader, start=1):
                if not seen_header:
                    if skipped < blank_lines:  # physical lines above the header
                        skipped += 1
                        continue
                    header_len, seen_header, header_no = len(row), True, rec_no
                    continue
                if rec_no - header_no > _SCAN_ROWS:
                    reached_eof = False
                    break
                if not row:
                    continue
                last = row
                if len(row) < header_len:
                    n_short += 1
                    if len(short) < 3:
                        short.append(rec_no)
    except (OSError, UnicodeDecodeError, LookupError, _csv.Error):
        return
    if n_short:
        rows = ", ".join(str(n) for n in short) + (", ..." if n_short > len(short) else "")
        warnings.warn(
            f"CleanFrame: {n_short} row(s) in {path.name} have fewer fields than the "
            f"{header_len}-column header (record {rows}); the missing cells were read as "
            "empty. Check for truncated or unquoted rows.",
            CleanFrameWarning,
            stacklevel=4,
        )
    if reached_eof and last and _FOOTER_RE.fullmatch(last[0].strip()):
        warnings.warn(
            f"CleanFrame: the last row of {path.name} starts with {last[0].strip()!r} - it "
            "looks like a footer/total row, which would be cleaned as data. Drop it with "
            "nrows= or remove it from the file if it is not a real record.",
            CleanFrameWarning,
            stacklevel=4,
        )


def _warn_excel_structure(path: Path, sheet_names: list[str]) -> None:
    """Warn about cells pandas cannot see: uncached formulas and merged ranges.

    pandas reads cached formula results, so a formula that was never calculated (a
    file written by a script, never opened in Excel) reads as empty; a merged range
    keeps its value only in the top-left cell. Both look like ordinary missing data.
    Advisory only: any failure here is swallowed.
    """
    if path.suffix.lower() not in (".xlsx", ".xlsm"):
        return
    try:
        if path.stat().st_size > _STRUCTURE_SCAN_BYTES:
            return
        import openpyxl

        formulas = openpyxl.load_workbook(path, data_only=False)
        cached = openpyxl.load_workbook(path, data_only=True)
    except Exception:  # noqa: BLE001 - advisory
        return
    notes: list[str] = []
    try:
        for name in sheet_names:
            if name not in formulas.sheetnames:
                continue
            ws, wc = formulas[name], cached[name]
            merged = len(ws.merged_cells.ranges)
            uncached = 0
            for row in ws.iter_rows():
                for cell in row:
                    if cell.data_type == "f" and wc[cell.coordinate].value is None:
                        uncached += 1
            if uncached:
                notes.append(
                    f"sheet {name!r}: {uncached} formula cell(s) have no cached value "
                    "(the workbook was never calculated) and read as empty"
                )
            if merged:
                notes.append(
                    f"sheet {name!r}: {merged} merged range(s) - only the top-left cell "
                    "of each keeps its value, the rest read as empty"
                )
    except Exception:  # noqa: BLE001 - advisory
        return
    finally:
        formulas.close()
        cached.close()
    if notes:
        warnings.warn(
            f"CleanFrame: {path.name} has cells pandas cannot read as data - "
            + "; ".join(notes)
            + ".",
            CleanFrameWarning,
            stacklevel=4,
        )


def _warn_excel_title_rows(path: Path, sheet, df: pd.DataFrame) -> None:
    """Warn when an Excel sheet's 'header' is a title row (mostly ``Unnamed:`` columns)."""
    cols = [str(c) for c in df.columns]
    if len(cols) < 2 or sum(c.startswith("Unnamed:") for c in cols) * 2 < len(cols):
        return
    try:
        from .readfix import suggest_header_row_from_counts

        raw = pd.read_excel(path, sheet_name=sheet, header=None, nrows=40)
        at = suggest_header_row_from_counts([int(n) for n in raw.notna().sum(axis=1)])
    except Exception:  # noqa: BLE001 - advisory
        return
    if at:
        warnings.warn(
            f"CleanFrame: most column names in {path.name} are blank ('Unnamed: N') - a "
            f"title row is probably above the real header. Try header_row={at} "
            f"(CLI --header-row {at}).",
            CleanFrameWarning,
            stacklevel=4,
        )


def _refuse_empty_slice(path: Path, df: pd.DataFrame, nrows, skiprows) -> None:
    """``skiprows`` past the end of the data used to succeed with an empty frame."""
    if len(df) or nrows == 0 or skiprows is None or isinstance(skiprows, bool):
        return
    skipped = skiprows if isinstance(skiprows, int) else len(skiprows)
    if skipped > 0:
        raise CleanFrameError(
            f"skiprows={skiprows!r} leaves no data rows in {path.name} - it skips past the "
            "end of the data. Lower it, or check that the right file is being read."
        )


def excel_sheet_names(path: str | Path) -> list[str]:
    """Return a workbook's sheet names in file order (raises CleanFrameError on error)."""
    path = Path(path)
    try:
        with pd.ExcelFile(path) as workbook:
            return list(workbook.sheet_names)
    except ImportError as exc:  # pragma: no cover - optional engine missing
        raise CleanFrameError(
            f"Reading Excel requires openpyxl ({_detail(exc)}). "
            "Try `pip install cleanframe-engine[excel]`."
        ) from exc
    except CleanFrameError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise CleanFrameError(
            f"Could not open workbook {path.name}: {_detail(exc)}."
        ) from exc


def _apply_row_slice(df: pd.DataFrame, nrows: int | None, skiprows) -> pd.DataFrame:
    """Row selection for formats with no header line (parquet/json).

    Uses the same public semantics as the CSV path: an int drops that many leading
    data rows, a list names 1-based data rows.
    """
    if skiprows is not None:
        if isinstance(skiprows, int) and not isinstance(skiprows, bool):
            if skiprows < 0:
                raise CleanFrameError(f"skiprows must be 0 or more, got {skiprows}.")
            df = df.iloc[skiprows:]
        elif isinstance(skiprows, (list, tuple)):
            drop = {int(r) - 1 for r in skiprows}
            df = df.iloc[[i for i in range(len(df)) if i not in drop]]
        else:
            raise CleanFrameError(
                "skiprows must be a row count or a list of row numbers, got "
                f"{type(skiprows).__name__}."
            )
    if nrows is not None:
        if not isinstance(nrows, int) or isinstance(nrows, bool) or nrows < 0:
            raise CleanFrameError(f"nrows must be 0 or more, got {nrows!r}.")
        df = df.head(nrows)
    return df


_LEADING_ZERO_RE = _re.compile(r"^[+-]?0\d")
_SCI_RE = _re.compile(r"^[+-]?\d+(?:\.\d+)?[eE][+-]?\d+$")
_BOOL_TEXT = frozenset({"true", "false", "yes", "no"})
#: Spellings that mean "missing" in most datasets. Reading them as NaN is what the
#: file asked for, so it is not worth a warning — unless the column holds short
#: codes, where ``NA`` is Namibia. ``None``/``nil`` stay reportable everywhere.
_PLAIN_NULL_TEXT = frozenset(
    {"n/a", "na", "null", "nan", "-nan", "<na>", "#n/a", "#na", "#n/a n/a"}
)


def compare_for_losses(df: pd.DataFrame, raw: pd.DataFrame) -> dict[str, str]:
    """Columns where the coerced frame differs from the verbatim one, and how."""
    losses: dict[str, str] = {}
    rows = min(len(raw), len(df))
    for col in df.columns:
        if col not in raw.columns:
            continue
        coerced = df[col].to_numpy()
        literal = raw[col].to_numpy()
        code_column = looks_like_code_values(literal[:rows])
        for i in range(rows):
            text = literal[i]
            if not isinstance(text, str) or not text.strip():
                continue
            token = text.strip()
            value = coerced[i]
            try:
                missing = bool(pd.isna(value))
            except (TypeError, ValueError):  # pragma: no cover - exotic cell
                continue
            if missing:
                if token.casefold() in _PLAIN_NULL_TEXT and not code_column:
                    continue
                losses[str(col)] = f"{token!r} was read as a missing value"
            elif isinstance(value, bool) and token.casefold() in _BOOL_TEXT:
                losses[str(col)] = f"{token!r} was read as the boolean {value}"
            elif not isinstance(value, str) and _LEADING_ZERO_RE.match(token):
                losses[str(col)] = f"{token!r} lost its leading zero(s) and became {value}"
            elif not isinstance(value, str) and _SCI_RE.match(token):
                losses[str(col)] = f"{token!r} was rewritten as {value}"
            else:
                continue
            break
    return losses


def inference_losses(path: str | Path, df: pd.DataFrame, *, limit: int = 200, **read_kwargs):
    """Columns whose values pandas changed while reading ``path``, and how.

    Read-time coercion happens before CleanFrame sees the data, so no diff can show
    it: a leading-zero ZIP, a ``None`` token or a country code of ``NA`` is already
    gone. A bounded verbatim re-read makes the loss visible.
    """
    try:
        raw = read_frame(path, nrows=limit, text=True, **read_kwargs)
    except CleanFrameError:
        return {}
    return compare_for_losses(df, raw)


def read_frame(
    path: str | Path,
    *,
    sheet: str | int | None = None,
    columns: list[str] | None = None,
    nrows: int | None = None,
    skiprows: int | list[int] | None = None,
    header_row: int | None = None,
    blank_lines: int = 0,
    text: bool = False,
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
        *filter*, not a reorder — output keeps file order. ``skiprows`` counts *data
        rows*, never file lines: an int drops that many leading records and keeps the
        header; a list names 1-based data rows. Under ``skiprows``/``nrows`` the diff's
        ``row_id`` is relative to the loaded slice, not the physical file line.
    header_row:
        0-based physical line (CSV) or row (Excel) holding the column names, for a
        file with title rows above the header. Everything above it is skipped.
    text:
        Read every field as a string (CSV/Excel). Pandas otherwise infers types while
        reading, which drops leading zeros, turns ``NA``/``None`` text into NaN and
        rewrites ``1e5`` — losses no diff can show because they happen before CleanFrame
        sees the data.

    Any failure pandas/pyarrow would surface as a raw traceback is re-raised as a
    :class:`~cleanframe.errors.CleanFrameError` with an actionable hint.
    """
    df = _read_frame(
        path, sheet=sheet, columns=columns, nrows=nrows, skiprows=skiprows,
        header_row=header_row, blank_lines=blank_lines, text=text, **kwargs,
    )
    _refuse_empty_slice(Path(path), df, nrows, skiprows)
    return df


def _read_frame(
    path: str | Path,
    *,
    sheet: str | int | None = None,
    columns: list[str] | None = None,
    nrows: int | None = None,
    skiprows: int | list[int] | None = None,
    header_row: int | None = None,
    blank_lines: int = 0,
    text: bool = False,
    **kwargs,
) -> pd.DataFrame:
    path = Path(path)
    if not path.exists():
        raise CleanFrameError(f"Input file not found: {path}")
    if not path.is_file():
        raise CleanFrameError(f"Input path is not a file (is it a directory?): {path}")
    if path.stat().st_size == 0:
        raise CleanFrameError(f"Input file is empty (no columns to parse): {path}")
    suffix = path.suffix.lower()
    is_excel = suffix in _EXCEL_SUFFIXES
    if header_row is not None:
        if isinstance(header_row, bool) or not isinstance(header_row, int) or header_row < 0:
            raise CleanFrameError(f"header_row must be 0 or more, got {header_row!r}.")
        if suffix in _TYPED_SUFFIXES:
            raise CleanFrameError("header_row= applies to CSV and Excel files only.")
        blank_lines = header_row  # rows above the header are skipped, like blank lines
    if sheet is not None and not is_excel:
        raise CleanFrameError(f"sheet= is only valid for Excel files, not {suffix or 'this file'}.")
    if not is_excel and suffix not in _CSV_SUFFIXES and suffix not in _TYPED_SUFFIXES:
        raise CleanFrameError(
            f"Unsupported input format {suffix!r} for {path.name}. CleanFrame reads "
            ".csv/.txt/.tsv/.dat, .xlsx/.xlsm/.xls, .parquet and .json. Convert the file, "
            "or rename it to the extension matching its contents."
        )
    try:
        if is_excel:
            if sheet is not None:
                names = excel_sheet_names(path)
                if isinstance(sheet, str) and sheet not in names:
                    hint = (
                        " A bare number is read as a sheet *name*; use the CLI form "
                        f"--sheet '#{sheet}' (or sheet={sheet} in Python) for a positional index."
                        if sheet.isdigit()
                        else ""
                    )
                    raise CleanFrameError(
                        f"Sheet {sheet!r} not found in {path.name}. "
                        f"Available sheets: {names}.{hint}"
                    )
                if isinstance(sheet, int) and not -len(names) <= sheet < len(names):
                    raise CleanFrameError(
                        f"Sheet index {sheet} is out of range for {path.name}, which has "
                        f"{len(names)} sheet(s): {names}."
                    )
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
            xl_kwargs.pop("encoding", None)
            xl_kwargs.pop("sep", None)
            if text:
                for key, value in _text_kwargs().items():
                    xl_kwargs.setdefault(key, value)
            xl_skiprows = _pandas_skiprows(skiprows, blank_lines)
            if columns is not None:
                head_skip = _pandas_skiprows(None, blank_lines)
                head_kw = {"skiprows": head_skip} if head_skip is not None else {}
                available = list(
                    pd.read_excel(path, sheet_name=sheet, nrows=0, **head_kw).columns
                )
                missing = [c for c in columns if c not in available]
                if missing:
                    raise CleanFrameError(
                        f"Requested column(s) not found in {path.name}: {missing}. "
                        f"Available: {available}."
                    )
                xl_kwargs["usecols"] = list(columns)
            if nrows is not None:
                xl_kwargs["nrows"] = nrows
            if xl_skiprows is not None:
                xl_kwargs["skiprows"] = xl_skiprows
            df = pd.read_excel(path, sheet_name=sheet, **xl_kwargs)
            if isinstance(df, dict):
                raise CleanFrameError("sheet must select a single sheet (a name or index).")
            if header_row is None and nrows is None:
                _warn_excel_title_rows(path, sheet, df)
            if nrows is None:
                names = excel_sheet_names(path)
                _warn_excel_structure(path, [names[sheet] if isinstance(sheet, int) else sheet])
            return df
        if suffix == ".parquet":
            df = pd.read_parquet(path, columns=list(columns) if columns else None, **kwargs)
            return _apply_row_slice(df, nrows, skiprows)
        if suffix == ".json":
            kwargs.setdefault("encoding", "utf-8")
            df = pd.read_json(path, **kwargs)
            if columns is not None:
                missing = [c for c in columns if c not in df.columns]
                if missing:
                    raise CleanFrameError(
                        f"Requested column(s) not found in {path.name}: {missing}. "
                        f"Available: {list(df.columns)}."
                    )
                df = df[list(columns)]
            return _apply_row_slice(df, nrows, skiprows)
        # CSV family (.csv/.txt/.tsv/.dat and extension-less files).
        kwargs.setdefault("encoding", _CSV_READ_ENCODING)
        if suffix == ".tsv":
            kwargs.setdefault("sep", "\t")
        if text:
            for key, value in _text_kwargs().items():
                kwargs.setdefault(key, value)
        _refuse_nul_bytes(path, kwargs["encoding"])
        _check_csv_header(path, kwargs["encoding"], kwargs.get("sep"), blank_lines)
        # index_col=False: a row with one field too many (a stray trailing delimiter)
        # otherwise becomes the index and shifts every column one place left.
        kwargs.setdefault("index_col", False)
        csv_skiprows = _pandas_skiprows(skiprows, blank_lines)
        if csv_skiprows is not None:
            kwargs["skiprows"] = csv_skiprows
        if columns is not None:
            header_kwargs = {
                k: v for k, v in kwargs.items() if k in ("encoding", "sep", "skiprows", "index_col")
            }
            available = list(pd.read_csv(path, nrows=0, **header_kwargs).columns)
            missing = [c for c in columns if c not in available]
            if missing:
                raise CleanFrameError(
                    f"Requested column(s) not found in {path.name}: {missing}. "
                    f"Available: {available}."
                )
            kwargs["usecols"] = list(columns)
        if nrows is not None:
            if not isinstance(nrows, int) or isinstance(nrows, bool) or nrows < 0:
                raise CleanFrameError(f"nrows must be 0 or more, got {nrows!r}.")
            kwargs["nrows"] = nrows
        try:
            df = pd.read_csv(path, **kwargs)
        except pd.errors.ParserError as exc:
            hint = _header_hint(path, kwargs["encoding"], kwargs.get("sep"), blank_lines)
            if not hint:
                raise
            raise CleanFrameError(
                f"Could not parse {path.name}: {type(exc).__name__}: {_detail(exc)}.{hint}"
            ) from exc
        if nrows is None:
            _warn_csv_shape(path, kwargs["encoding"], kwargs.get("sep"), blank_lines)
        return df
    except ImportError as exc:  # pragma: no cover - optional engine missing
        hint = "Try `pip install cleanframe-engine[excel]` for Excel support."
        if suffix == ".parquet":
            hint = "Try `pip install cleanframe-engine[parquet]` (pyarrow) for Parquet support."
        elif suffix == ".xls":
            hint = "Legacy .xls needs xlrd: `pip install xlrd` (.xlsx needs only openpyxl)."
        raise CleanFrameError(
            f"Reading {suffix} requires an extra engine ({_detail(exc)}). {hint}"
        ) from exc
    except CleanFrameError:
        raise
    except UnicodeDecodeError as exc:
        raise CleanFrameError(
            f"Could not decode {path.name} as UTF-8 ({exc}). It may be saved as "
            "Latin-1 / Windows-1252 / UTF-16 (common for Excel 'Save as CSV' on "
            "Windows). Read it with cleanframe.read_frame(..., encoding='cp1252'), "
            "let `clean`/`report` detect the encoding for you, or record it in the "
            "recipe's read: section (encoding: cp1252) for replay."
        ) from exc
    except (pd.errors.ParserError, pd.errors.EmptyDataError) as exc:
        raise CleanFrameError(
            f"Could not parse {path.name}: {type(exc).__name__}: {_detail(exc)}. "
            "Check the delimiter/quoting, that the file matches its extension, and "
            "that it is not truncated."
        ) from exc
    except OSError as exc:
        raise CleanFrameError(f"Could not read {path}: {_detail(exc)}.") from exc
    except Exception as exc:  # noqa: BLE001 - IO boundary: surface any failure cleanly
        raise CleanFrameError(
            f"Could not read {path.name}: {type(exc).__name__}: {_detail(exc)}."
        ) from exc


def _write_dispatch(
    df: pd.DataFrame, target: Path, suffix: str, sanitize_csv: bool, kwargs: dict
) -> None:
    if suffix == ".parquet":
        df.to_parquet(target, index=False, **kwargs)
        return
    if suffix == ".json":
        kwargs.setdefault("force_ascii", False)
        df.to_json(target, orient="records", indent=2, **kwargs)
        return
    if suffix in (".xlsx", ".xlsm"):
        out = sanitize_dataframe_for_spreadsheet(df) if sanitize_csv else df
        kwargs.setdefault("engine", "openpyxl")
        out.to_excel(target, index=False, **kwargs)
        return
    out = sanitize_dataframe_for_csv(df) if sanitize_csv else df
    kwargs.setdefault("encoding", _CSV_WRITE_ENCODING)
    kwargs.setdefault("lineterminator", "\n")
    kwargs.setdefault("sep", "\t" if suffix == ".tsv" else ",")
    out.to_csv(target, index=False, **kwargs)


def write_frame(
    df: pd.DataFrame,
    path: str | Path,
    *,
    sanitize_csv: bool = True,
    source: str | Path | None = None,
    overwrite: bool = False,
    **kwargs,
) -> Path:
    """Write a dataframe, dispatching on file extension. Never writes the index.

    The frame is written to a temporary sibling and moved into place, so a failure
    part-way through leaves the previous file intact rather than a truncated one.

    Parameters
    ----------
    sanitize_csv:
        When ``True`` (default), string cells and headers that look like spreadsheet
        formulas (leading ``=``, ``@``, or ``+``/``-`` followed by a non-number) are
        escaped before CSV/TSV/Excel export. Set ``False`` only when you intentionally
        need raw formula cells.
    source / overwrite:
        ``source`` is the path the data was read from; writing back over it needs
        ``overwrite=True`` so the original is never destroyed by accident.
    """
    if not isinstance(df, pd.DataFrame):
        raise CleanFrameError(f"write_frame expects a DataFrame, got {type(df).__name__}.")
    path = check_output_target(path, source, overwrite=overwrite)
    suffix = path.suffix.lower()
    if suffix == ".xls":
        raise OutputError(
            "Writing legacy .xls is not supported: pandas emits .xlsx bytes, which "
            "Excel refuses under an .xls name. Write .xlsx instead."
        )
    tmp = path.with_name(path.name + ".cf-tmp")
    try:
        _write_dispatch(df, tmp, suffix, sanitize_csv, kwargs)
        os.replace(tmp, path)
    except ImportError as exc:  # pragma: no cover - optional engine missing
        tmp.unlink(missing_ok=True)
        extra = "parquet" if suffix == ".parquet" else "excel"
        raise OutputError(
            f"Writing {suffix} requires an extra engine ({_detail(exc)}). "
            f"Try `pip install cleanframe-engine[{extra}]`."
        ) from exc
    except CleanFrameError:
        tmp.unlink(missing_ok=True)
        raise
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise OutputError(f"Could not write {path}: {_detail(exc)}.") from exc
    except Exception as exc:  # noqa: BLE001 - IO boundary
        tmp.unlink(missing_ok=True)
        raise OutputError(
            f"Could not write {path.name}: {type(exc).__name__}: {_detail(exc)}."
        ) from exc
    return path


__all__ = [
    "read_frame",
    "write_frame",
    "excel_sheet_names",
    "inference_losses",
    "compare_for_losses",
]
