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

#: Default HTTP timeout (seconds) for LLM provider calls.
LLM_HTTP_TIMEOUT = 60.0


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
            raise LLMError("The 'anthropic' package is required. Install cleanframe-engine[llm].") from exc
        client = anthropic.Anthropic(api_key=self._api_key, timeout=LLM_HTTP_TIMEOUT)
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
                + (
                    " (or OPENAI_API_KEY as a fallback)."
                    if self._key_env != "OPENAI_API_KEY"
                    else "."
                )
            )
        try:
            import openai
        except ImportError as exc:  # pragma: no cover - optional dep
            raise LLMError("The 'openai' package is required. Install cleanframe-engine[llm].") from exc
        kwargs: dict[str, Any] = {"api_key": self._api_key, "timeout": LLM_HTTP_TIMEOUT}
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
                "PII patterns (emails/phones) and long strings are redacted; short "
                "category/text values are sent verbatim — approve before sending off-machine"
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

Available ops: {ops}

Rules: prefer minimal, safe transforms; do NOT impute missing values; only propose a
rename when it improves clarity; put ops in a sensible execution order."""


def build_prompt(metadata: dict) -> tuple[str, str]:
    system = _SYSTEM_PROMPT.replace("{ops}", ", ".join(list_ops()))
    user = "Here is the dataset metadata. Produce the JSON recipe.\n\n" + json.dumps(
        metadata, indent=2, ensure_ascii=False
    )
    return system, user


#: Ops the LLM may not emit under stricter modes (rules planner already gates these
#: via confidence thresholds; the model has no confidence scores to filter on).
_LLM_BLOCKED_AUTO = frozenset({"fill_na"})
_LLM_BLOCKED_STRICT = frozenset({"fill_na", "drop_columns"})


def _extract_json_object(text: str) -> str:
    """Return the first top-level ``{...}`` object using a brace-depth scan.

    A greedy ``\\{.*\\}`` regex can swallow trailing prose or pick the wrong blob
    when the model wraps JSON in commentary that also contains braces.
    """
    start = text.find("{")
    if start < 0:
        raise LLMError("LLM response contained no JSON object.")
    depth = 0
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
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise LLMError("LLM response contained no complete JSON object.")


def _finalize_llm_recipe(recipe: Recipe, *, mode: Mode) -> Recipe:
    """Canonicalise op order and strip mode-inappropriate ops from an LLM recipe."""
    from .planner import _finalize_ops

    blocked = set()
    if mode is Mode.STRICT:
        blocked = set(_LLM_BLOCKED_STRICT)
    elif mode is Mode.AUTO:
        blocked = set(_LLM_BLOCKED_AUTO)

    removed: list[str] = []
    for col in recipe.columns:
        ops = [op for op in col.ops if op.name not in blocked]
        removed += [f"{op.name} on {col.source}" for op in col.ops if op.name in blocked]
        col.ops = _finalize_ops(ops)
    removed += [op.name for op in recipe.frame_ops if op.name in blocked]
    recipe.frame_ops = [op for op in recipe.frame_ops if op.name not in blocked]
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


def parse_recipe_json(text: str) -> Recipe:
    """Extract and validate a Recipe from the model's response.

    Validation runs through :meth:`Recipe.from_dict`, so an unknown op or malformed
    structure never reaches the data. Steps the model wrote that cannot load are
    dropped and listed in ``recipe.meta["llm_dropped"]`` rather than discarding the
    whole plan.
    """
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{") :] if "{" in text else text
    blob = _extract_json_object(text)
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
        if self.max_tokens_budget is not None:
            estimate = _estimate_tokens(system) + _estimate_tokens(user) + self.max_output_tokens
            if estimate > self.max_tokens_budget:
                raise BudgetExceeded(
                    f"Estimated {estimate} tokens exceeds max_tokens_budget={self.max_tokens_budget}."
                )
        response = self.client.complete(system, user, max_tokens=self.max_output_tokens)
        self.last_response = response  # noqa: RUF100
        if self.max_tokens_budget is not None and response.total_tokens > self.max_tokens_budget:
            raise BudgetExceeded(
                f"Used {response.total_tokens} tokens, over max_tokens_budget={self.max_tokens_budget}."
            )
        recipe = _finalize_llm_recipe(parse_recipe_json(response.text), mode=mode)
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

    def _fallback(self):
        from .planner import RulesPlanner

        if self.fallback is None or self.fallback in ("rules", True):
            return RulesPlanner()
        return self.fallback


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
