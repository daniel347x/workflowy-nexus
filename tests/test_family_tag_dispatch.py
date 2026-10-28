from __future__ import annotations

import asyncio

from workflowy_mcp import server
from workflowy_mcp.mcp_api_registry import dispatch_operation, validate_params


def test_tag_add_remove_rename_and_batch_route_with_standardized_acks(monkeypatch):
    calls = []

    async def add(handle, tags):
        calls.append(("add", handle, tags))
        return {
            "success": True,
            "node_id": handle,
            "tags_added": ["topic-a"],
            "tags_already_present": [],
            "no_op": False,
        }

    async def remove(handle, tags):
        calls.append(("remove", handle, tags))
        return {
            "success": True,
            "node_id": handle,
            "tags_removed": [],
            "tags_not_present": ["topic-a"],
            "no_op": True,
        }

    async def rename(handle, old_tag, new_tag):
        calls.append(("rename", handle, old_tag, new_tag))
        return {
            "success": True,
            "node_id": handle,
            "renamed": True,
            "no_op": False,
        }

    async def batch(operations):
        calls.append(("batch", operations))
        return {
            "success": False,
            "partial_success": True,
            "nodes_changed": ["N"],
            "results": [
                {"handle": "N", "status": "committed"},
                {"handle": "M", "status": "error"},
            ],
            "summary": {"committed": 1, "commit_failures": 1},
        }

    monkeypatch.setattr(server, "_tag_add_impl", add)
    monkeypatch.setattr(server, "_tag_remove_impl", remove)
    monkeypatch.setattr(server, "_tag_rename_impl", rename)
    monkeypatch.setattr(server, "_tag_batch_impl", batch)
    registry = server.build_tag_registry()

    added = asyncio.run(
        dispatch_operation(
            registry, "add", {"node_id": "N", "tag": "topic-a"}
        )
    )
    assert added["operation"] == "tag.add"
    assert added["changed"] is True
    assert added["detail"]["tags_added"] == ["topic-a"]

    removed = asyncio.run(
        dispatch_operation(
            registry, "remove", {"node_handle": "N", "tags": ["topic-a"]}
        )
    )
    assert removed["operation"] == "tag.remove"
    assert removed["changed"] is False
    assert removed["detail"]["no_op"] is True

    renamed = asyncio.run(
        dispatch_operation(
            registry,
            "rename",
            {"node_id": "N", "old_tag": "topic-a", "new_tag": "topic-b"},
        )
    )
    assert renamed["operation"] == "tag.rename"
    assert renamed["changed"] is True

    operations = [
        {"handle": "N", "operation": "add", "tags": ["topic-a"]},
        {"handle": "M", "operation": "remove", "tags": ["topic-b"]},
    ]
    batched = asyncio.run(
        dispatch_operation(registry, "batch", {"operations": operations})
    )
    assert batched["success"] is False
    assert batched["operation"] == "tag.batch"
    assert batched["changed"] is True
    assert batched["detail"]["results"] == [
        {"handle": "N", "status": "committed"},
        {"handle": "M", "status": "error"},
    ]

    assert calls == [
        ("add", "N", "topic-a"),
        ("remove", "N", ["topic-a"]),
        ("rename", "N", "topic-a", "topic-b"),
        ("batch", operations),
    ]


def test_tag_alias_conflict_names_both_fields(monkeypatch):
    async def add(_handle, _tags):
        raise AssertionError("conflicting aliases must fail before the handler")

    monkeypatch.setattr(server, "_tag_add_impl", add)
    registry = server.build_tag_registry()
    result = asyncio.run(
        dispatch_operation(
            registry,
            "add",
            {
                "node_handle": "N",
                "node_id": "legacy-N",
                "tags": ["x"],
            },
        )
    )
    assert result["error"]["code"] == "forbidden_parameter"
    assert "node_id" in result["error"]["message"]
    assert "node_handle" in result["error"]["message"]


def test_real_tag_batch_reuses_q4_normalizer_and_ledger(monkeypatch):
    captured = []

    async def mutate(*, spec):
        captured.append(spec)
        return {
            "success": True,
            "partial_success": False,
            "mode": "mutate_node_tags",
            "nodes_changed": ["N"],
            "results": [{"handle": "N", "status": "committed"}],
            "summary": {"committed": 1},
        }

    monkeypatch.setattr(server, "_lightning_rod_mutate_tags", mutate)
    operations = [
        {"handle": "N", "operation": "rename", "old_tag": "a", "new_tag": "b"}
    ]
    result = asyncio.run(server._tag_batch_impl(operations))
    assert result["results"] == [{"handle": "N", "status": "committed"}]
    assert len(captured) == 1
    assert captured[0].mode == "mutate_node_tags"
    assert captured[0].operations[0].operation == "rename"
    assert captured[0].operations[0].old_tag == "a"
    assert captured[0].operations[0].new_tag == "b"


def test_tag_registry_includes_s07_purge_and_examples_validate():
    registry = server.TAG_REGISTRY
    assert [spec.verb for spec in registry.operations()] == [
        "add",
        "remove",
        "rename",
        "batch",
        "purge.preview",
        "purge.apply",
    ]
    assert registry.resolve("purge.preview").side_effects == ("pure_read",)
    assert registry.resolve("purge.apply").side_effects == (
        "workflowy_mutation", "local_state_mutation"
    )
    index_verbs = [row["verb"] for row in registry.describe_index()["operations"]]
    assert "purge.preview" in index_verbs
    assert "purge.apply" in index_verbs

    for spec in registry.operations():
        full = registry.describe_full(spec.verb)
        assert not set(spec.param_aliases).intersection(
            full["params_schema"]["properties"]
        )
        for example in full["examples"]:
            validate_params(spec, example["call"].get("params") or {})


def test_tag_fastmcp_family_is_registered_without_legacy_surface():
    tools = asyncio.run(server.mcp.list_tools())
    names = {tool.name for tool in tools}
    assert "tag" in names
    assert {
        "workflowy_add_tag",
        "workflowy_remove_tag",
        "workflowy_rename_tag",
    }.isdisjoint(names)
