#!/usr/bin/env python3
"""The engine behind ``action/action.yml`` (CleanFrame Data CI).

Three subcommands, all standard-library only, so they run on any runner Python
before CleanFrame - or anything else - is installed:

``install``
    Create an isolated virtual environment and pip-install CleanFrame into it.
    When the action runs from a CleanFrame checkout (``uses:
    inboxpraveen/Cleanframe/action@vX``) and no version is pinned, the library
    is installed from that same checkout, so action and library always match.

``run``
    Expand the ``files`` globs, replay the recipe over every file with
    ``cleanframe apply --json``, then write the job summary, the step outputs and
    the annotations, and exit with the worst exit code. One file failing never
    hides the others: every file is replayed before the step fails.

``render RESULTS``
    Re-publish a results file written by ``run`` (tests, or re-rendering a
    summary from an uploaded artifact).

Why this lives in Python rather than in the action's bash: GitHub runs
``shell: bash`` with ``-e -o pipefail`` (the first failing file would end a bash
loop), ``**`` globbing needs bash 4 (macOS ships 3.2), and Windows paths are a
quoting minefield. Inputs arrive as ``CF_*`` environment variables and the CLI is
started with an argument list - never a shell - so no input is ever evaluated.

Exit codes follow the CleanFrame CLI contract: 0 ok, 1 data/recipe/output
error, 2 usage, 3 drift stop, 4 validation failure, 70 internal, 130 interrupt.
"""

from __future__ import annotations

import argparse
import glob
import html
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_DRIFT = 3
EXIT_VALIDATION = 4
EXIT_INTERNAL = 70
EXIT_INTERRUPT = 130

DIST_NAME = "cleanframe-engine"
#: ``--json`` (one summary object on stdout, even for failures) arrived in 0.4.0.
MIN_CLEANFRAME = (0, 4, 0)
RESULTS_SCHEMA = "cleanframe-action-results/1"
MODES = ("review", "auto", "strict")
ON_DRIFT_CHOICES = ("error", "warn")
#: Excel inputs are written back as .xlsx: a workbook recipe always emits a
#: workbook, and pandas can no longer write the legacy .xls format.
EXCEL_SUFFIXES = frozenset({".xlsx", ".xlsm", ".xls"})
DOCS_URL = "https://github.com/inboxpraveen/Cleanframe/blob/main/docs/github-action.md"
ISSUES_URL = "https://github.com/inboxpraveen/Cleanframe/issues"

# The job summary is capped by GitHub at 1 MiB per step; stay far below it so a
# run over thousands of files, or one with hundreds of drift findings, still
# renders instead of being rejected.
MAX_SUMMARY_BYTES = 256 * 1024
MAX_TABLE_ROWS = 200
MAX_FINDINGS_SHOWN = 8
MAX_WARNINGS_SHOWN = 10
MAX_CELL_CHARS = 240
#: GitHub shows at most 10 error and 10 warning annotations per step.
MAX_ANNOTATIONS = 10
LOG_LINES_PER_FILE = 400
STDERR_TAIL_LINES = 20

_EXIT_LABELS: dict[int, tuple[str, str]] = {
    EXIT_ERROR: ("❌", "Error"),
    EXIT_USAGE: ("❌", "Usage error"),
    EXIT_DRIFT: ("🛑", "Schema drift"),
    EXIT_VALIDATION: ("❌", "Validation failed"),
    EXIT_INTERNAL: ("💥", "Internal error"),
    EXIT_INTERRUPT: ("⏹️", "Interrupted"),
}
_SEVERITY_RANK = {"error": 2, "warning": 1, "info": 0}
_TRUE = frozenset({"true", "yes", "on", "1"})
_FALSE = frozenset({"false", "no", "off", "0", ""})


class ConfigError(ValueError):
    """An action input is invalid; reported as a usage error (exit 2)."""


# ---------------------------------------------------------------------------
# text helpers: markdown cells and workflow commands carry untrusted text
# ---------------------------------------------------------------------------
_MD_SPECIAL = re.compile(r"([\\`*_\[\]~|])")


def _one_line(value: object) -> str:
    return " ".join(str(value).split())


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(limit - 1, 0)] + "…"


def md_text(value: object, limit: int = MAX_CELL_CHARS) -> str:
    """Make arbitrary text safe inside a markdown table cell or list item.

    Column names, file names and messages come from the data under test - in a
    pull request, from whoever opened it - so they must not be able to break the
    table (``|``, newlines), inject HTML, or restyle the summary.
    """
    text = _truncate(_one_line(value), limit)
    # HTML-escape first: the entities it produces contain no markdown specials,
    # whereas a backslash added first would escape the entity's own '&'.
    text = html.escape(text, quote=False)
    return _MD_SPECIAL.sub(r"\\\1", text)


def md_code(value: object, limit: int = MAX_CELL_CHARS) -> str:
    """Inline code for prose lines (not table cells); markdown is inert inside <code>."""
    return f"<code>{html.escape(_truncate(_one_line(value), limit), quote=False)}</code>"


def _command_data(text: str) -> str:
    """Escape a workflow-command message so a newline cannot start a new command."""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _command_property(text: str) -> str:
    return _command_data(text).replace(":", "%3A").replace(",", "%2C")


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _version_tuple(text: str) -> tuple[int, ...] | None:
    match = re.match(r"v?(\d+)\.(\d+)(?:\.(\d+))?", text.strip())
    if not match:
        return None
    return tuple(int(part or 0) for part in match.groups())


# ---------------------------------------------------------------------------
# GitHub plumbing
# ---------------------------------------------------------------------------
def _reconfigure_stdio() -> None:
    # Emoji and data text must not crash on a cp1252 console (a Windows runner,
    # or a developer running this locally).
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):  # pragma: no cover - unusual stream
            pass


def _runner_temp() -> str | None:
    value = os.environ.get("RUNNER_TEMP", "").strip()
    return value if value and os.path.isdir(value) else None


def write_outputs(outputs: Mapping[str, str], env: Mapping[str, str] | None = None) -> None:
    """Append step outputs to ``$GITHUB_OUTPUT`` (printed instead when run locally)."""
    env = os.environ if env is None else env
    target = env.get("GITHUB_OUTPUT", "").strip()
    lines: list[str] = []
    for name, value in outputs.items():
        if "\n" in value or "\r" in value:
            delimiter = f"CLEANFRAME_{secrets.token_hex(8)}"
            lines += [f"{name}<<{delimiter}", value, delimiter]
        else:
            lines.append(f"{name}={value}")
    text = "\n".join(lines) + "\n"
    if target:
        with open(target, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    else:
        print(text, end="")


def write_step_summary(markdown: str, env: Mapping[str, str] | None = None) -> None:
    """Append to ``$GITHUB_STEP_SUMMARY`` (printed instead when run locally)."""
    env = os.environ if env is None else env
    target = env.get("GITHUB_STEP_SUMMARY", "").strip()
    if target:
        with open(target, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(markdown)
            if not markdown.endswith("\n"):
                handle.write("\n")
    else:
        print(markdown)


def _print_untrusted(lines: Iterable[str]) -> None:
    """Echo text that may contain data-derived content with workflow commands off.

    A column name such as ``::add-mask::x`` on a line of its own would otherwise
    be executed by the runner; ``::stop-commands::`` suspends that until the
    random token is echoed back.
    """
    token = secrets.token_hex(16)
    print(f"::stop-commands::{token}")
    for line in lines:
        print(line)
    print(f"::{token}::")
    sys.stdout.flush()


def _annotate(level: str, message: str, *, title: str, file: str | None = None) -> None:
    props = f"title={_command_property(title)}"
    if file:
        props = f"file={_command_property(file)},{props}"
    print(f"::{level} {props}::{_command_data(_truncate(message, 1000))}")


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------
_EXACT_VERSION = re.compile(r"v?\d[0-9A-Za-z.*+!-]*")
_SPECIFIER = re.compile(
    r"(?:===|==|!=|~=|<=|>=|<|>)\s*[0-9A-Za-z.*+!-]+"
    r"(?:\s*,\s*(?:===|==|!=|~=|<=|>=|<|>)\s*[0-9A-Za-z.*+!-]+)*"
)


def is_cleanframe_checkout(root: Path) -> bool:
    """True when ``root`` is a CleanFrame source tree (not just any Python project).

    The name check matters: someone who vendors this action into their own
    repo at ``<repo>/action/`` must not have their own project pip-installed.
    """
    pyproject = root / "pyproject.toml"
    try:
        text = pyproject.read_text(encoding="utf-8")
    except OSError:
        return False
    return re.search(r"(?m)^\s*name\s*=\s*[\"']cleanframe-engine[\"']", text) is not None


def install_requirement(action_dir: Path, version: str) -> tuple[str, str]:
    """Return ``(pip requirement, human description)`` for the ``cleanframe-version`` input."""
    version = version.strip()
    if version.lower() == "latest":
        return DIST_NAME, f"the latest {DIST_NAME} release from PyPI"
    if version:
        if _EXACT_VERSION.fullmatch(version):
            exact = version[1:] if version[:1] in "vV" else version
            parsed = _version_tuple(exact)
            if parsed is not None and parsed < MIN_CLEANFRAME:
                minimum = ".".join(map(str, MIN_CLEANFRAME))
                raise ConfigError(
                    f"cleanframe-version {version} is too old: the action needs CleanFrame "
                    f"{minimum} or later (it relies on `cleanframe apply --json`)."
                )
            return f"{DIST_NAME}=={exact}", f"{DIST_NAME}=={exact} from PyPI"
        if _SPECIFIER.fullmatch(version):
            spec = re.sub(r"\s+", "", version)
            return f"{DIST_NAME}{spec}", f"{DIST_NAME}{spec} from PyPI"
        raise ConfigError(
            f"cleanframe-version {version!r} is not a version (e.g. 0.5.0), a PEP 440 "
            "specifier (e.g. >=0.5,<0.6), 'latest', or empty."
        )
    root = action_dir.parent
    if is_cleanframe_checkout(root):
        return str(root), f"the action's own CleanFrame checkout ({root})"
    return DIST_NAME, f"the latest {DIST_NAME} release from PyPI"


def split_packages(raw: str) -> list[str]:
    """``extra-packages``: whitespace/newline separated pip arguments."""
    return [token for token in raw.split() if token]


def venv_python(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def cleanframe_command(python: Path) -> list[str]:
    """How to start the CLI that belongs to ``python``.

    The console script is preferred over ``python -m cleanframe`` because ``-m``
    puts the working directory - the repository under test, possibly a pull
    request's - first on ``sys.path``, where a planted ``pandas.py`` would run.
    """
    bindir = python.parent
    for candidate in (
        bindir / "cleanframe",
        bindir / "cleanframe.exe",
        bindir / "Scripts" / "cleanframe.exe",
    ):
        if candidate.is_file():
            return [str(candidate)]
    return [str(python), "-m", "cleanframe"]


def _run_grouped(title: str, argv: Sequence[str]) -> int:
    print(f"::group::{_command_data(title)}")
    sys.stdout.flush()
    try:
        return subprocess.run(list(argv), stdin=subprocess.DEVNULL, check=False).returncode
    except OSError as exc:
        print(f"could not start {argv[0]}: {exc}")
        return EXIT_ERROR
    finally:
        sys.stdout.flush()
        print("::endgroup::")


def cmd_install(env: Mapping[str, str]) -> int:
    try:
        requirement, source = install_requirement(
            Path(__file__).resolve().parent, env.get("CF_CLEANFRAME_VERSION", "")
        )
    except ConfigError as exc:
        _annotate("error", str(exc), title="CleanFrame Data CI: invalid input")
        return EXIT_USAGE
    extras = split_packages(env.get("CF_EXTRA_PACKAGES", ""))

    venv_dir = Path(tempfile.mkdtemp(prefix="cleanframe-venv-", dir=_runner_temp()))
    print(f"Installing CleanFrame from {source}")
    if _run_grouped("Create an isolated virtual environment", [sys.executable, "-m", "venv", str(venv_dir)]):
        _annotate(
            "error",
            f"could not create a virtual environment with {sys.executable} "
            "(on Debian/Ubuntu self-hosted runners install python3-venv).",
            title="CleanFrame Data CI: install failed",
        )
        return EXIT_ERROR
    python = venv_python(venv_dir)
    pip = [
        str(python), "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
        "--progress-bar", "off", requirement, *extras,
    ]
    if _run_grouped(f"pip install {requirement} {' '.join(extras)}".strip(), pip):
        _annotate(
            "error", f"pip could not install {requirement} {' '.join(extras)}".strip(),
            title="CleanFrame Data CI: install failed",
        )
        return EXIT_ERROR

    probe = subprocess.run(
        [*cleanframe_command(python), "--version"],
        stdin=subprocess.DEVNULL, capture_output=True, check=False,
    )
    reported = probe.stdout.decode("utf-8", "replace").strip()
    installed = reported.split()[-1] if reported else ""
    parsed = _version_tuple(installed)
    if probe.returncode != 0 or parsed is None:
        _annotate(
            "error", f"the installed CleanFrame does not run: {reported or probe.returncode}",
            title="CleanFrame Data CI: install failed",
        )
        return EXIT_ERROR
    if parsed < MIN_CLEANFRAME:
        minimum = ".".join(map(str, MIN_CLEANFRAME))
        _annotate(
            "error", f"CleanFrame {installed} is installed but the action needs {minimum} or later.",
            title="CleanFrame Data CI: install failed",
        )
        return EXIT_ERROR
    print(f"Installed cleanframe {installed} into {venv_dir}")
    write_outputs({"python": python.as_posix(), "version": installed}, env)
    return EXIT_OK


# ---------------------------------------------------------------------------
# run: configuration, file expansion, replay
# ---------------------------------------------------------------------------
@dataclass
class RunConfig:
    patterns: list[str]
    recipe: str
    out_dir: str | None
    mode: str
    on_drift: str
    fail_on_quarantine: bool


def split_patterns(raw: str) -> list[str]:
    """Split the ``files`` input.

    With newlines, every non-empty line is one entry, so paths containing spaces
    work in a YAML block (``files: |``) and ``#`` lines are comments. A single line
    is split on whitespace.
    """
    if "\n" in raw or "\r" in raw:
        entries = (line.strip() for line in raw.splitlines())
        return [entry for entry in entries if entry and not entry.startswith("#")]
    return raw.split()


def parse_bool(raw: str, name: str) -> bool:
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ConfigError(f"{name} must be true or false, got {raw!r}.")


def read_run_config(env: Mapping[str, str]) -> tuple[RunConfig, list[str]]:
    """Validate every input up front and report all problems at once."""
    errors: list[str] = []
    patterns = split_patterns(env.get("CF_FILES", ""))
    if not patterns:
        errors.append("The files input is empty: give at least one data file path or glob.")
    recipe = env.get("CF_RECIPE", "").strip()
    if not recipe:
        errors.append("The recipe input is empty: give the path of a committed recipe YAML.")
    elif not os.path.isfile(recipe):
        errors.append(f"Recipe not found: {recipe} (paths are relative to the workspace root).")
    mode = env.get("CF_MODE", "review").strip().lower() or "review"
    if mode not in MODES:
        errors.append(f"mode must be one of {', '.join(MODES)}; got {mode!r}.")
    on_drift = env.get("CF_ON_DRIFT", "error").strip().lower() or "error"
    if on_drift not in ON_DRIFT_CHOICES:
        errors.append(f"on-drift must be one of {', '.join(ON_DRIFT_CHOICES)}; got {on_drift!r}.")
    try:
        fail_on_quarantine = parse_bool(env.get("CF_FAIL_ON_QUARANTINE", ""), "fail-on-quarantine")
    except ConfigError as exc:
        errors.append(str(exc))
        fail_on_quarantine = False
    out_dir = env.get("CF_OUT_DIR", "").strip() or None
    if out_dir and os.path.exists(out_dir) and not os.path.isdir(out_dir):
        errors.append(f"out-dir {out_dir} exists and is not a directory.")
    config = RunConfig(
        patterns=patterns, recipe=recipe, out_dir=out_dir, mode=mode,
        on_drift=on_drift, fail_on_quarantine=fail_on_quarantine,
    )
    return config, errors


@dataclass
class Target:
    """One row of the run: a matched file to replay, or an entry that cannot run."""

    display: str
    path: str | None = None
    output: Path | None = None
    note: str | None = None


def expand_patterns(patterns: Sequence[str]) -> list[Target]:
    """Resolve paths/globs in order; a pattern matching no file is an error row.

    Matches are sorted per pattern for a deterministic summary, directories are
    skipped, and a file matched by two patterns is replayed once.
    """
    targets: list[Target] = []
    seen: set[str] = set()
    for pattern in patterns:
        matches = sorted(m for m in glob.glob(pattern, recursive=True) if os.path.isfile(m))
        if not matches:
            targets.append(
                Target(
                    display=pattern,
                    note=f"{pattern} matched no files (directories are skipped; "
                    "paths are relative to the workspace root).",
                )
            )
            continue
        for match in matches:
            key = os.path.normcase(os.path.abspath(match))
            if key in seen:
                continue
            seen.add(key)
            targets.append(Target(display=Path(match).as_posix(), path=match))
    return targets


def output_name(path: str) -> str:
    source = Path(path)
    suffix = ".clean.xlsx" if source.suffix.lower() in EXCEL_SUFFIXES else ".clean.csv"
    return source.stem + suffix


def plan_outputs(targets: list[Target], out_dir: str | None, scratch: Path) -> None:
    """Choose each file's output path; in ``out-dir`` two files may not share one."""
    runnable = [t for t in targets if t.path is not None]
    if out_dir is None:
        # Check-only run: `apply` always writes, so give each file its own scratch dir.
        for index, target in enumerate(runnable, start=1):
            target.output = scratch / f"{index:04d}" / output_name(target.path or "")
        return
    claimed: dict[str, list[Target]] = {}
    for target in runnable:
        name = output_name(target.path or "")
        claimed.setdefault(os.path.normcase(name), []).append(target)
        target.output = Path(out_dir) / name
    for group in claimed.values():
        if len(group) < 2:
            continue
        names = ", ".join(t.display for t in group)
        for target in group:
            target.note = (
                f"output {target.output.name if target.output else ''} in out-dir would be "
                f"written by more than one input ({names}); rename the files or split the run."
            )
            target.path = None
            target.output = None


def _cli_arg(value: str) -> str:
    # A path such as "-x.csv" would be parsed as an option by argparse.
    return f".{os.sep}{value}" if value.startswith("-") else value


def _tail(text: str, lines: int = STDERR_TAIL_LINES) -> str:
    kept = [line.rstrip() for line in text.splitlines() if line.strip()]
    return "\n".join(_truncate(line, 300) for line in kept[-lines:])


def parse_payload(stdout: str) -> dict | None:
    """The CLI's ``--json`` object, or None when stdout is not one JSON object."""
    text = stdout.strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def replay(cli: Sequence[str], target: Target, config: RunConfig) -> dict:
    """Run ``cleanframe apply --json`` on one file and record what happened."""
    assert target.path is not None and target.output is not None
    target.output.parent.mkdir(parents=True, exist_ok=True)
    argv = [
        *cli, "apply", _cli_arg(target.path), "--recipe", _cli_arg(config.recipe),
        "--out", str(target.output), "--mode", config.mode, "--json",
    ]
    if config.on_drift == "warn":
        argv.append("--force")
    record: dict = {
        "file": target.display,
        "output": str(target.output) if config.out_dir else None,
    }
    child_env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    try:
        proc = subprocess.run(
            argv, stdin=subprocess.DEVNULL, capture_output=True, env=child_env, check=False
        )
    except OSError as exc:
        record.update(
            exit_code=EXIT_INTERNAL, payload=None, stdout="",
            stderr_tail=f"could not start the CleanFrame CLI ({argv[0]}): {exc}",
        )
        return record
    stdout = proc.stdout.decode("utf-8", "replace")
    stderr = proc.stderr.decode("utf-8", "replace")
    # A signal-killed child reports -N on POSIX; map it the way a shell would.
    code = 128 - proc.returncode if proc.returncode < 0 else proc.returncode

    print(f"::group::{_command_data(f'cleanframe apply {target.display} (exit {code})')}")
    lines = stderr.splitlines()
    shown = lines[:LOG_LINES_PER_FILE]
    if len(lines) > LOG_LINES_PER_FILE:
        shown.append(f"... {len(lines) - LOG_LINES_PER_FILE} more line(s) not shown")
    _print_untrusted(shown)
    print("::endgroup::")

    payload = parse_payload(stdout)
    record.update(exit_code=code, payload=payload)
    if payload is None:
        # Only kept for diagnosis; an ok run's stderr holds sample cell values.
        record.update(stdout=_truncate(stdout, 4000), stderr_tail=_tail(stderr))
    return record


def cmd_run(env: Mapping[str, str]) -> int:
    config, errors = read_run_config(env)
    workdir = Path(tempfile.mkdtemp(prefix="cleanframe-run-", dir=_runner_temp()))
    records: list[dict] = []
    if not errors:
        targets = expand_patterns(config.patterns)
        plan_outputs(targets, config.out_dir, workdir / "outputs")
        cli = cleanframe_command(Path(sys.executable))
        for target in targets:
            if target.path is None:
                records.append(
                    {"file": target.display, "exit_code": EXIT_USAGE, "payload": None,
                     "note": target.note, "output": None}
                )
            else:
                records.append(replay(cli, target, config))
    results = {
        "schema": RESULTS_SCHEMA,
        "recipe": config.recipe,
        "mode": config.mode,
        "on_drift": config.on_drift,
        "fail_on_quarantine": config.fail_on_quarantine,
        "out_dir": config.out_dir,
        "errors": errors,
        "files": records,
    }
    results_path = workdir / "cleanframe-results.json"
    return publish(results, results_path, env)


# ---------------------------------------------------------------------------
# classification and rendering
# ---------------------------------------------------------------------------
@dataclass
class Outcome:
    file: str
    exit_code: int
    effective_exit: int
    level: str  # "passed" | "warning" | "failed"
    icon: str
    label: str
    is_file: bool = True
    changed_cells: int | None = None
    quarantined: int | None = None
    has_drift: bool = False
    findings: list[dict] = field(default_factory=list)
    message: str | None = None
    warnings: list[str] = field(default_factory=list)
    version: str | None = None

    @property
    def failed(self) -> bool:
        return self.effective_exit != EXIT_OK


def classify(record: Mapping, *, fail_on_quarantine: bool) -> Outcome:
    """Turn one recorded CLI run into a row. Every key of the payload is optional:
    a drift stop has no ``diff``, a usage error has no ``input``, a crash no JSON."""
    code = _as_int(record.get("exit_code"))
    code = EXIT_INTERNAL if code is None else code
    raw_payload = record.get("payload")
    payload: dict | None = raw_payload if isinstance(raw_payload, dict) else None
    note = record.get("note")
    out = Outcome(
        file=str(record.get("file") or "?"), exit_code=code, effective_exit=code,
        level="failed", icon="❌", label="Failed", is_file=not note,
    )
    if payload is not None:
        out.version = str(payload.get("cleanframe_version") or "") or None
        drift = payload.get("drift")
        if isinstance(drift, dict):
            out.has_drift = bool(drift.get("has_drift"))
            out.findings = [f for f in drift.get("findings") or [] if isinstance(f, dict)]
        if payload.get("status") == "drift":
            out.has_drift = True
        diff = payload.get("diff")
        changed = diff.get("changed_cells") if isinstance(diff, dict) else None
        out.changed_cells = _as_int(changed if changed is not None else payload.get("changed_cells"))
        out.quarantined = _as_int(payload.get("rows_quarantined"))
        error = payload.get("error")
        if isinstance(error, dict) and error.get("message"):
            out.message = str(error["message"])
        warnings = payload.get("warnings")
        if isinstance(warnings, list):
            out.warnings = [str(w) for w in warnings if w]

    if note:
        out.label = f"Usage error (exit {code})"
        out.message = str(note)
        return out
    if payload is None:
        tail = str(record.get("stderr_tail") or "").strip()
        last = " / ".join(tail.splitlines()[-3:]) if tail else ""
        if code == EXIT_OK:
            out.effective_exit = EXIT_INTERNAL
            out.icon, out.label = "💥", "No JSON summary"
        else:
            icon, label = _EXIT_LABELS.get(code, ("❌", "Failed"))
            out.icon, out.label = icon, f"{label} (exit {code})"
        out.message = last or (
            "cleanframe printed no JSON summary (CleanFrame 0.4.0 or later is required)."
        )
        return out
    if code == EXIT_OK:
        if fail_on_quarantine and (out.quarantined or 0) > 0:
            out.effective_exit = EXIT_VALIDATION
            out.label = "Rows quarantined"
            out.message = (
                f"{_plural(out.quarantined or 0, 'row')} failed validation and were "
                "quarantined; fail-on-quarantine is on."
            )
        elif out.has_drift:
            out.level, out.icon, out.label = "warning", "⚠️", "Applied despite drift"
        else:
            out.level, out.icon, out.label = "passed", "✅", "Passed"
        return out
    icon, label = _EXIT_LABELS.get(code, ("❌", "Failed"))
    out.icon, out.label = icon, f"{label} (exit {code})"
    return out


def _findings_lines(outcome: Outcome) -> list[str]:
    ranked = sorted(
        outcome.findings,
        key=lambda f: -_SEVERITY_RANK.get(str(f.get("severity", "")).lower(), 0),
    )
    lines = []
    for finding in ranked[:MAX_FINDINGS_SHOWN]:
        severity = md_text(finding.get("severity") or "finding", 20)
        message = finding.get("message") or finding.get("kind") or ""
        lines.append(f"• **{severity}** {md_text(message)}")
    if len(ranked) > MAX_FINDINGS_SHOWN:
        lines.append(f"• … and {len(ranked) - MAX_FINDINGS_SHOWN} more")
    return lines


def _details_cell(outcome: Outcome) -> str:
    parts: list[str] = []
    if outcome.message:
        parts.append(md_text(outcome.message))
    if outcome.has_drift:
        parts.extend(_findings_lines(outcome))
    return "<br>".join(parts) if parts else "—"


def _count_cell(value: int | None) -> str:
    return "—" if value is None else f"{value:,}"


def _table_row(outcome: Outcome) -> str:
    return (
        f"| {outcome.icon} | {md_text(outcome.file)} | {md_text(outcome.label, 80)} | "
        f"{_count_cell(outcome.changed_cells)} | {_count_cell(outcome.quarantined)} | "
        f"{_details_cell(outcome)} |"
    )


def _fix_hints(outcomes: Sequence[Outcome], recipe: str, has_errors: bool) -> list[str]:
    codes = {o.effective_exit for o in outcomes if o.failed}
    quarantine_failed = any(
        o.exit_code == EXIT_OK and o.effective_exit == EXIT_VALIDATION for o in outcomes
    )
    hints = []
    if EXIT_DRIFT in codes:
        hints.append(
            "🛑 **Schema drift** — the file no longer matches the recipe. Run "
            f"<code>cleanframe suggest FILE --recipe {html.escape(_one_line(recipe))} --update</code> "
            "locally, review the <code>*.patched.yaml</code>, and commit it as the recipe. "
            "To report drift without failing, set <code>on-drift: warn</code> "
            "(ignored under <code>mode: strict</code>)."
        )
    if any(o.failed and o.exit_code == EXIT_VALIDATION for o in outcomes):
        hints.append(
            "❌ **Validation failed** — a check with <code>on_fail: error</code> failed, or "
            "<code>mode: strict</code> met a failing check. Fix the data, or change the "
            "check in the recipe."
        )
    if quarantine_failed:
        hints.append(
            "❌ **Rows quarantined** — rows failed a validation and were set aside; "
            "<code>fail-on-quarantine: true</code> treats that as a failure. Re-run "
            "<code>cleanframe apply ... --quarantine rows.csv</code> locally to see them."
        )
    if EXIT_USAGE in codes or has_errors:
        hints.append(
            "❌ **Usage error** — check the action inputs: every <code>files</code> pattern "
            "must match a file, and paths are relative to the workspace root."
        )
    unknown = {c for c in codes if c not in _EXIT_LABELS}
    if EXIT_ERROR in codes or unknown:
        hints.append(
            "❌ **Error** — a file or the recipe could not be read or written; the message "
            "above and the job log say which."
        )
    if EXIT_INTERNAL in codes:
        hints.append(
            "💥 **Internal error** — this is a CleanFrame bug. Re-run the file locally with "
            f"<code>--debug</code> and report it at {ISSUES_URL}."
        )
    return hints


def _verdict(outcomes: Sequence[Outcome], errors: Sequence[str], worst: int) -> tuple[str, str]:
    failed = [o for o in outcomes if o.failed]
    if errors:
        return "❌ Failed", "**❌ Configuration error — no file was replayed** (exit 2)."
    if not outcomes:
        return "❌ Failed", "**❌ No files were checked.**"
    if failed:
        label = _EXIT_LABELS.get(worst, ("", "failure"))[1].lower()
        return "❌ Failed", (
            f"**❌ {len(failed)} of {_plural(len(outcomes), 'file')} failed** — "
            f"worst exit code {worst} ({label})."
        )
    warned = [o for o in outcomes if o.level == "warning"]
    if warned:
        return "⚠️ Passed with drift", (
            f"**⚠️ All {_plural(len(outcomes), 'file')} replayed; drift was applied in "
            f"{len(warned)} (on-drift: warn).**"
        )
    return "✅ Passed", f"**✅ All {_plural(len(outcomes), 'file')} passed.**"


def _warnings_block(outcomes: Sequence[Outcome], budget: int) -> str:
    noisy = [o for o in outcomes if o.warnings]
    if not noisy:
        return ""
    total = sum(len(o.warnings) for o in noisy)
    lines = [
        f"<details><summary>⚠️ CleanFrame warnings ({total})</summary>",
        "",
    ]
    used = sum(len(line.encode("utf-8")) + 1 for line in lines) + 32
    for outcome in noisy:
        chunk = [f"**{md_text(outcome.file)}**", ""]
        chunk += [f"- {md_text(w)}" for w in outcome.warnings[:MAX_WARNINGS_SHOWN]]
        if len(outcome.warnings) > MAX_WARNINGS_SHOWN:
            chunk.append(f"- … and {len(outcome.warnings) - MAX_WARNINGS_SHOWN} more")
        chunk.append("")
        size = sum(len(line.encode("utf-8")) + 1 for line in chunk)
        if used + size > budget:
            lines.append("_More warnings are in the job log._")
            lines.append("")
            break
        lines += chunk
        used += size
    lines.append("</details>")
    return "\n".join(lines)


def compute_outputs(outcomes: Sequence[Outcome], errors: Sequence[str]) -> dict[str, str]:
    worst = max([o.effective_exit for o in outcomes] + ([EXIT_USAGE] if errors else [EXIT_OK]))
    return {
        "drift": "true" if any(o.has_drift for o in outcomes) else "false",
        "changed-cells": str(sum(o.changed_cells or 0 for o in outcomes)),
        "quarantined": str(sum(o.quarantined or 0 for o in outcomes)),
        "failed-files": str(sum(1 for o in outcomes if o.failed)),
        "exit-code": str(worst),
    }


def render_markdown(results: Mapping, outcomes: Sequence[Outcome]) -> str:
    """The job-summary section: header, per-file table, verdict, fix hints."""
    errors = [str(e) for e in results.get("errors") or []]
    outputs = compute_outputs(outcomes, errors)
    worst = int(outputs["exit-code"])
    title, verdict = _verdict(outcomes, errors, worst)
    recipe = str(results.get("recipe") or "")
    version = next((o.version for o in outcomes if o.version), None)

    context = [f"Recipe {md_code(recipe)}" if recipe else "No recipe"]
    context.append(f"mode {md_code(results.get('mode') or 'review')}")
    context.append(f"on-drift {md_code(results.get('on_drift') or 'error')}")
    if results.get("fail_on_quarantine"):
        context.append("fail-on-quarantine")
    if version:
        context.append(f"CleanFrame {md_text(version, 40)}")
    head = [f"## 🧹 CleanFrame Data CI · {title}", "", " · ".join(context), ""]
    if errors:
        head += ["**Configuration error:**", ""] + [f"- {md_text(e, 500)}" for e in errors] + [""]

    totals = (
        f"{_plural(int(outputs['changed-cells']), 'cell')} changed · "
        f"{_plural(int(outputs['quarantined']), 'row')} quarantined"
    )
    tail = ["", f"{verdict} {totals}.", ""]
    hints = _fix_hints(outcomes, recipe, bool(errors))
    if hints:
        tail += ["**How to fix**", ""] + [f"- {hint}" for hint in hints] + [""]
    tail.append(f"<sub>Generated by <a href=\"{DOCS_URL}\">CleanFrame Data CI</a>.</sub>")

    def size(lines: Sequence[str]) -> int:
        return sum(len(line.encode("utf-8")) + 1 for line in lines)

    budget = MAX_SUMMARY_BYTES - size(head) - size(tail) - 512
    table: list[str] = []
    if outcomes:
        table = [
            "| | File | Result | Changed cells | Quarantined | Drift findings / details |",
            "|:-:|:--|:--|--:|--:|:--|",
        ]
        used = size(table)
        shown = 0
        for outcome in outcomes:
            row = _table_row(outcome)
            if shown >= MAX_TABLE_ROWS or used + len(row.encode("utf-8")) + 1 > budget * 3 // 4:
                break
            table.append(row)
            used += len(row.encode("utf-8")) + 1
            shown += 1
        if shown < len(outcomes):
            hidden = outcomes[shown:]
            hidden_failed = sum(1 for o in hidden if o.failed)
            table.append(
                f"| … | {len(hidden)} more file(s) not shown ({hidden_failed} failed) | "
                "see the job log and the results-file output | | | |"
            )
        budget -= size(table)
    warnings = _warnings_block(outcomes, max(budget, 0))
    body = head + table + ([""] + [warnings] if warnings else []) + tail
    text = "\n".join(body) + "\n"
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_SUMMARY_BYTES:  # pragma: no cover - the budget above prevents it
        text = encoded[: MAX_SUMMARY_BYTES - 64].decode("utf-8", "ignore") + "\n\n_(truncated)_\n"
    return text


def _log_lines(outcomes: Sequence[Outcome], verdict_title: str) -> list[str]:
    lines = [f"CleanFrame Data CI: {verdict_title}"]
    for outcome in outcomes:
        counts = []
        if outcome.changed_cells is not None:
            counts.append(f"{outcome.changed_cells} changed")
        if outcome.quarantined is not None:
            counts.append(f"{outcome.quarantined} quarantined")
        extra = f" ({', '.join(counts)})" if counts else ""
        lines.append(f"  {outcome.icon} {_one_line(outcome.file)}: {outcome.label}{extra}")
        if outcome.message and outcome.failed:
            lines.append(f"      {_truncate(_one_line(outcome.message), 500)}")
    return lines


def _emit_annotations(outcomes: Sequence[Outcome], errors: Sequence[str]) -> None:
    for message in errors[:MAX_ANNOTATIONS]:
        _annotate("error", message, title="CleanFrame Data CI: invalid input")
    failed = [o for o in outcomes if o.failed][:MAX_ANNOTATIONS]
    for outcome in failed:
        detail = outcome.message or "; ".join(
            _one_line(f.get("message") or f.get("kind") or "") for f in outcome.findings[:3]
        )
        _annotate(
            "error", detail or outcome.label, title=f"CleanFrame: {outcome.label}",
            file=outcome.file if outcome.is_file else None,
        )
    warned = [o for o in outcomes if o.level == "warning"][:MAX_ANNOTATIONS]
    for outcome in warned:
        detail = "; ".join(
            _one_line(f.get("message") or f.get("kind") or "") for f in outcome.findings[:3]
        )
        _annotate(
            "warning", detail or "schema drift", title="CleanFrame: applied despite drift",
            file=outcome.file,
        )


def publish(results: dict, results_path: Path, env: Mapping[str, str]) -> int:
    """Write the summary, outputs, annotations and results file; return the worst exit code."""
    fail_on_quarantine = bool(results.get("fail_on_quarantine"))
    records = [r for r in results.get("files") or [] if isinstance(r, Mapping)]
    outcomes = [classify(r, fail_on_quarantine=fail_on_quarantine) for r in records]
    errors = [str(e) for e in results.get("errors") or []]
    outputs = compute_outputs(outcomes, errors)

    for record, outcome in zip(records, outcomes, strict=True):
        record["outcome"] = {  # type: ignore[index]
            "level": outcome.level,
            "label": outcome.label,
            "effective_exit_code": outcome.effective_exit,
            "changed_cells": outcome.changed_cells,
            "quarantined": outcome.quarantined,
            "drift": outcome.has_drift,
        }
    results["totals"] = {key.replace("-", "_"): value for key, value in outputs.items()}
    results_path.parent.mkdir(parents=True, exist_ok=True)
    results_path.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    outputs["results-file"] = str(results_path)

    write_step_summary(render_markdown(results, outcomes), env)
    write_outputs(outputs, env)
    title, _ = _verdict(outcomes, errors, int(outputs["exit-code"]))
    _print_untrusted(_log_lines(outcomes, title))
    _emit_annotations(outcomes, errors)
    return int(outputs["exit-code"])


def cmd_render(path: str, env: Mapping[str, str]) -> int:
    source = Path(path)
    try:
        results = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _annotate("error", f"cannot read results file {path}: {exc}", title="CleanFrame Data CI")
        return EXIT_USAGE
    if not isinstance(results, dict) or results.get("schema") != RESULTS_SCHEMA:
        _annotate("error", f"{path} is not a {RESULTS_SCHEMA} file", title="CleanFrame Data CI")
        return EXIT_USAGE
    return publish(results, source, env)


def main(argv: list[str] | None = None) -> int:
    _reconfigure_stdio()
    parser = argparse.ArgumentParser(
        prog="summary.py", description="CleanFrame Data CI (GitHub Action) helper."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("install", help="install CleanFrame into an isolated virtualenv")
    sub.add_parser("run", help="replay the recipe over the files and publish the results")
    render = sub.add_parser("render", help="publish a results file written by `run`")
    render.add_argument("results", help="path to a cleanframe-results.json")
    args = parser.parse_args(argv)
    try:
        if args.command == "install":
            return cmd_install(os.environ)
        if args.command == "run":
            return cmd_run(os.environ)
        return cmd_render(args.results, os.environ)
    except KeyboardInterrupt:  # pragma: no cover - runner cancellation
        return EXIT_INTERRUPT


if __name__ == "__main__":
    raise SystemExit(main())
