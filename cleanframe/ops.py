"""The op registry: every transform a recipe can perform, as pure pandas.

This module is the deterministic heart of CleanFrame. A recipe is just an ordered
list of :class:`~cleanframe.types.Op` names + params; each name resolves here to a
plain function of a pandas object. There is **no** randomness, no clock, no
network, and no hidden state — the same op applied to the same data always yields
the same result. That is the whole promise ("Same input → same output, every
time"), and it lives or dies in this file.

Two op *scopes*:

* **column** ops take a :class:`pandas.Series` and return either a Series (the new
  column) or a :class:`ColumnOpResult` (a new Series *plus* extra columns to add,
  used by ``extract_currency``).
* **frame** ops take a :class:`pandas.DataFrame` and return a DataFrame. They must
  preserve the row index of surviving rows (the executor diffs the index to learn
  which rows were dropped).

Adding an op is intentionally a ~15-line affair — see ``CONTRIBUTING.md``.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from ._util import safe_compile_regex
from .errors import OpError, RecipeError
from .types import Op


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
@dataclass
class ColumnOpResult:
    """Return value for a column op that also emits *new* columns.

    ``series`` replaces the column the op ran on; ``emit`` maps new output column
    names to their Series (used by ``extract_currency`` to add ``<col>_currency``).
    """

    series: pd.Series
    emit: dict[str, pd.Series] = field(default_factory=dict)


@dataclass
class OpSpec:
    name: str
    func: Callable[..., Any]
    scope: str  # "column" | "frame"
    coerce: Callable[[Any], dict] | None = None
    compact: Callable[[dict], Any] | None = None
    doc: str = ""


OP_REGISTRY: dict[str, OpSpec] = {}


def register_op(
    name: str,
    *,
    scope: str = "column",
    coerce: Callable[[Any], dict] | None = None,
    compact: Callable[[dict], Any] | None = None,
) -> Callable[[Callable], Callable]:
    """Register a transform under ``name``. See module docstring for scopes.

    ``coerce`` maps the compact recipe form to canonical params (load direction).
    ``compact`` is the inverse for serialisation: it maps canonical params back to
    the minimal YAML value (bare string / list / trimmed dict), keeping generated
    recipes as clean as hand-written ones. ``coerce(compact(p))`` must equal ``p``.
    """

    if scope not in ("column", "frame"):
        raise ValueError(f"scope must be 'column' or 'frame', got {scope!r}")

    def decorator(func: Callable) -> Callable:
        if name in OP_REGISTRY:
            raise ValueError(f"Op {name!r} is already registered.")
        OP_REGISTRY[name] = OpSpec(
            name=name, func=func, scope=scope, coerce=coerce, compact=compact,
            doc=func.__doc__ or "",
        )
        return func

    return decorator


def _prune(params: dict, defaults: dict) -> dict:
    """Drop params equal to their documented default (for minimal serialisation)."""
    return {k: v for k, v in params.items() if k not in defaults or v != defaults[k]}


def op_to_compact(op: Op) -> Any:
    """Serialise an :class:`Op` to its minimal recipe form using the op's ``compact``.

    Falls back to :meth:`Op.to_compact` for ops that declare no custom compactor.
    """
    spec = OP_REGISTRY.get(op.name)
    if spec is None or spec.compact is None:
        return op.to_compact()
    value = spec.compact(op.params)
    if value is None or value == {} or value == "":
        return op.name
    return {op.name: value}


def get_op(name: str) -> OpSpec:
    spec = OP_REGISTRY.get(name)
    if spec is None:
        raise RecipeError(
            f"Unknown op {name!r}. Known ops: {', '.join(sorted(OP_REGISTRY))}."
        )
    return spec


def list_ops(scope: str | None = None) -> list[str]:
    names = [n for n, s in OP_REGISTRY.items() if scope is None or s.scope == scope]
    return sorted(names)


def normalize_op(name: str, raw_params: Any = None) -> Op:
    """Turn a compact op form into a canonical :class:`Op` with a params dict.

    ``raw_params`` is whatever appeared after the op name in the recipe YAML — a
    dict, a bare scalar/list shorthand, or ``None``. Each op's ``coerce`` function
    (if any) maps that to canonical params.
    """
    spec = get_op(name)
    if spec.coerce is not None:
        try:
            params = spec.coerce(raw_params)
        except RecipeError:
            raise
        except (KeyError, ValueError, TypeError) as exc:
            # A coerce touched a missing/invalid param — surface it as a RecipeError
            # so the errors.py contract (everything on purpose derives from
            # CleanFrameError) holds and the CLI shows a tidy message, not a traceback.
            detail = f"missing key {exc}" if isinstance(exc, KeyError) else str(exc)
            raise RecipeError(f"Op {name!r} has invalid parameters: {detail}.") from exc
    elif raw_params is None or raw_params == "":
        params = {}
    elif isinstance(raw_params, dict):
        params = dict(raw_params)
    else:
        raise RecipeError(
            f"Op {name!r} expects a mapping of parameters, got {type(raw_params).__name__}."
        )
    return Op(name=name, params=params)


def apply_column_op(op: Op, series: pd.Series) -> ColumnOpResult:
    """Apply a column-scope op to a Series, always returning a :class:`ColumnOpResult`."""
    spec = get_op(op.name)
    if spec.scope != "column":
        raise RecipeError(f"Op {op.name!r} is a frame op; it cannot run on a single column.")
    try:
        out = spec.func(series, **op.params)
    except OpError:
        raise
    except Exception as exc:  # noqa: BLE001 - surface any pandas failure as OpError
        raise OpError(f"Op {op.name!r} failed on column {series.name!r}: {exc}") from exc
    if isinstance(out, ColumnOpResult):
        return out
    if isinstance(out, pd.Series):
        return ColumnOpResult(series=out)
    raise OpError(f"Op {op.name!r} returned {type(out).__name__}, expected a Series.")


def apply_frame_op(op: Op, df: pd.DataFrame) -> pd.DataFrame:
    spec = get_op(op.name)
    if spec.scope != "frame":
        raise RecipeError(f"Op {op.name!r} is a column op; it cannot run on the whole frame.")
    try:
        out = spec.func(df, **op.params)
    except OpError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise OpError(f"Frame op {op.name!r} failed: {exc}") from exc
    if not isinstance(out, pd.DataFrame):
        raise OpError(f"Frame op {op.name!r} returned {type(out).__name__}, expected a DataFrame.")
    return out


# ---------------------------------------------------------------------------
# Small helpers (all NaN-preserving and deterministic)
# ---------------------------------------------------------------------------
def _is_na(v: Any) -> bool:
    if v is None:
        return True
    if isinstance(v, float):
        return math.isnan(v)
    try:
        return bool(pd.isna(v))
    except (TypeError, ValueError):
        return False


def _is_pure_string(series: pd.Series) -> bool:
    """True if a vectorised ``.str`` is safe: an object column whose non-null values
    are *all* ``str``. ``infer_dtype`` scans in C; only then can ``.str`` not silently
    NaN a stray non-string cell, and object→object keeps the output dtype identical to
    the elementwise map. (Non-object string dtypes stay on the elementwise path so the
    result dtype never changes from the historical behaviour.)"""
    return pd.api.types.is_object_dtype(series.dtype) and pd.api.types.infer_dtype(
        series, skipna=True
    ) in ("string", "empty")


def _apply_str(
    series: pd.Series, fn: Callable[[str], Any], vec: Callable[[pd.Series], pd.Series] | None = None
) -> pd.Series:
    """Apply ``fn`` to string cells only; leave NaN and non-strings untouched.

    The default path is elementwise rather than ``Series.str.*`` — the vectorised
    string accessor coerces every non-string cell (including stray ints in an object
    column) to NaN, which would silently destroy data. When a column is provably
    all-string, the ``vec`` fast-path runs the equivalent ``.str`` chain (byte-identical,
    verified in tests) for a large speed-up on million-row columns.
    """
    if vec is not None and _is_pure_string(series):
        return vec(series)
    if pd.api.types.is_object_dtype(series.dtype):
        # Avoid Series.map(): pandas >= 3 infers StringDtype for pure-string object
        # columns and rewrites None→nan, which would diverge from the .str fast-path.
        return pd.Series(
            [fn(v) if isinstance(v, str) else v for v in series],
            index=series.index,
            dtype=object,
            name=series.name,
        )
    return series.map(lambda v: fn(v) if isinstance(v, str) else v)


# ---------------------------------------------------------------------------
# Text ops
# ---------------------------------------------------------------------------
@register_op("strip_whitespace")
def strip_whitespace(series: pd.Series) -> pd.Series:
    """Trim leading and trailing whitespace from string cells."""
    return _apply_str(series, str.strip, vec=lambda s: s.str.strip())


_WS_RE = re.compile(r"\s+")


@register_op("collapse_whitespace")
def collapse_whitespace(series: pd.Series) -> pd.Series:
    """Collapse internal runs of whitespace to a single space, then trim."""
    return _apply_str(
        series,
        lambda s: _WS_RE.sub(" ", s).strip(),
        vec=lambda s: s.str.replace(r"\s+", " ", regex=True).str.strip(),
    )


@register_op("lowercase")
def lowercase(series: pd.Series) -> pd.Series:
    """Lowercase string cells."""
    return _apply_str(series, str.lower, vec=lambda s: s.str.lower())


@register_op("uppercase")
def uppercase(series: pd.Series) -> pd.Series:
    """Uppercase string cells."""
    return _apply_str(series, str.upper, vec=lambda s: s.str.upper())


@register_op("title_case")
def title_case(series: pd.Series) -> pd.Series:
    """Title-case string cells (``"new  YORK"`` -> ``"New York"``)."""
    return _apply_str(
        series,
        lambda s: _WS_RE.sub(" ", s).strip().title(),
        vec=lambda s: s.str.replace(r"\s+", " ", regex=True).str.strip().str.title(),
    )


@register_op("capitalize")
def capitalize(series: pd.Series) -> pd.Series:
    """Capitalize the first letter of each string cell."""
    return _apply_str(series, lambda s: s.strip().capitalize(), vec=lambda s: s.str.strip().str.capitalize())


def _coerce_symbols(raw: Any) -> dict:
    if raw is None:
        return {"symbols": []}
    if isinstance(raw, dict):
        return {"symbols": list(raw.get("symbols", []))}
    if isinstance(raw, (list, tuple)):
        return {"symbols": list(raw)}
    return {"symbols": [raw]}


@register_op("remove_symbols", coerce=_coerce_symbols, compact=lambda p: p.get("symbols", []))
def remove_symbols(series: pd.Series, symbols: list[str] | None = None) -> pd.Series:
    """Delete each listed substring from string cells (``["₹", ","]`` etc.)."""
    subs = [str(s) for s in (symbols or [])]

    def fn(s: str) -> str:
        for sub in subs:
            s = s.replace(sub, "")
        return s

    return _apply_str(series, fn)


def _coerce_replace(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise RecipeError("Op 'replace' expects a mapping with 'pattern' and 'repl'.")
    if "pattern" not in raw:
        raise RecipeError("Op 'replace' requires a 'pattern' parameter.")
    return {
        "pattern": raw["pattern"],
        "repl": raw.get("repl", ""),
        "regex": bool(raw.get("regex", True)),
    }


@register_op(
    "replace",
    coerce=_coerce_replace,
    compact=lambda p: _prune(p, {"repl": "", "regex": True}),
)
def replace(series: pd.Series, pattern: str, repl: str = "", regex: bool = True) -> pd.Series:
    """Regex (or literal) substitution over string cells."""
    if regex:
        try:
            compiled = safe_compile_regex(pattern)
        except ValueError as exc:
            raise OpError(str(exc)) from exc
        return _apply_str(series, lambda s: compiled.sub(repl, s))
    return _apply_str(series, lambda s: s.replace(pattern, repl))


#: Tokens that commonly stand in for a missing value in exported data.
DEFAULT_NA_TOKENS = [
    "",
    "na",
    "n/a",
    "n.a.",
    "null",
    "none",
    "nil",
    "nan",
    "-",
    "--",
    "?",
    "unknown",
    "not available",
    "not applicable",
]


def _coerce_to_na(raw: Any) -> dict:
    if raw is None:
        return {"tokens": None, "case_insensitive": True}
    if isinstance(raw, dict):
        return {
            "tokens": raw.get("tokens"),
            "case_insensitive": bool(raw.get("case_insensitive", True)),
        }
    if isinstance(raw, (list, tuple)):
        return {"tokens": list(raw), "case_insensitive": True}
    return {"tokens": [raw], "case_insensitive": True}


@register_op(
    "to_na",
    coerce=_coerce_to_na,
    compact=lambda p: _prune(p, {"tokens": None, "case_insensitive": True}),
)
def to_na(
    series: pd.Series,
    tokens: list[str] | None = None,
    case_insensitive: bool = True,
) -> pd.Series:
    """Convert disguised-null tokens (``"NA"``, ``"-"``, ``"unknown"``, …) to real NaN."""
    toks = DEFAULT_NA_TOKENS if tokens is None else [str(t) for t in tokens]
    lookup = {t.strip().casefold() if case_insensitive else t for t in toks}

    def fn(s: str) -> Any:
        key = s.strip().casefold() if case_insensitive else s
        return np.nan if key in lookup else s

    return _apply_str(series, fn)


_FILL_NA_STRATEGIES = frozenset(
    {"mean", "median", "mode", "ffill", "pad", "bfill", "backfill", "zero", "empty"}
)


def _coerce_fill_na(raw: Any) -> dict:
    if isinstance(raw, dict):
        strategy = raw.get("strategy")
        if strategy is not None and str(strategy).lower() not in _FILL_NA_STRATEGIES:
            raise RecipeError(
                f"Op 'fill_na' unknown strategy {strategy!r}. "
                f"Expected one of {sorted(_FILL_NA_STRATEGIES)}."
            )
        return {"value": raw.get("value"), "strategy": strategy}
    # bare scalar -> constant fill value
    return {"value": raw, "strategy": None}


def _compact_fill_na(p: dict) -> dict:
    out: dict[str, Any] = {}
    if p.get("strategy") is not None:
        out["strategy"] = p["strategy"]
    if p.get("value") is not None:
        out["value"] = p["value"]
    return out


@register_op("fill_na", coerce=_coerce_fill_na, compact=_compact_fill_na)
def fill_na(series: pd.Series, value: Any = None, strategy: str | None = None) -> pd.Series:
    """Fill missing values. Only ever runs when a human put it in the recipe.

    ``strategy`` is one of ``mean``/``median``/``mode``/``ffill``/``bfill``/``zero``/
    ``empty``; otherwise ``value`` is used as a constant. CleanFrame never *adds*
    this op automatically — missing data is reported, not silently imputed.
    """
    if strategy is None:
        return series.fillna(value)
    strategy = str(strategy).lower()
    if strategy == "mean":
        return series.fillna(pd.to_numeric(series, errors="coerce").mean())
    if strategy == "median":
        return series.fillna(pd.to_numeric(series, errors="coerce").median())
    if strategy == "mode":
        modes = series.dropna().mode()
        if len(modes) == 0:
            return series
        return series.fillna(sorted(modes.tolist(), key=str)[0])
    if strategy in ("ffill", "pad"):
        return series.ffill()
    if strategy in ("bfill", "backfill"):
        return series.bfill()
    if strategy == "zero":
        return series.fillna(0)
    if strategy == "empty":
        return series.fillna("")
    raise OpError(f"Unknown fill_na strategy {strategy!r}.")


# ---------------------------------------------------------------------------
# Contact ops
# ---------------------------------------------------------------------------
@register_op("normalize_email")
def normalize_email(series: pd.Series) -> pd.Series:
    """Trim and lowercase email addresses (the case-insensitive, safe normalisation)."""
    return _apply_str(series, lambda s: s.strip().lower(), vec=lambda s: s.str.strip().str.lower())


def _coerce_normalize_phone(raw: Any) -> dict:
    if raw is None:
        return {"default_country_code": None}
    if isinstance(raw, str):
        return {"default_country_code": raw}
    if isinstance(raw, dict):
        cc = raw.get("default_country_code", raw.get("country_code", raw.get("region")))
        return {"default_country_code": cc}
    raise RecipeError("Op 'normalize_phone' expects a country code or mapping.")


def _normalize_phone_scalar(value: Any, default_cc: str | None) -> Any:
    if _is_na(value):
        return value
    s = str(value)
    had_plus = s.strip().startswith("+")
    digits = re.sub(r"\D", "", s)
    if not digits:
        return np.nan
    if had_plus:
        return "+" + digits
    if default_cc:
        cc = re.sub(r"\D", "", str(default_cc))
        if cc and digits.startswith(cc):
            return "+" + digits
        local = digits.lstrip("0")
        return "+" + cc + local
    return digits


@register_op(
    "normalize_phone",
    coerce=_coerce_normalize_phone,
    compact=lambda p: _prune(p, {"default_country_code": None}),
)
def normalize_phone(series: pd.Series, default_country_code: str | None = None) -> pd.Series:
    """Best-effort phone normalisation: keep an existing ``+``, strip separators.

    With ``default_country_code`` (e.g. ``"+91"``), local numbers gain the country
    code (dropping a national trunk ``0``). This is intentionally lightweight — for
    strict E.164 across many regions, plug in a libphonenumber-backed detector.
    """
    return series.map(lambda v: _normalize_phone_scalar(v, default_country_code))


# ---------------------------------------------------------------------------
# Number ops
# ---------------------------------------------------------------------------
def _coerce_parse_number(raw: Any) -> dict:
    raw = raw or {}
    if not isinstance(raw, dict):
        raise RecipeError("Op 'parse_number' expects a mapping of parameters.")
    return {
        "decimal": raw.get("decimal", "."),
        "thousands": raw.get("thousands", ","),
        "symbols": list(raw.get("symbols", [])),
    }


#: A single, well-formed numeric token: optional sign, int/decimal, optional exponent.
_NUMBER_TOKEN_RE = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")


def _parse_number_scalar(value: Any, decimal: str, thousands: str, symbols: list[str]) -> float:
    if _is_na(value):
        return np.nan
    if isinstance(value, bool):
        return np.nan
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace("−", "-")  # normalise unicode minus
    if s == "":
        return np.nan
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1]
    for sym in symbols:
        s = s.replace(sym, "")
    if thousands:
        s = s.replace(thousands, "")
    if decimal != ".":
        s = s.replace(decimal, ".")
    s = s.strip()
    # Accounting/ERP trailing minus ("1234.56-") signals a negative.
    trailing_minus = s.endswith("-")
    # Extract ONE well-formed numeric token. If any *other* digits remain (e.g.
    # "12ab34", "10-12"), the string is not a single number — return NaN instead of
    # silently fusing the disjoint digit groups. Leading/trailing unit text
    # ("1200 INR") carries no stray digits, so it is still stripped cleanly.
    m = _NUMBER_TOKEN_RE.search(s)
    if not m:
        return np.nan
    token = m.group(0)
    leftover = s[: m.start()] + s[m.end() :]
    if any(ch.isdigit() for ch in leftover):
        return np.nan
    try:
        result = float(token)
    except ValueError:
        return np.nan
    if negative:
        result = -abs(result)
    elif trailing_minus and not token.startswith("-"):
        result = -result
    return result


@register_op(
    "parse_number",
    coerce=_coerce_parse_number,
    compact=lambda p: _prune(p, {"decimal": ".", "thousands": ",", "symbols": []}),
)
def parse_number(
    series: pd.Series,
    decimal: str = ".",
    thousands: str = ",",
    symbols: list[str] | None = None,
) -> pd.Series:
    """Parse messy numeric strings (``"₹1,20,000"``, ``"(1,200)"``, ``"1200 INR"``) to floats.

    Handles currency symbols, thousands separators (incl. Indian grouping), stray
    unit text, and accountant-style parentheses negatives. Unparseable cells become
    NaN. Configure ``decimal``/``thousands`` for European formats.
    """
    syms = [str(s) for s in (symbols or [])]
    return series.map(lambda v: _parse_number_scalar(v, decimal, thousands, syms))


def _coerce_cast(raw: Any) -> dict:
    if isinstance(raw, dict):
        if "to" not in raw:
            raise RecipeError("Op 'cast' requires a 'to' target type, e.g. `cast: float`.")
        return {"to": raw["to"]}
    if isinstance(raw, str):
        return {"to": raw}
    raise RecipeError("Op 'cast' expects a target type, e.g. `cast: float`.")


_TRUE_TOKENS = {"true", "t", "yes", "y", "1"}
_FALSE_TOKENS = {"false", "f", "no", "n", "0"}


@register_op("cast", coerce=_coerce_cast, compact=lambda p: p["to"])
def cast(series: pd.Series, to: str) -> pd.Series:
    """Cast a column to ``float``/``int``/``string``/``bool``/``datetime``/``category``.

    ``int`` and ``bool`` use pandas' nullable dtypes so missing values survive the
    cast instead of raising or being coerced to a sentinel. Note that ``int``
    *rounds* fractional values using banker's rounding (round-half-to-even, e.g.
    ``2.5 → 2``, ``1.5 → 2``) rather than truncating toward zero; the change is
    recorded in the diff. Parse messy numeric strings with ``parse_number`` first.
    """
    to = str(to).lower()
    if to in ("float", "float64", "number"):
        return pd.to_numeric(series, errors="coerce").astype("float64")
    if to in ("int", "integer", "int64"):
        numeric = pd.to_numeric(series, errors="coerce")
        return numeric.round().astype("Int64")
    if to in ("string", "str", "text"):
        return series.astype("string")
    if to in ("bool", "boolean"):
        def to_bool(v: Any) -> Any:
            if _is_na(v):
                return pd.NA
            if isinstance(v, bool):
                return v
            token = str(v).strip().casefold()
            if token in _TRUE_TOKENS:
                return True
            if token in _FALSE_TOKENS:
                return False
            return pd.NA
        return series.map(to_bool).astype("boolean")
    if to in ("datetime", "date"):
        # Deterministic, order-independent parse (see parse_dates_to_datetime).
        return parse_dates_to_datetime(series, None)
    if to == "category":
        return series.astype("category")
    raise OpError(f"Unknown cast target {to!r}.")


@register_op(
    "round",
    coerce=lambda raw: {"decimals": int(raw if raw is not None else 0)},
    compact=lambda p: p.get("decimals", 0),
)
def round_op(series: pd.Series, decimals: int = 0) -> pd.Series:
    """Round a numeric column to ``decimals`` places."""
    return pd.to_numeric(series, errors="coerce").round(decimals)


# ---------------------------------------------------------------------------
# Date ops
# ---------------------------------------------------------------------------
def _coerce_parse_date(raw: Any) -> dict:
    raw = raw or {}
    if not isinstance(raw, dict):
        raise RecipeError("Op 'parse_date' expects a mapping of parameters.")
    # `allowed` is the README's alias for `formats`.
    formats = raw.get("formats", raw.get("allowed"))
    return {
        "formats": list(formats) if formats else None,
        "dayfirst": bool(raw.get("dayfirst", False)),
        "yearfirst": bool(raw.get("yearfirst", False)),
        "output": raw.get("output", "%Y-%m-%d"),
    }


_DAYFIRST_SLASH = frozenset({"%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y", "%d-%m-%y"})
_MONTHFIRST_SLASH = frozenset({"%m/%d/%Y", "%m-%d-%Y", "%m/%d/%y"})


def _reconcile_date_formats(formats: list[str], dayfirst: bool) -> list[str]:
    """Drop the non-preferred d/m vs m/d family so ambiguous cells cannot swap."""
    has_day = any(f in _DAYFIRST_SLASH for f in formats)
    has_month = any(f in _MONTHFIRST_SLASH for f in formats)
    if not (has_day and has_month):
        return formats
    drop = _MONTHFIRST_SLASH if dayfirst else _DAYFIRST_SLASH
    return [f for f in formats if f not in drop]


def parse_dates_to_datetime(
    series: pd.Series,
    formats: list[str] | None,
    dayfirst: bool = False,
    yearfirst: bool = False,
) -> pd.Series:
    """Parse to a datetime64 Series (NaT where nothing matched). Shared with drift."""
    flex_fallback = False
    if not formats:
        # No explicit formats: coalesce over the common formats first — deterministic
        # and NOT order-dependent, unlike a bare format-less pd.to_datetime which locks
        # onto the first row's inferred format and silently nulls otherwise-valid dates
        # (and behaves differently across pandas versions).
        from .profile import COMMON_DATE_FORMATS

        formats = list(COMMON_DATE_FORMATS)
        flex_fallback = True

    formats = _reconcile_date_formats(list(formats), dayfirst=bool(dayfirst))

    result = pd.Series(pd.NaT, index=series.index, dtype="datetime64[ns]")
    for fmt in formats:
        mask = result.isna() & series.notna()
        if not mask.any():
            break
        parsed = pd.to_datetime(series[mask], format=fmt, errors="coerce")
        result.loc[mask] = parsed

    if flex_fallback:
        remaining = result.isna() & series.notna()
        if remaining.any():
            import warnings

            with warnings.catch_warnings():
                warnings.simplefilter("ignore")  # the dayfirst inference warning is expected here
                flex = pd.to_datetime(
                    series[remaining], errors="coerce", dayfirst=dayfirst, yearfirst=yearfirst
                )
            result.loc[remaining] = flex
    return result


@register_op(
    "parse_date",
    coerce=_coerce_parse_date,
    compact=lambda p: _prune(
        p, {"formats": None, "dayfirst": False, "yearfirst": False, "output": "%Y-%m-%d"}
    ),
)
def parse_date(
    series: pd.Series,
    formats: list[str] | None = None,
    dayfirst: bool = False,
    yearfirst: bool = False,
    output: str = "%Y-%m-%d",
) -> pd.Series:
    """Parse mixed-format dates and normalise them.

    Each declared ``format`` is tried in order (coalescing), so a column mixing
    ``31/01/2024`` and ``2024-01-31`` resolves cleanly. With no formats given,
    falls back to dateutil parsing honouring ``dayfirst``. ``output`` is a strftime
    pattern (default ISO ``%Y-%m-%d``); pass ``"datetime"`` to keep datetime dtype.
    """
    dt = parse_dates_to_datetime(series, formats, dayfirst=dayfirst, yearfirst=yearfirst)
    if str(output).lower() in ("datetime", "raw", "none"):
        return dt
    fmt = "%Y-%m-%d" if str(output).lower() in ("iso", "date") else output
    return dt.dt.strftime(fmt).where(dt.notna(), np.nan)


# ---------------------------------------------------------------------------
# Category ops
# ---------------------------------------------------------------------------
def _coerce_normalize_values(raw: Any) -> dict:
    if isinstance(raw, dict) and ("map" in raw and isinstance(raw["map"], dict)):
        return {
            "map": dict(raw["map"]),
            "case_insensitive": bool(raw.get("case_insensitive", False)),
        }
    if isinstance(raw, dict):
        # The whole mapping is the value map (README shorthand).
        return {"map": dict(raw), "case_insensitive": False}
    raise RecipeError("Op 'normalize_values' expects a mapping of old -> new values.")


def _compact_normalize_values(p: dict) -> Any:
    if p.get("case_insensitive"):
        return {"map": p.get("map", {}), "case_insensitive": True}
    return p.get("map", {})


@register_op("normalize_values", coerce=_coerce_normalize_values, compact=_compact_normalize_values)
def normalize_values(
    series: pd.Series,
    map: dict[Any, Any] | None = None,  # noqa: A002 - matches recipe key name
    case_insensitive: bool = False,
) -> pd.Series:
    """Canonicalise category variants via an explicit ``old -> new`` mapping."""
    mapping = map or {}
    if case_insensitive:
        folded = {str(k).strip().casefold(): v for k, v in mapping.items()}

        def fn(v: Any) -> Any:
            if _is_na(v):
                return v
            return folded.get(str(v).strip().casefold(), v)

        return series.map(fn)

    def fn_exact(v: Any) -> Any:
        if _is_na(v):
            return v
        return mapping.get(v, mapping.get(str(v), v))

    return series.map(fn_exact)


# ---------------------------------------------------------------------------
# Currency extraction (column op that emits an extra column)
# ---------------------------------------------------------------------------
#: Symbol / trailing-code -> ISO 4217 code. Deliberately small and explicit.
CURRENCY_SYMBOLS = {
    "₹": "INR",
    "$": "USD",
    "€": "EUR",
    "£": "GBP",
    "¥": "JPY",
    "₩": "KRW",
    "₽": "RUB",
    "R$": "BRL",
    "¢": "USD",
}
_CODE_RE = re.compile(r"\b([A-Z]{3})\b")


def _coerce_extract_currency(raw: Any) -> dict:
    raw = raw or {}
    if isinstance(raw, str):
        return {"to": raw, "default": None}
    if not isinstance(raw, dict):
        raise RecipeError("Op 'extract_currency' expects a target column name or mapping.")
    return {"to": raw.get("to"), "default": raw.get("default")}


def _detect_currency_scalar(value: Any, default: str | None) -> Any:
    if _is_na(value):
        return default if default is not None else np.nan
    s = str(value)
    for symbol, code in CURRENCY_SYMBOLS.items():
        if symbol in s:
            return code
    m = _CODE_RE.search(s.upper())
    if m and m.group(1) in _KNOWN_CODES:
        return m.group(1)
    return default if default is not None else np.nan


_KNOWN_CODES = set(CURRENCY_SYMBOLS.values()) | {
    "USD", "EUR", "GBP", "INR", "JPY", "CNY", "AUD", "CAD", "CHF", "SGD",
    "HKD", "NZD", "SEK", "NOK", "DKK", "ZAR", "AED", "SAR", "KRW", "RUB", "BRL",
}


def _compact_extract_currency(p: dict) -> Any:
    if p.get("default") is None:
        return p.get("to")
    return _prune(p, {"default": None})


@register_op(
    "extract_currency",
    scope="column",
    coerce=_coerce_extract_currency,
    compact=_compact_extract_currency,
)
def extract_currency(
    series: pd.Series,
    to: str | None = None,
    default: str | None = None,
) -> ColumnOpResult:
    """Read the currency out of a money column into a new ISO-code column.

    Returns the source column **unchanged** (so a following ``parse_number`` still
    sees ``"₹1,20,000"``) plus a new column named ``to`` (default ``<col>_currency``)
    holding ``"INR"``, ``"USD"``, … This is the one op that adds a column, which is
    why it returns a :class:`ColumnOpResult`.
    """
    target = to or f"{series.name}_currency"
    codes = series.map(lambda v: _detect_currency_scalar(v, default))
    codes.name = target
    return ColumnOpResult(series=series, emit={target: codes})


# ---------------------------------------------------------------------------
# Frame ops
# ---------------------------------------------------------------------------
def _coerce_dedup(raw: Any) -> dict:
    raw = raw or {}
    if isinstance(raw, (list, tuple)):
        return {"subset": list(raw), "keep": "first", "ignore_case": False}
    if not isinstance(raw, dict):
        raise RecipeError("Op 'dedup' expects a mapping or a list of subset columns.")
    keep = raw.get("keep", "first")
    if keep is False or str(keep).lower() == "false":
        keep = False
    subset = raw.get("subset")
    return {
        "subset": list(subset) if subset else None,
        "keep": keep,
        "ignore_case": bool(raw.get("ignore_case", False)),
    }


@register_op(
    "dedup",
    scope="frame",
    coerce=_coerce_dedup,
    compact=lambda p: _prune(p, {"subset": None, "keep": "first", "ignore_case": False}),
)
def dedup(
    df: pd.DataFrame,
    subset: list[str] | None = None,
    keep: Any = "first",
    ignore_case: bool = False,
) -> pd.DataFrame:
    """Drop duplicate rows, optionally keyed on ``subset`` and case-insensitively.

    Row index is preserved for surviving rows so the executor can attribute the
    dropped rows in the cell-level diff.
    """
    if subset:
        missing = [c for c in subset if c not in df.columns]
        if missing:
            raise OpError(f"dedup subset references unknown column(s): {missing}")
    if not ignore_case:
        return df.drop_duplicates(subset=subset, keep=keep)

    # Case/whitespace-insensitive: build a normalized key frame, dedup on it.
    key_cols = subset if subset else list(df.columns)
    key = df[key_cols].apply(
        lambda col: col.map(lambda v: v.strip().casefold() if isinstance(v, str) else v)
    )
    mask = ~key.duplicated(keep=keep if keep is not False else False)
    return df[mask]


def _coerce_drop_columns(raw: Any) -> dict:
    if isinstance(raw, str):
        return {"columns": [raw]}
    if isinstance(raw, (list, tuple)):
        return {"columns": list(raw)}
    if isinstance(raw, dict):
        return {"columns": list(raw.get("columns", []))}
    raise RecipeError("Op 'drop_columns' expects a column name or list.")


@register_op(
    "drop_columns",
    scope="frame",
    coerce=_coerce_drop_columns,
    compact=lambda p: p.get("columns", []),
)
def drop_columns(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    """Drop the named columns (ignoring any that are already absent)."""
    present = [c for c in columns if c in df.columns]
    return df.drop(columns=present)


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------
# Conversion factors to a family base unit (g, m, L). Keys are lowercased.
UNIT_FAMILIES: dict[str, dict[str, float]] = {
    "mass": {"kg": 1000.0, "g": 1.0, "mg": 0.001, "lb": 453.59237, "oz": 28.349523125},
    "length": {
        "km": 1000.0, "m": 1.0, "cm": 0.01, "mm": 0.001,
        "in": 0.0254, "ft": 0.3048, "yd": 0.9144,
    },
    "volume": {"l": 1.0, "ml": 0.001, "gal": 3.785411784},
}
_UNIT_TO_FAMILY: dict[str, str] = {
    u: fam for fam, units in UNIT_FAMILIES.items() for u in units
}
_UNIT_ALIASES = {"litre": "l", "liter": "l", "litres": "l", "liters": "l", "grams": "g", "kilos": "kg"}
_UNIT_VALUE_RE = re.compile(
    r"^\s*([+-]?\d+(?:[.,]\d+)?)\s*([A-Za-z]+)\s*$"
)


def parse_unit_scalar(value: Any) -> tuple[float, str] | None:
    """Parse ``"5kg"`` / ``"5000 g"`` → ``(5.0, "kg")``. Returns ``None`` if not a unit value."""
    if _is_na(value):
        return None
    if isinstance(value, (int, float, bool)):
        return None
    m = _UNIT_VALUE_RE.match(str(value))
    if not m:
        return None
    num_s, unit_s = m.group(1), m.group(2).casefold()
    unit_s = _UNIT_ALIASES.get(unit_s, unit_s)
    if unit_s not in _UNIT_TO_FAMILY:
        return None
    num_s = num_s.replace(",", ".")
    try:
        return float(num_s), unit_s
    except ValueError:
        return None


def _coerce_normalize_unit(raw: Any) -> dict:
    if isinstance(raw, str):
        return {"to": raw.casefold(), "emit_unit_column": None}
    raw = raw or {}
    if not isinstance(raw, dict):
        raise RecipeError("Op 'normalize_unit' expects a target unit string or mapping.")
    to = str(raw.get("to", "g")).casefold()
    to = _UNIT_ALIASES.get(to, to)
    emit = raw.get("emit_unit_column")
    return {"to": to, "emit_unit_column": emit}


def _compact_normalize_unit(p: dict) -> Any:
    if p.get("emit_unit_column"):
        return _prune(p, {"emit_unit_column": None})
    return p.get("to", "g")


@register_op(
    "normalize_unit",
    coerce=_coerce_normalize_unit,
    compact=_compact_normalize_unit,
)
def normalize_unit(
    series: pd.Series,
    to: str = "g",
    emit_unit_column: str | None = None,
) -> ColumnOpResult | pd.Series:
    """Convert mixed unit strings (``"5kg"``, ``"5000 g"``, ``"5 KG"``) to a single unit.

    Values that are already bare numbers are treated as already in ``to``. Unparseable
    cells become NaN. Optionally emit the original unit code into ``emit_unit_column``.
    """
    to = _UNIT_ALIASES.get(str(to).casefold(), str(to).casefold())
    if to not in _UNIT_TO_FAMILY:
        raise OpError(f"Unknown target unit {to!r}.")
    target_family = _UNIT_TO_FAMILY[to]
    target_factor = UNIT_FAMILIES[target_family][to]

    amounts: list[float] = []
    units: list[Any] = []
    for v in series.tolist():
        parsed = parse_unit_scalar(v)
        if parsed is None:
            if isinstance(v, (int, float)) and not isinstance(v, bool) and not _is_na(v):
                amounts.append(float(v))
                units.append(to)
            elif isinstance(v, str) and _is_strict_looking_number(v):
                try:
                    amounts.append(float(v.replace(",", ".").strip()))
                    units.append(to)
                except ValueError:
                    amounts.append(np.nan)
                    units.append(None)
            else:
                amounts.append(np.nan)
                units.append(None)
            continue
        amount, unit = parsed
        family = _UNIT_TO_FAMILY[unit]
        if family != target_family:
            amounts.append(np.nan)
            units.append(unit)
            continue
        base = amount * UNIT_FAMILIES[family][unit]
        amounts.append(base / target_factor)
        units.append(unit)

    out = pd.Series(amounts, index=series.index, dtype="float64")
    if emit_unit_column:
        return ColumnOpResult(out, emit={emit_unit_column: pd.Series(units, index=series.index)})
    return out


def _is_strict_looking_number(value: str) -> bool:
    s = value.strip().replace(",", ".")
    return bool(re.match(r"^[+-]?\d+(\.\d+)?$", s))


__all__ = [
    "OP_REGISTRY",
    "OpSpec",
    "ColumnOpResult",
    "register_op",
    "get_op",
    "list_ops",
    "normalize_op",
    "apply_column_op",
    "apply_frame_op",
    "parse_dates_to_datetime",
    "parse_unit_scalar",
    "UNIT_FAMILIES",
    "CURRENCY_SYMBOLS",
    "DEFAULT_NA_TOKENS",
]
