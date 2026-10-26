# CleanBench

**A benchmark for data-cleaning decisions made by decision models.**

Typed decision models (TypeSafe's Jev, open System One models, any LLM behind an
adapter) answer questions such as *"is `BLR` another name for `Bangalore`?"* or
*"does `none` mean missing in an `allergies` column?"* with a probability.
JevBench, the main leaderboard for these models, has no tabular or data-cleaning
tasks. CleanBench fills that gap. It scores these decisions **inside CleanFrame's real
cleaning pipeline**, on hand-labelled messy columns, and reports what matters in
production: how often a confident answer **corrupts data**.

```bash
python -m bench.cleanbench validate                  # check the dataset (offline)
python -m bench.cleanbench run --system rules        # the offline baseline
python -m bench.cleanbench leaderboard               # rebuild LEADERBOARD.md
```

Results so far: [LEADERBOARD.md](LEADERBOARD.md).

## Contents

- [Why: corruption versus coverage](#why-corruption-versus-coverage)
- [What a run does](#what-a-run-does)
- [The dataset](#the-dataset)
- [How the gold is written](#how-the-gold-is-written)
- [Metrics](#metrics)
- [Running a system](#running-a-system)
- [Submitting a result](#submitting-a-result)
- [Export: the items without CleanFrame](#export-the-items-without-cleanframe)
- [Held-out split](#held-out-split)
- [Changing the dataset](#changing-the-dataset)
- [Limitations](#limitations)

## Why: corruption versus coverage

A cleaning tool that merges `Hypotension` into `Hypertension`, or turns the `NA`
(Namibia) in a country-code column into a missing value, does more harm than one
that leaves both alone. The harm is also silent: the output looks cleaner. So the
headline number is not accuracy. It is **safe-apply precision**: of the answers
CleanFrame would act on to change data, how many were right. It is always read next
to **coverage**: of the changes that should happen, how many did. A system that
never changes anything has perfect safety and no use. Both numbers come from the
same answers, along with an end-to-end count that a reader can check by eye:
*"fixed 412 cells, corrupted 0"*.

## What a run does

Each scenario is a small table. The harness hands it to CleanFrame exactly as a
user would, in **one production call** per scenario:

```python
cf.clean(df, decide=<recorder around the system>, llm_exposure="sample",
         target_schema=<scenario schema>, decision_threshold=0.9, llm_fallback=False)
```

CleanFrame profiles the table, runs its detectors and decides which questions to
ask (see [docs/decisions.md](../docs/decisions.md)). The recorder passes each
request to the system under test and keeps the questions, answers, latency, tokens
and cost. So:

* every question scored is one that CleanFrame asks in production, built by the
  same code from the same data;
* the cleaned frame from the same call gives the end-to-end cell counts. The
  question-level and end-to-end numbers always come from the same answers, and a
  paid system is billed once;
* `llm_fallback=False` makes a backend failure visible. With the default `True`, a
  failed request would quietly fall back to the rules, and the rules' plan would be
  scored as the model's work. A failed request is retried twice by the harness (on
  top of the backend's own HTTP retries). If it still fails, the scenario is
  recorded as an error and the run is marked incomplete. An incomplete run is not
  ranked.

| Setting | Value | Why |
|---|---|---|
| mode | `review` (CleanFrame's default) | |
| `decision_threshold` | 0.9 (default) | the production bar for changing data |
| `llm_exposure` | `sample` | category questions need it; schema and null questions are asked either way |
| `max_decisions` | 200 (default) | never reached by a scenario |

Before any system runs, `run` validates the dataset offline with a gold oracle, so a
broken dataset can never cost a paid run anything.

## The dataset

`data/public.yaml` has **60 scenarios** and produces **299 questions** when every
answer is right (counts at the time of writing; `validate` prints the current
ones). The scenarios cover healthcare, logistics, e-commerce, tax, HR, CRM, finance,
travel, telecom and more. The places and languages are Indian, US, European
(German, Austrian and Portuguese names next to the English ones), Latin American
(Spanish, Portuguese), southern African and Pacific. Some values are in Devanagari
script.

| Family (question kind) | What CleanFrame asks | Scenarios (easy / std / hard) | Questions (gold changes data) |
|---|---|---|---|
| `category_alias` | Is value *V* another name for one of the column's other values, or `__distinct__`? | 16 (3 / 8 / 5) | 201 (80) |
| `category_merge` | The rules merged a rare spelling into a frequent look-alike: same category, yes or no? | 13 (3 / 2 / 8) | 32 (28) |
| `null_token` | Does this null-looking token (`none`, `nil`, `-`, `--`, `?`, `unknown`, `NA`/`NaN` among codes) mean missing here? | 18 (4 / 8 / 6) | 23 (14) |
| `schema_match` | Which source column holds target field *T*, or `__none__`? | 13 (2 / 6 / 5) | 43 (41) |

A scenario's family names what it was written to test. Questions are scored by
their own kind, so the alias questions that a null-token scenario also raises count
as alias questions.

**Traps.** Each family includes items that separate careful systems from
pattern-matchers:

* **Look-alikes that differ**: Austria/Australia, Slovakia/Slovenia, Niger/Nigeria,
  Iran/Iraq, Kansas/Arkansas, Virginia/West Virginia, Andhra/Arunachal Pradesh,
  Electrical/Electronics, Debit/Credit Card.
* **Opposites one letter apart, merged by the rules** (typo-merge questions, where a
  "no" vetoes the merge): Abduction/Adduction, Dysphagia/Dysphasia,
  Deferred/Referred, sodium nitrite/nitrate.
* **Opposites two letters apart** (the rules keep them apart since 0.5.0, so they are
  asked as alias questions instead): Hypotension/Hypertension, Hypo-/Hyperthyroidism,
  Hypo-/Hyperkalemia, Hypo-/Hypernatremia, Inter-state/Intra-state (GST),
  Prednisone/Prednisolone, Cyclosporine/Cycloserine.
* **Negations**: Insured/Uninsured/Underinsured/Not insured, Smoker/Non-smoker/Ex-smoker,
  Satisfied/Dissatisfied.
* **World knowledge**: BLR, Bombay, Madras, Calif., WA versus DC, CDMX, Rio, R&D,
  Ops, CC, COD, US$, Rs, ₹, m/w/d (German gender), मुंबई.
* **Code collisions, settled by context**: `CA` is Canada next to US and MX, and
  California in a US state column.
* **Null tokens that are data**: `None` in *allergies*, *sweetener* and Wi-Fi
  *encryption*; `-` as the DNA minus strand; `?` as a chess mistake; `NA` for
  Namibia; `NAN` for Nadi airport; `Nil` backlogs (zero). And ones that are
  placeholders: `-` for a middle name, `Unknown` blood group, `None` tracking number,
  `Nil` landmark.
* **Schema**: Placed On/Dispatched On for order_date/ship_date, Bill-to/Ship-to PIN,
  Taxable Value/GST/Invoice Total, Consultant as the attending physician, Spanish
  and German headers. Some targets have **no** source (`loyalty_tier`,
  `discount_amount`); the right answer is `__none__`. Some of the rules' own weak
  guesses are wrong (Taxable Value → tax_amount, Ort → country). A model has to
  override them with a confident answer.

**Format.** One scenario:

```yaml
- id: alias-in-delivery-cities
  family: category_alias            # category_alias | category_merge | null_token | schema_match
  domain: logistics (India)
  difficulty: easy                  # easy | standard | hard
  rationale: >-
    Indian cities under their old names, new names and airport codes. ...
  column: delivery_city
  values: {Bangalore: 9, Mumbai: 8, Delhi: 6, Chennai: 5, Bengaluru: 3, BLR: 2, Bombay: 2, Madras: 1}
  same: [[Bangalore, Bengaluru, BLR], [Mumbai, Bombay], [Chennai, Madras]]
  expect: {alias: 7}                # the questions it must produce (checked by validate)
```

Null-token scenarios add `placeholders: [...]`. Schema scenarios give
`columns: {source: [values...]}`, `schema: {target: {dtype: ...}}` and
`gold: {target: source | null}`. All names, codes and records are invented.

## How the gold is written

The gold is written as **equivalence classes**, not as one label per question. That
way it answers whatever question the pipeline asks, including questions that only
arise after a system's earlier answers.

* `same` groups spellings of one thing. A spelling in no group is a distinct value.
  The gold for an alias question about *V* is every offered option in *V*'s group
  (any one is correct), or `__distinct__` if none is. A typo-merge question is "yes"
  exactly when both spellings are in one group.
* `placeholders` lists the tokens that mean missing in that column. A token is a
  placeholder when it is **not a value from the column's domain** (a `-` where a
  middle name goes, `None` where a tracking number goes). It is **data** when it is
  one of the answers the column records (`None` allergies, the `-` strand, `NA`
  Namibia, `Nil` = zero backlogs).
* Two spellings are the same only if a careful person who knows the domain would
  merge them **without asking anyone**. Related-but-different values (debit and
  credit cards, prednisone and prednisolone, Underinsured and Uninsured) are
  different. An item whose answer depends on context the table does not contain is
  dropped. Every scenario carries a one-line `rationale`.
* Values the rules merge without a question (same letters and digits once case,
  spaces and punctuation are dropped, such as `NetBanking`/`Net Banking`) must share
  a class. `validate` refuses gold that splits them, because such an item would
  charge every system for the rules' choice.

If the pipeline asks something the gold cannot answer (a value nobody listed, a
schema target without gold), that is a dataset bug: `validate` and `run` stop and
name it. They never guess.

## Metrics

All metrics are computed from the per-question log
(`results/<system>.questions.jsonl`, one record per question with the answer,
probabilities, gold and gates), so anyone can recompute a row. Code:
[`cleanbench/metrics.py`](cleanbench/metrics.py).

| Metric | Definition |
|---|---|
| **Accuracy** | The decided label (`Answer.decision()`) is in the gold set. An unanswered question (abstained, dropped, or failed) counts as wrong. Reported as the **macro average of the four families**, so the many alias questions do not drown the rest (the micro average is in the JSON). |
| **Safe-apply precision** | Of the answers CleanFrame **would act on to change data**, the share that were right, pooled over all families and shown as right/applied. Unanswered questions are not applied. With zero applied answers it is undefined (`n/a`), never 100%. |
| **Coverage@0.9** | Of the questions whose gold answer changes data, the share that were changed correctly (macro over families). |
| **ECE** | Expected calibration error of the decision probability: 10 equal-width bins on [0, 1], with \|accuracy − mean confidence\| weighted by bin size. Empty bins are skipped. Answered questions only. |
| **Brier** | Mean of (1 − q)², where q is the probability mass the answer puts on the gold set. Answered questions only. This differs from JevBench's multi-class Brier because a CleanBench question can have several correct labels. |
| **Cells fixed / corrupted (of possible)** | See below. |
| **Requests, p50/p95 latency** | One request is one `.decide()` call (up to 8 questions). Latency is wall time around that call (`time.perf_counter`); percentiles are nearest-rank. |
| **Tokens, $ / 1k decisions** | Summed from the backend's `usage`. Cost is the backend's own `usage.cost` when every response reports it, else `--price-in/--price-out` (USD per million tokens) applied to the reported tokens, else `n/a`. **A measurement nobody reported is null, never 0.** Offline systems cost $0. |

**When an answer changes data.** The gates mirror `cleanframe/decisions.py` in its
default configuration exactly:

| Kind | Changes data when | Gate |
|---|---|---|
| alias | the answer is not `__distinct__` | `p ≥ 0.9`, where p = min(option probability, reported confidence) |
| null token | the answer is "yes, missing" | `p_yes ≥ 0.9` |
| schema | the answer is not `__none__` | `p ≥ 0.9` |
| typo merge | the answer is "yes, same" | `p_yes ≥ 0.5`: the rules already made the merge, and in review mode it goes ahead unless the model vetoes it |

**End to end.** In the cleaned output of the same call, inside the columns and
schema fields the system was asked about:

* a text cell changed to another spelling of the **same** thing is *fixed*;
  changed to a **different** value is *corrupted*;
* a cell turned into a missing value is *fixed* if its token is a placeholder there,
  and *corrupted* if it was data;
* changes CleanFrame makes without asking (case, spaces, punctuation) are not
  counted;
* a schema target filled from its gold source column counts that column's cells as
  *fixed*; filled from any other column, they are *corrupted*.

*Possible* is what the gold oracle fixes (the ceiling). Every results file also
records the same counts for CleanFrame **without** a decision model
(`decide=None`), so the lift is visible.

**Uncertainty and floors.** 95% intervals for accuracy and safe-apply precision
come from a bootstrap that resamples whole scenarios (1,000 samples, fixed seed).
The **majority floor** is the accuracy of always giving each family's most common
gold answer. Two offline reference systems can be run like any other
(`run --system cleanbench/oracle`, `run --system cleanbench/never-change`):
the oracle answers from the gold (the ceiling: accuracy and precision 100%, every
possible cell fixed), and never-change always keeps values apart, keeps tokens and
maps nothing (the floor: its precision is undefined because it changes nothing). If
their results files are present, the leaderboard shows them as unranked reference
rows.

**Label-only and uncalibrated systems are scored as given.** An LLM through
CleanFrame's adapter states probabilities that are not trained to be calibrated (it
is flagged `calibrated: no`). A system that returns only labels at p = 1.0 is
scored on those numbers: every wrong answer at p = 1.0 counts in full against ECE
and Brier, and nothing is rescaled.

**The question set depends on the answers**, as in production. Vetoing a correct
typo merge leaves the variant as a separate value, and CleanFrame then asks one
more alias question about it. So question counts can differ slightly between
systems. `questions_intended` in the results is the count a perfect system is asked.

## Running a system

Run from the repository root with CleanFrame installed (`pip install -e .`). Every
backend reads only its own key. Pin a model version, because the results file
records the model the backend reported.

| System | Key / setup | Command |
|---|---|---|
| TypeSafe Jev (direct) | `TYPESAFE_API_KEY` (`TYPESAFE_BASE_URL` optional) | `python -m bench.cleanbench run --system typesafe/jev-latest` |
| Jev via OpenRouter | `OPENROUTER_API_KEY` | `python -m bench.cleanbench run --system openrouter/typesafe/jev-1.14` |
| Liquid AI d1-ultra via OpenRouter | `OPENROUTER_API_KEY` | `python -m bench.cleanbench run --system openrouter/liquid/d1-ultra` |
| Any OpenRouter chat model (LLM adapter) | `OPENROUTER_API_KEY` | `python -m bench.cleanbench run --system openrouter/qwen/qwen3.8-flash` |
| Open System One model, self-hosted (imajev, Kev, Laya, …) | `SYSTEMONE_BASE_URL=http://127.0.0.1:8765` (`SYSTEMONE_API_KEY` optional) | `python -m bench.cleanbench run --system systemone/<model>` |
| Claude | `ANTHROPIC_API_KEY` | `python -m bench.cleanbench run --system anthropic/claude-sonnet-4-6 --price-in <in> --price-out <out>` |
| OpenAI | `OPENAI_API_KEY` | `python -m bench.cleanbench run --system openai/gpt-4o-mini --price-in <in> --price-out <out>` |
| Local LLM via Ollama | a running Ollama | `python -m bench.cleanbench run --system ollama/qwen3` |
| CleanFrame's rules (offline baseline) | none | `python -m bench.cleanbench run --system rules` |

Pass `--price-in/--price-out` (USD per million tokens, at the provider's current
list price) only when the backend does not report a cost itself. Other flags:
`--out PATH`, `--max-scenarios N` (a partial run, which is not ranked) and
`--quiet`. A run writes `results/<system>.json` and
`results/<system>.questions.jsonl`, then `leaderboard` rebuilds the table.

Your own backend, from Python:

```python
from bench.cleanbench.harness import run_benchmark

results, records = run_benchmark(my_client)   # any object with model, calibrated, decide()
```

## Submitting a result

1. Run the public split with a pinned model version. Do not edit the dataset.
2. `python -m bench.cleanbench leaderboard`
3. Open a PR that adds `bench/results/<system>.json`, its `.questions.jsonl` and the
   regenerated `LEADERBOARD.md`. Say in the description how the system was served
   (API, OpenRouter, self-hosted and on what hardware) and the run date.

A maintainer re-scores the log with `metrics.py` and may re-run the system on the
held-out split. The leaderboard ranks only complete runs on the **current** dataset
hash. A result from an older dataset version is listed as "re-run needed", not
silently mixed in.

## Export: the items without CleanFrame

```bash
python -m bench.cleanbench export --out cleanbench.jsonl
```

This writes one line per request, exactly as CleanFrame sends it over the System
One protocol, with the gold for every question:

```json
{"id": "alias-in-delivery-cities/00", "scenario": "alias-in-delivery-cities",
 "family": "category_alias", "difficulty": "easy", "domain": "logistics (India)",
 "state": {"task": "...", "column": "delivery_city", "values": [{"value": "Bangalore", "count": 9}, ...]},
 "questions": {"alias_0": {"type": "choice", "instructions": "...", "criteria": {...}}, ...},
 "gold": {"alias_0": ["Chennai"], ...}}
```

`gold` lists every acceptable label. POST `{"model": ..., "state": ...,
"questions": ...}` to any `/v1/systemone` server and score the argmax against
`gold`. The export is the question set a perfect system is asked, so it is ready for
JevBench or any other harness. The end-to-end cell counts need CleanFrame.

## Held-out split

A second split of 19 scenarios (89 questions) with the same format, families and
labelling rules is kept **private**. Only its SHA-256 is committed, in
[`data/HELDOUT.sha256`](data/HELDOUT.sha256), so it cannot leak into training data,
and anyone can check which file a held-out result used. Maintainers run submitted
systems on it:

```bash
python -m bench.cleanbench run --system SPEC --split /private/heldout.yaml \
    --out bench/results/heldout/<system>.json
```

No per-question log is written for a non-public split. The leaderboard lists results
whose dataset hash matches `HELDOUT.sha256` in a separate held-out table. A large
gap between a system's public and held-out scores is a sign of contamination.

## Changing the dataset

`python -m bench.cleanbench validate [-v]` runs every scenario through the pipeline
with the gold oracle and checks that:

- the YAML is well formed (values are quoted text, groups only name listed values,
  ids are unique);
- every scenario asks at least one question, including one of its own family, and
  exactly the questions in its `expect` block;
- every question has gold;
- a perfect system corrupts no cell.

`-v` prints every question with its gold. CleanFrame's rules decide which questions
exist, so a change to them (for example to the typo-merge thresholds) can make
`validate` fail with a precise diff. Re-check the affected scenarios, update
`expect`, and note in the PR that the dataset hash changed (old results move to
"re-run needed").

## Limitations

* **Small and hand-written.** 299 questions in 60 scenarios can separate a careless
  system from a careful one. They cannot rank two close systems: read the 95%
  intervals, and treat differences inside them as ties.
* **English-centric instructions.** The values are multilingual, but CleanFrame's
  question text is English.
* **Shared authorship.** The dataset and CleanFrame's rules baseline were written by
  the same project. This is mitigated, not removed:
  - the gold is written as equivalence classes about the *data*, independent of
    what the rules do;
  - the rules baseline is published with its misses visible;
  - the external systems never saw the dataset.
  Outside review of the gold is welcome; open an issue with the scenario id.
* **Pipeline-bound.** The questions are CleanFrame's. A system tuned to other
  question styles may do worse here than its general ability suggests. The export
  makes the items usable elsewhere.
* **The question set adapts to the answers**, as described under
  [Metrics](#metrics).
* **One threshold.** Precision and coverage are reported at CleanFrame's default
  0.9 bar. The per-question log has the probabilities to recompute other bars.
