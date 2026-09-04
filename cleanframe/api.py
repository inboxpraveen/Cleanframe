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
from .errors import CleanFrameError, CleanFrameWarning, DriftError
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
def _read_binding(sheet, columns, nrows, skiprows) -> dict[str, Any]:  # noqa: D401
    """Collect the non-default read/selection options into a dict (empty if none)."""
    binding = {"sheet": sheet, "columns": columns, "nrows": nrows, "skiprows": skiprows}
    return {k: v for k, v in binding.items() if v is not None}


def _read_input(
    data, source, *, sheet, columns, nrows, skiprows, correct_format, text=False,
    sep=None, encoding=None, warn=False,
) -> tuple[pd.DataFrame, str | None, dict[str, Any], list[str]]:
    """Read ``data`` with optional CSV format auto-correction.

    Returns ``(df, source, read_binding, notes)`` where ``read_binding`` is the full
    selection+format binding to record in a recipe's ``read:`` section.
    """
    fmt_kwargs: dict[str, Any] = {}
    notes: list[str] = []
    if correct_format and isinstance(data, (str, Path)):
        from .readfix import detect_csv_options, is_csv_family

        # Only sniff a file that is actually there: read_frame owns the
        # missing-file / directory / empty-file messages.
        if is_csv_family(data) and Path(data).is_file() and Path(data).stat().st_size:
            _opts, report_ = detect_csv_options(data)  # raises on ambiguous delimiter
            fmt_kwargs = report_.as_read_binding()  # {encoding?, sep?, blank_lines?}
            notes = report_.notes
            if warn and notes:
                warnings.warn(
                    "CleanFrame: read-time format correction — " + "; ".join(notes),
                    CleanFrameWarning,
                    stacklevel=3,
                )
    # An explicit sep/encoding always wins over what detection guessed.
    fmt_kwargs.update({k: v for k, v in (("sep", sep), ("encoding", encoding)) if v is not None})
    df, source = _as_frame(
        data, source, sheet=sheet, columns=columns, nrows=nrows, skiprows=skiprows,
        text=text, **fmt_kwargs,
    )
    if correct_format and not text and isinstance(data, (str, Path)):
        from .dataio import inference_losses

        if Path(data).is_file() and Path(data).suffix.lower() not in (".parquet", ".json"):
            losses = inference_losses(
                data, df, sheet=sheet, columns=columns, skiprows=skiprows, **fmt_kwargs
            )
            if losses:
                detail = "; ".join(f"{col}: {why}" for col, why in sorted(losses.items()))
                notes = [*notes, f"type inference changed values on read — {detail}"]
                if warn:
                    warnings.warn(
                        "CleanFrame: pandas type inference changed values while reading "
                        f"({detail}). Pass text=True to read every field verbatim.",
                        CleanFrameWarning,
                        stacklevel=3,
                    )
    binding = _read_binding(sheet, columns, nrows, skiprows)
    if text:
        binding["text"] = True
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
    text: bool = False,
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
            missing = [c for c in columns if c not in df.columns]
            if missing:
                raise CleanFrameError(
                    f"Requested column(s) not found: {missing}. "
                    f"Available: {list(df.columns)}."
                )
            df = df[list(columns)]
        return ensure_string_columns(df), source  # read_kwargs (encoding/sep) are file-only
    if isinstance(data, (str, Path)):
        df = read_frame(
            data, sheet=sheet, columns=columns, nrows=nrows, skiprows=skiprows,
            text=text, **read_kwargs,
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
    llm_fallback: bool = True,
) -> Planner:
    if planner is not None:
        if not hasattr(planner, "plan"):
            raise CleanFrameError(
                f"planner must have a .plan() method, got {type(planner).__name__}."
            )
        return planner
    if llm is None:
        return RulesPlanner()
    from .types import LLMExposure

    if LLMExposure(str(llm_exposure)) is LLMExposure.NONE:
        # "none" means nothing leaves the machine, so there is no request to make.
        warnings.warn(
            "CleanFrame: llm_exposure='none' keeps everything local, so the LLM was not "
            "called — planning with deterministic rules instead.",
            CleanFrameWarning,
            stacklevel=3,
        )
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
    return LLMPlanner(
        client,
        exposure=llm_exposure,
        max_tokens_budget=max_tokens_budget,
        fallback="rules" if llm_fallback else None,
    )


_ON_DRIFT = ("error", "warn", "ignore")


def _check_on_drift(on_drift: Any) -> str:
    if on_drift not in _ON_DRIFT:
        raise CleanFrameError(
            f"on_drift must be one of {list(_ON_DRIFT)}, got {on_drift!r}. A typo here "
            "would silently disable the drift guard."
        )
    return on_drift


def _check_options(options: Any) -> dict[str, Any]:
    if options is None:
        return {}
    if not isinstance(options, dict):
        raise CleanFrameError(
            f"options must be a mapping of detector knobs, got {type(options).__name__}."
        )
    cap = options.get("max_diff_changes", 0)
    if cap is not None and (not isinstance(cap, int) or isinstance(cap, bool) or cap < 0):
        raise CleanFrameError(
            f"options['max_diff_changes'] must be a non-negative int or None, got {cap!r}."
        )
    return dict(options)


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
    text: bool = False,
    sep: str | None = None,
    encoding: str | None = None,
    llm_fallback: bool = True,
) -> CleanResult:
    """Profile, plan, and clean ``data`` — the main entry point.

    ``sheet``/``columns``/``nrows``/``skiprows`` select part of a file (see
    :func:`read_frame`); the selection is recorded in the recipe's ``read:`` section
    so :func:`apply_recipe` re-reads the same slice. For a multi-sheet workbook, pass
    ``sheet=`` or use :func:`clean_workbook` to clean every sheet.

    ``correct_format`` (default ``True``) auto-detects a CSV-family file's encoding
    and delimiter at read time (e.g. a ``;``-separated or cp1252 file), warns, and
    pins the choice into the recipe's ``read:`` section for deterministic replay. An
    ambiguous delimiter raises rather than guessing. It also reports values that
    pandas' type inference changed while reading (a leading-zero ZIP, an ``NA``
    token); pass ``text=True`` to read every field verbatim instead.

    ``llm_fallback`` (default ``True``) keeps the documented behaviour of degrading
    to the rules planner when an LLM call fails. Pass ``False`` to make such a
    failure raise instead of quietly producing a rules-only recipe.

    The returned frame is indexed 0..n-1: replaying a recipe re-keys rows to the
    stable positional row ids the diff and quarantine refer to.

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
        correct_format=correct_format, text=text, sep=sep, encoding=encoding, warn=True,
    )
    schema_obj = _resolve_schema(target_schema if target_schema is not None else schema)
    options = _check_options(options)

    profile = profile_dataframe(df)
    issues = run_detectors(df, profile=profile, schema=schema_obj, options=options)
    the_planner = _resolve_planner(
        planner, llm, llm_exposure, max_tokens_budget, llm_fallback=llm_fallback
    )
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
    text: bool = False,
    sep: str | None = None,
    encoding: str | None = None,
) -> Report:
    """Profile ``data`` and return an HTML :class:`~cleanframe.result.Report` (no changes made)."""
    df, source, _, _ = _read_input(
        data, source, sheet=sheet, columns=columns, nrows=nrows, skiprows=skiprows,
        correct_format=correct_format, text=text, sep=sep, encoding=encoding, warn=True,
    )
    schema_obj = _resolve_schema(schema)
    profile = profile_dataframe(df)
    issues = run_detectors(df, profile=profile, schema=schema_obj, options=_check_options(options))
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
    text: bool = False,
    sep: str | None = None,
    encoding: str | None = None,
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
    _check_on_drift(on_drift)

    # Precedence: explicit call args > recipe-recorded read binding > whole file.
    call = _read_binding(sheet, columns, nrows, skiprows)
    call.update({k: v for k, v in (("sep", sep), ("encoding", encoding)) if v is not None})
    if text:
        call["text"] = True
    effective = {**(recipe_obj.read or {}), **call}
    if isinstance(data, pd.DataFrame) and effective:
        unreplayable = {k for k in effective if k in ("sheet", "nrows", "skiprows")}
        if unreplayable and not call:
            warnings.warn(
                f"CleanFrame: recipe's recorded read binding {sorted(unreplayable)} cannot "
                "replay against an in-memory DataFrame; ignoring it.",
                CleanFrameWarning,
                stacklevel=2,
            )
            effective = {k: v for k, v in effective.items() if k == "columns"}
    df, source = _as_frame(data, source, **effective)

    drift: DriftReport | None = None
    if check_drift and not recipe_obj.source_fingerprint:
        warnings.warn(
            "CleanFrame: recipe has no source_fingerprint — drift check skipped. "
            "Re-plan or stamp a fingerprint for production replay.",
            CleanFrameWarning,
            stacklevel=2,
        )
    if check_drift and recipe_obj.source_fingerprint:
        drift = detect_drift(df, recipe_obj, source=source)
        if drift.has_drift:
            if on_drift == "error" or mode is Mode.STRICT:
                raise DriftError(drift.render(), report=drift)
            if on_drift == "warn":
                warnings.warn("CleanFrame: " + drift.render(), CleanFrameWarning, stacklevel=2)

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
    sheet: str | int | None = None,
    columns: list[str] | None = None,
    nrows: int | None = None,
    skiprows: int | list[int] | None = None,
) -> tuple[Recipe, DriftReport]:
    """Return a recipe patched to accommodate drift in ``data``, plus the drift report.

    Applies safe, mechanical patches: repoint a renamed column to its new source,
    and teach ``parse_date`` any new date formats that appeared. Structural
    additions are reported but not auto-adopted — those are a human's call.

    The recipe's recorded ``read:`` binding (sheet, delimiter, encoding, selection)
    is re-applied, so a workbook or ``;``-separated file is read the same way it was
    planned instead of reporting every column as drifted.
    """
    original = _resolve_recipe(recipe)
    call = _read_binding(sheet, columns, nrows, skiprows)
    effective = {**(original.read or {}), **call}
    if isinstance(data, pd.DataFrame):
        effective = {k: v for k, v in effective.items() if k == "columns"}
    df, source = _as_frame(data, source, **effective)
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
        if new_col is None:
            continue
        matched = finding.evidence.get("match")
        for col in patched.columns:
            if col.output_name == matched or col.source == matched or col.rename_to == matched:
                changes.append(f"repointed {col.source!r} → {new_col!r}")
                col.source = new_col
                break

    # 2) new date formats -> extend the parse_date op
    for finding in report.by_kind("format_drift"):
        src = finding.column
        if src is None:
            continue
        column = next((c for c in patched.columns if c.source == src), None)
        if column is None:
            continue
        for op in column.ops:
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
    correct_format: bool = True,
    text: bool = False,
    sep: str | None = None,
    encoding: str | None = None,
) -> Schema:
    """Infer a target :class:`~cleanframe.schema.Schema` from data.

    See :func:`cleanframe.schema.infer_schema`. Like :func:`clean`, a CSV-family
    file's encoding and delimiter are detected unless ``correct_format=False``.
    """
    frame, _, _, _ = _read_input(
        df, None, sheet=sheet, columns=columns, nrows=nrows, skiprows=skiprows,
        correct_format=correct_format, text=text, sep=sep, encoding=encoding,
    )
    return _infer_schema(frame, name=name)


__all__ = ["clean", "report", "apply_recipe", "suggest_update", "infer_schema"]
