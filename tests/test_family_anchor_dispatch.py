from __future__ import annotations

import asyncio
import secrets
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from workflowy_mcp import server
from workflowy_mcp.models.node import WorkFlowyNode
from workflowy_mcp.mcp_api_registry import (
    OperationSpecError,
    dispatch_operation,
    make_error,
    validate_params,
)


async def _noop_identity_proof(paths, *, operation):
    """S04 test seam: bypass the hard-barrier identity-proof gate.

    The gate itself is covered separately by the S04 suites; these tests
    exercise AP-ID reservation/assignment mechanics in isolation.
    """

    return None


def _stub_identity_proof(monkeypatch):
    monkeypatch.setattr(
        "workflowy_mcp.coherence.admission.require_cache_identity_proof",
        _noop_identity_proof,
    )


def test_anchor_registry_routes_all_verbs_and_wraps_mutations(monkeypatch):
    calls = []

    async def dock_list(include_local=False, target_tab=None, **_kwargs):
        calls.append(("list", include_local, target_tab))
        return {"success": True, "docks": []}

    async def dock_create(topic, description, parent, local, extra, tab):
        calls.append(("create", topic, description, parent, local, extra, tab))
        return {"success": True, "root_node_ids": ["D"]}

    async def dock_update(handle, topic, description, local, tab, *, favorite=None, families=None, replace_emoji=False):
        calls.append(("update", handle, topic, description, local, tab, favorite, families))
        return {"success": True, "no_op": False}

    async def promote(handle, local, tab):
        calls.append(("promote", handle, local, tab))
        return {"success": True, "no_op": False}

    async def demote(handle, tab):
        calls.append(("demote", handle, tab))
        return {"success": True, "no_op": False}

    async def dock_get_description(handle, tab):
        calls.append(("get_description", handle, tab))
        return (
            "[ap:ABC123] Doc ⚓ ▸0 · desc:0 · self:0 B · tree:0 B\n"
            "child docks: 0\n"
            "created - · modified - · abc123de\n\n"
            "note body"
        )

    async def peek(handle, tab, **_kwargs):
        calls.append(("peek", handle, tab))
        return {"success": True, "rows": []}

    async def render(handle, **kwargs):
        calls.append(("render", handle, kwargs))
        # Real impl: as_markdown=True composes preview_markdown; full/UUID
        # mode (as_markdown=False) produces preview_tree instead.
        if kwargs.get("as_markdown") is False:
            return {"success": True, "preview_tree": ["# Dock"], "node_count": 1}
        return {"success": True, "preview_markdown": "# Dock"}

    async def capture(parent, **kwargs):
        calls.append(("capture", parent, kwargs))
        return {"success": True, "root_node_ids": ["P"]}

    async def assign(handle, ap_id="auto"):
        calls.append(("assign", handle, ap_id))
        return {"success": True, "uuid": handle, "ap_id": "[ap:ABC123]"}

    async def remove_id(handle):
        calls.append(("remove_id", handle))
        return {
            "success": True,
            "uuid": handle,
            "removed_ap_id": "[ap:ABC123]",
            "no_op": False,
            "warnings": ["citations invalidated"],
        }

    async def resolve(ap_id):
        calls.append(("resolve", ap_id))
        return {"success": True, "match_count": 0, "candidates": []}

    async def point_read(handle, **kwargs):
        calls.append(("point_read", handle, kwargs))
        return "[ap:ABC123] Doc header\n\nnote body"

    async def touch(handles, *, allow_docks=False, target_tab=None):
        calls.append(("touch", list(handles), allow_docks, target_tab))
        return {
            "success": True, "changed": True, "partial_success": False,
            "results": [], "summary": {"touched": len(handles)}, "warnings": [],
        }

    monkeypatch.setattr(server, "_anchor_dock_list_impl", dock_list)
    monkeypatch.setattr(server, "_anchor_dock_create_impl", dock_create)
    monkeypatch.setattr(server, "_anchor_dock_update_impl", dock_update)
    monkeypatch.setattr(server, "_anchor_dock_promote_impl", promote)
    monkeypatch.setattr(server, "_anchor_dock_demote_impl", demote)
    monkeypatch.setattr(server, "_anchor_dock_get_description_impl", dock_get_description)
    monkeypatch.setattr(server, "_anchor_dock_peek_impl", peek)
    monkeypatch.setattr(server, "_anchor_dock_render_impl", render)
    monkeypatch.setattr(server, "_anchor_point_capture_impl", capture)
    monkeypatch.setattr(server, "_anchor_point_assign_id_impl", assign)
    monkeypatch.setattr(server, "_anchor_point_remove_id_impl", remove_id)
    monkeypatch.setattr(server, "_anchor_point_resolve_impl", resolve)
    monkeypatch.setattr(server, "_anchor_point_read_impl", point_read)
    async def promote_points(**kwargs):
        calls.append(("promote_points", kwargs))
        return {
            "success": True, "changed": True, "partial_success": False,
            "results": [], "summary": {"promoted": len(kwargs.get("items") or kwargs.get("handles") or [])},
            "warnings": [],
        }

    monkeypatch.setattr(server, "_anchor_point_touch_impl", touch)
    monkeypatch.setattr(server, "_anchor_point_promote_impl", promote_points)
    registry = server.build_anchor_registry()

    assert asyncio.run(dispatch_operation(registry, "dock.list", {"include_local": True}))["success"]
    assert asyncio.run(dispatch_operation(registry, "dock.create", {"topic": "T", "description": "D", "parent_uuid": "P"}))["changed"]
    assert asyncio.run(dispatch_operation(registry, "dock.update", {"dock_handle": "D", "local": True}))["operation"] == "anchor.dock.update"
    assert asyncio.run(dispatch_operation(registry, "dock.promote", {"node_id": "N"}))["changed"]
    assert asyncio.run(dispatch_operation(registry, "dock.demote", {"dock_handle": "D"}))["changed"]
    get_description_result = asyncio.run(
        dispatch_operation(registry, "dock.get_description", {"dock_handle": "D"})
    )
    assert isinstance(get_description_result, str)
    assert get_description_result.startswith("[ap:ABC123] Doc")
    assert "child docks: 0" in get_description_result
    assert asyncio.run(dispatch_operation(registry, "dock.peek", {"dock_handle": "D"}))["rows"] == []
    # dock.render defaults to raw Markdown (the agent-facing GLIMPSE-family shaping);
    # as_markdown=false preserves the established structured render result.
    assert asyncio.run(dispatch_operation(registry, "dock.render", {"dock_handle": "D", "since": "last 2"})) == "# Dock"
    structured = asyncio.run(dispatch_operation(registry, "dock.render", {"dock_handle": "D", "as_markdown": False}))
    assert structured["success"] is True and structured["preview_tree"] == ["# Dock"]
    assert asyncio.run(dispatch_operation(registry, "point.capture", {"parent_uuid": "P", "content": "B", "name": "N"}))["changed"]
    assert asyncio.run(dispatch_operation(registry, "point.assign_id", {"node_id": "N"}))["changed"]
    removed = asyncio.run(dispatch_operation(registry, "point.remove_id", {"node_id": "N"}))
    assert removed["changed"] is True
    assert removed["warnings"] == ["citations invalidated"]
    assert asyncio.run(dispatch_operation(registry, "point.resolve", {"ap_id": "ABC123"}))["match_count"] == 0
    point_read_result = asyncio.run(dispatch_operation(registry, "point.read", {"node_handle": "[ap:A]"}))
    assert point_read_result.startswith("[ap:ABC123] Doc header")
    assert point_read_result.endswith("note body")
    touched = asyncio.run(dispatch_operation(registry, "point.touch", {"handles": ["[ap:A]", "B"], "allow_docks": True}))
    assert touched["operation"] == "anchor.point.touch" and touched["changed"] is True
    assert touched["partial_success"] is False and touched["detail"]["summary"]["touched"] == 2
    promoted = asyncio.run(dispatch_operation(registry, "point.promote", {
        "items": [{"handle": "[ap:A]", "destination": "[ap:D]", "tags": ["t"]}],
    }))
    assert promoted["operation"] == "anchor.point.promote" and promoted["changed"] is True
    assert promoted["partial_success"] is False and promoted["detail"]["summary"]["promoted"] == 1

    assert [row[0] for row in calls] == [
        "list", "create", "update", "promote", "demote", "get_description",
        "peek", "render", "render", "capture", "assign", "remove_id",
        "resolve", "point_read", "touch",
        "promote_points",
    ]
    assert calls[5][1:] == ("D", None)
    assert calls[13][1] == "[ap:A]"
    # point.read defaults to numbered rows via the shared flag set.
    assert calls[13][2]["with_line_numbers"] is True
    assert calls[14][1:] == (["[ap:A]", "B"], True, None)
    assert calls[15][1]["items"] == [{"handle": "[ap:A]", "destination": "[ap:D]", "tags": ["t"]}]
    assert calls[15][1]["include_completed"] is False and calls[15][1]["mode"] is None
    assert calls[1][3] == "P"
    # COORGU: legacy `since` reaches the impl as the canonical `recent`.
    assert calls[7][2]["recent"] == "last 2"
    assert calls[8][2]["as_markdown"] is False
    assert calls[9][2]["content"] == "B"


def test_anchor_registry_metadata_aliases_domain_codes_and_registration():
    registry = server.ANCHOR_REGISTRY
    assert [spec.verb for spec in registry.operations()] == [
        "dock.list", "dock.create", "dock.update", "dock.promote",
        "dock.demote", "dock.get_description", "dock.peek", "dock.render",
        "point.capture",
        "point.extract", "point.assign_id", "point.remove_id", "point.resolve",
        "point.read", "point.touch", "point.promote",
        "search",
    ]
    assert registry.resolve("dock.create").param_aliases == {
        "parent_uuid": "parent_handle", "name": "topic", "note": "description",
    }
    assert registry.resolve("dock.update").param_aliases == {
        "node_id": "dock_handle", "name": "topic", "note": "description",
    }
    assert "dock_families_ambiguous" in registry.resolve("dock.update").domain_error_codes
    assert "dock_family_not_on_node" in registry.resolve("dock.update").domain_error_codes
    assert registry.resolve("dock.render").param_aliases == {"since": "recent", "tail": "recent"}
    assert registry.resolve("dock.peek").param_aliases == {"max_chars": "max_name_chars"}
    # Unit B: the one harmless-ignored registration; every other anchor verb
    # keeps the empty default.
    assert registry.resolve("dock.peek").param_harmless_ignored == ("headings_only",)
    assert [
        spec.verb for spec in registry.operations() if spec.param_harmless_ignored
    ] == ["dock.peek"]
    assert registry.resolve("dock.list").param_aliases == {"max_chars": "max_name_chars"}
    assert registry.resolve("point.remove_id").param_aliases == {"node_id": "node_handle"}
    assert "ap_id_exists" in registry.resolve("point.assign_id").domain_error_codes
    assert "ap_id_collision" in registry.resolve("point.capture").domain_error_codes
    assert "duplicate_handles" in registry.resolve("point.touch").domain_error_codes
    assert "promote_cycle_refused" in registry.resolve("point.promote").domain_error_codes
    for spec in registry.operations():
        for example in registry.describe_full(spec.verb)["examples"]:
            validate_params(spec, example["call"].get("params") or {})

    bad = asyncio.run(dispatch_operation(registry, "dock.create", {"topic": "T", "description": "D", "detail": "legacy"}))
    assert bad["error"]["code"] == "forbidden_parameter"
    with pytest.raises(OperationSpecError):
        make_error(
            family="anchor",
            operation="anchor.point.assign_id",
            code="undeclared_anchor_error",
            message="no",
            operation_spec=registry.resolve("point.assign_id"),
        )
    names = {tool.name for tool in asyncio.run(server.mcp.list_tools())}
    assert "anchor" in names
    assert {"list_anchors", "create_anchor", "update_anchor", "add_to_anchor"}.isdisjoint(names)


def test_dock_peek_harmless_headings_only_is_dropped_at_the_gate(monkeypatch):
    """Unit B verb-level contract: dock.peek accepts headings_only with ANY
    value and returns the normal peek; everything else at the gate is
    unchanged. On the pre-fix tree every headings_only call below failed with
    forbidden_parameter (verified on 0feac7a)."""
    calls = []

    async def peek(handle, tab, **kwargs):
        calls.append((handle, tab, kwargs))
        return {"success": True, "rows": []}

    monkeypatch.setattr(server, "_anchor_dock_peek_impl", peek)
    registry = server.build_anchor_registry()

    baseline = asyncio.run(
        dispatch_operation(registry, "dock.peek", {"dock_handle": "D"})
    )
    assert baseline["rows"] == []

    with_true = asyncio.run(
        dispatch_operation(
            registry, "dock.peek", {"dock_handle": "D", "headings_only": True}
        )
    )
    with_false = asyncio.run(
        dispatch_operation(
            registry, "dock.peek", {"dock_handle": "D", "headings_only": False}
        )
    )
    # Both succeed, both return the normal peek, and the results AND the impl
    # calls are identical -- the value is ignored, the key is dropped.
    assert with_true == baseline
    assert with_false == baseline
    assert calls[0] == calls[1] == calls[2]

    # An unrelated forbidden parameter is still rejected.
    bogus = asyncio.run(
        dispatch_operation(registry, "dock.peek", {"dock_handle": "D", "bogus": 1})
    )
    assert bogus["error"]["code"] == "forbidden_parameter"
    assert len(calls) == 3  # rejected at the gate; the impl was never reached

    # Existing alias behaviour unchanged, including composition with the
    # harmless key in one envelope.
    aliased = asyncio.run(
        dispatch_operation(
            registry, "dock.peek", {"dock_handle": "D", "max_chars": 12}
        )
    )
    assert aliased == baseline
    assert calls[3][2]["max_chars"] == 12
    both = asyncio.run(
        dispatch_operation(
            registry,
            "dock.peek",
            {"dock_handle": "D", "max_chars": 12, "headings_only": True},
        )
    )
    assert both == baseline
    assert calls[4][2]["max_chars"] == 12
    conflict = asyncio.run(
        dispatch_operation(
            registry,
            "dock.peek",
            {"dock_handle": "D", "max_chars": 12, "max_name_chars": 20},
        )
    )
    assert conflict["error"]["code"] == "forbidden_parameter"

    # Documented Unit B bound (out of scope, recorded for Dan): the tolerated
    # slip spelling `headings` is still rejected on dock.peek -- only the
    # exact `headings_only` key is registered harmless, and the slips mapper
    # only maps onto real schema properties.
    slip = asyncio.run(
        dispatch_operation(registry, "dock.peek", {"dock_handle": "D", "headings": True})
    )
    assert slip["error"]["code"] == "forbidden_parameter"


def test_dock_create_name_note_aliases_and_optional_description(monkeypatch):
    """Unit 7A (VBIKIZ): dock.create accepts name=topic and note=description
    through the ONE existing param_aliases layer, and description is optional
    (default empty) so {name, parent_handle, local} alone creates the Dock.
    On the pre-fix tree (07c744d) every alias call below was refused with
    forbidden_parameter and the bare-topic call failed the required
    description check."""
    calls = []

    async def dock_create(topic, description="", parent=None, local=False,
                          extra=None, tab=None, *, favorite=False):
        calls.append(("create", topic, description, parent, local, extra, tab))
        return {"success": True, "root_node_ids": ["D"]}

    monkeypatch.setattr(server, "_anchor_dock_create_impl", dock_create)
    registry = server.build_anchor_registry()

    # Done-when 1: {name, parent_handle, local} and nothing else.
    ack = asyncio.run(
        dispatch_operation(
            registry, "dock.create", {"name": "T", "parent_handle": "P", "local": True}
        )
    )
    assert ack["changed"] is True
    assert calls[-1][1] == "T"
    assert calls[-1][2] == ""  # description defaulted to empty
    assert calls[-1][3] == "P" and calls[-1][4] is True

    # Done-when 3 (first half): description alone is no longer required.
    asyncio.run(dispatch_operation(registry, "dock.create", {"topic": "T"}))
    assert calls[-1][1] == "T" and calls[-1][2] == ""

    # Done-when 2: note sets the description.
    asyncio.run(
        dispatch_operation(registry, "dock.create", {"topic": "T", "note": "D"})
    )
    assert calls[-1][1] == "T" and calls[-1][2] == "D"

    # Mixed spellings in one call are accepted too.
    asyncio.run(
        dispatch_operation(
            registry, "dock.create", {"name": "T", "description": "D"}
        )
    )
    assert calls[-1][1] == "T" and calls[-1][2] == "D"

    # Done-when 4: alias + canonical together is the alias system's existing
    # conflict error (wording asserted so the test distinguishes the alias
    # conflict from the pre-fix unknown-parameter refusal).
    for conflict_params in (
        {"name": "N", "topic": "T"},
        {"topic": "T", "description": "D", "note": "N"},
    ):
        bad = asyncio.run(
            dispatch_operation(registry, "dock.create", conflict_params)
        )
        assert bad["error"]["code"] == "forbidden_parameter"
        assert "conflict" in bad["error"]["message"]
        assert "pass only canonical" in bad["error"]["message"]
    assert len(calls) == 4  # both conflicts rejected at the gate


def test_dock_update_name_note_aliases(monkeypatch):
    """Unit 7A (VBIKIZ): dock.update takes the same name=topic and
    note=description aliases for consistency (pre-fix: forbidden_parameter
    unknown-parameter refusals on both)."""
    calls = []

    async def dock_update(handle, topic=None, description=None, local=None,
                          tab=None, *, favorite=None, families=None,
                          replace_emoji=False):
        calls.append(("update", handle, topic, description))
        return {"success": True, "no_op": False}

    monkeypatch.setattr(server, "_anchor_dock_update_impl", dock_update)
    registry = server.build_anchor_registry()

    ack = asyncio.run(
        dispatch_operation(
            registry, "dock.update", {"dock_handle": "D", "name": "New", "note": "Desc"}
        )
    )
    assert ack["operation"] == "anchor.dock.update"
    assert calls[-1] == ("update", "D", "New", "Desc")

    # Single alias spellings.
    asyncio.run(
        dispatch_operation(registry, "dock.update", {"dock_handle": "D", "name": "N2"})
    )
    assert calls[-1] == ("update", "D", "N2", None)
    asyncio.run(
        dispatch_operation(
            registry, "dock.update", {"dock_handle": "D", "note": "D2"}
        )
    )
    assert calls[-1] == ("update", "D", None, "D2")

    # Alias + canonical together: the existing conflict error, wording
    # asserted (pre-fix this refusal was an unknown-parameter one).
    bad = asyncio.run(
        dispatch_operation(
            registry, "dock.update", {"dock_handle": "D", "name": "A", "topic": "B"}
        )
    )
    assert bad["error"]["code"] == "forbidden_parameter"
    assert "conflict" in bad["error"]["message"]
    bad = asyncio.run(
        dispatch_operation(
            registry,
            "dock.update",
            {"dock_handle": "D", "note": "A", "description": "B"},
        )
    )
    assert bad["error"]["code"] == "forbidden_parameter"
    assert "conflict" in bad["error"]["message"]
    assert len(calls) == 3  # both conflicts rejected at the gate


def test_dock_update_forwards_replace_emoji(monkeypatch):
    """Unit 7E (Z8Q0PF+LITF2F): the handler forwards the opt-in emoji flag."""
    calls = []

    async def dock_update(handle, topic=None, description=None, local=None,
                          tab=None, *, favorite=None, families=None,
                          replace_emoji=False):
        calls.append(("update", handle, topic, replace_emoji))
        return {"success": True, "no_op": False}

    monkeypatch.setattr(server, "_anchor_dock_update_impl", dock_update)
    registry = server.build_anchor_registry()

    asyncio.run(
        dispatch_operation(
            registry,
            "dock.update",
            {"dock_handle": "D", "topic": "New", "replace_emoji": True},
        )
    )
    assert calls[-1] == ("update", "D", "New", True)
    # Omitted -> the impl default (False) is the single authority.
    asyncio.run(
        dispatch_operation(registry, "dock.update", {"dock_handle": "D", "topic": "N2"})
    )
    assert calls[-1] == ("update", "D", "N2", False)
    assert len(calls) == 2


def test_dock_list_include_local_and_canonical_rows(monkeypatch):
    seen = []

    async def listing(**kwargs):
        seen.append(kwargs)
        return {"success": True, "sessions": [{"id": "D", "name": "Dock", "is_local": True}]}

    monkeypatch.setattr(server, "_list_anchors_internal", listing)
    result = asyncio.run(server._anchor_dock_list_impl(True, "tab", detail="full"))
    assert seen[0]["show_local_anchors"] is True
    assert result["dock_count"] == 1
    assert result["docks"][0]["dock_handle"] == "D"
    assert result["docks"][0]["topic"] == "Dock"
    assert result["docks"][0]["is_local"] is True


def test_promote_and_demote_are_idempotent_and_use_all_dock_tag_forms(monkeypatch):
    config = (["wfy-ns"], [], None, {"note_session_local_postfixes": ["local"]})
    monkeypatch.setattr(server, "_resolve_note_session_config", lambda _tab: _async_value(config))
    monkeypatch.setattr(server, "_resolve_generic_node_handle", lambda value, **_kw: _async_value(value))
    monkeypatch.setattr(server, "_classify_node_map_state", lambda *_a: {"state": "unmapped"})

    cache = {"N": {"id": "N", "name": "Plain", "parent_id": None}}
    monkeypatch.setattr(server, "_anchor_cache_by_id", lambda: cache)
    added = []

    async def add(handle, tags):
        added.append((handle, tags))
        return {"success": True, "no_op": False, "tags_added": [tags]}

    monkeypatch.setattr(server, "_tag_add_impl", add)
    result = asyncio.run(server._anchor_dock_promote_impl("N", local=True))
    assert result["success"] is True
    # §6.7: ONE _tag_add_impl call carrying the LIST of attribute tags.
    assert added == [("N", ["#wfy-ns--local"])]

    cache["N"]["name"] = "Dock #wfy-ns--local"
    added.clear()
    no_op = asyncio.run(server._anchor_dock_promote_impl("N", local=False))
    assert no_op["no_op"] is True
    assert added == []

    removed = []

    async def remove(handle, tags):
        removed.append((handle, tags))
        return {"success": True, "no_op": False}

    monkeypatch.setattr(server, "_tag_remove_impl", remove)
    cache["N"]["name"] = "Dock #wfy-ns #wfy-ns--local #other"
    demoted = asyncio.run(server._anchor_dock_demote_impl("N"))
    assert demoted["subtree_preserved"] is True
    assert removed == [("N", ["#wfy-ns", "#wfy-ns--local"])]

    cache["N"]["name"] = "Plain #other"
    removed.clear()
    no_op_demote = asyncio.run(server._anchor_dock_demote_impl("N"))
    assert no_op_demote["no_op"] is True
    assert removed == []


def test_promote_uses_shared_placement_guard(monkeypatch):
    config = (["wfy-ns"], [], None, {"note_session_local_postfixes": ["local"]})
    monkeypatch.setattr(server, "_resolve_note_session_config", lambda _tab: _async_value(config))
    monkeypatch.setattr(server, "_resolve_generic_node_handle", lambda value, **_kw: _async_value(value))
    monkeypatch.setattr(server, "_anchor_cache_by_id", lambda: {"N": {"id": "N", "name": "Plain"}})
    monkeypatch.setattr(server, "_classify_node_map_state", lambda *_a: {"state": "folder", "path_value": "X"})
    with pytest.raises(RuntimeError, match="FOLDER"):
        asyncio.run(server._anchor_dock_promote_impl("N"))


def test_update_locality_canonicalizes_all_dock_forms_in_one_write(monkeypatch, tmp_path):
    from test_mutation_regions_n05_fences import legacy_region_fronts
    key = "dddddddd-1111-4111-8111-111111111111"
    legacy_region_fronts(monkeypatch, tmp_path, nodes=[{"id": key, "parent_id": None}], entries=("_anchor_dock_update_impl",))
    config = (["wfy-ns"], [], None, {"note_session_local_postfixes": ["local"]})
    monkeypatch.setattr(server, "_resolve_note_session_config", lambda _tab: _async_value(config))
    monkeypatch.setattr(server, "_resolve_generic_node_handle", lambda value, **_kw: _async_value(key))
    monkeypatch.setattr(
        server,
        "_anchor_cache_by_id",
        lambda: {key: {"id": key, "name": "Topic #wfy-ns #other #wfy-ns--local"}},
    )
    monkeypatch.setattr(server, "_classify_node_map_state", lambda *_a: {"state": "unmapped"})
    monkeypatch.setattr(server, "_rate_limiter", None)

    async def _resolve(node_id, *, label, required=True, role=None):
        return node_id

    monkeypatch.setattr(server, "_resolve_mutator_node_id", _resolve)

    # Real edit core + ONE fake client: dock.update reads the LIVE node once,
    # composes (protected-name policy + the narrow locality tag transform),
    # and writes through _node_edit_core reusing that same live snapshot.
    client = MagicMock()
    client.get_node = AsyncMock(
        return_value=WorkFlowyNode(id=key, name="Topic #wfy-ns #other #wfy-ns--local")
    )
    client.update_node = AsyncMock(return_value=WorkFlowyNode(id=key))
    monkeypatch.setattr(server, "get_client", lambda: client)

    result = asyncio.run(server._anchor_dock_update_impl("D", local=True))
    assert client.update_node.await_count == 1
    request = client.update_node.call_args[0][1]
    assert request.name == "Topic #wfy-ns--local #other"
    assert result["local"] is True
    assert result["tags"] == ["#wfy-ns--local", "#other"]
    # One live GET feeds both the no-op composition and the core write.
    assert client.get_node.await_count == 1

    client.get_node = AsyncMock(
        return_value=WorkFlowyNode(id=key, name="Topic #wfy-ns #other")
    )
    client.update_node.reset_mock()
    unchanged = asyncio.run(server._anchor_dock_update_impl("D", topic="Topic", local=None))
    assert unchanged["no_op"] is True
    client.update_node.assert_not_called()


def test_update_description_only_never_recomposes_or_misreports_the_name(monkeypatch, tmp_path):
    from test_mutation_regions_n05_fences import legacy_region_fronts
    key = "dddddddd-1111-4111-8111-111111111111"
    legacy_region_fronts(monkeypatch, tmp_path, nodes=[{"id": key, "parent_id": None}], entries=("_anchor_dock_update_impl",))
    # Post-audit fix: a description-only update must not recompose the name
    # (canonical re-join would drop the trident here) nor report a rename
    # that the name=None write never performs.
    config = (["wfy-ns"], [], None, {"note_session_local_postfixes": ["local"]})
    monkeypatch.setattr(server, "_resolve_note_session_config", lambda _tab: _async_value(config))
    monkeypatch.setattr(server, "_resolve_generic_node_handle", lambda value, **_kw: _async_value(key))
    monkeypatch.setattr(
        server,
        "_anchor_cache_by_id",
        lambda: {key: {"id": key, "name": "Topic 🔱 #wfy-ns"}},
    )
    monkeypatch.setattr(server, "_classify_node_map_state", lambda *_a: {"state": "unmapped"})
    monkeypatch.setattr(server, "_rate_limiter", None)

    async def _resolve(node_id, *, label, required=True, role=None):
        return node_id

    monkeypatch.setattr(server, "_resolve_mutator_node_id", _resolve)

    client = MagicMock()
    client.get_node = AsyncMock(return_value=WorkFlowyNode(id=key, name="Topic 🔱 #wfy-ns"))
    client.update_node = AsyncMock(return_value=WorkFlowyNode(id=key))
    monkeypatch.setattr(server, "get_client", lambda: client)

    result = asyncio.run(server._anchor_dock_update_impl("D", description="new note"))
    assert client.update_node.await_count == 1
    request = client.update_node.call_args[0][1]
    assert request.note == "new note"
    assert request.name is None
    assert result["renamed"] is False
    assert result["new_name"] == "Topic 🔱 #wfy-ns"
    assert result["old_name"] == "Topic 🔱 #wfy-ns"


def test_peek_priority_shape_and_child_dock_signpost(monkeypatch):
    config = (["wfy-ns"], [], None, {"note_session_local_postfixes": ["local"]})
    monkeypatch.setattr(server, "_resolve_note_session_config", lambda _tab: _async_value(config))
    monkeypatch.setattr(server, "_resolve_generic_node_handle", lambda value, **_kw: _async_value(value))
    monkeypatch.setattr(
        server,
        "_anchor_cache_by_id",
        lambda: {
            "D": {"id": "D", "name": "Root #wfy-ns"},
            "B": {"id": "B", "name": "Second", "parent_id": "D", "priority": 2},
            "A": {"id": "A", "name": "[ap:ABC123] Child #wfy-ns--local", "parent_id": "D", "priority": 1},
            "AA": {"id": "AA", "name": "Grandchild", "parent_id": "A", "priority": 1},
        },
    )
    result = asyncio.run(server._anchor_dock_peek_impl("D", detail="full"))
    assert [row["uuid"] for row in result["rows"]] == ["A", "B"]
    first = result["rows"][0]
    assert first == {
        "uuid": "A",
        # UTAERZ: the walked parent, so the flat compact view can hint and
        # the JSON carries it.
        "parent_id": "D",
        "ap_id": "[ap:ABC123]",
        # WG3QTR: `name` is the FULL name verbatim, trailing #tags included;
        # `title`/`tags` carry the split for callers that want it.
        "name": "[ap:ABC123] Child #wfy-ns--local",
        "title": "[ap:ABC123] Child",
        "tags": ["#wfy-ns--local"],
        "is_dock": True,
        "is_local_dock": True,
        "is_favorite_dock": False,
        "has_children": True,
        "child_count": 1,
        "descendant_count": 1,
        "self_bytes": len("[ap:ABC123] Child #wfy-ns--local".encode("utf-8")),
        "subtree_bytes": len("[ap:ABC123] Child #wfy-ns--localGrandchild".encode("utf-8")),
        "depth": 0,
        "completed": False,
        "created_at": None,
        "modified_at": None,
    }
    # No recency knobs: document order, and the JSON says so.
    assert result["selection"] == {"recent": None, "recent_by": None, "candidates": 2, "selected": 2, "completed_excluded": 0}
    assert result["sort"] == {"by": "none", "direction": "asc"}


def test_render_is_bounded_preset_and_recent_token_reaches_since_edge(monkeypatch):
    config = (["wfy-ns"], [], None, {"note_session_local_postfixes": ["local"]})
    monkeypatch.setattr(server, "_resolve_note_session_config", lambda _tab: _async_value(config))
    monkeypatch.setattr(server, "_resolve_generic_node_handle", lambda value, **_kw: _async_value("D"))
    monkeypatch.setattr(server, "_anchor_cache_by_id", lambda: {"D": {"id": "D", "name": "Dock #wfy-ns"}})
    seen = []

    async def raw(**kwargs):
        seen.append(kwargs)
        return {"success": True, "preview_markdown": "# Dock"}

    monkeypatch.setattr(server, "_glimpse_root_uuid_from_cache", raw)
    result = asyncio.run(server._anchor_dock_render_impl("D", recent="last 2", headings_only=True))
    assert result["success"] is True
    assert seen[0]["root_uuid"] == "D"
    assert seen[0]["headings_only"] is True
    assert seen[0]["since"].form == "item_count"
    assert seen[0]["boundary_predicate"]({"name": "Child #wfy-ns"}) is True


def test_generate_reserve_and_release_close_in_process_ap_id_races(monkeypatch):
    _stub_identity_proof(monkeypatch)
    monkeypatch.setattr(
        server,
        "get_client",
        lambda: SimpleNamespace(_coherence_paths=lambda: None),
    )
    values = iter("AAAAAABBBBBB")
    monkeypatch.setattr(secrets, "choice", lambda _alphabet: next(values))
    assert server.generate_ap_core(lambda core: core == "AAAAAA") == "BBBBBB"

    server._ANCHOR_AP_ID_RESERVED_CORES.clear()
    server._ANCHOR_AP_ID_ALLOCATED_CORES.clear()
    first = asyncio.run(server._anchor_reserve_ap_core("[ap:ABC123]", nodes_by_id={}))
    assert first == "ABC123"
    with pytest.raises(server._AnchorOperationError) as collision:
        asyncio.run(server._anchor_reserve_ap_core("ABC123", nodes_by_id={}))
    assert collision.value.code == "ap_id_collision"
    asyncio.run(server._anchor_finish_ap_reservation(first, committed=False))
    assert asyncio.run(server._anchor_reserve_ap_core("ABC123", nodes_by_id={})) == "ABC123"
    asyncio.run(server._anchor_finish_ap_reservation("ABC123", committed=False))


def test_assign_id_refuses_existing_preserves_name_and_releases_failed_reservation(monkeypatch, tmp_path):
    from test_mutation_regions_n05_fences import legacy_region_fronts
    key = "11111111-1111-4111-8111-111111111111"
    legacy_region_fronts(monkeypatch, tmp_path, nodes=[{"id": key, "parent_id": None}], entries=("_anchor_point_assign_id_impl",))
    _stub_identity_proof(monkeypatch)
    monkeypatch.setattr(server, "_resolve_generic_node_handle", lambda value, **_kw: _async_value(key))
    monkeypatch.setattr(server, "_anchor_cache_by_id", lambda: {})
    monkeypatch.setattr(server, "generate_ap_core", lambda _check, tries=8: "NEW123")
    server._ANCHOR_AP_ID_RESERVED_CORES.clear()
    server._ANCHOR_AP_ID_ALLOCATED_CORES.clear()
    client = MagicMock()
    monkeypatch.setattr(server, "get_client", lambda: client)

    monkeypatch.setattr(server, "_node_read_impl", lambda *_a, **_kw: _async_value(SimpleNamespace(name="[ap:OLD123] Name #tag")))
    with pytest.raises(server._AnchorOperationError) as existing:
        asyncio.run(server._anchor_point_assign_id_impl("N"))
    assert existing.value.code == "ap_id_exists"

    monkeypatch.setattr(server, "_node_read_impl", lambda *_a, **_kw: _async_value(SimpleNamespace(name="Name  #tag")))

    async def _resolve(node_id, *, label, required=True, role=None):
        return node_id

    monkeypatch.setattr(server, "_resolve_mutator_node_id", _resolve)
    monkeypatch.setattr(server, "_rate_limiter", None)
    # Real edit core via the narrow _assign_ap_core channel; the read-node
    # snapshot is injected (_current_node), so no second GET is issued, and
    # the existing title + trailing tag are preserved (whitespace canonicalized
    # by the composer: 'Name  #tag' -> '[ap:NEW123] Name #tag').
    client = MagicMock()
    client.get_node = AsyncMock()
    client.update_node = AsyncMock(return_value=WorkFlowyNode(id=key))
    monkeypatch.setattr(server, "get_client", lambda: client)

    result = asyncio.run(server._anchor_point_assign_id_impl("N"))
    request = client.update_node.call_args[0][1]
    assert request.name == "[ap:NEW123] Name #tag"
    client.get_node.assert_not_called()
    assert result["ap_id"] == "[ap:NEW123]"
    assert result["new_name"] == "[ap:NEW123] Name #tag"

    server._ANCHOR_AP_ID_ALLOCATED_CORES.clear()

    async def fail(*_a, **_kw):
        raise RuntimeError("write failed")

    monkeypatch.setattr(server, "_node_edit_core", fail)
    with pytest.raises(RuntimeError, match="write failed"):
        asyncio.run(server._anchor_point_assign_id_impl("N"))
    assert "NEW123" not in server._ANCHOR_AP_ID_RESERVED_CORES


def test_remove_id_is_idempotent_and_preserves_decoration_title_tags(monkeypatch, tmp_path):
    from test_mutation_regions_n05_fences import legacy_region_fronts
    key = "11111111-1111-4111-8111-111111111111"
    legacy_region_fronts(monkeypatch, tmp_path, nodes=[{"id": key, "parent_id": None}], entries=("_anchor_point_remove_id_impl",))
    _stub_identity_proof(monkeypatch)
    monkeypatch.setattr(
        server,
        "_resolve_generic_node_handle",
        lambda value, **_kw: _async_value(key),
    )
    monkeypatch.setattr(server, "_rate_limiter", None)

    async def _resolve(node_id, *, label, required=True, role=None):
        return node_id

    monkeypatch.setattr(server, "_resolve_mutator_node_id", _resolve)

    # AP-present path: one structured live read, injected into the core; no
    # second GET. Only the leading AP token disappears. Decoration/title/tags
    # survive and the allocated-core ledger is deliberately retained.
    node = WorkFlowyNode(id=key, name="🔱 [ap:ABC123] Name #tag")
    monkeypatch.setattr(
        server,
        "_node_read_impl",
        lambda *_a, **_kw: _async_value(node),
    )
    server._ANCHOR_AP_ID_ALLOCATED_CORES.clear()
    client = MagicMock()
    client.get_node = AsyncMock()
    client.update_node = AsyncMock(return_value=WorkFlowyNode(id=key))
    monkeypatch.setattr(server, "get_client", lambda: client)

    result = asyncio.run(server._anchor_point_remove_id_impl("[ap:ABC123]"))
    request = client.update_node.call_args[0][1]
    assert request.name == "🔱 Name #tag"
    assert client.get_node.await_count == 0
    assert result["removed_ap_id"] == "[ap:ABC123]"
    assert result["new_name"] == "🔱 Name #tag"
    assert result["no_op"] is False
    assert "no longer resolve" in result["warnings"][0]
    assert "ABC123" in server._ANCHOR_AP_ID_ALLOCATED_CORES
    with pytest.raises(server._AnchorOperationError) as reused:
        asyncio.run(server._anchor_reserve_ap_core("ABC123", nodes_by_id={}))
    assert reused.value.code == "ap_id_collision"

    # AP-absent path: true no-op, preserving the exact name and issuing zero
    # writes (including no canonical tag/trident normalization).
    no_ap = WorkFlowyNode(id=key, name="⚡ Name 🔱 #tag")
    monkeypatch.setattr(
        server,
        "_node_read_impl",
        lambda *_a, **_kw: _async_value(no_ap),
    )
    client.update_node.reset_mock()
    no_op = asyncio.run(server._anchor_point_remove_id_impl("N"))
    assert no_op["no_op"] is True
    assert no_op["removed_ap_id"] is None
    assert no_op["new_name"] == "⚡ Name 🔱 #tag"
    assert no_op["warnings"] == []
    client.update_node.assert_not_called()


def test_capture_ap_id_policy_is_pre_etch_atomic_and_single_root_only(monkeypatch, tmp_path):
    from test_mutation_regions_n05_fences import legacy_region_fronts
    key = "22222222-2222-4222-8222-222222222222"
    legacy_region_fronts(monkeypatch, tmp_path, nodes=[{"id": key, "parent_id": None}], entries=("_anchor_point_capture_impl",))
    monkeypatch.setattr(server, "_resolve_generic_node_handle", lambda value, **_kw: _async_value(key))
    _stub_identity_proof(monkeypatch)
    class Client:
        def __init__(self):
            self.payloads = []

        def _coherence_paths(self):
            return None

        async def add_to_anchor(self, **kwargs):
            self.payloads.append(kwargs)
            return {"success": True, "root_node_ids": ["UUID-1"], "nodes_created": 1}

    client = Client()
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "_anchor_cache_by_id", lambda: {})
    server._ANCHOR_AP_ID_RESERVED_CORES.clear()
    server._ANCHOR_AP_ID_ALLOCATED_CORES.clear()

    result = asyncio.run(
        server._anchor_point_capture_impl(
            "P", content="Body", name="Point", ap_id="[ap:ABC123]"
        )
    )
    assert client.payloads[0]["nodes"][0]["name"] == "[ap:ABC123] Point"
    assert result["ap_id"] == "[ap:ABC123]"
    assert result["uuid"] == "UUID-1"

    preserved = asyncio.run(
        server._anchor_point_capture_impl(
            "P", nodes=[{"name": "[ap:KEEP01] Existing"}], ap_id=None
        )
    )
    assert preserved["success"] is True
    assert client.payloads[-1]["nodes"][0]["name"] == "[ap:KEEP01] Existing"

    with pytest.raises(server._AnchorOperationError, match="already carries"):
        asyncio.run(
            server._anchor_point_capture_impl(
                "P", nodes=[{"name": "[ap:KEEP01] Existing"}], ap_id="auto"
            )
        )
    with pytest.raises(server._AnchorOperationError, match="single-root"):
        asyncio.run(
            server._anchor_point_capture_impl(
                "P", nodes=[{"name": "A"}, {"name": "B"}], ap_id="auto"
            )
        )


def _async_value(value):
    async def _value(*_args, **_kwargs):
        return value

    return _value()


# ---------------------------------------------------------------------------
# Compact inventory views (output-side progressive disclosure): the
# consolidated anchor.dock.list / anchor.dock.peek default to detail='compact'
# (one raw-text inventory string), with detail='full' as the escape hatch.
# ---------------------------------------------------------------------------


def test_dock_list_and_peek_default_to_compact_strings(monkeypatch):
    calls = []

    async def list_impl(include_local=False, target_tab=None, **kwargs):
        calls.append(("list", include_local, target_tab, kwargs))
        return "DOCK INVENTORY"

    async def peek_impl(handle, tab=None, **kwargs):
        calls.append(("peek", handle, tab, kwargs))
        return "PEEK INVENTORY"

    monkeypatch.setattr(server, "_anchor_dock_list_impl", list_impl)
    monkeypatch.setattr(server, "_anchor_dock_peek_impl", peek_impl)
    registry = server.build_anchor_registry()

    assert asyncio.run(dispatch_operation(registry, "dock.list", {})) == "DOCK INVENTORY"
    assert calls[0][3]["detail"] == "compact"
    assert asyncio.run(
        dispatch_operation(registry, "dock.peek", {"dock_handle": "D"})
    ) == "PEEK INVENTORY"
    assert calls[1][3]["detail"] == "compact"
    assert calls[1][3]["show_ids"] is False
    assert calls[1][3]["show_counts"] is True
    assert calls[1][3]["show_description"] is True


def test_dock_list_full_escape_hatch_and_compact_only_rejections(monkeypatch):
    async def list_impl(include_local=False, target_tab=None, **kwargs):
        return {"success": True, "docks": [], "dock_count": 0}

    monkeypatch.setattr(server, "_anchor_dock_list_impl", list_impl)
    registry = server.build_anchor_registry()

    full = asyncio.run(dispatch_operation(registry, "dock.list", {"detail": "full"}))
    assert full == {"success": True, "docks": [], "dock_count": 0}
    rejected = asyncio.run(
        dispatch_operation(registry, "dock.list", {"detail": "full", "max_chars": 40})
    )
    assert rejected["error"]["code"] == "invalid_parameter_combination"
    bad_enum = asyncio.run(
        dispatch_operation(registry, "dock.list", {"detail": "Compact"})
    )
    assert bad_enum["error"]["code"] == "invalid_enum_value"
    bad_budget = asyncio.run(
        dispatch_operation(registry, "dock.list", {"max_chars": True})
    )
    assert bad_budget["error"]["code"] == "invalid_parameter_type"


def test_dock_peek_compact_only_knobs_rejected_in_full_mode(monkeypatch):
    async def peek_impl(handle, tab=None, **kwargs):
        return {"success": True, "rows": []}

    monkeypatch.setattr(server, "_anchor_dock_peek_impl", peek_impl)
    registry = server.build_anchor_registry()

    for knob in ("show_ids", "show_counts", "show_description", "max_chars"):
        value = 40 if knob == "max_chars" else True
        rejected = asyncio.run(
            dispatch_operation(
                registry, "dock.peek", {"dock_handle": "D", "detail": "full", knob: value}
            )
        )
        assert rejected["error"]["code"] == "invalid_parameter_combination", knob


def test_compact_inventory_describe_documents_both_shapes():
    registry = server.ANCHOR_REGISTRY
    list_doc = registry.describe_full("dock.list")
    assert "detail" in list_doc["params_schema"]["properties"]
    returns_text = list_doc["returns"]["description"]
    assert "compact" in returns_text and "full" in returns_text
    peek_doc = registry.describe_full("dock.peek")
    assert "show_ids" in peek_doc["params_schema"]["properties"]
    sg_doc = server.SLICE_GROUP_REGISTRY.describe_full("list")
    assert "show_tags" in sg_doc["params_schema"]["properties"]


def test_legacy_list_anchors_defaults_compact_with_full_escape(monkeypatch):
    # Dan, 2026: compact is the default at EVERY surface -- the legacy
    # list_anchors delegate included (single-user system; byte-compat guard
    # waived). detail='full' remains the byte-identical structured escape.
    async def impl(include_local=False, target_tab=None, **kwargs):
        return "ANCHOR INVENTORY TEXT"

    async def internal(**kwargs):
        return {"success": True, "sessions": []}

    monkeypatch.setattr(server, "_anchor_dock_list_impl", impl)
    monkeypatch.setattr(server, "_list_anchors_internal", internal)

    assert asyncio.run(server.list_anchors()) == "ANCHOR INVENTORY TEXT"
    full = asyncio.run(server.list_anchors(detail="full"))
    assert full == {"success": True, "sessions": []}
    with pytest.raises(ValueError, match="compact-mode knob"):
        asyncio.run(server.list_anchors(detail="full", max_chars=10))


def test_dock_render_exposes_ancestor_notes_flag_defaulting_false(monkeypatch):
    """U6 (ap:E03BN3): dock.render is one of the THREE surfaces exposing the
    ancestor_notes teaching flag, and the registry handler defaults it False
    (the chronological ancestor-note suppression rule ships by default)."""
    registry = server.build_anchor_registry()
    spec = registry.resolve("dock.render")
    props = spec.params_schema["properties"]
    assert "ancestor_notes" in props
    assert props["ancestor_notes"]["type"] == "boolean"
    assert (
        "Ancestor notes are suppressed on chronological renders"
        in props["ancestor_notes"]["description"]
    )
    assert "suppress ancestor notes" in spec.summary
    validate_params(spec, {"dock_handle": "D", "ancestor_notes": True})

    seen = []

    async def render_impl(handle, **kwargs):
        seen.append((handle, kwargs))
        return {"success": True, "preview_markdown": "# Dock"}

    monkeypatch.setattr(server, "_anchor_dock_render_impl", render_impl)
    asyncio.run(dispatch_operation(registry, "dock.render", {"dock_handle": "D"}))
    assert seen[-1][1]["ancestor_notes"] is False
    asyncio.run(dispatch_operation(
        registry, "dock.render", {"dock_handle": "D", "ancestor_notes": True},
    ))
    assert seen[-1][1]["ancestor_notes"] is True


def test_dock_render_impl_threads_ancestor_notes_to_the_raw_root_preset(monkeypatch):
    """U6: the flag rides the bounded raw-root preset (and its push twin), so
    a chronological dock.render suppresses ancestor notes unless opted out."""
    config = (["wfy-ns"], [], None, {"note_session_local_postfixes": ["local"]})
    monkeypatch.setattr(server, "_resolve_note_session_config", lambda _tab: _async_value(config))
    monkeypatch.setattr(server, "_resolve_generic_node_handle", lambda value, **_kw: _async_value("D"))
    monkeypatch.setattr(server, "_anchor_cache_by_id", lambda: {"D": {"id": "D", "name": "Dock #wfy-ns"}})
    seen = []

    async def raw(**kwargs):
        seen.append(kwargs)
        return {"success": True, "preview_markdown": "# Dock"}

    monkeypatch.setattr(server, "_glimpse_root_uuid_from_cache", raw)
    asyncio.run(server._anchor_dock_render_impl("D", sort_by="created"))
    assert seen[-1]["ancestor_notes"] is False
    asyncio.run(server._anchor_dock_render_impl("D", sort_by="created", ancestor_notes=True))
    assert seen[-1]["ancestor_notes"] is True
