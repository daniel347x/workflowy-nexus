"""Phase 0.5 Step 5 (ap:EXECLDG) — the shared materializer CORE, UNWIRED.

Step 5 builds ``MaterializedRenderDocument`` + the shared core
``materialize_from_document_forest`` and unit-tests it over hand-built
forests. NOTHING in production calls it yet (Steps 6/8 repoint the producers);
so there is no parity/regression surface here — these are pure unit locks on
the core's contract.

WHAT THE CORE OWNS (and what these tests lock):
  * document_rows derived from the UNFOCUSED document_forest (focus never
    shrinks the top-half list).
  * focus resolution EXACT-OR-ERROR (§5-Correction#3 / §9): None -> whole
    document; root-row -> whole rooted document; a real singleton focus ->
    occurrence-exact option-Y render_forest; an unresolvable CANONICAL focus
    -> FocusResolutionError (NO whole-document / first-occurrence / uuid-set
    degrade).
  * render_forest with NOTES INTACT (never blanked — v3.1 hazard-a).
  * policy_by_occurrence rebase VALUES from the unpruned document_forest
    (hazard-b immune) RE-KEYED to render-forest coords (v3.3 #1/#2). This is
    the load-bearing subtlety verified empirically during Step 5: a naive
    "recompute over the pruned forest" would lose an off-path sibling's
    shallower depth (and the legacy note-blanking pruner would also destroy
    the file header), so the core computes VALUES over document_forest and
    maps document-index_path -> render-index_path.
  * focused node_count / depth over render_forest.
  * input immutability (document_forest never mutated).
"""

import copy
import os
import sys

import pytest  # noqa: E402

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")
)

from workflowy_mcp.client.api_client_nexus import (  # noqa: E402
    WorkFlowyClientNexus as W,
)
from workflowy_mcp.cache_render_spec import (  # noqa: E402
    MaterializedRenderDocument,
    RenderPolicy,
    FocusResolutionError,
)


# ---------------------------------------------------------------------------
def _node(nid, name, note="", children=None):
    return {"id": nid, "name": name, "note": note, "children": children or []}


def _row_triple(uuid, ancestor_path, occurrence=0):
    return {
        "mode": "row",
        "uuid": uuid,
        "ancestor_path": list(ancestor_path),
        "occurrence": occurrence,
    }


# ===========================================================================
# FIXTURES
# ===========================================================================
def _plain_forest():
    return [
        _node("root", "Root", "root body", [
            _node("a", "Alpha", "alpha body", [
                _node("a1", "Alpha-1", "a1 body"),
                _node("a2", "Alpha-2", "a2 body"),
            ]),
            _node("b", "Beta", "beta body"),
        ]),
    ]


def _hazard_b_file_forest():
    # doc.md: Deep(### =3) -> Deeper(##### =5); Shallow(## =2) OFF-PATH owns
    # the file's shallowest md-depth (2). Focusing Deeper prunes Shallow.
    return [
        _node("fr", "doc.md", "Root: E:\\tmp\\doc.md", [
            _node("deep", "Deep", "MD_PATH:\n### Deep", [
                _node("deeper", "Deeper", "MD_PATH:\n##### Deeper"),
            ]),
            _node("shallow", "Shallow", "MD_PATH:\n## Shallow"),
        ]),
    ]


def _straddle_forest():
    # Two physically-distinct top-level 'A' nodes (the sorted-straddle shape):
    # same uuid + empty ancestor_path => occurrences 0 and 1, DIFFERENT rebase
    # values so a cross-occurrence mismatch would be visible.
    return [
        _node("A", "Alpha", "MD_PATH:\n## Alpha", [
            _node("A1", "A-one", "MD_PATH:\n#### A-one"),
        ]),
        _node("A", "Alpha", "MD_PATH:\n#### Alpha", [
            _node("A2", "A-two", "MD_PATH:\n##### A-two"),
        ]),
    ]


# ===========================================================================
# UNFOCUSED — whole document
# ===========================================================================
def test_unfocused_render_forest_is_document_forest():
    forest = _plain_forest()
    mrd = W.materialize_from_document_forest(forest, focus=None)
    assert isinstance(mrd, MaterializedRenderDocument)
    # Whole document: render == document (by value).
    assert mrd.render_forest == mrd.document_forest == forest
    assert mrd.focus is None
    # node_count = all 5 nodes; depth = 3 levels (root/alpha/a1).
    assert mrd.node_count == 5
    assert mrd.depth == 3


def test_unfocused_document_rows_full_triples():
    forest = _plain_forest()
    mrd = W.materialize_from_document_forest(forest, focus=None)
    got = [(r["uuid"], tuple(r["ancestor_path"]), r["occurrence"], r["depth"])
           for r in mrd.document_rows]
    assert got == [
        ("root", (), 0, 0),
        ("a", ("root",), 0, 1),
        ("a1", ("root", "a"), 0, 2),
        ("a2", ("root", "a"), 0, 2),
        ("b", ("root",), 0, 1),
    ]


def test_unfocused_rebase_policy_matches_document_policy():
    forest = _hazard_b_file_forest()
    mrd = W.materialize_from_document_forest(forest, focus=None)
    # Unfocused => render coords == document coords => identical rebase map.
    assert (
        mrd.policy_by_occurrence.rebase_by_render_key
        == W._compute_rebase_policy_for_forest(forest).rebase_by_render_key
    )
    assert isinstance(mrd.policy_by_occurrence, RenderPolicy)


# ===========================================================================
# ROOT-ROW focus — whole rooted document (no prune)
# ===========================================================================
def test_root_row_focus_is_whole_rooted_document():
    forest = _plain_forest()
    mrd = W.materialize_from_document_forest(
        forest, focus=_row_triple("root", [], 0)
    )
    # Root-row focus: NOT pruned — render == document.
    assert mrd.render_forest == forest
    assert mrd.node_count == 5


# ===========================================================================
# SINGLETON focus — option-Y, notes intact, focused stats
# ===========================================================================
def test_singleton_focus_option_y_shape_and_notes_intact():
    forest = _plain_forest()
    # Focus a1 (root/a/a1). Breadcrumb: root, a (heading-only in RENDER, but
    # NOTES PRESERVED here — suppression is later policy). a1 keeps its subtree.
    mrd = W.materialize_from_document_forest(
        forest, focus=_row_triple("a1", ["root", "a"], 0)
    )
    rf = mrd.render_forest
    assert len(rf) == 1
    root = rf[0]
    assert root["id"] == "root"
    # ⭐ NOTES INTACT (v3.1 hazard-a): breadcrumb ancestors keep their notes.
    assert root["note"] == "root body"
    assert root["children"][0]["id"] == "a"
    assert root["children"][0]["note"] == "alpha body"
    # Breadcrumb kept ONLY the on-path child (a2/b pruned).
    assert len(root["children"]) == 1
    assert len(root["children"][0]["children"]) == 1
    a1 = root["children"][0]["children"][0]
    assert a1["id"] == "a1"
    assert a1["note"] == "a1 body"
    # render_forest has 3 nodes (root, a, a1); depth 3.
    assert mrd.node_count == 3
    assert mrd.depth == 3
    # document_rows unchanged — focus does NOT shrink the top-half list.
    assert len(mrd.document_rows) == 5


# ===========================================================================
# ⭐ THE LOAD-BEARING LOCK — hazard-b rebase re-keying (values from document,
# keys in render coords). Focusing a deep node prunes the off-path sibling
# that owns the file's shallowest depth; the file-node rebase baseline MUST
# still be 2 (from the unpruned document), NOT 3 (a pruned recompute).
# ===========================================================================
def test_hazard_b_rebase_value_survives_focus_prune_via_rekeying():
    forest = _hazard_b_file_forest()
    mrd = W.materialize_from_document_forest(
        forest, focus=_row_triple("deeper", ["fr", "deep"], 0)
    )
    # The file node 'fr' is render_forest top-level (render index_path (0,)).
    # Its rebase baseline MUST be 2 (owned by the pruned-away off-path
    # 'Shallow'), proving VALUES came from the unpruned document_forest and
    # were RE-KEYED to render coords — NOT recomputed over the pruned tree.
    assert mrd.policy_by_occurrence.rebase_base_for((0,)) == 2

    # Contrast: a naive recompute over the (note-preserving) render_forest
    # would give 3, because 'Shallow' (depth 2) was pruned away. We assert the
    # core did NOT do that.
    naive = W._compute_rebase_policy_for_forest(mrd.render_forest)
    assert naive.rebase_base_for((0,)) == 3
    assert mrd.policy_by_occurrence.rebase_base_for((0,)) != naive.rebase_base_for((0,))

    # And notes are intact on the pruned file/heading breadcrumb (so a later
    # rebase/abs-path consumer still sees the Root:/MD_PATH: headers).
    assert mrd.render_forest[0]["note"] == "Root: E:\\tmp\\doc.md"
    assert mrd.render_forest[0]["children"][0]["note"] == "MD_PATH:\n### Deep"


# ===========================================================================
# SORTED STRADDLE — occurrence-exact focus picks the RIGHT physical node
# ===========================================================================
def test_straddle_focus_occurrence_0_picks_first_alpha():
    forest = _straddle_forest()
    mrd = W.materialize_from_document_forest(
        forest, focus=_row_triple("A", [], 0)
    )
    # occ 0 => the FIRST Alpha. It is a TOP-LEVEL node (empty ancestor_path),
    # so its resolved focus index_path is length-1. VERIFIED against the
    # composer: a length-1 focus index_path prunes NOTHING (there are no
    # ancestors ABOVE a top-level node, so no breadcrumb / no off-path prune)
    # -- the composer renders the WHOLE forest for such a focus. The
    # materializer faithfully mirrors that: render_forest == document_forest.
    # (This is the same 'root-row => whole rooted document' branch; it applies
    # to ANY top-level occurrence, matching composer behavior, NOT a special
    # case for occurrence 0.)
    assert mrd.render_forest == forest  # length-1 focus => nothing to prune
    # The focused stats cover the whole document (both Alphas).
    assert mrd.node_count == 4


def test_straddle_deep_focus_occurrence_exact_value():
    # Focus a DEEP node under the SECOND Alpha so it's a real (non-root) focus
    # and occurrence-exactness + re-keying both matter.
    forest = _straddle_forest()
    mrd = W.materialize_from_document_forest(
        forest, focus=_row_triple("A2", ["A"], 0)
    )
    # render_forest: second Alpha (#### =4) -> A2 (##### =5). The file/heading
    # baseline for the SECOND Alpha is 4 (its own subtree min), re-keyed to
    # render coords (0,). NOT 2 (the first Alpha's value).
    assert mrd.policy_by_occurrence.rebase_base_for((0,)) == 4
    assert mrd.render_forest[0]["id"] == "A"
    assert mrd.render_forest[0]["note"] == "MD_PATH:\n#### Alpha"  # 2nd Alpha
    assert mrd.render_forest[0]["children"][0]["id"] == "A2"


# ===========================================================================
# EXACT-OR-ERROR — an unresolvable CANONICAL focus fails LOUD
# ===========================================================================
def test_unresolvable_canonical_focus_raises():
    forest = _plain_forest()
    with pytest.raises(FocusResolutionError):
        W.materialize_from_document_forest(
            forest, focus=_row_triple("does-not-exist", ["root"], 0)
        )


def test_wrong_occurrence_canonical_focus_raises():
    forest = _straddle_forest()
    # 'A2' under 'A' exists at occurrence 0 only; occurrence 5 is unresolvable.
    with pytest.raises(FocusResolutionError):
        W.materialize_from_document_forest(
            forest, focus=_row_triple("A2", ["A"], 5)
        )


def test_malformed_focus_is_none_whole_document_no_raise():
    forest = _plain_forest()
    # A malformed focus (no uuid) canonicalizes to None => whole document,
    # never an error (only a CANONICAL focus can be stale).
    mrd = W.materialize_from_document_forest(
        forest, focus={"mode": "row", "uuid": "", "ancestor_path": []}
    )
    assert mrd.focus is None
    assert mrd.render_forest == forest


def test_materialized_document_owns_shadow_forest_no_input_aliasing():
    """Materialized forests are fresh node dicts, not aliases of caller input.

    This closes the small audit finding from the post-Step-5 review: a frozen
    dataclass alone does not deep-freeze mutable dict/list fields. The
    materializer must own a shadow forest so later render/serializer code cannot
    accidentally mutate the caller's input by mutating mrd.document_forest /
    mrd.render_forest.
    """
    forest = _plain_forest()
    mrd = W.materialize_from_document_forest(forest, focus=None)
    assert mrd.document_forest == forest
    assert mrd.document_forest is not forest
    assert mrd.document_forest[0] is not forest[0]
    assert mrd.document_forest[0]["children"][0] is not forest[0]["children"][0]

    # Mutating the materialized copy must not mutate caller input.
    mrd.document_forest[0]["name"] = "MUTATED COPY"
    assert forest[0]["name"] == "Root"


# ===========================================================================
# INPUT IMMUTABILITY
# ===========================================================================
def test_document_forest_never_mutated():
    forest = _hazard_b_file_forest()
    before = copy.deepcopy(forest)
    W.materialize_from_document_forest(
        forest, focus=_row_triple("deeper", ["fr", "deep"], 0)
    )
    assert forest == before


def test_focused_stats_zero_for_empty_forest():
    mrd = W.materialize_from_document_forest([], focus=None)
    assert mrd.node_count == 0
    assert mrd.depth == 0
    assert mrd.render_forest == []
    assert mrd.document_rows == []
