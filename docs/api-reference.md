# Python API reference

Import the public surface as:

```python
import cleanframe as cf
```

Full export list: `cf.__all__`. Below are the calls most applications use.

---

## `cf.clean(data, **kwargs) → CleanResult`

Profile, plan, and clean.

| Parameter | Type | Default | Notes |
|-----------|------|---------|-------|
| `data` | DataFrame \| path | required | CSV/TSV/Excel/Parquet/JSON via `read_frame` |
| `target_schema` / `schema` | Schema \| path \| dict | `None` | Drives mapping + validations |
| `llm` | `None` \| `"provider/model"` \| client | `None` | Rules-only when omitted |
| `mode` | `"review"` \| `"auto"` \| `"strict"` | `"review"` | |
| `options` | `dict` \| `None` | `None` | Detector knobs + `max_diff_changes` |
| `planner` | Planner \| `None` | `None` | Custom planner object with a `.plan()` method |
| `max_tokens_budget` | int \| None | `None` | Hard LLM token cap |
| `llm_exposure` | `"metadata"` \| `"sample"` \| `"none"` | `"metadata"` | What the model may see |
| `llm_fallback` | `bool` | `True` | `False` raises instead of degrading to the rules planner when an LLM call fails |
| `source` | `str` \| `None` | `None` | Label recorded in the recipe / report for a DataFrame input |
| `sheet` | `str` \| `int` \| `None` | `None` | Excel sheet name, or 0-based index (file input) |
| `columns` | `list[str]` \| `None` | `None` | `usecols` filter — keeps file order, not a reorder |
| `nrows` | `int` \| `None` | `None` | Read only the first N data rows (file input) |
| `skiprows` | `int` \| `list[int]` \| `None` | `None` | Data rows to drop — an int drops the first N and keeps the header, a list names 1-based data rows. Diff `row_id` is slice-relative |
| `header_row` | `int` \| `None` | `None` | 0-based line (CSV) / row (Excel) that holds the column names, for files with title rows above the header. Everything above it is skipped; recorded in the recipe `read:` section |
| `correct_format` | `bool` | `True` | CSV encoding + delimiter auto-detect, pinned to `read:`; ambiguous delimiter raises (CLI `--no-correct`) |
| `text` | `bool` | `False` | Read every field verbatim — keeps leading zeros, literal `NA`/`None` text, `1e5` exact |
| `sep` | `str` \| `None` | `None` | Field delimiter, overriding auto-detection |
| `encoding` | `str` \| `None` | `None` | File encoding, overriding auto-detection |

`sheet` / `columns` / `nrows` / `skiprows` / `header_row` are also accepted by `report`, `apply_recipe`, `suggest_update`, `infer_schema`; `text` / `sep` / `encoding` by `report`, `apply_recipe`, `infer_schema`; `correct_format` by `report` and `infer_schema`. On a DataFrame input `columns` projects while `sheet` / `nrows` / `skiprows` / `header_row` raise (file-only).

`llm_exposure="none"` plans with the rules planner and makes no network call, even when `llm=` is set.

```python
result = cf.clean(df, target_schema="schema.yaml", mode="auto")
result.dataframe
result.recipe
result.diff
result.quarantine
result.issues
result.quality
result.log
result.code          # CodeArtifact — standalone pandas
```

`result.dataframe` is indexed `0..n-1` — the stable positional row ids the diff and quarantine refer to.

Useful `options` keys:

| Key | Effect |
|-----|--------|
| `max_diff_changes` | Cap stored cell diffs (`None` = unlimited; default 100_000) |
| `dayfirst` | Date ambiguity preference for the dates detector |
| `phone_country_code` | Default country code for phone normalisation |
| `region` | Alias for `phone_country_code` |
| `category_map` | Seed alias map `{column: {variant: canonical}}` |
| `rename_columns` | `False` skips the automatic snake_case pass over every column; detector- and schema-proposed renames still apply (default `True`) |

---

## `cf.report(data, **kwargs) → Report`

Profile + detect only; returns an HTML report object. Takes `schema`, `options`, `source`, the selection arguments, and `correct_format` / `text` / `sep` / `encoding`.

```python
rep = cf.report("data.csv")
rep.save("report.html")
html = rep.html
rep.quality
```

---

## `cf.apply_recipe(data, recipe, **kwargs) → CleanResult`

Replay without re-planning.

| Parameter | Default | Notes |
|-----------|---------|-------|
| `check_drift` | `True` | Compare fingerprint |
| `on_drift` | `"error"` | `"error"` \| `"warn"` \| `"ignore"`; anything else raises rather than silently disabling the guard |
| `mode` | `"review"` | `strict` always raises on drift |
| `source` | `None` | Label for a DataFrame input |
| `sheet` / `columns` / `nrows` / `skiprows` / `header_row` | `None` | Override the recipe's recorded `read:` selection |
| `text` / `sep` / `encoding` | `False` / `None` / `None` | Override the recipe's recorded `read:` format |

The recipe's `read:` binding is re-applied when `data` is a path; explicit call arguments win over it.

The returned frame keeps the surviving rows' positions: rows removed by `dedup` or quarantined by validation leave gaps (e.g. `[0, 1, 3, 4]`), so `result.dataframe.index` is not `0..n-1` after a drop. Call `.reset_index(drop=True)` if you need that. `diff.row_id` and `diff.dropped_rows` refer to these original positions.

Drift also covers **number formats**: if a column the recipe parses with `parse_number` receives values that no longer fit its `decimal`/`thousands` convention (a European `€1.200,50` against a `.`-decimal recipe), replay stops with a `number_format_drift` finding instead of producing a wrong number.

---

## `cf.suggest_update(data, recipe, *, out=None, **kwargs) → (Recipe, DriftReport)`

Mechanical drift patches (repoint renamed columns, extend date formats). Date formats are learned **only from the values the recipe cannot parse today**, so a value that already parses is never re-read differently; if no known format fits the new values the patch says so instead of guessing. Number-format drift is reported but not auto-patched — set the op's `decimal`/`thousands` yourself. Also takes `source`, `sheet`, `columns`, `nrows`, `skiprows`, `header_row`; the recipe's recorded `read:` binding is re-applied so a workbook or `;`-separated file is read the way it was planned. `out=` writes the patched recipe.

---

## `cf.infer_schema(df, name=None, **kwargs) → Schema`

Draft schema from observed types / categories. Takes the selection arguments plus `correct_format`, `text`, `sep`, `encoding`.

---

## `cf.execute(recipe, df, mode=..., max_diff_changes=...) → ExecutionResult`

Low-level deterministic replay (used by `clean` / `apply_recipe`).

---

## `cf.generate_code(recipe, func_name="clean", *, allow_partial=False) → str`

Render a recipe to a standalone pandas module defining `func_name(df)`. Raises when the recipe uses a custom op or check the exporter cannot reproduce; `allow_partial=True` accepts a partial export whose gaps are marked with `# NOTE` comments.

---

## Multi-sheet workbooks

Clean every sheet of an `.xlsx` independently — one `Recipe` + diff per sheet.

| Call | Returns | Notes |
|------|---------|-------|
| `cf.clean_workbook(data, *, sheets=None, target_schema=None, schema=None, llm=None, mode="review", options=None, **clean_kwargs)` | `WorkbookResult` | `sheets=` limits which tabs; `**clean_kwargs` forward to `clean` |
| `cf.apply_workbook(data, recipe, *, mode="review", check_drift=True, on_drift="error")` | `WorkbookResult` | Replay a `WorkbookRecipe` across sheets |
| `cf.read_workbook(path, sheets=None)` | `dict[str, DataFrame]` | |
| `cf.load_recipe(path)` | `Recipe` \| `WorkbookRecipe` | Auto-detects a `sheets:` block |

```python
wb = cf.clean_workbook("book.xlsx", target_schema="schema.yaml")
wb.sheets            # dict name -> CleanResult
wb.untouched         # dict name -> DataFrame, the sheets left unchanged
wb.frames            # dict name -> DataFrame, cleaned sheets only
wb.sheet_order       # sheet names in workbook order
wb.recipe            # WorkbookRecipe
wb.summary()
wb.save_recipe("book.recipe.yaml")
wb.save_data("out.xlsx")             # every sheet to one .xlsx; refuses to overwrite the source (overwrite=True to force)
```

`save_data` writes cleaned sheets where cleaned and the untouched originals otherwise, in `sheet_order`.

`WorkbookRecipe` is one reviewable YAML — `version: 2` with a top-level `sheets:` mapping (sheet name -> a normal recipe); exposes `.sheets`, `.save(path)`, `.load(path)`. A per-sheet recipe never carries its own `read.sheet` — the dict key is the sheet.

A multi-sheet workbook with no sheet selected raises `CleanFrameError` listing the sheet names — `read_frame` / `clean` / `report` / `apply` / `infer_schema` never silently read sheet 1. The CLI auto-routes a multi-sheet `.xlsx` to workbook mode.

---

## Out-of-core streaming

Replay a **row-independent** recipe over a CSV in chunks — peak memory is bounded by `chunksize`, not file size.

```python
summary = cf.stream_apply(recipe, "big.csv", "clean.csv", chunksize=100_000)
summary.rows_in, summary.rows_out, summary.changed_cells
summary.rows_dropped, summary.rows_quarantined, summary.chunks
print(summary.render())
```

| Parameter | Default | Notes |
|-----------|---------|-------|
| `chunksize` | `100_000` | Peak memory ≈ one chunk |
| `mode` | `"review"` | |
| `check_drift` / `on_drift` | `True` / `"error"` | Drift checked over every row (first pass) before any output is written |
| `quarantine_path` | `None` | Where to write quarantined rows; without it they are counted and warned about, not saved |

`cf.check_streamable(recipe)` is the pre-flight. GLOBAL ops are refused with a named `CleanFrameError`: `dedup`; `fill_na` with `mean`/`median`/`mode`/`ffill`/`bfill`; `cast` to `category`/`datetime`/`date`; `parse_date` without explicit formats; the `unique` validator; and any unknown custom op (default-DENY).

CLI: `cleanframe apply FILE --recipe R --chunksize N [--out O]` streams; global-op recipes print a clear refusal.

---

## IO

```python
cf.read_frame("data.parquet")
cf.read_frame("data.csv", text=True)          # every field verbatim
cf.read_frame("data.csv", blank_lines=2)      # empty lines above the header
cf.write_frame(df, "out.csv")                 # formula-safe by default
cf.write_frame(df, "out.csv", sanitize_csv=False)
cf.write_frame(df, "out.csv", source="in.csv", overwrite=True)
```

| Parameter | Function | Default | Notes |
|-----------|----------|---------|-------|
| `sheet` / `columns` / `nrows` / `skiprows` | `read_frame` | `None` | As for `clean` |
| `blank_lines` | `read_frame` | `0` | Empty lines to skip above the header row |
| `text` | `read_frame` | `False` | Read every field as a string (CSV/Excel) |
| `sanitize_csv` | `write_frame` | `True` | Escape cells that look like spreadsheet formulas |
| `source` | `write_frame` | `None` | The path the data was read from |
| `overwrite` | `write_frame` | `False` | Required to write back over `source` |

`write_frame` refuses to overwrite the file named by `source=` unless `overwrite=True`, and writes through a temporary sibling that is moved into place, so a failure leaves the previous file intact rather than truncated.

---

## Plugins

```python
@cf.detector("iban", priority=45)
def detect_iban(series, ctx): ...

@cf.register_op("my_op", scope="column", streamable=True,
                codegen=lambda params, col: [f"df[{col!r}] = df[{col!r}].str.strip()"])
def my_op(series, **params): ...

@cf.validator("valid_iban")
def _(series): ...

cf.load_plugins(["my_pkg.cleanframe_plugin"])   # import plugin modules (idempotent)
```

`register_op(..., codegen=, helpers=, streamable=)` are opt-in: an op that declares neither is refused by
`result.code` and by `stream_apply` rather than silently mishandled. `cf.load_plugins(modules=(), *, discover=True)`
imports the named modules plus every installed `cleanframe.plugins` entry point and everything in
`CLEANFRAME_PLUGINS`. See the [plugin guide](plugins.md), [Detectors & ops](detectors-and-ops.md) and
[`CONTRIBUTING.md`](../CONTRIBUTING.md).

The library logs through the standard `logging` module under the `cleanframe` logger (a `NullHandler` by default):

```python
import logging
logging.getLogger("cleanframe").setLevel(logging.INFO)   # or run the CLI with --verbose
```

---

## Other exports

The remaining names in `cf.__all__`:

| Name | What it is |
|------|------------|
| `__version__` | Installed version string |
| `Mode` | Enum of `review` / `auto` / `strict` |
| `Severity` | Ordered severity for detected issues and validation results |
| `Op` | One step of a recipe: an op `name` plus its `params` |
| `LLMExposure` | Enum of how much data an LLM planner may see: `none` / `metadata` / `sample` |
| `Issue` | One detected problem, optionally with a proposed fix |
| `Issues` | Ordered, list-like collection of `Issue` (`result.issues`) |
| `Proposal` | A replayable fix for an issue: an optional rename plus ops |
| `DetectorContext` | What a detector receives: `df`, `series`, `column`, `profile`, `column_profile`, `schema`, `options` |
| `run_detectors(df, *, profile=, schema=, options=, only=)` | Run every applicable detector and return the aggregated `Issues` |
| `list_detectors()` | Registered detector names |
| `list_ops(scope=None)` | Registered op names, optionally filtered to `column` or `frame` |
| `list_validators()` | Registered validator names |
| `profile_dataframe(df)` | Profile every column plus frame-level facts (row count, duplicate rows) |
| `DataFrameProfile` | The column profiles plus those frame-level facts |
| `ColumnProfile` | Read-only statistics and a semantic-type guess for one column |
| `ColumnRecipe` | The plan for one source column: an optional rename plus ordered ops |
| `ValidationRule` | One post-clean check with a failure policy |
| `SchemaColumn` | One column of a target schema: name, logical type, constraints |
| `RulesPlanner` | Deterministic planner — no LLM, no network; the default path |
| `LLMPlanner` | Planner that asks an LLM for the recipe |
| `get_client(spec)` | Resolve a `"provider/model"` string to an LLM client |
| `list_providers()` | Canonical LLM provider names |
| `plan_recipe(df, ...)` | Profile + detect + plan in one call, rules planner by default |
| `CellDiff` | Structured record of everything a recipe changed (`result.diff`) |
| `CellChange` | One change: `row_id`, `column`, `before`, `after` |
| `compute_diff(original, cleaned, lineage, ...)` | Build a `CellDiff` from a before/after pair |
| `detect_drift(df, recipe, *, source=None)` | Compare an incoming frame against a recipe's expectations |
| `DriftFinding` | One drift observation: `kind`, `message`, `severity`, `column`, `suggestion`, `evidence` |
| `quality_score(profile, issues)` | Score a profile + issues into a `QualityScore` |
| `QualityScore` | `score`, `grade`, `label`, `color`, `penalty` (`result.quality`) |
| `StreamSummary` | Counts from a streamed replay (no cell-level diff is kept) |

---

## Errors

`CleanFrameError` is the base class for every error CleanFrame raises deliberately, so `except cf.CleanFrameError` catches all of them without swallowing unrelated bugs.

| Exception | When |
|-----------|------|
| `CleanFrameError` | Base / IO |
| `RecipeError` | Invalid recipe / check |
| `OpError` | Op params / execution |
| `ExecutionError` | Strict missing column, rename clash |
| `ValidationFailure` | `on_fail=error` / strict; carries `.failures` |
| `DriftError` | Schema drift on apply; carries `.report` |
| `SchemaError` | Bad schema YAML |
| `LLMError` | LLM path (may fall back to rules) |
| `BudgetExceeded` | Subclass of `LLMError` — planning would exceed `max_tokens_budget` |
| `OutputError` | An output file could not be written (bad path, permissions, missing engine, or an in-place overwrite without `overwrite=True`) |

## Warnings

Every advisory CleanFrame emits uses the `CleanFrameWarning` category, so callers can filter them in one line:

```python
import warnings
import cleanframe as cf
warnings.simplefilter("ignore", cf.CleanFrameWarning)
```
