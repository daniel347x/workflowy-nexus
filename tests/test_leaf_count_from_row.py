"""Regression lock for the LEAF COUNT ribbon feature (`_leaf_count_from_row`).

The docked-modal ribbon shows, for the currently-selected tree-view row, the
number of LEAF nodes from that row DOWN to the end of the render order -- i.e.
exactly the ``since="last N"`` value that keeps 'this row and everything below
it'. ``WorkFlowyClientNexus._leaf_count_from_row`` computes it over the FINAL
rendered slice forest, single-sourced on:

  * ``collect_leaf_branches`` (the ONE leaf definition, shared with the sort,
    the ``since`` window pivot, and the ``since`` item-count prune), and
  * the occurrence authority ``_iter_markdown_focus_rows`` (via
    ``_resolve_focus_index_path``) to disambiguate a clicked
    ``(uuid, ancestor_path, occurrence)`` triple to the exact focused node.

'From the row down' = the clicked node's OWN subtree leaves (a contiguous DFS
run) PLUS every leaf after it = ``total_leaves - k`` where ``k`` is the index
of the first leaf-branch passing through the focused node.

These tests assert:
  * whole-document / root / falsy focus -> the total leaf count;
  * a clicked interior row -> its subtree leaves + everything after it;
  * the LAST leaf -> 1; the FIRST leaf -> the total (nothing precedes it);
  * an occurrence-repeated (uuid, ancestor_path) is disambiguated so occ 0 and
    occ 1 give the correct (different) tails;
  * an unresolvable / stale row -> None (caller omits the label).
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


def _node(nid, name, children=None):
    return {"id": nid, "name": name, "note": "", "children": children or []}


# A simple document (unique uuids). DFS leaf order:
#   L1 (under A) , L2 (under B) , L3 (under B) , L4 (under C)
# So total leaves = 4.
#
#   ROOT
#   ├── A            -> L1
#   ├── B            -> L2, L3
#   └── C            -> L4
def _doc():
    return _node("ROOT", "Root", [
        _node("A", "A", [_node("L1", "leaf one")]),
        _node("B", "B", [
            _node("L2", "leaf two"),
            _node("L3", "leaf three"),
        ]),
        _node("C", "C", [_node("L4", "leaf four")]),
    ])


def test_whole_document_focus_returns_total():
    tree = _doc()
    # No uuid -> whole document.
    assert W._leaf_count_from_row(tree, None, []) == 4
    assert W._leaf_count_from_row(tree, "", []) == 4
    # The render root itself (no ancestor path) -> whole document.
    assert W._leaf_count_from_row(tree, "ROOT", []) == 4


def test_first_leaf_returns_total():
    # L1 is the FIRST leaf; nothing precedes it, so 'from here down' == all.
    tree = _doc()
    assert W._leaf_count_from_row(tree, "L1", ["ROOT", "A"]) == 4


def test_last_leaf_returns_one():
    # L4 is the LAST leaf; 'from here down' is just itself.
    tree = _doc()
    assert W._leaf_count_from_row(tree, "L4", ["ROOT", "C"]) == 1


def test_interior_row_counts_its_subtree_plus_everything_after():
    tree = _doc()
    # Click B: its subtree leaves are L2, L3 (a contiguous run), plus L4 after
    # it -> 3. (L1, which precedes B, is excluded.)
    assert W._leaf_count_from_row(tree, "B", ["ROOT"]) == 3
    # Click A: L1 + (L2, L3, L4) after it -> all 4.
    assert W._leaf_count_from_row(tree, "A", ["ROOT"]) == 4
    # Click C: just L4 -> 1.
    assert W._leaf_count_from_row(tree, "C", ["ROOT"]) == 1
    # Click L2 (middle leaf): itself + L4 after it -> 3.
    assert W._leaf_count_from_row(tree, "L2", ["ROOT", "B"]) == 3
    # Click L3: itself + L4 -> 2.
    assert W._leaf_count_from_row(tree, "L3", ["ROOT", "B"]) == 2


# Occurrence-repeated tree: ROOT->A->B appears TWICE as distinct fresh objects
# (what the timestamp-sort trie-merge produces), each B with its own leaf.
# DFS leaf order: L-first (occ0 branch), L-second (occ1 branch). total = 2.
def _repeated():
    return _node("ROOT", "Root", [
        _node("A", "A", [
            _node("B", "B", [_node("L1", "batch one")]),
        ]),
        _node("A", "A", [
            _node("B", "B", [_node("L2", "batch two")]),
        ]),
    ])


def test_occurrence_disambiguation():
    tree = _repeated()
    # Both A occurrences share (uuid='A', ancestor_path=['ROOT']).
    # occ 0 -> first A subtree: its leaf L1 + everything after (L2) -> 2.
    assert W._leaf_count_from_row(tree, "A", ["ROOT"], occurrence=0) == 2
    # occ 1 -> second A subtree: just L2 -> 1.
    assert W._leaf_count_from_row(tree, "A", ["ROOT"], occurrence=1) == 1


def test_unresolvable_row_returns_none():
    tree = _doc()
    # A uuid not present in the tree -> unresolvable -> None (omit the label).
    assert W._leaf_count_from_row(tree, "NOPE", ["ROOT"]) is None
    # A real uuid but a wrong ancestor_path (does not start at the root / does
    # not match any row) -> None rather than a wrong number.
    assert W._leaf_count_from_row(tree, "L2", ["ROOT", "A"]) is None


def test_non_dict_tree_returns_none():
    assert W._leaf_count_from_row(None, "X", []) is None
