from __future__ import annotations

import asyncio
import inspect
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
from fastmcp import FastMCP

from workflowy_mcp import server
from workflowy_mcp.legacy_tool_mapping import (
    CONSOLIDATED_TOOL_NAMES,
    LEGACY_TOOL_NAMES,
)
from workflowy_mcp.tool_surface_measurement import dump_tool_surface


REPO_ROOT = Path(__file__).resolve().parents[1]
FAMILY_NAMES = {"system", "node", "tag", "anchor", "slice_group", "beacon"}


def _tool_names() -> set[str]:
    return {tool.name for tool in asyncio.run(server.mcp.list_tools())}


def test_default_profile_is_consolidated_eight_tool_surface():
    assert server.REGISTRATION_PROFILE == "consolidated"
    assert _tool_names() == set(CONSOLIDATED_TOOL_NAMES)
    assert len(_tool_names()) == 8
    assert "glimpse" in _tool_names()
    assert "workflowy_glimpse" not in _tool_names()


def test_subprocess_profile_matrix_and_colliding_schema_shapes():
    legacy = dump_tool_surface("legacy", cwd=str(REPO_ROOT))
    consolidated = dump_tool_surface("consolidated", cwd=str(REPO_ROOT))
    legacy_by_name = {tool["name"]: tool for tool in legacy}
    consolidated_by_name = {tool["name"]: tool for tool in consolidated}

    assert set(legacy_by_name) == set(LEGACY_TOOL_NAMES) | FAMILY_NAMES
    assert "workflowy_glimpse" in legacy_by_name
    assert "glimpse" not in legacy_by_name
    assert set(consolidated_by_name) == set(CONSOLIDATED_TOOL_NAMES)
    assert "workflowy_glimpse" not in consolidated_by_name

    legacy_lightning_props = set(
        legacy_by_name["lightning_rod"]["inputSchema"]["properties"]
    )
    consolidated_lightning_props = set(
        consolidated_by_name["lightning_rod"]["inputSchema"]["properties"]
    )
    assert "mode" in legacy_lightning_props
    assert consolidated_lightning_props == {"verb", "params"}
    assert set(consolidated_by_name["glimpse"]["inputSchema"]["properties"]) == {
        "verb",
        "params",
    }


def test_invalid_profile_crashes_import_loudly_without_api_credentials():
    env = os.environ.copy()
    env["WORKFLOWY_MCP_PROFILE"] = "hybrid"
    env.pop("WORKFLOWY_API_KEY", None)
    completed = subprocess.run(
        [sys.executable, "-c", "import workflowy_mcp.server"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert completed.returncode != 0
    combined = completed.stdout + completed.stderr
    assert "Invalid MCP registration profile" in combined
    assert "legacy, consolidated" in combined


def test_supported_fastmcp_tool_decorator_returns_original_function():
    local_mcp = FastMCP("profile-gate-type-lock")

    def probe():
        return None

    assert local_mcp.tool(name="probe")(probe) is probe


def test_inactive_profile_decorator_is_identity_and_wrappers_stay_plain():
    # Under the consolidated DEFAULT, the inactive (identity) decorator is the
    # legacy gate; probing _consolidated_tool here would REGISTER a stray tool
    # on the live app and pollute every later list_tools assertion.
    def probe():
        return None

    assert server._legacy_tool(name="inactive-probe")(probe) is probe
    assert inspect.iscoroutinefunction(server._glimpse_consolidated_tool)
    assert inspect.iscoroutinefunction(server._lightning_rod_consolidated_tool)


def test_consolidated_wrappers_delegate_verbatim(monkeypatch):
    glimpse_calls = []
    lightning_calls = []

    async def fake_glimpse(*, verb=None, params=None):
        glimpse_calls.append((verb, params))
        return {"surface": "glimpse"}

    async def fake_lightning(*, verb=None, params=None):
        lightning_calls.append((verb, params))
        return {"surface": "lightning"}

    monkeypatch.setattr(server, "glimpse_family_tool", fake_glimpse)
    monkeypatch.setattr(server, "lightning_rod_family_tool", fake_lightning)

    assert asyncio.run(server._glimpse_consolidated_tool()) == {
        "surface": "glimpse"
    }
    assert asyncio.run(
        server._lightning_rod_consolidated_tool(
            verb="render_tags", params={"root_handle": "R"}
        )
    ) == {"surface": "lightning"}
    assert glimpse_calls == [(None, None)]
    assert lightning_calls == [("render_tags", {"root_handle": "R"})]


def test_lifespan_rejects_profile_drift_before_startup(monkeypatch):
    guard_calls = []

    def record_guard(config):
        guard_calls.append(config)

    monkeypatch.setattr(server.server_bootstrap, "acquire_single_writer_guard", record_guard)
    monkeypatch.setattr(
        server,
        "get_server_config",
        lambda reload=False: SimpleNamespace(mcp_profile="legacy"),
    )

    async def exercise() -> None:
        with pytest.raises(RuntimeError, match="restart the MCP server process"):
            async with server.lifespan(None):
                pass

    asyncio.run(exercise())
    assert len(guard_calls) == 1
