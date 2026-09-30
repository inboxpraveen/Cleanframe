"""Multi-sheet Excel workbooks: clean every tab, keep the rest, write them all back.

A workbook is handled as a set of *independent* single-frame cleans — each sheet
gets its own :class:`~cleanframe.recipe.Recipe` and :class:`~cleanframe.result.CleanResult`
(so per-sheet row-ids never collide) collected into a :class:`WorkbookResult`. The
matching :class:`WorkbookRecipe` is one reviewable YAML with a ``sheets:`` block.

Write-back caveat (important): re-emitting a workbook through pandas preserves the
*data* of every sheet, but NOT formulas, cell formatting, merged cells, multi-row
headers, or charts — untouched sheets are round-tripped through pandas, not
byte-copied. To avoid destroying such a workbook, :meth:`WorkbookResult.save_data`
refuses to overwrite the source file in place unless ``overwrite=True``.
"""

from __future__ import annotations

import os
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from ._util import ensure_parent, load_yaml, read_text, write_text
from ._version import __version__
from .dataio import excel_sheet_names
from .errors import CleanFrameError, CleanFrameWarning, RecipeError
from .recipe import Recipe
from .result import CleanResult


# ---------------------------------------------------------------------------
# Workbook recipe (one YAML, one recipe per sheet)
# ---------------------------------------------------------------------------
@dataclass
class WorkbookRecipe:
    """An ordered set of per-sheet recipes. The sheet name is the key — a per-sheet
    :class:`Recipe` never carries its own ``read.sheet`` (no duplicate source of truth)."""

    sheets: dict[str, Recipe] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        out: dict[str, Any] = {"version": 2, "sheets": {n: r.to_dict() for n, r in self.sheets.items()}}
        if self.meta:
            out["meta"] = self.meta
        return out

    def to_yaml(self) -> str:
        header = (
            "# CleanFrame workbook recipe — one recipe per sheet.\n"
            "# Docs: https://github.com/inboxpraveen/Cleanframe/wiki\n"
        )
        return header + yaml.safe_dump(
            self.to_dict(), sort_keys=False, allow_unicode=True, default_flow_style=False, width=100
        )

    def save(self, path: str | Path) -> Path:
        return write_text(path, self.to_yaml())

    @classmethod
    def from_dict(cls, raw: dict) -> WorkbookRecipe:
        if not isinstance(raw, dict) or "sheets" not in raw:
            raise RecipeError("A workbook recipe must have a top-level 'sheets' mapping.")
        if not isinstance(raw["sheets"], dict):
            raise RecipeError("Workbook 'sheets' must be a mapping of sheet name -> recipe.")
        sheets = {str(n): Recipe.from_dict(spec or {}) for n, spec in raw["sheets"].items()}
        return cls(sheets=sheets, meta=raw.get("meta", {}) or {})

    @classmethod
    def load(cls, path: str | Path) -> WorkbookRecipe:
        path = Path(path)
        if not path.exists():
            raise RecipeError(f"Recipe not found: {path}")
        if path.is_dir():
            raise RecipeError(f"Recipe path is a directory, not a file: {path}")
        return cls.from_dict(load_yaml(read_text(path), error=RecipeError, what="recipe"))


def load_recipe(path: str | Path) -> Recipe | WorkbookRecipe:
    """Load a recipe file, returning a :class:`WorkbookRecipe` if it has a ``sheets:``
    block, else a single :class:`~cleanframe.recipe.Recipe`."""
    path = Path(path)
    if not path.exists():
        raise RecipeError(f"Recipe not found: {path}")
    if path.is_dir():
        raise RecipeError(f"Recipe path is a directory, not a file: {path}")
    data = load_yaml(read_text(path), error=RecipeError, what="recipe")
    if isinstance(data, dict) and "sheets" in data:
        return WorkbookRecipe.from_dict(data)
    return Recipe.from_dict(data)


# ---------------------------------------------------------------------------
# Workbook result
# ---------------------------------------------------------------------------
@dataclass
class WorkbookResult:
    """The outcome of cleaning a workbook: a :class:`CleanResult` per cleaned sheet,
    plus the untouched sheets (kept verbatim for write-back), in workbook order."""

    sheets: dict[str, CleanResult]
    untouched: dict[str, pd.DataFrame] = field(default_factory=dict)
    sheet_order: list[str] = field(default_factory=list)
    source: str | None = None
    #: openpyxl ``sheet_state`` per sheet read (visible/hidden/veryHidden), so a hidden
    #: tab is written back hidden instead of silently becoming visible.
    sheet_states: dict[str, str] = field(default_factory=dict)

    @property
    def recipe(self) -> WorkbookRecipe:
        return WorkbookRecipe(
            sheets={n: r.recipe for n, r in self.sheets.items()},
            meta={"created_with": f"cleanframe {__version__}", "source": self.source},
        )

    @property
    def frames(self) -> dict[str, pd.DataFrame]:
        """Cleaned frame per cleaned sheet (does not include untouched sheets)."""
        return {n: r.dataframe for n, r in self.sheets.items()}

    def save_recipe(self, path: str | Path) -> Path:
        return self.recipe.save(path)

    def save_data(self, path: str | Path, *, overwrite: bool = False) -> Path:
        """Write every sheet (cleaned where cleaned, original otherwise) to one .xlsx.

        Refuses to overwrite the *source* workbook in place unless ``overwrite=True``
        — re-emitting through pandas keeps data but drops formulas/formatting, so an
        in-place rewrite could silently destroy a rich workbook.
        """
        path = Path(path)
        if (
            self.source
            and Path(self.source).exists()
            and path.resolve() == Path(self.source).resolve()
            and not overwrite
        ):
            raise CleanFrameError(
                "Refusing to overwrite the source workbook in place — formulas and "
                "formatting are lost when pandas re-emits a sheet. Write to a new path, "
                "or pass overwrite=True if you accept a data-only rewrite."
            )
        from ._util import sanitize_dataframe_for_spreadsheet

        ensure_parent(path)
        tmp = path.with_name(path.name + ".cf-tmp")
        used_sheet_names: dict[str, str] = {}
        written: dict[str, str] = {}  # written sheet name -> its original sheet_state
        try:
            with pd.ExcelWriter(tmp, engine="openpyxl") as xl:
                for name in self.sheet_order:
                    if name in self.sheets:
                        frame = sanitize_dataframe_for_spreadsheet(self.sheets[name].dataframe)
                    elif name in self.untouched:
                        frame = sanitize_dataframe_for_spreadsheet(self.untouched[name])
                    else:
                        continue
                    sheet_name = name[:31]
                    if sheet_name in used_sheet_names and used_sheet_names[sheet_name] != name:
                        raise CleanFrameError(
                            f"Excel sheet name collision after 31-char truncation: "
                            f"{name!r} and {used_sheet_names[sheet_name]!r} both become "
                            f"{sheet_name!r}. Rename one of the sheets before writing."
                        )
                    used_sheet_names[sheet_name] = name
                    frame.to_excel(xl, sheet_name=sheet_name, index=False)
                    written[sheet_name] = self.sheet_states.get(name, "visible")
                # Restore hidden tabs, but never hide every sheet (Excel needs one visible).
                if any(state == "visible" for state in written.values()):
                    for sheet_name, state in written.items():
                        if state != "visible":
                            xl.sheets[sheet_name].sheet_state = state
            os.replace(tmp, path)
        except ImportError as exc:  # pragma: no cover
            tmp.unlink(missing_ok=True)
            raise CleanFrameError(
                "Writing .xlsx requires openpyxl. Try `pip install cleanframe-engine[excel]`."
            ) from exc
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        return path

    def summary(self) -> str:
        lines = [f"Workbook: {self.source or '<in-memory>'}  ({len(self.sheet_order)} sheet(s))"]
        for name in self.sheet_order:
            if name in self.sheets:
                d = self.sheets[name].diff.summary()
                lines.append(
                    f"  ✓ {name}: {d['changed_cells']} cell(s) changed, "
                    f"{d['rows_before']} → {d['rows_after']} rows"
                )
            else:
                lines.append(f"  · {name}: untouched")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Reading + cleaning + applying
# ---------------------------------------------------------------------------
def _sheet_selection(sheets: Any) -> list[str] | None:
    if sheets is None:
        return None
    if isinstance(sheets, str):
        raise CleanFrameError(
            f"sheets= must be a list of sheet names, not the single string {sheets!r} "
            "(a string would be read one character at a time)."
        )
    return [str(s) for s in sheets]


def read_workbook(
    path: str | Path, sheets: list[str] | None = None, *, text: bool = False
) -> dict[str, pd.DataFrame]:
    """Read all (or the named) sheets of a workbook into an ordered dict.

    ``text=True`` reads every cell verbatim. Otherwise pandas' type inference is
    compared against a bounded verbatim read and anything it changed is reported,
    because that happens before any recipe runs and no diff can show it.
    """
    path = Path(path)
    selected = _sheet_selection(sheets)
    names = excel_sheet_names(path)
    want = selected if selected else names
    missing = [s for s in want if s not in names]
    if missing:
        raise CleanFrameError(f"Sheet(s) {missing} not found in {path.name} (has {names}).")
    try:
        from .dataio import _text_kwargs

        data = pd.read_excel(path, sheet_name=want, **(_text_kwargs() if text else {}))
    except Exception as exc:  # noqa: BLE001
        raise CleanFrameError(f"Could not read workbook {path.name}: {exc}.") from exc
    if not isinstance(data, dict):  # single sheet requested as a bare name
        data = {want[0]: data}
    frames = {name: data[name] for name in want}
    from .dataio import _warn_excel_structure

    _warn_excel_structure(path, want)
    if not text:
        _warn_workbook_losses(path, want, frames)
    return frames


def _warn_workbook_losses(
    path: Path, want: list[str], frames: dict[str, pd.DataFrame], limit: int = 200
) -> None:
    from .dataio import _text_kwargs, compare_for_losses

    try:
        verbatim = pd.read_excel(path, sheet_name=want, nrows=limit, **_text_kwargs())
    except Exception:  # noqa: BLE001 - the advisory is never worth failing over
        return
    if not isinstance(verbatim, dict):
        verbatim = {want[0]: verbatim}
    notes: list[str] = []
    for name in want:
        losses = compare_for_losses(frames[name], verbatim.get(name, pd.DataFrame()))
        notes += [f"{name}.{col}: {why}" for col, why in sorted(losses.items())]
    if notes:
        warnings.warn(
            "CleanFrame: pandas type inference changed values while reading "
            f"{path.name} ({'; '.join(notes)}). Pass text=True to read every cell "
            "verbatim.",
            CleanFrameWarning,
            stacklevel=3,
        )


def _sheet_states(path: str | Path | None) -> dict[str, str]:
    """``{sheet name: visible|hidden|veryHidden}`` for an .xlsx/.xlsm (empty otherwise)."""
    if path is None or Path(path).suffix.lower() not in (".xlsx", ".xlsm"):
        return {}
    try:
        import openpyxl

        wb = openpyxl.load_workbook(path, read_only=True)
        try:
            return {ws.title: ws.sheet_state for ws in wb.worksheets}
        finally:
            wb.close()
    except Exception:  # noqa: BLE001 - cosmetic; never worth failing a clean over
        return {}


def _load_all_sheets(
    data: str | Path | dict, *, text: bool = False
) -> tuple[dict[str, pd.DataFrame], list[str], str | None]:
    if isinstance(data, dict):
        frames = {str(k): v for k, v in data.items()}
        return frames, list(frames), None
    path = Path(data)
    order = excel_sheet_names(path)
    frames = read_workbook(path, order, text=text)
    return frames, order, str(path)


def clean_workbook(
    data: str | Path | dict,
    *,
    sheets: list[str] | None = None,
    target_schema: Any = None,
    schema: Any = None,
    llm: Any = None,
    mode: Any = "review",
    options: dict[str, Any] | None = None,
    text: bool = False,
    **clean_kwargs: Any,
) -> WorkbookResult:
    """Clean every sheet of a workbook independently and return a :class:`WorkbookResult`.

    ``sheets`` limits which tabs are cleaned; the rest are kept verbatim for write-back.
    ``text=True`` reads every cell of every sheet verbatim and records that in each
    sheet's recipe, so :func:`apply_workbook` replays the same read.
    """
    from .api import clean

    frames, order, source = _load_all_sheets(data, text=text)
    selected = _sheet_selection(sheets) or order
    missing = [s for s in selected if s not in frames]
    if missing:
        raise CleanFrameError(f"Sheet(s) {missing} not found (have {order}).")

    results: dict[str, CleanResult] = {}
    untouched: dict[str, pd.DataFrame] = {}
    for name in order:
        frame = frames[name]
        if name in selected:
            results[name] = clean(
                frame,
                target_schema=target_schema,
                schema=schema,
                llm=llm,
                mode=mode,
                options=options,
                text=text,
                source=f"{source}#{name}" if source else name,
                **clean_kwargs,
            )
        else:
            untouched[name] = frame
    return WorkbookResult(
        sheets=results, untouched=untouched, sheet_order=order, source=source,
        sheet_states=_sheet_states(source),
    )


def apply_workbook(
    data: str | Path | dict,
    recipe: WorkbookRecipe | str | Path | dict,
    *,
    mode: Any = "review",
    check_drift: bool = True,
    on_drift: str = "error",
) -> WorkbookResult:
    """Replay a :class:`WorkbookRecipe` across a workbook's sheets (no LLM)."""
    from .api import apply_recipe

    wr = _resolve_workbook_recipe(recipe)
    # Replay the read the recipes were planned with, or a verbatim sheet becomes a
    # type-inferred one and the values stop matching.
    text = any(bool((r.read or {}).get("text")) for r in wr.sheets.values())
    frames, order, source = _load_all_sheets(data, text=text)
    results: dict[str, CleanResult] = {}
    untouched: dict[str, pd.DataFrame] = {}
    for name in order:
        frame = frames[name]
        if name in wr.sheets:
            results[name] = apply_recipe(
                frame,
                wr.sheets[name],
                mode=mode,
                check_drift=check_drift,
                on_drift=on_drift,
                source=f"{source}#{name}" if source else name,
            )
        else:
            untouched[name] = frame
    return WorkbookResult(
        sheets=results, untouched=untouched, sheet_order=order, source=source,
        sheet_states=_sheet_states(source),
    )


def _resolve_workbook_recipe(recipe: WorkbookRecipe | str | Path | dict) -> WorkbookRecipe:
    if isinstance(recipe, WorkbookRecipe):
        return recipe
    if isinstance(recipe, (str, Path)):
        return WorkbookRecipe.load(recipe)
    if isinstance(recipe, dict):
        return WorkbookRecipe.from_dict(recipe)
    raise CleanFrameError(f"Unsupported workbook recipe type: {type(recipe).__name__}.")


__all__ = [
    "WorkbookRecipe",
    "WorkbookResult",
    "clean_workbook",
    "apply_workbook",
    "read_workbook",
    "load_recipe",
]
