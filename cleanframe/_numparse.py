"""Locale-aware numeric-text parsing, shared by the executor and generated code.

This file is deliberately self-contained (``re``, ``unicodedata``, ``functools`` and
``numpy`` only) because :mod:`cleanframe.codegen` embeds its source verbatim into
the standalone pandas it exports. That is what guarantees the generated code and
the executor parse numbers identically — there is one implementation, not two.

The parser is strict about *structure* and lenient about *decoration*:

* Decoration is ignored: currency symbols, unit text (``"1200 INR"``), NBSP and
  zero-width characters, full-width digits, unicode minus.
* Sign is found wherever accountants put it: ``-5``, ``-$5``, ``$-5``, ``5-``,
  ``(5)``, ``$(5)``, ``($5)``.
* Structure must be valid for the convention. With ``thousands=","`` the value
  ``"2,5"`` is *not* 25 — grouping must be 3-digit (``1,234``) or Indian
  (``1,20,000``) — so it becomes NaN, which the executor counts and warns about,
  instead of a number that is 10x too big.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

import numpy as np

_ZERO_WIDTH = {ord(c): None for c in "​‌‍‎‏⁠﻿"}
_MINUS = {ord(c): "-" for c in "−‐‑‒–"}
_EXPONENT_RE = re.compile(r"[eE][+-]?\d+$")
_PLAIN_NUMBER_RE = re.compile(r"[+-]?\d+(?:\.\d+)?$")
_HAS_DIGIT_RE = re.compile(r"\d")
_ALNUM_RE = re.compile(r"[^\W_]")
_DIGITS_RE = re.compile(r"\d+$")


def normalise_numeric_text(text: str) -> str:
    """NFKC (full-width digits/separators → ASCII, NBSP → space), drop zero-width
    characters, map every dash-like minus to ``-``."""
    if text.isascii():
        return text.strip()  # every character NFKC / the zero-width / minus maps touch is non-ASCII
    return unicodedata.normalize("NFKC", text).translate(_ZERO_WIDTH).translate(_MINUS).strip()


@lru_cache(maxsize=64)
def _core_pattern(decimal: str, thousands: str) -> re.Pattern:
    cls = re.escape(decimal) + (re.escape(thousands) if thousands else "")
    return re.compile(rf"(?:\d[\d{cls}]*|{re.escape(decimal)}\d+)(?:[eE][+-]?\d+)?")


def _grouping_ok(int_part: str, sep: str) -> bool:
    """True for western (``1,234,567``) or Indian (``1,20,00,000``) digit grouping."""
    groups = int_part.split(sep)
    if not all(_DIGITS_RE.match(g) for g in groups):
        return False
    first, rest = groups[0], groups[1:]
    if not 1 <= len(first) <= 3 or first.startswith("0"):
        return False  # "0,500" / "0.500" is a decimal, never a grouped 500
    if all(len(g) == 3 for g in rest):
        return True
    return len(first) <= 2 and len(rest[-1]) == 3 and all(len(g) == 2 for g in rest[:-1])


def parse_number_text(value, decimal=".", thousands=",", symbols=()):
    """Parse one cell to a float; NaN when it is not a single well-formed number."""
    if value is None or isinstance(value, bool):
        return np.nan
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        try:
            if value != value:  # NaN / NaT
                return np.nan
        except (TypeError, ValueError):
            return np.nan  # pd.NA: its comparison has no truth value
        value = str(value)
    s = value.strip()
    if decimal == "." and not symbols and _PLAIN_NUMBER_RE.match(s):
        # The overwhelmingly common cell ("1234", "-5.25"): nothing to decode or validate.
        result = float(s)
        if result != result or result in (_INF, -_INF):
            return np.nan
        return 0.0 if result == 0 else result
    for sym in symbols:
        s = s.replace(sym, "")
    s = normalise_numeric_text(s)
    if s == "":
        return np.nan
    m = _core_pattern(decimal, thousands).search(s)
    if not m:
        return np.nan
    start, end = m.span()
    core = m.group(0)
    # A trailing separator is punctuation, not part of the number ("1,200,").
    while core and thousands and core[-1] == thousands and not _EXPONENT_RE.search(core):
        core = core[:-1]
        end -= 1
    if not core:
        return np.nan
    prefix, suffix = s[:start], s[end:]
    around = prefix + suffix
    # ASCII: one C-level regex scan. Otherwise keep str.isdigit, which also counts
    # superscripts and circled digits that \d does not.
    if _HAS_DIGIT_RE.search(around) if around.isascii() else any(ch.isdigit() for ch in around):
        return np.nan  # "12ab34", "10-12": not a single number
    mantissa, exponent = core, ""
    em = _EXPONENT_RE.search(core)
    if em:
        mantissa, exponent = core[: em.start()], core[em.start() :]
    if decimal in mantissa:
        if mantissa.count(decimal) > 1:
            return np.nan
        int_part, _, frac = mantissa.partition(decimal)
        if not _DIGITS_RE.match(frac) and frac != "":
            return np.nan  # a thousands separator after the decimal point
    else:
        int_part, frac = mantissa, ""
    if thousands and thousands in int_part:
        if not _grouping_ok(int_part, thousands):
            return np.nan
        int_part = int_part.replace(thousands, "")
    if int_part == "" and frac == "":
        return np.nan
    try:
        result = float((int_part or "0") + ("." + frac if frac else "") + exponent)
    except ValueError:
        return np.nan
    if result != result or result in (_INF, -_INF):
        return np.nan  # "9" * 400 overflows to inf: not a number worth keeping
    if result == 0:
        return 0.0  # never "-0.0"
    return -abs(result) if _is_negative(prefix, suffix) else result


_INF = float("inf")


def _decoration_only(text):
    return _ALNUM_RE.search(text) is None


def _is_negative(prefix, suffix):
    """Is the number negative, going by what surrounds it?

    A sign only counts as *accounting* notation when everything around the number is
    decoration (symbols, spaces, brackets): ``-$5``, ``$(5)``, ``5-``. Words around it
    disqualify parentheses and stray hyphens, so ``N/A (2023)``, ``Widget (5 pack)`` and
    ``5 - see note`` stay positive; only a hyphen touching the number (``abc-5``) counts.
    """
    pre, suf = prefix.strip(), suffix.strip()
    if _decoration_only(pre):
        if "-" in pre:
            return True
        if "(" in pre and ")" in suf and _decoration_only(suf):
            return True
    elif pre.endswith("-"):
        return True
    return _decoration_only(suf) and "-" in suf


_INT_TEXT_RE = re.compile(r"[+-]?\d+$")
_INT64_MAX = 2**63 - 1


def parse_int_text(value):
    """One cell -> ``int`` (or ``None`` when it is missing / not a number).

    Integer text is converted *exactly* — going through float64 would silently round
    ``9007199254740993`` to ``...992``. Anything else numeric rounds half-to-even, as
    ``cast: int`` documents. Values outside int64 become ``None`` (counted as
    unparseable by the executor) rather than raising.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        result = value
    else:
        text = value.strip() if isinstance(value, str) else None
        if text is not None and "_" in text:
            return None  # Python would read "1_0" as 10; a cell is not a Python literal
        if text is not None and _INT_TEXT_RE.match(text):
            result = int(text)
        else:
            try:
                number = float(value if text is None else text)
            except (TypeError, ValueError):
                return None
            if number != number or number in (float("inf"), float("-inf")):
                return None
            result = int(round(number))
    return result if -_INT64_MAX - 1 <= result <= _INT64_MAX else None


_BARE_NUMBER_RE = re.compile(r"[+-]?[\d.,]*\d(?:[eE][+-]?\d+)?$")


def parse_plain_number(text):
    """A bare number in *either* convention, or ``None`` when that is ambiguous.

    ``"12.5"`` and ``"1,5"`` are unambiguous. ``"1,500"`` could be 1500 or 1.5, so it
    is refused instead of being guessed at (a 1000x error is worse than a NaN).
    """
    if not isinstance(text, str) or not _BARE_NUMBER_RE.match(normalise_numeric_text(text)):
        return None  # "2 lbs" is not a bare number, whatever parse_number_text ignores
    us = parse_number_text(text, ".", ",")
    eu = parse_number_text(text, ",", ".")
    us_ok, eu_ok = us == us, eu == eu
    if us_ok and eu_ok:
        return us if us == eu else None
    if us_ok:
        return us
    if eu_ok:
        return eu
    return None


_NUMBER_UNIT_RE = re.compile(r"^([+-]?[\d.,]*\d(?:[eE][+-]?\d+)?)\s*([A-Za-z]+)$")


def split_number_unit(value):
    """``"5kg"`` / ``"1.5e3 g"`` / ``"1,5 kg"`` -> ``(5.0, "kg")``; ``None`` if not that shape
    (or if the number is ambiguous, see :func:`parse_plain_number`)."""
    if not isinstance(value, str):
        return None
    m = _NUMBER_UNIT_RE.match(normalise_numeric_text(value))
    if not m:
        return None
    number = parse_plain_number(m.group(1))
    if number is None:
        return None
    return number, m.group(2).casefold()
