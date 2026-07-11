"""Reading and writing dataframes by file extension.

A thin, predictable wrapper so the CLI and API accept a path anywhere a dataframe
is expected. CSV/TSV/Excel/Parquet/JSON are dispatched by suffix. Reading uses
pandas' default NA handling (blank/``NA``/``null`` → NaN); CleanFrame's detectors
then catch the *disguised* nulls pandas leaves behind (``unknown``, ``-``, ``?``).
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from .errors import CleanFrameError


def read_frame(path: str | Path, **kwargs) -> pd.DataFrame:
    """Read a dataframe, dispatching on file extension."""
    path = Path(path)
    if not path.exists():
        raise CleanFrameError(f"Input file not found: {path}")
    suffix = path.suffix.lower()
    try:
        if suffix in (".csv", ".txt"):
            return pd.read_csv(path, **kwargs)
        if suffix in (".tsv",):
            return pd.read_csv(path, sep="\t", **kwargs)
        if suffix in (".xlsx", ".xls", ".xlsm"):
            return pd.read_excel(path, **kwargs)
        if suffix == ".parquet":
            return pd.read_parquet(path, **kwargs)
        if suffix in (".json",):
            return pd.read_json(path, **kwargs)
        # default: try CSV
        return pd.read_csv(path, **kwargs)
    except ImportError as exc:  # pragma: no cover - optional engine missing
        raise CleanFrameError(
            f"Reading {suffix} requires an extra engine: {exc}. "
            "Try `pip install cleanframe[excel]` for Excel support."
        ) from exc


def write_frame(df: pd.DataFrame, path: str | Path, **kwargs) -> Path:
    """Write a dataframe, dispatching on file extension. Never writes the index."""
    path = Path(path)
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix.lower()
    if suffix in (".csv", ".txt"):
        df.to_csv(path, index=False, **kwargs)
    elif suffix == ".tsv":
        df.to_csv(path, sep="\t", index=False, **kwargs)
    elif suffix in (".xlsx", ".xls", ".xlsm"):
        df.to_excel(path, index=False, **kwargs)
    elif suffix == ".parquet":
        df.to_parquet(path, index=False, **kwargs)
    elif suffix == ".json":
        df.to_json(path, orient="records", indent=2, **kwargs)
    else:
        df.to_csv(path, index=False, **kwargs)
    return path


__all__ = ["read_frame", "write_frame"]
