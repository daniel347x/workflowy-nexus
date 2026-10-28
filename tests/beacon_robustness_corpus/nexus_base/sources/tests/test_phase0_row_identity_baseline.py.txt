"""PHASE 0 BASELINE LOCKS — universal row-identity consolidation (pre-multi-select).

WHY THIS FILE EXISTS
--------------------
Before consolidating modal ROW IDENTITY onto ONE server-authored, occurrence-
exact contract (the pre-phase for the forthcoming multi-select modal focus),
we FREEZE the *current* observable behavior of the row-identity + snapshot
surfaces as golden-output locks. The consolidation is claimed to be
"behavior-preserving"; these tests are how that claim is PROVEN rather than
assumed. Anything the consolidation deliberately changes must change a lock
HERE, visibly, with Dan's sign-off — never silently.

The three surfaces locked:
  1. ``_iter_markdown_focus_rows`` / ``_build_modal_rows`` — the shared row
     enumerator (order, depth, the (uuid, ancestor_path, occurrence) triple,
     empty-id sentinel transparency, private ``_index_path`` stripping).
  2. The ROW-KEY string contract — the byte-for-byte format that MUST stay
     identical across the server ``_slice_row_key``, the snapshot inlined
     ``_row_key``, and the JS ``sliceRowKey``. Phase 0 makes this key
     UNIVERSAL, so locking the exact bytes now guards the contract.
  3. The DOM-modal snapshot keying SEAM as it exists TODAY:
       * UNSORTED  -> ``render_keyed_by == "uuid"``, ``rows`` OMITTED,
                      ``renders`` keyed by bare uuid.
       * SORTED    -> ``render_keyed_by == "row"``, ``rows`` PRESENT,
                      ``renders`` keyed by the row-triple key.
     ⚠️ THIS IS THE EXACT SEAM PHASE 0 WILL REMOVE (making rows + row-keying
     universal). These two tests are therefore CHANGE-DETECTORS: when Phase 0
     lands, the UNSORTED expectations here are expected to FLIP to the sorted
     shape. Until then they document — and pin — the status quo.

Same pure, no-I/O harness the other modal tests use: instantiate via
``__new__`` and stub ``_get_nodes_export_cache_nodes``.
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
# Fixtures — a small tree with a straddled ancestor a `created` sort re-emits.
# ---------------------------------------------------------------------------
_ROOT = "aaaaaaaa-0000-0000-0000-000000000001"
_A = "aaaaaaaa-0000-0000-0000-000000000002"
_A1 = "aaaaaaaa-0000-0000-0000-000000000003"
_A2 = "aaaaaaaa-0000-0000-0000-000000000004"
_B = "aaaaaaaa-0000-0000-0000-000000000005"
_B1 = "aaaaaaaa-0000-0000-0000-000000000006"

# createdAt chosen so a `created` ASC leaf sort INTERLEAVES A's two leaves
# around B1: A1=100, B1=500, A2=900 -> sorted leaf order A1, B1, A2. Because
# B1 (under Section B) sits BETWEEN A's two leaves, the trie-merge must emit
# Section A, then Section B, then RE-EMIT Section A a second time -- so A
# recurs with occurrence 0 and 1. This straddle (not mere adjacency) is the
# whole reason occurrence is load-bearing in row identity; an earlier fixture
# left A's leaves adjacent post-sort and A did NOT recur.
_CACHE_NODES = [
    {"id": _ROOT, "name": "Captured Root", "note": "", "parent_id": None, "priority": 0, "createdAt": 100, "modifiedAt": 100},
    {"id": _A, "name": "Section A", "note": "body A", "parent_id": _ROOT, "priority": 0, "createdAt": 100, "modifiedAt": 100},
    {"id": _A1, "name": "Point A1", "note": "alpha one", "parent_id": _A, "priority": 0, "createdAt": 100, "modifiedAt": 100},
    {"id": _A2, "name": "Point A2", "note": "alpha two", "parent_id": _A, "priority": 1, "createdAt": 900, "modifiedAt": 900},
    {"id": _B, "name": "Section B", "note": "body B", "parent_id": _ROOT, "priority": 1, "createdAt": 100, "modifiedAt": 100},
    {"id": _B1, "name": "Point B1", "note": "beta one", "parent_id": _B, "priority": 0, "createdAt": 500, "modifiedAt": 500},
]


def _make_client():
    c = W.__new__(W)
    c._get_nodes_export_cache_nodes = lambda: [dict(n) for n in _CACHE_NODES]
    return c


def _visible_tree():
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


# ===========================================================================
# 1. SHARED ROW ENUMERATOR — order / depth / identity triple / privacy
# ===========================================================================
def test_build_modal_rows_document_order_depth_and_triple():
    """Unsorted document order, depths, and the public identity triple."""
    root = W._normalize_modal_visible_tree(_visible_tree())
    rows = W._build_modal_rows(root)
    assert [r["uuid"] for r in rows] == [_ROOT, _A, _A1, _A2, _B, _B1]
    assert [r["depth"] for r in rows] == [0, 1, 2, 2, 1, 2]
    # Every row carries the FULL public triple and NOTHING private.
    for r in rows:
        assert set(r.keys()) == {"uuid", "depth", "name", "ancestor_path", "occurrence"}
    # A leaf's ancestor_path is root-first, self-excluded.
    a1 = next(r for r in rows if r["uuid"] == _A1)
    assert a1["ancestor_path"] == [_ROOT, _A]
    assert a1["occurrence"] == 0


def test_enumerator_index_path_is_private_and_present():
    """The enumerator carries the private _index_path; _build_modal_rows strips it."""
    root = W._normalize_modal_visible_tree(_visible_tree())
    recs = W._iter_markdown_focus_rows([root])
    assert recs and all("_index_path" in rec for rec in recs)
    rows = W._build_modal_rows(root)
    assert rows and all("_index_path" not in r for r in rows)


def test_empty_id_sentinel_is_transparent_in_ancestor_path():
    """An empty-id 'Home' sentinel consumes an index slot but never a path uuid."""
    stree = {
        "id": "", "name": "Home", "note": "",
        "children": [
            {"id": _ROOT, "name": "Captured Root", "note": "", "children": [
                {"id": _A1, "name": "Point A1", "note": "alpha one", "children": []},
            ]},
        ],
    }
    recs = W._iter_markdown_focus_rows([stree])
    a1 = next(r for r in recs if r["uuid"] == _A1)
    # Sentinel absent from ancestor_path ...
    assert a1["ancestor_path"] == [_ROOT]
    # ... but its index_path routes THROUGH the sentinel's child slot.
    assert a1["_index_path"][0] == 0  # top-level = the sentinel
    assert W._node_uuid_at_index_path([stree], a1["_index_path"]) == _A1


def test_created_sort_reemits_straddled_ancestor_as_two_occurrences():
    """The `created` sort straddles Section A, so A recurs with occurrence 0 and 1.

    This is the core reason occurrence is load-bearing in row identity: the SAME
    (uuid, ancestor_path) pair appears twice and is disambiguated ONLY by
    occurrence + the private index_path.
    """
    c = _make_client()
    cache = {n["id"]: dict(n) for n in _CACHE_NODES}
    reordered = c._reorder_tree_by_timestamp(
        [W._normalize_modal_visible_tree(_visible_tree())],
        sort_by="created", direction="asc", cache_by_id=cache,
    )
    assert reordered and len(reordered) == 1
    recs = W._iter_markdown_focus_rows(reordered)
    a_occs = sorted(r["occurrence"] for r in recs if r["uuid"] == _A)
    assert a_occs == [0, 1], "Section A must be re-emitted as two occurrences"
    # Leaf order follows createdAt ASC: A1(100), B1(500), A2(900).
    leaf_order = [r["uuid"] for r in recs if r["uuid"] in (_A1, _A2, _B1)]
    assert leaf_order == [_A1, _B1, _A2]
    # The two A occurrences carry distinct index paths (the disambiguator).
    a_index_paths = [tuple(r["_index_path"]) for r in recs if r["uuid"] == _A]
    assert len(set(a_index_paths)) == 2


# ===========================================================================
# 2. ROW-KEY STRING CONTRACT — byte-for-byte (server / snapshot / JS must match)
# ===========================================================================
def _server_slice_row_key(row):
    """Re-implement the FROZEN server _slice_row_key format to lock the bytes.

    Kept as a literal here (not imported from server.py, which drags heavy
    deps) so this test pins the exact wire format independently. If the real
    _slice_row_key ever changes, its own callers break AND this literal lock
    documents what the contract WAS.
    """
    uuid = str(row.get("uuid") or "")
    ancestor_path = row.get("ancestor_path") or []
    occ = int(row.get("occurrence") or 0)
    path_str = "\x1f".join(str(p) for p in ancestor_path)
    return f"{uuid}\x1f|{path_str}\x1f#{occ}"


def test_row_key_exact_bytes():
    """The row-key format is a frozen 3-field, \\x1f-separated contract."""
    row = {"uuid": _A, "ancestor_path": [_ROOT], "occurrence": 1}
    key = _server_slice_row_key(row)
    assert key == f"{_A}\x1f|{_ROOT}\x1f#1"
    # Empty ancestor_path (a top-level node) still round-trips unambiguously.
    top = {"uuid": _ROOT, "ancestor_path": [], "occurrence": 0}
    assert _server_slice_row_key(top) == f"{_ROOT}\x1f|\x1f#0"
    # Two distinct occurrences of the same (uuid, path) yield distinct keys.
    r0 = {"uuid": _A, "ancestor_path": [_ROOT], "occurrence": 0}
    r1 = {"uuid": _A, "ancestor_path": [_ROOT], "occurrence": 1}
    assert _server_slice_row_key(r0) != _server_slice_row_key(r1)


def test_row_key_matches_server_slice_row_key_import():
    """Sanity: the real server._slice_row_key agrees with the locked literal.

    Imported lazily so a server import failure skips (rather than fails) this
    one cross-check — the literal lock above is the primary contract guard.
    """
    try:
        from workflowy_mcp.server import _slice_row_key  # noqa: E402
    except Exception:
        import pytest
        pytest.skip("server import unavailable in this environment")
    row = {"uuid": _A, "ancestor_path": [_ROOT], "occurrence": 2}
    assert _slice_row_key(row) == _server_slice_row_key(row)


# ===========================================================================
# 3. DOM SNAPSHOT KEYING SEAM  —  UNIVERSAL SERVER-AUTHORED ROWS (post-Phase-0)
# ===========================================================================
# Phase 0 made rows + row-keying UNIVERSAL. The UNSORTED lock below was FLIPPED
# from its pre-Phase-0 shape (render_keyed_by=='uuid', rows is None) to the
# universal shape (render_keyed_by=='row', rows populated, renders triple-keyed)
# — a deliberate, Dan-approved change, recorded here so the flip is visible in
# history rather than silent.
def test_snapshot_unsorted_is_now_row_keyed_and_emits_rows():
    """POST-PHASE-0: unsorted snapshot is UNIVERSAL — row-keyed + emits rows.

    (Pre-Phase-0 this asserted render_keyed_by=='uuid' and rows is None; the
    consolidation retired that unsorted-only regime. See the FLIP note above.)
    """
    c = _make_client()
    res = c.compose_markdown_modal_snapshot(_visible_tree(), sort_by=None)
    assert res["success"] is True
    assert res["render_keyed_by"] == "row"            # FLIPPED (was "uuid")
    assert isinstance(res["rows"], list) and res["rows"]  # FLIPPED (was None)
    # Unsorted document order, via the shared authority.
    assert [r["uuid"] for r in res["rows"]] == [_ROOT, _A, _A1, _A2, _B, _B1]
    # renders now keyed by the \x1f row-triple key; every row's key resolves.
    for r in res["rows"]:
        assert _server_slice_row_key(r) in res["renders"]
    # Unsorted uuids are unique, so there is exactly one render per node.
    assert len(res["renders"]) == 6


def test_BASELINE_snapshot_sorted_keyed_by_row_triple_and_emits_rows():
    """PRE-PHASE-0: sorted snapshot keys by the row triple and DOES emit rows.

    This is the shape Phase 0 will make UNIVERSAL; it should remain green
    across the consolidation (it already uses the target contract).
    """
    c = _make_client()
    res = c.compose_markdown_modal_snapshot(
        _visible_tree(), sort_by="created", sort_direction="asc"
    )
    assert res["success"] is True
    assert res["render_keyed_by"] == "row"
    assert isinstance(res["rows"], list) and res["rows"]
    # renders keyed by the \x1f row-triple key; each row's key resolves.
    for r in res["rows"]:
        assert _server_slice_row_key(r) in res["renders"]


def test_BASELINE_snapshot_sorted_row_keys_are_unique_despite_repeated_uuid():
    """Sorted snapshot: the straddled Section A recurs, but ROW KEYS stay unique.

    Proves the triple key (not uuid) is what keeps the snapshot map collision-
    free — the invariant Phase 0's universal keying must preserve.
    """
    c = _make_client()
    res = c.compose_markdown_modal_snapshot(
        _visible_tree(), sort_by="created", sort_direction="asc"
    )
    keys = [_server_slice_row_key(r) for r in res["rows"]]
    assert len(keys) == len(set(keys)), "row-triple keys must be unique"
    # Section A really did recur (otherwise this test proves nothing).
    assert sum(1 for r in res["rows"] if r["uuid"] == _A) == 2


# ===========================================================================
# 4. rows_only endpoint parity (already universal — must stay green post-Phase-0)
# ===========================================================================
def test_rows_only_matches_build_modal_rows_authority_unsorted_and_sorted():
    """compose_markdown_modal_rows == _build_modal_rows over the render root,

    for BOTH orders. This endpoint is ALREADY on the universal contract, so it
    is the reference the snapshot/single paths should converge to in Phase 0.
    """
    c = _make_client()
    for sb in (None, "created"):
        ctx = c._build_modal_render_context(_visible_tree(), sort_by=sb, sort_direction="asc")
        # Pass the SAME cache_by_id the production compose path uses, so the
        # authority `expected` carries the additive per-row created/modified
        # timestamp fields (Aug 2026 timeline/hover metadata) exactly like
        # `got` -- otherwise the two drift ONLY on those additive fields.
        expected = W._build_modal_rows(ctx["render_root"], cache_by_id=ctx["cache_by_id"])
        got = c.compose_markdown_modal_rows(_visible_tree(), sort_by=sb, sort_direction="asc")["rows"]
        assert got == expected, f"rows_only drifted from _build_modal_rows (sort_by={sb})"


def test_snapshot_rows_match_build_modal_rows_authority_both_orders():
    """POST-PHASE-0: the SNAPSHOT path's rows now also equal _build_modal_rows.

    Convergence check: before Phase 0 the unsorted snapshot emitted no rows and
    the browser DOM-walked its own list; now the snapshot rows come from the
    SAME shared authority the rows_only endpoint uses, so the two can never
    drift and the client has ONE row contract to consume.
    """
    c = _make_client()
    for sb in (None, "created"):
        ctx = c._build_modal_render_context(_visible_tree(), sort_by=sb, sort_direction="asc")
        # Same cache_by_id passthrough as the rows_only authority check above:
        # the snapshot compose stamps the additive created/modified row fields
        # from the cache, so the authority must be given the same cache to match.
        expected = W._build_modal_rows(ctx["render_root"], cache_by_id=ctx["cache_by_id"])
        got = c.compose_markdown_modal_snapshot(
            _visible_tree(), sort_by=sb, sort_direction="asc"
        )["rows"]
        assert got == expected, f"snapshot rows drifted from _build_modal_rows (sort_by={sb})"


# ===========================================================================
# 5. STALENESS-SHRINK  —  the ONE new behavior Phase 0 introduces (Dan-approved)
# ===========================================================================
def test_staleness_shrinks_rows_to_what_bottom_pane_can_render():
    """Under cache staleness, a DOM-only node absent from the cache is DROPPED

    from the server rows -- the list now honestly reflects what the bottom pane
    can render (intersection-pruned render_root), instead of listing a node the
    renderer would then reject. This is the accepted behavior change Dan signed
    off on for Phase 0 (previously the browser DOM-walk kept the unrenderable
    node clickable, producing an 'unmatched focus' warning on click).

    Setup: the visible DOM contains an EXTRA leaf (_GHOST) that is NOT in the
    cache. _modal_staleness flags it; _modal_build_intersection_root prunes it;
    the universal rows are built from that pruned render_root -> _GHOST absent.
    """
    _GHOST = "aaaaaaaa-0000-0000-0000-0000000000ff"
    c = _make_client()

    def _tree_with_ghost():
        def n(nid, name, note, children=None):
            return {"id": nid, "name": name, "note": note, "children": children or []}
        return {
            "root": {"id": _ROOT, "name": "Captured Root", "note": ""},
            "children": [
                n(_A, "Section A", "body A", [
                    n(_A1, "Point A1", "alpha one"),
                    # _GHOST is visible in the DOM but ABSENT from the cache.
                    n(_GHOST, "Ghost node", "not in cache"),
                ]),
            ],
        }

    res = c.compose_markdown_modal_snapshot(_tree_with_ghost(), sort_by=None)
    assert res["success"] is True
    # Staleness is detected (the DOM-only ghost) ...
    assert res["cache_stale"] is True
    assert _GHOST in (res["stale_nodes"] or [])
    # ... and the emitted rows are the intersection-pruned set: the ghost is
    # NOT listed (the bottom pane cannot render it), while the cached siblings
    # remain. This is the deliberate staleness-shrink.
    row_uuids = {r["uuid"] for r in res["rows"]}
    assert _GHOST not in row_uuids
    assert {_ROOT, _A, _A1} <= row_uuids
