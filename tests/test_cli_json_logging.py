"""--json gives CI one parseable object; the library logs instead of printing."""

from __future__ import annotations

import json
import logging
import subprocess
import sys

import pandas as pd

import cleanframe as cf

CSV = "Name,City,Amount\nAnn ,pune,$1200\nBob,Delhi,$50\n"


def _run(args, cwd):
    return subprocess.run(
        [sys.executable, "-m", "cleanframe", *args],
        cwd=cwd, capture_output=True, text=True, encoding="utf-8",
        env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"},
    )


def test_json_stdout_is_exactly_one_object_for_clean_and_apply(tmp_path):
    (tmp_path / "d.csv").write_text(CSV, encoding="utf-8")
    cleaned = _run(["clean", "d.csv", "--out", "o.csv", "--json"], tmp_path)
    assert cleaned.returncode == 0, cleaned.stderr
    summary = json.loads(cleaned.stdout)  # would raise if anything else hit stdout
    assert summary["status"] == "ok" and summary["exit_code"] == 0
    assert summary["command"] == "clean" and summary["rows_out"] == 2
    assert summary["outputs"]["recipe"].endswith("d.recipe.yaml")
    assert "changed_cells" in summary["diff"]

    replay = _run(["apply", "d.csv", "--recipe", "d.recipe.yaml", "--out", "o2.csv", "--json"], tmp_path)
    assert replay.returncode == 0, replay.stderr
    body = json.loads(replay.stdout)
    assert body["drift"] == {"has_drift": False, "findings": []}


def test_json_reports_drift_and_errors_with_the_documented_exit_codes(tmp_path):
    (tmp_path / "d.csv").write_text(CSV, encoding="utf-8")
    assert _run(["clean", "d.csv", "--out", "o.csv"], tmp_path).returncode == 0
    (tmp_path / "eu.csv").write_text('Name,City,Amount\nAnn,pune,"€1.200,50"\nBob,Delhi,"€3,25"\n', encoding="utf-8")
    drifted = _run(["apply", "eu.csv", "--recipe", "d.recipe.yaml", "--json"], tmp_path)
    assert drifted.returncode == 3
    body = json.loads(drifted.stdout)
    assert body["status"] == "drift" and body["exit_code"] == 3
    assert any(f["kind"] == "number_format_drift" for f in body["drift"]["findings"])

    missing = _run(["apply", "nope.csv", "--recipe", "d.recipe.yaml", "--json"], tmp_path)
    assert missing.returncode == 1
    err = json.loads(missing.stdout)
    assert err["status"] == "error" and "not found" in err["error"]["message"].lower()


def test_library_is_silent_by_default_and_logs_when_asked(caplog):
    df = pd.DataFrame({"a": [" x ", "y"], "b": ["1", "2"]})
    assert any(isinstance(h, logging.NullHandler) for h in logging.getLogger("cleanframe").handlers)
    with caplog.at_level(logging.INFO, logger="cleanframe"):
        cf.clean(df)
    messages = [r.getMessage() for r in caplog.records if r.name.startswith("cleanframe")]
    assert any("executed recipe" in m for m in messages)
