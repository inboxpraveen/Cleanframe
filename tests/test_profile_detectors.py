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


def test_detectors_work_on_pandas_string_dtype():
    """Pandas 3 defaults text to dtype='str'/'string' — detectors must not require object."""
    df = pd.DataFrame(
        {
            "City": pd.Series(
                ["Bengaluru", "bengaluru ", "Mumbai", "MUMBAI"], dtype="string"
            ),
            "note": pd.Series(["unknown", "ok", "-", "value"], dtype="string"),
        }
    )
    issues = run_detectors(df)
    assert "category_variants" in kinds(issues, "City")
    assert "whitespace" in kinds(issues, "City")
    assert "disguised_nulls" in kinds(issues, "note")


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



def test_fuzzy_category_merge_needs_a_typo_not_just_similar_letters():
    # "Austria" scores 0.875 against "Australia" (above the 0.86 bar) and is rare
    # enough to fold, yet it is a different country two edits away. A typo is one
    # edit (two for long values), so only the real typos merge.
    import cleanframe as cf

    df = pd.DataFrame(
        {"country": ["Australia"] * 10 + ["Austria"] * 3 + ["Bangalore"] * 10 + ["Banglore"] * 2}
    )
    out = cf.clean(df).dataframe["country"]
    assert set(out) == {"Australia", "Austria", "Bangalore"}


def test_quality_score_counts_rows_a_format_fix_rewrites():
    # A column in three date formats, or money stored as text, used to add no penalty:
    # the scorer read their `unparsed: 0` (every value parses) as "no rows affected".
    from cleanframe.quality import _affected_fraction, quality_score

    df = pd.DataFrame(
        {
            "signup": ["31/01/2024", "2024-02-15", "1 Jan 2024", "15/03/2024"] * 3,
            "amount": ["₹1,200", "₹300", "₹45", "₹9,999"] * 3,
        }
    )
    profile = profile_dataframe(df)
    issues = run_detectors(df, profile=profile)
    by_kind = {i.kind: i for i in issues}
    assert by_kind["mixed_date_formats"].evidence["affected"] == 9  # the three ISO cells are fine
    assert by_kind["currency_format"].evidence["affected"] == 12
    assert _affected_fraction(by_kind["mixed_date_formats"].evidence, 12) == 0.75
    assert quality_score(profile, issues).score < 100


def test_signs_and_marks_next_to_letters_keep_categories_apart():
    # normalize_key drops "+", "-" and "#", so blood groups A-/A+ and C/C++/C# used to
    # cluster as case variants (confidence 0.9) and silently merge.
    import cleanframe as cf
    from cleanframe.detectors.categories import _is_negation_pair

    df = pd.DataFrame(
        {
            "blood": ["A+"] * 10 + ["A-"] * 2 + ["O+"] * 9 + ["O-"] * 1 + ["B+"] * 5,
            "lang": ["C"] * 6 + ["C++"] * 2 + ["C#"] * 4 + ["Python"] * 15,
            "city": ["New York"] * 10 + ["New-York"] * 2 + ["NEW YORK"] * 3 + ["Boston"] * 12,
        }
    )
    out = cf.clean(df).dataframe
    assert set(out["blood"]) == {"A+", "A-", "O+", "O-", "B+"}
    assert set(out["lang"]) == {"C", "C++", "C#", "Python"}
    assert set(out["city"]) == {"New York", "Boston"}  # a hyphen between letters is cosmetic
    assert _is_negation_pair("Very dissatisfied", "Very satisfied")
    assert not _is_negation_pair("Very satisfied", "Quite satisfied")


def test_similarity_of_non_latin_text_is_not_a_perfect_match():
    from cleanframe._util import similarity

    assert similarity("दिल्ली", "मुंबई") < 0.5
    assert similarity("名前", "名前") == 1.0
