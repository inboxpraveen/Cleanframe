# Decision models (`decide=`)

Some cleaning steps are not parsing, they are **judgment calls**:

| Question | Why rules can't settle it |
|---|---|
| Is `BLR` another name for `Bangalore`? | Needs world knowledge |
| Is `Banglore` a typo of `Bangalore`, or a different place? | Spelling similarity is not meaning (`Paris` / `Parish` are one letter apart) |
| Does `none` in an *allergies* column mean "missing" or "no allergies"? | Depends on the column |
| Which source column holds the schema's `amount_inr`? | Headers like `Amt (INR)` are only weak name matches |

CleanFrame's rules answer these conservatively or not at all. With `decide=`, a
**decision model** answers each one as a typed question (*pick one of these
options*, or *yes/no*) with a probability. TypeSafe's Jev, an open "System One"
model, or any LLM through an adapter can play that role. CleanFrame applies only the answers it can stand behind and
freezes them into the recipe as ordinary ops. **Replay never calls the model
again.**

```text
Jev decides once.  Pandas executes.  The recipe replays forever.
```

## Quick start

```python
import cleanframe as cf

result = cf.clean(
    "vendor_export.csv",
    decide="typesafe/jev-latest",     # or openrouter/typesafe/jev-1.14, systemone/<model>, rules, any LLM spec
    llm_exposure="sample",          # category questions send distinct values (see Privacy)
)
result.recipe.save("vendor.recipe.yaml")
print(result.decisions.summary())   # asked / applied / suggested / vetoed
```

```bash
export TYPESAFE_API_KEY=...
cleanframe clean vendor_export.csv --decide typesafe/jev-latest --llm-exposure sample
# Illustrative output (the numbers depend on your data and the model):
# ✓ Decisions → 23 asked (jev-1.14.0) · 2 applied · 1 suggested · 0 vetoed (bar p ≥ 0.9)
#     applied   City: BLR → Bangalore  (p=0.97)
#     applied   City: Bombay → Mumbai  (p=0.96)
#     suggested City: Calcutta → Kolkata  (p=0.81)
```

## Backends

| `decide=` | What answers | Key (each backend reads only its own) |
|---|---|---|
| `typesafe/jev-latest` | TypeSafe's API (`https://api.typesafe.ai`) | `TYPESAFE_API_KEY` (`TYPESAFE_BASE_URL` to override the URL, as in TypeSafe's SDK) |
| `openrouter/typesafe/jev-1.14` | Jev through OpenRouter's System One route | `OPENROUTER_API_KEY` |
| `systemone/<model>` | Any self-hosted Jev-compatible server (imajev, Laya, …) at `SYSTEMONE_BASE_URL` | `SYSTEMONE_API_KEY` (optional) |
| `rules` | CleanFrame's own heuristics, offline: a dry run and the benchmark baseline | none |
| any LLM spec (`anthropic/claude-sonnet-4-6`, `ollama/qwen3`, …) | The LLM, through the same typed questions | the provider's usual key |
| an object with `.decide(state, questions)` | Your own backend | — |

Jev-compatible backends speak TypeSafe's **System One** contract
(`POST /v1/systemone` with `model`, `state` and named `questions`). CleanFrame
implements it with the standard library, so no SDK is installed. `jev-latest` follows
TypeSafe's newest release. To get the same answers next time, pin a version that
`GET https://api.typesafe.ai/v1/models` lists (TypeSafe's SDK shows pins such as
`jev-1.13.0`). OpenRouter names the model `typesafe/jev-1.14`.

**Check the first real run.** A backend error (a wrong model name, a bad key) degrades to the
rules-only plan with a warning. Run once with `--no-llm-fallback --json` and confirm that
`decisions.answered` is above 0.

LLM backends state probabilities that are not trained to be calibrated, so they
are recorded as `calibrated: false`. Prefer a decision model when you lower
`decision_threshold`.

## What gets asked

| Question | Type | Asked when | Applied as |
|---|---|---|---|
| Is value *V* another name for one of the column's other values? | choice (+ "distinct") | Category columns whose values repeat; the most common value is never asked about | `normalize_values` |
| Are *variant* and *canonical* the same category? | yes/no | Every typo merge the rules made (similarity ≥ 0.86, one edit apart, rare vs frequent, no `+`/`-`/`#` difference) | confirms or **vetoes** the merge |
| Does *token* stand for missing data here? | yes/no | Ambiguous null tokens (`none`, `nil`, `-`, `--`, `?`, `unknown`) that rules never convert | `to_na` |
| Which source column holds target field *T*? | choice (+ "none") | Weak or missing schema matches | `rename_to` |

Dates and numbers are **not** asked. Decision models are documented to be weak at
arithmetic and dates, so those stay with CleanFrame's deterministic parsers.

## The policy: who gets to change your data

* **Changing data needs `p ≥ max(mode threshold, decision_threshold)`** (0.9 by
  default; strict mode's 0.85 never lowers it). This covers merging an alias,
  turning a token into a missing value and renaming a column onto a schema field.
  `cf.clean` runs the recipe immediately, so the bar is the gate.
* **Below the bar it is a suggestion.** The answer is recorded in
  `recipe.meta.decisions` and as a `decision_suggestion` issue, and it is never
  executed. A human can copy it into the recipe.
* **Undoing a rules merge needs only `p > 0.5`.** A confident "no, these are
  different" keeps two spellings apart, which cannot corrupt data. Confirming every
  typo merge in a column lifts that fix to 0.9 confidence, so it survives `strict`.
* For a choice, CleanFrame gates on the smaller of the option's probability and
  the model's reported confidence.
* A group of aliases (`BLR`, `Bengaluru`, `Bangalore`) takes the **most frequent**
  spelling as canonical (the category detector's rule), never the direction of an
  answer.

## What the recipe records

```yaml
columns:
  City:
    ops:
    - strip_whitespace
    - normalize_values: {Banglore: Bangalore}     # rules (typo merge, confirmed)
    - normalize_values: {BLR: Bangalore, Bombay: Mumbai}   # decisions
meta:
  decisions:
    model: jev-1.14.0
    calibrated: true
    threshold: 0.9
    asked: 23
    answered: 23
    applied: 2
    suggested: 1
    vetoed: 0
    records:
    - {kind: category_merge, column: City, subject: Banglore -> Bangalore, answer: 'yes', p: 0.98, outcome: confirmed}
    - {kind: category_alias, column: City, subject: BLR, answer: Bangalore, p: 0.97, outcome: applied}
    - {kind: category_alias, column: City, subject: Bombay, answer: Mumbai, p: 0.96, outcome: applied}
    - {kind: category_alias, column: City, subject: Calcutta, answer: Kolkata, p: 0.81, outcome: suggested}
```

Records keep everything that changed data, was suggested, vetoed or confirmed. The
confident "this is a distinct value" answers are counted but not listed.

## Privacy

`decide=` follows `llm_exposure`:

| Exposure | Asked |
|---|---|
| `metadata` (default) | Schema questions (column names, semantic types, value **patterns**) and null-token questions (the token always comes from CleanFrame's fixed null vocabulary, never from free-form data) |
| `sample` | Also category questions, which send the column's **distinct values and their counts**, plus redacted examples as for the LLM planner. A `CleanFrameWarning` names the columns |
| `none` | Nothing. The backend is not called |

Category questions are only asked about columns whose values repeat (a profiled
`categorical` column, or two or more rows per spelling on average). A column of
mostly-unique names is never sent. Plain `http://` to a non-local host warns,
because questions would travel unencrypted.

## Limits and failure handling

* `max_decisions=200` (CLI `--max-decisions`) caps the questions per run. Skipped
  questions are noted in `meta.decisions.notes`.
* Questions go 8 per request. A `429`/`5xx` is retried twice with backoff
  (honouring `Retry-After`, at most 10 s per wait).
* A backend failure warns and continues with the rules-only plan for the
  remaining questions; the error is kept in `meta.decisions.error`.
  `llm_fallback=False` (CLI `--no-llm-fallback`) raises a `DecisionError` instead.
* `decide=` cannot be combined with `llm=` yet: the LLM planner writes the whole
  recipe and would discard the decisions.
* Multi-sheet workbooks: pass `--sheet` to use `--decide` on one sheet.
* The questions are deterministic: the same data (in any row order) produces the
  same questions. The answers are the model's, so pin its version.

## Measuring a backend

The [benchmark](Benchmark) runs these exact questions over hand-labelled
messy columns and reports accuracy, **safe-apply precision** (of the answers that
would change data at the 0.9 bar, how many are right), calibration, cells fixed
vs corrupted, latency and cost per 1,000 decisions. Run it on your own data before
lowering `decision_threshold`.

## Python API

```python
from cleanframe.decisions import (
    get_decision_client,   # spec -> client
    refine_issues,         # the decision pass, if you drive the pipeline yourself
    Question, Answer, DecisionResponse,   # write your own backend
    SystemOneClient, LLMDecisionClient, RulesDecisionClient,
)
```

A custom backend is any object with `model`, `calibrated` and
`decide(state, questions) -> DecisionResponse`. Each `Question` carries a
`context` dict (kind, column, value, options) that is never sent over the wire.
