"""CleanBench (bench/): dataset validity, determinism and metric arithmetic.

bench/ lives in the source checkout only (it is not in the wheel or the sdist), so
this module skips itself when it is absent.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if not (ROOT / "bench" / "cleanbench" / "harness.py").is_file():
    pytest.skip("bench/ is not part of this checkout", allow_module_level=True)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bench.cleanbench import metrics  # noqa: E402
from bench.cleanbench.__main__ import main as cli  # noqa: E402
from bench.cleanbench.dataset import (  # noqa: E402
    FAMILIES,
    DatasetError,
    file_sha256,
    parse_dataset,
)
from bench.cleanbench.export import export_items  # noqa: E402
from bench.cleanbench.harness import run_benchmark, validation_for  # noqa: E402
from bench.cleanbench.leaderboard import render  # noqa: E402
from cleanframe.decisions import (  # noqa: E402
    CHOICE,
    DISTINCT,
    NO_MATCH,
    NOUL,
    Answer,
    DecisionResponse,
)


@pytest.fixture(scope="module")
def checked():
    return validation_for("public")


@pytest.fixture(scope="module")
def scenarios(checked):
    return checked[1]


@pytest.fixture(scope="module")
def rules_run():
    return run_benchmark("rules")


# --------------------------------------------------------------------- dataset
def test_public_dataset_validates(checked):
    _path, scenarios, check = checked
    assert check.ok, check.problems
    counts = check.counts()
    assert counts["scenarios"] >= 45 and counts["questions"] >= 250
    for family in FAMILIES:
        assert counts["scenarios_by_family"][family] >= 10, family
        assert counts["questions_by_kind"][family] >= 10, family
        # every family has questions whose right answer changes data, and ones where it does not
        assert 0 < counts["gold_changes_data_by_kind"][family] < counts["questions_by_kind"][family]
    assert len({s.id for s in scenarios}) == len(scenarios)


def test_heldout_hash_is_committed_but_the_file_is_not():
    text = (ROOT / "bench" / "data" / "HELDOUT.sha256").read_text(encoding="utf-8")
    assert any(len(tok) == 64 for line in text.splitlines() for tok in line.split()[:1])
    assert not list((ROOT / "bench").rglob("heldout*.yaml"))


def _doc(**overrides):
    item = {
        "id": "s1", "family": "category_alias", "domain": "test", "difficulty": "easy",
        "rationale": "r", "column": "city",
        "values": {"Bangalore": 5, "BLR": 2, "Delhi": 4},
        "same": [["Bangalore", "BLR"]], "expect": {"alias": 2},
    }
    item.update(overrides)
    return {"scenarios": [item]}


def test_gold_is_looked_up_from_equivalence_classes():
    (sc,) = parse_dataset(_doc())
    alias = {"kind": "category_alias", "column": "city", "value": "BLR", "options": ["Bangalore", "Delhi"]}
    assert sc.gold(alias) == ["Bangalore"]
    assert sc.gold({**alias, "value": "Delhi", "options": ["Bangalore", "BLR"]}) == [DISTINCT]
    merge = {"kind": "category_merge", "column": "city", "variant": "blr", "canonical": "Bangalore"}
    assert sc.gold(merge) == ["yes"]  # case-insensitive lookup
    with pytest.raises(DatasetError, match="not one of the scenario's values"):
        sc.gold({**alias, "value": "Bengaluru"})


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"values": {"Yes": 3, True: 2}}, "not text"),
        ({"same": [["Bangalore", "Bengaluru"]]}, "not in the values"),
        # Punctuation-only spelling differences are merged by the rules with no question.
        ({"values": {"St. Louis": 3, "St Louis": 2, "Delhi": 2}, "same": []}, "without a question"),
        ({"expect": None}, "`expect` is required"),
        ({"difficulty": "medium"}, "difficulty"),
    ],
)
def test_dataset_problems_are_refused(overrides, message):
    with pytest.raises(DatasetError, match=message):
        parse_dataset(_doc(**overrides))


def test_split_hash_ignores_line_endings(tmp_path):
    a, b = tmp_path / "a.yaml", tmp_path / "b.yaml"
    a.write_bytes(b"x: 1\ny: 2\n")
    b.write_bytes(b"x: 1\r\ny: 2\r\n")
    assert file_sha256(a) == file_sha256(b)


# --------------------------------------------------------------------- metrics
def test_ece_and_brier_on_hand_built_cases():
    # Perfectly calibrated: in the 0.8 bin, 4 of 5 are right.
    assert metrics.ece([(0.8, True)] * 4 + [(0.8, False)]) == pytest.approx(0.0)
    # Overconfident: says 0.95, right half the time -> gap 0.45.
    assert metrics.ece([(0.95, True), (0.95, False)]) == pytest.approx(0.45)
    # Two bins weighted by size: |1 - 0.9| * 1/2 + |0 - 0.6| * 1/2.
    assert metrics.ece([(0.9, True), (0.6, False)]) == pytest.approx(0.35)
    assert metrics.ece([(1.0, True)]) == pytest.approx(0.0)  # 1.0 lands in the top bin
    assert metrics.ece([]) is None
    assert metrics.brier([1.0, 0.5, 0.0]) == pytest.approx((0 + 0.25 + 1) / 3)
    assert metrics.brier([]) is None


def _choice(labels, label, p, confidence=None):
    rest = (1 - p) / (len(labels) - 1)
    return Answer(CHOICE, {lbl: (p if lbl == label else rest) for lbl in labels}, choice=label, confidence=confidence)


def test_gating_mirrors_decisions_py():
    labels = ["Bangalore", "Delhi", DISTINCT]
    sure = _choice(labels, "Bangalore", 0.95)
    assert metrics.would_apply("category_alias", sure)
    # A choice gates on min(probability, confidence).
    assert not metrics.would_apply("category_alias", _choice(labels, "Bangalore", 0.95, confidence=0.7))
    assert not metrics.would_apply("category_alias", _choice(labels, DISTINCT, 0.99))
    assert not metrics.would_apply("schema_match", _choice(["a", NO_MATCH], NO_MATCH, 0.99))
    yes = Answer(NOUL, {"yes": 0.6, "no": 0.4})
    assert not metrics.would_apply("null_token", yes)  # converting a token needs 0.9
    assert metrics.would_apply("category_merge", yes)  # a rules merge survives unless vetoed
    assert not metrics.would_apply("category_merge", Answer(NOUL, {"yes": 0.4, "no": 0.6}))
    assert not metrics.would_apply("category_alias", None)


def test_score_question_and_safe_apply_precision():
    labels = ["Bangalore", "Delhi", DISTINCT]
    right = metrics.score_question("category_alias", labels, ["Bangalore"], _choice(labels, "Bangalore", 0.95))
    wrong = metrics.score_question("category_alias", labels, [DISTINCT], _choice(labels, "Delhi", 0.97))
    timid = metrics.score_question("category_alias", labels, ["Bangalore"], _choice(labels, "Bangalore", 0.6))
    missing = metrics.score_question("category_alias", labels, ["Bangalore"], None)
    assert right["correct"] and right["applied_correct"] and right["p_gold"] == pytest.approx(0.95)
    assert wrong["applied"] and not wrong["applied_correct"] and not wrong["gold_changes_data"]
    assert timid["correct"] and not timid["applied"]
    assert not missing["correct"] and not missing["answered"]
    records = [dict(r, kind="category_alias", gold=g, scenario=s, difficulty="easy")
               for r, g, s in ((right, ["Bangalore"], "a"), (wrong, [DISTINCT], "b"),
                               (timid, ["Bangalore"], "c"), (missing, ["Bangalore"], "d"))]
    m = metrics.group_metrics(records)
    assert m["accuracy"] == pytest.approx(2 / 4)  # unanswered counts as wrong
    assert m["safe_apply_precision"] == pytest.approx(1 / 2) and m["applied_wrong"] == 1
    assert m["coverage"] == pytest.approx(1 / 3)  # 3 questions should change data, 1 did
    nothing = metrics.group_metrics([records[2]])
    assert nothing["safe_apply_precision"] is None  # applying nothing is not 100%


def test_operational_never_reports_a_missing_measurement_as_zero():
    op = metrics.operational([0.1, 0.2], [0, 0], [0, 0], [None, None], 10)
    assert op["input_tokens"] is None and op["cost_usd"] is None and op["cost_per_1k_decisions_usd"] is None
    priced = metrics.operational([0.1], [1000], [10], [None], 4, price_in=3.0, price_out=15.0)
    assert priced["cost_usd"] == pytest.approx((1000 * 3 + 10 * 15) / 1e6) and priced["cost_source"] == "price"
    billed = metrics.operational([0.1, 0.3], [5, 5], [1, 1], [0.001, 0.002], 2)
    assert billed["cost_per_1k_decisions_usd"] == pytest.approx(1.5) and billed["latency_ms_p95"] == pytest.approx(300)
    assert metrics.percentile([1, 2, 3, 4], 50) == 2


# --------------------------------------------------------------------- runs
def _strip_volatile(results):
    out = json.loads(json.dumps(results))
    out.pop("date")
    for k in ("latency_ms_p50", "latency_ms_p95"):
        out["operational"].pop(k)
    return out


def test_rules_run_is_deterministic(rules_run):
    results, records = rules_run
    again, records_again = run_benchmark("rules")
    assert _strip_volatile(results) == _strip_volatile(again)
    assert records == records_again
    assert results["complete"] and results["system"]["calibrated"] is False
    assert results["operational"]["cost_source"] == "offline" and results["operational"]["input_tokens"] is None
    # The baseline's misses stay visible: it confirms one-letter look-alike typo merges.
    wrong = {r["subject"] for r in records if r["applied"] and not r["applied_correct"]}
    assert "Adduction -> Abduction" in wrong


def test_gold_perfect_backend_scores_perfectly():
    results, records = run_benchmark("cleanbench/oracle")
    o, e = results["metrics"]["overall"], results["end_to_end"]
    assert o["accuracy"] == 1.0 and o["safe_apply_precision"] == 1.0 and o["coverage"] == 1.0
    assert o["ece"] == pytest.approx(0.0) and o["brier"] == pytest.approx(0.0)
    assert e["cells_corrupted"] == 0 and e["cells_fixed"] == e["cells_fixable"] > 0


class AlwaysChange:
    """Says every value is an alias, every token is missing, every field has a source."""

    model = "always-change"
    calibrated = True

    def decide(self, state, questions):
        answers = {}
        for name, q in questions.items():
            if q.type == NOUL:
                answers[name] = Answer(NOUL, {"yes": 0.99, "no": 0.01})
            else:
                label = next(lbl for lbl in q.labels if lbl not in (DISTINCT, NO_MATCH))
                answers[name] = _choice(q.labels, label, 0.99)
        return DecisionResponse(answers, model=self.model, input_tokens=10, output_tokens=2)


def test_a_backend_that_always_merges_corrupts_cells():
    results, _ = run_benchmark(AlwaysChange())
    o, e = results["metrics"]["overall"], results["end_to_end"]
    assert e["cells_corrupted"] > 0
    assert o["safe_apply_precision"] < 0.6 and o["applied_wrong"] > 50
    assert results["system"]["type"] == "custom" and results["operational"]["input_tokens"] > 0


def test_export_items_are_standalone_system_one_requests(scenarios):
    items = list(export_items(scenarios[:6]))
    assert items
    for item in items:
        assert {"id", "state", "questions", "gold", "family", "difficulty"} <= set(item)
        assert 1 <= len(item["questions"]) <= 8
        for name, question in item["questions"].items():
            assert question["type"] in ("choice", "noul") and "context" not in question
            labels = list(question["criteria"]) if question["type"] == "choice" else ["yes", "no"]
            assert item["gold"][name] and set(item["gold"][name]) <= set(labels)
        json.dumps(item)
    assert len({i["id"] for i in items}) == len(items)


def test_leaderboard_shows_only_systems_that_ran(rules_run):
    results, _ = rules_run
    text = render([results])
    assert "| 1 | `rules` |" in text
    assert "Pending: run with your API key" in text
    assert "python -m bench.cleanbench run --system typesafe/jev-latest" in text
    # No ranked row for a system that did not run.
    assert not any(line.startswith("| ") and "typesafe/jev" in line and "%" in line for line in text.splitlines())
    stale = json.loads(json.dumps(results))
    stale["dataset"]["sha256"] = "0" * 64
    assert "dataset changed since this run" in render([stale])


def test_cli_validate_and_hash(tmp_path, capsys):
    import yaml

    split = tmp_path / "mini.yaml"
    split.write_text(yaml.safe_dump(_doc()), encoding="utf-8")
    assert cli(["validate", "--split", str(split)]) == 0
    assert "OK: every scenario asks its questions" in capsys.readouterr().out
    assert cli(["hash", str(split)]) == 0
    assert capsys.readouterr().out.split()[0] == file_sha256(split)
    broken = tmp_path / "broken.yaml"
    broken.write_text(yaml.safe_dump(_doc(expect={"alias": 5})), encoding="utf-8")
    assert cli(["validate", "--split", str(broken)]) == 1
    assert "expected 5 alias questions, got 2" in capsys.readouterr().err
