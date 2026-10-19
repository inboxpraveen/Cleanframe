"""`cleanframe demo` must work from a plain `pip install` (no repository clone)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from cleanframe.demo import DEMO_FILES, demo_file, run_demo
from cleanframe.errors import CleanFrameError

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


@pytest.mark.parametrize("name", DEMO_FILES)
def test_bundled_demo_files_match_the_examples_folder(name):
    if not EXAMPLES.is_dir():
        pytest.skip("examples/ is not part of this checkout")
    assert demo_file(name).read_bytes() == (EXAMPLES / name).read_bytes()


def test_unknown_demo_file_is_refused():
    with pytest.raises(CleanFrameError, match="Unknown demo file"):
        demo_file("secrets.csv")


def test_demo_runs_report_clean_drift_and_patch(tmp_path):
    run = run_demo(tmp_path / "demo")
    for key in ("report", "recipe", "clean", "code", "diff", "patched"):
        assert Path(run.files[key]).is_file(), key
    assert run.quality is not None and 0 <= run.quality.score <= 100
    assert run.quarantined == 1  # the one invalid email is held back, not deleted
    # the quarantined row is the only one missing from the output: it is no duplicate
    assert (run.duplicates, run.dropped) == (0, 0)
    assert run.clean_summary["rows_dropped"] == run.quarantined
    assert "Amt (INR)" in run.drift_text
    assert any("Amt (INR)" in note for note in run.patch_notes)
    assert run.replay_rows == 4
    with pytest.raises(CleanFrameError, match="--overwrite"):
        run_demo(tmp_path / "demo")
    assert run_demo(tmp_path / "demo", overwrite=True).replay_rows == 4


def test_demo_cli_json_summary(tmp_path):
    out = subprocess.run(
        [sys.executable, "-m", "cleanframe", "demo", "--out-dir", str(tmp_path / "d"), "--json"],
        capture_output=True, text=True, encoding="utf-8",
        env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert out.returncode == 0, out.stderr
    summary = json.loads(out.stdout)
    assert summary["status"] == "ok" and summary["command"] == "demo"
    assert Path(summary["outputs"]["recipe"]).is_file()
    assert "Schema drift detected" in out.stderr
    assert "1 row(s) quarantined" in out.stderr
    assert "duplicate row(s) dropped" not in out.stderr
