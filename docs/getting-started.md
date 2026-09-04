# Getting started

CleanFrame profiles messy tabular data, proposes a cleanup **recipe** (YAML),
executes it with pure pandas, and lets you replay that recipe forever — with
schema-drift alerts when next month's file changes shape.

> **The LLM never touches your data. It only writes the plan.**

## Install

```bash
pip install cleanframe-engine
```

Optional extras:

```bash
pip install "cleanframe-engine[excel]"     # .xlsx / .xlsm
pip install "cleanframe-engine[parquet]"   # .parquet (pyarrow)
pip install "cleanframe-engine[llm]"       # Anthropic + OpenAI SDKs
pip install "cleanframe-engine[all]"       # everything
```

Requires **Python 3.10+**. The distribution is `cleanframe-engine`; the import
package is `cleanframe` (`import cleanframe as cf`).

## 30-second CLI demo

From a clone of this repo (or any CSV):

```bash
cleanframe report examples/messy_customers.csv
```

Opens nothing automatically — it prints the path to an HTML report
(`examples/messy_customers.report.html`) plus a quality score. Add `--open` to
open it in a browser, or `-o PATH` to choose the output path. Then clean and save
artifacts:

```bash
cleanframe clean examples/messy_customers.csv \
  --schema examples/customer.schema.yaml \
  --mode auto \
  --out-dir out/
```

`--out-dir` creates the directory and fills in four artifact paths from the input
file's stem:

| File | Contents |
|------|----------|
| `out/messy_customers.recipe.yaml` | The recipe — the durable artifact to review and commit |
| `out/messy_customers.clean.csv` | Cleaned data |
| `out/messy_customers.py` | Standalone pandas script (no CleanFrame dependency) |
| `out/messy_customers.report.html` | HTML diff report |

An explicit `--recipe` / `--out` / `--code` / `--report` overrides the path
`--out-dir` would have chosen. Quarantined rows are **not** among them — see the
next step.

## Keep the rows that failed validation

The demo ends with:

```text
⚠ 1 row(s) quarantined (pass --quarantine FILE to save them).
```

That row is not lost and not in the clean output — it is held aside. Ask for it:

```bash
cleanframe clean examples/messy_customers.csv \
  --schema examples/customer.schema.yaml \
  --mode auto \
  --out-dir out/ \
  --quarantine out/quarantined.csv
```

```text
customer_name,signup_date,amount_inr,city,email,phone,_cf_quarantine_reason
charlie brown,2024-01-01,1200.0,Mumbai,not-an-email,08012345678,email:valid_email
```

The `_cf_quarantine_reason` column names the rule that held the row
(`not-an-email` fails `valid_email`). In Python the same rows are in
`result.quarantine`. This is the default for a failing rule — CleanFrame never
silently drops a row unless you write `on_fail: drop` yourself.

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
clean_df = result.dataframe                     # 5 rows
quarantine = result.quarantine                  # 1 row that failed validation
```

## Replay next month (no LLM)

```bash
cleanframe apply new_customers.csv \
  --recipe customer.recipe.yaml \
  --out clean.csv
```

Add `--quarantine q.csv` here too; without it, `apply` prints the count and drops
the rows from the output. If columns renamed or formats drifted, `apply` stops
with exit code `3` rather than cleaning with a stale recipe:

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
