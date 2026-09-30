"""Plugin discovery: how a custom op or detector reaches the CLI, replay and CI.

A custom op registered in *your* Python session is invisible to ``cleanframe apply``
running in CI, because nothing imports your module there. This module closes that gap.
Three routes, all of which simply **import** a module (whose top level registers its
detectors/ops/validators with the usual decorators):

1. **Entry points** — a package declares ``[project.entry-points."cleanframe.plugins"]``
   and is loaded whenever the CLI starts (or :func:`load_plugins` is called).
2. **Environment** — ``CLEANFRAME_PLUGINS="my_pkg.ops,other_mod"`` (comma- or
   ``os.pathsep``-separated).
3. **Explicit** — ``cleanframe --plugin my_pkg.ops apply ...`` or
   ``cf.load_plugins(["my_pkg.ops"])``.

A recipe that names an op nobody has registered triggers one lazy discovery pass
(entry points + environment) before failing, so ``apply`` works wherever the plugin
package is *installed*.

Loading a plugin runs its code. Only install plugins you trust; set
``CLEANFRAME_NO_PLUGINS=1`` (or pass ``--no-plugins``) to disable automatic discovery.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Iterable

from .errors import CleanFrameError

ENTRY_POINT_GROUP = "cleanframe.plugins"
_LOADED: set[str] = set()


def _disabled() -> bool:
    return os.environ.get("CLEANFRAME_NO_PLUGINS", "").strip() not in ("", "0", "false", "False")


def _import(name: str, origin: str) -> None:
    if name in _LOADED:
        return
    try:
        importlib.import_module(name)
    except CleanFrameError:
        raise
    except Exception as exc:  # noqa: BLE001 - a broken plugin must not read as a CleanFrame bug
        raise CleanFrameError(
            f"Plugin {name!r} (from {origin}) failed to load: {type(exc).__name__}: {exc}"
        ) from exc
    _LOADED.add(name)


def _entry_point_modules() -> list[tuple[str, str]]:
    from importlib.metadata import entry_points

    found: list[tuple[str, str]] = []
    for ep in sorted(entry_points(group=ENTRY_POINT_GROUP), key=lambda e: e.name):
        module = ep.value.split(":", 1)[0].strip()
        found.append((module, f"entry point {ep.name!r}"))
    return found


def _env_modules() -> list[tuple[str, str]]:
    raw = os.environ.get("CLEANFRAME_PLUGINS", "")
    names = [n.strip() for chunk in raw.split(os.pathsep) for n in chunk.split(",")]
    return [(n, "CLEANFRAME_PLUGINS") for n in names if n]


def load_plugins(modules: Iterable[str] = (), *, discover: bool = True) -> list[str]:
    """Import ``modules`` and (unless ``discover=False``) every discoverable plugin.

    Idempotent: a module is imported at most once. Returns the names imported by this
    call. Raises :class:`~cleanframe.errors.CleanFrameError` naming the plugin that
    failed to load.
    """
    plan: list[tuple[str, str]] = [(m, "explicit request") for m in modules]
    if discover and not _disabled():
        plan += _entry_point_modules() + _env_modules()
    loaded: list[str] = []
    for name, origin in plan:
        if name not in _LOADED:
            _import(name, origin)
            loaded.append(name)
    return loaded


__all__ = ["load_plugins", "ENTRY_POINT_GROUP"]
