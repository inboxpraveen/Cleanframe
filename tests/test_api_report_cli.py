"""High-level API, HTML report safety, and CLI smoke tests."""

from __future__ import annotations

import pandas as pd
import pytest

import cleanframe as cf
from cleanframe.cli import main


# -- API ---------------------------------------------------------------------
def test_clean_returns_full_result(messy_df):
    result = cf.clean(messy_df, mode="review")
    assert isinstance(result.dataframe, pd.DataFrame)
    assert result.recipe.columns
    assert result.quality.score >= 0
    assert result.code.to_string().startswith('"""')


def test_report_object_saves_html(messy_df, tmp_path):
    rep = cf.report(messy_df, source="x.csv")
    out = rep.save(tmp_path / "r.html")
    assert out.exists()
    assert "<!doctype html>" in out.read_text(encoding="utf-8")


def test_apply_recipe_replays(messy_df):
    recipe = cf.clean(messy_df, mode="auto").recipe
    result = cf.apply_recipe(messy_df, recipe, mode="auto")
    assert "amount_inr" in result.dataframe.columns


def test_apply_recipe_stops_on_drift_by_default(messy_df, drifted_df):
    recipe = cf.clean(messy_df, mode="auto").recipe
    with pytest.raises(cf.DriftError) as exc:
        cf.apply_recipe(drifted_df, recipe, mode="auto")
    assert "Schema drift detected" in str(exc.value)
    assert "suggest" in str(exc.value) and "--recipe" in str(exc.value)


def test_apply_recipe_strict_raises_on_drift(messy_df, drifted_df):
    recipe = cf.clean(messy_df, mode="auto").recipe
    with pytest.raises(cf.DriftError):
        cf.apply_recipe(drifted_df, recipe, mode="strict")


def test_suggest_update_patches_recipe(messy_df, drifted_df, tmp_path):
    recipe = cf.clean(messy_df, mode="auto").recipe
    patched, report = cf.suggest_update(drifted_df, recipe)
    assert report.has_drift
    # after patching, the drifted file should replay without producing all-NaN money
    result = cf.apply_recipe(drifted_df, patched, mode="auto", check_drift=False)
    assert result.dataframe["amount_inr"].notna().any()


def test_clean_with_fake_llm(messy_df):
    import json

    class FakeClient:
        model = "fake/m"

        def complete(self, system, user, *, max_tokens=2048):
            from cleanframe.llm import LLMResponse

            return LLMResponse(json.dumps({"version": 1, "columns": {"Amount": {"rename_to": "amt"}}}))

    result = cf.clean(messy_df, llm=FakeClient(), mode="review")
    assert result.recipe.meta["generated_by"].startswith("llm:")


# -- report safety -----------------------------------------------------------
def test_report_autoescapes_user_data():
    df = pd.DataFrame({"<script>alert(1)</script>": [1, 2], "ok": ["a", "b"]})
    html = cf.report(df).html
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


# -- CLI ---------------------------------------------------------------------
def _write_csv(df, path):
    df.to_csv(path, index=False, encoding="utf-8")
    return str(path)


def test_cli_report(messy_df, tmp_path, capsys):
    src = _write_csv(messy_df, tmp_path / "m.csv")
    rc = main(["report", src, "--out", str(tmp_path / "r.html")])
    assert rc == 0
    assert (tmp_path / "r.html").exists()


def test_cli_clean_and_apply(messy_df, tmp_path):
    src = _write_csv(messy_df, tmp_path / "m.csv")
    recipe = tmp_path / "r.recipe.yaml"
    rc = main(["clean", src, "--recipe", str(recipe), "--out", str(tmp_path / "clean.csv"), "--mode", "auto"])
    assert rc == 0 and recipe.exists()
    rc2 = main(["apply", src, "--recipe", str(recipe), "--out", str(tmp_path / "c2.csv"), "--mode", "auto"])
    assert rc2 == 0


def test_cli_apply_stops_on_drift(messy_df, drifted_df, tmp_path, capsys):
    src = _write_csv(messy_df, tmp_path / "m.csv")
    recipe = tmp_path / "r.recipe.yaml"
    main(["clean", src, "--recipe", str(recipe), "--mode", "auto"])
    drift_src = _write_csv(drifted_df, tmp_path / "m2.csv")
    out = tmp_path / "should_not_exist.csv"
    rc = main(["apply", drift_src, "--recipe", str(recipe), "--out", str(out)])
    assert rc == 1
    assert not out.exists()
    captured = capsys.readouterr().out
    assert "Schema drift detected" in captured
    assert "--update" in captured


def test_cli_apply_force_continues_on_drift(messy_df, drifted_df, tmp_path):
    src = _write_csv(messy_df, tmp_path / "m.csv")
    recipe = tmp_path / "r.recipe.yaml"
    main(["clean", src, "--recipe", str(recipe), "--mode", "auto"])
    drift_src = _write_csv(drifted_df, tmp_path / "m2.csv")
    out = tmp_path / "forced.csv"
    rc = main(["apply", drift_src, "--recipe", str(recipe), "--out", str(out), "--force"])
    assert rc == 0 and out.exists()


def test_cli_suggest_reports_drift(messy_df, drifted_df, tmp_path):
    src = _write_csv(messy_df, tmp_path / "m.csv")
    recipe = tmp_path / "r.recipe.yaml"
    main(["clean", src, "--recipe", str(recipe), "--mode", "auto"])
    drift_src = _write_csv(drifted_df, tmp_path / "m2.csv")
    rc = main(["suggest", drift_src, "--recipe", str(recipe)])
    assert rc == 1  # drift present, not updated


def test_cli_detectors_and_ops():
    assert main(["detectors"]) == 0
    assert main(["ops"]) == 0


def test_readme_recipe_executes_on_messy_data(messy_df):
    """The README recipe example must round-trip and clean the sample frame."""
    yaml_text = """
version: 1
columns:
  "Customer Name":
    rename_to: customer_name
    ops: [strip_whitespace, title_case]
  "Signup Date":
    rename_to: signup_date
    parse_date: {dayfirst: true, allowed: ["%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d %b %Y"]}
  "Amount":
    rename_to: amount_inr
    ops: [{remove_symbols: ["₹", ","]}, {cast: float}]
  "City":
    rename_to: city
    normalize_values: {Bengaluru: Bangalore, BLR: Bangalore, Bombay: Mumbai}
  "Email":
    rename_to: email
    ops: [strip_whitespace, normalize_email]
validate:
  - {column: email, check: valid_email, on_fail: quarantine}
  - {column: amount_inr, check: ">= 0", on_fail: quarantine}
"""
    recipe = cf.Recipe.from_yaml(yaml_text)
    result = cf.apply_recipe(messy_df, recipe, check_drift=False, mode="review")
    assert "amount_inr" in result.dataframe.columns
    assert "email" in result.dataframe.columns
    assert result.dataframe["amount_inr"].dtype.kind == "f"
    assert len(result.quarantine) >= 1  # not-an-email


def test_cli_infer_schema(clean_df, tmp_path):
    src = _write_csv(clean_df, tmp_path / "c.csv")
    rc = main(["infer-schema", src, "--out", str(tmp_path / "s.yaml")])
    assert rc == 0 and (tmp_path / "s.yaml").exists()
