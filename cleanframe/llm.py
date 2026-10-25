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
import logging
import os
import re
import time
import warnings
from dataclasses import dataclass
from typing import Any, Protocol

import pandas as pd

from ._util import DETECTOR_SAMPLE_CAP, sample_non_null
from .errors import BudgetExceeded, CleanFrameWarning, LLMError
from .issues import Issues
from .ops import list_ops
from .profile import DataFrameProfile
from .recipe import Recipe
from .types import LLMExposure, Mode

logger = logging.getLogger(__name__)

#: Default cap on the tokens a model may write per call, reasoning included. Reasoning
#: models think before they answer: at 2048, three of six models tested in 2026-10
#: spent the whole cap thinking and returned no answer. Only tokens actually written
#: are billed, and a cap above a model's own limit is lowered to it (see
#: :func:`_lower_cap`).
DEFAULT_MAX_OUTPUT_TOKENS = 32_768
#: Seconds to wait for the connection, and for the answer. A reasoning model may take
#: minutes to write a long answer; an unreachable host should fail fast.
LLM_CONNECT_TIMEOUT = 15.0
LLM_HTTP_TIMEOUT = 600.0
#: Retries the provider SDK makes on rate limits, server errors and timeouts. One
#: keeps a stalled request from hanging a run for half an hour.
LLM_MAX_RETRIES = 1


# ---------------------------------------------------------------------------
# Client interface + provider adapters
# ---------------------------------------------------------------------------
@dataclass
class LLMResponse:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    #: ``"stop"``, ``"length"`` (the output cap was hit), ... or ``None`` if unknown.
    finish_reason: str | None = None
    #: Output tokens the model spent reasoning (included in ``output_tokens``).
    reasoning_tokens: int = 0
    #: USD, when the provider reports it (OpenRouter does).
    cost: float | None = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class LLMClient(Protocol):
    """Minimal provider interface. ``complete`` returns text plus token accounting."""

    model: str

    def complete(
        self, system: str, user: str, *, max_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    ) -> LLMResponse:
        """Return the model's reply plus token accounting."""


def _timeout(sdk: Any) -> Any:
    return sdk.Timeout(LLM_HTTP_TIMEOUT, connect=LLM_CONNECT_TIMEOUT)


_CAP_WORDS = ("max_tokens", "max_completion_tokens", "output tokens", "completion tokens")


_CONTEXT_RE = re.compile(r"(?:maximum context length|context (?:length|window))\D{0,24}(\d+)")
_PROMPT_USED_RE = re.compile(r"(\d+) (?:input tokens|prompt tokens|in the messages|tokens in the prompt)")
#: Tokens kept free when a cap is derived from a context window (tokenizers differ).
_CONTEXT_MARGIN = 64


def _lower_cap(message: str, cap: int, prompt_tokens: int = 0) -> int | None:
    """The output cap a provider's "cap too large" error allows, or ``None``.

    OpenRouter lowers a cap above the model's limit by itself; direct APIs refuse it
    with a 400 that names the limit (OpenAI: "supports at most 16384 completion
    tokens", Anthropic: "32768 > 8192, which is the maximum", DeepSeek: "valid range
    of max_tokens is [1, 8192]"): the largest number below ``cap`` is that limit. A
    local server with a small context window (vLLM: "maximum context length is 8192
    tokens ... 1300 in the messages") allows the window minus the prompt; the prompt
    size comes from the message, or else from ``prompt_tokens``.
    """
    text = message.lower().replace(",", "")
    window = _CONTEXT_RE.search(text)
    if window:
        used = _PROMPT_USED_RE.search(text)
        room = int(window.group(1)) - (int(used.group(1)) if used else prompt_tokens) - _CONTEXT_MARGIN
        return room if 1024 <= room < cap else None
    if not any(word in text for word in _CAP_WORDS):
        return None
    below = [n for n in (int(x) for x in re.findall(r"\d+", text)) if 1024 <= n < cap]
    return max(below) if below else None


def _log_call(model: str, response: LLMResponse, seconds: float) -> None:
    logger.info(
        "llm %s: %d in / %d out tokens (%d reasoning), finish=%s, %.1fs%s",
        model, response.input_tokens, response.output_tokens, response.reasoning_tokens,
        response.finish_reason, seconds,
        f", ${response.cost:.6f}" if response.cost is not None else "",
    )


def check_answer(response: LLMResponse, max_tokens: int) -> None:
    """Raise :class:`LLMError` naming the cause when a reply has no answer in it."""
    if (response.text or "").strip():
        return
    if response.finish_reason == "length":
        spent = (
            f" ({response.reasoning_tokens} of them reasoning)" if response.reasoning_tokens else ""
        )
        raise LLMError(
            f"The model used its whole {max_tokens}-token output cap{spent} before it wrote "
            "an answer. Raise max_output_tokens, or use a model that reasons less."
        )
    reason = f" (finish_reason={response.finish_reason})" if response.finish_reason else ""
    raise LLMError(f"The model returned an empty answer{reason}.")


class AnthropicClient:
    """Adapter for the Anthropic Messages API. Requires ``anthropic`` + ``ANTHROPIC_API_KEY``."""

    def __init__(self, model: str, api_key: str | None = None) -> None:
        self.model = model
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")

    def complete(
        self, system: str, user: str, *, max_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    ) -> LLMResponse:
        if not self._api_key:
            raise LLMError("ANTHROPIC_API_KEY is not set.")
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - optional dep
            raise LLMError("The 'anthropic' package is required. Install cleanframe-engine[llm].") from exc
        client = anthropic.Anthropic(
            api_key=self._api_key, timeout=_timeout(anthropic), max_retries=LLM_MAX_RETRIES
        )
        start = time.perf_counter()
        while True:
            try:
                msg = client.messages.create(
                    model=self.model,
                    max_tokens=max_tokens,
                    system=system,
                    messages=[{"role": "user", "content": user}],
                )
                break
            except anthropic.BadRequestError as exc:
                lower = _lower_cap(str(exc), max_tokens, _estimate_tokens(system + user))
                if lower is None:
                    raise
                logger.info("llm %s: output cap %d refused, retrying at %d", self.model, max_tokens, lower)
                max_tokens = lower
        text = "".join(block.text for block in msg.content if getattr(block, "type", "") == "text")
        stop = getattr(msg, "stop_reason", None)
        response = LLMResponse(
            text,
            msg.usage.input_tokens,
            msg.usage.output_tokens,
            finish_reason="length" if stop == "max_tokens" else stop,
        )
        _log_call(self.model, response, time.perf_counter() - start)
        return response


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
        # Only the variable this provider names is ever read. A key for one vendor must
        # never be sent to another, so there is no OPENAI_API_KEY fallback here.
        self._api_key = api_key or os.environ.get(key_env)
        self._base_url = base_url or os.environ.get("OPENAI_BASE_URL")
        self._default_headers = default_headers
        self._key_env = key_env

    def complete(
        self, system: str, user: str, *, max_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    ) -> LLMResponse:
        if not self._api_key:
            raise LLMError(
                f"{self._key_env} is not set. Set it, or pass api_key= to the client."
            )
        try:
            import openai
        except ImportError as exc:  # pragma: no cover - optional dep
            raise LLMError("The 'openai' package is required. Install cleanframe-engine[llm].") from exc
        kwargs: dict[str, Any] = {
            "api_key": self._api_key,
            "timeout": _timeout(openai),
            "max_retries": LLM_MAX_RETRIES,
        }
        if self._base_url:
            kwargs["base_url"] = self._base_url
        if self._default_headers:
            kwargs["default_headers"] = self._default_headers
        client = openai.OpenAI(**kwargs)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        cap_param = "max_tokens"
        start = time.perf_counter()
        while True:
            request: dict[str, Any] = {
                "model": self.model, "messages": messages, cap_param: max_tokens,
            }
            try:
                resp = client.chat.completions.create(**request)
                break
            except (openai.BadRequestError, openai.UnprocessableEntityError) as exc:
                text = str(exc)
                if cap_param == "max_tokens" and "max_completion_tokens" in text:
                    # OpenAI's reasoning models refuse max_tokens and name the new field.
                    cap_param = "max_completion_tokens"
                    continue
                lower = _lower_cap(text, max_tokens, _estimate_tokens(system + user))
                if lower is None:
                    raise
                logger.info("llm %s: output cap %d refused, retrying at %d", self.model, max_tokens, lower)
                max_tokens = lower
        if not resp.choices:
            # OpenRouter reports some upstream failures inside a 200 response.
            error = (getattr(resp, "model_extra", None) or {}).get("error")
            detail = f": {error.get('message', error)}" if isinstance(error, dict) else ""
            raise LLMError(f"The provider returned no answer for {self.model}{detail}.")
        choice = resp.choices[0]
        usage = resp.usage
        details = getattr(usage, "completion_tokens_details", None)
        extra = getattr(usage, "model_extra", None) or {}
        cost = extra.get("cost")
        response = LLMResponse(
            choice.message.content or "",
            getattr(usage, "prompt_tokens", 0) or 0,
            getattr(usage, "completion_tokens", 0) or 0,
            finish_reason=getattr(choice, "finish_reason", None),
            reasoning_tokens=int(getattr(details, "reasoning_tokens", 0) or 0),
            cost=float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None,
        )
        _log_call(self.model, response, time.perf_counter() - start)
        return response


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
                            "HTTP-Referer": "https://github.com/inboxpraveen/Cleanframe",
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
        key_env="OLLAMA_API_KEY",  # optional: local servers need no key
        default_key="ollama",
    ),
    ProviderSpec(
        "lmstudio",
        kind="openai",
        base_url="http://localhost:1234/v1",
        key_env="LMSTUDIO_API_KEY",  # optional: local servers need no key
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
    if not isinstance(spec, str):
        raise LLMError(
            f"LLM spec must be a 'provider/model' string, got {type(spec).__name__}."
        )
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

    # OPENAI_BASE_URL may only redirect the openai / openai-compatible / azure
    # family — never OpenRouter/Groq/etc., where a leaked env would exfiltrate keys.
    if info.name in ("openai", "openai-compatible", "azure"):
        base_url = os.environ.get("OPENAI_BASE_URL") or info.base_url
    else:
        base_url = info.base_url
    # Each provider reads only its own key variable (or a keyless local default).
    # OPENAI_API_KEY is used solely by the openai / azure / openai-compatible family,
    # whose key_env it is - it is never forwarded to any other vendor.
    api_key = os.environ.get(info.key_env) or info.default_key
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
    # Cap materialisation so SAMPLE mode cannot load a multi-million-row column.
    values = sample_non_null(series, cap=min(DETECTOR_SAMPLE_CAP, 10_000))
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
                "emails, phones and long strings are redacted; short "
                "category/text values are sent verbatim"
            )
        columns.append(entry)

    payload: dict[str, Any] = {
        "row_count": profile.n_rows,
        "columns": columns,
        # Only the structured facts of each issue are forwarded — NOT the free-text
        # message, which can embed a raw cell value (a constant column's value, a
        # disguised-null token, …). Under METADATA exposure no raw value may leave.
        "detected_issues": [
            {
                "column": i.column,
                "kind": i.kind,
                "severity": i.severity.value,
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

Column ops, with the only parameters each accepts in brackets: {ops}
Whole-table ops, as top-level keys like "dedup" above: {frame_ops}

Rules: prefer minimal, safe transforms; do NOT impute missing values; only propose a
rename when it improves clarity; put ops in a sensible execution order; use only the
parameters listed; "validate" and "dedup" name a column by its final name (its
rename_to, if it has one); a validation's "on_fail" is "quarantine", "warn" or "error"."""


def _op_catalog(scope: str) -> str:
    """``name[param, ...]`` for every op of ``scope`` an LLM plan may use."""
    from .ops import OP_REGISTRY

    out = []
    for name in list_ops():
        spec = OP_REGISTRY[name]
        if spec.scope != scope or name in _LLM_BLOCKED_OPS:
            continue
        params = sorted(spec.known_params)
        out.append(f"{name}[{', '.join(params)}]" if params else name)
    return ", ".join(out)


def build_prompt(metadata: dict) -> tuple[str, str]:
    system = _SYSTEM_PROMPT.replace("{ops}", _op_catalog("column")).replace(
        "{frame_ops}", _op_catalog("frame")
    )
    user = "Here is the dataset metadata. Produce the JSON recipe.\n\n" + json.dumps(
        metadata, indent=2, ensure_ascii=False
    )
    return system, user


def coerce_exposure(value: Any) -> LLMExposure:
    """Parse an ``llm_exposure`` value, case-insensitively, or raise :class:`LLMError`."""
    if isinstance(value, LLMExposure):
        return value
    try:
        return LLMExposure(str(value).strip().lower())
    except ValueError:
        valid = ", ".join(repr(e.value) for e in LLMExposure)
        raise LLMError(f"llm_exposure must be one of {valid}; got {value!r}.") from None


#: Ops an LLM plan may never contain, in any mode. Imputing a value or dropping a
#: column is a human decision (CONTRIBUTING invariant #4: nothing is silently
#: imputed or dropped); a model has no confidence score to gate it on, and
#: ``cf.clean`` executes the recipe immediately, so "review" would not be a gate.
_LLM_BLOCKED_OPS = frozenset({"fill_na", "drop_columns"})

#: Validation policies that discard or blank data. A model may only ask for a check
#: with a policy that keeps the offending row visible (quarantine / warn / error).
_LLM_UNSAFE_ON_FAIL = frozenset({"drop", "null"})

#: Rows the planner executes a model's recipe on to prove it runs.
_DRY_RUN_ROWS = 200


#: A reasoning block some servers put in the answer itself (Ollama, vLLM, LM Studio
#: running DeepSeek-R1 / Qwen3). Its draft JSON is not the answer. An unclosed block
#: (the cap cut it off) runs to the end.
_THINK_RE = re.compile(r"<(think|thinking|reasoning)>.*?(?:</\1>|\Z)", re.DOTALL | re.IGNORECASE)


def _extract_json_object(text: str, *, close_unbalanced: bool = False) -> str:
    """Return the first top-level ``{...}`` object using a bracket-depth scan.

    A greedy ``\\{.*\\}`` regex can swallow trailing prose or pick the wrong blob
    when the model wraps JSON in commentary that also contains braces. A
    ``<think>`` block in the reply is skipped.

    ``close_unbalanced`` repairs one common model slip: a reply that ends, outside any
    string, with its last closing brackets missing (``{"a": {"b": 1}``). Callers pass it
    only for a reply the model finished on its own; a reply cut off by the output cap
    may end mid-value (``0.97`` -> ``0.9``) and is never completed.
    """
    text = _THINK_RE.sub("", text)
    start = text.find("{")
    if start < 0:
        raise LLMError("LLM response contained no JSON object.")
    stack: list[str] = []
    in_str = False
    escape = False
    for i, ch in enumerate(text[start:], start=start):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if stack:
                stack.pop()
            if not stack:
                return text[start : i + 1]
    if close_unbalanced and stack and not in_str:
        logger.info("closed %d unbalanced bracket(s) at the end of the model's reply", len(stack))
        return text[start:].rstrip() + "".join(reversed(stack))
    raise LLMError("LLM response contained no complete JSON object.")


#: Finish reasons that mean the model ended its reply itself (OpenAI-style, Anthropic).
_FINISHED = frozenset({"stop", "end_turn", "stop_sequence"})


def finished_on_its_own(response: Any) -> bool:
    """Whether the provider says the model ended the reply itself (not the cap, not unknown)."""
    return getattr(response, "finish_reason", None) in _FINISHED


def _point_at_output_names(recipe: Recipe) -> None:
    """Point validations and dedup keys that name a renamed column's *source* at its
    output name.

    Both run after renames, so ``{"column": "Email"}`` beside ``Email -> email`` was
    skipped with a warning and the check silently did nothing. Models write it either
    way. Each change is listed in ``recipe.meta["llm_renamed_refs"]``.
    """
    outputs = {c.output_name for c in recipe.columns}
    renamed = {
        c.source: c.output_name
        for c in recipe.columns
        if c.output_name != c.source and c.source not in outputs
    }
    if not renamed:
        return
    changed: list[str] = []
    for rule in recipe.validations:
        if rule.column in renamed:
            changed.append(f"validation {rule.check}: {rule.column} -> {renamed[rule.column]}")
            rule.column = renamed[rule.column]
    for op in recipe.frame_ops:
        subset = op.params.get("subset") if op.name == "dedup" else None
        if isinstance(subset, list) and any(c in renamed for c in subset):
            changed.append(f"dedup subset: {subset} -> {[renamed.get(c, c) for c in subset]}")
            op.params["subset"] = [renamed.get(c, c) for c in subset]
    if changed:
        recipe.meta["llm_renamed_refs"] = changed


def _finalize_llm_recipe(recipe: Recipe, *, mode: Mode) -> Recipe:
    """Canonicalise op order and strip ops an LLM plan may never contain.

    ``fill_na`` and ``drop_columns`` are removed in every mode, and validation
    rules that would drop or blank data are downgraded to quarantine; everything
    removed is listed in ``recipe.meta["llm_blocked_ops"]``.
    """
    from .planner import _finalize_ops

    _point_at_output_names(recipe)
    blocked = _LLM_BLOCKED_OPS
    removed: list[str] = []
    for col in recipe.columns:
        ops = [op for op in col.ops if op.name not in blocked]
        removed += [f"{op.name} on {col.source}" for op in col.ops if op.name in blocked]
        col.ops = _finalize_ops(ops)
    removed += [op.name for op in recipe.frame_ops if op.name in blocked]
    recipe.frame_ops = [op for op in recipe.frame_ops if op.name not in blocked]
    for rule in recipe.validations:
        if rule.on_fail in _LLM_UNSAFE_ON_FAIL:
            removed.append(f"on_fail: {rule.on_fail} on {rule.column} (kept as quarantine)")
            rule.on_fail = "quarantine"
    if removed:
        recipe.meta["llm_blocked_ops"] = removed
    return recipe


def _sanitize_llm_recipe(data: Any) -> list[str]:
    """Drop model-written steps a recipe cannot load, returning what was dropped.

    A hand-written recipe fails loud on a misspelled parameter — that is the point.
    Model output is different: one stray parameter would throw away an otherwise
    good plan, so the bad step is removed and reported instead. The result still
    goes through :meth:`Recipe.from_dict`, so nothing unvalidated reaches the data.
    """
    from .errors import CleanFrameError
    from .ops import OP_REGISTRY, normalize_op
    from .recipe import _fuse_scalar_arguments

    dropped: list[str] = []
    if not isinstance(data, dict):
        return dropped

    def clean_entry(entry: Any, where: str, scope: str) -> Any:
        if isinstance(entry, str):
            name, value = entry, None
        elif isinstance(entry, dict) and len(entry) == 1:
            name, value = next(iter(entry.items()))
        elif (
            isinstance(entry, (list, tuple))
            and 1 <= len(entry) <= 2
            and isinstance(entry[0], str)
        ):
            name = entry[0]
            value = entry[1] if len(entry) == 2 else None
        else:
            dropped.append(f"{where}: unrecognised op {entry!r}")
            return None
        spec = OP_REGISTRY.get(str(name))
        if spec is None or spec.scope != scope:
            dropped.append(f"{where}: unusable op {name!r}")
            return None
        if isinstance(value, dict) and not spec.free_form and spec.known_params:
            unknown = sorted(str(k) for k in value if str(k) not in spec.known_params)
            if unknown:
                value = {k: v for k, v in value.items() if str(k) not in unknown}
                dropped.append(f"{where}: {name} parameter(s) {unknown}")
        try:
            normalize_op(str(name), value)
        except CleanFrameError as exc:
            dropped.append(f"{where}: {name} ({exc})")
            return None
        return name if value is None or value == {} else {name: value}

    columns = data.get("columns")
    if isinstance(columns, dict):
        for column, spec in list(columns.items()):
            if not isinstance(spec, dict):
                continue
            rename = spec.get("rename_to")
            if rename is not None and (not isinstance(rename, str) or not rename.strip()):
                dropped.append(f"column {column!r}: rename_to {rename!r}")
                spec.pop("rename_to")
            raw_ops = spec.get("ops")
            if isinstance(raw_ops, (list, tuple)):
                kept = [
                    cleaned
                    for entry in _fuse_scalar_arguments(list(raw_ops))
                    if (cleaned := clean_entry(entry, f"column {column!r}", "column"))
                    is not None
                ]
                spec["ops"] = kept

    raw_frame_ops = data.get("frame_ops")
    if isinstance(raw_frame_ops, (list, tuple)):
        data["frame_ops"] = [
            cleaned
            for entry in _fuse_scalar_arguments(list(raw_frame_ops))
            if (cleaned := clean_entry(entry, "frame_ops", "frame")) is not None
        ]

    rules = data.get("validate")
    if isinstance(rules, (list, tuple)):
        from .recipe import ValidationRule

        kept_rules = []
        for rule in rules:
            try:
                ValidationRule.from_dict(rule)
            except CleanFrameError as exc:
                dropped.append(f"validation {rule!r} ({exc})")
                continue
            kept_rules.append(rule)
        data["validate"] = kept_rules

    return dropped


def parse_recipe_json(text: str, *, close_unbalanced: bool = False) -> Recipe:
    """Extract and validate a Recipe from the model's response.

    Validation runs through :meth:`Recipe.from_dict`, so an unknown op or malformed
    structure never reaches the data. Steps the model wrote that cannot load are
    dropped and listed in ``recipe.meta["llm_dropped"]`` rather than discarding the
    whole plan. ``close_unbalanced`` (see :func:`_extract_json_object`) is for a reply
    the model finished on its own.
    """
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{") :] if "{" in text else text
    blob = _extract_json_object(text, close_unbalanced=close_unbalanced)
    try:
        data = json.loads(blob)
    except json.JSONDecodeError as exc:
        raise LLMError(f"LLM returned invalid JSON: {exc}") from exc
    dropped = _sanitize_llm_recipe(data)
    try:
        recipe = Recipe.from_dict(data)
    except LLMError:
        raise
    except Exception as exc:  # noqa: BLE001 - any validation failure => a clean LLMError
        # Wrap RecipeError and any stray KeyError/TypeError from a malformed model
        # recipe so the caller only ever sees LLMError (and the planner falls back).
        raise LLMError(f"LLM produced an invalid recipe: {exc}") from exc
    if dropped:
        recipe.meta["llm_dropped"] = dropped
    return recipe


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
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        fallback: Any | None = "rules",
    ) -> None:
        self.client = client
        self.exposure = coerce_exposure(exposure)
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

        mode = Mode.coerce(mode)
        if self.exposure is LLMExposure.NONE:
            warnings.warn(
                "CleanFrame: exposure='none' keeps everything local, so no request was "
                "made — planning with deterministic rules instead.",
                CleanFrameWarning,
                stacklevel=2,
            )
            recipe = self._fallback().plan(
                df, profile, issues, schema=schema, mode=mode, options=options
            )
            recipe.meta["llm_exposure"] = "none"
            return recipe
        try:
            recipe = self._plan_via_llm(df, profile, issues, schema, mode=mode)
        except Exception as exc:  # noqa: BLE001
            # The promise is "any failure degrades to deterministic rules so the
            # pipeline never dies because an LLM was flaky" — that must hold for a
            # bad key, over-budget, malformed output, AND a provider/network error,
            # not just LLMError/BudgetExceeded. (KeyboardInterrupt/SystemExit are
            # BaseException and correctly propagate.)
            if self.fallback is None:
                if isinstance(exc, LLMError):
                    raise
                raise LLMError(f"LLM planning failed: {exc}") from exc
            warnings.warn(
                f"CleanFrame: LLM planning failed ({exc}); falling back to rules planner. "
                "Inspect recipe.meta['llm_fallback'] for details.",
                CleanFrameWarning,
                stacklevel=2,
            )
            recipe = self._fallback().plan(
                df, profile, issues, schema=schema, mode=mode, options=options
            )
            recipe.meta["llm_fallback"] = str(exc)
            return recipe

        if recipe.source_fingerprint is None:
            recipe.source_fingerprint = fingerprint_dataframe(df)
        recipe.stamp_meta(
            generated_by=f"llm:{self.client.model}",
            mode=mode.value,
            llm_exposure=self.exposure.value,
        )
        return recipe

    def _plan_via_llm(self, df, profile, issues, schema, *, mode: Mode) -> Recipe:
        metadata = build_metadata(df, profile, issues, schema, self.exposure)
        system, user = build_prompt(metadata)
        cap = self.max_output_tokens
        if self.max_tokens_budget is not None:
            # The answer may use whatever the prompt leaves of the budget, so a
            # generous default cap never turns a sensible budget into a refusal.
            prompt = _estimate_tokens(system) + _estimate_tokens(user)
            room = self.max_tokens_budget - prompt
            if room < _MIN_ANSWER_TOKENS:
                raise BudgetExceeded(
                    f"The prompt alone is about {prompt} tokens, which leaves {max(room, 0)} of "
                    f"max_tokens_budget={self.max_tokens_budget} for the answer "
                    f"(at least {_MIN_ANSWER_TOKENS} are needed)."
                )
            cap = min(cap, room)
        response = self.client.complete(system, user, max_tokens=cap)
        self.last_response = response  # noqa: RUF100
        if self.max_tokens_budget is not None and response.total_tokens > self.max_tokens_budget:
            raise BudgetExceeded(
                f"Used {response.total_tokens} tokens, over max_tokens_budget={self.max_tokens_budget}."
            )
        check_answer(response, cap)
        if self.exposure is LLMExposure.SAMPLE:
            sent = [c["name"] for c in metadata["columns"] if c.get("example_values")]
            warnings.warn(
                "CleanFrame: llm_exposure='sample' sends example cell values off this "
                "machine (short text/category values verbatim; emails, phones and long "
                f"strings are redacted). Columns with values sent: {', '.join(sent) or 'none'}.",
                CleanFrameWarning,
                stacklevel=2,
            )
        try:
            parsed = parse_recipe_json(
                response.text, close_unbalanced=finished_on_its_own(response)
            )
        except LLMError as exc:
            if getattr(response, "finish_reason", None) == "length":
                raise LLMError(
                    f"The answer was cut off at the {cap}-token output cap ({exc}). "
                    "Raise max_output_tokens."
                ) from exc
            raise
        recipe = _finalize_llm_recipe(parsed, mode=mode)
        blocked = recipe.meta.get("llm_blocked_ops")
        if blocked:
            warnings.warn(
                "CleanFrame: removed step(s) an LLM plan may not contain - a person must "
                f"add these to the recipe: {'; '.join(blocked)}. "
                "See recipe.meta['llm_blocked_ops'].",
                CleanFrameWarning,
                stacklevel=2,
            )
        self._dry_run(recipe, df)
        dropped = recipe.meta.get("llm_dropped")
        if dropped:
            warnings.warn(
                f"CleanFrame: {len(dropped)} step(s) the model wrote could not be used and "
                f"were dropped ({'; '.join(dropped[:3])}"
                + (", …)" if len(dropped) > 3 else ")")
                + ". See recipe.meta['llm_dropped'].",
                CleanFrameWarning,
                stacklevel=2,
            )
        return recipe

    @staticmethod
    def _dry_run(recipe: Recipe, df: pd.DataFrame) -> None:
        """Execute the model's recipe on a small head so an unusable plan fails here.

        A rename collision, a rejected regex or a bad parameter would otherwise only
        surface at execution, after planning succeeded, and bypass the fallback.
        """
        from .errors import ValidationFailure
        from .executor import execute

        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                execute(recipe, df.head(_DRY_RUN_ROWS), mode=Mode.REVIEW, max_diff_changes=0)
        except ValidationFailure:
            pass  # a data problem the recipe is meant to surface, not a bad plan
        except Exception as exc:  # noqa: BLE001
            raise LLMError(f"LLM produced a recipe that cannot run: {exc}") from exc

    def _fallback(self):
        from .planner import RulesPlanner

        if self.fallback is None or self.fallback in ("rules", True):
            return RulesPlanner()
        return self.fallback


#: The smallest output cap worth sending under ``max_tokens_budget``.
_MIN_ANSWER_TOKENS = 1024


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
    "coerce_exposure",
    "build_metadata",
    "build_prompt",
    "parse_recipe_json",
    "value_sketches",
    "check_answer",
    "finished_on_its_own",
    "DEFAULT_MAX_OUTPUT_TOKENS",
]
