"""Category canonicalisation.

Finds values that *mean the same thing* but are spelled differently and proposes a
``normalize_values`` mapping to a single canonical spelling. Two mechanisms, both
deterministic:

* **Exact-after-normalisation** clusters (``"Bengaluru"``/``"bengaluru "``/
  ``"BENGALURU"``) — high confidence.
* **Fuzzy typo** clusters (``"Banglore"`` vs ``"Bangalore"``) via a similarity
  threshold — lower confidence.

Semantic abbreviations (``"BLR"`` → ``"Bangalore"``) are deliberately *out of
scope* for rules — that's domain knowledge for a human or the LLM planner. The
canonical spelling for each cluster is the most frequent original (ties broken
alphabetically), so results never depend on row order.
"""

from __future__ import annotations

import re

import pandas as pd

from .._util import normalize_key, similarity
from ..issues import Issues
from ..types import Op, Severity
from .base import DetectorContext, detector

#: Above this cardinality a column is treated as free text, not categories.
_MAX_CATEGORY_CARDINALITY = 60
_FUZZY_THRESHOLD = 0.86
_WS = re.compile(r"\s+")


def _clean(value: str) -> str:
    """Whitespace-normalise a value so category clustering sees post-trim spellings.

    Categories run *after* whitespace ops in a recipe, so clustering on the cleaned
    form keeps the ``normalize_values`` map keys matching the data at that point and
    prevents a whitespace-messy spelling from ever being chosen as canonical.
    """
    return _WS.sub(" ", value).strip()


def _casing_rank(s: str) -> int:
    """Prefer nicely-cased spellings when frequencies tie: Title > Mixed > lower > UPPER."""
    if s.istitle():
        return 0
    if not s.isupper() and not s.islower():
        return 1
    if s.islower():
        return 2
    return 3


def _canonical(spellings: dict[str, int]) -> str:
    """Canonical spelling: most frequent, then nicest casing, then alphabetical.

    All three keys are deterministic, so the choice never depends on row order.
    """
    return sorted(spellings.items(), key=lambda kv: (-kv[1], _casing_rank(kv[0]), kv[0]))[0][0]


def _cluster(counts: dict[str, int], seed_map: dict[str, str] | None) -> dict[str, str]:
    """Return a mapping ``{variant: canonical}`` covering only values that change."""
    # 1) group by aggressive normalisation key
    groups: dict[str, dict[str, int]] = {}
    for value, n in counts.items():
        groups.setdefault(normalize_key(value), {})[value] = n

    canon_of_group = {key: _canonical(spellings) for key, spellings in groups.items()}

    # 2) fuzzy-merge whole groups whose canonical spellings are near-identical.
    #    Deterministic: iterate group keys in sorted order, attach to the first
    #    (largest, then alphabetically-first) existing cluster within threshold.
    ordered = sorted(groups, key=lambda k: (-sum(groups[k].values()), k))
    cluster_rep: dict[str, str] = {}
    reps: list[str] = []
    for key in ordered:
        canon = canon_of_group[key]
        match = None
        for rep in reps:
            if similarity(canon, canon_of_group[rep]) >= _FUZZY_THRESHOLD:
                match = rep
                break
        if match is None:
            reps.append(key)
            cluster_rep[key] = key
        else:
            cluster_rep[key] = match

    # 3) build variant -> canonical, honouring any seed overrides first.
    mapping: dict[str, str] = {}
    for key, spellings in groups.items():
        rep_key = cluster_rep[key]
        canonical = canon_of_group[rep_key]
        for value in spellings:
            target = (seed_map or {}).get(value, canonical)
            if value != target:
                mapping[value] = target
    # Sort keys so the emitted recipe is byte-identical regardless of input row
    # order (the cleaned data is order-independent either way).
    return dict(sorted(mapping.items()))


@detector("categories", priority=50)
def detect_categories(series: pd.Series, ctx: DetectorContext) -> Issues:
    """Cluster case/whitespace/typo variants of a category into one canonical value."""
    issues = Issues()
    cp = ctx.column_profile
    if cp is None or cp.count == 0:
        return issues
    if series.dtype != object and str(series.dtype) not in ("string", "category"):
        return issues
    # Only meaningful for low-cardinality columns.
    if cp.unique_count > _MAX_CATEGORY_CARDINALITY or cp.semantic_type in (
        "email", "phone", "url", "currency", "date", "datetime", "id",
    ):
        return issues

    counts: dict[str, int] = {}
    for v in series.dropna().tolist():
        if isinstance(v, str):
            cleaned = _clean(v)
            counts[cleaned] = counts.get(cleaned, 0) + 1
    if len(counts) < 2:
        return issues

    seed_map = ctx.option("category_map", {}).get(ctx.column) if ctx.option("category_map") else None
    mapping = _cluster(counts, seed_map)
    if not mapping:
        return issues

    distinct_before = len(counts)
    distinct_after = len({counts_key if counts_key not in mapping else mapping[counts_key]
                          for counts_key in counts})
    # Confidence: purely case/space merges are safe; fuzzy typo merges less so.
    fuzzy = any(normalize_key(k) != normalize_key(v) for k, v in mapping.items())
    confidence = 0.7 if fuzzy else 0.9

    issues.add(
        "category_variants",
        f"{len(mapping)} value(s) are variants of other categories "
        f"({distinct_before} → {distinct_after} distinct)",
        severity=Severity.WARNING,
        confidence=confidence,
        evidence={
            "mapping": mapping,
            "distinct_before": distinct_before,
            "distinct_after": distinct_after,
            "fuzzy": fuzzy,
        },
        ops=[Op("normalize_values", {"map": mapping})],
    )
    return issues


__all__ = ["detect_categories"]
