"""Load and check CleanBench scenarios, and look up the gold answer for a question.

A scenario is a small messy table plus its gold, written as *equivalence classes*
rather than per-question labels, so the gold answers whatever question CleanFrame's
pipeline happens to ask:

* ``same``: groups of spellings that name one thing (``[Bangalore, Bengaluru, BLR]``).
  A spelling in no group is its own, distinct value.
* ``placeholders``: tokens that mean "missing" in that column. Every other value,
  including a null-looking token such as ``none`` in an *allergies* column, is data.
* ``gold`` (schema scenarios): which source column holds each target field, or
  ``null`` when none does.

A question the gold cannot answer (an unknown value, an unknown target) is a dataset
bug and raises :class:`DatasetError` instead of being scored.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from cleanframe._util import normalize_key
from cleanframe.decisions import DISTINCT, NO_MATCH
from cleanframe.detectors.categories import _skeleton

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
SPLITS = {"public": DATA_DIR / "public.yaml"}

#: Question kinds, which are also the scenario families.
ALIAS = "category_alias"
MERGE = "category_merge"
NULL = "null_token"
SCHEMA = "schema_match"
FAMILIES = (ALIAS, MERGE, NULL, SCHEMA)
DIFFICULTIES = ("easy", "standard", "hard")
#: ``expect:`` keys and the question kind each one pins down.
EXPECT_KEYS = {"alias": ALIAS, "merge": MERGE, "nulls": NULL, "schema": SCHEMA}

_SCHEMA_DTYPES = frozenset(
    {"string", "text", "integer", "float", "boolean", "date", "datetime", "email",
     "phone", "url", "category"}
)
_REQUIRED = ("id", "family", "domain", "difficulty", "rationale")


class DatasetError(ValueError):
    """The dataset is malformed, or a question cannot be answered from its gold."""


def key(value: Any) -> str:
    """How gold compares spellings: whitespace-collapsed and case-folded.

    ``bengaluru`` and ``Bengaluru`` are one spelling for the gold (CleanFrame merges
    case variants without asking); ``BLR`` and ``Bangalore`` are not.
    """
    return " ".join(str(value).split()).casefold()


def file_sha256(path: Path) -> str:
    """SHA-256 of a text file with line endings normalised to LF.

    A Windows checkout (CRLF) and a POSIX one hash the same. Equivalent to
    ``tr -d '\\r' < FILE | sha256sum``.
    """
    data = Path(path).read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


@dataclass
class ColumnGold:
    """The gold for one text column: which spellings are one value, which mean missing."""

    name: str
    #: spelling -> number of cells (the authored values; whitespace already clean)
    counts: dict[str, int]
    #: key(spelling) -> class id; spellings in no ``same`` group get their own id
    class_id: dict[str, int] = field(default_factory=dict)
    #: keys of the tokens that mean "missing" in this column
    placeholders: set[str] = field(default_factory=set)

    def known(self, value: Any) -> bool:
        return key(value) in self.class_id

    def same(self, a: Any, b: Any) -> bool:
        return self._cls(a) == self._cls(b)

    def is_placeholder(self, value: Any) -> bool:
        k = key(value)
        self._cls(value)
        return k in self.placeholders

    def _cls(self, value: Any) -> int:
        k = key(value)
        if k not in self.class_id:
            raise DatasetError(
                f"column {self.name!r}: the pipeline asked about {value!r}, which is not one of "
                "the scenario's values - add it to `values` (and to a `same` group if it has one)"
            )
        return self.class_id[k]


@dataclass
class Scenario:
    id: str
    family: str
    domain: str
    difficulty: str
    rationale: str
    #: column -> cells, in row order (``None`` is an empty cell)
    data: dict[str, list[str | None]]
    #: gold for every text column
    columns: dict[str, ColumnGold]
    #: target schema as CleanFrame takes it (``{"columns": {...}}``), or None
    schema: dict[str, Any] | None = None
    #: target -> source column (None: no column holds it)
    schema_gold: dict[str, str | None] = field(default_factory=dict)
    #: expected questions (kind -> sorted subjects, or a count for aliases)
    expect: dict[str, Any] = field(default_factory=dict)

    @property
    def rows(self) -> int:
        return len(next(iter(self.data.values()))) if self.data else 0

    def frame(self) -> pd.DataFrame:
        """The scenario as the DataFrame a user would hand to ``cf.clean``.

        Every cell is text (as when a messy CSV is read verbatim); CleanFrame's own
        profiler infers what the columns hold.
        """
        return pd.DataFrame({c: pd.Series(v, dtype=object) for c, v in self.data.items()})

    # -- gold lookup -------------------------------------------------------------
    def gold(self, context: Mapping[str, Any]) -> list[str]:
        """The acceptable answer labels for a question, from its ``context``.

        Choice questions get every acceptable option (any one is correct); yes/no
        questions get ``["yes"]`` or ``["no"]``.
        """
        kind = context.get("kind")
        if kind == SCHEMA:
            target = str(context["target"])
            if target not in self.schema_gold:
                raise DatasetError(f"{self.id}: no gold for schema target {target!r}")
            source = self.schema_gold[target]
            options = [str(o) for o in context.get("options", [])]
            if source is not None and source not in options:
                raise DatasetError(
                    f"{self.id}: the gold source {source!r} for {target!r} is not among the "
                    f"options {options} (the rules already gave it to another target)"
                )
            return [source] if source is not None else [NO_MATCH]
        column = self._column(context)
        if kind == ALIAS:
            value = context["value"]
            column._cls(value)
            options = [str(o) for o in context.get("options", [])]
            for option in options:
                column._cls(option)
            matches = [o for o in options if column.same(value, o)]
            return matches or [DISTINCT]
        if kind == MERGE:
            return ["yes"] if column.same(context["variant"], context["canonical"]) else ["no"]
        if kind == NULL:
            return ["yes"] if column.is_placeholder(context["token"]) else ["no"]
        raise DatasetError(f"{self.id}: unknown question kind {kind!r}")

    def _column(self, context: Mapping[str, Any]) -> ColumnGold:
        name = str(context.get("column"))
        if name not in self.columns:
            raise DatasetError(f"{self.id}: the pipeline asked about column {name!r}, which has no gold")
        return self.columns[name]


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def load_split(split: str | Path) -> list[Scenario]:
    """Load a split by name (``public``) or by path. Raises :class:`DatasetError`."""
    path = split_path(split)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise DatasetError(f"{path}: not valid YAML: {exc}") from None
    return parse_dataset(raw, source=str(path))


def split_path(split: str | Path) -> Path:
    if isinstance(split, str) and split in SPLITS:
        return SPLITS[split]
    path = Path(split)
    if not path.is_file():
        raise DatasetError(f"no such split or file: {split!s} (known splits: {', '.join(SPLITS)})")
    return path


def parse_dataset(raw: Any, *, source: str = "<dataset>") -> list[Scenario]:
    """Parse and check a dataset document, reporting every problem at once."""
    errors: list[str] = []
    if not isinstance(raw, dict) or not isinstance(raw.get("scenarios"), list):
        raise DatasetError(f"{source}: expected a mapping with a `scenarios` list")
    scenarios: list[Scenario] = []
    seen: set[str] = set()
    for i, item in enumerate(raw["scenarios"]):
        where = f"{source} scenario #{i + 1}"
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            where = f"{source} [{item['id']}]"
        problems: list[str] = []
        scenario = _parse_scenario(item, problems)
        if scenario is not None:
            if scenario.id in seen:
                problems.append(f"duplicate id {scenario.id!r}")
            seen.add(scenario.id)
        errors.extend(f"{where}: {p}" for p in problems)
        if scenario is not None and not problems:
            scenarios.append(scenario)
    if not scenarios and not errors:
        errors.append(f"{source}: no scenarios")
    if errors:
        raise DatasetError("dataset problems:\n  " + "\n  ".join(errors))
    return scenarios


def _text(value: Any, what: str, problems: list[str]) -> str | None:
    """A cell value. YAML turns bare yes/no/null/dates into other types - refuse them."""
    if isinstance(value, bool) or value is None or not isinstance(value, (str, int, float)):
        problems.append(
            f"{what}: {value!r} is not text - quote it (YAML reads bare yes/no/on/off/null/dates "
            "as other types)"
        )
        return None
    text = str(value)
    if text != " ".join(text.split()) or not text:
        problems.append(f"{what}: {text!r} has stray whitespace or is empty")
        return None
    return text


def _parse_scenario(item: Any, problems: list[str]) -> Scenario | None:
    if not isinstance(item, dict):
        problems.append("a scenario must be a mapping")
        return None
    for name in _REQUIRED:
        if not isinstance(item.get(name), str) or not item[name].strip():
            problems.append(f"`{name}` must be a non-empty string")
    if problems:
        return None
    family, difficulty = item["family"], item["difficulty"]
    if family not in FAMILIES:
        problems.append(f"family {family!r} is not one of {', '.join(FAMILIES)}")
    if difficulty not in DIFFICULTIES:
        problems.append(f"difficulty {difficulty!r} is not one of {', '.join(DIFFICULTIES)}")
    allowed = set(_REQUIRED) | {"expect"}
    if family == SCHEMA:
        allowed |= {"columns", "schema", "gold", "same", "placeholders"}
    else:
        allowed |= {"column", "values", "same", "placeholders"}
    unknown = sorted(set(item) - allowed)
    if unknown:
        problems.append(f"unknown field(s) {unknown} for a {family} scenario")
    if problems:
        return None

    data: dict[str, list[str | None]] = {}
    counts_by_col: dict[str, dict[str, int]] = {}
    same_by_col: dict[str, Any] = {}
    ph_by_col: dict[str, Any] = {}
    schema = None
    schema_gold: dict[str, str | None] = {}

    if family == SCHEMA:
        columns = item.get("columns")
        if not isinstance(columns, dict) or len(columns) < 2:
            problems.append("`columns` must map at least two source column names to value lists")
            return None
        lengths = set()
        for col, cells in columns.items():
            if not isinstance(col, str) or not isinstance(cells, list) or not cells:
                problems.append(f"column {col!r} must be a name with a non-empty list of values")
                continue
            parsed = [_text(v, f"column {col!r}", problems) for v in cells]
            data[col] = parsed
            lengths.add(len(cells))
            counts: dict[str, int] = {}
            for v in parsed:
                if v is not None:
                    counts[v] = counts.get(v, 0) + 1
            counts_by_col[col] = counts
        if len(lengths) > 1:
            problems.append(f"all columns need the same number of rows, got {sorted(lengths)}")
        raw_schema = item.get("schema")
        if not isinstance(raw_schema, dict) or not raw_schema:
            problems.append("`schema` must map target fields to {dtype: ...}")
            return None
        schema_cols: dict[str, Any] = {}
        for target, spec in raw_schema.items():
            dtype = spec.get("dtype") if isinstance(spec, dict) else spec
            if dtype not in _SCHEMA_DTYPES:
                problems.append(f"schema target {target!r}: dtype {dtype!r} is not one of {sorted(_SCHEMA_DTYPES)}")
                continue
            schema_cols[str(target)] = {"dtype": dtype, "required": False}
        schema = {"columns": schema_cols}
        raw_gold = item.get("gold")
        if not isinstance(raw_gold, dict) or set(map(str, raw_gold)) != set(schema_cols):
            problems.append("`gold` must give a source column (or null) for every schema target")
        else:
            for target, src in raw_gold.items():
                if src is not None and src not in data:
                    problems.append(f"gold for {target!r} names {src!r}, which is not a column")
                schema_gold[str(target)] = src
            sources = [s for s in schema_gold.values() if s is not None]
            if len(sources) != len(set(sources)):
                problems.append("`gold` maps one source column onto two targets")
        same_by_col = item.get("same") or {}
        ph_by_col = item.get("placeholders") or {}
        if not isinstance(same_by_col, dict) or not isinstance(ph_by_col, dict):
            problems.append("in a schema scenario `same`/`placeholders` map a column to its gold")
            return None
    else:
        col = item.get("column")
        values = item.get("values")
        if not isinstance(col, str) or not col:
            problems.append("`column` must be a column name")
            return None
        if not isinstance(values, dict) or len(values) < 2:
            problems.append("`values` must map at least two spellings to their counts")
            return None
        counts = {}
        for spelling, n in values.items():
            text = _text(spelling, "values", problems)
            if text is None:
                continue
            if isinstance(n, bool) or not isinstance(n, int) or n < 1:
                problems.append(f"values: count for {text!r} must be a positive integer")
                continue
            counts[text] = n
        counts_by_col[col] = counts
        data[col] = _interleave(counts)
        same_by_col = {col: item.get("same") or []}
        ph_by_col = {col: item.get("placeholders") or []}

    columns: dict[str, ColumnGold] = {}
    for col, counts in counts_by_col.items():
        columns[col] = _column_gold(col, counts, same_by_col.get(col) or [], ph_by_col.get(col) or [], problems)
    for col in set(same_by_col) | set(ph_by_col):
        if col not in counts_by_col:
            problems.append(f"`same`/`placeholders` name {col!r}, which is not a column")

    expect = _parse_expect(item.get("expect"), problems)
    if problems:
        return None
    return Scenario(
        id=item["id"], family=family, domain=item["domain"], difficulty=difficulty,
        rationale=" ".join(item["rationale"].split()), data=data, columns=columns,
        schema=schema, schema_gold=schema_gold, expect=expect,
    )


def _interleave(counts: dict[str, int]) -> list[str | None]:
    """Expand ``{value: count}`` into cells, round-robin, so the column looks like data."""
    remaining = dict(counts)
    out: list[str | None] = []
    while any(remaining.values()):
        for value in counts:
            if remaining[value]:
                out.append(value)
                remaining[value] -= 1
    return out


def _column_gold(name: str, counts: dict[str, int], same: Any, placeholders: Any,
                 problems: list[str]) -> ColumnGold:
    gold = ColumnGold(name=name, counts=dict(counts))
    by_key: dict[str, str] = {}
    for spelling in counts:
        # Case variants ("bengaluru" / "Bengaluru") share a key, so they share a class.
        by_key.setdefault(key(spelling), spelling)
    if not isinstance(same, list) or not all(isinstance(g, list) for g in same):
        problems.append(f"column {name!r}: `same` must be a list of lists of spellings")
        same = []
    next_id = 0
    for group in same:
        if len(group) < 2:
            problems.append(f"column {name!r}: a `same` group needs two or more spellings: {group}")
        for spelling in group:
            if not isinstance(spelling, str) or key(spelling) not in by_key:
                problems.append(f"column {name!r}: `same` lists {spelling!r}, which is not in the values")
                continue
            if key(spelling) in gold.class_id:
                problems.append(f"column {name!r}: {spelling!r} is in two `same` groups")
                continue
            gold.class_id[key(spelling)] = next_id
        next_id += 1
    for k in by_key:
        if k not in gold.class_id:
            gold.class_id[k] = next_id
            next_id += 1
    if not isinstance(placeholders, list):
        problems.append(f"column {name!r}: `placeholders` must be a list")
        placeholders = []
    for token in placeholders:
        if not isinstance(token, str) or key(token) not in by_key:
            problems.append(f"column {name!r}: placeholder {token!r} is not in the values")
            continue
        gold.placeholders.add(key(token))
    for token in gold.placeholders:
        cls = gold.class_id[token]
        if sum(1 for c in gold.class_id.values() if c == cls) > 1:
            problems.append(f"column {name!r}: placeholder {by_key[token]!r} cannot also be in a `same` group")
    # CleanFrame merges spellings that differ only in case, spaces and punctuation
    # without asking anyone (``normalize_key`` + number skeleton). Such pairs cannot
    # test a decision model, and gold that splits them would charge every system for
    # the rules' choice - so they must sit in one class.
    groups: dict[str, list[str]] = {}
    for spelling in counts:
        nk = normalize_key(spelling)
        if nk:
            groups.setdefault(f"{nk}\x00{_skeleton(spelling)}", []).append(spelling)
    for members in groups.values():
        if len({gold.class_id[key(m)] for m in members}) > 1:
            problems.append(
                f"column {name!r}: CleanFrame's rules merge {members} without a question "
                "(same letters and digits once case, spaces and punctuation are dropped), "
                "but the gold keeps them apart - drop one of them"
            )
    return gold


def _parse_expect(raw: Any, problems: list[str]) -> dict[str, Any]:
    """``expect:`` pins the questions a scenario must produce (checked by ``validate``).

    ``merge``/``nulls``/``schema`` list subjects (``"variant -> canonical"``, token,
    target); a kind that is left out must produce no question. ``alias`` is a count.
    """
    if raw is None:
        problems.append("`expect` is required: the questions this scenario is written to produce")
        return {}
    if not isinstance(raw, dict):
        problems.append("`expect` must be a mapping")
        return {}
    out: dict[str, Any] = {}
    for name, value in raw.items():
        if name not in EXPECT_KEYS:
            problems.append(f"`expect` key {name!r} is not one of {sorted(EXPECT_KEYS)}")
        elif name == "alias":
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                problems.append("`expect.alias` is the number of alias questions")
            else:
                out[name] = value
        elif not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            problems.append(f"`expect.{name}` must be a list of strings")
        else:
            out[name] = sorted(value)
    return out


def subject(context: Mapping[str, Any]) -> str:
    """A short, human-readable subject for a question (used in logs and ``expect``)."""
    kind = context.get("kind")
    if kind == MERGE:
        return f"{context['variant']} -> {context['canonical']}"
    if kind == NULL:
        return str(context["token"])
    if kind == SCHEMA:
        return str(context["target"])
    return str(context.get("value"))


__all__ = [
    "ALIAS", "MERGE", "NULL", "SCHEMA", "FAMILIES", "DIFFICULTIES", "DATA_DIR",
    "ColumnGold", "DatasetError", "Scenario", "file_sha256", "key", "load_split",
    "parse_dataset", "split_path", "subject",
]
