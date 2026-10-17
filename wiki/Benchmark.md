# Benchmark: CleanBench

**CleanBench** measures how well a decision model answers the questions that
[`decide=`](Decision-Models) asks while cleaning data: is `BLR` another name for
`Bangalore`, is `Adduction` a typo of `Abduction` (no), does `none` mean
missing in an *allergies* column (no), which source column holds `order_date`?

It lives in the repository under [`bench/`](https://github.com/inboxpraveen/Cleanframe/blob/main/bench/README.md) and is not part of
the installed package.

## What it measures

Each scenario is a small, hand-labelled messy table. The harness cleans it with
`cf.clean(..., decide=<system>, llm_exposure="sample")`, CleanFrame's real
pipeline, and records every question asked and every answer given.

| Metric | Meaning |
|--------|---------|
| Safe-apply precision | Of the answers CleanFrame would act on to change data (p ≥ 0.9), the share that were right. A wrong one corrupts data |
| Coverage@0.9 | Of the changes that should happen, the share that did |
| Accuracy | The answer is in the gold set (macro over the four question families) |
| ECE, Brier | Calibration of the reported probabilities |
| Cells fixed / corrupted | In the cleaned output: cells changed to a correct value / to a wrong one |
| Latency, $ / 1k decisions | p50/p95 per request; cost from the backend's usage or a list price |

The public split has 60 scenarios (about 300 questions) across Indian, US, European
and Latin American data. It includes deliberate traps: look-alikes that differ,
opposites one letter apart, null tokens that are real data, and schema fields with no
source. A private held-out split is committed only as a hash.

## Run it

```bash
git clone https://github.com/inboxpraveen/Cleanframe && cd Cleanframe
pip install -e .
python -m bench.cleanbench validate                          # offline dataset check
python -m bench.cleanbench run --system rules                # offline baseline
TYPESAFE_API_KEY=... python -m bench.cleanbench run --system typesafe/jev-latest
python -m bench.cleanbench leaderboard                       # rebuild bench/LEADERBOARD.md
```

Any `decide=` backend works: `typesafe/...`, `openrouter/typesafe/...`,
`systemone/<model>` (with `SYSTEMONE_BASE_URL`), `anthropic/...`, `openai/...` or
`ollama/...`. `export --out cleanbench.jsonl` writes the items as standalone System
One requests with gold, for harnesses that do not use CleanFrame.

## Read the results

* Read precision **with** coverage. A system that changes nothing is perfectly safe
  and useless; the leaderboard shows right/applied next to the percentage.
* Before you lower `decision_threshold` in production, check the system's
  calibration (ECE) and its precision on the per-family breakdown.
* The rules baseline is listed with its misses visible. The leaderboard also shows
  what CleanFrame does with no decision model, and what perfect answers would fix.

Full method, metric definitions, the labelling rules, how to submit a result and
the limitations: [bench/README.md](https://github.com/inboxpraveen/Cleanframe/blob/main/bench/README.md). Results:
[bench/LEADERBOARD.md](https://github.com/inboxpraveen/Cleanframe/blob/main/bench/LEADERBOARD.md).
