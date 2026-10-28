"""Audit E/WO-1: absent input roots still protect their member notes."""
from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest

from tests.test_lens_scaffold_ancestor_notes import (
    _install_snapshot,
    _lens_spec,
    _node,
    _render_lens_markdown,
)
from workflowy_mcp import server
from workflowy_mcp.lens_engine import lens_scaffold_ancestor_ids


def _cluster_cache():
    return {
        "R": _node("R", "Root", note="R-note"),
        "C": _node("C", "Cluster", "R", 0, "C-note"),
        "D1": _node("D1", "First #probe", "C", 0, "D1-note"),
        "E1": _node("E1", "Deep #probe", "D1", 0, "E1-note"),
        "D2": _node("D2", "Second #probe", "C", 1, "D2-note"),
    }


def _cluster_tree():
    return {"id": "C", "children": [
        {"id": "D1", "children": [{"id": "E1", "children": []}]},
        {"id": "D2", "children": []},
    ]}


@pytest.mark.parametrize("include_input_root", [False, True])
def test_scaffold_pinned_graph_protects_cluster_below_input_root(include_input_root):
    cache = _cluster_cache()
    tree = _cluster_tree()
    if include_input_root:
        tree = {"id": "R", "children": [tree]}
    before = deepcopy((cache, tree))
    assert lens_scaffold_ancestor_ids(tree, ["R"], cache_by_id=cache) == frozenset()
    assert (cache, tree) == before


def test_scaffold_pinned_graph_distant_union_keeps_only_strict_ancestors():
    cache = _cluster_cache()
    cache.update({
        "TOP": _node("TOP", "Top"),
        "S": _node("S", "Other root", "TOP"),
        "S1": _node("S1", "Other leaf", "S"),
    })
    cache["R"]["parent_id"] = "TOP"
    # Neither R nor S needs to appear in the final tree to establish ancestry.
    tree = {"id": "TOP", "children": [_cluster_tree(), {"id": "S1"}]}
    assert lens_scaffold_ancestor_ids(tree, ["R", "S"], cache_by_id=cache) == frozenset({"TOP"})


def test_scaffold_pinned_graph_nested_absent_root_stays_member_region():
    cache = _cluster_cache()
    assert lens_scaffold_ancestor_ids(
        _cluster_tree(), ["R", "E1"], cache_by_id=cache,
    ) == frozenset()


@pytest.mark.parametrize("atom_kind", ["subtree", "tags", "group"])
def test_single_atom_lca_below_input_root_prints_member_notes(monkeypatch, atom_kind):
    cache = _cluster_cache()
    groups = {"Probe": {"id": "Probe", "root_uuid": "R", "prefixes": ["probe"]}}
    if atom_kind == "group":
        lens = {"kind": "group", "group_handle": "Probe"}
    else:
        lens = {"kind": atom_kind, "root_handle": "R"}
        if atom_kind == "tags":
            lens["tags"] = ["probe"]
    spec = _lens_spec(lens=lens, headings_only=False)
    _install_snapshot(monkeypatch, cache, groups)

    async def project():
        snapshot = await server._build_lens_stage0_snapshot(None)
        return await server._project_render_lens_from_cache(spec.lightning, snapshot=snapshot)

    result = _render_lens_markdown(monkeypatch, cache, groups, spec)
    markdown = result["preview_markdown"]
    for note in ("C-note", "D1-note", "E1-note", "D2-note"):
        assert note in markdown
    # No input-root resurrection: the final tree still begins at its real LCA.
    assert "R-note" not in markdown
    projected = asyncio.run(project())
    assert projected["root_uuid"] == "C"
    assert projected["lens_scaffold_ids"] == frozenset()
