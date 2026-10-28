"""Large-result declarations must survive actual MCP tools/list serialization."""

from __future__ import annotations

import asyncio
import copy
import json
import os
from pathlib import Path
import subprocess
import sys

from fastmcp import Client, FastMCP
import pytest

from workflowy_mcp import server
from workflowy_mcp.legacy_tool_mapping import CONSOLIDATED_TOOL_NAMES, LEGACY_TOOL_NAMES


REPO_ROOT = Path(__file__).resolve().parents[1]
META_KEY = "anthropic/maxResultSizeChars"
FAMILY_NAMES = {"system", "node", "tag", "anchor", "slice_group", "beacon"}

# Do not use dump_tool_surface: its prompt-size measurement projection omits
# metadata. Exercise JSON-RPC and the real MCP serializer, not Tool.meta alone.
CATALOG_CODE = r"""
import asyncio
from contextlib import asynccontextmanager
from fastmcp import Client
from workflowy_mcp.server import mcp

@asynccontextmanager
async def no_lifespan(app):
    yield {}

# Only skip credential/broker startup. Registration, transforms, the protocol
# handler and MCP serialization are real, with no tool execution or network.
mcp._lifespan = no_lifespan

async def main():
    async with Client(mcp) as client:
        result = await client.list_tools_mcp()
        print(result.model_dump_json(by_alias=True, exclude_none=True))

asyncio.run(main())
"""


@pytest.mark.parametrize("profile", ["consolidated", "legacy"])
@pytest.mark.parametrize("schema_mode", ["object", "union"])
def test_every_published_tool_serializes_large_result_metadata(profile, schema_mode):
    env = os.environ.copy()
    env["WORKFLOWY_MCP_PROFILE"] = profile
    env["WORKFLOWY_MCP_PARAMS_SCHEMA"] = schema_mode
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("WORKFLOWY_API_KEY", None)
    completed = subprocess.run(
        [sys.executable, "-c", CATALOG_CODE],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=60,
    )
    catalog = json.loads(completed.stdout.splitlines()[-1])
    assert not catalog.get("nextCursor"), "Do not silently inspect a partial catalog"
    tools = catalog["tools"]
    expected = (
        set(CONSOLIDATED_TOOL_NAMES)
        if profile == "consolidated"
        else set(LEGACY_TOOL_NAMES) | FAMILY_NAMES
    )
    assert {tool["name"] for tool in tools} == expected
    assert len(tools) == len(expected)
    for tool in tools:
        value = tool.get("_meta", {}).get(META_KEY)
        assert type(value) is int and value == 500000, tool["name"]


@pytest.mark.parametrize(
    "original_meta",
    [None, {"vendor/unrelated": {"keep": ["exact", 7]}}, {META_KEY: 123, "keep": True}],
)
def test_registration_preserves_other_metadata_schema_and_execution(monkeypatch, original_meta):
    local_mcp = FastMCP("large-result-registration")
    monkeypatch.setattr(server, "mcp", local_mcp)
    before = copy.deepcopy(original_meta)

    async def probe(value: str) -> str:
        return value

    # A plain FastMCP registration is the preservation baseline.
    local_mcp.tool(name="baseline", title="Probe", meta=original_meta)(probe)
    assert server._workflowy_tool(name="annotated", title="Probe", meta=original_meta)(probe) is probe
    assert original_meta == before

    async def exercise():
        async with Client(local_mcp) as client:
            result = await client.list_tools_mcp()
            catalog = json.loads(result.model_dump_json(by_alias=True, exclude_none=True))
            baseline = await client.call_tool("baseline", {"value": "whole\n🧭"})
            annotated = await client.call_tool("annotated", {"value": "whole\n🧭"})
        return {tool["name"]: tool for tool in catalog["tools"]}, baseline, annotated

    tools, baseline, annotated = asyncio.run(exercise())
    expected_meta = {**(before or {}), META_KEY: 500000}
    # FastMCP may add its own _meta; preserve that too, not just caller keys.
    assert tools["annotated"]["_meta"] == {
        **tools["baseline"].get("_meta", {}),
        **expected_meta,
    }
    for name in ("baseline", "annotated"):
        tools[name].pop("name")
        tools[name].pop("_meta", None)
    assert tools["annotated"] == tools["baseline"]
    assert annotated.content == baseline.content
    assert annotated.structured_content == baseline.structured_content
    assert annotated.is_error == baseline.is_error is False
