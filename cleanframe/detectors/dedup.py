"""Duplicate-row detection — exact and fuzzy.

Exact full-row duplicates are safe to drop, so we propose a ``dedup`` op with high
confidence. *Key* duplicates (two rows sharing an email/id but differing elsewhere)
and *fuzzy* near-duplicates on name-like columns are only *reported* with
reviewable pair evidence — silently collapsing them would lose data.
"""

from __future__ import annotations

from difflib import SequenceMatcher

import pandas as pd

from .._util import normalize_key
from ..issues import Issues, _cap_examples
from ..types import Op, Severity
from .base import DetectorContext, detector

_KEY_HINTS = ("email", "id", "uuid", "guid")
_NAME_HINTS = ("name", "customer", "client", "company", "vendor", "title")
_FUZZY_THRESHOLD = 0.88
_FUZZY_MAX_ROWS = 200  # pairwise cost; bound for determinism + speed


def _key_columns(ctx: DetectorContext) -> list[str]:
    keys: list[str] = []
    for cp in ctx.profile.columns:
        low = cp.name.lower()
        if cp.semantic_type in ("email", "id") or any(h in low for h in _KEY_HINTS):
            keys.append(cp.name)
    return keys


def _fuzzy_columns(ctx: DetectorContext) -> list[str]:
    cols: list[str] = []
    for cp in ctx.profile.columns:
        low = cp.name.lower()
        if cp.semantic_type in ("text", "categorical", "id") and any(
            h in low for h in _NAME_HINTS
        ):
            cols.append(cp.name)
    return cols


def _fuzzy_pairs(df: pd.DataFrame, column: str) -> list[dict]:
    """Return near-duplicate (i, j, score, values) pairs, sorted deterministically."""
    if column not in df.columns or len(df) < 2:
        return []
    # Cap pairwise work; take a stable head so results don't depend on shuffle.
    work = df.head(_FUZZY_MAX_ROWS)
    values = [(int(idx), str(v).strip()) for idx, v in work[column].items() if pd.notna(v)]
    pairs: list[dict] = []
    for i in range(len(values)):
        idx_i, a = values[i]
        if not a:
            continue
        na = normalize_key(a)
        for j in range(i + 1, len(values)):
            idx_j, b = values[j]
            if not b or a == b:
                continue
            # Skip exact-normalized matches — those are category/casing, not fuzzy.
            if na == normalize_key(b):
                continue
            score = SequenceMatcher(None, a.casefold(), b.casefold()).ratio()
            if score >= _FUZZY_THRESHOLD:
                pairs.append(
                    {
                        "rows": [idx_i, idx_j],
                        "values": [a, b],
                        "score": round(score, 3),
                        "proposal": {"keep": idx_i, "drop": idx_j},
                    }
                )
    pairs.sort(key=lambda p: (-p["score"], p["rows"][0], p["rows"][1]))
    return pairs


@detector("dedup", scope="frame", priority=80)
def detect_duplicates(df: pd.DataFrame, ctx: DetectorContext) -> Issues:
    """Propose dropping exact duplicate rows; report key/fuzzy duplicates for review."""
    issues = Issues()
    if len(df) == 0:
        return issues

    exact = int(df.duplicated().sum())
    if exact:
        issues.add(
            "duplicate_rows",
            f"{exact} exact duplicate row(s)",
            severity=Severity.WARNING,
            confidence=1.0,
            evidence={"count": exact},
            ops=[Op("dedup", {"keep": "first"})],
        )

    # Key duplicates beyond the exact ones — report, don't auto-merge.
    for key in _key_columns(ctx):
        if key not in df.columns:
            continue
        dupe_mask = df[key].dropna().duplicated(keep=False)
        n = int(dupe_mask.sum())
        if n > exact:
            examples = df.loc[df[key].duplicated(keep=False), key].dropna().unique().tolist()
            issues.add(
                "duplicate_keys",
                f"Column {key!r} has {n} row(s) sharing a value that should be unique",
                severity=Severity.WARNING,
                column=key,
                confidence=0.6,
                evidence={"count": n, "examples": _cap_examples(examples)},
            )

    # Fuzzy near-duplicates on name-like columns — reviewable merge proposals only.
    for col in _fuzzy_columns(ctx):
        pairs = _fuzzy_pairs(df, col)
        if not pairs:
            continue
        issues.add(
            "fuzzy_duplicates",
            f"{len(pairs)} near-duplicate pair(s) in {col!r} (fuzzy match ≥ {_FUZZY_THRESHOLD:.0%})",
            severity=Severity.INFO,
            column=col,
            confidence=0.55,  # below auto threshold — review mode surfaces it
            evidence={
                "count": len(pairs),
                "threshold": _FUZZY_THRESHOLD,
                "pairs": pairs[:10],
            },
            # No ops — merge is a human decision; evidence carries keep/drop proposals.
        )
    return issues


__all__ = ["detect_duplicates"]
