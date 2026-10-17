"""``python -m bench.cleanbench {validate,run,leaderboard,export,hash}`` (from the repo root)."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from .dataset import SPLITS, DatasetError, file_sha256, load_split, split_path
from .export import export_items, write_jsonl
from .harness import run_benchmark, validate
from .leaderboard import write_leaderboard

BENCH_DIR = Path(__file__).resolve().parent.parent
RESULTS_DIR = BENCH_DIR / "results"
LEADERBOARD = BENCH_DIR / "LEADERBOARD.md"


def _slug(spec: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", spec.strip()).strip("-").lower() or "system"


def _say(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def cmd_validate(args: argparse.Namespace) -> int:
    scenarios = load_split(args.split)
    check = validate(scenarios)
    if args.verbose:
        for sc in scenarios:
            run = check.runs.get(sc.id)
            print(f"\n{sc.id}  [{sc.family}, {sc.difficulty}, {sc.domain}]")
            if run is None:
                continue
            for r in run.records:
                where = f"{r['column']}: " if r["column"] else ""
                print(f"  {r['kind']:<15} {where}{r['subject']!s:<32} gold={r['gold']}")
            outcome = check.ceiling.get(sc.id)
            if outcome:
                print(f"  perfect answers fix {outcome['cells_fixed']} cell(s)")
    counts = check.counts()
    print(f"\n{counts['scenarios']} scenarios, {counts['questions']} questions "
          f"(oracle pass), {counts['cells_fixable']} cells fixable")
    print("scenarios by family:     " + ", ".join(f"{k} {v}" for k, v in counts["scenarios_by_family"].items()))
    print("scenarios by difficulty: " + ", ".join(f"{k} {v}" for k, v in counts["scenarios_by_difficulty"].items()))
    print("questions by kind:       " + ", ".join(f"{k} {v}" for k, v in counts["questions_by_kind"].items()))
    print("  ...whose answer changes data: "
          + ", ".join(f"{k} {v}" for k, v in counts["gold_changes_data_by_kind"].items()))
    print("questions by difficulty: " + ", ".join(f"{k} {v}" for k, v in counts["questions_by_difficulty"].items()))
    if check.problems:
        print(f"\n{len(check.problems)} problem(s):", file=sys.stderr)
        for p in check.problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print("\nOK: every scenario asks its questions and every question has gold.")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    if (args.price_in is None) != (args.price_out is None):
        _say("--price-in and --price-out go together (USD per million tokens)")
        return 2
    results, records = run_benchmark(
        args.system, split=args.split, max_scenarios=args.max_scenarios,
        price_in=args.price_in, price_out=args.price_out,
        progress=None if args.quiet else _say,
    )
    out = Path(args.out) if args.out else RESULTS_DIR / f"{_slug(args.system)}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    public = args.split in SPLITS
    log = out.with_name(out.stem + ".questions.jsonl")
    if public or args.log:
        with open(log, "w", encoding="utf-8", newline="\n") as fh:
            for record in records:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        results["log"] = log.name
    else:
        results["log"] = None
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    o, e = results["metrics"]["overall"], results["end_to_end"]
    prec = o["safe_apply_precision"]
    print(
        f"{args.system}: accuracy {o['accuracy']:.3f}, safe-apply precision "
        f"{'n/a' if prec is None else f'{prec:.3f}'} ({o['applied'] - o['applied_wrong']}/{o['applied']}), "
        f"coverage {o['coverage']:.3f}; cells fixed {e['cells_fixed']}, corrupted "
        f"{e['cells_corrupted']} (of {e['cells_fixable']} fixable)"
    )
    print(f"wrote {out}" + (f" and {log.name}" if results["log"] else ""))
    if not public and not args.log:
        print("held-out run: no per-question log written (it would reveal the items)")
    if not results["complete"]:
        print(f"INCOMPLETE: {len(results['errors'])} scenario(s) failed - see 'errors' in the JSON",
              file=sys.stderr)
        return 1
    return 0


def cmd_leaderboard(args: argparse.Namespace) -> int:
    paths = [Path(p) for p in args.files] or sorted(RESULTS_DIR.rglob("*.json"))
    write_leaderboard(paths, Path(args.out))
    print(f"wrote {args.out} from {len(paths)} results file(s)")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    n = write_jsonl(export_items(load_split(args.split)), args.out)
    print(f"wrote {n} requests to {args.out}")
    return 0


def cmd_hash(args: argparse.Namespace) -> int:
    path = split_path(args.file)
    print(f"{file_sha256(path)}  {path.name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        # Scenario values include Devanagari and accented text; a cp1252 console
        # must not crash on them.
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(prog="python -m bench.cleanbench", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("validate", help="check the dataset with an offline oracle pass")
    p.add_argument("--split", default="public", help="'public' or a path to a split YAML")
    p.add_argument("--verbose", "-v", action="store_true", help="print every question and its gold")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("run", help="run a system and write a results JSON + per-question log")
    p.add_argument("--system", required=True, help="a decide= spec, e.g. typesafe/jev-latest or rules")
    p.add_argument("--split", default="public")
    p.add_argument("--out", help="results JSON path (default bench/results/<system>.json)")
    p.add_argument("--max-scenarios", type=int, help="run only the first N scenarios (a partial run)")
    p.add_argument("--price-in", type=float, help="USD per million input tokens (if the backend reports no cost)")
    p.add_argument("--price-out", type=float, help="USD per million output tokens")
    p.add_argument("--log", action="store_true", help="also write the per-question log for a non-public split")
    p.add_argument("--quiet", "-q", action="store_true")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("leaderboard", help="render bench/LEADERBOARD.md from results files")
    p.add_argument("files", nargs="*", help="results JSON files (default: every JSON under bench/results/)")
    p.add_argument("--out", default=str(LEADERBOARD))
    p.set_defaults(func=cmd_leaderboard)

    p = sub.add_parser("export", help="write standalone System One requests with gold (JSONL)")
    p.add_argument("--split", default="public")
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("hash", help="SHA-256 of a split file with line endings normalised")
    p.add_argument("file")
    p.set_defaults(func=cmd_hash)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except DatasetError as exc:
        print(f"dataset error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
