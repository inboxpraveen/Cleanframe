"""Production-safety guards: CSV injection, diff caps, regex limits, sampling."""

from __future__ import annotations

import pandas as pd
import pytest

import cleanframe as cf
from cleanframe._util import (
    DEFAULT_MAX_DIFF_CHANGES,
    MAX_REGEX_PATTERN_LENGTH,
    safe_compile_regex,
    sample_non_null,
    sanitize_csv_value,
)
from cleanframe.diff import compute_diff
from cleanframe.ops import replace


def test_sanitize_csv_formula_cells(tmp_path):
    df = pd.DataFrame({"a": ["=1+1", "+cmd", "safe", None], "b": [1, 2, 3, 4]})
    out = tmp_path / "out.csv"
    cf.write_frame(df, out)
    text = out.read_text(encoding="utf-8")
    assert "'=1+1" in text
    assert "'+cmd" in text
    assert "safe" in text


def test_sanitize_csv_can_be_disabled(tmp_path):
    df = pd.DataFrame({"a": ["=1+1"]})
    out = tmp_path / "raw.csv"
    cf.write_frame(df, out, sanitize_csv=False)
    assert out.read_text(encoding="utf-8").splitlines()[1].startswith("=1+1")


def test_sanitize_csv_value_helper():
    assert sanitize_csv_value("=HYPERLINK()") == "'=HYPERLINK()"
    assert sanitize_csv_value("hello") == "hello"
    assert sanitize_csv_value(42) == 42


def test_diff_truncates_detail_but_keeps_count():
    n = 50
    original = pd.DataFrame({"x": list(range(n))})
    cleaned = pd.DataFrame({"x": list(range(1, n + 1))})
    diff = compute_diff(original, cleaned, {"x": "x"}, max_changes=10)
    assert diff.truncated
    assert len(diff.changes) == 10
    assert diff.changed_cells == n
    assert diff.summary()["stored_changes"] == 10


def test_default_max_diff_is_generous():
    assert DEFAULT_MAX_DIFF_CHANGES >= 100_000


def test_sample_non_null_caps():
    s = pd.Series(list(range(1000)) + [None, None])
    assert len(sample_non_null(s, cap=50)) == 50
    assert sample_non_null(s, cap=50) == list(range(50))


def test_safe_compile_rejects_long_pattern():
    with pytest.raises(ValueError, match="exceeds limit"):
        safe_compile_regex("a" * (MAX_REGEX_PATTERN_LENGTH + 1))


def test_safe_compile_rejects_nested_quantifiers():
    with pytest.raises(ValueError, match="ReDoS"):
        safe_compile_regex("(a+)+")


def test_replace_op_rejects_dangerous_regex():
    s = pd.Series(["aaa"])
    with pytest.raises(cf.OpError, match="ReDoS|exceeds|Invalid"):
        replace(s, pattern="(a+)+", repl="x")


def test_validation_rejects_dangerous_regex():
    from cleanframe.recipe import ValidationRule
    from cleanframe.validate import pass_mask

    rule = ValidationRule(column="x", check="matches: (a+)+")
    with pytest.raises(cf.RecipeError):
        pass_mask(rule, pd.Series(["aaa"]))
