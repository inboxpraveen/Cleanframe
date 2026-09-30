"""LLM trust fixes: no unguarded fill/drop, no cross-vendor keys, validated exposure,
fallback on an unrunnable plan, honest sample mode, and the budget warning."""
from __future__ import annotations

import json
import warnings

import pandas as pd
import pytest

import cleanframe as cf
from cleanframe.errors import CleanFrameError, CleanFrameWarning, LLMError
from cleanframe.llm import LLMPlanner, LLMResponse, get_client


class _Client:
    model = "fake/model"

    def __init__(self, recipe):
        self._text = recipe if isinstance(recipe, str) else json.dumps(recipe)

    def complete(self, system, user, *, max_tokens=2048):
        return LLMResponse(self._text, input_tokens=10, output_tokens=10)


def _df():
    return pd.DataFrame({"amt": [1.0, None, 3.0], "keep": ["a", "b", "c"]})


# --- F-07 ------------------------------------------------------------------
@pytest.mark.parametrize("mode", ["review", "auto", "strict"])
def test_llm_fill_na_is_never_executed(mode):
    plan = {"version": 1, "columns": {"amt": {"ops": [{"fill_na": {"strategy": "mean"}}]}}}
    with pytest.warns(CleanFrameWarning, match="may not contain"):
        result = cf.clean(_df(), planner=LLMPlanner(_Client(plan)), mode=mode)
    assert result.dataframe["amt"].isna().sum() == 1  # the null survived
    assert result.recipe.meta["llm_blocked_ops"] == ["fill_na on amt"]


def test_llm_fill_value_and_drop_all_columns_are_stripped():
    plan = {
        "version": 1,
        "columns": {"amt": {"ops": [{"fill_na": {"value": 0}}]}},
        "frame_ops": [{"drop_columns": ["amt", "keep"]}],
    }
    with pytest.warns(CleanFrameWarning):
        result = cf.clean(_df(), planner=LLMPlanner(_Client(plan)))
    assert list(result.dataframe.columns) == ["amt", "keep"]
    assert result.dataframe["amt"].isna().sum() == 1
    assert "drop_columns" in result.recipe.meta["llm_blocked_ops"]


def test_llm_validation_that_drops_or_nulls_becomes_quarantine():
    plan = {
        "version": 1,
        "columns": {"keep": {"ops": ["strip_whitespace"]}},
        "validate": [{"column": "keep", "check": "not_null", "on_fail": "drop"}],
    }
    with pytest.warns(CleanFrameWarning):
        result = cf.clean(_df(), planner=LLMPlanner(_Client(plan)))
    assert [r.on_fail for r in result.recipe.validations] == ["quarantine"]


# --- A7 --------------------------------------------------------------------
_VENDOR_KEYS = {
    "groq/m": "GROQ_API_KEY",
    "openrouter/m": "OPENROUTER_API_KEY",
    "together/m": "TOGETHER_API_KEY",
    "deepseek/m": "DEEPSEEK_API_KEY",
    "google/m": "GOOGLE_API_KEY",
}


@pytest.mark.parametrize("spec,env", list(_VENDOR_KEYS.items()))
def test_openai_key_is_never_sent_to_another_vendor(monkeypatch, spec, env):
    for var in [*_VENDOR_KEYS.values(), "GEMINI_API_KEY"]:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-secret")
    client = get_client(spec)
    assert client._api_key is None
    with pytest.raises(LLMError, match=env):
        client.complete("s", "u")


def test_openai_family_still_uses_openai_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-secret")
    assert get_client("openai/gpt-4o")._api_key == "sk-openai-secret"
    assert get_client("openai-compatible/x")._api_key == "sk-openai-secret"


def test_local_providers_need_no_key_and_ignore_openai_key(monkeypatch):
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-secret")
    assert get_client("ollama/llama3.2")._api_key == "ollama"
    assert get_client("lmstudio/x")._api_key == "lmstudio"


# --- F-16 ------------------------------------------------------------------
@pytest.mark.parametrize("bad", ["smaple", "", "NOPE"])
@pytest.mark.parametrize("llm", [None, _Client({"version": 1, "columns": {}})])
def test_bad_llm_exposure_is_a_clean_error_even_without_llm(bad, llm):
    with pytest.raises(CleanFrameError, match="llm_exposure"):
        cf.clean(_df(), llm=llm, llm_exposure=bad)


def test_llm_exposure_is_case_insensitive():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        cf.clean(_df(), llm=_Client({"version": 1, "columns": {}}), llm_exposure="Metadata")
        cf.clean(_df(), llm=_Client({"version": 1, "columns": {}}), llm_exposure="NONE")


# --- F-18 ------------------------------------------------------------------
_COLLIDING = {
    "version": 1,
    "columns": {"amt": {"rename_to": "x"}, "keep": {"rename_to": "x"}},
}


def test_unrunnable_llm_recipe_falls_back_to_rules():
    with pytest.warns(CleanFrameWarning, match="falling back"):
        result = cf.clean(_df(), llm=_Client(_COLLIDING))
    assert "cannot run" in result.recipe.meta["llm_fallback"]


def test_unrunnable_llm_recipe_raises_without_fallback():
    with pytest.raises(LLMError, match="cannot run"):
        cf.clean(_df(), llm=_Client(_COLLIDING), llm_fallback=False)


# --- sample honesty / budget ----------------------------------------------
def test_sample_exposure_warns_which_columns_send_values():
    with pytest.warns(CleanFrameWarning, match=r"sends example cell values.*keep"):
        cf.clean(_df(), llm=_Client({"version": 1, "columns": {}}), llm_exposure="sample")


def test_exposure_docs_do_not_claim_anonymised_or_approved():
    from cleanframe.types import LLMExposure

    doc = " ".join(
        [LLMExposure.__doc__ or ""]
        + [str(getattr(LLMExposure, n, "")) for n in ("SAMPLE",)]
    ).lower()
    assert "anonymized" not in doc and "approved" not in doc


def test_budget_without_llm_warns():
    with pytest.warns(CleanFrameWarning, match="max_tokens_budget was ignored"):
        cf.clean(_df(), max_tokens_budget=1000)
