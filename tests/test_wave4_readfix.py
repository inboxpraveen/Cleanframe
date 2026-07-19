"""Wave 4 — read-time format auto-correction (encoding + delimiter), reviewable + replayable."""
from __future__ import annotations

import pytest

import cleanframe as cf
from cleanframe.errors import CleanFrameError
from cleanframe.readfix import detect_csv_options


def _semicolon_csv(tmp_path):
    p = tmp_path / "semi.csv"
    p.write_text("name;age;city\nAlice;30;NYC\nBob;25;LA\n", encoding="utf-8")
    return p


def test_detect_semicolon_delimiter(tmp_path):
    opts, report = detect_csv_options(_semicolon_csv(tmp_path))
    assert opts.get("sep") == ";"
    assert report.delimiter == ";"


def test_clean_autocorrects_semicolon_and_records_it(tmp_path):
    p = _semicolon_csv(tmp_path)
    with pytest.warns(UserWarning, match="delimiter"):
        result = cf.clean(p, mode="auto")
    # Parsed into 3 columns (not one 'name;age;city' column).
    assert result.dataframe.shape[1] == 3
    assert result.recipe.read.get("sep") == ";"
    # Replay re-reads with the recorded delimiter — no re-detection needed.
    applied = cf.apply_recipe(p, result.recipe, check_drift=False)
    assert applied.dataframe.shape[1] == 3


def test_clean_reads_cp1252_and_records_encoding(tmp_path):
    p = tmp_path / "latin.csv"
    p.write_bytes("name,city\nJos\xe9,M\xfcnchen\n".encode("cp1252"))
    result = cf.clean(p, mode="auto")
    assert result.recipe.read.get("encoding") == "cp1252"
    assert result.dataframe.shape == (1, 2)


def test_ambiguous_delimiter_refuses(tmp_path):
    # Every row splits into 2 fields by BOTH ';' and '|' -> ambiguous.
    p = tmp_path / "amb.csv"
    p.write_text("a;b|c\n1;2|3\n", encoding="utf-8")
    with pytest.raises(CleanFrameError, match="[Aa]mbiguous"):
        detect_csv_options(p)


def test_correct_format_opt_out(tmp_path):
    p = _semicolon_csv(tmp_path)
    result = cf.clean(p, mode="auto", correct_format=False)
    # Without correction, a semicolon file reads as a single column.
    assert result.dataframe.shape[1] == 1


def test_plain_utf8_comma_csv_is_untouched(tmp_path):
    p = tmp_path / "plain.csv"
    p.write_text("a,b\n1,2\n", encoding="utf-8")
    result = cf.clean(p, mode="auto")
    # No format correction needed -> no read: binding recorded.
    assert not (result.recipe.read or {}).get("sep")
    assert not (result.recipe.read or {}).get("encoding")
