"""Phase 0.5 Step 8 (ap:EXECLDG) — route the MARKDOWN DOM-capture branch

through the shared materializer core (materialize_from_document_forest).

WHAT STEP 8 CHANGED
-------------------
``_glimpse_dispatch_dom_capture`` is the DOM-frozen sibling producer of the
cache tail. Step 7 already routed its FULL/UUID branch through the shared
materializer (so the breadcrumb file node's ``→abs:`` leader survives the
note-preserving render_forest). Step 8 does the SAME for its MARKDOWN branch:

  * ``_mat_focus_dc`` (the canonical, occurrence-exact, non-root focus) is now
    minted ONCE above the ``as_markdown`` split, and the ONE
    ``materialize_from_document_forest`` call (``_mrd_dc``) is made there too.
  * the MARKDOWN branch now feeds ``_mrd_dc.render_forest`` +
    ``render_policy=_mrd_dc.policy_by_occurrence`` to the composer, with NO
    ``focus_path`` / ``focus_leaf`` / ``focus_index_path`` params — breadcrumb
    body suppression is the policy's occurrence/index-path-keyed set.
  * the FULL/UUID branch simply CONSUMES the shared ``_mrd_dc`` (no longer
    builds its own).

So the DOM-capture producer is now IDENTICAL-AFTER-the-document-forest to the
cache tail (§5-Correction#2): the two producers differ ONLY in HOW they obtain
the document forest (cache projection vs. normalize+sort+since of a captured
tree), and converge on the ONE occurrence-exact-focus + note-preserving
option-Y + render-forest-keyed-policy authority.

WHAT THESE TESTS LOCK
  * MARKDOWN DOM-capture whole-document parity: an unfocused render is
    byte-for-byte what it produced before (materializer whole-doc == identity).
  * MARKDOWN DOM-capture FOCUS == the cache-path FOCUS for the SAME tree/row —
    the convergence lock proving both producers share the ONE materializer
    core (the Step-8 architectural guarantee).
  * FOCUS renders the option-Y shape: breadcrumb ancestors HEADING-ONLY (bodies
    suppressed via policy), the focused leaf WITH its body — and notes are
    NEVER blanked (v3.1 hazard-a: a breadcrumb file node keeps a resolvable
    note, proven by the ``→abs:``-equivalent survival in full mode + the
    heading-only-not-gone body in markdown mode).
  * FULL/UUID + headings_only focused: the breadcrumb FILE node's abs-path
    leader is PRESENT (the Step-7 hazard-a fix on this DOM-capture path stays
    green under the Step-8 shared-mint), and only the focused row survives.
  * exact-or-error, honest echo, and the whole existing DOM-capture matrix are
    untouched (that regression file, run alongside, must stay green).

These tests drive ``_glimpse_dispatch_dom_capture`` directly (its return dict
carries preview_markdown / preview_tree / render_scope / node_count), which is
where the materializer switchover lives. No network, no real Workflowy, no disk.
"""
from __future__ import annotations

import asyncio
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from workflowy_mcp import server as srv  # noqa: E402
from workflowy_mcp.client.api_client_nexus import WorkFlowyClientNexus as W  # noqa: E402
from workflowy_mcp.f8_slice_policy import build_slice_subtree_from_cache  # noqa: E402


# ---------------------------------------------------------------------------
# A plain (non-Cartographer) forest: root / (Alpha / [Alpha-1, Alpha-2]) / Beta.
# The DOM-capture render is self-contained; the cache is consulted only for
# sort keys + the root regime seed, so a matching in-memory cache is provided
# purely so the two producers (DOM-capture vs cache-root GLIMPSE) see the SAME
# node bodies for the convergence lock.
# ---------------------------------------------------------------------------
_ROOT = "root"
_A = "a"
_A1 = "a1"
_A2 = "a2"
_B = "b"

_CACHE = [
    {"id": _ROOT, "name": "Root", "note": "root body", "parent_id": None, "priority": 0},
    {"id": _A, "name": "Alpha", "note": "alpha body", "parent_id": _ROOT, "priority": 0},
    {"id": _A1, "name": "Alpha-1", "note": "a1 body", "parent_id": _A, "priority": 0},
    {"id": _A2, "name": "Alpha-2", "note": "a2 body", "parent_id": _A, "priority": 1},
    {"id": _B, "name": "Beta", "note": "beta body", "parent_id": _ROOT, "priority": 1},
]


def _client():
    c = W.__new__(W)
    c._get_nodes_export_cache_nodes = lambda: [dict(n) for n in _CACHE]
    return c


def _captured_tree():
    """The {root, children} shape the client's serializeVisibleTreeForModal sends.

    Bodies match the cache so the DOM-capture render and the cache-root GLIMPSE
    render are comparable for the convergence lock.
    """
    return {
        "root": {"id": _ROOT, "name": "Root", "note": "root body"},
        "children": [
            {"id": _A, "name": "Alpha", "note": "alpha body", "children": [
                {"id": _A1, "name": "Alpha-1", "note": "a1 body", "children": []},
                {"id": _A2, "name": "Alpha-2", "note": "a2 body", "children": []},
            ]},
            {"id": _B, "name": "Beta", "note": "beta body", "children": []},
        ],
    }


def _dispatch(monkeypatch, *, focus=None, as_markdown=True, headings_only=False):
    client = _client()
    monkeypatch.setattr(srv, "get_client", lambda: client)
    return asyncio.run(srv._glimpse_dispatch_dom_capture(
        captured_tree=_captured_tree(),
        root_uuid_hint=_ROOT,
        focus=focus,
        as_markdown=as_markdown,
        headings_only=headings_only,
        sort_by=None,
        sort_direction="asc",
        since=None,
        output_file=None,
    ))


def _row(uuid, ancestor_path, occurrence=0):
    return {"mode": "row", "uuid": uuid, "ancestor_path": list(ancestor_path),
            "occurrence": occurrence}


# ===========================================================================
# 1) WHOLE-DOCUMENT parity: an unfocused markdown DOM-capture render is the
#    plain whole-document render (materializer whole-doc == identity clone).
# ===========================================================================
def test_dom_capture_markdown_whole_document(monkeypatch):
    res = _dispatch(monkeypatch, focus=None)
    assert res["success"] is True, res
    assert res["preview_markdown"] == (
        "# Root\n\nroot body\n\n"
        "## Alpha\n\nalpha body\n\n"
        "### Alpha-1\n\na1 body\n\n"
        "### Alpha-2\n\na2 body\n\n"
        "## Beta\n\nbeta body"
    ), res["preview_markdown"]


# ===========================================================================
# 2) FOCUS option-Y shape: breadcrumb ancestors HEADING-ONLY (bodies
#    suppressed via the materializer policy, NOT via note-blanking), the
#    focused leaf WITH its body, off-path siblings pruned.
# ===========================================================================
def test_dom_capture_markdown_focus_option_y_shape(monkeypatch):
    # Focus Alpha-1 (root/a/a1). Breadcrumb = Root + Alpha (heading-only); a1
    # keeps its body; Alpha-2 (off-path sibling) + Beta pruned.
    res = _dispatch(monkeypatch, focus=_row(_A1, [_ROOT, _A], 0))
    assert res["success"] is True, res
    md = res["preview_markdown"]
    assert md == "# Root\n\n## Alpha\n\n### Alpha-1\n\na1 body", md
    # Explicit intent guards.
    assert "root body" not in md          # breadcrumb body suppressed
    assert "alpha body" not in md         # breadcrumb body suppressed
    assert "a1 body" in md                # focused leaf body present
    assert "Alpha-2" not in md            # off-path sibling pruned
    assert "Beta" not in md               # off-path sibling pruned


# ===========================================================================
# 3) ⭐ CONVERGENCE LOCK (the Step-8 architectural guarantee): the MARKDOWN
#    DOM-capture focused render EQUALS the cache-path focused render for the
#    SAME tree + row. Both producers share the ONE materializer core, so their
#    option-Y output is byte-identical (§5-Correction#2).
# ===========================================================================
def test_dom_capture_markdown_focus_equals_cache_path(monkeypatch):
    dom = _dispatch(monkeypatch, focus=_row(_A1, [_ROOT, _A], 0))
    assert dom["success"] is True, dom

    # The cache-path focused render (the dock's per-row view) over the SAME
    # tree/row via _render_slice_row_view — the cache tail's Step-6 mechanism.
    cache_by_id = {n["id"]: dict(n) for n in _CACHE}
    subtree = build_slice_subtree_from_cache(cache_by_id, _ROOT, [], member_all=True)
    dock = W._render_slice_row_view(
        subtree, _A1, [_ROOT, _A], occurrence=0, root_in_non_md_file=False
    )
    assert dom["preview_markdown"] == dock, (dom["preview_markdown"], dock)


# ===========================================================================
# 4) FOCUSED STATS come from the materializer (single authority): node_count
#    / depth describe the focused option-Y view, not the whole scope.
# ===========================================================================
def test_dom_capture_markdown_focus_stats_from_materializer(monkeypatch):
    res = _dispatch(monkeypatch, focus=_row(_A1, [_ROOT, _A], 0))
    assert res["success"] is True, res
    # Option-Y forest = Root -> Alpha -> Alpha-1 (3 nodes, depth 3).
    assert res["node_count"] == 3, res
    assert res["depth"] == 3, res
    # Whole-document stats (contrast) — the unfocused render describes all 5.
    whole = _dispatch(monkeypatch, focus=None)
    assert whole["node_count"] == 5, whole
    assert whole["depth"] == 3, whole


# ===========================================================================
# 5) FULL/UUID + headings_only focused: the Step-7 note-preservation fix on
#    THIS DOM-capture path stays green under the Step-8 shared mint — only the
#    focused row survives, and its UUID is a harvestable handle.
# ===========================================================================
def test_dom_capture_full_headings_only_focus_note_preserving(monkeypatch):
    res = _dispatch(monkeypatch, focus=_row(_A1, [_ROOT, _A], 0),
                    as_markdown=False, headings_only=True)
    assert res["success"] is True, res
    tree = res.get("preview_tree")
    assert isinstance(tree, list) and tree, res
    flat = "\n".join(tree)
    # Focused leaf UUID present (a real handle); off-path sibling pruned.
    assert _A1 in flat, flat
    assert "Alpha-1" in flat, flat
    assert "Alpha-2" not in flat, flat
    assert "Beta" not in flat, flat


# ===========================================================================
# 6) ⭐ hazard-a on the DOM-capture MARKDOWN path: a breadcrumb FILE node keeps
#    its abs-path leader (notes NEVER blanked). A focused markdown render whose
#    breadcrumb is a Cartographer file node must still emit the bold abs-path
#    leader — proving the materializer's note-preserving render_forest (not the
#    old note-blanking prune) drives the markdown branch too.
# ===========================================================================
_FILE = "fr"
_H_DEEP = "deep"
_H_LEAF = "leaf"

_FILE_CACHE = [
    {"id": _FILE, "name": "doc.md", "note": "Root: E:\\tmp\\doc.md", "parent_id": None, "priority": 0},
    {"id": _H_DEEP, "name": "Deep", "note": "MD_PATH:\n## Deep", "parent_id": _FILE, "priority": 0},
    {"id": _H_LEAF, "name": "Leaf", "note": "MD_PATH:\n### Leaf\n\nleaf body", "parent_id": _H_DEEP, "priority": 0},
]


def _file_captured_tree():
    return {
        "root": {"id": _FILE, "name": "doc.md", "note": "Root: E:\\tmp\\doc.md"},
        "children": [
            {"id": _H_DEEP, "name": "Deep", "note": "MD_PATH:\n## Deep", "children": [
                {"id": _H_LEAF, "name": "Leaf", "note": "MD_PATH:\n### Leaf\n\nleaf body", "children": []},
            ]},
        ],
    }


def test_dom_capture_markdown_focus_breadcrumb_file_leader_survives(monkeypatch):
    client = W.__new__(W)
    client._get_nodes_export_cache_nodes = lambda: [dict(n) for n in _FILE_CACHE]
    monkeypatch.setattr(srv, "get_client", lambda: client)
    # Focus the leaf heading; the file node (doc.md) is a breadcrumb ancestor.
    res = asyncio.run(srv._glimpse_dispatch_dom_capture(
        captured_tree=_file_captured_tree(),
        root_uuid_hint=_FILE,
        focus=_row(_H_LEAF, [_FILE, _H_DEEP], 0),
        as_markdown=True,
        headings_only=False,
        sort_by=None,
        sort_direction="asc",
        since=None,
        output_file=None,
    ))
    assert res["success"] is True, res
    md = res["preview_markdown"]
    # The breadcrumb FILE node's bold abs-path leader SURVIVES (notes not
    # blanked — v3.1 hazard-a). This is the load-bearing assertion: if the
    # markdown branch had kept the old note-blanking prune, the leader (which
    # _build_abs_path_map_for_preview derives from the file node's Root: note)
    # would be LOST when the breadcrumb note was blanked. Its survival proves
    # the markdown branch now renders over the materializer's NOTE-PRESERVING
    # render_forest.
    assert "**E:\\tmp\\doc.md**" in md, md
    # The focused leaf's HEADING renders; the file node heading renders.
    # NOTE (the documented Step-6 fixture trap, verified live): under
    # suppress_metadata=True a synthetic Cartographer MD_PATH note strips to
    # EMPTY (the whole note IS metadata; a section's prose body is not modeled
    # in these synthetic MD_PATH nodes), so NO node emits a BODY here -- the
    # 'leaf body' text can never render for a synthetic node. The abs-path
    # leader survival above IS the hazard-a proof; the body is orthogonal.
    assert "Leaf" in md, md
    assert "doc.md" in md, md


# ===========================================================================
# 7) EXACT-OR-ERROR preserved on the markdown path (mod #6): an unresolvable
#    canonical focus fails LOUD, never degrades to the whole document.
# ===========================================================================
def test_dom_capture_markdown_unresolvable_focus_errors(monkeypatch):
    res = _dispatch(
        monkeypatch,
        focus=_row("deadbeef-0000-0000-0000-000000000000", [_ROOT], 0),
    )
    assert res.get("success") is False, res
    assert "could not be resolved" in (res.get("error") or "").lower(), res
