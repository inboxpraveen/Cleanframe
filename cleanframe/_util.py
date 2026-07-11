"""Small, dependency-light string helpers shared across modules.

Centralised so that column-name normalisation and fuzzy matching behave
*identically* everywhere they matter — schema mapping, category clustering, and
drift detection all compare names/values the same way, which keeps confidence
scores consistent between "planning" and "drift" time.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_NON_ALNUM_RE = re.compile(r"[^0-9a-zA-Z]+")


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


__all__ = ["snake_case", "normalize_key", "token_set", "similarity", "best_match"]
