"""Optional LLM-assisted planning — the plan-writer that never sees your data.

The contract, enforced structurally:

1. An :class:`LLMPlanner` builds a prompt from **metadata only** — column names,
   dtypes, semantic types, null/cardinality stats, and *pattern sketches* of values
   (``₹99,99,999``-style), never raw cells — unless the caller explicitly opts into
   :data:`LLMExposure.SAMPLE` and passes an approved sample.
2. The model returns a JSON **recipe**, which is parsed through the *same*
   :class:`~cleanframe.recipe.Recipe` model the rules planner uses. Anything the
   model asks for is therefore just a deterministic plan the executor runs — the
   model has no path to your rows.
3. A hard :data:`max_tokens_budget` aborts before it gets expensive, and any
   failure (no key, bad JSON, over budget) falls back to the rules planner so the
   pipeline never dies because an LLM was flaky.

Providers are pluggable. An ``LLMClient`` is anything with a ``complete`` method;
built-in Anthropic/OpenAI adapters import their SDKs lazily, so this module (and
all of CleanFrame) imports fine with neither installed.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Protocol

import pandas as pd

from .errors import BudgetExceeded, LLMError
from .issues import Issues
from .ops import list_ops
from .profile import DataFrameProfile
from .recipe import Recipe
from .types import LLMExposure, Mode


# ---------------------------------------------------------------------------
# Client interface + provider adapters
# ---------------------------------------------------------------------------
@dataclass
class LLMResponse:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class LLMClient(Protocol):
    """Minimal provider interface. ``complete`` returns text plus token accounting."""

    model: str

    def complete(self, system: str, user: str, *, max_tokens: int = 2048) -> LLMResponse: ...


class AnthropicClient:
    """Adapter for the Anthropic Messages API. Requires ``anthropic`` + ``ANTHROPIC_API_KEY``."""

    def __init__(self, model: str, api_key: str | None = None) -> None:
        self.model = model
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")

    def complete(self, system: str, user: str, *, max_tokens: int = 2048) -> LLMResponse:
        if not self._api_key:
            raise LLMError("ANTHROPIC_API_KEY is not set.")
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - optional dep
            raise LLMError("The 'anthropic' package is required. Install cleanframe[llm].") from exc
        client = anthropic.Anthropic(api_key=self._api_key)
        msg = client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(block.text for block in msg.content if getattr(block, "type", "") == "text")
        return LLMResponse(text, msg.usage.input_tokens, msg.usage.output_tokens)


class OpenAIClient:
    """Adapter for OpenAI Chat Completions and any OpenAI-compatible endpoint.

    Used for OpenAI, OpenRouter, Groq, Together, DeepSeek, Mistral, Gemini's
    OpenAI surface, Ollama, and anything else that speaks the same protocol.
    Pass ``base_url`` / ``api_key`` explicitly, or rely on env vars (see
    :data:`PROVIDERS`).
    """

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        *,
        default_headers: dict[str, str] | None = None,
        key_env: str = "OPENAI_API_KEY",
    ) -> None:
        self.model = model
        self._api_key = api_key or os.environ.get(key_env) or os.environ.get("OPENAI_API_KEY")
        self._base_url = base_url or os.environ.get("OPENAI_BASE_URL")
        self._default_headers = default_headers
        self._key_env = key_env

    def complete(self, system: str, user: str, *, max_tokens: int = 2048) -> LLMResponse:
        if not self._api_key:
            raise LLMError(
                f"{self._key_env} is not set"
                + (f" (or OPENAI_API_KEY as a fallback)." if self._key_env != "OPENAI_API_KEY" else ".")
            )
        try:
            import openai
        except ImportError as exc:  # pragma: no cover - optional dep
            raise LLMError("The 'openai' package is required. Install cleanframe[llm].") from exc
        kwargs: dict[str, Any] = {"api_key": self._api_key}
        if self._base_url:
            kwargs["base_url"] = self._base_url
        if self._default_headers:
            kwargs["default_headers"] = self._default_headers
        client = openai.OpenAI(**kwargs)
        resp = client.chat.completions.create(
            model=self.model,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        usage = resp.usage
        return LLMResponse(
            resp.choices[0].message.content or "",
            getattr(usage, "prompt_tokens", 0) or 0,
            getattr(usage, "completion_tokens", 0) or 0,
        )


# ---------------------------------------------------------------------------
# Provider registry — almost everyone speaks OpenAI Chat Completions
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ProviderSpec:
    """How to reach an OpenAI-compatible (or Anthropic-native) provider."""

    name: str
    kind: str  # "openai" | "anthropic"
    base_url: str | None = None
    key_env: str = "OPENAI_API_KEY"
    default_key: str | None = None  # e.g. "ollama" for local servers
    default_headers: dict[str, str] | None = None
    aliases: tuple[str, ...] = ()


# Built-in providers. Specs use ``provider/model``; for OpenRouter the model may
# itself contain slashes (``openrouter/anthropic/claude-sonnet-4``).
_PROVIDER_LIST: list[ProviderSpec] = [
    ProviderSpec("anthropic", kind="anthropic", key_env="ANTHROPIC_API_KEY"),
    ProviderSpec("openai", kind="openai", key_env="OPENAI_API_KEY"),
    ProviderSpec(
        "openrouter",
        kind="openai",
        base_url="https://openrouter.ai/api/v1",
        key_env="OPENROUTER_API_KEY",
        # OpenRouter optionally uses these for rankings; harmless if unset.
        default_headers={
            "HTTP-Referer": "https://github.com/cleanframe/cleanframe",
            "X-Title": "CleanFrame",
        },
    ),
    ProviderSpec(
        "groq",
        kind="openai",
        base_url="https://api.groq.com/openai/v1",
        key_env="GROQ_API_KEY",
    ),
    ProviderSpec(
        "together",
        kind="openai",
        base_url="https://api.together.xyz/v1",
        key_env="TOGETHER_API_KEY",
        aliases=("togetherai",),
    ),
    ProviderSpec(
        "fireworks",
        kind="openai",
        base_url="https://api.fireworks.ai/inference/v1",
        key_env="FIREWORKS_API_KEY",
        aliases=("fireworksai",),
    ),
    ProviderSpec(
        "deepseek",
        kind="openai",
        base_url="https://api.deepseek.com",
        key_env="DEEPSEEK_API_KEY",
    ),
    ProviderSpec(
        "mistral",
        kind="openai",
        base_url="https://api.mistral.ai/v1",
        key_env="MISTRAL_API_KEY",
    ),
    ProviderSpec(
        "google",
        kind="openai",
        # Gemini's OpenAI-compatible endpoint
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        key_env="GOOGLE_API_KEY",
        aliases=("gemini",),
    ),
    ProviderSpec(
        "xai",
        kind="openai",
        base_url="https://api.x.ai/v1",
        key_env="XAI_API_KEY",
        aliases=("grok",),
    ),
    ProviderSpec(
        "perplexity",
        kind="openai",
        base_url="https://api.perplexity.ai",
        key_env="PERPLEXITY_API_KEY",
    ),
    ProviderSpec(
        "cohere",
        kind="openai",
        base_url="https://api.cohere.ai/compatibility/v1",
        key_env="COHERE_API_KEY",
    ),
    ProviderSpec(
        "ollama",
        kind="openai",
        base_url="http://localhost:11434/v1",
        key_env="OPENAI_API_KEY",
        default_key="ollama",
    ),
    ProviderSpec(
        "lmstudio",
        kind="openai",
        base_url="http://localhost:1234/v1",
        key_env="OPENAI_API_KEY",
        default_key="lmstudio",
        aliases=("lm-studio",),
    ),
    # Generic: honour OPENAI_BASE_URL / OPENAI_API_KEY from the environment.
    ProviderSpec("azure", kind="openai", key_env="OPENAI_API_KEY", aliases=("azure-openai",)),
    ProviderSpec(
        "openai-compatible",
        kind="openai",
        key_env="OPENAI_API_KEY",
        aliases=("compatible", "local"),
    ),
]

PROVIDERS: dict[str, ProviderSpec] = {}
for _spec in _PROVIDER_LIST:
    PROVIDERS[_spec.name] = _spec
    for _alias in _spec.aliases:
        PROVIDERS[_alias] = _spec


def list_providers() -> list[str]:
    """Canonical provider names (aliases omitted)."""
    return sorted({s.name for s in _PROVIDER_LIST})


def get_client(spec: str) -> LLMClient:
    """Resolve a ``"provider/model"`` string.

    Examples::

        anthropic/claude-sonnet-4-6
        openai/gpt-4o
        openrouter/anthropic/claude-sonnet-4          # model may contain slashes
        groq/llama-3.3-70b-versatile
        together/meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo
        google/gemini-2.0-flash
        ollama/llama3.2
        openai-compatible/my-model   # set OPENAI_BASE_URL

    Almost every hosted provider speaks OpenAI Chat Completions; only Anthropic
    uses a native adapter. Unknown providers raise :class:`LLMError` listing the
    supported names.
    """
    if "/" not in spec:
        raise LLMError(f"LLM spec must be 'provider/model', got {spec!r}.")
    provider, model = spec.split("/", 1)
    provider = provider.lower()
    info = PROVIDERS.get(provider)
    if info is None:
        known = ", ".join(list_providers())
        raise LLMError(f"Unknown LLM provider {provider!r}. Supported: {known}.")

    if info.kind == "anthropic":
        return AnthropicClient(model)

    # Prefer an explicit env override of the base URL for any OpenAI-compatible
    # provider (handy for proxies / self-hosted gateways).
    base_url = os.environ.get("OPENAI_BASE_URL") or info.base_url
    api_key = os.environ.get(info.key_env) or os.environ.get("OPENAI_API_KEY") or info.default_key
    # Google also accepts GEMINI_API_KEY
    if info.name == "google" and not api_key:
        api_key = os.environ.get("GEMINI_API_KEY")

    return OpenAIClient(
        model,
        api_key=api_key,
        base_url=base_url,
        default_headers=info.default_headers,
        key_env=info.key_env,
    )


# ---------------------------------------------------------------------------
# Metadata extraction (what the model is allowed to see)
# ---------------------------------------------------------------------------
def _sketch(value: str) -> str:
    """Structural sketch of a value: digits→9, letters→A, runs collapsed with ``+``."""
    out: list[str] = []
    for ch in value:
        if ch.isdigit():
            cls = "9"
        elif ch.isalpha():
            cls = "A"
        else:
            cls = ch
        if out and out[-1].rstrip("+") == cls:
            if not out[-1].endswith("+"):
                out[-1] = cls + "+"
        else:
            out.append(cls)
    return "".join(out)


def value_sketches(series: pd.Series, k: int = 3) -> list[str]:
    """Top-``k`` structural patterns for a column (no raw values leak)."""
    counts: dict[str, int] = {}
    for v in series.dropna().head(500).tolist():
        s = _sketch(v if isinstance(v, str) else str(v))
        counts[s] = counts.get(s, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [s for s, _ in ranked[:k]]


def _anonymize_value(value: Any, semantic_type: str) -> str:
    """Redact PII-ish samples while preserving enough shape for planning."""
    s = str(value)
    if semantic_type == "email":
        return "user@example.com"
    if semantic_type == "phone":
        digits = re.sub(r"\D", "", s)
        return ("+" + "X" * max(len(digits), 10)) if digits else "+XXXXXXXXXX"
    if semantic_type in ("currency", "float", "integer", "unit"):
        return _sketch(s)
    if semantic_type in ("id",):
        return _sketch(s)
    # Categories / text: keep short tokens (city names help planning) but mask long strings.
    if len(s) > 24:
        return _sketch(s)
    return s


def _sample_for_llm(cp: Any, series: pd.Series, *, k: int = 5) -> list[str]:
    """Deterministically shuffled, anonymized sample values for SAMPLE exposure."""
    values = [v for v in series.dropna().tolist()]
    # Stable shuffle keyed by column name — same input → same sample order.
    seed = sum(ord(c) for c in cp.name) % 2_147_483_647 or 1
    rng = __import__("random").Random(seed)
    shuffled = list(values)
    rng.shuffle(shuffled)
    # Prefer distinct values, then anonymize.
    seen: set[str] = set()
    out: list[str] = []
    for v in shuffled:
        anon = _anonymize_value(v, cp.semantic_type)
        if anon in seen:
            continue
        seen.add(anon)
        out.append(anon)
        if len(out) >= k:
            break
    return out


def build_metadata(
    df: pd.DataFrame,
    profile: DataFrameProfile,
    issues: Issues,
    schema: Any | None,
    exposure: LLMExposure,
) -> dict:
    """Assemble the JSON-serialisable metadata payload sent to the model."""
    columns = []
    for cp in profile.columns:
        entry: dict[str, Any] = {
            "name": cp.name,
            "dtype": cp.dtype,
            "semantic_type": cp.semantic_type,
            "null_fraction": round(cp.null_fraction, 3),
            "unique_count": cp.unique_count,
            "patterns": value_sketches(df[cp.name]),
        }
        if exposure == LLMExposure.SAMPLE:
            entry["example_values"] = _sample_for_llm(cp, df[cp.name])
            entry["sample_note"] = (
                "anonymized + deterministically shuffled; approve before sending off-machine"
            )
        columns.append(entry)

    payload: dict[str, Any] = {
        "row_count": profile.n_rows,
        "columns": columns,
        "detected_issues": [
            {
                "column": i.column,
                "kind": i.kind,
                "severity": i.severity.value,
                "message": i.message,
            }
            for i in issues
        ],
    }
    if schema is not None:
        payload["target_schema"] = schema.to_dict()
    return payload


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------
_SYSTEM_PROMPT = """You are CleanFrame's planning assistant. You write a data-cleaning \
RECIPE as JSON. You never see raw data — only metadata and value patterns. Your recipe \
is executed deterministically by pure pandas; you cannot run code or touch values.

Return ONLY a JSON object (no prose, no markdown fences) matching this shape:
{
  "version": 1,
  "columns": {
    "<source column name>": {
      "rename_to": "<snake_case name, optional>",
      "ops": [ <ordered ops> ]
    }
  },
  "dedup": { "subset": ["col"], "keep": "first" },   // optional
  "validate": [ {"column": "col", "check": "valid_email", "on_fail": "quarantine"} ]  // optional
}

Each op is either a bare string or a single-key object, e.g. "strip_whitespace",
{"parse_date": {"formats": ["%d/%m/%Y"]}}, {"remove_symbols": ["₹", ","]},
{"cast": "float"}, {"normalize_values": {"BLR": "Bangalore"}}.

Available ops: {ops}

Rules: prefer minimal, safe transforms; do NOT impute missing values; only propose a
rename when it improves clarity; put ops in a sensible execution order."""


def build_prompt(metadata: dict) -> tuple[str, str]:
    system = _SYSTEM_PROMPT.replace("{ops}", ", ".join(list_ops()))
    user = "Here is the dataset metadata. Produce the JSON recipe.\n\n" + json.dumps(
        metadata, indent=2, ensure_ascii=False
    )
    return system, user


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_recipe_json(text: str) -> Recipe:
    """Extract and validate a Recipe from the model's response.

    Validation runs through :meth:`Recipe.from_dict`, so an unknown op or malformed
    structure raises before anything touches data.
    """
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):] if "{" in text else text
    match = _JSON_RE.search(text)
    if not match:
        raise LLMError("LLM response contained no JSON object.")
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise LLMError(f"LLM returned invalid JSON: {exc}") from exc
    return Recipe.from_dict(data)


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------
class LLMPlanner:
    """A :class:`~cleanframe.planner.Planner` that asks an LLM for the recipe.

    Falls back to the rules planner on any failure (unless ``fallback=None``), so an
    LLM outage or a bad key degrades to deterministic rules rather than an error.
    """

    def __init__(
        self,
        client: LLMClient,
        *,
        exposure: LLMExposure | str = LLMExposure.METADATA,
        max_tokens_budget: int | None = None,
        max_output_tokens: int = 2048,
        fallback: Any | None = "rules",
    ) -> None:
        self.client = client
        self.exposure = LLMExposure(str(exposure)) if not isinstance(exposure, LLMExposure) else exposure
        self.max_tokens_budget = max_tokens_budget
        self.max_output_tokens = max_output_tokens
        self.fallback = fallback
        self.last_response: LLMResponse | None = None

    def plan(
        self,
        df: pd.DataFrame,
        profile: DataFrameProfile,
        issues: Issues,
        *,
        schema: Any | None = None,
        mode: Mode | str = Mode.REVIEW,
        options: dict[str, Any] | None = None,
    ) -> Recipe:
        from .fingerprint import fingerprint_dataframe

        try:
            recipe = self._plan_via_llm(df, profile, issues, schema)
        except (LLMError, BudgetExceeded) as exc:
            if self.fallback is None:
                raise
            recipe = self._fallback().plan(
                df, profile, issues, schema=schema, mode=mode, options=options
            )
            recipe.meta["llm_fallback"] = str(exc)
            return recipe

        if recipe.source_fingerprint is None:
            recipe.source_fingerprint = fingerprint_dataframe(df)
        recipe.stamp_meta(
            generated_by=f"llm:{self.client.model}",
            mode=Mode.coerce(mode).value,
            llm_exposure=self.exposure.value,
        )
        return recipe

    def _plan_via_llm(self, df, profile, issues, schema) -> Recipe:
        metadata = build_metadata(df, profile, issues, schema, self.exposure)
        system, user = build_prompt(metadata)
        if self.max_tokens_budget is not None:
            estimate = _estimate_tokens(system) + _estimate_tokens(user) + self.max_output_tokens
            if estimate > self.max_tokens_budget:
                raise BudgetExceeded(
                    f"Estimated {estimate} tokens exceeds max_tokens_budget={self.max_tokens_budget}."
                )
        response = self.client.complete(system, user, max_tokens=self.max_output_tokens)
        self.last_response = response
        if self.max_tokens_budget is not None and response.total_tokens > self.max_tokens_budget:
            raise BudgetExceeded(
                f"Used {response.total_tokens} tokens, over max_tokens_budget={self.max_tokens_budget}."
            )
        return parse_recipe_json(response.text)

    def _fallback(self):
        from .planner import RulesPlanner

        return RulesPlanner() if self.fallback in ("rules", True) else self.fallback


def _estimate_tokens(text: str) -> int:
    # ~4 chars/token is a good enough pre-flight estimate for budgeting.
    return max(1, len(text) // 4)


__all__ = [
    "LLMClient",
    "LLMResponse",
    "LLMPlanner",
    "AnthropicClient",
    "OpenAIClient",
    "ProviderSpec",
    "PROVIDERS",
    "get_client",
    "list_providers",
    "build_metadata",
    "build_prompt",
    "parse_recipe_json",
    "value_sketches",
]
