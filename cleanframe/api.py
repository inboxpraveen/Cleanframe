"""The high-level API: ``clean``, ``report``, ``apply_recipe``, ``suggest_update``.

These stitch the pipeline stages (profile → detect → plan → execute) into the few
calls most users ever touch. Every function accepts either a DataFrame or a path,
and returns rich result objects rather than bare frames so the recipe, diff, and
report are always one attribute away.
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any

import pandas as pd

from .dataio import read_frame
from .detectors import run_detectors
from .drift import DriftReport, detect_drift
from .errors import CleanFrameError, DriftError
from .executor import execute
from .issues import Issues
from .planner import Planner, RulesPlanner
from .profile import profile_dataframe
from .quality import quality_score
from .recipe import Recipe
from .result import CleanResult, Report, build_profile_report_object
from .schema import Schema
from .schema import infer_schema as _infer_schema
from .types import Mode


# ---------------------------------------------------------------------------
# input coercion
# ---------------------------------------------------------------------------
def _read_binding(sheet, columns, nrows, skiprows) -> dict[str, Any]:
    """Collect the non-default read/selection options into a dict (empty if none)."""
    binding = {"sheet": sheet, "columns": columns, "nrows": nrows, "skiprows": skiprows}
    return {k: v for k, v in binding.items() if v is not None}


def _read_input(
    data, source, *, sheet, columns, nrows, skiprows, correct_format, warn=False
) -> tuple[pd.DataFrame, str | None, dict[str, Any], list[str]]:
    """Read ``data`` with optional CSV format auto-correction.

    Returns ``(df, source, read_binding, notes)`` where ``read_binding`` is the full
    selection+format binding to record in a recipe's ``read:`` section.
    """
    fmt_kwargs: dict[str, Any] = {}
    notes: list[str] = []
    eff_skiprows = skiprows
    if correct_format and isinstance(data, (str, Path)):
        from .readfix import detect_csv_options, is_csv_family

        if is_csv_family(data):
            opts, report_ = detect_csv_options(data)  # raises on ambiguous delimiter
            fmt_kwargs = report_.as_read_binding()  # {encoding?, sep?}
            notes = report_.notes
            if "skiprows" in opts and skiprows is None:
                eff_skiprows = opts["skiprows"]
            if warn and notes:
                warnings.warn(
                    "CleanFrame: read-time format correction — " + "; ".join(notes), stacklevel=3
                )
    df, source = _as_frame(
        data, source, sheet=sheet, columns=columns, nrows=nrows, skiprows=eff_skiprows, **fmt_kwargs
    )
    binding = _read_binding(sheet, columns, nrows, eff_skiprows)
    binding.update(fmt_kwargs)
    return df, source, binding, notes


def _as_frame(
    data: pd.DataFrame | str | Path,
    source: str | None,
    *,
    sheet=None,
    columns=None,
    nrows=None,
    skiprows=None,
    **read_kwargs,
) -> tuple[pd.DataFrame, str | None]:
    from ._util import ensure_string_columns

    if isinstance(data, pd.DataFrame):
        if sheet is not None or nrows is not None or skiprows is not None:
            raise CleanFrameError(
                "sheet=/nrows=/skiprows= selection applies to file inputs only, "
                "not an in-memory DataFrame. Slice the DataFrame yourself first."
            )
        df = data
        if columns is not None:  # a column projection is well-defined on a DataFrame
            df = df[[c for c in columns if c in df.columns]]
        return ensure_string_columns(df), source  # read_kwargs (encoding/sep) are file-only
    if isinstance(data, (str, Path)):
        df = read_frame(
            data, sheet=sheet, columns=columns, nrows=nrows, skiprows=skiprows, **read_kwargs
        )
        return ensure_string_columns(df), source or str(data)
    raise CleanFrameError(f"Expected a DataFrame or file path, got {type(data).__name__}.")


def _resolve_schema(schema: Any) -> Schema | None:
    if schema is None or isinstance(schema, Schema):
        return schema
    if isinstance(schema, (str, Path)):
        return Schema.load(schema)
    if isinstance(schema, dict):
        return Schema.from_dict(schema)
    raise CleanFrameError(f"Unsupported schema type: {type(schema).__name__}.")


def _resolve_recipe(recipe: Any) -> Recipe:
    if isinstance(recipe, Recipe):
        return recipe
    if isinstance(recipe, (str, Path)):
        return Recipe.load(recipe)
    if isinstance(recipe, dict):
        return Recipe.from_dict(recipe)
    raise CleanFrameError(f"Unsupported recipe type: {type(recipe).__name__}.")


def _resolve_planner(
    planner: Planner | None,
    llm: Any,
    llm_exposure: str,
    max_tokens_budget: int | None,
) -> Planner:
    if planner is not None:
        return planner
    if llm is None:
        return RulesPlanner()
    from .llm import LLMPlanner, get_client

    if isinstance(llm, str):
        client = get_client(llm)
    elif hasattr(llm, "complete"):
        client = llm
    else:
        raise CleanFrameError(
            "llm must be a 'provider/model' string or an object with a .complete() method."
        )
    return LLMPlanner(client, exposure=llm_exposure, max_tokens_budget=max_tokens_budget)


# ---------------------------------------------------------------------------
# clean
# ---------------------------------------------------------------------------
def clean(
    data: pd.DataFrame | str | Path,
    *,
    target_schema: Any = None,
    schema: Any = None,
    llm: Any = None,
    mode: Mode | str = Mode.REVIEW,
    options: dict[str, Any] | None = None,
    planner: Planner | None = None,
    max_tokens_budget: int | None = None,
    llm_exposure: str = "metadata",
    source: str | None = None,
    sheet: str | int | None = None,
    columns: list[str] | None = None,
    nrows: int | None = None,
    skiprows: int | list[int] | None = None,
    correct_format: bool = True,
) -> CleanResult:
    """Profile, plan, and clean ``data`` — the main entry point.

    ``sheet``/``columns``/``nrows``/``skiprows`` select part of a file (see
    :func:`read_frame`); the selection is recorded in the recipe's ``read:`` section
    so :func:`apply_recipe` re-reads the same slice. For a multi-sheet workbook, pass
    ``sheet=`` or use :func:`clean_workbook` to clean every sheet.

    ``correct_format`` (default ``True``) auto-detects a CSV-family file's encoding
    and delimiter at read time (e.g. a ``;``-separated or cp1252 file), warns, and
    pins the choice into the recipe's ``read:`` section for deterministic replay. An
    ambiguous delimiter raises rather than guessing.

    Parameters
    ----------
    data:
        A DataFrame or a path to a CSV/Excel/Parquet/JSON file.
    target_schema / schema:
        Optional target :class:`~cleanframe.schema.Schema`, path, or dict. Drives
        column mapping and validation synthesis.
    llm:
        ``None`` (rules-only, default), a ``"provider/model"`` string, or any object
        with a ``.complete()`` method. The LLM only writes the recipe; it never sees
        raw data (see :mod:`cleanframe.llm`).
    mode:
        ``"review"`` (default), ``"auto"``, or ``"strict"``.
    max_tokens_budget:
        Hard cap that aborts LLM planning before it gets expensive.

    Returns
    -------
    CleanResult
        Cleaned dataframe plus recipe, diff, quarantine, issues, and report.
    """
    df, source, read_binding, read_notes = _read_input(
        data, source, sheet=sheet, columns=columns, nrows=nrows, skiprows=skiprows,
        correct_format=correct_format, warn=True,
    )
    schema_obj = _resolve_schema(target_schema if target_schema is not None else schema)
    options = dict(options or {})

    profile = profile_dataframe(df)
    issues = run_detectors(df, profile=profile, schema=schema_obj, options=options)
    the_planner = _resolve_planner(planner, llm, llm_exposure, max_tokens_budget)
    recipe = the_planner.plan(df, profile, issues, schema=schema_obj, mode=mode, options=options)

    # Record the read/selection + format binding so `apply` re-reads identically.
    if read_binding:
        recipe.read = {**(recipe.read or {}), **read_binding}

    from ._util import DEFAULT_MAX_DIFF_CHANGES

    max_diff = options.get("max_diff_changes", DEFAULT_MAX_DIFF_CHANGES)
    exec_result = execute(recipe, df, mode=mode, max_diff_changes=max_diff)
    quality = quality_score(profile, issues)

    return CleanResult(
        dataframe=exec_result.dataframe,
        recipe=recipe,
        diff=exec_result.diff,
        quarantine=exec_result.quarantine,
        issues=issues,
        profile=profile,
        validation_results=exec_result.validation_results,
        quality=quality,
        source=source,
        log=[*(f"read-fix: {n}" for n in read_notes), *exec_result.log],
    )


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------
def report(
    data: pd.DataFrame | str | Path,
    *,
    schema: Any = None,
    options: dict[str, Any] | None = None,
    source: str | None = None,
    sheet: str | int | None = None,
    columns: list[str] | None = None,
    nrows: int | None = None,
    skiprows: int | list[int] | None = None,
    correct_format: bool = True,
) -> Report:
    """Profile ``data`` and return an HTML :class:`~cleanframe.result.Report` (no changes made)."""
    df, source, _, _ = _read_input(
        data, source, sheet=sheet, columns=columns, nrows=nrows, skiprows=skiprows,
        correct_format=correct_format, warn=True,
    )
    schema_obj = _resolve_schema(schema)
    profile = profile_dataframe(df)
    issues = run_detectors(df, profile=profile, schema=schema_obj, options=options or {})
    quality = quality_score(profile, issues)
    return build_profile_report_object(profile, issues, source=source, quality=quality)


# ---------------------------------------------------------------------------
# apply (replay)
# ---------------------------------------------------------------------------
def apply_recipe(
    data: pd.DataFrame | str | Path,
    recipe: Recipe | str | Path | dict,
    *,
    mode: Mode | str = Mode.REVIEW,
    check_drift: bool = True,
    on_drift: str = "error",
    source: str | None = None,
    sheet: str | int | None = None,
    columns: list[str] | None = None,
    nrows: int | None = None,
    skiprows: int | list[int] | None = None,
) -> CleanResult:
    """Replay a saved recipe on new data — deterministic, no LLM.

    If ``check_drift`` and the incoming schema drifted, ``on_drift`` decides:
    ``"error"`` (default — raise :class:`~cleanframe.errors.DriftError` so nothing
    is silently corrupted), ``"warn"`` (attach the report and warn, then continue),
    or ``"ignore"``. ``strict`` mode always raises on drift.

    The selection used to plan the recipe (its ``read:`` section) is re-applied when
    ``data`` is a path, unless overridden by an explicit ``sheet``/``columns``/etc.
    """
    recipe_obj = _resolve_recipe(recipe)
    mode = Mode.coerce(mode)

    # Precedence: explicit call args > recipe-recorded read binding > whole file.
    call = _read_binding(sheet, columns, nrows, skiprows)
    effective = {**(recipe_obj.read or {}), **call}
    if isinstance(data, pd.DataFrame) and effective:
        unreplayable = {k for k in effective if k in ("sheet", "nrows", "skiprows")}
        if unreplayable and not call:
            warnings.warn(
                f"CleanFrame: recipe's recorded read binding {sorted(unreplayable)} cannot "
                "replay against an in-memory DataFrame; ignoring it.",
                stacklevel=2,
            )
            effective = {k: v for k, v in effective.items() if k == "columns"}
    df, source = _as_frame(data, source, **effective)

    drift: DriftReport | None = None
    if check_drift and recipe_obj.source_fingerprint:
        drift = detect_drift(df, recipe_obj, source=source)
        if drift.has_drift:
            if on_drift == "error" or mode is Mode.STRICT:
                raise DriftError(drift.render(), report=drift)
            if on_drift == "warn":
                warnings.warn("CleanFrame: " + drift.render(), stacklevel=2)

    exec_result = execute(recipe_obj, df, mode=mode)
    log = list(exec_result.log)
    if drift is not None and drift.has_drift:
        log.insert(0, "drift detected: " + "; ".join(f.message for f in drift.findings))

    return CleanResult(
        dataframe=exec_result.dataframe,
        recipe=recipe_obj,
        diff=exec_result.diff,
        quarantine=exec_result.quarantine,
        issues=Issues(),
        profile=None,
        validation_results=exec_result.validation_results,
        quality=None,
        source=source,
        log=log,
        drift=drift,
    )


# ---------------------------------------------------------------------------
# suggest --update (drift patch)
# ---------------------------------------------------------------------------
def suggest_update(
    data: pd.DataFrame | str | Path,
    recipe: Recipe | str | Path | dict,
    *,
    out: str | Path | None = None,
    source: str | None = None,
) -> tuple[Recipe, DriftReport]:
    """Return a recipe patched to accommodate drift in ``data``, plus the drift report.

    Applies safe, mechanical patches: repoint a renamed column to its new source,
    and teach ``parse_date`` any new date formats that appeared. Structural
    additions are reported but not auto-adopted — those are a human's call.
    """
    df, source = _as_frame(data, source)
    original = _resolve_recipe(recipe)
    report_ = detect_drift(df, original, source=source)
    patched = _patch_recipe_for_drift(original, report_, df)
    if out is not None:
        patched.save(out)
    return patched, report_


def _patch_recipe_for_drift(recipe: Recipe, report: DriftReport, df: pd.DataFrame) -> Recipe:
    from ._util import sample_non_null
    from .detectors.dates import _infer_formats
    from .fingerprint import fingerprint_dataframe

    patched = Recipe.from_dict(recipe.to_dict())  # deep copy
    patched.source_fingerprint = recipe.source_fingerprint
    changes: list[str] = []

    # 1) renamed columns -> repoint the recipe column's source
    for finding in report.by_kind("renamed_column"):
        new_col = finding.column
        matched = finding.evidence.get("match")
        for col in patched.columns:
            if col.output_name == matched or col.source == matched or col.rename_to == matched:
                changes.append(f"repointed {col.source!r} → {new_col!r}")
                col.source = new_col
                break

    # 2) new date formats -> extend the parse_date op
    for finding in report.by_kind("format_drift"):
        src = finding.column
        col = next((c for c in patched.columns if c.source == src), None)
        if col is None:
            continue
        for op in col.ops:
            if op.name != "parse_date" or src not in df.columns:
                continue
            existing = list(op.params.get("formats") or [])
            new_formats, _ = _infer_formats(
                [str(v) for v in sample_non_null(df[src])], op.params.get("dayfirst")
            )
            added = [f for f in new_formats if f not in existing]
            if added:
                op.params["formats"] = existing + added
                changes.append(f"added date format(s) {added} to {src!r}")

    if df is not None:
        patched.source_fingerprint = fingerprint_dataframe(df)
    patched.stamp_meta(patched_for_drift=changes or "no automatic patch applied")
    return patched


# re-export
def infer_schema(
    df: pd.DataFrame | str | Path,
    name: str | None = None,
    *,
    sheet: str | int | None = None,
    columns: list[str] | None = None,
    nrows: int | None = None,
    skiprows: int | list[int] | None = None,
) -> Schema:
    """Infer a target :class:`~cleanframe.schema.Schema` from data. See :func:`cleanframe.schema.infer_schema`."""
    frame, _ = _as_frame(df, None, sheet=sheet, columns=columns, nrows=nrows, skiprows=skiprows)
    return _infer_schema(frame, name=name)


__all__ = ["clean", "report", "apply_recipe", "suggest_update", "infer_schema"]
