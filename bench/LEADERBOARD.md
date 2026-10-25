# CleanBench leaderboard

Data-cleaning decisions by decision models, scored inside CleanFrame's real pipeline.
What the columns mean and how to add a row: [bench/README.md](README.md).

Public split: 60 scenarios, 299 questions (dataset `sha256:b018fd41383b`, CleanFrame 0.6.0). Sorted by safe-apply precision, then accuracy. Always giving each family's most common gold answer would score 53.3% accuracy (the majority floor).

## Public split

| # | System | Model reported | Calibrated? | Accuracy (95% CI) | Safe-apply precision | Coverage@0.9 | ECE | Brier | Cells fixed / corrupted | p50 latency | $ / 1k decisions |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `openrouter/deepseek/deepseek-v4-flash-0731` | `llm:deepseek/deepseek-v4-flash-0731` | no | 99.0% (97.7–100.0) | 100.0% (146/146) | 83.8% | 0.032 | 0.028 | 397 / 0 (of 436) | 3.11 s | $0.0756 |
| 2 | `openrouter/liquid/d1-ultra` | `liquid/d1-ultra-20261015` | yes | 96.7% (92.3–100.0) | 100.0% (124/124) | 79.5% | 0.091 | 0.023 | 301 / 6 (of 436) | 690 ms | $0.0061 |
| 3 | `openrouter/z-ai/glm-5.3-flash` | `llm:z-ai/glm-5.3-flash` | no | 95.6% (92.2–98.0) | 100.0% (119/119) | 75.9% | 0.052 | 0.082 | 375 / 6 (of 436) | 3.45 s | $0.0358 |
| 4 | `openrouter/xiaomi/mimo-v2.6-flash` | `llm:xiaomi/mimo-v2.6-flash` | no | 94.8% (89.8–98.5) | 100.0% (125/125) | 76.4% | 0.035 | 0.053 | 383 / 0 (of 436) | 2.15 s | $0.0380 |
| 5 | `openrouter/qwen/qwen3.7-flash` | `llm:qwen/qwen3.7-flash` | no | 94.4% (90.6–97.9) | 99.3% (139/140) | 85.4% | 0.036 | 0.058 | 405 / 5 (of 436) | 14.79 s | $0.0753 |
| 6 | `openrouter/minimax/minimax-m3.1-flash` | `llm:minimax/minimax-m3.1-flash` | no | 97.1% (93.6–99.6) | 98.5% (135/137) | 89.5% | 0.050 | 0.027 | 394 / 12 (of 436) | 3.06 s | $0.1015 |
| 7 | `rules` | `rules` | no | 54.6% (46.9–62.2) | 87.5% (28/32) | 25.0% | 0.273 | 0.388 | 68 / 16 (of 436) | 0.01 ms | $0 (offline) |

* **Accuracy**: the answer is in the gold set; macro-average of the four question families; an unanswered question counts as wrong. 95% CI resamples whole scenarios.
* **Safe-apply precision**: of the answers CleanFrame would act on to change data (p ≥ 0.9; a typo merge goes ahead unless vetoed), the share that were right - with (right/applied). Read it next to coverage: applying nothing is not a score.
* **Coverage@0.9**: of the questions whose right answer changes data, the share changed correctly (macro over families).
* **Cells fixed / corrupted (of possible)**: cells the cleaned output changed to a gold-correct value / to a wrong one, in the columns and schema fields the system was asked about. *Possible* is what perfect answers fix.
* Label-only or uncalibrated systems are scored on the probabilities they report, as given.

For comparison, CleanFrame **without** a decision model (`decide=None`) fixes 68 and corrupts 16 of the same cells (perfect answers fix 436 and corrupt 0).

### By question family

| System | alias: accuracy | alias: wrong / applied | typo merge: accuracy | typo merge: wrong / applied | null token: accuracy | null token: wrong / applied | schema: accuracy | schema: wrong / applied |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `openrouter/deepseek/deepseek-v4-flash-0731` | 96.0% | 0 / 72 | 100.0% | 0 / 28 | 100.0% | 0 / 7 | 100.0% | 0 / 39 |
| `openrouter/liquid/d1-ultra` | 98.0% | 0 / 61 | 100.0% | 0 / 28 | 91.3% | 0 / 12 | 97.7% | 0 / 23 |
| `openrouter/z-ai/glm-5.3-flash` | 86.6% | 0 / 47 | 100.0% | 0 / 28 | 95.7% | 0 / 8 | 100.0% | 0 / 36 |
| `openrouter/xiaomi/mimo-v2.6-flash` | 93.0% | 0 / 55 | 90.6% | 0 / 25 | 95.7% | 0 / 8 | 100.0% | 0 / 37 |
| `openrouter/qwen/qwen3.7-flash` | 90.5% | 0 / 64 | 100.0% | 0 / 28 | 87.0% | 1 / 11 | 100.0% | 0 / 37 |
| `openrouter/minimax/minimax-m3.1-flash` | 97.0% | 1 / 55 | 100.0% | 0 / 28 | 91.3% | 1 / 14 | 100.0% | 0 / 40 |
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

## Not ranked

* `openrouter-qwen-qwen3.8-flash.json` (openrouter/qwen/qwen3.8-flash): partial run
