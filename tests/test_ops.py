"""Op registry: transform correctness, NaN handling, and compact<->coerce round-trips."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from cleanframe.errors import RecipeError
from cleanframe.ops import (
    OP_REGISTRY,
    apply_column_op,
    apply_frame_op,
    list_ops,
    normalize_op,
    op_to_compact,
)
from cleanframe.types import Op


def col(op, values):
    return apply_column_op(op, pd.Series(values)).series.tolist()


def test_parse_number_handles_currency_and_grouping():
    out = col(Op("parse_number"), ["₹1,20,000", "$1,200", "1200 INR", "(500)", "", None, "abc"])
    assert out[0] == 120000.0
    assert out[1] == 1200.0
    assert out[2] == 1200.0
    assert out[3] == -500.0  # parentheses negative
    assert np.isnan(out[4]) and out[5] is None or np.isnan(out[4])
    assert np.isnan(out[6])


def test_parse_number_european_decimal():
    out = col(Op("parse_number", {"decimal": ",", "thousands": "."}), ["1.234,56", "9,50"])
    assert out == [1234.56, 9.5]


def test_parse_number_negative_forms():
    # trailing minus (ERP/bank exports) and unicode minus must be negative
    out = col(Op("parse_number"), ["1234.56-", "−5", "(-5)", "($5)", "(500)"])
    assert out == [-1234.56, -5.0, -5.0, -5.0, -500.0]


def test_parse_date_coalesces_multiple_formats():
    out = col(
        Op("parse_date", {"formats": ["%d/%m/%Y", "%Y-%m-%d", "%d %b %Y"]}),
        ["31/01/2024", "2024-01-31", "1 Jan 2024", "garbage", None],
    )
    assert out[:3] == ["2024-01-31", "2024-01-31", "2024-01-01"]
    assert out[3] is np.nan or pd.isna(out[3])
    assert pd.isna(out[4])


def test_strip_and_title_preserve_non_strings():
    out = col(Op("strip_whitespace"), ["  x ", None, 5])
    assert out[0] == "x" and out[2] == 5
    assert pd.isna(out[1])  # None/NaN — pandas 3 str dtype uses nan
    out = col(Op("title_case"), ["new  YORK", None])
    assert out[0] == "New York"
    assert pd.isna(out[1])


def test_to_na_default_tokens():
    out = col(Op("to_na"), ["NA", "n/a", "value", "-", "unknown"])
    assert out[2] == "value"
    assert all(pd.isna(x) for i, x in enumerate(out) if i != 2)


def test_normalize_values_exact_and_case_insensitive():
    out = col(Op("normalize_values", {"map": {"BLR": "Bangalore"}}), ["BLR", "Mumbai", None])
    assert out[0] == "Bangalore" and out[1] == "Mumbai"
    assert pd.isna(out[2])  # None/NaN — pandas 3 str dtype uses nan
    out = col(
        Op("normalize_values", {"map": {"bangalore": "Bangalore"}, "case_insensitive": True}),
        ["BANGALORE", "bangalore"],
    )
    assert out == ["Bangalore", "Bangalore"]


def test_cast_int_and_bool_use_nullable():
    ints = col(Op("cast", {"to": "int"}), ["1", "2", None, "3.7"])
    assert ints[0] == 1 and ints[3] == 4 and pd.isna(ints[2])
    bools = col(Op("cast", {"to": "bool"}), ["yes", "No", "1", None, "maybe"])
    assert bools[0] is True and bools[1] is False and pd.isna(bools[4])


def test_extract_currency_emits_column_without_touching_source():
    result = apply_column_op(Op("extract_currency", {"to": "amt_currency"}), pd.Series(["₹5", "$3"]))
    assert result.series.tolist() == ["₹5", "$3"]  # source unchanged
    assert result.emit["amt_currency"].tolist() == ["INR", "USD"]


def test_dedup_ignore_case_frame_op():
    df = pd.DataFrame({"email": ["a@x.com", "A@X.com", "b@x.com"], "v": [1, 2, 3]})
    out = apply_frame_op(Op("dedup", {"subset": ["email"], "ignore_case": True}), df)
    assert out.index.tolist() == [0, 2]


def test_normalize_phone_country_code():
    out = col(Op("normalize_phone", {"default_country_code": "+91"}), ["080 22334455", "+91 99", "12-34-567"])
    # leading national-trunk 0 is dropped before prepending the country code
    assert out[0] == "+918022334455"
    assert out[1] == "+9199"


def test_normalize_unit_converts_mass_family():
    out = col(Op("normalize_unit", {"to": "g"}), ["5kg", "5000 g", "5 KG", "2 lb", "nope"])
    assert out[0] == 5000.0
    assert out[1] == 5000.0
    assert out[2] == 5000.0
    assert abs(out[3] - 907.18474) < 0.01
    assert np.isnan(out[4])


# Representative raw compact inputs per op (None = the op takes no params).
_RAW_SAMPLES = {
    "remove_symbols": ["₹", ","],
    "cast": "float",
    "parse_date": {"formats": ["%d/%m/%Y"], "dayfirst": True},
    "parse_number": {"symbols": ["INR"]},
    "normalize_values": {"a": "b"},
    "dedup": {"subset": ["k"]},
    "round": 2,
    "replace": {"pattern": "x", "repl": "y"},
    "fill_na": {"value": 0},
    "drop_columns": ["a", "b"],
    "extract_currency": "amt_currency",
    "normalize_phone": {"default_country_code": "+91"},
    "normalize_unit": "g",
}


@pytest.mark.parametrize("name", list_ops())
def test_compact_coerce_round_trip(name):
    """normalize -> compact -> normalize is stable (defaults survive minimal serialisation)."""
    canonical = normalize_op(name, _RAW_SAMPLES.get(name))
    compact = op_to_compact(canonical)
    if isinstance(compact, str):
        rebuilt = normalize_op(compact, None)
    else:
        (n, raw), = compact.items()
        rebuilt = normalize_op(n, raw)
    assert rebuilt.name == canonical.name
    assert rebuilt.params == canonical.params


def test_unknown_op_raises():
    with pytest.raises(RecipeError):
        normalize_op("does_not_exist", {})


def test_all_ops_have_scope():
    for spec in OP_REGISTRY.values():
        assert spec.scope in ("column", "frame")
