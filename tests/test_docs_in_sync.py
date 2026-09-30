"""docs/ is the source of truth for the wiki: they must not drift apart."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "sync_wiki.py"

pytestmark = pytest.mark.skipif(
    not SCRIPT.exists() or not (ROOT / "docs").is_dir(),
    reason="only meaningful in a source checkout (docs/ and scripts/ are not in the wheel)",
)


def _load():
    spec = importlib.util.spec_from_file_location("sync_wiki", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["sync_wiki"] = module
    spec.loader.exec_module(module)
    return module


def test_wiki_pages_match_docs():
    sync = _load()
    stale = [p.name for p in sync.stale_pages()]
    assert not stale, f"wiki pages out of date {stale}; run `python scripts/sync_wiki.py`"


def test_link_rewriting():
    sync = _load()
    text = "See [CLI](cli.md#json), [Plugins](plugins.md), [Contributing](../CONTRIBUTING.md), [x](https://a.b/c.md), [top](#a)."
    assert sync.render_wiki_page(text) == (
        "See [CLI](CLI#json), [Plugins](Plugins), "
        "[Contributing](https://github.com/inboxpraveen/Cleanframe/blob/main/CONTRIBUTING.md), "
        "[x](https://a.b/c.md), [top](#a)."
    )


def test_every_docs_page_is_mapped():
    sync = _load()
    unmapped = sorted(
        p.name for p in (ROOT / "docs").glob("*.md") if p.name != "README.md" and p.name not in sync.PAGES
    )
    assert not unmapped, f"add {unmapped} to scripts/sync_wiki.py PAGES"
