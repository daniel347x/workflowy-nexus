"""Phase 0.5 Step 12b (ap:S12BREMAIN) — sub-commit 4: the CONSOLIDATED

exact-or-error-EVERYWHERE lock.

The Dan-signed-off contract (§5-Correction#3 / §9): a REQUESTED CANONICAL row
focus ({mode:'row', uuid, ancestor_path, occurrence} with a real uuid, NOT a
root-click) that CANNOT be resolved occurrence-exactly against the render tree
FAILS LOUD — it NEVER silently degrades to a whole-document render, a
first-occurrence render, or a uuid-set wrong-breadcrumb render. Only a genuinely
ABSENT focus (None / malformed) or a ROOT-CLICK focus renders the whole document.

This module pins that invariant on the two surfaces exercisable as direct
(non-async) Python calls:

  * DOM-LIVE modal (``compose_markdown_modal_view``) — BOTH the sorted branch
    and the unsorted scalar-focus bridge. (Highest-traffic path.)
  * (The materializer CORE ``materialize_from_document_forest`` — the shared
    engine all four surfaces funnel through — is asserted to RAISE
    ``FocusResolutionError`` directly, which is the single root guarantee that
    makes every surface's loud behavior structural rather than incidental.)

The other two surfaces are locked by their own existing suites:
  * cache-tail ``render_stage2_filter_sort`` — tests/test_glimpse_focus_feature2.py
    (test_unresolvable_focus_fails_loud) + tests/test_glimpse_focus_node_count.py
    (test_unresolvable_focus_fails_loud_zero_count).
  * DOM-capture ``_glimpse_dispatch_dom_capture`` — its exact-or-error (mod #6,
    Step 8) is locked by tests/test_glimpse_render_spec_modal_dom_capture.py.
"""

import pytest

from workflowy_mcp.client.api_client_nexus import WorkFlowyClientNexus as W


def _client_with_cache(cache_nodes):
    c = W.__new__(W)
    c._get_nodes_export_cache_nodes = lambda: [dict(n) for n in cache_nodes]
    return c


def _node(uuid, name, note, children=None):
    return {"id": uuid, "name": name, "note": note, "children": children or []}


_ROOT = "44444444-0000-0000-0000-000000000001"
_A = "44444444-0000-0000-0000-000000000002"
_A1 = "44444444-0000-0000-0000-000000000003"
_B = "44444444-0000-0000-0000-000000000004"

_CACHE = [
    {"id": _ROOT, "name": "Root", "note": "", "parent_id": None, "priority": 0, "createdAt": 100, "modifiedAt": 100},
    {"id": _A, "name": "Section A", "note": "body A", "parent_id": _ROOT, "priority": 0, "createdAt": 100, "modifiedAt": 100},
    {"id": _A1, "name": "Point A1", "note": "alpha one", "parent_id": _A, "priority": 0, "createdAt": 100, "modifiedAt": 100},
    {"id": _B, "name": "Section B", "note": "body B", "parent_id": _ROOT, "priority": 1, "createdAt": 100, "modifiedAt": 100},
]


def _visible_tree():
    return {
        "root": {"id": _ROOT, "name": "Root", "note": ""},
        "children": [
            _node(_A, "Section A", "body A", [_node(_A1, "Point A1", "alpha one")]),
            _node(_B, "Section B", "body B"),
        ],
    }


_WHOLE_DOC = (
    "# Root\n\n## Section A\n\nbody A\n\n### Point A1\n\nalpha one\n\n"
    "## Section B\n\nbody B"
)


# ===========================================================================
# ROOT GUARANTEE: the shared materializer core RAISES on a canonical
# unresolvable focus (no lenient degrade anywhere it is used).
# ===========================================================================
def test_materializer_core_raises_on_unresolvable_canonical_focus():
    from workflowy_mcp.cache_render_spec import FocusResolutionError

    tree = _node(_ROOT, "Root", "", [
        _node(_A, "Section A", "body A", [_node(_A1, "Point A1", "alpha one")]),
    ])
    bad = {"mode": "row", "uuid": "no-such-uuid", "ancestor_path": [_ROOT], "occurrence": 0}
    with pytest.raises(FocusResolutionError):
        W.materialize_from_document_forest([tree], focus=bad)


def test_materializer_core_none_focus_renders_whole_document():
    tree = _node(_ROOT, "Root", "", [
        _node(_A, "Section A", "body A", [_node(_A1, "Point A1", "alpha one")]),
    ])
    mrd = W.materialize_from_document_forest([tree], focus=None)
    # render_forest == document_forest (whole doc), node_count covers all 3.
    assert mrd.focus is None
    assert mrd.node_count == 3


# ===========================================================================
# DOM-LIVE modal — UNSORTED scalar-focus bridge branch.
# ===========================================================================
def test_dom_live_unsorted_absent_leaf_not_requested_renders_whole_doc():
    # A falsy focus_leaf is ABSENT (not requested) -> whole document (§9).
    c = _client_with_cache(_CACHE)
    res = c.compose_markdown_modal_view(_visible_tree(), focus_leaf=None)
    assert res["success"] is True
    assert res.get("focus_unresolved") is None
    assert res["markdown"] == _WHOLE_DOC


def test_dom_live_unsorted_healthy_focus_resolves():
    # Sanity: a well-formed bare focus_leaf resolves (does NOT go loud).
    c = _client_with_cache(_CACHE)
    res = c.compose_markdown_modal_view(_visible_tree(), focus_leaf=_A1)
    assert res["success"] is True and res.get("focus_unresolved") is None
    assert res["markdown"] == "# Root\n\n## Section A\n\n### Point A1\n\nalpha one"


# ===========================================================================
# DOM-LIVE modal — SORTED branch: a MALFORMED (unresolvable) ancestor_path
# for a real requested leaf FAILS LOUD (never a wrong render, never whole-doc).
# ===========================================================================
def test_dom_live_sorted_unresolvable_fails_loud():
    c = _client_with_cache(_CACHE)
    res = c.compose_markdown_modal_view(
        _visible_tree(), focus_leaf=_A1,
        # A1's real parent is A; claim it under B -> cannot resolve.
        focus_ancestor_path=[_ROOT, _B], focus_occurrence=0,
    )
    assert res["success"] is True
    assert res.get("focus_unresolved") == _A1
    assert res["markdown"] == ""
    # Categorically NOT a whole-document degrade.
    assert res["markdown"] != _WHOLE_DOC


def test_dom_live_root_click_renders_whole_document():
    # A ROOT-CLICK focus (root uuid, empty ancestor_path) is the whole rooted
    # document — the §9 exception, NOT an error.
    c = _client_with_cache(_CACHE)
    res = c.compose_markdown_modal_view(_visible_tree(), focus_leaf=_ROOT)
    assert res["success"] is True
    assert res.get("focus_unresolved") is None
    assert res["markdown"] == _WHOLE_DOC
