# CLI reference

Entry point: `cleanframe` (console script from `cleanframe.cli:main`). `python -m cleanframe ...` is an equivalent alias for every invocation below.

```bash
cleanframe --help
cleanframe --version
cleanframe <subcommand> --help
```

## Global flags

Accepted by every subcommand:

| Flag | Meaning |
|------|---------|
| `--verbose`, `-v` | Print the run log: skipped columns, quarantine reasons, values an op could not parse (emitted by `clean` and `apply`), and turn on the library's `logging` output at INFO |
| `--json` | Print **one** machine-readable JSON summary on stdout; every human-readable line moves to stderr (see [`--json`](#--json-run-summary)) |
| `--plugin MODULE` | Import `MODULE` first so its custom ops/detectors are available (repeatable) |
| `--no-plugins` | Do not auto-load installed `cleanframe.plugins` entry points or `CLEANFRAME_PLUGINS` |
| `--debug` | Print a traceback on an internal error instead of a one-line message; the `CLEANFRAME_DEBUG=1` environment variable does the same |
| `--help`, `-h` | Show usage |

`cleanframe --version` prints the installed version.

## Read flags

Accepted by `report`, `clean`, `infer-schema`:

| Flag | Meaning |
|------|---------|
| `--sep SEP` | Field delimiter, overriding auto-detection |
| `--encoding ENC` | File encoding, overriding auto-detection |
| `--text` | Read every field verbatim: keeps leading zeros, literal `NA`/`None` text, and `1e5` exact |
| `--no-correct` | Disable read-time encoding/delimiter auto-detection |

`apply` accepts `--sep`, `--encoding` and `--text`; there they override the recipe's `read:` section. `apply` has no `--no-correct`.

## Selection flags

Accepted by `report`, `clean`, `apply`, `suggest`, `infer-schema`:

| Flag | Meaning |
|------|---------|
| `--sheet NAME` | Excel sheet by name; `#N` selects the 0-based positional index (e.g. `--sheet '#0'`). A bare number is a sheet *name*, not an index |
| `--columns A,B,C` | Read a subset of columns (filter; keeps file order) |
| `--nrows N` | Read only the first N data rows |
| `--skiprows N` | Skip the first N data rows; the header row is always kept |
| `--header-row N` | The 0-based line the header is on, when title rows sit above it (recorded in the recipe `read:` section) |

`--skiprows` and `--nrows` count *data rows* — identically for CSV, Excel, Parquet and JSON. A negative value is a usage error. Under `--skiprows`/`--nrows` the diff's `row_id` is relative to the loaded slice.

## `report`

Profile a file and write an HTML report (no transforms applied).

```bash
cleanframe report examples/messy_customers.csv
cleanframe report data.csv --schema examples/customer.schema.yaml -o report.html
```

| Flag | Meaning |
|------|---------|
| `--out PATH`, `-o` | Output `.html` (default: `<file>.report.html`) |
| `--schema PATH` | Target schema YAML (adds mapping diagnostics) |
| `--open` | Open the report in a browser |

Also takes the read and selection flags.

## `clean`

Full pipeline: profile → detect → plan → execute → save artifacts.

```bash
cleanframe clean examples/messy_customers.csv \
  --schema examples/customer.schema.yaml \
  --mode auto \
  --out-dir out/
```

| Flag | Meaning |
|------|---------|
| `--recipe PATH` | Recipe output (default: `<file>.recipe.yaml`) |
| `--out PATH`, `-o` | Cleaned data output (csv/tsv/xlsx/parquet/json) |
| `--out-dir DIR` | Write recipe, cleaned data, code and report into `DIR` |
| `--code PATH` | Export standalone pandas to this `.py` |
| `--report PATH` | Write an HTML diff report here |
| `--quarantine PATH` | Write quarantined rows to this file |
| `--schema PATH` | Target schema YAML |
| `--llm provider/model` | LLM planner, e.g. `anthropic/claude-sonnet-4-6`, `openrouter/anthropic/claude-sonnet-4`, `groq/llama-3.3-70b-versatile` |
| `--max-tokens N` | LLM token budget cap |
| `--llm-exposure none\|metadata\|sample` | What the LLM may see (default: `metadata` — never raw cells) |
| `--no-llm-fallback` | Fail instead of degrading to the rules planner when an LLM call fails |
| `--mode review\|auto\|strict` | Confidence / failure policy (see [Modes](#modes)) |
| `--overwrite` | Allow writing output over the input file (loses the original) |

Also takes the read and selection flags.

The recipe is always written; cleaned data, code, report and quarantine only when their flag is given. Re-running over unchanged input regenerates the same recipe bytes and is fine, but if the file at the recipe path **differs** from the recipe just planned (you hand-edited it, or it is from an older version) `clean` refuses and asks for `--overwrite` (or a different `--recipe`), so an edit is never lost silently. An explicit `--recipe`/`--out`/`--code`/`--report` overrides the path `--out-dir` would have chosen.

A multi-sheet `.xlsx` auto-routes to workbook mode: `cleanframe clean file.xlsx` cleans every sheet; `cleanframe apply file.xlsx --recipe wb.yaml` replays a workbook recipe across sheets.

## `apply`

Replay a saved recipe (deterministic, no LLM).

```bash
cleanframe apply new.csv --recipe customer.recipe.yaml --out clean.csv
cleanframe apply new.csv --recipe customer.recipe.yaml --force   # ignore drift (dangerous)
```

| Flag | Meaning |
|------|---------|
| `--recipe PATH` | Required recipe YAML to replay |
| `--out PATH`, `-o` | Cleaned data output (default: `<file>.clean.csv`, or `.clean.xlsx` for a workbook recipe) |
| `--report PATH` | Write an HTML diff report here |
| `--quarantine PATH` | Write quarantined rows to this file |
| `--mode review\|auto\|strict` | Execution mode (see [Modes](#modes)) |
| `--no-drift-check` | Skip the schema-drift check |
| `--force` | Apply even when schema drift is detected (`on_drift=ignore`); `--mode strict` stops regardless |
| `--overwrite` | Allow writing output over the input file (loses the original) |
| `--chunksize N` | Stream the CSV in chunks of N rows (out-of-core; row-independent recipes only). N must be 1 or more |

Also takes the selection flags plus `--sep`, `--encoding`, `--text`.

## `suggest`

Detect drift and optionally write a patched recipe.

```bash
cleanframe suggest new.csv --recipe customer.recipe.yaml
cleanframe suggest new.csv --recipe customer.recipe.yaml --update -o patched.recipe.yaml
```

| Flag | Meaning |
|------|---------|
| `--recipe PATH` | Required recipe YAML to check |
| `--update` | Write a patched recipe |
| `--out PATH`, `-o` | Where to write the patched recipe (default: `<recipe>.patched.yaml`) |
| `--in-place` | With `--update`, overwrite the recipe file |

Also takes the selection flags.

Without `--update`, finding drift exits `3`; the recipe is left alone.

## `infer-schema`

Draft a target schema from data.

```bash
cleanframe infer-schema examples/messy_customers.csv -o examples/customer.schema.yaml
```

| Flag | Meaning |
|------|---------|
| `--out PATH`, `-o` | Schema output (default: `<file>.schema.yaml`) |
| `--name NAME` | Schema name |

Also takes the read and selection flags.

## `detectors` / `ops`

List registered plugins (built-ins + any you imported).

```bash
cleanframe detectors
cleanframe ops
```

## Modes

| `--mode` | Meaning |
|----------|---------|
| `review` | Default. Surface everything for approval |
| `auto` | Unattended: only higher-confidence fixes |
| `strict` | Fail on drift or validation failure |

## Output paths

Output paths are validated before any work starts, so a bad path fails immediately rather than after a long clean.

`--overwrite` applies to every output they write: without it, writing an output over the input file is refused and nothing is written. `report` and `infer-schema` also refuse to write over their input; they have no `--overwrite`.

## Workbook mode

A multi-sheet `.xlsx`/`.xlsm`/`.xls` with no `--sheet` is cleaned tab by tab into one workbook recipe. These flags are refused there:

| Subcommand | Refused in workbook mode |
|------------|--------------------------|
| `clean` | `--code`, `--report`, `--quarantine`, `--nrows`, `--skiprows`, `--sep`, `--encoding`, `--text` |
| `apply` | `--report`, `--quarantine`, `--sheet`, `--columns`, `--nrows`, `--skiprows`, `--sep`, `--encoding`, `--text` |

In workbook mode `--out-dir` writes the recipe and one rewritten `.xlsx`; there is no per-sheet code or report. Pass `--sheet` to treat one tab as a single sheet instead.

## Streaming mode

`apply --chunksize N` streams the CSV out of core. `--report`, the selection flags (`--sheet`, `--columns`, `--nrows`, `--skiprows`) and the read flags (`--sep`, `--encoding`, `--text`) are refused: the recipe's `read:` section governs how the file is read and sliced.

## `--json` run summary

With `--json`, stdout carries exactly one JSON object — on success **and** on failure — and all the usual
human-readable output goes to stderr, so `cleanframe ... --json | jq` is safe in a pipeline:

```json
{
  "cleanframe_version": "0.4.0",
  "command": "apply",
  "input": "incoming.csv",
  "status": "ok",
  "exit_code": 0,
  "outputs": {"data": "clean.csv"},
  "rows_out": 5,
  "rows_quarantined": 1,
  "diff": {"changed_cells": 21, "rows_dropped": 1, "rows_before": 6, "rows_after": 5},
  "drift": {"has_drift": false, "findings": []},
  "warnings": []
}
```

`status` is one of `ok`, `drift`, `validation_failed`, `error`. A failure adds `"error": {"type", "message"}`; a
drift stop adds the findings (`kind`, `severity`, `column`, `message`). Fields depend on the command
(`quality` for `report`, `chunks` and row counts for `apply --chunksize`, `sheets` for workbooks). Usage errors
(exit `2`) are reported by `argparse` before the summary can be built.

## Exit codes

| Code | Meaning |
|------|---------|
| `0` | Success |
| `1` | Data, recipe or output error |
| `2` | Usage error (bad or missing arguments) |
| `3` | Stopped on schema drift |
| `4` | Validation failed under an erroring policy |
| `70` | Internal error (a bug — re-run with `--debug` for a traceback) |
| `130` | Interrupted |

`suggest` exits `3` when it finds drift and was not given `--update`.
