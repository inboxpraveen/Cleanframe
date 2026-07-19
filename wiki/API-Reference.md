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
| `options` | dict | `{}` | Detector knobs + `max_diff_changes` |
| `max_tokens_budget` | int \| None | `None` | Hard LLM token cap |
| `llm_exposure` | `"metadata"` \| `"sample"` \| `"none"` | `"metadata"` | What the model may see |
| `sheet` | `str` \| `int` \| `None` | `None` | Excel sheet name / 0-based index (file input) |
| `columns` | `list[str]` \| `None` | `None` | `usecols` filter — keeps file order, not a reorder |
| `nrows` / `skiprows` | `int` \| `None` | `None` | Row slice (file input); diff `row_id` is slice-relative |
| `correct_format` | `bool` | `True` | CSV encoding + delimiter auto-detect, pinned to `read:`; ambiguous delimiter raises (CLI `--no-correct`) |

`sheet` / `columns` / `nrows` / `skiprows` are also accepted by `report`, `apply_recipe`, `infer_schema`; `correct_format` by `report`. On a DataFrame input `columns` projects while `sheet` / `nrows` / `skiprows` raise (file-only).

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

Useful `options` keys:

| Key | Effect |
|-----|--------|
| `max_diff_changes` | Cap stored cell diffs (`None` = unlimited; default 100_000) |
| `dayfirst` | Date ambiguity preference for the dates detector |
| `phone_country_code` | Default country code for phone normalisation |
| `category_map` | Seed alias map `{column: {variant: canonical}}` |

---

## `cf.report(data, **kwargs) → Report`

Profile + detect only; returns an HTML report object.

```python
rep = cf.report("data.csv")
rep.save("report.html")
html = rep.html
```

---

## `cf.apply_recipe(data, recipe, **kwargs) → CleanResult`

Replay without re-planning.

| Parameter | Default | Notes |
|-----------|---------|-------|
| `check_drift` | `True` | Compare fingerprint |
| `on_drift` | `"error"` | `"error"` \| `"warn"` \| `"ignore"` |
| `mode` | `"review"` | `strict` always raises on drift |

---

## `cf.suggest_update(data, recipe, out=None) → (Recipe, DriftReport)`

Mechanical drift patches (repoint renamed columns, extend date formats).

---

## `cf.infer_schema(data, name=None) → Schema`

Draft schema from observed types / categories.

---

## `cf.execute(recipe, df, mode=..., max_diff_changes=...) → ExecutionResult`

Low-level deterministic replay (used by `clean` / `apply_recipe`).

---

## Multi-sheet workbooks

Clean every sheet of an `.xlsx` independently — one `Recipe` + diff per sheet.

| Call | Returns | Notes |
|------|---------|-------|
| `cf.clean_workbook(data, *, sheets=None, target_schema=None, schema=None, llm=None, mode="review", options=None)` | `WorkbookResult` | `sheets=` limits which tabs |
| `cf.apply_workbook(data, recipe, *, mode="review", check_drift=True, on_drift="error")` | `WorkbookResult` | Replay a `WorkbookRecipe` across sheets |
| `cf.read_workbook(path, sheets=None)` | `dict[str, DataFrame]` | |
| `cf.load_recipe(path)` | `Recipe` \| `WorkbookRecipe` | Auto-detects a `sheets:` block |

```python
wb = cf.clean_workbook("book.xlsx", target_schema="schema.yaml")
wb.sheets            # dict name -> CleanResult
wb.untouched         # sheet names left unchanged
wb.frames            # dict name -> DataFrame (cleaned or untouched)
wb.recipe            # WorkbookRecipe
wb.summary()
wb.save_recipe("book.recipe.yaml")
wb.save_data("out.xlsx")             # every sheet to one .xlsx; refuses to overwrite the source (overwrite=True to force)
```

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
| `check_drift` / `on_drift` | `True` / `"error"` | Drift checked on a bounded head sample |
| `quarantine_path` | `None` | Write dropped / quarantined rows |

`cf.check_streamable(recipe)` is the pre-flight. GLOBAL ops are refused with a named `CleanFrameError`: `dedup`; `fill_na` with `mean`/`median`/`mode`/`ffill`/`bfill`; `cast` to `category`/`datetime`/`date`; `parse_date` without explicit formats; the `unique` validator; and any unknown custom op (default-DENY).

CLI: `cleanframe apply FILE --recipe R --chunksize N [--out O]` streams; global-op recipes print a clear refusal.

---

## IO

```python
cf.read_frame("data.parquet")
cf.write_frame(df, "out.csv")                 # formula-safe by default
cf.write_frame(df, "out.csv", sanitize_csv=False)
```

---

## Plugins

```python
@cf.detector("iban", priority=45)
def detect_iban(series, ctx): ...

@cf.register_op("my_op", scope="column", ...)
def my_op(series, **params): ...

@cf.validator("valid_iban")
def _(series): ...
```

See [Detectors & ops](Detectors-and-Ops) and [`CONTRIBUTING.md`](https://github.com/inboxpraveen/Cleanframe/blob/main/CONTRIBUTING.md).

---

## Errors

| Exception | When |
|-----------|------|
| `CleanFrameError` | Base / IO |
| `RecipeError` | Invalid recipe / check |
| `OpError` | Op params / execution |
| `ExecutionError` | Strict missing column, rename clash |
| `ValidationFailure` | `on_fail=error` / strict |
| `DriftError` | Schema drift on apply |
| `SchemaError` | Bad schema YAML |
| `LLMError` / `BudgetExceeded` | LLM path (may fall back to rules) |
