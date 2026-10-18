"""CleanBench: a benchmark for data-cleaning decisions made by decision models.

Run from the repository root::

    python -m bench.cleanbench validate
    python -m bench.cleanbench run --system rules
    python -m bench.cleanbench leaderboard

See ``bench/README.md``.
"""

from .dataset import DatasetError, Scenario, load_split
from .harness import run_benchmark, validate

__all__ = ["DatasetError", "Scenario", "load_split", "run_benchmark", "validate"]
