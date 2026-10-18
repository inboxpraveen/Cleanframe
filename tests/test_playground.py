"""The in-browser playground's Python glue (``playground/app.py``) under plain CPython.

The page runs this exact module inside Pyodide; these tests pin its contract so the
page and the library cannot drift apart: JSON-safe and deterministic output, a Mess
Score that is exactly ``100 - quality_score``, a bounded change list, a share card
that never carries column names, cell values or the file name, and a clean error
dict (never an exception) for unusable uploads.
"""

from __future__ import annotations

import importlib.util
import io
import json
import re
import tempfile
from pathlib import Path

import pytest

import cleanframe as cf

ROOT = Path(__file__).resolve().parent.parent
PLAYGROUND = ROOT / "playground"
APP = PLAYGROUND / "app.py"
SAMPLE = ROOT / "examples" / "messy_customers.csv"

# tests/ ships in the sdist but playground/ does not.
pytestmark = pytest.mark.skipif(not APP.is_file(), reason="playground/ is not in this checkout")


@pytest.fixture(scope="module")
def app():
    spec = importlib.util.spec_from_file_location("cf_playground_under_test", APP)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def sample_result(app):
    return app.analyze(SAMPLE.read_bytes(), "messy_customers.csv")


def _strict_json(obj) -> str:
    # JSON.parse in the browser rejects NaN/Infinity, so the encoding must be strict.
    return json.dumps(obj, ensure_ascii=False, allow_nan=False, sort_keys=True)


# ---------------------------------------------------------------------------
# the happy path
# ---------------------------------------------------------------------------
def test_sample_result_is_complete_and_strict_json(app, sample_result):
    r = sample_result
    assert r["ok"] is True
    assert r["api"] == app.API_VERSION
    _strict_json(r)
    assert r["file"] == {
        "name": "messy_customers.csv", "kind": "csv", "bytes": SAMPLE.stat().st_size,
        "rows": 6, "columns": 6, "sheet": None,
    }
    assert r["recipe_yaml"].startswith("# CleanFrame recipe")
    assert "def clean(" in r["code"]
    assert r["cleaned_csv"].splitlines()[0].startswith("customer_name,")
    assert "<html" in r["report_html"].lower() and "<html" in r["clean_report_html"].lower()
    assert r["kinds"] and r["issues"] and r["columns"]
    labels = {k["kind"]: k["text"] for k in r["kinds"]}
    assert labels["mixed_date_formats"] == "3 date formats in one column"
    assert labels["category_variants"] == "5 spellings of 3 categories"


def test_analyze_json_round_trips(app):
    def no_bare_constants(token):  # JSON.parse rejects NaN / Infinity tokens
        raise AssertionError(f"non-standard JSON constant {token}")

    text = app.analyze_json(SAMPLE.read_bytes(), "messy_customers.csv")
    assert json.loads(text, parse_constant=no_bare_constants)["ok"] is True


def test_same_bytes_give_identical_output(app, sample_result):
    again = app.analyze(SAMPLE.read_bytes(), "messy_customers.csv")
    assert _strict_json(again) == _strict_json(sample_result)


def test_no_private_temp_path_leaks(sample_result):
    blob = _strict_json(sample_result)
    assert "cfpg-" not in blob
    assert tempfile.gettempdir() not in blob
    assert "input.csv" not in blob


def test_mess_score_is_100_minus_cleanframe_quality(sample_result):
    quality = cf.clean(str(SAMPLE)).quality
    score = sample_result["score"]
    assert 0 <= score["mess"] <= 100
    assert score["quality"] == quality.score
    assert score["mess"] == 100 - quality.score
    assert score["grade"] == quality.grade
    # The per-issue breakdown re-uses quality.py's helpers; it must add up exactly.
    assert score["penalty"] == pytest.approx(quality.penalty, abs=1e-3)
    assert sum(k["penalty"] for k in sample_result["kinds"]) + score["duplicate_penalty"] == pytest.approx(
        score["penalty"], abs=1e-3
    )


def test_change_list_is_bounded_and_covers_every_column(app):
    rows = ["Name,City,Joined"]
    for i in range(1000):
        rows.append(f"  person {i} ,{'MUMBAI' if i % 2 else 'mumbai '},{'2024-01-0' + str(i % 9 + 1) if i % 3 else '05/02/2024'}")
    data = ("\n".join(rows) + "\n").encode()
    r = app.analyze(data, "many.csv")
    assert r["ok"] is True
    assert r["diff"]["changed_cells"] > app.MAX_CHANGES
    assert len(r["changes"]) == r["diff"]["shown"] <= app.MAX_CHANGES
    changed = {c["output_name"] for c in r["columns"] if c["changed"]}
    assert changed and changed == {ch["column"] for ch in r["changes"]}


# ---------------------------------------------------------------------------
# privacy of the share card
# ---------------------------------------------------------------------------
SENTINEL_COLUMNS = ["Zqx Client Label", "Vrk Joined On", "Plm Contact Mail", "Hjt Dial", "Tqw Branch Town"]


def _sentinel_csv() -> bytes:
    lines = [",".join(SENTINEL_COLUMNS)]
    towns = ["Quorvex", "quorvex ", "QUORVEX", "Blemtide", "blemtide", "Snarlow"]
    dates = ["31/01/2024", "2024-02-15", "1 Jan 2024", "15/03/2024"]
    for i in range(24):
        lines.append(",".join([
            f"  yorvalindra {chr(97 + i % 26)}zzq ",
            dates[i % len(dates)],
            "not-an-xyzmail" if i % 7 == 0 else f"Glimmer{i}@Brovex.example ",
            "N/A" if i % 5 == 0 else f"+91 98765 4{i:04d}",
            towns[i % len(towns)],
        ]))
    lines.append(lines[1])  # an exact duplicate row
    return ("\n".join(lines) + "\n").encode()


def test_share_card_carries_no_names_values_or_filename(app):
    data = _sentinel_csv()
    r = app.analyze(data, "topsecret_clientlist.csv")
    assert r["ok"] is True
    assert len(r["kinds"]) >= 4, "the fixture should trigger several detectors"
    share = r["share"]
    payload = _strict_json(share).lower()
    forbidden = {c.lower() for c in SENTINEL_COLUMNS} | {"topsecret", "clientlist"}
    # every cell value (and every word of one) of 4+ characters
    for line in data.decode().splitlines()[1:]:
        for cell in line.split(","):
            forbidden |= {w.lower() for w in re.split(r"[\s@.,]+", cell) if len(w) >= 4}
    leaks = sorted(f for f in forbidden if f and f in payload)
    assert not leaks, f"share payload leaks {leaks}"
    # The card shows only the precomputed per-kind phrases, and the post links the repo.
    assert share["items"] == [k["text"] for k in r["kinds"][: app.SHARE_ITEMS]]
    assert share["mess"] == r["score"]["mess"]
    assert share["text"].endswith(app.REPO_URL)
    assert len(share["text"]) - len(app.REPO_URL) + 23 <= 280  # X counts a URL as 23


def test_share_text_with_no_problems(app):
    r = app.analyze(b"id,amount\n1,10\n2,12\n3,11\n4,13\n", "tidy.csv")
    assert r["ok"] is True
    assert r["share"]["items"] in (["No problems found"], [k["text"] for k in r["kinds"][:4]])


# ---------------------------------------------------------------------------
# unusable uploads: always a clean error dict, never an exception
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("data", "name"),
    [
        (b"", "empty.csv"),
        (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x01\x00", "photo.csv"),
        ("name,city\n日本語,東京\n山田,大阪\n".encode("shift_jis"), "jp.csv"),
        (bytes(range(128, 256)) * 20, "noise.csv"),
        (b"a,b,c\n", "header_only.csv"),
        (b"\n\n\n", "blank.csv"),
        (b"PK\x03\x04" + b"\x00" * 40, "zipped.csv"),
        (b"not a workbook", "book.xlsx"),
        (b"\xd0\xcf\x11\xe0", "legacy.xls"),
        (None, "none.csv"),
    ],
    ids=["empty", "binary", "shift-jis", "noise", "header-only", "blank-lines", "zip-as-csv",
         "fake-xlsx", "legacy-xls", "none"],
)
def test_unusable_input_returns_error_dict(app, data, name):
    r = app.analyze(data, name)
    assert r["ok"] is False
    assert isinstance(r["error"], str) and r["error"].strip()
    assert r["error_kind"] in ("input", "internal")
    blob = _strict_json(r)
    assert "cfpg-" not in blob and "input.csv" not in blob


def test_oversized_upload_is_refused_before_parsing(app, monkeypatch):
    monkeypatch.setattr(app, "MAX_BYTES", 64)
    r = app.analyze(SAMPLE.read_bytes(), "messy_customers.csv")
    assert r["ok"] is False and "limit" in r["error"]


def test_western_non_utf8_is_decoded_with_a_note(app):
    # CleanFrame decodes single-byte Western files (and says so) rather than refusing.
    data = "name,city\nJosé,Zürich\nRenée,Köln\nAnna,Wien\n".encode("cp1252")
    r = app.analyze(data, "latin.csv")
    assert r["ok"] is True
    assert any("cp1252" in note for note in r["notes"])
    assert "Zürich" in r["cleaned_csv"]


def test_xlsx_upload(app):
    pd = pytest.importorskip("pandas")
    pytest.importorskip("openpyxl")
    frame = pd.read_csv(SAMPLE, dtype=str, keep_default_na=False)
    one = io.BytesIO()
    frame.to_excel(one, index=False)
    r = app.analyze(one.getvalue(), "messy_customers.xlsx")
    assert r["ok"] is True and r["file"]["kind"] == "xlsx"
    assert r["score"]["mess"] > 0

    two = io.BytesIO()
    with pd.ExcelWriter(two) as writer:
        frame.to_excel(writer, sheet_name="Customers", index=False)
        frame.to_excel(writer, sheet_name="Copy", index=False)
    r = app.analyze(two.getvalue(), "two_sheets.xlsx")
    assert r["ok"] is True and r["file"]["sheet"] == "Customers"
    assert any("2 sheets" in note for note in r["notes"])


# ---------------------------------------------------------------------------
# drift guards for the static files
# ---------------------------------------------------------------------------
def test_sample_copy_matches_examples():
    assert (PLAYGROUND / "sample" / "messy_customers.csv").read_bytes() == SAMPLE.read_bytes()


def test_pyodide_version_is_pinned_consistently():
    worker = (PLAYGROUND / "worker.js").read_text(encoding="utf-8")
    version = re.search(r'const PYODIDE_VERSION = "([^"]+)"', worker).group(1)
    assert re.fullmatch(r"\d+\.\d+\.\d+", version)
    html = (PLAYGROUND / "index.html").read_text(encoding="utf-8")
    csp = re.search(r'http-equiv="Content-Security-Policy" content="([^"]+)"', html).group(1)
    assert f"https://cdn.jsdelivr.net/pyodide/v{version}/full/" in csp
    assert "'unsafe-eval'" not in csp
    assert f"/pyodide/v{version}/" in html.split("<pre class=\"origins\"", 1)[1]
    package = json.loads((PLAYGROUND / "tests" / "package.json").read_text(encoding="utf-8"))
    assert package["devDependencies"]["pyodide"] == version


def test_scrubber_hides_every_spelling_of_the_temp_path(app, tmp_path):
    workdir = tmp_path / "cfpg-abc123"
    workdir.mkdir()
    stored = workdir / "input.csv"
    scrub = app._scrubber(workdir, stored, "mine.csv")
    for text in (str(stored), stored.as_posix(), str(stored.resolve()), f"see {workdir.resolve()}/x.tmp now"):
        out = scrub(f"Could not read {text}.")
        assert "cfpg-abc123" not in out and "input.csv" not in out, out
