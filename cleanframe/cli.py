"""The ``cleanframe`` command-line interface.

Subcommands mirror the library:

* ``report FILE``            — write an HTML profiling report.
* ``clean FILE``            — plan + clean; save recipe, code, cleaned data, report.
* ``apply FILE --recipe R`` — replay a recipe (with drift check).
* ``suggest FILE --recipe R`` — show drift and optionally patch the recipe.
* ``infer-schema FILE``     — draft a target schema.
* ``detectors`` / ``ops``   — list what's available.

Stdout is switched to UTF-8 so currency symbols and diff glyphs render on any
terminal (notably Windows consoles).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ._version import __version__
from .errors import CleanFrameError


def _reconfigure_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):  # pragma: no cover - older/odd streams
            pass


def _default_out(file: str, suffix: str) -> Path:
    return Path(file).with_suffix(suffix)


# ---------------------------------------------------------------------------
# command handlers
# ---------------------------------------------------------------------------
def _cmd_report(args: argparse.Namespace) -> int:
    from . import report as _report

    rep = _report(args.file, schema=args.schema)
    out = Path(args.out) if args.out else _default_out(args.file, ".report.html")
    rep.save(out)
    q = rep.quality
    print(f"✓ Report written to {out}")
    if q:
        print(f"  Quality score: {q.score}/100 (grade {q.grade} — {q.label})")
    if args.open:
        import webbrowser

        webbrowser.open(out.resolve().as_uri())
    return 0


def _cmd_clean(args: argparse.Namespace) -> int:
    from . import clean as _clean

    result = _clean(
        args.file,
        target_schema=args.schema,
        llm=args.llm,
        mode=args.mode,
        max_tokens_budget=args.max_tokens,
        llm_exposure=args.llm_exposure,
    )
    recipe_out = Path(args.recipe) if args.recipe else _default_out(args.file, ".recipe.yaml")
    result.recipe.save(recipe_out)
    print(f"✓ Recipe   → {recipe_out}")

    if args.out:
        from .dataio import write_frame

        write_frame(result.dataframe, args.out)
        print(f"✓ Cleaned  → {args.out}  ({len(result.dataframe)} rows)")
    if args.code:
        result.code.save(args.code)
        print(f"✓ Code     → {args.code}")
    if args.report:
        result.report(args.report)
        print(f"✓ Report   → {args.report}")
    if result.has_quarantine and args.quarantine:
        result.quarantine.to_csv(args.quarantine, index=False)
        print(f"✓ Quarantine → {args.quarantine}  ({len(result.quarantine)} rows)")

    print()
    result.diff.show()
    if result.has_quarantine and not args.quarantine:
        print(f"\n⚠ {len(result.quarantine)} row(s) quarantined "
              f"(pass --quarantine FILE to save them).")
    return 0


def _cmd_apply(args: argparse.Namespace) -> int:
    from . import apply_recipe
    from .errors import DriftError

    # Default: stop on drift (matches README). --force continues anyway.
    on_drift = "ignore" if args.force else "error"
    try:
        result = apply_recipe(
            args.file,
            args.recipe,
            mode=args.mode,
            check_drift=not args.no_drift_check,
            on_drift=on_drift,
        )
    except DriftError as exc:
        print(exc.report.render() if exc.report is not None else str(exc))
        print()
        print(
            f"Stopped — re-run with --force to apply anyway, or\n"
            f"  cleanframe suggest {args.file} --recipe {args.recipe} --update"
        )
        return 1

    if result.drift is not None and result.drift.has_drift:
        print(result.drift.render())
        print()
    out = Path(args.out) if args.out else _default_out(args.file, ".clean.csv")
    from .dataio import write_frame

    write_frame(result.dataframe, out)
    print(f"✓ Cleaned → {out}  ({len(result.dataframe)} rows)")
    if args.report:
        result.report(args.report)
        print(f"✓ Report  → {args.report}")
    if result.has_quarantine:
        print(f"⚠ {len(result.quarantine)} row(s) quarantined by validation.")
    print()
    result.diff.show()
    return 0


def _cmd_suggest(args: argparse.Namespace) -> int:
    from . import suggest_update

    write_to = None
    if args.update:
        write_to = args.out or args.recipe  # in-place unless --out given
    patched, drift = suggest_update(args.file, args.recipe, out=write_to)
    print(drift.render())
    if write_to:
        changes = patched.meta.get("patched_for_drift")
        print(f"\n✓ Patched recipe written to {write_to}")
        if isinstance(changes, list) and changes:
            for c in changes:
                print(f"  • {c}")
    elif drift.has_drift:
        print("\nRe-run with --update to write a patched recipe.")
    return 1 if drift.has_drift and not args.update else 0


def _cmd_infer_schema(args: argparse.Namespace) -> int:
    from . import infer_schema

    schema = infer_schema(args.file, name=args.name)
    out = Path(args.out) if args.out else _default_out(args.file, ".schema.yaml")
    schema.save(out)
    print(f"✓ Schema ({len(schema.columns)} columns) → {out}")
    return 0


def _cmd_detectors(args: argparse.Namespace) -> int:
    from .detectors import DETECTOR_REGISTRY, list_detectors

    for name in list_detectors():
        spec = DETECTOR_REGISTRY[name]
        doc = (spec.doc or "").strip().splitlines()[0] if spec.doc else ""
        print(f"  {name:16s} [{spec.scope}]  {doc}")
    return 0


def _cmd_ops(args: argparse.Namespace) -> int:
    from .ops import OP_REGISTRY, list_ops

    for name in list_ops():
        spec = OP_REGISTRY[name]
        doc = (spec.doc or "").strip().splitlines()[0] if spec.doc else ""
        print(f"  {name:20s} [{spec.scope}]  {doc}")
    return 0


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cleanframe",
        description="The reproducible data-cleaning engine. Profile, clean, replay, detect drift.",
    )
    parser.add_argument("--version", action="version", version=f"cleanframe {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("report", help="write an HTML profiling report")
    p.add_argument("file")
    p.add_argument("--out", "-o", help="output .html (default: <file>.report.html)")
    p.add_argument("--schema", help="target schema YAML (adds mapping diagnostics)")
    p.add_argument("--open", action="store_true", help="open the report in a browser")
    p.set_defaults(func=_cmd_report)

    p = sub.add_parser("clean", help="plan and clean a file")
    p.add_argument("file")
    p.add_argument("--recipe", help="recipe output (default: <file>.recipe.yaml)")
    p.add_argument("--out", help="cleaned data output (csv/xlsx/parquet/json)")
    p.add_argument("--code", help="export standalone pandas to this .py")
    p.add_argument("--report", help="write an HTML diff report here")
    p.add_argument("--quarantine", help="write quarantined rows to this file")
    p.add_argument("--schema", help="target schema YAML")
    p.add_argument(
        "--llm",
        help="LLM planner as provider/model, e.g. openrouter/anthropic/claude-sonnet-4, "
        "groq/llama-3.3-70b-versatile, anthropic/claude-sonnet-4-6",
    )
    p.add_argument("--max-tokens", type=int, default=None, help="LLM token budget cap")
    p.add_argument(
        "--llm-exposure",
        default="metadata",
        choices=["none", "metadata", "sample"],
        help="what the LLM may see (default: metadata — never raw cells)",
    )
    p.add_argument("--mode", default="review", choices=["review", "auto", "strict"])
    p.set_defaults(func=_cmd_clean)

    p = sub.add_parser("apply", help="replay a saved recipe (no LLM)")
    p.add_argument("file")
    p.add_argument("--recipe", required=True, help="recipe YAML to replay")
    p.add_argument("--out", help="cleaned data output (default: <file>.clean.csv)")
    p.add_argument("--report", help="write an HTML diff report here")
    p.add_argument("--mode", default="review", choices=["review", "auto", "strict"])
    p.add_argument("--no-drift-check", action="store_true", help="skip schema-drift check")
    p.add_argument(
        "--force",
        action="store_true",
        help="apply even when schema drift is detected (default: stop)",
    )
    p.set_defaults(func=_cmd_apply)

    p = sub.add_parser("suggest", help="detect drift and optionally patch the recipe")
    p.add_argument("file")
    p.add_argument("--recipe", required=True, help="recipe YAML to check")
    p.add_argument("--update", action="store_true", help="write a patched recipe")
    p.add_argument("--out", help="where to write the patched recipe (default: in place)")
    p.set_defaults(func=_cmd_suggest)

    p = sub.add_parser("infer-schema", help="draft a target schema from a file")
    p.add_argument("file")
    p.add_argument("--out", help="schema output (default: <file>.schema.yaml)")
    p.add_argument("--name", help="schema name")
    p.set_defaults(func=_cmd_infer_schema)

    sub.add_parser("detectors", help="list available detectors").set_defaults(func=_cmd_detectors)
    sub.add_parser("ops", help="list available ops").set_defaults(func=_cmd_ops)

    return parser


def main(argv: list[str] | None = None) -> int:
    _reconfigure_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except CleanFrameError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:  # pragma: no cover
        print("Interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
