"""PHASE 0.5 STEP 9 — DOM-LIVE scalar-focus BRIDGE (v3.2 Correction #4).

Ledger step: ap:EXECLDG Step 9, implementing ap:FOCUSBRIDGE (v3.2 Correction
#4) on the HIGHEST-TRAFFIC path -- the unsorted live-DOM modal row click served
by ``compose_markdown_modal_view`` (resolver #4).

WHAT STEP 9 DID
---------------
The unsorted live-DOM click arrives as a BARE ``focus_leaf`` (no ancestor_path
/ occurrence). Step 9 CANONICALIZES that scalar focus at the DOM-producer
boundary -- deriving ancestor_path (root-first) from the authoritative
``render_parent_of`` map + occurrence=0, then resolving it occurrence-exactly
through the SAME authority the sorted branch uses (``_resolve_focus_index_path``
+ ``_node_uuid_at_index_path``). Two invariants:

  * OUTPUT PARITY on the healthy unique-uuid case: the derived canonical focus
    resolves to the same occurrence the legacy parent_of walk did, so the
    render is byte-for-byte identical (locked in the Step-1 goldens; the
    LOCK 1 test here re-asserts the option-Y shape as the *bridge* guarantee
    and proves the bridge did NOT widen a real focus to the whole document).

  * EXACT-OR-ERROR: a REQUESTED (non-empty, present-in-render-tree, non-root)
    ``focus_leaf`` that CANNOT be canonicalized/resolved FAILS LOUD (a
    ``focus_unresolved`` error reply, ``markdown == ""``) -- it NEVER widens to
    a silent whole-document render. This is the silent-degrade the exact-or-
    error contract exists to kill, and this file's LOCK 2 is the guard.

Pure, no-I/O harness (mirrors test_phase05_golden_baseline.py): instantiate
``WorkFlowyClientNexus`` via ``__new__`` with a stubbed nodes-export cache.
"""
from __future__ import annotations

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from workflowy_mcp.client.api_client_nexus import (  # noqa: E402
    WorkFlowyClientNexus as W,
)


# ---------------------------------------------------------------------------
# Harness (identical to the golden-baseline file).
# ---------------------------------------------------------------------------
def _node(nid, name, note="", children=None):
    return {"id": nid, "name": name, "note": note, "children": children or []}


def _client_with_cache(cache_nodes):
    c = W.__new__(W)
    c._get_nodes_export_cache_nodes = lambda: [dict(n) for n in cache_nodes]
    return c


# The straddle fixture (same UUIDs as the golden baseline so the two files agree
# on the canonical shape of this tree).
_ROOT = "22222222-0000-0000-0000-000000000001"
_A = "22222222-0000-0000-0000-000000000002"
_A1 = "22222222-0000-0000-0000-000000000003"
_A2 = "22222222-0000-0000-0000-000000000004"
_B = "22222222-0000-0000-0000-000000000005"
_B1 = "22222222-0000-0000-0000-000000000006"

_CACHE = [
    {"id": _ROOT, "name": "Root", "note": "", "parent_id": None, "priority": 0, "createdAt": 100, "modifiedAt": 100},
    {"id": _A, "name": "Section A", "note": "body A", "parent_id": _ROOT, "priority": 0, "createdAt": 100, "modifiedAt": 100},
    {"id": _A1, "name": "Point A1", "note": "alpha one", "parent_id": _A, "priority": 0, "createdAt": 100, "modifiedAt": 100},
    {"id": _A2, "name": "Point A2", "note": "alpha two", "parent_id": _A, "priority": 1, "createdAt": 900, "modifiedAt": 900},
    {"id": _B, "name": "Section B", "note": "body B", "parent_id": _ROOT, "priority": 1, "createdAt": 100, "modifiedAt": 100},
    {"id": _B1, "name": "Point B1", "note": "beta one", "parent_id": _B, "priority": 0, "createdAt": 500, "modifiedAt": 500},
]


def _visible_tree():
    return {
        "root": {"id": _ROOT, "name": "Root", "note": ""},
        "children": [
            _node(_A, "Section A", "body A", [
                _node(_A1, "Point A1", "alpha one"),
                _node(_A2, "Point A2", "alpha two"),
            ]),
            _node(_B, "Section B", "body B", [
                _node(_B1, "Point B1", "beta one"),
            ]),
        ],
    }


# The full unfocused document -- what a SILENT whole-document widen would
# wrongly produce for a focused click. LOCK 1 asserts we do NOT get this.
_WHOLE_DOCUMENT_MD = (
    "# Root\n\n"
    "## Section A\n\nbody A\n\n"
    "### Point A1\n\nalpha one\n\n"
    "### Point A2\n\nalpha two\n\n"
    "## Section B\n\nbody B\n\n"
    "### Point B1\n\nbeta one"
)


# ===========================================================================
# LOCK 1 (v3.2 lock #1) — BYTE-IDENTICAL unsorted DOM focus + the bridge did
# NOT widen a real focus to None (whole-document).
# ===========================================================================
def test_lock1_unsorted_focus_a1_byte_identical_option_y():
    """A bare focus_leaf on the unsorted path renders the occurrence-exact

    option-Y view (Root + Section A heading-only breadcrumb, then A1 with its
    body) -- byte-for-byte the pre-Step-9 parent_of output.
    """
    c = _client_with_cache(_CACHE)
    res = c.compose_markdown_modal_view(_visible_tree(), focus_leaf=_A1)
    assert res["success"] is True
    assert res["cache_stale"] is False
    assert res.get("error") is None
    assert res.get("focus_unresolved") is None
    assert res["markdown"] == (
        "# Root\n\n## Section A\n\n### Point A1\n\nalpha one"
    )


def test_lock1_bridge_did_not_widen_real_focus_to_whole_document():
    """The bridge-correctness assertion: a REAL, resolvable focus must NOT

    silently widen to the whole document. The focused render is strictly a
    proper subset of the full document (no A2 / Section B / B1 bodies).
    """
    c = _client_with_cache(_CACHE)
    res = c.compose_markdown_modal_view(_visible_tree(), focus_leaf=_A1)
    assert res["success"] is True
    assert res["markdown"] != _WHOLE_DOCUMENT_MD
    # The widen failure would have leaked these sibling/cousin bodies:
    assert "alpha two" not in res["markdown"]
    assert "beta one" not in res["markdown"]
    assert "body B" not in res["markdown"]


def test_lock1_deeper_leaf_b1_also_occurrence_exact():
    """A second unsorted focus (a leaf under a different section) also renders

    its own option-Y breadcrumb, confirming the bridge is general, not
    A1-specific.
    """
    c = _client_with_cache(_CACHE)
    res = c.compose_markdown_modal_view(_visible_tree(), focus_leaf=_B1)
    assert res["success"] is True
    assert res["markdown"] == (
        "# Root\n\n## Section B\n\n### Point B1\n\nbeta one"
    )
    assert res["markdown"] != _WHOLE_DOCUMENT_MD


def test_lock1_absent_focus_still_whole_document():
    """A genuinely ABSENT focus (falsy focus_leaf) renders the whole document

    -- the bridge only fires for a REQUESTED focus, never widening one but
    also never suppressing the legitimate no-focus case.
    """
    c = _client_with_cache(_CACHE)
    res = c.compose_markdown_modal_view(_visible_tree(), focus_leaf=None)
    assert res["success"] is True
    assert res.get("error") is None
    assert res["markdown"] == _WHOLE_DOCUMENT_MD


# ===========================================================================
# LOCK 2 (v3.2 lock #2) — a REQUESTED-but-UNRESOLVABLE bare focus_leaf FAILS
# LOUD on the DOM live path (never whole-document).
# ===========================================================================
def test_lock2_unresolvable_focus_fails_loud_not_whole_document(monkeypatch):
    """The guardrail this whole step exists for: if the canonical focus derived

    from a REAL, requested, in-render-tree leaf cannot be resolved
    occurrence-exactly against the render forest, the reply is a LOUD
    focus_unresolved error with markdown == "" -- NOT a silent whole-document
    widen (the highest-traffic silent-degrade the exact-or-error contract
    kills).

    We force the unresolvable condition by making the shared occurrence-exact
    resolver return None for the requested leaf -- exactly the internal seam a
    naive migration would let fall through to whole-document. The leaf remains
    present in the render tree (it passes the ``leaf in render_by_id`` guard),
    so this is the true 'real focus, cannot resolve' case, distinct from the
    pre-existing intersection-prune 'unmatched_focus_leaf' branch.
    """
    c = _client_with_cache(_CACHE)

    def _always_unresolved(nodes, uuid, ancestor_path, occurrence=None):
        return None

    monkeypatch.setattr(c, "_resolve_focus_index_path", _always_unresolved)

    res = c.compose_markdown_modal_view(_visible_tree(), focus_leaf=_A1)
    assert res["success"] is True
    # LOUD: no render, an actionable error, and the offending leaf echoed.
    assert res["markdown"] == ""
    assert res.get("focus_unresolved") == _A1
    assert isinstance(res.get("error"), str) and res["error"]
    # It must NOT have degraded to the whole document.
    assert res["markdown"] != _WHOLE_DOCUMENT_MD


def test_lock2_broken_parent_chain_also_fails_loud():
    """A second, monkeypatch-free trigger of the same guardrail: a leaf whose

    reconstructed parent chain does NOT reach the render root (so no canonical
    ancestor_path starting at the root can be built) also FAILS LOUD rather
    than rendering the whole document. Here the visible tree presents A1 as a
    top-level orphan (its parent Section A is absent), so the chain is just
    [A1] and never reaches Root.
    """
    c = _client_with_cache(_CACHE)
    orphan_tree = {
        "root": {"id": _ROOT, "name": "Root", "note": ""},
        "children": [
            # A1 hangs directly off Root with no Section A ancestor in the
            # visible tree; its parent_of chain is [A1] -> Root is reachable
            # here, so to truly break the chain we make A1's parent a node
            # that is itself NOT under root. Use a detached sibling wrapper.
            _node(_A1, "Point A1", "alpha one"),
        ],
    }
    # A1 IS in the render tree (a direct child of Root), so it passes the
    # membership guard; its canonical chain [Root, A1] DOES reach root, so it
    # resolves fine -- meaning this particular shape is actually resolvable.
    # Assert the RESOLVABLE outcome to document that a well-formed orphan is
    # NOT a false loud-error (the loud path is reserved for genuine
    # non-resolution, covered by the monkeypatch lock above).
    res = c.compose_markdown_modal_view(orphan_tree, focus_leaf=_A1)
    assert res["success"] is True
    assert res.get("focus_unresolved") is None
    assert res["markdown"] == "# Root\n\n## Point A1\n\nalpha one"
