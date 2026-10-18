"""Build the playground's wheel bundle and serve ``playground/`` on localhost.

    python playground/dev_server.py              # build, then serve on http://127.0.0.1:8000/
    python playground/dev_server.py --port 9000
    python playground/dev_server.py --no-build   # serve whatever is already in wheels/
    python playground/dev_server.py --build-only # what the Pages workflow runs

The build step writes, into ``playground/wheels/`` (git-ignored):

* ``cleanframe_engine-<version>-py3-none-any.whl``, built from this checkout, so the
  page always runs the code on your branch (no waiting on PyPI);
* the pure-Python ``openpyxl`` + ``et_xmlfile`` wheels that add .xlsx support
  (pinned below; skipped with a warning if PyPI is unreachable);
* ``manifest.json``: ``{"wheel": ..., "version": ..., "xlsx": [...]}``, which
  ``worker.js`` reads to know what to install.

The server is stdlib-only, binds to 127.0.0.1, and forces correct MIME types
(module workers refuse ``.js`` served as anything but JavaScript, and Windows
takes MIME types from the registry).
"""

from __future__ import annotations

import argparse
import functools
import http.server
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
WHEELS = HERE / "wheels"
#: Pure-Python wheels for .xlsx input. Pyodide's own index does not ship openpyxl.
XLSX_REQUIREMENTS = ("openpyxl==3.1.5", "et_xmlfile==2.0.0")

MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".py": "text/plain; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".whl": "application/zip",
    ".wasm": "application/wasm",
    ".txt": "text/plain; charset=utf-8",
}


def _run(cmd: list[str]) -> None:
    print("$", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)


def build_wheels(*, xlsx: bool = True) -> dict:
    """Rebuild ``playground/wheels/`` from scratch and return the manifest."""
    WHEELS.mkdir(exist_ok=True)
    for old in [*WHEELS.glob("*.whl"), WHEELS / "manifest.json"]:
        old.unlink(missing_ok=True)

    with tempfile.TemporaryDirectory(prefix="cf-wheel-") as tmp:
        _run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--quiet", "-w", tmp, str(REPO)])
        built = sorted(Path(tmp).glob("cleanframe_engine-*-py3-none-any.whl"))
        if len(built) != 1:
            raise SystemExit(f"expected one cleanframe_engine wheel, found {[p.name for p in built]}")
        wheel = WHEELS / built[0].name
        shutil.copyfile(built[0], wheel)

    version = wheel.name.split("-")[1]
    manifest: dict = {"wheel": wheel.name, "version": version, "xlsx": []}

    if xlsx:
        try:
            _run([
                sys.executable, "-m", "pip", "download", "--quiet", "--no-deps",
                "--only-binary=:all:", "-d", str(WHEELS), *XLSX_REQUIREMENTS,
            ])
        except subprocess.CalledProcessError:
            print("warning: could not download the openpyxl wheels; .xlsx input is disabled.",
                  file=sys.stderr)
        else:
            extra = sorted(
                p.name for p in WHEELS.glob("*.whl")
                if p.name != wheel.name and p.name.lower().startswith(("openpyxl-", "et_xmlfile-"))
            )
            not_pure = [n for n in extra if not n.endswith("-none-any.whl")]
            if not_pure:
                raise SystemExit(f"expected pure-Python wheels, got {not_pure}")
            # et_xmlfile first: openpyxl imports it.
            manifest["xlsx"] = sorted(extra, key=lambda n: not n.lower().startswith("et_xmlfile"))

    (WHEELS / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {WHEELS / 'manifest.json'}: {manifest}", flush=True)
    return manifest


class _Handler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map, **MIME_TYPES}

    def guess_type(self, path):  # registry-proof on Windows
        suffix = Path(str(path)).suffix.lower()
        return MIME_TYPES.get(suffix) or super().guess_type(path)

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()


def serve(port: int) -> None:
    handler = functools.partial(_Handler, directory=str(HERE))
    with http.server.ThreadingHTTPServer(("127.0.0.1", port), handler) as httpd:
        print(f"CleanFrame playground: http://127.0.0.1:{port}/  (Ctrl+C to stop)", flush=True)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8000)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--no-build", action="store_true", help="serve without rebuilding wheels/")
    mode.add_argument("--build-only", action="store_true", help="build wheels/ and exit")
    parser.add_argument("--no-xlsx", action="store_true", help="skip the openpyxl wheels")
    args = parser.parse_args(argv)

    if not args.no_build:
        build_wheels(xlsx=not args.no_xlsx)
    elif not (WHEELS / "manifest.json").is_file():
        parser.error("wheels/manifest.json is missing; run once without --no-build")
    if args.build_only:
        return 0
    serve(args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
