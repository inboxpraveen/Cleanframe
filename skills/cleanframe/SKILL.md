---
name: cleanframe
description: Clean, fix, standardise or deduplicate messy CSV, TSV or Excel files reproducibly with CleanFrame (pip package cleanframe-engine). It profiles the file, writes a reviewable YAML cleaning recipe, replays it with deterministic pandas, quarantines invalid rows and stops on schema drift when the next file arrives. Use when the user asks to clean, fix, normalise, standardise or dedupe a spreadsheet or CSV (mixed date formats, currency text like "₹1,20,000", phone numbers, emails, inconsistent category spellings, stray whitespace, duplicate rows), to handle a recurring messy vendor, CRM or finance export, or to set up a repeatable data-cleaning step in a pipeline. Prefer it over writing one-off pandas that modifies the data.
---

# CleanFrame: clean data with a reviewable recipe, not throwaway pandas

One-off pandas written to "fix the CSV" cannot be reviewed, cannot be replayed on next
month's file, and silently does something different when the file changes shape. CleanFrame
writes the cleaning plan as a **recipe** (small YAML). Pure pandas executes it
deterministically: the same input gives the same output, every changed cell is tracked,
invalid rows are quarantined with a reason, and a replay **stops** when the file drifts.
The recipe is the artifact: the user reviews it and keeps it in git.

**Use it for:** cleaning or standardising values in a tabular file, especially a file that
recurs (monthly exports, vendor feeds) or that someone must trust.
**Not for:** analysis or reshaping (groupby, joins, pivots, charts). CleanFrame fixes values;
it does not analyse them.

## Setup

```bash
python -m cleanframe --version || pip install cleanframe-engine
pip install "cleanframe-engine[excel]"     # needed for .xlsx / .xlsm
```

The CLI is `cleanframe` (equivalently `python -m cleanframe`). To show the user the whole
workflow on sample data first, run `cleanframe demo --out-dir cleanframe-demo`. If the CleanFrame **MCP
server** is connected (tools `profile_data`, `suggest_recipe`, `apply_recipe`,
`check_drift`, `explain_recipe`, `list_capabilities`, `export_code`), use those tools for
the same workflow: they return structured JSON and refuse unsafe writes.

## Workflow

1. **Profile (read-only).** See what is wrong before changing anything:

   ```bash
   cleanframe report data.csv --out data.report.html --json        # quality score
   python -c "import cleanframe as cf; r = cf.clean('data.csv'); [print(i.severity.value, i.column, i.kind, '-', i.message) for i in r.issues]"
   ```

   `cf.clean(...)` here only plans and previews in memory; it writes nothing.

2. **Plan the recipe.** Writes only the recipe; the data file is untouched:

   ```bash
   cleanframe clean data.csv --recipe data.recipe.yaml --json
   ```

   Add `--schema target.schema.yaml` to map onto a target schema (draft one with
   `cleanframe infer-schema data.csv -o target.schema.yaml`). Use `--mode auto` for
   unattended pipelines (only higher-confidence fixes). With `--json`, stdout is one JSON
   summary (row counts, `diff`, `rows_quarantined`); the cell-level diff is on stderr.

3. **Review with the user.** Show them the recipe YAML and the diff, and summarise each
   step. To change behaviour, **edit the recipe YAML**, never the data: add a
   `normalize_values` mapping, fix a date format, or remove an op. List every op with
   `cleanframe ops --json` and every check with the Python API (`cf.list_validators()`).
   If `clean` refuses because the recipe file already exists and differs (it may hold the
   user's edits), write to a new `--recipe` path or ask the user. Do not add `--overwrite`.

4. **Apply.** Replay the recipe into a **new** output file:

   ```bash
   cleanframe apply data.csv --recipe data.recipe.yaml --out data.clean.csv \
     --quarantine data.quarantine.csv --json
   ```

   Always pass `--out`; the default is `<file>.clean.csv` next to the input. Report
   `rows_out`, `rows_quarantined` and any `warnings` from the JSON to the user.

5. **Next month's file.** Replay the same recipe:
   `cleanframe apply new.csv --recipe data.recipe.yaml --out new.clean.csv --json`.
   If it exits **3**, the file drifted (renamed, new or missing column, new date or number
   format). **Stop and tell the user.** Then:

   ```bash
   cleanframe suggest new.csv --recipe data.recipe.yaml                          # show findings (exits 3 on drift)
   cleanframe suggest new.csv --recipe data.recipe.yaml --update -o data.patched.yaml
   ```

   `--update` drafts a patch: renamed columns repointed, new date formats added; structural
   changes are only reported. Show the patch to the user. Once they approve, apply with
   `--recipe data.patched.yaml` and keep the reviewed recipe in git.

6. **Optional: plain pandas.** Export the *reviewed* recipe as a standalone script with no
   CleanFrame dependency; its `clean(df)` output equals `apply`:

   ```bash
   python -c "import cleanframe as cf; cf.CodeArtifact(cf.Recipe.load('data.recipe.yaml')).save('clean_data.py')"
   ```

   Do not use `clean --code` for this after a review: `clean` re-plans from scratch.

## Hard rules

- **Never hand-edit or overwrite the data file.** Write outputs to new paths. Never pass
  `--overwrite` unless the user explicitly asks: it allows writing over the input and
  replacing an edited recipe.
- **Prefer the recipe over ad-hoc pandas** for any cleaning that will be repeated or must be
  trusted. If you need a transform the ops cannot express, say so, or write a plugin op (below).
- **Exit code 3 means stop.** Do not re-run with `--force` or `--no-drift-check` unless the user
  approves after seeing the findings.
- **Exit code 4** means validation failed under `--mode strict` or `on_fail: error`. Report
  the failing checks. In the default `review` mode those rows are quarantined instead.
- **Quarantined rows are not deleted.** Save them with `--quarantine FILE` and tell the user
  how many there are and why (the `_cf_quarantine_reason` column).
- **Keep the recipe (and schema) in git** next to the pipeline that uses it.
- **Never impute or drop silently.** Add `fill_na` or `drop_columns` only when the user asks.
  CleanFrame reports missing values; it never fills them by itself.
- **Do not use `--llm` or `--decide`** unless the user asks. They send column metadata (and
  with `--llm-exposure sample`, values) to an external provider and need an API key. You can
  improve the recipe yourself by editing the YAML. `--decide` applies a model's answer only at
  p ≥ 0.9 and records every answer in the recipe's `meta.decisions`; show those to the user.
- Parse the `--json` output, not the human text. Global flags go **after** the subcommand:
  `cleanframe apply --plugin my_pkg.ops ...` works; `cleanframe --json apply ...` is a usage error.

## CLI quick reference

| Command | Purpose |
|---------|---------|
| `cleanframe report FILE [--out R.html] [--schema S] --json` | Profile; HTML report + quality score |
| `cleanframe clean FILE --recipe R.yaml [--schema S] [--mode review\|auto\|strict] [--out C.csv] [--quarantine Q.csv] --json` | Plan and save a new recipe (optionally write outputs too) |
| `cleanframe apply FILE --recipe R.yaml --out C.csv [--quarantine Q.csv] [--mode M] --json` | Replay a recipe behind the drift guard |
| `cleanframe suggest FILE --recipe R.yaml [--update -o P.yaml] --json` | Show drift; draft a patched recipe |
| `cleanframe infer-schema FILE -o S.yaml` | Draft a target schema |
| `cleanframe ops --json` / `cleanframe detectors --json` | List ops / detectors (including plugins) |

Read and selection flags: `--sheet NAME` (or `--sheet '#0'` for a 0-based index), `--header-row N`
(title rows above the header), `--columns A,B`, `--nrows N`, `--text` (read every field verbatim:
keeps leading zeros such as ZIP codes), `--sep`, `--encoding`. `apply --chunksize N` streams a huge
CSV. A multi-sheet `.xlsx` without `--sheet` is cleaned tab by tab into one workbook recipe.

| Exit code | Meaning |
|-----------|---------|
| 0 | OK |
| 1 | Data, recipe or output error (message in `error.message`) |
| 2 | Usage error |
| 3 | Stopped on schema drift: stop and tell the user |
| 4 | Validation failed under an erroring policy |
| 70 | Internal error (a CleanFrame bug; re-run with `--debug`) |

## Recipe cheat-sheet

```yaml
version: 1
columns:
  "Customer Name":                 # key = source column as it appears in the file
    rename_to: customer_name
    ops: [strip_whitespace, title_case]
  "Signup Date":
    rename_to: signup_date
    ops:
      - parse_date: {formats: ["%d/%m/%Y", "%Y-%m-%d"], dayfirst: true}
  Amount:
    rename_to: amount_inr
    ops: [parse_number]            # "₹1,20,000" -> 120000.0
  City:
    rename_to: city
    ops:
      - strip_whitespace
      - normalize_values: {bengaluru: Bengaluru, BLR: Bengaluru, Bombay: Mumbai}
  Email:
    rename_to: email
    ops: [normalize_email]
dedup: {subset: [email], keep: first}   # frame ops see the renamed columns
validate:                                # runs last; on_fail: quarantine | error | warn | drop | null
  - {column: email, check: valid_email, on_fail: quarantine}
  - {column: amount_inr, check: ">= 0", on_fail: quarantine}
  - {column: city, check: in, values: [Bengaluru, Mumbai, Delhi]}
```

Execution order: each column's ops top to bottom, then renames, then frame ops, then
validation. Unknown ops, misspelled parameters and duplicate keys fail when the recipe loads.
Quote YAML values such as `"no"`, `"null"` and `"010"`. Keep the `source_fingerprint`
block that `clean` writes: it powers drift detection. Full spec:
https://github.com/inboxpraveen/Cleanframe/blob/main/docs/recipe-spec.md

## Python API

```python
import cleanframe as cf

result = cf.clean("data.csv", mode="review")       # profile + plan + execute, in memory
result.recipe.save("data.recipe.yaml")              # the durable artifact
print(result.diff.render())                         # cell-level before/after
result.quarantine                                   # held rows + _cf_quarantine_reason

try:
    replay = cf.apply_recipe("next.csv", "data.recipe.yaml")   # deterministic, no LLM
except cf.DriftError as exc:
    print(exc.report.render())                      # stop and show the user
else:
    cf.write_frame(replay.dataframe, "next.clean.csv", source="next.csv")
```

`cf.clean` also takes `target_schema=`, `sheet=`, `header_row=`, `text=True`, and
`options={"dayfirst": True, "phone_country_code": "+91", "category_map": {"City": {"BLR": "Bengaluru"}}}`.

## Custom ops and detectors (plugins)

When the built-in ops cannot express a fix, add a plugin op instead of post-processing
with pandas. That keeps it in the recipe and keeps replay deterministic:

```python
# my_pkg/cleanframe_plugin.py
import re
import cleanframe as cf

_SEP = re.compile(r"[^A-Za-z0-9]")

@cf.register_op("acme_normalize_sku", streamable=True)
def acme_normalize_sku(series):
    """Strip separators and upper-case a SKU."""
    return series.map(lambda v: _SEP.sub("", v).upper() if isinstance(v, str) else v)
```

Use it in a recipe (`ops: [acme_normalize_sku]`) and load it with
`cleanframe apply --plugin my_pkg.cleanframe_plugin ...`, with `CLEANFRAME_PLUGINS=my_pkg.cleanframe_plugin`,
or with an entry point in the `cleanframe.plugins` group. Ops must be pure and deterministic. Pass
`codegen=` to make the op exportable as standalone code. Detectors (`@cf.detector`) and validators
(`@cf.validator`) work the same way. Guide:
https://github.com/inboxpraveen/Cleanframe/blob/main/docs/plugins.md
