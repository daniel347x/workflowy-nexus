"""Exact-view reuse, ownership and invalidation (no time or marker cache)."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import gc
import json
import weakref
from unittest.mock import Mock

import pytest

from workflowy_mcp import cache_metrics as cache
from workflowy_mcp import dock_peek_metrics as dock
from workflowy_mcp.client.api_client_nexus import build_uuid_prefix_collisions
from workflowy_mcp.coherence.cache_view import CacheView, CacheViewPublisher
from workflowy_mcp.coherence.operation_cache import (
    OperationCacheSnapshot, _OPERATION_CACHE_SNAPSHOT,
    archive_operation_scope, cache_operation_boundary,
    current_operation_cache, publish_local_cache_view,
)
from workflowy_mcp.node_child_counts import build_direct_child_counts

R = "11111111-1111-4111-8111-111111111111"
A = "abcdef01-1111-4111-8111-111111111111"
B = "abcdef01-2111-4111-8111-111111111111"


def make_view(nodes=None, *, epoch=1, sequence=0):
    return CacheView.build(epoch_number=epoch, epoch_id=f"epoch-{epoch}", adopted_sequence=sequence,
        nodes_export={"nodes": nodes if nodes is not None else [
            {"id": R, "name": "Root #dock", "note": "é\n"},
            {"id": A, "name": "Alpha #wfy-suppress", "note": "📊", "parent_id": R, "priority": 1},
            {"id": B, "name": "Done #dock", "note": "BEACON (x)", "parent_id": R, "priority": 0, "completed": True},
        ]})


@contextmanager
def pinned(view):
    snapshot = OperationCacheSnapshot("test", "metrics", view, view.nodes_export, view.nodes_by_id)
    token = _OPERATION_CACHE_SNAPSHOT.set(snapshot)
    try:
        yield view.nodes_by_id
    finally:
        _OPERATION_CACHE_SNAPSHOT.reset(token)


def legacy_index(nodes, root):
    """Independent pre-change e1e9d06 algorithm, including insertion order."""
    direct = {str(key): 0 for key in nodes}
    children = {}
    for key, node in nodes.items():
        parent = node.get("parent_id") if "parent_id" in node else node.get("parentId")
        if parent is not None and str(parent) in direct:
            direct[str(parent)] += 1
            children.setdefault(str(parent), []).append(str(key))
    def priority(key):
        value = nodes[key].get("priority")
        return (0, float(value)) if isinstance(value, (int, float)) else (1, 0.0)
    ordered = {parent: tuple(sorted(ids, key=priority)) for parent, ids in children.items()}
    metrics, active = {}, set()
    stack = [(root, False)]
    while stack:
        key, finishing = stack.pop()
        if finishing:
            node = nodes[key]
            size = len(str(node.get("name") or node.get("nm") or "").encode("utf-8")) + len(str(node.get("note") or node.get("no") or "").encode("utf-8"))
            kids = ordered.get(key, ())
            metrics[key] = {"child_count": direct[key],
                "descendant_count": sum(1 + metrics[c]["descendant_count"] for c in kids),
                "self_bytes": size, "subtree_bytes": size + sum(metrics[c]["subtree_bytes"] for c in kids)}
            active.remove(key)
        else:
            if key in active:
                raise ValueError("Cycle")
            if key in metrics:
                continue
            active.add(key)
            stack.append((key, True))
            stack.extend((c, False) for c in reversed(ordered.get(key, ())))
    return ordered, metrics


def test_reuse_within_and_between_operations_owns_all_results(monkeypatch):
    view = make_view()
    builder = Mock(wraps=dock._build_ordered_children)
    monkeypatch.setattr(dock, "_build_ordered_children", builder)
    with pinned(view) as nodes:
        first_memo = cache.view_metrics_for(nodes)
        ordered, metrics = dock.build_dock_peek_index(nodes, R)
        counts = build_direct_child_counts(nodes)
        collisions = build_uuid_prefix_collisions(nodes)
        first_index = first_memo.source_metrics
        ordered[R] = ()
        metrics[A]["self_bytes"] = -1
        counts[R] = -1
        collisions["abcdef01"].clear()
        with pytest.raises(TypeError):
            first_index[A]["self_bytes"] = 0
    with pinned(view) as nodes:
        assert cache.view_metrics_for(nodes) is first_memo
        assert dock.build_dock_peek_index(nodes, R) == legacy_index(nodes, R)
        assert first_memo.source_metrics is first_index
        assert build_direct_child_counts(nodes)[R] == 2
        assert build_uuid_prefix_collisions(nodes) == {"abcdef01": [A, B]}
    builder.assert_called_once()


@pytest.mark.parametrize("epoch,sequence", [(2, 0), (1, 1), (1, 0)])
def test_new_view_recomputes_even_equal_markers(epoch, sequence):
    before = make_view()
    with pinned(before) as nodes:
        old = cache.view_metrics_for(nodes)
        dock.build_dock_peek_index(nodes, R)
        build_uuid_prefix_collisions(nodes)
    after = make_view([{"id": R, "name": "changed 📊", "note": "longer", "parent_id": None},
                       {"id": A, "name": "only child", "parent_id": R}], epoch=epoch, sequence=sequence)
    with pinned(after) as nodes:
        assert cache.view_metrics_for(nodes) is not old
        assert dock.build_dock_peek_index(nodes, R) == legacy_index(nodes, R)
        assert build_direct_child_counts(nodes)[R] == 1
        assert build_uuid_prefix_collisions(nodes) == {}


@pytest.mark.parametrize("change", ["name", "note", "added", "moved", "completed"])
def test_cow_read_your_writes_equal_marker_successor(change):
    publisher = CacheViewPublisher(make_view())
    class Client:
        _cache_view_publisher = publisher
        def _capture_view_and_dirty(self):
            return publisher.capture(), frozenset()
    async def scenario():
        async with cache_operation_boundary(Client(), "metrics"):
            original = current_operation_cache().view
            old = cache.view_metrics_for(original.nodes_by_id)
            dock.build_dock_peek_index(original.nodes_by_id, R)
            build_uuid_prefix_collisions(original.nodes_by_id)
            def transform(payload):
                rows = [dict(row) for row in payload["nodes"]]
                if change in ("name", "note"):
                    rows[0][change] = "a much longer 📊 value"
                elif change == "added":
                    rows.append({"id": "new", "name": "new child", "parent_id": R})
                elif change == "moved":
                    rows[1]["parent_id"] = B
                else:
                    rows[1]["completed"] = True
                payload["nodes"] = rows
            publish_local_cache_view(publisher, transform, reason="test")
            successor = current_operation_cache().view
            assert successor is not original
            assert (successor.epoch_id, successor.adopted_sequence) == (original.epoch_id, original.adopted_sequence)
            assert cache.view_metrics_for(successor.nodes_by_id) is not old
            assert dock.build_dock_peek_index(successor.nodes_by_id, R) == legacy_index(successor.nodes_by_id, R)
            assert dock.build_dock_peek_index(original.nodes_by_id, R) == legacy_index(original.nodes_by_id, R)
    asyncio.run(scenario())


def test_archive_augmented_equal_marker_view_and_restore():
    live = make_view()
    augmented = make_view([*live.nodes_export["nodes"],
        {"id": "archive", "name": "📦 archived", "note": "é", "parent_id": R, "_nexus_archive": {"file": "test"}}])
    with pinned(live) as nodes:
        old = cache.view_metrics_for(nodes)
        expected = dock.build_dock_peek_index(nodes, R)
        with archive_operation_scope(object(), view=augmented):
            assert cache.view_metrics_for(augmented.nodes_by_id) is not old
            actual = dock.build_dock_peek_index(augmented.nodes_by_id, R)
            assert actual == legacy_index(augmented.nodes_by_id, R)
            assert actual[1][R]["child_count"] == 3
        assert dock.build_dock_peek_index(nodes, R) == expected


def test_map_identity_only_and_plain_map_fallback():
    view = make_view()
    with pinned(view) as nodes:
        assert cache.map_for_pinned_nodes(view.nodes_export["nodes"]) is nodes
        assert cache.map_for_pinned_nodes(list(view.nodes_export["nodes"])) is None
        private = {key: dict(value) for key, value in nodes.items()}
        assert cache.view_metrics_for(private) is None
        old = dock.build_dock_peek_index(private, R)
        private[A]["note"] = "changed"
        assert dock.build_dock_peek_index(private, R) != old
    assert cache.view_metrics_for(view.nodes_by_id) is None
    assert cache.map_for_pinned_nodes(view.nodes_export["nodes"]) is None


def test_dead_view_cleanup_and_recycled_identity_cannot_alias():
    view = make_view()
    key = id(view)
    reference = weakref.ref(view)
    with pinned(view) as nodes:
        memo = cache.view_metrics_for(nodes)
        dock.build_dock_peek_index(nodes, R)
    del nodes, view
    gc.collect()
    assert reference() is None  # memo holds neither view nor source rows
    assert key not in cache._VIEW_METRICS
    newer = make_view([{"id": R, "name": "different"}])
    # Deterministic recycled-id witness: stale/dead weak ref at the new key.
    cache._VIEW_METRICS[id(newer)] = (reference, memo)
    with pinned(newer) as nodes:
        assert cache.view_metrics_for(nodes) is not memo
        assert dock.build_dock_peek_index(nodes, R) == legacy_index(nodes, R)


def test_overlapping_roots_extend_without_mutating_published_indexes():
    view = make_view()
    with pinned(view) as nodes:
        dock.build_dock_peek_index(nodes, A)
        memo = cache.view_metrics_for(nodes)
        earlier = memo.source_metrics
        merged = dock.build_render_source_metrics(nodes, [A, "absent", R, B])
        assert set(earlier) == {A}
        assert memo.source_metrics is not earlier
        assert merged == legacy_index(nodes, R)[1]


@pytest.mark.parametrize("root", [R, A, B])
def test_byte_identical_postorder_dicts_against_base_algorithm(root):
    view = make_view()
    with pinned(view) as nodes:
        old = legacy_index(nodes, root)
        for _ in range(2):
            new = dock.build_dock_peek_index(nodes, root)
            assert json.dumps(new, ensure_ascii=False).encode() == json.dumps(old, ensure_ascii=False).encode()


def test_cycle_and_unrelated_unencodable_content_keep_old_failure_scope():
    view = make_view([{"id": R, "name": "good"}, {"id": A, "parent_id": A, "note": "\ud800"}])
    with pinned(view) as nodes:
        assert dock.build_dock_peek_index(nodes, R) == legacy_index(nodes, R)
        with pytest.raises(ValueError, match="Cycle"):
            dock.build_dock_peek_index(nodes, A)
