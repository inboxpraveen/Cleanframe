"""Render the README's brand images from ``docs/assets/Logo.svg`` and a real demo run.

    python scripts/render_brand_assets.py    # writes docs/assets/Hero.png, Hero-dark.png, Logo.png

The hero is not a mock-up. Every value in it (cells, renames, op names, scores, counts,
the quarantine reason and the drift findings) comes from running the bundled demo, the
same run as ``cleanframe demo``. Re-render it when the demo data or the engine changes.

``Logo.svg`` is the one source of the mark. ``Logo.png`` is a raster of it for places that
cannot take SVG, and the playground (``favicon.svg``, the top bar, ``drawLogo`` in
``app.js``) copies its geometry and colours.

Needs ``pip install playwright`` and a Chromium-family browser: an installed Edge or Chrome
is used, else run ``playwright install chromium``. Inter and JetBrains Mono load from
Google Fonts, so it needs network access; it refuses to render with fallback fonts.
"""

from __future__ import annotations

import csv
import html
import io
import re
import sys
import tempfile
import warnings
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "docs" / "assets"
sys.path.insert(0, str(ROOT))

import cleanframe as cf  # noqa: E402
from cleanframe.cli import EXIT_DRIFT  # noqa: E402
from cleanframe.demo import run_demo  # noqa: E402
from cleanframe.errors import DriftError  # noqa: E402

#: The source columns the hero shows (the demo file has six). The recipe excerpt follows.
SHOWN = ("Customer Name", "Signup Date", "Amount")
#: The validation rule whose quarantine the hero shows.
SHOWN_RULE = {"column": "email", "check": "valid_email"}
WIDTH = 1200  # CSS pixels; rendered at 2x


# ---------------------------------------------------------------------------
# data: one real demo run
# ---------------------------------------------------------------------------
def _read_csv(path: Path) -> tuple[list[str], list[list[str]]]:
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    return rows[0], rows[1:]


def _held(reason: str | None, raw: dict, source_of: dict) -> tuple[str, str, str] | None:
    """(column, check, raw value) for a quarantine reason such as ``email:valid_email``."""
    if not reason:
        return None
    column, _, check = reason.partition(":")
    return column, check, raw[source_of[column]]


def collect(workdir: Path) -> dict:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        run = run_demo(workdir)
        messy, schema = run.files["messy_customers.csv"], run.files["customer.schema.yaml"]
        result = cf.clean(messy, target_schema=schema)  # the call run_demo makes
        after_quality = cf.report(run.files["clean"]).quality
        recipe = cf.load_recipe(run.files["recipe"])
        try:
            cf.apply_recipe(run.files["next_month_customers.csv"], recipe)
        except DriftError as exc:
            findings = exc.report.findings
        else:
            raise SystemExit("The demo's next-month file no longer drifts; update the hero.")

    head, raw = _read_csv(messy)
    clean_head, clean_rows = _read_csv(run.files["clean"])
    diff = result.diff
    if diff.truncated:
        raise SystemExit("The demo diff is truncated; the hero would under-report changes.")
    renames = diff.renamed_columns
    idx = [head.index(c) for c in SHOWN]
    cidx = [clean_head.index(renames[c]) for c in SHOWN]
    # strict: the written CSV and the result frame must hold the same rows
    after = dict(zip(result.dataframe.index, ([r[i] for i in cidx] for r in clean_rows), strict=True))
    quarantine = result.quarantine
    reasons = dict(zip(quarantine.index, quarantine["_cf_quarantine_reason"], strict=True))
    source_of = {new: old for old, new in renames.items()}
    changed = {(c.row_id, c.column) for c in diff.changes}

    text = run.files["recipe"].read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    rules = [r for r in data["validate"] if all(r.get(k) == v for k, v in SHOWN_RULE.items())]
    excerpt = {"columns": {c: data["columns"][c] for c in SHOWN}, "validate": rules}
    body = yaml.safe_dump(excerpt, sort_keys=False, allow_unicode=True,
                          default_flow_style=False, width=100)
    lines = set(text.splitlines())
    missing = [ln for ln in body.splitlines() if ln not in lines]
    if missing:  # the excerpt must be lines of the real file, not a paraphrase
        raise SystemExit(f"Recipe excerpt lines not in the recipe: {missing}")
    comment = (f"# excerpt: {len(SHOWN)} of {len(data['columns'])} columns, "
               f"{len(rules)} of {len(data['validate'])} rules")

    by_kind = {}
    for f in findings:
        by_kind.setdefault(f.kind, f)
    renamed, gone, fmt = by_kind["renamed_column"], by_kind["missing_column"], by_kind["format_drift"]

    s = result.diff.summary()
    return {
        "file": messy.name,
        "clean_file": run.files["clean"].name,
        "recipe_file": run.files["recipe"].name,
        "next_file": run.files["next_month_customers.csv"].name,
        "head": list(SHOWN),
        "clean_head": [renames[c] for c in SHOWN],
        "rows": [
            {
                "n": i + 1,
                "before": [r[j] for j in idx],
                "after": after.get(i),
                "changed": [(i, renames[c]) in changed for c in SHOWN],
                "reason": reasons.get(i),
                "held": _held(reasons.get(i), dict(zip(head, r, strict=True)), source_of),
            }
            for i, r in enumerate(raw)
        ],
        "before_q": run.quality,
        "after_q": after_quality,
        "cells": s["changed_cells"],
        "columns": s["changed_columns"],
        "quarantined": len(quarantine),
        "yaml": comment + "\n" + body.rstrip("\n"),
        "generated_by": data["meta"]["generated_by"],
        "drift": {
            "missing": gone.column,
            "new": renamed.column,
            "match": round(100 * renamed.evidence["score"]),
            "match_to": renamed.evidence["match"],
            "date": fmt.evidence["examples"][0],
            "exit": EXIT_DRIFT,
        },
    }


# ---------------------------------------------------------------------------
# html
# ---------------------------------------------------------------------------
CSS = """
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@500;700&display=block');
:root {
  --bg: #f6f7fb; --surface: #ffffff; --surface-2: #f0f2f8; --ink: #0d1530; --ink-2: #39445e;
  --muted: #5b6680; --line: #e2e6ef; --accent: #4338ca;
  --grad-text: linear-gradient(100deg, #0e7490 0%, #4f46e5 55%, #7c3aed 100%);
  --del-bg: #fdecec; --del-ink: #8f1d1d; --add-bg: #e6f5ea; --add-ink: #115e27;
  --q-bg: #fff4dc; --q-ink: #7a4a00; --stop: #c2410c; --stop-ink: #9a3412;
  --code-bg: #f7f8fc; --code-ink: #1b2440; --yaml-key: #4338ca; --yaml-str: #0f6b3f; --yaml-com: #6b7690;
  --mark-c: #06b6d4; --mark-s: #5a6780;
  --good-bg: #e6f5ea; --good-ink: #115e27; --fair-bg: #fff4dc; --fair-ink: #7a4a00;
  --glow-a: rgba(6, 182, 212, .10); --glow-b: rgba(139, 92, 246, .10);
  --shadow: 0 1px 2px rgba(13, 21, 48, .06), 0 8px 24px -12px rgba(13, 21, 48, .18);
}
:root[data-theme="dark"] {
  --bg: #090d18; --surface: #10162a; --surface-2: #161e36; --ink: #e8ecf6; --ink-2: #bcc5d9;
  --muted: #8f9ab4; --line: #212a45; --accent: #a5b4fc;
  --grad-text: linear-gradient(100deg, #22d3ee 0%, #818cf8 55%, #c4b5fd 100%);
  --del-bg: rgba(239, 68, 68, .16); --del-ink: #fecaca; --add-bg: rgba(34, 197, 94, .15); --add-ink: #bbf7d0;
  --q-bg: rgba(250, 178, 25, .13); --q-ink: #fde68a; --stop: #fb923c; --stop-ink: #fdba74;
  --code-bg: #0c1224; --code-ink: #dfe5f3; --yaml-key: #a5b4fc; --yaml-str: #86efac; --yaml-com: #7d88a3;
  --mark-c: #22d3ee; --mark-s: #64748b;
  --good-bg: rgba(34, 197, 94, .15); --good-ink: #bbf7d0; --fair-bg: rgba(250, 178, 25, .14); --fair-ink: #fde68a;
  --glow-a: rgba(6, 182, 212, .13); --glow-b: rgba(139, 92, 246, .15);
  --shadow: 0 1px 2px rgba(0, 0, 0, .3), 0 10px 30px -14px rgba(0, 0, 0, .6);
}
* { box-sizing: border-box; }
html, body { margin: 0; background: transparent; }
body { font: 16px/1.5 Inter, sans-serif; color: var(--ink); -webkit-font-smoothing: antialiased; }
code, pre, .mono { font-family: "JetBrains Mono", monospace; }

#hero {
  width: __WIDTH__px; padding: 26px 36px 34px; border-radius: 22px; border: 1px solid var(--line);
  background:
    radial-gradient(640px 360px at 0% 0%, var(--glow-a), transparent 70%),
    radial-gradient(720px 420px at 100% 100%, var(--glow-b), transparent 70%),
    var(--bg);
}
.top { display: flex; align-items: center; justify-content: space-between; margin-bottom: 20px; }
.brand { display: flex; align-items: center; gap: 11px; font-size: 22px; font-weight: 700; letter-spacing: -.015em; }
.brand svg { width: 30px; height: 30px; }
.brand .c { fill: var(--mark-c); } .brand .s { fill: var(--mark-s); }
.brand .fr { background: var(--grad-text); -webkit-background-clip: text; background-clip: text; color: transparent; }
.src { font-size: 15px; color: var(--muted); }
.src code { font-size: 14px; color: var(--ink); background: var(--surface); border: 1px solid var(--line);
  border-radius: 8px; padding: 3px 9px; margin-left: 4px; }

.grid { display: grid; grid-template-columns: 612px 1fr; gap: 26px; align-items: stretch; }
.col { display: flex; flex-direction: column; gap: 0; min-width: 0; }
.col.right { gap: 18px; }
.col.right .recipe { flex: 1; display: flex; flex-direction: column; }
.col.right .recipe pre { flex: 1; }
.card { background: var(--surface); border: 1px solid var(--line); border-radius: 14px; box-shadow: var(--shadow); overflow: hidden; }
.card-h { display: flex; align-items: center; gap: 10px; padding: 11px 16px; border-bottom: 1px solid var(--line); }
.num { font: 700 13px/1 "JetBrains Mono", monospace; color: var(--accent); white-space: nowrap; }
.file { font-size: 15px; font-weight: 600; color: var(--ink); }
.pill { margin-left: auto; font-size: 13px; font-weight: 600; padding: 3px 10px; border-radius: 999px; white-space: nowrap; }
.pill.good { background: var(--good-bg); color: var(--good-ink); }
.pill.fair { background: var(--fair-bg); color: var(--fair-ink); }
.pill.plain { background: var(--surface-2); color: var(--ink-2); }

table { width: 100%; border-collapse: collapse; font-size: 16.5px; font-variant-numeric: tabular-nums; }
th { text-align: left; font-size: 13px; font-weight: 600; color: var(--muted); background: var(--surface-2);
  padding: 7px 14px; border-bottom: 1px solid var(--line); white-space: nowrap; }
th.mono { font-size: 12.5px; font-weight: 500; }
td { padding: 8px 14px; border-bottom: 1px solid var(--line); white-space: nowrap; color: var(--ink-2); height: 44px; }
tr:last-child td { border-bottom: 0; }
td.n, th.n { width: 1%; color: var(--muted); font-size: 13px; padding-right: 4px; }
.r { text-align: right; }
.val { border-radius: 6px; padding: 2px 7px; margin: 0 -7px; }
.val.before { background: var(--del-bg); color: var(--del-ink); }
.val.after { background: var(--add-bg); color: var(--add-ink); }
.ws { opacity: .5; }
tr.q td { background: var(--q-bg); color: var(--q-ink); font-size: 14.5px; }
tr.q td.n { color: var(--q-ink); }
tr.q code { font-size: 13px; font-weight: 500; }
tr.q svg { width: 15px; height: 15px; vertical-align: -2px; margin-right: 6px; }

.framed { position: relative; }
.framed > svg { position: absolute; width: 22px; height: 22px; }
.framed > .tl { left: -9px; top: -9px; } .framed > .tr { right: -9px; top: -9px; transform: scaleX(-1); }
.framed > .bl { left: -9px; bottom: -9px; transform: scaleY(-1); } .framed > .br { right: -9px; bottom: -9px; transform: scale(-1); }
.flow { display: flex; align-items: center; justify-content: center; gap: 12px; height: 62px; padding-bottom: 4px; font-size: 15px; color: var(--ink-2); white-space: nowrap; }
.flow svg { width: 22px; height: 22px; flex: none; }
.flow b { color: var(--ink); font-weight: 600; }
.flow .sep { color: var(--muted); }

pre.yaml { margin: 0; padding: 13px 18px; font-size: 13.5px; line-height: 1.45; color: var(--code-ink); background: var(--code-bg); }
pre.yaml .k { color: var(--yaml-key); } pre.yaml .s { color: var(--yaml-str); } pre.yaml .cm { color: var(--yaml-com); }
.card-f { padding: 10px 16px; border-top: 1px solid var(--line); font-size: 14px; color: var(--ink-2); line-height: 1.45; }

.drift .card-h .num { color: var(--stop); }
.drift ul { margin: 0; padding: 11px 16px 2px 36px; font-size: 14.5px; color: var(--ink-2); }
.drift li { margin-bottom: 3px; }
.drift li code { font-size: 13px; color: var(--ink); }
.drift .stop { display: flex; align-items: center; gap: 8px; margin: 6px 16px 13px; font-size: 14.5px; font-weight: 600; color: var(--stop-ink); }
.drift .stop svg { width: 18px; height: 18px; flex: none; }
"""

ARROW_DOWN = ('<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><defs><linearGradient id="ad" x1="0" y1="0" '
              'x2="0" y2="1"><stop offset="0" stop-color="#06b6d4"/><stop offset="1" stop-color="#8b5cf6"/>'
              '</linearGradient></defs><path d="M12 3v17m-7-7 7 7 7-7" stroke="url(#ad)" stroke-width="2.4" '
              'stroke-linecap="round" stroke-linejoin="round"/></svg>')
PAUSE = ('<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="7" fill="none" stroke="currentColor" '
         'stroke-width="1.6"/><path d="M6.2 5v6M9.8 5v6" stroke="currentColor" stroke-width="1.6" '
         'stroke-linecap="round"/></svg>')
STOP = ('<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8.2 2h7.6L22 8.2v7.6L15.8 22H8.2L2 15.8V8.2z" '
        'fill="currentColor"/><path d="M8 12h8" stroke="#fff" stroke-width="2.6" stroke-linecap="round"/></svg>')


def corner(pos: str, color: str) -> str:
    """One corner bracket of the mark, drawn top-left and mirrored into place by CSS."""
    return (f'<svg class="{pos}" viewBox="0 0 22 22" aria-hidden="true"><path d="M2 20V2h18" fill="none" '
            f'stroke="{color}" stroke-width="4"/></svg>')


FRAME = "".join(corner(p, c) for p, c in (("tl", "#06b6d4"), ("tr", "#3b82f6"), ("bl", "#3b82f6"), ("br", "#8b5cf6")))


def mark_svg() -> str:
    """The mark from Logo.svg, recoloured through CSS variables for the hero's theme."""
    svg = (ASSETS / "Logo.svg").read_text(encoding="utf-8")
    svg = re.sub(r"<title>.*?</title>|<style>.*?</style>", "", svg, flags=re.S)
    root = re.match(r"<svg[^>]*>", svg).group(0)
    bare = re.sub(r'\s(width|height|role|aria-label)="[^"]*"', "", root)
    return svg.replace(root, bare.replace("<svg", '<svg aria-hidden="true"', 1), 1)


def cell(value: str, kind: str | None) -> str:
    """A cell value; leading/trailing spaces drawn as dots, as the playground does."""
    m = re.match(r"^(\s*)(.*?)(\s*)$", value, flags=re.S)
    lead, core, trail = m.groups()
    dots = lambda s: f'<span class="ws">{"·" * len(s)}</span>' if s else ""  # noqa: E731
    inner = dots(lead) + html.escape(core) + dots(trail)
    return f'<span class="val {kind}">{inner}</span>' if kind else inner


def yaml_html(text: str) -> str:
    out = []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            out.append(f'<span class="cm">{html.escape(line)}</span>')
            continue
        m = re.match(r"^(\s*(?:- )?)([^:'\"]+?)(:)(\s.*|)$", line)
        if m:
            lead, key, colon, rest = m.groups()
            out.append(html.escape(lead) + f'<span class="k">{html.escape(key)}</span>{colon}' + _yaml_value(rest))
        else:
            li = re.match(r"^(\s*- )(.*)$", line)
            out.append(html.escape(li.group(1)) + _yaml_value(li.group(2)) if li else html.escape(line))
    return "\n".join(out)


def _yaml_value(rest: str) -> str:
    v = rest.strip()
    if v.startswith(("'", '"')):
        return html.escape(rest[: len(rest) - len(rest.lstrip())]) + f'<span class="s">{html.escape(v)}</span>'
    return html.escape(rest)


def grade_pill(q) -> str:
    tone = "good" if q.grade in ("A", "B") else "fair"
    return f'<span class="pill {tone}">Quality {q.score}/100 · {html.escape(q.grade)}</span>'


def build(d: dict, theme: str) -> str:
    num_col = 2  # Amount is right-aligned in both tables
    th = lambda names, cls="": "".join(  # noqa: E731
        f'<th class="{cls}{" r" if i == num_col else ""}">{html.escape(n)}</th>' for i, n in enumerate(names))

    before_rows, after_rows = [], []
    for row in d["rows"]:
        tds = "".join(
            f'<td class="{"r" if i == num_col else ""}">{cell(v, "before" if row["changed"][i] else None)}</td>'
            for i, v in enumerate(row["before"]))
        before_rows.append(f'<tr><td class="n">{row["n"]}</td>{tds}</tr>')
        if row["held"]:
            col, check, value = row["held"]
            after_rows.append(
                f'<tr class="q"><td class="n">{row["n"]}</td><td colspan="3">{PAUSE}Held in quarantine, '
                f'not deleted: <code>{html.escape(col)}</code> “{html.escape(value)}” fails '
                f'<code>{html.escape(check)}</code></td></tr>')
        else:
            tds = "".join(
                f'<td class="{"r" if i == num_col else ""}">{cell(v, "after" if row["changed"][i] else None)}</td>'
                for i, v in enumerate(row["after"]))
            after_rows.append(f'<tr><td class="n">{row["n"]}</td>{tds}</tr>')

    dr = d["drift"]
    rows_word = "row" if d["quarantined"] == 1 else "rows"
    return f"""<!doctype html>
<html data-theme="{theme}"><head><meta charset="utf-8"><style>{CSS.replace("__WIDTH__", str(WIDTH))}</style></head>
<body><div id="hero">
  <div class="top">
    <div class="brand">{mark_svg()}<span>Clean<span class="fr">Frame</span></span></div>
    <div class="src">Real CleanFrame output on the <code>cleanframe demo</code> files</div>
  </div>
  <div class="grid">
    <div class="col">
      <div class="card">
        <div class="card-h"><span class="num">01 · Detect</span><span class="file">{html.escape(d["file"])}</span>{grade_pill(d["before_q"])}</div>
        <table><thead><tr><th class="n">#</th>{th(d["head"])}</tr></thead><tbody>{"".join(before_rows)}</tbody></table>
      </div>
      <div class="flow">{ARROW_DOWN}<span>pandas replays the recipe</span><span class="sep">·</span>
        <span><b>{d["cells"]}</b> cells changed in {d["columns"]} columns</span><span class="sep">·</span><span><b>{d["quarantined"]}</b> {rows_word} quarantined</span></div>
      <div class="framed">{FRAME}<div class="card">
        <div class="card-h"><span class="num">03 · Replay</span><span class="file">{html.escape(d["clean_file"])}</span>{grade_pill(d["after_q"])}</div>
        <table><thead><tr><th class="n">#</th>{th(d["clean_head"], "mono ")}</tr></thead><tbody>{"".join(after_rows)}</tbody></table>
      </div></div>
    </div>
    <div class="col right">
      <div class="card recipe">
        <div class="card-h"><span class="num">02 · Plan</span><span class="file">{html.escape(d["recipe_file"])}</span><span class="pill plain">by {html.escape(d["generated_by"])}, no API key</span></div>
        <pre class="yaml">{yaml_html(d["yaml"])}</pre>
        <div class="card-f">Planned once by rules or an LLM, reviewed by you, kept in git.</div>
      </div>
      <div class="card drift">
        <div class="card-h"><span class="num">Next month</span><span class="file">{html.escape(d["next_file"])}</span></div>
        <ul>
          <li><code>{html.escape(dr["missing"])}</code> is missing</li>
          <li><code>{html.escape(dr["new"])}</code> is new: {dr["match"]}% match to <code>{html.escape(dr["match_to"])}</code></li>
          <li>A new date format: <code>{html.escape(dr["date"])}</code></li>
        </ul>
        <div class="stop">{STOP}apply stops with exit {dr["exit"]} instead of writing wrong data</div>
      </div>
    </div>
  </div>
</div></body></html>"""


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------
def _browser(p):
    for channel in (None, "msedge", "chrome"):
        try:
            return p.chromium.launch(channel=channel) if channel else p.chromium.launch()
        except Exception:  # noqa: BLE001 - try the next browser
            continue
    raise SystemExit("No Chromium-family browser found. Run `playwright install chromium`.")


FONTS_LOADED = """() => document.fonts.ready.then(() => {
  const ok = (name) => [...document.fonts].some((f) => f.family.replace(/"/g, '') === name && f.status === 'loaded');
  return ok('Inter') && ok('JetBrains Mono');
})"""


def _png(raw: bytes, out: Path) -> None:
    from PIL import Image

    Image.open(io.BytesIO(raw)).save(out, optimize=True)
    print(f"wrote {out.relative_to(ROOT)} ({out.stat().st_size // 1024} KB)")


def render(d: dict) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = _browser(p)
        for theme, name in (("light", "Hero.png"), ("dark", "Hero-dark.png")):
            page = browser.new_page(viewport={"width": WIDTH + 40, "height": 900}, device_scale_factor=2,
                                    color_scheme=theme)
            page.set_content(build(d, theme), wait_until="networkidle")
            if not page.evaluate(FONTS_LOADED):
                raise SystemExit("Inter / JetBrains Mono did not load (offline?); refusing to render with fallbacks.")
            _png(page.locator("#hero").screenshot(omit_background=True), ASSETS / name)
            page.close()

        # Logo.png: the mark on a transparent square, light colours, ~12% padding.
        size, pad = 1024, 120
        svg = (ASSETS / "Logo.svg").read_text(encoding="utf-8")
        svg = svg.replace("<svg ", '<svg style="width:100%;height:100%;display:block" ', 1)
        page = browser.new_page(viewport={"width": size, "height": size}, color_scheme="light")
        page.set_content(f'<html><body style="margin:0;background:transparent"><div style="width:{size}px;'
                         f'height:{size}px;padding:{pad}px;box-sizing:border-box">{svg}</div></body></html>')
        _png(page.screenshot(omit_background=True), ASSETS / "Logo.png")
        browser.close()


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        data = collect(Path(tmp) / "demo")
    render(data)


if __name__ == "__main__":
    main()
