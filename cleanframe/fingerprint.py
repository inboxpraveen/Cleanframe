"""Deterministic hashing and dataframe fingerprints.

Everything here is pure and reproducible: the same input always yields the same
digest, on any machine, in any process. That property is what lets a recipe's
``source_fingerprint`` be compared reliably months later for drift detection.
"""

from __future__ import annotations

import hashlib
from typing import Any

import pandas as pd

#: How many leading rows feed the content hash. Fixed so the fingerprint is
#: stable regardless of total file size.
DEFAULT_SAMPLE_ROWS = 200


def _canonical(value: Any) -> str:
    """Render a single cell to a stable string.

    ``NaN``/``None`` collapse to a single sentinel so that a missing value hashes
    the same whether it arrived as ``float('nan')``, ``None``, or ``pd.NA``.
    """
    if value is None:
        return "\x00NA\x00"
    # pd.isna raises on array-like; cells are scalars here.
    try:
        if pd.isna(value):
            return "\x00NA\x00"
    except (TypeError, ValueError):
        pass
    if isinstance(value, float):
        # repr(float) is round-trippable and stable across platforms in CPython.
        return repr(value)
    return str(value)


def stable_hash(*parts: Any, length: int | None = None) -> str:
    """Hash an arbitrary sequence of parts into a hex digest.

    Parts are joined with a delimiter that cannot appear in :func:`_canonical`
    output, so ``("a", "bc")`` and ``("ab", "c")`` never collide.
    """
    hasher = hashlib.sha256()
    for part in parts:
        hasher.update(_canonical(part).encode("utf-8"))
        hasher.update(b"\x1f")  # unit separator delimiter
    digest = hasher.hexdigest()
    return digest[:length] if length else digest


def fingerprint_dataframe(df: pd.DataFrame, sample_rows: int = DEFAULT_SAMPLE_ROWS) -> dict:
    """Build a compact, comparable fingerprint of a dataframe's shape and content.

    The returned dict is plain JSON/YAML-serialisable and is stored verbatim in a
    recipe's ``source_fingerprint``. It intentionally records *structure* (column
    names, order, dtypes) plus a *content* hash of the leading rows — enough to
    detect drift without embedding the data itself.
    """
    columns = [str(c) for c in df.columns]
    dtypes = {str(c): str(df[c].dtype) for c in df.columns}

    head = df.head(sample_rows)
    hasher = hashlib.sha256()
    # Column names participate in the content hash so a rename alone changes it.
    for col in columns:
        hasher.update(col.encode("utf-8"))
        hasher.update(b"\x1e")  # record separator
        series = head[col]
        for value in series.tolist():
            hasher.update(_canonical(value).encode("utf-8"))
            hasher.update(b"\x1f")

    return {
        "columns": len(columns),
        "column_names": columns,
        "dtypes": dtypes,
        "row_count": int(len(df)),
        "sampled_rows": int(len(head)),
        "hash_sample": hasher.hexdigest()[:16],
    }
