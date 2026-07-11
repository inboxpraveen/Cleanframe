"""The planner: turn a profile + detected issues into a concrete :class:`Recipe`.

Detectors supply *proposals* (domain knowledge). The planner applies *policy*:

* **Confidence gating by mode** — ``strict`` takes only high-confidence fixes,
  ``review`` surfaces almost everything for a human, ``auto`` sits between.
* **Canonical op ordering** — the single source of truth for what order ops run in
  a column, so whitespace is cleaned before categories are normalised, currency is
  split before its symbols are stripped, and casing is applied last. This is what
  makes independently-authored detectors compose correctly.
* **Rename resolution** — schema mapping beats a detector's rename (currency's
  ``amount_inr``) beats a default ``snake_case``, with collision handling.
* **Validation synthesis** — from the target schema's constraints, or (schemaless)
  from the semantic types of the columns.

The planner is deliberately an interface (:class:`Planner`) with a deterministic
:class:`RulesPlanner` implementation. An LLM planner (see :mod:`cleanframe.llm`)
implements the same interface and returns a :class:`Recipe` validated by the *same*
model — which is exactly why "the LLM never touches your data": it only ever emits
a plan the deterministic executor runs.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

import pandas as pd

from ._util import snake_case
from .fingerprint import fingerprint_dataframe
from .issues import Issues
from .ops import get_op
from .profile import DataFrameProfile, profile_dataframe
from .recipe import ColumnRecipe, Recipe, ValidationRule
from .types import Mode, Op

#: Minimum issue confidence for a fix to be included, per mode.
MODE_THRESHOLDS: dict[Mode, float] = {
    Mode.REVIEW: 0.5,
    Mode.AUTO: 0.65,
    Mode.STRICT: 0.85,
}

#: Canonical execution order for column ops. Ops not listed keep insertion order
#: after all listed ones. THE contract that lets detectors stay independent.
OP_ORDER: list[str] = [
    "strip_whitespace",
    "collapse_whitespace",
    "to_na",
    "extract_currency",
    "remove_symbols",
    "normalize_unit",
    "parse_number",
    "round",
    "cast",
    "parse_date",
    "normalize_email",
    "normalize_phone",
    "replace",
    "normalize_values",
    "capitalize",
    "title_case",
    "lowercase",
    "uppercase",
    "fill_na",
]
_ORDER_INDEX = {name: i for i, name in enumerate(OP_ORDER)}

# Detector-source ranking for rename resolution (lower wins).
_RENAME_RANK = {"schema_mapping": 0, "currency": 1}


class Planner(Protocol):
    """Anything that maps (data, profile, issues, schema, mode) to a Recipe."""

    def plan(
        self,
        df: pd.DataFrame,
        profile: DataFrameProfile,
        issues: Issues,
        *,
        schema: Any | None = None,
        mode: Mode | str = Mode.REVIEW,
        options: dict[str, Any] | None = None,
    ) -> Recipe: ...


def _op_key(op: Op) -> tuple[str, str]:
    return op.name, json.dumps(op.params, sort_keys=True, default=str)


def _finalize_ops(ops: list[Op]) -> list[Op]:
    """De-duplicate, subsume, and canonically order a column's ops."""
    # 1) drop exact duplicates, keep first occurrence
    seen: set[tuple[str, str]] = set()
    unique: list[Op] = []
    for op in ops:
        key = _op_key(op)
        if key in seen:
            continue
        seen.add(key)
        unique.append(op)

    names = {op.name for op in unique}
    # 2) subsumption: a stronger op makes a weaker one redundant
    drop: set[str] = set()
    if "normalize_email" in names:
        drop |= {"strip_whitespace", "collapse_whitespace", "lowercase"}
    if "collapse_whitespace" in names:
        drop |= {"strip_whitespace"}
    unique = [op for op in unique if op.name not in drop]

    # 3) canonical order; ties and unlisted ops fall back to original position,
    #    so the sort is stable and deterministic.
    ordered = sorted(
        enumerate(unique),
        key=lambda pair: (_ORDER_INDEX.get(pair[1].name, len(OP_ORDER) + pair[0]), pair[0]),
    )
    return [op for _, op in ordered]


class RulesPlanner:
    """Deterministic planner. No LLM, no network — the default and always-available path."""

    def plan(
        self,
        df: pd.DataFrame,
        profile: DataFrameProfile,
        issues: Issues,
        *,
        schema: Any | None = None,
        mode: Mode | str = Mode.REVIEW,
        options: dict[str, Any] | None = None,
    ) -> Recipe:
        mode = Mode.coerce(mode)
        options = options or {}
        threshold = MODE_THRESHOLDS[mode]
        rename_columns = options.get("rename_columns", True)

        column_ops: dict[str, list[Op]] = {}
        frame_ops: list[Op] = []
        rename_candidates: dict[str, list[tuple[int, float, str]]] = {}

        for issue in issues:
            if not issue.has_fix or issue.confidence < threshold:
                continue
            proposal = issue.proposal
            for op in proposal.ops:
                if get_op(op.name).scope == "frame":
                    frame_ops.append(op)
                elif issue.column is not None:
                    column_ops.setdefault(issue.column, []).append(op)
            if proposal.rename_to and issue.column is not None:
                rank = _RENAME_RANK.get(issue.detector, 2)
                rename_candidates.setdefault(issue.column, []).append(
                    (rank, issue.confidence, proposal.rename_to)
                )

        renames = self._resolve_renames(df, rename_candidates, rename_columns)
        self._apply_schema_casts(df, schema, column_ops, renames)

        columns: list[ColumnRecipe] = []
        for col in df.columns:
            col = str(col)
            ops = _finalize_ops(column_ops.get(col, []))
            rename_to = renames.get(col)
            if ops or rename_to:
                columns.append(ColumnRecipe(source=col, rename_to=rename_to, ops=ops))

        validations = self._build_validations(df, profile, schema, renames, mode)

        recipe = Recipe(
            columns=columns,
            frame_ops=_dedup_ops(frame_ops),
            validations=validations,
            source_fingerprint=fingerprint_dataframe(df),
        )
        notes = self._notes(issues, mode, threshold)
        recipe.stamp_meta(generated_by="rules", mode=mode.value, notes=notes or None)
        return recipe

    # -- renames ---------------------------------------------------------
    def _resolve_renames(
        self,
        df: pd.DataFrame,
        candidates: dict[str, list[tuple[int, float, str]]],
        rename_columns: bool,
    ) -> dict[str, str]:
        chosen: dict[str, str] = {}
        for col, cands in candidates.items():
            cands.sort(key=lambda t: (t[0], -t[1], t[2]))
            chosen[col] = cands[0][2]

        if rename_columns:
            for col in df.columns:
                col = str(col)
                if col not in chosen:
                    sn = snake_case(col)
                    if sn and sn != col:
                        chosen[col] = sn

        # Collision handling: an output name is claimed at most once. A rename must
        # not clobber another column that is KEEPING its name, so we reserve those
        # up front (this is the case a purely-incremental check misses: e.g. a frame
        # with both "First Name" and "first_name" — the former must not snake_case
        # onto the latter). A colliding rename is dropped; the column keeps its
        # source name, and only if that too is taken do we fall back to a suffix.
        occupied: set[str] = {str(c) for c in df.columns if str(c) not in chosen}
        final: dict[str, str] = {}
        for col in df.columns:
            col = str(col)
            if col not in chosen:
                continue  # keeps its original name, already reserved
            target = chosen[col]
            if target not in occupied:
                final[col] = target
                occupied.add(target)
            elif col not in occupied:
                occupied.add(col)  # give up the rename, keep the source name
            else:
                suffix = 2
                while f"{target}_{suffix}" in occupied:
                    suffix += 1
                final[col] = f"{target}_{suffix}"
                occupied.add(f"{target}_{suffix}")
        return final

    # -- schema-driven casts (light touch) -------------------------------
    def _apply_schema_casts(
        self,
        df: pd.DataFrame,
        schema: Any | None,
        column_ops: dict[str, list[Op]],
        renames: dict[str, str],
    ) -> None:
        if schema is None:
            return
        output_to_source = {renames.get(str(c), str(c)): str(c) for c in df.columns}
        for scol in getattr(schema, "columns", []):
            src = output_to_source.get(scol.name)
            if src is None:
                continue
            existing = {op.name for op in column_ops.get(src, [])}
            if scol.dtype in ("integer", "float") and not ({"parse_number", "cast"} & existing):
                column_ops.setdefault(src, []).append(Op("cast", {"to": scol.dtype}))
            elif scol.dtype == "integer" and "parse_number" in existing and "cast" not in existing:
                column_ops.setdefault(src, []).append(Op("cast", {"to": "int"}))
            if scol.dtype in ("date", "datetime") and "parse_date" not in existing and scol.date_formats:
                column_ops.setdefault(src, []).append(
                    Op("parse_date", {"formats": list(scol.date_formats)})
                )

    # -- validations -----------------------------------------------------
    def _build_validations(
        self,
        df: pd.DataFrame,
        profile: DataFrameProfile,
        schema: Any | None,
        renames: dict[str, str],
        mode: Mode,
    ) -> list[ValidationRule]:
        on_fail = "error" if mode is Mode.STRICT else "quarantine"
        rules: list[ValidationRule] = []

        def out(col: str) -> str:
            return renames.get(col, col)

        if schema is not None:
            output_names = {out(str(c)) for c in df.columns}
            for scol in getattr(schema, "columns", []):
                if scol.name not in output_names:
                    continue
                if scol.required:
                    rules.append(ValidationRule(scol.name, "not_null", on_fail))
                if scol.unique:
                    rules.append(ValidationRule(scol.name, "unique", on_fail))
                if scol.dtype == "email":
                    rules.append(ValidationRule(scol.name, "valid_email", on_fail))
                if scol.dtype == "phone":
                    rules.append(ValidationRule(scol.name, "valid_phone", on_fail))
                if scol.min is not None:
                    rules.append(ValidationRule(scol.name, f">= {scol.min}", on_fail))
                if scol.max is not None:
                    rules.append(ValidationRule(scol.name, f"<= {scol.max}", on_fail))
                if scol.allowed_values:
                    rules.append(
                        ValidationRule(scol.name, "in", on_fail, params={"values": list(scol.allowed_values)})
                    )
        else:
            for cp in profile.columns:
                o = out(cp.name)
                if cp.semantic_type == "email":
                    rules.append(ValidationRule(o, "valid_email", on_fail))
                elif cp.semantic_type == "phone":
                    rules.append(ValidationRule(o, "valid_phone", on_fail))
        return rules

    def _notes(self, issues: Issues, mode: Mode, threshold: float) -> list[str]:
        notes: list[str] = []
        skipped = [i for i in issues if i.has_fix and i.confidence < threshold]
        if skipped:
            kinds = sorted({i.kind for i in skipped})
            notes.append(
                f"{len(skipped)} low-confidence fix(es) omitted under mode={mode.value} "
                f"(threshold {threshold}): {', '.join(kinds)}"
            )
        unresolved = [
            i for i in issues
            if not i.has_fix and i.severity.rank >= 1 and i.kind not in ("missing_values",)
        ]
        if unresolved:
            notes.append(
                f"{len(unresolved)} issue(s) need review (no automatic fix): "
                + ", ".join(sorted({i.kind for i in unresolved}))
            )
        return notes


def _dedup_ops(ops: list[Op]) -> list[Op]:
    seen: set[tuple[str, str]] = set()
    out: list[Op] = []
    for op in ops:
        key = _op_key(op)
        if key not in seen:
            seen.add(key)
            out.append(op)
    return out


def plan_recipe(
    df: pd.DataFrame,
    *,
    profile: DataFrameProfile | None = None,
    issues: Issues | None = None,
    schema: Any | None = None,
    mode: Mode | str = Mode.REVIEW,
    options: dict[str, Any] | None = None,
    planner: Planner | None = None,
) -> Recipe:
    """Convenience: profile + detect + plan in one call using the rules planner by default."""
    from .detectors import run_detectors  # local import avoids a cycle

    profile = profile or profile_dataframe(df)
    if issues is None:
        issues = run_detectors(df, profile=profile, schema=schema, options=options)
    planner = planner or RulesPlanner()
    return planner.plan(df, profile, issues, schema=schema, mode=mode, options=options)


__all__ = ["Planner", "RulesPlanner", "plan_recipe", "MODE_THRESHOLDS", "OP_ORDER"]
