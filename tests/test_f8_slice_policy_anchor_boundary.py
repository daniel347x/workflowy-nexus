"""Regression lock for the ANCHOR-BOUNDARY stop-recursion traversal policy.

Background
----------
A raw-root / Lightning-Rod cache projection can be asked to STOP DESCENDING at
child anchors: when it reaches a descendant node that carries an anchor-defining
tag (the live ``note_session_tags`` from extension config, e.g. ``#wfy-ns``), it
must EMIT that child-anchor node as a visible leaf SIGNPOST but NOT recurse into
its contents. This is 'prune AT the boundary, keep the boundary visible' -- NOT
'suppress the boundary and everything under it' (the corrected semantics from
the [ap:SA3NRC] refinement of [ap:TD5WM8]).

The mechanism is a SECOND traversal policy on the ONE shared Phase-1 primitive
``build_selected_subtree_from_cache(boundary_predicate=...)``, orthogonal to
``member_predicate`` (admission) and ``exclude_completed`` (hiding). The pure
predicate factory is ``build_anchor_boundary_predicate(anchor_tags)`` -- an
EXACT (not prefix/family) NAME-tag set-membership test, reusing
``extract_name_tags`` so 'what is an anchor' cannot drift from the anchor
tooling. The extension already stores anchor tags as a LIST, so the whole set
is honored.

These tests lock:
  * the predicate factory: exact match, list of anchor tags, NAME-only,
    empty-list => never-fires, no prefix/family bleed;
  * direct child anchor emitted-but-not-descended (rendered as a leaf);
  * nested (grand-descendant) anchor boundary under a non-anchor branch;
  * ROOT-is-anchor exemption (a boundary root still renders its immediate
    subtree up to the next child anchor);
  * boundary node's OWN descendants stay out even if a descendant would
    independently be an admitted member (materialize also stops at boundary);
  * default (no boundary_predicate) is byte-for-byte the prior raw dump.
"""

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import pytest  # noqa: E402

from workflowy_mcp.f8_slice_policy import (  # noqa: E402
    build_anchor_boundary_predicate,
    build_selected_subtree_from_cache,
    build_slice_subtree_from_cache,
    gather_tags_in_subtree,
    build_stage1_base_tree,
)


# ---------------------------------------------------------------------------
# Cache fixtures. A cache is a flat {id: node} map; each node carries
# id / name / note / parent_id / priority (the shape build_*_from_cache reads).
# ---------------------------------------------------------------------------

def _node(nid, name, parent_id, priority=0, note=None):
    return {
        "id": nid,
        "name": name,
        "note": note,
        "parent_id": parent_id,
        "priority": priority,
    }


def _flat_cache(nodes):
    return {n["id"]: n for n in nodes}


def _child_names(subtree):
    """Return the ordered list of child NAMEs of a materialized subtree node."""
    return [c["name"] for c in (subtree.get("children") or [])]


def _find(subtree, nid):
    """DFS-find a node by id in a materialized subtree (or None)."""
    if subtree is None:
        return None
    if subtree.get("id") == nid:
        return subtree
    for ch in subtree.get("children") or []:
        hit = _find(ch, nid)
        if hit is not None:
            return hit
    return None


ANCHOR = ["wfy-ns"]  # the default anchor tag (a LIST, as the extension stores it)
ALL = lambda node: True  # noqa: E731  (raw-dump member predicate: everything)


# ---------------------------------------------------------------------------
# The predicate factory
# ---------------------------------------------------------------------------

def test_predicate_exact_match_fires():
    pred = build_anchor_boundary_predicate(ANCHOR)
    assert pred({"name": "Sub-anchor #wfy-ns"}) is True
    assert pred({"name": "Plain point"}) is False


def test_predicate_no_prefix_family_bleed():
    # An anchor tag is a SPECIFIC configured tag, matched EXACTLY -- it must NOT
    # match an unrelated longer tag (unlike a slice prefix).
    pred = build_anchor_boundary_predicate(ANCHOR)
    assert pred({"name": "Not an anchor #wfy-nsx"}) is False
    assert pred({"name": "Not an anchor #wfy-ns-personal"}) is False


def test_predicate_honors_full_list():
    pred = build_anchor_boundary_predicate(["wfy-ns", "wfy-anchor"])
    assert pred({"name": "A #wfy-anchor"}) is True
    assert pred({"name": "B #wfy-ns"}) is True
    assert pred({"name": "C #other"}) is False


def test_predicate_name_only_not_note():
    # Anchor membership lives in the NAME trailing-tag region, exactly like the
    # anchor tooling. A tag only in the NOTE body must NOT trigger a boundary.
    pred = build_anchor_boundary_predicate(ANCHOR)
    assert pred({"name": "Plain", "note": "mentions #wfy-ns in prose"}) is False


def test_predicate_empty_list_never_fires():
    for empty in ([], None, ["", "   "]):
        pred = build_anchor_boundary_predicate(empty)
        assert pred({"name": "Sub-anchor #wfy-ns"}) is False


def test_predicate_hash_optional_and_markup_tolerant():
    # Config values may arrive with or without '#'; cache names may carry
    # Workflowy DOM markup around the tag. Both normalize via the shared
    # extract_name_tags / _strip_hash primitives.
    pred = build_anchor_boundary_predicate(["#wfy-ns"])
    assert pred({"name": "X #wfy-ns"}) is True
    assert pred({"name": "Y #<u>wfy-ns</u>"}) is True


# ---------------------------------------------------------------------------
# The traversal: build_selected_subtree_from_cache(boundary_predicate=...)
# ---------------------------------------------------------------------------

def test_direct_child_anchor_emitted_but_not_descended():
    # root
    #   child-anchor  (#wfy-ns)  -> emitted, but its kids pruned
    #     deep-1
    #     deep-2
    #   plain-child
    cache = _flat_cache([
        _node("root", "Root", None, 0),
        _node("ca", "Child anchor #wfy-ns", "root", 0),
        _node("d1", "Deep 1", "ca", 0),
        _node("d2", "Deep 2", "ca", 1),
        _node("pc", "Plain child", "root", 1),
    ])
    pred = build_anchor_boundary_predicate(ANCHOR)
    tree = build_selected_subtree_from_cache(cache, "root", ALL, boundary_predicate=pred)

    # Root shows both children in document order.
    assert _child_names(tree) == ["Child anchor #wfy-ns", "Plain child"]
    # The child anchor is present but a LEAF (no descendants).
    ca = _find(tree, "ca")
    assert ca is not None
    assert ca.get("children") == []
    # Its descendants are entirely absent from the projection.
    assert _find(tree, "d1") is None
    assert _find(tree, "d2") is None


def test_nested_anchor_boundary_under_plain_branch():
    # root
    #   plain
    #     sub-anchor (#wfy-ns) -> emitted as leaf
    #       hidden
    cache = _flat_cache([
        _node("root", "Root", None, 0),
        _node("p", "Plain", "root", 0),
        _node("sa", "Sub anchor #wfy-ns", "p", 0),
        _node("h", "Hidden under sub-anchor", "sa", 0),
    ])
    pred = build_anchor_boundary_predicate(ANCHOR)
    tree = build_selected_subtree_from_cache(cache, "root", ALL, boundary_predicate=pred)

    p = _find(tree, "p")
    assert _child_names(p) == ["Sub anchor #wfy-ns"]
    sa = _find(tree, "sa")
    assert sa.get("children") == []
    assert _find(tree, "h") is None


def test_root_is_anchor_is_exempt():
    # The boundary test is applied only when descending into a CHILD, so a
    # ROOT that is itself an anchor still renders its immediate subtree up to
    # the next child anchor.
    cache = _flat_cache([
        _node("root", "Root anchor #wfy-ns", None, 0),
        _node("a", "Point A", "root", 0),
        _node("b", "Point B", "root", 1),
        _node("nested", "Nested anchor #wfy-ns", "root", 2),
        _node("hidden", "Hidden", "nested", 0),
    ])
    pred = build_anchor_boundary_predicate(ANCHOR)
    tree = build_selected_subtree_from_cache(cache, "root", ALL, boundary_predicate=pred)

    # Root (itself an anchor) is NOT treated as a boundary: its children render.
    assert _child_names(tree) == ["Point A", "Point B", "Nested anchor #wfy-ns"]
    # But a NESTED child anchor IS a boundary -> leaf, descendants pruned.
    assert _find(tree, "nested").get("children") == []
    assert _find(tree, "hidden") is None


def test_boundary_descendants_excluded_even_if_independent_member():
    # A descendant under a boundary anchor would, under a selective
    # member_predicate, be an independent member -- but because collect() never
    # descends past the boundary AND materialize() stops there too, it can
    # never re-attach. Use a selective predicate that WOULD admit "keepme".
    cache = _flat_cache([
        _node("root", "Root", None, 0),
        _node("ca", "Child anchor #wfy-ns", "root", 0),
        _node("keepme", "keepme", "ca", 0),
    ])
    pred = build_anchor_boundary_predicate(ANCHOR)
    member = lambda node: node.get("name") in ("keepme",)  # noqa: E731
    tree = build_selected_subtree_from_cache(cache, "root", member, boundary_predicate=pred)

    # Even though "keepme" satisfies the member predicate, it is under a
    # boundary anchor and must NOT appear; the child anchor also must not be
    # dragged in as a connective ancestor of a (now-absent) member. With no
    # surviving member, only the connective root remains.
    assert _find(tree, "keepme") is None
    ca = _find(tree, "ca")
    # The child anchor is not a member and has no surviving member descendant,
    # so it is not kept by the ancestor-keep pass -> absent.
    assert ca is None
    assert _child_names(tree) == []


def test_default_no_boundary_is_prior_behavior():
    # Without a boundary_predicate the raw dump includes EVERYTHING (the child
    # anchor's descendants are present), proving the policy is opt-in and the
    # default path is byte-for-byte unchanged.
    cache = _flat_cache([
        _node("root", "Root", None, 0),
        _node("ca", "Child anchor #wfy-ns", "root", 0),
        _node("d1", "Deep 1", "ca", 0),
    ])
    tree = build_selected_subtree_from_cache(cache, "root", ALL)  # no boundary
    assert _find(tree, "d1") is not None
    assert _child_names(_find(tree, "ca")) == ["Deep 1"]


def test_raw_dump_wrapper_threads_boundary():
    # build_slice_subtree_from_cache(member_all=True) is the raw-dump wrapper
    # the GLIMPSE root_uuid path uses; confirm it forwards boundary_predicate.
    cache = _flat_cache([
        _node("root", "Root", None, 0),
        _node("ca", "Child anchor #wfy-ns", "root", 0),
        _node("d1", "Deep 1", "ca", 0),
    ])
    pred = build_anchor_boundary_predicate(ANCHOR)
    tree = build_slice_subtree_from_cache(
        cache, "root", [], member_all=True, boundary_predicate=pred
    )
    assert _find(tree, "ca") is not None
    assert _find(tree, "ca").get("children") == []
    assert _find(tree, "d1") is None


def test_gather_tags_threads_boundary_for_same_universe_as_render_tags():
    # Phase C: gather must survey the SAME bounded universe that a later
    # render_tags would filter. Tags under a child anchor are invisible when
    # descend_policy stops at child anchors.
    cache = _flat_cache([
        _node("root", "Root #top", None, 0),
        _node("ca", "Child anchor #wfy-ns #anchor-tag", "root", 0),
        _node("hidden", "Hidden #hidden-tag", "ca", 0),
        _node("plain", "Plain #plain-tag", "root", 1),
    ])
    pred = build_anchor_boundary_predicate(ANCHOR)
    tags = gather_tags_in_subtree(cache, "root", boundary_predicate=pred)
    assert tags == ["anchor-tag", "plain-tag", "top", "wfy-ns"]
    assert "hidden-tag" not in tags


def test_slice_projection_boundary_survives_auto_expand_hydration():
    # The subtle Phase-C crack: Phase 1 can stop at child anchors, but Phase 3
    # --expand hydration reads from the raw cache. If hydration does not honor
    # the same boundary, a tagged parent with --expand silently re-inflates the
    # child-anchor subtree that Phase 1 pruned. Lock that it stays a leaf.
    cache = _flat_cache([
        _node("root", "Root", None, 0),
        _node("parent", "Parent #topic--expand", "root", 0),
        _node("ca", "Child anchor #wfy-ns", "parent", 0),
        _node("hidden", "Hidden under anchor", "ca", 0),
        _node("sibling", "Sibling under parent", "parent", 1),
    ])
    pred = build_anchor_boundary_predicate(ANCHOR)
    tree, plan, meta = build_stage1_base_tree(
        cache,
        "root",
        ["topic"],
        boundary_predicate=pred,
    )
    assert plan is not None
    assert meta == {"pruned_zombie_count": 0}
    parent = _find(tree, "parent")
    assert parent is not None
    assert _child_names(parent) == ["Child anchor #wfy-ns", "Sibling under parent"]
    ca = _find(tree, "ca")
    assert ca is not None
    assert ca.get("children") == []
    assert _find(tree, "hidden") is None
