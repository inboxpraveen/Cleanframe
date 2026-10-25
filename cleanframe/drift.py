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

import math
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from ._util import best_match, canonicalize_dtype, similarity
from .errors import CleanFrameError
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
    if not isinstance(fp, dict):
        raise CleanFrameError(
            f"recipe source_fingerprint must be a mapping, got {type(fp).__name__}."
        )
    raw_cols = fp.get("column_names") or []
    if not isinstance(raw_cols, (list, tuple)):
        raise CleanFrameError("recipe source_fingerprint.column_names must be a list.")
    raw_dtypes = fp.get("dtypes") or {}
    if not isinstance(raw_dtypes, dict):
        raise CleanFrameError("recipe source_fingerprint.dtypes must be a mapping.")
    expected_cols: list[str] = [str(c) for c in raw_cols]
    expected_dtypes: dict[str, str] = {str(k): str(v) for k, v in raw_dtypes.items()}
    actual_cols = [str(c) for c in df.columns]

    # A fingerprint without column names (hand-written recipe) says nothing about
    # structure; comparing against an empty list would call every column new.
    missing = [c for c in expected_cols if c not in actual_cols] if expected_cols else []
    new = [c for c in actual_cols if c not in expected_cols] if expected_cols else []

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

    try:
        sample_rows = int(fp.get("sampled_rows") or DEFAULT_SAMPLE_ROWS)
    except (TypeError, ValueError):
        sample_rows = DEFAULT_SAMPLE_ROWS
    actual_fp = fingerprint_dataframe(df, sample_rows=sample_rows)
    actual_rows = actual_fp["row_count"]
    actual_hash = actual_fp["hash_sample"]

    try:
        rows_changed = expected_rows is not None and int(expected_rows) != int(actual_rows)
    except (TypeError, ValueError):
        rows_changed = False
        expected_rows = None
    hash_changed = expected_hash is not None and str(expected_hash) != str(actual_hash)

    if rows_changed:
        report.findings.append(
            DriftFinding(
                kind="row_count_change",
                message=f"Row count changed ({expected_rows} → {actual_rows})",
                severity=Severity.INFO,
                evidence={"was": int(expected_rows), "now": int(actual_rows)},  # type: ignore[arg-type]
            )
        )

    if hash_changed:
        same_n = bool(expected_rows is not None and int(expected_rows) == int(actual_rows))
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


#: Fingerprint key holding, per ``parse_date`` column, the shapes of the values the
#: recipe's formats could not parse when it was learned, with their share of the
#: column (``{"Signup Date": {"9+": 0.12}}``). A replay of the same data then reports
#: no drift; only a new shape, or a known one that became far more common, does.
#: Shapes are structural sketches (digits -> 9, letters -> A), never values.
UNPARSED_DATES_KEY = "unparsed_date_patterns"
_MAX_LEARNED_SHAPES = 20
_EXAMPLES_PER_SHAPE = 3


@dataclass
class UnparsedDates:
    """Values a ``parse_date`` op's formats do not parse, counted by shape."""

    counts: dict[str, int] = field(default_factory=dict)
    examples: dict[str, list[str]] = field(default_factory=dict)
    non_null: int = 0

    def add(self, other: UnparsedDates) -> None:
        self.non_null += other.non_null
        for shape, n in other.counts.items():
            self.counts[shape] = self.counts.get(shape, 0) + n
            kept = self.examples.setdefault(shape, [])
            kept.extend(other.examples.get(shape, [])[: _EXAMPLES_PER_SHAPE - len(kept)])


def _date_ops(recipe: Recipe):
    for col_recipe in recipe.columns:
        for op in col_recipe.ops:
            if op.name == "parse_date" and op.params.get("formats"):
                yield col_recipe.source, op


def unparsed_dates(series: pd.Series, op: Any) -> UnparsedDates:
    """Count the values of ``series`` that ``op``'s formats do not parse, by shape."""
    values = series.dropna()
    out = UnparsedDates(non_null=int(len(values)))
    if values.empty:
        return out
    parsed = parse_dates_to_datetime(
        values,
        list(op.params.get("formats") or []),
        dayfirst=bool(op.params.get("dayfirst", False)),
        yearfirst=bool(op.params.get("yearfirst", False)),
    )
    unmatched = values[parsed.isna().to_numpy()]
    if unmatched.empty:
        return out
    # One sketch per distinct value: a column of a million identical bad cells is cheap.
    for value, n in unmatched.astype(str).value_counts(sort=False).items():
        shape = _sketch(str(value))
        out.counts[shape] = out.counts.get(shape, 0) + int(n)
        kept = out.examples.setdefault(shape, [])
        if len(kept) < _EXAMPLES_PER_SHAPE:
            kept.append(str(value))
    return out


def learn_unparsed_dates(df: pd.DataFrame, recipe: Recipe) -> None:
    """Record in the fingerprint what the recipe's date formats leave unparsed today.

    Called once when a recipe is planned (and again when it is patched for drift), so
    the values a user already accepted as unparseable are not reported as drift on
    every replay. Leaves the fingerprint untouched when every date parses.
    """
    fp = recipe.source_fingerprint
    if not isinstance(fp, dict):
        return
    learned: dict[str, dict[str, float]] = {}
    for src, op in _date_ops(recipe):
        if src not in df.columns:
            continue
        seen = unparsed_dates(df[src], op)
        if not seen.counts or not seen.non_null:
            continue
        top = sorted(seen.counts.items(), key=lambda kv: (-kv[1], kv[0]))[:_MAX_LEARNED_SHAPES]
        learned[str(src)] = {shape: round(n / seen.non_null, 4) for shape, n in sorted(top)}
    if learned:
        fp[UNPARSED_DATES_KEY] = learned
    else:
        fp.pop(UNPARSED_DATES_KEY, None)


def _learned_shapes(recipe: Recipe, src: str) -> dict[str, float]:
    fp = recipe.source_fingerprint if isinstance(recipe.source_fingerprint, dict) else {}
    raw = (fp.get(UNPARSED_DATES_KEY) or {}).get(str(src)) or {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float] = {}
    for shape, share in raw.items():
        try:
            out[str(shape)] = float(share)
        except (TypeError, ValueError):
            continue
    return out


def format_drift_finding(src: str, seen: UnparsedDates, learned: dict[str, float]) -> DriftFinding | None:
    """The finding for unparsed dates, or ``None`` when they match what was learned.

    A shape the recipe never met is a new format. A known shape counts again only
    when it is much more common than it was (at least 3 values, and over twice its
    learned share plus 5 points): a source switching from DD/MM to MM/DD shows up
    that way, while the same stray ``31/02/2024`` in every file does not.
    """
    if not seen.counts or not seen.non_null:
        return None
    new = {s: n for s, n in seen.counts.items() if s not in learned}
    grown = {
        s: n
        for s, n in seen.counts.items()
        if s in learned and n >= 3 and n / seen.non_null > max(2 * learned[s], learned[s] + 0.05)
    }
    flagged = {**grown, **new}
    if not flagged:
        return None
    shapes = sorted(flagged, key=lambda s: (-flagged[s], s))
    examples = [e for s in shapes for e in seen.examples.get(s, [])][:_EXAMPLES_PER_SHAPE]
    count = sum(flagged.values())
    if new:
        message = f'{count} value(s) in "{src}" match no allowed date format (new: {examples[0]!r})'
        suggestion = f"New pattern {sorted(new)[0]!r}; add its format to the recipe."
    else:
        message = (
            f'{count} value(s) in "{src}" match no allowed date format - far more than when '
            f"the recipe was learned (e.g. {examples[0]!r})"
        )
        suggestion = "Check whether the source changed its date format, then re-plan or patch."
    return DriftFinding(
        kind="format_drift",
        message=message,
        severity=Severity.WARNING,
        column=src,
        suggestion=suggestion,
        evidence={"count": int(count), "examples": examples, "sketches": sorted(flagged)},
    )


def _detect_format_drift(df: pd.DataFrame, recipe: Recipe, report: DriftReport) -> None:
    for col_recipe in recipe.columns:
        src = col_recipe.source
        if src not in df.columns:
            continue
        for op in col_recipe.ops:
            if op.name == "parse_number":
                _detect_number_drift(df[src], src, op.params, report)
            elif op.name == "parse_date" and op.params.get("formats"):
                finding = format_drift_finding(
                    src, unparsed_dates(df[src], op), _learned_shapes(recipe, src)
                )
                if finding is not None:
                    report.findings.append(finding)


#: Share of digit-bearing values a ``parse_number`` op may reject before the column
#: counts as drifted (a stray "12ab34" is data noise; "1.200,50" everywhere is a
#: different number format).
_NUMBER_DRIFT_BLOCK_RATE = 0.05
_NUMBER_SAMPLE = 20_000


def _detect_number_drift(series: pd.Series, src: str, params: dict, report: DriftReport) -> None:
    """Flag a column whose numbers no longer fit the recipe's ``parse_number`` format.

    Without this a European ``1.200,50`` replayed through a ``.``-decimal recipe is a
    silent-wrong-number risk. Only values that *contain a digit* and still fail to
    parse count — ``N/A`` and blanks are missing data, not a new format.
    """
    from ._numparse import parse_number_text

    decimal = str(params.get("decimal", "."))
    thousands = str(params.get("thousands", ","))
    symbols = tuple(str(x) for x in (params.get("symbols") or ()))
    values = [v for v in series.dropna().head(_NUMBER_SAMPLE).tolist() if isinstance(v, str)]
    numeric = [v for v in values if any(ch.isdigit() for ch in v)]
    if not numeric:
        return
    rejected = [
        v for v in numeric if math.isnan(parse_number_text(v, decimal, thousands, symbols))
    ]
    if not rejected:
        return
    rate = len(rejected) / len(numeric)
    examples = [str(v) for v in rejected[:3]]
    # A share alone is too twitchy on a small file (one "$10-20" in 20 rows is 5%): a stop
    # needs a few offenders, or most of the column.
    blocking = rate >= 0.5 or (rate >= _NUMBER_DRIFT_BLOCK_RATE and len(rejected) >= 3)
    report.findings.append(
        DriftFinding(
            kind="number_format_drift",
            message=(
                f'{len(rejected)} of {len(numeric)} numeric value(s) in "{src}" do not fit the '
                f"recipe's number format (decimal {decimal!r}, thousands {thousands!r}; "
                f"new: {examples[0]!r})"
            ),
            severity=Severity.WARNING if blocking else Severity.INFO,
            column=src,
            suggestion=(
                "Set the op's decimal/thousands to the new convention "
                "(e.g. decimal: ',' thousands: '.') or re-plan."
            ),
            evidence={"count": len(rejected), "rate": round(rate, 4), "examples": examples},
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
