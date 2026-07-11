# FAQ

## Does CleanFrame need an API key?

No. Rules-only mode is the default and is fully offline.

## Does the LLM see my data?

Not in the default `metadata` exposure. It sees column names, types, stats, and
pattern sketches. Opt into `llm_exposure="sample"` only when you accept sending
an anonymized sample.

## Why didn't it fill my missing values?

By design. Missingness is reported; `fill_na` is never auto-proposed. Add it to
the recipe yourself if imputation is appropriate.

## Why did a recipe column get skipped?

The source column was missing from the frame. Non-strict modes warn and continue;
`strict` raises. Prefer `suggest_update` / re-plan when schema drifts.

## Can I use this in Airflow / Prefect / CI?

Yes — commit the recipe YAML and call `apply_recipe` (or the CLI `apply`
subcommand) in the task. Fail the run on `DriftError`.

## How do I handle Excel?

```bash
pip install "cleanframe[excel]"
```

## How do I handle Parquet?

```bash
pip install "cleanframe[parquet]"
```

## Is the HTML report XSS-safe?

Yes — Jinja2 autoescape is on; covered by tests.

## Will `cast: int` truncate money?

`cast` to int **rounds** floats (pandas nullable `Int64`). Prefer keeping amounts
as float, or round explicitly with the `round` op first.

## Where is the wiki?

[GitHub Wiki](https://github.com/inboxpraveen/Cleanframe/wiki) — sources live in
[`wiki/`](../wiki/) in this repository.
