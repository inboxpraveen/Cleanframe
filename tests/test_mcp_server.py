"""The MCP server: tools registered with the right schemas, and a real stdio round trip.

Skipped unless the optional SDK is installed (``pip install "cleanframe-engine[mcp]"``).
The tool logic itself is covered without the SDK in test_agent_tools.py.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

mcp = pytest.importorskip("mcp")
pytest.importorskip("mcp.server.mcpserver", reason="needs the mcp 2.x SDK (MCPServer)")

import anyio  # noqa: E402 - only importable once mcp (which depends on it) is present
from mcp import Client, StdioServerParameters  # noqa: E402

from cleanframe import agent_tools  # noqa: E402
from cleanframe.mcp_server import INSTRUCTIONS, build_server  # noqa: E402

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
ROOT = Path(__file__).resolve().parent.parent

EXPECTED_TOOLS = [
    "profile_data",
    "suggest_recipe",
    "apply_recipe",
    "check_drift",
    "explain_recipe",
    "list_capabilities",
    "export_code",
]


def _run(coro_fn):
    return anyio.run(coro_fn)


def test_tools_are_registered_with_schemas_and_hints():
    async def main():
        async with Client(build_server()) as client:
            return (await client.list_tools()).tools

    tools = {t.name: t for t in _run(main)}
    assert list(tools) == EXPECTED_TOOLS
    profile = tools["profile_data"]
    assert list(profile.input_schema["properties"]) == ["path", "sheet", "nrows", "header_row", "text"]
    assert profile.input_schema["required"] == ["path"]
    assert tools["suggest_recipe"].input_schema["required"] == ["path", "recipe_out"]
    assert tools["apply_recipe"].input_schema["required"] == ["path", "recipe"]
    assert profile.annotations.read_only_hint is True
    assert tools["apply_recipe"].annotations.read_only_hint is False
    for name, tool in tools.items():
        # The agent reads the docstring; cleandoc must have removed source indentation.
        assert tool.description and not tool.description.startswith(" "), name
        assert tool.output_schema is not None, name


def test_failed_tool_call_is_flagged_is_error_with_structured_payload(tmp_path):
    async def main():
        async with Client(build_server()) as client:
            return await client.call_tool("profile_data", {"path": str(tmp_path / "nope.csv")})

    result = _run(main)
    assert result.is_error is True
    assert result.structured_content["ok"] is False
    assert result.structured_content["kind"] == "CleanFrameError"


def test_stdio_round_trip(tmp_path):
    """Spawn `python -m cleanframe.mcp_server` and drive it like an MCP client would."""
    csv = tmp_path / "customers.csv"
    shutil.copy(EXAMPLES / "messy_customers.csv", csv)
    recipe_out = tmp_path / "customers.recipe.yaml"
    env = {
        "PYTHONIOENCODING": "utf-8",
        # The child must import this checkout (it is what an editable install points at).
        "PYTHONPATH": os.pathsep.join(filter(None, [str(ROOT), os.environ.get("PYTHONPATH")])),
        "CLEANFRAME_NO_PLUGINS": "1",
    }
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "cleanframe.mcp_server"], env=env, cwd=str(tmp_path)
    )

    async def main():
        with anyio.fail_after(180):
            async with Client(params) as client:
                tools = (await client.list_tools()).tools
                profile = await client.call_tool("profile_data", {"path": str(csv)})
                plan = await client.call_tool(
                    "suggest_recipe", {"path": "customers.csv", "recipe_out": str(recipe_out)}
                )
                info = client.server_info
                return client.instructions, info, tools, profile, plan

    instructions, info, tools, profile, plan = _run(main)
    assert [t.name for t in tools] == EXPECTED_TOOLS
    assert instructions == INSTRUCTIONS
    assert info is None or info.name == "cleanframe"

    assert profile.is_error is False
    data = profile.structured_content
    assert data["ok"] is True and data["rows"] == 6
    assert data["quality"]["score"] == agent_tools.profile_data(str(csv))["quality"]["score"]

    assert plan.is_error is False
    plan_data = plan.structured_content
    assert plan_data["ok"] is True
    # A relative path resolves against the server's working directory.
    assert plan_data["source"] == str(csv.resolve())
    assert recipe_out.exists()
    assert plan_data["recipe_yaml"] == recipe_out.read_text(encoding="utf-8")
    assert plan_data["quarantine"]["count"] == 1
