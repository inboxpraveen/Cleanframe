"""CleanFrame — the reproducible data-cleaning engine for Python.

    import cleanframe as cf

    result = cf.clean(df, target_schema="customer.yaml", mode="review")
    result.diff.show()
    result.recipe.save("customer.recipe.yaml")   # the durable artifact
    clean_df = result.dataframe

The LLM (optional) only ever writes the recipe; pure pandas executes it. Same
input → same output, every time. See the README for the full tour.
"""

from __future__ import annotations

from ._version import __version__

# -- high-level API --------------------------------------------------------
from .api import apply_recipe, clean, infer_schema, report, suggest_update
from .codegen import generate_code
from .dataio import read_frame, write_frame
from .detectors import DetectorContext, detector, list_detectors, run_detectors

# -- diff / drift / quality ------------------------------------------------
from .diff import CellChange, CellDiff, compute_diff
from .drift import DriftFinding, DriftReport, detect_drift

# -- errors ----------------------------------------------------------------
from .errors import (
    BudgetExceeded,
    CleanFrameError,
    DriftError,
    ExecutionError,
    LLMError,
    OpError,
    RecipeError,
    SchemaError,
    ValidationFailure,
)
from .executor import ExecutionResult, execute

# -- issues / detectors (the plugin surface) -------------------------------
from .issues import Issue, Issues, Proposal

# -- optional LLM planner --------------------------------------------------
from .llm import LLMPlanner, get_client, list_providers

# -- ops -------------------------------------------------------------------
from .ops import list_ops, register_op

# -- planning / execution --------------------------------------------------
from .planner import Planner, RulesPlanner, plan_recipe

# -- profiling -------------------------------------------------------------
from .profile import ColumnProfile, DataFrameProfile, profile_dataframe
from .quality import QualityScore, quality_score

# -- recipe / schema -------------------------------------------------------
from .recipe import ColumnRecipe, Recipe, ValidationRule

# -- results / io / codegen ------------------------------------------------
from .result import CleanResult, CodeArtifact, Report
from .schema import Schema, SchemaColumn

# -- core types ------------------------------------------------------------
from .types import LLMExposure, Mode, Op, Severity

# -- validators ------------------------------------------------------------
from .validate import list_validators, validator

__all__ = [
    "__version__",
    # api
    "clean",
    "report",
    "apply_recipe",
    "suggest_update",
    "infer_schema",
    # types
    "Mode",
    "Severity",
    "Op",
    "LLMExposure",
    # issues / plugins
    "Issue",
    "Issues",
    "Proposal",
    "detector",
    "validator",
    "register_op",
    "DetectorContext",
    "run_detectors",
    "list_detectors",
    "list_ops",
    "list_validators",
    # profiling
    "profile_dataframe",
    "DataFrameProfile",
    "ColumnProfile",
    # recipe / schema
    "Recipe",
    "ColumnRecipe",
    "ValidationRule",
    "Schema",
    "SchemaColumn",
    # planning / execution
    "Planner",
    "RulesPlanner",
    "LLMPlanner",
    "get_client",
    "list_providers",
    "plan_recipe",
    "execute",
    "ExecutionResult",
    # diff / drift / quality
    "CellDiff",
    "CellChange",
    "compute_diff",
    "detect_drift",
    "DriftReport",
    "DriftFinding",
    "quality_score",
    "QualityScore",
    # results / io / codegen
    "CleanResult",
    "Report",
    "CodeArtifact",
    "read_frame",
    "write_frame",
    "generate_code",
    # errors
    "CleanFrameError",
    "RecipeError",
    "OpError",
    "ExecutionError",
    "ValidationFailure",
    "DriftError",
    "SchemaError",
    "LLMError",
    "BudgetExceeded",
]
