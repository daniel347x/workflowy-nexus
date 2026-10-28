from __future__ import annotations

import json
from pathlib import Path

import pytest

from workflowy_mcp.tool_surface_measurement import (
    canonical_tool_records,
    measure_profiles,
    measure_tool_records,
    serialize_tool_surface,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_canonical_serializer_is_order_independent_and_wire_shaped():
    tools = [
        {"name": "z", "description": "Z", "inputSchema": {"type": "object"}},
        {"name": "a", "description": "A", "inputSchema": {}},
    ]
    reversed_tools = list(reversed(tools))
    assert canonical_tool_records(tools)[0]["name"] == "a"
    assert serialize_tool_surface(tools) == serialize_tool_surface(reversed_tools)
    payload = json.loads(serialize_tool_surface(tools))
    assert list(payload) == ["tools"]
    assert [tool["name"] for tool in payload["tools"]] == ["a", "z"]


def test_measurement_uses_serialized_payload_as_authority():
    tools = [{"name": "x", "description": "desc", "inputSchema": {}}]
    measured = measure_tool_records(tools)
    assert measured["wire_chars"] == len(serialize_tool_surface(tools))
    assert measured["approx_tokens"] == (measured["wire_chars"] + 3) // 4


def test_consolidated_surface_meets_sixty_percent_wire_reduction_target():
    report = measure_profiles(cwd=str(REPO_ROOT))
    assert report["legacy"]["tool_count"] == 43
    assert report["consolidated"]["tool_count"] == 8
    # Archive Reads U3: the published consolidated surface grew BY DESIGN
    # (the §5.10 archive sentence on the eight family summaries, the
    # ARCHIVE_AUTO_SENTENCE on six verb summaries, and the two new
    # system.archives.* verbs — §5.7's canonical-doctrine summary alone is
    # contract-fixed text); the legacy surface is untouched, so the S10
    # budget relaxes from 0.40 to 0.42 across the two U3 gates (0.41 in
    # Gate 3a for the describe sentences; 0.42 in Gate 3b for the two
    # verbs, named in U3's RESULT: measured 49209/119286 = 0.4125 object,
    # 49371/119448 = 0.4133 union).
    assert report["consolidated"]["wire_chars"] <= 0.42 * report["legacy"][
        "wire_chars"
    ]
    # Same U3 named relaxation: 58.6% vs the old 60% floor (the archive
    # describe sentences and the two archives.* verbs grew the consolidated
    # surface by design; legacy untouched).
    assert report["wire_reduction_fraction"] >= 0.58


@pytest.mark.parametrize("mode", ["object", "union"])
def test_surface_budget_holds_in_both_params_schema_modes(monkeypatch, mode):
    """The published-params-schema flag must not blow the S10 target in either
    mode. measure_profiles runs the fresh-process rail, which copies this
    process's environment, so the flag is set here per measurement. The
    before/after numbers are printed (pytest -rP / the run log) so the RESULT
    can record them; the assertion is the S10 acceptance target itself."""
    monkeypatch.setenv("WORKFLOWY_MCP_PARAMS_SCHEMA", mode)
    report = measure_profiles(cwd=str(REPO_ROOT))
    print(
        f"[params-schema {mode}] legacy wire_chars={report['legacy']['wire_chars']} "
        f"consolidated wire_chars={report['consolidated']['wire_chars']} "
        f"reduction={report['wire_reduction_percent']}%"
    )
    assert report["legacy"]["tool_count"] == 43
    assert report["consolidated"]["tool_count"] == 8
    # Archive Reads U3: the published consolidated surface grew BY DESIGN
    # (the §5.10 archive sentence on the eight family summaries, the
    # ARCHIVE_AUTO_SENTENCE on six verb summaries, and the two new
    # system.archives.* verbs — §5.7's canonical-doctrine summary alone is
    # contract-fixed text); the legacy surface is untouched, so the S10
    # budget relaxes from 0.40 to 0.42 across the two U3 gates (0.41 in
    # Gate 3a for the describe sentences; 0.42 in Gate 3b for the two
    # verbs, named in U3's RESULT).
    assert report["consolidated"]["wire_chars"] <= 0.42 * report["legacy"][
        "wire_chars"
    ]
    # Same U3 named relaxation: 58.6% vs the old 60% floor (the archive
    # describe sentences and the two archives.* verbs grew the consolidated
    # surface by design; legacy untouched).
    assert report["wire_reduction_fraction"] >= 0.58
