"""The GitHub Action in ``action/``: its metadata contract, the job summary it renders
from real ``cleanframe apply --json`` payloads, and an end-to-end replay.

The action cannot be run on a real runner from a unit test. What can be pinned here is
everything it is made of: ``action.yml`` declares the documented interface, wires
every output to a step and never interpolates an input into a shell script, and
``summary.py`` - which holds all of the logic - is driven as a subprocess exactly as
the composite step drives it, with ``GITHUB_STEP_SUMMARY``/``GITHUB_OUTPUT`` pointed at
temporary files. The payloads are captured by running the real CLI, so a change to
the ``--json`` contract fails here rather than in someone's pipeline.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
ACTION_DIR = ROOT / "action"
ACTION_YML = ACTION_DIR / "action.yml"
SUMMARY = ACTION_DIR / "summary.py"
EXAMPLES = ROOT / "examples"
WORKFLOW_EXAMPLE = EXAMPLES / "github-action" / "data-ci.yml"
RECIPE = "recipes/customer.recipe.yaml"

pytestmark = pytest.mark.skipif(
    not (ACTION_YML.exists() and SUMMARY.exists()),
    reason="action/ ships only in a source checkout, not in the sdist or wheel",
)

DOCUMENTED_INPUTS = {
    "files", "recipe", "out-dir", "mode", "on-drift", "fail-on-quarantine",
    "cleanframe-version", "python-version", "extra-packages",
}
DOCUMENTED_OUTPUTS = {
    "drift", "changed-cells", "quarantined", "failed-files", "exit-code", "results-file",
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def action() -> dict:
    return yaml.safe_load(ACTION_YML.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def summary():
    """summary.py imported as a module, for its pure helpers."""
    spec = importlib.util.spec_from_file_location("cleanframe_action_summary", SUMMARY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module by name
    previous, sys.dont_write_bytecode = sys.dont_write_bytecode, True  # keep action/ clean
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


def _workspace(root: Path) -> Path:
    """A repo-like tree: one clean month, one drifted month, the committed recipe."""
    (root / "data").mkdir(parents=True)
    (root / "recipes").mkdir()
    shutil.copy(EXAMPLES / "messy_customers.csv", root / "data" / "jan.csv")
    shutil.copy(EXAMPLES / "customer.recipe.yaml", root / RECIPE)
    lines = (EXAMPLES / "messy_customers.csv").read_text(encoding="utf-8").splitlines()
    header = lines[0].replace("Amount", "Total Amount") + ",Loyalty Tier"
    drifted = [header] + [line + ",gold" for line in lines[1:]]
    (root / "data" / "feb.csv").write_text("\n".join(drifted) + "\n", encoding="utf-8")
    return root


def _env(tmp_path: Path, **inputs: str) -> dict[str, str]:
    # Drop the outer job's GitHub/CF variables, and PYTHONIOENCODING so a summary.py
    # that printed emoji without reconfiguring stdout would fail on Windows cp1252.
    env = {
        k: v for k, v in os.environ.items()
        if not k.startswith(("CF_", "GITHUB_")) and k != "PYTHONIOENCODING"
    }
    runner_temp = tmp_path / "_runner_temp"
    runner_temp.mkdir(exist_ok=True)
    env.update(
        GITHUB_STEP_SUMMARY=str(tmp_path / "step_summary.md"),
        GITHUB_OUTPUT=str(tmp_path / "github_output.txt"),
        RUNNER_TEMP=str(runner_temp),
    )
    env.update(inputs)
    return env


def _summary_py(args: list[str], cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SUMMARY), *args], cwd=cwd, env=env, capture_output=True,
        timeout=600,
    )


def _stdout(proc: subprocess.CompletedProcess) -> str:
    return proc.stdout.decode("utf-8")


def _outputs(tmp_path: Path) -> dict[str, str]:
    text = (tmp_path / "github_output.txt").read_text(encoding="utf-8")
    pairs = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
    return pairs


def _step_summary(tmp_path: Path) -> str:
    return (tmp_path / "step_summary.md").read_text(encoding="utf-8")


def _table_rows(markdown: str) -> list[str]:
    return [line for line in markdown.splitlines() if line.startswith("| ") and "---" not in line][1:]


def _unescaped_pipes(row: str) -> int:
    return len(re.findall(r"(?<!\\)\|", row))


@pytest.fixture(scope="module")
def captured(tmp_path_factory) -> dict[str, dict]:
    """Real ``cleanframe apply --json`` runs: ok, drift stop, forced drift, strict, usage."""
    work = _workspace(tmp_path_factory.mktemp("captured"))

    def cli(name: str, *args: str) -> dict:
        proc = subprocess.run(
            [sys.executable, "-m", "cleanframe", "apply", *args, "--out", f"out/{name}.csv", "--json"],
            cwd=work, capture_output=True, timeout=300,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        return {"exit_code": proc.returncode, "payload": json.loads(proc.stdout.decode("utf-8"))}

    return {
        "ok": cli("ok", "data/jan.csv", "--recipe", RECIPE),
        "drift": cli("drift", "data/feb.csv", "--recipe", RECIPE),
        "forced": cli("forced", "data/feb.csv", "--recipe", RECIPE, "--force"),
        "strict": cli("strict", "data/jan.csv", "--recipe", RECIPE, "--mode", "strict"),
        "usage": cli("usage", "data/jan.csv", "--recipe", RECIPE, "--mode", "bogus"),
    }


def _results(records: list[dict], **config) -> dict:
    base = {
        "schema": "cleanframe-action-results/1", "recipe": RECIPE, "mode": "review",
        "on_drift": "error", "fail_on_quarantine": False, "out_dir": None, "errors": [],
    }
    base.update(config)
    base["files"] = records
    return base


def _render(tmp_path: Path, results: dict) -> tuple[subprocess.CompletedProcess, str, dict]:
    path = tmp_path / "results.json"
    path.write_text(json.dumps(results), encoding="utf-8")
    proc = _summary_py(["render", str(path)], tmp_path, _env(tmp_path))
    return proc, _step_summary(tmp_path), _outputs(tmp_path)


# ---------------------------------------------------------------------------
# action.yml: the documented interface
# ---------------------------------------------------------------------------
def test_metadata_is_marketplace_ready(action):
    assert action["name"] == "CleanFrame Data CI"
    assert 0 < len(action["description"]) < 125
    assert action["branding"]["icon"] and action["branding"]["color"]
    assert action["runs"]["using"] == "composite"


def test_declares_exactly_the_documented_inputs_and_outputs(action):
    inputs, outputs = action["inputs"], action["outputs"]
    assert set(inputs) == DOCUMENTED_INPUTS
    assert set(outputs) == DOCUMENTED_OUTPUTS
    assert {name for name, spec in inputs.items() if spec.get("required")} == {"files", "recipe"}
    assert inputs["mode"]["default"] == "review"
    assert inputs["on-drift"]["default"] == "error"
    assert inputs["fail-on-quarantine"]["default"] == "false"
    assert inputs["python-version"]["default"] == "3.12"  # a string, not the float 3.12
    assert inputs["cleanframe-version"]["default"] == ""
    assert all(spec.get("description") for spec in [*inputs.values(), *outputs.values()])


def test_every_output_is_wired_to_a_step_that_exists(action):
    step_ids = {step.get("id") for step in action["runs"]["steps"]}
    for name, spec in action["outputs"].items():
        match = re.fullmatch(r"\$\{\{\s*steps\.([\w-]+)\.outputs\.([\w-]+)\s*\}\}", spec["value"])
        assert match, f"output {name} is not a plain step-output reference"
        assert match.group(1) in step_ids
        assert match.group(2) == name


def test_steps_use_bash_and_never_interpolate_inputs_into_scripts(action):
    steps = action["runs"]["steps"]
    run_steps = [step for step in steps if "run" in step]
    assert len(run_steps) >= 2
    for step in run_steps:
        assert step.get("shell") == "bash", step["name"]
        # Script-injection hardening: expressions go through env:, never into the body.
        assert "${{" not in step["run"], step["name"]
        assert "eval" not in step["run"].split(), step["name"]
    referenced = set(re.findall(r"inputs\.([\w-]+)", ACTION_YML.read_text(encoding="utf-8")))
    assert referenced == DOCUMENTED_INPUTS  # every input is used, none is undeclared


def test_every_variable_summary_py_reads_is_provided_by_action_yml(action):
    provided = {key for step in action["runs"]["steps"] for key in (step.get("env") or {})}
    read = set(re.findall(r"\"(CF_[A-Z_]+)\"", SUMMARY.read_text(encoding="utf-8")))
    assert read and read <= provided, read - provided


def test_setup_python_matches_this_repos_own_workflows(action):
    ci = ROOT / ".github" / "workflows" / "ci.yml"
    if not ci.exists():
        pytest.skip("no .github/workflows/ci.yml in this tree")
    ours = next(s["uses"] for s in action["runs"]["steps"] if "setup-python" in s.get("uses", ""))
    theirs = set(re.findall(r"actions/setup-python@(v\d+)", ci.read_text(encoding="utf-8")))
    assert theirs == {ours.split("@", 1)[1]}
    setup = next(s for s in action["runs"]["steps"] if "setup-python" in s.get("uses", ""))
    assert setup["with"]["update-environment"] is False  # never repoints the caller's python


def test_summary_py_is_stdlib_only():
    tree = ast.parse(SUMMARY.read_text(encoding="utf-8"))
    modules = {
        alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0] for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0
    }
    assert modules - {"__future__"} <= set(sys.stdlib_module_names), modules


@pytest.mark.parametrize("doc", ["action/README.md", "docs/github-action.md"])
def test_docs_cover_every_input_and_output(doc):
    text = (ROOT / doc).read_text(encoding="utf-8")
    for name in DOCUMENTED_INPUTS | DOCUMENTED_OUTPUTS:
        assert f"| `{name}` |" in text, f"{doc} does not document {name}"
    assert "inboxpraveen/cleanframe-action@v1" in text  # the Marketplace mirror line


def test_all_docs_reference_one_action_version():
    texts = [
        (ROOT / "action" / "README.md").read_text(encoding="utf-8"),
        (ROOT / "docs" / "github-action.md").read_text(encoding="utf-8"),
        WORKFLOW_EXAMPLE.read_text(encoding="utf-8"),
    ]
    refs = {ref for text in texts for ref in re.findall(r"inboxpraveen/Cleanframe/action@(v[\w.]+)", text)}
    assert len(refs) == 1, refs


def test_example_workflow_is_valid_and_uses_only_real_inputs(action):
    workflow = yaml.safe_load(WORKFLOW_EXAMPLE.read_text(encoding="utf-8"))
    triggers = workflow[True]  # PyYAML (YAML 1.1) reads the bare key `on` as True
    assert "pull_request" in triggers and "schedule" in triggers
    assert workflow["permissions"] == {"contents": "read"}
    steps = [step for job in workflow["jobs"].values() for step in job["steps"]]
    ours = [step for step in steps if step.get("uses", "").startswith("inboxpraveen/Cleanframe/action@")]
    assert len(ours) == 1
    assert {"files", "recipe"} <= set(ours[0]["with"]) <= set(action["inputs"])
    for step in steps:
        assert "${{" not in step.get("run", "")


# ---------------------------------------------------------------------------
# the --json contract the action depends on
# ---------------------------------------------------------------------------
def test_captured_cli_payloads_have_the_shapes_the_action_reads(captured):
    ok, drift, forced = captured["ok"], captured["drift"], captured["forced"]
    strict, usage = captured["strict"], captured["usage"]
    assert ok["exit_code"] == 0 and ok["payload"]["status"] == "ok"
    # Exact cell counts can move with the pandas version (see the minimum-pins CI job),
    # so the tests below compare against each payload's own numbers.
    assert isinstance(ok["payload"]["diff"]["changed_cells"], int)
    assert ok["payload"]["diff"]["changed_cells"] > 0
    assert ok["payload"]["rows_quarantined"] == 1
    assert ok["payload"]["drift"] == {"has_drift": False, "findings": []}
    assert drift["exit_code"] == 3 and drift["payload"]["status"] == "drift"
    assert drift["payload"]["drift"]["has_drift"] is True
    assert {"kind", "severity", "column", "message"} <= set(drift["payload"]["drift"]["findings"][0])
    assert "diff" not in drift["payload"]  # a stopped file has no counts
    assert forced["exit_code"] == 0 and forced["payload"]["drift"]["has_drift"] is True
    assert forced["payload"]["warnings"]
    assert strict["exit_code"] == 4 and strict["payload"]["status"] == "validation_failed"
    assert "error" in strict["payload"] and "diff" not in strict["payload"]
    assert usage["exit_code"] == 2 and usage["payload"]["status"] == "usage_error"
    assert "input" not in usage["payload"]  # the row's file name must come from the action


# ---------------------------------------------------------------------------
# render: summary markdown and outputs from captured payloads
# ---------------------------------------------------------------------------
def test_render_reports_every_kind_of_result(tmp_path, captured):
    records = [
        {"file": "data/jan.csv", **captured["ok"]},
        {"file": "data/feb.csv", **captured["drift"]},
        {"file": "data/strict.csv", **captured["strict"]},
        {"file": "data/usage.csv", **captured["usage"]},
        {"file": "missing/*.csv", "exit_code": 2, "payload": None,
         "note": "missing/*.csv matched no files"},
    ]
    proc, markdown, outputs = _render(tmp_path, _results(records))
    changed = captured["ok"]["payload"]["diff"]["changed_cells"]

    assert proc.returncode == 4  # worst of 0, 3, 4, 2, 2
    assert outputs == {
        "drift": "true", "changed-cells": str(changed), "quarantined": "1", "failed-files": "4",
        "exit-code": "4", "results-file": str(tmp_path / "results.json"),
    }
    rows = _table_rows(markdown)
    assert len(rows) == 5 and all(_unescaped_pipes(row) == 7 for row in rows)
    jan, feb, strict, usage, missing = rows
    assert jan.startswith(f"| ✅ | data/jan.csv | Passed | {changed:,} | 1 |")
    assert feb.startswith("| 🛑 | data/feb.csv | Schema drift (exit 3) | — | — |")
    assert "**warning** Column \"Total Amount\" is new" in feb
    assert "<br>" in feb  # findings are bulleted inside the cell
    assert "Validation failed (exit 4)" in strict and "email:valid\\_email" in strict
    assert "Usage error (exit 2)" in usage and "invalid choice: 'bogus'" in usage
    assert "missing/\\*.csv matched no files" in missing
    assert "## 🧹 CleanFrame Data CI · ❌ Failed" in markdown
    assert "**❌ 4 of 5 files failed** — worst exit code 4 (validation failed)" in markdown
    assert "cleanframe suggest FILE --recipe recipes/customer.recipe.yaml --update" in markdown
    assert "CleanFrame 0." in markdown  # version taken from the payloads
    # annotations go to the log, one per failing file, with file= only for real files
    log = _stdout(proc)
    assert "::error file=data/feb.csv,title=CleanFrame%3A Schema drift (exit 3)::" in log
    assert re.search(r"^::error title=CleanFrame%3A Usage error \(exit 2\)::missing", log, re.M)
    # the results file is enriched with each outcome for later steps
    saved = json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))
    assert [f["outcome"]["effective_exit_code"] for f in saved["files"]] == [0, 3, 4, 2, 2]
    assert saved["totals"]["exit_code"] == "4"


def test_render_all_passing(tmp_path, captured):
    proc, markdown, outputs = _render(tmp_path, _results([{"file": "data/jan.csv", **captured["ok"]}]))
    assert proc.returncode == 0
    assert outputs["exit-code"] == "0" and outputs["failed-files"] == "0"
    assert outputs["drift"] == "false"
    assert "· ✅ Passed" in markdown and "**✅ All 1 file passed.**" in markdown
    assert "How to fix" not in markdown


def test_on_drift_warn_passes_but_reports_drift_and_warnings(tmp_path, captured):
    proc, markdown, outputs = _render(
        tmp_path, _results([{"file": "data/feb.csv", **captured["forced"]}], on_drift="warn")
    )
    assert proc.returncode == 0
    assert outputs["drift"] == "true" and outputs["failed-files"] == "0"
    assert outputs["changed-cells"] == str(captured["forced"]["payload"]["diff"]["changed_cells"])
    assert "| ⚠️ | data/feb.csv | Applied despite drift |" in markdown
    assert "Passed with drift" in markdown
    assert "<details><summary>⚠️ CleanFrame warnings (3)</summary>" in markdown
    assert "skipped recipe column 'Amount'" in markdown
    assert "::warning file=data/feb.csv,title=CleanFrame%3A applied despite drift::" in _stdout(proc)


def test_fail_on_quarantine_turns_quarantined_rows_into_exit_4(tmp_path, captured):
    proc, markdown, outputs = _render(
        tmp_path, _results([{"file": "data/jan.csv", **captured["ok"]}], fail_on_quarantine=True)
    )
    assert proc.returncode == 4
    assert outputs["failed-files"] == "1" and outputs["exit-code"] == "4"
    changed = captured["ok"]["payload"]["diff"]["changed_cells"]
    assert f"| ❌ | data/jan.csv | Rows quarantined | {changed:,} | 1 |" in markdown
    assert "fail-on-quarantine is on" in markdown


def test_a_run_without_json_is_reported_not_crashed_on(tmp_path):
    records = [
        {"file": "a.csv", "exit_code": 1, "payload": None,
         "stderr_tail": "Traceback (most recent call last):\nModuleNotFoundError: No module named 'cleanframe'"},
        {"file": "b.csv", "exit_code": 0, "payload": None, "stderr_tail": ""},
    ]
    proc, markdown, outputs = _render(tmp_path, _results(records))
    assert proc.returncode == 70  # exit 0 with no summary is never a pass
    a, b = _table_rows(markdown)
    assert "Error (exit 1)" in a and "ModuleNotFoundError" in a
    assert "No JSON summary" in b and "0.4.0 or later" in b
    assert outputs["failed-files"] == "2"


def test_hostile_names_cannot_break_the_table_or_issue_workflow_commands(tmp_path):
    evil = "evil|name\n::error::pwned <script>alert(1)</script> *x*.csv"
    finding = {"kind": "new_column", "severity": "warning", "column": evil,
               "message": f'Column "{evil}" is new\n::add-mask::secret | more'}
    payload = {"status": "drift", "exit_code": 3, "drift": {"has_drift": True, "findings": [finding]},
               "warnings": [evil]}
    proc, markdown, _ = _render(tmp_path, _results([{"file": evil, "exit_code": 3, "payload": payload}]))
    assert proc.returncode == 3
    (row,) = _table_rows(markdown)
    assert _unescaped_pipes(row) == 7
    assert "<script>" not in markdown and "&lt;script&gt;" in markdown
    assert "evil\\|name ::error::pwned" in row and "\\*x\\*" in row
    for line in _stdout(proc).splitlines():
        stripped = line.strip()
        assert not stripped.startswith(("::error::pwned", "::add-mask::")), line


def test_summary_stays_under_the_size_cap_with_huge_runs(tmp_path, summary):
    findings = [{"kind": "new_column", "severity": "warning", "column": f"c{i}", "message": "m" * 300}
                for i in range(20)]
    payload = {"status": "drift", "exit_code": 3, "drift": {"has_drift": True, "findings": findings},
               "warnings": ["w" * 300] * 12}
    records = [{"file": f"data/part-{i:04d}.csv", "exit_code": 3, "payload": payload} for i in range(400)]
    proc, markdown, outputs = _render(tmp_path, _results(records))
    assert proc.returncode == 3
    assert len(markdown.encode("utf-8")) <= summary.MAX_SUMMARY_BYTES
    assert "more file(s) not shown (" in markdown
    assert "_More warnings are in the job log._" in markdown
    assert outputs["failed-files"] == "400"  # outputs always count everything
    assert "• … and 12 more" in markdown  # 8 findings shown per file
    assert "m" * 240 not in markdown  # long messages are cut


# ---------------------------------------------------------------------------
# run: the composite step end to end, against the real CLI
# ---------------------------------------------------------------------------
def test_run_replays_every_file_then_fails_with_the_worst_code(tmp_path):
    work = _workspace(tmp_path / "repo")
    env = _env(
        tmp_path, CF_FILES="data/*.csv\nmissing/*.csv\n", CF_RECIPE=RECIPE,
        CF_OUT_DIR="cleaned", CF_MODE="review", CF_ON_DRIFT="error", CF_FAIL_ON_QUARANTINE="false",
    )
    proc = _summary_py(["run"], work, env)
    assert proc.returncode == 3, _stdout(proc)
    outputs = _outputs(tmp_path)
    assert outputs["drift"] == "true" and outputs["exit-code"] == "3"
    results = json.loads(Path(outputs["results-file"]).read_text(encoding="utf-8"))
    jan_changed = results["files"][1]["payload"]["diff"]["changed_cells"]
    assert outputs["failed-files"] == "2" and outputs["changed-cells"] == str(jan_changed)
    assert outputs["quarantined"] == "1"
    # the drifted file did not hide the clean one, and nothing was written beside the inputs
    assert (work / "cleaned" / "jan.clean.csv").is_file()
    assert not (work / "cleaned" / "feb.clean.csv").exists()
    assert sorted(p.name for p in (work / "data").iterdir()) == ["feb.csv", "jan.csv"]
    markdown = _step_summary(tmp_path)
    assert [row.split(" | ")[1] for row in _table_rows(markdown)] == [
        "data/feb.csv", "data/jan.csv", "missing/\\*.csv",
    ]
    assert [r["exit_code"] for r in results["files"]] == [3, 0, 2]
    assert results["files"][1]["output"].endswith("jan.clean.csv")
    log = _stdout(proc)
    assert "::group::cleanframe apply data/jan.csv (exit 0)" in log
    assert "::stop-commands::" in log  # CLI output is echoed with commands disabled


def test_run_check_only_leaves_the_workspace_untouched(tmp_path):
    work = _workspace(tmp_path / "repo")
    before = sorted(p.relative_to(work) for p in work.rglob("*"))
    env = _env(tmp_path, CF_FILES="data/jan.csv", CF_RECIPE=RECIPE)
    proc = _summary_py(["run"], work, env)
    assert proc.returncode == 0, _stdout(proc)
    assert sorted(p.relative_to(work) for p in work.rglob("*")) == before
    outputs = _outputs(tmp_path)
    assert outputs["exit-code"] == "0" and outputs["drift"] == "false"


def test_run_on_drift_warn_applies_and_strict_still_stops(tmp_path):
    work = _workspace(tmp_path / "repo")
    warn = _summary_py(["run"], work, _env(tmp_path, CF_FILES="data/feb.csv", CF_RECIPE=RECIPE, CF_ON_DRIFT="warn"))
    assert warn.returncode == 0, _stdout(warn)
    assert _outputs(tmp_path)["drift"] == "true"

    strict_dir = tmp_path / "strict"
    strict_dir.mkdir()
    strict = _summary_py(
        ["run"], work,
        _env(strict_dir, CF_FILES="data/feb.csv", CF_RECIPE=RECIPE, CF_ON_DRIFT="warn", CF_MODE="strict"),
    )
    assert strict.returncode == 3, _stdout(strict)


def test_run_rejects_bad_inputs_before_running_anything(tmp_path):
    work = _workspace(tmp_path / "repo")
    env = _env(
        tmp_path, CF_FILES="   ", CF_RECIPE="recipes/nope.yaml", CF_MODE="yolo",
        CF_ON_DRIFT="ignore", CF_FAIL_ON_QUARANTINE="maybe",
    )
    proc = _summary_py(["run"], work, env)
    assert proc.returncode == 2
    outputs = _outputs(tmp_path)
    assert outputs["exit-code"] == "2" and outputs["failed-files"] == "0"
    markdown = _step_summary(tmp_path)
    assert "Configuration error" in markdown
    for fragment in ("files input is empty", "Recipe not found", "'yolo'", "'ignore'", "'maybe'"):
        assert fragment in markdown, fragment
    assert "::group::cleanframe apply" not in _stdout(proc)


def test_run_refuses_out_dir_collisions_instead_of_overwriting(tmp_path):
    work = tmp_path / "repo"
    for sub in ("a", "b"):
        (work / "data" / sub).mkdir(parents=True)
        shutil.copy(EXAMPLES / "messy_customers.csv", work / "data" / sub / "x.csv")
    (work / "recipes").mkdir()
    shutil.copy(EXAMPLES / "customer.recipe.yaml", work / RECIPE)
    env = _env(tmp_path, CF_FILES="data/**/*.csv", CF_RECIPE=RECIPE, CF_OUT_DIR="cleaned")
    proc = _summary_py(["run"], work, env)
    assert proc.returncode == 2
    assert _outputs(tmp_path)["failed-files"] == "2"
    assert "would be written by more than one input" in _step_summary(tmp_path)
    assert not (work / "cleaned" / "x.clean.csv").exists()


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------
def test_md_text_escapes_cells(summary):
    assert summary.md_text("a|b\nc <b>x</b> *y* _z_ `k`") == (
        "a\\|b c &lt;b&gt;x&lt;/b&gt; \\*y\\* \\_z\\_ \\`k\\`"
    )
    assert summary.md_text("x" * 500, 10) == "x" * 9 + "…"


def test_workflow_command_escaping(summary):
    assert summary._command_data("50%\r\n::x") == "50%25%0D%0A::x"
    assert summary._command_property("a:b,c\nd") == "a%3Ab%2Cc%0Ad"


def test_split_patterns(summary):
    assert summary.split_patterns("data/*.csv  more/**/*.csv") == ["data/*.csv", "more/**/*.csv"]
    block = "data/Q1 sales.csv\n# a comment\n\n  data/*.tsv  \n"
    assert summary.split_patterns(block) == ["data/Q1 sales.csv", "data/*.tsv"]
    assert summary.split_patterns("") == []


def test_output_names(summary):
    assert summary.output_name("data/jan.csv") == "jan.clean.csv"
    assert summary.output_name("Book.XLSX") == "Book.clean.xlsx"
    assert summary.output_name("old.xls") == "old.clean.xlsx"  # pandas cannot write .xls
    assert summary.output_name("big.parquet") == "big.clean.csv"
    assert summary._cli_arg("-x.csv").endswith("-x.csv") and summary._cli_arg("-x.csv")[0] == "."


def test_install_requirement(summary, tmp_path):
    # From this repository: the action installs the library from its own checkout.
    requirement, _ = summary.install_requirement(ACTION_DIR, "")
    assert Path(requirement) == ROOT
    # Vendored into someone else's project: never pip-install *their* pyproject.
    other = tmp_path / "theirs"
    (other / "action").mkdir(parents=True)
    (other / "pyproject.toml").write_text('[project]\nname = "their-app"\n', encoding="utf-8")
    assert summary.install_requirement(other / "action", "")[0] == "cleanframe-engine"
    # The Marketplace mirror has no ../pyproject.toml at all.
    assert summary.install_requirement(tmp_path / "mirror" / "action", "")[0] == "cleanframe-engine"
    assert summary.install_requirement(other / "action", "0.5.0")[0] == "cleanframe-engine==0.5.0"
    assert summary.install_requirement(other / "action", "v0.5.1")[0] == "cleanframe-engine==0.5.1"
    assert summary.install_requirement(ACTION_DIR, "latest")[0] == "cleanframe-engine"
    assert summary.install_requirement(other / "action", ">= 0.5, <0.6")[0] == "cleanframe-engine>=0.5,<0.6"
    for bad in ("0.3.1", "0.5.0; rm -rf /", "https://evil.example/x.whl", "../../x"):
        with pytest.raises(summary.ConfigError):
            summary.install_requirement(other / "action", bad)


def test_cleanframe_command_prefers_the_console_script(summary, tmp_path):
    python = tmp_path / "bin" / "python"
    python.parent.mkdir()
    assert summary.cleanframe_command(python) == [str(python), "-m", "cleanframe"]
    script = tmp_path / "bin" / ("cleanframe.exe" if os.name == "nt" else "cleanframe")
    script.write_text("", encoding="utf-8")
    assert summary.cleanframe_command(python) == [str(script)]


def test_parse_payload_rejects_non_objects(summary):
    assert summary.parse_payload('{"status": "ok"}') == {"status": "ok"}
    for text in ("", "not json", "[1, 2]", "Traceback (most recent call last):"):
        assert summary.parse_payload(text) is None
