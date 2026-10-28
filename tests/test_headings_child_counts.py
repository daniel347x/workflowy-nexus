"""Source child counts survive projection/focus; render and mutation rails intact."""
from __future__ import annotations

import asyncio
import copy
import json
from unittest.mock import AsyncMock, Mock

import pytest
from coherence_harness import make_boundary_capable, make_coherence_root, s04_raise_barrier
from workflowy_mcp import server
from workflowy_mcp.cache_render_spec import RenderPolicy, RenderProjectionSpec
from workflowy_mcp.client.api_client_nexus import WorkFlowyClientNexus
from workflowy_mcp.coherence.operation_cache import current_operation_cache
from workflowy_mcp.mcp_api_registry import dispatch_operation
from workflowy_mcp.models.node import WorkFlowyNode
from workflowy_mcp.node_child_counts import build_direct_child_counts, format_child_count_suffix

R = "11111111-1111-4111-8111-111111111111"
A = "22222222-2222-4222-8222-222222222222"
B = "33333333-3333-4333-8333-333333333333"
C = "44444444-4444-4444-8444-444444444444"
G = "55555555-5555-4555-8555-555555555555"


def cache_rows():
    return [
        {"id": R, "name": "Root #wfy-ns", "note": "ROOT BODY", "parent_id": None, "priority": 0},
        {"id": A, "name": "Alpha #view", "note": "ALPHA BODY", "parent_id": R, "priority": 0},
        {"id": B, "name": "Beta", "note": "HIDDEN BODY", "parent_id": R, "priority": 1},
        {"id": C, "name": "Completed #wfy-suppress", "note": "", "parent_id": R, "completed": True, "priority": 2},
        {"id": G, "name": "Hidden grandchild", "note": "", "parent_id": A, "completed": True},
    ]


def forest():
    # Projection has hidden ALL but Alpha, and Alpha looks childless.
    return [{"id": R, "name": "Root #wfy-ns", "note": "ROOT BODY", "children": [
        {"id": A, "name": "Alpha #view", "note": "ALPHA BODY", "children": []},
    ]}]


def setup(monkeypatch):
    client = make_boundary_capable(object.__new__(WorkFlowyClientNexus), nodes=cache_rows())
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "_rate_limiter", None)
    monkeypatch.setattr(server, "_resolve_note_session_config", AsyncMock(return_value=(["wfy-ns"], [], None, {"note_session_local_postfixes": ["local"]})))
    monkeypatch.setattr(server, "_resolve_slice_group_config", AsyncMock(return_value=([], None, {"auto_expand": "expand", "suppress": "suppress", "deprecated": "deprecated"}, ["wfy-suppress"], {})))
    return client


def run(coro):
    return asyncio.run(coro)


async def inside(client, callback):
    async with server._mcp_operation_boundary("test.child-counts"):
        return callback()


def test_pure_source_index_counts_all_children_not_just_visible_or_active():
    rows = {row["id"]: row for row in cache_rows()}
    before = copy.deepcopy(rows)
    counts = build_direct_child_counts(rows)
    assert counts == {R: 3, A: 1, B: 0, C: 0, G: 0}
    assert rows == before
    assert format_child_count_suffix(R, counts) == " ▸3"
    assert format_child_count_suffix(B, counts) == " ▸0"
    assert format_child_count_suffix("missing", counts) == " ▸?"
    assert format_child_count_suffix(R, None) == " ▸?"
    assert format_child_count_suffix("", counts) == ""


@pytest.mark.parametrize("full", [False, True])
def test_serializers_read_counts_from_policy_and_never_mutate_inputs(full):
    tree = forest()
    before = copy.deepcopy(tree)
    counts = build_direct_child_counts({row["id"]: row for row in cache_rows()})
    policy = RenderPolicy(direct_child_counts=counts)
    if full:
        text = "\n".join(WorkFlowyClientNexus._annotate_preview_ids_and_build_tree(tree, "T", headings_only=True, render_policy=policy))
    else:
        text = WorkFlowyClientNexus._build_markdown_preview_from_tree(tree, headings_only=True, render_policy=policy)
    assert "Root #wfy-ns ▸3" in text and "Alpha #view ▸1" in text
    assert "BODY" not in text
    assert tree == before


@pytest.mark.parametrize("full", [False, True])
def test_missing_source_facts_are_unknown_even_if_projected_children_empty(full):
    tree = forest()
    if full:
        text = "\n".join(WorkFlowyClientNexus._annotate_preview_ids_and_build_tree(tree, "T", headings_only=True))
    else:
        text = WorkFlowyClientNexus._build_markdown_preview_from_tree(tree, headings_only=True)
    assert "Root #wfy-ns ▸?" in text and "Alpha #view ▸?" in text
    assert "▸0" not in text


def test_counts_do_not_change_full_body_render_or_focus_membership():
    tree = forest()
    counts = build_direct_child_counts({row["id"]: row for row in cache_rows()})
    focus = {"mode": "row", "uuid": A, "ancestor_path": [R], "occurrence": 0}
    plain = WorkFlowyClientNexus.materialize_from_document_forest(tree, focus=focus)
    counted = WorkFlowyClientNexus.materialize_from_document_forest(tree, focus=focus, direct_child_counts=counts)
    assert counted.render_forest == plain.render_forest
    assert counted.document_rows == plain.document_rows
    assert counted.policy_by_occurrence.direct_child_counts is counts  # reuse, not N vault scans per row
    for full in (False, True):
        renderer = WorkFlowyClientNexus._annotate_preview_ids_and_build_tree if full else WorkFlowyClientNexus._build_markdown_preview_from_tree
        args = [plain.render_forest, "T"] if full else [plain.render_forest]
        assert renderer(*args, render_policy=plain.policy_by_occurrence) == renderer(*args, render_policy=counted.policy_by_occurrence)


@pytest.mark.parametrize("full", [False, True])
@pytest.mark.parametrize("kind", ["raw_root", "slice", "lightning_rod"])
def test_stage2_source_counts_survive_filtered_leaf_set_and_focus(monkeypatch, full, kind):
    client = setup(monkeypatch)
    cache = client._cache_view_publisher.capture().nodes_by_id
    before = copy.deepcopy(cache)
    spec = RenderProjectionSpec(kind=kind, root_uuid=R, as_markdown=not full, headings_only=True, focus={"mode": "row", "uuid": A, "ancestor_path": [R], "occurrence": 0})
    result = run(inside(client, lambda: server.render_stage2_filter_sort(spec=spec, final_subtree=forest()[0], cache_by_id=cache, output_file=None, source_meta={})))
    text = "\n".join(result["preview_tree"]) if full else result["preview_markdown"]
    assert "Root #wfy-ns ▸3" in text and "Alpha #view ▸1" in text
    if not full:
        # [ap:UH0UOU]: the cache-sourced tail carries dock.peek's four-field
        # vocabulary on the markdown headings.
        assert "Root #wfy-ns ▸3 · desc:" in text and "· tree:" in text
    assert "Completed" not in text and "Hidden grandchild" not in text
    assert cache == before


def test_dom_modal_snapshot_computes_count_index_once_for_all_rows(monkeypatch):
    client = setup(monkeypatch)
    import workflowy_mcp.node_child_counts as module
    builder = Mock(wraps=module.build_direct_child_counts)
    monkeypatch.setattr(module, "build_direct_child_counts", builder)
    result = run(inside(client, lambda: client.compose_markdown_modal_snapshot(forest()[0], headings_only=True)))
    assert result["success"] is True
    assert builder.call_count == 1
    for row in result["renders"].values():
        assert "Root #wfy-ns ▸3" in row["markdown"]
        assert "Alpha #view ▸1" in row["markdown"]
        # [ap:UH0UOU]: the modal ctx carries the source-metrics index, so its
        # rows carry the same four-field vocabulary as GLIMPSE (parity).
        assert "· desc:" in row["markdown"] and "· tree:" in row["markdown"]


@pytest.mark.parametrize("full", [False, True])
def test_frozen_dom_readback_uses_same_metadata(full, monkeypatch):
    client = setup(monkeypatch)
    async def render():
        async with server._mcp_operation_boundary("glimpse.run"):
            return await server._glimpse_dispatch_dom_capture(
                captured_tree=forest()[0], root_uuid_hint=R, focus=None,
                as_markdown=not full, headings_only=True, sort_by=None,
                sort_direction="asc", since=None, output_file=None,
            )
    result = run(render())
    text = "\n".join(result["preview_tree"]) if full else result["preview_markdown"]
    assert "Root #wfy-ns ▸3" in text and "Alpha #view ▸1" in text
    if not full:
        # [ap:UH0UOU]: the frozen-DOM readback carries the same vocabulary.
        assert "· desc:" in text and "· tree:" in text


def test_slice_row_reuses_supplied_counts_after_option_y_pruning():
    counts = build_direct_child_counts({row["id"]: row for row in cache_rows()})
    text = WorkFlowyClientNexus._render_slice_row_view(forest()[0], A, [R], occurrence=0, headings_only=True, direct_child_counts=counts)
    assert "Root #wfy-ns ▸3" in text and "Alpha #view ▸1" in text


@pytest.mark.parametrize("family,verb,params", [
    ("glimpse", "run", {"root_handle": R, "allow_anchor_dock": True, "headings_only": True}),
    ("anchor", "dock.render", {"dock_handle": R, "headings_only": True}),
    ("lightning_rod", "render_search", {"root_handle": R, "query": "Alpha", "headings_only": True}),
    ("lightning_rod", "render_tags", {"root_handle": R, "tags": ["view"], "headings_only": True}),
    ("slice_group", "sift", {"lens": {"kind": "subtree", "root_handle": R}, "query": "Alpha", "tier": "headings"}),
])
def test_public_family_paths_disclose_hidden_descendants(monkeypatch, family, verb, params):
    setup(monkeypatch)
    registry = {"glimpse": server.GLIMPSE_REGISTRY, "anchor": server.ANCHOR_REGISTRY, "lightning_rod": server.LIGHTNING_ROD_REGISTRY, "slice_group": server.SLICE_GROUP_REGISTRY}[family]
    result = run(dispatch_operation(registry, verb, params,
        operation_boundary=server._mcp_operation_boundary,
        mutation_admission=server._mcp_mutation_admission,
        exception_mapper=server._mcp_exception_mapper,
        result_finalizer=server._mcp_operation_result_finalizer,
    ))
    # U01: slice_group render/sift/merge.render return the Markdown string
    # directly now, like the other families in this table.
    assert isinstance(result, str), result
    assert "Alpha #view ▸1" in result
    # [ap:UH0UOU]: every covered family carries the four-field vocabulary.
    assert "· desc:" in result and "· tree:" in result


@pytest.mark.parametrize("completed", [False, True])
def test_completion_ack_is_compact_counts_source_children_and_calls_existing_mutator(monkeypatch, tmp_path, completed):
    client = setup(monkeypatch)
    from n05_region_harness import isolate_claim_scope
    isolate_claim_scope(client, tmp_path)
    paths = make_coherence_root(tmp_path)
    monkeypatch.setattr(server, "_admission_coherence_paths", lambda: paths)
    returned = WorkFlowyNode(id=R, name="Root #wfy-ns", note="GIANT BODY" * 100_000,
        completed_flag=completed, completedAt=123 if completed else None,
        children=[WorkFlowyNode(id=A, name="Child", note="CHILD BODY" * 10000)],
        data={"unused_blob": "GIANT BODY" * 1000})
    client.complete_node = AsyncMock(return_value=returned)
    client.uncomplete_node = AsyncMock(return_value=returned)
    client.get_node = AsyncMock(side_effect=AssertionError("no extra GET"))
    before = copy.deepcopy(client._cache_view_publisher.capture().nodes_export)
    result = run(dispatch_operation(server.NODE_REGISTRY, "complete" if completed else "uncomplete", {"node_handle": R},
        operation_boundary=server._mcp_operation_boundary,
        mutation_admission=server._mcp_mutation_admission,
        exception_mapper=server._mcp_exception_mapper,
        result_finalizer=server._mcp_operation_result_finalizer,
    ))
    assert result["success"] and result["detail"]["child_count"] == 3
    assert result["detail"]["completed"] is completed
    assert result["detail"]["child_count_source"] == "operation_cache"
    assert not {"note", "no", "children", "ch", "data"} & result["detail"].keys()
    assert len(json.dumps(result)) < 1000
    assert "GIANT BODY" in returned.note  # no mutation of returned model
    (client.complete_node if completed else client.uncomplete_node).assert_awaited_once_with(R)
    (client.uncomplete_node if completed else client.complete_node).assert_not_awaited()
    client.get_node.assert_not_awaited()
    assert client._cache_view_publisher.capture().nodes_export == before
    assert current_operation_cache(required=False) is None


@pytest.mark.parametrize("mode", ["snapshot", "row", "multi"])
def test_applied_modal_all_paths_share_one_source_index(monkeypatch, mode):
    setup(monkeypatch)
    import workflowy_mcp.node_child_counts as module
    from test_render_slice_modal_spec_e3 import _FakeCaptureWebsocket
    builder = Mock(wraps=module.build_direct_child_counts)
    monkeypatch.setattr(module, "build_direct_child_counts", builder)
    focus = None
    if mode == "row":
        focus = {"mode": "row", "uuid": A, "ancestor_path": [R], "occurrence": 0}
    elif mode == "multi":
        focus = {"mode": "rows", "rows": [
            {"uuid": A, "ancestor_path": [R], "occurrence": 0},
            {"uuid": B, "ancestor_path": [R], "occurrence": 0},
        ]}
    spec = RenderProjectionSpec.normalize_from_glimpse_args(root_uuid=R, headings_only=True, focus=focus)
    ws = _FakeCaptureWebsocket()
    run(server._handle_render_slice_modal({
        "action": "render_slice_row" if mode == "row" else "render_slice_modal",
        "request_id": "counts", "endpoint_id": "test-tab", "max_prerender_chars": 100000,
        "render_spec": spec.to_render_spec_blob(),
    }, ws))
    reply = ws.sent[-1]
    assert reply["success"] is True, reply
    assert "Root #wfy-ns ▸3" in reply["markdown"]
    assert "Alpha #view ▸1" in reply["markdown"]
    assert builder.call_count == 1
    if mode == "snapshot":
        assert reply["renders"]
        for rendered in reply["renders"].values():
            assert "Root #wfy-ns ▸3" in rendered["markdown"]


@pytest.mark.parametrize("full", [False, True])
def test_live_dom_glimpse_counts_use_cache_not_dom_visibility(monkeypatch, full):
    client = setup(monkeypatch)
    async def render():
        queue = asyncio.Queue()
        class Socket:
            async def send(self, raw):
                request = json.loads(raw)
                root = copy.deepcopy(forest()[0])
                await queue.put({"success": True, "request_id": request["request_id"],
                    "root": root, "children": root["children"], "node_count": 2, "depth": 2})
        async with server._mcp_operation_boundary("glimpse.run"):
            return await client.workflowy_glimpse(
                headings_only=True, as_markdown=not full, _ws_connection=Socket(), _ws_queue=queue,
            )
    result = run(render())
    assert result["success"] is True, result
    text = "\n".join(result["preview_tree"]) if full else result["preview_markdown"]
    assert "Root #wfy-ns ▸3" in text and "Alpha #view ▸1" in text


def test_markdown_file_and_agent_return_have_identical_counted_bytes(monkeypatch, tmp_path):
    client = setup(monkeypatch)
    cache = client._cache_view_publisher.capture().nodes_by_id
    spec = RenderProjectionSpec(kind="raw_root", root_uuid=R, headings_only=True)
    kwargs = dict(spec=spec, final_subtree=forest()[0], cache_by_id=cache, source_meta={})
    expected = run(inside(client, lambda: server.render_stage2_filter_sort(**kwargs, output_file=None)))["preview_markdown"]
    path = tmp_path / "counts.md"
    result = run(inside(client, lambda: server.render_stage2_filter_sort(**kwargs, output_file=str(path))))
    assert path.read_bytes() == expected.encode("utf-8")
    assert result["bytes_written"] == len(path.read_bytes())


def test_child_count_disclosure_reaches_tools_and_describe():
    tools = {tool.name: tool for tool in run(server.mcp.list_tools())}
    for family in ("glimpse", "anchor", "lightning_rod", "slice_group"):
        assert "▸N" in tools[family].description and "▸?" in tools[family].description
    for registry, verb in ((server.GLIMPSE_REGISTRY, "run"), (server.ANCHOR_REGISTRY, "dock.render"), (server.LIGHTNING_ROD_REGISTRY, "render_search"), (server.SLICE_GROUP_REGISTRY, "sift")):
        assert "completed/suppressed/out-of-slice" in registry.describe_full(verb)["params_schema"]["properties"]["headings_only"]["description"]
    assert "child_count" in tools["node"].description
    assert "No note" in server.NODE_REGISTRY.describe_full("complete")["returns"]["description"]


def test_hard_barrier_still_refuses_completion_before_mutation(monkeypatch, tmp_path):
    client = setup(monkeypatch)
    paths = make_coherence_root(tmp_path)
    s04_raise_barrier(paths)
    monkeypatch.setattr(server, "_admission_coherence_paths", lambda: paths)
    client.complete_node = AsyncMock()
    result = run(dispatch_operation(server.NODE_REGISTRY, "complete", {"node_handle": R},
        operation_boundary=server._mcp_operation_boundary,
        mutation_admission=server._mcp_mutation_admission,
        exception_mapper=server._mcp_exception_mapper,
    ))
    assert result["error"]["code"] == "coherence_barrier_active"
    client.complete_node.assert_not_awaited()
