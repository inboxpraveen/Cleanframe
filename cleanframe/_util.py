"""Small, dependency-light string helpers shared across modules.

Centralised so that column-name normalisation and fuzzy matching behave
*identically* everywhere they matter — schema mapping, category clustering, and
drift detection all compare names/values the same way, which keeps confidence
scores consistent between "planning" and "drift" time.

Also hosts production-safety helpers used across detectors, IO, and validation:
bounded sampling for large columns, regex length guards, and CSV formula escaping.
"""

from __future__ import annotations

import os
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from .errors import CleanFrameError, OutputError

_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_NON_ALNUM_RE = re.compile(r"[^0-9a-zA-Z]+")

#: Default cap for detector / planner scans over non-null values on large columns.
DETECTOR_SAMPLE_CAP = 50_000

#: Default cap for cell-level diff entries (prevents OOM on wide dirty frames).
DEFAULT_MAX_DIFF_CHANGES = 100_000

#: Reject recipe/user regexes longer than this (ReDoS mitigation).
MAX_REGEX_PATTERN_LENGTH = 500

#: Characters that make a CSV cell look like a spreadsheet formula.
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

#: A signed number is not a formula. Escaping it would corrupt normalised phone
#: numbers (``+919876543210``) and negative amounts (``-1.5``) on every export.
_SIGNED_NUMBER_RE = re.compile(r"^[+-](?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")

#: Control characters Excel refuses to store, and its per-cell character limit.
_EXCEL_ILLEGAL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
EXCEL_MAX_CELL_CHARS = 32_767


def is_string_like(series: pd.Series) -> bool:
    """True for object / string / str / category columns (pandas 1.x–3.x).

    Pandas 3 defaults inferred text to ``dtype='str'`` (not ``object``). Detectors
    that only checked ``object`` or ``\"string\"`` silently no-op'd on CI.
    """
    dtype = series.dtype
    if pd.api.types.is_object_dtype(dtype):
        return True
    if pd.api.types.is_string_dtype(dtype):
        return True
    # Categorical (avoid deprecated is_categorical_dtype).
    if isinstance(dtype, pd.CategoricalDtype) or str(dtype) == "category":
        return True
    # Belt-and-suspenders for unusual StringDtype spellings across versions.
    name = str(dtype).lower()
    return name in ("str", "string", "object") or name.startswith("string")


def canonicalize_dtype(dtype: Any) -> str:
    """Map a pandas dtype to a coarse family for cross-version drift comparison.

    ``object`` / ``string`` / ``str`` (pandas 2 vs 3) collapse to ``string`` so a
    fingerprint recorded under one pandas major doesn't false-alarm under another.
    """
    name = str(dtype).lower()
    if name in ("object", "str", "string") or name.startswith("string"):
        return "string"
    if name == "category" or name.startswith("category"):
        return "category"
    if "bool" in name:
        return "bool"
    if "int" in name:
        return "int"
    if "float" in name or name == "double":
        return "float"
    if "datetime" in name or name.startswith("date"):
        return "datetime"
    if "timedelta" in name:
        return "timedelta"
    return name


def snake_case(name: str) -> str:
    """``"Customer Name"`` / ``"CustomerName"`` / ``"Amt (INR)"`` -> ``customer_name`` / ``amt_inr``."""
    text = _CAMEL_RE.sub("_", str(name))
    text = _NON_ALNUM_RE.sub("_", text)
    return text.strip("_").lower()


def normalize_key(value: str) -> str:
    """Aggressive normalisation for equality-style comparison (case/space/punct-insensitive)."""
    return _NON_ALNUM_RE.sub("", str(value)).casefold()


def token_set(name: str) -> set[str]:
    return {t for t in snake_case(name).split("_") if t}


def similarity(a: str, b: str) -> float:
    """A blended name-similarity score in ``[0, 1]``.

    Combines a character-level ratio (catches typos/abbreviations) with a
    token-overlap ratio (catches word reordering like ``"INR Amount"`` vs
    ``"amount_inr"``). Deterministic and symmetric.
    """
    sa, sb = snake_case(a), snake_case(b)
    if not sa and not sb:
        return 1.0
    if not sa or not sb:
        return 0.0
    if sa == sb:
        return 1.0
    char_ratio = SequenceMatcher(None, sa, sb).ratio()
    ta, tb = token_set(a), token_set(b)
    if ta and tb:
        token_ratio = len(ta & tb) / len(ta | tb)
    else:
        token_ratio = 0.0
    return round(max(char_ratio, 0.5 * char_ratio + 0.5 * token_ratio), 4)


def best_match(target: str, candidates: list[str]) -> tuple[str | None, float]:
    """Return the ``(candidate, score)`` most similar to ``target`` (deterministic)."""
    best: str | None = None
    best_score = 0.0
    for cand in candidates:
        score = similarity(target, cand)
        # Strict > keeps the first (input-order) candidate on ties -> deterministic.
        if score > best_score:
            best_score = score
            best = cand
    return best, best_score


def looks_like_code_values(values: Any) -> bool:
    """True when the values are short all-caps codes, where ``NA`` is Namibia.

    Used to decide whether a null-looking token is really a null: in a country or
    currency column it is data, and turning it into NaN is a silent loss.
    """
    tokens = [v.strip() for v in values if isinstance(v, str) and v.strip()]
    if len(tokens) < 3:
        return False
    return all(len(t) <= 3 and t.isalpha() and t.isupper() for t in tokens)


def sample_non_null(series: pd.Series, cap: int = DETECTOR_SAMPLE_CAP) -> list[Any]:
    """Return up to ``cap`` non-null values in frame order (deterministic head sample).

    Detectors use this instead of ``series.dropna().tolist()`` so a multi-million-row
    column cannot force a full materialisation into Python lists. Pattern inference
    on the head is sufficient for planning; execution still transforms every row.
    """
    if cap <= 0:
        return []
    non_null = series.dropna()
    if len(non_null) > cap:
        non_null = non_null.iloc[:cap]
    return non_null.tolist()


_QUANTIFIED_ALT_RE = re.compile(r"\(([^()]*\|[^()]*)\)\s*(?:[+*]|\{)")


def _has_overlapping_alternation(pattern: str) -> bool:
    """True if a quantified group contains alternatives where one is a prefix of another.

    ``(a|aa)+`` / ``(a|a)*`` backtrack catastrophically; ``(cat|dog)+`` does not.
    Best-effort (single-level groups) — a guard, not a proof.
    """
    for m in _QUANTIFIED_ALT_RE.finditer(pattern):
        alts = [a for a in m.group(1).split("|")]
        for i, a in enumerate(alts):
            for j, b in enumerate(alts):
                if i != j and a and b.startswith(a):
                    return True
    return False


def safe_compile_regex(pattern: str, *, flags: int = 0) -> re.Pattern[str]:
    """Compile a user/recipe regex with length and complexity guards.

    Python's ``re`` engine has no built-in timeout; bounding pattern size and
    rejecting obviously nested quantifiers is the practical ReDoS mitigation for
    recipe-driven ``replace`` / ``matches`` checks on production data.
    """
    if not isinstance(pattern, str):
        raise ValueError(f"Regex pattern must be a string, got {type(pattern).__name__}.")
    if len(pattern) > MAX_REGEX_PATTERN_LENGTH:
        raise ValueError(
            f"Regex pattern length {len(pattern)} exceeds limit of {MAX_REGEX_PATTERN_LENGTH}."
        )
    # Nested quantifiers like (a+)+ / (a*)* are classic ReDoS shapes.
    if re.search(r"\([^)]*[+*][^)]*\)[+*]", pattern) or re.search(r"\([^)]*[+*]\)\{", pattern):
        raise ValueError(
            "Regex pattern looks like a nested-quantifier ReDoS risk; "
            "simplify it or split into multiple safer checks."
        )
    # An optional inside a quantified group — (a?)+ — can match empty then repeat,
    # another catastrophic-backtracking shape that the check above misses.
    if re.search(r"\([^)]*\?\s*\)\s*[+*]", pattern):
        raise ValueError(
            "Regex pattern has an optional inside a quantified group (e.g. (a?)+), "
            "a catastrophic-backtracking risk; simplify it."
        )
    # Overlapping alternation in a quantified group — (a|aa)+ — where one alternative
    # is a prefix of another. (Non-overlapping alternations like (cat|dog)+ are fine.)
    if _has_overlapping_alternation(pattern):
        raise ValueError(
            "Regex pattern has an overlapping alternation inside a quantifier "
            "(e.g. (a|aa)+), a catastrophic-backtracking risk; simplify it."
        )
    try:
        return re.compile(pattern, flags)
    except re.error as exc:
        raise ValueError(f"Invalid regex pattern: {exc}") from exc


def sanitize_csv_value(value: Any) -> Any:
    """Neutralise spreadsheet formula injection in a single CSV/Excel cell.

    Excel / Google Sheets / LibreOffice treat cells starting with ``=``, ``+``,
    ``-``, ``@``, or certain control characters as formulas. Prefixing with a
    single quote forces text interpretation without changing the visible value in
    most spreadsheet UIs.
    """
    if not isinstance(value, str) or not value:
        return value
    if value[0] not in _CSV_FORMULA_PREFIXES:
        return value
    if _SIGNED_NUMBER_RE.match(value):
        return value
    return "'" + value


def sanitize_dataframe_for_csv(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy of ``df`` with formula-like cells *and headers* escaped.

    Header labels are escaped too: a column literally named ``=CMD()`` would
    otherwise be written as a live formula in the first row of the export.
    """
    out = df.copy()
    for col in out.columns:
        series = out[col]
        if is_string_like(series):
            out[col] = series.map(sanitize_csv_value)
    labels = [sanitize_csv_value(str(c)) for c in out.columns]
    if labels != [str(c) for c in out.columns]:
        out.columns = labels
    return out


def sanitize_dataframe_for_spreadsheet(df: pd.DataFrame) -> pd.DataFrame:
    """Escape formula-like cells and drop characters Excel refuses to store."""
    out = sanitize_dataframe_for_csv(df)
    for col in out.columns:
        series = out[col]
        if not is_string_like(series):
            continue
        if bool(series.map(lambda v: isinstance(v, str) and len(v) > EXCEL_MAX_CELL_CHARS).any()):
            raise OutputError(
                f"Column {col!r} holds a value longer than Excel's "
                f"{EXCEL_MAX_CELL_CHARS}-character cell limit. Write .csv or .parquet "
                "instead, or shorten the value."
            )
        out[col] = series.map(lambda v: _EXCEL_ILLEGAL_RE.sub("", v) if isinstance(v, str) else v)
    return out


def ensure_string_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Return a frame whose column labels are unique strings (copy only if needed).

    CleanFrame keys lineage, the diff, recipes, and fingerprints by *string* column
    name. Non-string labels (ints, tuples/MultiIndex, ``None``/``NaN``) are coerced
    with ``str`` so they can't crash ``df[label]`` / ``.astype(str)`` downstream. If
    that coercion collides — or the frame already carries duplicate labels — a
    :class:`~cleanframe.errors.CleanFrameError` is raised naming them, because
    silently de-duplicating columns would itself be undeclared data loss.
    """
    cols = list(df.columns)
    str_cols = [str(c) for c in cols]
    counts: dict[str, int] = {}
    for s in str_cols:
        counts[s] = counts.get(s, 0) + 1
    dups = sorted(s for s, n in counts.items() if n > 1)
    if dups:
        raise CleanFrameError(
            f"Duplicate column name(s): {dups}. CleanFrame needs unique column names — "
            "rename or drop the duplicates before cleaning."
        )
    if str_cols == cols:
        return df
    out = df.copy()
    out.columns = str_cols
    return out


def ensure_parent(path: str | Path) -> Path:
    """Validate an output path and create its parent directories if needed."""
    path = Path(path)
    if path.is_dir():
        raise OutputError(
            f"Output path is a directory, not a file: {path}. Pass the file to write to."
        )
    parent = path.parent
    # Path("file.txt").parent is "."; Path(".").parent is also "." — skip useless mkdir.
    if parent.parts and str(parent) not in (".", ""):
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise OutputError(f"Could not create output directory {parent}: {exc}") from exc
    return path


def check_output_target(
    path: str | Path, source: str | Path | None = None, *, overwrite: bool = False
) -> Path:
    """Prepare an output path, refusing to overwrite the input it came from.

    Writing cleaned data back over its own source destroys the original before
    anyone can review the diff, so it takes an explicit ``overwrite``.
    """
    out = ensure_parent(path)
    if source is None or overwrite:
        return out
    try:
        src = Path(source)
        same = src.exists() and out.exists() and out.resolve() == src.resolve()
    except OSError:  # pragma: no cover - unresolvable path
        same = False
    if same:
        raise OutputError(
            f"Refusing to overwrite the input file in place: {out}. Write to a different "
            "path, or pass overwrite=True (CLI: --overwrite) to accept losing the original."
        )
    return out


class _NoDuplicateLoader(yaml.SafeLoader):
    """SafeLoader that refuses duplicate mapping keys instead of keeping the last."""


def _construct_unique_mapping(loader, node, deep=False):
    mapping: dict = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if isinstance(key, list):
            key = tuple(key)
        if key in mapping:
            raise CleanFrameError(
                f"duplicate key {key!r} on line {key_node.start_mark.line + 1} — the second "
                "value would silently win; remove one of them"
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_NoDuplicateLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def load_yaml(text: str, *, error: type = CleanFrameError, what: str = "input"):
    """Parse YAML strictly: syntax errors and duplicate keys both become ``error``."""
    try:
        return yaml.load(text, Loader=_NoDuplicateLoader)
    except CleanFrameError as exc:
        raise error(f"Invalid {what} YAML: {exc}") from exc
    except yaml.YAMLError as exc:
        raise error(f"Invalid {what} YAML: {exc}") from exc


def write_text(path: str | Path, text: str) -> Path:
    """Write UTF-8 text with ``\\n`` newlines on every OS (no Windows CRLF translation).

    Recipes, schemas, reports, and generated code must round-trip identically whether
    authored on Windows, macOS, or Linux — forcing ``newline='\\n'`` keeps git diffs
    and byte-identical YAML stable across platforms.
    """
    path = ensure_parent(path)
    tmp = path.with_name(path.name + ".cf-tmp")
    try:
        tmp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(tmp, path)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise OutputError(f"Could not write {path}: {exc}") from exc
    return path


def read_text(path: str | Path) -> str:
    """Read a UTF-8 text file; accept UTF-8 BOM (common on Windows Excel / Notepad)."""
    path = Path(path)
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CleanFrameError(
            f"Could not read {path.name} as UTF-8 ({exc}). Recipes and schemas are UTF-8 "
            "text files — re-save it as UTF-8."
        ) from exc
    except OSError as exc:
        raise CleanFrameError(f"Could not read {path}: {exc}") from exc


__all__ = [
    "snake_case",
    "normalize_key",
    "token_set",
    "similarity",
    "best_match",
    "sample_non_null",
    "looks_like_code_values",
    "safe_compile_regex",
    "sanitize_csv_value",
    "sanitize_dataframe_for_csv",
    "sanitize_dataframe_for_spreadsheet",
    "ensure_parent",
    "check_output_target",
    "load_yaml",
    "ensure_string_columns",
    "write_text",
    "read_text",
    "is_string_like",
    "canonicalize_dtype",
    "DETECTOR_SAMPLE_CAP",
    "DEFAULT_MAX_DIFF_CHANGES",
    "MAX_REGEX_PATTERN_LENGTH",
    "EXCEL_MAX_CELL_CHARS",
]
