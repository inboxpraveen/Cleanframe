"""Agent-facing tools: CleanFrame's plan -> review -> replay workflow as JSON in, JSON out.

Coding agents are good at writing throwaway pandas, and throwaway pandas is exactly the
wrong artifact for a file that comes back every month: nobody reviews it, it cannot be
replayed, and next month it silently does something different. These functions give an
agent the CleanFrame workflow instead - profile a file, plan a reviewable YAML recipe,
replay it behind the drift guard, explain it, export it - as plain functions that
return JSON-serialisable dicts.

Design rules, and why:

* **No ``mcp`` import.** :mod:`cleanframe.mcp_server` registers these functions as MCP
  tools. Keeping the logic here means it is unit-tested, and usable from any other agent
  framework, without the optional dependency.
* **No LLM inside.** The calling agent *is* the LLM. Planning uses the deterministic
  rules planner, so the same file always yields the same recipe (invariant 1).
* **Structured errors.** Every result has ``"ok"``. A deliberate CleanFrame error (missing
  file, malformed recipe, drift stop, validation failure, refused write) comes back as
  ``{"ok": false, "kind": <exception class>, "error": <message>}``: agents recover from
  that far better than from a traceback. Anything else is a bug and propagates.
* **Bounded output.** Results land in an LLM context window, so every list is capped and
  every cap is reported (``*_truncated`` / ``*_total``) - never silently (invariant 4).
* **Never destructive by default.** Outputs go through CleanFrame's atomic writers. The
  input data file can never be written from here, and an existing output is replaced
  only with ``overwrite=True`` (a recipe or script with identical bytes is the one
  exception, so re-running the same call is idempotent).
* **Cell values are opt-out.** Results quote a few capped cell values (examples, diff
  previews) because an agent needs them to judge a fix. Set ``CLEANFRAME_AGENT_HIDE_VALUES=1``
  in the server's environment to replace those value fields with ``"<hidden>"``. Free text
  is not scrubbed and can still quote data: the recipe itself (its category mappings and
  allowed values, also in the explanation and the code preview), a constant column's issue
  message, read notes / warnings about type inference changing a value, and drift
  findings, warnings and log lines about a new date or number format (one example each).
"""

from __future__ import annotations

import datetime as _dt
import math
import os
import threading
import warnings
from collections.abc import Callable, Mapping
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ._version import __version__
from .errors import CleanFrameError, CleanFrameWarning, DriftError, OutputError, ValidationFailure

if TYPE_CHECKING:  # annotations only; the heavy modules are imported lazily per call
    import pandas as pd

    from .diff import CellDiff
    from .drift import DriftReport
    from .recipe import Recipe
    from .types import Op

# ---------------------------------------------------------------------------
# Output bounds (each one is reported when it bites)
# ---------------------------------------------------------------------------
#: Issues listed by ``profile_data`` (most severe first).
MAX_ISSUES = 40
#: Column profiles listed by ``profile_data``.
MAX_COLUMNS = 60
#: Entries kept in a per-column map (renames, change counts, reasons) or column list.
MAX_MAP_ENTRIES = 50
#: Columns and validation rules ``explain_recipe`` explains (per sheet).
MAX_EXPLAIN_STEPS = 40
#: Sheets of a workbook recipe ``explain_recipe`` explains.
MAX_EXPLAIN_SHEETS = 10
#: Characters of explanation text returned.
MAX_EXPLAIN_CHARS = 24_000
#: Example values per issue / per column.
MAX_EXAMPLES = 3
#: Characters kept from any quoted cell value.
MAX_VALUE_CHARS = 80
#: Before/after pairs in a diff preview.
MAX_PREVIEW_CHANGES = 20
#: Quarantined (or otherwise removed) rows listed with their reason.
MAX_QUARANTINE_ROWS = 20
#: Drift findings listed.
MAX_DRIFT_FINDINGS = 50
#: Issues "needing review" listed by ``suggest_recipe``.
MAX_REVIEW_ITEMS = 20
#: Distinct warning lines returned.
MAX_WARNINGS = 20
#: Run-log lines returned.
MAX_LOG_LINES = 30
#: Characters of recipe YAML / code returned inline (the full text is on disk).
MAX_TEXT_CHARS = 12_000
#: Mapping entries / list items quoted per op parameter by ``explain_recipe``.
MAX_EXPLAIN_ITEMS = 10

#: Environment switch: replace quoted cell values with a placeholder.
HIDE_VALUES_ENV = "CLEANFRAME_AGENT_HIDE_VALUES"
_HIDDEN = "<hidden>"

#: File types ``apply_recipe`` may write (CleanFrame's writer dispatches on the suffix).
DATA_SUFFIXES = (".csv", ".tsv", ".txt", ".xlsx", ".xlsm", ".parquet", ".json")
_RECIPE_SUFFIXES = (".yaml", ".yml")

#: One tool call at a time. MCP runs sync tools on worker threads, and the warning
#: capture below swaps process-global state (``warnings.showwarning``/filters), so two
#: overlapping calls would steal each other's warnings.
_LOCK = threading.Lock()


class InvalidArgument(CleanFrameError):
    """A tool argument cannot be used as given (wrong type, unsafe or unsupported path)."""


# ---------------------------------------------------------------------------
# Plumbing: run, capture warnings, structure errors, make JSON-safe
# ---------------------------------------------------------------------------
def _run(impl: Callable[..., dict[str, Any]], /, **kwargs: Any) -> dict[str, Any]:
    """Run one tool body: serialise calls, capture warnings, structure deliberate errors.

    ``simplefilter("always")`` matters for determinism: under the default filter Python
    remembers a warning's source location and suppresses it on the next call, so the
    same call twice would report different ``warnings``.
    """
    with _LOCK, warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            result: dict[str, Any] = {"ok": True, **impl(**kwargs)}
        except CleanFrameError as exc:
            result = _error_result(exc)
    result["warnings"] = _warning_lines(caught)
    return _jsonable(result)


def _error_result(exc: CleanFrameError) -> dict[str, Any]:
    out: dict[str, Any] = {"ok": False, "kind": type(exc).__name__, "error": str(exc)}
    if isinstance(exc, DriftError):
        report = getattr(exc, "report", None)
        if report is not None:
            out["error"] = "Schema drift detected; nothing was executed or written."
            out["drift"] = _drift_dict(report)
        out["next_step"] = (
            "Stop and show the user these drift findings. Do not retry with "
            "on_drift='ignore' unless they explicitly ask. check_drift(path, recipe, "
            "patched_recipe_out=...) drafts a patched recipe for them to review."
        )
    elif isinstance(exc, ValidationFailure):
        out["failures"] = [
            {
                "column": getattr(f, "column", None),
                "check": getattr(f, "check", None),
                "failed_rows": getattr(f, "n_failed", None),
                "row_ids": list(getattr(f, "failed_row_ids", []) or [])[:MAX_QUARANTINE_ROWS],
            }
            for f in exc.failures
        ]
        out["next_step"] = (
            "Validation failed under an erroring policy (strict mode or on_fail: error). "
            "Show the user the failing checks; mode='review' quarantines such rows instead."
        )
    return out


def _warning_lines(caught: list[warnings.WarningMessage]) -> list[str]:
    """Distinct warning texts in order, CleanFrame's own prefix stripped, capped."""
    lines: list[str] = []
    for w in caught:
        text = str(w.message)
        if text.startswith("CleanFrame:"):
            text = text[len("CleanFrame:") :].strip()
        elif not issubclass(w.category, CleanFrameWarning):
            text = f"{w.category.__name__}: {text}"
        if text not in lines:
            lines.append(text)
    if len(lines) > MAX_WARNINGS:
        extra = len(lines) - MAX_WARNINGS
        lines = [*lines[:MAX_WARNINGS], f"(+{extra} more warning(s) not shown)"]
    return lines


def _jsonable(value: Any) -> Any:
    """Convert to plain JSON types: NaN/inf/NA/NaT -> None, numpy scalars -> Python.

    ``json.dumps`` happily writes ``NaN``, which is not JSON and breaks strict parsers in
    MCP clients, so missing values must become ``null`` here rather than downstream.
    """
    import numpy as np
    import pandas as pd

    # NaT subclasses datetime and would otherwise serialise as the string "NaT".
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, Enum):
        return _jsonable(value.value)
    if isinstance(value, str):
        return str(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return [_jsonable(v) for v in sorted(value, key=str)]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, _dt.timedelta):
        return str(value)
    if isinstance(value, (np.datetime64, np.timedelta64)):
        # .item() on a nanosecond datetime64 returns a bare int, so go through pandas.
        if np.isnat(value):
            return None
        converted = pd.Timestamp(value) if isinstance(value, np.datetime64) else pd.Timedelta(value)
        return _jsonable(converted)
    if isinstance(value, np.generic):
        return _jsonable(value.item())
    if isinstance(value, np.ndarray):
        return _jsonable(value.tolist())
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):  # array-likes and exotic objects
        pass
    return str(value)


def _hide_values() -> bool:
    return os.environ.get(HIDE_VALUES_ENV, "").strip().lower() not in ("", "0", "false", "no")


def _cell(value: Any) -> Any:
    """A cell value as the agent may see it: JSON-safe, length-capped, or hidden."""
    plain = _jsonable(value)
    if plain is None:
        return None  # missingness is structure, not content
    if _hide_values():
        return _HIDDEN
    if isinstance(plain, str) and len(plain) > MAX_VALUE_CHARS:
        return plain[:MAX_VALUE_CHARS] + "..."
    return plain


def _capped_text(text: str, limit: int = MAX_TEXT_CHARS) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _capped_map(mapping: dict[str, Any], limit: int = MAX_MAP_ENTRIES) -> tuple[dict[str, Any], bool]:
    """The first ``limit`` entries (insertion order is deterministic) and whether more exist."""
    items = list(mapping.items())
    return dict(items[:limit]), len(items) > limit


# ---------------------------------------------------------------------------
# Argument and path validation
# ---------------------------------------------------------------------------
def _path(value: Any, what: str) -> Path:
    """An absolute path from a caller-supplied string (relative = the server's cwd)."""
    if isinstance(value, os.PathLike):
        value = os.fspath(value)
    if not isinstance(value, str) or not value.strip():
        raise InvalidArgument(f"{what} must be a non-empty file path, got {value!r}.")
    return Path(value).expanduser().resolve()


def _opt_path(value: Any, what: str) -> Path | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return _path(value, what)


def _with_suffix(path: Path, what: str, suffixes: tuple[str, ...]) -> Path:
    if path.suffix.lower() not in suffixes:
        raise InvalidArgument(
            f"{what} must end in one of {', '.join(suffixes)}, got {path.name!r}. "
            "The extension decides the file format, so a wrong one would write the "
            "wrong kind of file."
        )
    return path


def _sheet(value: Any) -> str | int | None:
    """A sheet name, or a 0-based index given as an int or as ``"#N"`` (like the CLI)."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise InvalidArgument(f"sheet must be a sheet name or a 0-based index, got {value!r}.")
    if isinstance(value, int):
        if value < 0:
            raise InvalidArgument(f"sheet index must be 0 or more, got {value}.")
        return value
    if isinstance(value, str):
        if not value.strip():
            return None
        text = value.strip()
        if text.startswith("#") and text[1:].isdigit():
            return int(text[1:])
        return value
    raise InvalidArgument(f"sheet must be a sheet name or a 0-based index, got {value!r}.")


def _opt_int(value: Any, what: str, minimum: int) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise InvalidArgument(f"{what} must be a whole number >= {minimum}, got {value!r}.")
    return value


def _same_file(a: Path, b: Path) -> bool:
    if os.path.normcase(str(a)) == os.path.normcase(str(b)):
        return True
    try:
        return a.exists() and b.exists() and os.path.samefile(a, b)
    except OSError:  # pragma: no cover - unreadable path
        return False


def _refuse_clash(out: Path, what: str, protected: Mapping[str, Path | None]) -> None:
    """Never write ``out`` over a file this call reads (input data, recipe, schema)."""
    for label, other in protected.items():
        if other is not None and _same_file(out, other):
            raise OutputError(
                f"Refusing to write the {what} to {out}: that is the {label}. "
                "Choose a different output path."
            )


def _refuse_existing(out: Path, what: str, *, overwrite: bool, new_text: str | None = None) -> None:
    """Refuse to replace an existing file unless ``overwrite`` - or the bytes are identical.

    Identical text (re-running the same plan) is allowed so a repeated call is
    idempotent, exactly like ``cleanframe clean`` re-generating an unchanged recipe.
    """
    if not out.exists():
        return
    if out.is_dir():
        raise OutputError(f"Output path is a directory, not a file: {out}.")
    if overwrite:
        return
    if new_text is not None:
        try:
            if out.read_text(encoding="utf-8") == new_text:
                return
        except (OSError, UnicodeDecodeError):
            pass
        detail = " and differs from what would be written (hand-edited?)"
    else:
        detail = ""
    raise OutputError(
        f"The {what} {out} already exists{detail}. Pass overwrite=true only if the user "
        "agreed to replace it, or choose a new path."
    )


def _load_single_recipe(path: Path) -> Recipe:
    from .recipe import Recipe
    from .workbook import WorkbookRecipe, load_recipe

    loaded = load_recipe(path)
    if isinstance(loaded, WorkbookRecipe):
        raise InvalidArgument(
            f"{path} is a multi-sheet workbook recipe ({len(loaded.sheets)} sheet(s)); this "
            "tool handles single-sheet recipes. Use the CLI for workbooks: "
            f"cleanframe apply <file.xlsx> --recipe {path} --out <clean.xlsx> --json"
        )
    assert isinstance(loaded, Recipe)
    return loaded


# ---------------------------------------------------------------------------
# Shared result builders
# ---------------------------------------------------------------------------
def _quality_dict(quality: Any) -> dict[str, Any] | None:
    if quality is None:
        return None
    return {"score": quality.score, "grade": quality.grade, "label": quality.label}


def _drift_dict(report: DriftReport) -> dict[str, Any]:
    findings = list(report.findings)
    return {
        "checked": True,
        "has_drift": bool(report.has_drift),
        "worst": report.worst.value if report.worst is not None else None,
        "findings": [
            {
                "kind": f.kind,
                "severity": f.severity.value,
                "column": f.column,
                "message": f.message,
                "suggestion": f.suggestion,
            }
            for f in findings[:MAX_DRIFT_FINDINGS]
        ],
        "findings_total": len(findings),
        "findings_truncated": len(findings) > MAX_DRIFT_FINDINGS,
    }


def _diff_dict(diff: CellDiff) -> dict[str, Any]:
    """Diff summary, per-column counts and a column-balanced before/after preview."""
    by_column = diff.changes_by_column()
    # Round-robin across columns so a 1,000-change column cannot crowd out the others.
    picked: list[Any] = []
    queues = [list(changes) for changes in by_column.values()]
    depth = 0
    while len(picked) < MAX_PREVIEW_CHANGES and any(depth < len(q) for q in queues):
        for queue in queues:
            if depth < len(queue) and len(picked) < MAX_PREVIEW_CHANGES:
                picked.append(queue[depth])
        depth += 1
    order = {name: i for i, name in enumerate(by_column)}
    picked.sort(key=lambda c: (order[c.column], c.row_id))

    removed: dict[str, int] = {}
    for _, reason in diff.dropped_rows:
        removed[reason] = removed.get(reason, 0) + 1
    removed_shown, removed_cut = _capped_map(removed)

    summary = diff.summary()
    renamed, renamed_cut = _capped_map(dict(summary["renamed_columns"]))
    added, removed_cols = list(summary["added_columns"]), list(summary["removed_columns"])
    per_column, per_column_cut = _capped_map(
        {name: len(changes) for name, changes in by_column.items()}
    )
    return {
        "summary": {
            "rows_before": summary["rows_before"],
            "rows_after": summary["rows_after"],
            "rows_removed": summary["rows_dropped"],
            "changed_cells": summary["changed_cells"],
            "changed_columns": summary["changed_columns"],
            "renamed_columns": renamed,
            "renamed_columns_total": len(summary["renamed_columns"]),
            "renamed_columns_truncated": renamed_cut,
            "added_columns": added[:MAX_MAP_ENTRIES],
            "removed_columns": removed_cols[:MAX_MAP_ENTRIES],
            "column_lists_truncated": max(len(added), len(removed_cols)) > MAX_MAP_ENTRIES,
        },
        "removed_rows_by_reason": removed_shown,
        "removed_rows_by_reason_truncated": removed_cut,
        # Counted from the stored change detail, which the executor caps; when that cap
        # was hit the per-column numbers are lower bounds (changed_cells stays exact).
        "changes_by_column": per_column,
        "changes_by_column_truncated": per_column_cut,
        "changes_by_column_exact": not diff.truncated,
        "preview": [
            {
                "row_id": c.row_id,
                "column": c.column,
                "before": _cell(c.before),
                "after": _cell(c.after),
            }
            for c in picked
        ],
        "preview_total": diff.changed_cells,
        "preview_truncated": diff.changed_cells > len(picked),
    }


def _quarantine_dict(quarantine: pd.DataFrame) -> dict[str, Any]:
    from .validate import QUARANTINE_REASON_COL

    if quarantine is None or quarantine.empty:
        return {
            "count": 0, "reasons": {}, "reasons_truncated": False, "rows": [],
            "rows_truncated": False,
        }
    # Validation appends the reason column last; it carries a suffix when the data
    # already had a column of that name.
    reason_col = quarantine.columns[-1]
    if not str(reason_col).startswith(QUARANTINE_REASON_COL):  # pragma: no cover - defensive
        reasons = ["validation"] * len(quarantine)
    else:
        reasons = [str(r) for r in quarantine[reason_col].tolist()]
    tally: dict[str, int] = {}
    for reason in reasons:
        tally[reason] = tally.get(reason, 0) + 1
    ids = [int(i) for i in quarantine.index.tolist()]
    reasons_shown, reasons_cut = _capped_map(tally)
    return {
        "count": len(quarantine),
        "reasons": reasons_shown,
        "reasons_truncated": reasons_cut,
        "rows": [
            {"row_id": rid, "reason": reason}
            for rid, reason in list(zip(ids, reasons, strict=True))[:MAX_QUARANTINE_ROWS]
        ],
        "rows_truncated": len(quarantine) > MAX_QUARANTINE_ROWS,
    }


def _capped_log(log: list[str]) -> list[str]:
    lines = [str(line) for line in log]
    if len(lines) > MAX_LOG_LINES:
        extra = len(lines) - MAX_LOG_LINES
        lines = [*lines[:MAX_LOG_LINES], f"(+{extra} more log line(s) not shown)"]
    return lines


# ---------------------------------------------------------------------------
# profile_data
# ---------------------------------------------------------------------------
def profile_data(
    path: str,
    sheet: str | int | None = None,
    nrows: int | None = None,
    header_row: int | None = None,
    text: bool = False,
) -> dict[str, Any]:
    """Profile a messy CSV/TSV/Excel/Parquet/JSON file. Read-only: changes nothing.

    Start here before cleaning a file. Returns the row/column counts, a 0-100 quality
    score with grade, the semantic type of each column (email, phone, date, currency,
    categorical, ...), and the detected issues - each with kind, column, severity,
    message, confidence, whether CleanFrame has an automatic fix (auto_fix, fix) and the
    modes whose confidence bar that fix clears (planned_in_modes). Issues are sorted most
    severe first and capped at 40 (column profiles at 60); issues_truncated / issue_count
    say when more exist.

    Args:
        path: Data file. Use an absolute path; a relative one resolves against the
            server's working directory.
        sheet: Excel only - sheet name, or 0-based index (int or "#N"). Required for a
            workbook with several sheets.
        nrows: Read only the first N data rows (a quick look at a huge file).
        header_row: 0-based line holding the column names, when title rows sit above it.
        text: Read every field verbatim (keeps leading zeros such as ZIP codes and
            literal "NA" text). Use it when read_notes says type inference changed values.

    Next: suggest_recipe(path, recipe_out) to plan a reviewable cleaning recipe.
    """
    return _run(
        _profile_data, path=path, sheet=sheet, nrows=nrows, header_row=header_row, text=text
    )


def _profile_data(
    path: Any, sheet: Any, nrows: Any, header_row: Any, text: Any
) -> dict[str, Any]:
    from .api import _read_input
    from .detectors import run_detectors
    from .planner import MODE_THRESHOLDS
    from .profile import profile_dataframe
    from .quality import quality_score

    src = _path(path, "path")
    df, _, binding, read_notes = _read_input(
        str(src), None,
        sheet=_sheet(sheet), columns=None, nrows=_opt_int(nrows, "nrows", 1), skiprows=None,
        header_row=_opt_int(header_row, "header_row", 0), correct_format=True,
        text=bool(text), warn=True,
    )
    # The same stages, options and score as `cleanframe report`, so both agree.
    profile = profile_dataframe(df)
    issues = run_detectors(df, profile=profile, schema=None, options={})
    quality = quality_score(profile, issues)

    ranked = sorted(enumerate(issues), key=lambda p: (-p[1].severity.rank, p[0]))
    by_severity = {"error": 0, "warning": 0, "info": 0}
    for issue in issues:
        by_severity[issue.severity.value] = by_severity.get(issue.severity.value, 0) + 1

    issue_rows = []
    for _, issue in ranked[:MAX_ISSUES]:
        proposal = issue.proposal if issue.has_fix else None
        evidence = issue.evidence or {}
        examples = evidence.get("examples")
        issue_rows.append(
            {
                "kind": issue.kind,
                "column": issue.column,
                "severity": issue.severity.value,
                "message": issue.message,
                "confidence": round(float(issue.confidence), 3),
                "detector": issue.detector,
                "auto_fix": proposal is not None,
                "fix": (
                    {"ops": [op.name for op in proposal.ops], "rename_to": proposal.rename_to}
                    if proposal is not None
                    else None
                ),
                "planned_in_modes": (
                    [m.value for m, bar in MODE_THRESHOLDS.items() if issue.confidence >= bar]
                    if proposal is not None
                    else []
                ),
                "counts": {
                    str(k): v
                    for k, v in evidence.items()
                    if isinstance(v, (int, float)) and not isinstance(v, bool)
                },
                "examples": (
                    [_cell(v) for v in list(examples)[:MAX_EXAMPLES]]
                    if isinstance(examples, (list, tuple))
                    else []
                ),
            }
        )

    columns = [
        {
            "name": cp.name,
            "dtype": str(cp.dtype),
            "semantic_type": cp.semantic_type,
            "type_confidence": round(float(cp.type_confidence), 3),
            "non_null": cp.count,
            "nulls": cp.null_count,
            "unique": cp.unique_count,
            "examples": [_cell(v) for v in list(cp.sample_values)[:MAX_EXAMPLES]],
        }
        for cp in profile.columns[:MAX_COLUMNS]
    ]
    return {
        "source": str(src),
        "rows": profile.n_rows,
        "columns": profile.n_columns,
        "duplicate_rows": profile.duplicate_row_count,
        "quality": _quality_dict(quality),
        "read": binding,
        "read_notes": list(read_notes),
        "column_profiles": columns,
        "column_profiles_truncated": len(profile.columns) > MAX_COLUMNS,
        "issue_count": len(issues),
        "issues_by_severity": by_severity,
        "issues": issue_rows,
        "issues_truncated": len(issues) > MAX_ISSUES,
        "next_step": (
            "Call suggest_recipe(path, recipe_out='<name>.recipe.yaml') to plan a "
            "reviewable recipe. Do not modify the data file directly."
        ),
    }


# ---------------------------------------------------------------------------
# suggest_recipe
# ---------------------------------------------------------------------------
def suggest_recipe(
    path: str,
    recipe_out: str,
    schema: str | None = None,
    mode: str = "review",
    sheet: str | int | None = None,
    header_row: int | None = None,
    text: bool = False,
    options: dict[str, Any] | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Plan a cleaning recipe for a file and save it as reviewable YAML. Data is not modified.

    Uses CleanFrame's deterministic rules planner (no LLM): the same file always gives
    the same recipe. The recipe is the durable artifact - commit it, review it, replay
    it on next month's file with apply_recipe. Returns the YAML text, planning notes
    (fixes left out and issues needing a human), a diff summary with per-column change
    counts, a preview of up to 20 before/after cells, and the rows validation would
    quarantine (held aside with a reason, never deleted).

    Args:
        path: Messy data file (absolute path recommended).
        recipe_out: Where to save the recipe; must end in .yaml or .yml.
        schema: Optional target schema YAML (column names, types, allowed values) that
            drives renames and validation rules.
        mode: "review" (default; includes fixes with confidence >= 0.5), "auto"
            (>= 0.65, for unattended pipelines) or "strict" (>= 0.85, and any
            validation failure is an error).
        sheet: Excel only - sheet name, or 0-based index (int or "#N").
        header_row: 0-based line holding the column names, when title rows sit above it.
        text: Read every field verbatim (keeps leading zeros); recorded in the recipe.
        options: Planner knobs, e.g. {"dayfirst": true}, {"phone_country_code": "+91"},
            {"category_map": {"City": {"BLR": "Bangalore"}}}, {"rename_columns": false}.
            category_map is how you teach domain synonyms; the planner stays deterministic.
        overwrite: Replace an existing, different file at recipe_out. Only when the user
            agreed - it may hold hand edits. Re-planning to identical YAML never needs it.

    To change what the recipe does, edit the YAML (explain_recipe and list_capabilities
    help), then run apply_recipe. Never edit the data file by hand.
    """
    return _run(
        _suggest_recipe, path=path, recipe_out=recipe_out, schema=schema, mode=mode,
        sheet=sheet, header_row=header_row, text=text, options=options, overwrite=overwrite,
    )


def _suggest_recipe(
    path: Any, recipe_out: Any, schema: Any, mode: Any, sheet: Any, header_row: Any,
    text: Any, options: Any, overwrite: Any,
) -> dict[str, Any]:
    from .api import clean
    from .types import Mode

    src = _path(path, "path")
    out = _with_suffix(_path(recipe_out, "recipe_out"), "recipe_out", _RECIPE_SUFFIXES)
    schema_path = _opt_path(schema, "schema")
    mode_value = Mode.coerce(mode)
    _refuse_clash(out, "recipe", {"input data file": src, "schema file": schema_path})
    if options is not None and not isinstance(options, dict):
        raise InvalidArgument(f"options must be a JSON object, got {type(options).__name__}.")

    result = clean(
        str(src),
        target_schema=str(schema_path) if schema_path is not None else None,
        mode=mode_value,
        options=options,
        sheet=_sheet(sheet),
        header_row=_opt_int(header_row, "header_row", 0),
        text=bool(text),
    )
    yaml_text = result.recipe.to_yaml()
    _refuse_existing(out, "recipe", overwrite=bool(overwrite), new_text=yaml_text)
    result.recipe.save(out)

    review = [
        {
            "kind": i.kind,
            "column": i.column,
            "severity": i.severity.value,
            "message": i.message,
        }
        for i in sorted(
            (i for i in result.issues if not i.has_fix), key=lambda i: -i.severity.rank
        )
    ]
    shown_yaml, yaml_cut = _capped_text(yaml_text)
    notes = result.recipe.meta.get("notes") or []
    return {
        "source": str(src),
        "recipe_path": str(out),
        "mode": mode_value.value,
        "recipe_yaml": shown_yaml,
        "recipe_yaml_truncated": yaml_cut,
        "read": result.recipe.read or {},
        "notes": list(notes) if isinstance(notes, list) else [str(notes)],
        "quality_before": _quality_dict(result.quality),
        **_diff_dict(result.diff),
        "quarantine": _quarantine_dict(result.quarantine),
        "needs_review": review[:MAX_REVIEW_ITEMS],
        "needs_review_truncated": len(review) > MAX_REVIEW_ITEMS,
        "log": _capped_log(result.log),
        "next_step": (
            f"Show the user the recipe ({out}) and the preview; they own it. Adjust it by "
            "editing the YAML, never the data. Then apply_recipe(path, recipe, out=...) "
            "writes the cleaned file."
        ),
    }


# ---------------------------------------------------------------------------
# apply_recipe
# ---------------------------------------------------------------------------
def apply_recipe(
    path: str,
    recipe: str,
    out: str | None = None,
    mode: str = "review",
    on_drift: str = "error",
    quarantine_out: str | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Replay a saved recipe on a data file (deterministic, no LLM), behind the drift guard.

    Use this to produce cleaned data, and every time the same kind of file comes back.
    The input file is never modified. Without `out` it is a dry run: nothing is written
    and you get the same summary. Before executing, the file is compared with the shape
    the recipe was planned on; if it drifted (renamed/new/missing columns, new date or
    number formats) the call returns ok=false with kind "DriftError" and the findings -
    stop and show them to the user. Rows failing the recipe's validation rules are
    quarantined (held aside with a reason, never silently deleted).

    Args:
        path: Data file to clean (absolute path recommended).
        recipe: Recipe YAML from suggest_recipe (or hand-written).
        out: Cleaned data output: .csv, .tsv, .txt, .xlsx, .xlsm, .parquet or .json.
            Never the input file (always refused).
        mode: "review" (default), "auto", or "strict" (drift or any validation
            failure becomes an error).
        on_drift: "error" (default: stop), "warn" (continue and report the drift) or
            "ignore". Use warn/ignore only when the user explicitly said to proceed.
        quarantine_out: Optional file for the quarantined rows (same formats as out).
        overwrite: Replace existing out / quarantine_out files. Only when the user
            agreed; it never allows writing over the input file.
    """
    return _run(
        _apply_recipe, path=path, recipe=recipe, out=out, mode=mode, on_drift=on_drift,
        quarantine_out=quarantine_out, overwrite=overwrite,
    )


def _apply_recipe(
    path: Any, recipe: Any, out: Any, mode: Any, on_drift: Any, quarantine_out: Any,
    overwrite: Any,
) -> dict[str, Any]:
    from .api import _check_on_drift
    from .api import apply_recipe as _apply
    from .dataio import write_frame
    from .types import Mode

    src = _path(path, "path")
    recipe_path = _path(recipe, "recipe")
    out_path = _opt_path(out, "out")
    q_path = _opt_path(quarantine_out, "quarantine_out")
    mode_value = Mode.coerce(mode)
    _check_on_drift(on_drift)
    protected = {"input data file": src, "recipe file": recipe_path}
    # Validate every output before any work, so a bad path fails in milliseconds.
    for target, what in ((out_path, "cleaned data"), (q_path, "quarantine")):
        if target is None:
            continue
        _with_suffix(target, "out" if what == "cleaned data" else "quarantine_out", DATA_SUFFIXES)
        _refuse_clash(target, what, protected)
        _refuse_existing(target, f"{what} file", overwrite=bool(overwrite))
    if out_path is not None and q_path is not None and _same_file(out_path, q_path):
        raise InvalidArgument("out and quarantine_out must be different files.")

    loaded = _load_single_recipe(recipe_path)
    result = _apply(str(src), loaded, mode=mode_value, on_drift=on_drift)
    if out_path is not None:
        write_frame(result.dataframe, out_path, source=str(src))
    q_written = None
    if q_path is not None and result.has_quarantine:
        write_frame(result.quarantine, q_path, source=str(src))
        q_written = str(q_path)

    drift: dict[str, Any]
    if result.drift is not None:
        drift = _drift_dict(result.drift)
    else:
        drift = {
            "checked": False,
            "reason": "the recipe has no source_fingerprint, so drift could not be checked; "
            "re-plan with suggest_recipe to stamp one",
        }
    return {
        "source": str(src),
        "recipe_path": str(recipe_path),
        "mode": mode_value.value,
        "on_drift": on_drift,
        "dry_run": out_path is None,
        "out_path": str(out_path) if out_path is not None else None,
        "rows_in": result.diff.n_rows_before,
        "rows_out": int(len(result.dataframe)),
        "rows_quarantined": int(len(result.quarantine)),
        **_diff_dict(result.diff),
        "quarantine": _quarantine_dict(result.quarantine),
        "quarantine_path": q_written,
        "drift": drift,
        "log": _capped_log(result.log),
        "next_step": (
            "Report the row counts, quarantine reasons and any warnings to the user."
            if out_path is not None
            else "Dry run - nothing was written. Pass out=... to write the cleaned file."
        ),
    }


# ---------------------------------------------------------------------------
# check_drift
# ---------------------------------------------------------------------------
def check_drift(
    path: str,
    recipe: str,
    patched_recipe_out: str | None = None,
) -> dict[str, Any]:
    """Check whether a new data file still matches a recipe, without executing anything.

    Run this when a recurring file arrives, before apply_recipe. Compares the file with
    the shape the recipe was planned on (column names, types, date and number formats)
    and returns each finding with kind, severity, column, message and suggestion. If
    there is drift, stop and show the findings to the user.

    Args:
        path: The new data file (absolute path recommended).
        recipe: Recipe YAML to check against.
        patched_recipe_out: Optional .yaml path for a drafted, patched recipe (renamed
            columns repointed, new date formats added; structural changes are reported,
            not adopted). It is never written over the original recipe or an existing
            different file. It re-stamps the fingerprint, so a human must review the
            listed patch_changes before it replaces the original.
    """
    return _run(_check_drift, path=path, recipe=recipe, patched_recipe_out=patched_recipe_out)


def _check_drift(path: Any, recipe: Any, patched_recipe_out: Any) -> dict[str, Any]:
    from .api import suggest_update

    src = _path(path, "path")
    recipe_path = _path(recipe, "recipe")
    patched_path = _opt_path(patched_recipe_out, "patched_recipe_out")
    if patched_path is not None:
        _with_suffix(patched_path, "patched_recipe_out", _RECIPE_SUFFIXES)
        _refuse_clash(
            patched_path, "patched recipe", {"input data file": src, "recipe file": recipe_path}
        )
    loaded = _load_single_recipe(recipe_path)
    base = {"source": str(src), "recipe_path": str(recipe_path)}
    if not loaded.source_fingerprint:
        return {
            **base,
            "drift": {
                "checked": False,
                "has_drift": None,
                "reason": "the recipe has no source_fingerprint, so drift cannot be checked "
                "(apply_recipe skips the guard too); re-plan with suggest_recipe to stamp one",
            },
            "patched_recipe_path": None,
        }

    # suggest_update re-applies the recipe's read: binding (sheet, delimiter, encoding),
    # exactly as apply would, and drafts the mechanical patch without writing it.
    patched, report = suggest_update(str(src), loaded)
    changes = patched.meta.get("patched_for_drift")
    change_list = changes if isinstance(changes, list) else ([str(changes)] if changes else [])
    written = None
    if report.has_drift and patched_path is not None:
        yaml_text = patched.to_yaml()
        _refuse_existing(patched_path, "patched recipe", overwrite=False, new_text=yaml_text)
        patched.save(patched_path)
        written = str(patched_path)
    return {
        **base,
        "drift": _drift_dict(report),
        "patch_changes": change_list[:MAX_MAP_ENTRIES] if report.has_drift else [],
        "patched_recipe_path": written,
        "next_step": (
            "Drift found: stop and show the user these findings"
            + (" and the patched recipe" if written else "")
            + " before replaying. Do not force the replay without their approval."
            if report.has_drift
            else "No drift: apply_recipe(path, recipe, out=...) is safe to run."
        ),
    }


# ---------------------------------------------------------------------------
# explain_recipe
# ---------------------------------------------------------------------------
_ON_FAIL_TEXT = {
    "quarantine": "failing rows are moved to the quarantine with the reason (never deleted)",
    "error": "the run stops with a validation error",
    "warn": "failures are only logged; the rows are kept",
    "drop": "failing rows are dropped (recorded in the diff)",
    "null": "failing cells are blanked to missing",
}

_NAMED_CHECKS = {
    "not_null": "must not be missing",
    "unique": "must be unique (every duplicated value fails)",
    "valid_email": "must look like an email address",
    "valid_phone": "must contain 7 to 15 digits",
    "valid_url": "must be a URL (http://, https:// or www.)",
}


def explain_recipe(recipe: str) -> dict[str, Any]:
    """Explain a recipe YAML in plain English, step by step. Read-only.

    Use it to review a recipe with the user before applying it, or to understand a
    recipe someone else wrote. "text" is the explanation, ready to show: for each
    source column the rename and what each op does, then frame-level ops (e.g. dedup),
    the validation rules and what happens to failing rows, the recorded read options
    and the drift guard. "steps" and "validations" give the same outline as data.
    Long recipes are capped (first 40 columns and rules; *_truncated says so).
    Workbook recipes are explained sheet by sheet.

    Args:
        recipe: Recipe YAML path (absolute path recommended).
    """
    return _run(_explain_recipe, recipe=recipe)


def _explain_recipe(recipe: Any) -> dict[str, Any]:
    from .workbook import WorkbookRecipe, load_recipe

    recipe_path = _path(recipe, "recipe")
    loaded = load_recipe(recipe_path)
    if isinstance(loaded, WorkbookRecipe):
        names = list(loaded.sheets)
        sheets = {name: _explain_single(loaded.sheets[name]) for name in names[:MAX_EXPLAIN_SHEETS]}
        lines = [f"Workbook recipe {recipe_path}: {len(names)} sheet(s), each cleaned on its own."]
        for name, part in sheets.items():
            lines.append("")
            lines.append(f"=== Sheet {name!r} ===")
            lines.extend(part.pop("_lines"))
        if len(names) > MAX_EXPLAIN_SHEETS:
            lines.append(f"(+{len(names) - MAX_EXPLAIN_SHEETS} more sheet(s) not explained)")
        text, cut = _capped_text("\n".join(lines), MAX_EXPLAIN_CHARS)
        return {
            "recipe_path": str(recipe_path),
            "recipe_type": "workbook",
            "sheets": sheets,
            "sheets_total": len(names),
            "sheets_truncated": len(names) > MAX_EXPLAIN_SHEETS,
            "text": text,
            "text_truncated": cut,
        }
    part = _explain_single(loaded)
    lines = [f"Recipe {recipe_path} (format v{2 if loaded.read else 1})", *part.pop("_lines")]
    text, cut = _capped_text("\n".join(lines), MAX_EXPLAIN_CHARS)
    return {
        "recipe_path": str(recipe_path),
        "recipe_type": "single",
        **part,
        "text": text,
        "text_truncated": cut,
    }


def _doc_line(doc: str) -> str:
    for line in (doc or "").strip().splitlines():
        if line.strip():
            return line.strip().replace("``", "`")
    return "(no description)"


def _short(value: Any) -> str:
    text = repr(value) if isinstance(value, str) else str(_jsonable(value))
    return text if len(text) <= 60 else text[:57] + "..."


def _render(value: Any) -> str:
    if isinstance(value, dict):
        items = list(value.items())
        parts = [f"{_short(k)} -> {_short(v)}" for k, v in items[:MAX_EXPLAIN_ITEMS]]
        if len(items) > MAX_EXPLAIN_ITEMS:
            parts.append(f"... +{len(items) - MAX_EXPLAIN_ITEMS} more")
        return "{" + ", ".join(parts) + "}"
    if isinstance(value, (list, tuple)):
        parts = [_short(v) for v in list(value)[:MAX_EXPLAIN_ITEMS]]
        if len(value) > MAX_EXPLAIN_ITEMS:
            parts.append(f"... +{len(value) - MAX_EXPLAIN_ITEMS} more")
        return "[" + ", ".join(parts) + "]"
    return _short(value)


def _op_text(op: Op) -> str:
    """``name (params as written in the YAML): first docstring line``."""
    from .ops import OP_REGISTRY, op_to_compact

    spec = OP_REGISTRY.get(op.name)
    doc = _doc_line(spec.doc) if spec is not None else "custom op (not registered in this process)"
    compact = op_to_compact(op)
    detail = ""
    if isinstance(compact, dict):
        value = compact.get(op.name)
        if isinstance(value, dict) and not (spec is not None and spec.free_form):
            detail = ", ".join(f"{k}={_render(v)}" for k, v in value.items())
        else:
            detail = _render(value)
    return f"{op.name}{f' ({detail})' if detail else ''}: {doc}"


def _check_text(rule: Any) -> str:
    check = str(rule.check).strip()
    if check in _NAMED_CHECKS:
        return _NAMED_CHECKS[check]
    if check == "in":
        return f"must be one of {_render(rule.params.get('values') or [])}"
    from .checks import _CMP_RE, VALIDATOR_REGISTRY

    if _CMP_RE.match(check):
        return f"must be {check}"
    if check in VALIDATOR_REGISTRY:
        return f"must pass the custom check {check!r}"
    return f"must satisfy `{check}`"


def _explain_single(recipe: Recipe) -> dict[str, Any]:
    """Outline (data) and explanation lines (text) for one recipe, both capped."""
    lines: list[str] = []
    read = recipe.read or {}
    if read:
        opts = ", ".join(f"{k}={_render(v)}" for k, v in read.items())
        read_text = f"Read options recorded at planning time and re-applied on replay: {opts}."
    else:
        read_text = "Reads the whole file; encoding and delimiter are auto-detected."
    lines.append(read_text)
    lines.append(
        "Order: each column's ops top to bottom -> renames -> frame ops -> validation. "
        "Columns the recipe does not list pass through unchanged."
    )

    columns = recipe.columns
    steps = []
    lines.append("")
    lines.append(f"Columns ({len(columns)}):")
    for n, col in enumerate(columns[:MAX_EXPLAIN_STEPS], start=1):
        head = f'{n}. "{col.source}"'
        if col.rename_to:
            head += f' -> renamed to "{col.rename_to}"'
        if not col.ops:
            head += " (values unchanged)"
        lines.append(head)
        lines.extend(f"   - {_op_text(op)}" for op in col.ops)
        steps.append(
            {"column": col.source, "output_name": col.output_name, "ops": [op.name for op in col.ops]}
        )
    if len(columns) > MAX_EXPLAIN_STEPS:
        lines.append(f"(+{len(columns) - MAX_EXPLAIN_STEPS} more column(s) not explained)")

    lines.append("")
    if recipe.frame_ops:
        lines.append("Frame operations (whole table, after the renames):")
        lines.extend(f"- {_op_text(op)}" for op in recipe.frame_ops)
    else:
        lines.append("Frame operations: none.")

    rules = recipe.validations
    lines.append("")
    if rules:
        lines.append(
            f"Validation ({len(rules)} rule(s), checked after cleaning; strict mode "
            "turns every failure into an error):"
        )
        for rule in rules[:MAX_EXPLAIN_STEPS]:
            policy = _ON_FAIL_TEXT.get(rule.on_fail, f"on_fail={rule.on_fail}")
            lines.append(f"- {rule.column} {_check_text(rule)}; otherwise {policy}.")
        if len(rules) > MAX_EXPLAIN_STEPS:
            lines.append(f"(+{len(rules) - MAX_EXPLAIN_STEPS} more rule(s) not explained)")
    else:
        lines.append("Validation: no rules.")

    fp = recipe.source_fingerprint
    if isinstance(fp, dict) and fp:
        names = [str(c) for c in fp.get("column_names") or []]
        shown = ", ".join(names[:MAX_EXPLAIN_ITEMS]) + (" ..." if len(names) > MAX_EXPLAIN_ITEMS else "")
        drift_text = (
            f"Drift guard: planned on a file with {len(names)} column(s) ({shown}); "
            "apply_recipe stops when a new file's columns, types or value formats differ."
        )
    else:
        drift_text = "Drift guard: none (no source_fingerprint), so replays are not checked."
    lines.append("")
    lines.append(drift_text)
    notes = recipe.meta.get("notes") if isinstance(recipe.meta, dict) else None
    if isinstance(notes, list) and notes:
        lines.append("Planning notes: " + " | ".join(str(n) for n in notes[:MAX_EXPLAIN_ITEMS]))

    return {
        "read": read_text,
        "steps": steps,
        "steps_total": len(columns),
        "steps_truncated": len(columns) > MAX_EXPLAIN_STEPS,
        "frame_ops": [op.name for op in recipe.frame_ops],
        "validations": [
            {"column": r.column, "check": r.check, "on_fail": r.on_fail}
            for r in rules[:MAX_EXPLAIN_STEPS]
        ],
        "validations_total": len(rules),
        "validations_truncated": len(rules) > MAX_EXPLAIN_STEPS,
        "drift_guard": drift_text,
        "_lines": lines,
    }


# ---------------------------------------------------------------------------
# list_capabilities
# ---------------------------------------------------------------------------
_OPTION_DOCS = {
    "dayfirst": "true/false: how to read ambiguous dates such as 03/04/2024",
    "phone_country_code": 'default country code for local phone numbers, e.g. "+91"',
    "region": "alias for phone_country_code",
    "category_map": 'seed synonyms per column, e.g. {"City": {"BLR": "Bangalore"}}',
    "rename_columns": "false skips the automatic snake_case renaming of every column",
    "max_diff_changes": "cap on stored cell-level diff entries (default 100000)",
}


def list_capabilities() -> dict[str, Any]:
    """List what CleanFrame can do here: ops, detectors, validation checks, modes. Read-only.

    Use it before writing or editing a recipe by hand: every op name, its scope
    (column or frame), its parameters and a one-line description; every detector
    (what problems profiling can find); the validation checks a recipe may use; the
    modes; the planner options suggest_recipe accepts; and the file formats. Includes
    custom ops/detectors from installed CleanFrame plugins.
    """
    return _run(_list_capabilities)


def _list_capabilities() -> dict[str, Any]:
    from .checks import list_validators
    from .detectors import DETECTOR_REGISTRY, list_detectors
    from .ops import OP_REGISTRY, list_ops
    from .planner import MODE_THRESHOLDS

    ops = [
        {
            "name": name,
            "scope": OP_REGISTRY[name].scope,
            "params": sorted(OP_REGISTRY[name].known_params),
            "doc": _doc_line(OP_REGISTRY[name].doc),
            "streamable": bool(OP_REGISTRY[name].streamable),
        }
        for name in list_ops()
    ]
    detectors = [
        {
            "name": name,
            "scope": DETECTOR_REGISTRY[name].scope,
            "requires_schema": bool(DETECTOR_REGISTRY[name].requires_schema),
            "doc": _doc_line(DETECTOR_REGISTRY[name].doc),
        }
        for name in list_detectors()
    ]
    return {
        "cleanframe_version": __version__,
        "ops": ops,
        "detectors": detectors,
        "validation_checks": {
            "named": list_validators(),
            "expressions": [
                "comparisons: '>= 0', '> 0', '<= 100', '== 1', '!= 0'",
                "membership: check: in, with values: [a, b]",
                "regex: 'matches: ^[A-Z]{3}$'",
            ],
            "on_fail": sorted(_ON_FAIL_TEXT),
        },
        "modes": {
            m.value: f"plans fixes with confidence >= {bar}"
            + ("; drift and any validation failure are errors" if m.value == "strict" else "")
            for m, bar in MODE_THRESHOLDS.items()
        },
        "on_drift": ["error", "warn", "ignore"],
        "planner_options": dict(_OPTION_DOCS),
        "read_formats": [".csv", ".tsv", ".txt", ".xlsx", ".xlsm", ".xls", ".parquet", ".json"],
        "write_formats": list(DATA_SUFFIXES),
        "docs": "https://github.com/inboxpraveen/Cleanframe/blob/main/docs/recipe-spec.md",
    }


# ---------------------------------------------------------------------------
# export_code
# ---------------------------------------------------------------------------
def export_code(
    recipe: str,
    out: str,
    func_name: str = "clean",
    allow_partial: bool = False,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Export a recipe as a standalone pandas script (no CleanFrame dependency).

    The script defines `func_name(df) -> DataFrame` and embeds CleanFrame's own parsing
    code, so it produces the same frame as apply_recipe (rows failing validation are
    removed, not quarantined to a file). Use it when the user wants plain pandas in a
    pipeline or notebook. Returns the path and the readable top part of the code.

    Args:
        recipe: Recipe YAML path.
        out: Output .py path.
        func_name: Name of the generated function (default "clean").
        allow_partial: Export anyway when a custom op has no code equivalent; the gaps
            are marked with "# NOTE" comments. Off by default so nothing is dropped silently.
        overwrite: Replace an existing, different file at out (only when the user agreed).
    """
    return _run(
        _export_code, recipe=recipe, out=out, func_name=func_name,
        allow_partial=allow_partial, overwrite=overwrite,
    )


def _export_code(
    recipe: Any, out: Any, func_name: Any, allow_partial: Any, overwrite: Any
) -> dict[str, Any]:
    from ._util import write_text
    from .codegen import generate_code

    recipe_path = _path(recipe, "recipe")
    out_path = _with_suffix(_path(out, "out"), "out", (".py",))
    _refuse_clash(out_path, "generated code", {"recipe file": recipe_path})
    if not isinstance(func_name, str):
        raise InvalidArgument(f"func_name must be a string, got {func_name!r}.")
    loaded = _load_single_recipe(recipe_path)
    code = generate_code(loaded, func_name=func_name, allow_partial=bool(allow_partial))
    _refuse_existing(out_path, "code file", overwrite=bool(overwrite), new_text=code)
    write_text(out_path, code)

    # Everything under the banner is embedded helper source; the top is the recipe.
    readable = code.split("\n# " + "=" * 76, 1)[0]
    shown, cut = _capped_text(readable)
    module = out_path.stem
    usage = (
        f"from {module} import {func_name}; clean_df = {func_name}(df)"
        if module.isidentifier()
        else f"load {out_path.name} with runpy/importlib, then call {func_name}(df)"
    )
    return {
        "recipe_path": str(recipe_path),
        "code_path": str(out_path),
        "function": func_name,
        "lines": code.count("\n") + 1,
        "partial": "# NOTE:" in code,
        "code_preview": shown,
        "code_preview_truncated": cut,
        "usage": usage,
        "read_options": loaded.read or {},
        "next_step": (
            "Read the input with the same options as the recipe's read: section "
            "(read_options) before calling the function."
        ),
    }


__all__ = [
    "profile_data",
    "suggest_recipe",
    "apply_recipe",
    "check_drift",
    "explain_recipe",
    "list_capabilities",
    "export_code",
    "InvalidArgument",
    "TOOLS",
]

#: The public tools, in the order an agent typically uses them.
TOOLS: tuple[Callable[..., dict[str, Any]], ...] = (
    profile_data,
    suggest_recipe,
    apply_recipe,
    check_drift,
    explain_recipe,
    list_capabilities,
    export_code,
)
