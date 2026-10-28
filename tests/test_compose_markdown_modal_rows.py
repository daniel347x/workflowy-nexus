"""Regression tests for ``compose_markdown_modal_rows`` (FIX 2 corollary ⑥b).

The FROZEN-DOM-SINGLE ordered-rows endpoint. In FROZEN-DOM-SINGLE mode a large
DOM tree is rendered ONE row per click on demand from a tree CAPTURED at freeze
time (no all-rows snapshot -- that compose is what hung 10-15s+ and timed out
into per-row 'server unavailable'). But the top-half tree-view list still needs
a server-authoritative ORDER + per-row identity triples, so the client asks for
``mode='rows_only'`` -> ``compose_markdown_modal_rows``.

What these lock in (the invariants FIX 2 depends on):
  * rows are ALWAYS emitted -- sorted AND unsorted (document order) -- because
    the caller has no live DOM to walk; a sort<->none toggle just re-fetches
    here and always gets a correct list (this is the key divergence from the
    snapshot path, which omits rows when unsorted);
  * ``render_keyed_by`` is always 'row' and every row carries the full
    ``(uuid, ancestor_path, occurrence)`` identity triple the per-row single
    render needs for occurrence-exact focus;
  * NO per-row markdown is composed (the whole point -- O(N), no hang): the
    reply shape carries ``rows`` but never a ``renders`` map;
  * a ``created`` sort reorders the rows vs document order (it delegates to the
    same ``_build_modal_render_context`` sort the snapshot/single paths use);
  * the rows for the SAME tree are identical to ``_build_modal_rows`` over the
    render root -- i.e. the endpoint cannot drift from the shared authority;
  * a malformed ``visible_tree`` fails soft (success False, rows None).

Pure, no-I/O seam: an instance whose ``_get_nodes_export_cache_nodes`` is
stubbed to a fixed cache (the same pattern the other modal tests use).
"""
from __future__ import annotations

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from workflowy_mcp.client.api_client_nexus import WorkFlowyClientNexus  # noqa: E402


_ROOT = "cccccccc-0000-0000-0000-000000000001"
_A = "cccccccc-0000-0000-0000-000000000002"
_A1 = "cccccccc-0000-0000-0000-000000000003"
_A2 = "cccccccc-0000-0000-0000-000000000004"
_B = "cccccccc-0000-0000-0000-000000000005"
_B1 = "cccccccc-0000-0000-0000-000000000006"

# createdAt chosen so a `created` ASC leaf sort differs from document order:
# document-order leaves are A1, A2, B1; by createdAt ASC they are B1, A2, A1.
_CACHE_NODES = [
    {"id": _ROOT, "name": "Captured Root", "note": "", "parent_id": None, "priority": 0, "createdAt": 100, "modifiedAt": 100},
    {"id": _A, "name": "Section A", "note": "body A", "parent_id": _ROOT, "priority": 0, "createdAt": 100, "modifiedAt": 100},
    {"id": _A1, "name": "Point A1", "note": "alpha one", "parent_id": _A, "priority": 0, "createdAt": 900, "modifiedAt": 900},
    {"id": _A2, "name": "Point A2", "note": "alpha two", "parent_id": _A, "priority": 1, "createdAt": 500, "modifiedAt": 500},
    {"id": _B, "name": "Section B", "note": "body B", "parent_id": _ROOT, "priority": 1, "createdAt": 100, "modifiedAt": 100},
    {"id": _B1, "name": "Point B1", "note": "beta one", "parent_id": _B, "priority": 0, "createdAt": 200, "modifiedAt": 200},
]


def _make_client():
    c = WorkFlowyClientNexus.__new__(WorkFlowyClientNexus)
    c._get_nodes_export_cache_nodes = lambda: [dict(n) for n in _CACHE_NODES]
    return c


def _visible_tree():
    """The nested {root, children} payload the browser would send for the

    CAPTURED tree (document order, all nodes present)."""
    def n(nid, name, note, children=None):
        return {"id": nid, "name": name, "note": note, "children": children or []}

    return {
        "root": {"id": _ROOT, "name": "Captured Root", "note": ""},
        "children": [
            n(_A, "Section A", "body A", [
                n(_A1, "Point A1", "alpha one"),
                n(_A2, "Point A2", "alpha two"),
            ]),
            n(_B, "Section B", "body B", [
                n(_B1, "Point B1", "beta one"),
            ]),
        ],
    }


def test_rows_only_emits_rows_unsorted_document_order():
    """Unsorted: rows are STILL emitted (document order), keyed 'row', with the

    identity triple -- the divergence from the snapshot path that lets the
    frozen-single caller (which has no live DOM) always get a list.
    """
    c = _make_client()
    res = c.compose_markdown_modal_rows(_visible_tree(), sort_by=None)
    assert res["success"] is True
    assert res["render_keyed_by"] == "row"
    assert "renders" not in res  # NO per-row markdown compose (no hang)
    rows = res["rows"]
    assert isinstance(rows, list) and rows
    # every row carries the identity triple
    for r in rows:
        assert "uuid" in r and "depth" in r and "name" in r
        assert isinstance(r["ancestor_path"], list)
        assert isinstance(r["occurrence"], int)
    order = [r["uuid"] for r in rows]
    # document order: root, A, A1, A2, B, B1
    assert order == [_ROOT, _A, _A1, _A2, _B, _B1]


def test_rows_only_created_sort_reorders_leaves():
    """A `created` ASC sort reorders vs document order (delegates to the same

    render-context sort the snapshot/single paths use). Leaf order by createdAt
    ASC is B1 (200), A2 (500), A1 (900).
    """
    c = _make_client()
    res = c.compose_markdown_modal_rows(
        _visible_tree(), sort_by="created", sort_direction="asc"
    )
    assert res["success"] is True
    assert res["render_keyed_by"] == "row"
    leaf_order = [r["uuid"] for r in res["rows"] if r["uuid"] in (_A1, _A2, _B1)]
    assert leaf_order == [_B1, _A2, _A1]
    # sanity: unsorted leaf order is the document order, so the sort changed it
    unsorted = c.compose_markdown_modal_rows(_visible_tree(), sort_by=None)
    unsorted_leaves = [r["uuid"] for r in unsorted["rows"] if r["uuid"] in (_A1, _A2, _B1)]
    assert unsorted_leaves == [_A1, _A2, _B1]
    assert leaf_order != unsorted_leaves


def test_rows_only_matches_build_modal_rows_authority():
    """The endpoint's rows equal _build_modal_rows over the same render root --

    it cannot drift from the shared row-identity authority.
    """
    c = _make_client()
    ctx = c._build_modal_render_context(_visible_tree(), sort_by="created", sort_direction="asc")
    # Pass the SAME cache_by_id the production compose path uses so the
    # authority carries the additive per-row created/modified timestamp fields
    # (Aug 2026 timeline/hover metadata); without it the two drift ONLY on
    # those additive fields.
    expected = WorkFlowyClientNexus._build_modal_rows(
        ctx["render_root"], cache_by_id=ctx["cache_by_id"]
    )
    got = c.compose_markdown_modal_rows(
        _visible_tree(), sort_by="created", sort_direction="asc"
    )["rows"]
    assert got == expected


def test_rows_only_malformed_tree_fails_soft():
    """A malformed visible_tree fails soft: success False, rows None (never a

    raise), so the ribbon can surface it without crashing the dock.
    """
    c = _make_client()
    res = c.compose_markdown_modal_rows(None)
    assert res["success"] is False
    assert res["rows"] is None
