# Changelog

All notable changes to CleanFrame are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.6.0] — 2026-10-26

Reasoning models, Liquid AI's d1-ultra, and files as people really send them. Six current
reasoning models now plan and decide end to end (three of them used to return nothing), a
recipe no longer reports drift on the very file it was learned from, Excel's own quirks are
read for what they are, and reading a large workbook takes a sixth of the memory.

### Added

- **Liquid AI's d1 decision models: `decide="openrouter/liquid/d1-ultra"`.** They go through
  OpenRouter's System One route, like Jev; `openrouter/~typesafe/...` aliases do too. Any
  other decision model OpenRouter serves works through `systemone/<vendor>/<model>` with
  `SYSTEMONE_BASE_URL=https://openrouter.ai/api`.
- **Reasoning models in the LLM planner and decisions.** `LLMResponse` gains optional
  `finish_reason`, `reasoning_tokens` and `cost` fields (trailing, so custom clients are
  unaffected). Each call is logged at INFO on `cleanframe.llm` with tokens, reasoning tokens,
  finish reason, seconds and cost. CleanBench now records the cost OpenRouter reports for
  LLM backends, so their `$ / 1k decisions` column is filled.
- **Excel's quirks, read for what they are.**
  - Excel error values (`#DIV/0!`, `#REF!`, `#VALUE!`, `#NAME?`, `#NUM!`, `#NULL!`, `#N/A`,
    `#SPILL!`, …) are formulas that failed in the source workbook. They join the
    disguised-null vocabulary (`ops.EXCEL_ERROR_TOKENS`) and become missing values, with a
    message that says what they are.
  - A new `excel_text` detector unwraps `="00123"`, the text formula exports use to keep
    leading zeros in Excel, when most of a column uses it.
  - A CSV whose first line is Excel's `sep=;` is read with that delimiter, and the line is
    skipped; both are pinned in the recipe's `read:` section.
- JSON Lines saved with a `.json` name are read line by line instead of failing with
  pandas' "Trailing data".
- A warning for rows that repeat the header row (several exports joined together), with the
  `skiprows=[...]` that skips them. The CLI's `--skiprows` now also takes a comma-separated
  list of 1-based data rows (`--skiprows 81,` skips row 81 only).
- `cleanframe.readfix.suggest_header_row_from_rows` and `header_spans_rows`.
- **CleanBench rows for seven systems** (public split, 60 scenarios, 299 questions), run through
  OpenRouter: `deepseek/deepseek-v4-flash-0731` (99.0% accuracy, 397 cells fixed / 0 corrupted),
  `liquid/d1-ultra` (96.7%, 100% safe-apply precision, 690 ms p50, $0.006 per 1k decisions),
  `z-ai/glm-5.3-flash`, `xiaomi/mimo-v2.6-flash`, `qwen/qwen3.7-flash` and
  `minimax/minimax-m3.1-flash` are ranked; `qwen/qwen3.8-flash` is a 10-scenario partial run (p95
  latency of nearly 5 minutes per request). Every model kept safe-apply precision at 98.5% or
  above under CleanFrame's 0.9 bar; the rules alone fix 68 cells and corrupt 16.

### Changed

- **LLM output cap 2,048 (planner) / 4,096 (decisions) → 32,768 tokens**
  (`llm.DEFAULT_MAX_OUTPUT_TOKENS`). Only tokens written are billed. A direct API that refuses
  a cap above the model's limit is retried once at the limit it names; a local server with a
  small context window (vLLM: "maximum context length is 8192 tokens") is retried at the window
  minus the prompt; OpenAI models that refuse `max_tokens` get `max_completion_tokens`. An
  error OpenRouter reports inside a 200 response is now part of the message.
- **LLM HTTP timeouts: 60 s → 15 s to connect and 600 s for the answer**, with one SDK retry
  (the SDK default was two, so a stalled call could hang for half an hour).
- **`max_tokens_budget` lowers the answer's cap** to what the prompt leaves of the budget,
  instead of refusing whenever prompt + maximum output exceeded it. It refuses only when the
  prompt alone leaves less than 1,024 tokens.
- The planner prompt lists each op with the only parameters it accepts and no longer offers
  `fill_na` / `drop_columns`, which an LLM plan may not contain anyway.
- An LLM recipe whose validation or `dedup` names a renamed column's *source* is pointed at
  the output name, instead of the check being skipped. Listed in
  `recipe.meta["llm_renamed_refs"]`.
- The CSV formula guard leaves signed numbers with thousands separators or a percent sign
  alone (`-1,234.50`, `-3.5%`); Excel reads them as numbers.

### Fixed

- **`dedup` on a key column dropped every row that had no key.** pandas counts missing values
  as equal, so `dedup: {subset: [email]}` on a customer file where a third of the emails were
  blank kept one blank-email customer and dropped the rest (55 of 161 rows in a test file,
  proposed by two of the six LLMs tested). With a `subset`, a row whose key columns are all
  missing or blank is now never a duplicate; generated code does the same. Full-row dedup is
  unchanged.
- **Replaying a recipe on the file it was learned from stopped with `format_drift`** (exit 3)
  whenever some dates in a `parse_date` column did not parse (an impossible `31/02/2024`, an
  Excel serial, a `TBD`): 4 of 29 real-world-shaped test files. The fingerprint now records the
  shapes of those values and their share (`unparsed_date_patterns`; shapes only, never values),
  and replay reports only a new shape, or a known one that became far more common, as in a
  DD/MM → MM/DD switch. Streamed replay counts the whole file the same way; `suggest --update`
  re-learns the shapes. Recipes saved before 0.6.0 behave as before.
- **Reasoning models returned no answer.** At the old cap, `qwen/qwen3.8-flash`,
  `qwen/qwen3.7-flash` and `deepseek/deepseek-v4-flash-0731` spent the whole cap reasoning, and
  every plan fell back to the rules. An answer that still does not fit now says so ("used its
  whole 32768-token output cap ... before it wrote an answer") instead of "no JSON object", and
  a `<think>` block in the answer is skipped.
- **A reply missing its last closing brace failed the whole decision request.** Models slip
  like this now and then (`{"answers": {"q": {"probabilities": {"yes": 0.97}}}` - four `{`,
  three `}`); two of seven systems lost a CleanBench scenario to it. A reply the model *finished*
  (`finish_reason` stop) that ends outside a string with closing brackets missing is completed
  and parsed, for decisions and plans alike. A reply cut off by the output cap is never
  completed: it may end mid-number.
- **Header-row hints pointed at data rows.** For a report workbook with a two-row merged header
  the hint said `header_row=6` (a data row); for pivot-table output `header_row=3`; for a CSV
  with one blank header name, "line 2". A hint is now the last row of text labels above the
  first row holding a number or date, and a two-row header is named as such.
- **Reading a 9 MB, 200,000-row workbook took 62 s and 1.7 GB of memory**, 44 s and 1.6 GB of it
  in the advisory check for uncached formulas and merged cells, which loaded the workbook twice.
  The check now streams the sheet XML from the archive: 25 s and 287 MB in all. The size above
  which the check is skipped rose from 25 MB to 200 MB.
- A file that is not a workbook says what it is: an HTML or XML table saved as `.xls` (common
  in bank and ERP exports), a password-protected workbook, a zip without workbook parts.
  These used to surface pandas messages such as "No such keys(s): 'io.excel.zip.reader'".
- The bounded verbatim re-read that looks for type-inference losses no longer repeats the
  read's own warnings.
- A streamed replay warned once per chunk ("values became missing ... (37,649)" ten times for a
  1M-row file in 100k chunks). It now warns once with the whole-file count, repeats any other
  chunk warning once, and `StreamSummary.parse_losses` / `ExecutionResult.parse_losses` carry
  the counts per column.

## [0.5.1] — 2026-10-19

One logo everywhere, a README hero made from real output, and a correct row count in `cleanframe demo`.

### Changed

- The HTML profiling and diff reports show the CleanFrame mark in their header, the same mark as the
  README, the docs and the playground. `docs/assets/Logo.svg` is now the one source of the mark.
- The README hero is rendered from a real `cleanframe demo` run by `scripts/render_brand_assets.py`, in a
  light and a dark version: the messy file, the recipe excerpt, the clean output with its quarantined row,
  and the drift stop on next month's file. It replaces an illustration whose steps were not CleanFrame ops.

### Fixed

- `cleanframe demo` said "1 duplicate row(s) dropped" for the row it had quarantined. Its summary now
  reports the quarantined row on its own and names duplicates only when the recipe dropped some.

## [0.5.0] — 2026-10-18

Decision models, agents, a playground and data CI. CleanFrame can now ask a typed decision model
(TypeSafe's Jev, an open "System One" model, or any LLM through an adapter) the judgment calls rules
leave open, and freeze the confident answers into the recipe. Replay still makes zero AI calls. This
release also fixes two pandas 3 bugs and a rules merge that could fold one real value into another.

### Added

- **Decision models: `clean(decide=...)` / `cleanframe clean --decide SPEC`** (`cleanframe/decisions.py`).
  CleanFrame asks four kinds of typed questions: is `BLR` another name for `Bangalore` (choice); is a typo
  merge the rules made really the same category (yes/no, and a "no" **vetoes** the merge); does an ambiguous
  token such as `none`/`-`/`unknown` mean missing in *this* column (yes/no); which source column holds a
  weakly-matched schema field (choice).
  - Backends: `typesafe/<model>` (`TYPESAFE_API_KEY`), `openrouter/typesafe/<model>` (`OPENROUTER_API_KEY`),
    `systemone/<model>` for self-hosted Jev-compatible servers (`SYSTEMONE_BASE_URL`), `rules` (offline
    heuristics), any LLM spec through an adapter (recorded as `calibrated: false`), or a custom object with
    `.decide()`. The System One wire protocol is implemented with the standard library: no new dependency.
  - Policy: an answer that changes data needs `p ≥ max(mode threshold, decision_threshold)` (0.9 by default).
    Below that it is a recorded suggestion, never executed. Undoing a rules merge only needs `p > 0.5`.
    Confirming every typo merge in a column lifts it to 0.9 confidence so it survives `strict`.
  - Privacy follows `llm_exposure`: under `metadata` only schema questions (names, types, patterns) and
    null-token questions (tokens from CleanFrame's fixed vocabulary) are asked. Category questions need
    `sample` and are only asked about columns whose values repeat.
  - Every answer is recorded in `recipe.meta.decisions` (model, calibrated, threshold, counts, records);
    `CleanResult.decisions` and the CLI's `--json` summary carry the same. Questions are built from sorted
    distinct values, so row order cannot change them. `max_decisions` caps cost, 429/5xx are retried with
    backoff, and a failing backend falls back to the rules plan with a warning (`--no-llm-fallback` raises).
  - New flags: `--decide`, `--decision-threshold`, `--max-decisions`.
- **`cleanframe demo`.** A guided run on sample data that now ships inside the wheel
  (`cleanframe/demo_data/`): profile, clean into a recipe, watch next month's drifted file **stop**, then
  replay the drafted patch. It works right after `pip install`, with no clone and no API key. Also
  `cleanframe.demo.demo_file()` / `run_demo()`. Before this, the README's first command needed a clone.
- `examples/next_month_customers.csv`: the drifted sample the demo uses.
- **MCP server for AI agents**: `cleanframe-mcp`, `cleanframe mcp` or `python -m cleanframe.mcp_server`
  (extra `[mcp]`, `mcp>=2.3`). Seven tools: `profile_data`, `suggest_recipe`, `apply_recipe`,
  `check_drift`, `explain_recipe`, `list_capabilities`, `export_code`.
  - Errors come back structured (`ok`/`kind`) and flagged `isError`. Output is bounded JSON, with every cap reported.
  - The input file is never written, and drift stops a replay by default.
  - `CLEANFRAME_AGENT_HIDE_VALUES=1` replaces quoted cell values with `"<hidden>"`.
  - The same tools are plain functions in `cleanframe.agent_tools`, with no `mcp` dependency.
- **Agent Skill** `skills/cleanframe/SKILL.md`, installed with `npx skills add inboxpraveen/Cleanframe`. It
  teaches Claude Code, Codex, Cursor and other agents the recipe workflow and its hard rules, instead of
  throwaway pandas.
- **GitHub Action "CleanFrame Data CI"** (`action/`, `uses: inboxpraveen/Cleanframe/action@v0.5.0`).
  - Replays committed recipes over data files on pull requests or a schedule, and fails the check on schema
    drift (exit 3) or validation failure (exit 4).
  - Writes a per-file job-summary table and annotations.
  - Outputs: `drift`, `changed-cells`, `quarantined`, `failed-files`, `exit-code`, `results-file`.
  - Inputs reach its scripts only through environment variables, and CleanFrame runs in an isolated virtualenv.
  - See `docs/github-action.md` and `examples/github-action/data-ci.yml`.
- **In-browser playground** (`playground/`, https://inboxpraveen.github.io/Cleanframe/).
  - Drop a CSV/TSV/XLSX and get a Mess Score (100 − quality score), the detected problems, the YAML recipe, a
    cell-level diff, the generated pandas code and both HTML reports.
  - A shareable score card shows counts only, never names or values.
  - CleanFrame runs client-side in Pyodide 314.0.7 in a Web Worker. A Content-Security-Policy limits the page
    to its own site and the pinned Pyodide CDN path, so files never leave the browser.
  - Deployed by `.github/workflows/pages.yml` from a wheel built from `main`; `python playground/dev_server.py`
    serves it locally.
- **CleanBench** (`bench/`, not shipped in the wheel or sdist): a benchmark for data-cleaning decisions.
  - It runs `decide=` backends through CleanFrame's real pipeline on 60 hand-labelled messy tables
    (299 questions: aliases, typo merges, null tokens, schema mapping).
  - Gold is written as equivalence classes and checked by an offline oracle pass. Every scenario lists the
    questions it must produce.
  - Metrics: safe-apply precision (of the changes a system makes, how many are right), coverage@0.9, macro
    accuracy with scenario-bootstrap CIs, ECE, Brier, cells fixed vs corrupted, latency, $ per 1k decisions.
  - `python -m bench.cleanbench {validate,run,leaderboard,export,hash}`. `export` writes standalone System One
    requests with gold, for other leaderboards.
  - A private held-out split is committed only as a SHA-256.
  - The rules baseline is on `bench/LEADERBOARD.md` with its misses visible. Docs: `docs/benchmark.md`.
- CI: a required **pandas 3** job (pandas 3.0 shipped in January; the existing job only tried
  pre-releases), and an **action smoke** job that runs the action on Linux, Windows and macOS runners.

### Fixed

- **pandas 3: `dedup` with `ignore_case` silently kept case-variant duplicates.** The key builder only
  case-folded `object` columns, and pandas 3 reads text as dtype `str`. It now folds `str`/`string`/categorical
  columns too, in the executor and in exported code alike.
- **pandas 3.1: `fill_na` warns, and pandas 4 would make it fail,** when the fill value doesn't fit the
  column: `empty` or a text value into a number or date column, `zero` into text (pandas 3's default
  text dtype). pandas used to turn such a column into `object` silently; 3.1 deprecates that. The op and
  exported code now cast explicitly first, so results are unchanged and nothing warns. Caught by the
  pandas pre-release CI job on 3.1.0rc0.
- **A typo merge could fold one real value into another.** `Austria` (rare) was merged into `Australia`
  in `review` and `auto` mode because the names score 0.875 similar. A typo merge now also needs the two
  spellings to be one edit apart, so look-alikes and opposites two letters apart stay separate
  (`Hypothyroidism`/`Hyperthyroidism`, `Prednisone`/`Prednisolone`). Real typos (`Banglore`, `Kolkatta`,
  `Califronia`) still merge; rarer two-edit typos are left to a human or a decision model.
- **Categories that differ only in `+`, `-` or `#` were merged as case variants** (confidence 0.9, applied
  in every mode): blood groups `A-` → `A+`, `O-` → `O+`; `C++`/`C#` → `C`. These marks now always count.
  A hyphen between letters (`New-York`) is still cosmetic.
- **`similarity()` scored any two non-Latin strings as identical** (both reduce to an empty ASCII key), so
  `दिल्ली` matched `मुंबई` perfectly in schema mapping and drift. Such pairs are now compared as text.
- Negation is detected inside phrases too (`Very dissatisfied` / `Very satisfied`).
- **The quality score ignored date-format and money-as-text problems.** Those issues report `unparsed: 0`
  when every value parses, and the scorer read that as "no rows affected". So a column in three date
  formats cost nothing. The detectors now report an explicit `affected` count, and the scorer prefers it.
  Scores for such files drop (the bundled sample goes from 87 to 80); that is the fix, not a regression.
- `docs/plugins.md` showed `cleanframe --plugin X apply …`, which is a usage error; global flags go after
  the subcommand.
- Two tests assumed pandas 2 string semantics and failed on pandas 3 without a library bug behind them;
  they now accept either representation of a missing text cell.

### Changed

- `pyproject.toml` and `CITATION.cff` name the maintainer (Praveen Kumar); PyPI keywords and project
  URLs (Playground, Funding) added.
- `docs/cli.md`: the `--json` summary documents `usage_error` and the `decisions` field.

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

[Unreleased]: https://github.com/inboxpraveen/Cleanframe/compare/v0.6.0...HEAD
[0.6.0]: https://github.com/inboxpraveen/Cleanframe/releases/tag/v0.6.0
[0.5.1]: https://github.com/inboxpraveen/Cleanframe/releases/tag/v0.5.1
[0.5.0]: https://github.com/inboxpraveen/Cleanframe/releases/tag/v0.5.0
[0.4.0]: https://github.com/inboxpraveen/Cleanframe/releases/tag/v0.4.0
[0.3.1]: https://github.com/inboxpraveen/Cleanframe/releases/tag/v0.3.1
[0.3.0]: https://github.com/inboxpraveen/Cleanframe/releases/tag/v0.3.0
[0.2.0]: https://github.com/inboxpraveen/Cleanframe/commits/main
[0.1.0]: https://github.com/inboxpraveen/Cleanframe/commits/main
