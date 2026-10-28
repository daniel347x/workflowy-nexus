"""Canonical FastMCP tools/list surface dump and measurement helpers."""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from typing import Any, Iterable, Mapping


_SUBPROCESS_DUMP_CODE = r"""
import asyncio
import json
from workflowy_mcp.server import mcp

tools = asyncio.run(mcp.list_tools())
normalized = []
for tool in tools:
    normalized.append({
        "name": str(tool.name),
        "description": str(tool.description or ""),
        "inputSchema": dict(tool.parameters or {}),
    })
print(json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
"""


# @beacon[
#   id=mcp-api@dump-tool-surface,
#   role=dump_tool_surface,
#   slice_labels=nexus--mcp-api,
#   kind=ast,
#   comment=Fresh-process profile inventory helper shared by registration tests and canonical prompt-surface measurement; deliberately strips API credentials to lock import-safe bootstrap,
# ]
def dump_tool_surface(
    profile: str,
    *,
    python_executable: str | None = None,
    cwd: str | None = None,
) -> list[dict[str, Any]]:
    """Import one profile in a fresh process and return its compact tool records."""
    env = os.environ.copy()
    env["WORKFLOWY_MCP_PROFILE"] = profile
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("WORKFLOWY_API_KEY", None)
    completed = subprocess.run(
        [python_executable or sys.executable, "-c", _SUBPROCESS_DUMP_CODE],
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError(
            f"Profile {profile!r} produced no tools/list JSON; stderr={completed.stderr!r}"
        )
    payload = json.loads(lines[-1])
    if not isinstance(payload, list):
        raise TypeError("tools/list subprocess payload must be a list")
    return payload


def canonical_tool_records(
    tools: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Normalize tool records to the canonical measurement shape and order."""
    normalized = [
        {
            "name": str(tool.get("name") or ""),
            "description": str(tool.get("description") or ""),
            "inputSchema": dict(tool.get("inputSchema") or {}),
        }
        for tool in tools
    ]
    return sorted(normalized, key=lambda tool: tool["name"])


def serialize_tool_surface(tools: Iterable[Mapping[str, Any]]) -> str:
    """Serialize the acceptance-authority compact tools/list payload."""
    return json.dumps(
        {"tools": canonical_tool_records(tools)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def measure_tool_records(tools: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Measure one canonical surface without ad-hoc repr/component arithmetic."""
    records = canonical_tool_records(tools)
    serialized = serialize_tool_surface(records)
    schema_chars = sum(
        len(
            json.dumps(
                record["inputSchema"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        for record in records
    )
    wire_chars = len(serialized)
    return {
        "tool_count": len(records),
        "name_chars": sum(len(record["name"]) for record in records),
        "description_chars": sum(len(record["description"]) for record in records),
        "schema_chars": schema_chars,
        "wire_chars": wire_chars,
        "approx_tokens": math.ceil(wire_chars / 4),
    }


# @beacon[
#   id=mcp-api@measure-tool-profiles,
#   role=measure_profiles,
#   slice_labels=nexus--mcp-api,
#   kind=ast,
#   comment=Canonical S10 acceptance measurement for legacy versus consolidated tools-list wire payloads using one serializer and the active sys.executable,
# ]
def measure_profiles(
    *,
    python_executable: str | None = None,
    cwd: str | None = None,
) -> dict[str, Any]:
    """Measure legacy and consolidated surfaces through the same fresh-process rail."""
    legacy = measure_tool_records(
        dump_tool_surface(
            "legacy", python_executable=python_executable, cwd=cwd
        )
    )
    consolidated = measure_tool_records(
        dump_tool_surface(
            "consolidated", python_executable=python_executable, cwd=cwd
        )
    )
    reduction = 1.0 - (consolidated["wire_chars"] / legacy["wire_chars"])
    return {
        "legacy": legacy,
        "consolidated": consolidated,
        "wire_reduction_fraction": reduction,
        "wire_reduction_percent": round(reduction * 100, 2),
    }
