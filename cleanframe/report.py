"""Self-contained HTML reports — profiling and cleaning results.

Two renderers, both returning a complete, standalone HTML document (inline CSS, no
external assets, light/dark aware):

* :func:`render_profile_report` — what ``cleanframe report file.csv`` produces:
  quality score, detected issues, and a column-by-column diagnosis.
* :func:`render_clean_report` — what a clean produces: the cell-level diff, renames,
  dropped rows, and the quarantine.

User data flows into these, so the Jinja environment runs with ``autoescape`` on —
a column literally named ``<script>`` renders as text, never markup.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
from jinja2 import Environment, select_autoescape

from ._version import __version__
from .diff import CellDiff
from .issues import Issues
from .profile import ColumnProfile, DataFrameProfile
from .quality import QualityScore, quality_score

_env = Environment(autoescape=select_autoescape(default=True, default_for_string=True))

#: Colour per semantic type badge.
_TYPE_COLORS = {
    "email": "#2563eb", "phone": "#7c3aed", "date": "#0891b2", "datetime": "#0891b2",
    "currency": "#16a34a", "integer": "#0d9488", "float": "#0d9488", "boolean": "#9333ea",
    "categorical": "#db2777", "id": "#64748b", "url": "#2563eb", "text": "#475569",
    "empty": "#94a3b8",
}
_SEV_COLORS = {"error": "#dc2626", "warning": "#d97706", "info": "#2563eb"}


_CSS = """
:root {
  --bg: #ffffff; --fg: #0f172a; --muted: #64748b; --card: #f8fafc;
  --border: #e2e8f0; --accent: #4f46e5; --chip-bg: #eef2ff;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0b1120; --fg: #e2e8f0; --muted: #94a3b8; --card: #131c31;
    --border: #24304a; --accent: #818cf8; --chip-bg: #1e253c;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--fg);
  font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
}
.wrap { max-width: 1040px; margin: 0 auto; padding: 32px 20px 80px; }
header.masthead { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap;
  border-bottom: 1px solid var(--border); padding-bottom: 16px; margin-bottom: 24px; }
header.masthead h1 { font-size: 22px; margin: 0; letter-spacing: -0.02em; }
header.masthead .sub { color: var(--muted); font-size: 14px; }
.grid { display: grid; gap: 16px; }
.stats { grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); margin-bottom: 28px; }
.tile { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 16px 18px; }
.tile .k { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }
.tile .v { font-size: 26px; font-weight: 650; margin-top: 4px; letter-spacing: -0.02em; }
.score { display: flex; align-items: center; gap: 16px; }
.score .ring { width: 74px; height: 74px; border-radius: 50%; display: grid; place-items: center;
  color: #fff; font-weight: 700; font-size: 22px; flex: none; }
.score .meta .g { font-weight: 650; font-size: 16px; } .score .meta .l { color: var(--muted); font-size: 13px; }
h2 { font-size: 15px; text-transform: uppercase; letter-spacing: .05em; color: var(--muted);
  margin: 34px 0 14px; font-weight: 600; }
.chip { display: inline-block; padding: 2px 9px; border-radius: 999px; font-size: 12px; font-weight: 600;
  color: #fff; }
.badge { display:inline-block; padding: 1px 8px; border-radius: 6px; font-size: 11px; font-weight: 600;
  background: var(--chip-bg); color: var(--accent); }
.issue { display: flex; gap: 10px; align-items: baseline; padding: 9px 12px; border: 1px solid var(--border);
  border-radius: 9px; background: var(--card); margin-bottom: 8px; }
.issue .dot { width: 8px; height: 8px; border-radius: 50%; flex: none; margin-top: 6px; }
.issue .col { font-weight: 600; font-size: 13px; } .issue .msg { font-size: 14px; }
.issue .kind { color: var(--muted); font-size: 12px; }
.cols { grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); }
.colcard { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 16px; }
.colcard .name { font-weight: 650; font-size: 15px; word-break: break-word; }
.colcard .row { display: flex; justify-content: space-between; font-size: 13px; color: var(--muted);
  margin-top: 4px; } .colcard .row b { color: var(--fg); font-weight: 600; }
.bar { height: 6px; border-radius: 4px; background: var(--accent); opacity: .85; }
.mc { margin-top: 10px; } .mc .item { display: grid; grid-template-columns: 1fr auto; gap: 8px;
  align-items: center; font-size: 12px; margin-top: 5px; } .mc .track { background: var(--border);
  border-radius: 4px; overflow: hidden; }
table.data { width: 100%; border-collapse: collapse; font-size: 13px; }
.scroll { overflow-x: auto; border: 1px solid var(--border); border-radius: 10px; }
table.data th, table.data td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--border);
  white-space: nowrap; } table.data th { color: var(--muted); font-weight: 600; background: var(--card); }
.before { color: #dc2626; } .after { color: #16a34a; }
.arrow { color: var(--muted); padding: 0 6px; }
code { background: var(--chip-bg); padding: 1px 5px; border-radius: 5px; font-size: 12px; }
.empty { color: var(--muted); font-style: italic; }
footer { margin-top: 48px; color: var(--muted); font-size: 12px; border-top: 1px solid var(--border);
  padding-top: 16px; }
"""

_BASE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{{ title }}</title>
<style>{{ css|safe }}</style>
</head>
<body><div class="wrap">
<header class="masthead">
  <h1>CleanFrame</h1>
  <span class="sub">{{ subtitle }}</span>
</header>
{{ body|safe }}
<footer>Generated by CleanFrame v{{ version }}. Deterministic &middot; reproducible &middot; reviewable.</footer>
</div></body></html>
"""

_PROFILE_BODY = """
<div class="grid stats">
  <div class="tile">
    <div class="k">Quality score</div>
    <div class="score">
      <div class="ring" style="background: {{ q.color }}">{{ q.score }}</div>
      <div class="meta"><div class="g">Grade {{ q.grade }}</div><div class="l">{{ q.label }}</div></div>
    </div>
  </div>
  <div class="tile"><div class="k">Rows</div><div class="v">{{ n_rows }}</div></div>
  <div class="tile"><div class="k">Columns</div><div class="v">{{ n_cols }}</div></div>
  <div class="tile"><div class="k">Issues</div><div class="v">{{ n_issues }}</div>
    <div class="l" style="color:var(--muted);font-size:12px">
      {{ n_error }} error &middot; {{ n_warn }} warning &middot; {{ n_info }} info</div></div>
  <div class="tile"><div class="k">Duplicate rows</div><div class="v">{{ dup_rows }}</div></div>
</div>

{% if issues %}
<h2>Detected issues</h2>
{% for i in issues %}
<div class="issue">
  <span class="dot" style="background: {{ i.color }}"></span>
  <div>
    {% if i.column %}<span class="col">{{ i.column }}</span> {% endif %}
    <span class="msg">{{ i.message }}</span>
    <div class="kind"><span class="badge">{{ i.kind }}</span> &middot; {{ i.severity }}
      &middot; via {{ i.detector }}{% if i.confidence < 1 %} &middot; {{ i.confidence }} conf{% endif %}</div>
  </div>
</div>
{% endfor %}
{% else %}<h2>Detected issues</h2><p class="empty">No issues detected — this file looks clean.</p>{% endif %}

<h2>Columns</h2>
<div class="grid cols">
{% for c in columns %}
<div class="colcard">
  <div class="name">{{ c.name }}
    <span class="chip" style="background: {{ c.color }}">{{ c.semantic_type }}</span></div>
  <div class="row"><span>dtype</span><b>{{ c.dtype }}</b></div>
  <div class="row"><span>missing</span><b>{{ c.null_pct }}%</b></div>
  <div class="row"><span>unique</span><b>{{ c.unique_count }}</b></div>
  {% if c.numeric %}<div class="row"><span>range</span><b>{{ c.numeric }}</b></div>{% endif %}
  {% if c.examples %}<div class="row" style="display:block"><span>examples</span>
    <div style="color:var(--fg);margin-top:3px">{% for e in c.examples %}<code>{{ e }}</code> {% endfor %}</div></div>{% endif %}
  {% if c.most_common %}<div class="mc">
    {% for m in c.most_common %}<div class="item"><div class="track"><div class="bar" style="width: {{ m.pct }}%"></div></div>
      <span>{{ m.value }} ({{ m.count }})</span></div>{% endfor %}</div>{% endif %}
</div>
{% endfor %}
</div>
"""

_CLEAN_BODY = """
<div class="grid stats">
  {% if q %}<div class="tile"><div class="k">Quality (before)</div>
    <div class="score"><div class="ring" style="background: {{ q.color }}">{{ q.score }}</div>
      <div class="meta"><div class="g">Grade {{ q.grade }}</div><div class="l">{{ q.label }}</div></div></div></div>{% endif %}
  <div class="tile"><div class="k">Cells changed</div><div class="v">{{ d.changed_cells }}</div></div>
  <div class="tile"><div class="k">Columns changed</div><div class="v">{{ d.changed_columns }}</div></div>
  <div class="tile"><div class="k">Rows</div><div class="v">{{ d.rows_before }} &rarr; {{ d.rows_after }}</div>
    <div class="l" style="color:var(--muted);font-size:12px">{{ d.rows_dropped }} dropped</div></div>
  <div class="tile"><div class="k">Quarantined</div><div class="v">{{ quarantine_n }}</div></div>
</div>

{% if renames or added or removed %}
<h2>Structure</h2>
{% for src, dst in renames.items() %}<div class="issue"><span class="dot" style="background:#2563eb"></span>
  <div><span class="msg">Renamed <code>{{ src }}</code> <span class="arrow">&rarr;</span> <code>{{ dst }}</code></span></div></div>{% endfor %}
{% for a in added %}<div class="issue"><span class="dot" style="background:#16a34a"></span>
  <div><span class="msg">Added column <code>{{ a }}</code></span></div></div>{% endfor %}
{% for r in removed %}<div class="issue"><span class="dot" style="background:#dc2626"></span>
  <div><span class="msg">Removed column <code>{{ r }}</code></span></div></div>{% endfor %}
{% endif %}

{% if changes_by_col %}
<h2>Changed cells</h2>
{% for col, rows in changes_by_col.items() %}
<div style="margin-bottom:18px">
  <div style="font-weight:650;margin-bottom:8px">{{ col }} <span class="badge">{{ rows.total }} changed</span></div>
  <div class="scroll"><table class="data">
    <thead><tr><th>row</th><th>before</th><th></th><th>after</th></tr></thead>
    <tbody>
    {% for r in rows.sample %}<tr><td>{{ r.row_id }}</td>
      <td class="before">{{ r.before }}</td><td class="arrow">&rarr;</td><td class="after">{{ r.after }}</td></tr>{% endfor %}
    </tbody></table></div>
  {% if rows.total > rows.sample|length %}<div class="l" style="color:var(--muted);font-size:12px;margin-top:6px">
    … and {{ rows.total - rows.sample|length }} more</div>{% endif %}
</div>
{% endfor %}
{% else %}<h2>Changed cells</h2><p class="empty">No cell values changed.</p>{% endif %}

{% if quarantine_cols %}
<h2>Quarantine <span class="badge">{{ quarantine_n }} row(s)</span></h2>
<div class="scroll"><table class="data">
  <thead><tr>{% for h in quarantine_cols %}<th>{{ h }}</th>{% endfor %}</tr></thead>
  <tbody>{% for row in quarantine_rows %}<tr>{% for cell in row %}<td>{{ cell }}</td>{% endfor %}</tr>{% endfor %}</tbody>
</table></div>
{% endif %}
"""


def _fmt_cell(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "∅"
    try:
        if pd.isna(value):
            return "∅"
    except (TypeError, ValueError):
        pass
    return str(value)


def _column_context(cp: ColumnProfile) -> dict:
    top = cp.most_common[:5]
    max_count = max((c for _, c in top), default=1) or 1
    numeric = None
    if cp.numeric_stats:
        numeric = f"{cp.numeric_stats['min']:g} – {cp.numeric_stats['max']:g}"
    return {
        "name": cp.name,
        "dtype": cp.dtype,
        "semantic_type": cp.semantic_type,
        "color": _TYPE_COLORS.get(cp.semantic_type, "#475569"),
        "null_pct": round(cp.null_fraction * 100, 1),
        "unique_count": cp.unique_count,
        "numeric": numeric,
        "examples": [_fmt_cell(v) for v in cp.sample_values[:4]],
        "most_common": [
            {"value": _fmt_cell(v), "count": n, "pct": round(100 * n / max_count)} for v, n in top
        ],
    }


def _issue_context(issues: Issues) -> list[dict]:
    ordered = sorted(issues, key=lambda i: (-i.severity.rank, str(i.column or ""), i.kind))
    return [
        {
            "column": i.column,
            "message": i.message,
            "kind": i.kind,
            "severity": i.severity.value,
            "detector": i.detector,
            "confidence": round(i.confidence, 2),
            "color": _SEV_COLORS.get(i.severity.value, "#64748b"),
        }
        for i in ordered
    ]


def render_profile_report(
    profile: DataFrameProfile,
    issues: Issues,
    *,
    source: str | None = None,
    quality: QualityScore | None = None,
) -> str:
    """Render the profiling report (issues, quality score, per-column diagnosis)."""
    quality = quality or quality_score(profile, issues)
    issue_ctx = _issue_context(issues)
    body = _env.from_string(_PROFILE_BODY).render(
        q=quality,
        n_rows=profile.n_rows,
        n_cols=profile.n_columns,
        n_issues=len(issues),
        n_error=sum(1 for i in issues if i.severity.value == "error"),
        n_warn=sum(1 for i in issues if i.severity.value == "warning"),
        n_info=sum(1 for i in issues if i.severity.value == "info"),
        dup_rows=profile.duplicate_row_count,
        issues=issue_ctx,
        columns=[_column_context(c) for c in profile.columns],
    )
    return _env.from_string(_BASE).render(
        title=f"CleanFrame report — {source}" if source else "CleanFrame report",
        subtitle=source or "data profile",
        css=_CSS,
        version=__version__,
        body=body,
    )


def render_clean_report(
    diff: CellDiff,
    *,
    quarantine: pd.DataFrame | None = None,
    source: str | None = None,
    quality: QualityScore | None = None,
    max_rows_per_column: int = 12,
) -> str:
    """Render the cleaning report (diff, structure changes, quarantine)."""
    changes_by_col = {}
    for col, changes in diff.changes_by_column().items():
        changes_by_col[col] = {
            "total": len(changes),
            "sample": [
                {"row_id": c.row_id, "before": _fmt_cell(c.before), "after": _fmt_cell(c.after)}
                for c in changes[:max_rows_per_column]
            ],
        }

    q_cols: list[str] = []
    q_rows: list[list[str]] = []
    if quarantine is not None and not quarantine.empty:
        q_cols = [str(c) for c in quarantine.columns]
        q_rows = [[_fmt_cell(v) for v in rec] for rec in quarantine.head(50).to_numpy().tolist()]

    body = _env.from_string(_CLEAN_BODY).render(
        q=quality,
        d=diff.summary(),
        renames=diff.renamed_columns,
        added=diff.added_columns,
        removed=diff.removed_columns,
        changes_by_col=changes_by_col,
        quarantine_n=0 if quarantine is None else int(len(quarantine)),
        quarantine_cols=q_cols,
        quarantine_rows=q_rows,
    )
    return _env.from_string(_BASE).render(
        title=f"CleanFrame diff — {source}" if source else "CleanFrame diff",
        subtitle=source or "cleaning result",
        css=_CSS,
        version=__version__,
        body=body,
    )


__all__ = ["render_profile_report", "render_clean_report", "quality_score", "QualityScore"]
