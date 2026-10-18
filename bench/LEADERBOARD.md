# CleanBench leaderboard

Data-cleaning decisions by decision models, scored inside CleanFrame's real pipeline.
What the columns mean and how to add a row: [bench/README.md](README.md).

Public split: 60 scenarios, 299 questions (dataset `sha256:b018fd41383b`, CleanFrame 0.5.0). Sorted by safe-apply precision, then accuracy. Always giving each family's most common gold answer would score 53.1% accuracy (the majority floor).

## Public split

| # | System | Model reported | Calibrated? | Accuracy (95% CI) | Safe-apply precision | Coverage@0.9 | ECE | Brier | Cells fixed / corrupted | p50 latency | $ / 1k decisions |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `rules` | `rules` | no | 54.6% (46.9–62.2) | 87.5% (28/32) | 25.0% | 0.273 | 0.388 | 68 / 16 (of 436) | 0.01 ms | $0 (offline) |

* **Accuracy**: the answer is in the gold set; macro-average of the four question families; an unanswered question counts as wrong. 95% CI resamples whole scenarios.
* **Safe-apply precision**: of the answers CleanFrame would act on to change data (p ≥ 0.9; a typo merge goes ahead unless vetoed), the share that were right - with (right/applied). Read it next to coverage: applying nothing is not a score.
* **Coverage@0.9**: of the questions whose right answer changes data, the share changed correctly (macro over families).
* **Cells fixed / corrupted (of possible)**: cells the cleaned output changed to a gold-correct value / to a wrong one, in the columns and schema fields the system was asked about. *Possible* is what perfect answers fix.
* Label-only or uncalibrated systems are scored on the probabilities they report, as given.

For comparison, CleanFrame **without** a decision model (`decide=None`) fixes 68 and corrupts 16 of the same cells (perfect answers fix 436 and corrupt 0).

### By question family

| System | alias: accuracy | alias: wrong / applied | typo merge: accuracy | typo merge: wrong / applied | null token: accuracy | null token: wrong / applied | schema: accuracy | schema: wrong / applied |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `rules` | 59.4% | 0 / 0 | 87.5% | 4 / 32 | 39.1% | 0 / 0 | 32.6% | 0 / 0 |

### Pending: run with your API key

No numbers are shown for systems nobody has run. Each row below is one command (see [bench/README.md](README.md#running-a-system)); open a PR with the results JSON.

| System | Key | Command |
|---|---|---|
| TypeSafe Jev (direct) | `TYPESAFE_API_KEY` | `python -m bench.cleanbench run --system typesafe/jev-latest` |
| TypeSafe Jev via OpenRouter | `OPENROUTER_API_KEY` | `python -m bench.cleanbench run --system openrouter/typesafe/jev-1.14` |
| Claude (LLM adapter) | `ANTHROPIC_API_KEY` | `python -m bench.cleanbench run --system anthropic/claude-sonnet-4-6` |
| OpenAI (LLM adapter) | `OPENAI_API_KEY` | `python -m bench.cleanbench run --system openai/gpt-4o-mini` |
| Open System One model, self-hosted | `SYSTEMONE_BASE_URL` | `python -m bench.cleanbench run --system systemone/<model>` |

## Held-out split

_Run by the maintainers on submitted systems; the held-out file is never published (see [bench/README.md](README.md#held-out-split))._
