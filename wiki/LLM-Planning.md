# LLM planning

LLM planning is **optional**. Default `cf.clean(df)` uses the rules planner only.

## Contract

1. The model receives **metadata** (column names, dtypes, semantic types, null
   rates, unique counts, pattern sketches) — not raw cells — unless you opt into
   `sample`.
2. The model returns JSON that is parsed through the same `Recipe` model as rules.
3. On failure / budget exceed, CleanFrame **falls back to rules** and warns
   (`CleanFrameWarning`), recording the reason in `recipe.meta["llm_fallback"]`.
   Pass `llm_fallback=False` to raise instead.
4. HTTP calls use a **60 second** timeout.

## Spec format

```text
provider/model
```

Examples:

```text
anthropic/claude-sonnet-4-6
openai/gpt-4o
openrouter/anthropic/claude-sonnet-4
groq/llama-3.3-70b-versatile
ollama/llama3.2
openai-compatible/my-model    # set OPENAI_BASE_URL
```

```python
result = cf.clean(
    df,
    llm="anthropic/claude-sonnet-4-6",
    llm_exposure="metadata",
    max_tokens_budget=50_000,
)
```

Install SDKs: `pip install "cleanframe-engine[llm]"`.

## Exposure modes

| Mode | What leaves the machine |
|------|-------------------------|
| `metadata` (default) | Names, dtypes, semantic types, null fraction, unique count, pattern sketches (`₹99,99,999`-style); issue kinds and severities without their messages; the target schema if you passed one |
| `sample` | All of the above plus up to 5 distinct example values per column, redacted as described below |
| `none` | **Nothing.** No request is made at all |

`llm_exposure="none"` (CLI `--llm-exposure none`) makes **no network call**: it
warns and plans with the deterministic rules planner instead. Use it to prove a
run stayed local, not to get a cheaper LLM plan.

### What `sample` actually sends

Under `sample` exposure, each column contributes up to 5 distinct values,
deterministically shuffled (seeded by the column name, so the same input sends
the same sample), then rewritten:

| Column semantic type | Sent as |
|----------------------|---------|
| `email` | `user@example.com` |
| `phone` | `+XXXXXXXXXX` — one `X` per digit, minimum 10 |
| `integer`, `float`, `currency`, `unit`, `id` | A structural sketch (`9,99,999`) |
| Anything longer than 24 characters | A structural sketch |
| Short category / text values (≤ 24 chars) | **Verbatim** |

So `sample` is *partly* redacted, not anonymised: emails and phone numbers are
replaced and long strings become patterns, but short category and text values —
city names, status codes, product labels, a person's short name — are sent to
the provider exactly as they appear in your data. There is no interactive
approval step; choosing `sample` **is** the opt-in. Do not use it on columns you
are not willing to send off-machine.

## Failure handling

| Setting | On an LLM failure (no key, bad JSON, over budget, network error) |
|---------|------------------------------------------------------------------|
| `llm_fallback=True` (default) | Warn (`CleanFrameWarning`), plan with rules, record the reason in `recipe.meta["llm_fallback"]` |
| `llm_fallback=False` / `--no-llm-fallback` | Raise `LLMError` — no rules-only recipe is silently produced |

Use `llm_fallback=False` when a rules-only recipe would be worse than no recipe,
so a scheduled job fails visibly instead of quietly degrading.

## Ops the mode forbids

Modes gate what the model is allowed to put in the recipe. `auto` strips
`fill_na`; `strict` strips `fill_na` and `drop_columns`; `review` strips nothing.
Anything removed is listed in `recipe.meta["llm_blocked_ops"]`
(e.g. `["fill_na on Amount"]`), so a missing op is always traceable rather than
silent.

## Providers

Built-in: Anthropic (native), OpenAI, OpenRouter, Groq, Together, Fireworks,
DeepSeek, Mistral, Google Gemini (OpenAI-compatible), xAI, Perplexity, Cohere,
Ollama, LM Studio, Azure / generic `openai-compatible`.

```python
cf.list_providers()
```

## Custom client

Any object with `.complete(system, user, *, max_tokens) → LLMResponse` works:

```python
result = cf.clean(df, llm=MyClient(), mode="review")
```

## Cost control

```python
cf.clean(df, llm="openai/gpt-4o", max_tokens_budget=10_000)
```

Pre-flight estimate uses ~4 chars/token; actual usage is checked after the call.
Either check going over the budget raises `BudgetExceeded`, which is an
`LLMError` — so it falls back to rules, or raises, per `llm_fallback`.
