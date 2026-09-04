# FAQ

## Does CleanFrame need an API key?

No. Rules-only mode is the default and is fully offline.

## Does the LLM see my data?

Not in the default `metadata` exposure. It sees column names, types, stats, and
pattern sketches. `llm_exposure="sample"` also sends up to 5 example values per
column: emails and phones are replaced and long strings become patterns, but
short category/text values go verbatim. `llm_exposure="none"` makes no network
call at all.

## Why didn't it fill my missing values?

By design. Missingness is reported; `fill_na` is never auto-proposed. Add it to
the recipe yourself if imputation is appropriate.

## Why did a recipe column get skipped?

The source column was missing from the frame. Non-strict modes warn and continue;
`strict` raises. Prefer `suggest_update` / re-plan when schema drifts.

## Why did my ZIP code lose its leading zeros?

pandas inferred the column as numeric **while reading the file**, so `01234`
became `1234` before CleanFrame saw it — which is why no diff shows the change.
The same coercion turns literal `NA`/`None` text into a missing value and
rewrites `1e5`. `clean`/`report` re-read a bounded verbatim slice and warn naming
the column, e.g.:

```text
⚠ pandas type inference changed values while reading (Zip: '01234' lost its
  leading zero(s) and became 1234). Pass text=True to read every field verbatim.
```

Fix it by reading verbatim:

```bash
cleanframe clean data.csv --text --out-dir out/
```

```python
cf.clean("data.csv", text=True)
```

`text=True` is recorded as `text: true` in the recipe's `read:` section, so
`apply_recipe` replays the same read. Use it for any identifier-like column —
ZIP codes, account numbers, SKUs, part numbers.

## How do I silence CleanFrame's warnings?

Every advisory uses the `CleanFrameWarning` category, so one filter covers them
all:

```python
import warnings
import cleanframe as cf

warnings.simplefilter("ignore", cf.CleanFrameWarning)
```

Use `"error"` instead of `"ignore"` to make advisories fatal in CI. Filtering by
category leaves unrelated Python warnings alone.

## Can I use this in Airflow / Prefect / CI?

Yes — commit the recipe YAML and call `apply_recipe` (or the CLI `apply`
subcommand) in the task. Fail the run on `DriftError`. From a shell, branch on the
exit code: `3` is drift, `4` is validation failure.

## How do I handle Excel?

```bash
pip install "cleanframe-engine[excel]"
```

That covers `.xlsx` / `.xlsm`. A legacy `.xls` needs `pip install xlrd`; writing
`.xls` is refused — write `.xlsx`.

## How do I handle Parquet?

```bash
pip install "cleanframe-engine[parquet]"
```

## How do I clean an Excel file with multiple sheets?

Multi-sheet workbooks now raise if no sheet is chosen. Use
`cf.clean_workbook(path)` or `cleanframe clean file.xlsx` to clean every tab
(one recipe + diff per sheet), or pass `sheet=` to pick one.

## Can it handle files bigger than memory?

Yes — `cf.stream_apply(recipe, in_path, out_path, chunksize=N)` (CLI
`apply FILE --recipe R --chunksize N`) replays row-independent recipes
out-of-core. Global ops (dedup, `fill_na` mean/median/…) are refused; peak
memory is bounded by the chunk size, not file size.

## It read my CSV as one column / wrong encoding?

Read-time format auto-correction detects the delimiter and encoding by default
and pins them into the recipe. Pass `--no-correct` (`correct_format=False`) to
disable; an ambiguous delimiter raises rather than guessing.

## Is the HTML report XSS-safe?

Yes — Jinja2 autoescape is on; covered by tests.

## Will `cast: int` truncate money?

`cast` to int **rounds** floats (pandas nullable `Int64`). Prefer keeping amounts
as float, or round explicitly with the `round` op first.

## Where is the wiki?

[GitHub Wiki](https://github.com/inboxpraveen/Cleanframe/wiki) — sources live in
[`wiki/`](https://github.com/inboxpraveen/Cleanframe/blob/main/wiki/) in this repository.
