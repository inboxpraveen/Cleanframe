"""CleanBench metrics: pure functions over per-question records.

Every number in a results file is computed here from the per-question log, so anyone
can recompute a leaderboard row from the published JSONL.

Gating mirrors :mod:`cleanframe.decisions` in its default configuration (review
mode, ``decision_threshold=0.9``):

* ``category_alias`` changes data when the answer is not ``__distinct__`` and
  ``Answer.decision()`` gives ``p >= 0.9``;
* ``null_token`` changes data when ``p_yes >= 0.9``;
* ``schema_match`` changes data when the answer is not ``__none__`` and ``p >= 0.9``;
* ``category_merge`` asks about a typo merge the rules already made. In review mode
  that merge goes ahead unless the model vetoes it (``p_yes < 0.5``), so a "yes" at
  ``p_yes >= 0.5`` is the answer that changes data.
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from cleanframe.decisions import DISTINCT, NO_MATCH, NOUL, Answer

from .dataset import ALIAS, FAMILIES, MERGE, NULL, SCHEMA

#: CleanFrame's default bar for an answer that changes data.
THRESHOLD = 0.9
#: A rules typo merge survives unless the model says "no" with more than this.
MERGE_GATE = 0.5
ECE_BINS = 10
BOOTSTRAP_SAMPLES = 1000
BOOTSTRAP_SEED = 20261017


def changes_data(kind: str, label: str | None) -> bool:
    """True when ``label`` is the answer that would change the data for ``kind``."""
    if label is None:
        return False
    if kind == ALIAS:
        return label != DISTINCT
    if kind == SCHEMA:
        return label != NO_MATCH
    return label == "yes"  # null_token, category_merge


def would_apply(kind: str, answer: Answer | None, *, threshold: float = THRESHOLD,
                merge_gate: float = MERGE_GATE) -> bool:
    """Would CleanFrame act on ``answer`` and change data (review mode)?"""
    if answer is None:
        return False
    if kind == MERGE:
        return answer.p_yes >= merge_gate
    if kind == NULL:
        return answer.p_yes >= threshold
    label, p = answer.decision()
    return changes_data(kind, label) and p >= threshold


def score_question(kind: str, labels: Sequence[str], gold: Sequence[str], answer: Answer | None,
                   *, threshold: float = THRESHOLD, merge_gate: float = MERGE_GATE) -> dict[str, Any]:
    """The scored fields of one question. An unanswered question is wrong and not applied."""
    gold_set = set(gold)
    gold_changes = any(changes_data(kind, g) for g in gold)
    out: dict[str, Any] = {
        "answered": answer is not None,
        "label": None,
        "p": None,
        "p_yes": None,
        "probabilities": None,
        "p_gold": None,
        "correct": False,
        "gold_changes_data": gold_changes,
        "applied": False,
        "applied_correct": False,
    }
    if answer is None:
        return out
    label, p = answer.decision()
    probs = {lbl: float(answer.probabilities.get(lbl, 0.0)) for lbl in labels}
    p_gold = sum(probs[g] for g in gold_set if g in probs)
    applied = would_apply(kind, answer, threshold=threshold, merge_gate=merge_gate)
    out.update(
        label=label,
        p=_round(p),
        p_yes=_round(answer.p_yes) if answer.type == NOUL else None,
        probabilities={k: _round(v) for k, v in probs.items()},
        p_gold=_round(min(1.0, p_gold)),
        correct=label in gold_set,
        applied=applied,
        applied_correct=applied and label in gold_set,
    )
    return out


def _round(x: float) -> float:
    return round(float(x), 6)


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------
def ece(pairs: Iterable[tuple[float, bool]], bins: int = ECE_BINS) -> float | None:
    """Expected calibration error over ``(confidence, correct)`` pairs.

    ``bins`` equal-width bins on ``[0, 1]`` (a confidence of exactly 1.0 falls in the
    top bin); each bin contributes ``|accuracy - mean confidence|`` weighted by its
    share of the pairs. Empty bins contribute nothing. ``None`` with no pairs.
    """
    totals = [[0, 0.0, 0] for _ in range(bins)]  # n, sum confidence, n correct
    n = 0
    for conf, correct in pairs:
        b = min(bins - 1, max(0, int(conf * bins)))
        totals[b][0] += 1
        totals[b][1] += conf
        totals[b][2] += int(bool(correct))
        n += 1
    if not n:
        return None
    return sum(abs(c / k - s / k) * k / n for k, s, c in totals if k)


def brier(p_gold: Iterable[float]) -> float | None:
    """Mean squared shortfall of the probability mass on the gold set: ``(1 - q)^2``."""
    values = [(1.0 - q) ** 2 for q in p_gold]
    return sum(values) / len(values) if values else None


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------
def _ratio(num: int, den: int) -> float | None:
    return num / den if den else None


def _mean(values: Iterable[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def majority_floor(records: Sequence[Mapping[str, Any]]) -> float | None:
    """Accuracy of the best constant answer (JevBench's majority-class floor)."""
    if not records:
        return None
    candidates = {lbl for r in records for lbl in r["gold"]}
    return max(sum(1 for r in records if c in r["gold"]) for c in candidates) / len(records)


def group_metrics(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Accuracy, safe-apply precision, coverage and calibration for a set of questions."""
    answered = [r for r in records if r["answered"]]
    applied = [r for r in records if r["applied"]]
    good = sum(1 for r in applied if r["applied_correct"])
    changing = [r for r in records if r["gold_changes_data"]]
    return {
        "questions": len(records),
        "answered": len(answered),
        "accuracy": _ratio(sum(1 for r in records if r["correct"]), len(records)),
        "applied": len(applied),
        "applied_wrong": len(applied) - good,
        "safe_apply_precision": _ratio(good, len(applied)),
        "gold_changes_data": len(changing),
        "coverage": _ratio(sum(1 for r in changing if r["applied_correct"]), len(changing)),
        "ece": ece((r["p"], r["correct"]) for r in answered),
        "brier": brier(r["p_gold"] for r in answered),
        "majority_floor": majority_floor(records),
    }


def summarize(records: Sequence[Mapping[str, Any]], *, bootstrap: int = BOOTSTRAP_SAMPLES,
              seed: int = BOOTSTRAP_SEED) -> dict[str, Any]:
    """Overall, per-family and per-difficulty metrics.

    Accuracy and coverage are *macro* averages over the four question families (each
    family counts the same, so the many "is this value distinct?" alias questions do
    not drown the rest). Safe-apply precision, ECE and Brier pool every question: a
    wrong change is a wrong change, whichever family it came from.
    """
    by_family = {f: [r for r in records if r["kind"] == f] for f in FAMILIES}
    families = {f: group_metrics(rs) for f, rs in by_family.items() if rs}
    pooled = group_metrics(records)
    overall = {
        "questions": pooled["questions"],
        "answered": pooled["answered"],
        "answer_rate": _ratio(pooled["answered"], pooled["questions"]),
        "accuracy": _mean(m["accuracy"] for m in families.values()),
        "accuracy_micro": pooled["accuracy"],
        "safe_apply_precision": pooled["safe_apply_precision"],
        "applied": pooled["applied"],
        "applied_wrong": pooled["applied_wrong"],
        "coverage": _mean(m["coverage"] for m in families.values()),
        "coverage_micro": pooled["coverage"],
        "ece": pooled["ece"],
        "brier": pooled["brier"],
        "majority_floor": _mean(m["majority_floor"] for m in families.values()),
    }
    if bootstrap:
        overall["ci95"] = bootstrap_ci(records, samples=bootstrap, seed=seed)
    difficulties = sorted({r["difficulty"] for r in records})
    return {
        "overall": overall,
        "families": families,
        "difficulty": {
            d: {
                k: v for k, v in group_metrics([r for r in records if r["difficulty"] == d]).items()
                if k in ("questions", "accuracy", "applied", "applied_wrong", "safe_apply_precision", "coverage")
            }
            for d in difficulties
        },
    }


def bootstrap_ci(records: Sequence[Mapping[str, Any]], *, samples: int = BOOTSTRAP_SAMPLES,
                 seed: int = BOOTSTRAP_SEED) -> dict[str, list[float] | None]:
    """95% intervals for macro accuracy and pooled safe-apply precision.

    Whole scenarios are resampled with replacement (their questions are not
    independent), with a fixed seed so the interval is reproducible.
    """
    scenarios = sorted({r["scenario"] for r in records})
    if not scenarios:
        return {"accuracy": None, "safe_apply_precision": None}
    # Per scenario, per family: (questions, correct, applied, applied correct).
    tallies: dict[str, dict[str, list[int]]] = {s: {} for s in scenarios}
    for r in records:
        t = tallies[r["scenario"]].setdefault(r["kind"], [0, 0, 0, 0])
        t[0] += 1
        t[1] += int(r["correct"])
        t[2] += int(r["applied"])
        t[3] += int(r["applied_correct"])
    rng = random.Random(seed)
    accs: list[float] = []
    precs: list[float] = []
    for _ in range(samples):
        fam: dict[str, list[int]] = {}
        for s in (rng.choice(scenarios) for _ in scenarios):
            for kind, t in tallies[s].items():
                f = fam.setdefault(kind, [0, 0, 0, 0])
                for i in range(4):
                    f[i] += t[i]
        family_acc = [f[1] / f[0] for f in fam.values() if f[0]]
        if family_acc:
            accs.append(sum(family_acc) / len(family_acc))
        applied = sum(f[2] for f in fam.values())
        if applied:
            precs.append(sum(f[3] for f in fam.values()) / applied)
    return {"accuracy": _interval(accs), "safe_apply_precision": _interval(precs)}


def _interval(values: list[float]) -> list[float] | None:
    if not values:
        return None
    values = sorted(values)
    return [round(percentile(values, 2.5), 4), round(percentile(values, 97.5), 4)]


def percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile of ``values`` (``0 < q <= 100``)."""
    if not values:
        raise ValueError("percentile of no values")
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[rank - 1]


def operational(latencies_s: Sequence[float], input_tokens: Sequence[int],
                output_tokens: Sequence[int], costs: Sequence[float | None], questions: int,
                *, price_in: float | None = None, price_out: float | None = None,
                offline: bool = False) -> dict[str, Any]:
    """Requests, latency, tokens and cost. A measurement nobody reported is ``None``, never 0.

    Token counts are ``None`` when every response reported 0 input tokens (no real
    request costs zero input tokens, so 0 means "not reported"). Cost comes from the
    backend's own ``usage.cost`` when every response reported it, else from
    ``price_in``/``price_out`` (USD per million tokens) when tokens are known; an
    offline system costs 0.
    """
    requests = len(latencies_s)
    tin = sum(input_tokens) if any(input_tokens) else None
    tout = sum(output_tokens) if tin is not None else None
    cost: float | None = None
    source: str | None = None
    if offline:
        cost, source = 0.0, "offline"
    elif requests and all(c is not None for c in costs):
        cost, source = float(sum(c for c in costs if c is not None)), "backend"
    elif tin is not None and price_in is not None and price_out is not None:
        cost, source = (tin * price_in + (tout or 0) * price_out) / 1_000_000, "price"
    ms = [s * 1000 for s in latencies_s]
    return {
        "requests": requests,
        "latency_ms_p50": round(percentile(ms, 50), 3) if ms else None,
        "latency_ms_p95": round(percentile(ms, 95), 3) if ms else None,
        "input_tokens": tin,
        "output_tokens": tout,
        "cost_usd": round(cost, 6) if cost is not None else None,
        "cost_source": source,
        "cost_per_1k_decisions_usd": round(cost / questions * 1000, 6) if cost is not None and questions else None,
    }


__all__ = [
    "MERGE_GATE", "THRESHOLD", "brier", "bootstrap_ci", "changes_data", "ece",
    "group_metrics", "majority_floor", "operational", "percentile", "score_question",
    "summarize", "would_apply",
]
