"""Shared enums and small value types used across CleanFrame.

Kept dependency-free (stdlib only) so every other module can import it without
risk of an import cycle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Mode(str, Enum):
    """How aggressively CleanFrame commits to fixes and how it reacts to problems.

    ``str`` mixin so a plain ``"review"`` string round-trips through configs and
    YAML transparently (``Mode("review") == "review"``).
    """

    #: Build the recipe and cleaned frame, quarantine validation failures, and
    #: surface everything for a human to approve. Nothing is written to disk
    #: automatically. This is the default.
    REVIEW = "review"

    #: Same deterministic result as ``review`` but intended for unattended
    #: pipelines: no interactive gating is expected.
    AUTO = "auto"

    #: Zero tolerance. Validation failures, schema drift, and low-confidence
    #: proposals raise instead of being quarantined or silently dropped.
    STRICT = "strict"

    @classmethod
    def coerce(cls, value: Mode | str) -> Mode:
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).lower())
        except ValueError as exc:  # pragma: no cover - defensive
            valid = ", ".join(m.value for m in cls)
            raise ValueError(f"Unknown mode {value!r}. Expected one of: {valid}") from exc


class Severity(str, Enum):
    """Ordered severity for detected issues and validation results."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"

    @property
    def rank(self) -> int:
        return {"info": 0, "warning": 1, "error": 2}[self.value]

    def __lt__(self, other: object) -> bool:
        if isinstance(other, Severity):
            return self.rank < other.rank
        return NotImplemented


class LLMExposure(str, Enum):
    """How much of your data an LLM planner is permitted to see.

    Mirrors the privacy tiers documented in the README. ``NONE`` keeps everything
    on your machine; higher tiers progressively expand what metadata may leave it,
    and never include raw cell values unless you explicitly opt into ``SAMPLE``.
    """

    #: Rules-only. No network calls, nothing leaves the machine.
    NONE = "none"
    #: LLM sees column names, dtypes, and value *patterns* (regex sketches) only.
    METADATA = "metadata"
    #: LLM additionally sees an anonymized, shuffled sample the caller approved.
    SAMPLE = "sample"


@dataclass
class Op:
    """A single transformation step: an op name plus its parameters.

    ``Op`` is the atomic unit of a recipe. It is deliberately dumb — it carries a
    name and a params dict, and knows how to render itself back to the compact
    YAML form. The mapping from name to a concrete pandas function lives in the op
    registry (:mod:`cleanframe.ops`); the mapping to executable Python source lives
    in :mod:`cleanframe.codegen`. Keeping ``Op`` free of behaviour is what lets the
    exact same object flow from detector → planner → recipe → executor → codegen.

    The compact form used in recipe YAML is either a bare string (no params) or a
    single-key mapping ``{name: params}``::

        strip_whitespace                 # -> Op("strip_whitespace", {})
        {remove_symbols: ["₹", ","]}     # -> Op("remove_symbols", {"symbols": [...]})
        {cast: float}                    # -> Op("cast", {"to": "float"})
    """

    name: str
    params: dict[str, Any] = field(default_factory=dict)

    def to_compact(self) -> Any:
        """Render to the compact YAML form (bare string when there are no params)."""
        if not self.params:
            return self.name
        return {self.name: dict(self.params)}

    def with_params(self, **updates: Any) -> Op:
        merged = dict(self.params)
        merged.update(updates)
        return Op(self.name, merged)

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        if not self.params:
            return f"Op({self.name!r})"
        return f"Op({self.name!r}, {self.params!r})"
