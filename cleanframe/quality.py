"""A single, deterministic data-quality score.

Turns a profile + detected issues into a 0–100 score and a letter grade. The
formula is intentionally simple and documented so the number is explainable in a
report and stable across runs:

* Each issue contributes a penalty = ``severity_weight × affected_fraction``.
* Exact-duplicate rows add their own penalty.
* Penalties are averaged over the number of columns (so wide and narrow files land
  on a comparable scale), then ``score = 100 × (1 − avg_penalty)``.

It is a heuristic, not a benchmark — good for "is this file better than last
month's?", not for splitting hairs between 87 and 88.
"""

from __future__ import annotations

from dataclasses import dataclass

from .issues import Issues
from .profile import DataFrameProfile
from .types import Severity

_SEVERITY_WEIGHT = {Severity.ERROR: 1.0, Severity.WARNING: 0.4, Severity.INFO: 0.1}
#: Used when an issue has no count in its evidence — a moderate assumed impact.
_DEFAULT_FRACTION = 0.25


@dataclass
class QualityScore:
    score: int
    grade: str
    label: str
    color: str
    penalty: float

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return f"{self.score}/100 ({self.grade})"


def _affected_fraction(evidence: dict, n_rows: int) -> float:
    if n_rows <= 0:
        return _DEFAULT_FRACTION
    for key in ("count", "null_count", "unparsed", "n_failed"):
        if key in evidence and isinstance(evidence[key], (int, float)):
            return min(1.0, evidence[key] / n_rows)
    return _DEFAULT_FRACTION


def _grade(score: int) -> tuple[str, str, str]:
    if score >= 90:
        return "A", "Excellent", "#16a34a"
    if score >= 80:
        return "B", "Good", "#65a30d"
    if score >= 70:
        return "C", "Fair", "#ca8a04"
    if score >= 60:
        return "D", "Poor", "#ea580c"
    return "F", "Needs work", "#dc2626"


def quality_score(profile: DataFrameProfile, issues: Issues) -> QualityScore:
    n_rows = profile.n_rows or 1
    n_cols = max(1, profile.n_columns)

    penalty = 0.0
    for issue in issues:
        weight = _SEVERITY_WEIGHT.get(issue.severity, 0.2)
        penalty += weight * _affected_fraction(issue.evidence, n_rows)

    if profile.duplicate_row_count:
        penalty += 0.5 * min(1.0, profile.duplicate_row_count / n_rows)

    avg_penalty = min(1.0, penalty / n_cols)
    score = int(round(100 * (1 - avg_penalty)))
    score = max(0, min(100, score))
    grade, label, color = _grade(score)
    return QualityScore(score=score, grade=grade, label=label, color=color, penalty=round(penalty, 3))


__all__ = ["QualityScore", "quality_score"]
