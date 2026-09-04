# Changelog

All notable changes to CleanFrame are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.0] — 2026-09-04

Release-readiness pass driven by a full audit of the CLI, the Python API, packaging and
the docs. Several fixes change behaviour on purpose: cases that used to pass silently
now fail loudly, and a few transforms that quietly corrupted data no longer run.

### Security

- **Generated code no longer trusts column names.** `generate_code` interpolated the
  source column into a comment and the validation label into a `raise` without escaping,
  so a CSV header containing a newline produced an exported `clean(df)` that executed
  arbitrary statements. Both are now sanitised.
- **CSV/Excel exports sanitise header labels**, not just cells: a column literally named
  `=CMD()` was written as a live formula.
- The formula sanitiser no longer escapes plain signed numbers, so a normalised phone
  number (`+919876543210`) and a negative amount (`-1.5`) survive export unchanged.
- Recipes and schemas reject **duplicate YAML keys** instead of silently keeping the last.

### Fixed — silent data corruption

- `normalize_phone` on a numeric column (one blank cell makes pandas read the column as
  float) appended a spurious trailing digit; `9876543210.0` became `98765432100`.
  Trailing extensions (`ext 12`) are no longer fused into the number either.
- ISO timestamps and dashed identifiers were classified as phone numbers and stripped to
  digits. Phone classification now needs a phone-ish column name or an explicit `+`/`(`
  dial prefix, and clock times and ISO dates are never phone-like.
- European decimals were parsed with the US convention: `€1.200,50` became `1.2005` while
  the issue reported nothing unparseable. The currency detector now infers the grouping.
- A fuzzy category merge folded `Unapproved` into `Approved` (and `Unverified` into
  `Verified`). A spelling that is another plus a negation prefix is never merged.
- A ragged CSV row (one field too many, typically a trailing delimiter) shifted every
  column left because pandas promoted the first field to the index. Reads now pass
  `index_col=False`.
- `in [Yes, No]` rejected every row: YAML 1.1 read the values as booleans. Membership
  values are parsed as text.
- Mixed day-first and month-first dates reported `unparsed: 0` while nulling the values
  that no longer matched after the conflicting format was dropped. The count is now taken
  after reconciliation.
- Punctuation-only values (`-`, `?`) were collapsed into `""` by category clustering.
- `NA` in a short all-caps code column (Namibia) is treated as ambiguous, not a null.
- Category canonicalisation prefers short all-caps codes (`CA` over `ca`).
- A user column named `_cf_quarantine_reason` was overwritten in the quarantine frame.
- `skiprows` meant different things per format and could eat the CSV header. It now
  counts **data rows** everywhere: an int drops that many leading records, a list names
  1-based data rows.
- `profile_dataframe` no longer takes O(n²) time on long text values, so a column holding
  a notes field or a JSON blob does not stall `clean()`. It also survives unhashable
  cells (lists/dicts) and categorical columns.
- A validation rule written `values: [Yes, No]` matched nothing, because YAML 1.1 reads
  those as booleans. Every spelling of the boolean is now matched, in the executor and in
  the exported code alike.
- `round: {decimals: 2}` — the documented parameter form — failed to load; only the bare
  `round: 2` worked.
- The schema dtype alias `str` was listed but not accepted.
- `excel_sheet_names` left the workbook file handle open, so a later write to the same
  path could fail with a permission error on Windows.

### Fixed — errors that reached users as tracebacks

- The CLI has a catch-all: an unexpected exception prints one line plus an invitation to
  re-run with `--debug` (or `CLEANFRAME_DEBUG=1`) instead of a traceback.
- Clean messages replace tracebacks for: a missing or directory input path on
  `clean`/`report` (the format detector opened the file before the existence check); an
  output path that is a directory, read-only, or locked; malformed recipe YAML through
  `apply`; malformed schema YAML; a recipe or schema in a non-UTF-8 encoding; a
  non-integer recipe `version`; a non-mapping `meta`; a malformed `source_fingerprint`; a
  validation rule with an unsupported parameter; a bad `mode`, `on_drift` or `chunksize`;
  a wrongly-typed `options`, `planner` or `recipe` argument.
- The encoding ladder ends at latin-1, which cannot fail. Byte values cp1252 leaves
  undefined (a Shift-JIS export, for instance) no longer raise mid-detection.
- UTF-16/32 files are detected by byte-order mark instead of being reported as binary.
- Excel writes strip the control characters openpyxl refuses and check its 32,767-character
  cell limit up front, so a failed write no longer leaves a truncated workbook behind.

### Changed

- **The PyPI distribution is now `cleanframe-engine`** (the name `cleanframe` belongs to
  an unrelated project). The import package, the console script and every API are
  unchanged: `pip install cleanframe-engine`, then `import cleanframe as cf`.
- **Exit codes are distinct:** `0` success, `1` data/recipe/output error, `2` usage,
  `3` stopped on drift, `4` validation failed, `70` internal error, `130` interrupted.
  Previously everything but a usage error exited `1`.
- **Writing output over the input file is refused** unless `overwrite=True` /
  `--overwrite`. A single-sheet clean written back over its own workbook used to leave
  one renamed sheet and delete the rest.
- Every write goes through a temporary file and is moved into place — cleaned data,
  quarantine, streamed output, recipes, schemas, generated code and HTML reports.
- `stream_apply` accepts `overwrite=True` (CLI `--overwrite`), which it previously had no
  way to express.
- **Unknown and misspelled op parameters are rejected at load time** with the list of
  valid names. `parse_date: {format: ...}`, `dedup: {case_insensitive: true}` and
  `to_na: {token: ...}` used to load and do nothing. `cast` targets, `normalize_unit`
  units, `dedup keep` and validation check names are validated at load too.
- Wrongly-typed parameters are refused rather than coerced: `remove_symbols: 5` used to
  delete every digit 5, and a bare string where a list belongs used to be read one
  character at a time.
- Schema dtype aliases (`int`, `number`, `text`, `bool`, `id`) are normalised, so they now
  drive the same casts as their canonical spellings instead of being silently ignored.
- Flags that were silently ignored now error: `--code`/`--report`/`--quarantine` and the
  file-only read flags in workbook mode, `--report` and the selection flags in streaming
  mode. `--chunksize 0` was accepted and quietly ran a whole-frame apply.
- `llm_exposure="none"` makes no network call at all; it plans with rules and warns.
  Previously it sent the same metadata payload as `metadata` exposure.
- `on_drift` is validated: a typo used to disable the drift guard silently.
- Streaming pins column types from the recipe's fingerprint, so a streamed replay renders
  numbers the same way a whole-frame replay does (`2` vs `2.0`), falling back to text with
  a warning if the file no longer matches.
- `suggest` re-applies the recipe's recorded `read:` binding and accepts the selection
  flags, so the command the drift message recommends now works on workbooks and on
  `;`-separated files instead of reporting every column as drifted.
- `generate_code` raises when a recipe uses an op or check it cannot reproduce, instead of
  emitting a silently incomplete module (`allow_partial=True` restores the old output).
- Every warning uses the new `CleanFrameWarning` category and prints as one line from the
  CLI. Filter them with `warnings.simplefilter("ignore", cleanframe.CleanFrameWarning)`.
- `Mode.coerce` raises `CleanFrameError` rather than `ValueError`.
- Unsupported input extensions are refused instead of being parsed as CSV.
- Duplicate and blank CSV header names are refused instead of being renamed by pandas to
  `name.1` / `Unnamed: 3`.
- Writing legacy `.xls` is refused (pandas emits `.xlsx` bytes under the name).

### Added

- `text=True` / `--text` reads every field verbatim, keeping leading zeros, literal `NA`
  and `1e5` exactly as the file has them, and records the choice in the recipe. Without
  it, `clean`/`report` compare a bounded verbatim re-read and warn naming the columns
  pandas' type inference changed.
- Values an op could not parse are counted per column, logged and warned about.
- `python -m cleanframe` as an alias for the console script.
- CLI: `--debug`, `--verbose`, `--sep`, `--encoding`, `--text`, `--no-llm-fallback`,
  `clean --out-dir`, and `-o` on every subcommand that has `--out`.
- `clean(..., llm_fallback=False)` makes an LLM failure raise instead of degrading.
- **A model-written step that cannot load no longer costs the whole plan.** It is dropped,
  warned about, and listed in `recipe.meta["llm_dropped"]`, so the rest of the model's
  recipe still runs. Previously one stray parameter, or a `cast` with no target, discarded
  the entire LLM recipe and silently fell back to rules. A response that is not a recipe at
  all still falls back. Ops a mode forbids are recorded in `recipe.meta["llm_blocked_ops"]`.
- Recipe loading joins an op and its single argument written as two list items
  (`["extract_currency", "cast", "float"]`), a shape models emit regularly. Two real op
  names in a row are never joined.
- `sep=` / `encoding=` on `clean`, `report`, `apply_recipe` and `infer_schema`;
  `blank_lines=` and `text=` on `read_frame`; `source=`/`overwrite=` on `write_frame`.
- `OutputError` and `CleanFrameWarning` in the public API.
- `mypy` runs clean over the package and is part of the dev extra and CI.
- Release workflow (PyPI trusted publishing), Dependabot, CodeQL, issue and pull-request
  templates, a code of conduct, and a citation file.

## [0.2.0] — 2026-07-19

### Added — production-hardening upgrade (multi-sheet, scaling, selection, format-correction, streaming)

- **Multi-sheet Excel workbooks**: `clean_workbook` / `apply_workbook` clean every tab
  independently (one recipe + diff per sheet), collected in a `WorkbookResult` with a
  single reviewable `WorkbookRecipe` (`sheets:` block). `read_frame` now *refuses* to
  silently read only sheet 1 of a multi-sheet workbook — pass `sheet=` or use the
  workbook API. Write-back preserves untouched sheets but **refuses in-place overwrite
  of the source** (formulas/formatting are lost on pandas re-emit) unless `overwrite=True`.
  The `cleanframe clean/apply` CLI auto-routes a multi-sheet `.xlsx`.
- **Selective ingestion**: `sheet` / `columns` / `nrows` / `skiprows` on
  `read_frame`/`clean`/`report`/`apply`/`infer-schema` and the CLI, recorded in the recipe's
  new `read:` section (recipe v2, backward-compatible) so `apply` re-reads the same slice.
- **Read-time format auto-correction** (`correct_format=True`, default; `--no-correct` to
  opt out): deterministic encoding fallback (utf-8 → cp1252) and header-consensus delimiter
  detection for CSV-family files, pinned into the recipe `read:` section for replay; refuses
  on an ambiguous delimiter.
- **Out-of-core streaming replay**: `stream_apply(recipe, in, out, chunksize=)` and
  `cleanframe apply --chunksize N` process files larger than RAM. Row-independent recipes
  stream with byte-identical values and bounded (chunk-sized) memory; global ops
  (`dedup`, aggregate `fill_na`, `cast` to category/datetime, format-less `parse_date`, the
  `unique` validator) are **refused with a clear, named error**. Streaming also honours the
  refuse-on-drift guarantee (checked on a bounded head sample).
- **In-RAM scaling**: ~44× faster diff extraction (bulk slicing), the executor snapshots only
  op-touched columns (peak memory ≈ input, not 2×), a vectorised `.str` fast-path for text ops
  (byte-identical), and deduplicated profiler signal computation.

### Fixed — correctness & robustness (from a full empirical audit)

- **Silent data loss / lineage**: an emitted derived column that overwrites an existing column
  is now tracked in the diff (was reported as add+remove with the change lost); a value rewrite
  on a row later dropped by dedup/validation keeps its change provenance; two ops emitting the
  same column now raise instead of silently clobbering.
- **Detectors**: ambiguous DD/MM vs MM/DD dates are resolved from the data (no silent day/month
  swap); fuzzy category clustering no longer merges two both-frequent look-alikes
  (`insured`/`uninsured`); datetimes keep their time-of-day; disguised-null tokens that are often
  legitimate (`none`, `-`, `unknown`) are surfaced for review, not auto-converted.
- **`parse_number`** rejects fused digit groups (`"12ab34"` → NaN) and handles scientific
  notation; format-less `parse_date`/`cast(datetime)` are now deterministic (order-independent).
- **Crash-hardening**: non-UTF-8 CSVs, ragged/empty files, directories, mislabeled extensions,
  malformed YAML/op params, duplicate/non-string/MultiIndex column names, and a Windows cp1252
  console now raise a clean `CleanFrameError` / render safely instead of a raw traceback.
- **LLM planner**: any failure (bad JSON, malformed recipe, provider/network error) now falls
  back to the deterministic rules planner as documented — previously some outputs crashed the
  pipeline with an uncaught `KeyError`. Array-form ops are parsed leniently; the `METADATA`
  exposure no longer leaks a raw cell value via a detector message.
- **Codegen**: generated standalone pandas now reproduces the executor exactly (currency symbols,
  number sign/unicode-minus, NA tokens, unit aliases, `cast` bool/date, validation row-filtering,
  and case-insensitive dedup), rendered from a single source of truth in `cleanframe.ops`.
- **Schema**: an unknown/typo'd dtype (`"flaot"`) is rejected instead of silently ignored.

### Added — production safety guards for large datasets and untrusted exports

- Detector scans sample at most 50,000 non-null values per column (`sample_non_null`)
- Cell-level diffs cap stored detail at 100,000 changes by default (`max_diff_changes`)
- CSV/TSV writes escape spreadsheet formula injection by default (`sanitize_csv=True`)
- Recipe regexes (`replace`, `matches:`) reject oversized / nested-quantifier patterns
- LLM HTTP clients use a 60s timeout; SAMPLE exposure no longer materialises entire columns
- Missing recipe columns and LLM fallbacks emit `warnings.warn` (not silent skips)
- Optional `parquet` extra (`pyarrow`)
- `cleanframe/py.typed` marker (PEP 561)
- Example schema + recipe under `examples/`
- CI workflow (pytest + ruff on Python 3.10–3.13)
- Full documentation under `docs/` and GitHub Wiki pages under `wiki/`

### Fixed

- Units detector threshold now compares against the sampled column size, not the full row count
- Cross-platform IO: UTF-8 (+ BOM-tolerant reads), LF-only text/CSV writes, auto-create parent dirs
- Quarantine / `save_all` CSV exports go through `write_frame` (same sanitise + encoding path)
- Pandas 3 compatibility: detectors recognise default ``str`` / ``string`` dtypes (not only ``object``)

## [0.1.0] — 2026-07-11

### Added

- Initial public release: profiler, detectors, rules + optional LLM planner, recipe YAML,
  deterministic executor, validation/quarantine, cell-level diff, schema drift, HTML reports,
  codegen, and CLI.
