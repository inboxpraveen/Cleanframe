"""CleanFrame as a Model Context Protocol (MCP) server, over stdio.

    pip install "cleanframe-engine[mcp]"
    cleanframe-mcp                       # or: python -m cleanframe.mcp_server

Point an MCP client (Claude Code, Claude Desktop, Cursor, VS Code, Codex, ...) at that
command and the agent gets CleanFrame's workflow as tools: profile a file, plan a
reviewable YAML recipe, replay it behind the drift guard, explain it, export it.

All the logic lives in :mod:`cleanframe.agent_tools`, which is dependency-free and
unit-tested. This module only registers those functions with the MCP Python SDK
(``mcp>=2.3``, whose server class is ``MCPServer``), so the optional dependency is
imported lazily here and nowhere else in the package.
"""

from __future__ import annotations

import argparse
import functools
import inspect
import json
import sys
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any

from ._version import __version__

if TYPE_CHECKING:
    from mcp.server import MCPServer

SERVER_NAME = "cleanframe"

INSTALL_HINT = (
    'The CleanFrame MCP server needs the optional MCP SDK (mcp 2.3 or newer). Install it with:\n'
    '  pip install "cleanframe-engine[mcp]"'
)

#: Sent to the client at initialisation; agents read it to learn the workflow.
INSTRUCTIONS = """\
CleanFrame cleans messy CSV/Excel/Parquet/JSON files by writing a reviewable YAML recipe
that pure, deterministic pandas executes and replays on every future file, with a cell-level
diff, a quarantine for invalid rows, and a schema-drift guard. Prefer it over writing ad-hoc
pandas that modifies a data file.

Workflow:
1. profile_data(path): quality score, column types, detected issues. Read-only.
2. suggest_recipe(path, recipe_out): plan a recipe with deterministic rules and save the YAML.
   Show the user the YAML and the change preview; the recipe is theirs to review and commit.
3. To change behaviour, edit the recipe YAML (explain_recipe explains it; list_capabilities
   lists every op and check). Never hand-edit the data file.
4. apply_recipe(path, recipe, out): write the cleaned file. Rows failing validation are
   quarantined with a reason, never silently deleted.
5. When the same kind of file arrives again: check_drift(path, recipe), then apply_recipe.
   If drift is reported (ok=false, kind "DriftError" from apply_recipe), stop and show the
   user the findings. Do not pass on_drift="ignore" unless they say so.
6. export_code(recipe, out) writes an equivalent standalone pandas script.

Rules: use absolute paths. The input file is never overwritten (always refused). Pass
overwrite=true only when the user agreed to replace an existing output. Results are capped;
*_truncated fields say when something was left out.
"""

#: tool -> (title, read_only, destructive, idempotent). "destructive" is honest about
#: overwrite=true: those tools may replace an existing output file when asked to.
_TOOL_HINTS: dict[str, tuple[str, bool, bool, bool]] = {
    "profile_data": ("Profile a data file", True, False, True),
    "suggest_recipe": ("Plan a cleaning recipe", False, True, True),
    "apply_recipe": ("Apply a recipe", False, True, True),
    "check_drift": ("Check a file for schema drift", False, False, True),
    "explain_recipe": ("Explain a recipe", True, False, True),
    "list_capabilities": ("List ops, detectors and checks", True, False, True),
    "export_code": ("Export a recipe as pandas code", False, True, True),
}


def build_server() -> MCPServer:
    """Create the MCP server with every CleanFrame tool registered.

    Raises :class:`ImportError` carrying :data:`INSTALL_HINT` when the SDK is missing,
    or is a 1.x release (which has no ``MCPServer``).
    """
    try:
        from mcp.server import MCPServer
        from mcp.types import CallToolResult, TextContent, ToolAnnotations
    except ImportError as exc:
        raise ImportError(f"{INSTALL_HINT}\n({type(exc).__name__}: {exc})") from exc

    from .agent_tools import TOOLS

    def as_mcp_tool(fn: Callable[..., dict[str, Any]]) -> Callable[..., Any]:
        """Wrap a tool so ``ok: false`` is also flagged ``isError`` on the wire.

        The MCP spec reports a failed tool execution as a result with ``isError: true``
        (so the model knows the call did not do what it asked) - not as a protocol
        error. The structured payload is kept either way. ``functools.wraps`` preserves
        the signature and annotations the SDK derives the input schema from.
        """

        @functools.wraps(fn)
        def tool(**kwargs: Any) -> CallToolResult:
            result = fn(**kwargs)
            return CallToolResult(
                # One line, no indentation: the same data is in structured_content, and
                # pretty-printing would only spend the agent's context on whitespace.
                content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False))],
                structured_content=result,
                is_error=not result.get("ok", False),
            )

        return tool

    server = MCPServer(
        SERVER_NAME,
        title="CleanFrame",
        description="Reproducible data cleaning: reviewable YAML recipes replayed by pandas.",
        instructions=INSTRUCTIONS,
        website_url="https://github.com/inboxpraveen/Cleanframe",
        version=__version__,
    )
    for fn in TOOLS:
        title, read_only, destructive, idempotent = _TOOL_HINTS[fn.__name__]
        server.add_tool(
            as_mcp_tool(fn),
            name=fn.__name__,
            title=title,
            # cleandoc: before Python 3.13 a docstring keeps its source indentation.
            description=inspect.cleandoc(fn.__doc__ or ""),
            annotations=ToolAnnotations(
                title=title,
                read_only_hint=read_only,
                destructive_hint=destructive,
                idempotent_hint=idempotent,
                open_world_hint=False,
            ),
        )
    return server


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cleanframe-mcp",
        description="Run CleanFrame as an MCP server over stdio (for Claude Code, Cursor, "
        "VS Code, Codex and other MCP clients).",
    )
    parser.add_argument("--version", action="version", version=f"cleanframe-mcp {__version__}")
    parser.add_argument(
        "--plugin", action="append", default=[], metavar="MODULE",
        help="import MODULE first so its custom ops/detectors are available (repeatable)",
    )
    parser.add_argument(
        "--no-plugins", action="store_true",
        help="do not auto-load installed entry-point / CLEANFRAME_PLUGINS plugins",
    )
    return parser


def serve(plugins: Iterable[str] = (), *, discover: bool = True) -> int:
    """Load plugins and serve over stdio until the client disconnects; return an exit code.

    Shared by ``cleanframe-mcp`` and any CLI subcommand that wraps it (which would pass
    ``discover=False`` after loading plugins itself). stdout is the protocol channel, so
    every human-readable message goes to stderr.
    """
    try:
        server = build_server()
    except ImportError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1

    from .errors import CleanFrameError
    from .plugins import load_plugins

    try:
        # Same discovery as the CLI: entry points + CLEANFRAME_PLUGINS, plus --plugin.
        load_plugins(list(plugins), discover=discover)
    except CleanFrameError as exc:
        sys.stderr.write(f"cleanframe-mcp: {exc}\n")
        return 1
    server.run("stdio")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Console entry point (``cleanframe-mcp``)."""
    args = _parser().parse_args(argv)
    return serve(args.plugin, discover=not args.no_plugins)


__all__ = ["build_server", "serve", "main", "INSTRUCTIONS", "INSTALL_HINT", "SERVER_NAME"]


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess in tests
    raise SystemExit(main())
