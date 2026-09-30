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

import inspect
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from ._numparse import (
    parse_int_text,
    parse_number_text,
    parse_plain_number,
    split_number_unit,
)
from ._textparse import (
    detect_currency_text,
    map_exact,
    map_folded,
    normalize_unicode_text,
    title_case_text,
)
from ._util import is_pure_string, map_pure, safe_compile_regex
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
    #: Parameter names a recipe may set for this op (plus any documented aliases).
    known_params: frozenset[str] = frozenset()
    #: True when a mapping *is* the payload (``normalize_values``), so its keys
    #: are user data rather than parameter names.
    free_form: bool = False
    #: ``codegen(params, column) -> list[str]``: unindented statements that read and
    #: write ``df[column]`` (or ``df``, for a frame op) in exported standalone pandas.
    #: Without it, exporting a recipe that uses the op raises instead of skipping it.
    codegen: Callable[[dict, str], list[str]] | None = None
    #: Module-level source (imports, helper functions) the generated code needs.
    helpers: str = ""
    #: The op gives the same result chunk-by-chunk as on the whole frame. Streaming
    #: refuses every op that does not say so (default-deny).
    streamable: bool = False


OP_REGISTRY: dict[str, OpSpec] = {}


def register_op(
    name: str,
    *,
    scope: str = "column",
    coerce: Callable[[Any], dict] | None = None,
    compact: Callable[[dict], Any] | None = None,
    aliases: tuple[str, ...] = (),
    free_form: bool = False,
    codegen: Callable[[dict, str], list[str]] | None = None,
    helpers: str = "",
    streamable: bool = False,
) -> Callable[[Callable], Callable]:
    """Register a transform under ``name``. See module docstring for scopes.

    ``codegen`` / ``helpers`` let ``result.code`` export the op as standalone pandas;
    ``streamable=True`` promises the op is row-independent so ``stream_apply`` may run
    it chunk by chunk. Both are opt-in: an op that declares neither is refused by the
    exporter and by streaming rather than silently mishandled.

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
            known_params=frozenset(_signature_params(func)) | frozenset(aliases),
            free_form=free_form,
            codegen=codegen, helpers=helpers, streamable=streamable,
        )
        return func

    return decorator


def _signature_params(func: Callable) -> set[str]:
    """Keyword parameter names of an op, minus its leading series/frame argument."""
    try:
        params = [
            p
            for p in inspect.signature(func).parameters.values()
            if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
        ]
    except (TypeError, ValueError):  # pragma: no cover - builtins without signatures
        return set()
    return {p.name for p in params[1:]}


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
        # A recipe may name an op from an installed plugin nobody has imported yet
        # (e.g. `cleanframe apply` in CI). Discover plugins once before giving up.
        from .plugins import load_plugins

        load_plugins()
        spec = OP_REGISTRY.get(name)
    if spec is None:
        raise RecipeError(
            f"Unknown op {name!r}. Known ops: {', '.join(sorted(OP_REGISTRY))}. If it comes "
            "from a plugin, install that package or pass --plugin MODULE "
            "(or set CLEANFRAME_PLUGINS)."
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
    if isinstance(raw_params, dict) and not spec.free_form and spec.known_params:
        unknown = sorted(str(k) for k in raw_params if str(k) not in spec.known_params)
        if unknown:
            raise RecipeError(
                f"Op {name!r} got unknown parameter(s) {unknown}. Valid parameters: "
                f"{sorted(spec.known_params)}. A misspelled parameter would be ignored."
            )
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
    if isinstance(v, str):
        return False  # by far the commonest cell; skip the pandas call
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
    return is_pure_string(series)


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
    """Title-case string cells (``"new  YORK"`` -> ``"New York"``; ``"3rd st"`` stays ``"3rd St"``)."""
    return _apply_str(series, title_case_text)


_UNICODE_FORMS = frozenset({"NFC", "NFD", "NFKC", "NFKD"})


def _coerce_normalize_unicode(raw: Any) -> dict:
    form = raw.get("form", "NFC") if isinstance(raw, dict) else (raw or "NFC")
    form = str(form).upper()
    if form not in _UNICODE_FORMS:
        raise RecipeError(
            f"Op 'normalize_unicode' form must be one of {sorted(_UNICODE_FORMS)}, got {form!r}."
        )
    return {"form": form}


@register_op(
    "normalize_unicode",
    coerce=_coerce_normalize_unicode,
    compact=lambda p: _prune(p, {"form": "NFC"}),
    streamable=True,
    codegen=lambda params, column: [
        f"df[{column!r}] = _smap(df[{column!r}], "
        f"lambda v: normalize_unicode_text(v, {params.get('form', 'NFC')!r}))"
    ],
)
def normalize_unicode(series: pd.Series, form: str = "NFC") -> pd.Series:
    """Unicode-normalise text (NFC by default) and remove zero-width spaces / BOMs / NBSPs."""
    return _apply_str(series, lambda s: normalize_unicode_text(s, form))


@register_op("capitalize")
def capitalize(series: pd.Series) -> pd.Series:
    """Capitalize the first letter of each string cell."""
    return _apply_str(series, lambda s: s.strip().capitalize(), vec=lambda s: s.str.strip().str.capitalize())


def _coerce_symbols(raw: Any) -> dict:
    if raw is None:
        return {"symbols": []}
    if isinstance(raw, dict):
        raw = raw.get("symbols", [])
    if isinstance(raw, str):
        return {"symbols": [raw]}
    if isinstance(raw, (list, tuple)):
        bad = [s for s in raw if not isinstance(s, str)]
        if bad:
            raise RecipeError(
                f"Op 'remove_symbols' expects strings, got {bad!r}. Quote them in YAML "
                "so a number is not treated as the digit to delete."
            )
        return {"symbols": list(raw)}
    raise RecipeError(
        f"Op 'remove_symbols' expects a string or a list of strings, got "
        f"{type(raw).__name__}."
    )


@register_op(
    "remove_symbols", coerce=_coerce_symbols, compact=lambda p: p.get("symbols", [])
)
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
    pattern, repl = raw["pattern"], raw.get("repl", "")
    if not isinstance(pattern, str) or not isinstance(repl, str):
        raise RecipeError(
            f"Op 'replace' pattern and repl must be text, got {pattern!r} / {repl!r}. "
            "Quote yes/no/null and numbers."
        )
    regex = bool(raw.get("regex", True))
    if regex:
        try:
            compiled = safe_compile_regex(pattern)
        except ValueError as exc:
            raise RecipeError(f"Op 'replace' pattern rejected: {exc}") from exc
        refs = [int(g) for g in re.findall(r"(?<!\\)\\(\d+)", repl)]
        if refs and max(refs) > compiled.groups:
            raise RecipeError(
                f"Op 'replace' repl {repl!r} refers to group {max(refs)} but the pattern "
                f"has only {compiled.groups}."
            )
    return {"pattern": pattern, "repl": repl, "regex": regex}


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


def _require_string_tokens(tokens: Any) -> None:
    """YAML 1.1 reads an unquoted ``no`` / ``null`` / ``~`` as a bool / None."""
    for t in tokens or ():
        if not isinstance(t, str):
            raise RecipeError(
                f"Op 'to_na' token {t!r} is not text. YAML reads unquoted yes/no/on/off/null/~ "
                "and bare numbers as non-strings — quote it (e.g. \"no\")."
            )


def _coerce_to_na(raw: Any) -> dict:
    if raw is None:
        return {"tokens": None, "case_insensitive": True}
    if isinstance(raw, dict):
        tokens = raw.get("tokens")
        if isinstance(tokens, str):
            tokens = [tokens]
        if tokens is not None and not isinstance(tokens, (list, tuple)):
            raise RecipeError("Op 'to_na' tokens must be a string or a list of strings.")
        _require_string_tokens(tokens)
        return {
            "tokens": list(tokens) if tokens is not None else None,
            "case_insensitive": bool(raw.get("case_insensitive", True)),
        }
    if isinstance(raw, (list, tuple)):
        _require_string_tokens(raw)
        return {"tokens": list(raw), "case_insensitive": True}
    _require_string_tokens([raw])
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


#: A trailing extension is not part of the number; fusing it corrupts both.
_PHONE_EXT_RE = re.compile(r"[\s,;]*(?:ext|extn|x|#)\.?\s*\d+\s*$", re.IGNORECASE)


def _phone_text(value: Any) -> str:
    """Stringify a phone cell without inventing digits.

    A phone column holding one blank cell is read as float64, and ``str(9876543210.0)``
    would append a spurious trailing zero once the dot is stripped.
    """
    if isinstance(value, float) and float(value).is_integer():
        return str(int(value))
    return str(value)


def _normalize_phone_scalar(value: Any, default_cc: str | None) -> Any:
    if _is_na(value):
        return value
    if isinstance(value, bool):
        return np.nan
    s = _PHONE_EXT_RE.sub("", _phone_text(value))
    had_plus = s.strip().startswith("+")
    digits = re.sub(r"\D", "", s)
    if not digits:
        return np.nan
    if had_plus:
        return "+" + digits
    if digits.startswith("00"):
        # International call prefix: 0044 20 7946 0958 is +44 20 7946 0958, whatever
        # the default country code is.
        return "+" + digits[2:] if digits[2:] else np.nan
    if default_cc:
        cc = re.sub(r"\D", "", str(default_cc))
        if cc and digits.startswith(cc):
            return "+" + digits
        local = digits.lstrip("0")
        if len(local) < 7:
            return digits  # too short to be a phone number: never invent a country code
        return "+" + cc + local
    return digits


@register_op(
    "normalize_phone",
    aliases=("country_code", "region"),
    coerce=_coerce_normalize_phone,
    compact=lambda p: _prune(p, {"default_country_code": None}),
)
def normalize_phone(series: pd.Series, default_country_code: str | None = None) -> pd.Series:
    """Best-effort phone normalisation: keep an existing ``+``, strip separators.

    With ``default_country_code`` (e.g. ``"+91"``), local numbers gain the country
    code (dropping a national trunk ``0``). This is intentionally lightweight — for
    strict E.164 across many regions, plug in a libphonenumber-backed detector.
    """
    return map_pure(series, lambda v: _normalize_phone_scalar(v, default_country_code))


# ---------------------------------------------------------------------------
# Number ops
# ---------------------------------------------------------------------------
def _coerce_parse_number(raw: Any) -> dict:
    raw = raw or {}
    if not isinstance(raw, dict):
        raise RecipeError("Op 'parse_number' expects a mapping of parameters.")
    decimal, thousands = raw.get("decimal", "."), raw.get("thousands", ",")
    if not isinstance(decimal, str) or len(decimal) != 1:
        raise RecipeError(f"Op 'parse_number' decimal must be one character, got {decimal!r}.")
    if not isinstance(thousands, str) or len(thousands) > 1:
        raise RecipeError(
            f"Op 'parse_number' thousands must be one character or \"\", got {thousands!r}."
        )
    if decimal == thousands:
        raise RecipeError("Op 'parse_number' decimal and thousands must differ.")
    return {
        "decimal": decimal,
        "thousands": thousands,
        "symbols": [str(x) for x in (raw.get("symbols") or [])],
    }


def _parse_number_scalar(value: Any, decimal: str, thousands: str, symbols: list[str]) -> float:
    """One cell → float, or NaN when it is not a single well-formed number.

    The implementation lives in :mod:`cleanframe._numparse` so the generated
    standalone code can embed the very same source.
    """
    return parse_number_text(value, decimal, thousands, tuple(symbols))


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
    return map_pure(series, lambda v: _parse_number_scalar(v, decimal, thousands, syms))


#: Every accepted ``cast`` target. A typo here used to load and fail at replay.
CAST_TARGETS = frozenset(
    {
        "float", "float64", "number", "int", "integer", "int64", "string", "str",
        "text", "bool", "boolean", "datetime", "date", "category",
    }
)


def _check_cast_target(to: Any) -> str:
    if not isinstance(to, str) or to.strip().lower() not in CAST_TARGETS:
        raise RecipeError(
            f"Op 'cast' has unknown target {to!r}. Valid targets: "
            f"{', '.join(sorted(CAST_TARGETS))}."
        )
    return to


def _coerce_cast(raw: Any) -> dict:
    if isinstance(raw, dict):
        if "to" not in raw:
            raise RecipeError("Op 'cast' requires a 'to' target type, e.g. `cast: float`.")
        return {"to": _check_cast_target(raw["to"])}
    if isinstance(raw, str):
        return {"to": _check_cast_target(raw)}
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
        if pd.api.types.is_object_dtype(series.dtype) or pd.api.types.is_string_dtype(
            series.dtype
        ):
            # Exact per-cell conversion: float64 would round IDs above 2**53.
            return pd.Series(
                pd.array([parse_int_text(v) for v in series], dtype="Int64"),
                index=series.index,
                name=series.name,
            )
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


def _coerce_round(raw: Any) -> dict:
    if isinstance(raw, dict):
        raw = raw.get("decimals", 0)
    if raw is None:
        raw = 0
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise RecipeError(f"Op 'round' expects a number of decimals, got {raw!r}.")
    return {"decimals": int(raw)}


@register_op(
    "round",
    coerce=_coerce_round,
    compact=lambda p: p.get("decimals", 0),
)
def round_op(series: pd.Series, decimals: int = 0) -> pd.Series:
    """Round a numeric column to ``decimals`` places."""
    return pd.to_numeric(series, errors="coerce").round(decimals)


# ---------------------------------------------------------------------------
# Date ops
# ---------------------------------------------------------------------------
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
    # appended (never inserted): earlier formats keep winning on existing recipes
    "%b %d, %y",
    "%B %d, %y",
    "%d %b, %Y",
    "%d.%m.%y",
    "%Y.%m.%d",
]


def _coerce_parse_date(raw: Any) -> dict:
    raw = raw or {}
    if not isinstance(raw, dict):
        raise RecipeError("Op 'parse_date' expects a mapping of parameters.")
    # `allowed` is the README's alias for `formats`.
    formats = raw.get("formats", raw.get("allowed"))
    if isinstance(formats, str):
        raise RecipeError(
            "Op 'parse_date' formats must be a list, e.g. formats: ['%d/%m/%Y'] — a bare "
            "string would be read one character at a time."
        )
    if formats is not None and not isinstance(formats, (list, tuple)):
        raise RecipeError(
            f"Op 'parse_date' formats must be a list of strftime patterns, got "
            f"{type(formats).__name__}."
        )
    if formats and any(not isinstance(f, str) for f in formats):
        raise RecipeError("Op 'parse_date' formats must all be strings.")
    return {
        "formats": list(formats) if formats else None,
        "dayfirst": bool(raw.get("dayfirst", False)),
        "yearfirst": bool(raw.get("yearfirst", False)),
        "output": raw.get("output", "%Y-%m-%d"),
    }


_DAYFIRST_SLASH = frozenset(
    {"%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y", "%d-%m-%y", "%d.%m.%y"}
)
_MONTHFIRST_SLASH = frozenset({"%m/%d/%Y", "%m-%d-%Y", "%m/%d/%y"})


def _reconcile_date_formats(formats: list[str], dayfirst: bool) -> list[str]:
    """Drop the non-preferred d/m vs m/d family so ambiguous cells cannot swap."""
    has_day = any(f in _DAYFIRST_SLASH for f in formats)
    has_month = any(f in _MONTHFIRST_SLASH for f in formats)
    if not (has_day and has_month):
        return formats
    drop = _MONTHFIRST_SLASH if dayfirst else _DAYFIRST_SLASH
    return [f for f in formats if f not in drop]


def _naive_datetimes(series: pd.Series) -> pd.Series:
    """Force a parse result to tz-naive ``datetime64[ns]``.

    A value carrying an offset (``2024-01-01T10:00:00Z``) parses to a tz-aware series,
    which cannot be stored in a tz-naive one: older pandas silently turns the column to
    ``object`` and the following ``.dt`` access then fails. A column with one offset
    keeps its local wall-clock; a column mixing offsets has no single wall-clock, so
    it is converted to UTC.
    """
    if getattr(series.dtype, "tz", None) is not None:
        # Keep the wall-clock the file shows: 2024-01-01T02:00+05:30 is 2024-01-01,
        # not the UTC calendar day before it.
        return series.dt.tz_localize(None)
    if not pd.api.types.is_datetime64_any_dtype(series.dtype):
        converted = pd.to_datetime(series, errors="coerce", utc=True)
        if getattr(converted.dtype, "tz", None) is not None:
            converted = converted.dt.tz_convert("UTC").dt.tz_localize(None)
        return converted
    return series


def parse_dates_to_datetime(
    series: pd.Series,
    formats: list[str] | None,
    dayfirst: bool = False,
    yearfirst: bool = False,
) -> pd.Series:
    """Parse to a datetime64 Series (NaT where nothing matched). Shared with drift."""
    if formats:
        # Explicit formats make each cell's result depend only on the cell, so a
        # repetitive text column is parsed once per distinct value. (Format-less
        # parsing infers from the whole column and must see all of it.)
        from . import _util

        n = len(series)
        if n >= _util._UNIQUE_MAP_MIN_ROWS and is_pure_string(series):
            codes, uniques = pd.factorize(series)
            if len(uniques) <= n * _util._UNIQUE_MAP_MAX_SHARE:
                probe = pd.Series([*uniques, np.nan], dtype=object)
                parsed = _parse_dates_core(probe, formats, dayfirst, yearfirst)
                out = parsed.iloc[codes]  # -1 (missing) selects the trailing NaT
                out.index = series.index
                out.name = series.name
                return out
    return _parse_dates_core(series, formats, dayfirst, yearfirst)


def _parse_dates_core(
    series: pd.Series,
    formats: list[str] | None,
    dayfirst: bool = False,
    yearfirst: bool = False,
) -> pd.Series:
    """The parsing algorithm itself. Self-contained on purpose: :mod:`cleanframe.codegen`
    embeds this function's source verbatim into exported code."""
    flex_fallback = False
    if not formats:
        # No explicit formats: coalesce over the common formats first — deterministic
        # and NOT order-dependent, unlike a bare format-less pd.to_datetime which locks
        # onto the first row's inferred format and silently nulls otherwise-valid dates
        # (and behaves differently across pandas versions).
        formats = list(COMMON_DATE_FORMATS)
        flex_fallback = True

    formats = _reconcile_date_formats(list(formats), dayfirst=bool(dayfirst))

    result = pd.Series(pd.NaT, index=series.index, dtype="datetime64[ns]")
    for fmt in formats:
        mask = result.isna() & series.notna()
        if not mask.any():
            break
        parsed = _naive_datetimes(pd.to_datetime(series[mask], format=fmt, errors="coerce"))
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
            result.loc[remaining] = _naive_datetimes(flex)
    return _naive_datetimes(result)


@register_op(
    "parse_date",
    aliases=("allowed",),
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


@register_op(
    "normalize_values",
    coerce=_coerce_normalize_values,
    compact=_compact_normalize_values,
    free_form=True,
)
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
            return map_folded(folded, v)

        return map_pure(series, fn)

    def fn_exact(v: Any) -> Any:
        if _is_na(v):
            return v
        return map_exact(mapping, v)

    return map_pure(series, fn_exact)


# ---------------------------------------------------------------------------
# Currency extraction (column op that emits an extra column)
# ---------------------------------------------------------------------------
#: Symbol / trailing-code -> ISO 4217 code. Deliberately small and explicit.
CURRENCY_SYMBOLS = dict(
    sorted(
        {
            "₹": "INR",
            "$": "USD",
            "US$": "USD",
            "€": "EUR",
            "£": "GBP",
            "¥": "JPY",
            "₩": "KRW",
            "₽": "RUB",
            "₺": "TRY",
            "₫": "VND",
            "₱": "PHP",
            "₪": "ILS",
            "฿": "THB",
            "R$": "BRL",
            "A$": "AUD",
            "AU$": "AUD",
            "C$": "CAD",
            "CA$": "CAD",
            "HK$": "HKD",
            "NZ$": "NZD",
            "S$": "SGD",
            "AR$": "ARS",
            "MX$": "MXN",
            "NT$": "TWD",
            "CL$": "CLP",
            "CO$": "COP",
            "¢": "USD",
        }.items(),
        # Longest first: "R$" must be tried before "$", or every dollar-family
        # currency reads as USD.
        key=lambda kv: -len(kv[0]),
    )
)
#: Symbols spelled with letters, matched only when not glued to another letter.
CURRENCY_WORDS = {"Rs": "INR"}
_CURRENCY_ITEMS = tuple(CURRENCY_SYMBOLS.items())
_CURRENCY_WORD_ITEMS = tuple(CURRENCY_WORDS.items())


def _coerce_extract_currency(raw: Any) -> dict:
    raw = raw or {}
    if isinstance(raw, str):
        return {"to": raw, "default": None}
    if not isinstance(raw, dict):
        raise RecipeError("Op 'extract_currency' expects a target column name or mapping.")
    to = raw.get("to")
    if to is not None and not isinstance(to, str):
        raise RecipeError("Op 'extract_currency' target column name must be a string.")
    return {"to": to, "default": raw.get("default")}


def _detect_currency_scalar(value: Any, default: str | None) -> Any:
    if _is_na(value):
        return default if default is not None else np.nan
    code = detect_currency_text(str(value), _CURRENCY_ITEMS, _CURRENCY_WORD_ITEMS, _KNOWN_CODES)
    if code is not None:
        return code
    return default if default is not None else np.nan


_KNOWN_CODES = frozenset(CURRENCY_SYMBOLS.values()) | frozenset(CURRENCY_WORDS.values()) | {
    "USD", "EUR", "GBP", "INR", "JPY", "CNY", "AUD", "CAD", "CHF", "SGD",
    "HKD", "NZD", "SEK", "NOK", "DKK", "ZAR", "AED", "SAR", "KRW", "RUB", "BRL",
    "MXN", "ARS", "CLP", "COP", "TWD", "TRY", "PLN", "CZK", "HUF", "ILS", "THB",
    "MYR", "IDR", "PHP", "VND", "NGN", "EGP", "KES", "PKR", "BDT", "LKR", "UAH", "RON",
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
    codes = map_pure(series, lambda v: _detect_currency_scalar(v, default))
    codes.name = target
    return ColumnOpResult(series=series, emit={target: codes})


# ---------------------------------------------------------------------------
# Frame ops
# ---------------------------------------------------------------------------
def _column_names(values: Any, op: str) -> list[str]:
    """Column names from a recipe list. YAML reads a bare ``2024`` as an int (and ``yes``
    as a bool); an int is a perfectly good column name, a bool means the user forgot to quote."""
    names = []
    for v in values:
        if isinstance(v, bool) or v is None:
            raise RecipeError(
                f"Op {op!r} column name {v!r} is not text. YAML reads unquoted yes/no/null as "
                "booleans/None - quote it."
            )
        names.append(v if isinstance(v, str) else str(v))
    return names


def _coerce_dedup(raw: Any) -> dict:
    raw = raw or {}
    if isinstance(raw, (list, tuple)):
        return {"subset": _column_names(raw, "dedup"), "keep": "first", "ignore_case": False}
    if not isinstance(raw, dict):
        raise RecipeError("Op 'dedup' expects a mapping or a list of subset columns.")
    keep = raw.get("keep", "first")
    if keep is False or str(keep).lower() == "false":
        keep = False
    if keep not in ("first", "last", False):
        raise RecipeError(
            f"Op 'dedup' keep must be 'first', 'last' or false, got {keep!r}."
        )
    subset = raw.get("subset")
    if isinstance(subset, str):
        subset = [subset]
    if subset is not None and not isinstance(subset, (list, tuple)):
        raise RecipeError("Op 'dedup' subset must be a column name or a list of names.")
    return {
        "subset": _column_names(subset, "dedup") if subset else None,
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
    key_cols = subset if subset else list(df.columns)
    key = df[key_cols].apply(_dedup_key_column, ignore_case=ignore_case)
    mask = ~key.duplicated(keep=keep if keep is not False else False)
    return df[mask]


def _dedup_key_column(col: pd.Series, ignore_case: bool = False) -> pd.Series:
    """The comparison key for one column: case-folded strings when asked, and booleans
    kept distinct from the numbers they compare equal to (``1 == True == 1.0``)."""
    if not pd.api.types.is_object_dtype(col.dtype):
        return col
    mixed = pd.api.types.infer_dtype(col, skipna=True).startswith("mixed")

    def fn(v: Any) -> Any:
        if isinstance(v, bool):
            return ("bool", v)
        if ignore_case and isinstance(v, str):
            return v.strip().casefold()
        return v

    if not (ignore_case or mixed):
        return col
    return col.map(fn)


def _coerce_drop_columns(raw: Any) -> dict:
    if isinstance(raw, str):
        return {"columns": [raw]}
    if isinstance(raw, (list, tuple)):
        cols = list(raw)
    elif isinstance(raw, dict):
        cols = list(raw.get("columns", []))
    else:
        raise RecipeError("Op 'drop_columns' expects a column name or list.")
    return {"columns": _column_names(cols, "drop_columns")}


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
_UNIT_ALIASES = {
    "litre": "l", "liter": "l", "litres": "l", "liters": "l",
    "gram": "g", "grams": "g", "gm": "g", "gms": "g",
    "kilo": "kg", "kilos": "kg", "kgs": "kg", "kilogram": "kg", "kilograms": "kg",
    "milligram": "mg", "milligrams": "mg",
    "lbs": "lb", "pound": "lb", "pounds": "lb", "ounce": "oz", "ounces": "oz",
    "meter": "m", "meters": "m", "metre": "m", "metres": "m",
    "kilometer": "km", "kilometers": "km", "kilometre": "km", "kilometres": "km",
    "centimeter": "cm", "centimeters": "cm", "centimetre": "cm", "centimetres": "cm",
    "millimeter": "mm", "millimeters": "mm", "millimetre": "mm", "millimetres": "mm",
    "inch": "in", "inches": "in", "foot": "ft", "feet": "ft", "yard": "yd", "yards": "yd",
    "milliliter": "ml", "milliliters": "ml", "millilitre": "ml", "millilitres": "ml",
    "gallon": "gal", "gallons": "gal",
}


def parse_unit_scalar(value: Any) -> tuple[float, str] | None:
    """Parse ``"5kg"`` / ``"5000 g"`` → ``(5.0, "kg")``. Returns ``None`` if not a unit value.

    An ambiguous number (``"1,500 g"`` — 1500 or 1.5?) also returns ``None``, so it
    is reported as unparseable instead of being silently guessed.
    """
    if _is_na(value):
        return None
    split = split_number_unit(value)
    if split is None:
        return None
    number, unit = split
    unit = _UNIT_ALIASES.get(unit, unit)
    if unit not in _UNIT_TO_FAMILY:
        return None
    return number, unit


def _check_unit(to: str) -> str:
    unit = _UNIT_ALIASES.get(str(to).casefold(), str(to).casefold())
    if unit not in _UNIT_TO_FAMILY:
        raise RecipeError(
            f"Op 'normalize_unit' has unknown target unit {to!r}. Known units: "
            f"{', '.join(sorted(_UNIT_TO_FAMILY))}."
        )
    return unit


def _coerce_normalize_unit(raw: Any) -> dict:
    if isinstance(raw, str):
        return {"to": _check_unit(raw), "emit_unit_column": None}
    raw = raw or {}
    if not isinstance(raw, dict):
        raise RecipeError("Op 'normalize_unit' expects a target unit string or mapping.")
    to = _check_unit(raw.get("to", "g"))
    emit = raw.get("emit_unit_column")
    if emit is not None and not isinstance(emit, str):
        raise RecipeError("Op 'normalize_unit' emit_unit_column must be a column name.")
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

    def cell(v: Any) -> tuple[float, Any]:
        """(amount in the target unit, source unit) for one cell; pure, so shareable."""
        parsed = parse_unit_scalar(v)
        if parsed is None:
            if isinstance(v, (int, float)) and not isinstance(v, bool) and not _is_na(v):
                return float(v), to
            if isinstance(v, str) and (bare := parse_plain_number(v.strip())) is not None:
                return bare, to
            return np.nan, None
        amount, unit = parsed
        family = _UNIT_TO_FAMILY[unit]
        if family != target_family:
            return np.nan, unit
        return amount * UNIT_FAMILIES[family][unit] / target_factor, unit

    pairs = map_pure(series, cell).tolist()
    amounts = [p[0] for p in pairs]
    units = [p[1] for p in pairs]

    out = pd.Series(amounts, index=series.index, dtype="float64", name=series.name)
    if emit_unit_column:
        return ColumnOpResult(out, emit={emit_unit_column: pd.Series(units, index=series.index)})
    return out


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
    "COMMON_DATE_FORMATS",
    "CAST_TARGETS",
    "UNIT_FAMILIES",
    "CURRENCY_SYMBOLS",
    "CURRENCY_WORDS",
    "DEFAULT_NA_TOKENS",
]
