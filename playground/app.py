"""CleanFrame playground glue: one call from raw upload bytes to everything the page shows.

This module runs in two places and must behave identically in both:

* in the browser, inside Pyodide (WebAssembly CPython), loaded by ``worker.js``;
* in normal CPython, imported by path from ``tests/test_playground.py`` and by
  ``tests/node_smoke.mjs`` (Node + Pyodide), so the page and the tests cannot drift.

The public surface is :func:`analyze` (returns a JSON-serialisable dict),
:func:`analyze_json` (the same, already encoded, for the Web Worker) and
:func:`runtime_info`. Nothing here does network I/O; the upload is written to a
private temporary directory, read by CleanFrame, and the directory is deleted.

Privacy contract for the share card
-----------------------------------
``result["share"]`` is the only part of the output meant to leave the visitor's
machine (it is drawn on a PNG and put in a post). It is built **only** from issue
*kinds* and *numeric* evidence fields. It never reads ``Issue.message``,
``evidence["examples"]``, column names, cell values or the file name, because
those carry the visitor's data. ``tests/test_playground.py`` enforces this with
sentinel column names and values.

How the Mess Score is computed
------------------------------
``mess = 100 - quality`` where ``quality`` is :func:`cleanframe.quality_score`,
unchanged: every detected issue adds ``severity_weight x affected_fraction``
(error 1.0, warning 0.4, info 0.1; the affected fraction is the issue's count
over the row count, or 0.25 when the issue carries no count), exact duplicate
rows add ``0.5 x duplicate_fraction``, the total is averaged over the columns and
``quality = round(100 x (1 - min(1, average)))``. The page's "How is this
scored?" note shows the same formula with the visitor's own numbers.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import math
import re
import shutil
import sys
import tempfile
import traceback
import warnings
from collections import Counter
from pathlib import Path
from typing import Any

import pandas as pd

import cleanframe as cf
from cleanframe import quality as _quality
from cleanframe.dataio import excel_sheet_names
from cleanframe.result import build_profile_report_object

#: Bumped when the shape of :func:`analyze`'s output changes (the page checks it).
API_VERSION = 1
#: Hard input caps. The page enforces the same numbers before reading the file.
MAX_BYTES = 20 * 1024 * 1024
MAX_XLSX_BYTES = 5 * 1024 * 1024  # a zip expands several-fold once parsed
#: How many example cell changes are returned for display (the counts stay exact).
MAX_CHANGES = 200
#: Longest cell text returned for display; the downloads are never truncated.
MAX_CELL_CHARS = 160
#: How many issue kinds the share card lists.
SHARE_ITEMS = 4
REPO_URL = "https://github.com/inboxpraveen/Cleanframe"

_CSV_EXTS = {".csv": ".csv", ".tsv": ".tsv", ".txt": ".txt", ".tab": ".tsv", ".dat": ".dat"}
_XLSX_EXTS = {".xlsx": ".xlsx", ".xlsm": ".xlsm"}
_ZIP_MAGIC = b"PK\x03\x04"
_SEV_RANK = {"error": 2, "warning": 1, "info": 0}

# ---------------------------------------------------------------------------
# friendly, count-only wording per issue kind
# ---------------------------------------------------------------------------
#: kind -> short chip label (no counts, no data).
KIND_LABELS: dict[str, str] = {
    "mixed_date_formats": "Mixed date formats",
    "nonstandard_date_format": "Non-ISO dates",
    "unparseable_dates": "Unreadable dates",
    "category_variants": "Category spellings",
    "whitespace": "Stray whitespace",
    "unicode_hygiene": "Hidden Unicode",
    "inconsistent_case": "Inconsistent case",
    "missing_values": "Missing values",
    "disguised_nulls": "Nulls in disguise",
    "empty_column": "Empty columns",
    "constant_column": "Constant columns",
    "currency_format": "Money as text",
    "ambiguous_number_format": "Ambiguous numbers",
    "email_normalization": "Untidy emails",
    "invalid_emails": "Invalid emails",
    "phone_normalization": "Phone formats",
    "invalid_phones": "Implausible phones",
    "duplicate_rows": "Duplicate rows",
    "duplicate_keys": "Duplicate IDs",
    "fuzzy_duplicates": "Near-duplicates",
    "outliers": "Outliers",
    "mixed_units": "Mixed units",
    "unit_format": "Units as text",
}


def _plural(n: int, singular: str, plural: str | None = None) -> str:
    return f"{n:,} {singular if n == 1 else (plural or singular + 's')}"


def _int(value: Any) -> int:
    """A non-negative int from an evidence field (anything else counts as 0)."""
    if isinstance(value, bool):
        return 0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(number) or number < 0:
        return 0
    return int(number)


def _sum(group: list[Any], *keys: str) -> int:
    return sum(_int(i.evidence.get(k)) for i in group for k in keys)


def _max_len(group: list[Any], key: str) -> int:
    lengths = [len(v) for i in group if isinstance(v := i.evidence.get(key), (list, tuple, dict))]
    return max(lengths, default=0)


def _cols(group: list[Any]) -> int:
    return len({i.column for i in group}) or 1


def _headline(kind: str, group: list[Any]) -> tuple[int, str]:
    """``(count, phrase)`` for a kind, built from numbers only — never from values."""
    c = _cols(group)
    if kind == "mixed_date_formats":
        n = _max_len(group, "formats_found")
        if c == 1:
            return n, f"{n} date formats in one column"
        return n, f"{c} columns mixing date formats (up to {n} each)"
    if kind == "nonstandard_date_format":
        return c, _plural(c, "column") + " of non-ISO dates"
    if kind == "unparseable_dates":
        return c, _plural(c, "column") + " of unreadable dates"
    if kind == "category_variants":
        before = _sum(group, "distinct_before")
        after = _sum(group, "distinct_after")
        n = sum(len(i.evidence.get("mapping") or {}) for i in group)
        if before and after:
            return n, f"{before:,} spellings of {_plural(after, 'category', 'categories')}"
        return n, _plural(n, "stray category spelling")
    if kind == "whitespace":
        # A value can have both kinds; the larger count never over-claims.
        n = sum(
            max(_int(i.evidence.get("leading_trailing")), _int(i.evidence.get("internal_doubles")))
            for i in group
        )
        return n, _plural(n, "value") + " with stray whitespace"
    if kind == "unicode_hygiene":
        n = _sum(group, "zero_width", "nbsp", "decomposed")
        return n, _plural(n, "value") + " with hidden Unicode characters"
    if kind == "inconsistent_case":
        n = _sum(group, "count")
        return n, _plural(n, "value") + " in inconsistent case"
    if kind == "missing_values":
        n = _sum(group, "null_count")
        return n, _plural(n, "missing value")
    if kind == "disguised_nulls":
        n = _sum(group, "count")
        return n, _plural(n, "null") + " in disguise"
    if kind == "empty_column":
        return c, _plural(c, "empty column")
    if kind == "constant_column":
        return c, _plural(c, "column") + " with one repeated value"
    if kind == "currency_format":
        k = _max_len(group, "currencies")
        if k > 1:
            return k, f"{k} currencies mixed in one column"
        return c, _plural(c, "money column") + " stored as text"
    if kind == "ambiguous_number_format":
        return c, _plural(c, "column") + " of ambiguous numbers"
    if kind == "email_normalization":
        n = _sum(group, "count")
        return n, _plural(n, "email") + " needing a tidy-up"
    if kind == "invalid_emails":
        n = _sum(group, "count")
        return n, _plural(n, "invalid email address", "invalid email addresses")
    if kind == "phone_normalization":
        n = _sum(group, "count")
        return n, _plural(n, "phone number") + " in mixed formats"
    if kind == "invalid_phones":
        n = _sum(group, "count")
        return n, _plural(n, "implausible phone number")
    if kind == "duplicate_rows":
        n = _sum(group, "count")
        return n, _plural(n, "duplicate row")
    if kind == "duplicate_keys":
        n = _sum(group, "count")
        return n, _plural(n, "row") + " sharing an ID that should be unique"
    if kind == "fuzzy_duplicates":
        n = _sum(group, "count")
        return n, _plural(n, "near-duplicate pair")
    if kind == "outliers":
        n = _sum(group, "count")
        return n, _plural(n, "outlier")
    if kind == "mixed_units":
        n = _max_len(group, "units")
        return n, f"{n} units mixed in one column"
    if kind == "unit_format":
        return c, _plural(c, "column") + " of quantities stored as text"
    # Unknown (plugin) kinds: the slug is code, not data.
    return c, f"{kind.replace('_', ' ')} in " + _plural(c, "column")


def _issue_count(issue: Any) -> int | None:
    """The single most useful count for one issue, or ``None`` when it has none."""
    ev = issue.evidence or {}
    for key in ("count", "null_count", "unparsed", "n_failed"):
        if key in ev and not isinstance(ev[key], bool) and isinstance(ev[key], (int, float)):
            return _int(ev[key])
    return None


# ---------------------------------------------------------------------------
# scoring (delegates to cleanframe.quality — never re-implements it)
# ---------------------------------------------------------------------------
_BANDS = {"A": "Pretty tidy", "B": "A little messy", "C": "Messy", "D": "Very messy", "F": "A real mess"}


def _issue_penalty(issue: Any, n_rows: int) -> float:
    """This issue's share of quality_score's penalty (same helpers, same weights)."""
    weight = _quality._SEVERITY_WEIGHT.get(issue.severity, 0.2)
    return weight * _quality._affected_fraction(issue.evidence, n_rows)


def _duplicate_penalty(profile: Any, n_rows: int) -> float:
    if not profile.duplicate_row_count:
        return 0.0
    return 0.5 * min(1.0, profile.duplicate_row_count / n_rows)


# ---------------------------------------------------------------------------
# value formatting
# ---------------------------------------------------------------------------
def _cell(value: Any) -> str | None:
    """A display string for one cell (``None`` for missing). JSON-safe by construction."""
    if value is None:
        return None
    if pd.api.types.is_scalar(value):
        try:
            if pd.isna(value):
                return None
        except (TypeError, ValueError):
            pass
    if isinstance(value, str):
        text = value
    elif isinstance(value, (pd.Timestamp, _dt.datetime, _dt.date)):
        text = value.isoformat()
    elif hasattr(value, "item") and not isinstance(value, (list, tuple, dict)):
        try:
            text = str(value.item())  # numpy scalar -> plain Python
        except (TypeError, ValueError):
            text = str(value)
    else:
        text = str(value)
    if len(text) > MAX_CELL_CHARS:
        text = text[: MAX_CELL_CHARS - 1] + "…"
    return text


def _scrubber(workdir: Path, stored: Path, display: str):
    """Return a function that replaces private temp paths with the display name.

    Both the path as created and its resolved form are replaced (on Windows the temp
    dir may be an 8.3 short path that resolves to a different spelling), and any
    remaining token that mentions the unique temp-dir name is replaced as a whole.
    """
    pairs: dict[str, str] = {}
    for base in (workdir, workdir.resolve()):
        for spelling in (str(base / stored.name), (base / stored.name).as_posix()):
            pairs[spelling] = display
        for spelling in (str(base), base.as_posix()):
            pairs.setdefault(spelling, ".")
    pairs.setdefault(stored.name, display)
    # Longest first, so a file path is replaced before its directory.
    replacements = sorted(pairs.items(), key=lambda kv: -len(kv[0]))
    leftover = re.compile(r"[^\s'\"()]*" + re.escape(workdir.name) + r"[^\s'\"()]*")

    def scrub(text: str) -> str:
        for old, new in replacements:
            if old:
                text = text.replace(old, new)
        return leftover.sub(display, text)

    return scrub


def _display_name(filename: Any) -> str:
    name = str(filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    name = "".join(ch for ch in name if ch.isprintable())
    if not name:
        return "data.csv"
    return name if len(name) <= 120 else name[:117] + "..."


def _error(message: str, *, kind: str = "input", detail: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"ok": False, "api": API_VERSION, "error": message, "error_kind": kind}
    if detail:
        out["detail"] = detail
    return out


# ---------------------------------------------------------------------------
# the main entry point
# ---------------------------------------------------------------------------
def analyze(data: bytes, filename: str = "data.csv") -> dict[str, Any]:
    """Profile, score and clean one uploaded file. Never raises; failures are ``ok: False``.

    ``data`` is the raw upload; ``filename`` is used only to pick the reader (by
    extension) and to label the local report — it is never written into the share
    card. The same bytes and name always produce the same dict.
    """
    try:
        if hasattr(data, "to_bytes") and not isinstance(data, (bytes, bytearray, int)):
            data = data.to_bytes()  # a Pyodide JsProxy of the page's Uint8Array
        data = bytes(data)
    except Exception:  # noqa: BLE001 - JS proxies / odd buffers
        return _error("Could not read the uploaded file's bytes.")
    display = _display_name(filename)
    ext = Path(display).suffix.lower()

    if not data:
        return _error(f"{display} is empty.")
    is_zip = data[:4] == _ZIP_MAGIC
    if ext in _XLSX_EXTS:
        if len(data) > MAX_XLSX_BYTES:
            return _error(
                f"{display} is {len(data) / 2**20:.1f} MB. The playground reads Excel files up to "
                f"{MAX_XLSX_BYTES // 2**20} MB; export the sheet as CSV (up to "
                f"{MAX_BYTES // 2**20} MB) or run CleanFrame locally."
            )
        if not is_zip:
            return _error(f"{display} is not a valid .xlsx workbook (it is not a zip archive).")
        try:
            import openpyxl  # noqa: F401
        except ImportError:
            return _error("Excel support is not loaded in this build. Export the sheet as CSV.")
        stored_ext = _XLSX_EXTS[ext]
    elif ext == ".xls":
        return _error("Legacy .xls workbooks are not supported here. Save it as .xlsx or CSV.")
    else:
        if len(data) > MAX_BYTES:
            return _error(
                f"{display} is {len(data) / 2**20:.1f} MB; the playground's limit is "
                f"{MAX_BYTES // 2**20} MB. Run CleanFrame locally for bigger files "
                "(`pip install cleanframe-engine`)."
            )
        if is_zip:
            return _error(
                f"{display} looks like an Excel workbook or zip archive, not CSV text. "
                "If it is a workbook, give it its .xlsx name."
            )
        stored_ext = _CSV_EXTS.get(ext, ".csv")

    workdir = Path(tempfile.mkdtemp(prefix="cfpg-"))
    stored = workdir / f"input{stored_ext}"
    scrub = _scrubber(workdir, stored, display)
    try:
        stored.write_bytes(data)
        return _analyze_file(stored, workdir, display, len(data), scrub)
    except cf.CleanFrameError as exc:
        message = scrub(str(exc))
        if "encoding=" in message:
            message += (
                " The playground has no encoding option: re-save the file as UTF-8 CSV and "
                "try again, or run CleanFrame locally with --encoding."
            )
        return _error(message)
    except MemoryError:
        return _error(
            f"{display} needs more memory than the browser gives this page. "
            "Try a smaller file, or run CleanFrame locally."
        )
    except Exception as exc:  # noqa: BLE001 - the page must always get a dict
        return _error(
            scrub(f"Unexpected error: {type(exc).__name__}: {exc}"),
            kind="internal",
            detail=scrub(traceback.format_exc(limit=12)),
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _analyze_file(
    stored: Path, workdir: Path, display: str, n_bytes: int, scrub
) -> dict[str, Any]:
    notes: list[str] = []
    sheet = None
    if stored.suffix in (".xlsx", ".xlsm"):
        names = excel_sheet_names(stored)
        if not names:
            return _error(f"{display} has no sheets.")
        if len(names) > 1:
            sheet = names[0]
            notes.append(
                f"The workbook has {len(names)} sheets; the playground analyzed the first one. "
                "Locally, `cleanframe clean` takes --sheet, and clean_workbook() cleans every sheet."
            )

    with warnings.catch_warnings(record=True) as caught:
        # "always": the default once-per-location registry would hide the second
        # call's warnings and make the output depend on call history.
        warnings.simplefilter("always")
        result = cf.clean(stored, source=display, sheet=sheet)
        profile = result.profile
        issues = list(result.issues)
        quality = result.quality
        cleaned_path = workdir / "cleaned.csv"
        cf.write_frame(result.dataframe, cleaned_path)
        cleaned_csv = cleaned_path.read_text(encoding="utf-8")
        quarantine_csv = None
        if result.has_quarantine:
            q_path = workdir / "quarantine.csv"
            cf.write_frame(result.quarantine, q_path)
            quarantine_csv = q_path.read_text(encoding="utf-8")
        code = result.code.to_string()
        recipe_yaml = result.recipe.to_yaml()
        report_html = build_profile_report_object(
            profile, result.issues, source=display, quality=quality
        ).html
        clean_report_html = result.report().html

    for w in caught:
        if issubclass(w.category, cf.CleanFrameWarning):
            text = scrub(str(w.message))
            text = text[len("CleanFrame: "):] if text.startswith("CleanFrame: ") else text
            if text not in notes:
                notes.append(text)

    if profile is None or quality is None:  # pragma: no cover - clean() always sets both
        return _error("CleanFrame returned no profile for this file.", kind="internal")
    if profile.n_rows == 0:
        return _error(f"{display} has a header row but no data rows.")

    n_rows = profile.n_rows or 1
    n_cols = max(1, profile.n_columns)

    # -- issues ---------------------------------------------------------
    issue_rows = []
    for idx, issue in enumerate(issues):
        sev = issue.severity.value
        issue_rows.append({
            "id": idx,
            "kind": issue.kind,
            "label": KIND_LABELS.get(issue.kind, issue.kind.replace("_", " ").capitalize()),
            "severity": sev,
            "column": issue.column,
            "message": scrub(issue.message),
            "count": _issue_count(issue),
            "has_fix": bool(issue.has_fix),
            "penalty": round(_issue_penalty(issue, n_rows), 4),
        })

    # -- per-kind summary (drives chips and the share card) -------------
    groups: dict[str, list[Any]] = {}
    for issue in issues:
        groups.setdefault(issue.kind, []).append(issue)
    kinds = []
    for kind, group in groups.items():
        count, text = _headline(kind, group)
        severity = max((i.severity.value for i in group), key=lambda s: _SEV_RANK.get(s, 0))
        kinds.append({
            "kind": kind,
            "label": KIND_LABELS.get(kind, kind.replace("_", " ").capitalize()),
            "text": text,
            "count": count,
            "columns": _cols(group) if any(i.column is not None for i in group) else 0,
            "issues": len(group),
            "severity": severity,
            "penalty": round(sum(_issue_penalty(i, n_rows) for i in group), 4),
        })
    kinds.sort(key=lambda k: (-_SEV_RANK.get(k["severity"], 0), -k["penalty"], -k["count"], k["kind"]))

    # -- score ----------------------------------------------------------
    dup_penalty = _duplicate_penalty(profile, n_rows)
    total_penalty = sum(_issue_penalty(i, n_rows) for i in issues) + dup_penalty
    mess = 100 - int(quality.score)
    band = "Spotless" if mess == 0 and not issues else _BANDS.get(quality.grade, quality.label)
    score = {
        "mess": mess,
        "quality": int(quality.score),
        "grade": quality.grade,
        "band": band,
        "penalty": round(total_penalty, 4),
        "duplicate_penalty": round(dup_penalty, 4),
        "duplicate_rows": int(profile.duplicate_row_count or 0),
        "columns": n_cols,
        "rows": int(profile.n_rows),
        "average_penalty": round(min(1.0, total_penalty / n_cols), 4),
    }

    # -- columns --------------------------------------------------------
    diff = result.diff
    by_col_changes = diff.changes_by_column()
    renamed = dict(diff.renamed_columns)
    columns = []
    for cp in profile.columns:
        out_name = renamed.get(cp.name, cp.name)
        columns.append({
            "name": cp.name,
            "output_name": out_name,
            "type": cp.semantic_type,
            "nulls": int(cp.null_count),
            "unique": int(cp.unique_count),
            "issues": [r["id"] for r in issue_rows if r["column"] == cp.name],
            "changed": len(by_col_changes.get(out_name, [])),
        })

    # -- diff -----------------------------------------------------------
    changes = _sample_changes(by_col_changes, MAX_CHANGES)
    reasons = Counter(reason for _, reason in diff.dropped_rows)
    summary = diff.summary()
    diff_out = {
        "changed_cells": int(summary["changed_cells"]),
        "changed_columns": int(summary["changed_columns"]),
        "rows_before": int(summary["rows_before"]),
        "rows_after": int(summary["rows_after"]),
        "rows_dropped": int(summary["rows_dropped"]),
        "quarantined": int(len(result.quarantine)),
        "dropped_reasons": [[scrub(str(r)), int(n)] for r, n in sorted(reasons.items())],
        "renamed": [[k, v] for k, v in renamed.items()],
        "added": list(diff.added_columns),
        "removed": list(diff.removed_columns),
        "truncated": bool(diff.truncated),
        "stored": len(diff.changes),
        "shown": len(changes),
    }

    return {
        "ok": True,
        "api": API_VERSION,
        "file": {
            "name": display,
            "kind": "xlsx" if stored.suffix in (".xlsx", ".xlsm") else "csv",
            "bytes": int(n_bytes),
            "rows": int(profile.n_rows),
            "columns": int(profile.n_columns),
            "sheet": sheet,
        },
        "score": score,
        "kinds": kinds,
        "issues": issue_rows,
        "columns": columns,
        "diff": diff_out,
        "changes": changes,
        "notes": notes,
        "recipe_yaml": recipe_yaml,
        "code": code,
        "cleaned_csv": cleaned_csv,
        "quarantine_csv": quarantine_csv,
        "report_html": scrub(report_html),
        "clean_report_html": scrub(clean_report_html),
        "share": build_share(score, kinds),
    }


def _sample_changes(by_col: dict[str, list[Any]], cap: int) -> list[dict[str, Any]]:
    """Pick up to ``cap`` changes round-robin across columns, then order them for display.

    ``compute_diff`` stores changes column by column, so "the first N" would show only
    the first column or two. Round-robin keeps every changed column represented.
    """
    order = list(by_col)
    picked: list[tuple[int, Any]] = []
    depth = 0
    while len(picked) < cap:
        progressed = False
        for ci, col in enumerate(order):
            items = by_col[col]
            if depth < len(items):
                picked.append((ci, items[depth]))
                progressed = True
                if len(picked) >= cap:
                    break
        if not progressed:
            break
        depth += 1
    picked.sort(key=lambda p: (p[0], p[1].row_id))
    return [
        {"row": int(ch.row_id), "column": str(ch.column), "before": _cell(ch.before), "after": _cell(ch.after)}
        for _, ch in picked
    ]


# ---------------------------------------------------------------------------
# share card payload — numbers and fixed wording only
# ---------------------------------------------------------------------------
def build_share(score: dict[str, Any], kinds: list[dict[str, Any]]) -> dict[str, Any]:
    """The share card and post text. Uses only the score and per-kind count phrases."""
    items = [k["text"] for k in kinds[:SHARE_ITEMS]]
    if not items:
        items = ["No problems found"]
    mess = int(score["mess"])
    band = str(score["band"])
    head = f"My data scored {mess}/100 on the CleanFrame Mess Score ({band.lower()})"
    tail = f"Diagnosed and cleaned in my browser, nothing uploaded. {REPO_URL}"
    # X counts every URL as 23 characters; keep a margin under 280.
    budget = 270 - (len(tail) - len(REPO_URL) + 23)
    shown = list(items[:3])
    while shown:
        body = f"{head}: {'; '.join(shown)}."
        if len(body) + 1 <= budget:
            break
        shown.pop()
    text = (f"{head}: {'; '.join(shown)}." if shown else f"{head}.") + " " + tail
    return {"mess": mess, "band": band, "items": items, "text": text, "url": REPO_URL}


def analyze_json(data: Any, filename: str = "data.csv") -> str:
    """:func:`analyze`, encoded as strict JSON (no NaN/Infinity) for the Web Worker."""
    result = analyze(data, filename)
    try:
        return json.dumps(result, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        return json.dumps(
            _error(f"Internal error encoding the result: {exc}", kind="internal"),
            ensure_ascii=False,
        )


def runtime_info() -> dict[str, str]:
    """Versions shown in the page footer."""
    info = {
        "cleanframe": cf.__version__,
        "pandas": pd.__version__,
        "python": sys.version.split()[0],
    }
    with contextlib.suppress(ImportError):
        import openpyxl

        info["openpyxl"] = openpyxl.__version__
    return info


__all__ = ["analyze", "analyze_json", "runtime_info", "build_share", "API_VERSION", "MAX_BYTES"]
