"""The bundled demo: sample files that ship inside the wheel, and a guided run.

``pip install cleanframe-engine && cleanframe demo`` must work on a machine with no
clone of the repository, so the sample data lives in the package
(``cleanframe/demo_data``). The same files are kept in ``examples/`` for people
browsing the repo; a test asserts the two copies are identical.
"""

from __future__ import annotations

import shutil
import warnings
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from .errors import CleanFrameError, DriftError
from .quality import QualityScore

#: The files :func:`run_demo` copies out, in the order the demo uses them.
DEMO_FILES = ("messy_customers.csv", "customer.schema.yaml", "next_month_customers.csv")


def demo_file(name: str) -> Path:
    """Path of a bundled demo file (e.g. ``"messy_customers.csv"``)."""
    if name not in DEMO_FILES:
        raise CleanFrameError(f"Unknown demo file {name!r}. Available: {list(DEMO_FILES)}.")
    resource = resources.files("cleanframe").joinpath("demo_data", name)
    path = Path(str(resource))
    if not path.is_file():  # pragma: no cover - a broken install
        raise CleanFrameError(f"Demo file {name!r} is missing from this installation.")
    return path


@dataclass
class DemoRun:
    """Everything the guided demo produced, for the CLI to narrate."""

    directory: Path
    files: dict[str, Path] = field(default_factory=dict)
    quality: QualityScore | None = None
    issues: list = field(default_factory=list)
    clean_summary: dict = field(default_factory=dict)
    quarantined: int = 0
    #: Exact duplicate rows the recipe dropped (quarantined rows are not counted here).
    duplicates: int = 0
    #: Rows dropped for any other reason: a ``drop`` validation rule or a plugin frame op.
    dropped: int = 0
    drift_text: str = ""
    patch_notes: list[str] = field(default_factory=list)
    replay_rows: int = 0


def run_demo(directory: str | Path, *, overwrite: bool = False) -> DemoRun:
    """Copy the demo files into ``directory`` and run report → clean → drift → patch.

    Refuses to replace existing files unless ``overwrite``: the directory may be
    one the user already works in.
    """
    from .api import apply_recipe, clean, report, suggest_update
    from .dataio import write_frame
    from .types import Severity

    target = Path(directory)
    planned = [target / name for name in DEMO_FILES] + [
        target / "messy_customers.report.html",
        target / "customer.recipe.yaml",
        target / "messy_customers.clean.csv",
        target / "clean_customers.py",
        target / "messy_customers.diff.html",
        target / "customer.recipe.patched.yaml",
    ]
    existing = [p for p in planned if p.exists()]
    if existing and not overwrite:
        raise CleanFrameError(
            f"{target} already has demo output ({existing[0].name}, …). Pass --overwrite "
            "to replace it, or choose another --out-dir."
        )
    target.mkdir(parents=True, exist_ok=True)
    run = DemoRun(directory=target)
    for name in DEMO_FILES:
        run.files[name] = Path(shutil.copyfile(demo_file(name), target / name))

    messy = run.files["messy_customers.csv"]
    schema = run.files["customer.schema.yaml"]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # the narrative explains what the warnings would say
        rep = report(messy, schema=schema)
        run.files["report"] = rep.save(target / "messy_customers.report.html")
        run.quality = rep.quality

        result = clean(messy, target_schema=schema)
        run.issues = [
            (i.column, i.kind, i.message)
            for i in result.issues
            if i.severity.rank >= Severity.WARNING.rank
        ]
        run.files["recipe"] = result.recipe.save(target / "customer.recipe.yaml")
        run.files["clean"] = write_frame(result.dataframe, target / "messy_customers.clean.csv")
        run.files["code"] = result.code.save(target / "clean_customers.py")
        run.files["diff"] = target / "messy_customers.diff.html"
        result.report(run.files["diff"])
        run.clean_summary = result.diff.summary()
        run.quarantined = int(len(result.quarantine))
        # diff.dropped_rows lists every row missing from the clean output, the
        # quarantined ones included, so split it before naming any count.
        removed = result.diff.dropped_rows
        run.duplicates = sum(1 for _, reason in removed if reason == "dedup")
        run.dropped = len(removed) - run.quarantined - run.duplicates

        next_month = run.files["next_month_customers.csv"]
        try:
            apply_recipe(next_month, result.recipe)
        except DriftError as exc:
            run.drift_text = exc.report.render() if exc.report is not None else str(exc)
        patched, _ = suggest_update(next_month, result.recipe)
        run.files["patched"] = patched.save(target / "customer.recipe.patched.yaml")
        notes = patched.meta.get("patched_for_drift") or []
        run.patch_notes = [str(n) for n in notes] if isinstance(notes, list) else [str(notes)]
        run.replay_rows = int(len(apply_recipe(next_month, patched).dataframe))
    return run


__all__ = ["DEMO_FILES", "DemoRun", "demo_file", "run_demo"]
