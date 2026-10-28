from __future__ import annotations

import asyncio
from collections import Counter
from pathlib import Path

from workflowy_mcp import server
from workflowy_mcp.legacy_tool_mapping import (
    CONSOLIDATED_TOOL_NAMES,
    LEGACY_TOOL_MAPPING,
    LEGACY_TOOL_NAMES,
    LIGHTNING_ROD_LEGACY_SELECTORS,
)
from workflowy_mcp.tool_surface_measurement import dump_tool_surface


REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRIES = {
    registry.family: registry
    for registry in (
        server.SYSTEM_REGISTRY,
        server.NODE_REGISTRY,
        server.TAG_REGISTRY,
        server.ANCHOR_REGISTRY,
        server.SLICE_GROUP_REGISTRY,
        server.BEACON_REGISTRY,
        server.GLIMPSE_REGISTRY,
        server.LIGHTNING_ROD_REGISTRY,
    )
}


def test_mapping_has_exactly_one_top_level_row_per_legacy_tool():
    row_counts = Counter(row.legacy_tool for row in LEGACY_TOOL_MAPPING)
    assert len(LEGACY_TOOL_NAMES) == 37
    assert len(LEGACY_TOOL_MAPPING) == 37
    assert set(row_counts) == set(LEGACY_TOOL_NAMES)
    assert set(row_counts.values()) == {1}
    assert all(row.disposition != "removed" for row in LEGACY_TOOL_MAPPING)


def test_every_mapping_route_targets_one_real_registered_operation():
    for row in LEGACY_TOOL_MAPPING:
        assert row.routes
        for route in row.routes:
            assert route.target.count(".") >= 1
            family = route.target.split(".", 1)[0]
            assert family in REGISTRIES
            resolved = REGISTRIES[family].resolve(route.target)
            assert resolved is not None, (row.legacy_tool, route.target)
            assert resolved.canonical_name == route.target


def test_lightning_rod_capability_inventory_is_exact_and_duplicate_free():
    row = next(
        item for item in LEGACY_TOOL_MAPPING if item.legacy_tool == "lightning_rod"
    )
    selectors = [route.selector for route in row.routes]
    assert tuple(selectors) == LIGHTNING_ROD_LEGACY_SELECTORS
    assert len(selectors) == len(set(selectors)) == 7
    assert [route.target for route in row.routes] == [
        "lightning_rod.gather",
        "lightning_rod.render_tags",
        "lightning_rod.render_search",
        "lightning_rod.render_symbols",
        "beacon.tags.ensure",
        "beacon.tags.remove",
        "tag.batch",
    ]


def test_runtime_legacy_inventory_and_consolidated_constant_match_surfaces():
    consolidated_names = {tool.name for tool in asyncio.run(server.mcp.list_tools())}
    assert consolidated_names == set(CONSOLIDATED_TOOL_NAMES)

    family_names = {"system", "node", "tag", "anchor", "slice_group", "beacon"}
    legacy = dump_tool_surface("legacy", cwd=str(REPO_ROOT))
    assert {tool["name"] for tool in legacy} == set(LEGACY_TOOL_NAMES) | family_names


def test_all_family_qualified_and_explicit_aliases_resolve_canonically():
    for registry in REGISTRIES.values():
        for spec in registry.operations():
            assert registry.resolve(spec.canonical_name) is spec
            for alias in spec.aliases:
                assert registry.resolve(alias) is spec

            described = registry.describe_full(spec.verb)
            properties = set(described["params_schema"].get("properties", {}))
            assert properties.isdisjoint(spec.param_aliases)
