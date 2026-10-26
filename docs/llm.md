# LLM planning

LLM planning is **optional**. Default `cf.clean(df)` uses the rules planner only.

## Contract

1. The model receives **metadata** (column names, dtypes, semantic types, null
   rates, unique counts, pattern sketches) — not raw cells — unless you opt into
   `sample`.
2. The model returns JSON that is parsed through the same `Recipe` model as rules.
3. On failure / budget exceed / a plan that cannot run, CleanFrame **falls back to rules** and warns
   (`CleanFrameWarning`), recording the reason in `recipe.meta["llm_fallback"]`.
   Pass `llm_fallback=False` to raise instead.
4. HTTP calls wait **15 seconds** to connect and up to **600 seconds** for the answer (a
   reasoning model can take minutes), with one SDK retry on rate limits and server errors.
   Each call is logged at INFO on the `cleanframe.llm` logger: tokens in and out, reasoning
   tokens, finish reason, seconds, and the cost when the provider reports it (OpenRouter does).

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
| `sample` | All of the above plus up to 5 distinct example values per column, redacted as described below. A `CleanFrameWarning` lists the columns whose values are sent |
| `none` | **Nothing.** No request is made at all |

`llm_exposure` is validated on every call (case-insensitive); a typo such as
`"smaple"` raises an `LLMError` listing the valid values, even with no `llm=`.

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
approval step; choosing `sample` **is** the opt-in, and the run warns which columns
send values. Do not use it on columns you
are not willing to send off-machine.

## Failure handling

| Setting | On an LLM failure (no key, bad JSON, over budget, network error) |
|---------|------------------------------------------------------------------|
| `llm_fallback=True` (default) | Warn (`CleanFrameWarning`), plan with rules, record the reason in `recipe.meta["llm_fallback"]` |
| `llm_fallback=False` / `--no-llm-fallback` | Raise `LLMError` — no rules-only recipe is silently produced |

Use `llm_fallback=False` when a rules-only recipe would be worse than no recipe,
so a scheduled job fails visibly instead of quietly degrading.

A model's recipe is also **dry-run on the first 200 rows** before it is accepted, so a
plan that loads but cannot run (two columns renamed to one name, a rejected regex)
takes the same fallback path instead of failing at execution.

## Reasoning models

Most current models reason before they answer, and the reasoning counts against the
output cap. CleanFrame's cap is therefore **32,768 tokens** per call
(`cleanframe.llm.DEFAULT_MAX_OUTPUT_TOKENS`; `LLMPlanner(max_output_tokens=...)` and
`LLMDecisionClient(max_output_tokens=...)` change it). You pay only for the tokens the
model writes, not for the cap. At the old 2,048 cap, three of six models tested in
October 2026 spent the whole cap reasoning and returned no answer at all; one used
19,113 tokens (16,927 of them reasoning) for a single recipe.

- **A cap above the model's own limit.** OpenRouter lowers it for you. A direct API that
  refuses it with a 400 naming its limit (OpenAI: "supports at most 16384 completion
  tokens", Anthropic, DeepSeek, Groq) is retried once at that limit. A local server whose
  model has a small context window (vLLM: "maximum context length is 8192 tokens") is
  retried at the window minus the prompt. OpenAI models that refuse `max_tokens` outright
  get `max_completion_tokens` instead.
- **An answer that still does not fit** is reported as such ("used its whole
  32768-token output cap (32768 of them reasoning) before it wrote an answer"), not as
  "no JSON", and takes the usual fallback path.
- **A `<think>…</think>` block in the answer** (local servers running DeepSeek-R1 or
  Qwen3) is skipped, so draft JSON inside it is never taken for the recipe.

Tested end to end through OpenRouter, planner and decisions, in October 2026:
`qwen/qwen3.8-flash`, `qwen/qwen3.7-flash`, `deepseek/deepseek-v4-flash-0731`,
`z-ai/glm-5.3-flash`, `xiaomi/mimo-v2.6-flash` and `minimax/minimax-m3.1-flash`. GLM and
MiniMax refuse a request with reasoning switched off, so CleanFrame does not try to.
Their CleanBench scores are in [bench/LEADERBOARD.md](../bench/LEADERBOARD.md).

## Ops the mode forbids

An LLM plan never contains `fill_na` or `drop_columns`, **in any mode** — imputing a
value or dropping a column is a person's decision, and `clean()` runs the recipe
immediately, so `review` mode would not be a gate. Validation rules the model wrote
with `on_fail: drop` or `null` are downgraded to `quarantine`. Anything removed warns
and is listed in `recipe.meta["llm_blocked_ops"]` (e.g. `["fill_na on Amount"]`);
add the step to the recipe yourself if you want it.

Validations and `dedup` run after renames, so they name a column by its output name. A
model that renames `Email` to `email` but validates `Email` would lose the check; CleanFrame
points such a reference at the renamed column and lists the change in
`recipe.meta["llm_renamed_refs"]`. The prompt lists each op with the only parameters it
accepts, so a model has less reason to invent one; a step it still gets wrong is dropped
and listed in `recipe.meta["llm_dropped"]`.

## Providers

Each provider reads only its own key variable (see the environment table in the
installation guide); `OPENAI_API_KEY` is never sent to another vendor.

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

The budget caps prompt plus answer. Before the request the prompt is estimated
(~4 chars/token) and the answer's output cap is lowered to what the budget has left, so
the provider itself stops the model at the budget; if the prompt alone leaves less than
1,024 tokens for the answer, the call is refused before it is made. Afterwards the usage
the provider reports is checked again. That second check cannot un-spend tokens — the
estimate can undercount non-Latin text — so set the budget with headroom. Either check
going over the budget raises `BudgetExceeded`, which is an `LLMError` — so it falls back
to rules, or raises, per `llm_fallback`. A reasoning model needs room to think: a budget
of a few thousand tokens can leave it no room to answer. Passing `max_tokens_budget`
without `llm=` warns that it was ignored.
