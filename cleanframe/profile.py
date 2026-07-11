"""Deterministic profiling: what is *in* each column before we touch it.

The profiler is the first stage of the pipeline. It computes per-column statistics
(nulls, cardinality, examples) and — crucially — a best-guess **semantic type**
(``email``, ``currency``, ``date``, ``categorical``, …) that detectors and schema
inference build on. Everything here is read-only and reproducible: profiling the
same frame twice yields identical numbers.

Performance note: pattern-matching fractions are computed over a bounded, stable
*head* sample (:data:`PATTERN_SAMPLE_CAP`) so profiling a million-row file stays
fast without becoming non-deterministic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from .ops import CURRENCY_SYMBOLS, parse_unit_scalar

PATTERN_SAMPLE_CAP = 5000

# -- reusable patterns -------------------------------------------------------
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
URL_RE = re.compile(r"^(https?://|www\.)\S+$", re.IGNORECASE)
_DATEISH_RE = re.compile(r"[/.\-]|\d{1,2}\s*[A-Za-z]{3,}|[A-Za-z]{3,}\s*\d{1,2}")
_DIGIT_RE = re.compile(r"\d")
_BOOL_TOKENS = {"true", "false", "yes", "no", "t", "f", "y", "n"}

#: Candidate date formats, ordered most-specific first. Shared with the dates
#: detector so profiling and planning agree on what "a date" looks like. Pure
#: all-digit formats are intentionally excluded to avoid classifying plain
#: integers (``"20240101"``, ``"1200"``) as dates.
COMMON_DATE_FORMATS = [
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%d-%m-%Y",
    "%m-%d-%Y",
    "%d.%m.%Y",
    "%d/%m/%y",
    "%m/%d/%y",
    "%d-%m-%y",
    "%d %b %Y",
    "%d %B %Y",
    "%b %d, %Y",
    "%B %d, %Y",
    "%d-%b-%Y",
    "%d-%b-%y",
    "%b %d %Y",
    "%d %b %y",
]

# Column-name hints (lowercased substrings) that nudge ambiguous classifications.
_NAME_HINTS = {
    "email": ("email", "e-mail", "mail"),
    "phone": ("phone", "mobile", "contact", "tel", "cell", "whatsapp"),
    "date": ("date", "dob", "day", "created", "updated", "signup", "joined", "timestamp"),
    "currency": ("amount", "price", "cost", "salary", "revenue", "inr", "usd", "paid", "total"),
    "id": ("id", "code", "uuid", "guid", "ref", "sku"),
    "unit": ("weight", "mass", "height", "length", "width", "depth", "volume", "qty", "quantity", "size"),
}


@dataclass
class ColumnProfile:
    """Read-only statistics and a semantic-type guess for one column."""

    name: str
    dtype: str
    count: int  # non-null
    null_count: int
    unique_count: int
    semantic_type: str = "text"
    type_confidence: float = 0.0
    sample_values: list[Any] = field(default_factory=list)
    most_common: list[tuple[Any, int]] = field(default_factory=list)
    numeric_stats: dict[str, float] | None = None
    str_len_stats: dict[str, float] | None = None
    signals: dict[str, float] = field(default_factory=dict)

    @property
    def n(self) -> int:
        return self.count + self.null_count

    @property
    def null_fraction(self) -> float:
        return self.null_count / self.n if self.n else 0.0

    @property
    def unique_fraction(self) -> float:
        return self.unique_count / self.count if self.count else 0.0

    @property
    def is_unique(self) -> bool:
        return self.count > 0 and self.unique_count == self.count

    @property
    def is_constant(self) -> bool:
        return self.unique_count == 1


@dataclass
class DataFrameProfile:
    """Profiles for every column plus a few frame-level facts."""

    n_rows: int
    n_columns: int
    columns: list[ColumnProfile]
    duplicate_row_count: int = 0

    def column(self, name: str) -> ColumnProfile | None:
        for c in self.columns:
            if c.name == name:
                return c
        return None

    def __iter__(self):
        return iter(self.columns)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _str_sample(series: pd.Series, cap: int = PATTERN_SAMPLE_CAP) -> list[str]:
    """Bounded, stable list of stringified non-null values."""
    non_null = series.dropna()
    if len(non_null) > cap:
        non_null = non_null.head(cap)
    return [v if isinstance(v, str) else str(v) for v in non_null.tolist()]


def _name_hint(column_name: str, kind: str) -> bool:
    low = str(column_name).lower()
    return any(h in low for h in _NAME_HINTS.get(kind, ()))


def _frac(matches: int, total: int) -> float:
    return matches / total if total else 0.0


def _looks_date(value: str) -> bool:
    return bool(_DATEISH_RE.search(value)) and bool(_DIGIT_RE.search(value))


def _count_date_matches(values: list[str]) -> int:
    dateish = [v for v in values if _looks_date(v)]
    if not dateish:
        return 0
    ser = pd.Series(dateish)
    matched = pd.Series(False, index=ser.index)
    for fmt in COMMON_DATE_FORMATS:
        pending = ser[~matched]
        if pending.empty:
            break
        parsed = pd.to_datetime(pending, format=fmt, errors="coerce")
        matched.loc[pending.index[parsed.notna().to_numpy()]] = True
    return int(matched.sum())


def _looks_currency(value: str) -> bool:
    has_symbol = any(sym in value for sym in CURRENCY_SYMBOLS)
    up = value.upper()
    has_code = any(f" {c}" in f" {up} " or up.strip().endswith(c) or up.strip().startswith(c)
                   for c in ("USD", "EUR", "GBP", "INR", "JPY"))
    return (has_symbol or has_code) and bool(_DIGIT_RE.search(value))


def _looks_unit(value: str) -> bool:
    return parse_unit_scalar(value) is not None


def _looks_phone(value: str) -> bool:
    digits = re.sub(r"\D", "", value)
    if not (7 <= len(digits) <= 15):
        return False
    # Reject values that are "just a number" with no phone-like structure and no
    # leading + / 0 — those are more likely quantities than phone numbers.
    structured = bool(re.search(r"[+\-() ]", value)) or value.strip().startswith(("+", "0"))
    return structured


_STRICT_NUM_RE = re.compile(r"^[+-]?\d+(\.\d+)?$")


def _is_strict_numeric(value: str) -> bool:
    """True only for genuine numbers (allowing thousands separators/parentheses).

    Deliberately stricter than :func:`ops._parse_number_scalar`, which strips *any*
    non-digit and would turn an id like ``"U001"`` into ``1``. Here a stray letter
    disqualifies the value, so alphanumeric ids are not mistaken for numbers.
    """
    s = value.strip()
    if s.startswith("(") and s.endswith(")"):
        s = s[1:-1]
    s = s.replace(",", "").replace(" ", "")
    return bool(_STRICT_NUM_RE.match(s))


def _numeric_signal(values: list[str]) -> tuple[float, bool]:
    """Return ``(fraction_strictly_numeric, any_have_decimals)``."""
    if not values:
        return 0.0, False
    numeric = [v for v in values if _is_strict_numeric(v)]
    has_decimal = any("." in v for v in numeric)
    return _frac(len(numeric), len(values)), has_decimal


def _infer_semantic_type(
    name: str, series: pd.Series, count: int, unique_count: int
) -> tuple[str, float, dict[str, float]]:
    """Return ``(semantic_type, confidence, signals)`` for a column.

    Object/string columns are classified by the fraction of sampled values that
    match each pattern, resolved by a fixed priority so the result is stable.
    """
    signals: dict[str, float] = {}
    if count == 0:
        return "empty", 1.0, signals

    dtype = series.dtype
    if pd.api.types.is_bool_dtype(dtype):
        return "boolean", 1.0, signals
    if pd.api.types.is_datetime64_any_dtype(dtype):
        return "datetime", 1.0, signals
    if pd.api.types.is_integer_dtype(dtype):
        return "integer", 1.0, signals
    if pd.api.types.is_float_dtype(dtype):
        return "float", 1.0, signals

    values = _str_sample(series)
    total = len(values)
    if total == 0:
        return "text", 0.0, signals

    frac_bool = _frac(sum(1 for v in values if v.strip().casefold() in _BOOL_TOKENS), total)
    frac_email = _frac(sum(1 for v in values if EMAIL_RE.match(v.strip())), total)
    frac_url = _frac(sum(1 for v in values if URL_RE.match(v.strip())), total)
    frac_currency = _frac(sum(1 for v in values if _looks_currency(v)), total)
    frac_unit = _frac(sum(1 for v in values if _looks_unit(v)), total)
    frac_date = _frac(_count_date_matches(values), total)
    frac_phone = _frac(sum(1 for v in values if _looks_phone(v)), total)
    frac_numeric, numeric_has_decimal = _numeric_signal(values)

    signals.update(
        bool=round(frac_bool, 3), email=round(frac_email, 3), url=round(frac_url, 3),
        currency=round(frac_currency, 3), unit=round(frac_unit, 3),
        date=round(frac_date, 3), phone=round(frac_phone, 3), numeric=round(frac_numeric, 3),
    )

    uniq_frac = unique_count / count

    # Fixed-priority resolution. Currency/date beat plain numeric because a money
    # or date column also "parses as a number" once symbols are stripped.
    if frac_bool >= 0.9:
        return "boolean", frac_bool, signals
    if frac_email >= 0.8 or (frac_email >= 0.5 and _name_hint(name, "email")):
        return "email", max(frac_email, 0.8 if _name_hint(name, "email") else frac_email), signals
    if frac_url >= 0.8:
        return "url", frac_url, signals
    if frac_currency >= 0.6:
        return "currency", frac_currency, signals
    if frac_unit >= 0.6 or (frac_unit >= 0.4 and _name_hint(name, "unit")):
        return "unit", max(frac_unit, 0.8 if _name_hint(name, "unit") else frac_unit), signals
    if frac_date >= 0.8 or (frac_date >= 0.5 and _name_hint(name, "date")):
        return "date", max(frac_date, 0.8 if _name_hint(name, "date") else frac_date), signals
    if frac_phone >= 0.8 or (frac_phone >= 0.5 and _name_hint(name, "phone")):
        return "phone", max(frac_phone, 0.8 if _name_hint(name, "phone") else frac_phone), signals
    if frac_numeric >= 0.95:
        return ("float" if numeric_has_decimal else "integer"), frac_numeric, signals
    if unique_count <= 50 and uniq_frac < 0.5:
        return "categorical", 1.0 - uniq_frac, signals
    if uniq_frac >= 0.95:
        return "id" if _name_hint(name, "id") else "text", uniq_frac, signals
    return "text", 0.5, signals


def _value_counts_stable(series: pd.Series, top: int = 10) -> list[tuple[Any, int]]:
    vc = series.dropna().value_counts()
    pairs = list(vc.items())
    # Deterministic tie-break: count desc, then string of value asc.
    pairs.sort(key=lambda kv: (-int(kv[1]), str(kv[0])))
    return [(k, int(v)) for k, v in pairs[:top]]


def profile_column(series: pd.Series, name: str | None = None) -> ColumnProfile:
    name = str(series.name if name is None else name)
    n = len(series)
    null_count = int(series.isna().sum())
    count = n - null_count
    non_null = series.dropna()
    unique_count = int(non_null.nunique())

    semantic_type, confidence, signals = _infer_semantic_type(name, series, count, unique_count)

    sample_values = non_null.drop_duplicates().head(5).tolist()
    most_common = _value_counts_stable(series)

    numeric_stats = None
    if pd.api.types.is_numeric_dtype(series.dtype) and count:
        numeric = pd.to_numeric(series, errors="coerce")
        numeric_stats = {
            "min": float(numeric.min()),
            "max": float(numeric.max()),
            "mean": float(numeric.mean()),
        }

    str_len_stats = None
    if semantic_type in ("text", "categorical", "id") and count:
        lengths = non_null.map(lambda v: len(v) if isinstance(v, str) else len(str(v)))
        if len(lengths):
            str_len_stats = {
                "min": float(lengths.min()),
                "max": float(lengths.max()),
                "mean": float(lengths.mean()),
            }

    return ColumnProfile(
        name=name,
        dtype=str(series.dtype),
        count=count,
        null_count=null_count,
        unique_count=unique_count,
        semantic_type=semantic_type,
        type_confidence=round(float(confidence), 3),
        sample_values=sample_values,
        most_common=most_common,
        numeric_stats=numeric_stats,
        str_len_stats=str_len_stats,
        signals=signals,
    )


def profile_dataframe(df: pd.DataFrame) -> DataFrameProfile:
    """Profile every column plus frame-level facts (row count, exact-duplicate rows)."""
    columns = [profile_column(df[c], name=str(c)) for c in df.columns]
    duplicate_row_count = int(df.duplicated().sum())
    return DataFrameProfile(
        n_rows=int(len(df)),
        n_columns=int(df.shape[1]),
        columns=columns,
        duplicate_row_count=duplicate_row_count,
    )


__all__ = [
    "ColumnProfile",
    "DataFrameProfile",
    "profile_column",
    "profile_dataframe",
    "COMMON_DATE_FORMATS",
    "EMAIL_RE",
    "URL_RE",
]
