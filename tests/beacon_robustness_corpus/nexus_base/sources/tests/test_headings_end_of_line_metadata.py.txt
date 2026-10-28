"""End-of-line headings metadata ([ap:UH0UOU]): one vocabulary with dock.peek.

Every headings-only surface shows dock.peek's four-field metrics suffix
(``▸N · desc:N · self: · tree:``) from the ONE source-metrics engine, except
managed source lines (Cartographer/beacon markers in the note's first line),
which carry none of it. Unmanaged parents of managed subtrees still show it.
"""
from __future__ import annotations

import asyncio
import copy
from unittest.mock import AsyncMock

import pytest
from coherence_harness import make_boundary_capable
from workflowy_mcp import server
from workflowy_mcp.cache_render_spec import RenderPolicy
from workflowy_mcp.client.api_client_nexus import WorkFlowyClientNexus
from workflowy_mcp.dock_peek_metrics import (
    build_dock_peek_index,
    build_render_source_metrics,
    format_dock_peek_metrics,
)
from workflowy_mcp.managed_metadata import is_managed_source_line
from workflowy_mcp.mcp_api_registry import dispatch_operation
from workflowy_mcp.node_child_counts import build_direct_child_counts

R = "11111111-1111-4111-8111-111111111111"  # plain root ("Root #wfy-ns": a Dock)
D = "22222222-2222-4222-8222-222222222222"  # plain child "Docs"
F = "aaaaaaaa-3333-4333-8333-333333333333"  # mapped FILE node (Path: header)
S = "bbbbbbbb-4444-4444-8444-444444444444"  # mapped AST node under F
M = "cccccccc-5555-4555-8555-555555555555"  # beacon-residue node under D
L = "dddddddd-6666-4666-8666-666666666666"  # plain leaf under R


def cache_rows():
    return [
        {"id": R, "name": "Root #wfy-ns", "note": "ROOT BODY", "parent_id": None, "priority": 0},
        {"id": D, "name": "Docs #view", "note": "DOCS BODY", "parent_id": R, "priority": 0},
        {"id": F, "name": "mod.py", "note": "Path: E:\\repo\\mod.py\n---\nfile leader", "parent_id": R, "priority": 1},
        {"id": L, "name": "Plain leaf", "note": "LEAF BODY", "parent_id": R, "priority": 2},
        {"id": M, "name": "residue", "note": "BEACON (id=x\n---\ncomment: hi", "parent_id": D, "priority": 0},
        {"id": S, "name": "func", "note": "AST_QUALNAME: mod.func\n---\ncomment: hello", "parent_id": F, "priority": 0},
    ]


def rows_by_id():
    return {row["id"]: row for row in cache_rows()}


def forest():
    return [{
        "id": R, "name": "Root #wfy-ns", "note": "ROOT BODY", "children": [
            {"id": D, "name": "Docs #view", "note": "DOCS BODY", "children": [
                {"id": M, "name": "residue", "note": "BEACON (id=x\n---\ncomment: hi", "children": []},
            ]},
            {"id": F, "name": "mod.py", "note": "Path: E:\\repo\\mod.py\n---\nfile leader", "children": [
                {"id": S, "name": "func", "note": "AST_QUALNAME: mod.func\n---\ncomment: hello", "children": []},
            ]},
            {"id": L, "name": "Plain leaf", "note": "LEAF BODY", "children": []},
        ],
    }]


def setup(monkeypatch):
    client = make_boundary_capable(object.__new__(WorkFlowyClientNexus), nodes=cache_rows())
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "_rate_limiter", None)
    monkeypatch.setattr(
        server, "_resolve_note_session_config",
        AsyncMock(return_value=(["wfy-ns"], [], None, {"note_session_local_postfixes": ["local"]})),
    )
    monkeypatch.setattr(
        server, "_resolve_slice_group_config",
        AsyncMock(return_value=([], None, {"auto_expand": "expand", "suppress": "suppress", "deprecated": "deprecated"}, ["wfy-suppress"], {})),
    )
    return client


def run(coro):
    return asyncio.run(coro)


async def inside(client, callback):
    async with server._mcp_operation_boundary("test.eol-metadata"):
        return callback()


def expected_suffix(nodes_by_id, uuid):
    _ordered, metrics = build_dock_peek_index(nodes_by_id, uuid)
    return format_dock_peek_metrics(metrics[uuid])


# ---------------------------------------------------------------------------
# The pure pieces.
# ---------------------------------------------------------------------------


def test_is_managed_source_line_shapes():
    assert is_managed_source_line("AST_QUALNAME: mod.func\n---\nx")
    assert is_managed_source_line("MD_PATH: doc.md\n---\nx")
    assert is_managed_source_line("BEACON (id=x\ncomment: hi")
    assert is_managed_source_line("@beacon[\n  id=x\n]")
    assert is_managed_source_line("Path: E:\\repo\\mod.py\n---")
    assert is_managed_source_line("Root: E:\\repo\n---")
    # Not managed: markers below line 1, prose, glued tokens, empty.
    assert not is_managed_source_line("prose first\nAST_QUALNAME: later")
    assert not is_managed_source_line("see Path: notes below")
    assert not is_managed_source_line("Path:glued-no-space")
    assert not is_managed_source_line("")
    assert not is_managed_source_line(None)
    assert not is_managed_source_line("ROOT BODY")


def test_build_render_source_metrics_merges_roots_and_skips_absent():
    rows = rows_by_id()
    before = copy.deepcopy(rows)
    merged = build_render_source_metrics(rows, [R, D])
    _ordered, root_metrics = build_dock_peek_index(rows, R)
    _ordered2, docs_metrics = build_dock_peek_index(rows, D)
    assert merged[R] == root_metrics[R]
    assert merged[D] == docs_metrics[D]
    assert build_render_source_metrics(rows, ["nope", ""]) == {}
    assert build_render_source_metrics(rows, []) == {}
    assert rows == before  # read-only


# ---------------------------------------------------------------------------
# The composer: one vocabulary with dock.peek, exempting managed lines.
# ---------------------------------------------------------------------------


def test_composer_headings_carry_dock_peek_vocabulary_and_exempt_managed_lines():
    rows = rows_by_id()
    index = build_render_source_metrics(rows, [R])
    policy = RenderPolicy(
        direct_child_counts=build_direct_child_counts(rows),
        source_metrics=index,
    )
    text = WorkFlowyClientNexus._build_markdown_preview_from_tree(
        copy.deepcopy(forest()), headings_only=True, render_policy=policy
    )
    # Root (unmanaged, whose subtree contains managed nodes): the FULL
    # four-field vocabulary, byte-identical to dock.peek's formatter.
    assert f"Root #wfy-ns{expected_suffix(rows, R)}" in text
    # Docs: unmanaged parent of a managed (beacon-residue) subtree -> shows.
    assert f"Docs #view{expected_suffix(rows, D)}" in text
    assert f"Plain leaf{expected_suffix(rows, L)}" in text
    # Managed source lines: NO end-of-line metadata at all (not even ▸N).
    for managed_uuid, name in ((F, "mod.py"), (S, "func"), (M, "residue")):
        short = managed_uuid[:8]
        heading = f"[{short}] {name}"
        assert heading in text
        assert f"{heading} ▸" not in text
        assert f"{heading} · desc:" not in text


def test_composer_headings_suffix_is_dock_peeks_own_format():
    rows = rows_by_id()
    index = build_render_source_metrics(rows, [R])
    policy = RenderPolicy(source_metrics=index)
    text = WorkFlowyClientNexus._build_markdown_preview_from_tree(
        copy.deepcopy(forest()), headings_only=True, render_policy=policy
    )
    # The agreement claim, mechanically: the rendered suffix IS the dock.peek
    # formatter's output for the same node (dock.peek rows call the same
    # format_dock_peek_metrics over the same build_dock_peek_index engine).
    assert f"Root #wfy-ns{format_dock_peek_metrics(index[R])}" in text


def test_no_metrics_index_keeps_child_count_only_suffix():
    rows = rows_by_id()
    counts = build_direct_child_counts(rows)
    policy = RenderPolicy(direct_child_counts=counts)
    text = WorkFlowyClientNexus._build_markdown_preview_from_tree(
        copy.deepcopy(forest()), headings_only=True, render_policy=policy
    )
    assert "Root #wfy-ns ▸3" in text
    assert "desc:" not in text


def test_uuid_absent_from_a_present_index_shows_unknown_fields():
    rows = rows_by_id()
    index = build_render_source_metrics(rows, [D])  # R not indexed
    policy = RenderPolicy(source_metrics=index)
    text = WorkFlowyClientNexus._build_markdown_preview_from_tree(
        copy.deepcopy(forest()), headings_only=True, render_policy=policy
    )
    assert "Root #wfy-ns ▸? · desc:? · self:? · tree:?" in text


# ---------------------------------------------------------------------------
# The production surfaces (the contract's covered list).
# ---------------------------------------------------------------------------


def _stage2_case(monkeypatch, kind):
    client = setup(monkeypatch)
    cache = client._cache_view_publisher.capture().nodes_by_id
    from workflowy_mcp.cache_render_spec import RenderProjectionSpec

    spec = RenderProjectionSpec(kind=kind, root_uuid=R, as_markdown=True, headings_only=True)
    result = run(inside(client, lambda: server.render_stage2_filter_sort(
        spec=spec, final_subtree=forest()[0], cache_by_id=cache,
        output_file=None, source_meta={},
    )))
    return result["preview_markdown"]


@pytest.mark.parametrize("kind", ["raw_root", "slice", "lightning_rod"])
def test_stage2_surfaces_show_vocabulary_and_exempt_managed(monkeypatch, kind):
    rows = rows_by_id()
    text = _stage2_case(monkeypatch, kind)
    assert f"Root #wfy-ns{expected_suffix(rows, R)}" in text
    assert f"Docs #view{expected_suffix(rows, D)}" in text
    assert "[aaaaaaaa] mod.py\n" in text
    assert "mod.py ▸" not in text
    assert "mod.py · desc:" not in text
    assert "[bbbbbbbb] func" in text
    assert "func ▸" not in text


@pytest.mark.parametrize("family,verb,params", [
    ("glimpse", "run", {"root_handle": R, "allow_anchor_dock": True, "headings_only": True}),
    ("anchor", "dock.render", {"dock_handle": R, "headings_only": True}),
    ("lightning_rod", "render_tags", {"root_handle": R, "tags": ["view"], "headings_only": True}),
    ("slice_group", "sift", {"lens": {"kind": "subtree", "root_handle": R}, "query": "Docs", "tier": "headings"}),
])
def test_public_family_paths_show_vocabulary_and_exempt_managed(monkeypatch, family, verb, params):
    setup(monkeypatch)
    registry = {
        "glimpse": server.GLIMPSE_REGISTRY,
        "anchor": server.ANCHOR_REGISTRY,
        "lightning_rod": server.LIGHTNING_ROD_REGISTRY,
        "slice_group": server.SLICE_GROUP_REGISTRY,
    }[family]
    result = run(dispatch_operation(
        registry, verb, params,
        operation_boundary=server._mcp_operation_boundary,
        mutation_admission=server._mcp_mutation_admission,
        exception_mapper=server._mcp_exception_mapper,
        result_finalizer=server._mcp_operation_result_finalizer,
    ))
    assert isinstance(result, str), result
    rows = rows_by_id()
    # Every surface: the admitted nodes carry dock.peek's four-field
    # vocabulary (Root's subtree includes the managed nodes -- partial
    # overlap; Docs' subtree includes the beacon-residue child).
    assert f"Root #wfy-ns{expected_suffix(rows, R)}" in result
    assert f"Docs #view{expected_suffix(rows, D)}" in result
    if family in ("glimpse", "anchor"):
        # The full-subtree surfaces also render the managed source lines,
        # which stay bare.
        assert "[aaaaaaaa] mod.py\n" in result
        assert "mod.py ▸" not in result
        assert "mod.py · desc:" not in result
        assert "[bbbbbbbb] func" in result
        assert "func ▸" not in result
