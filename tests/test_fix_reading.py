"""Regressions for the read-time review findings (F-13, F-22, F-23, F-26a-c).

Each test failed on 0.3.1: a Shift-JIS file "decoded" as latin-1 mojibake, a NUL byte
silently truncated a value, a file with title rows had no way to be read, and footer
rows / ragged rows / skiprows past the end / hidden sheets went unmentioned.
"""

from __future__ import annotations

import sys
import warnings

import pandas as pd
import pytest

import cleanframe as cf
from cleanframe import readfix
from cleanframe.errors import CleanFrameError, CleanFrameWarning

openpyxl = pytest.importorskip("openpyxl")


def _caught(fn, *args, **kwargs):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        out = fn(*args, **kwargs)
    return out, [str(w.message) for w in caught if issubclass(w.category, CleanFrameWarning)]


# ---------------------------------------------------------------------------
# F-13: the encoding ladder must not turn multi-byte text into mojibake
# ---------------------------------------------------------------------------
def _sjis(rows: int = 6) -> bytes:
    body = "".join(f"{i},山田太郎{i},東京都千代田区{i},{i * 100}\n" for i in range(rows))
    return ("id,名前,住所,金額\n" + body).encode("shift_jis")


@pytest.mark.parametrize(
    ("codec", "text"),
    [
        ("shift_jis", "id,名前,金額\n1,山田太郎,1000\n2,鈴木花子,2000\n"),
        ("gbk", "id,姓名,城市\n" + "".join(f"{i},张三{i},北京市朝阳区\n" for i in range(8))),
        ("euc_kr", "id,이름,도시\n" + "".join(f"{i},김철수,서울특별시\n" for i in range(8))),
        ("big5", "id,姓名,城市\n" + "".join(f"{i},王小明{i},臺北市中正區\n" for i in range(8))),
        ("cp1251", "id,имя,город\n" + "".join(f"{i},Иван Петров,Москва\n" for i in range(8))),
    ],
)
def test_non_western_bytes_are_refused_not_decoded_as_mojibake(tmp_path, codec, text):
    p = tmp_path / f"{codec}.csv"
    p.write_bytes(text.encode(codec))
    with pytest.raises(CleanFrameError, match="encoding") as exc:
        cf.clean(p, mode="auto")
    assert "--encoding" in str(exc.value)
    with pytest.raises(CleanFrameError, match="encoding"):
        cf.report(p)


def test_an_explicit_encoding_gets_past_the_refusal_and_is_pinned(tmp_path):
    p = tmp_path / "sjis.csv"
    p.write_bytes(_sjis())
    result = cf.clean(p, mode="auto", encoding="shift_jis")
    assert list(result.dataframe.columns)[:3] == ["id", "名前", "住所"] or "名前" in " ".join(
        map(str, result.dataframe.columns)
    )
    assert result.recipe.read.get("encoding") == "shift_jis"
    # replay reads it the same way, with no re-detection
    again = cf.apply_recipe(p, result.recipe, check_drift=False)
    assert again.dataframe.shape == result.dataframe.shape


def test_western_text_with_smart_quotes_and_accents_is_still_cp1252(tmp_path):
    p = tmp_path / "west.csv"
    text = "name,city\nJosé,München\n“Élan” “Ünï”,Zürich\nCafé…,São Paulo\nÖzil,Göttingen\n"
    p.write_bytes(text.encode("cp1252"))
    result = cf.clean(p, mode="auto")
    assert result.recipe.read.get("encoding") == "cp1252"
    assert "München" in set(result.dataframe.iloc[:, 1])


def test_too_little_evidence_still_reads_but_says_it_guessed(tmp_path):
    p = tmp_path / "tiny.csv"
    p.write_bytes(b"name,city\nA,\x8f\x81\nB,x\n")
    result = cf.clean(p, mode="auto")
    assert result.dataframe.shape == (2, 2)
    assert any("latin-1" in line for line in result.log)


def test_looks_non_western_is_deterministic_on_a_corpus():
    assert readfix._looks_non_western(_sjis())
    assert not readfix._looks_non_western("Zürich,München,Köln,Göttingen,Ärger\n".encode("cp1252"))
    assert not readfix._looks_non_western(b"plain ascii only\n")


def test_candidates_are_listed_without_the_optional_detector(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "charset_normalizer", None)  # simulate it missing
    p = tmp_path / "sjis.csv"
    p.write_bytes(_sjis())
    with pytest.raises(CleanFrameError, match="cp932"):
        cf.clean(p)


# ---------------------------------------------------------------------------
# F-22: NUL bytes are refused by read_frame, as clean already did
# ---------------------------------------------------------------------------
def test_read_frame_refuses_nul_bytes_instead_of_truncating(tmp_path):
    p = tmp_path / "nul.csv"
    p.write_bytes(b"k,v\n1,a\x00b\n2,c\n")
    with pytest.raises(CleanFrameError, match="NUL"):
        cf.read_frame(p)
    with pytest.raises(CleanFrameError, match="NUL|binary"):
        cf.clean(p)


def test_utf16_text_is_not_mistaken_for_nul_garbage(tmp_path):
    p = tmp_path / "u16.csv"
    p.write_bytes("k,v\n1,a\n".encode("utf-16"))
    assert cf.read_frame(p, encoding="utf-16").shape == (1, 2)


# ---------------------------------------------------------------------------
# F-23: header_row for files with title rows above the header
# ---------------------------------------------------------------------------
TITLED = "Quarterly report,,\nprepared by finance,,\nid,name,amount\n1,a,5\n2,b,7\n"


def test_title_rows_error_suggests_header_row(tmp_path):
    p = tmp_path / "titled.csv"
    p.write_text(TITLED, encoding="utf-8")
    with pytest.raises(CleanFrameError, match=r"header_row=2"):
        cf.read_frame(p)


def test_single_field_title_row_suggests_header_row(tmp_path):
    p = tmp_path / "title1.csv"
    p.write_text("Sales export\nid,name,amount\n1,a,5\n2,b,7\n", encoding="utf-8")
    with pytest.raises(CleanFrameError, match=r"header_row=1"):
        cf.read_frame(p)


def test_read_frame_header_row_skips_the_title_rows(tmp_path):
    p = tmp_path / "titled.csv"
    p.write_text(TITLED, encoding="utf-8")
    df = cf.read_frame(p, header_row=2)
    assert list(df.columns) == ["id", "name", "amount"]
    assert len(df) == 2
    # skiprows still counts data rows after the header
    assert len(cf.read_frame(p, header_row=2, skiprows=1)) == 1


def test_clean_records_header_row_and_apply_replays_it(tmp_path):
    p = tmp_path / "titled.csv"
    p.write_text(TITLED, encoding="utf-8")
    result = cf.clean(p, mode="auto", header_row=2)
    assert result.recipe.read["header_row"] == 2
    assert result.recipe.read["blank_lines"] == 2  # so streaming skips the same lines
    assert list(result.dataframe.columns) == ["id", "name", "amount"]

    next_month = tmp_path / "next.csv"
    next_month.write_text(TITLED.replace("1,a,5", "9,z,1"), encoding="utf-8")
    replay = cf.apply_recipe(next_month, result.recipe, check_drift=False)
    assert list(replay.dataframe.columns) == ["id", "name", "amount"]

    saved = tmp_path / "r.yaml"
    result.recipe.save(saved)
    assert cf.Recipe.load(saved).read["header_row"] == 2  # a valid recipe read: key


def test_streaming_replay_honours_header_row(tmp_path):
    p = tmp_path / "titled.csv"
    p.write_text(TITLED, encoding="utf-8")
    result = cf.clean(p, mode="auto", header_row=2, text=True)
    out = tmp_path / "out.csv"
    cf.stream_apply(result.recipe, p, out, chunksize=1)
    assert list(pd.read_csv(out).columns)[:3] == ["id", "name", "amount"]


def test_header_row_is_validated(tmp_path):
    p = tmp_path / "t.csv"
    p.write_text(TITLED, encoding="utf-8")
    for bad in (-1, True, "2"):
        with pytest.raises(CleanFrameError, match="header_row"):
            cf.read_frame(p, header_row=bad)
    with pytest.raises(CleanFrameError, match="file inputs only"):
        cf.clean(pd.DataFrame({"a": [1]}), header_row=1)
    pq = tmp_path / "t.json"
    pq.write_text('[{"a": 1}]', encoding="utf-8")
    with pytest.raises(CleanFrameError, match="CSV and Excel"):
        cf.read_frame(pq, header_row=1)


def _titled_xlsx(path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Quarterly report", None, None])
    ws.append(["id", "name", "amount"])
    ws.append([1, "a", 5])
    ws.append([2, "b", 7])
    wb.save(path)


def test_excel_title_row_warns_and_header_row_fixes_it(tmp_path):
    p = tmp_path / "titled.xlsx"
    _titled_xlsx(p)
    df, notes = _caught(cf.read_frame, p)
    assert any("header_row=1" in n for n in notes)
    fixed, notes = _caught(cf.read_frame, p, header_row=1)
    assert list(fixed.columns) == ["id", "name", "amount"]
    assert not notes
    with_cols = cf.read_frame(p, header_row=1, columns=["id", "amount"])
    assert list(with_cols.columns) == ["id", "amount"]


# ---------------------------------------------------------------------------
# F-26: quiet shape problems become warnings; skiprows past the end raises
# ---------------------------------------------------------------------------
def test_short_rows_warn_but_are_kept(tmp_path):
    p = tmp_path / "ragged.csv"
    p.write_text("a,b,c\n1,2,3\n4,5\n6,7,8\n", encoding="utf-8")
    df, notes = _caught(cf.read_frame, p)
    assert len(df) == 3
    assert any("fewer fields" in n and "record 3" in n for n in notes)


def test_footer_total_row_warns_but_is_never_dropped(tmp_path):
    p = tmp_path / "footer.csv"
    p.write_text("item,amt\nx,1\ny,2\nGrand Total,3\n", encoding="utf-8")
    df, notes = _caught(cf.read_frame, p)
    assert len(df) == 3 and df.iloc[-1, 0] == "Grand Total"
    assert any("footer" in n for n in notes)


def test_clean_files_raise_no_shape_warning(tmp_path):
    p = tmp_path / "ok.csv"
    p.write_text("item,amt\nTotal Wine,1\nx,2\n", encoding="utf-8")
    _, notes = _caught(cf.read_frame, p)
    assert notes == []


def test_skiprows_past_the_end_raises(tmp_path):
    p = tmp_path / "one.csv"
    p.write_text("a,b\n1,2\n", encoding="utf-8")
    with pytest.raises(CleanFrameError, match="skiprows"):
        cf.read_frame(p, skiprows=5)
    with pytest.raises(CleanFrameError, match="skiprows"):
        cf.clean(p, skiprows=5)
    assert len(cf.read_frame(p, skiprows=0)) == 1


def test_uncached_formulas_and_merged_cells_warn(tmp_path):
    p = tmp_path / "f.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "S"
    ws.append(["a", "b"])
    ws.append([1, 2])
    ws["A3"] = "=SUM(A2:B2)"  # written by a script, never calculated
    ws.merge_cells("A4:B4")
    ws["A4"] = "merged"
    wb.save(p)
    _, notes = _caught(cf.read_frame, p)
    joined = " ".join(notes)
    assert "no cached value" in joined and "merged range" in joined


def test_hidden_sheet_stays_hidden_after_write_back(tmp_path):
    src = tmp_path / "wb.xlsx"
    wb = openpyxl.Workbook()
    a = wb.active
    a.title = "Visible"
    a.append(["x", "y"])
    a.append([1, 2])
    b = wb.create_sheet("Secret")
    b.append(["x", "y"])
    b.append([3, 4])
    b.sheet_state = "hidden"
    wb.save(src)

    result = cf.clean_workbook(src, mode="auto")
    out = tmp_path / "out.xlsx"
    result.save_data(out)
    states = {ws.title: ws.sheet_state for ws in openpyxl.load_workbook(out).worksheets}
    assert states == {"Visible": "visible", "Secret": "hidden"}

    replay = cf.apply_workbook(src, result.recipe)
    out2 = tmp_path / "out2.xlsx"
    replay.save_data(out2)
    assert openpyxl.load_workbook(out2)["Secret"].sheet_state == "hidden"
