"""The agent tool layer (cleanframe.agent_tools): JSON in, JSON out, never destructive.

Runs without the optional ``mcp`` package - the MCP server is only a thin registration
of these functions, tested separately in test_mcp_server.py.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import cleanframe as cf
from cleanframe import agent_tools as at
from cleanframe.types import Mode

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


@pytest.fixture
def work(tmp_path: Path) -> dict[str, Path]:
    """The example file and recipe, copied so tests never touch the repo's copies."""
    csv = tmp_path / "customers.csv"
    recipe = tmp_path / "customer.recipe.yaml"
    shutil.copy(EXAMPLES / "messy_customers.csv", csv)
    shutil.copy(EXAMPLES / "customer.recipe.yaml", recipe)
    return {"dir": tmp_path, "csv": csv, "recipe": recipe}


@pytest.fixture
def drifted_csv(tmp_path: Path, drifted_df: pd.DataFrame) -> Path:
    path = tmp_path / "next_month.csv"
    drifted_df.to_csv(path, index=False, encoding="utf-8")
    return path


def _strict_json(result: dict) -> str:
    """allow_nan=False: NaN is not JSON and breaks strict MCP clients."""
    return json.dumps(result, allow_nan=False, ensure_ascii=False)


def _ok(result: dict) -> dict:
    _strict_json(result)
    assert result["ok"] is True, result
    assert isinstance(result["warnings"], list)
    return result


def _err(result: dict, kind: str) -> dict:
    _strict_json(result)
    assert result["ok"] is False, result
    assert result["kind"] == kind, result
    assert result["error"]
    return result


# ---------------------------------------------------------------------------
# profile_data
# ---------------------------------------------------------------------------
def test_profile_data_reports_issues_and_matches_cli_report(work):
    res = _ok(at.profile_data(str(work["csv"])))
    assert res["rows"] == 6 and res["columns"] == 6
    assert res["source"] == str(work["csv"].resolve())
    # Same read path, detectors and score as `cleanframe report`.
    assert res["quality"]["score"] == cf.report(str(work["csv"])).quality.score
    kinds = {i["kind"] for i in res["issues"]}
    assert {"invalid_emails", "mixed_date_formats", "currency_format"} <= kinds
    # Most severe first.
    ranks = {"error": 2, "warning": 1, "info": 0}
    severities = [ranks[i["severity"]] for i in res["issues"]]
    assert severities == sorted(severities, reverse=True)
    invalid = next(i for i in res["issues"] if i["kind"] == "invalid_emails")
    assert invalid["auto_fix"] is False and invalid["fix"] is None
    assert invalid["examples"] == ["not-an-email"]
    dates = next(i for i in res["issues"] if i["kind"] == "mixed_date_formats")
    assert dates["auto_fix"] is True and dates["fix"]["ops"] == ["parse_date"]
    assert dates["planned_in_modes"] == ["review", "auto", "strict"]
    types = {c["name"]: c["semantic_type"] for c in res["column_profiles"]}
    assert types["Email"] == "email" and types["Amount"] == "currency"
    assert sum(res["issues_by_severity"].values()) == res["issue_count"]


def test_profile_data_low_confidence_fix_names_the_modes_that_plan_it(work):
    res = _ok(at.profile_data(str(work["csv"])))
    case = next(i for i in res["issues"] if i["kind"] == "inconsistent_case")
    assert case["auto_fix"] is True
    assert case["planned_in_modes"] == ["review"]  # 0.55 clears review only


def test_profile_data_nrows_and_bad_arguments(work):
    res = _ok(at.profile_data(str(work["csv"]), nrows=2))
    assert res["rows"] == 2
    _err(at.profile_data(str(work["csv"]), nrows=0), "InvalidArgument")
    _err(at.profile_data(str(work["csv"]), header_row=-1), "InvalidArgument")
    _err(at.profile_data(str(work["csv"]), sheet=True), "InvalidArgument")
    _err(at.profile_data(""), "InvalidArgument")
    _err(at.profile_data(None), "InvalidArgument")  # type: ignore[arg-type]


def test_missing_input_is_a_structured_error(work):
    res = _err(at.profile_data(str(work["dir"] / "nope.csv")), "CleanFrameError")
    assert "not found" in res["error"]


def test_relative_paths_resolve_against_cwd(work, monkeypatch):
    monkeypatch.chdir(work["dir"])
    res = _ok(at.profile_data("customers.csv"))
    assert res["source"] == str(work["csv"].resolve())


def test_unexpected_exceptions_propagate(work, monkeypatch):
    """Only deliberate CleanFrame errors are structured; a bug must stay a bug."""
    import cleanframe.profile

    def boom(df):
        raise RuntimeError("bug")

    monkeypatch.setattr(cleanframe.profile, "profile_dataframe", boom)
    with pytest.raises(RuntimeError, match="bug"):
        at.profile_data(str(work["csv"]))


# ---------------------------------------------------------------------------
# suggest_recipe
# ---------------------------------------------------------------------------
def test_suggest_recipe_saves_reviewable_recipe(work):
    out = work["dir"] / "planned.recipe.yaml"
    res = _ok(at.suggest_recipe(str(work["csv"]), str(out)))
    assert out.exists() and res["recipe_path"] == str(out.resolve())
    text = out.read_text(encoding="utf-8")
    assert res["recipe_yaml"] == text and res["recipe_yaml_truncated"] is False
    recipe = cf.Recipe.from_yaml(text)
    assert recipe.source_fingerprint and recipe.meta["generated_by"] == "rules"
    assert res["summary"]["rows_before"] == 6 and res["summary"]["rows_after"] == 5
    assert res["quarantine"]["count"] == 1
    assert res["quarantine"]["rows"] == [{"row_id": 2, "reason": "email:valid_email"}]
    assert res["notes"] and any("invalid_emails" in n for n in res["notes"])
    assert any(r["kind"] == "invalid_emails" for r in res["needs_review"])
    assert sum(res["changes_by_column"].values()) == res["summary"]["changed_cells"]
    assert res["changes_by_column_exact"] is True
    # The preview is column-balanced: every changed column is represented.
    assert {p["column"] for p in res["preview"]} == set(res["changes_by_column"])
    assert len(res["preview"]) <= at.MAX_PREVIEW_CHANGES
    # The data file is untouched.
    assert work["csv"].read_bytes() == (EXAMPLES / "messy_customers.csv").read_bytes()


def test_suggest_recipe_is_deterministic_and_idempotent(work):
    out = work["dir"] / "planned.recipe.yaml"
    first = _ok(at.suggest_recipe(str(work["csv"]), str(out)))
    # Re-planning to identical bytes is allowed without overwrite (like `cleanframe clean`).
    second = _ok(at.suggest_recipe(str(work["csv"]), str(out)))
    assert first == second


def test_suggest_recipe_refuses_to_clobber_a_different_recipe(work):
    out = work["recipe"]  # an existing, different (hand-written) recipe
    before = out.read_bytes()
    res = _err(at.suggest_recipe(str(work["csv"]), str(out)), "OutputError")
    assert "overwrite" in res["error"]
    assert out.read_bytes() == before
    _ok(at.suggest_recipe(str(work["csv"]), str(out), overwrite=True))
    assert out.read_bytes() != before


def test_suggest_recipe_output_must_be_yaml_and_not_an_input(work):
    _err(at.suggest_recipe(str(work["csv"]), str(work["dir"] / "x.csv")), "InvalidArgument")
    schema = work["dir"] / "s.schema.yaml"
    shutil.copy(EXAMPLES / "customer.schema.yaml", schema)
    before = schema.read_bytes()
    _err(
        at.suggest_recipe(str(work["csv"]), str(schema), schema=str(schema), overwrite=True),
        "OutputError",
    )
    assert schema.read_bytes() == before


def test_suggest_recipe_with_schema_and_options(work):
    out = work["dir"] / "s.recipe.yaml"
    res = _ok(
        at.suggest_recipe(
            str(work["csv"]), str(out), schema=str(EXAMPLES / "customer.schema.yaml"),
            mode="auto", options={"category_map": {"City": {"Bengaluru": "Bangalore"}}},
        )
    )
    assert res["mode"] == "auto"
    assert "Bangalore" in res["recipe_yaml"]


def test_suggest_recipe_bad_mode_and_options(work):
    out = work["dir"] / "x.recipe.yaml"
    res = _err(at.suggest_recipe(str(work["csv"]), str(out), mode="yolo"), "CleanFrameError")
    assert "mode" in res["error"].lower()
    _err(at.suggest_recipe(str(work["csv"]), str(out), options=["x"]), "InvalidArgument")  # type: ignore[arg-type]
    assert not out.exists()


# ---------------------------------------------------------------------------
# apply_recipe
# ---------------------------------------------------------------------------
def test_apply_recipe_dry_run_writes_nothing(work):
    before = sorted(p.name for p in work["dir"].iterdir())
    res = _ok(at.apply_recipe(str(work["csv"]), str(work["recipe"])))
    assert res["dry_run"] is True and res["out_path"] is None
    assert res["rows_in"] == 6 and res["rows_out"] == 5 and res["rows_quarantined"] == 1
    assert res["quarantine"]["reasons"] == {"email:valid_email": 1}
    assert res["drift"] == {
        "checked": True, "has_drift": False, "worst": None, "findings": [],
        "findings_total": 0, "findings_truncated": False,
    }
    assert sorted(p.name for p in work["dir"].iterdir()) == before


def test_apply_recipe_writes_output_and_quarantine(work):
    out = work["dir"] / "clean.csv"
    quarantine = work["dir"] / "held.csv"
    res = _ok(
        at.apply_recipe(
            str(work["csv"]), str(work["recipe"]), out=str(out), quarantine_out=str(quarantine)
        )
    )
    assert res["out_path"] == str(out.resolve()) and res["dry_run"] is False
    assert len(pd.read_csv(out)) == 5
    assert res["quarantine_path"] == str(quarantine.resolve())
    assert len(pd.read_csv(quarantine)) == 1
    # Same frame as the library call.
    expected = cf.apply_recipe(str(work["csv"]), str(work["recipe"])).dataframe
    assert len(expected) == res["rows_out"]


def test_apply_recipe_never_writes_over_the_input(work):
    before = work["csv"].read_bytes()
    for overwrite in (False, True):
        res = _err(
            at.apply_recipe(
                str(work["csv"]), str(work["recipe"]), out=str(work["csv"]), overwrite=overwrite
            ),
            "OutputError",
        )
        assert "input data file" in res["error"]
    _err(
        at.apply_recipe(str(work["csv"]), str(work["recipe"]), quarantine_out=str(work["csv"])),
        "OutputError",
    )
    assert work["csv"].read_bytes() == before


def test_apply_recipe_refuses_recipe_as_output_and_existing_files(work):
    before = work["recipe"].read_bytes()
    # A .yaml target is not a data format, and is the recipe anyway.
    _err(
        at.apply_recipe(str(work["csv"]), str(work["recipe"]), out=str(work["recipe"])),
        "InvalidArgument",
    )
    assert work["recipe"].read_bytes() == before
    out = work["dir"] / "clean.csv"
    out.write_text("keep me", encoding="utf-8")
    _err(at.apply_recipe(str(work["csv"]), str(work["recipe"]), out=str(out)), "OutputError")
    assert out.read_text(encoding="utf-8") == "keep me"
    _ok(at.apply_recipe(str(work["csv"]), str(work["recipe"]), out=str(out), overwrite=True))
    assert len(pd.read_csv(out)) == 5
    _err(
        at.apply_recipe(
            str(work["csv"]), str(work["recipe"]), out=str(work["dir"] / "a.csv"),
            quarantine_out=str(work["dir"] / "a.csv"),
        ),
        "InvalidArgument",
    )


def test_apply_recipe_stops_on_drift_with_structured_findings(work, drifted_csv):
    out = work["dir"] / "clean.csv"
    res = _err(at.apply_recipe(str(drifted_csv), str(work["recipe"]), out=str(out)), "DriftError")
    assert not out.exists()
    assert res["drift"]["has_drift"] is True
    kinds = {f["kind"] for f in res["drift"]["findings"]}
    assert "renamed_column" in kinds
    renamed = next(f for f in res["drift"]["findings"] if f["kind"] == "renamed_column")
    assert renamed["column"] == "Amt (INR)" and renamed["suggestion"]
    assert "stop" in res["next_step"].lower()


def test_apply_recipe_on_drift_warn_continues_and_reports(work, drifted_csv):
    res = _ok(at.apply_recipe(str(drifted_csv), str(work["recipe"]), on_drift="warn"))
    assert res["drift"]["has_drift"] is True
    assert any("drift" in w.lower() for w in res["warnings"])
    _err(at.apply_recipe(str(drifted_csv), str(work["recipe"]), on_drift="maybe"), "CleanFrameError")


def test_apply_recipe_strict_validation_failure_is_structured(work):
    res = _err(at.apply_recipe(str(work["csv"]), str(work["recipe"]), mode="strict"), "ValidationFailure")
    failure = next(f for f in res["failures"] if f["check"] == "valid_email")
    assert failure["column"] == "email" and failure["failed_rows"] == 1
    assert failure["row_ids"] == [2]


def test_apply_recipe_without_fingerprint_says_drift_was_not_checked(work):
    recipe = cf.Recipe.load(work["recipe"])
    recipe.source_fingerprint = None
    bare = work["dir"] / "bare.recipe.yaml"
    recipe.save(bare)
    res = _ok(at.apply_recipe(str(work["csv"]), str(bare)))
    assert res["drift"]["checked"] is False
    assert any("source_fingerprint" in w for w in res["warnings"])


def test_bad_recipe_is_a_structured_recipe_error(work):
    bad = work["dir"] / "bad.recipe.yaml"
    bad.write_text("columns:\n  Email:\n    ops: [no_such_op]\n", encoding="utf-8")
    res = _err(at.apply_recipe(str(work["csv"]), str(bad)), "RecipeError")
    assert "no_such_op" in res["error"]
    _err(at.apply_recipe(str(work["csv"]), str(work["dir"] / "missing.yaml")), "RecipeError")


def test_workbook_recipe_is_refused_by_replay_tools_but_explained(work):
    single = cf.Recipe.load(work["recipe"]).to_dict()
    wb = work["dir"] / "wb.recipe.yaml"
    wb.write_text(json.dumps({"version": 2, "sheets": {"Customers": single}}), encoding="utf-8")
    res = _err(at.apply_recipe(str(work["csv"]), str(wb)), "InvalidArgument")
    assert "workbook" in res["error"]
    _err(at.check_drift(str(work["csv"]), str(wb)), "InvalidArgument")
    _err(at.export_code(str(wb), str(work["dir"] / "wb.py")), "InvalidArgument")
    explained = _ok(at.explain_recipe(str(wb)))
    assert explained["recipe_type"] == "workbook"
    assert "Customers" in explained["sheets"] and "Sheet 'Customers'" in explained["text"]


# ---------------------------------------------------------------------------
# check_drift
# ---------------------------------------------------------------------------
def test_check_drift_clean_file(work):
    res = _ok(at.check_drift(str(work["csv"]), str(work["recipe"])))
    assert res["drift"]["checked"] is True and res["drift"]["has_drift"] is False
    assert res["patch_changes"] == [] and res["patched_recipe_path"] is None


def test_check_drift_reports_and_drafts_a_patch_without_touching_the_recipe(work, drifted_csv):
    before = work["recipe"].read_bytes()
    patched = work["dir"] / "customer.patched.yaml"
    res = _ok(at.check_drift(str(drifted_csv), str(work["recipe"]), patched_recipe_out=str(patched)))
    assert res["drift"]["has_drift"] is True
    assert any("repointed" in c for c in res["patch_changes"])
    assert res["patched_recipe_path"] == str(patched.resolve())
    assert "Amt (INR)" in patched.read_text(encoding="utf-8")
    assert work["recipe"].read_bytes() == before
    # Never written over the original recipe, even on request.
    _err(
        at.check_drift(str(drifted_csv), str(work["recipe"]), patched_recipe_out=str(work["recipe"])),
        "OutputError",
    )
    assert work["recipe"].read_bytes() == before


def test_check_drift_without_fingerprint_is_not_checked_not_clean(work):
    recipe = cf.Recipe.load(work["recipe"])
    recipe.source_fingerprint = None
    bare = work["dir"] / "bare.recipe.yaml"
    recipe.save(bare)
    res = _ok(at.check_drift(str(work["csv"]), str(bare)))
    assert res["drift"]["checked"] is False and res["drift"]["has_drift"] is None


# ---------------------------------------------------------------------------
# explain_recipe / list_capabilities / export_code
# ---------------------------------------------------------------------------
def test_explain_recipe_covers_every_step(work):
    res = _ok(at.explain_recipe(str(work["recipe"])))
    recipe = cf.Recipe.load(work["recipe"])
    assert res["recipe_type"] == "single"
    assert [s["column"] for s in res["steps"]] == [c.source for c in recipe.columns]
    assert res["steps"][0]["ops"] == ["collapse_whitespace"]
    assert len(res["validations"]) == len(recipe.validations)
    assert res["steps_truncated"] is False and res["text_truncated"] is False
    text = res["text"]
    for col in recipe.columns:
        assert f'"{col.source}"' in text
        for op in col.ops:
            assert op.name in text
    assert "quarantine" in text and "Drift guard" in text
    # Op explanations come from the op registry docstrings.
    assert "- collapse_whitespace: Collapse internal runs" in text


def test_explain_recipe_bounds_large_mappings_and_long_recipes(work):
    mapping = {f"variant {i}": "Canonical" for i in range(500)}
    columns = {f"Col {i}": {"ops": ["strip_whitespace"]} for i in range(300)}
    columns["City"] = {"normalize_values": mapping}
    validate = [{"column": f"Col {i}", "check": "not_null"} for i in range(300)]
    recipe = cf.Recipe.from_dict({"columns": columns, "validate": validate})
    path = work["dir"] / "big.recipe.yaml"
    recipe.save(path)
    res = _ok(at.explain_recipe(str(path)))
    assert res["steps_total"] == 301 and len(res["steps"]) == at.MAX_EXPLAIN_STEPS
    assert res["steps_truncated"] is True and res["validations_truncated"] is True
    assert "more column(s) not explained" in res["text"]
    assert len(_strict_json(res)) < 40_000
    small = cf.Recipe.from_dict({"columns": {"City": {"normalize_values": mapping}}})
    small.save(path)
    text = _ok(at.explain_recipe(str(path)))["text"]
    assert "+490 more" in text and len(text) < 3000


def test_list_capabilities_matches_registries():
    res = _ok(at.list_capabilities())
    assert [o["name"] for o in res["ops"]] == cf.list_ops()
    assert [d["name"] for d in res["detectors"]] == cf.list_detectors()
    assert set(res["modes"]) == {m.value for m in Mode}
    parse_date = next(o for o in res["ops"] if o["name"] == "parse_date")
    assert "formats" in parse_date["params"] and parse_date["doc"]
    assert "valid_email" in res["validation_checks"]["named"]


def test_export_code_writes_runnable_pandas(work):
    out = work["dir"] / "clean_customers.py"
    res = _ok(at.export_code(str(work["recipe"]), str(out)))
    assert res["code_path"] == str(out.resolve()) and res["partial"] is False
    assert "def clean(df):" in res["code_preview"]
    namespace: dict = {}
    exec(compile(out.read_text(encoding="utf-8"), str(out), "exec"), namespace)
    cleaned = namespace["clean"](cf.read_frame(work["csv"]))
    assert len(cleaned) == 5
    # Identical regeneration is idempotent; a different file is protected.
    _ok(at.export_code(str(work["recipe"]), str(out)))
    out.write_text("# hand-written\n", encoding="utf-8")
    _err(at.export_code(str(work["recipe"]), str(out)), "OutputError")
    assert out.read_text(encoding="utf-8") == "# hand-written\n"
    _err(at.export_code(str(work["recipe"]), str(work["dir"] / "x.txt")), "InvalidArgument")
    _err(at.export_code(str(work["recipe"]), str(out), func_name="not valid"), "CleanFrameError")


# ---------------------------------------------------------------------------
# cross-cutting: bounded, JSON-safe, deterministic, private when asked
# ---------------------------------------------------------------------------
def test_outputs_are_bounded_on_a_wide_messy_file(tmp_path):
    n_cols = 130
    frame = pd.DataFrame(
        {f"Col {i}": [f"  value {j} " if j % 2 else f"VALUE {j}" for j in range(60)] for i in range(n_cols)}
    )
    path = tmp_path / "wide.csv"
    frame.to_csv(path, index=False)
    prof = _ok(at.profile_data(str(path)))
    assert prof["issue_count"] > at.MAX_ISSUES
    assert len(prof["issues"]) == at.MAX_ISSUES and prof["issues_truncated"] is True
    assert len(prof["column_profiles"]) == at.MAX_COLUMNS
    assert prof["column_profiles_truncated"] is True
    recipe = tmp_path / "wide.recipe.yaml"
    plan = _ok(at.suggest_recipe(str(path), str(recipe)))
    assert len(plan["preview"]) == at.MAX_PREVIEW_CHANGES and plan["preview_truncated"] is True
    assert plan["preview_total"] == plan["summary"]["changed_cells"]
    assert len(plan["changes_by_column"]) == at.MAX_MAP_ENTRIES
    assert plan["changes_by_column_truncated"] is True
    assert plan["summary"]["renamed_columns_total"] == n_cols
    assert len(plan["summary"]["renamed_columns"]) == at.MAX_MAP_ENTRIES
    # Every tool's whole payload stays well inside an agent's context budget.
    payloads = {
        "profile_data": prof,
        "suggest_recipe": plan,
        "apply_recipe": at.apply_recipe(str(path), str(recipe)),
        "check_drift": at.check_drift(str(path), str(recipe)),
        "explain_recipe": at.explain_recipe(str(recipe)),
        "export_code": at.export_code(str(recipe), str(tmp_path / "wide.py")),
        "list_capabilities": at.list_capabilities(),
    }
    for name, payload in payloads.items():
        assert payload["ok"] is True, name
        assert len(_strict_json(payload)) < 40_000, name


def test_long_values_are_capped(tmp_path):
    long_value = "x" * 5000
    path = tmp_path / "long.csv"
    pd.DataFrame({"Notes": [long_value, " padded ", "b"]}).to_csv(path, index=False)
    res = _ok(at.profile_data(str(path)))
    examples = res["column_profiles"][0]["examples"]
    assert all(len(e) <= at.MAX_VALUE_CHARS + 3 for e in examples if isinstance(e, str))


def test_hide_values_switch_removes_cell_values(work, monkeypatch):
    monkeypatch.setenv(at.HIDE_VALUES_ENV, "1")
    prof = _ok(at.profile_data(str(work["csv"])))
    for col in prof["column_profiles"]:
        assert all(v in (None, "<hidden>") for v in col["examples"])
    assert "not-an-email" not in _strict_json(prof)
    plan = _ok(at.suggest_recipe(str(work["csv"]), str(work["dir"] / "h.recipe.yaml")))
    for change in plan["preview"]:
        assert change["before"] in (None, "<hidden>") and change["after"] in (None, "<hidden>")


@pytest.mark.parametrize(
    ("call", "needs_out"),
    [
        (lambda w: at.profile_data(str(w["csv"])), False),
        (lambda w: at.apply_recipe(str(w["csv"]), str(w["recipe"])), False),
        (lambda w: at.check_drift(str(w["csv"]), str(w["recipe"])), False),
        (lambda w: at.explain_recipe(str(w["recipe"])), False),
        (lambda w: at.list_capabilities(), False),
        (lambda w: at.export_code(str(w["recipe"]), str(w["dir"] / "c.py")), True),
    ],
)
def test_every_tool_is_deterministic_and_strict_json(work, call, needs_out):
    first = call(work)
    second = call(work)
    assert first == second
    assert _strict_json(first) == _strict_json(second)
    assert first["ok"] is True


def test_jsonable_handles_numpy_pandas_and_missing_values():
    raw = {
        "nan": float("nan"), "inf": float("inf"), "np_nan": np.float64("nan"),
        "np_int": np.int64(3), "np_bool": np.bool_(True), "na": pd.NA, "nat": pd.NaT,
        "ts": pd.Timestamp("2024-01-31"), "dt64": np.datetime64("2024-01-31T00:00:00.000000000"),
        "nat64": np.datetime64("NaT", "ns"), "date": dt.date(2024, 1, 31), "tuple": (1, "a"),
        "set": {"b", "a"}, "mode": Mode.REVIEW, "path": Path("x.csv"), "obj": object,
    }
    out = at._jsonable(raw)
    json.dumps(out, allow_nan=False)
    assert out["nan"] is None and out["inf"] is None and out["np_nan"] is None
    assert out["np_int"] == 3 and type(out["np_int"]) is int
    assert out["np_bool"] is True
    assert out["na"] is None and out["nat"] is None and out["nat64"] is None
    assert out["ts"].startswith("2024-01-31") and out["dt64"].startswith("2024-01-31")
    assert out["tuple"] == [1, "a"] and out["set"] == ["a", "b"] and out["mode"] == "review"


def test_mcp_server_main_explains_how_to_install_mcp(monkeypatch, capsys):
    """Without the optional SDK the entry point fails with the install command, not a traceback."""
    monkeypatch.setitem(sys.modules, "mcp", None)
    monkeypatch.setitem(sys.modules, "mcp.server", None)
    from cleanframe.mcp_server import main

    assert main([]) == 1
    assert 'pip install "cleanframe-engine[mcp]"' in capsys.readouterr().err
