# Changelog

All notable changes to CleanFrame are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Production safety guards for large datasets and untrusted exports:
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

## [0.1.0] — 2026-07-11

### Added

- Initial public release: profiler, detectors, rules + optional LLM planner, recipe YAML,
  deterministic executor, validation/quarantine, cell-level diff, schema drift, HTML reports,
  codegen, and CLI.
