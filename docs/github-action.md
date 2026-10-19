# GitHub Action (data CI)

**CleanFrame Data CI** replays your committed recipes against your data files in GitHub
Actions. It runs on every pull request or on a schedule, fails the check when a file has
drifted from its recipe or a validation fails, and writes a per-file table to the job
summary.

```yaml
- uses: inboxpraveen/Cleanframe/action@v0.5.1
  with:
    files: data/incoming/*.csv
    recipe: recipes/customer.recipe.yaml
```

The action is the [production pipeline](production.md#recommended-pipeline-shape) turned
into a check:

```text
1. Once:          cleanframe clean sample.csv → review the recipe in a PR → commit it
2. Every PR:      the action replays the recipe over the changed data (no LLM, deterministic)
3. On drift:      the check fails → cleanframe suggest … --update → review → merge
```

Source and full usage notes: [`action/`](../action/README.md). A workflow ready to
copy: [`examples/github-action/data-ci.yml`](../examples/github-action/data-ci.yml).
After it is published to the GitHub Marketplace, the same inputs will also work with
`uses: inboxpraveen/cleanframe-action@v1`.

## Quick start

1. Plan a recipe locally, review it and commit it:

   ```bash
   pip install cleanframe-engine
   cleanframe clean data/incoming/sample.csv --recipe recipes/customer.recipe.yaml
   ```

2. Copy [`examples/github-action/data-ci.yml`](../examples/github-action/data-ci.yml) to
   `.github/workflows/data-ci.yml` and adjust the `files`, `recipe` and `paths:` lines.

3. Open a pull request that changes a data file. The **replay recipes** check runs
   `cleanframe apply` on every matched file and posts its table to the run's summary page.

## How it works

| Step | What happens |
|------|--------------|
| Set up Python | `actions/setup-python` (default `3.12`) with `update-environment: false`, so the job's own `python` is untouched |
| Install | A fresh virtual environment under `$RUNNER_TEMP`, with CleanFrame (see [Versioning](#versioning)) plus any `extra-packages` |
| Replay | `cleanframe apply FILE --recipe RECIPE --mode MODE --json` for each matched file, plus `--force` under `on-drift: warn`. Every file runs, even after one fails |
| Report | Job summary table, an error annotation on each failing file, and the step outputs |
| Verdict | The step exits with the worst exit code, `0` when every file passed |

The CLI always writes cleaned data, because `apply` has no dry-run mode. With `out-dir`
the files go to `<out-dir>/<stem>.clean.csv`, or `.clean.xlsx` for Excel inputs.
Without it they go to a scratch directory that is deleted with the job.

## Inputs

| Input | Default | Meaning |
|-------|---------|---------|
| `files` | *(required)* | Paths or globs (`**` recurses), relative to the workspace root. Put one per line in a YAML block (paths may contain spaces, and `#` lines are comments), or separate them with spaces on one line. A pattern that matches nothing is a usage error (exit `2`), not a silent pass |
| `recipe` | *(required)* | The committed recipe YAML. One recipe per step |
| `out-dir` | *(empty)* | Where to write the cleaned data. Two inputs with the same stem are refused rather than overwriting each other |
| `mode` | `review` | `review`, `auto` or `strict` (see [Modes](cli.md#modes)) |
| `on-drift` | `error` | `error` stops a drifted file (exit `3`). `warn` passes `--force`, applies the file and reports the drift without failing. `mode: strict` stops on drift regardless |
| `fail-on-quarantine` | `false` | `true` fails a file whose rows were quarantined (reported as exit `4`) |
| `cleanframe-version` | *(empty)* | An exact version (`0.5.0`), a specifier (`>=0.5,<0.6`) or `latest`. Empty matches the action's ref. The minimum is `0.4.0` |
| `python-version` | `"3.12"` | 3.10 or later. Quote it, because YAML reads `3.10` as `3.1`. Empty uses the runner's `python3`/`python` |
| `extra-packages` | *(empty)* | Extra pip requirements: `openpyxl` (Excel), `pyarrow` (Parquet), or a [plugin](plugins.md) package |

## Outputs

| Output | Meaning |
|--------|---------|
| `drift` | `true` when any file drifted, whether it was stopped or applied |
| `changed-cells` | Changed cells, summed over the replayed files (from each file's `diff.changed_cells`) |
| `quarantined` | Quarantined rows, summed (`rows_quarantined`) |
| `failed-files` | Files, or unmatched patterns, that failed |
| `exit-code` | The worst exit code, meaning the highest among the failed files |
| `results-file` | JSON file with each file's full [`--json` summary](cli.md#--json-run-summary) and its outcome |

## Results and exit codes

The action keeps the [CLI's exit codes](cli.md#exit-codes):

| Per-file result | Exit | Fails |
|-----------------|------|-------|
| ✅ Passed | `0` | no |
| ⚠️ Applied despite drift (`on-drift: warn`) | `0` | no |
| ❌ Rows quarantined (`fail-on-quarantine: true`) | `4` | yes |
| ❌ Error: unreadable file or recipe, unwritable output | `1` | yes |
| ❌ Usage error, including a pattern that matched nothing | `2` | yes |
| 🛑 Schema drift: the file was stopped | `3` | yes |
| ❌ Validation failed (`on_fail: error`, or `mode: strict`) | `4` | yes |
| 💥 Internal error (a CleanFrame bug) | `70` | yes |

Without `mode: strict`, a validation failure quarantines the rows and exits `0`, as it does
on the command line. The step's exit code, like the `exit-code` output, is the highest code
among the failed files.

## The job summary

```text
## 🧹 CleanFrame Data CI · ❌ Failed
Recipe recipes/customer.recipe.yaml · mode review · on-drift error · CleanFrame 0.5.0

|    | File             | Result                | Changed cells | Quarantined | Drift findings / details                   |
| ✅ | data/jan.csv     | Passed                |            18 |           1 | —                                          |
| 🛑 | data/feb.csv     | Schema drift (exit 3) |             — |           — | • warning Column "Total Amount" is new …   |

❌ 1 of 2 files failed — worst exit code 3 (schema drift). 18 cells changed · 1 row quarantined.
How to fix: run cleanframe suggest FILE --recipe recipes/customer.recipe.yaml --update …
```

- Up to 8 drift findings are shown per file, most severe first. CleanFrame's warnings
  are listed in a collapsed block.
- The summary is capped at 200 rows and 256 KiB, against GitHub's 1 MiB limit. Anything
  left out stays in the job log and the `results-file`.
- The summary carries counts, column names, drift messages and warnings, but no
  cell-level diff. A drift message or warning can still quote one sample value, such as
  a date in a format the recipe doesn't know. The job log keeps CleanFrame's usual
  human-readable output, including a sample of the cell diff, in a collapsed group per
  file.

## Common setups

**Strict pull-request gate:** `mode: strict` fails on drift and on any failing validation.

**Several recipes:** use a matrix with one `recipe`/`files` pair per entry and
`fail-fast: false`.

**Nightly monitoring that reports instead of blocking:** set `on-drift: warn` and
`continue-on-error: true` on the step, then branch on `steps.<id>.outcome` and the `drift`
and `exit-code` outputs. If a later step uses an output in a `run:` script, pass it in
through `env:` rather than pasting `${{ … }}` into the script.

**Keeping the cleaned files:** set `out-dir: cleaned`, then upload `cleaned/` and
`${{ steps.<id>.outputs.results-file }}` with `actions/upload-artifact`.

**Excel and Parquet:** `extra-packages: openpyxl` or `extra-packages: pyarrow`. A
multi-sheet workbook with a [workbook recipe](recipe-spec.md) is replayed sheet by sheet
and written back as `<stem>.clean.xlsx`.

**Custom ops:** install the plugin with `extra-packages`. An entry-point plugin loads
automatically. A plain module is named in `CLEANFRAME_PLUGINS` in the step's `env:`.

Full YAML for each setup: [`action/README.md`](../action/README.md#recipes).

## Versioning

| `uses:` | `cleanframe-version` empty installs |
|---------|-------------------------------------|
| `inboxpraveen/Cleanframe/action@v0.5.1` | CleanFrame **from the same checkout** as the action, so action and library are one release |
| `inboxpraveen/cleanframe-action@v1` (Marketplace) | The CleanFrame release pinned by that action release |

For a fully reproducible run, pin a tag or commit SHA in `uses:` and set
`cleanframe-version`. The action needs CleanFrame 0.4.0 or later, the first release
whose `--json` prints one summary for every run, failures included.

## Security

- **Inputs never reach a shell.** Each one is passed through an environment variable,
  never interpolated into a `run:` script. The CLI is started with an argument list.
- **Your repository's code does not run.** CleanFrame runs from its console script in
  its own virtual environment, not as `python -m`, which would put the checked-out
  repository on `sys.path`. Recipes are read with a safe YAML loader, so a pull
  request's recipes and data stay data. The exception is a plugin you opt into.
- **Data cannot drive the runner.** Column names and messages are escaped in the
  summary. CleanFrame's output is echoed with workflow commands disabled
  (`::stop-commands::`).
- Use the plain `pull_request` trigger. `pull_request_target` combined with a checkout
  of the pull request's head hands untrusted content a write token.
- `permissions: contents: read` is all the action needs.

## Troubleshooting

| Symptom | Cause and fix |
|---------|---------------|
| `… matched no files` (exit `2`) | Patterns are relative to the workspace root and are case-sensitive on Linux. `actions/checkout` must run first. Hidden files need an explicit `.` in the pattern |
| Python 3.1 cannot be found | Quote the version: `python-version: "3.10"` |
| `Unknown op '…'` (exit `1`) | The recipe uses a custom op; see **Custom ops** above |
| `Reading .xlsx requires an extra engine` (exit `1`) | Add `extra-packages: openpyxl`. Legacy `.xls` also needs `xlrd`, and `.parquet` needs `pyarrow` |
| 💥 No JSON summary | The installed CleanFrame is older than 0.4.0, or crashed before printing. Check the collapsed log group for the file |
| `on-drift: warn` still fails on drift | `mode: strict` always stops on drift |
| `output … would be written by more than one input` | Two inputs share a stem under `out-dir`. Rename them or split the step |

## Limitations

- One recipe per step.
- `--chunksize` streaming, `--report`, `--quarantine`, `--sheet` and the read flags are
  not exposed. The recipe's `read:` section still governs reading. For those, call the
  CLI in a `run:` step (see the [CI pattern](production.md#ci-pattern)).
- Linux, macOS and Windows runners are supported. Every step uses `shell: bash`.
