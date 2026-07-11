"""Map a messy file onto a target schema — with confidence scores.

Runs only when the caller supplied a target schema. Performs a deterministic,
greedy 1:1 assignment of schema columns to source columns using fuzzy name
similarity nudged by type compatibility, then emits rename proposals (strong
matches), review-flagged weak matches, and reports for schema columns with no
home and source columns with no schema slot.
"""

from __future__ import annotations

import pandas as pd

from .._util import similarity
from ..issues import Issues
from ..types import Severity
from .base import DetectorContext, detector

#: Which source semantic types satisfy a given schema dtype (None = anything).
_COMPAT: dict[str, set[str]] = {
    "integer": {"integer", "float", "currency"},
    "float": {"float", "integer", "currency"},
    "currency": {"currency", "float", "integer"},
    "date": {"date", "datetime"},
    "datetime": {"datetime", "date"},
    "email": {"email", "text", "id"},
    "phone": {"phone", "text"},
    "url": {"url", "text"},
    "category": {"categorical", "text", "id"},
    "boolean": {"boolean"},
}

_STRONG = 0.6
_WEAK = 0.4


def _type_ok(schema_dtype: str, source_semantic: str) -> bool:
    allowed = _COMPAT.get(schema_dtype)
    return True if allowed is None else source_semantic in allowed


@detector("schema_mapping", scope="frame", priority=5, requires_schema=True)
def detect_schema_mapping(df: pd.DataFrame, ctx: DetectorContext) -> Issues:
    """Map source columns onto the target schema by fuzzy name + type, with confidence."""
    issues = Issues()
    schema = ctx.schema
    if schema is None:
        return issues

    sources = [str(c) for c in df.columns]
    used: set[str] = set()

    for scol in schema.columns:
        scored: list[tuple[float, float, str]] = []
        for src in sources:
            if src in used:
                continue
            sim = similarity(scol.name, src)
            sp = ctx.profile.column(src)
            compat = _type_ok(scol.dtype, sp.semantic_type) if sp else True
            score = 1.0 if (src == scol.name or src in (scol.aliases or [])) else sim
            if not compat:
                score -= 0.15
            scored.append((score, sim, src))

        if not scored:
            issues.add(
                "missing_schema_column",
                f"Schema column {scol.name!r} has no candidate in the data",
                severity=Severity.WARNING,
                confidence=1.0,
                evidence={"target": scol.name},
            )
            continue

        scored.sort(key=lambda t: (-t[0], t[2]))  # best score, then name asc -> deterministic
        best_score, best_sim, best_src = scored[0]

        if best_score >= _STRONG:
            used.add(best_src)
            if best_src != scol.name:
                issues.add(
                    "schema_mapping",
                    f"Map {best_src!r} → {scol.name!r} ({best_sim:.0%} name match)",
                    severity=Severity.INFO,
                    column=best_src,
                    confidence=round(best_sim, 3),
                    evidence={"target": scol.name, "score": round(best_sim, 3)},
                    rename_to=scol.name,
                )
        elif best_score >= _WEAK:
            used.add(best_src)
            issues.add(
                "weak_schema_match",
                f"{best_src!r} only weakly matches schema column {scol.name!r} ({best_sim:.0%})",
                severity=Severity.WARNING,
                column=best_src,
                confidence=round(best_sim, 3),
                evidence={"target": scol.name, "score": round(best_sim, 3)},
                rename_to=scol.name,
            )
        else:
            issues.add(
                "missing_schema_column",
                f"Schema column {scol.name!r} has no good match "
                f"(closest {best_src!r} at {best_sim:.0%})",
                severity=Severity.WARNING,
                confidence=1.0,
                evidence={"target": scol.name, "closest": best_src, "score": round(best_sim, 3)},
            )

    for src in sources:
        if src not in used:
            issues.add(
                "extra_source_column",
                f"Source column {src!r} is not in the target schema",
                severity=Severity.INFO,
                column=src,
                confidence=1.0,
                evidence={},
            )
    return issues


__all__ = ["detect_schema_mapping"]
