from __future__ import annotations

import asyncio

from workflowy_mcp import server
from workflowy_mcp.mcp_api_registry import dispatch_operation


def test_slice_group_registry_routes_crud_and_wraps_mutations(monkeypatch):
    calls = []

    async def list_impl(tab=None, **_kwargs):
        calls.append(("list", tab))
        return {"success": True, "slice_groups": []}

    async def get_impl(handle, tab=None):
        calls.append(("get", handle, tab))
        return {"success": True, "slice_group": {"id": handle}}

    async def create_impl(name, tags, root, description, sort_by, sort_dir, active, tab):
        calls.append(("create", name, tags, root, description, sort_by, sort_dir, active, tab))
        return {"success": True, "slice_group": {"id": "new"}}

    async def update_impl(handle, name, description, tags, root, sort_by, sort_dir, active, tab):
        calls.append(("update", handle, name, description, tags, root, sort_by, sort_dir, active, tab))
        return {"success": True, "slice_group": {"id": handle, "name": name}}

    monkeypatch.setattr(server, "_slice_group_list_impl", list_impl)
    monkeypatch.setattr(server, "_slice_group_get_impl", get_impl)
    monkeypatch.setattr(server, "_slice_group_create_impl", create_impl)
    monkeypatch.setattr(server, "_slice_group_update_impl", update_impl)
    registry = server.build_slice_group_registry()

    assert asyncio.run(dispatch_operation(registry, "list", {"target_tab": "t"}))["success"]
    assert asyncio.run(dispatch_operation(registry, "get", {"name": "Architecture"}))["slice_group"]["id"] == "Architecture"
    created = asyncio.run(
        dispatch_operation(
            registry,
            "create",
            {"name": "N", "tags": ["x"], "root_uuid": "R", "make_active": True},
        )
    )
    assert created["operation"] == "slice_group.create" and created["changed"]
    updated = asyncio.run(
        dispatch_operation(
            registry,
            "update",
            {"group_id": "G", "new_name": "Renamed", "root_uuid": "R2"},
        )
    )
    assert updated["operation"] == "slice_group.update" and updated["changed"]
    assert calls == [
        ("list", "t"),
        ("get", "Architecture", None),
        ("create", "N", ["x"], "R", "", None, None, True, None),
        ("update", "G", "Renamed", None, None, "R2", None, None, False, None),
    ]


def test_alias_conflicts_and_update_name_semantic_split():
    registry = server.build_slice_group_registry()
    conflict = asyncio.run(
        dispatch_operation(
            registry,
            "get",
            {"group_handle": "G", "name": "Other"},
        )
    )
    assert conflict["error"]["code"] == "forbidden_parameter"

    update = registry.resolve("update")
    assert update.param_aliases == {
        "group_id": "group_handle",
        "new_name": "name",
        "root_uuid": "root_handle",
    }
    assert "name" in update.params_schema["properties"]
    assert "name" not in update.param_aliases


def test_s07_family_index_is_honest_and_effects_are_declared():
    registry = server.SLICE_GROUP_REGISTRY
    assert [spec.verb for spec in registry.operations()] == [
        "list",
        "get",
        "create",
        "update",
        "delete",
        "render",
        "sift",
        "merge.render",
        "merge.persist",
        "purge.preview",
        "purge.apply",
    ]
    assert registry.resolve("list").side_effects == ("pure_read",)
    assert registry.resolve("get").side_effects == ("pure_read",)
    assert registry.resolve("create").side_effects == ("config_mutation",)
    assert registry.resolve("update").side_effects == ("config_mutation",)
    assert registry.resolve("delete").side_effects == ("config_mutation",)
    assert registry.resolve("render").side_effects == ("render", "modal_push", "artifact_write")
    assert registry.resolve("sift").side_effects == ("render", "modal_push", "artifact_write")
    assert registry.resolve("merge.render").side_effects == ("render", "modal_push", "artifact_write")
    assert registry.resolve("merge.persist").side_effects == (
        "workflowy_mutation", "config_mutation", "local_state_mutation"
    )
    assert registry.resolve("purge.apply").side_effects == (
        "workflowy_mutation", "local_state_mutation"
    )
    index = asyncio.run(dispatch_operation(registry, "describe", {"detail": "index"}))
    names = {f"slice_group.{row['verb']}" for row in index["operations"]}
    assert names == {
        *(f"slice_group.{spec.verb}" for spec in registry.operations()),
        "slice_group.describe",
    }


def test_legacy_delegates_preserve_shifted_lookup_and_rename(monkeypatch):
    calls = []

    async def get_impl(handle, tab=None):
        calls.append(("get", handle, tab))
        return {"success": True}

    async def update_impl(*args):
        calls.append(("update",) + args)
        return {"success": True}

    monkeypatch.setattr(server, "_slice_group_get_impl", get_impl)
    monkeypatch.setattr(server, "_slice_group_update_impl", update_impl)
    assert asyncio.run(server.get_slice_group(group_id="G", name="ignored"))["success"]
    assert asyncio.run(
        server.update_slice_group(name="Old", new_name="New", root_uuid="R")
    )["success"]
    assert calls[0] == ("get", "G", None)
    assert calls[1][1:6] == ("Old", "New", None, None, "R")


def test_slice_group_fastmcp_tool_is_registered_without_legacy_tools():
    tools = asyncio.run(server.mcp.list_tools())
    by_name = {tool.name: tool for tool in tools}
    assert "slice_group" in by_name
    assert {
        "list_slice_groups",
        "get_slice_group",
        "create_slice_group",
        "update_slice_group",
    }.isdisjoint(by_name)
    assert [spec.verb for spec in server.SLICE_GROUP_REGISTRY.operations()] == [
        "list", "get", "create", "update", "delete", "render", "sift",
        "merge.render", "merge.persist", "purge.preview", "purge.apply"
    ]


# ---------------------------------------------------------------------------
# Compact inventory views (output-side progressive disclosure): slice_group.list
# defaults to detail='compact' (one raw-text nested outline), with detail='full'
# as the escape hatch; legacy list_slice_groups stays structured forever.
# ---------------------------------------------------------------------------


def test_slice_group_list_defaults_compact_and_full_escape_hatch(monkeypatch):
    seen = []

    async def list_impl(tab=None, **kwargs):
        seen.append(kwargs)
        return "INVENTORY TEXT"

    monkeypatch.setattr(server, "_slice_group_list_impl", list_impl)
    registry = server.build_slice_group_registry()

    assert asyncio.run(dispatch_operation(registry, "list", {})) == "INVENTORY TEXT"
    assert seen[0]["detail"] == "compact"
    assert seen[0]["show_tags"] is True

    async def full_impl(tab=None, **kwargs):
        return {"success": True, "slice_groups": []}

    monkeypatch.setattr(server, "_slice_group_list_impl", full_impl)
    result = asyncio.run(dispatch_operation(registry, "list", {"detail": "full"}))
    assert result == {"success": True, "slice_groups": []}

    rejected_tags = asyncio.run(
        dispatch_operation(registry, "list", {"detail": "full", "show_tags": False})
    )
    assert rejected_tags["error"]["code"] == "invalid_parameter_combination"
    rejected_budget = asyncio.run(
        dispatch_operation(registry, "list", {"detail": "full", "max_chars": 100})
    )
    assert rejected_budget["error"]["code"] == "invalid_parameter_combination"
    bad_enum = asyncio.run(dispatch_operation(registry, "list", {"detail": "FULL"}))
    assert bad_enum["error"]["code"] == "invalid_enum_value"


def test_legacy_list_slice_groups_defaults_compact_with_full_escape(monkeypatch):
    # Dan, 2026: compact is the default at EVERY surface -- the legacy
    # delegate included (single-user system; byte-compat guard waived).
    # detail='full' remains the byte-identical structured escape hatch.
    calls = []

    async def core(tab=None):
        calls.append(("core", tab))
        return {"success": True, "slice_groups": [{"id": "grp-x"}], "postfixes": {}}

    async def impl(tab=None, **kwargs):
        calls.append(("impl", tab, kwargs))
        return "COMPACT TEXT"

    monkeypatch.setattr(server, "_slice_group_list_core", core)
    monkeypatch.setattr(server, "_slice_group_list_impl", impl)

    assert asyncio.run(server.list_slice_groups("t")) == "COMPACT TEXT"
    assert calls[0] == ("impl", "t", {"detail": "compact", "max_chars": None})
    full = asyncio.run(server.list_slice_groups("t", detail="full"))
    assert isinstance(full, dict) and full["slice_groups"][0]["id"] == "grp-x"
    assert calls[1] == ("core", "t")


def test_all_three_lens_surfaces_expose_ancestor_notes(monkeypatch):
    """U6 (ap:E03BN3) + [ap:I506WY] (unit 5g): ALL THREE lens surfaces --
    render, sift, and merge.render -- expose the ancestor_notes flag. Since
    5g the flag on a lens render is ALSO the opt-out of the lens-scaffold
    suppression (the connective scaffolding above the input roots renders
    heading-only even in document order), so a flag-free sift/merge.render
    schema would ship an unrestorable suppression. lightning_rod keeps its
    schema flag-free (the U6 surface list minus the lens verbs). The recipe
    folds the param onto the lens render spec for all three."""
    registry = server.build_slice_group_registry()

    render_props = registry.resolve("render").params_schema["properties"]
    assert "ancestor_notes" in render_props
    assert render_props["ancestor_notes"]["type"] == "boolean"
    assert (
        "Ancestor notes are suppressed on chronological renders"
        in render_props["ancestor_notes"]["description"]
    )
    assert "suppress ancestor notes" in registry.resolve("render").summary
    assert "ancestor_notes" in registry.resolve("sift").params_schema["properties"]
    assert (
        "ancestor_notes"
        in registry.resolve("merge.render").params_schema["properties"]
    )
    # lightning_rod's own schema stays flag-free (not a lens surface).
    lightning_registry = server.build_lightning_rod_registry()
    for verb in ("render_tags", "render_search", "render_symbols"):
        props = lightning_registry.resolve(verb).params_schema["properties"]
        assert "ancestor_notes" not in props, verb

    captured = []

    async def fake_render_lens(render_spec, **kwargs):
        captured.append(render_spec)
        return {"success": True, "preview_markdown": "# Lens"}

    monkeypatch.setattr(server, "_lightning_rod_render_lens", fake_render_lens)
    lens = {"kind": "tags", "tags": ["x"]}

    asyncio.run(dispatch_operation(
        registry, "render", {"lens": lens, "sort_by": "created"},
    ))
    assert captured[-1].ancestor_notes is False

    asyncio.run(dispatch_operation(
        registry, "render",
        {"lens": lens, "sort_by": "created", "ancestor_notes": True},
    ))
    assert captured[-1].ancestor_notes is True

    # [ap:I506WY]: the override threads through sift (headings tier) and
    # merge.render onto the same spec field.
    asyncio.run(dispatch_operation(
        registry,
        "sift",
        {"lens": lens, "query": "x", "tier": "headings", "ancestor_notes": True},
    ))
    assert captured[-1].ancestor_notes is True
    asyncio.run(dispatch_operation(
        registry,
        "merge.render",
        {"lens": lens, "ancestor_notes": True},
    ))
    assert captured[-1].ancestor_notes is True
