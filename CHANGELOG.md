# Changelog

All notable changes to CleanFrame are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.0] — 2026-09-30

A correctness release. An empirical review (26 hard-input probes, a 1M/3M-row benchmark and a
docs audit that ran every example) found places where CleanFrame produced a **wrong number, a
wrong merge or a wrong file with no warning**. Every one is fixed, each has a regression test, and
the invariants that should have caught them now have property-based tests.

### Fixed — silent wrong results

- **A new number format no longer replays silently.** Replaying a `.`-decimal recipe on `€1.200,50`
  produced `1.2005`, exit 0, no warning. Numbers are now parsed by structure (below), and `apply_recipe`
  raises a `number_format_drift` finding (exit 3) when digit-bearing values stop fitting the recipe's
  `decimal`/`thousands` convention (a small share is INFO, so a stray `12ab34` is not a stop).
- **Signs were dropped.** `-$5`, `-€3.50`, `-₹500` and `$(8)` parsed as positives. `parse_number` now finds
  the sign wherever it is written (`-5`, `-$5`, `$-5`, `5-`, `(5)`, `$(5)`, unicode minus).
- **`€2,5` became 25 and `€999,99` became 99999** whenever one value in the column looked US-grouped.
  Grouping is now validated (3-digit or Indian lakh) and the currency detector infers comma-decimal only
  from unambiguous evidence; values that fit neither convention become a counted, warned null.
- **The category detector merged different numbers**, in every mode including `strict`: `-1,200`→`1,200`,
  `12.00`→`1,200`, `inf`→`-inf`, `A1`→`A-1`. Values that differ in sign, decimal or grouping are never one category.
- **`normalize_unit` read `1,500 g` as 1.5 g** (1000x off). Ambiguous numbers are refused (null + warning);
  `1,5 kg` and `0.500 kg` still parse, and `1.5e3 g` now does too.
- **`cast: int` rounded IDs above 2**53** through float64; integer text is now converted exactly.
- **The cell diff missed precision loss** (`9007199254740993` → `...992.0`) and `1` → `True`, because numpy
  compared `int64` with `float64` after promotion. Both are now reported.
- **`R$`, `A$`, `C$`, `HK$`, `NZ$`, `S$` were all labelled USD**: symbols are matched longest-first, and `₺ ₫ ₱ ₪ ฿` were added.
- **Auto-dedup treated `1`, `True` and `1.0` as the same value**, and collapsed a one-column frame to its
  distinct values. Booleans stay distinct from numbers, and one-column frames are reported, not deduplicated.
- **Offset timestamps were shifted to the UTC calendar day** (`2024-01-01T02:00+05:30` → `2023-12-31`); a
  single-offset column now keeps its wall-clock date.
- **`title_case` produced `3Rd Street` and `Don'T`**, and **`normalize_phone` turned `0044…` into `+91442…` and `12345` into
  `+9112345`**. Ordinals and apostrophes are handled; `00` is read as `+`; numbers too short to be phones never get a country code invented.
- **Any non-UTF-8 file was accepted as latin-1** (Shift-JIS became mojibake with exit 0). The encoding ladder
  can now refuse: a file that is neither UTF-8, cp1252 nor plausibly a single-byte Latin text raises with the likely
  encodings and `--encoding` advice.
- **`read_frame` silently truncated `a\x00b` to `a`**; it now raises like `clean`.
- **Generated code diverged from the executor**: `05/01/2024` read as Jan 5 instead of May 1, offset timestamps
  crashed it, `to_na case_insensitive: false` was ignored, `normalize_values` skipped non-string cells, integer column
  labels and missing columns raised `KeyError`. Exported pandas now **embeds the executor's own helper source**
  for numbers, units, ints, dates and title case, and skips missing columns like the executor.
- **`stream_apply` leaked raw pandas errors** on a late chunk that did not fit the recorded dtypes (exit 70), and its
  output could differ from `apply_recipe` (`2` vs `2.0`, drift missed beyond the first 200 rows). It now makes a first
  bounded-memory pass, checks drift across every row, and writes byte-identical output or refuses with a named error.
  Streaming also stops writing a quarantine file nobody asked for.
- **`suggest --update` "learned" a competing reading of dates that already parse** (`03/03/2026` → `%m/%d/%Y`) and could
  not fix the README's own `Jan 5, 26` example. It now learns only from values the recipe cannot parse.
- **YAML 1.1 traps in hand-written recipes.** `yes:`, `no:`, `null:`, `010:`, `12:30:` keys were read as `True`, `False`,
  `None`, `8`, `750` and the column was skipped with only a warning. Plain keys now stay text; `to_na: [no, null]` is refused
  with a "quote it" hint.
- **`cleanframe clean` silently overwrote a hand-edited recipe.** It now refuses to replace a recipe that differs from
  the one just planned unless `--overwrite` is given (re-running over unchanged input is still fine).
- `Recipe.from_yaml(r.to_yaml()) == r` was `False` for every generated recipe: equality now compares the canonical serialisation.
- **Money in `1.234` shape is never guessed.** With no other evidence `€1.234` could be 1234 (German) or 1.234 (Irish); the currency detector now reports `ambiguous_number_format` and proposes nothing, instead of picking one and being 1000x wrong.
- **Currency codes:** an explicit code beats a symbol (`$5 MXN` is pesos), `$`-family symbols glued to letters are no longer mislabelled (`AR$`, `MX$`, `NT$`, `CL$`, `CO$` added; an unknown `XY$5` is "no currency", not USD), `Rs` is INR, and more ISO codes are recognised.
- **`normalize_unit` recognises the spellings people write** (`lbs`, `pounds`, `kgs`, `ounces`, `inches`, `feet`, `gallons`, …).
- **A deliberate `to_na` was reported as "values became missing because an op could not parse them"**; it is now logged as intentional and only real parse failures warn.
- **A derived column could be named `None_currency`** when `extract_currency` followed an op that returned an unnamed Series; op results keep the column name.
- **`stream_apply` silently dropped the extra field of a row wider than the header** (pandas' chunked reader truncates it); streaming now refuses such a file, as `apply_recipe` does.
- `dedup` / `drop_columns` accept numeric-looking column names (`subset: [2024]`); `on_fail: null` explains the YAML trap; the Excel writer warns when it removes illegal control characters; `read_frame` on a `;`-delimited file with commas in the text says "semicolon-delimited" instead of suggesting `header_row`; a lone `-`/`+` is no longer formula-escaped; a percent or currency symbol keeps `10 %` distinct from `10` in category clustering.

### Security & trust

- **`OPENAI_API_KEY` is no longer sent to other providers.** Each provider reads only its own key variable.
- **LLM plans can never contain `fill_na` or `drop_columns`** (any mode); they are stripped with a warning, closing a
  hole in the "nothing silently imputed or dropped" invariant. An LLM recipe is also dry-run on the first rows and falls back to the rules planner if it cannot run.
- `sample` exposure is documented as what it is (values partly redacted, no approval step) and warns which columns are sent; `llm_exposure` typos raise a clear error; `max_tokens_budget` without `llm=` warns.
- ReDoS-prone `replace` patterns and regex back-references to missing groups are refused **at load**.

### Added

- **Plugins that travel.** `cleanframe.plugins`: entry points (`cleanframe.plugins` group), `CLEANFRAME_PLUGINS`,
  `--plugin MODULE`, `cf.load_plugins()`, and lazy discovery when a recipe names an unknown op.
  `register_op(codegen=, helpers=, streamable=)` lets a custom op export as standalone pandas and stream (default-deny). See the new plugin guide.
- **`--json`**: one machine-readable summary object on stdout (human output to stderr) for every command, on success and failure — including usage errors (`status: usage_error`, exit 2), and `ops`/`detectors` list themselves. A workbook `apply --chunksize` is now refused instead of ignored.
- **`normalize_unicode` op + `unicode` detector**: NFC-normalises text and removes zero-width spaces, word joiners, BOMs and non-breaking spaces (zero-width joiners are kept). It fixes `café` typed two ways splitting a category, and invisible characters that `strip` leaves behind.
- **Logging**: the library logs through `logging.getLogger("cleanframe")` (silent `NullHandler`); `--verbose` enables INFO.
- **`header_row=` / `--header-row`** for files with title rows above the header, with a suggested value when the header looks misplaced; read-time warnings for ragged rows and `Total` footer rows; hidden Excel sheets stay hidden on write-back; warnings for uncached formulas and merged cells.
- **Property-based tests (Hypothesis)** for the parser, generated-code parity, determinism, recipe round-trip, "nothing silently dropped" and streaming parity; `scripts/sync_wiki.py` + a test that keeps `docs/` and `wiki/` identical.
- More date formats (`%b %d, %y`, `%B %d, %y`, `%d %b, %Y`, `%d.%m.%y`, `%Y.%m.%d`) — appended, so existing recipes are unaffected.
- A `detect` extra (`charset-normalizer`) that ranks candidate encodings in the refusal message.
- Unknown `options=` keys warn with a did-you-mean.

### Performance

- Pure text ops (`parse_number`, `normalize_unit`, `normalize_phone`, `normalize_values`, `extract_currency`, explicit-format `parse_date`, and the email/url/phone/regex validators) now run **once per distinct value** of an all-string column and fan the results out — identical output, 15-25x faster on repetitive columns (1M rows: `parse_number` 4.5 s → 0.17 s, `normalize_unit` 3.8 s → 0.26 s). Measured end to end on a realistic 18-column, 1M-row file: `clean()` 42 s → 29 s, replay 28 s → 16 s.
- Peak memory is unchanged and is now documented honestly: the frame plus the op-touched columns, about 2x when most columns are cleaned. Use `stream_apply` for row-independent recipes on files that do not fit.

### Changed

- **Behaviour changes worth knowing when upgrading:**
  - Numbers with invalid or ambiguous grouping now become a warned null instead of a guess (`2,5` under `thousands=","`, `1,500 g`).
  - Streaming reads the file twice, and writes a quarantine file only when `quarantine_path` is given.
  - Offset timestamps keep their wall-clock date; mixed-offset columns are converted to UTC.
  - `title_case` no longer capitalises after apostrophes or digits; `normalize_phone` treats `00` as `+`.
  - `to_na` tokens, `replace` pattern/repl and `parse_number` separators must be text; `version: true` and unknown option keys are rejected.
  - `Recipe` defines `__eq__` on its canonical form (it was already unhashable).
- Streaming `apply` measured at ~270 MB peak for 600k rows at `chunksize=50_000` (docs updated with the baseline).
- The validation check registry moved into `cleanframe.checks` (internal; removes the last import cycle).

## [0.3.1] — 2026-09-04

First follow-up to the 0.3.0 release, from the CI and code-scanning results that
only appear once a project is public.

### Fixed

- **`Severity` comparisons were alphabetical, not ordered.** Only `__lt__` was
  defined, so the `str` mixin answered everything else: `Severity.ERROR >
  Severity.INFO` returned `False` because `"error" < "info"` as text. All four
  comparisons are now defined on the severity rank. Library code always used
  `.rank` explicitly, so nothing internal was affected, but any caller comparing
  severities directly got wrong answers. Found by CodeQL's incomplete-ordering
  query.
- **`mypy` failed in CI** with `numpy/__init__.pyi:737: error: Type statement is
  only supported in Python 3.12 and greater`. The `python_version = "3.10"` pin
  also governs how dependency stubs are parsed, and numpy 2.5's stubs use syntax
  that needs 3.12. Removed the pin; minimum-version support is still checked by
  ruff's `target-version` and by the Python 3.10 test job.
- **The wiki sync workflow could not push.** GitHub Actions' built-in token can
  clone a wiki but not write to it, so the job committed and then failed on
  authentication. It now requires the `WIKI_TOKEN` secret and stops up front with
  an explanatory message. `wiki/README.md` claimed no token was needed; corrected.
- Two module-level import cycles removed: `profile` with `ops`, and `recipe` with
  `validate`. `COMMON_DATE_FORMATS` now lives in `ops`, which owns date parsing,
  and is re-exported from `profile` so every existing import keeps working.
  `validate` imports `ValidationRule` for annotations only.
- Empty exception handlers in `fingerprint` and `report` replaced with a helper
  that returns whether a cell is missing, so the intent is in the code rather
  than in a comment beside `pass`.
- Redundant function-level `import re` in the dates detector removed; it now
  reuses the module's compiled pattern instead of recompiling per call.

### Changed

- Workflow actions updated: checkout to v7, setup-python to v7, upload-artifact
  to v7, download-artifact to v8, codeql-action to v4. Clears the Node 20
  deprecation warnings.
- Dependabot groups the `github-actions` ecosystem, so action bumps arrive as one
  pull request instead of one per action.
- CodeQL analyses the library and skips `tests`. Test fixtures are deliberately
  hostile (catastrophic-backtracking patterns, injection payloads, handlers that
  assert something must not raise) and are not part of the wheel, so reporting
  them buries real findings. Configuration lives in
  `.github/codeql/codeql-config.yml`.
- Protocol methods on `LLMClient` and `Planner` carry a docstring instead of a
  bare `...`.
- Installation docs link the package page, show how to pin a version, and mention
  `cleanframe --version`. `CHANGELOG.md` gained the Keep a Changelog link
  definitions now that releases are tagged.

### Verified

335 tests pass on Python 3.10 with the declared minimum pins (pandas 1.5.3,
numpy 1.23.5), on Python 3.13 with pandas 2.3.3, and on Python 3.13 with pandas
3.0.5 and numpy 2.5.2, which is what a fresh `pip install` resolves today.

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

[Unreleased]: https://github.com/inboxpraveen/Cleanframe/compare/v0.3.1...HEAD
[0.3.1]: https://github.com/inboxpraveen/Cleanframe/releases/tag/v0.3.1
[0.3.0]: https://github.com/inboxpraveen/Cleanframe/releases/tag/v0.3.0
[0.2.0]: https://github.com/inboxpraveen/Cleanframe/commits/main
[0.1.0]: https://github.com/inboxpraveen/Cleanframe/commits/main
