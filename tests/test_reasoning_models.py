"""Reasoning models through the LLM planner and decisions, and decision models on OpenRouter.

Reasoning models think before they answer, and the thinking counts against the output
cap: at the old 2048-token cap three of six models tested in 2026-10 returned an empty
answer, which quietly fell back to the rules. These tests pin the cap, the retries on a
provider's own cap, the truncation messages, the budget clamp and the d1 routing.
"""

from __future__ import annotations

import json
import sys
import types

import pandas as pd
import pytest

import cleanframe as cf
from cleanframe.decisions import (
    LLMDecisionClient,
    Question,
    SystemOneClient,
    get_decision_client,
)
from cleanframe.detectors import run_detectors
from cleanframe.errors import BudgetExceeded, LLMError
from cleanframe.llm import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    LLM_HTTP_TIMEOUT,
    LLM_MAX_RETRIES,
    AnthropicClient,
    LLMPlanner,
    LLMResponse,
    OpenAIClient,
    _lower_cap,
    build_prompt,
    parse_recipe_json,
)
from cleanframe.profile import profile_dataframe

RECIPE = {"version": 1, "columns": {"Email": {"rename_to": "email", "ops": ["normalize_email"]}}}


# --------------------------------------------------------------------- a fake openai SDK
class _StatusError(Exception):
    def __init__(self, message):
        super().__init__(message)


def _fake_openai(monkeypatch, replies):
    """Install a fake ``openai`` module whose create() pops ``replies`` in order.

    A reply is an exception to raise or a ``(content, finish_reason, usage)`` tuple.
    """
    calls: list[dict] = []
    clients: list[dict] = []
    module = types.ModuleType("openai")

    class BadRequestError(_StatusError):
        pass

    class UnprocessableEntityError(_StatusError):
        pass

    class Timeout:
        def __init__(self, timeout, connect=None):
            self.timeout, self.connect = timeout, connect

    class _Completions:
        def create(self, **kwargs):
            calls.append(kwargs)
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            content, finish, usage = reply
            return types.SimpleNamespace(
                choices=[types.SimpleNamespace(
                    message=types.SimpleNamespace(content=content), finish_reason=finish
                )],
                usage=usage,
            )

    class OpenAI:
        def __init__(self, **kwargs):
            clients.append(kwargs)
            self.chat = types.SimpleNamespace(completions=_Completions())

    module.OpenAI = OpenAI
    module.BadRequestError = BadRequestError
    module.UnprocessableEntityError = UnprocessableEntityError
    module.Timeout = Timeout
    monkeypatch.setitem(sys.modules, "openai", module)
    return module, calls, clients


def _usage(prompt=100, completion=50, reasoning=0, cost=None):
    return types.SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        completion_tokens_details=types.SimpleNamespace(reasoning_tokens=reasoning),
        model_extra={"cost": cost} if cost is not None else {},
    )


def test_default_output_cap_leaves_room_for_reasoning():
    assert DEFAULT_MAX_OUTPUT_TOKENS >= 32_768
    assert LLMPlanner(object()).max_output_tokens == DEFAULT_MAX_OUTPUT_TOKENS
    assert LLMDecisionClient(types.SimpleNamespace(complete=None)).max_output_tokens == DEFAULT_MAX_OUTPUT_TOKENS


def test_openai_client_sends_the_cap_and_reads_reasoning_usage(monkeypatch):
    _, calls, clients = _fake_openai(monkeypatch, [("{}", "stop", _usage(1316, 6452, 6055, 0.003))])
    resp = OpenAIClient("qwen/qwen3.8-flash", api_key="k").complete("s", "u")
    assert calls[0]["max_tokens"] == DEFAULT_MAX_OUTPUT_TOKENS
    timeout = clients[0]["timeout"]
    assert timeout.timeout == LLM_HTTP_TIMEOUT and timeout.connect < 60
    assert clients[0]["max_retries"] == LLM_MAX_RETRIES
    assert (resp.input_tokens, resp.output_tokens, resp.reasoning_tokens) == (1316, 6452, 6055)
    assert resp.finish_reason == "stop" and resp.cost == pytest.approx(0.003)


def test_openai_client_retries_at_the_limit_a_provider_names(monkeypatch):
    replies: list = []
    module, calls, _ = _fake_openai(monkeypatch, replies)
    replies += [
        module.BadRequestError(
            "Error code: 400 - max_tokens is too large: 32768. This model supports at most "
            "16384 completion tokens, whereas you provided 32768."
        ),
        ("{}", "stop", _usage()),
    ]
    OpenAIClient("gpt-4o-mini", api_key="k").complete("s", "u")
    assert [c["max_tokens"] for c in calls] == [32768, 16384]


def test_openai_client_switches_to_max_completion_tokens_when_told(monkeypatch):
    replies: list = []
    module, calls, _ = _fake_openai(monkeypatch, replies)
    replies += [
        module.BadRequestError(
            "Unsupported parameter: 'max_tokens' is not supported with this model. "
            "Use 'max_completion_tokens' instead."
        ),
        ("{}", "stop", _usage()),
    ]
    OpenAIClient("o4-mini", api_key="k").complete("s", "u", max_tokens=4096)
    assert "max_tokens" in calls[0] and calls[1].get("max_completion_tokens") == 4096
    assert "max_tokens" not in calls[1]


def test_unrelated_bad_request_is_not_retried(monkeypatch):
    replies: list = []
    module, calls, _ = _fake_openai(monkeypatch, replies)
    replies.append(module.BadRequestError("invalid model id 12345"))
    with pytest.raises(module.BadRequestError, match="invalid model"):
        OpenAIClient("nope", api_key="k").complete("s", "u")
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("message", "cap", "expected"),
    [
        ("max_tokens: 32768 > 8192, which is the maximum allowed number of output tokens "
         "for claude-3-5-haiku-20241022", 32768, 8192),
        ("Invalid max_tokens value, the valid range of max_tokens is [1, 8192]", 32768, 8192),
        ("`max_tokens` must be less than or equal to `32,768`", 65536, 32768),
        ("max_tokens is too large", 32768, None),  # no limit named: do not guess
        ("model not found: llama-3-70b", 32768, None),  # not about the cap
        # vLLM with an 8k model: the window minus the prompt, minus a small margin
        ("This model's maximum context length is 8192 tokens. However, you requested 34068 "
         "tokens (1300 in the messages, 32768 in the completion).", 32768, 6828),
        ("'max_tokens' or 'max_completion_tokens' is too large: 32768. This model's maximum "
         "context length is 8192 tokens and your request has 1300 input tokens", 32768, 6828),
    ],
)
def test_lower_cap_reads_the_providers_limit(message, cap, expected):
    assert _lower_cap(message, cap) == expected


def test_lower_cap_estimates_the_prompt_when_the_message_does_not_say():
    assert _lower_cap("maximum context length is 4096 tokens", 32768, prompt_tokens=1500) == 2532
    assert _lower_cap("maximum context length is 2048 tokens", 32768, prompt_tokens=1500) is None


def test_anthropic_adapter_sets_an_explicit_timeout(monkeypatch):
    # Without one the SDK refuses a large non-streaming max_tokens ("Streaming is required").
    seen: dict = {}
    module = types.ModuleType("anthropic")

    class BadRequestError(Exception):
        pass

    class Anthropic:
        def __init__(self, **kwargs):
            seen.update(kwargs)
            self.messages = types.SimpleNamespace(create=self._create)

        def _create(self, **kwargs):
            seen["max_tokens"] = kwargs["max_tokens"]
            return types.SimpleNamespace(
                content=[types.SimpleNamespace(type="text", text="{}")],
                usage=types.SimpleNamespace(input_tokens=10, output_tokens=5),
                stop_reason="max_tokens",
            )

    module.Anthropic = Anthropic
    module.BadRequestError = BadRequestError
    module.Timeout = lambda timeout, connect=None: ("timeout", timeout, connect)
    monkeypatch.setitem(sys.modules, "anthropic", module)
    resp = AnthropicClient("claude-sonnet-4-6", api_key="k").complete("s", "u")
    assert seen["timeout"][1] == LLM_HTTP_TIMEOUT and seen["max_retries"] == LLM_MAX_RETRIES
    assert seen["max_tokens"] == DEFAULT_MAX_OUTPUT_TOKENS
    assert resp.finish_reason == "length"  # Anthropic's max_tokens stop, normalised


# --------------------------------------------------------------------- planner behaviour
class Scripted:
    model = "scripted/model"

    def __init__(self, response: LLMResponse):
        self.response = response
        self.max_tokens: list[int] = []

    def complete(self, system, user, *, max_tokens=DEFAULT_MAX_OUTPUT_TOKENS):
        self.max_tokens.append(max_tokens)
        return self.response


def _df():
    return pd.DataFrame({"Email": ["A@X.COM ", "b@y.com"], "Name": ["al", "bo"]})


def test_reasoning_that_eats_the_cap_is_named_not_reported_as_bad_json():
    client = Scripted(LLMResponse("", 1300, 2048, finish_reason="length", reasoning_tokens=2048))
    with pytest.raises(LLMError, match="whole 32768-token output cap .2048 of them reasoning"):
        cf.clean(_df(), llm=client, llm_fallback=False)
    with pytest.warns(cf.CleanFrameWarning, match="output cap"):
        result = cf.clean(_df(), llm=client)
    assert "output cap" in result.recipe.meta["llm_fallback"]


def test_an_answer_cut_off_mid_json_says_so():
    client = Scripted(LLMResponse('{"version": 1, "columns": {"Email": ', 100, 50, finish_reason="length"))
    with pytest.raises(LLMError, match="cut off at the .*output cap"):
        cf.clean(_df(), llm=client, llm_fallback=False)


def test_a_think_block_before_the_json_is_skipped():
    text = '<think>Maybe {"version": 99}? No.</think>\n' + json.dumps(RECIPE)
    assert parse_recipe_json(text).column("Email").rename_to == "email"
    unclosed = "<think>I will write {broken"  # cut off while thinking: no answer at all
    with pytest.raises(LLMError, match="no JSON object"):
        parse_recipe_json(unclosed)


def test_budget_clamps_the_cap_instead_of_refusing_a_sensible_budget():
    client = Scripted(LLMResponse(json.dumps(RECIPE), 300, 100))
    result = cf.clean(_df(), llm=client, max_tokens_budget=10_000, llm_fallback=False)
    assert result.recipe.meta.get("generated_by") == "llm:scripted/model"
    assert 1024 <= client.max_tokens[0] < 10_000  # what the prompt left of the budget
    df = _df()
    profile = profile_dataframe(df)
    with pytest.raises(BudgetExceeded, match="prompt alone"):
        LLMPlanner(client, max_tokens_budget=500, fallback=None).plan(
            df, profile, run_detectors(df, profile=profile)
        )
    assert len(client.max_tokens) == 1  # refused before any request


def test_validation_on_a_renamed_columns_source_name_still_runs():
    recipe = {
        "version": 1,
        "columns": {"Email": {"rename_to": "email", "ops": ["normalize_email"]}},
        "validate": [{"column": "Email", "check": "valid_email", "on_fail": "quarantine"}],
        "dedup": {"subset": ["Email"], "keep": "first"},
    }
    df = pd.DataFrame({"Email": ["a@x.com", "not-an-email", "a@x.com"]})
    result = cf.clean(df, llm=Scripted(LLMResponse(json.dumps(recipe), 10, 10)), llm_fallback=False)
    assert result.recipe.validations[0].column == "email"
    assert len(result.quarantine) == 1  # the check ran instead of being skipped
    assert any("validation valid_email: Email -> email" in c for c in result.recipe.meta["llm_renamed_refs"])


def test_prompt_lists_each_ops_parameters_and_hides_blocked_ops():
    system, _ = build_prompt({"columns": []})
    assert "parse_date[allowed, dayfirst, formats, output, yearfirst]" in system
    assert "normalize_phone[country_code, default_country_code, region]" in system
    assert "fill_na" not in system and "drop_columns" not in system
    assert "final name" in system


def test_missing_closing_brackets_are_completed_only_for_a_finished_reply():
    from cleanframe.llm import _extract_json_object

    # A real reply from a model in October 2026: four "{" and three "}".
    slip = '{"answers": {"merge_0": {"probabilities": {"yes": 0.97, "no": 0.03}}}'
    assert json.loads(_extract_json_object(slip, close_unbalanced=True))["answers"]["merge_0"]
    assert _extract_json_object('{"a": [1, 2', close_unbalanced=True) == '{"a": [1, 2]}'
    with pytest.raises(LLMError, match="no complete JSON"):
        _extract_json_object(slip)  # off by default
    with pytest.raises(LLMError, match="no complete JSON"):
        _extract_json_object('{"a": "unterminated', close_unbalanced=True)  # inside a string


def test_decisions_repair_a_finished_reply_but_never_a_cut_off_one():
    questions = {"merge_0": Question.noul("same?")}
    slip = '{"answers": {"merge_0": {"probabilities": {"yes": 0.97, "no": 0.03}}}'
    finished = LLMDecisionClient(Scripted(LLMResponse(slip, 10, 30, finish_reason="stop")))
    assert finished.decide("s", questions).answers["merge_0"].p_yes == pytest.approx(0.97)
    # Cut off by the cap, "0.97" may really have been "0.975" or "0.9" + more: never guess.
    cut = LLMDecisionClient(Scripted(LLMResponse(slip, 10, 30, finish_reason="length")))
    with pytest.raises(cf.CleanFrameError, match="output cap"):
        cut.decide("s", questions)
    unknown = LLMDecisionClient(Scripted(LLMResponse(slip, 10, 30)))  # custom client: no reason
    with pytest.raises(cf.CleanFrameError):
        unknown.decide("s", questions)


# --------------------------------------------------------------------- decisions
def test_llm_decisions_report_truncation_and_cost():
    questions = {"q": Question.noul("same?")}
    cut = LLMDecisionClient(Scripted(LLMResponse("", 10, 4096, finish_reason="length", reasoning_tokens=4096)))
    with pytest.raises(cf.CleanFrameError, match="output cap"):
        cut.decide("state", questions)
    ok = LLMDecisionClient(Scripted(LLMResponse(
        json.dumps({"answers": {"q": {"probabilities": {"yes": 0.2, "no": 0.8}}}}), 10, 5, cost=0.0004
    )))
    resp = ok.decide("state", questions)
    assert resp.cost == pytest.approx(0.0004) and resp.answers["q"].p_yes == pytest.approx(0.2)


def test_openrouter_decision_models_use_the_system_one_route(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    for spec in ("openrouter/liquid/d1-ultra", "openrouter/liquid/d1-ultra-20261015",
                 "openrouter/typesafe/jev-1.14", "openrouter/~typesafe/jev-latest"):
        client = get_decision_client(spec)
        assert isinstance(client, SystemOneClient), spec
        assert client.base_url == "https://openrouter.ai/api" and client.calibrated
    # Liquid's LFM models are chat models, not decision models.
    assert isinstance(get_decision_client("openrouter/liquid/lfm-2.5-24b-a2b"), LLMDecisionClient)
    assert isinstance(get_decision_client("openrouter/qwen/qwen3.8-flash"), LLMDecisionClient)
