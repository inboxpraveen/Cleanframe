"""Detected problems and the fixes proposed for them.

A detector's job is to return :class:`Issues` — a small, ergonomic collection of
:class:`Issue` objects. Each issue may carry a :class:`Proposal`: the ops and/or
rename that would fix it. The planner later folds proposals into a recipe.

This separation is deliberate. Detectors own *domain knowledge* ("these three
spellings are the same city"); the planner owns *policy* ("include this fix only
if confidence clears the bar for the current mode"). Keeping proposals on the
issue means a rules-only run and an LLM-assisted run share the exact same fix
vocabulary.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

from .types import Op, Severity


def _cap_examples(values: Iterable[Any], limit: int = 5) -> list[Any]:
    """Take up to ``limit`` example values for evidence, preserving order."""
    out: list[Any] = []
    for v in values:
        out.append(v)
        if len(out) >= limit:
            break
    return out


@dataclass
class Proposal:
    """A concrete, replayable fix for an issue: an optional rename plus ops.

    ``ops`` are applied in order to the column. ``rename_to`` (if set) becomes the
    column's output name. The planner may merge proposals from several issues that
    target the same column, concatenating their ops in detector-priority order.
    """

    rename_to: str | None = None
    ops: list[Op] = field(default_factory=list)

    def is_empty(self) -> bool:
        return self.rename_to is None and not self.ops


@dataclass
class Issue:
    """One detected problem, optionally with a proposed fix.

    Attributes
    ----------
    detector:
        Name of the detector that raised it (stamped automatically by the runner).
    column:
        Column the issue pertains to, or ``None`` for dataset-level issues
        (duplicates, for example). Stamped by the runner for column detectors.
    kind:
        A stable machine slug (``"mixed_date_formats"``) used by reports and tests.
    severity:
        :class:`~cleanframe.types.Severity`.
    message:
        Human-readable, one line.
    confidence:
        How sure the detector is, in ``[0, 1]``. The planner gates on this.
    evidence:
        Small JSON-serialisable dict of counts and example values for the report.
    proposal:
        The fix, or ``None`` if the issue is informational / needs a human.
    """

    kind: str
    message: str
    severity: Severity = Severity.WARNING
    column: str | None = None
    detector: str = ""
    confidence: float = 1.0
    evidence: dict[str, Any] = field(default_factory=dict)
    proposal: Proposal | None = None

    @property
    def has_fix(self) -> bool:
        return self.proposal is not None and not self.proposal.is_empty()


class Issues:
    """An ordered, list-like collection of :class:`Issue` objects.

    This is the public return type for detectors (``def detect(series) -> Issues``).
    The :meth:`add` helper is the ergonomic path — it builds an :class:`Issue`,
    appends it, and returns it so evidence can be tweaked inline::

        issues = cf.Issues()
        issues.add("bad_email", "3 invalid addresses", severity=cf.Severity.ERROR)
    """

    def __init__(self, items: Iterable[Issue] | None = None) -> None:
        self._items: list[Issue] = list(items) if items else []

    # -- construction ----------------------------------------------------
    def add(
        self,
        kind: str,
        message: str,
        *,
        severity: Severity | str = Severity.WARNING,
        column: str | None = None,
        confidence: float = 1.0,
        evidence: dict[str, Any] | None = None,
        proposal: Proposal | None = None,
        ops: list[Op] | None = None,
        rename_to: str | None = None,
    ) -> Issue:
        """Build, append, and return an :class:`Issue`.

        ``ops`` / ``rename_to`` are a shorthand for constructing a
        :class:`Proposal`; passing them alongside an explicit ``proposal`` is an
        error to avoid silently dropping one.
        """
        if proposal is not None and (ops or rename_to):
            raise ValueError("Pass either `proposal` or `ops`/`rename_to`, not both.")
        if ops or rename_to:
            proposal = Proposal(rename_to=rename_to, ops=list(ops or []))
        if not isinstance(severity, Severity):
            severity = Severity(str(severity))
        issue = Issue(
            kind=kind,
            message=message,
            severity=severity,
            column=column,
            confidence=confidence,
            evidence=evidence or {},
            proposal=proposal,
        )
        self._items.append(issue)
        return issue

    def append(self, issue: Issue) -> None:
        self._items.append(issue)

    def extend(self, other: Iterable[Issue]) -> None:
        self._items.extend(other)

    # -- querying --------------------------------------------------------
    def for_column(self, column: str | None) -> Issues:
        return Issues(i for i in self._items if i.column == column)

    def by_severity(self, minimum: Severity) -> Issues:
        return Issues(i for i in self._items if i.severity.rank >= minimum.rank)

    def with_fixes(self) -> Issues:
        return Issues(i for i in self._items if i.has_fix)

    def columns(self) -> list[str]:
        seen: list[str] = []
        for i in self._items:
            if i.column is not None and i.column not in seen:
                seen.append(i.column)
        return seen

    @property
    def max_severity(self) -> Severity | None:
        if not self._items:
            return None
        return max((i.severity for i in self._items), key=lambda s: s.rank)

    # -- dunder ----------------------------------------------------------
    def __iter__(self) -> Iterator[Issue]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __getitem__(self, idx: int) -> Issue:
        return self._items[idx]

    def __bool__(self) -> bool:
        return bool(self._items)

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"Issues({len(self._items)} issue(s))"


__all__ = ["Issue", "Issues", "Proposal", "_cap_examples"]
