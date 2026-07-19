"""The recipe: CleanFrame's durable artifact.

A recipe is a small, human-reviewable YAML file that fully describes how to clean
a file. It is the thing you commit to git, review in a PR, and replay in CI with
**zero** AI calls. This module is the in-memory model plus a lenient loader and a
canonical serialiser.

Design notes
------------
* **Lenient in, canonical out.** The loader accepts the relaxed forms shown in the
  README (ops as sibling keys, ``allowed`` as an alias for ``formats``, a bare
  ``dedup`` list). :meth:`Recipe.to_dict` always emits one canonical shape, so a
  ``load`` → ``save`` round-trip normalises a hand-written recipe without changing
  its meaning.
* **Block-style YAML on purpose.** One op per line diffs cleanly in a PR — the
  whole point of "recipes are reviewed like code".
* **Fail loud on typos.** An unknown op name or an unexpected key raises
  :class:`~cleanframe.errors.RecipeError` at load time, not silently at run time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ._util import read_text, write_text
from ._version import __version__
from .errors import RecipeError
from .ops import get_op, normalize_op, op_to_compact
from .types import Op

RECIPE_VERSION = 1

#: Keys allowed inside a column entry besides op names.
_COLUMN_RESERVED = {"rename_to", "ops", "source", "name"}


@dataclass
class ValidationRule:
    """A single post-clean check with a failure policy.

    ``check`` is a compact expression understood by :mod:`cleanframe.validate`:
    a named check (``valid_email``, ``not_null``, ``unique``, ``valid_phone``), a
    comparison (``">= 0"``, ``"> 0"``), a membership test (``"in [a, b]"``), or a
    regex (``"matches: ^[A-Z]{3}$"``). ``on_fail`` is one of ``quarantine``
    (default), ``error``, ``warn``, ``drop``, or ``null``.
    """

    column: str
    check: str
    on_fail: str = "quarantine"
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        out: dict[str, Any] = {"column": self.column, "check": self.check, "on_fail": self.on_fail}
        out.update(self.params)
        return out

    @classmethod
    def from_dict(cls, raw: dict) -> ValidationRule:
        if not isinstance(raw, dict) or "column" not in raw or "check" not in raw:
            raise RecipeError(f"Validation rule must have 'column' and 'check': {raw!r}")
        params = {k: v for k, v in raw.items() if k not in ("column", "check", "on_fail")}
        return cls(
            column=str(raw["column"]),
            check=str(raw["check"]),
            on_fail=str(raw.get("on_fail", "quarantine")),
            params=params,
        )


@dataclass
class ColumnRecipe:
    """The cleaning plan for one source column: an optional rename plus ordered ops."""

    source: str
    rename_to: str | None = None
    ops: list[Op] = field(default_factory=list)

    @property
    def output_name(self) -> str:
        return self.rename_to or self.source

    def to_dict(self) -> dict:
        out: dict[str, Any] = {}
        if self.rename_to is not None:
            out["rename_to"] = self.rename_to
        if self.ops:
            out["ops"] = [op_to_compact(op) for op in self.ops]
        return out

    @classmethod
    def from_dict(cls, source: str, raw: Any) -> ColumnRecipe:
        if raw is None:
            return cls(source=source)
        if not isinstance(raw, dict):
            raise RecipeError(f"Column {source!r} must map to a mapping, got {type(raw).__name__}.")

        rename_to = raw.get("rename_to")
        ops: list[Op] = []

        # 1) explicit ops list
        for entry in raw.get("ops", []) or []:
            ops.append(_parse_op_entry(entry, scope="column", where=f"column {source!r}"))

        # 2) README-style sibling ops (e.g. `parse_date:` next to `rename_to:`)
        for key, value in raw.items():
            if key in _COLUMN_RESERVED:
                continue
            spec = _lookup_op(key, where=f"column {source!r}")
            if spec.scope != "column":
                raise RecipeError(f"Op {key!r} in column {source!r} is not a column op.")
            ops.append(normalize_op(key, value))

        return cls(source=source, rename_to=rename_to, ops=ops)


@dataclass
class Recipe:
    """A complete, replayable cleaning plan."""

    version: int = RECIPE_VERSION
    columns: list[ColumnRecipe] = field(default_factory=list)
    frame_ops: list[Op] = field(default_factory=list)
    validations: list[ValidationRule] = field(default_factory=list)
    source_fingerprint: dict | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    # -- convenience -----------------------------------------------------
    def column(self, source: str) -> ColumnRecipe | None:
        for c in self.columns:
            if c.source == source:
                return c
        return None

    @property
    def dedup_op(self) -> Op | None:
        for op in self.frame_ops:
            if op.name == "dedup":
                return op
        return None

    def rename_map(self) -> dict[str, str]:
        return {c.source: c.rename_to for c in self.columns if c.rename_to}

    # -- serialisation ---------------------------------------------------
    def to_dict(self) -> dict:
        """Canonical dict form (block-style, README-shaped)."""
        out: dict[str, Any] = {"version": self.version}
        if self.source_fingerprint is not None:
            out["source_fingerprint"] = self.source_fingerprint
        if self.columns:
            out["columns"] = {c.source: c.to_dict() for c in self.columns}
        # Serialise dedup at top level (readable); anything else under frame_ops.
        other_frame_ops = []
        for op in self.frame_ops:
            if op.name == "dedup":
                compact = op_to_compact(op)
                # compact is either "dedup" (all defaults) or {"dedup": {...}}
                out["dedup"] = compact[op.name] if isinstance(compact, dict) else True
            else:
                other_frame_ops.append(op_to_compact(op))
        if other_frame_ops:
            out["frame_ops"] = other_frame_ops
        if self.validations:
            out["validate"] = [v.to_dict() for v in self.validations]
        if self.meta:
            out["meta"] = self.meta
        return out

    def to_yaml(self) -> str:
        header = (
            "# CleanFrame recipe — generated by CleanFrame, edited by you, owned by git.\n"
            f"# Docs: https://github.com/inboxpraveen/Cleanframe/wiki  |  format v{self.version}\n"
        )
        body = yaml.safe_dump(
            self.to_dict(),
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=False,
            width=100,
        )
        return header + body

    def save(self, path: str | Path) -> Path:
        return write_text(path, self.to_yaml())

    @classmethod
    def from_dict(cls, raw: dict) -> Recipe:
        if not isinstance(raw, dict):
            raise RecipeError("A recipe must be a mapping at the top level.")

        version = int(raw.get("version", RECIPE_VERSION))
        if version != RECIPE_VERSION:
            raise RecipeError(
                f"Unsupported recipe version {version}. This build reads v{RECIPE_VERSION}."
            )

        columns: list[ColumnRecipe] = []
        raw_columns = raw.get("columns", {}) or {}
        if isinstance(raw_columns, dict):
            for source, spec in raw_columns.items():
                columns.append(ColumnRecipe.from_dict(str(source), spec))
        elif isinstance(raw_columns, list):
            for spec in raw_columns:
                if not isinstance(spec, dict) or not (spec.get("source") or spec.get("name")):
                    raise RecipeError("List-form columns need a 'source' (or 'name') key.")
                source = str(spec.get("source") or spec.get("name"))
                columns.append(ColumnRecipe.from_dict(source, spec))
        else:
            raise RecipeError("'columns' must be a mapping or a list.")

        frame_ops: list[Op] = []
        # Top-level `dedup:` sugar (bare list, mapping, or `true`).
        if "dedup" in raw and raw["dedup"] is not None:
            dv = raw["dedup"]
            frame_ops.append(normalize_op("dedup", None if dv is True else dv))
        for entry in raw.get("frame_ops", []) or []:
            frame_ops.append(_parse_op_entry(entry, scope="frame", where="frame_ops"))

        validations = [ValidationRule.from_dict(v) for v in raw.get("validate", []) or []]

        known_top = {
            "version", "source_fingerprint", "columns", "dedup", "frame_ops",
            "validate", "meta",
        }
        unknown = set(raw) - known_top
        if unknown:
            raise RecipeError(f"Unknown top-level recipe key(s): {sorted(unknown)}.")

        return cls(
            version=version,
            columns=columns,
            frame_ops=frame_ops,
            validations=validations,
            source_fingerprint=raw.get("source_fingerprint"),
            meta=raw.get("meta", {}) or {},
        )

    @classmethod
    def from_yaml(cls, text: str) -> Recipe:
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise RecipeError(f"Invalid recipe YAML: {exc}") from exc
        return cls.from_dict(data)

    @classmethod
    def load(cls, path: str | Path) -> Recipe:
        path = Path(path)
        if not path.exists():
            raise RecipeError(f"Recipe not found: {path}")
        return cls.from_yaml(read_text(path))

    def stamp_meta(self, **kv: Any) -> Recipe:
        self.meta.setdefault("created_with", f"cleanframe {__version__}")
        self.meta.update({k: v for k, v in kv.items() if v is not None})
        return self


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _lookup_op(name: str, where: str):
    try:
        return get_op(name)
    except RecipeError as exc:
        raise RecipeError(f"In {where}: {exc}") from exc


def _parse_op_entry(entry: Any, scope: str, where: str) -> Op:
    """Parse one op from the compact form (bare string, single-key mapping, or the
    ``[name]`` / ``[name, params]`` array shape some LLMs emit)."""
    if isinstance(entry, str):
        name, value = entry, None
    elif isinstance(entry, dict):
        if len(entry) != 1:
            raise RecipeError(
                f"In {where}: each op mapping must have exactly one key, got {sorted(entry)}."
            )
        name, value = next(iter(entry.items()))
    elif isinstance(entry, (list, tuple)) and 1 <= len(entry) <= 2 and isinstance(entry[0], str):
        # Lenient: models frequently write ops as ["remove_symbols", [","]] arrays.
        name = entry[0]
        value = entry[1] if len(entry) == 2 else None
    else:
        raise RecipeError(f"In {where}: op must be a string or single-key mapping, got {entry!r}.")

    spec = _lookup_op(name, where)
    if spec.scope != scope:
        raise RecipeError(f"In {where}: op {name!r} is a {spec.scope} op, expected {scope}.")
    return normalize_op(name, value)


__all__ = ["Recipe", "ColumnRecipe", "ValidationRule", "RECIPE_VERSION"]
