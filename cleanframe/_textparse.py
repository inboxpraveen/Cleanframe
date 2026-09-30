"""Text helpers shared by the executor and generated code.

Like :mod:`cleanframe._numparse`, this file is self-contained (``re`` only) because
:mod:`cleanframe.codegen` embeds its source verbatim into exported pandas.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

_WORD_RE = re.compile(r"\w+(?:['’]\w+)*")


def _cap_word(word: str) -> str:
    if word[0].isdigit():
        return word.lower()  # "3rd", "2ND" -> "3rd", never "3Rd"
    head, sep, tail = word.partition("'")
    if not sep:
        head, sep, tail = word.partition("’")
    if sep and len(head) == 1 and tail:
        # O'Neil, D'Angelo: a one-letter prefix keeps the name capitalised
        return head.upper() + sep + tail[:1].upper() + tail[1:].lower()
    return word[:1].upper() + word[1:].lower()


def title_case_text(text: str) -> str:
    """Whitespace-collapsed title case that survives ordinals and apostrophes.

    ``str.title()`` turns ``"3rd street"`` into ``"3Rd Street"`` and ``"don't"`` into
    ``"Don'T"``; this does not.
    """
    collapsed = re.sub(r"\s+", " ", text).strip()
    return _WORD_RE.sub(lambda m: _cap_word(m.group(0)), collapsed)


def map_exact(mapping, v):
    """``normalize_values`` lookup: the cell, its text, and ``5`` for a float ``5.0``.

    A recipe key written as ``1`` (YAML reads a plain key as text) must still find the
    cell ``1.0`` that a NaN in an integer column turned into a float.
    """
    if v in mapping:
        return mapping[v]
    key = str(v)
    if key in mapping:
        return mapping[key]
    if isinstance(v, float) and v.is_integer():
        key = str(int(v))
        if key in mapping:
            return mapping[key]
    return v


def map_folded(folded, v):
    """Case-insensitive ``normalize_values`` lookup (``folded`` keys are stripped, casefolded)."""
    key = str(v).strip().casefold()
    if key in folded:
        return folded[key]
    if isinstance(v, float) and v.is_integer():
        key = str(int(v))
        if key in folded:
            return folded[key]
    return v


_ISO_RE = re.compile(r"(?<![A-Z])[A-Z]{3}(?![A-Z])")


@lru_cache(maxsize=8)
def _currency_regex(symbols, words):
    parts = []
    for sym, _code in symbols:
        # "$"-family symbols must not be glued to a letter: "AR$ 3" is not "R$" + ...
        parts.append(("(?<![A-Za-z])" + re.escape(sym)) if "$" in sym else re.escape(sym))
    for word, _code in words:
        parts.append("(?<![A-Za-z])" + re.escape(word) + "(?![A-Za-z])")
    return re.compile("|".join(parts)), dict((*symbols, *words))


def detect_currency_text(text, symbols, words, known_codes):
    """The ISO code a money cell states, or ``None`` when it states none.

    An explicit code wins over a symbol (``"$5 MXN"`` is pesos). ``symbols`` and ``words``
    are tuples of ``(text, code)`` pairs, longest symbol first, so ``R$`` is tried before
    ``$``. A ``$``-family symbol glued to a letter is not matched, which turns an
    unknown ``XY$5`` into "no currency" rather than a wrong USD.
    """
    for m in _ISO_RE.finditer(text.upper()):
        if m.group(0) in known_codes:
            return m.group(0)
    pattern, table = _currency_regex(symbols, words)
    hit = pattern.search(text)
    return table[hit.group(0)] if hit else None


_INVISIBLE = {ord(c): None for c in "\u200b\u2060\ufeff"}


def normalize_unicode_text(text, form="NFC"):
    """Unicode-normalise one cell and drop characters no reader can see.

    ``café`` typed as ``cafe`` + combining accent and as one precomposed character look the
    same, compare unequal, and split a category in two; NFC makes them one. Zero-width space,
    word joiner and byte-order mark (invisible, and untouched by ``strip``) are removed; a
    non-breaking space becomes a plain one. Zero-width joiners are *kept*: they are part of
    emoji sequences and of several scripts.
    """
    return unicodedata.normalize(form, text).translate(_INVISIBLE).replace("\u00a0", " ")
