// CleanFrame playground UI. Vanilla ES module, no build step, no dependencies.
//
// The heavy lifting happens in worker.js (Pyodide + CleanFrame, off the main
// thread) and app.py (the Python glue). This file handles input, renders the
// result and draws the share card. Every user-derived string reaches the DOM
// through textContent, never innerHTML.

const MAX_BYTES = 20 * 1024 * 1024; // keep in sync with app.py
const MAX_XLSX_BYTES = 5 * 1024 * 1024;
const API_VERSION = 1;
const SANS = 'system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif';
const MONO = 'ui-monospace, "SF Mono", "Cascadia Mono", Consolas, "Liberation Mono", Menlo, monospace';

const $ = (id) => document.getElementById(id);
const state = {
  worker: null,
  ready: false,
  info: null,
  queued: null,
  busy: false,
  seq: 0,
  started: performance.now(),
  stageTimer: null,
  busyTimer: null,
  result: null,
  cardBlob: null,
  reportUrls: [],
};

// ---------------------------------------------------------------------------
// tiny DOM helpers (text only)
// ---------------------------------------------------------------------------
function h(tag, props, ...kids) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : String(v));
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    node.append(kid instanceof Node ? kid : String(kid));
  }
  return node;
}
const fmt = (n) => Number(n).toLocaleString("en-US");
const mb = (n) => `${(n / 1024 / 1024).toFixed(n < 10 * 1024 * 1024 ? 1 : 0)} MB`;
const secs = (ms) => (ms < 1000 ? `${Math.max(1, Math.round(ms))} ms` : `${(ms / 1000).toFixed(1)} s`);
const plural = (n, one, many) => `${fmt(n)} ${n === 1 ? one : many || `${one}s`}`;

let liveRegion;
function announce(text) {
  if (!liveRegion) {
    liveRegion = h("div", { class: "sr-only", "aria-live": "polite" });
    document.body.append(liveRegion);
  }
  liveRegion.textContent = "";
  setTimeout(() => { liveRegion.textContent = text; }, 30);
}

// ---------------------------------------------------------------------------
// worker lifecycle
// ---------------------------------------------------------------------------
const STAGES = ["runtime", "packages", "cleanframe"];
const STAGE_WIDTH = { runtime: 12, packages: 45, cleanframe: 85 };

// The worker is always started from a blob: shim, because a blob: worker inherits
// this page's Content-Security-Policy while one loaded from worker.js's URL would
// not (GitHub Pages sends no CSP header). There is deliberately no fallback to a
// URL worker: it would run without the policy the page promises.
function startWorker() {
  const workerUrl = new URL("worker.js", location.href).href;
  const unsupported = "This browser cannot run the Python worker. Try a current Chrome, Edge, Firefox or Safari.";
  let worker;
  try {
    const shim = new Blob([`import ${JSON.stringify(workerUrl)};\n`], { type: "text/javascript" });
    worker = new Worker(URL.createObjectURL(shim), { type: "module", name: "cleanframe" });
  } catch (err) {
    fatal(`${unsupported} (${err.message})`);
    return;
  }
  let booted = false;
  worker.addEventListener("error", (ev) => {
    if (booted) return;
    ev.preventDefault();
    worker.terminate();
    fatal(unsupported);
  });
  worker.addEventListener("message", (ev) => {
    const msg = ev.data || {};
    if (msg.type === "booted") booted = true;
    onWorkerMessage(msg);
  });
  state.worker = worker;
}

function onWorkerMessage(msg) {
  switch (msg.type) {
    case "progress":
      setStage(msg.stage, msg.detail);
      break;
    case "ready":
      onReady(msg);
      break;
    case "fatal":
      fatal(msg.error);
      break;
    case "result":
      onResult(msg);
      break;
    default:
      break;
  }
}

function setStage(stage, detail) {
  if (stage === "xlsx") {
    $("busy-detail").textContent = "Loading Excel support (first .xlsx only)…";
    return;
  }
  const idx = STAGES.indexOf(stage);
  for (const li of $("steps").children) {
    const i = STAGES.indexOf(li.dataset.step);
    li.classList.toggle("done", i < idx);
    li.classList.toggle("active", i === idx);
  }
  $("runtime-bar").style.width = `${STAGE_WIDTH[stage] || 0}%`;
  const firstVisit = "Starting Python takes a few seconds to half a minute, depending on your device. The first visit also downloads about 14 MB (CPython, pandas and NumPy compiled to WebAssembly) from cdn.jsdelivr.net; your browser caches it after that.";
  clearInterval(state.stageTimer);
  const t0 = performance.now();
  const tick = () => {
    const s = Math.round((performance.now() - t0) / 1000);
    $("runtime-note").textContent = `${detail}…${s >= 2 ? ` ${s} s.` : ""} ${firstVisit}`;
  };
  tick();
  state.stageTimer = setInterval(tick, 1000);
}

function onReady(msg) {
  clearInterval(state.stageTimer);
  const info = msg.info || {};
  if (info.api !== API_VERSION) {
    fatal("This page and its Python code are out of sync (a deploy is probably in progress). Reload in a minute.");
    return;
  }
  state.ready = true;
  state.info = info;
  console.info(`[cleanframe-playground] ready in ${msg.seconds.toFixed(1)} s ${JSON.stringify(msg.timings || {})}`);
  $("runtime").classList.add("ready");
  $("runtime-bar").style.width = "100%";
  $("ready-text").textContent =
    `Ready. CleanFrame ${info.cleanframe} on Python ${info.python} and pandas ${info.pandas}, loaded in ${msg.seconds.toFixed(1)} s.`;
  $("versions").textContent =
    `CleanFrame ${info.cleanframe} · Python ${info.python} · pandas ${info.pandas} · Pyodide ${info.pyodide}, running in your browser.`;
  if (!info.xlsx) {
    $("xlsx-hint").hidden = true;
    $("file").accept = ".csv,.tsv,.txt,.tab,.dat,text/csv,text/tab-separated-values,text/plain";
  }
  if (state.queued) {
    const { name, buffer } = state.queued;
    state.queued = null;
    run(name, buffer);
  }
}

function fatal(message) {
  clearInterval(state.stageTimer);
  setBusy(false);
  state.queued = null;
  const box = $("runtime");
  box.classList.add("failed");
  box.replaceChildren(
    h("p", { class: "note" },
      h("b", { text: "Python could not start. " }),
      `${message} The runtime loads from cdn.jsdelivr.net, so check your connection or any content blocker, then `,
      h("a", { href: location.href.split("#")[0], text: "reload the page" }), "."),
  );
}

// ---------------------------------------------------------------------------
// input: drop, pick, paste, sample
// ---------------------------------------------------------------------------
async function takeFile(file) {
  if (!file || state.busy) return;
  hideAlert();
  const name = file.name || "data.csv";
  const excel = /\.(xlsx|xlsm)$/i.test(name);
  if (/\.xls$/i.test(name)) {
    showError(`${name} is a legacy .xls workbook`, "Save it as .xlsx or CSV and try again.");
    return;
  }
  if (excel && state.info && !state.info.xlsx) {
    showError("Excel files are not supported in this build", "Export the sheet as CSV and drop that instead.");
    return;
  }
  const cap = excel ? MAX_XLSX_BYTES : MAX_BYTES;
  if (file.size > cap) {
    showError(
      `${name} is too big for the playground`,
      `It is ${mb(file.size)}; the limit is ${mb(cap)}${excel ? " for Excel files (or 20 MB as CSV)" : ""}. ` +
        "For bigger files run CleanFrame locally: pip install cleanframe-engine.",
    );
    return;
  }
  let buffer;
  try {
    buffer = await file.arrayBuffer();
  } catch (err) {
    showError(`Could not read ${name}`, String(err.message || err));
    return;
  }
  submit(name, buffer);
}

function submit(name, buffer) {
  if (state.busy) return;
  if (!state.ready) {
    state.queued = { name, buffer };
    setBusy(true, name, "Waiting for Python to finish loading. Your file will be analyzed right after.");
    return;
  }
  run(name, buffer);
}

function run(name, buffer) {
  const id = ++state.seq;
  setBusy(true, name);
  state.worker.postMessage({ type: "analyze", id, name, buffer }, [buffer]);
}

function onResult(msg) {
  if (msg.id !== state.seq) return;
  setBusy(false);
  let result;
  try {
    result = JSON.parse(msg.json);
  } catch (err) {
    showError("Something went wrong", `The result could not be read (${err.message}).`);
    return;
  }
  if (!result.ok) {
    const title = result.error_kind === "internal" ? "CleanFrame hit an unexpected error" : "That file could not be analyzed";
    showError(title, result.error || "Unknown error.", result.detail);
    return;
  }
  render(result, msg.ms);
}

function setBusy(on, name, detail) {
  state.busy = on;
  const zone = $("dropzone");
  zone.classList.toggle("busy", on);
  zone.setAttribute("aria-busy", on ? "true" : "false");
  $("busy").hidden = !on;
  $("pick").disabled = on;
  $("sample").disabled = on;
  clearInterval(state.busyTimer);
  if (!on) return;
  $("busy-title").textContent = `Analyzing ${name}`;
  const t0 = performance.now();
  const tick = () => {
    if (detail && !state.ready) {
      $("busy-detail").textContent = detail;
      return;
    }
    const s = Math.round((performance.now() - t0) / 1000);
    $("busy-detail").textContent = s < 6
      ? "Python is running in this tab."
      : `${s} s. Large files can take a minute: every step runs on your device, nothing is uploaded.`;
  };
  tick();
  state.busyTimer = setInterval(tick, 1000);
  announce(`Analyzing ${name}`);
}

function wireInputs() {
  const zone = $("dropzone");
  const input = $("file");
  $("pick").addEventListener("click", (ev) => { ev.stopPropagation(); input.click(); });
  zone.addEventListener("click", (ev) => {
    if (state.busy || ev.target.closest("button, a, input")) return;
    input.click();
  });
  input.addEventListener("change", () => {
    const file = input.files && input.files[0];
    input.value = "";
    takeFile(file);
  });
  $("sample").addEventListener("click", async (ev) => {
    ev.stopPropagation();
    hideAlert();
    try {
      const res = await fetch("sample/messy_customers.csv", { cache: "no-cache" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      submit("messy_customers.csv", await res.arrayBuffer());
    } catch (err) {
      showError("Could not load the sample", String(err.message || err));
    }
  });

  // The whole window is a drop target; the zone lights up while dragging.
  let depth = 0;
  const hasFiles = (ev) => Array.from(ev.dataTransfer?.types || []).includes("Files");
  window.addEventListener("dragenter", (ev) => {
    if (!hasFiles(ev)) return;
    depth += 1;
    zone.classList.add("drag");
    $("dz-title").textContent = "Drop it anywhere";
  });
  window.addEventListener("dragleave", () => {
    depth = Math.max(0, depth - 1);
    if (depth === 0) resetDrag();
  });
  window.addEventListener("dragover", (ev) => {
    if (!hasFiles(ev)) return;
    ev.preventDefault();
    ev.dataTransfer.dropEffect = state.busy ? "none" : "copy";
  });
  window.addEventListener("drop", (ev) => {
    if (!hasFiles(ev)) return;
    ev.preventDefault();
    depth = 0;
    resetDrag();
    takeFile(ev.dataTransfer.files[0]);
  });
  function resetDrag() {
    zone.classList.remove("drag");
    $("dz-title").textContent = "Drop a CSV file here";
  }

  // Paste: a file from the clipboard, or cells copied from a spreadsheet (TSV text).
  document.addEventListener("paste", (ev) => {
    const target = ev.target;
    if (state.busy || (target && target.closest && target.closest("input, textarea, [contenteditable]"))) return;
    const data = ev.clipboardData;
    if (!data) return;
    if (data.files && data.files.length) {
      ev.preventDefault();
      takeFile(data.files[0]);
      return;
    }
    const text = data.getData("text/plain");
    if (!text || !/\n./.test(text.trim()) || !/[\t,;|]/.test(text)) return;
    ev.preventDefault();
    hideAlert();
    const bytes = new TextEncoder().encode(text.endsWith("\n") ? text : `${text}\n`);
    if (bytes.byteLength > MAX_BYTES) {
      showError("That paste is too big", `It is ${mb(bytes.byteLength)}; the limit is ${mb(MAX_BYTES)}.`);
      return;
    }
    submit(text.includes("\t") ? "pasted.tsv" : "pasted.csv", bytes.buffer);
  });

  $("again").addEventListener("click", () => {
    window.scrollTo({ top: 0, behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
    $("pick").focus({ preventScroll: true });
  });
}

// ---------------------------------------------------------------------------
// errors
// ---------------------------------------------------------------------------
function showError(title, text, detail) {
  $("alert-title").textContent = title;
  $("alert-text").textContent = text;
  $("alert-details").hidden = !detail;
  $("alert-pre").textContent = detail || "";
  $("alert").hidden = false;
}
function hideAlert() {
  $("alert").hidden = true;
}

// ---------------------------------------------------------------------------
// rendering
// ---------------------------------------------------------------------------
const SEV_TEXT = { error: "Error", warning: "Warning", info: "Info" };
const BAND_COLOR = { A: "var(--good)", B: "var(--warn)", C: "var(--serious)", D: "var(--crit)", F: "var(--crit)" };
const CARD_BAND_COLOR = { A: "#22c55e", B: "#fab219", C: "#f0845c", D: "#ef4444", F: "#ef4444" };

function sevIcon(sev) {
  return h("span", { class: `sev sev-${sev}`, title: SEV_TEXT[sev] || sev, "aria-hidden": "true" });
}

function fileStem(name) {
  const stem = String(name).replace(/\.[^.]+$/, "").trim() || "data";
  return stem.replace(/[\\/:*?"<>|\u0000-\u001f]+/g, "_").slice(0, 80);
}

function render(r, ms) {
  state.result = r;
  for (const url of state.reportUrls) URL.revokeObjectURL(url);
  state.reportUrls = [];
  const stem = fileStem(r.file.name);
  const names = {
    csv: `${stem}.clean.csv`,
    recipe: `${stem}.recipe.yaml`,
    code: `clean_${stem.replace(/[^\w]+/g, "_").replace(/^_+|_+$/g, "") || "data"}.py`,
    quarantine: `${stem}.quarantine.csv`,
  };

  // header
  $("r-file").textContent = r.file.name;
  const meta = [
    `${plural(r.file.rows, "row")} × ${plural(r.file.columns, "column")}`,
    r.file.sheet ? `sheet "${r.file.sheet}"` : null,
    `analyzed in ${secs(ms)}`,
  ].filter(Boolean);
  $("r-meta").textContent = meta.join(" · ");

  // score
  const s = r.score;
  const panel = $("score-panel");
  panel.style.setProperty("--band", BAND_COLOR[s.grade] || "var(--crit)");
  $("mess").textContent = String(s.mess);
  $("band").textContent = s.band;
  const meter = $("meter");
  meter.setAttribute("aria-valuenow", String(s.mess));
  meter.setAttribute("aria-valuetext", `${s.mess} out of 100: ${s.band}`);
  const fill = $("meter-fill");
  fill.style.width = "0";
  requestAnimationFrame(() => requestAnimationFrame(() => { fill.style.width = `${Math.max(s.mess, s.mess > 0 ? 1.5 : 0)}%`; }));
  $("st-grade").replaceChildren(s.grade, h("small", { text: `${s.quality}/100` }));
  $("st-issues").textContent = fmt(r.issues.length);
  $("st-cells").textContent = fmt(r.diff.changed_cells);
  $("st-quarantine").textContent = fmt(r.diff.rows_dropped);
  $("formula").textContent = [
    `penalty = Σ weight × share affected${s.duplicate_penalty ? " + duplicates" : ""}  = ${s.penalty}`,
    `average = min(1, ${s.penalty} ÷ ${plural(s.columns, "column")})  = ${s.average_penalty}`,
    `quality = round(100 × (1 − ${s.average_penalty}))  = ${s.quality}`,
    `mess    = 100 − ${s.quality}  = ${s.mess}`,
  ].join("\n");

  // chips
  const chips = $("chips");
  chips.replaceChildren(
    ...(r.kinds.length
      ? r.kinds.map((k) => h("li", { class: "chip", title: k.label }, sevIcon(k.severity), h("span", { class: "sr-only", text: `${SEV_TEXT[k.severity]}: ` }), k.text))
      : [h("li", { class: "chip" }, sevIcon("info"), "No problems found. Nice.")]),
  );

  // downloads
  const dl = $("downloads");
  dl.replaceChildren(
    downloadButton("Cleaned CSV", names.csv, r.cleaned_csv, "text/csv", true),
    downloadButton("Recipe (.yaml)", names.recipe, r.recipe_yaml, "application/yaml"),
    downloadButton("Pandas code (.py)", names.code, r.code, "text/x-python"),
    r.quarantine_csv ? downloadButton(`Quarantined rows (${fmt(r.diff.quarantined)})`, names.quarantine, r.quarantine_csv, "text/csv") : null,
  );

  // notes from the reader (encoding fixes, inference losses, multi-sheet)
  const notes = $("notes");
  notes.replaceChildren(...r.notes.map((n) => h("li", { text: n })));
  notes.hidden = r.notes.length === 0;

  renderProblems(r);
  renderChanges(r);
  $("recipe-code").replaceChildren(...highlightYaml(r.recipe_yaml));
  $("py-code").textContent = r.code;
  setTabCount("tab-problems", r.issues.length);
  setTabCount("tab-changes", r.diff.changed_cells);

  // share + next step
  drawCard($("card"), r.share, s.grade);
  $("card").setAttribute("aria-label", `Share card: Mess Score ${r.share.mess} out of 100, ${r.share.band}. ${r.share.items.join(". ")}.`);
  $("share-x").href = `https://x.com/intent/post?text=${encodeURIComponent(r.share.text)}`;
  const recipeArg = /^[\w.-]+$/.test(names.recipe) ? names.recipe : `"${names.recipe.replace(/"/g, "")}"`;
  $("cmd-apply").textContent = `cleanframe apply next_export.csv --recipe ${recipeArg}`;

  document.body.classList.add("tidy");
  $("results").hidden = false;
  selectTab("tab-problems", false);
  const heading = $("results-title");
  heading.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
  heading.focus({ preventScroll: true });
  announce(`Mess Score ${s.mess} out of 100, ${s.band}. ${plural(r.issues.length, "problem")} found.`);
}

function setTabCount(id, n) {
  const tab = $(id);
  tab.querySelector(".count")?.remove();
  tab.append(h("span", { class: "count", text: fmt(n) }));
}

function downloadButton(label, filename, text, type, primary = false) {
  const icon = svgIcon("M8 1.5v8.2m0 0L4.8 6.5M8 9.7l3.2-3.2M2.5 11v2.5h11V11");
  return h("button", {
    type: "button",
    class: `btn ${primary ? "primary" : "ghost"} small`,
    onclick: () => saveFile(filename, new Blob([text], { type: `${type};charset=utf-8` })),
  }, icon, label);
}

function svgIcon(d) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 16 16");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS(ns, "path");
  path.setAttribute("d", d);
  path.setAttribute("fill", "none");
  path.setAttribute("stroke", "currentColor");
  path.setAttribute("stroke-width", "1.6");
  path.setAttribute("stroke-linecap", "round");
  path.setAttribute("stroke-linejoin", "round");
  svg.append(path);
  return svg;
}

function saveFile(filename, blob) {
  const url = URL.createObjectURL(blob);
  const a = h("a", { href: url, download: filename });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 30_000);
  announce(`Downloading ${filename}`);
}

function renderProblems(r) {
  const cards = [];
  const byId = new Map(r.issues.map((i) => [i.id, i]));
  const issueItem = (i) => h("li", {},
    sevIcon(i.severity),
    h("span", { class: "msg" }, h("span", { class: "sr-only", text: `${SEV_TEXT[i.severity]}: ` }), h("b", { text: i.label }), ` · ${i.message}`),
    h("span", { class: `fix ${i.has_fix ? "yes" : "no"}`, text: i.has_fix ? "fixed by recipe" : "needs review" }));

  const fileLevel = r.issues.filter((i) => i.column == null);
  if (fileLevel.length) {
    cards.push(h("section", { class: "colcard" },
      h("header", {}, h("span", { class: "name", text: "Whole file" })),
      h("ul", { class: "issue-list" }, fileLevel.map(issueItem))));
  }
  const clean = [];
  for (const col of r.columns) {
    if (!col.issues.length) {
      clean.push(col.name);
      continue;
    }
    cards.push(h("section", { class: "colcard" },
      h("header", {},
        h("span", { class: "name", text: col.name }),
        col.output_name !== col.name ? [h("span", { class: "arrow", "aria-label": "renamed to", text: "→" }), h("span", { class: "out", text: col.output_name })] : null,
        h("span", { class: "typechip", text: col.type })),
      h("ul", { class: "issue-list" }, col.issues.map((id) => issueItem(byId.get(id))))));
  }
  if (clean.length) {
    cards.push(h("p", { class: "renames" }, h("b", { text: "No problems: " }), clean.join(", ")));
  }
  if (!cards.length) cards.push(h("p", { text: "CleanFrame found nothing to fix in this file." }));
  $("colcards").replaceChildren(...cards);
}

function renderChanges(r) {
  const d = r.diff;
  // Reasons are CleanFrame slugs: an op name ("dedup") or "<column>:<check>" for validation.
  const reasonText = (why) => {
    if (why === "dedup") return "duplicates";
    const at = why.indexOf(":");
    return at > 0 ? `failed ${why.slice(at + 1)} on ${why.slice(0, at)}` : why;
  };
  const reasons = d.dropped_reasons.map(([why, n]) => `${fmt(n)} ${reasonText(why)}`).join(", ");
  const parts = [
    `${plural(d.changed_cells, "cell")} changed in ${plural(d.changed_columns, "column")}`,
    `${fmt(d.rows_before)} → ${plural(d.rows_after, "row")}${d.rows_dropped ? ` (removed: ${reasons})` : ""}`,
  ];
  if (d.shown < d.changed_cells) parts.push(`showing ${fmt(d.shown)} examples; the downloads and the cleaning report cover every change`);
  $("changes-summary").textContent = `${parts.join(" · ")}.`;
  const ren = $("renames");
  ren.hidden = d.renamed.length === 0;
  ren.replaceChildren(h("b", { text: "Renamed: " }), d.renamed.map(([a, b]) => `${a} → ${b}`).join(", "));

  const table = $("diff-table");
  for (const tb of table.querySelectorAll("tbody")) tb.remove();
  const changedByOut = new Map(r.columns.map((c) => [c.output_name, c.changed]));
  let current = null;
  let body = null;
  for (const ch of r.changes) {
    if (ch.column !== current) {
      current = ch.column;
      const n = changedByOut.get(ch.column);
      body = h("tbody", {}, h("tr", {}, h("th", { colspan: "4", scope: "colgroup" },
        ch.column, n ? h("span", { class: "muted", text: `${plural(n, "change")}${d.truncated ? "+" : ""}` }) : null)));
      table.append(body);
    }
    body.append(h("tr", {},
      h("td", { class: "row", text: String(ch.row) }),
      h("td", {}, valueChip(ch.before, "before")),
      h("td", { class: "arrow", "aria-hidden": "true", text: "→" }),
      h("td", {}, valueChip(ch.after, "after"))));
  }
  if (!r.changes.length) {
    table.append(h("tbody", {}, h("tr", {}, h("td", { colspan: "4", text: "No cell values changed." }))));
  }
}

// Shows leading/trailing spaces as dots so "bengaluru " visibly differs from "bengaluru".
function valueChip(value, kind) {
  if (value === null) return h("span", { class: `val ${kind} null`, text: "empty" });
  const m = /^(\s*)([\s\S]*?)(\s*)$/.exec(value);
  const ws = (s) => (s ? h("span", { class: "ws", "aria-label": `${s.length} space${s.length === 1 ? "" : "s"}`, text: "·".repeat(s.length) }) : null);
  return h("span", { class: `val ${kind}` }, ws(m[1]), m[2], ws(m[3]));
}

function highlightYaml(text) {
  const out = [];
  const lines = text.split("\n");
  lines.forEach((line, idx) => {
    const trimmed = line.trimStart();
    if (trimmed.startsWith("#")) {
      out.push(h("span", { class: "c", text: line }));
    } else {
      const m = /^(\s*(?:- )?)([^:#'"\s][^:#'"]*?|'[^']*'|"[^"]*")(:)(?=\s|$)(.*)$/.exec(line);
      if (m) {
        out.push(m[1], h("span", { class: "k", text: m[2] }), m[3], ...yamlValue(m[4]));
      } else {
        const li = /^(\s*- )(.*)$/.exec(line);
        if (li) out.push(li[1], ...yamlValue(li[2]));
        else out.push(line);
      }
    }
    if (idx < lines.length - 1) out.push("\n");
  });
  return out;
}

function yamlValue(rest) {
  const v = rest.trim();
  if (!v) return [rest];
  const lead = rest.slice(0, rest.length - rest.trimStart().length);
  if (/^['"]/.test(v)) return [lead, h("span", { class: "s", text: v })];
  if (/^(-?\d[\d_.]*|true|false|null|~)$/i.test(v)) return [lead, h("span", { class: "n", text: v })];
  return [rest];
}

// ---------------------------------------------------------------------------
// tabs (WAI-ARIA pattern: arrows, Home, End)
// ---------------------------------------------------------------------------
function selectTab(id, focus = true) {
  for (const tab of document.querySelectorAll('[role="tab"]')) {
    const on = tab.id === id;
    tab.setAttribute("aria-selected", on ? "true" : "false");
    tab.tabIndex = on ? 0 : -1;
    $(tab.getAttribute("aria-controls")).hidden = !on;
    if (on && focus) tab.focus();
  }
}
function wireTabs() {
  const tabs = Array.from(document.querySelectorAll('[role="tab"]'));
  for (const tab of tabs) {
    tab.addEventListener("click", () => selectTab(tab.id));
    tab.addEventListener("keydown", (ev) => {
      const i = tabs.indexOf(tab);
      const next = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: tabs.length - 1 }[ev.key];
      if (next === undefined) return;
      ev.preventDefault();
      selectTab(tabs[(next + tabs.length) % tabs.length].id);
    });
  }
}

// ---------------------------------------------------------------------------
// copy buttons, reports, share actions
// ---------------------------------------------------------------------------
async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const ta = h("textarea", { class: "sr-only", readonly: true });
    ta.value = text;
    document.body.append(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch { ok = false; }
    ta.remove();
    return ok;
  }
}

function flash(button, label) {
  const original = button.dataset.label || button.textContent;
  button.dataset.label = original;
  if (button.classList.contains("icon-btn")) {
    button.classList.add("done");
  } else {
    button.textContent = label;
    button.classList.add("done");
  }
  announce(label);
  setTimeout(() => {
    button.classList.remove("done");
    if (!button.classList.contains("icon-btn")) button.textContent = original;
  }, 1600);
}

function wireActions() {
  document.addEventListener("click", async (ev) => {
    const btn = ev.target.closest("[data-copy]");
    if (!btn) return;
    const key = btn.dataset.copy;
    let text;
    if (key === "recipe" || key === "code") {
      if (!state.result) return;
      text = key === "recipe" ? state.result.recipe_yaml : state.result.code;
    } else {
      text = $(key)?.textContent;
    }
    if (text) flash(btn, (await copyText(text)) ? "Copied" : "Copy failed");
  });

  const openHtml = (html) => {
    const url = URL.createObjectURL(new Blob([html], { type: "text/html;charset=utf-8" }));
    state.reportUrls.push(url);
    const a = h("a", { href: url, target: "_blank", rel: "noopener" });
    document.body.append(a);
    a.click();
    a.remove();
  };
  $("open-report").addEventListener("click", () => state.result && openHtml(state.result.report_html));
  $("open-clean-report").addEventListener("click", () => state.result && openHtml(state.result.clean_report_html));

  $("card-download").addEventListener("click", async () => {
    const blob = await cardBlob();
    if (blob) saveFile(`cleanframe-mess-score-${state.result.share.mess}.png`, blob);
  });

  const copyBtn = $("card-copy");
  if (window.ClipboardItem && navigator.clipboard && navigator.clipboard.write) {
    copyBtn.hidden = false;
    copyBtn.addEventListener("click", async () => {
      try {
        // A promise keeps Safari's user-gesture requirement satisfied.
        await navigator.clipboard.write([new ClipboardItem({ "image/png": cardBlob() })]);
        flash(copyBtn, "Copied");
      } catch {
        flash(copyBtn, "Copy failed");
      }
    });
  }

  const shareBtn = $("card-share");
  if (navigator.canShare && navigator.share) {
    try {
      const probe = new File([new Uint8Array(8)], "card.png", { type: "image/png" });
      shareBtn.hidden = !navigator.canShare({ files: [probe] });
    } catch {
      shareBtn.hidden = true;
    }
    shareBtn.addEventListener("click", async () => {
      const blob = await cardBlob();
      if (!blob) return;
      const file = new File([blob], `cleanframe-mess-score-${state.result.share.mess}.png`, { type: "image/png" });
      try {
        await navigator.share({ files: [file], text: state.result.share.text });
      } catch {
        /* the visitor closed the share sheet */
      }
    });
  }
}

function cardBlob() {
  if (state.cardBlob) return Promise.resolve(state.cardBlob);
  return new Promise((resolve) => $("card").toBlob((b) => { state.cardBlob = b; resolve(b); }, "image/png"));
}

// ---------------------------------------------------------------------------
// the share card (1200 x 630). Draws ONLY share.mess, share.band, share.items.
// ---------------------------------------------------------------------------
function drawLogo(ctx, x, y, size) {
  const k = size / 64;
  ctx.save();
  ctx.translate(x, y);
  ctx.scale(k, k);
  const g = ctx.createLinearGradient(0, 0, 64, 64);
  g.addColorStop(0, "#06b6d4");
  g.addColorStop(0.5, "#3b82f6");
  g.addColorStop(1, "#8b5cf6");
  ctx.strokeStyle = g;
  ctx.lineWidth = 6;
  ctx.lineCap = "butt";
  ctx.beginPath();
  ctx.moveTo(5, 21); ctx.lineTo(5, 5); ctx.lineTo(21, 5);
  ctx.moveTo(43, 5); ctx.lineTo(59, 5); ctx.lineTo(59, 21);
  ctx.moveTo(59, 43); ctx.lineTo(59, 59); ctx.lineTo(43, 59);
  ctx.moveTo(21, 59); ctx.lineTo(5, 59); ctx.lineTo(5, 43);
  ctx.stroke();
  for (let r = 0; r < 3; r += 1) {
    for (let c = 0; c < 3; c += 1) {
      ctx.fillStyle = (r + c) % 2 === 0 ? "#22d3ee" : "#64748b";
      ctx.fillRect(15 + c * 12, 15 + r * 12, 10, 10);
    }
  }
  ctx.restore();
}

function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

function setSpacing(ctx, px) {
  if ("letterSpacing" in ctx) ctx.letterSpacing = `${px}px`;
}

// Lay out one item: a leading count in bold, the rest regular, wrapped to maxWidth.
function layoutItem(ctx, text, maxWidth, boldFont, regFont) {
  const words = text.split(" ");
  const tokens = words.map((w, i) => ({ w, bold: i === 0 && /^[\d,]+$/.test(w) }));
  const lines = [[]];
  let width = 0;
  for (const t of tokens) {
    ctx.font = t.bold ? boldFont : regFont;
    const tw = ctx.measureText(`${t.w} `).width;
    if (width + tw > maxWidth && lines[lines.length - 1].length) {
      lines.push([]);
      width = 0;
    }
    lines[lines.length - 1].push(t);
    width += tw;
  }
  return lines;
}

function drawCard(canvas, share, grade) {
  state.cardBlob = null;
  const W = 1200;
  const H = 630;
  const ctx = canvas.getContext("2d");
  ctx.save();
  ctx.clearRect(0, 0, W, H);

  // background: deep navy with two soft brand glows
  ctx.fillStyle = "#0a0f1f";
  ctx.fillRect(0, 0, W, H);
  let g = ctx.createRadialGradient(120, 40, 0, 120, 40, 640);
  g.addColorStop(0, "rgba(6, 182, 212, 0.20)");
  g.addColorStop(1, "rgba(6, 182, 212, 0)");
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, W, H);
  g = ctx.createRadialGradient(1150, 640, 0, 1150, 640, 720);
  g.addColorStop(0, "rgba(139, 92, 246, 0.24)");
  g.addColorStop(1, "rgba(139, 92, 246, 0)");
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, W, H);
  g = ctx.createLinearGradient(0, 0, W, 0);
  g.addColorStop(0, "#06b6d4");
  g.addColorStop(0.5, "#3b82f6");
  g.addColorStop(1, "#8b5cf6");
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, W, 6);

  // brand
  drawLogo(ctx, 64, 50, 52);
  ctx.textBaseline = "middle";
  ctx.font = `700 34px ${SANS}`;
  setSpacing(ctx, 0);
  ctx.fillStyle = "#f8fafc";
  ctx.fillText("Clean", 132, 77);
  const cleanW = ctx.measureText("Clean").width;
  const frameW = ctx.measureText("Frame").width;
  g = ctx.createLinearGradient(132 + cleanW, 0, 132 + cleanW + frameW, 0);
  g.addColorStop(0, "#22d3ee");
  g.addColorStop(1, "#a78bfa");
  ctx.fillStyle = g;
  ctx.fillText("Frame", 132 + cleanW, 77);
  ctx.textAlign = "right";
  ctx.font = `500 21px ${SANS}`;
  ctx.fillStyle = "#94a3b8";
  ctx.fillText("Data quality, scored in the browser", W - 64, 77);
  ctx.textAlign = "left";

  // the score
  const mess = Math.max(0, Math.min(100, Number(share.mess) || 0));
  const bandColor = CARD_BAND_COLOR[grade] || "#ef4444";
  ctx.textBaseline = "alphabetic";
  ctx.font = `700 21px ${SANS}`;
  setSpacing(ctx, 4);
  ctx.fillStyle = "#94a3b8";
  ctx.fillText("MESS SCORE", 64, 190);
  setSpacing(ctx, -6);
  ctx.font = `800 210px ${SANS}`;
  ctx.fillStyle = "#ffffff";
  const num = String(mess);
  ctx.fillText(num, 56, 382);
  const numW = ctx.measureText(num).width;
  setSpacing(ctx, 0);
  ctx.font = `600 46px ${SANS}`;
  ctx.fillStyle = "#64748b";
  ctx.fillText("/100", 56 + numW + 14, 382);

  ctx.beginPath();
  ctx.arc(74, 436, 10, 0, Math.PI * 2);
  ctx.fillStyle = bandColor;
  ctx.fill();
  ctx.font = `700 34px ${SANS}`;
  ctx.fillStyle = "#f1f5f9";
  ctx.fillText(String(share.band), 96, 448);

  const mx = 64;
  const my = 482;
  const mw = 440;
  ctx.fillStyle = "rgba(148, 163, 184, 0.18)";
  roundRect(ctx, mx, my, mw, 14, 7);
  ctx.fill();
  if (mess > 0) {
    ctx.fillStyle = bandColor;
    roundRect(ctx, mx, my, Math.max(14, (mw * mess) / 100), 14, 7);
    ctx.fill();
  }
  ctx.font = `500 17px ${SANS}`;
  ctx.fillStyle = "#64748b";
  ctx.fillText("0 spotless", mx, my + 40);
  ctx.textAlign = "right";
  ctx.fillText("100 chaos", mx + mw, my + 40);
  ctx.textAlign = "left";

  // divider
  ctx.fillStyle = "rgba(148, 163, 184, 0.16)";
  ctx.fillRect(576, 160, 1, 370);

  // what was found (counts only)
  const lx = 624;
  const lw = W - 64 - lx;
  ctx.font = `700 21px ${SANS}`;
  setSpacing(ctx, 4);
  ctx.fillStyle = "#94a3b8";
  ctx.fillText("WHAT CLEANFRAME FOUND", lx, 190);
  setSpacing(ctx, 0);
  const bold = `800 32px ${SANS}`;
  const reg = `500 29px ${SANS}`;
  let y = 248;
  const lineH = 40;
  const items = (share.items || []).slice(0, 4);
  for (const item of items) {
    const lines = layoutItem(ctx, String(item), lw - 30, bold, reg).slice(0, 2);
    const needed = lines.length * lineH;
    if (y + needed - lineH > 520) break;
    g = ctx.createLinearGradient(lx, y - 20, lx + 12, y - 8);
    g.addColorStop(0, "#22d3ee");
    g.addColorStop(1, "#a78bfa");
    ctx.fillStyle = g;
    ctx.fillRect(lx, y - 21, 12, 12);
    for (const line of lines) {
      let x = lx + 30;
      for (const t of line) {
        ctx.font = t.bold ? bold : reg;
        ctx.fillStyle = t.bold ? "#ffffff" : "#cbd5e1";
        ctx.fillText(t.w, x, y);
        x += ctx.measureText(`${t.w} `).width;
      }
      y += lineH;
    }
    y += 22;
  }

  // footer
  ctx.font = `500 23px ${MONO}`;
  const cmd = "pip install cleanframe-engine";
  const cmdW = ctx.measureText(`$ ${cmd}`).width;
  ctx.fillStyle = "rgba(15, 23, 42, 0.9)";
  roundRect(ctx, 64, 548, cmdW + 40, 50, 12);
  ctx.fill();
  ctx.strokeStyle = "rgba(148, 163, 184, 0.28)";
  ctx.lineWidth = 1.5;
  ctx.stroke();
  ctx.fillStyle = "#64748b";
  ctx.fillText("$", 84, 581);
  ctx.fillStyle = "#e2e8f0";
  ctx.fillText(cmd, 84 + ctx.measureText("$ ").width, 581);
  ctx.textAlign = "right";
  ctx.font = `500 20px ${SANS}`;
  ctx.fillStyle = "#94a3b8";
  ctx.fillText("github.com/inboxpraveen/Cleanframe", W - 64, 568);
  ctx.fillStyle = "#64748b";
  ctx.fillText("Runs locally. No data was uploaded.", W - 64, 596);
  ctx.restore();
}

// ---------------------------------------------------------------------------
// boot
// ---------------------------------------------------------------------------
wireInputs();
wireTabs();
wireActions();
setStage("runtime", "Starting Python");
startWorker();
