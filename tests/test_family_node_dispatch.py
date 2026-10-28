from __future__ import annotations

import asyncio

from workflowy_mcp import server
from workflowy_mcp.mcp_api_registry import dispatch_operation, validate_params


def test_each_node_verb_routes_with_exact_parameter_unpack(monkeypatch):
    calls = []

    async def read(handle, as_raw, *, output_file=None, offset=0, limit=None, with_line_numbers=False, view=None):
        calls.append(("read", handle, as_raw))
        assert output_file is None
        return "RAW DOCUMENT"

    async def edit(handle, **kwargs):
        calls.append(("edit", handle, kwargs))
        return "RAW DIFF"

    async def update_emoji(handle, emoji):
        calls.append(("update_emoji", handle, emoji))
        return {"success": True, "no_op": False, "emoji": emoji}

    async def prepend_emoji(handle, emoji):
        calls.append(("prepend_emoji", handle, emoji))
        return {"success": True, "no_op": False, "emoji": emoji, "added_emoji": emoji}

    async def append_emoji(handle, emoji):
        calls.append(("append_emoji", handle, emoji))
        return {"success": True, "no_op": False, "emoji": emoji, "added_emoji": emoji}

    async def append_note(handle, text, separator="\n"):
        calls.append(("append_note", handle, text, separator))
        return {"success": True, "position": "append", "note_chars": 3}

    async def prepend_note(handle, text, separator="\n"):
        calls.append(("prepend_note", handle, text, separator))
        return {"success": True, "position": "prepend", "note_chars": 3}

    async def etch(handle, nodes=None, nodes_file=None, execution="sync"):
        calls.append(("etch", handle, nodes, nodes_file, execution))
        return {"route": "etch"}

    async def move(handle, parent, position):
        calls.append(("move", handle, parent, position))
        return True

    async def complete(handle):
        calls.append(("complete", handle))
        return {"id": handle}

    async def uncomplete(handle):
        calls.append(("uncomplete", handle))
        return {"id": handle}

    async def delete(handle):
        calls.append(("delete", handle))
        return {"success": True, "deleted_id": handle}

    async def export(handle, *, output_file=None, archive_name=None, archive_description=None):
        calls.append(("export", handle, output_file, archive_name, archive_description))
        return {"nodes": []}

    async def scry(handle, depth, size_limit, output_file, allow_anchor_dock=False):
        calls.append(("scry", handle, depth, size_limit, output_file, allow_anchor_dock))
        return {"root": handle}

    async def ancestors(handle, **kwargs):
        calls.append(("ancestors", handle, kwargs))
        return "ANCESTOR DOCUMENT"

    async def resolve(handle):
        calls.append(("resolve", handle))
        return {"success": True, "match_count": 1}

    monkeypatch.setattr(server, "_node_read_impl", read)
    monkeypatch.setattr(server, "_node_edit_impl", edit)
    monkeypatch.setattr(server, "_node_update_emoji_impl", update_emoji)
    monkeypatch.setattr(server, "_node_prepend_emoji_impl", prepend_emoji)
    monkeypatch.setattr(server, "_node_append_emoji_impl", append_emoji)
    monkeypatch.setattr(server, "_node_append_note_impl", append_note)
    monkeypatch.setattr(server, "_node_prepend_note_impl", prepend_note)
    monkeypatch.setattr(server, "_node_etch_impl", etch)
    monkeypatch.setattr(server, "_node_move_impl", move)
    monkeypatch.setattr(server, "_node_complete_impl", complete)
    monkeypatch.setattr(server, "_node_uncomplete_impl", uncomplete)
    monkeypatch.setattr(server, "_node_delete_impl", delete)
    monkeypatch.setattr(server, "_node_export_impl", export)
    monkeypatch.setattr(server, "_node_scry_impl", scry)
    monkeypatch.setattr(server, "_node_ancestors_impl", ancestors)
    monkeypatch.setattr(server, "_node_resolve_impl", resolve)
    registry = server.build_node_registry()

    assert asyncio.run(
        dispatch_operation(registry, "read", {"node_id": "N", "as_raw_node": False})
    ) == "RAW DOCUMENT"
    assert asyncio.run(
        dispatch_operation(
            registry,
            "edit.node",
            {"node_handle": "N", "note_search": "old", "note_replace": "new"},
        )
    ) == "RAW DIFF"
    assert asyncio.run(
        dispatch_operation(
            registry,
            "update_emoji",
            {"node_id": "N", "emoji": "✅"},
        )
    )["operation"] == "node.update_emoji"
    assert asyncio.run(
        dispatch_operation(
            registry,
            "prepend_emoji",
            {"node_id": "N", "emoji": "✅"},
        )
    )["operation"] == "node.prepend_emoji"
    assert asyncio.run(
        dispatch_operation(
            registry,
            "append_emoji",
            {"node_id": "N", "emoji": "✅"},
        )
    )["operation"] == "node.append_emoji"
    assert asyncio.run(
        dispatch_operation(
            registry,
            "append_note",
            {"node_handle": "N", "text": "part 2"},
        )
    )["operation"] == "node.append_note"
    assert asyncio.run(
        dispatch_operation(
            registry,
            "prepend_note",
            {"node_id": "N", "text": "part 0", "separator": ""},
        )
    )["operation"] == "node.prepend_note"
    assert asyncio.run(
        dispatch_operation(
            registry,
            "etch",
            {"parent_uuid": "P", "nodes": [{"name": "x"}], "execution": "async"},
        )
    ) == {"route": "etch"}
    assert asyncio.run(
        dispatch_operation(
            registry,
            "move",
            {"node_id": "N", "parent_id": "inbox", "position": "bottom"},
        )
    )["changed"] is True
    assert asyncio.run(dispatch_operation(registry, "complete", {"node_id": "N"}))[
        "operation"
    ] == "node.complete"
    assert asyncio.run(
        dispatch_operation(registry, "uncomplete", {"node_handle": "N"})
    )["operation"] == "node.uncomplete"
    assert asyncio.run(dispatch_operation(registry, "delete", {"node_handle": "N"}))[
        "detail"
    ]["deleted_id"] == "N"
    assert asyncio.run(dispatch_operation(registry, "export", {})) == {"nodes": []}
    assert asyncio.run(
        dispatch_operation(
            registry,
            "export",
            {
                "node_handle": "N",
                "output_file": "/x/y.json",
                "archive_name": "A",
                "archive_description": "D",
            },
        )
    ) == {"nodes": []}
    assert asyncio.run(
        dispatch_operation(
            registry,
            "scry",
            {
                "root_uuid": "R",
                "depth": 2,
                "size_limit": 50,
                "output_file": "out.json",
            },
        )
    ) == {"root": "R"}
    assert asyncio.run(
        dispatch_operation(
            registry,
            "ancestors",
            {"node_id": "N", "uuids": "every-level", "detail": "full"},
        )
    ) == "ANCESTOR DOCUMENT"
    assert asyncio.run(dispatch_operation(registry, "resolve", {"node_id": "N"}))[
        "match_count"
    ] == 1

    assert calls == [
        ("read", "N", False),
        (
            "edit",
            "N",
            {
                "name": None,
                "note": None,
                "note_search": "old",
                "note_replace": "new",
                "note_edits": None,
                "layout_mode": None,
                "replace_all": False,
                "diff": None,
                "return_diff": None,
                "replace_emoji": False,
            },
        ),
        ("update_emoji", "N", "✅"),
        ("prepend_emoji", "N", "✅"),
        ("append_emoji", "N", "✅"),
        ("append_note", "N", "part 2", "\n"),
        ("prepend_note", "N", "part 0", ""),
        ("etch", "P", [{"name": "x"}], None, "async"),
        ("move", "N", "inbox", "bottom"),
        ("complete", "N"),
        ("uncomplete", "N"),
        ("delete", "N"),
        ("export", None, None, None, None),
        ("export", "N", "/x/y.json", "A", "D"),
        ("scry", "R", 2, 50, "out.json", False),
        ("ancestors", "N", {
            "uuids": "every-level",
            "markers": True,
            "metrics": True,
            "detail": "full",
            "target_tab": None,
        }),
        ("resolve", "N"),
    ]


def test_node_scry_aliases_map_only_to_root_handle(monkeypatch):
    calls = []

    async def scry(handle, depth, size_limit, output_file, allow_anchor_dock=False):
        calls.append((handle, depth, size_limit, output_file, allow_anchor_dock))
        return {"ok": True}

    monkeypatch.setattr(server, "_node_scry_impl", scry)
    registry = server.build_node_registry()

    for alias in ("node_id", "root_uuid"):
        result = asyncio.run(dispatch_operation(registry, "scry", {alias: "ROOT"}))
        assert result == {"ok": True}
    assert calls == [("ROOT", None, 1000, None, False), ("ROOT", None, 1000, None, False)]

    spec = registry.resolve("scry")
    assert spec is not None
    assert spec.param_aliases == {"node_id": "root_handle", "root_uuid": "root_handle"}
    assert "node_handle" not in spec.params_schema["properties"]


def test_node_raw_returns_and_standardized_edit_ack(monkeypatch):
    async def read(_handle, _as_raw, *, output_file=None, offset=0, limit=None, with_line_numbers=False, view=None):
        assert output_file is None
        return "RAW NODE"

    async def edit_raw(_handle, **_kwargs):
        return '{"success": true}\n\n--- before\n+++ after'

    monkeypatch.setattr(server, "_node_read_impl", read)
    monkeypatch.setattr(server, "_node_edit_impl", edit_raw)
    registry = server.build_node_registry()
    assert asyncio.run(
        dispatch_operation(registry, "read", {"node_handle": "N"})
    ) == "RAW NODE"
    assert asyncio.run(
        dispatch_operation(
            registry, "edit", {"node_handle": "N", "note_search": "x"}
        )
    ).startswith('{"success": true}')

    async def edit_structured(_handle, **_kwargs):
        return {"id": "N", "note": None}

    monkeypatch.setattr(server, "_node_edit_impl", edit_structured)
    registry = server.build_node_registry()
    result = asyncio.run(
        dispatch_operation(registry, "edit", {"node_handle": "N", "name": "new"})
    )
    assert result == {
        "success": True,
        "operation": "node.edit",
        "changed": True,
        "warnings": [],
        "detail": {"id": "N", "note": None},
    }


def test_node_etch_execution_validation_and_async_job_record(monkeypatch):
    bad = asyncio.run(
        dispatch_operation(
            server.NODE_REGISTRY,
            "etch",
            {"parent_handle": "P", "nodes": [], "execution": "later"},
        )
    )
    assert bad["error"]["code"] == "invalid_enum_value"

    async def async_impl(parent, nodes=None, nodes_file=None):
        assert (parent, nodes, nodes_file) == ("P", [{"name": "x"}], None)
        return {"success": True, "job_id": "etch-9", "status": "started", "kind": "etch"}

    monkeypatch.setattr(server, "_node_etch_async_impl", async_impl)
    result = asyncio.run(
        server._node_etch_impl("P", [{"name": "x"}], execution="async")
    )
    assert result == {
        "success": True,
        "operation": "node.etch",
        "job": {"id": "etch-9", "state": "started", "kind": "etch"},
    }


def test_node_dispatch_leaves_cross_field_exclusivity_to_impl(monkeypatch):
    received = []

    async def edit(_handle, **kwargs):
        received.append(kwargs)
        raise ValueError("impl owns note exclusivity")

    monkeypatch.setattr(server, "_node_edit_impl", edit)
    registry = server.build_node_registry()
    result = asyncio.run(
        dispatch_operation(
            registry,
            "edit",
            {"node_handle": "N", "note": "whole", "note_search": "part"},
        )
    )
    assert received and received[0]["note"] == "whole"
    assert received[0]["note_search"] == "part"
    assert result["error"]["code"] == "operation_failed"
    assert "impl owns note exclusivity" in result["error"]["message"]


def test_node_update_emoji_typed_validation_error_reaches_family_envelope(monkeypatch):
    async def invalid(_handle, _emoji):
        raise server.ProtectedNameViolation(
            "invalid_emoji_value", "emoji-only please", {"supplied_emoji": "x"}
        )

    monkeypatch.setattr(server, "_node_update_emoji_impl", invalid)
    registry = server.build_node_registry()
    result = asyncio.run(
        dispatch_operation(
            registry,
            "update_emoji",
            {"node_handle": "N", "emoji": "x"},
        )
    )
    assert result["success"] is False
    assert result["operation"] == "node.update_emoji"
    assert result["error"]["code"] == "invalid_emoji_value"
    assert result["error"]["extra"] == {"supplied_emoji": "x"}


def test_node_prepend_emoji_typed_validation_error_reaches_family_envelope(monkeypatch):
    async def invalid(_handle, _emoji):
        raise server.ProtectedNameViolation(
            "invalid_emoji_value", "emoji-only please", {"supplied_emoji": ""}
        )

    monkeypatch.setattr(server, "_node_prepend_emoji_impl", invalid)
    registry = server.build_node_registry()
    result = asyncio.run(
        dispatch_operation(
            registry,
            "prepend_emoji",
            {"node_handle": "N", "emoji": ""},
        )
    )
    assert result["success"] is False
    assert result["operation"] == "node.prepend_emoji"
    assert result["error"]["code"] == "invalid_emoji_value"
    assert result["error"]["extra"] == {"supplied_emoji": ""}


def test_node_append_emoji_typed_validation_error_reaches_family_envelope(monkeypatch):
    async def invalid(_handle, _emoji):
        raise server.ProtectedNameViolation(
            "invalid_emoji_value", "emoji-only please", {"supplied_emoji": "x"}
        )

    monkeypatch.setattr(server, "_node_append_emoji_impl", invalid)
    registry = server.build_node_registry()
    result = asyncio.run(
        dispatch_operation(
            registry,
            "append_emoji",
            {"node_handle": "N", "emoji": "x"},
        )
    )
    assert result["success"] is False
    assert result["operation"] == "node.append_emoji"
    assert result["error"]["code"] == "invalid_emoji_value"
    assert result["error"]["extra"] == {"supplied_emoji": "x"}


def test_node_registry_metadata_examples_and_fastmcp_registration():
    registry = server.NODE_REGISTRY
    assert [spec.verb for spec in registry.operations()] == [
        "read",
        "search",
        "edit",
        "update_emoji",
        "prepend_emoji",
        "append_emoji",
        "append_note",
        "prepend_note",
        "etch",
        "move",
        "complete",
        "uncomplete",
        "delete",
        "export",
        "scry",
        "ancestors",
        "resolve",
    ]
    assert registry.resolve("scry").side_effects == ("pure_read", "artifact_write")
    for spec in registry.operations():
        full = registry.describe_full(spec.verb)
        legacy_names = set(spec.param_aliases)
        assert not legacy_names.intersection(full["params_schema"]["properties"])
        for example in full["examples"]:
            validate_params(spec, example["call"].get("params") or {})

    tools = asyncio.run(server.mcp.list_tools())
    names = {tool.name for tool in tools}
    assert "node" in names
    assert {
        "workflowy_read_node",
        "workflowy_edit_node",
        "workflowy_etch",
        "workflowy_etch_async",
        "workflowy_move_node",
        "workflowy_complete_node",
        "workflowy_uncomplete_node",
        "workflowy_delete_node",
        "workflowy_export_node",
        "workflowy_scry",
    }.isdisjoint(names)


def test_node_move_reserved_inbox_bypasses_handle_resolution(monkeypatch, tmp_path):
    resolver_calls = []
    move_calls = []
    resolved_node = "11111111-1111-4111-8111-111111111111"

    async def resolve(value, *, label, required=True, role=None):
        resolver_calls.append((value, label, required))
        return resolved_node

    class Client:
        async def move_node(self, node_id, parent_id, position):
            move_calls.append((node_id, parent_id, position))
            return True

    monkeypatch.setattr(server, "_resolve_mutator_node_id", resolve)
    from coherence_harness import make_boundary_capable
    from n05_region_harness import isolate_claim_scope
    from workflowy_mcp.coherence.operation_cache import cache_operation_boundary
    client = make_boundary_capable(Client(), nodes=[{"id": resolved_node, "parent_id": None}])
    isolate_claim_scope(client, tmp_path)
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "_rate_limiter", None)

    async def move():
        async with cache_operation_boundary(client, "inbox-fixture"):
            return await server._node_move_impl("N", "INBOX", "bottom")

    assert asyncio.run(move()) is True
    assert resolver_calls == [
        ("N", "workflowy_move_node (node_id)", True),
    ]
    assert move_calls == [(resolved_node, "inbox", "bottom")]
