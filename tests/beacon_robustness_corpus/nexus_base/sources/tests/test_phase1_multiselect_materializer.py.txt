"""Phase 1 multi-select (ap:MULTISEL) — materializer-core locks.

These lock the SERVER core of the docked-modal multi-select feature: the
minimal-covering-subtree render_forest produced by
``materialize_from_document_forest`` for a ``focus={mode:'rows', rows:[...]}``
selection, built by the new ``_multi_focus_render_forest_with_origin`` sibling
of the single-focus builder.

The load-bearing invariants (per the Phase-0.5 baton + the ap:MULTISEL spec):

  * REGRESSION ANCHOR — a ``mode:'rows'`` focus with EXACTLY ONE element is
    byte-for-byte identical (render_forest shape + breadcrumb policy + node
    count) to the ``mode:'row'`` single-select path and to today's output.
  * MINIMAL COVERING SUBTREE — two disjoint selections render the UNION of
    their option-Y breadcrumb spines, each selected node keeping its FULL
    subtree; shared breadcrumb ancestors coalesce.
  * ANCESTOR-ABSORBS-DESCENDANT — selecting a node AND one of its descendants
    collapses to the ancestor (the descendant is inside the ancestor's full
    subtree; the ancestor renders FULL, its body NOT suppressed).
  * OCCURRENCE-EXACT under a timestamp sort — two occurrences of the same uuid
    can be selected independently and each lands on its own subtree.
  * NOTES NEVER BLANKED (v3.1 hazard-a) — every retained note is byte-intact.
  * EXACT-OR-ERROR, ALL-OR-NOTHING (§9) — if ANY selected row fails to resolve
    occurrence-exactly, the whole materialization raises FocusResolutionError
    (never silently drops a row, never degrades to whole-document).
  * document_rows still derived from the UNFOCUSED forest (multi-focus never
    shrinks the top-half list).
  * input immutability — the source forest is never mutated.
"""

from __future__ import annotations

import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from workflowy_mcp.cache_render_spec import (  # noqa: E402
    FocusResolutionError,
    _canonicalize_focus,
)
from workflowy_mcp.client.api_client_nexus import (  # noqa: E402
    WorkFlowyClientNexus as W,
)


# ---------------------------------------------------------------------------
# Helpers (mirror test_phase05_step6_markdown_materializer.py idioms)
# ---------------------------------------------------------------------------
def _node(nid, name, note="", children=None):
    return {"id": nid, "name": name, "note": note, "children": children or []}


def _row(uuid, ancestor_path, occurrence=0):
    return {
        "mode": "row",
        "uuid": uuid,
        "ancestor_path": list(ancestor_path),
        "occurrence": occurrence,
    }


def _rows(*row_triples):
    return {"mode": "rows", "rows": [dict(r) for r in row_triples]}


def _r(uuid, ancestor_path, occurrence=0):
    return {"uuid": uuid, "ancestor_path": list(ancestor_path), "occurrence": occurrence}


def _plain_forest():
    # root
    #  ├─ a (Alpha)
    #  │   ├─ a1
    #  │   └─ a2
    #  └─ b (Beta)
    #      └─ b1
    return [
        _node("root", "Root", "root body", [
            _node("a", "Alpha", "alpha body", [
                _node("a1", "Alpha-1", "a1 body"),
                _node("a2", "Alpha-2", "a2 body"),
            ]),
            _node("b", "Beta", "beta body", [
                _node("b1", "Beta-1", "b1 body"),
            ]),
        ]),
    ]


def _uuids_in(forest):
    out = []
    stack = list(forest)
    # DFS preorder
    def walk(nodes):
        for n in nodes:
            out.append(n["id"])
            walk([c for c in (n.get("children") or []) if isinstance(c, dict)])
    walk(forest)
    return out


# ===========================================================================
# 1) REGRESSION ANCHOR — 1-element rows == single row == byte-identical
# ===========================================================================
def test_canonicalize_single_element_rows_shape():
    canon = _canonicalize_focus(_rows(_r("a1", ["root", "a"], 0)))
    assert canon == {"mode": "rows", "rows": [_r("a1", ["root", "a"], 0)]}


def test_one_element_rows_matches_single_row_render_forest_and_policy():
    forest_single = _plain_forest()
    forest_multi = _plain_forest()

    mrd_single = W.materialize_from_document_forest(
        forest_single, focus=_row("a1", ["root", "a"], 0)
    )
    mrd_multi = W.materialize_from_document_forest(
        forest_multi, focus=_rows(_r("a1", ["root", "a"], 0))
    )

    # render_forest structurally identical (uuids + notes).
    assert _uuids_in(mrd_single.render_forest) == _uuids_in(mrd_multi.render_forest)
    assert mrd_single.render_forest == mrd_multi.render_forest
    # Breadcrumb suppression identical.
    assert (
        mrd_single.policy_by_occurrence.breadcrumb_emit_body_false
        == mrd_multi.policy_by_occurrence.breadcrumb_emit_body_false
    )
    # Focused stats identical.
    assert mrd_single.node_count == mrd_multi.node_count
    assert mrd_single.depth == mrd_multi.depth


# ===========================================================================
# 2) MINIMAL COVERING SUBTREE — two disjoint selections
# ===========================================================================
def test_two_disjoint_selections_cover_both_with_shared_breadcrumb():
    forest = _plain_forest()
    # Select a1 (root/a/a1) and b1 (root/b/b1): disjoint leaves under a shared
    # root breadcrumb. Expect render_forest = root -> {a -> a1, b -> b1}.
    mrd = W.materialize_from_document_forest(
        forest,
        focus=_rows(
            _r("a1", ["root", "a"], 0),
            _r("b1", ["root", "b"], 0),
        ),
    )
    got = set(_uuids_in(mrd.render_forest))
    # Present: the two spines + the leaves' own (leaf) subtrees.
    assert got == {"root", "a", "a1", "b", "b1"}
    # a2 (off-path sibling of a1) is PRUNED (a is a breadcrumb, keeps only a1).
    assert "a2" not in got
    # Breadcrumb bodies suppressed for root/a/b; leaves a1/b1 NOT suppressed.
    p = mrd.policy_by_occurrence
    bc = p.breadcrumb_emit_body_false
    # render coords: root (0,); a (0,0); a1 (0,0,0); b (0,1); b1 (0,1,0).
    assert (0,) in bc          # root breadcrumb
    assert (0, 0) in bc        # a breadcrumb
    assert (0, 1) in bc        # b breadcrumb
    assert (0, 0, 0) not in bc  # a1 leaf full
    assert (0, 1, 0) not in bc  # b1 leaf full


def test_selected_node_keeps_full_subtree():
    forest = _plain_forest()
    # Select a (root/a): a is a FULL node -> keeps a1 AND a2.
    mrd = W.materialize_from_document_forest(
        forest, focus=_rows(_r("a", ["root"], 0))
    )
    got = set(_uuids_in(mrd.render_forest))
    assert got == {"root", "a", "a1", "a2"}
    p = mrd.policy_by_occurrence
    bc = p.breadcrumb_emit_body_false
    # root is a breadcrumb (0,); a is FULL (0,0) -> body NOT suppressed.
    assert (0,) in bc
    assert (0, 0) not in bc


# ===========================================================================
# 3) ANCESTOR-ABSORBS-DESCENDANT
# ===========================================================================
def test_ancestor_and_descendant_selected_collapses_to_ancestor():
    forest = _plain_forest()
    # Select BOTH a (root/a) and a1 (root/a/a1). a already contains a1, so a
    # renders FULL and a1 is subsumed (NOT treated as a separate breadcrumb-
    # terminated leaf). Net render = the same as selecting a alone.
    mrd_both = W.materialize_from_document_forest(
        forest, focus=_rows(_r("a", ["root"], 0), _r("a1", ["root", "a"], 0))
    )
    mrd_anc = W.materialize_from_document_forest(
        _plain_forest(), focus=_rows(_r("a", ["root"], 0))
    )
    assert _uuids_in(mrd_both.render_forest) == _uuids_in(mrd_anc.render_forest)
    # a's body is NOT suppressed (it's a full node), even though a1 was also
    # selected under it.
    bc = mrd_both.policy_by_occurrence.breadcrumb_emit_body_false
    assert (0, 0) not in bc  # a is full
    assert (0,) in bc        # root breadcrumb


# ===========================================================================
# 4) OCCURRENCE-EXACT under a timestamp sort (repeated straddled ancestor)
# ===========================================================================
def _straddle_forest():
    # A file leader that a timestamp sort re-emits: two leaves under the SAME
    # (uuid, ancestor_path) 'file' node land in different sort clusters, so the
    # trie-merge produces TWO physical 'file' occurrences. We simulate the
    # post-sort forest directly (the sort is upstream; the materializer only
    # sees the already-sorted forest).
    return [
        _node("file", "File", "MD_PATH:\nfile.md", [
            _node("h1", "H1", "h1 body"),
        ]),
        _node("file", "File", "MD_PATH:\nfile.md", [
            _node("h2", "H2", "h2 body"),
        ]),
    ]


def test_occurrence_exact_two_occurrences_selected_independently():
    forest = _straddle_forest()
    # Select the SECOND occurrence's leaf (h2 under the 2nd 'file'
    # re-emission, occurrence=1 of ('file', [])).
    mrd = W.materialize_from_document_forest(
        forest, focus=_rows(_r("h2", ["file"], 0))
    )
    got = _uuids_in(mrd.render_forest)
    # Only the second file occurrence + h2 render; h1 (first occurrence) does
    # NOT bleed in.
    assert got == ["file", "h2"]


def test_occurrence_exact_both_occurrences_selected_cover_both():
    forest = _straddle_forest()
    mrd = W.materialize_from_document_forest(
        forest,
        focus=_rows(
            _r("h1", ["file"], 0),
            _r("h2", ["file"], 0),
        ),
    )
    got = _uuids_in(mrd.render_forest)
    # BOTH file occurrences re-emit, each with its own leaf.
    assert got == ["file", "h1", "file", "h2"]


# ===========================================================================
# 5) NOTES NEVER BLANKED (hazard-a)
# ===========================================================================
def test_notes_preserved_byte_for_byte_in_multi_render():
    forest = _plain_forest()
    mrd = W.materialize_from_document_forest(
        forest,
        focus=_rows(_r("a1", ["root", "a"], 0), _r("b1", ["root", "b"], 0)),
    )

    def note_of(uuid):
        found = []
        def walk(nodes):
            for n in nodes:
                if n["id"] == uuid:
                    found.append(n.get("note"))
                walk([c for c in (n.get("children") or []) if isinstance(c, dict)])
        walk(mrd.render_forest)
        return found[0] if found else None

    # Breadcrumb ancestors keep their notes INTACT (body suppression is policy,
    # not tree mutation).
    assert note_of("root") == "root body"
    assert note_of("a") == "alpha body"
    assert note_of("b") == "beta body"
    assert note_of("a1") == "a1 body"
    assert note_of("b1") == "b1 body"


# ===========================================================================
# 6) EXACT-OR-ERROR, ALL-OR-NOTHING (§9)
# ===========================================================================
def test_any_unresolvable_row_raises_all_or_nothing():
    forest = _plain_forest()
    with pytest.raises(FocusResolutionError):
        W.materialize_from_document_forest(
            forest,
            focus=_rows(
                _r("a1", ["root", "a"], 0),          # resolvable
                _r("ghost", ["root", "nope"], 0),    # NOT resolvable
            ),
        )


def test_bad_occurrence_row_raises():
    forest = _plain_forest()
    with pytest.raises(FocusResolutionError):
        W.materialize_from_document_forest(
            forest,
            focus=_rows(_r("a1", ["root", "a"], 5)),  # occurrence 5 doesn't exist
        )


# ===========================================================================
# 7) document_rows from UNFOCUSED forest; input immutability
# ===========================================================================
def test_document_rows_unaffected_by_multi_focus():
    forest = _plain_forest()
    mrd = W.materialize_from_document_forest(
        forest, focus=_rows(_r("a1", ["root", "a"], 0))
    )
    row_uuids = [r["uuid"] for r in mrd.document_rows]
    # The full tree's rows (focus never shrinks the top-half list).
    assert row_uuids == ["root", "a", "a1", "a2", "b", "b1"]


def test_input_forest_never_mutated_by_multi_focus():
    forest = _plain_forest()
    before = copy.deepcopy(forest)
    W.materialize_from_document_forest(
        forest,
        focus=_rows(_r("a1", ["root", "a"], 0), _r("b1", ["root", "b"], 0)),
    )
    assert forest == before


# ===========================================================================
# 8) empty / degenerate rows focus -> whole document
# ===========================================================================
def test_empty_rows_focus_degrades_to_whole_document():
    # A rows focus with no usable rows canonicalizes to None (whole document).
    assert _canonicalize_focus({"mode": "rows", "rows": []}) is None
    forest = _plain_forest()
    mrd = W.materialize_from_document_forest(forest, focus={"mode": "rows", "rows": []})
    assert _uuids_in(mrd.render_forest) == ["root", "a", "a1", "a2", "b", "b1"]
    assert mrd.policy_by_occurrence.breadcrumb_emit_body_false == frozenset()


def test_duplicate_rows_deduped():
    canon = _canonicalize_focus(
        _rows(_r("a1", ["root", "a"], 0), _r("a1", ["root", "a"], 0))
    )
    assert canon == {"mode": "rows", "rows": [_r("a1", ["root", "a"], 0)]}


# ===========================================================================
# 9) END-TO-END through the cache tail (the real glimpse / render_spec="modal"
#    path) -- _glimpse_root_uuid_from_cache with a mode='rows' focus.
# ===========================================================================
from workflowy_mcp import server as srv  # noqa: E402

_PLAIN_CACHE = [
    {"id": "root", "name": "Root", "note": "root body", "parent_id": None, "priority": 0},
    {"id": "a", "name": "Alpha", "note": "alpha body", "parent_id": "root", "priority": 0},
    {"id": "a1", "name": "Alpha-1", "note": "a1 body", "parent_id": "a", "priority": 0},
    {"id": "a2", "name": "Alpha-2", "note": "a2 body", "parent_id": "a", "priority": 1},
    {"id": "b", "name": "Beta", "note": "beta body", "parent_id": "root", "priority": 1},
    {"id": "b1", "name": "Beta-1", "note": "b1 body", "parent_id": "b", "priority": 0},
]


def _client(cache_nodes):
    c = W.__new__(W)
    c._get_nodes_export_cache_nodes = lambda: [dict(n) for n in cache_nodes]
    return c


def _run_root_glimpse(monkeypatch, cache_nodes, root_uuid, *, focus=None):
    client = _client(cache_nodes)
    monkeypatch.setattr(srv, "get_client", lambda: client)
    import asyncio
    return asyncio.run(
        srv._glimpse_root_uuid_from_cache(
            root_uuid=root_uuid,
            as_markdown=True,
            output_file=None,
            sort_by=None,
            sort_direction="asc",
            headings_only=False,
            since=None,
            focus=focus,
        )
    )


def test_e2e_rows_single_element_matches_single_row(monkeypatch):
    # 1-element rows == mode='row' through the REAL cache tail.
    res_row = _run_root_glimpse(
        monkeypatch, _PLAIN_CACHE, "root", focus=_row("a1", ["root", "a"], 0)
    )
    res_rows = _run_root_glimpse(
        monkeypatch, _PLAIN_CACHE, "root", focus=_rows(_r("a1", ["root", "a"], 0))
    )
    assert res_row["success"] is True
    assert res_rows["success"] is True
    assert res_rows["preview_markdown"] == res_row["preview_markdown"]


def test_e2e_rows_two_disjoint_cover_both(monkeypatch):
    res = _run_root_glimpse(
        monkeypatch, _PLAIN_CACHE, "root",
        focus=_rows(_r("a1", ["root", "a"], 0), _r("b1", ["root", "b"], 0)),
    )
    assert res["success"] is True
    md = res["preview_markdown"]
    # Both leaves' bodies present; the off-path sibling a2 is pruned; the
    # breadcrumb ancestors render heading-only (bodies withheld).
    assert "a1 body" in md
    assert "b1 body" in md
    assert "a2 body" not in md
    assert "Alpha-2" not in md
    # Breadcrumb bodies suppressed (root/Alpha/Beta headings present, bodies
    # withheld).
    assert "# Root" in md
    assert "root body" not in md
    assert "alpha body" not in md
    assert "beta body" not in md


def test_e2e_rows_unresolvable_fails_loud(monkeypatch):
    # ALL-OR-NOTHING exact-or-error: one bad row -> stale focus_unresolved,
    # NOT a silent whole-document degrade.
    res = _run_root_glimpse(
        monkeypatch, _PLAIN_CACHE, "root",
        focus=_rows(
            _r("a1", ["root", "a"], 0),
            _r("ghost", ["root", "nope"], 0),
        ),
    )
    assert res.get("focus_unresolved")
    assert res.get("preview_markdown") == ""
    assert "a1 body" not in (res.get("preview_markdown") or "")
