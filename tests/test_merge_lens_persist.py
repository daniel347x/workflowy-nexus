from __future__ import annotations

import asyncio
from types import MappingProxyType

import pytest

from workflowy_mcp import server
from workflowy_mcp.lens_engine import LensEvalContext
from workflowy_mcp.lens_spec import LensSpecError
from workflowy_mcp.mcp_api_registry import dispatch_operation


def _cache_one_tree():
    return {
        "root": {"id": "root", "parent_id": None, "name": "Root", "priority": 0},
        "a": {"id": "a", "parent_id": "root", "name": "A", "priority": 0},
        "a1": {"id": "a1", "parent_id": "a", "name": "A1", "priority": 0},
        "b": {"id": "b", "parent_id": "root", "name": "B", "priority": 1},
        "b1": {"id": "b1", "parent_id": "b", "name": "B1", "priority": 0},
    }


def _snapshot(cache=None, *, groups=(), global_never=()):
    cache = cache or _cache_one_tree()
    context = LensEvalContext(
        cache_by_id=cache,
        resolve_group=lambda handle: next(
            group for group in groups if group["id"] == handle or group["name"] == handle
        ),
        resolve_root=lambda handle: handle if handle in cache else (_ for _ in ()).throw(ValueError(handle)),
        auto_expand_postfix="expand",
        suppress_postfix="suppress",
        deprecated_postfix="deprecated",
        global_never_stems=tuple(global_never),
        exclude_completed=True,
        anchor_boundary_predicate=None,
    )
    return server._LensStage0Snapshot(
        context=context,
        cache_by_id=cache,
        groups_cfg=tuple(MappingProxyType(dict(group)) for group in groups),
        active_group_id=None,
        postfixes=MappingProxyType(
            {"auto_expand": "expand", "suppress": "suppress", "deprecated": "deprecated"}
        ),
        global_never=tuple(global_never),
        raw_config=MappingProxyType({}),
    )


def _union_recipe():
    return {
        "kind": "merge",
        "operation": "union",
        "inputs": [
            {"kind": "subtree", "root_handle": "a", "descend": "all"},
            {"kind": "subtree", "root_handle": "b", "descend": "all"},
        ],
    }


def _difference_recipe():
    return {
        "kind": "merge",
        "operation": "difference",
        "inputs": [
            {"kind": "subtree", "root_handle": "root", "descend": "all"},
            {"kind": "subtree", "root_handle": "b", "descend": "all"},
        ],
    }


def test_merge_render_rejects_single_atom_before_transport():
    result = asyncio.run(
        dispatch_operation(
            server.SLICE_GROUP_REGISTRY,
            "merge.render",
            {"lens": {"kind": "subtree", "root_handle": "root"}},
        )
    )
    assert result["success"] is False
    assert result["error"]["code"] == "invalid_lens_shape"
    assert "slice_group.render" in result["error"]["message"]


@pytest.mark.parametrize(
    ("operation", "roots"),
    (("intersection", ("a", "b")), ("difference", ("a", "a"))),
)
def test_merge_persist_empty_set_is_pre_mutation(monkeypatch, operation, roots):
    recipe = {
        "kind": "merge",
        "operation": operation,
        "inputs": [
            {"kind": "subtree", "root_handle": root}
            for root in roots
        ],
    }
    monkeypatch.setattr(server, "_build_lens_stage0_snapshot", lambda *a, **k: asyncio.sleep(0, result=_snapshot()))
    called = False

    async def no_mutation(_ops):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(server, "_tag_batch_impl", no_mutation)
    with pytest.raises(server._S07OperationError) as raised:
        asyncio.run(server._merge_persist_lens_impl({"lens": recipe, "tag": "new-family"}))
    assert raised.value.code == "empty_selection_not_persistable"
    assert called is False


def test_global_never_preflight_is_leaf_inclusive_and_root_exempt():
    cache = _cache_one_tree()
    cache["a1"]["name"] = "A1 #wfy-suppress"
    affected = server._unpersistable_global_never_leaves(
        cache_by_id=cache,
        leaf_ids={"a1", "b1"},
        root_uuid="root",
        global_never=["wfy-suppress"],
    )
    assert affected == [{"uuid": "a1", "name": "A1 #wfy-suppress"}]
    cache["a1"]["name"] = "A1"
    cache["root"]["name"] = "Root #wfy-suppress"
    assert server._unpersistable_global_never_leaves(
        cache_by_id=cache,
        leaf_ids={"a1", "b1"},
        root_uuid="root",
        global_never=["wfy-suppress"],
    ) == []


def test_merge_persist_rejects_phantom_document_root_before_mutation(monkeypatch):
    cache = {
        "r1": {"id":"r1","parent_id":None,"name":"R1"},
        "a": {"id":"a","parent_id":"r1","name":"A"},
        "r2": {"id":"r2","parent_id":None,"name":"R2"},
        "b": {"id":"b","parent_id":"r2","name":"B"},
    }
    snapshot = _snapshot(cache)

    async def build_snapshot(*_args, **_kwargs):
        return snapshot

    called = False

    async def mutate(_ops):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(server, "_build_lens_stage0_snapshot", build_snapshot)
    monkeypatch.setattr(server, "_tag_batch_impl", mutate)
    recipe = {
        "kind":"merge",
        "operation":"union",
        "inputs":[
            {"kind":"subtree","root_handle":"r1"},
            {"kind":"subtree","root_handle":"r2"},
        ],
    }
    with pytest.raises(LensSpecError) as raised:
        asyncio.run(server._merge_persist_lens_impl({"lens":recipe,"tag":"combo"}))
    assert raised.value.code == "no_common_ancestor"
    assert called is False


def test_generated_group_identity_conflicts_and_exact_reuse():
    exact = {"id": "g1", "name": "Combined", "root_uuid": "root", "prefixes": ["combo"]}
    chosen, summary = server._generated_group_identity(
        [exact], group_name="Combined", root_uuid="root", tag="combo"
    )
    assert chosen is exact
    assert summary["exact_match_ids"] == ["g1"]
    with pytest.raises(server._S07OperationError) as raised:
        server._generated_group_identity(
            [exact, {"id": "g2", "name": "Combined", "root_uuid": "other", "prefixes": ["combo"]}],
            group_name="Combined",
            root_uuid="root",
            tag="combo",
        )
    assert raised.value.code == "generated_group_conflict"


def test_merge_persist_uses_materialized_tree_expected_set_and_t2_config(monkeypatch):
    snapshot = _snapshot()

    async def build_snapshot(*_args, **_kwargs):
        return snapshot

    monkeypatch.setattr(server, "_build_lens_stage0_snapshot", build_snapshot)
    monkeypatch.setattr(
        server,
        "compute_merge_tag_plan",
        lambda *a, **k: {"ok": True, "expand_nodes": [], "plain_nodes": ["a1", "b1"]},
    )
    collision_kwargs = {}

    def collisions(*_args, **kwargs):
        collision_kwargs.update(kwargs)
        return {"count": 0, "collisions": [], "truncated": 0, "congruent_count": 0}

    monkeypatch.setattr(server, "find_tag_family_collisions", collisions)
    expected = {"root", "a", "a1", "b", "b1"}
    seen_simulation = {}

    def simulate(_overlay, _starts, _tag, **_kwargs):
        seen_simulation["expected"] = set(expected)
        return set(expected)

    monkeypatch.setattr(server, "simulate_merge_projection", simulate)
    monkeypatch.setattr(server, "build_merge_overlay_cache", lambda cache, *a, **k: cache)

    async def mutate(ops):
        assert {row["handle"] for row in ops} == {"a1", "b1"}
        return {
            "success": True,
            "nodes_changed": ["a1", "b1"],
            "summary": {"committed": 2, "noop": 0, "preflight_failures": 0, "commit_failures": 0},
            "results": [],
        }

    monkeypatch.setattr(server, "_tag_batch_impl", mutate)
    monkeypatch.setattr(server, "_current_cache_by_id", lambda: snapshot.cache_by_id)
    verify_seen = {}

    def verify(_cache, _starts, _tag, expected_node_ids, **_kwargs):
        verify_seen["ids"] = set(expected_node_ids)
        return {"matched": True, "missing": [], "unexpected": []}

    monkeypatch.setattr(server, "verify_merge_projection", verify)

    async def read_t2(_target_tab):
        return [], None, {"auto_expand": "expand", "suppress": "suppress", "deprecated": "deprecated"}, [], {}

    monkeypatch.setattr(server, "_resolve_slice_group_config", read_t2)
    writes = []

    async def write(groups, active, target_tab):
        writes.append((groups, active, target_tab))
        return {"expand_groups": groups, "active_group": active}

    monkeypatch.setattr(server, "_write_slice_group_config", write)
    result = asyncio.run(
        server._merge_persist_lens_impl(
            {
                "lens": _union_recipe(),
                "tag": "combo",
                "group_name": "Combined",
                "make_active": True,
            }
        )
    )
    assert result["success"] is True
    assert result["expected_node_ids"] == sorted(expected)
    assert verify_seen["ids"] == expected
    assert collision_kwargs["skip_completed"] is False
    assert len(writes) == 1
    persisted_group = writes[0][0][0]
    assert persisted_group["name"] == "Combined"
    assert persisted_group["root_uuid"] == "root"
    assert persisted_group["prefixes"] == ["combo"]
    assert writes[0][1] == persisted_group["id"]


def test_merge_persist_registry_declares_async_effects_and_codes():
    spec = server.SLICE_GROUP_REGISTRY.resolve("merge.persist")
    assert spec.async_mode == "optional"
    assert spec.side_effects == (
        "workflowy_mutation",
        "config_mutation",
        "local_state_mutation",
    )
    assert {
        "empty_selection_not_persistable",
        "unpersistable_leaves",
        "generated_group_conflict",
        "merge_verification_failed",
    }.issubset(set(spec.domain_error_codes))


def test_merge_persist_async_returns_normalized_job_envelope(monkeypatch):
    captured = {}

    async def start(kind, payload, coro_factory):
        captured.update(kind=kind, payload=payload, coro_factory=coro_factory)
        return {"success": True, "job_id": "job-7", "status": "started", "kind": kind}

    monkeypatch.setattr(server, "_start_background_job", start)
    result = asyncio.run(
        dispatch_operation(
            server.SLICE_GROUP_REGISTRY,
            "merge.persist",
            {"lens": _union_recipe(), "tag": "combo", "execution": "async"},
        )
    )
    assert result == {
        "success": True,
        "operation": "slice_group.merge.persist",
        "job": {"id": "job-7", "state": "started", "kind": "slice-group-merge-persist"},
    }
    assert captured["kind"] == "slice-group-merge-persist"


def test_post_write_failure_ack_keeps_report_and_declared_error():
    report = {
        "success": False,
        "changed": True,
        "code": "merge_verification_failed",
        "error": "verification failed",
        "mutation": {"nodes_changed": ["a1"]},
    }
    ack = server._s07_mutation_ack(
        server.SLICE_GROUP_REGISTRY,
        "merge.persist",
        report,
        changed=True,
    )
    assert ack["success"] is False
    assert ack["changed"] is True
    assert ack["detail"] is report
    assert ack["error"]["code"] == "merge_verification_failed"


@pytest.mark.parametrize(
    "recipe_factory", (_union_recipe, _difference_recipe), ids=("union", "difference")
)
def test_merge_render_persist_overlay_equivalence_uses_materialized_tree(recipe_factory):
    snapshot = _snapshot()
    parsed = server._require_top_level_merge_lens(recipe_factory())
    evaluation = server.evaluate_lens(parsed, snapshot.context)
    materialized = server.build_lens_result_tree(
        evaluation.effective_leaf_ids, snapshot.context
    )
    expected = server._walk_materialized_tree_ids(materialized.tree)
    plan = server.compute_merge_tag_plan(
        dict(snapshot.cache_by_id),
        set(evaluation.effective_leaf_ids),
        materialized.root_uuid,
        target_tag="combo",
        expand_postfix_word="expand",
        global_never_expand_tags=snapshot.global_never,
        skip_completed=True,
    )
    assert plan["ok"] is True
    overlay = server.build_merge_overlay_cache(
        dict(snapshot.cache_by_id),
        plan["expand_nodes"],
        plan["plain_nodes"],
        "combo",
        "expand",
    )
    simulated = server.simulate_merge_projection(
        overlay,
        [materialized.root_uuid],
        "combo",
        auto_expand_postfix="expand",
        suppress_postfix="suppress",
        deprecated_postfix="deprecated",
        global_never_expand_tags=snapshot.global_never,
        exclude_completed=True,
        match_scope="name_and_note",
    )
    assert set(simulated) == expected


def test_merge_persist_resume_readds_residue_and_preserves_then_updates_description(
    monkeypatch,
):
    cache = _cache_one_tree()
    state = {"groups": [], "active": None}

    async def build_snapshot(*_args, **_kwargs):
        return _snapshot(cache, groups=tuple(state["groups"]))

    monkeypatch.setattr(server, "_build_lens_stage0_snapshot", build_snapshot)
    monkeypatch.setattr(
        server,
        "compute_merge_tag_plan",
        lambda *a, **k: {
            "ok": True,
            "expand_nodes": [],
            "plain_nodes": ["a1", "b1"],
        },
    )
    expected = {"root", "a", "a1", "b", "b1"}
    monkeypatch.setattr(
        server,
        "simulate_merge_projection",
        lambda *a, **k: set(expected),
    )
    monkeypatch.setattr(
        server,
        "build_merge_overlay_cache",
        lambda cache_by_id, *a, **k: cache_by_id,
    )
    mutation_calls = []

    async def mutate(operations):
        mutation_calls.append(operations)
        changed = []
        for operation in operations:
            node = cache[operation["handle"]]
            for tag in operation["tags"]:
                marker = f" #{tag}"
                if marker not in node["name"]:
                    node["name"] += marker
                    changed.append(operation["handle"])
        return {
            "success": True,
            "nodes_changed": sorted(set(changed)),
            "results": [],
            "summary": {
                "committed": len(set(changed)),
                "noop": len(operations) - len(set(changed)),
                "preflight_failures": 0,
                "commit_failures": 0,
            },
        }

    monkeypatch.setattr(server, "_tag_batch_impl", mutate)
    monkeypatch.setattr(server, "_current_cache_by_id", lambda: cache)
    monkeypatch.setattr(
        server,
        "verify_merge_projection",
        lambda *a, **k: {"matched": True, "missing": [], "unexpected": []},
    )

    async def read_t2(_target_tab):
        return (
            [dict(group) for group in state["groups"]],
            state["active"],
            {
                "auto_expand": "expand",
                "suppress": "suppress",
                "deprecated": "deprecated",
            },
            [],
            {},
        )

    async def write(groups, active, _target_tab):
        state["groups"] = [dict(group) for group in groups]
        state["active"] = active
        return {"expand_groups": state["groups"], "active_group": active}

    monkeypatch.setattr(server, "_resolve_slice_group_config", read_t2)
    monkeypatch.setattr(server, "_write_slice_group_config", write)

    first = asyncio.run(
        server._merge_persist_lens_impl(
            {
                "lens": _union_recipe(),
                "tag": "combo",
                "group_name": "Combined",
                "group_description": "First description",
            }
        )
    )
    second = asyncio.run(
        server._merge_persist_lens_impl(
            {
                "lens": _union_recipe(),
                "tag": "combo",
                "group_name": "Combined",
            }
        )
    )
    third = asyncio.run(
        server._merge_persist_lens_impl(
            {
                "lens": _union_recipe(),
                "tag": "combo",
                "group_name": "Combined",
                "group_description": "Updated description",
            }
        )
    )

    assert first["success"] is True
    assert second["success"] is True
    assert second["changed"] is False
    assert second["slice_group"]["description_preserved"] is True
    assert third["success"] is True
    assert third["changed"] is True
    assert state["groups"][0]["description"] == "Updated description"
    assert len(mutation_calls) == 3
    assert all(
        {row["handle"] for row in operations} == {"a1", "b1"}
        for operations in mutation_calls
    )
