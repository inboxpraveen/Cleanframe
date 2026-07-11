# Contributing to CleanFrame

Thank you for helping make messy data reproducible. This guide is short on purpose.
Read the **Invariants** section once — everything else follows from it.

CleanFrame's whole promise is: *AI writes the recipe once; pure pandas replays it
forever, deterministically.* A change that quietly breaks that promise is worse
than no change at all. The rules below exist to protect it.

---

## The five invariants (do not break these)

1. **Determinism.** The same input must always produce the same recipe, the same
   cleaned frame, the same diff — on any machine, in any process. That means:
   - No `set` iteration where order matters, no reliance on dict ordering from an
     unstable source, no `random`, no clock, no network in the core path.
   - Sort before you iterate when order is observable. Break ties explicitly
     (see `_canonical` in `detectors/categories.py` for the pattern).
   - There is a test for this (`tests/test_executor_diff.py::test_execution_is_deterministic`).
     Add one for anything new that could vary.

2. **The LLM never touches your data.** The LLM planner (`llm.py`) sends *metadata
   and value pattern sketches only* (unless the caller explicitly opts into a
   sample). It returns a recipe that is parsed through the *same* `Recipe` model
   the rules planner uses. If you extend the LLM path, the model must never receive
   raw cell values by default, and its output must go through `Recipe.from_dict`
   (which validates it) — never straight to the executor.

3. **Recipes round-trip losslessly and idempotently.** `Recipe.from_yaml(r.to_yaml())`
   must equal `r`, and applying `to_yaml` twice must be byte-identical. If you add
   an op with parameters, you must add a `coerce`/`compact` pair such that
   `coerce(compact(params)) == params` (see the op registry contract below).

4. **Nothing is silently imputed or dropped.** Missing values are *reported*, never
   filled unless a human puts `fill_na` in the recipe. Validation failures go to a
   *quarantine* frame with a reason — never deleted. Outliers are flagged, never
   "fixed." When you must bound coverage (top-N, sampling), surface it.

5. **Every changed cell is tracked.** The executor assigns a stable row id and
   tracks column lineage so `CellDiff` can attribute every change. If you add a
   transform that adds/removes/reorders columns or rows, make sure the lineage in
   `executor.py` stays correct and the diff still reconciles.

If a change would weaken one of these, open an issue to discuss it first.

---

## Architecture in one screen

```
DataFrame ─▶ profile.py ─▶ detectors/ ─▶ planner.py ─▶ Recipe (recipe.py)
             (semantic     (Issues +      (assemble +        │
              types)        Proposals)     order + gate)      ▼
                                                       executor.py  (pure pandas)
                                                             │
                              ┌──────────────────────────────┼───────────────┐
                              ▼              ▼                ▼               ▼
                         validate.py     diff.py          drift.py       report.py
                         (quarantine)   (cell diff)     (replay guard)    (HTML)
```

- **`ops.py`** — the vocabulary. Every recipe op is a pure function of a pandas
  object registered here. The deterministic heart.
- **`detectors/`** — plugins that find problems and *propose* fixes (ops + renames).
  Domain knowledge lives here.
- **`planner.py`** — turns proposals into a recipe: applies mode policy, resolves
  renames, and — critically — orders ops via `OP_ORDER`.
- **`executor.py`** — replays a recipe in fixed phases with lineage tracking.
- **`recipe.py` / `schema.py`** — the durable, human-reviewable artifacts.
- **`llm.py`** — optional; writes a recipe from metadata only.

The dependency direction is strictly downward (detectors import ops, planner
imports detectors, etc.). Don't introduce an upward import — it will create a
cycle. When you need a lower layer from a higher one at call time, use a local
import inside the function (there are a few examples already).

---

Demo data lives in `examples/messy_customers.csv` — plus a sample schema and
recipe (`customer.schema.yaml`, `customer.recipe.yaml`). Full docs:
[`docs/`](docs/) and the [Wiki](https://github.com/inboxpraveen/Cleanframe/wiki).

---

## Getting set up

```bash
git clone https://github.com/inboxpraveen/Cleanframe.git
cd Cleanframe
pip install -e ".[dev]"      # editable install + pytest + openpyxl + ruff
pytest                       # full suite, runs in a few seconds
```

Windows note: the code and tests handle currency symbols (`₹`, `€`). If your
console mangles them, run with `PYTHONUTF8=1`. The CLI sets UTF-8 output itself.

---

## The most common contribution: a new detector

Detectors are the point of the plugin system — the community owns the long tail of
messy-data weirdness. A detector is ~15 lines.

```python
# cleanframe/detectors/iban.py
import re
import pandas as pd
from ..issues import Issues
from ..types import Op, Severity
from .base import DetectorContext, detector

IBAN_RE = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{11,30}$")

@detector("iban", priority=45)                 # lower priority runs earlier
def detect_iban(series: pd.Series, ctx: DetectorContext) -> Issues:
    """One-line summary — shows up in `cleanframe detectors`."""
    issues = Issues()
    cp = ctx.column_profile
    if cp is None or cp.semantic_type not in ("text", "id"):
        return issues                          # cheap early-out for irrelevant columns
    bad = [v for v in series.dropna() if isinstance(v, str) and not IBAN_RE.match(v.replace(" ", ""))]
    if bad:
        issues.add(
            "invalid_iban",
            f"{len(bad)} value(s) are not valid IBANs",
            severity=Severity.WARNING,
            confidence=1.0,
            evidence={"count": len(bad), "examples": bad[:5]},
            ops=[Op("remove_symbols", {"symbols": [" "]})],   # optional proposed fix
        )
    return issues
```

Then register it by importing the module in `cleanframe/detectors/__init__.py`.

Rules for a good detector:

- **Return `Issues`.** Use `issues.add(...)`; attach a fix with `ops=[...]` and/or
  `rename_to=...` only when you're confident. `confidence` gates inclusion by mode
  (`strict` needs ≥ 0.85, `auto` ≥ 0.65, `review` ≥ 0.5).
- **Be deterministic.** Sort before iterating; break ties explicitly. Never let row
  order change your output (test it — feed the column shuffled and assert the same
  proposal).
- **Don't over-reach.** Propose only *safe, reversible* fixes automatically. Domain
  guesses (e.g. "BLR means Bangalore") belong to the LLM or the human, not rules.
- **Early-out** on columns that aren't yours (check `ctx.column_profile.semantic_type`).
- The signature can be `(series)` or `(series, ctx)` — the runner passes `ctx` if you
  declare it. Frame-level detectors use `@detector("name", scope="frame")` and take
  the whole `df`.

Add a test in `tests/test_profile_detectors.py` asserting your detector fires (and
does *not* fire on clean data).

---

## Adding an op (a recipe transform)

Ops must be pure and deterministic. If your op takes parameters, you owe a
`coerce`/`compact` pair so recipes stay minimal *and* round-trip losslessly.

```python
@register_op(
    "titlecase_words",
    scope="column",                                  # or "frame"
    coerce=lambda raw: {"min_len": (raw or {}).get("min_len", 2)},   # recipe form -> params
    compact=lambda p: _prune(p, {"min_len": 2}),      # params -> minimal recipe form
)
def titlecase_words(series, min_len=2):
    """Docstring first line shows in `cleanframe ops`."""
    return _apply_str(series, lambda s: " ".join(w.title() if len(w) >= min_len else w for w in s.split()))
```

Then, if the planner should ever emit it, add its name to **`OP_ORDER`** in
`planner.py` at the correct position. `OP_ORDER` is the single source of truth for
op sequencing — getting the position right is how independently-written detectors
compose safely (whitespace before categories, currency split before symbol
stripping, casing last). If your op isn't ordered, it runs after all ordered ops.

Column ops return a `Series` (or a `ColumnOpResult` if they also emit new columns —
see `extract_currency`). Frame ops return a `DataFrame` and **must preserve the row
index** of surviving rows, or the cell diff will misattribute changes.

Required test: `coerce(compact(params)) == params` (the parametrized test in
`tests/test_ops.py` will pick your op up automatically if you give it a sample).

---

## Adding a validator

```python
from cleanframe.validate import validator

@validator("valid_iban")
def _valid_iban(series):
    return series.isna() | series.astype(str).str.replace(" ", "").str.match(IBAN_RE)  # True = passes
```

Return a boolean pass-mask (`True` means the value is fine; NaN should usually
pass — `not_null` is the check for missingness). `on_fail` policy is handled for
you.

---

## Testing & quality bar

- **Every PR keeps `pytest` green.** New behavior needs a new test.
- **Test the invariant, not just the happy path.** For anything order-sensitive, add
  a determinism assertion. For a new op with params, add the round-trip.
- Prefer small, focused tests using the fixtures in `tests/conftest.py`.
- Run `ruff check cleanframe` if you have it (config is in `pyproject.toml`); it's
  advisory, not blocking.

## Style

- Match the surrounding code: type hints, `from __future__ import annotations`,
  docstrings that explain *why* (the reader can see *what*).
- Target Python 3.10+. Keep the core dependencies minimal (pandas, numpy, pyyaml,
  jinja2). Anything heavier goes in an optional extra in `pyproject.toml`.
- No `print` in library code — return data or `log` to a list. `print` is for the
  CLI only.

## Pull requests

1. Branch from `main`. One logical change per PR.
2. Describe the user-visible behavior change and which invariant(s) you considered.
3. If you touched the recipe format, the executor, or `OP_ORDER`, say so
   prominently — those are the load-bearing walls.
4. Be kind in review. We're all here to make one messy CSV less painful.

Questions or a detector idea? Open an issue tagged `good-first-detector`.
