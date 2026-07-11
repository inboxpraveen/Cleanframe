# Production guide

CleanFrame is designed for **repeatable pipelines**, not one-off notebooks.
This page covers scale, safety, and operational practices for production datasets.

## Recommended pipeline shape

```text
1. One-time (or rare):  cf.clean(...) → review recipe in PR → commit YAML
2. Every file:          cf.apply_recipe(...) with on_drift="error"
3. On DriftError:       cf.suggest_update(...) → human review → merge
```

Never call an LLM on every nightly batch. Plan once; replay forever.

## Scale & memory

| Concern | Default behaviour | Knob |
|---------|-------------------|------|
| Detector scans | Sample first 50k non-null values / column | `DETECTOR_SAMPLE_CAP` in `_util` |
| Cell diff detail | Store ≤ 100k changes; counts stay exact | `options={"max_diff_changes": N}` or `None` |
| Executor | Holds original + working copy (~2× frame) | Process one file at a time; chunk upstream if needed |
| Profiling | Pattern sample capped at 5k | Built-in |
| LLM SAMPLE | Cap 10k before shuffle | Built-in |

```python
result = cf.clean(df, mode="auto", options={"max_diff_changes": 10_000})
# or unlimited detail (can OOM on huge dirty frames):
result = cf.clean(df, options={"max_diff_changes": None})
```

**Practical guidance**

- Prefer Parquet over CSV for multi-million-row files (`pip install cleanframe[parquet]`).
- Profile/report on a sample; apply the committed recipe to the full file.
- Do not keep `result.diff.changes` in memory longer than needed — use
  `result.diff.summary()` for metrics.

## Safety defaults

| Guard | Default | Disable? |
|-------|---------|----------|
| CSV formula escaping | On (`sanitize_csv=True`) | `write_frame(..., sanitize_csv=False)` |
| Regex length / nested quantifiers | Reject dangerous patterns | Edit recipe to safer patterns |
| Drift on apply | `on_drift="error"` | `"warn"` / `"ignore"` / CLI `--force` |
| Missing recipe columns | Warn + skip (non-strict) | `mode="strict"` to fail |
| LLM fallback | Warn + rules planner | `LLMPlanner(..., fallback=None)` to raise |

### CSV formula injection

Cells starting with `=`, `+`, `-`, `@`, tab, or CR are prefixed with `'` on
CSV/TSV export so Excel/Sheets treat them as text. Keep this enabled when
exports may be opened by humans.

### Untrusted recipes

Treat third-party recipe YAML like untrusted config. Review ops (especially
`replace` regexes and `drop` validations) before applying to sensitive data.

## Modes in production

| Environment | Suggested mode |
|-------------|----------------|
| Interactive cleanup | `review` |
| Scheduled ETL with reviewed recipe | `apply_recipe` + `auto` or `strict` |
| Regulated / financial | `strict` + quarantine review workflow |

## Observability

- Inspect `result.log` for skips, quarantine counts, truncated diffs.
- `recipe.meta["llm_fallback"]` records degraded LLM planning.
- `result.quality.score` is a heuristic for reports — not a compliance metric.
- Wire `warnings` into your logging framework.

## CI pattern

```yaml
# pseudo
- pip install cleanframe
- cleanframe apply fixtures/incoming.csv --recipe recipes/customer.recipe.yaml --out out/clean.csv
# fail the job if DriftError / ValidationFailure
```

Commit recipes next to dbt/Airflow code. Review recipe diffs in PRs like code.

## What CleanFrame will not do

- Silently fill missing values (`fill_na` is human-authored only)
- Auto-"fix" outliers
- Delete validation failures without an explicit `on_fail: drop`
- Guarantee identical float bit-patterns across pandas/numpy versions (use
  tolerance checks in tests if needed)

## Typing

CleanFrame ships `py.typed` and inline annotations for editors / mypy.
