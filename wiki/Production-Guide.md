# Production guide

CleanFrame is designed for **repeatable pipelines**, not one-off notebooks.
This page covers scale, safety, and operational practices for production datasets.

## Recommended pipeline shape

```text
1. One-time (or rare):  cf.clean(...) → review recipe in PR → commit YAML
2. Every file:          cf.apply_recipe(...) with on_drift="error"
3. On DriftError:       cf.suggest_update(...) → human review → merge
```

Never call an LLM on every nightly batch. Plan once; replay forever.

## Scale & memory

| Concern | Default behaviour | Knob |
|---------|-------------------|------|
| Detector scans | Sample first 50k non-null values / column | `DETECTOR_SAMPLE_CAP` in `_util` |
| Cell diff detail | Store ≤ 100k changes; counts stay exact | `options={"max_diff_changes": N}` or `None` |
| Executor | Snapshots the op-touched columns for the diff, so the peak is the frame **plus** those columns — about 2.2× the frame when most columns are cleaned (measured: 3M rows, 4.8 GB peak for a 2.1 GB frame); far less when few are touched | Process one file at a time; use `stream_apply` for row-independent recipes; chunk upstream otherwise |
| Streaming replay | Peak memory bounded by chunk, not file size | `stream_apply(..., chunksize=N)` / CLI `apply --chunksize N` |
| Profiling | Pattern sample capped at 5k | Built-in |
| LLM SAMPLE | Cap 10k before shuffle | Built-in |

```python
result = cf.clean(df, mode="auto", options={"max_diff_changes": 10_000})
# or unlimited detail (can OOM on huge dirty frames):
result = cf.clean(df, options={"max_diff_changes": None})
```

**Practical guidance**

- Prefer Parquet over CSV for multi-million-row files (`pip install "cleanframe-engine[parquet]"`).
- Profile/report on a sample; apply the committed recipe to the full file.
- Do not keep `result.diff.changes` in memory longer than needed — use
  `result.diff.summary()` for metrics.

### Out-of-core streaming

`stream_apply(recipe, in_path, out_path, chunksize=100_000)` (CLI `apply FILE --recipe R --chunksize N`) replays a recipe over a CSV in chunks — peak memory tracks `chunksize`, not file size (measured: 600k rows × 5 columns at `chunksize=50_000` peaked at ≈ 270 MB RSS, of which ≈ 90 MB is the Python + pandas + CleanFrame baseline; 1M and 3M rows at `chunksize=100_000` peaked at ≈ 530–590 MB, so the bound holds but scales with chunk size **and column count** — re-measure on your own schema). Only **row-independent** recipes stream; global ops (dedup, `fill_na` with mean/median/mode/ffill/bfill, `category`/`datetime`/`date` casts, format-less `parse_date`, the `unique` validator, unknown custom ops) are refused with a named error. `check_streamable(recipe)` is the pre-flight; drift is checked over every row before any output is written.

Streaming reads the file **twice**. Chunked reads must agree on column types, or a
later chunk can infer a different type from an earlier one and render the same
value differently (`2` vs `2.0`). The first pass is a bounded-memory scan that
learns the types a whole-frame read would infer (an integer column with a blank
anywhere becomes float, text anywhere makes the column text) and checks schema
and date-format drift across **every** row — so a drifted file, or a blank
beyond the first 200 rows, is refused with a `DriftError` exactly as
`apply_recipe` would, before any output exists. The second pass cleans and
writes, with those types pinned, so the streamed file is byte-identical to a
whole-frame replay in every drift mode (including `on_drift="warn"` /
`check_drift=False`). A value that cannot be read is a `CleanFrameError` naming
the chunk, never a raw pandas exception. A column that mixes booleans with
blanks or other values across chunks has no chunk-independent equivalent and is
refused (run it whole-frame).

The output file is written to a temporary sibling and moved into place only
after the whole stream succeeds; a quarantine file is written **only** when you
pass `quarantine_path` (CLI `--quarantine`) — otherwise quarantined rows are
counted and a warning says they were not saved, exactly like `apply`.

### Multi-sheet Excel & format auto-correction

- `cf.clean_workbook(...)` cleans each sheet independently (one Recipe + diff per sheet); `cf.apply_workbook(...)` replays a `WorkbookRecipe` (`version: 2` YAML with a `sheets:` mapping). `read_frame`/`clean`/`apply` on a multi-sheet workbook with no `sheet=` selected raise, listing the tabs — never silently read sheet 1.
- Read-time format auto-correction is on by default (`correct_format=True`; CLI `--no-correct`): CSV-family encoding (utf-8 → cp1252) and delimiter (`, ; \t |`) are detected, warned, and pinned into the recipe `read:` section for byte-stable replay; an ambiguous delimiter raises, and so does a file whose bytes look like Shift-JIS / GBK / Big5 / EUC-KR / Cyrillic (see [Awkward file shapes](#awkward-file-shapes)).

## Read-time type inference

pandas coerces values **while reading**, before CleanFrame ever sees them, so no
diff can show the change:

| In the file | Read as | Effect |
|-------------|---------|--------|
| `01234` | `1234` | ZIP / account number loses its leading zeros |
| `NA`, `None` | `NaN` | A literal text value becomes missing |
| `1e5` | `100000.0` | Rewritten |
| `TRUE` | `True` | Text becomes a boolean |

`clean` and `report` detect this: they re-read a bounded verbatim slice of the
CSV-family file (the first 200 rows) and compare it to what pandas produced, then
warn naming the affected columns and the value that changed:

```text
⚠ pandas type inference changed values while reading (Phone: 'N/A' was read as a
  missing value). Pass text=True to read every field verbatim.
```

`text=True` (CLI `--text`) reads every field verbatim — no numeric coercion and
no invented nulls, so leading zeros, literal `NA`/`None` and `1e5` all survive —
and the choice is recorded as `text: true` in the recipe's `read:` section, so
`apply_recipe` replays the same read. **Recommend `--text` for any file with
identifier-like columns** (ZIP codes, account numbers, phone numbers, SKUs,
leading-zero part numbers), where a "number" is really a label.

## Awkward file shapes

**Non-UTF-8 files.** A file that is not valid UTF-8 is read as cp1252 (Excel's Western
"Save as CSV"). If its bytes instead look like Shift-JIS, GBK, Big5, EUC-KR or a Cyrillic
code page, decoding them as a Western codec would produce garbled text, so `clean`/`report`
**refuse** and list likely encodings:

```text
✗ sjis.csv is not UTF-8 and its bytes look like a multi-byte or non-Latin encoding … Likely
  encodings: cp932, gb18030, big5. Pass encoding=... (CLI --encoding) to choose one.
```

Pass `encoding="shift_jis"` (`--encoding shift_jis`); the choice is pinned into the recipe's
`read:` section. The decision uses a fixed, deterministic byte heuristic;
`pip install "cleanframe-engine[detect]"` (charset-normalizer) only improves the *ranking* of
candidates in that message. A file with too few non-ASCII bytes to tell (a couple of stray
bytes) is still read as latin-1, with a warning saying so.

**Title rows above the header.** `header_row=N` (CLI `--header-row N`) names the 0-based line
that holds the column names (CSV) or row (Excel); everything above it is skipped and the
setting is recorded in the recipe's `read:` section, so `apply` replays it. When a header
looks wrong (an "empty column name" error, or an Excel sheet whose columns are mostly
`Unnamed: N`) the message suggests a value — it never applies one for you.

**Things CleanFrame warns about but never changes.** Rows with fewer fields than the header
(read as empty cells), a last row that is a `Total` / `Subtotal` / `Grand total` footer,
Excel formulas that were never calculated (no cached value) and Excel merged ranges (only the
top-left cell keeps its value). A NUL byte inside a CSV is refused, because the parser would
silently truncate the value there. `skiprows` past the end of the data raises instead of
returning an empty frame. Writing a workbook back keeps hidden sheets hidden.

## Writing output safely

- Writing output over the input file is **refused** — otherwise the original is
  destroyed before anyone can review the diff. `overwrite=True` (CLI
  `--overwrite`) accepts that loss, for a streamed replay as well as for cleaned
  data, quarantine and workbook writes.
- Every write — cleaned data, quarantine, streamed output, recipe, schema,
  generated `.py`, HTML report — goes to a temporary sibling file and is moved
  into place only on success, so a failure part-way through leaves the previous
  file intact rather than a truncated one.
- Output paths are validated before any work starts, so a bad path fails
  immediately instead of after a long clean.

## Values an op could not parse

When an op cannot parse a cell, the cell becomes null. Those nulls are **counted
per column**, added to `result.log`, and warned about — they are no longer
silent:

```text
⚠ values became missing because an op could not parse them: Phone (1)
```

Treat a non-zero count as a signal that the op's parameters (date `formats`,
`decimal`/`thousands`, unit family) do not match the data, or that the column
holds values worth quarantining rather than nulling.

## Warnings

Every advisory CleanFrame raises uses the `CleanFrameWarning` category (a
`UserWarning` subclass), so it can be routed or silenced as a group:

```python
import warnings
import cleanframe as cf

warnings.simplefilter("ignore", cf.CleanFrameWarning)          # silence all
warnings.simplefilter("error", cf.CleanFrameWarning)           # or make them fatal
```

The CLI renders them as one `⚠ …` line on stderr.

## Safety defaults

| Guard | Default | Disable? |
|-------|---------|----------|
| CSV formula escaping | On (`sanitize_csv=True`) | `write_frame(..., sanitize_csv=False)` |
| Regex length / nested quantifiers | Reject dangerous patterns | Edit recipe to safer patterns |
| Drift on apply | `on_drift="error"` | `"warn"` / `"ignore"` / CLI `--force` |
| Missing recipe columns | Warn + skip (non-strict) | `mode="strict"` to fail |
| Overwriting the input file | Refused | `overwrite=True` / CLI `--overwrite` (also for streaming) |
| LLM fallback | Warn + rules planner | `llm_fallback=False` / CLI `--no-llm-fallback` to raise |
| Read-time inference losses | Warn, naming the columns | `text=True` / CLI `--text` to read verbatim |

### CSV formula injection

Cells starting with `=`, `+`, `-`, `@`, tab, or CR are prefixed with `'` on
CSV/TSV export so Excel/Sheets treat them as text. Keep this enabled when
exports may be opened by humans.

### Untrusted recipes

Treat third-party recipe YAML like untrusted config. Review ops (especially
`replace` regexes and `drop` validations) before applying to sensitive data.

## Modes in production

| Environment | Suggested mode |
|-------------|----------------|
| Interactive cleanup | `review` |
| Scheduled ETL with reviewed recipe | `apply_recipe` + `auto` or `strict` |
| Regulated / financial | `strict` + quarantine review workflow |

## Observability

- Inspect `result.log` for skips, quarantine counts, parse losses, truncated diffs.
- `recipe.meta["llm_fallback"]` records degraded LLM planning;
  `recipe.meta["llm_blocked_ops"]` records `fill_na` / `drop_columns` stripped from an LLM plan (every mode).
- `result.quality.score` is a heuristic for reports — not a compliance metric.
- Wire `warnings` into your logging framework (filter on `CleanFrameWarning`).

## CLI exit codes

Branch on these in a scheduler or CI job rather than parsing stdout:

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

## CI pattern

```yaml
# GitHub Actions
- run: pip install cleanframe-engine
- name: Replay the committed recipe
  run: >
    cleanframe apply fixtures/incoming.csv --recipe recipes/customer.recipe.yaml
    --out out/clean.csv --mode strict --json > out/summary.json
# exit 3 = drift (also number-format drift such as 1.200,50 vs 1,200.50)
# exit 4 = a validation with on_fail: error failed, or --mode strict met any failure
# Without --mode strict a plain validation failure QUARANTINES the rows and exits 0.
```

`out/summary.json` is one JSON object (status, exit code, row counts, drift findings, warnings) you can
assert on or post to a dashboard; see [`--json`](CLI#--json-run-summary). Library code logs through the
standard `logging` module under the `cleanframe` logger (silent unless you configure it; `--verbose` turns it on),
so run summaries land in your normal log pipeline rather than only on stderr.

Commit recipes next to dbt/Airflow code. Review recipe diffs in PRs like code.

## What CleanFrame will not do

- Silently fill missing values (`fill_na` is human-authored only)
- Auto-"fix" outliers
- Delete validation failures without an explicit `on_fail: drop`
- Guarantee identical float bit-patterns across pandas/numpy versions (use
  tolerance checks in tests if needed)

## Typing

CleanFrame ships `py.typed` and inline annotations for editors / mypy.
