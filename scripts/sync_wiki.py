"""Regenerate ``wiki/*.md`` from ``docs/*.md`` so the two can never drift apart.

``docs/`` is the source of truth. Each page is copied to its GitHub-Wiki name with
only its *links* rewritten (wiki links are page names, not relative file paths):

    python scripts/sync_wiki.py            # rewrite wiki/ from docs/
    python scripts/sync_wiki.py --check    # exit 1 if wiki/ is out of date (CI / tests)

``tests/test_docs_in_sync.py`` runs the check, so a docs edit without a sync fails CI.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO_BLOB = "https://github.com/inboxpraveen/Cleanframe/blob/main/"

#: docs page -> wiki page. ``docs/README.md`` and ``wiki/Home.md`` are written by hand.
PAGES = {
    "getting-started.md": "Getting-Started.md",
    "installation.md": "Installation.md",
    "concepts.md": "Concepts.md",
    "cli.md": "CLI.md",
    "api-reference.md": "API-Reference.md",
    "recipe-spec.md": "Recipe-Specification.md",
    "schema-spec.md": "Schema-Specification.md",
    "detectors-and-ops.md": "Detectors-and-Ops.md",
    "plugins.md": "Plugins.md",
    "llm.md": "LLM-Planning.md",
    "production.md": "Production-Guide.md",
    "architecture.md": "Architecture.md",
    "faq.md": "FAQ.md",
}

_LINK_RE = re.compile(r"\]\((?P<target>[^)\s]+)\)")


def _rewrite_target(target: str) -> str:
    if re.match(r"^[a-z][a-z0-9+.-]*:", target) or target.startswith("#"):
        return target  # absolute URL / mailto / in-page anchor
    path, _, fragment = target.partition("#")
    suffix = f"#{fragment}" if fragment else ""
    if path in PAGES:
        return PAGES[path][: -len(".md")] + suffix
    if path.startswith("../"):
        return REPO_BLOB + path[3:] + suffix
    return target


def render_wiki_page(text: str) -> str:
    """The wiki flavour of a docs page: LF endings, links rewritten."""
    text = text.replace("\r\n", "\n")
    return _LINK_RE.sub(lambda m: f"]({_rewrite_target(m.group('target'))})", text)


def expected_wiki() -> dict[Path, str]:
    return {
        ROOT / "wiki" / wiki_name: render_wiki_page((ROOT / "docs" / doc_name).read_text(encoding="utf-8"))
        for doc_name, wiki_name in PAGES.items()
    }


def stale_pages() -> list[Path]:
    stale = []
    for path, text in expected_wiki().items():
        current = path.read_text(encoding="utf-8").replace("\r\n", "\n") if path.exists() else None
        if current != text:
            stale.append(path)
    return stale


def main(argv: list[str]) -> int:
    check = "--check" in argv
    stale = stale_pages()
    if check:
        for path in stale:
            print(f"out of date: {path.relative_to(ROOT)}  (run: python scripts/sync_wiki.py)")
        return 1 if stale else 0
    for path, text in expected_wiki().items():
        if path in stale:
            path.write_text(text, encoding="utf-8", newline="\n")
            print(f"updated {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
