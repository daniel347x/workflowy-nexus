"""S04 A4 — the extended S03 boundary inventory: every mutating boundary
carries an explicit coherence-admission classification.

Permanent inventory test: an UNCLASSIFIED mutating boundary fails loudly —
covering consolidated MCP fronts, legacy-profile tools, browser WS
ingresses, CARTO launches, and worker jobs.  The classifications live in
``tests/coherence_s03b_registry.json`` under ``admission_classes``.

S04 Remediation Chain 4 (work-order item 2): the map is an HONEST COMPLETE
boundary map — an effectful MCP boundary can no longer DISAPPEAR merely
because its correct class is ``proceed`` (slice-group CRUD) or
``proceed_exception`` (the §7.2 recovery/control fronts).  Every
OperationSpec carrying workflowy_mutation / config_mutation /
disk_mutation / local_state_mutation requires an explicit registry entry,
and the runtime classifier names the named recovery/control exceptions
``proceed_exception`` (runtime refusal behavior unchanged).
"""

from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
REGISTRY_PATH = REPO / "tests" / "coherence_s03b_registry.json"

# The effectful census (S04 Remediation Chain 4, item 2): any operation
# carrying one of these side effects is an effectful boundary that must
# carry an explicit admission_classes entry — including boundaries whose
# correct class is proceed / proceed_exception.
MUTATING_EFFECTS = {
    "workflowy_mutation",
    "config_mutation",
    "disk_mutation",
    "local_state_mutation",
}

NAMED_NON_MCP_BOUNDARIES = {
    "ws:mutate_node_tags",
    "ws:merge_slice_groups",
    "carto:refresh_launch",
    "carto:markdown_generate_launch",
    "carto:bulk_apply_launch",
    "carto:phase8_inline",
    "carto:task_dispatch",
    "job:background_task",
    "legacy:mutation_tools",
    "ws:save_nodes_export_cache",
}


def _registries():
    from workflowy_mcp import server

    return (
        server.NODE_REGISTRY,
        server.TAG_REGISTRY,
        server.ANCHOR_REGISTRY,
        server.SLICE_GROUP_REGISTRY,
        server.BEACON_REGISTRY,
        server.SYSTEM_REGISTRY,
        server.GLIMPSE_REGISTRY,
        server.LIGHTNING_ROD_REGISTRY,
    )


def _admission_classes() -> dict:
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))["admission_classes"]


def collect_unclassified_mutating_boundaries(
    registries=None, admission_classes: dict | None = None
) -> list[str]:
    """Every effectful OperationSpec must have an ``mcp:`` admission class.

    Returns the list of UNCLASSIFIED effectful boundaries (must be empty).
    S04 Remediation Chain 4 (item 2): there is deliberately NO proceed-class
    skip — a boundary whose correct class is ``proceed`` or
    ``proceed_exception`` must STILL be recorded explicitly, or the
    boundary map silently loses it.
    """

    if registries is None:
        registries = _registries()
    if admission_classes is None:
        admission_classes = _admission_classes()
    missing: list[str] = []
    for registry in registries:
        for spec in registry.operations():
            if not (set(spec.side_effects) & MUTATING_EFFECTS):
                continue
            if f"mcp:{spec.canonical_name}" not in admission_classes:
                missing.append(spec.canonical_name)
    return missing


def test_every_mutating_mcp_operation_is_classified():
    """Required test (a): EVERY OperationSpec carrying workflowy_mutation /
    config_mutation / disk_mutation / local_state_mutation has an explicit
    registry entry (proceed and proceed_exception classes included)."""
    assert collect_unclassified_mutating_boundaries() == []


def test_slice_group_crud_registered_as_proceed():
    """Required test (b): the three slice-group CRUD fronts are present and
    classified ``proceed`` (cache-free config mutations), with owner,
    gate/no-gate reason, first side effect, and recheck recorded."""
    classes = _admission_classes()
    for name in ("slice_group.create", "slice_group.update", "slice_group.delete"):
        entry = classes.get(f"mcp:{name}")
        assert entry is not None, f"{name}: missing from admission_classes"
        assert entry["class"] == "proceed", entry
        for field in ("class", "gate", "owner", "first_side_effect", "recheck"):
            assert entry.get(field), f"{name}: admission entry missing {field}"


def test_recovery_control_exceptions_registered_as_proceed_exception():
    """Required test (c): cache.refresh, job.status, and job.cancel are
    present as explicit §7.2 recovery/control ``proceed_exception``
    entries, with owner, gate/no-gate reason, first side effect, and
    recheck recorded."""
    classes = _admission_classes()
    for name in ("system.cache.refresh", "system.job.status", "system.job.cancel"):
        entry = classes.get(f"mcp:{name}")
        assert entry is not None, f"{name}: missing from admission_classes"
        assert entry["class"] == "proceed_exception", entry
        for field in ("class", "gate", "owner", "first_side_effect", "recheck"):
            assert entry.get(field) is not None, (
                f"{name}: admission entry missing {field}"
            )


def test_missing_proceed_entry_fails_the_fence():
    """Required test (d): an injected missing ``proceed`` entry is reported
    loudly (slice-group CRUD cannot silently disappear from the map)."""
    classes = {k: v for k, v in _admission_classes().items() if k != "mcp:slice_group.create"}
    assert collect_unclassified_mutating_boundaries(admission_classes=classes) == [
        "slice_group.create"
    ]


def test_missing_proceed_exception_entry_fails_the_fence():
    """Required test (d): an injected missing ``proceed_exception`` entry
    is reported loudly (a recovery/control front cannot silently
    disappear)."""
    classes = {
        k: v for k, v in _admission_classes().items() if k != "mcp:system.cache.refresh"
    }
    assert collect_unclassified_mutating_boundaries(admission_classes=classes) == [
        "system.cache.refresh"
    ]


def test_named_non_mcp_boundaries_are_classified():
    classes = _admission_classes()
    missing = NAMED_NON_MCP_BOUNDARIES - set(classes)
    assert missing == set(), f"non-MCP mutating boundaries without classification: {missing}"
    for name in NAMED_NON_MCP_BOUNDARIES:
        entry = classes[name]
        for field in ("class", "gate", "owner", "first_side_effect"):
            assert entry.get(field), f"{name}: admission entry missing {field}"


def test_mcp_entry_classes_match_the_runtime_authority():
    from workflowy_mcp.coherence.admission import mutation_admission_class

    classes = _admission_classes()
    for registry in _registries():
        for spec in registry.operations():
            entry = classes.get(f"mcp:{spec.canonical_name}")
            if entry is None:
                continue
            runtime = mutation_admission_class(
                spec.canonical_name, spec.side_effects, spec.cache_backed
            )
            assert entry["class"] == runtime, spec.canonical_name


def test_no_stale_mcp_entries():
    classes = _admission_classes()
    live = set()
    for registry in _registries():
        for spec in registry.operations():
            live.add(spec.canonical_name)
    stale = sorted(
        key[len("mcp:") :]
        for key in classes
        if key.startswith("mcp:") and key[len("mcp:") :] not in live
    )
    assert stale == [], f"admission_classes entries for removed operations: {stale}"


def test_unclassified_mutating_boundary_fails_the_check():
    """A4: an injected unclassified mutating boundary is reported loudly."""

    from workflowy_mcp.mcp_api_registry import OperationRegistry, OperationSpec

    async def _handler(params):
        return {}

    rogue = OperationRegistry("rogue", "rogue family")
    rogue.register(
        OperationSpec(
            family="rogue",
            verb="mutate",
            cache_backed=True,
            summary="Unclassified mutating operation.",
            side_effects=("workflowy_mutation",),
            async_mode="never",
            params_schema={"type": "object", "properties": {}, "additionalProperties": False},
            returns={"description": "x"},
            examples=(),
            related=(),
            handler=_handler,
        )
    )
    missing = collect_unclassified_mutating_boundaries(
        registries=(rogue,), admission_classes=_admission_classes()
    )
    assert missing == ["rogue.mutate"]


def test_every_legacy_mutation_tool_resolves_to_a_classified_canonical():
    """S04 Remediation Chain 5 (expanded item 3): the strengthened legacy
    census — NO ``spec is None: continue`` escape for a wrapped legacy
    tool.  Every non-cache-free, non-split legacy tool resolves to its
    unique canonical OperationSpec; every Lightning mode resolves to its
    expected canonical operation; every mutating legacy route classifies
    ``refuse_under_hard_dirt``; every read route carries its intended
    read-safe classification (``proceed`` / ``proceed_exception``)."""
    from workflowy_mcp import server
    from workflowy_mcp.coherence.admission import (
        ADMISSION_CLASS_PROCEED,
        ADMISSION_CLASS_PROCEED_EXCEPTION,
        ADMISSION_CLASS_REFUSE_HARD_DIRT,
        mutation_admission_class,
    )
    from workflowy_mcp.legacy_tool_mapping import LEGACY_TOOL_MAPPING

    def _assert_intended_class(tool_name: str, spec) -> None:
        runtime = mutation_admission_class(
            spec.canonical_name, spec.side_effects, spec.cache_backed
        )
        if set(spec.side_effects) & MUTATING_EFFECTS:
            assert runtime == ADMISSION_CLASS_REFUSE_HARD_DIRT, (
                tool_name,
                spec.canonical_name,
                runtime,
            )
        else:
            assert runtime in (ADMISSION_CLASS_PROCEED, ADMISSION_CLASS_PROCEED_EXCEPTION), (
                tool_name,
                spec.canonical_name,
                runtime,
            )

    checked = 0
    for entry in LEGACY_TOOL_MAPPING:
        tool_name = str(getattr(entry, "legacy_tool", ""))
        if not tool_name or tool_name in server._LEGACY_CACHE_FREE_TOOLS:
            continue
        if tool_name == "lightning_rod":
            # The split-by-mode legacy tool: EVERY mode must resolve to its
            # expected canonical operation and classify correctly.
            for mode, expected in server._LEGACY_LIGHTNING_ROUTE_CANONICAL.items():
                spec = server._legacy_admission_spec(tool_name, {"mode": mode})
                assert spec is not None, mode
                assert spec.canonical_name == expected, (mode, spec.canonical_name)
                _assert_intended_class(f"lightning_rod[{mode}]", spec)
                checked += 1
            continue
        # Non-cache-free, non-split: resolution is MANDATORY (no escape).
        spec = server._legacy_admission_spec(tool_name, {})
        assert spec is not None, f"wrapped legacy tool unresolved: {tool_name}"
        assert spec.canonical_name == server._legacy_canonical_operations()[tool_name]
        _assert_intended_class(tool_name, spec)
        checked += 1
    # 26 wrapped non-split tools + 7 lightning modes.
    assert checked == 26 + 7


def test_every_wrapped_legacy_tool_resolves_to_its_unique_canonical():
    """S04 Remediation Chain 5 (expanded item 3): the resolution census —
    every non-cache-free, non-split legacy tool resolves to its unique
    canonical OperationSpec derived from its single unqualified route."""
    from workflowy_mcp import server
    from workflowy_mcp.legacy_tool_mapping import LEGACY_TOOL_MAPPING

    expected = {
        "workflowy_edit_node": "node.edit",
        "workflowy_add_tag": "tag.add",
        "workflowy_remove_tag": "tag.remove",
        "workflowy_rename_tag": "tag.rename",
        "workflowy_read_node": "node.read",
        "workflowy_delete_node": "node.delete",
        "workflowy_complete_node": "node.complete",
        "workflowy_uncomplete_node": "node.uncomplete",
        "workflowy_move_node": "node.move",
        "workflowy_export_node": "node.export",
        "merge_slice_groups": "slice_group.merge.persist",
        "workflowy_glimpse": "glimpse.run",
        "list_anchors": "anchor.dock.list",
        "create_anchor": "anchor.dock.create",
        "update_anchor": "anchor.dock.update",
        "add_to_anchor": "anchor.point.capture",
        "workflowy_scry": "node.scry",
        "workflowy_etch": "node.etch",
        "workflowy_etch_async": "node.etch",
        "read_text_snippet": "beacon.snippet",
        "read_text_snippet_by_uuid": "beacon.snippet",
        "read_text_snippet_by_symbol": "beacon.snippet",
        "read_uuid_from_symbol": "beacon.resolve",
        "read_uuid_from_anchor_point": "anchor.point.resolve",
        "update_beacon_from_node": "beacon.sync_node",
        "cartographer_refresh_async": "beacon.map.refresh",
    }
    mapped_names = {
        str(getattr(entry, "legacy_tool", ""))
        for entry in LEGACY_TOOL_MAPPING
        if str(getattr(entry, "disposition", "")) == "mapped"
    }
    wrapped = mapped_names - set(server._LEGACY_CACHE_FREE_TOOLS)
    assert set(expected) == wrapped
    for tool_name, canonical in expected.items():
        assert server._legacy_canonical_operations()[tool_name] == canonical
        spec = server._legacy_admission_spec(tool_name, {})
        assert spec is not None, tool_name
        assert spec.canonical_name == canonical


def test_every_lightning_mode_resolves_to_expected_canonical():
    """S04 Remediation Chain 5 (expanded item 3): every Lightning mode
    resolves to its expected canonical operation."""
    from workflowy_mcp import server

    expected = {
        "gather": "lightning_rod.gather",
        "render_tags": "lightning_rod.render_tags",
        "render_search": "lightning_rod.render_search",
        "render_symbols": "lightning_rod.render_symbols",
        "ensure_beacons": "beacon.tags.ensure",
        "remove_beacon_tags": "beacon.tags.remove",
        "mutate_node_tags": "tag.batch",
    }
    assert dict(server._LEGACY_LIGHTNING_ROUTE_CANONICAL) == expected
    for mode, canonical in expected.items():
        spec = server._legacy_admission_spec("lightning_rod", {"mode": mode})
        assert spec is not None, mode
        assert spec.canonical_name == canonical


def test_malformed_ordinary_mapping_row_fails_loudly(monkeypatch):
    """S04 Remediation Chain 5 (expanded item 3): a future ordinary
    (``disposition=\"mapped\"``) mapping row with zero, multiple, or
    selector-qualified routes fails loudly — never a silent ``targets[0]``
    pick."""
    import pytest

    from workflowy_mcp import server
    from workflowy_mcp.legacy_tool_mapping import LegacyRoute, LegacyToolMapping

    def _run_with(rows):
        monkeypatch.setattr(
            "workflowy_mcp.legacy_tool_mapping.LEGACY_TOOL_MAPPING", tuple(rows)
        )
        monkeypatch.setattr(server, "_LEGACY_CANONICAL_LOOKUP", None)
        with pytest.raises(ValueError, match="exactly one unqualified route"):
            server._legacy_canonical_operations()

    # Zero routes.
    _run_with([LegacyToolMapping(legacy_tool="rogue_zero", disposition="mapped", routes=())])
    # Multiple routes.
    _run_with(
        [
            LegacyToolMapping(
                legacy_tool="rogue_multi",
                disposition="mapped",
                routes=(LegacyRoute("node.read"), LegacyRoute("node.scry")),
            )
        ]
    )
    # Selector-qualified route.
    _run_with(
        [
            LegacyToolMapping(
                legacy_tool="rogue_selector",
                disposition="mapped",
                routes=(LegacyRoute("node.read", "mode=x"),),
            )
        ]
    )


def test_carto_recheck_descriptions_are_current():
    """S04 Remediation Chain 2 (work-order item 4) — currency assertion:
    the CARTO recheck descriptions record the common TASK execution recheck
    (before preparation side effects) and the INLINE phase-8 recheck
    (before mutation dispatch); the stale 'launch-gated / no mid-flight
    abort' wording must never return."""

    classes = _admission_classes()
    blob = json.dumps(classes).lower()
    assert "launch-gated" not in blob, "stale 'launch-gated' wording returned"
    assert "mid-flight abort" not in blob, "stale 'no mid-flight abort' wording returned"
    refresh = str(classes["carto:refresh_launch"]["recheck"]).lower()
    assert "prepared" in refresh, "prepared execution recheck not recorded"
    assert "before preparation" in refresh, "preparation-side-effect timing not recorded"
    assert "mutation dispatch" in refresh, "task recheck not recorded"
    assert "server._run_carto_job" in refresh
    for name in ("carto:markdown_generate_launch", "carto:task_dispatch", "carto:phase8_inline"):
        recheck = str(classes[name]["recheck"]).lower()
        assert "mutation dispatch" in recheck, f"{name}: execution recheck not recorded"
        assert ("_run_phase8_refresh_inline" if name == "carto:phase8_inline" else
                "_run_carto_job" if name == "carto:markdown_generate_launch" else
                "waiting grant") in recheck, f"{name}: current execution gate not named"
