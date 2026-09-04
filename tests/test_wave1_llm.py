"""Wave 1 — Batch D: LLM planner robustness (from the live 4-model test)."""
from __future__ import annotations

import json

import pandas as pd
import pytest

import cleanframe as cf
from cleanframe.detectors import run_detectors
from cleanframe.errors import LLMError
from cleanframe.llm import LLMPlanner, LLMResponse, build_metadata, parse_recipe_json
from cleanframe.profile import profile_dataframe
from cleanframe.types import LLMExposure


class _Client:
    model = "fake/model"

    def __init__(self, text=None, exc=None):
        self._text = text
        self._exc = exc

    def complete(self, system, user, *, max_tokens=2048):
        if self._exc is not None:
            raise self._exc
        return LLMResponse(self._text, input_tokens=10, output_tokens=10)


def _df():
    return pd.DataFrame({"Amount": ["₹1,200", "₹2,500"], "Name": ["  a ", "b"]})


def _clean_with(client) -> cf.CleanResult:
    return cf.clean(_df(), planner=LLMPlanner(client))


# --- L1/L4: any LLM failure must fall back to rules, never crash the pipeline --
def test_unusable_llm_op_is_dropped_not_fatal():
    """One unloadable op (replace without a pattern) must not cost the whole plan.

    It is removed and recorded, so the rest of the model's recipe still runs and the
    omission is visible — the pipeline never sees an unvalidated op either way.
    """
    bad = json.dumps(
        {
            "version": 1,
            "columns": {
                "Amount": {"ops": [{"replace": {"repl": "x"}}]},
                "Email": {"ops": ["normalize_email"]},
            },
        }
    )
    result = _clean_with(_Client(text=bad))
    assert "llm_fallback" not in result.recipe.meta
    assert result.recipe.meta["generated_by"].startswith("llm:")
    assert any("replace" in note for note in result.recipe.meta["llm_dropped"])
    assert [op.name for op in result.recipe.column("Email").ops] == ["normalize_email"]


def test_structurally_broken_llm_recipe_falls_back():
    """A response that is not a recipe at all still degrades to the rules planner."""
    result = _clean_with(_Client(text=json.dumps({"version": 99, "columns": {}})))
    assert "llm_fallback" in result.recipe.meta


def test_llm_client_exception_falls_back():
    """A client-side/network exception must degrade to rules, not crash."""
    result = _clean_with(_Client(exc=RuntimeError("connection reset")))
    assert "llm_fallback" in result.recipe.meta


def test_llm_no_json_falls_back():
    result = _clean_with(_Client(text="Sure! Here is my reasoning, no JSON at all."))
    assert "llm_fallback" in result.recipe.meta


# --- L3: lenient parsing of array-form ops that small models emit -------------
def test_array_form_ops_are_parsed():
    """Ops emitted as [name] / [name, params] arrays must parse, not error out."""
    recipe = parse_recipe_json(
        json.dumps(
            {
                "version": 1,
                "columns": {"Amount": {"ops": [["remove_symbols", [",", "₹"]], ["parse_number"]]}},
            }
        )
    )
    col = recipe.column("Amount")
    assert [o.name for o in col.ops] == ["remove_symbols", "parse_number"]
    assert col.ops[0].params.get("symbols") == [",", "₹"]


# --- L4: parse_recipe_json wraps all failures as LLMError ---------------------
def test_parse_recipe_json_wraps_validation_error():
    for payload in (
        {"version": 99, "columns": {}},
        {"version": 1, "unknown_section": {}},
        ["not", "a", "mapping"],
    ):
        with pytest.raises(LLMError):
            parse_recipe_json(json.dumps(payload))


def test_parse_recipe_json_drops_an_unusable_op_and_records_it():
    recipe = parse_recipe_json(
        json.dumps({"version": 1, "columns": {"a": {"ops": ["lowercase", {"cast": {}}]}}})
    )
    assert [op.name for op in recipe.column("a").ops] == ["lowercase"]
    assert any("cast" in note for note in recipe.meta["llm_dropped"])


# --- H12: constant-column raw value must not leak into the LLM metadata --------
def test_constant_column_value_not_leaked_to_llm():
    secret = "TOP-SECRET-PROJECT-ORION"
    df = pd.DataFrame({"codename": [secret, secret, secret], "n": [1, 2, 3]})
    prof = profile_dataframe(df)
    issues = run_detectors(df, profile=prof)
    md = build_metadata(df, prof, issues, None, LLMExposure.METADATA)
    assert secret not in json.dumps(md, ensure_ascii=False)
