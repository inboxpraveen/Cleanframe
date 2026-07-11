"""Cross-platform path, encoding, and newline behaviour."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

import cleanframe as cf
from cleanframe._util import ensure_parent, read_text, write_text


def test_write_text_uses_lf_only(tmp_path):
    path = tmp_path / "nested" / "dir" / "note.txt"
    write_text(path, "line1\nline2\n")
    raw = path.read_bytes()
    assert b"\r\n" not in raw
    assert raw == b"line1\nline2\n"
    assert path.exists()


def test_read_text_accepts_utf8_bom(tmp_path):
    path = tmp_path / "bom.yaml"
    path.write_bytes("\ufeffversion: 1\ncolumns: {}\n".encode())
    text = read_text(path)
    assert not text.startswith("\ufeff")
    assert "version: 1" in text


def test_recipe_save_roundtrip_in_nested_dir(tmp_path, messy_df):
    recipe = cf.clean(messy_df, mode="auto").recipe
    path = tmp_path / "a" / "b" / "customer.recipe.yaml"
    recipe.save(path)
    loaded = cf.Recipe.load(path)
    assert loaded.to_yaml() == recipe.to_yaml()
    # On-disk bytes stay LF even on Windows.
    assert b"\r\n" not in path.read_bytes()


def test_csv_roundtrip_utf8_and_lf(tmp_path):
    df = pd.DataFrame({"name": ["Alice", "José", "₹100"], "n": [1, 2, 3]})
    path = tmp_path / "sub" / "data.csv"
    cf.write_frame(df, path)
    raw = path.read_bytes()
    assert b"\r\n" not in raw
    assert "José".encode() in raw
    back = cf.read_frame(path)
    assert list(back["name"]) == ["Alice", "José", "₹100"]


def test_csv_read_strips_bom(tmp_path):
    path = tmp_path / "excelish.csv"
    path.write_bytes("\ufeffa,b\n1,2\n".encode())
    df = cf.read_frame(path)
    assert list(df.columns) == ["a", "b"]
    assert int(df.iloc[0]["a"]) == 1


def test_ensure_parent_noop_for_bare_filename(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    p = ensure_parent("local.txt")
    assert p == Path("local.txt")
    # Should not raise even though parent is "."
    write_text(p, "ok\n")
    assert Path("local.txt").read_text(encoding="utf-8") == "ok\n"


def test_save_all_creates_parents(tmp_path, messy_df):
    result = cf.clean(messy_df, mode="auto")
    prefix = tmp_path / "artifacts" / "run" / "customer"
    paths = result.save_all(prefix)
    for p in paths.values():
        assert p.exists()


@pytest.mark.parametrize("sep", ["/", "\\"] if __import__("os").name == "nt" else ["/"])
def test_path_separators_accepted(tmp_path, messy_df, sep):
    """Both slash styles work on Windows; POSIX uses forward slash."""
    if sep == "\\" and __import__("os").name != "nt":
        pytest.skip("backslash paths are Windows-specific")
    sub = tmp_path / "mix"
    sub.mkdir()
    src = sub / "m.csv"
    messy_df.to_csv(src, index=False)
    # Build a path string with the chosen separator
    parts = [str(tmp_path), "mix", "m.csv"]
    mixed = sep.join(parts) if sep == "\\" else str(src)
    df = cf.read_frame(mixed)
    assert len(df) == len(messy_df)
