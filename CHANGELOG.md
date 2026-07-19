# Changelog

All notable changes to CleanFrame are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
