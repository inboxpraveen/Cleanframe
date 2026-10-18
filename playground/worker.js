// CleanFrame playground worker: runs Pyodide (CPython compiled to WebAssembly),
// pandas and CleanFrame off the main thread, so the page never freezes.
//
// Loaded as a *module* worker (Pyodide 314 refuses classic workers). app.js starts
// it from a tiny blob: shim so the worker inherits the page's Content-Security-
// Policy; a worker loaded straight from a URL would take its policy from response
// headers, which GitHub Pages does not send.
//
// Network: the pinned Pyodide CDN directory below, plus this site's own
// wheels/manifest.json, wheels/*.whl and app.py. The visitor's file arrives as an
// ArrayBuffer via postMessage and never leaves this worker.

// Keep in sync with index.html (CSP), tests/node_smoke.mjs and README.md.
const PYODIDE_VERSION = "314.0.7";
const INDEX_URL = `https://cdn.jsdelivr.net/pyodide/v${PYODIDE_VERSION}/full/`;
const BASE = new URL("./", import.meta.url);
// Everything CleanFrame imports, loaded in one parallel batch from the Pyodide CDN.
const PACKAGES = ["micropip", "pandas", "pyyaml", "jinja2", "python-dateutil"];

let pyodide = null;
let analyzeJson = null;
let manifest = null;
let xlsxReady = null; // a Promise once the Excel wheels start installing

const post = (msg) => self.postMessage(msg);
const timings = {};
let lastMark = performance.now();
const progress = (stage, detail) => {
  post({ type: "progress", stage, detail });
};
const mark = (label) => {
  const now = performance.now();
  timings[label] = Math.round(now - lastMark);
  lastMark = now;
};
const errorText = (err) => String(err && err.message ? err.message : err);

async function fetchSameOrigin(path, as) {
  // "no-cache" revalidates with the server (a cheap 304), so a fresh deploy is
  // picked up at once and the wheel, manifest and app.py never mix versions.
  const res = await fetch(new URL(path, BASE), { cache: "no-cache" });
  if (!res.ok) throw new Error(`${path}: HTTP ${res.status}`);
  if (as === "json") return res.json();
  if (as === "text") return res.text();
  return new Uint8Array(await res.arrayBuffer());
}

async function installWheels(names) {
  pyodide.FS.mkdirTree("/wheels");
  const urls = [];
  for (const name of names) {
    if (!/^[\w.-]+\.whl$/.test(name)) throw new Error(`unexpected wheel name ${name}`);
    pyodide.FS.writeFile(`/wheels/${name}`, await fetchSameOrigin(`wheels/${name}`));
    urls.push(`emfs:/wheels/${name}`);
  }
  const micropip = pyodide.pyimport("micropip");
  try {
    // deps=False: every dependency is already loaded from the Pyodide CDN, so
    // micropip never has a reason to reach for PyPI.
    await micropip.install.callKwargs(urls, { deps: false });
  } finally {
    micropip.destroy();
  }
}

async function boot() {
  const t0 = performance.now();
  progress("runtime", "Downloading the Python runtime");
  const { loadPyodide } = await import(`${INDEX_URL}pyodide.mjs`);
  pyodide = await loadPyodide({ indexURL: INDEX_URL, stdout: () => {}, stderr: () => {} });
  mark("runtime_ms");

  progress("packages", "Loading pandas, NumPy, PyYAML and Jinja2");
  const loadErrors = [];
  const [, mf] = await Promise.all([
    pyodide.loadPackage(PACKAGES, { messageCallback: () => {}, errorCallback: (m) => loadErrors.push(m) }),
    fetchSameOrigin("wheels/manifest.json", "json"),
  ]);
  if (loadErrors.length) throw new Error(loadErrors.join("; "));
  mark("packages_ms");
  manifest = mf;
  if (!manifest || typeof manifest.wheel !== "string") throw new Error("wheels/manifest.json is malformed");

  progress("cleanframe", `Installing CleanFrame ${manifest.version || ""}`.trim());
  const [, appSource] = await Promise.all([installWheels([manifest.wheel]), fetchSameOrigin("app.py", "text")]);
  mark("install_ms");
  pyodide.FS.writeFile("/home/pyodide/cf_playground.py", appSource);
  const info = JSON.parse(
    pyodide.runPython(
      [
        "import json, sys",
        "if '/home/pyodide' not in sys.path: sys.path.insert(0, '/home/pyodide')",
        "import cf_playground",
        "json.dumps({**cf_playground.runtime_info(), 'api': cf_playground.API_VERSION})",
      ].join("\n"),
    ),
  );
  analyzeJson = pyodide.globals.get("cf_playground").analyze_json;
  mark("import_ms");
  post({
    type: "ready",
    seconds: (performance.now() - t0) / 1000,
    timings,
    info: { ...info, pyodide: pyodide.version, xlsx: Array.isArray(manifest.xlsx) && manifest.xlsx.length > 0 },
  });
}

function ensureXlsx() {
  if (!Array.isArray(manifest.xlsx) || manifest.xlsx.length === 0) return Promise.resolve(false);
  if (!xlsxReady) {
    progress("xlsx", "Loading Excel support");
    xlsxReady = installWheels(manifest.xlsx).then(() => true);
    xlsxReady.catch(() => { xlsxReady = null; }); // allow a retry
  }
  return xlsxReady;
}

async function analyze({ id, name, buffer }) {
  let json;
  let ms = 0;
  try {
    if (/\.(xlsx|xlsm)$/i.test(name)) await ensureXlsx();
    const t0 = performance.now();
    // Passed as a JsProxy; app.analyze() copies it into Python bytes.
    json = analyzeJson(new Uint8Array(buffer), name);
    ms = performance.now() - t0;
  } catch (err) {
    json = JSON.stringify({ ok: false, error_kind: "internal", error: errorText(err) });
  }
  post({ type: "result", id, json, ms });
}

self.addEventListener("message", (ev) => {
  const msg = ev.data || {};
  if (msg.type !== "analyze") return;
  if (!analyzeJson) {
    post({ type: "result", id: msg.id, json: JSON.stringify({ ok: false, error_kind: "internal", error: "The Python runtime is still loading." }), ms: 0 });
    return;
  }
  analyze(msg);
});

post({ type: "booted" });
boot().catch((err) => post({ type: "fatal", error: errorText(err) }));
