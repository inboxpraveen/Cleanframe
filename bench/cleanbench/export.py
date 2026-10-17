"""Export CleanBench as standalone System One requests, runnable without CleanFrame.

Each line is one request exactly as CleanFrame sends it (``state`` plus up to 8 named
``questions`` in System One wire form) with the gold for every question::

    {"id": "...", "scenario": "...", "family": "category_alias", "difficulty": "hard",
     "domain": "...", "state": {...}, "questions": {"alias_0": {...}}, "gold": {"alias_0": ["Bangalore"]}}

``gold`` lists every acceptable label (any one is correct). The requests come from
running the pipeline with the gold oracle, so they are the questions a perfect
system is asked; a system that vetoes a correct typo merge inside CleanFrame is
then asked one more alias question about that spelling, which this snapshot lacks.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from .dataset import Scenario
from .harness import OracleClient, run_scenario


def export_items(scenarios: list[Scenario]) -> Iterator[dict[str, Any]]:
    for sc in scenarios:
        run = run_scenario(sc, OracleClient(sc))
        if run.error:
            raise RuntimeError(f"{sc.id}: the oracle run failed: {run.error}")
        for index, req in enumerate(run.recorder.requests):
            kinds = sorted({str(q.context.get("kind")) for q in req.questions.values()})
            yield {
                "id": f"{sc.id}/{index:02d}",
                "scenario": sc.id,
                "family": kinds[0] if len(kinds) == 1 else kinds,
                "difficulty": sc.difficulty,
                "domain": sc.domain,
                "state": req.state,
                "questions": {name: q.to_wire() for name, q in req.questions.items()},
                "gold": {name: sc.gold(q.context) for name, q in req.questions.items()},
            }


def write_jsonl(items: Iterator[dict[str, Any]], path: Any) -> int:
    n = 0
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for item in items:
            fh.write(json.dumps(item, ensure_ascii=False, sort_keys=False) + "\n")
            n += 1
    return n
