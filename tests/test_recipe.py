"""Recipe format: lenient loading, canonical + idempotent serialisation, validation."""

from __future__ import annotations

import pytest

from cleanframe.errors import RecipeError
from cleanframe.recipe import Recipe

README_RECIPE = """
version: 1
columns:
  "Customer Name":
    rename_to: customer_name
    ops: [strip_whitespace, title_case]
  "Signup Date":
    rename_to: signup_date
    parse_date: {dayfirst: true, allowed: ["%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d"]}
  "Amount":
    rename_to: amount_inr
    ops: [{remove_symbols: ["₹", ","]}, {cast: float}]
  "City":
    rename_to: city
    normalize_values: {Bengaluru: Bangalore, BLR: Bangalore}
dedup: {subset: [email], keep: first}
validate:
  - {column: email, check: valid_email, on_fail: quarantine}
  - {column: amount_inr, check: ">= 0", on_fail: quarantine}
"""


def test_loads_readme_shorthand():
    r = Recipe.from_yaml(README_RECIPE)
    signup = r.column("Signup Date")
    assert [o.name for o in signup.ops] == ["parse_date"]
    # `allowed` is aliased to `formats`
    assert signup.ops[0].params["formats"] == ["%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d"]
    assert signup.ops[0].params["dayfirst"] is True
    assert r.column("Amount").rename_to == "amount_inr"
    assert r.dedup_op.params["subset"] == ["email"]
    assert len(r.validations) == 2


def test_serialisation_is_idempotent():
    r = Recipe.from_yaml(README_RECIPE)
    once = r.to_yaml()
    twice = Recipe.from_yaml(once).to_yaml()
    assert once == twice


def test_minimal_serialisation_omits_defaults():
    r = Recipe.from_yaml(README_RECIPE)
    yaml_text = r.to_yaml()
    # defaults like yearfirst/output should not appear
    assert "yearfirst" not in yaml_text
    assert "cast: float" in yaml_text  # compact form preserved


def test_params_survive_minimal_round_trip():
    r = Recipe.from_yaml(README_RECIPE)
    r2 = Recipe.from_yaml(r.to_yaml())
    assert r2.column("City").ops[0].params["map"] == {"Bengaluru": "Bangalore", "BLR": "Bangalore"}


def test_unknown_op_in_recipe_raises():
    with pytest.raises(RecipeError):
        Recipe.from_yaml("version: 1\ncolumns:\n  a: {ops: [made_up_op]}\n")


def test_unknown_top_level_key_raises():
    with pytest.raises(RecipeError):
        Recipe.from_yaml("version: 1\nnonsense: true\n")


def test_wrong_version_raises():
    with pytest.raises(RecipeError):
        Recipe.from_yaml("version: 99\ncolumns: {}\n")


def test_frame_op_in_column_position_raises():
    with pytest.raises(RecipeError):
        Recipe.from_yaml("version: 1\ncolumns:\n  a: {ops: [dedup]}\n")


def test_save_and_load_roundtrip(tmp_path):
    r = Recipe.from_yaml(README_RECIPE)
    path = tmp_path / "r.recipe.yaml"
    r.save(path)
    reloaded = Recipe.load(path)
    assert reloaded.to_yaml() == r.to_yaml()
