<img src="https://raw.githubusercontent.com/inboxpraveen/Cleanframe/main/docs/assets/Logo.svg" alt="CleanFrame logo" width="72" />

# CleanFrame Data CI

**Data CI for your cleaning recipes.** On every pull request (or on a schedule) this
GitHub Action replays your committed [CleanFrame](https://github.com/inboxpraveen/Cleanframe)
recipes against your data files. It fails the build when a file's schema has drifted
or a validation fails, and it writes a readable table to the job summary.

```yaml
- uses: inboxpraveen/Cleanframe/action@v0.6.0
  with:
    files: data/incoming/*.csv
    recipe: recipes/customer.recipe.yaml
```

> Once the action is published to the GitHub Marketplace, the same inputs work with
> `uses: inboxpraveen/cleanframe-action@v1`.

CleanFrame plans a cleaning **recipe** (YAML) once. You review it and commit it, and it
replays deterministically from then on. `cleanframe apply` **stops** when next month's file
no longer matches the recipe, for example when a column was renamed, a new column appeared
or a date format changed. This action turns that stop into a red check on the pull request
that introduced the change.

## Quick start

Copy [`examples/github-action/data-ci.yml`](https://github.com/inboxpraveen/Cleanframe/blob/main/examples/github-action/data-ci.yml) into
your repository's `.github/workflows/` directory, or start from this:

```yaml
# .github/workflows/data-ci.yml
name: Data CI

on:
  pull_request:
    paths: ["data/**", "recipes/**"]
  workflow_dispatch:

permissions:
  contents: read

jobs:
  replay:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@v7
      - name: Replay committed recipes
        uses: inboxpraveen/Cleanframe/action@v0.6.0
        with:
          files: |
            data/incoming/*.csv
            data/archive/**/*.csv
          recipe: recipes/customer.recipe.yaml
```

Don't have a recipe yet? Plan one locally, review it, and commit it:

```bash
pip install cleanframe-engine
cleanframe clean data/incoming/sample.csv --recipe recipes/customer.recipe.yaml
```

## What it does

1. Sets up Python (default 3.12) and installs CleanFrame into an **isolated virtual
   environment**. Your job's `python` and `PATH` are not changed.
2. Runs `cleanframe apply FILE --recipe RECIPE --json` for every file the `files`
   patterns match. Every file is replayed even when an earlier one fails.
3. Writes a job summary table, adds annotations to the failing files, and sets the
   outputs below.
4. Fails the step with the **worst** exit code when any file failed.

## Inputs

| Input | Default | Meaning |
|-------|---------|---------|
| `files` | *(required)* | Data files to check, as paths or globs (`**` recurses), relative to the workspace root. Put one per line (a YAML `\|` block; paths may contain spaces and `#` lines are comments), or separate them with spaces on a single line. **A pattern that matches no file fails the run** (exit `2`) instead of passing silently. Directories are skipped and a file matched twice is replayed once |
| `recipe` | *(required)* | Path of the committed recipe YAML to replay. One recipe per step: use a [matrix](#several-recipes) for more |
| `out-dir` | *(empty)* | Write the cleaned data here as `<stem>.clean.csv` (`<stem>.clean.xlsx` for Excel inputs). When empty, the run only checks: `apply` always writes, so its outputs go to a scratch directory under `$RUNNER_TEMP` that is deleted with the job. Two inputs with the same stem (`a/x.csv`, `b/x.csv`) are refused in `out-dir` mode rather than overwriting each other |
| `mode` | `review` | `review`, `auto` or `strict`, passed to `--mode`. `strict` fails a file on any drift or any failing validation |
| `on-drift` | `error` | `error` stops a drifted file and fails it (exit `3`). `warn` applies it anyway (`--force`), reports the drift in the summary and as a warning annotation, and does not fail. **`mode: strict` always stops on drift**, whatever `on-drift` says |
| `fail-on-quarantine` | `false` | When `true`, a file whose rows were quarantined by a validation fails (reported as exit `4`). By default quarantining is the recipe working as designed: the rows are counted in the summary and the check passes |
| `cleanframe-version` | *(empty)* | The CleanFrame to install: an exact version (`0.5.0`), a PEP 440 specifier (`>=0.5,<0.6`) or `latest`. When empty, the action installs the CleanFrame that matches its own ref (see [Versioning](#versioning)). Must be `0.4.0` or later |
| `python-version` | `"3.12"` | Python for the action's environment (3.10 or later). Quote it: YAML reads `3.10` as `3.1`. Empty uses `python3`/`python` from `PATH`, for self-hosted runners without `actions/setup-python` |
| `extra-packages` | *(empty)* | Extra pip requirements installed next to CleanFrame, separated by spaces or newlines. Use `openpyxl` for Excel, `pyarrow` for Parquet, or your [plugin](#custom-ops-plugins) package |

## Outputs

| Output | Meaning |
|--------|---------|
| `drift` | `true` when any file showed schema drift, whether it was stopped or applied under `on-drift: warn` |
| `changed-cells` | Cells changed by the recipe, summed over the files that were replayed |
| `quarantined` | Rows quarantined by validations, summed over all files |
| `failed-files` | Number of files, or of unmatched patterns, that failed |
| `exit-code` | The worst exit code: the highest number among the failed files |
| `results-file` | Path of a JSON file with every file's full `--json` summary and its outcome, ready for a later step or `actions/upload-artifact` |

## Results and exit codes

The CleanFrame CLI's exit codes are a contract, and the action keeps them:

| Per-file result | Exit | Fails the build |
|-----------------|------|-----------------|
| ✅ Passed | `0` | no |
| ⚠️ Applied despite drift (`on-drift: warn`) | `0` | no |
| ❌ Rows quarantined (`fail-on-quarantine: true`) | `4` | yes |
| ❌ Error: the file or recipe could not be read or written | `1` | yes |
| ❌ Usage error, including a `files` pattern that matched nothing | `2` | yes |
| 🛑 Schema drift: the file was stopped | `3` | yes |
| ❌ Validation failed (`on_fail: error`, or `mode: strict`) | `4` | yes |
| 💥 Internal error, which is a CleanFrame bug | `70` | yes |

Without `mode: strict`, a plain validation failure **quarantines** the rows and exits
`0`. Use `fail-on-quarantine: true` to fail on those rows too.

## The job summary

Each run appends a section like this to the workflow run's summary page:

> ## 🧹 CleanFrame Data CI · ❌ Failed
>
> Recipe `recipes/customer.recipe.yaml` · mode `review` · on-drift `error` · CleanFrame 0.5.0
>
> | | File | Result | Changed cells | Quarantined | Drift findings / details |
> |:-:|:--|:--|--:|--:|:--|
> | ✅ | data/jan.csv | Passed | 18 | 1 | — |
> | 🛑 | data/feb.csv | Schema drift (exit 3) | — | — | • **warning** Column "Total Amount" is new — 67% match to recipe column "Amount"<br>• **warning** Column "Loyalty Tier" is new and unrecognised |
>
> **❌ 1 of 2 files failed** — worst exit code 3 (schema drift). 18 cells changed · 1 row quarantined.

The section ends with "How to fix" hints for the failure kinds it found. For drift, the
hint is to run `cleanframe suggest FILE --recipe RECIPE --update` locally, review the
patched recipe and commit it. CleanFrame's warnings sit in a collapsed block below the
table.

The summary is limited to 200 table rows, 8 drift findings per file and 256 KiB in total,
which keeps it well under GitHub's 1 MiB limit. Whatever is left out stays in the job log
and the `results-file`. The summary shows counts, column names, drift messages and
warnings. It never includes a cell-level diff, but a drift message or warning can quote
one sample value, such as a date in a format the recipe doesn't know.

## Recipes

### Fail pull requests on drift

The [quick start](#quick-start) does this. Add `mode: strict` to also fail on any
validation failure.

### Several recipes

Run one step per recipe, or use a matrix:

```yaml
jobs:
  replay:
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        include:
          - recipe: recipes/customers.recipe.yaml
            files: data/customers/*.csv
          - recipe: recipes/orders.recipe.yaml
            files: data/orders/**/*.csv
    steps:
      - uses: actions/checkout@v7
      - uses: inboxpraveen/Cleanframe/action@v0.6.0
        with:
          files: ${{ matrix.files }}
          recipe: ${{ matrix.recipe }}
```

### Nightly drift monitoring that reports and doesn't block

```yaml
on:
  schedule:
    - cron: "0 6 * * *"   # 06:00 UTC daily

jobs:
  monitor:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - name: Replay
        id: cleanframe
        uses: inboxpraveen/Cleanframe/action@v0.6.0
        continue-on-error: true          # keep going so the next step can react
        with:
          files: data/landing/**/*.csv
          recipe: recipes/customer.recipe.yaml
          on-drift: warn                  # apply drifted files, report the drift
      - name: Flag drift
        if: steps.cleanframe.outcome == 'failure' || steps.cleanframe.outputs.drift == 'true'
        env:
          EXIT_CODE: ${{ steps.cleanframe.outputs.exit-code }}
        run: echo "::warning::CleanFrame found drift or failures (exit code $EXIT_CODE)"
```

### Keep the cleaned files

```yaml
      - uses: inboxpraveen/Cleanframe/action@v0.6.0
        id: cleanframe
        with:
          files: data/incoming/*.csv
          recipe: recipes/customer.recipe.yaml
          out-dir: cleaned
      - uses: actions/upload-artifact@v7
        if: always()
        with:
          name: cleaned-data
          path: |
            cleaned/
            ${{ steps.cleanframe.outputs.results-file }}
          if-no-files-found: ignore
```

### Excel and Parquet

```yaml
        with:
          files: data/*.xlsx
          recipe: recipes/workbook.recipe.yaml
          extra-packages: openpyxl          # pyarrow for .parquet; legacy .xls also needs xlrd
```

An Excel input is written back as `<stem>.clean.xlsx`. A multi-sheet workbook with a
workbook recipe (`version: 2`) is replayed sheet by sheet.

### Custom ops (plugins)

The action installs CleanFrame into its own environment. A recipe that uses
[custom ops](https://github.com/inboxpraveen/Cleanframe/blob/main/docs/plugins.md) therefore needs the plugin installed there as well:

```yaml
        with:
          files: data/*.csv
          recipe: recipes/iban.recipe.yaml
          extra-packages: my-cleanframe-plugins==1.2   # or ./plugins for a package in this repo
```

A plugin that declares a `cleanframe.plugins` entry point loads automatically. Otherwise,
set `CLEANFRAME_PLUGINS: my_pkg.cleanframe_plugin` under the step's `env:`.

### Pin the CleanFrame version

```yaml
        with:
          cleanframe-version: "0.6.0"
```

## Versioning

- **`uses: inboxpraveen/Cleanframe/action@v0.6.0`**: with `cleanframe-version` left
  empty, CleanFrame is installed **from the same checkout as the action**. The action
  and the library are always the same release.
- **`uses: inboxpraveen/cleanframe-action@v1`** (once published to the Marketplace):
  `@v1` follows the latest 1.x release of the action. Each release pins the CleanFrame
  version it was tested with.
- To be fully reproducible, pin a full tag or a commit SHA and set `cleanframe-version`.

The action needs CleanFrame 0.4.0 or later, the first release where every
`cleanframe apply --json` run, including a failed one, prints one JSON summary.

## Security

- **No script injection.** Every input reaches the scripts through an environment
  variable. No `${{ inputs.* }}` expression is pasted into a shell script, and the CLI
  is started with an argument list, never through a shell.
- **The repository's code is not executed.** CleanFrame runs from its console script in
  an isolated virtual environment. It does not run as `python -m`, which would put the
  checked-out repository first on `sys.path`. Recipes are YAML read with a safe loader,
  so a pull request's data files and recipes are treated as data, not code. The
  exception is a plugin you opt into with `extra-packages` or `CLEANFRAME_PLUGINS`.
- **Data-derived text cannot drive the runner.** Column names and messages are
  escaped in the summary table. CleanFrame's own output is echoed to the log with
  workflow commands switched off, so a column named `::add-mask::…` is just text.
- **Logs show sample values.** The job log keeps CleanFrame's normal human-readable
  output in a collapsed group per file, and that output includes a sample of changed
  cell values. The job summary has no cell-level diff, but a drift message or warning
  can quote one sample value. If your logs are public and the data is sensitive, run
  this in a private repository.
- **Don't combine with `pull_request_target` plus a checkout of the pull request's
  head.** That pattern gives untrusted code a write token. Plain `pull_request` is the
  right trigger for data CI.
- Grant the job `permissions: contents: read`. The action needs nothing more.

## Limitations

- One recipe per step.
- `apply` options that the action doesn't expose (`--chunksize` streaming, `--report`,
  `--quarantine`, `--sheet` and the read flags) are not available. A recipe's `read:`
  section still governs how each file is read. For anything else, call the CLI in a
  `run:` step: `pip install cleanframe-engine` and then `cleanframe apply ... --json`.
- Works on Linux, macOS and Windows runners. Every step uses `shell: bash`, which
  Windows runners provide through Git Bash.

## Running the logic locally

All of the action's logic is in [`summary.py`](summary.py), which uses only the standard
library. To reproduce a CI run on your machine, use a Python that has CleanFrame
installed:

```bash
CF_FILES='data/incoming/*.csv' CF_RECIPE=recipes/customer.recipe.yaml \
  python path/to/summary.py run
```

It prints the markdown summary and the outputs to stdout when `GITHUB_STEP_SUMMARY` and
`GITHUB_OUTPUT` are not set. The other inputs are `CF_OUT_DIR`, `CF_MODE`, `CF_ON_DRIFT`
and `CF_FAIL_ON_QUARANTINE`.
