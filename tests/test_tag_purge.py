from __future__ import annotations

import asyncio
from copy import deepcopy
from types import MappingProxyType

import pytest

from workflowy_mcp import server
from workflowy_mcp.lens_engine import LensEvalContext
from workflowy_mcp.mcp_api_registry import dispatch_operation
from workflowy_mcp import tag_purge_policy
from workflowy_mcp.tag_purge_policy import (
    find_purge_shared_use,
    normalize_base_tag_family,
    plan_tag_family_purge,
    purge_fingerprint,
    scan_tag_family_occurrences,
)


POSTFIXES = ("expand", "suppress", "deprecated")


def _cache():
    return {
        "root": {"id": "root", "parent_id": None, "name": "Root", "priority": 0},
        "a": {
            "id": "a",
            "parent_id": "root",
            "name": "A #fam #fam-team--deprecated",
            "note": "note #fam-note",
            "priority": 0,
        },
        "b": {
            "id": "b",
            "parent_id": "root",
            "name": "Inline#fam-inline words #other #fam-team--expand",
            "note": "",
            "completed": True,
            "priority": 1,
        },
        "c": {
            "id": "c",
            "parent_id": "root",
            "name": "Suppressed #wfy-suppress #fam--suppress",
            "note": "body #fam-body",
            "priority": 2,
        },
    }


def test_strict_base_family_normalizer():
    assert normalize_base_tag_family(" #fam ") == "fam"
    assert normalize_base_tag_family("nexus--architecture") == "nexus--architecture"
    assert normalize_base_tag_family("topic--custom", ("grow",)) == "topic--custom"
    for bad in ("", "#", "a b", "a#b", "fam--expand", "--expand", "fam--"):
        with pytest.raises(ValueError):
            normalize_base_tag_family(bad)


def test_scan_includes_completed_and_suppressed_and_classifies_regions():
    rows = scan_tag_family_occurrences(_cache(), ["root"], "fam", POSTFIXES)
    triples = {(row["uuid"], row["source_region"], row["tag"]) for row in rows}
    assert ("a", "name_trailing", "fam") in triples
    assert ("a", "name_trailing", "fam-team--deprecated") in triples
    assert ("a", "note", "fam-note") in triples
    assert ("b", "name_inline", "fam-inline") in triples
    assert ("b", "name_trailing", "fam-team--expand") in triples
    assert ("c", "name_trailing", "fam--suppress") in triples
    assert ("c", "note", "fam-body") in triples
    assert any(row["uuid"] == "b" and row["completed"] for row in rows)


def test_plan_batches_overlapping_families_once_per_node():
    plan = plan_tag_family_purge(_cache(), ["root"], ["fam", "fam-team"], POSTFIXES)
    assert [row["handle"] for row in plan["ops"]] == ["a", "b", "c"]
    assert plan["ops"][0]["tags"] == ["fam", "fam-team--deprecated"]
    assert plan["ops"][1]["tags"] == ["fam-team--expand"]
    assert plan["counts"]["nodes_with_mutable_tags"] == 3
    assert {(row["uuid"], row["source_region"]) for row in plan["report_only"]} >= {
        ("a", "note"),
        ("b", "name_inline"),
        ("c", "note"),
    }


def test_shared_use_is_bidirectional_and_excludes_only_selected_group():
    groups = [
        {"id": "g1", "name": "One", "prefixes": ["fam"]},
        {"id": "g2", "name": "Two", "prefixes": ["fam-team"]},
        {"id": "g3", "name": "Three", "prefixes": ["unrelated"]},
    ]
    rows = find_purge_shared_use(
        ["fam-team"], groups, excluded_group_ids=["g2"], live_postfixes=POSTFIXES
    )
    assert rows == [
        {"group_id": "g1", "group_name": "One", "prefix": "fam", "family": "fam-team"}
    ]


def test_fingerprint_is_deterministic_and_tracks_semantic_drift_only():
    plan = plan_tag_family_purge(_cache(), ["root"], ["fam"], POSTFIXES)
    shared = [{"group_id": "g1", "group_name": "Display", "prefix": "fam"}]
    first = purge_fingerprint({"start_ids": ["root"]}, ["fam"], plan, shared)
    cosmetically_changed = deepcopy(plan)
    for row in cosmetically_changed["mutable"] + cosmetically_changed["report_only"]:
        row["name"] = "different display name"
    second = purge_fingerprint(
        {"start_ids": ["root"]},
        ["fam"],
        cosmetically_changed,
        [{"group_id": "g1", "group_name": "Renamed", "prefix": "fam"}],
    )
    assert first == second
    drifted = deepcopy(plan)
    drifted["ops"][0]["tags"].append("fam-new")
    assert purge_fingerprint({"start_ids": ["root"]}, ["fam"], drifted, shared) != first


def test_fingerprint_uses_live_postfix_vocabulary():
    custom_postfixes = ("unfold", "hide", "old")
    family = "topic--expand"
    plan = plan_tag_family_purge(
        _cache(), ["root"], [family], custom_postfixes
    )
    fingerprint = purge_fingerprint(
        {"start_ids": ["root"]},
        [family],
        plan,
        [],
        live_postfixes=custom_postfixes,
    )
    assert len(fingerprint) == 64


def _server_snapshot(groups=()):
    cache = _cache()
    context = LensEvalContext(
        cache_by_id=cache,
        resolve_group=lambda handle: next(group for group in groups if group["id"] == handle),
        resolve_root=lambda handle: handle if handle in cache else (_ for _ in ()).throw(ValueError(handle)),
        resolve_inherited_root=lambda handle: handle if handle in cache else (_ for _ in ()).throw(ValueError(handle)),
        auto_expand_postfix="expand",
        suppress_postfix="suppress",
        deprecated_postfix="deprecated",
        global_never_stems=(),
        exclude_completed=False,
        anchor_boundary_predicate=None,
    )
    return server._LensStage0Snapshot(
        context=context,
        cache_by_id=cache,
        groups_cfg=tuple(MappingProxyType(dict(group)) for group in groups),
        active_group_id=(groups[0]["id"] if groups else None),
        postfixes=MappingProxyType({"auto_expand":"expand","suppress":"suppress","deprecated":"deprecated"}),
        global_never=(),
        raw_config=MappingProxyType({}),
    )


def test_server_purge_preview_derives_group_scope_and_excludes_self(monkeypatch):
    groups = (
        {"id":"g1","name":"One","root_uuid":"root","prefixes":["fam"],"description":""},
        {"id":"g2","name":"Two","root_uuid":"root","prefixes":["fam-team"],"description":""},
    )

    async def snapshot(*_args, **_kwargs):
        return _server_snapshot(groups)

    monkeypatch.setattr(server, "_build_lens_stage0_snapshot", snapshot)
    preview = asyncio.run(server._purge_preview_impl(group_handle="g1"))
    assert preview["scope"] == {"start_ids": ["root"], "group_id": "g1"}
    assert preview["families"] == ["fam"]
    assert {row["group_id"] for row in preview["shared_use"]} == {"g2"}
    assert preview["plan"]["ops"]


def test_purge_apply_stale_and_shared_refusals_are_zero_write(monkeypatch):
    called = False

    async def preview(**_kwargs):
        return {
            "success": True,
            "fingerprint": "current",
            "shared_use": [{"group_id":"g2","prefix":"fam"}],
            "plan": {"ops": [{"handle":"a","operation":"remove","tags":["fam"]}]},
            "scope": {"start_ids":["root"]},
            "families": ["fam"],
        }

    async def mutate(_ops):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(server, "_purge_preview_impl", preview)
    monkeypatch.setattr(server, "_tag_batch_impl", mutate)
    with pytest.raises(server._S07OperationError) as stale:
        asyncio.run(server._purge_apply_impl(fingerprint="old", force_shared=False))
    assert stale.value.code == "stale_purge_preview"
    with pytest.raises(server._S07OperationError) as shared:
        asyncio.run(server._purge_apply_impl(fingerprint="current", force_shared=False))
    assert shared.value.code == "shared_family_collision"
    assert called is False


def test_purge_apply_preserves_q4_ledger_and_verifies_fresh_cache(monkeypatch):
    preview_payload = {
        "success": True,
        "fingerprint": "fp",
        "shared_use": [],
        "plan": {"ops": [{"handle":"a","operation":"remove","tags":["fam"]}]},
        "scope": {"start_ids":["root"]},
        "families": ["fam"],
    }

    async def preview(**_kwargs):
        return preview_payload

    async def mutate(_ops):
        return {"success": True, "nodes_changed": ["a"], "results": [{"status":"committed"}]}

    async def config(_target_tab):
        return [], None, {"auto_expand":"expand","suppress":"suppress","deprecated":"deprecated"}, [], {}

    monkeypatch.setattr(server, "_purge_preview_impl", preview)
    monkeypatch.setattr(server, "_tag_batch_impl", mutate)
    monkeypatch.setattr(server, "_current_cache_by_id", lambda: _cache())
    monkeypatch.setattr(server, "_resolve_slice_group_config", config)
    monkeypatch.setattr(
        tag_purge_policy,
        "plan_tag_family_purge",
        lambda *a, **k: {"mutable": [], "ops": [], "report_only": [], "counts": {}},
    )
    result = asyncio.run(server._purge_apply_impl(fingerprint="fp", force_shared=False))
    assert result["success"] is True
    assert result["changed"] is True
    assert result["mutation"]["results"] == [{"status":"committed"}]
    assert result["verification"]["matched"] is True


def test_slice_group_delete_clears_active_in_same_write_and_never_tags(monkeypatch):
    groups = [{"id":"g1","name":"One","root_uuid":"root","prefixes":["fam"],"description":""}]

    async def config(_target_tab):
        return groups, "g1", {}, [], {}

    writes = []

    async def write(next_groups, active, target_tab):
        writes.append((next_groups, active, target_tab))
        return {"expand_groups": next_groups, "active_group": active}

    monkeypatch.setattr(server, "_resolve_slice_group_config", config)
    monkeypatch.setattr(server, "_write_slice_group_config", write)
    result = asyncio.run(server._slice_group_delete_impl("g1"))
    assert result["success"] is True
    assert result["active_cleared"] is True
    assert writes == [([], None, None)]


def test_tag_purge_async_returns_normalized_job_envelope(monkeypatch):
    async def start(kind, payload, coro_factory):
        return {"success": True, "job_id": "purge-7", "status": "started", "kind": kind}

    monkeypatch.setattr(server, "_start_background_job", start)
    result = asyncio.run(
        dispatch_operation(
            server.TAG_REGISTRY,
            "purge.apply",
            {
                "family": "fam",
                "root_handle": "root",
                "fingerprint": "fp",
                "execution": "async",
            },
        )
    )
    assert result == {
        "success": True,
        "operation": "tag.purge.apply",
        "job": {"id": "purge-7", "state": "started", "kind": "tag-purge"},
    }
