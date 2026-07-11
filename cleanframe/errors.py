"""Exception hierarchy for CleanFrame.

Every error CleanFrame raises deliberately derives from :class:`CleanFrameError`,
so callers can ``except cleanframe.CleanFrameError`` to catch anything the library
raises on purpose without also swallowing unrelated bugs.
"""

from __future__ import annotations


class CleanFrameError(Exception):
    """Base class for all errors raised deliberately by CleanFrame."""


class RecipeError(CleanFrameError):
    """A recipe is malformed, references an unknown op, or fails to load."""


class OpError(CleanFrameError):
    """An op could not be applied (bad params, or a runtime failure in pandas)."""


class ExecutionError(CleanFrameError):
    """Applying a recipe to a dataframe failed."""


class ValidationFailure(CleanFrameError):
    """A validation rule failed under a policy that raises (e.g. ``on_fail: error``
    or ``mode="strict"``)."""

    def __init__(self, message: str, failures: list | None = None) -> None:
        super().__init__(message)
        self.failures = failures or []


class DriftError(CleanFrameError):
    """Schema drift was detected under a policy that raises (e.g. ``mode="strict"``)."""

    def __init__(self, message: str, report=None) -> None:  # noqa: ANN001 - avoid import cycle
        super().__init__(message)
        self.report = report


class SchemaError(CleanFrameError):
    """A target schema is malformed or cannot be satisfied."""


class LLMError(CleanFrameError):
    """The LLM planner could not produce a valid recipe (misconfigured, no key,
    budget exceeded, or invalid output)."""


class BudgetExceeded(LLMError):
    """Planning was aborted because it would exceed ``max_tokens_budget``."""
