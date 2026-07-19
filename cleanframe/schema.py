"""Target schemas: the shape you want your data to end up in.

A schema is optional. When you pass one to :func:`cleanframe.clean`, the planner
maps messy source columns onto your canonical columns (by fuzzy name + type match,
with confidence) and derives validation rules from your constraints. When you
don't have one, :func:`infer_schema` proposes a starting point from a clean-ish
frame that you then edit and commit.

Schemas are plain YAML, deliberately close to the recipe format so the two read
alike in a PR.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from ._util import read_text, snake_case, write_text
from .errors import SchemaError
from .profile import COMMON_DATE_FORMATS, _name_hint, profile_dataframe

#: Map a profiler semantic type to a schema logical dtype.
_SEMANTIC_TO_DTYPE = {
    "integer": "integer",
    "float": "float",
    "currency": "float",
    "boolean": "boolean",
    "date": "date",
    "datetime": "datetime",
    "email": "email",
    "phone": "phone",
    "url": "url",
    "categorical": "category",
    "id": "string",
    "text": "string",
    "empty": "string",
}

#: How many distinct values a category may have before we stop enumerating them.
_MAX_ALLOWED_VALUES = 50

#: The logical dtypes a schema column may declare. A typo ("flaot", "emial") would
#: otherwise be silently ignored (no cast, no warning), so it is rejected at load.
_VALID_DTYPES = frozenset(
    {
        "string", "text", "integer", "int", "float", "number", "boolean", "bool",
        "date", "datetime", "email", "phone", "url", "category", "id",
    }
)


def _check_dtype(dtype: str, column: str) -> None:
    if dtype.strip().lower() not in _VALID_DTYPES:
        raise SchemaError(
            f"Schema column {column!r} has unknown dtype {dtype!r}. "
            f"Valid dtypes: {', '.join(sorted(_VALID_DTYPES))}."
        )


@dataclass
class SchemaColumn:
    """One column in a target schema: a name, a logical type, and constraints."""

    name: str
    dtype: str = "string"
    required: bool = False
    unique: bool = False
    allowed_values: list[Any] | None = None
    date_formats: list[str] | None = None
    min: float | None = None
    max: float | None = None
    aliases: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        out: dict[str, Any] = {"dtype": self.dtype}
        if self.required:
            out["required"] = True
        if self.unique:
            out["unique"] = True
        if self.allowed_values is not None:
            out["allowed_values"] = list(self.allowed_values)
        if self.date_formats:
            out["date_formats"] = list(self.date_formats)
        if self.min is not None:
            out["min"] = self.min
        if self.max is not None:
            out["max"] = self.max
        if self.aliases:
            out["aliases"] = list(self.aliases)
        return out

    @classmethod
    def from_dict(cls, name: str, raw: Any) -> SchemaColumn:
        if raw is None:
            return cls(name=name)
        if isinstance(raw, str):  # shorthand: `col: float`
            _check_dtype(raw, name)
            return cls(name=name, dtype=raw)
        if not isinstance(raw, dict):
            raise SchemaError(f"Schema column {name!r} must be a mapping or a dtype string.")
        _check_dtype(str(raw.get("dtype", "string")), name)
        return cls(
            name=name,
            dtype=str(raw.get("dtype", "string")),
            required=bool(raw.get("required", False)),
            unique=bool(raw.get("unique", False)),
            allowed_values=raw.get("allowed_values"),
            date_formats=raw.get("date_formats"),
            min=raw.get("min"),
            max=raw.get("max"),
            aliases=list(raw.get("aliases", []) or []),
        )


@dataclass
class Schema:
    """An ordered set of target columns."""

    columns: list[SchemaColumn] = field(default_factory=list)
    name: str | None = None

    def column(self, name: str) -> SchemaColumn | None:
        for c in self.columns:
            if c.name == name:
                return c
        return None

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]

    # -- serialisation ---------------------------------------------------
    def to_dict(self) -> dict:
        out: dict[str, Any] = {"version": 1}
        if self.name:
            out["name"] = self.name
        out["columns"] = {c.name: c.to_dict() for c in self.columns}
        return out

    def to_yaml(self) -> str:
        return yaml.safe_dump(
            self.to_dict(), sort_keys=False, allow_unicode=True, default_flow_style=False, width=100
        )

    def save(self, path: str | Path) -> Path:
        return write_text(path, self.to_yaml())

    @classmethod
    def from_dict(cls, raw: dict) -> Schema:
        if not isinstance(raw, dict):
            raise SchemaError("A schema must be a mapping at the top level.")
        raw_cols = raw.get("columns", raw if "columns" not in raw and "version" not in raw else {})
        if not isinstance(raw_cols, dict):
            raise SchemaError("Schema 'columns' must be a mapping of name -> spec.")
        columns = [SchemaColumn.from_dict(str(n), spec) for n, spec in raw_cols.items()]
        return cls(columns=columns, name=raw.get("name"))

    @classmethod
    def load(cls, path: str | Path) -> Schema:
        path = Path(path)
        if not path.exists():
            raise SchemaError(f"Schema not found: {path}")
        return cls.from_dict(yaml.safe_load(read_text(path)))


def _infer_date_formats(series: pd.Series) -> list[str]:
    """Which of the common date formats actually parse this column (order preserved)."""
    values = series.dropna().astype(str)
    if values.empty:
        return []
    found: list[str] = []
    remaining = values
    for fmt in COMMON_DATE_FORMATS:
        if remaining.empty:
            break
        parsed = pd.to_datetime(remaining, format=fmt, errors="coerce")
        if parsed.notna().any():
            found.append(fmt)
            remaining = remaining[parsed.isna()]
    return found


def infer_schema(df: pd.DataFrame, name: str | None = None) -> Schema:
    """Propose a target :class:`Schema` from a (reasonably clean) dataframe.

    A convenience for bootstrapping: profile the frame, translate each column's
    semantic type into a logical dtype, and capture obvious constraints (no-nulls
    → ``required``, all-unique → ``unique``, low-cardinality → ``allowed_values``,
    numeric range → ``min``/``max``). Treat the result as a first draft to edit,
    not gospel.
    """
    profile = profile_dataframe(df)
    columns: list[SchemaColumn] = []
    for cp in profile.columns:
        dtype = _SEMANTIC_TO_DTYPE.get(cp.semantic_type, "string")
        # A target schema uses canonical (snake_case) names so that it agrees with
        # the planner's default output names; the original messy name becomes an
        # alias so column mapping still recognises it. Without this, an inferred
        # schema's names would not match the cleaned output and its validations
        # would silently apply to nothing.
        canonical = snake_case(cp.name) or cp.name
        aliases = [cp.name] if canonical != cp.name else []
        # Only infer `unique` for key-like columns. A tiny sample makes *everything*
        # look unique; asserting it on, say, an amount column would wrongly reject
        # valid future rows (and cleaning can even collapse distinct raw strings —
        # "₹1,200" and "₹1200" — into one value).
        key_like = cp.semantic_type in ("id", "email") or _name_hint(cp.name, "id")
        col = SchemaColumn(
            name=canonical,
            dtype=dtype,
            required=cp.null_count == 0 and cp.count > 0,
            unique=cp.is_unique and key_like,
            aliases=aliases,
        )
        if dtype == "category" and cp.count:
            distinct = sorted({str(v) for v in df[cp.name].dropna().unique()})
            if len(distinct) <= _MAX_ALLOWED_VALUES:
                col.allowed_values = distinct
        if dtype in ("date", "datetime"):
            fmts = _infer_date_formats(df[cp.name])
            if fmts:
                col.date_formats = fmts
        if dtype in ("integer", "float") and cp.numeric_stats:
            col.min = cp.numeric_stats["min"]
            col.max = cp.numeric_stats["max"]
        columns.append(col)
    return Schema(columns=columns, name=name)


__all__ = ["Schema", "SchemaColumn", "infer_schema"]
