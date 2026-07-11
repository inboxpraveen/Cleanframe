# Getting started

CleanFrame profiles messy tabular data, proposes a cleanup **recipe** (YAML),
executes it with pure pandas, and lets you replay that recipe forever — with
schema-drift alerts when next month's file changes shape.

> **The LLM never touches your data. It only writes the plan.**

## Install

```bash
pip install cleanframe
```

Optional extras:

```bash
pip install "cleanframe[excel]"     # .xlsx
pip install "cleanframe[parquet]"   # .parquet (pyarrow)
pip install "cleanframe[llm]"       # Anthropic + OpenAI SDKs
pip install "cleanframe[all]"       # everything
```

Requires **Python 3.10+**.

## 30-second CLI demo

From a clone of this repo (or any CSV):

```bash
cleanframe report examples/messy_customers.csv
```

Opens nothing automatically — it prints a path to an HTML report with issues and
a quality score. Then clean and save artifacts:

```bash
cleanframe clean examples/messy_customers.csv \
  --schema examples/customer.schema.yaml \
  --mode auto \
  --out-dir out/
```

## 30-second Python demo

```python
import pandas as pd
import cleanframe as cf

df = pd.read_csv("examples/messy_customers.csv")

result = cf.clean(
    df,
    target_schema="examples/customer.schema.yaml",  # optional
    # llm="anthropic/claude-sonnet-4-6",            # optional
    mode="review",  # review | auto | strict
)

result.diff.show()                              # cell-level before/after
result.recipe.save("customer.recipe.yaml")      # durable artifact
result.code.save("clean_customers.py")          # plain pandas, no CleanFrame dep
clean_df = result.dataframe
quarantine = result.quarantine                  # rows that failed validation
```

## Replay next month (no LLM)

```bash
cleanframe apply new_customers.csv \
  --recipe customer.recipe.yaml \
  --out clean.csv
```

If columns renamed or formats drifted:

```bash
cleanframe suggest new_customers.csv \
  --recipe customer.recipe.yaml \
  --update
```

## What "mode" means

| Mode | Confidence gate | Typical use |
|------|-----------------|-------------|
| `review` | ≥ 0.50 | Explore; review the recipe before trusting it |
| `auto` | ≥ 0.65 | Pipelines where proposals are usually safe |
| `strict` | ≥ 0.85 | Fail loud: missing columns, validation, drift |

## Next steps

- [Concepts](concepts.md) — recipes, quarantine, drift
- [Production guide](production.md) — large files, CI, safety
- [Recipe specification](recipe-spec.md) — edit recipes by hand
- [LLM planning](llm.md) — optional AI-assisted planning
