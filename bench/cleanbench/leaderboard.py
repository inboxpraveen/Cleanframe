"""Render ``bench/LEADERBOARD.md`` from results files. Never invents a number."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .dataset import ALIAS, DATA_DIR, FAMILIES, MERGE, NULL, SCHEMA, file_sha256, split_path

HELDOUT_HASH_FILE = DATA_DIR / "HELDOUT.sha256"
_FAMILY_NAMES = {ALIAS: "alias", MERGE: "typo merge", NULL: "null token", SCHEMA: "schema"}

#: Systems the leaderboard invites, with the command that fills their row.
PENDING = [
    ("TypeSafe Jev (direct)", "TYPESAFE_API_KEY", "typesafe/jev-latest"),
    ("TypeSafe Jev via OpenRouter", "OPENROUTER_API_KEY", "openrouter/typesafe/jev-1.14"),
    ("Claude (LLM adapter)", "ANTHROPIC_API_KEY", "anthropic/claude-sonnet-4-6"),
    ("OpenAI (LLM adapter)", "OPENAI_API_KEY", "openai/gpt-4o-mini"),
    ("Open System One model, self-hosted", "SYSTEMONE_BASE_URL", "systemone/<model>"),
]


def load_results(paths: list[Path]) -> list[dict[str, Any]]:
    out = []
    for path in paths:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if data.get("benchmark") != "CleanBench":
            continue
        data["_file"] = Path(path).name
        out.append(data)
    return out


def heldout_hash() -> str | None:
    if not HELDOUT_HASH_FILE.is_file():
        return None
    for line in HELDOUT_HASH_FILE.read_text(encoding="utf-8").splitlines():
        token = line.strip().split(" ")[0] if line.strip() else ""
        if len(token) == 64 and all(c in "0123456789abcdef" for c in token):
            return token
    return None


# -- formatting -----------------------------------------------------------------
def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"


def _num(x: float | None, digits: int = 3) -> str:
    return "n/a" if x is None else f"{x:.{digits}f}"


def _ci(ci: list[float] | None) -> str:
    return "" if not ci else f" ({ci[0] * 100:.1f}–{ci[1] * 100:.1f})"


def _ms(x: float | None) -> str:
    if x is None:
        return "n/a"
    if x >= 1000:
        return f"{x / 1000:.2f} s"
    if x >= 10:
        return f"{x:.0f} ms"
    return f"{x:.2f} ms"


def _cost(op: dict[str, Any]) -> str:
    value = op.get("cost_per_1k_decisions_usd")
    if op.get("cost_source") == "offline":
        return "$0 (offline)"
    if value is None:
        return "n/a"
    suffix = " (list price)" if op.get("cost_source") == "price" else ""
    return f"${value:.4f}{suffix}"


def _precision(m: dict[str, Any]) -> str:
    applied, wrong = m.get("applied", 0), m.get("applied_wrong", 0)
    if not applied:
        return "n/a (0 applied)"
    return f"{_pct(m['safe_apply_precision'])} ({applied - wrong}/{applied})"


def _cells(e: dict[str, Any]) -> str:
    return f"{e['cells_fixed']} / {e['cells_corrupted']} (of {e['cells_fixable']})"


def _name(r: dict[str, Any]) -> str:
    return f"`{r['system']['spec']}`"


def _sort_key(r: dict[str, Any]) -> tuple:
    o = r["metrics"]["overall"]
    p = o.get("safe_apply_precision")
    return (p is None, -(p or 0.0), -(o.get("accuracy") or 0.0), r["system"]["spec"])


_COLUMNS = [
    "System", "Model reported", "Calibrated?", "Accuracy (95% CI)", "Safe-apply precision",
    "Coverage@0.9", "ECE", "Brier", "Cells fixed / corrupted", "p50 latency", "$ / 1k decisions",
]


def _row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def _main_table(rows: list[dict[str, Any]], ranked: bool) -> list[str]:
    head = (["#"] if ranked else []) + _COLUMNS
    lines = [_row(head), _row(["---"] * len(head))]
    for i, r in enumerate(rows, 1):
        o, e, op = r["metrics"]["overall"], r["end_to_end"], r["operational"]
        cells = ([str(i)] if ranked else []) + [
            _name(r),
            f"`{r['system'].get('model_reported') or 'n/a'}`",
            "yes" if r["system"].get("calibrated") else "no",
            _pct(o["accuracy"]) + _ci((o.get("ci95") or {}).get("accuracy")),
            _precision(o),
            _pct(o["coverage"]),
            _num(o["ece"]),
            _num(o["brier"]),
            _cells(e),
            _ms(op.get("latency_ms_p50")),
            _cost(op),
        ]
        lines.append(_row(cells))
    return lines


def _family_table(rows: list[dict[str, Any]]) -> list[str]:
    head = ["System"]
    for f in FAMILIES:
        head += [f"{_FAMILY_NAMES[f]}: accuracy", f"{_FAMILY_NAMES[f]}: wrong / applied"]
    lines = [_row(head), _row(["---"] * len(head))]
    for r in rows:
        fams = r["metrics"]["families"]
        cells = [_name(r)]
        for f in FAMILIES:
            m = fams.get(f)
            cells += ["n/a", "n/a"] if m is None else [_pct(m["accuracy"]), f"{m['applied_wrong']} / {m['applied']}"]
        lines.append(_row(cells))
    return lines


def render(results: list[dict[str, Any]]) -> str:
    public_hash = file_sha256(split_path("public"))
    held = heldout_hash()
    current = [r for r in results if r["dataset"]["sha256"] == public_hash]
    ranked = sorted(
        (r for r in current if r["system"]["type"] != "reference" and r.get("complete")), key=_sort_key
    )
    references = sorted(
        (r for r in current if r["system"]["type"] == "reference" and r.get("complete")),
        key=lambda r: r["system"]["spec"],
    )
    heldout = sorted((r for r in results if held and r["dataset"]["sha256"] == held), key=_sort_key)
    other = [r for r in results if r not in ranked and r not in references and r not in heldout]

    sample = (ranked or references or current or [None])[0]
    lines = [
        "# CleanBench leaderboard",
        "",
        "Data-cleaning decisions by decision models, scored inside CleanFrame's real pipeline.",
        "What the columns mean and how to add a row: [bench/README.md](README.md).",
        "",
    ]
    if sample is not None:
        n_q = sample["dataset"].get("questions_intended") or sample["metrics"]["overall"]["questions"]
        floor = sample["metrics"]["overall"].get("majority_floor")
        lines += [
            f"Public split: {sample['dataset']['scenarios_total']} scenarios, {n_q} questions "
            f"(dataset `sha256:{public_hash[:12]}`, CleanFrame {sample['environment']['cleanframe']}). "
            "Sorted by safe-apply precision, then accuracy. Always giving each family's most "
            f"common gold answer would score {_pct(floor)} accuracy (the majority floor).",
            "",
        ]
    lines += ["## Public split", ""]
    if ranked:
        lines += _main_table(ranked, ranked=True)
    else:
        lines.append("_No system has been run on the current dataset yet._")
    lines += [
        "",
        "* **Accuracy**: the answer is in the gold set; macro-average of the four question "
        "families; an unanswered question counts as wrong. 95% CI resamples whole scenarios.",
        "* **Safe-apply precision**: of the answers CleanFrame would act on to change data "
        "(p ≥ 0.9; a typo merge goes ahead unless vetoed), the share that were right - "
        "with (right/applied). Read it next to coverage: applying nothing is not a score.",
        "* **Coverage@0.9**: of the questions whose right answer changes data, the share "
        "changed correctly (macro over families).",
        "* **Cells fixed / corrupted (of possible)**: cells the cleaned output changed to a "
        "gold-correct value / to a wrong one, in the columns and schema fields the system "
        "was asked about. *Possible* is what perfect answers fix.",
        "* Label-only or uncalibrated systems are scored on the probabilities they report, as given.",
        "",
    ]
    full = [r for r in ranked + references if r["dataset"]["scenarios_run"] == r["dataset"]["scenarios_total"]]
    if full:
        e = full[0]["end_to_end"]
        ref = e["reference_no_decision_model"]
        lines += [
            f"For comparison, CleanFrame **without** a decision model (`decide=None`) fixes "
            f"{ref['cells_fixed']} and corrupts {ref['cells_corrupted']} of the same cells "
            f"(perfect answers fix {e['cells_fixable']} and corrupt 0).",
            "",
        ]
    if references:
        lines += ["### Reference points (not ranked)", ""]
        lines += _main_table(references, ranked=False)
        lines += [
            "",
            "`cleanbench/oracle` answers from the gold (the ceiling); `cleanbench/never-change` "
            "always keeps values apart, keeps tokens and maps nothing (the floor - its "
            "precision is undefined because it never changes data).",
            "",
        ]
    rows_for_families = ranked + references
    if rows_for_families:
        lines += ["### By question family", ""]
        lines += _family_table(rows_for_families)
        lines += [""]
    lines += [
        "### Pending: run with your API key",
        "",
        "No numbers are shown for systems nobody has run. Each row below is one command "
        "(see [bench/README.md](README.md#running-a-system)); open a PR with the results JSON.",
        "",
        "| System | Key | Command |",
        "|---|---|---|",
    ]
    done = {r["system"]["spec"] for r in ranked}
    for label, env, spec in PENDING:
        if spec in done:
            continue
        lines.append(f"| {label} | `{env}` | `python -m bench.cleanbench run --system {spec}` |")
    lines += [""]
    lines += ["## Held-out split", ""]
    if heldout:
        lines += _main_table(heldout, ranked=True)
    else:
        lines.append(
            "_Run by the maintainers on submitted systems; the held-out file is never "
            "published (see [bench/README.md](README.md#held-out-split))._"
        )
    lines += [""]
    if other:
        lines += ["## Not ranked", ""]
        for r in other:
            why = (
                "dataset changed since this run - re-run it"
                if r["dataset"]["sha256"] not in (public_hash, held)
                else "partial run" if not r.get("complete") else "reference"
            )
            lines.append(f"* `{r.get('_file', 'results')}` ({r['system']['spec']}): {why}")
        lines += [""]
    return "\n".join(lines)


def write_leaderboard(paths: list[Path], out: Path) -> str:
    text = render(load_results(paths))
    Path(out).write_text(text, encoding="utf-8", newline="\n")
    return text
