"""Profiler semantic-type inference and the built-in detector suite."""

from __future__ import annotations

import pandas as pd

from cleanframe.detectors import DETECTOR_REGISTRY, detector, run_detectors, unregister_detector
from cleanframe.issues import Issues
from cleanframe.profile import profile_dataframe


def kinds(issues, column=None):
    return {i.kind for i in issues if column is None or i.column == column}


def test_semantic_types(messy_df):
    p = profile_dataframe(messy_df)
    types = {c.name: c.semantic_type for c in p.columns}
    assert types["Signup Date"] == "date"
    assert types["Amount"] == "currency"
    assert types["Email"] == "email"
    assert types["Phone"] == "phone"


def test_id_not_misclassified_as_number():
    df = pd.DataFrame({"ID": ["U001", "U002", "U003"]})
    p = profile_dataframe(df)
    assert p.column("ID").semantic_type in ("id", "text")


def test_detectors_fire_on_messy_data(messy_df):
    issues = run_detectors(messy_df, options={"phone_country_code": "+91"})
    assert "mixed_date_formats" in kinds(issues, "Signup Date")
    assert "currency_format" in kinds(issues, "Amount")
    assert "category_variants" in kinds(issues, "City")
    assert "whitespace" in kinds(issues, "Email")
    assert "invalid_emails" in kinds(issues, "Email")
    assert "disguised_nulls" in kinds(issues, "Phone")


def test_currency_folds_code_into_name(messy_df):
    issues = run_detectors(messy_df)
    amount = next(i for i in issues if i.kind == "currency_format")
    assert amount.proposal.rename_to == "amount_inr"
    assert [o.name for o in amount.proposal.ops] == ["parse_number"]


def test_category_canonical_prefers_title_case():
    df = pd.DataFrame({"City": ["Mumbai", "MUMBAI", "mumbai", "Delhi"]})
    issues = run_detectors(df)
    variants = next(i for i in issues if i.kind == "category_variants")
    assert set(variants.proposal.ops[0].params["map"].values()) == {"Mumbai"}


def test_schema_mapping_requires_schema(messy_df):
    # Without a schema, schema_mapping is skipped.
    issues = run_detectors(messy_df)
    assert "schema_mapping" not in kinds(issues)


def test_custom_detector_plugin():
    @detector("shouty")
    def _shouty(series) -> Issues:
        out = Issues()
        if any(isinstance(v, str) and v.isupper() for v in series.dropna()):
            out.add("shouty", "found shouting")
        return out

    try:
        assert "shouty" in DETECTOR_REGISTRY
        df = pd.DataFrame({"c": ["HELLO", "world"]})
        issues = run_detectors(df, only=["shouty"])
        assert "shouty" in {i.kind for i in issues}
    finally:
        unregister_detector("shouty")


def test_disguised_nulls_not_double_counted_after_pandas_na():
    # pandas would read "" as NaN; "unknown" survives and should be caught.
    df = pd.DataFrame({"c": ["unknown", "ok", "-", "value"]})
    issues = run_detectors(df)
    assert "disguised_nulls" in {i.kind for i in issues}


def test_units_detector_and_normalize():
    df = pd.DataFrame({"Weight": ["5kg", "5000 g", "5 KG", "2.5kg"]})
    p = profile_dataframe(df)
    assert p.column("Weight").semantic_type == "unit"
    issues = run_detectors(df)
    assert "mixed_units" in kinds(issues, "Weight")
    unit_issue = next(i for i in issues if i.kind == "mixed_units")
    assert unit_issue.proposal.ops[0].name == "normalize_unit"
    from cleanframe.ops import apply_column_op
    from cleanframe.types import Op

    out = apply_column_op(Op("normalize_unit", {"to": "g"}), df["Weight"]).series.tolist()
    assert out == [5000.0, 5000.0, 5000.0, 2500.0]


def test_outliers_flagged_never_auto_fixed():
    df = pd.DataFrame({"amount": [10, 11, 12, 10, 11, 12, 10, 11, 1000]})
    issues = run_detectors(df)
    out = next(i for i in issues if i.kind == "outliers")
    assert out.proposal is None or not out.proposal.ops
    assert out.evidence["count"] >= 1


def test_fuzzy_duplicates_reported_with_merge_proposal():
    df = pd.DataFrame(
        {
            "Customer Name": ["Alice Smith", "Alice Smth", "Bob Jones"],
            "Email": ["a@x.com", "b@x.com", "c@x.com"],
        }
    )
    issues = run_detectors(df)
    fuzzy = next(i for i in issues if i.kind == "fuzzy_duplicates")
    assert fuzzy.evidence["pairs"]
    assert "proposal" in fuzzy.evidence["pairs"][0]
    assert not fuzzy.has_fix or not fuzzy.proposal.ops  # never auto-merged

