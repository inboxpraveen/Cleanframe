"""Run a decision backend through CleanFrame's real pipeline and score what it decided.

Each scenario goes through one production call::

    cf.clean(df, decide=<recorder around the system>, llm_exposure="sample",
             target_schema=..., decision_threshold=0.9, llm_fallback=False)

The recorder sees every question CleanFrame asks and every answer the system gives,
so the questions scored are exactly the ones production would ask, and the cleaned
frame from the same call gives the end-to-end count of cells fixed and corrupted.
One call per scenario also means a paid system is billed once, and the question-level
and end-to-end numbers always come from the same answers.
"""

from __future__ import annotations

import datetime as _dt
import platform
import time
import warnings
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

import cleanframe as cf
from cleanframe._util import normalize_key
from cleanframe.decisions import (
    CHOICE,
    DISTINCT,
    NO_MATCH,
    NOUL,
    Answer,
    DecisionError,
    DecisionResponse,
    LLMDecisionClient,
    Question,
    RulesDecisionClient,
    SystemOneClient,
    get_decision_client,
)
from cleanframe.detectors.categories import _skeleton

from . import metrics
from .dataset import (
    ALIAS,
    DIFFICULTIES,
    EXPECT_KEYS,
    FAMILIES,
    MERGE,
    NULL,
    SCHEMA,
    SPLITS,
    DatasetError,
    Scenario,
    file_sha256,
    key,
    load_split,
    split_path,
    subject,
)

BENCHMARK = "CleanBench"
FORMAT_VERSION = 1
ORACLE = "cleanbench/oracle"
NEVER_CHANGE = "cleanbench/never-change"
#: How every scenario is cleaned: CleanFrame's defaults plus the opt-in that lets
#: category questions be asked at all.
SETTINGS = {
    "mode": "review",
    "llm_exposure": "sample",
    "decision_threshold": metrics.THRESHOLD,
    "merge_gate": metrics.MERGE_GATE,
    "max_decisions": 200,
    "llm_fallback": False,
}


# ---------------------------------------------------------------------------
# Recording and reference systems
# ---------------------------------------------------------------------------
@dataclass
class RequestRecord:
    state: Any
    questions: dict[str, Question]
    response: DecisionResponse | None
    seconds: float
    error: str | None = None


class Recorder:
    """Wraps a decision client and records each request, answer and latency.

    A request that raises is retried ``retries`` more times (on top of the
    backend's own HTTP retries) before the error reaches CleanFrame; the latency
    recorded is that of the attempt that answered.
    """

    def __init__(self, inner: Any, *, retries: int = 0) -> None:
        self.inner = inner
        self.model = str(getattr(inner, "model", "custom"))
        self.calibrated = bool(getattr(inner, "calibrated", False))
        self.retries = max(0, int(retries))
        self.failed_attempts = 0
        self.requests: list[RequestRecord] = []

    def decide(self, state: Any, questions: dict[str, Question]) -> DecisionResponse:
        attempt = 0
        while True:
            start = time.perf_counter()
            try:
                response = self.inner.decide(state, questions)
            except Exception as exc:
                if attempt < self.retries:
                    attempt += 1
                    self.failed_attempts += 1
                    continue
                self.requests.append(
                    RequestRecord(state, dict(questions), None, time.perf_counter() - start, str(exc))
                )
                raise
            seconds = time.perf_counter() - start
            self.requests.append(RequestRecord(state, dict(questions), response, seconds))
            return response


def _one_hot(question: Question, label: str) -> Answer:
    if question.type == NOUL:
        p = 1.0 if label == "yes" else 0.0
        return Answer(NOUL, {"yes": p, "no": 1.0 - p})
    probs = {lbl: (1.0 if lbl == label else 0.0) for lbl in question.labels}
    return Answer(CHOICE, probs, choice=label, confidence=1.0)


class OracleClient:
    """Answers every question from the gold with certainty: the ceiling, and the
    client ``validate`` and ``export`` use to produce the intended question set."""

    model = "cleanbench-oracle"
    calibrated = True

    def __init__(self, scenario: Scenario) -> None:
        self.scenario = scenario

    def decide(self, state: Any, questions: dict[str, Question]) -> DecisionResponse:
        answers = {
            name: _one_hot(q, self.scenario.gold(q.context)[0]) for name, q in questions.items()
        }
        return DecisionResponse(answers, model=self.model)


class NeverChangeClient:
    """Always gives the answer that changes nothing (distinct / no / none): the floor.

    It vetoes every rules typo merge too, so it also undoes the rules' fixes.
    """

    model = "cleanbench-never-change"
    calibrated = False

    def decide(self, state: Any, questions: dict[str, Question]) -> DecisionResponse:
        answers = {}
        for name, q in questions.items():
            kind = q.context.get("kind")
            label = DISTINCT if kind == ALIAS else NO_MATCH if kind == SCHEMA else "no"
            answers[name] = _one_hot(q, label)
        return DecisionResponse(answers, model=self.model)


@dataclass
class System:
    spec: str
    #: builds the client for one scenario (reference systems need the scenario's gold)
    factory: Callable[[Scenario], Any]
    type: str
    offline: bool


def resolve_system(spec: Any) -> System:
    """``cleanbench/oracle``, ``cleanbench/never-change``, any ``decide=`` spec, or a
    client object with ``.decide()`` (your own backend)."""
    if not isinstance(spec, str):
        client = get_decision_client(spec)
        name = f"custom:{getattr(client, 'model', type(client).__name__)}"
        return System(name, lambda _s: client, "custom", False)
    text = spec.strip()
    if text == ORACLE:
        return System(text, OracleClient, "reference", True)
    if text == NEVER_CHANGE:
        return System(text, lambda _s: NeverChangeClient(), "reference", True)
    client = get_decision_client(text)
    if isinstance(client, RulesDecisionClient):
        kind, offline = "rules", True
    elif isinstance(client, SystemOneClient):
        kind, offline = "decision-model", False
    elif isinstance(client, LLMDecisionClient):
        kind, offline = "llm", False
    else:
        kind, offline = "custom", False
    return System(text, lambda _s: client, kind, offline)


# ---------------------------------------------------------------------------
# One scenario
# ---------------------------------------------------------------------------
@dataclass
class ScenarioRun:
    scenario: Scenario
    frame: pd.DataFrame
    recorder: Recorder
    result: Any | None
    error: str | None = None
    records: list[dict[str, Any]] = field(default_factory=list)


def clean_scenario(scenario: Scenario, client: Any | None) -> Any:
    """``cf.clean`` exactly as the benchmark runs it (``client=None``: no decision model)."""
    kwargs: dict[str, Any] = {"target_schema": scenario.schema}
    if client is not None:
        kwargs.update(
            decide=client,
            llm_exposure=SETTINGS["llm_exposure"],
            decision_threshold=SETTINGS["decision_threshold"],
            max_decisions=SETTINGS["max_decisions"],
            llm_fallback=SETTINGS["llm_fallback"],
        )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return cf.clean(scenario.frame(), mode=SETTINGS["mode"], **kwargs)


def run_scenario(scenario: Scenario, client: Any, *, retries: int = 0) -> ScenarioRun:
    recorder = Recorder(client, retries=retries)
    try:
        result, error = clean_scenario(scenario, recorder), None
    except DecisionError as exc:
        result, error = None, str(exc)
    run = ScenarioRun(scenario, scenario.frame(), recorder, result, error)
    run.records = question_records(run)
    return run


def question_records(run: ScenarioRun) -> list[dict[str, Any]]:
    """One scored record per question asked. Raises :class:`DatasetError` on missing gold."""
    sc = run.scenario
    out: list[dict[str, Any]] = []
    for index, req in enumerate(run.recorder.requests):
        for name, question in req.questions.items():
            ctx = question.context
            kind = str(ctx.get("kind"))
            gold = sc.gold(ctx)
            answer = req.response.answers.get(name) if req.response is not None else None
            record: dict[str, Any] = {
                "scenario": sc.id,
                "family": sc.family,
                "difficulty": sc.difficulty,
                "kind": kind,
                "column": ctx.get("column"),
                "subject": subject(ctx),
                "request": index,
                "name": name,
                "labels": question.labels,
                "gold": gold,
            }
            record.update(metrics.score_question(kind, question.labels, gold, answer))
            out.append(record)
    return out


def scope(records: list[dict[str, Any]]) -> tuple[set[str], set[str]]:
    """The columns and schema targets the decision layer was asked about.

    End-to-end counts stay inside this scope, so a system is only charged for what
    its answers could change. The scope depends on the data, not on the answers.
    """
    columns = {str(r["column"]) for r in records if r["kind"] in (ALIAS, MERGE, NULL)}
    targets = {str(r["subject"]) for r in records if r["kind"] == SCHEMA}
    return columns, targets


def _missing(value: Any) -> bool:
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):  # list-like cells are values, not missing
        return False


def end_to_end(scenario: Scenario, frame: pd.DataFrame, result: Any,
               columns: set[str], targets: set[str]) -> dict[str, int]:
    """Cells changed to a gold-correct value ("fixed") and to a wrong one ("corrupted").

    Text columns, cell by cell against the raw value: turned into a missing value -
    fixed if the token is a placeholder here, corrupted otherwise; changed to another
    spelling of the same thing - fixed; changed to a different value of the column -
    corrupted; a change CleanFrame makes without asking (case, spaces, punctuation:
    ``NetBanking`` -> ``Net Banking``) is not counted; any other change (a parser's) is
    ``other``. Schema targets, per target: filled from the gold source column - all its
    cells fixed; from any other column - all of that column's cells corrupted.
    """
    fixed = corrupted = other = 0
    out = result.dataframe
    if columns and len(out) != len(frame):
        raise DatasetError(
            f"{scenario.id}: cleaning changed the row count ({len(frame)} -> {len(out)}), "
            "so cells cannot be compared - remove what makes rows drop or quarantine"
        )
    for column in sorted(columns):
        gold = scenario.columns[column]
        recipe_col = result.recipe.column(column)
        out_name = recipe_col.output_name if recipe_col is not None else column
        if out_name not in out.columns:
            raise DatasetError(f"{scenario.id}: column {column!r} is missing from the cleaned frame")
        for before, after in zip(frame[column].tolist(), out[out_name].tolist(), strict=True):
            if _missing(before):
                continue
            if _missing(after):
                if gold.is_placeholder(before):
                    fixed += 1
                else:
                    corrupted += 1
            elif _cosmetic(before, after):
                continue
            elif gold.known(after):
                if gold.same(before, after):
                    fixed += 1
                else:
                    corrupted += 1
            else:
                other += 1
    for target in sorted(targets):
        sources = [c.source for c in result.recipe.columns if c.output_name == target]
        source = sources[0] if sources else None
        if source is None:
            continue
        cells = int(frame[source].notna().sum())
        if source == scenario.schema_gold.get(target):
            fixed += cells
        else:
            corrupted += cells
    return {"cells_fixed": fixed, "cells_corrupted": corrupted, "cells_other": other}


def _cosmetic(before: Any, after: Any) -> bool:
    """Same spelling once case, spaces and punctuation are ignored (the rules' own merge)."""
    if key(before) == key(after):
        return True
    a, b = str(before), str(after)
    return bool(normalize_key(a)) and normalize_key(a) == normalize_key(b) and _skeleton(a) == _skeleton(b)


def _add(total: dict[str, int], part: Mapping[str, int]) -> None:
    for k, v in part.items():
        total[k] = total.get(k, 0) + v


# ---------------------------------------------------------------------------
# Validation (an oracle pass: free, offline, and the gate before any paid run)
# ---------------------------------------------------------------------------
@dataclass
class Validation:
    scenarios: list[Scenario]
    problems: list[str]
    runs: dict[str, ScenarioRun]
    #: per scenario: what a perfect decision model achieves end to end
    ceiling: dict[str, dict[str, int]]
    #: per scenario: what CleanFrame does with no decision model (``decide=None``)
    reference: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.problems

    def counts(self) -> dict[str, Any]:
        records = [r for run in self.runs.values() for r in run.records]
        return {
            "scenarios": len(self.scenarios),
            "questions": len(records),
            "scenarios_by_family": {f: sum(1 for s in self.scenarios if s.family == f) for f in FAMILIES},
            "scenarios_by_difficulty": {
                d: sum(1 for s in self.scenarios if s.difficulty == d) for d in DIFFICULTIES
            },
            "questions_by_kind": {f: sum(1 for r in records if r["kind"] == f) for f in FAMILIES},
            "questions_by_difficulty": {
                d: sum(1 for r in records if r["difficulty"] == d) for d in DIFFICULTIES
            },
            "gold_changes_data_by_kind": {
                f: sum(1 for r in records if r["kind"] == f and r["gold_changes_data"]) for f in FAMILIES
            },
            "cells_fixable": sum(c["cells_fixed"] for c in self.ceiling.values()),
        }


def validate(scenarios: list[Scenario]) -> Validation:
    """Run every scenario with the oracle and check the dataset does what it says.

    Checks: each scenario asks at least one question and one of its own family; every
    question has gold; the questions match ``expect``; and a perfect decision model
    corrupts no cell (anything else would charge every system for the rules' choices).
    """
    problems: list[str] = []
    runs: dict[str, ScenarioRun] = {}
    ceiling: dict[str, dict[str, int]] = {}
    reference: dict[str, dict[str, int]] = {}
    for sc in scenarios:
        try:
            run = run_scenario(sc, OracleClient(sc))
        except DatasetError as exc:
            problems.append(str(exc))
            continue
        runs[sc.id] = run
        if run.error:
            problems.append(f"{sc.id}: the oracle run failed: {run.error}")
            continue
        if not run.records:
            problems.append(f"{sc.id}: the pipeline asks no question about this scenario")
            continue
        kinds = {r["kind"] for r in run.records}
        if sc.family not in kinds:
            problems.append(f"{sc.id}: a {sc.family} scenario that asks no {sc.family} question")
        for name, kind in EXPECT_KEYS.items():
            got = sorted(r["subject"] for r in run.records if r["kind"] == kind)
            if name == "alias":
                if "alias" in sc.expect and len(got) != sc.expect["alias"]:
                    problems.append(f"{sc.id}: expected {sc.expect['alias']} alias questions, got {len(got)}")
            elif got != sc.expect.get(name, []):
                problems.append(f"{sc.id}: expected {name} questions {sc.expect.get(name, [])}, got {got}")
        try:
            columns, targets = scope(run.records)
            outcome = end_to_end(sc, run.frame, run.result, columns, targets)
        except DatasetError as exc:
            problems.append(str(exc))
            continue
        ceiling[sc.id] = outcome
        reference[sc.id] = end_to_end(sc, run.frame, clean_scenario(sc, None), columns, targets)
        if outcome["cells_corrupted"]:
            problems.append(
                f"{sc.id}: even perfect answers corrupt {outcome['cells_corrupted']} cell(s) - "
                "the rules change something no question covers"
            )
    return Validation(scenarios, problems, runs, ceiling, reference)


_VALIDATIONS: dict[str, Validation] = {}


def validation_for(split: str = "public") -> tuple[Path, list[Scenario], Validation]:
    """Load and validate a split once per process (keyed by the file's hash)."""
    path = split_path(split)
    scenarios = load_split(split)
    key = f"{path.resolve()}:{file_sha256(path)}"
    if key not in _VALIDATIONS:
        _VALIDATIONS[key] = validate(scenarios)
    return path, scenarios, _VALIDATIONS[key]


# ---------------------------------------------------------------------------
# A full run
# ---------------------------------------------------------------------------
#: Harness-level retries of a failed request (a malformed LLM reply, a dropped connection).
REQUEST_RETRIES = 2


def run_benchmark(
    spec: Any,
    *,
    split: str = "public",
    max_scenarios: int | None = None,
    price_in: float | None = None,
    price_out: float | None = None,
    progress: Callable[[str], None] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run ``spec`` over a split. Returns ``(results, per-question records)``.

    The dataset is validated first (offline), so a broken dataset never costs a
    paid run anything.
    """
    path, scenarios, check = validation_for(split)
    total = len(scenarios)
    if max_scenarios is not None:
        scenarios = scenarios[: max(0, int(max_scenarios))]
    if not check.ok:
        raise DatasetError("the dataset failed validation:\n  " + "\n  ".join(check.problems))
    system = resolve_system(spec)

    records: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    e2e: dict[str, int] = {"cells_fixed": 0, "cells_corrupted": 0, "cells_other": 0}
    reference: dict[str, int] = {"cells_fixed": 0, "cells_corrupted": 0, "cells_other": 0}
    e2e_by_kind: dict[str, dict[str, int]] = {}
    latencies: list[float] = []
    tokens_in: list[int] = []
    tokens_out: list[int] = []
    costs: list[float | None] = []
    retried = 0
    reported_model: str | None = None
    calibrated: bool | None = None
    for i, sc in enumerate(scenarios, 1):
        client = system.factory(sc)
        if calibrated is None:
            calibrated = bool(getattr(client, "calibrated", False))
        run = run_scenario(sc, client, retries=0 if system.offline else REQUEST_RETRIES)
        records.extend(run.records)
        retried += run.recorder.failed_attempts
        for req in run.recorder.requests:
            latencies.append(req.seconds)
            if req.response is not None:
                tokens_in.append(req.response.input_tokens)
                tokens_out.append(req.response.output_tokens)
                costs.append(req.response.cost)
                if reported_model is None and req.response.model:
                    reported_model = req.response.model
        # Scope comes from the oracle pass: it depends on the data, not on the answers.
        columns, targets = scope(check.runs[sc.id].records)
        if run.error:
            errors.append({"scenario": sc.id, "error": run.error})
        else:
            outcome = end_to_end(sc, run.frame, run.result, columns, targets)
            _add(e2e, outcome)
            _add(e2e_by_kind.setdefault(sc.family, {}), outcome)
            _add(reference, check.reference[sc.id])
        if progress:
            status = f"error: {run.error}" if run.error else f"{len(run.records)} question(s)"
            progress(f"[{i}/{len(scenarios)}] {sc.id}: {status}")

    questions = len(records)
    summary = metrics.summarize(records)
    results: dict[str, Any] = {
        "benchmark": BENCHMARK,
        "format": FORMAT_VERSION,
        "system": {
            "spec": system.spec,
            "type": system.type,
            "model_reported": reported_model,
            "calibrated": bool(calibrated),
        },
        "dataset": {
            "split": split if split in SPLITS else path.stem,
            "file": path.name,
            "sha256": file_sha256(path),
            "scenarios_run": len(scenarios),
            "scenarios_total": total,
            # The questions a perfect system is asked; a system that vetoes a correct
            # typo merge gets one more alias question per veto, so counts can differ.
            "questions_intended": sum(len(check.runs[s.id].records) for s in scenarios),
        },
        "settings": dict(SETTINGS),
        "environment": {
            "cleanframe": cf.__version__,
            "python": platform.python_version(),
            "pandas": pd.__version__,
        },
        "date": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "complete": not errors and len(scenarios) == total,
        "errors": errors,
        "metrics": summary,
        "end_to_end": {
            **e2e,
            "cells_fixable": sum(check.ceiling[s.id]["cells_fixed"] for s in scenarios),
            "by_family": dict(sorted(e2e_by_kind.items())),
            "reference_no_decision_model": reference,
        },
        "operational": {
            **metrics.operational(
                latencies, tokens_in, tokens_out, costs, questions,
                price_in=price_in, price_out=price_out, offline=system.offline,
            ),
            "retried_requests": retried,
        },
    }
    return results, records


__all__ = [
    "NEVER_CHANGE", "ORACLE", "SETTINGS", "NeverChangeClient", "OracleClient", "Recorder",
    "System", "Validation", "clean_scenario", "end_to_end", "question_records",
    "resolve_system", "run_benchmark", "run_scenario", "scope", "validate", "validation_for",
]
