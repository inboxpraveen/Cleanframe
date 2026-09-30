"""Export a recipe to standalone, readable pandas — no CleanFrame dependency.

``result.code.save("clean_customers.py")`` produces a plain ``clean(df)`` function
you can read, diff, and drop into a pipeline that never imports CleanFrame.

Fidelity is a load-bearing invariant: the generated code must reproduce the
executor's output *exactly*. To keep the two from drifting, the lookup tables
(currency symbols, NA tokens, unit factors, date formats) are rendered here from
the single source of truth in :mod:`cleanframe.ops`, and the emitted helper bodies
mirror the executor's scalar logic. ``tests/test_wave1_codegen.py`` locks this by
running representative recipes through both paths and asserting frame equality.

Validation is reproduced for the built-in checks (quarantine/drop filter the
primary frame; ``null`` blanks the cell; ``error`` raises). The quarantine *side*
frame is a CleanFrame runtime concept and cannot round-trip through a ``df -> df``
function, so only the cleaned output is reproduced.
"""

from __future__ import annotations

import keyword
import re
from collections.abc import Callable

from .errors import CleanFrameError
from .recipe import Recipe, ValidationRule
from .types import Op

_UNSAFE_COMMENT_RE = re.compile(r"[\r\n]+")


def _comment(text: object) -> str:
    """One-line, code-safe rendering of user text for a generated comment.

    A column name containing a newline would otherwise continue the generated module
    on the next line — outside the comment — as executable code.
    """
    return _UNSAFE_COMMENT_RE.sub(" ", str(text))[:120]


# ---------------------------------------------------------------------------
# Constant tables rendered from the executor's single source of truth
# ---------------------------------------------------------------------------
def _constants_source() -> str:
    from .ops import (
        _KNOWN_CODES,
        _UNIT_ALIASES,
        _UNIT_TO_FAMILY,
        COMMON_DATE_FORMATS,
        CURRENCY_SYMBOLS,
        CURRENCY_WORDS,
        DEFAULT_NA_TOKENS,
        UNIT_FAMILIES,
    )

    unit_factors = {u: f for fam in UNIT_FAMILIES.values() for u, f in fam.items()}
    na_tokens = sorted({t.casefold() for t in DEFAULT_NA_TOKENS})  # includes '' (H9)
    return "\n".join(
        [
            f"_NA_TOKENS = set({na_tokens!r})",
            f"_CURRENCY_SYMBOLS = {tuple(CURRENCY_SYMBOLS.items())!r}",
            f"_CURRENCY_WORDS = {tuple(CURRENCY_WORDS.items())!r}",
            f"_KNOWN_CODES = frozenset({sorted(_KNOWN_CODES)!r})",
            f"_UNIT_FACTORS = {unit_factors!r}",
            f"_UNIT_FAMILY = {dict(_UNIT_TO_FAMILY)!r}",
            f"_UNIT_ALIASES = {dict(_UNIT_ALIASES)!r}",
            f"_COMMON_DATE_FORMATS = {list(COMMON_DATE_FORMATS)!r}",
            r"_PHONE_EXT_RE = re.compile(r'[\s,;]*(?:ext|extn|x|#)\.?\s*\d+\s*$', re.IGNORECASE)",
            r"_STRICT_NUM_RE = re.compile(r'^[+-]?\d+(\.\d+)?$')",
            r"_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')",
            r"_URL_RE = re.compile(r'^(https?://|www\.)\S+$', re.IGNORECASE)",
            "_TRUE_TOKENS = {'true', 't', 'yes', 'y', '1'}",
            "_FALSE_TOKENS = {'false', 'f', 'no', 'n', '0'}",
        ]
    )


def _date_source() -> str:
    """The executor's date-parsing helpers, embedded verbatim (one implementation)."""
    import inspect

    from . import ops

    parts = [
        f"_DAYFIRST_SLASH = {set(ops._DAYFIRST_SLASH)!r}",
        f"_MONTHFIRST_SLASH = {set(ops._MONTHFIRST_SLASH)!r}",
        f"COMMON_DATE_FORMATS = {list(ops.COMMON_DATE_FORMATS)!r}",
        inspect.getsource(ops._reconcile_date_formats),
        inspect.getsource(ops._naive_datetimes),
        inspect.getsource(ops._parse_dates_core)
        .replace("def _parse_dates_core(", "def _parse_dates_to_datetime(")
        .replace("formats: list[str] | None,", "formats: list[str] | None = None,"),
    ]
    return "\n" + "\n\n".join(parts) + "\n"


def _plugin_helpers(recipe: Recipe) -> str:
    """Module-level source declared by the plugin ops this recipe uses (once each)."""
    from .ops import OP_REGISTRY

    seen: list[str] = []
    ops = [op for c in recipe.columns for op in c.ops] + list(recipe.frame_ops)
    for op in ops:
        spec = OP_REGISTRY.get(op.name)
        if spec is not None and spec.helpers and spec.helpers not in seen:
            seen.append(spec.helpers)
    return "\n".join(seen)


def _textparse_source() -> str:
    """The executor's text helpers, embedded verbatim."""
    import inspect

    from . import _textparse

    src = inspect.getsource(_textparse)
    return "\n" + src[src.index("_WORD_RE") :]


def _numparse_source() -> str:
    """The executor's number parser, embedded verbatim (one implementation, not two)."""
    import inspect

    from . import _numparse

    src = inspect.getsource(_numparse)
    body = src[src.index("_ZERO_WIDTH") :]  # skip the module docstring and imports
    return "\n" + body


_DOC_AND_IMPORTS = '''"""Auto-generated by CleanFrame. Deterministic, dependency-free pandas.

Edit freely — this file has no third-party dependency. Regenerate with
``result.code.save(...)`` if you change the recipe.
"""

import re
import unicodedata
import warnings
from functools import lru_cache

import numpy as np
import pandas as pd
'''

# Helper bodies mirror cleanframe.ops / cleanframe.validate scalar logic exactly.
_HELPERS = '''
def _smap(series, fn):
    """Apply fn to string cells only; leave NaN and non-strings untouched."""
    return series.map(lambda v: fn(v) if isinstance(v, str) else v)


def _to_na(series, tokens=None, case_insensitive=True):
    if case_insensitive:
        toks = _NA_TOKENS if tokens is None else {str(t).strip().casefold() for t in tokens}
        return _smap(series, lambda v: np.nan if v.strip().casefold() in toks else v)
    raw = _NA_TOKENS if tokens is None else {str(t) for t in tokens}
    return _smap(series, lambda v: np.nan if v in raw else v)


def _dedup_key_column(col, ignore_case=False):
    if col.dtype != object:
        return col
    mixed = pd.api.types.infer_dtype(col, skipna=True).startswith("mixed")
    if not (ignore_case or mixed):
        return col

    def fn(v):
        if isinstance(v, bool):
            return ("bool", v)
        if ignore_case and isinstance(v, str):
            return v.strip().casefold()
        return v

    return col.map(fn)


def _is_na(v):
    if v is None:
        return True
    if isinstance(v, float):
        return v != v
    try:
        return bool(pd.isna(v))
    except (TypeError, ValueError):
        return False


def _map_values(series, mapping, case_insensitive=False):
    """normalize_values: applies to every non-missing cell, not just strings."""
    if case_insensitive:
        folded = {str(k).strip().casefold(): v for k, v in mapping.items()}
        return series.map(lambda v: v if _is_na(v) else map_folded(folded, v))
    return series.map(lambda v: v if _is_na(v) else map_exact(mapping, v))


def _parse_number(series, decimal=".", thousands=",", symbols=()):
    syms = tuple(symbols)
    return series.map(lambda v: parse_number_text(v, decimal, thousands, syms))


def _cast_int(series):
    if series.dtype == object or str(series.dtype) in ("string", "str"):
        return pd.Series(
            pd.array([parse_int_text(v) for v in series], dtype="Int64"),
            index=series.index,
            name=series.name,
        )
    return pd.to_numeric(series, errors="coerce").round().astype("Int64")


def _parse_date(series, formats=None, dayfirst=False, yearfirst=False, output="%Y-%m-%d"):
    dt = _parse_dates_to_datetime(series, formats, dayfirst=dayfirst, yearfirst=yearfirst)
    if str(output).lower() in ("datetime", "raw", "none"):
        return dt
    fmt = "%Y-%m-%d" if str(output).lower() in ("iso", "date") else output
    return dt.dt.strftime(fmt).where(dt.notna(), np.nan)


def _detect_currency(v, default=None):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return default if default is not None else np.nan
    code = detect_currency_text(str(v), _CURRENCY_SYMBOLS, _CURRENCY_WORDS, _KNOWN_CODES)
    if code is not None:
        return code
    return default if default is not None else np.nan


def _phone_text(v):
    """Stringify a phone cell without inventing digits (a float column has a '.0')."""
    if isinstance(v, float) and float(v).is_integer():
        return str(int(v))
    return str(v)


def _normalize_phone(series, default_country_code=None):
    def one(v):
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return v
        if isinstance(v, bool):
            return np.nan
        s = _PHONE_EXT_RE.sub("", _phone_text(v))
        plus = s.strip().startswith("+")
        digits = re.sub(r"\\D", "", s)
        if not digits:
            return np.nan
        if plus:
            return "+" + digits  # noqa: RET504
        if digits.startswith("00"):
            return "+" + digits[2:] if digits[2:] else np.nan
        if default_country_code:
            cc = re.sub(r"\\D", "", str(default_country_code))
            if cc and digits.startswith(cc):
                return "+" + digits
            local = digits.lstrip("0")
            if len(local) < 7:
                return digits
            return "+" + cc + local
        return digits

    return series.map(one)


def _parse_unit_scalar(v):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    if isinstance(v, (int, float, bool)):
        return None
    split = split_number_unit(v)
    if split is None:
        return None
    number, unit_s = split
    unit_s = _UNIT_ALIASES.get(unit_s, unit_s)
    if unit_s not in _UNIT_FAMILY:
        return None
    return number, unit_s


def _normalize_unit(series, to="g", emit_unit_column=False):
    to = _UNIT_ALIASES.get(str(to).casefold(), str(to).casefold())
    target_fam = _UNIT_FAMILY[to]
    target_factor = _UNIT_FACTORS[to]
    amounts, units = [], []
    for v in series.tolist():
        parsed = _parse_unit_scalar(v)
        if parsed is None:
            if isinstance(v, (int, float)) and not isinstance(v, bool) and not (isinstance(v, float) and np.isnan(v)):
                amounts.append(float(v))
                units.append(to)
            elif isinstance(v, str) and (bare := parse_plain_number(v.strip())) is not None:
                amounts.append(bare)
                units.append(to)
            else:
                amounts.append(np.nan)
                units.append(None)
            continue
        amount, unit = parsed
        if _UNIT_FAMILY[unit] != target_fam:
            amounts.append(np.nan)
            units.append(unit)
            continue
        amounts.append(amount * _UNIT_FACTORS[unit] / target_factor)
        units.append(unit)
    out = pd.Series(amounts, index=series.index, dtype="float64")
    if emit_unit_column:
        return out, pd.Series(units, index=series.index)
    return out


def _to_bool(series):
    def one(v):
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return pd.NA
        if isinstance(v, bool):
            return v
        t = str(v).strip().casefold()
        if t in _TRUE_TOKENS:
            return True
        if t in _FALSE_TOKENS:
            return False
        return pd.NA

    return series.map(one).astype("boolean")


# -- validation pass-masks (True = passes), mirroring cleanframe.validate ------
def _v_not_null(s):
    return s.notna()


def _v_unique(s):
    return ~(s.duplicated(keep=False) & s.notna())


def _v_valid_email(s):
    return s.isna() | s.map(lambda v: bool(_EMAIL_RE.match(str(v).strip().lower())))


def _v_valid_url(s):
    return s.isna() | s.map(lambda v: bool(_URL_RE.match(str(v).strip())))


def _v_valid_phone(s):
    return s.isna() | s.map(lambda v: 7 <= len(re.sub(r"\\D", "", str(v))) <= 15)


_CMP_OPS = {
    ">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b, ">": lambda a, b: a > b,
    "<": lambda a, b: a < b, "==": lambda a, b: a == b, "!=": lambda a, b: a != b,
}


def _v_cmp(s, op, threshold):
    numeric = pd.to_numeric(s, errors="coerce")
    satisfies = _CMP_OPS[op](numeric, threshold).fillna(False).astype(bool)
    return s.isna() | (numeric.notna() & satisfies)


def _v_in(s, values):
    _BOOL_SPELLINGS = {
        True: ("True", "true", "TRUE", "Yes", "yes", "YES", "On", "on", "Y", "y", "1"),
        False: ("False", "false", "FALSE", "No", "no", "NO", "Off", "off", "N", "n", "0"),
    }
    as_str = set()
    for v in values:
        if isinstance(v, bool):
            as_str.update(_BOOL_SPELLINGS[v])
        else:
            as_str.add(str(v))
    return s.isna() | s.isin(values) | s.astype(str).isin(as_str)


def _v_matches(s, pattern):
    if len(pattern) > 500:
        raise ValueError(f"Regex pattern exceeds limit of 500 characters ({len(pattern)}).")
    compiled = re.compile(pattern)
    return s.isna() | s.map(lambda v: bool(compiled.search(str(v))))
'''


def _col(name: str) -> str:
    return f"df[{name!r}]"


# -- per-op code emitters ----------------------------------------------------
def _remove_symbols(params: dict, c: str) -> list[str]:
    chain = "".join(f".replace({s!r}, '')" for s in params.get("symbols", []))
    return [f"    {_col(c)} = _smap({_col(c)}, lambda v: v{chain})"]


def _replace(params: dict, c: str) -> list[str]:
    pattern = params["pattern"]
    if params.get("regex", True):
        # Fail at export time with the same ReDoS / length guards the executor uses.
        from ._util import safe_compile_regex

        safe_compile_regex(pattern)
        return [
            f"    {_col(c)} = _smap({_col(c)}, "
            f"lambda v: re.sub({pattern!r}, {params.get('repl', '')!r}, v))"
        ]
    return [
        f"    {_col(c)} = _smap({_col(c)}, "
        f"lambda v: v.replace({pattern!r}, {params.get('repl', '')!r}))"
    ]


def _cast(params: dict, c: str) -> list[str]:
    to = str(params["to"]).lower()
    col = _col(c)
    expr = {
        "float": f"pd.to_numeric({col}, errors='coerce').astype('float64')",
        "float64": f"pd.to_numeric({col}, errors='coerce').astype('float64')",
        "number": f"pd.to_numeric({col}, errors='coerce').astype('float64')",
        "int": f"_cast_int({col})",
        "integer": f"_cast_int({col})",
        "int64": f"_cast_int({col})",
        "string": f"{col}.astype('string')",
        "str": f"{col}.astype('string')",
        "text": f"{col}.astype('string')",
        "bool": f"_to_bool({col})",
        "boolean": f"_to_bool({col})",
        "datetime": f"_parse_dates_to_datetime({col})",
        "date": f"_parse_dates_to_datetime({col})",
        "category": f"{col}.astype('category')",
    }.get(to)
    if expr is None:
        return [f"    # NOTE: cast to {to!r} not reproduced"]
    return [f"    {col} = {expr}"]


def _normalize_values(params: dict, c: str) -> list[str]:
    mapping = params.get("map", {})
    ci = ", case_insensitive=True" if params.get("case_insensitive") else ""
    return [f"    {_col(c)} = _map_values({_col(c)}, {mapping!r}{ci})"]


def _parse_date_gen(params: dict, c: str) -> list[str]:
    args = []
    if params.get("formats"):
        args.append(f"formats={list(params['formats'])!r}")
    if params.get("dayfirst"):
        args.append("dayfirst=True")
    if params.get("yearfirst"):
        args.append("yearfirst=True")
    out = params.get("output", "%Y-%m-%d")
    if out != "%Y-%m-%d":
        args.append(f"output={out!r}")
    tail = (", " + ", ".join(args)) if args else ""
    return [f"    {_col(c)} = _parse_date({_col(c)}{tail})"]


def _parse_number_gen(params: dict, c: str) -> list[str]:
    args = []
    if params.get("decimal", ".") != ".":
        args.append(f"decimal={params['decimal']!r}")
    if params.get("thousands", ",") != ",":
        args.append(f"thousands={params['thousands']!r}")
    if params.get("symbols"):
        args.append(f"symbols={list(params['symbols'])!r}")
    tail = (", " + ", ".join(args)) if args else ""
    return [f"    {_col(c)} = _parse_number({_col(c)}{tail})"]


def _extract_currency_gen(params: dict, c: str) -> list[str]:
    to = params.get("to") or f"{c}_currency"
    default = params.get("default")
    return [f"    df[{to!r}] = {_col(c)}.map(lambda v: _detect_currency(v, {default!r}))"]


def _normalize_unit_gen(params: dict, c: str) -> list[str]:
    to = params.get("to", "g")
    emit = params.get("emit_unit_column")
    if emit:
        return [
            f"    {_col(c)}, df[{emit!r}] = _normalize_unit("
            f"{_col(c)}, to={to!r}, emit_unit_column=True)"
        ]
    return [f"    {_col(c)} = _normalize_unit({_col(c)}, to={to!r})"]


def _fill_na_gen(params: dict, c: str) -> list[str]:
    col = _col(c)
    strategy = params.get("strategy")
    if strategy is None:
        return [f"    {col} = {col}.fillna({params.get('value')!r})"]
    strategy = str(strategy).lower()
    if strategy == "mean":
        return [f"    {col} = {col}.fillna(pd.to_numeric({col}, errors='coerce').mean())"]
    if strategy == "median":
        return [f"    {col} = {col}.fillna(pd.to_numeric({col}, errors='coerce').median())"]
    if strategy == "mode":
        return [
            f"    _modes = {col}.dropna().mode()",
            f"    {col} = {col} if len(_modes) == 0 else {col}.fillna(sorted(_modes.tolist(), key=str)[0])",
        ]
    if strategy in ("ffill", "pad"):
        return [f"    {col} = {col}.ffill()"]
    if strategy in ("bfill", "backfill"):
        return [f"    {col} = {col}.bfill()"]
    if strategy == "zero":
        return [f"    {col} = {col}.fillna(0)"]
    if strategy == "empty":
        return [f"    {col} = {col}.fillna('')"]
    return [f"    # NOTE: unknown fill_na strategy {strategy!r} not reproduced"]


_SIMPLE: dict[str, str] = {
    "strip_whitespace": "lambda v: v.strip()",
    "collapse_whitespace": "lambda v: re.sub(r'\\s+', ' ', v).strip()",
    "lowercase": "lambda v: v.lower()",
    "uppercase": "lambda v: v.upper()",
    "title_case": "title_case_text",
    "capitalize": "lambda v: v.strip().capitalize()",
    "normalize_email": "lambda v: v.strip().lower()",
}

_EMITTERS: dict[str, Callable[[dict, str], list[str]]] = {
    "remove_symbols": _remove_symbols,
    "replace": _replace,
    "cast": _cast,
    "normalize_values": _normalize_values,
    "parse_date": _parse_date_gen,
    "parse_number": _parse_number_gen,
    "extract_currency": _extract_currency_gen,
    "normalize_unit": _normalize_unit_gen,
}


def _emit_column_op(op: Op, source: str, unsupported: list[str] | None = None) -> list[str]:
    if op.name in _SIMPLE:
        return [f"    {_col(source)} = _smap({_col(source)}, {_SIMPLE[op.name]})"]
    if op.name in _EMITTERS:
        return _EMITTERS[op.name](op.params, source)
    if op.name == "to_na":
        tokens = op.params.get("tokens")
        arg = f", tokens={list(tokens)!r}" if tokens else ""
        if op.params.get("case_insensitive", True) is False:
            arg += ", case_insensitive=False"
        return [f"    {_col(source)} = _to_na({_col(source)}{arg})"]
    if op.name == "normalize_phone":
        cc = op.params.get("default_country_code")
        arg = f"default_country_code={cc!r}" if cc else ""
        return [f"    {_col(source)} = _normalize_phone({_col(source)}{', ' + arg if arg else ''})"]
    if op.name == "fill_na":
        return _fill_na_gen(op.params, source)
    if op.name == "round":
        return [
            f"    {_col(source)} = pd.to_numeric({_col(source)}, errors='coerce')"
            f".round({op.params.get('decimals', 0)})"
        ]
    from .ops import OP_REGISTRY

    spec = OP_REGISTRY.get(op.name)
    if spec is not None and spec.codegen is not None:
        return [f"    {ln}" if ln.strip() else ln for ln in spec.codegen(dict(op.params), source)]
    if unsupported is not None:
        unsupported.append(f"op {op.name!r} on column {source!r}")
    return [f"    # NOTE: op {_comment(op.name)!r} is not reproduced by codegen"]


# -- validation emission -----------------------------------------------------
_NAMED_VALIDATORS = {"not_null", "unique", "valid_email", "valid_url", "valid_phone"}
_CMP_RE = re.compile(r"^(>=|<=|==|!=|>|<)\s*(-?\d+(?:\.\d+)?)$")


def _membership_values(rule: ValidationRule) -> list:
    values = rule.params.get("values")
    if values is not None:
        return [values] if isinstance(values, str) else list(values)
    import yaml

    rest = rule.check[2:].strip()
    parsed = yaml.load(rest, Loader=yaml.BaseLoader) if rest else []
    return list(parsed) if isinstance(parsed, (list, tuple)) else [parsed]


def _mask_expr(rule: ValidationRule) -> str | None:
    check = rule.check.strip()
    col = _col(rule.column)
    if check in _NAMED_VALIDATORS:
        return f"_v_{check}({col})"
    cmp = _CMP_RE.match(check)
    if cmp:
        return f"_v_cmp({col}, {cmp.group(1)!r}, {float(cmp.group(2))!r})"
    if check == "in" or check.startswith("in ") or check.startswith("in["):
        return f"_v_in({col}, {_membership_values(rule)!r})"
    if check.startswith("matches") or check.startswith("regex"):
        pattern = rule.params.get("pattern") or re.sub(r"^(matches|regex):?\s*", "", check)
        from ._util import safe_compile_regex

        safe_compile_regex(str(pattern))
        return f"_v_matches({col}, {pattern!r})"
    return None


def _emit_validations(rules: list[ValidationRule], unsupported: list[str]) -> list[str]:
    """Emit ALL rules evaluated against one snapshot, then remove the union once.

    Mirrors :func:`cleanframe.validate.apply_validations`: every rule's pass-mask is
    computed on the same (pre-null, pre-drop) frame, so non-row-local checks like
    ``unique`` see all rows — applying rules sequentially would evaluate ``unique``
    over the already-shrunk frame and diverge from the executor.
    """
    lines = ["    # --- validation (all masks over one snapshot; union removed once) ---"]
    lines.append("    _remove = pd.Series(False, index=df.index)")
    null_targets: list[tuple[int, str]] = []
    idx = 0
    for rule in rules:
        expr = _mask_expr(rule)
        label = f"{rule.column}:{rule.check}"
        if expr is None:
            unsupported.append(f"validation {label}")
            lines.append(
                f"    # NOTE: validation {_comment(label)} (custom check) not reproduced"
            )
            continue
        # A rule on a column that is not in the frame is skipped (the executor warns
        # and skips it outside strict mode) instead of raising KeyError.
        lines.append(f"    if {rule.column!r} in df.columns:")
        lines.append(
            f"        _pass{idx} = ({expr}).astype(bool)  # {_comment(label)} "
            f"(on_fail={rule.on_fail})"
        )
        if rule.on_fail in ("quarantine", "drop"):
            lines.append(f"        _remove = _remove | ~_pass{idx}")
        elif rule.on_fail == "error":
            lines.append(f"        if (~_pass{idx}).any():")
            lines.append(f"            raise ValueError({f'validation failed: {label}'!r})")
        elif rule.on_fail == "null":
            null_targets.append((idx, rule.column))
        # warn -> reported only; nothing to enforce here
        idx += 1
    # Null-blank against the snapshot (masks were all computed pre-null), then drop once.
    for i, col in null_targets:
        lines.append(f"    if {col!r} in df.columns:")
        lines.append(f"        df.loc[~_pass{i}, {col!r}] = np.nan")
    lines.append("    df = df[~_remove]")
    return lines


def generate_code(
    recipe: Recipe, func_name: str = "clean", *, allow_partial: bool = False
) -> str:
    """Render ``recipe`` to a standalone pandas module defining ``func_name(df)``.

    Raises :class:`~cleanframe.errors.CleanFrameError` when the recipe uses a custom
    op or check the exporter cannot reproduce, because silently dropping a step would
    make the exported code disagree with the executor. Pass ``allow_partial=True`` to
    accept a partial export whose gaps are marked with ``# NOTE`` comments.
    """
    if not isinstance(recipe, Recipe):
        raise CleanFrameError(
            f"generate_code expects a Recipe, got {type(recipe).__name__}. Load it with "
            "cleanframe.Recipe.load() first."
        )
    if not func_name.isidentifier() or keyword.iskeyword(func_name):
        raise CleanFrameError(
            f"func_name {func_name!r} is not a valid Python function name."
        )
    unsupported: list[str] = []
    lines: list[str] = []
    lines.append(f"def {func_name}(df):")
    lines.append('    """Clean a DataFrame according to the exported recipe. Returns a new frame."""')
    lines.append("    df = df.copy()")
    lines.append("    df.columns = [str(c) for c in df.columns]  # labels are keyed as strings")

    for col_recipe in recipe.columns:
        if not col_recipe.ops and not col_recipe.rename_to:
            continue
        lines.append("")
        lines.append(f"    # --- {_comment(col_recipe.source)} ---")
        if col_recipe.ops:
            lines.append(f"    if {col_recipe.source!r} in df.columns:")
            for op in col_recipe.ops:
                lines.extend(
                    "    " + ln if ln.strip() else ln
                    for ln in _emit_column_op(op, col_recipe.source, unsupported)
                )
            lines.append("    else:")
            lines.append(
                f"        warnings.warn({'recipe column ' + repr(col_recipe.source) + ' is not in the data; skipped'!r})"
            )

    renames = {c.source: c.rename_to for c in recipe.columns if c.rename_to}
    if renames:
        lines.append("")
        lines.append(f"    df = df.rename(columns={renames!r})")

    for op in recipe.frame_ops:
        lines.append("")
        if op.name == "dedup":
            subset = op.params.get("subset")
            keep = op.params.get("keep", "first")
            ic = bool(op.params.get("ignore_case"))
            cols_expr = f"{subset!r}" if subset else "list(df.columns)"
            lines.append(
                f"    _key = df[{cols_expr}].apply(_dedup_key_column, ignore_case={ic!r})"
            )
            lines.append(f"    df = df[~_key.duplicated(keep={keep!r})]")
        elif op.name == "drop_columns":
            lines.append(f"    df = df.drop(columns={op.params.get('columns', [])!r}, errors='ignore')")
        else:
            from .ops import OP_REGISTRY

            spec = OP_REGISTRY.get(op.name)
            if spec is not None and spec.codegen is not None:
                lines.extend(f"    {ln}" if ln.strip() else ln for ln in spec.codegen(dict(op.params), ""))
            else:
                unsupported.append(f"frame op {op.name!r}")
                lines.append(f"    # NOTE: frame op {_comment(op.name)!r} is not reproduced by codegen")

    if recipe.validations:
        lines.append("")
        lines.extend(_emit_validations(recipe.validations, unsupported))

    lines.append("")
    lines.append("    return df")
    lines.append("")
    if unsupported and not allow_partial:
        raise CleanFrameError(
            "Cannot export this recipe as standalone pandas: "
            f"{'; '.join(unsupported)} has no code equivalent. Pass allow_partial=True "
            "to export the rest with the gaps marked."
        )
    # The readable part comes first. Function bodies only look names up when called, so the
    # helpers may sit below: this is the whole recipe, and everything under the banner is the
    # executor's own parsing code, embedded so the two cannot disagree.
    banner = [
        "",
        "# " + "=" * 76,
        "# Helpers: the same parsing code CleanFrame's executor runs (embedded verbatim, so this",
        "# file needs no CleanFrame install). You do not need to read below this line.",
        "# " + "=" * 76,
    ]
    helpers = [
        _constants_source(),
        _numparse_source(),
        _textparse_source(),
        _date_source(),
        _HELPERS,
        _plugin_helpers(recipe),
    ]
    return "\n".join([_DOC_AND_IMPORTS, *lines, *banner, *helpers, ""])


__all__ = ["generate_code"]
