# LLM planning

LLM planning is **optional**. Default `cf.clean(df)` uses the rules planner only.

## Contract

1. The model receives **metadata** (column names, dtypes, semantic types, null
   rates, pattern sketches) — not raw cells — unless you opt into `sample`.
2. The model returns JSON that is parsed through the same `Recipe` model as rules.
3. On failure / budget exceed, CleanFrame **falls back to rules** and emits a
   `UserWarning` (also recorded in `recipe.meta["llm_fallback"]`).
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

Install SDKs: `pip install "cleanframe[llm]"`.

## Exposure modes

| Mode | What leaves the machine |
|------|-------------------------|
| `metadata` (default) | Names, dtypes, stats, pattern sketches (`₹99,99,999`-style) |
| `sample` | Small anonymized, deterministically shuffled sample (emails/phones redacted) |
| `none` | Structural metadata only |

Short categorical tokens (≤ 24 chars) may be sent verbatim in `sample` mode to
help planning (e.g. city names). Do not use `sample` on highly sensitive columns
without review.

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
