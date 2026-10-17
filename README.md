<p align="center">
  <img src="https://raw.githubusercontent.com/inboxpraveen/Cleanframe/main/docs/assets/Logo.png" alt="CleanFrame logo" width="180" />
</p>

<h1 align="center">CleanFrame</h1>

<p align="center">
  <b>The reproducible data-cleaning engine for Python.</b><br/>
  AI decides once. The recipe runs forever — deterministic, diffable, reviewable.
</p>

<p align="center">
  <a href="https://inboxpraveen.github.io/Cleanframe/"><b>▶ Try it in your browser</b></a> (nothing to install, your file never leaves the tab)
  &nbsp;·&nbsp; <code>pip install cleanframe-engine && cleanframe demo</code>
</p>

<p align="center">
  <img src="https://raw.githubusercontent.com/inboxpraveen/Cleanframe/main/docs/assets/Hero.png" width="auto" alt="CleanFrame"/>
</p>

<p align="center">
  <a href="https://pypi.org/project/cleanframe-engine/"><img src="https://img.shields.io/pypi/v/cleanframe-engine" alt="PyPI"/></a>
  <a href="https://github.com/inboxpraveen/Cleanframe/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-blue" alt="License"/></a>
  <a href="https://github.com/inboxpraveen/Cleanframe/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/inboxpraveen/Cleanframe/ci.yml?branch=main" alt="CI"/></a>
  <img src="https://img.shields.io/badge/python-3.10%2B-blue" alt="Python"/>
  <img src="https://img.shields.io/badge/LLM-optional-success" alt="LLM optional"/>
  <a href="https://github.com/inboxpraveen/Cleanframe/wiki"><img src="https://img.shields.io/badge/docs-wiki-informational" alt="Wiki"/></a>
  <a href="https://inboxpraveen.github.io/Cleanframe/"><img src="https://img.shields.io/badge/playground-in_browser-ff6f00" alt="Playground"/></a>
  <a href="https://github.com/inboxpraveen/Cleanframe/blob/main/docs/decisions.md"><img src="https://img.shields.io/badge/decision_models-Jev--style-8a2be2" alt="Decision models"/></a>
</p>

---

Every team has that file. The vendor spreadsheet that arrives monthly with dates in three formats. The CRM export where `Bengaluru`, `Bangalore`, and `BLR` are three different cities. The finance sheet with `₹1,20,000` in a column typed as text.

You clean it by hand. Next month, it arrives broken in a new way, and you clean it again.

**CleanFrame ends that loop.** It profiles your data, detects issues, proposes a cleanup plan (with an LLM's help — or without one), executes it with pure pandas, and saves the whole thing as a **recipe**: a versionable YAML file that replays on every future file with **zero AI calls**, and alerts you when the incoming schema drifts.

```
AI suggests.  Pandas executes.  Rules validate.  You approve.  Everything is reproducible.
```

## Documentation

| | |
|--|--|
| **[Wiki (full docs)](https://github.com/inboxpraveen/Cleanframe/wiki)** | Getting started, API, recipe/schema specs, production guide |
| **[docs/](https://github.com/inboxpraveen/Cleanframe/tree/main/docs)** | Same guides in-repo |
| **[CONTRIBUTING.md](https://github.com/inboxpraveen/Cleanframe/blob/main/CONTRIBUTING.md)** | Invariants + how to add detectors/ops |
| **[SECURITY.md](https://github.com/inboxpraveen/Cleanframe/blob/main/SECURITY.md)** | Vulnerability reporting |
| **[CHANGELOG.md](https://github.com/inboxpraveen/Cleanframe/blob/main/CHANGELOG.md)** | Release notes |

## Why not just use an LLM agent on my dataframe?

Because you can't ship "the model probably fixed it" to production. Chat-with-your-data tools are great for exploration and say so themselves — they are not built for pipelines. CleanFrame is built on one rule:

> **The LLM never touches your data. It only writes the plan.**

The plan compiles to deterministic pandas operations. Same input → same output, every time. Every changed cell is tracked. Every step is reviewable, reversible, and exportable as plain Python you can read.

## 30-second demo

No API key, no sample files to download. The demo data ships inside the package:

```bash
pip install cleanframe-engine
cleanframe demo
```

It profiles a messy customer file (HTML report + quality score), cleans it into a recipe, then replays
that recipe on *next month's* file. That file has a renamed column and a new date format, so the replay
**stops** and tells you why. Finally the demo drafts the patch that makes it replay cleanly. Every step
prints the command to run on your own data.

The same thing in Python:

```python
import pandas as pd
import cleanframe as cf

df = pd.read_csv("cleanframe-demo/messy_customers.csv")      # written by `cleanframe demo`

result = cf.clean(
    df,
    target_schema="cleanframe-demo/customer.schema.yaml",    # or: cf.infer_schema(df)
    mode="review",                                           # review | auto | strict
    # decide="typesafe/jev-latest",   # optional decision model for judgment calls (TYPESAFE_API_KEY)
)

result.diff.show()                # cell-level before/after, git-diff style
result.recipe.save("customer.recipe.yaml")   # ← the durable artifact
result.code.save("clean_customers.py")       # plain pandas, no cleanframe dependency
clean_df = result.dataframe
result.quarantine                 # rows validation held back, with reasons — never deleted
```

The sample file has one genuinely invalid email, so the demo quarantines that row and
cleans the other five.

Next month, the file comes back. No LLM, no tokens, no variance:

```bash
cleanframe apply new_customers.csv --recipe customer.recipe.yaml --out clean.csv
```

If the new file's schema drifted (a renamed column, a new currency format), CleanFrame **stops and tells you** instead of silently corrupting data:

```
⚠ Schema drift detected in new_customers.csv
  • Column "Amt (INR)" is new — 94% match to recipe column "amount_inr"
  • 312 value(s) in "signup_date" match no allowed date format (new: 'Jan 5, 26')
  • 40 of 312 numeric value(s) in "Amount" do not fit the recipe's number format
    (decimal '.', thousands ','; new: '€1.200,50')
  Run `cleanframe suggest new_customers.csv --recipe <recipe.yaml> --update` to review a patch.
```

## Decision models: AI makes the judgment calls, under a 0.9 bar

Some cleaning steps are judgment calls, not parsing. Is `BLR` another name for `Bangalore`? Is `Banglore`
a typo, or a different place? Does `none` in an *allergies* column mean "missing" or "no allergies"? Which
column is the schema's `amount_inr`? Rules can't settle these safely. With `decide=`, a **decision model**
answers each one as a typed question (pick an option, or yes/no) with a probability. That can be
[Jev](https://typesafe.ai), an open "System One" model, or any LLM through an adapter.

```bash
cleanframe clean vendor.csv --decide typesafe/jev-latest --llm-exposure sample
```

- **An answer changes data only at p ≥ 0.9** (`decision_threshold`). Below that it is recorded as a
  suggestion in the recipe and never executed. A confident "no, these differ" vetoes a typo merge the
  rules would have made.
- **Asked once, at planning time.** Accepted answers become ordinary recipe ops (`normalize_values`,
  `to_na`, `rename_to`), and every answer is logged in `recipe.meta.decisions`. Replay makes zero AI calls.
- **Privacy follows `llm_exposure`.** Category values are sent only under `sample`, and only for columns
  whose values repeat. Dates and numbers are never asked: they stay with the deterministic parsers.
- Backends: `typesafe/…`, `openrouter/typesafe/…`, self-hosted `systemone/…`, offline `rules`, or any
  `--llm` spec. No SDK is needed.

How good are decision models at this? **[CleanBench](https://github.com/inboxpraveen/Cleanframe/blob/main/bench/README.md)** measures it on hand-labelled messy
columns full of traps, scoring *safe-apply precision*: of the answers that would change your data, how
many are right. See [Decision models](https://github.com/inboxpraveen/Cleanframe/blob/main/docs/decisions.md).

## Bring your own API key (or no key at all)

CleanFrame is LLM-optional and provider-agnostic.

| Mode | What runs | Data leaves your machine? |
|---|---|---|
| **Rules-only** (default) | Deterministic detectors + heuristics | Never |
| **Metadata** | LLM sees column names, dtypes, and *value patterns* (regex sketches) — never raw values | Only metadata |
| **Sample** | LLM sees a small shuffled sample of *values*. Emails, phones and long strings are redacted; other short values (names, cities) are sent verbatim. Passing `llm_exposure="sample"` is the opt-in — there is no separate approval step | Sample values, per the redaction above |
| **Replay** | Saved recipes | Never — recipes need no LLM |
| **Decisions** (`decide=`) | Typed questions to a decision model while planning; same exposure rules | Metadata only by default; distinct category values under `sample` |

- Keys come from environment variables (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
  `OPENROUTER_API_KEY`, `GROQ_API_KEY`, …). CleanFrame never stores or logs them, and
  each provider reads **only its own** variable — `OPENAI_API_KEY` is never sent to
  another provider.
- Any provider that speaks OpenAI Chat Completions works out of the box —
  Anthropic (native), OpenAI, OpenRouter, Groq, Together, Fireworks, DeepSeek,
  Mistral, Google Gemini, xAI, Perplexity, Cohere, plus local Ollama / LM Studio
  or any custom `OPENAI_BASE_URL`. Spec format: `provider/model`
  (e.g. `openrouter/anthropic/claude-sonnet-4`, `groq/llama-3.3-70b-versatile`).
- Cost cap: `cf.clean(..., max_tokens_budget=50_000)` is checked before each call (against an
  estimate) and again against the usage the provider reports — leave headroom, since a call
  that overshoots has already been billed.
- Fully offline / air-gapped operation is a supported first-class mode, not a degraded one.

## What it cleans

| Problem | Example |
|---|---|
| Invisible characters | zero-width spaces, BOMs, non-breaking spaces and decomposed accents (`café` typed two ways) → one normalised form |
| Column name chaos | `Cust Name`, `customer_name`, `CustomerName` → `customer_name` |
| Date formats | `12/01/24`, `1 Jan 2024`, `2024-01-01` → ISO dates |
| Currency & numbers | `₹1,20,000`, `$1,200`, `-$5`, `(1,200)`, `1.234,56`, `1200 INR` → typed floats (+ a currency column); a value whose grouping is invalid or ambiguous becomes a *counted, warned* null, never a guess |
| Category variants | `Bengaluru` / `bengaluru ` / `BENGALURU` and typos (`Banglore`, one edit away) → one canonical value; look-alikes such as `Austria`/`Australia` stay apart; abbreviations such as `BLR` need a seed map or a [decision model](https://github.com/inboxpraveen/Cleanframe/blob/main/docs/decisions.md) |
| Emails & phones | validation, normalization, `00`/`+` international prefixes, optional default country code |
| Duplicates | exact duplicates are dropped (tracked in the diff); fuzzy near-duplicates and key collisions are *reported* with keep/drop proposals for you to act on |
| Missing values | detected and explained; strategies proposed, never silently applied |
| Units | `5kg`, `5000 g`, `5 KG` → normalized |
| Schema mapping | messy file → your target schema, with confidence scores |
| Outliers | flagged with evidence — **detected, never auto-"fixed"** |

## Production-ready defaults

CleanFrame is built for pipelines, not just demos:

- **Plan once, replay forever** — commit recipes; fail on schema drift
- **Multi-sheet workbooks** — clean every Excel tab into one reviewable recipe; write-back refuses to overwrite the source in place
- **Out-of-core streaming** — replay row-independent recipes over larger-than-RAM CSVs at chunk-bounded memory
- **Format auto-detection** — encoding (utf-8 → cp1252) and delimiter sniffed at read time, pinned into the recipe for replay
- **Bounded memory** — detector sampling (50k values) + capped cell-diff detail (100k); the diff snapshots the op-touched columns (peak is the frame plus those columns — about 2× when most columns are cleaned; use streaming for files that do not fit)
- **Safe CSV exports** — spreadsheet formula injection escaped by default
- **Regex guards** — oversized / nested-quantifier patterns rejected
- **Visible degradation** — missing columns, unparseable values and LLM fallbacks warn instead of failing silently
- **Quarantine, don't delete** — validation failures are held with reasons
- **Verbatim reads** — `text=True` keeps leading zeros, literal `NA` and `1e5` exactly as the file has them; otherwise CleanFrame warns about what pandas' type inference changed on read
- **Never overwrites its input** — writing output over the source file takes an explicit `--overwrite`, and every write lands via a temporary file
- **Scriptable exits** — distinct exit codes for usage errors, data errors, drift stops and validation failures; `--json` prints one machine-readable summary on stdout
- **Number-format drift** — a file whose numbers stop fitting the recipe (`€1.200,50` against a `.`-decimal recipe) stops the replay instead of yielding `1.2005`
- **Strict recipes** — unknown ops/params, duplicate keys and bad regexes fail at *load*; plain YAML keys like `yes`, `010` or `12:30` stay column names
- **Generated code = executor** — exported pandas embeds the executor's own parsers, and property-based tests check they agree
- **Plugins that travel** — custom ops/detectors load from entry points, `--plugin` or `CLEANFRAME_PLUGINS`, and can opt in to code export and streaming
- **Gated AI decisions** — a decision model's answer changes data only at p ≥ 0.9, every answer is logged in the recipe, and replay never calls the model
- **pandas 1.5 → 3.x** — CI runs the minimum pins, pandas 2 and pandas 3

See the [Production Guide](https://github.com/inboxpraveen/Cleanframe/wiki/Production-Guide).

## Data CI on GitHub

Fail a pull request when a data file drifts from its committed recipe:

```yaml
- uses: actions/checkout@v7
- uses: inboxpraveen/Cleanframe/action@v0.5.0
  with:
    files: data/incoming/*.csv
    recipe: recipes/customer.recipe.yaml
```

Every file is replayed, the job summary gets a per-file table, and the check fails on schema drift
(exit 3) or a failing validation (exit 4). See [GitHub Action](https://github.com/inboxpraveen/Cleanframe/blob/main/docs/github-action.md).

## Use it from your AI agent

Coding agents love to "just fix the CSV" with throwaway pandas that nobody reviews and nothing replays.
Give them CleanFrame instead, and they write a **recipe**: reviewed in git, replayed deterministically,
stopped by drift.

```bash
npx skills add inboxpraveen/Cleanframe          # Agent Skill: Claude Code, Codex, Cursor, …
pip install "cleanframe-engine[mcp]"            # MCP server: profile, suggest, apply, drift, explain, export
claude mcp add cleanframe -- cleanframe-mcp
```

See [AI agents](https://github.com/inboxpraveen/Cleanframe/blob/main/docs/agents.md) for Claude Desktop, Cursor, VS Code and Codex configs.
Tools: `profile_data`, `suggest_recipe`, `apply_recipe`, `check_drift`, `explain_recipe`, `list_capabilities`,
`export_code`. There is no LLM inside the server, the input file is never overwritten, and drift stops the replay.

<!-- mcp-name: io.github.inboxpraveen/cleanframe -->

## The recipe: your durable artifact

```yaml
# customer.recipe.yaml — generated by CleanFrame, edited by you, owned by git
version: 1
# source_fingerprint: {...}   ← stamped automatically; drives drift detection
columns:
  "Customer Name":
    rename_to: customer_name
    ops: [strip_whitespace, title_case]
  "Signup Date":
    rename_to: signup_date
    parse_date: {dayfirst: true, allowed: ["%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d"]}
  "Amount":
    rename_to: amount_inr
    ops: [{remove_symbols: ["₹", ","]}, {cast: float}]
  "City":
    rename_to: city
    normalize_values: {Bengaluru: Bangalore, BLR: Bangalore, Bombay: Mumbai}
  "Email":
    rename_to: email
    ops: [strip_whitespace, normalize_email]
validate:
  - {column: email, check: valid_email, on_fail: quarantine}
  - {column: amount_inr, check: ">= 0", on_fail: quarantine}
```

A worked example ships in [`examples/`](https://github.com/inboxpraveen/Cleanframe/tree/main/examples) (`messy_customers.csv`,
`customer.schema.yaml`, `customer.recipe.yaml`).

Recipes are reviewed in PRs like code, replayed in CI/Airflow/dbt, and exported as
plain pandas via `result.code` — CleanFrame plays *with* your validation stack,
not against it.

## How it compares

| | CleanFrame | PandasAI | Great Expectations / pandera | YData Profiling | OpenRefine | Flatfile / OneSchema |
|---|---|---|---|---|---|---|
| Fixes data (not just reports) | ✅ | ⚠️ ad-hoc | ❌ validates only | ❌ profiles only | ✅ | ✅ |
| Deterministic & reproducible | ✅ recipes | ❌ | ✅ | ✅ | ⚠️ manual | ⚠️ |
| Production pipelines | ✅ | ❌ (exploration tool) | ✅ | ⚠️ | ❌ GUI | ✅ |
| Python-native library + CLI | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ JS widget / SaaS |
| Works with zero LLM / offline | ✅ | ❌ | ✅ | ✅ | ✅ | ⚠️ |
| Cell-level diff & lineage | ✅ | ❌ | ❌ | ❌ | ⚠️ | ⚠️ |
| Schema-drift alerts on re-import | ✅ | ❌ | ⚠️ | ❌ | ❌ | ✅ ($6k+/yr) |
| AI judgment calls gated by probability, frozen for replay | ✅ | ❌ | ❌ | ❌ | ❌ | ⚠️ |
| Free & open source | ✅ Apache-2.0 | ✅ | ✅ | ✅ | ✅ | ❌ |

Use PandasAI to *explore*. Use pandera/GX to *guard*. Use **CleanFrame to fix — repeatably.**

## Architecture

```
CSV / Excel / DataFrame
   │
   ▼
Profiler ─→ Issue Detectors ─→ [Decisions] ─→ Planner ──────────→ Recipe (YAML)
             (deterministic)    (optional:      (rules, or            │
                                 typed Qs to     LLM-assisted)        ▼
                                 Jev / an LLM,                Executor (pure pandas)
                                 gated p ≥ 0.9)
                                                      │
                                                      ▼
                                        Validator · Cell-diff · HTML report
```

Detectors and ops are plugins — write your own in ~30 lines:

```python
@cf.register_op("normalize_iban", streamable=True,
                codegen=lambda p, col: [f"df[{col!r}] = df[{col!r}].str.replace(' ', '').str.upper()"])
def normalize_iban(series: pd.Series) -> pd.Series:
    return series.str.replace(" ", "").str.upper()

@cf.detector("iban")
def detect_iban(series: pd.Series) -> cf.Issues: ...   # propose cf.Op("normalize_iban")
```

Package it under the `cleanframe.plugins` entry-point group and `cleanframe apply` in CI
finds it — see the [plugin guide](https://github.com/inboxpraveen/Cleanframe/wiki/Plugins).

## Install

```bash
pip install cleanframe-engine
pip install "cleanframe-engine[excel]"      # Excel
pip install "cleanframe-engine[parquet]"    # Parquet
pip install "cleanframe-engine[llm]"        # Anthropic + OpenAI SDKs
pip install "cleanframe-engine[mcp]"        # MCP server for AI agents
pip install "cleanframe-engine[all]"
# decision models (decide=) need no extra: the System One protocol uses the standard library

# straight from git
pip install "cleanframe-engine @ git+https://github.com/inboxpraveen/Cleanframe"
```

Python 3.10+. The distribution is [`cleanframe-engine`](https://pypi.org/project/cleanframe-engine/); the import package is
`cleanframe` (`import cleanframe as cf`), and the CLI is `cleanframe` (or
`python -m cleanframe`).

## Roadmap

- [x] Profiler, core detectors (dates, currency, categories, dedup, nulls, schema mapping)
- [x] Recipe format v1 + replay + drift detection
- [x] HTML reports, cell-level diff
- [x] Production safety guards (sampling, CSV sanitisation, regex limits, diff caps)
- [x] Multi-sheet Excel workbooks (clean every tab, safe write-back)
- [x] Selective ingestion (sheet / columns / rows)
- [x] Out-of-core streaming replay (larger-than-RAM CSVs)
- [x] Read-time format auto-correction (encoding + delimiter)
- [x] Plugin discovery; custom ops that export and stream
- [x] `--json` run summaries, library logging, property-based invariant tests
- [x] Decision models (`decide=`): typed, probability-gated judgment calls frozen into recipes
- [x] CleanBench: a public benchmark and leaderboard for data-cleaning decisions
- [x] MCP server + Agent Skill; GitHub Action for data CI; in-browser playground; `cleanframe demo`
- [ ] Decision questions for more judgment calls (unit families, near-duplicate rows), with per-question calibration from CleanBench
- [ ] pandera / GX / dbt exporters
- [ ] Polars backend
- [ ] Recipe registry for teams

## Contributing

The detector plugin system exists so the community owns the long tail of messy-data weirdness. Good first issues are tagged [`good-first-detector`](https://github.com/inboxpraveen/Cleanframe/issues). See [CONTRIBUTING.md](https://github.com/inboxpraveen/Cleanframe/blob/main/CONTRIBUTING.md).

## Sponsoring

CleanFrame is free, Apache-2.0, and will stay that way. No feature is paywalled for a person with a laptop
and a messy CSV. If it saves your team recurring hours, [sponsorship](https://github.com/sponsors/inboxpraveen)
funds detector coverage, CleanBench runs on new models, and fast fixes for anything that costs you data.

| Tier | What it funds, and what you get |
|---|---|
| **Supporter** ($5/mo) | Your name in the release notes |
| **Cleaner** ($25/mo) | Your detector request jumps the queue |
| **Team** ($100/mo) | Logo here, priority bug triage, a vote on the roadmap |
| **Production** ($500/mo) | A quarterly recipe/pipeline review call and private CleanBench runs on your anonymised data |
| **One-time** | A regional detector pack (India GST/PAN/lakh–crore, EU IBAN/VAT, …) built in the open, credited to you |

---

<p align="center"><i>Profile it once. Recipe it forever.</i></p>
