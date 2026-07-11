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
