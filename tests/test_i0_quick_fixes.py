"""I0: remove discarded work, keep the existing byte/order/error contracts."""
from __future__ import annotations

import asyncio
from unittest.mock import Mock

import pytest

from workflowy_mcp import ancestor_path, dock_peek_metrics, server
from workflowy_mcp.client import api_client_nexus as acn
from workflowy_mcp.client.api_client_etch import _project_cached_export

import test_anchor_point_read as point
import test_anchor_point_promote as promote
from test_anchor_dock_get_description import getter_env
from test_archive_hint import _Env
from test_beacon_fix_verb import env, _call, _item
from test_beacon_snippet_outcomes import _L4, _block, _nodes, _write, NODE


def test_point_header_sizes_only_target_and_keeps_old_bytes(monkeypatch):
    point.setup(monkeypatch)
    composer = ancestor_path.build_ancestor_path_document
    chains = []

    def counted(chain, *args, **kwargs):
        chains.append([entry["id"] for entry in chain])
        return composer(chain, *args, **kwargs)

    monkeypatch.setattr(ancestor_path, "build_ancestor_path_document", counted)
    actual = point.dispatch_verb()
    assert chains == [[point.T]]

    def old_composer(_chain, nodes, **kwargs):
        return composer(ancestor_path.walk_ancestor_chain(point.T, nodes), nodes, **kwargs)

    monkeypatch.setattr(ancestor_path, "build_ancestor_path_document", old_composer)
    assert actual.encode() == point.dispatch_verb().encode()


def test_description_counts_direct_docks_without_an_extra_aggregate(getter_env, monkeypatch):
    cache = getter_env
    cache["alias"] = {"id": "alias", "name": "Alias #wfy-ns--local", "parentId": "d1"}
    cache["masked"] = {"id": "masked", "name": "Masked #wfy-ns", "parent_id": None, "parentId": "d1"}
    cache["done"] = {"id": "done", "name": "Done #wfy-ns--fav", "parent_id": "d1", "completed": True}
    counted = Mock(wraps=dock_peek_metrics.build_dock_peek_index)
    monkeypatch.setattr(dock_peek_metrics, "build_dock_peek_index", counted)
    monkeypatch.setattr(ancestor_path, "build_dock_peek_index", counted)
    text = asyncio.run(server._anchor_dock_get_description_impl("d1"))
    assert "child docks: 3" in text
    assert [call.args[1] for call in counted.call_args_list] == ["d1"]


@pytest.mark.parametrize("handle", ["[ap:APCHILD]", "ap:APRSLV3", "[APFAMILY2]", "not-a-uuid-prefix", "1234567z-tail"])
def test_impossible_uuid_minimum_prefix_never_scans(handle):
    class NoScan(dict):
        def keys(self):
            raise AssertionError("impossible UUID candidate scanned keys")

    assert acn._resolve_uuid_with_prefix_fallback(handle, NoScan({point.T: {}})) == {}


@pytest.mark.parametrize("key_prefix", ["", "grp-"])
def test_nonhex_invented_tail_keeps_grace_ambiguity_order_and_filters(key_prefix):
    first = key_prefix + "12345678-aaaa-4000-8000-000000000001"
    second = key_prefix + "12345678-bbbb-4000-8000-000000000002"
    nodes = {first: {"name": " First "}, second: {"nm": "Second"}}
    handle = key_prefix + "12345678nothex"
    assert acn._resolve_uuid_with_prefix_fallback(handle, nodes, key_prefix=key_prefix, max_candidates=1) == {
        "candidates": [{"uuid": first, "name": "First"}], "matched_prefix_len": 8, "truncated": 1}
    assert acn._resolve_uuid_with_prefix_fallback(handle, nodes, key_prefix=key_prefix,
                                                 key_filter=lambda uid: uid == second) == {
        "resolved_uuid": second, "matched_prefix_len": 8}


def test_ap_hint_reuses_overlay_and_rebuilds_for_view_and_store_changes(tmp_path, monkeypatch):
    from workflowy_mcp.archive_store import get_archive_store, set_archive_store_for_tests
    from workflowy_mcp.coherence.cache_view import CacheView

    _Env(tmp_path, monkeypatch)
    store = get_archive_store()
    view = CacheView.build(nodes_export={"nodes": []}, epoch_number=1, epoch_id="e", adopted_sequence=0)
    build = Mock(wraps=store._build_overlay)
    monkeypatch.setattr(store, "_build_overlay", build)
    try:
        assert store.overlay_for(view) is not None
        expected = {"state": "B", "folder": str(tmp_path), "n": 1}
        assert store.ap_hint_facts(view) == store.ap_hint_facts(view) == expected
        assert build.call_count == 1
        successor = CacheView.build(nodes_export={"nodes": []}, epoch_number=1, epoch_id="e", adopted_sequence=0)
        assert store.ap_hint_facts(successor) == expected
        assert build.call_count == 2
        (tmp_path / "arch.json").unlink()
        assert store.ap_hint_facts(successor) == {**expected, "n": 0}
        assert build.call_count == 3
    finally:
        set_archive_store_for_tests(None)


@pytest.mark.parametrize("children,include_completed", [(False, False), (False, True), (True, False), (True, True)])
def test_derivative_enumeration_keeps_old_order_without_sizes(monkeypatch, children, include_completed):
    from workflowy_mcp.f8_slice_policy import _node_is_completed
    nodes = {row["id"]: row for row in promote._nodes()}
    ordered, _ = dock_peek_metrics.build_dock_peek_index(nodes, promote.C)
    expected = []
    stack = [(promote.C, None, 0)]
    while stack:
        uid, parent, depth = stack.pop()
        completed = _node_is_completed(nodes[uid])
        if depth and completed and not include_completed:
            continue
        expected.append({"uuid": uid, "parent": parent, "depth": depth, "completed": completed})
        if children:
            stack.extend((child, uid, depth + 1) for child in reversed(ordered.get(uid, ())))
    monkeypatch.setattr(dock_peek_metrics, "_aggregate_source_metrics", lambda *a: pytest.fail("discarded sizes"))
    assert server._promote_enumerate_subtree(nodes, promote.C, children=children, include_completed=include_completed) == expected


@pytest.mark.parametrize("children", [False, True])
def test_derivative_reachable_cycle_keeps_exact_error_even_behind_completed(children):
    nodes = {"r": {"parent_id": "c"}, "c": {"parent_id": "r", "completed": True}}
    with pytest.raises(ValueError, match="Cycle in Dock peek source topology at 'r'"):
        server._promote_enumerate_subtree(nodes, "r", children=children, include_completed=False)
    with pytest.raises(ValueError, match="Dock peek metrics root 'missing' is absent from the snapshot"):
        server._promote_enumerate_subtree(nodes, "missing", children=children, include_completed=False)


def test_derivative_copy_frontier_count_uses_no_metrics(monkeypatch, tmp_path):
    _client, fake, *_ = promote.setup(monkeypatch, tmp_path)
    monkeypatch.setattr(dock_peek_metrics, "_aggregate_source_metrics", lambda *a: pytest.fail("discarded sizes"))
    result = promote.promote(items=[{"handle": promote.C, "destination": promote.T, "mode": "derivative"}])
    assert result["success"], result
    assert result["detail"]["results"][0]["copy"]["completed_skipped"] == 1
    assert len(fake.creates) == 4


def test_topology_only_owner_reuses_the_existing_exact_view_memo(monkeypatch):
    from workflowy_mcp.cache_metrics import view_metrics_for
    from workflowy_mcp.coherence.operation_cache import cache_operation_boundary

    client = point.setup(monkeypatch)

    async def check():
        async with cache_operation_boundary(client, "i0:topology-only") as snapshot:
            nodes = snapshot.nodes_by_id
            topology = dock_peek_metrics._build_ordered_children(nodes)
            server._promote_enumerate_subtree(nodes, point.DOCK, children=True, include_completed=False)
            assert dock_peek_metrics._build_ordered_children(nodes) is topology
            assert not view_metrics_for(nodes).source_metrics
            with pytest.raises(TypeError):
                topology[point.DOCK] = ()
            ordered, metrics = dock_peek_metrics.build_dock_peek_index(nodes, point.DOCK)
            assert ordered == topology and ordered is not topology
            assert metrics[point.DOCK]["descendant_count"] == 1
            assert dock_peek_metrics._build_ordered_children(nodes) is topology

    asyncio.run(check())


@pytest.mark.parametrize("markdown", [False, True])
def test_snippet_ordinal_only_for_markdown_but_ast_twin_evidence_stays(tmp_path, monkeypatch, markdown):
    import nexus_map_codebase as source
    if markdown:
        path = _write(tmp_path, "doc.md", ["<!-- @beacon[", "id=demo@target,", "kind=ast,", "slice_labels=demo,", "] -->", "# Target", "body"])
        note = "MD_PATH:\n# Target\n---\nBEACON (MD AST)\nid: demo@target\nkind: ast"
    else:
        path = _write(tmp_path, "source.py", [*_block("demo@target"), "def target():", "    return 1"])
        note = "AST_QUALNAME: target\nBEACON (AST)\nid: demo@target\nkind: ast"
    node, nodes = _nodes(path, note)
    ordinal = Mock(wraps=acn._compute_same_name_sibling_ordinal)
    twin = Mock(wraps=source._beacon_cache_twin_witness)
    monkeypatch.setattr(acn, "_compute_same_name_sibling_ordinal", ordinal)
    monkeypatch.setattr(source, "_beacon_cache_twin_witness", twin)
    result = asyncio.run(_L4().get_code_snippet_for_resolved_node(NODE, node, nodes, 0))
    assert result["success"]
    assert ordinal.call_count == int(markdown)
    assert twin.call_count == 1


@pytest.mark.parametrize("paths", [False, True])
def test_fix_path_index_only_when_requested(env, monkeypatch, paths):
    from workflowy_mcp import beacon_fix
    indexed = Mock(wraps=beacon_fix.mapped_path_index)
    monkeypatch.setattr(beacon_fix, "mapped_path_index", indexed)
    item = _item(env.a) if paths else _item(env.a, node_handle=env.nodes[2]["id"])
    result = _call([item], dry_run=True)
    assert result["success"], result
    assert indexed.call_count == int(paths)


def test_cached_export_has_no_dead_diagnostic_and_keeps_aliases(capsys):
    node = {"id": point.T, "name": "__CARTO_DEBUG_EXPORT_UNUSED__", "note": "same sentinel"}
    payload = {"nodes": [node]}
    actual = _project_cached_export(payload, None)
    assert actual == {"nodes": [node], "_total_fetched_from_api": 1}
    assert actual is not payload and actual["nodes"] is payload["nodes"]
    assert actual["nodes"][0] is node
    assert "_total_fetched_from_api" not in payload
    assert capsys.readouterr().err == ""
