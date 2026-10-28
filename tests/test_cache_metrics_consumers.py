"""Every metrics origin retains the exact pinned map and fresh vocabulary."""
from __future__ import annotations

import asyncio
import contextvars
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, Mock

import pytest

from workflowy_mcp import cache_metrics, dock_peek_metrics, server
from workflowy_mcp.client.api_client_nexus import load_nodes_by_id_from_cache
from workflowy_mcp.coherence.operation_cache import cache_operation_boundary
from workflowy_mcp.mcp_api_registry import dispatch_operation

from test_cache_metrics import make_view, pinned, legacy_index, R
from test_dock_peek_metrics import setup as peek_setup, dispatch as peek, X
from test_headings_end_of_line_metadata import setup as render_setup, R as RENDER_ROOT
from test_anchor_dock_get_description import getter_env


def dispatch(family, verb, params):
    registry = {"glimpse": server.GLIMPSE_REGISTRY, "anchor": server.ANCHOR_REGISTRY,
                "lightning_rod": server.LIGHTNING_ROD_REGISTRY, "node": server.NODE_REGISTRY,
                "slice_group": server.SLICE_GROUP_REGISTRY}[family]
    return asyncio.run(dispatch_operation(registry, verb, params,
        operation_boundary=server._mcp_operation_boundary,
        mutation_admission=server._mcp_mutation_admission,
        exception_mapper=server._mcp_exception_mapper,
        result_finalizer=server._mcp_operation_result_finalizer))


@pytest.mark.parametrize("family,verb,params", [
    ("anchor", "dock.peek", {"dock_handle": RENDER_ROOT, "detail": "full"}),
    ("glimpse", "run", {"root_handle": RENDER_ROOT, "allow_anchor_dock": True, "headings_only": True}),
    ("glimpse", "estate", {"handle": {"subtree": RENDER_ROOT}, "headings_only": True}),
    ("lightning_rod", "render_tags", {"root_handle": RENDER_ROOT, "tags": ["view"], "headings_only": True}),
    ("slice_group", "render", {"lens": {"subtree": RENDER_ROOT}, "headings_only": True}),
    ("node", "ancestors", {"node_handle": RENDER_ROOT, "markers": False}),
])
def test_public_origins_reuse_one_exact_view(monkeypatch, family, verb, params):
    render_setup(monkeypatch)
    builder = Mock(wraps=dock_peek_metrics._build_ordered_children)
    monkeypatch.setattr(dock_peek_metrics, "_build_ordered_children", builder)
    first = dispatch(family, verb, params)
    second = dispatch(family, verb, params)
    assert first == second
    assert not isinstance(first, dict) or first.get("success") is not False, first
    builder.assert_called_once()


def test_map_producers_borrow_only_the_accessor_proved_view(monkeypatch):
    client = render_setup(monkeypatch)
    async def scenario():
        async with cache_operation_boundary(client, "metrics") as snapshot:
            expected = snapshot.view.nodes_by_id
            assert server._anchor_cache_by_id() is expected
            assert server._current_cache_by_id() is expected
            assert client._modal_cache_nodes_by_id() is expected
            assert load_nodes_by_id_from_cache(client) is expected
            # Export subsets / ad-hoc accessor doubles must not be re-labelled.
            monkeypatch.setattr(client, "_get_nodes_export_cache_nodes", lambda: [{"id": "private", "name": "private"}])
            assert client._modal_cache_nodes_by_id() == {"private": {"id": "private", "name": "private"}}
            assert load_nodes_by_id_from_cache(client) == {"private": {"id": "private", "name": "private"}}
    asyncio.run(scenario())


def test_next_peek_observes_changed_vocabulary_without_invalidating_metrics(monkeypatch):
    client, config = peek_setup(monkeypatch)
    config.side_effect = [(["dock"], [], None, {"note_session_local_postfixes": ["local"]}),
                          (["dock"], [], None, {"note_session_local_postfixes": ["near"]})]
    first = peek(detail="full", recursive=True)
    with pinned(client._cache_view_publisher.capture()) as nodes:
        memo = cache_metrics.view_metrics_for(nodes)
        immutable_sizes = memo.source_metrics
    second = peek(detail="full", recursive=True)
    assert X not in {row["uuid"] for row in first["rows"]}
    assert X in {row["uuid"] for row in second["rows"]}
    assert first["root_metrics"] == second["root_metrics"]
    assert config.await_count == 2
    assert memo.source_metrics is immutable_sizes


def test_description_shares_one_vocabulary_read_but_next_call_rereads(getter_env, monkeypatch):
    config = AsyncMock(wraps=server._resolve_note_session_config)
    monkeypatch.setattr(server, "_resolve_note_session_config", config)
    monkeypatch.setattr(server, "get_ws_connection", lambda: (object(), object()))
    first = asyncio.run(server._anchor_dock_get_description_impl("d1"))
    assert "dock markers unavailable" not in first
    assert config.await_count == 1
    assert asyncio.run(server._anchor_dock_get_description_impl("d1")) == first
    assert config.await_count == 2


def test_parallel_duplicate_builds_publish_only_complete_immutable_indexes():
    view = make_view()
    with pinned(view) as nodes:
        contexts = [contextvars.copy_context() for _ in range(8)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(ctx.run, dock_peek_metrics.build_dock_peek_index, nodes, R) for ctx in contexts]
            outputs = [future.result(timeout=10) for future in futures]
        assert all(output == legacy_index(nodes, R) for output in outputs)
        memo = cache_metrics.view_metrics_for(nodes)
        with pytest.raises(TypeError):
            memo.indexes["ordered_children"][R] = ()
        with pytest.raises(TypeError):
            memo.source_metrics[R]["self_bytes"] = 0
