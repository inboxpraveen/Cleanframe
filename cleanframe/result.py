"""User-facing result objects returned by the high-level API.

:class:`CleanResult` is what ``cf.clean(...)`` hands back — the cleaned dataframe
plus every durable artifact (recipe, exportable code, diff, quarantine, report) and
convenience savers so the README's ergonomics work verbatim::

    result = cf.clean(df, ...)
    result.diff.show()
    result.recipe.save("customer.recipe.yaml")
    result.code.save("clean_customers.py")
    clean_df = result.dataframe
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from ._util import write_text
from .codegen import generate_code
from .dataio import write_frame
from .diff import CellDiff
from .drift import DriftReport
from .issues import Issues
from .profile import DataFrameProfile
from .quality import QualityScore
from .recipe import Recipe
from .report import render_clean_report, render_profile_report
from .validate import ValidationResult


class CodeArtifact:
    """Lazily-generated standalone pandas code for a recipe. ``.save()`` writes ``.py``."""

    def __init__(self, recipe: Recipe, func_name: str = "clean") -> None:
        self._recipe = recipe
        self._func_name = func_name
        self._cached: str | None = None

    def to_string(self) -> str:
        if self._cached is None:
            self._cached = generate_code(self._recipe, func_name=self._func_name)
        return self._cached

    def save(self, path: str | Path) -> Path:
        return write_text(path, self.to_string())

    def __str__(self) -> str:
        return self.to_string()


class Report:
    """A rendered HTML report. ``.save()`` writes it; displays inline in Jupyter."""

    def __init__(self, html: str, quality: QualityScore | None = None) -> None:
        self.html = html
        self.quality = quality

    def save(self, path: str | Path) -> Path:
        return write_text(path, self.html)

    def _repr_html_(self) -> str:  # pragma: no cover - Jupyter hook
        return self.html

    def __str__(self) -> str:
        return self.html


@dataclass
class CleanResult:
    """The full outcome of a clean: cleaned data + every artifact needed to reproduce it."""

    dataframe: pd.DataFrame
    recipe: Recipe
    diff: CellDiff
    quarantine: pd.DataFrame = field(default_factory=pd.DataFrame)
    issues: Issues = field(default_factory=Issues)
    profile: DataFrameProfile | None = None
    validation_results: list[ValidationResult] = field(default_factory=list)
    quality: QualityScore | None = None
    source: str | None = None
    log: list[str] = field(default_factory=list)
    drift: DriftReport | None = None

    @property
    def code(self) -> CodeArtifact:
        return CodeArtifact(self.recipe)

    @property
    def has_quarantine(self) -> bool:
        return not self.quarantine.empty

    def show(self, **kwargs) -> None:
        """Print the cell-level diff, git-diff style."""
        self.diff.show(**kwargs)

    def report(self, path: str | Path | None = None) -> Report:
        """Build the HTML cleaning report (diff + quarantine); optionally save it."""
        html = render_clean_report(
            self.diff, quarantine=self.quarantine, source=self.source, quality=self.quality
        )
        report = Report(html, self.quality)
        if path is not None:
            report.save(path)
        return report

    def summary(self) -> dict:
        s = self.diff.summary()
        s["quarantined"] = int(len(self.quarantine))
        if self.quality is not None:
            s["quality_before"] = self.quality.score
        return s

    def save_all(self, prefix: str | Path) -> dict[str, Path]:
        """Save recipe, code, cleaned data, and report next to ``prefix``."""
        prefix = Path(prefix)
        paths = {
            "recipe": self.recipe.save(prefix.with_suffix(".recipe.yaml")),
            "code": self.code.save(prefix.with_suffix(".py")),
            "clean": _save_csv(self.dataframe, prefix.with_suffix(".clean.csv")),
            "report": self.report().save(prefix.with_suffix(".report.html")),
        }
        if self.has_quarantine:
            paths["quarantine"] = write_frame(
                self.quarantine, prefix.with_suffix(".quarantine.csv")
            )
        return paths


def _save_csv(df: pd.DataFrame, path: Path) -> Path:
    return write_frame(df, path)


def build_profile_report_object(
    profile: DataFrameProfile, issues: Issues, *, source: str | None = None,
    quality: QualityScore | None = None,
) -> Report:
    html = render_profile_report(profile, issues, source=source, quality=quality)
    return Report(html, quality)


__all__ = ["CleanResult", "Report", "CodeArtifact", "build_profile_report_object"]
