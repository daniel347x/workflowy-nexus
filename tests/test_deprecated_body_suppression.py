"""Regression lock for DEPRECATED-BODY SUPPRESSION (ap:DEPBOD).

Background
----------
When a node carrying the ``--deprecated`` slice postfix SURVIVES into the final
cache-projected render tree, it renders HEADER-ONLY: its heading (node name)
still renders, but its NOTE BODY is withheld. A deprecated node only ever
survives in ONE case -- it is a RESCUED deprecated node (it has at least one
LIVE, non-deprecated, in-slice descendant, so Phase 2's rescue rule does NOT add
it to the contraction set). It therefore reaches the composer intact and, before
this feature, rendered header+body. New behavior: keep the node in document
order as an ancestor/context signpost while its real current content lives in a
subtree below it.

The mechanism is a DERIVED UUID SET (``body_suppressed_ids``) computed ONCE in
Phase 2 (``build_expansion_plan``), threaded to the ONE composer
(``_build_markdown_preview_from_tree``) as a side-channel render directive
(exactly like ``pending_collapse_ids`` / ``focus_index_path`` travel BESIDE the
tree) -- NOT per-node flags, NOT note mutation, NOT a RenderProjectionSpec /
render_scope field.

SCOPE (falls out for free): applies ONLY where Phase 2 runs = the
cache-projected SLICE paths. The raw root_uuid dump, Lightning Rod, and live-DOM
paths never produce the set, so a title containing '--deprecated' never triggers
suppression there.

ROOT INCLUSION (Dan-approved): the set is computed INDEPENDENTLY of the
collapse/expand root-discard; if the render ROOT is a surviving deprecated node
its body IS suppressed (heading / abs-path leader still render).

These tests cover the 8 cases enumerated in the ap:DEPB02 spec.
"""
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from workflowy_mcp.client.api_client_nexus import (  # noqa: E402
    WorkFlowyClientNexus as W,
)
from workflowy_mcp.f8_slice_policy import (  # noqa: E402
    build_expansion_plan,
    build_stage1_base_tree,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _mk(nid, name, parent, prio, note=None):
    """A flat /nodes-export-style cache node."""
    return {
        "id": nid,
        "name": name,
        "note": note,
        "parent_id": parent,
        "priority": prio,
    }


def _nested(nid, name, children=None, note=""):
    """A nested subtree node (the shape build_expansion_plan consumes)."""
    return {"id": nid, "name": name, "note": note, "children": children or []}


DEP = "deprecated"  # DEFAULT_DEPRECATED_POSTFIX
PREF = ["topic"]    # a single base slice prefix used across the tests


def _plan(root):
    return build_expansion_plan(root, PREF)


# ===========================================================================
# CASE 1 -- deprecated LEAF with NO live descendant => collapses (Phase 4
#           zombie); NOT a body-suppression node.
# ===========================================================================

def test_case1_deprecated_leaf_no_live_descendant_collapses_not_body_suppressed():
    # Root is a plain member; a deprecated leaf hangs off it with nothing live
    # beneath it. Phase 2 must add it to the DEPRECATED CONTRACTION set (it is
    # a collapse target), and it must NOT appear in body_suppressed_ids.
    root = _nested("root", "Root #topic", [
        _nested("dep", "Dead #topic--deprecated", note="stale body text"),
    ])
    plan = _plan(root)
    assert "dep" in plan["collapse_deprecated"]
    assert "dep" not in plan["body_suppressed_ids"]


# ===========================================================================
# CASE 2 -- deprecated ANCESTOR with a live child => ancestor is body-
#           suppressed (survives, header-only); live child untouched.
# ===========================================================================

def _rescued_ancestor_tree_nested():
    # dep is deprecated but has a LIVE (#topic, non-deprecated) child, so it is
    # RESCUED (not collapsed) and must be body-suppressed.
    return _nested("root", "Root #topic", [
        _nested("dep", "Superseded ancestor #topic--deprecated",
                children=[
                    _nested("live", "Current child #topic", note="LIVE-CHILD-BODY"),
                ],
                note="OLD-ANCESTOR-BODY"),
    ])


def test_case2_plan_marks_rescued_ancestor_body_suppressed():
    plan = _plan(_rescued_ancestor_tree_nested())
    # It is NOT collapsed (rescued by the live child) ...
    assert "dep" not in plan["collapse_deprecated"]
    assert "dep" not in plan["collapse_suppressed"]
    # ... and IS flagged for body suppression; the live child is NOT.
    assert "dep" in plan["body_suppressed_ids"]
    assert "live" not in plan["body_suppressed_ids"]


def test_case2_composer_renders_ancestor_header_not_body_live_child_full():
    tree = _rescued_ancestor_tree_nested()
    plan = _plan(tree)
    md = W._build_markdown_preview_from_tree(
        [tree], suppress_metadata=True,
        body_suppressed_ids=set(plan["body_suppressed_ids"]),
    )
    # ancestor HEADING renders; ancestor BODY does NOT.
    assert "Superseded ancestor" in md
    assert "OLD-ANCESTOR-BODY" not in md
    # live child header + body render.
    assert "Current child" in md
    assert "LIVE-CHILD-BODY" in md


def test_case2_without_the_set_body_would_render_control():
    # Control: with body_suppressed_ids absent (the old behavior), the ancestor
    # body renders -- proving the suppression is what withholds it.
    tree = _rescued_ancestor_tree_nested()
    md = W._build_markdown_preview_from_tree([tree], suppress_metadata=True)
    assert "OLD-ANCESTOR-BODY" in md


# ===========================================================================
# CASE 3 -- deprecated ancestor whose NOTE is EMBEDDED MARKDOWN (headings in
#           the note) => heading renders, embedded-Markdown body does NOT.
#           Guards the _md_content_body BYPASS specifically.
# ===========================================================================

def test_case3_embedded_markdown_body_of_deprecated_ancestor_is_suppressed():
    # dep's NOTE is itself Markdown (has a heading), which normally takes the
    # _md_content_body EMBED branch (bypassing _emit_node_note). The
    # suppression must gate THAT branch too.
    embedded = "## Embedded heading\n\nEMBEDDED-MD-BODY paragraph."
    tree = _nested("root", "Root #topic", [
        _nested("dep", "Superseded md-ancestor #topic--deprecated",
                children=[_nested("live", "Live child #topic", note="LIVE-BODY")],
                note=embedded),
    ])
    plan = _plan(tree)
    assert "dep" in plan["body_suppressed_ids"]
    md = W._build_markdown_preview_from_tree(
        [tree], suppress_metadata=True,
        body_suppressed_ids=set(plan["body_suppressed_ids"]),
    )
    assert "Superseded md-ancestor" in md          # heading present
    assert "Embedded heading" not in md            # embedded body withheld
    assert "EMBEDDED-MD-BODY" not in md
    assert "Live child" in md and "LIVE-BODY" in md  # live child intact


def test_case3_control_embedded_markdown_renders_without_suppression():
    embedded = "## Embedded heading\n\nEMBEDDED-MD-BODY paragraph."
    tree = _nested("root", "Root #topic", [
        _nested("dep", "Superseded md-ancestor #topic--deprecated",
                children=[_nested("live", "Live child #topic", note="LIVE-BODY")],
                note=embedded),
    ])
    md = W._build_markdown_preview_from_tree([tree], suppress_metadata=True)
    # Without the set, the embedded-Markdown body IS embedded.
    assert "Embedded heading" in md or "EMBEDDED-MD-BODY" in md


# ===========================================================================
# CASE 4 -- full-document render vs per-row render (render_slice_row) => BOTH
#           suppress the same deprecated ancestor body (the consistency
#           guarantee). Exercised via the shared _render_slice_row_view helper.
# ===========================================================================

def test_case4_full_document_and_per_row_agree():
    tree = _rescued_ancestor_tree_nested()
    plan = _plan(tree)
    bset = set(plan["body_suppressed_ids"])

    full = W._build_markdown_preview_from_tree(
        [tree], suppress_metadata=True, body_suppressed_ids=bset,
    )
    assert "OLD-ANCESTOR-BODY" not in full

    # Per-row render of the deprecated ancestor row (option-Y focus on 'dep').
    # It must ALSO withhold the ancestor body.
    row = W._render_slice_row_view(
        tree, "dep", ["root"], occurrence=0, body_suppressed_ids=bset,
    )
    assert "Superseded ancestor" in row
    assert "OLD-ANCESTOR-BODY" not in row
    # The live child (inside dep's subtree, which the focus includes) keeps body.
    assert "LIVE-CHILD-BODY" in row


# ===========================================================================
# CASE 5 -- SNAPSHOT-style per-row prerender ALSO suppresses. (The snapshot
#           loop calls the SAME _render_slice_row_view per row; assert the
#           root row's option-Y view -- which IS the full document -- suppresses
#           the ancestor body just like the direct full-document render.)
# ===========================================================================

def test_case5_snapshot_root_row_view_suppresses():
    tree = _rescued_ancestor_tree_nested()
    plan = _plan(tree)
    bset = set(plan["body_suppressed_ids"])
    # The root row's option-Y view (focus == root) is the whole document.
    root_row_view = W._render_slice_row_view(
        tree, "root", [], occurrence=0, body_suppressed_ids=bset,
    )
    assert "Superseded ancestor" in root_row_view
    assert "OLD-ANCESTOR-BODY" not in root_row_view
    assert "LIVE-CHILD-BODY" in root_row_view


# ===========================================================================
# CASE 6 -- raw dump / Lightning Rod / live-DOM: a title containing
#           '--deprecated' does NOT trigger suppression (those paths never run
#           Phase 2, so body_suppressed_ids is None/empty at the composer).
# ===========================================================================

def test_case6_no_set_means_no_suppression_even_for_deprecated_title():
    # Simulate the raw-dump/Lightning/live-DOM call: composer invoked with NO
    # body_suppressed_ids. A node titled '--deprecated' must render its body.
    tree = _nested("root", "Root #topic", [
        _nested("dep", "Looks deprecated #topic--deprecated",
                children=[_nested("live", "Live #topic", note="X")],
                note="RAWDUMP-BODY"),
    ])
    md_none = W._build_markdown_preview_from_tree([tree], suppress_metadata=True)
    assert "RAWDUMP-BODY" in md_none
    md_empty = W._build_markdown_preview_from_tree(
        [tree], suppress_metadata=True, body_suppressed_ids=set(),
    )
    assert "RAWDUMP-BODY" in md_empty


# ===========================================================================
# CASE 7 -- CUSTOM deprecated postfix (non-default word) => Phase 2 still
#           computes the set (uses the live postfix); composer stays oblivious.
# ===========================================================================

def test_case7_custom_deprecated_postfix():
    # Use a non-default deprecated postfix word 'retired'.
    tree = _nested("root", "Root #topic", [
        _nested("dep", "Superseded #topic--retired",
                children=[_nested("live", "Live #topic", note="LIVE")],
                note="OLD-BODY"),
    ])
    plan = build_expansion_plan(tree, PREF, deprecated_postfix="retired")
    assert "dep" in plan["body_suppressed_ids"]
    md = W._build_markdown_preview_from_tree(
        [tree], suppress_metadata=True,
        body_suppressed_ids=set(plan["body_suppressed_ids"]),
    )
    assert "Superseded" in md
    assert "OLD-BODY" not in md
    assert "LIVE" in md
    # A default-word '--deprecated' tag with a CUSTOM postfix configured is NOT
    # treated as deprecated (so it would NOT be body-suppressed) -- proves the
    # live postfix drives it.
    tree2 = _nested("root", "Root #topic", [
        _nested("d2", "NotReally #topic--deprecated",
                children=[_nested("l2", "Live #topic", note="L2")],
                note="BODY2"),
    ])
    plan2 = build_expansion_plan(tree2, PREF, deprecated_postfix="retired")
    assert "d2" not in plan2["body_suppressed_ids"]


# ===========================================================================
# CASE 8 -- ROOT is a surviving deprecated node => root BODY suppressed, root
#           HEADING (+ any leader) still renders (root-inclusion approved).
# ===========================================================================

def test_case8_root_is_surviving_deprecated_node():
    # The render root itself is deprecated but rescued by a live child.
    root = _nested("root", "Root superseded #topic--deprecated", [
        _nested("live", "Live child #topic", note="LIVE-BODY"),
    ], note="ROOT-BODY")
    plan = _plan(root)
    # Root-inclusive: root IS in the body-suppression set (NOT discarded like
    # the operation-target sets).
    assert "root" in plan["body_suppressed_ids"]
    # And it is NOT an operation target.
    assert "root" not in plan["collapse_deprecated"]
    assert "root" not in plan["collapse_suppressed"]
    md = W._build_markdown_preview_from_tree(
        [root], suppress_metadata=True,
        body_suppressed_ids=set(plan["body_suppressed_ids"]),
    )
    assert "Root superseded" in md      # root heading renders
    assert "ROOT-BODY" not in md        # root body suppressed
    assert "Live child" in md and "LIVE-BODY" in md


# ===========================================================================
# END-TO-END -- build_stage1_base_tree carries body_suppressed_ids inside
#               `plan`, and a deprecated ancestor UNDER A SUPPRESS/DEPRECATED
#               collapse ancestor is NOT in the set (it is pruned entirely).
# ===========================================================================

def test_end_to_end_funnel_returns_body_suppressed_ids():
    cache = {
        "root": _mk("root", "Root #topic", None, 0),
        "dep": _mk("dep", "Superseded #topic--deprecated", "root", 1,
                   note="OLD-BODY"),
        "live": _mk("live", "Live #topic", "dep", 1, note="LIVE-BODY"),
    }
    final_subtree, plan, _prune = build_stage1_base_tree(
        cache, "root", ["topic"],
    )
    assert final_subtree is not None
    assert "dep" in plan["body_suppressed_ids"]


def test_deprecated_under_collapse_ancestor_not_in_set():
    # A deprecated node whose ANCESTOR is a suppress collapse root is dropped
    # entirely by Phase 3 -- it must NOT appear in body_suppressed_ids (the
    # 'no contraction ancestor' clause of the definition).
    root = _nested("root", "Root #topic", [
        _nested("sup", "Suppressed region #topic--suppress", children=[
            _nested("dep", "Dep under suppress #topic--deprecated",
                    children=[_nested("live", "Live #topic", note="L")],
                    note="B"),
        ]),
    ])
    plan = _plan(root)
    assert "sup" in plan["collapse_suppressed"]
    assert "dep" not in plan["body_suppressed_ids"]
