"""Schema-drift detection: catch the file that came back *different*.

Before replaying a recipe on next month's file, compare that file against the
recipe's ``source_fingerprint`` and its declared expectations. If a column was
renamed, a new one appeared, a dtype shifted, or values stopped matching the
formats the recipe parses, CleanFrame **stops and tells you** instead of silently
producing garbage — exactly the failure mode that makes hand-cleaning fragile.

Findings carry fuzzy-match suggestions ("94% match to recipe column …") so the fix
is obvious. By default ``apply_recipe`` / ``cleanframe apply`` **stop** on drift;
pass ``on_drift="warn"`` / ``--force`` only when you intentionally want to continue.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from ._util import best_match, canonicalize_dtype, similarity
from .fingerprint import DEFAULT_SAMPLE_ROWS, fingerprint_dataframe
from .llm import _sketch
from .ops import parse_dates_to_datetime
from .recipe import Recipe
from .types import Severity

_RENAME_CONFIDENCE = 0.6


@dataclass
class DriftFinding:
    kind: str
    message: str
    severity: Severity = Severity.WARNING
    column: str | None = None
    suggestion: str | None = None
    evidence: dict = field(default_factory=dict)


@dataclass
class DriftReport:
    findings: list[DriftFinding] = field(default_factory=list)
    source: str | None = None

    @property
    def has_drift(self) -> bool:
        return any(f.severity.rank >= Severity.WARNING.rank for f in self.findings)

    @property
    def worst(self) -> Severity | None:
        if not self.findings:
            return None
        return max((f.severity for f in self.findings), key=lambda s: s.rank)

    def by_kind(self, kind: str) -> list[DriftFinding]:
        return [f for f in self.findings if f.kind == kind]

    def render(self) -> str:
        if not self.findings:
            where = f" in {self.source}" if self.source else ""
            return f"✓ No schema drift detected{where}."
        where = f" in {self.source}" if self.source else ""
        lines = [f"⚠ Schema drift detected{where}"]
        for f in self.findings:
            lines.append(f"  • {f.message}")
            if f.suggestion:
                lines.append(f"    ↳ {f.suggestion}")
        lines.append(
            "  Run `cleanframe suggest <file> --recipe <recipe.yaml> --update` to review a patch."
        )
        return "\n".join(lines)

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"DriftReport({len(self.findings)} finding(s), has_drift={self.has_drift})"


def detect_drift(df: pd.DataFrame, recipe: Recipe, *, source: str | None = None) -> DriftReport:
    """Compare an incoming frame against the expectations baked into ``recipe``."""
    report = DriftReport(source=source)
    fp = recipe.source_fingerprint or {}
    expected_cols: list[str] = list(fp.get("column_names", []))
    expected_dtypes: dict[str, str] = dict(fp.get("dtypes", {}))
    actual_cols = [str(c) for c in df.columns]

    missing = [c for c in expected_cols if c not in actual_cols]
    new = [c for c in actual_cols if c not in expected_cols]

    # -- new columns: try to explain each as a rename ------------------
    for col in new:
        candidate, score = best_match(col, missing + [c.output_name for c in recipe.columns])
        if candidate and score >= _RENAME_CONFIDENCE:
            report.findings.append(
                DriftFinding(
                    kind="renamed_column",
                    message=f'Column "{col}" is new — {score:.0%} match to recipe column "{candidate}"',
                    severity=Severity.WARNING,
                    column=col,
                    suggestion=f'If "{col}" replaces "{candidate}", update the recipe to map it.',
                    evidence={"match": candidate, "score": round(score, 3)},
                )
            )
        else:
            report.findings.append(
                DriftFinding(
                    kind="new_column",
                    message=f'Column "{col}" is new and unrecognised',
                    severity=Severity.WARNING,
                    column=col,
                    suggestion="Confirm the new column is expected, or drop/ignore it before apply.",
                    evidence={},
                )
            )

    # -- missing columns not explained by a rename --------------------
    explained = {f.evidence.get("match") for f in report.by_kind("renamed_column")}
    for col in missing:
        if col in explained:
            continue
        report.findings.append(
            DriftFinding(
                kind="missing_column",
                message=f'Column "{col}" expected by the recipe is missing',
                severity=Severity.WARNING,
                column=col,
                suggestion=_closest_hint(col, new),
                evidence={},
            )
        )

    # -- dtype changes (canonical families — object vs str is not drift) -
    for col in actual_cols:
        if col not in expected_dtypes:
            continue
        was = expected_dtypes[col]
        now = str(df[col].dtype)
        if canonicalize_dtype(was) == canonicalize_dtype(now):
            continue
        report.findings.append(
            DriftFinding(
                kind="dtype_change",
                message=f'Column "{col}" changed dtype ({was} → {now})',
                severity=Severity.WARNING,
                column=col,
                suggestion="Re-plan or update casts if ops assume the old dtype.",
                evidence={"was": was, "now": now},
            )
        )

    # -- content fingerprint (row_count / hash_sample) ------------------
    _detect_content_drift(df, fp, report)

    # -- value-level format drift (declared parse_date formats) --------
    _detect_format_drift(df, recipe, report)

    return report


def _detect_content_drift(df: pd.DataFrame, fp: dict, report: DriftReport) -> None:
    """Compare stored content fingerprint fields when present.

    Row-count and sample-hash changes are expected for monthly files, so they are
    INFO (visible in the report, do not stop apply). Schema / dtype / format
    findings remain the apply-blocking signals. Same row count + different hash is
    called out in the message so operators can spot a possible wrong-file swap.
    """
    expected_rows = fp.get("row_count")
    expected_hash = fp.get("hash_sample")
    if expected_rows is None and expected_hash is None:
        return

    sample_rows = int(fp.get("sampled_rows") or DEFAULT_SAMPLE_ROWS)
    actual_fp = fingerprint_dataframe(df, sample_rows=sample_rows)
    actual_rows = actual_fp["row_count"]
    actual_hash = actual_fp["hash_sample"]

    rows_changed = expected_rows is not None and int(expected_rows) != int(actual_rows)
    hash_changed = expected_hash is not None and str(expected_hash) != str(actual_hash)

    if rows_changed:
        report.findings.append(
            DriftFinding(
                kind="row_count_change",
                message=f"Row count changed ({expected_rows} → {actual_rows})",
                severity=Severity.INFO,
                evidence={"was": int(expected_rows), "now": int(actual_rows)},
            )
        )

    if hash_changed:
        same_n = expected_rows is not None and int(expected_rows) == int(actual_rows)
        report.findings.append(
            DriftFinding(
                kind="content_hash_change",
                message=(
                    "Leading-row content hash changed "
                    f"({expected_hash} → {actual_hash})"
                ),
                severity=Severity.INFO,
                suggestion=(
                    "Same row count but different sample values — confirm this is "
                    "the intended file (not a schema-matched swap)."
                    if same_n
                    else None
                ),
                evidence={"was": str(expected_hash), "now": str(actual_hash)},
            )
        )


def _detect_format_drift(df: pd.DataFrame, recipe: Recipe, report: DriftReport) -> None:
    for col_recipe in recipe.columns:
        src = col_recipe.source
        if src not in df.columns:
            continue
        for op in col_recipe.ops:
            if op.name != "parse_date":
                continue
            formats = op.params.get("formats")
            if not formats:
                continue
            series = df[src].dropna()
            if series.empty:
                continue
            parsed = parse_dates_to_datetime(
                series,
                list(formats),
                dayfirst=bool(op.params.get("dayfirst", False)),
                yearfirst=bool(op.params.get("yearfirst", False)),
            )
            unmatched = series[parsed.isna()]
            if len(unmatched):
                examples = [str(v) for v in unmatched.head(3).tolist()]
                sketches = sorted({_sketch(e) for e in examples})
                report.findings.append(
                    DriftFinding(
                        kind="format_drift",
                        message=(
                            f'{len(unmatched)} value(s) in "{src}" match no allowed date format '
                            f"(new: {examples[0]!r})"
                        ),
                        severity=Severity.WARNING,
                        column=src,
                        suggestion=f"New pattern {sketches[0]!r}; add its format to the recipe.",
                        evidence={
                            "count": int(len(unmatched)),
                            "examples": examples,
                            "sketches": sketches,
                        },
                    )
                )


def _closest_hint(col: str, candidates: list[str]) -> str | None:
    if not candidates:
        return None
    best = max(candidates, key=lambda c: similarity(col, c))
    score = similarity(col, best)
    if score >= 0.4:
        return f'Did a new column "{best}" ({score:.0%} match) replace it?'
    return None


__all__ = ["detect_drift", "DriftReport", "DriftFinding"]
