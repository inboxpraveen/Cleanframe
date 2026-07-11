"""LLM planner (fake client, privacy, budget) and schema inference/round-trip."""

from __future__ import annotations

import json

import pandas as pd
import pytest

from cleanframe.detectors import run_detectors
from cleanframe.issues import Issues
from cleanframe.llm import LLMPlanner, LLMResponse, build_metadata, value_sketches
from cleanframe.profile import profile_dataframe
from cleanframe.schema import Schema, infer_schema
from cleanframe.types import LLMExposure


class FakeClient:
    model = "fake/model-1"

    def __init__(self, recipe: dict):
        self._recipe = recipe
        self.calls = 0

    def complete(self, system, user, *, max_tokens=2048):
        self.calls += 1
        self.last_user = user
        return LLMResponse(json.dumps(self._recipe), input_tokens=120, output_tokens=60)


def _df():
    return pd.DataFrame({"Customer Name": ["  alice ", "BOB"], "Amount": ["₹1,20,000", "₹1,200"]})


def test_metadata_never_leaks_raw_values():
    df = _df()
    md = build_metadata(df, profile_dataframe(df), Issues(), None, LLMExposure.METADATA)
    blob = json.dumps(md, ensure_ascii=False)
    assert "alice" not in blob and "BOB" not in blob and "1,20,000" not in blob
    # but patterns are present
    assert md["columns"][1]["patterns"]


def test_sample_exposure_anonymizes_and_shuffles():
    df = pd.DataFrame(
        {
            "Email": ["alice@secret.com", "bob@secret.com", "carol@secret.com"],
            "Phone": ["+91 98765 43210", "+91 90000 11111", "+91 91111 22222"],
            "City": ["Bengaluru", "Mumbai", "Delhi"],
        }
    )
    md = build_metadata(df, profile_dataframe(df), Issues(), None, LLMExposure.SAMPLE)
    blob = json.dumps(md, ensure_ascii=False)
    assert "alice@secret.com" not in blob
    assert "98765" not in blob
    email_col = next(c for c in md["columns"] if c["name"] == "Email")
    assert email_col["example_values"]
    assert all("@" in v for v in email_col["example_values"])
    assert "example.com" in email_col["example_values"][0]
    # deterministic across calls
    md2 = build_metadata(df, profile_dataframe(df), Issues(), None, LLMExposure.SAMPLE)
    assert md["columns"][0]["example_values"] == md2["columns"][0]["example_values"]


def test_get_client_resolves_openai_compatible_providers(monkeypatch):
    from cleanframe.llm import OpenAIClient, get_client, list_providers

    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setenv("GROQ_API_KEY", "groq-key")
    monkeypatch.setenv("GOOGLE_API_KEY", "goog-key")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    or_client = get_client("openrouter/anthropic/claude-sonnet-4")
    assert isinstance(or_client, OpenAIClient)
    assert or_client.model == "anthropic/claude-sonnet-4"  # slash preserved
    assert or_client._base_url == "https://openrouter.ai/api/v1"
    assert or_client._api_key == "or-key"

    groq = get_client("groq/llama-3.3-70b-versatile")
    assert groq._base_url == "https://api.groq.com/openai/v1"
    assert groq._api_key == "groq-key"

    gemini = get_client("gemini/gemini-2.0-flash")  # alias for google
    assert "generativelanguage.googleapis.com" in (gemini._base_url or "")

    ollama = get_client("ollama/llama3.2")
    assert ollama._base_url == "http://localhost:11434/v1"
    assert ollama._api_key == "ollama"

    assert "openrouter" in list_providers()
    assert "groq" in list_providers()


def test_get_client_unknown_provider():
    from cleanframe.errors import LLMError
    from cleanframe.llm import get_client

    with pytest.raises(LLMError, match="Unknown LLM provider"):
        get_client("nope/some-model")


def test_value_sketch_structure():
    assert value_sketches(pd.Series(["₹1,200", "₹99,999"]))[0].startswith("₹")


def test_llm_recipe_parsed_via_same_model():
    df = _df()
    prof = profile_dataframe(df)
    issues = run_detectors(df, profile=prof)
    recipe = {
        "version": 1,
        "columns": {
            "Customer Name": {"rename_to": "customer_name", "ops": ["strip_whitespace"]},
            "Amount": {"rename_to": "amount_inr", "ops": [{"cast": "float"}]},
        },
    }
    planner = LLMPlanner(FakeClient(recipe))
    out = planner.plan(df, prof, issues, mode="review")
    assert out.meta["generated_by"] == "llm:fake/model-1"
    assert out.column("Customer Name").rename_to == "customer_name"


def test_llm_budget_falls_back_to_rules():
    df = _df()
    prof = profile_dataframe(df)
    issues = run_detectors(df, profile=prof)
    planner = LLMPlanner(FakeClient({"version": 1}), max_tokens_budget=5)  # impossibly small
    out = planner.plan(df, prof, issues, mode="review")
    assert out.meta["generated_by"] == "rules"
    assert "llm_fallback" in out.meta


def test_infer_schema_captures_constraints(clean_df):
    schema = infer_schema(clean_df, name="customer")
    age = schema.column("age")
    assert age.dtype == "integer" and age.required is True
    assert schema.column("signup").dtype == "date"


def test_schema_yaml_round_trip(clean_df, tmp_path):
    schema = infer_schema(clean_df)
    path = tmp_path / "s.yaml"
    schema.save(path)
    reloaded = Schema.load(path)
    assert reloaded.column_names == schema.column_names
    assert reloaded.column("age").dtype == "integer"


def test_inferred_schema_uses_canonical_names_with_aliases():
    df = pd.DataFrame({"Customer Name": ["a", "b"], "Amount (INR)": ["₹1", "₹2"]})
    schema = infer_schema(df)
    assert "customer_name" in schema.column_names  # snake_cased target name
    assert schema.column("customer_name").aliases == ["Customer Name"]


def test_inferred_schema_validations_apply_to_cleaned_output():
    # Regression: schema names must match the planner's snake_cased outputs, or
    # validations silently apply to nothing.
    df = pd.DataFrame(
        {"Email": ["a@x.com", "bob@y.org", "not-an-email", "d@z.co", "bad@"], "Amt": ["1", "2", "3", "4", "5"]}
    )
    result = cf_clean_with(df)
    assert any(v.check == "valid_email" for v in result.recipe.validations)
    assert len(result.quarantine) == 2  # the two invalid emails, and nothing else


def test_infer_schema_does_not_overmark_unique():
    # Regression: a tiny sample makes everything look unique; only key-like columns
    # (id/email) should be marked unique.
    df = pd.DataFrame({"amount": ["₹1,200", "₹1200", "₹5"], "email": ["a@x.com", "b@y.com", "c@z.com"]})
    schema = infer_schema(df)
    assert schema.column("amount").unique is False
    assert schema.column("email").unique is True


def cf_clean_with(df):
    import cleanframe as cf

    return cf.clean(df, target_schema=cf.infer_schema(df), mode="review")
