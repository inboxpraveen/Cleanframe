"""The detector plugin system.

A **detector** inspects data and returns :class:`~cleanframe.issues.Issues`, each
optionally carrying a :class:`~cleanframe.issues.Proposal` (the fix). Detectors are
the extension point CleanFrame is built around — the community owns the long tail
of messy-data weirdness by writing ~15-line detectors::

    @cf.detector("iban")
    def detect_iban(series):
        issues = cf.Issues()
        ...
        return issues

Signatures are flexible. A detector takes the data object for its scope
(``series`` for column detectors, ``df`` for frame detectors) and, optionally, a
second :class:`DetectorContext` argument when it needs the column name, the
profile, or a target schema. The runner introspects the signature and passes what
you ask for, so the minimal form above just works.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

import pandas as pd

from ..issues import Issue, Issues
from ..profile import ColumnProfile, DataFrameProfile


@dataclass
class DetectorContext:
    """Everything a detector might want beyond the raw data object.

    Column detectors get ``column``/``series``/``column_profile`` set; frame
    detectors get them as ``None``. ``schema`` is present only when the caller
    passed a target schema. ``options`` carries user knobs (region for phones, a
    seed alias map for categories, thresholds, …).
    """

    df: pd.DataFrame
    profile: DataFrameProfile
    column: str | None = None
    series: pd.Series | None = None
    column_profile: ColumnProfile | None = None
    schema: Any | None = None
    options: dict[str, Any] | None = None

    def option(self, key: str, default: Any = None) -> Any:
        return (self.options or {}).get(key, default)


@dataclass
class DetectorSpec:
    name: str
    func: Callable[..., Any]
    scope: str  # "column" | "frame"
    priority: int
    requires_schema: bool
    wants_ctx: bool
    doc: str = ""


DETECTOR_REGISTRY: dict[str, DetectorSpec] = {}


def _wants_ctx(func: Callable) -> bool:
    """Does this detector accept a second (context) argument?"""
    try:
        params = [
            p
            for p in inspect.signature(func).parameters.values()
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
        ]
    except (TypeError, ValueError):  # pragma: no cover - builtins without signatures
        return False
    if any(p.name == "ctx" for p in params):
        return True
    return len(params) >= 2


def detector(
    name: str,
    *,
    scope: str = "column",
    priority: int = 100,
    requires_schema: bool = False,
) -> Callable[[Callable], Callable]:
    """Register a detector under ``name``.

    Parameters
    ----------
    scope:
        ``"column"`` (called once per column with its Series) or ``"frame"``
        (called once with the whole DataFrame).
    priority:
        Lower runs earlier. Only affects the *order issues are reported*; the
        planner orders the resulting ops canonically, so priority is about
        presentation, not correctness.
    requires_schema:
        If true, the detector is skipped unless a target schema was supplied.
    """

    if scope not in ("column", "frame"):
        raise ValueError(f"scope must be 'column' or 'frame', got {scope!r}")

    def decorator(func: Callable) -> Callable:
        if name in DETECTOR_REGISTRY:
            raise ValueError(f"Detector {name!r} is already registered.")
        DETECTOR_REGISTRY[name] = DetectorSpec(
            name=name,
            func=func,
            scope=scope,
            priority=priority,
            requires_schema=requires_schema,
            wants_ctx=_wants_ctx(func),
            doc=func.__doc__ or "",
        )
        return func

    return decorator


def unregister_detector(name: str) -> None:
    """Remove a detector (used by tests and for overriding a built-in)."""
    DETECTOR_REGISTRY.pop(name, None)


def list_detectors() -> list[str]:
    return sorted(DETECTOR_REGISTRY)


def _normalize_result(result: Any) -> Iterable[Issue]:
    if result is None:
        return []
    if isinstance(result, Issues):
        return list(result)
    if isinstance(result, Issue):
        return [result]
    if isinstance(result, (list, tuple)):
        return [r for r in result if isinstance(r, Issue)]
    raise TypeError(
        f"A detector must return Issues / Issue / list / None, got {type(result).__name__}."
    )


def _invoke(spec: DetectorSpec, data: Any, ctx: DetectorContext) -> Iterable[Issue]:
    result = spec.func(data, ctx) if spec.wants_ctx else spec.func(data)
    return _normalize_result(result)


def run_detectors(
    df: pd.DataFrame,
    *,
    profile: DataFrameProfile | None = None,
    schema: Any | None = None,
    options: dict[str, Any] | None = None,
    only: Iterable[str] | None = None,
) -> Issues:
    """Run every applicable detector and return the aggregated, stamped issues.

    Deterministic: detectors run in ``(priority, name)`` order and columns in frame
    order. Each issue is stamped with its detector name (and column, for column
    detectors) if the detector didn't set them.
    """
    from ..profile import profile_dataframe  # local import avoids a cycle at module load

    profile = profile or profile_dataframe(df)
    options = options or {}
    only_set = set(only) if only is not None else None
    issues = Issues()

    specs = sorted(DETECTOR_REGISTRY.values(), key=lambda s: (s.priority, s.name))
    for spec in specs:
        if only_set is not None and spec.name not in only_set:
            continue
        if spec.requires_schema and schema is None:
            continue

        if spec.scope == "column":
            for col in df.columns:
                col_name = str(col)
                ctx = DetectorContext(
                    df=df,
                    profile=profile,
                    column=col_name,
                    series=df[col],
                    column_profile=profile.column(col_name),
                    schema=schema,
                    options=options,
                )
                for issue in _invoke(spec, df[col], ctx):
                    issue.detector = issue.detector or spec.name
                    if issue.column is None:
                        issue.column = col_name
                    issues.append(issue)
        else:
            ctx = DetectorContext(
                df=df, profile=profile, schema=schema, options=options
            )
            for issue in _invoke(spec, df, ctx):
                issue.detector = issue.detector or spec.name
                issues.append(issue)

    return issues


__all__ = [
    "DetectorContext",
    "DetectorSpec",
    "DETECTOR_REGISTRY",
    "detector",
    "unregister_detector",
    "list_detectors",
    "run_detectors",
]
