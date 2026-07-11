"""Planner: op ordering, mode gating, rename resolution, validation synthesis."""

from __future__ import annotations

import pandas as pd

from cleanframe.planner import OP_ORDER, plan_recipe


def op_names(recipe, source):
    c = recipe.column(source)
    return [o.name for o in c.ops] if c else []


def test_canonical_op_order(messy_df):
    recipe = plan_recipe(messy_df, mode="review")
    ops = op_names(recipe, "City")
    # whitespace must precede normalize_values must precede title_case
    idx = {name: OP_ORDER.index(name) for name in ops}
    assert ops == sorted(ops, key=lambda n: idx[n])
    assert "strip_whitespace" in ops and "normalize_values" in ops


def test_strict_mode_omits_low_confidence(messy_df):
    strict = plan_recipe(messy_df, mode="strict")
    review = plan_recipe(messy_df, mode="review")
    # title_case (conf 0.55) is present in review, absent in strict
    assert "title_case" in op_names(review, "Customer Name")
    assert "title_case" not in op_names(strict, "Customer Name")


def test_default_snake_case_renames(messy_df):
    recipe = plan_recipe(messy_df, mode="auto")
    assert recipe.column("Customer Name").rename_to == "customer_name"


def test_rename_can_be_disabled(messy_df):
    recipe = plan_recipe(messy_df, mode="auto", options={"rename_columns": False})
    # currency still renames (fold in code), but generic columns keep their name
    assert recipe.column("City").rename_to is None


def test_validations_from_semantic_types(messy_df):
    recipe = plan_recipe(messy_df, mode="review")
    checks = {(v.column, v.check) for v in recipe.validations}
    assert ("email", "valid_email") in checks
    assert ("phone", "valid_phone") in checks


def test_no_rename_collision():
    df = pd.DataFrame({"A B": [1], "A_B": [2]})  # both snake_case to a_b
    recipe = plan_recipe(df, mode="auto")
    outputs = [c.output_name for c in recipe.columns] + [
        str(c) for c in df.columns if recipe.column(str(c)) is None
    ]
    assert len(outputs) == len(set(outputs)), "output names must be unique"


def test_rename_does_not_collide_with_existing_column():
    # Regression: 'First Name' snake_cases to 'first_name', which already exists.
    # The planner must NOT emit a colliding rename (executor would raise).
    from cleanframe.executor import execute

    df = pd.DataFrame({"First Name": ["Alice", "Bob"], "first_name": ["a", "b"], "Age": [1, 2]})
    recipe = plan_recipe(df, mode="auto")
    out = execute(recipe, df, mode="auto").dataframe  # must not raise
    assert len(set(out.columns)) == len(out.columns)


def test_category_map_order_is_row_order_independent():
    d = pd.DataFrame({"City": ["Mumbai", "MUMBAI", "mumbai", "Delhi", "delhi"] * 3})

    def city_map(frame):
        op = next(o for o in plan_recipe(frame, options={"rename_columns": False}).column("City").ops
                  if o.name == "normalize_values")
        return list(op.params["map"].items())

    assert city_map(d) == city_map(d.sample(frac=1, random_state=3).reset_index(drop=True))


def test_schema_drives_renames_and_validations(clean_df):
    from cleanframe.schema import Schema, SchemaColumn

    schema = Schema(columns=[SchemaColumn(name="full_name", dtype="string", aliases=["name"], required=True)])
    recipe = plan_recipe(clean_df, mode="review", schema=schema)
    # 'name' maps to schema column 'full_name'
    assert recipe.column("name").rename_to == "full_name"
    assert ("full_name", "not_null") in {(v.column, v.check) for v in recipe.validations}
