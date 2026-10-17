// Node smoke test: the playground's exact Python path, under real Pyodide.
//
// It loads the same Pyodide version and package list as worker.js (both are read
// out of worker.js, so they cannot drift), installs the wheels listed in
// wheels/manifest.json with micropip (deps=False, as the page does), imports
// app.py as `cf_playground`, and runs analyze_json() on the sample.
//
// Not part of CI (it downloads ~25 MB of Pyodide packages on first run).
//
//   python playground/dev_server.py --build-only   # builds playground/wheels/
//   cd playground/tests
//   npm install                                     # pyodide, pinned in package.json
//   node node_smoke.mjs                             # sample + xlsx + determinism
//   node node_smoke.mjs --big=200000                # also time a generated 200k-row CSV
//   node node_smoke.mjs --out=some/dir              # also write recipe/code/csv there
//
// Exit code 0 = every check passed.

import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { createRequire } from "node:module";

const here = path.dirname(fileURLToPath(import.meta.url));
const playground = path.resolve(here, "..");
const args = Object.fromEntries(
  process.argv.slice(2).map((a) => {
    const [k, v] = a.replace(/^--/, "").split("=");
    return [k, v ?? "1"];
  }),
);

const workerSrc = readFileSync(path.join(playground, "worker.js"), "utf8");
const wantVersion = /const PYODIDE_VERSION = "([^"]+)"/.exec(workerSrc)?.[1];
const packages = JSON.parse(/const PACKAGES = (\[[^\]]*\])/.exec(workerSrc)?.[1] ?? "null");
if (!wantVersion || !packages) throw new Error("could not read PYODIDE_VERSION/PACKAGES from worker.js");

const require = createRequire(import.meta.url);
const npmVersion = require("pyodide/package.json").version;
if (npmVersion !== wantVersion) {
  throw new Error(`worker.js pins Pyodide ${wantVersion} but node_modules has ${npmVersion}`);
}
const { loadPyodide } = await import("pyodide");

let failures = 0;
const check = (ok, label) => {
  console.log(`${ok ? "PASS" : "FAIL"}  ${label}`);
  if (!ok) failures += 1;
};
const t0 = performance.now();
const lap = () => `${((performance.now() - t0) / 1000).toFixed(2)}s`;

const manifestPath = path.join(playground, "wheels", "manifest.json");
let manifest;
try {
  manifest = JSON.parse(readFileSync(manifestPath, "utf8"));
} catch {
  console.error(`missing ${manifestPath}: run \`python playground/dev_server.py --build-only\` first`);
  process.exit(2);
}

const py = await loadPyodide({ stdout: () => {}, stderr: () => {} });
console.log(`[${lap()}] Pyodide ${py.version} runtime loaded`);
const loadErrors = [];
await py.loadPackage(packages, { messageCallback: () => {}, errorCallback: (m) => loadErrors.push(m) });
check(loadErrors.length === 0, `loadPackage(${packages.join(", ")}) ${loadErrors.join("; ")}`);
console.log(`[${lap()}] packages loaded`);

async function installWheels(names) {
  py.FS.mkdirTree("/wheels");
  for (const n of names) py.FS.writeFile(`/wheels/${n}`, readFileSync(path.join(playground, "wheels", n)));
  const micropip = py.pyimport("micropip");
  try {
    await micropip.install.callKwargs(names.map((n) => `emfs:/wheels/${n}`), { deps: false });
  } finally {
    micropip.destroy();
  }
}
await installWheels([manifest.wheel]);
py.FS.writeFile("/home/pyodide/cf_playground.py", readFileSync(path.join(playground, "app.py"), "utf8"));
const info = JSON.parse(
  py.runPython(
    [
      "import json, sys",
      "if '/home/pyodide' not in sys.path: sys.path.insert(0, '/home/pyodide')",
      "import cf_playground",
      "json.dumps({**cf_playground.runtime_info(), 'api': cf_playground.API_VERSION})",
    ].join("\n"),
  ),
);
console.log(`[${lap()}] CleanFrame installed:`, JSON.stringify(info));
check(info.cleanframe === manifest.version, `installed CleanFrame ${info.cleanframe} matches manifest ${manifest.version}`);
const analyzeJson = py.globals.get("cf_playground").analyze_json;

function run(bytes, name) {
  const s = performance.now();
  const json = analyzeJson(bytes, name);
  return { json, out: JSON.parse(json), ms: performance.now() - s };
}

// -- the sample ---------------------------------------------------------------
const sample = new Uint8Array(readFileSync(path.join(playground, "sample", "messy_customers.csv")));
const first = run(sample, "messy_customers.csv");
const r = first.out;
check(r.ok === true, `sample analyzed in ${first.ms.toFixed(0)} ms (first call)`);
if (r.ok) {
  console.log(`      Mess Score ${r.score.mess}/100 (${r.score.band}); quality ${r.score.quality} ${r.score.grade}; ` +
    `penalty ${r.score.penalty} over ${r.score.columns} columns`);
  for (const k of r.kinds) console.log(`      - [${k.severity}] ${k.text}`);
  console.log(`      diff: ${r.diff.changed_cells} cells in ${r.diff.changed_columns} columns, ` +
    `${r.diff.rows_before} -> ${r.diff.rows_after} rows, ${r.diff.quarantined} quarantined; ${r.changes.length} shown`);
  console.log(`      share: ${r.share.text}`);
  console.log(`      sizes: recipe ${r.recipe_yaml.length} B, code ${r.code.length} B, cleaned csv ${r.cleaned_csv.length} B, ` +
    `report ${r.report_html.length} B, clean report ${r.clean_report_html.length} B`);
  check(r.score.mess >= 0 && r.score.mess <= 100, "mess score within 0..100");
  check(r.changes.length <= 200, "change list is bounded");
  check(/^version: 1$/m.test(r.recipe_yaml), "recipe YAML looks like a v1 recipe");
  check(r.code.includes("def clean("), "generated code defines clean()");
  const shareBlob = JSON.stringify(r.share);
  const leaks = ["Customer Name", "Signup Date", "Bengaluru", "alice", "messy_customers"].filter((s) => shareBlob.includes(s));
  check(leaks.length === 0, `share payload carries no column names / values / file name ${leaks.join(", ")}`);
}
const second = run(sample, "messy_customers.csv");
check(second.json === first.json, `deterministic: identical JSON on a second run (${second.ms.toFixed(0)} ms warm)`);

// -- error paths ----------------------------------------------------------------
for (const [label, bytes, name] of [
  ["empty file", new Uint8Array(0), "empty.csv"],
  ["binary garbage", new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0, 0, 0, 13, 0, 0]), "photo.csv"],
  ["Shift-JIS text", new Uint8Array([0x93, 0xfa, 0x96, 0x7b, 0x8c, 0xea, 0x2c, 0x41, 0x0a, 0x93, 0x8c, 0x8b, 0x9e, 0x2c, 0x42, 0x0a]), "jp.csv"],
]) {
  const e = run(bytes, name).out;
  check(e.ok === false && typeof e.error === "string" && !e.error.includes("/tmp"), `${label} -> clean error: ${e.error}`);
}

// -- .xlsx via the vendored openpyxl wheels --------------------------------------
if (Array.isArray(manifest.xlsx) && manifest.xlsx.length) {
  const s = performance.now();
  await installWheels(manifest.xlsx);
  console.log(`[${lap()}] Excel wheels installed in ${(performance.now() - s).toFixed(0)} ms`);
  py.FS.writeFile("/tmp/sample.csv", sample);
  py.runPython([
    "import pandas as pd",
    "pd.read_csv('/tmp/sample.csv', dtype=str, keep_default_na=False).to_excel('/tmp/sample.xlsx', index=False)",
  ].join("\n"));
  const x = run(py.FS.readFile("/tmp/sample.xlsx"), "messy_customers.xlsx");
  check(x.out.ok === true, `xlsx analyzed in ${x.ms.toFixed(0)} ms: mess ${x.out.score?.mess} ${x.out.error ?? ""}`);
} else {
  console.log("SKIP  xlsx (manifest lists no Excel wheels)");
}

// -- optional: a generated big file -----------------------------------------------
if (args.big) {
  const n = Number(args.big);
  py.runPython(`
import random
random.seed(7)
cities = ["Bengaluru", "bengaluru ", "BENGALURU", "Mumbai", "mumbai", "Delhi", " delhi", "Pune"]
dates = ["31/01/2024", "2024-02-15", "1 Jan 2024", "15/03/2024", "N/A", ""]
with open("/tmp/big.csv", "w", encoding="utf-8") as fh:
    fh.write("Customer Name,Signup Date,Amount,City,Email,Phone\\n")
    for i in range(${n}):
        fh.write(f"  person {i} ,{random.choice(dates)},\\"\\u20b9{random.randint(0, 99999):,}\\",{random.choice(cities)},"
                 f"P{i}@Example.com ,+91 98765 {random.randint(10000, 99999)}\\n")
`);
  const big = py.FS.readFile("/tmp/big.csv");
  const b = run(big, "big.csv");
  check(b.out.ok === true, `${n.toLocaleString()} rows (${(big.length / 2 ** 20).toFixed(1)} MB) analyzed in ` +
    `${(b.ms / 1000).toFixed(1)} s; mess ${b.out.score?.mess}; result JSON ${(b.json.length / 2 ** 20).toFixed(1)} MB ${b.out.error ?? ""}`);
}

if (args.out && r.ok) {
  mkdirSync(args.out, { recursive: true });
  writeFileSync(path.join(args.out, "messy_customers.recipe.yaml"), r.recipe_yaml);
  writeFileSync(path.join(args.out, "clean_messy_customers.py"), r.code);
  writeFileSync(path.join(args.out, "messy_customers.clean.csv"), r.cleaned_csv);
  console.log(`wrote recipe, code and cleaned CSV to ${args.out}`);
}

console.log(`[${lap()}] done: ${failures ? `${failures} FAILED` : "all checks passed"}`);
process.exit(failures ? 1 : 0);
