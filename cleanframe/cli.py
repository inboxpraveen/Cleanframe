"""The ``cleanframe`` command-line interface.

Subcommands mirror the library:

* ``report FILE``            — write an HTML profiling report.
* ``clean FILE``            — plan + clean; save recipe, code, cleaned data, report.
* ``apply FILE --recipe R`` — replay a recipe (with drift check).
* ``suggest FILE --recipe R`` — show drift and optionally patch the recipe.
* ``infer-schema FILE``     — draft a target schema.
* ``detectors`` / ``ops``   — list what's available.

Exit codes: ``0`` success, ``1`` data/recipe/output error, ``2`` usage error,
``3`` stopped on schema drift, ``4`` validation failed, ``70`` internal error,
``130`` interrupted. Pass ``--debug`` (or set ``CLEANFRAME_DEBUG=1``) to print a
traceback for an internal error instead of a one-line message.

Stdout is switched to UTF-8 so currency symbols and diff glyphs render on any
terminal (notably Windows consoles).
"""

from __future__ import annotations

import argparse
import os
import sys
import warnings
from pathlib import Path

from ._version import __version__
from .errors import CleanFrameError, CleanFrameWarning, DriftError, ValidationFailure

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_DRIFT = 3
EXIT_VALIDATION = 4
EXIT_INTERNAL = 70
EXIT_INTERRUPT = 130

_ISSUE_URL = "https://github.com/inboxpraveen/Cleanframe/issues"


def _reconfigure_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):  # pragma: no cover - older/odd streams
            pass


def _emit(text: str, *, stream=None) -> None:
    """Print a line, degrading gracefully on a console that cannot encode it."""
    target = stream or sys.stdout
    try:
        print(text, file=target)
    except UnicodeEncodeError:  # pragma: no cover - depends on console codepage
        encoding = getattr(target, "encoding", None) or "ascii"
        print(text.encode(encoding, "replace").decode(encoding, "replace"), file=target)


def _install_warning_format() -> None:
    """Show advisories as one readable line instead of a file path and source echo."""

    def show(message, category, filename, lineno, file=None, line=None):  # noqa: ANN001
        text = str(message)
        if text.startswith("CleanFrame:"):
            text = text[len("CleanFrame:") :].strip()
        elif not issubclass(category, CleanFrameWarning):
            text = f"{category.__name__}: {text}"
        _emit(f"⚠ {text}", stream=file or sys.stderr)

    warnings.showwarning = show


def _debug_enabled(args: argparse.Namespace | None = None) -> bool:
    if args is not None and getattr(args, "debug", False):
        return True
    return os.environ.get("CLEANFRAME_DEBUG", "").strip() not in ("", "0", "false", "False")


# ---------------------------------------------------------------------------
# argument helpers
# ---------------------------------------------------------------------------
def _nonneg_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a whole number, got {text!r}") from None
    if value < 0:
        raise argparse.ArgumentTypeError(f"must be 0 or more, got {value}")
    return value


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a whole number, got {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be 1 or more, got {value}")
    return value


def _default_out(file: str, suffix: str) -> Path:
    return Path(file).with_suffix(suffix)


def _add_selection_args(parser: argparse.ArgumentParser, *, sheet: bool = True) -> None:
    if sheet:
        parser.add_argument(
            "--sheet",
            help="Excel sheet name, or #N for a 0-based index (e.g. --sheet '#0')",
        )
    parser.add_argument("--columns", help="comma-separated column subset to read")
    parser.add_argument("--nrows", type=_nonneg_int, help="read only the first N data rows")
    parser.add_argument(
        "--skiprows",
        type=_nonneg_int,
        help="skip the first N data rows (the header row is always kept)",
    )


def _add_read_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--sep", help="field delimiter, overriding auto-detection")
    parser.add_argument("--encoding", help="file encoding, overriding auto-detection")
    parser.add_argument(
        "--text",
        action="store_true",
        help="read every field verbatim (keeps leading zeros, 'NA' text and 1e5 exact)",
    )
    parser.add_argument(
        "--no-correct", action="store_true", help="disable read-time format auto-detection"
    )


def _selection_kwargs(args: argparse.Namespace) -> dict:
    out: dict = {}
    sheet = getattr(args, "sheet", None)
    if sheet is not None:
        # Digits alone are sheet *names* (e.g. "2024"). Use "#0" / "#1" for indices.
        if sheet.startswith("#") and sheet[1:].lstrip("-").isdigit():
            out["sheet"] = int(sheet[1:])
        else:
            out["sheet"] = sheet
    cols = getattr(args, "columns", None)
    if cols:
        out["columns"] = [c.strip() for c in cols.split(",") if c.strip()]
    for name in ("nrows", "skiprows"):
        if getattr(args, name, None) is not None:
            out[name] = getattr(args, name)
    return out


def _read_kwargs(args: argparse.Namespace) -> dict:
    out: dict = {}
    if getattr(args, "sep", None):
        out["sep"] = args.sep
    if getattr(args, "encoding", None):
        out["encoding"] = args.encoding
    if getattr(args, "text", False):
        out["text"] = True
    if hasattr(args, "no_correct"):
        out["correct_format"] = not args.no_correct
    return out


def _print_log(args: argparse.Namespace, log: list[str]) -> None:
    if getattr(args, "verbose", False) and log:
        _emit("")
        for line in log:
            _emit(f"  · {line}")


def _reserve_outputs(args: argparse.Namespace, *attrs: str) -> None:
    """Validate output paths up front so a bad one fails before any work is done."""
    from ._util import check_output_target

    for attr in attrs:
        target = getattr(args, attr, None)
        if target:
            check_output_target(target, args.file, overwrite=getattr(args, "overwrite", False))


def _reject_unsupported(mode: str, args: argparse.Namespace, names: dict[str, str]) -> None:
    """Fail loudly for flags that would otherwise be silently ignored."""
    given = [flag for attr, flag in names.items() if getattr(args, attr, None)]
    if given:
        raise CleanFrameError(
            f"{', '.join(given)} {'is' if len(given) == 1 else 'are'} not supported in "
            f"{mode}. Re-run without {'it' if len(given) == 1 else 'them'}."
        )


# ---------------------------------------------------------------------------
# command handlers
# ---------------------------------------------------------------------------
def _cmd_report(args: argparse.Namespace) -> int:
    from . import report as _report
    from ._util import check_output_target

    rep = _report(args.file, schema=args.schema, **_selection_kwargs(args), **_read_kwargs(args))
    out = Path(args.out) if args.out else _default_out(args.file, ".report.html")
    rep.save(check_output_target(out, args.file))
    _emit(f"✓ Report written to {out}")
    q = rep.quality
    if q:
        _emit(f"  Quality score: {q.score}/100 (grade {q.grade} — {q.label})")
    if args.open:
        import webbrowser

        webbrowser.open(out.resolve().as_uri())
    return EXIT_OK


def _is_multisheet_workbook(file: str, selection: dict) -> bool:
    """A multi-sheet .xlsx with no explicit --sheet -> clean every tab (workbook mode)."""
    path = Path(file)
    if path.suffix.lower() not in (".xlsx", ".xls", ".xlsm") or "sheet" in selection:
        return False
    try:
        from .dataio import excel_sheet_names

        return len(excel_sheet_names(path)) > 1
    except CleanFrameError:
        return False


def _out_dir_targets(args: argparse.Namespace, *, workbook: bool) -> None:
    """Expand --out-dir into the individual artifact paths."""
    directory = Path(args.out_dir)
    stem = Path(args.file).stem
    args.recipe = args.recipe or str(directory / f"{stem}.recipe.yaml")
    if workbook:
        # A workbook produces one recipe and one rewritten workbook, nothing else.
        args.out = args.out or str(directory / f"{stem}.clean.xlsx")
        return
    args.out = args.out or str(directory / f"{stem}.clean.csv")
    args.code = args.code or str(directory / f"{stem}.py")
    args.report = args.report or str(directory / f"{stem}.report.html")


def _cmd_clean_workbook(args: argparse.Namespace) -> int:
    from .workbook import clean_workbook

    _reject_unsupported(
        "workbook mode (every sheet is cleaned into one recipe)",
        args,
        {
            "code": "--code", "report": "--report", "quarantine": "--quarantine",
            "nrows": "--nrows", "skiprows": "--skiprows", "sep": "--sep",
            "encoding": "--encoding",
        },
    )
    _reserve_outputs(args, "recipe")
    selection = _selection_kwargs(args)
    result = clean_workbook(
        args.file,
        target_schema=args.schema,
        llm=args.llm,
        mode=args.mode,
        max_tokens_budget=args.max_tokens,
        llm_exposure=args.llm_exposure,
        llm_fallback=not args.no_llm_fallback,
        text=args.text,
        **{k: v for k, v in selection.items() if k == "columns"},
    )
    recipe_out = Path(args.recipe) if args.recipe else _default_out(args.file, ".recipe.yaml")
    result.save_recipe(recipe_out)
    _emit(f"✓ Workbook recipe → {recipe_out}  ({len(result.sheets)} sheet(s) cleaned)")
    if args.out:
        result.save_data(args.out, overwrite=bool(args.overwrite))
        _emit(f"✓ Cleaned workbook → {args.out}")
    _emit("")
    _emit(result.summary())
    if not args.out:
        _emit("\n  (pass --out cleaned.xlsx to write every cleaned sheet back)")
    return EXIT_OK


def _cmd_clean(args: argparse.Namespace) -> int:
    from . import clean as _clean
    from .dataio import write_frame

    selection = _selection_kwargs(args)
    workbook = _is_multisheet_workbook(args.file, selection)
    if args.out_dir:
        _out_dir_targets(args, workbook=workbook)
    if workbook:
        return _cmd_clean_workbook(args)

    _reserve_outputs(args, "recipe", "out", "code", "report", "quarantine")
    result = _clean(
        args.file,
        target_schema=args.schema,
        llm=args.llm,
        mode=args.mode,
        max_tokens_budget=args.max_tokens,
        llm_exposure=args.llm_exposure,
        llm_fallback=not args.no_llm_fallback,
        **selection,
        **_read_kwargs(args),
    )
    recipe_out = Path(args.recipe) if args.recipe else _default_out(args.file, ".recipe.yaml")
    result.recipe.save(recipe_out)
    _emit(f"✓ Recipe   → {recipe_out}")

    if args.out:
        write_frame(result.dataframe, args.out, source=args.file, overwrite=args.overwrite)
        _emit(f"✓ Cleaned  → {args.out}  ({len(result.dataframe)} rows)")
    if args.code:
        result.code.save(args.code)
        _emit(f"✓ Code     → {args.code}")
    if args.report:
        result.report(args.report)
        _emit(f"✓ Report   → {args.report}")
    if result.has_quarantine and args.quarantine:
        write_frame(result.quarantine, args.quarantine, source=args.file, overwrite=args.overwrite)
        _emit(f"✓ Quarantine → {args.quarantine}  ({len(result.quarantine)} rows)")

    _emit("")
    result.diff.show()
    if result.has_quarantine and not args.quarantine:
        _emit(
            f"\n⚠ {len(result.quarantine)} row(s) quarantined "
            "(pass --quarantine FILE to save them)."
        )
    _print_log(args, result.log)
    return EXIT_OK


def _drift_stop(args: argparse.Namespace, exc: DriftError, action: str) -> int:
    _emit(exc.report.render() if exc.report is not None else str(exc))
    _emit("")
    if args.mode == "strict":
        _emit(f"Stopped — strict mode never {action} a drifted file. Re-plan the recipe, or")
        _emit(f"  cleanframe suggest {args.file} --recipe {args.recipe} --update")
    else:
        _emit(f"Stopped — re-run with --force to {action} anyway, or")
        _emit(f"  cleanframe suggest {args.file} --recipe {args.recipe} --update")
    return EXIT_DRIFT


def _cmd_apply_workbook(args: argparse.Namespace, recipe) -> int:
    from .workbook import apply_workbook

    _reject_unsupported(
        "workbook mode (the recipe's per-sheet read: sections govern selection)",
        args,
        {
            "report": "--report", "quarantine": "--quarantine", "sheet": "--sheet",
            "columns": "--columns", "nrows": "--nrows", "skiprows": "--skiprows",
            "sep": "--sep", "encoding": "--encoding", "text": "--text",
        },
    )
    on_drift = "ignore" if args.force else "error"
    try:
        result = apply_workbook(
            args.file, recipe, mode=args.mode,
            check_drift=not args.no_drift_check, on_drift=on_drift,
        )
    except DriftError as exc:
        return _drift_stop(args, exc, "apply")
    out = Path(args.out) if args.out else _default_out(args.file, ".clean.xlsx")
    result.save_data(out, overwrite=bool(args.overwrite))
    _emit(f"✓ Cleaned workbook → {out}")
    _emit("")
    _emit(result.summary())
    return EXIT_OK


def _cmd_apply_stream(args: argparse.Namespace, recipe) -> int:
    from .streaming import stream_apply

    _reject_unsupported(
        "streaming mode (--chunksize); the recipe's read: section governs selection",
        args,
        {"report": "--report", "sheet": "--sheet", "columns": "--columns", "nrows": "--nrows",
         "skiprows": "--skiprows", "sep": "--sep", "encoding": "--encoding", "text": "--text"},
    )
    _reserve_outputs(args, "out", "quarantine")
    out = Path(args.out) if args.out else _default_out(args.file, ".clean.csv")
    try:
        summary = stream_apply(
            recipe, args.file, out, chunksize=args.chunksize, mode=args.mode,
            quarantine_path=args.quarantine, overwrite=args.overwrite,
            check_drift=not args.no_drift_check,
            on_drift="ignore" if args.force else "error",
        )
    except DriftError as exc:
        return _drift_stop(args, exc, "stream")
    _emit(f"✓ Streamed → {out}")
    _emit(summary.render())
    return EXIT_OK


def _cmd_apply(args: argparse.Namespace) -> int:
    from . import apply_recipe
    from .dataio import write_frame
    from .workbook import WorkbookRecipe, load_recipe

    loaded = load_recipe(args.recipe)
    if isinstance(loaded, WorkbookRecipe):
        return _cmd_apply_workbook(args, loaded)
    if args.chunksize:
        return _cmd_apply_stream(args, loaded)

    _reserve_outputs(args, "out", "report", "quarantine")
    try:
        result = apply_recipe(
            args.file,
            loaded,
            mode=args.mode,
            check_drift=not args.no_drift_check,
            on_drift="ignore" if args.force else "error",
            **_selection_kwargs(args),
            **{k: v for k, v in _read_kwargs(args).items() if k != "correct_format"},
        )
    except DriftError as exc:
        return _drift_stop(args, exc, "apply")

    if result.drift is not None and result.drift.has_drift:
        _emit(result.drift.render())
        _emit("")
    out = Path(args.out) if args.out else _default_out(args.file, ".clean.csv")
    write_frame(result.dataframe, out, source=args.file, overwrite=args.overwrite)
    _emit(f"✓ Cleaned → {out}  ({len(result.dataframe)} rows)")
    if args.report:
        result.report(args.report)
        _emit(f"✓ Report  → {args.report}")
    if result.has_quarantine:
        if args.quarantine:
            write_frame(
                result.quarantine, args.quarantine, source=args.file, overwrite=args.overwrite
            )
            _emit(f"✓ Quarantine → {args.quarantine}  ({len(result.quarantine)} rows)")
        else:
            _emit(
                f"⚠ {len(result.quarantine)} row(s) quarantined by validation "
                "(pass --quarantine FILE to save them)."
            )
    _emit("")
    result.diff.show()
    _print_log(args, result.log)
    return EXIT_OK


def _cmd_suggest(args: argparse.Namespace) -> int:
    from . import suggest_update

    patched, drift = suggest_update(args.file, args.recipe, **_selection_kwargs(args))
    _emit(drift.render())
    if not drift.has_drift:
        if args.update:
            _emit("\nNothing to patch — the recipe already matches this file.")
        return EXIT_OK

    if not args.update:
        _emit("\nRe-run with --update to write a patched recipe.")
        return EXIT_DRIFT

    src = Path(args.recipe)
    if args.out:
        write_to: Path = Path(args.out)
    elif args.in_place:
        write_to = src
    else:
        # Default: write a sibling patched file — never clobber the recipe silently.
        write_to = src.with_name(src.stem + ".patched.yaml")
    patched.save(write_to)
    _emit(f"\n✓ Patched recipe written to {write_to}")
    changes = patched.meta.get("patched_for_drift")
    if isinstance(changes, list) and changes:
        for change in changes:
            _emit(f"  • {change}")
    return EXIT_OK


def _cmd_infer_schema(args: argparse.Namespace) -> int:
    from . import infer_schema
    from ._util import check_output_target

    schema = infer_schema(
        args.file, name=args.name, **_selection_kwargs(args), **_read_kwargs(args)
    )
    out = Path(args.out) if args.out else _default_out(args.file, ".schema.yaml")
    schema.save(check_output_target(out, args.file))
    _emit(f"✓ Schema ({len(schema.columns)} columns) → {out}")
    return EXIT_OK


def _cmd_detectors(args: argparse.Namespace) -> int:
    from .detectors import DETECTOR_REGISTRY, list_detectors

    for name in list_detectors():
        spec = DETECTOR_REGISTRY[name]
        doc = (spec.doc or "").strip().splitlines()[0] if spec.doc else ""
        _emit(f"  {name:16s} [{spec.scope}]  {doc}")
    return EXIT_OK


def _cmd_ops(args: argparse.Namespace) -> int:
    from .ops import OP_REGISTRY, list_ops

    for name in list_ops():
        spec = OP_REGISTRY[name]
        doc = (spec.doc or "").strip().splitlines()[0] if spec.doc else ""
        _emit(f"  {name:20s} [{spec.scope}]  {doc}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------
_MODE_HELP = (
    "review (default: surface everything for approval), auto (unattended: only "
    "higher-confidence fixes), strict (fail on drift or validation failures)"
)


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--verbose", "-v", action="store_true", default=argparse.SUPPRESS,
        help="print the run log (skipped columns, quarantine reasons, parse losses)",
    )
    common.add_argument(
        "--debug", action="store_true", default=argparse.SUPPRESS,
        help="print a traceback on an internal error",
    )

    parser = argparse.ArgumentParser(
        prog="cleanframe",
        description="The reproducible data-cleaning engine. Profile, clean, replay, detect drift.",
    )
    parser.add_argument("--version", action="version", version=f"cleanframe {__version__}")
    parser.add_argument("--verbose", "-v", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--debug", action="store_true", help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("report", help="write an HTML profiling report", parents=[common])
    p.add_argument("file")
    p.add_argument("--out", "-o", help="output .html (default: <file>.report.html)")
    p.add_argument("--schema", help="target schema YAML (adds mapping diagnostics)")
    p.add_argument("--open", action="store_true", help="open the report in a browser")
    _add_read_args(p)
    _add_selection_args(p)
    p.set_defaults(func=_cmd_report)

    p = sub.add_parser("clean", help="plan and clean a file", parents=[common])
    p.add_argument("file")
    p.add_argument("--recipe", help="recipe output (default: <file>.recipe.yaml)")
    p.add_argument("--out", "-o", help="cleaned data output (csv/tsv/xlsx/parquet/json)")
    p.add_argument(
        "--out-dir",
        help="write recipe, cleaned data, code and report into this directory",
    )
    p.add_argument("--code", help="export standalone pandas to this .py")
    p.add_argument("--report", help="write an HTML diff report here")
    p.add_argument("--quarantine", help="write quarantined rows to this file")
    p.add_argument("--schema", help="target schema YAML")
    p.add_argument(
        "--llm",
        help="LLM planner as provider/model, e.g. openrouter/anthropic/claude-sonnet-4, "
        "groq/llama-3.3-70b-versatile, anthropic/claude-sonnet-4-6",
    )
    p.add_argument("--max-tokens", type=_positive_int, default=None, help="LLM token budget cap")
    p.add_argument(
        "--llm-exposure",
        default="metadata",
        choices=["none", "metadata", "sample"],
        help="what the LLM may see (default: metadata — never raw cells)",
    )
    p.add_argument(
        "--no-llm-fallback",
        action="store_true",
        help="fail instead of degrading to the rules planner when an LLM call fails",
    )
    p.add_argument("--mode", default="review", choices=["review", "auto", "strict"], help=_MODE_HELP)
    p.add_argument(
        "--overwrite",
        action="store_true",
        help="allow writing output over the input file (loses the original)",
    )
    _add_read_args(p)
    _add_selection_args(p)
    p.set_defaults(func=_cmd_clean)

    p = sub.add_parser("apply", help="replay a saved recipe (no LLM)", parents=[common])
    p.add_argument("file")
    p.add_argument("--recipe", required=True, help="recipe YAML to replay")
    p.add_argument(
        "--out", "-o",
        help="cleaned data output (default: <file>.clean.csv, or .clean.xlsx for a workbook recipe)",
    )
    p.add_argument("--report", help="write an HTML diff report here")
    p.add_argument("--mode", default="review", choices=["review", "auto", "strict"], help=_MODE_HELP)
    p.add_argument("--no-drift-check", action="store_true", help="skip schema-drift check")
    p.add_argument(
        "--force",
        action="store_true",
        help="apply even when schema drift is detected (default: stop)",
    )
    p.add_argument(
        "--overwrite",
        action="store_true",
        help="allow writing output over the input file (loses the original)",
    )
    p.add_argument(
        "--chunksize",
        type=_positive_int,
        help="stream the CSV in chunks of N rows (out-of-core; row-independent recipes only)",
    )
    p.add_argument("--quarantine", help="write quarantined rows to this file")
    p.add_argument("--sep", help="field delimiter, overriding the recipe's read: section")
    p.add_argument("--encoding", help="file encoding, overriding the recipe's read: section")
    p.add_argument("--text", action="store_true", help="read every field verbatim")
    _add_selection_args(p)
    p.set_defaults(func=_cmd_apply)

    p = sub.add_parser(
        "suggest", help="detect drift and optionally patch the recipe", parents=[common]
    )
    p.add_argument("file")
    p.add_argument("--recipe", required=True, help="recipe YAML to check")
    p.add_argument("--update", action="store_true", help="write a patched recipe")
    p.add_argument(
        "--out", "-o",
        help="where to write the patched recipe (default: <recipe>.patched.yaml)",
    )
    p.add_argument(
        "--in-place",
        action="store_true",
        help="with --update, overwrite the recipe file (default writes *.patched.yaml)",
    )
    _add_selection_args(p)
    p.set_defaults(func=_cmd_suggest)

    p = sub.add_parser("infer-schema", help="draft a target schema from a file", parents=[common])
    p.add_argument("file")
    p.add_argument("--out", "-o", help="schema output (default: <file>.schema.yaml)")
    p.add_argument("--name", help="schema name")
    _add_read_args(p)
    _add_selection_args(p)
    p.set_defaults(func=_cmd_infer_schema)

    sub.add_parser("detectors", help="list available detectors", parents=[common]).set_defaults(
        func=_cmd_detectors
    )
    sub.add_parser("ops", help="list available ops", parents=[common]).set_defaults(func=_cmd_ops)

    return parser


def main(argv: list[str] | None = None) -> int:
    _reconfigure_stdout()
    _install_warning_format()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ValidationFailure as exc:
        _emit(f"✗ {exc}", stream=sys.stderr)
        return EXIT_VALIDATION
    except DriftError as exc:
        _emit(f"✗ {exc}", stream=sys.stderr)
        return EXIT_DRIFT
    except CleanFrameError as exc:
        _emit(f"✗ {exc}", stream=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:  # pragma: no cover
        _emit("Interrupted.", stream=sys.stderr)
        return EXIT_INTERRUPT
    except BrokenPipeError:  # pragma: no cover - piped into head/less
        return EXIT_OK
    except Exception as exc:  # noqa: BLE001 - the CLI must not hand users a traceback
        if _debug_enabled(args):
            raise
        _emit(
            f"✗ Internal error: {type(exc).__name__}: {exc}\n"
            f"  This is a bug. Re-run with --debug for a traceback, then report it at "
            f"{_ISSUE_URL}",
            stream=sys.stderr,
        )
        return EXIT_INTERNAL


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
