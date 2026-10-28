"""Characterization + contract lock for the Layer-1 UUID primitive.

``resolve_uuid_like_node_handle`` (api_client_nexus) is the single confluence
point for ALL UUID-like node-handle resolution: it folds the EXACT-hit check
and the shared prefix kernel (``_resolve_uuid_with_prefix_fallback``) into one
pure, read-only function that callers use in place of the hand-rolled
``if id not in nodes_by_id: _resolve_uuid_with_prefix_fallback(...)`` block.

These tests are written FIRST (Commit 0 of the handle-resolution unification)
and serve two purposes:

1. CONTRACT: pin the tri-state return shape the universal resolver (Layer 3)
   and every lower-layer caller branch on:
     * exact hit     -> {"success": True, "resolved_uuid", "input_kind": "exact_uuid"}
     * unique prefix  -> {"success": True, "resolved_uuid", "input_kind": "uuid_prefix", ...}
     * ambiguous      -> {"success": False, "candidates", "matched_prefix_len",
                          "truncated", "input_kind": None}
     * unresolved     -> {"success": False, "input_kind": None}  (no resolved_uuid,
                          no candidates)

2. NO-BEHAVIOR-CHANGE: assert the primitive's resolution DECISION is identical
   to the pre-existing exact-hit + kernel pattern it replaces, so the Commit-0
   migration of beacon_get_code_snippet / workflowy_scry / the anchor tools /
   root_uuid GLIMPSE is a true refactor. The kernel's own multi/none shapes are
   surfaced verbatim (``candidates`` / ``matched_prefix_len`` / ``truncated``),
   so each caller's existing ``.get("resolved_uuid")`` / ``.get("candidates")``
   branch keeps working unchanged.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest  # noqa: E402

from workflowy_mcp.client.api_client_nexus import (  # noqa: E402
    _resolve_uuid_with_prefix_fallback,
    load_nodes_by_id_from_cache,
    resolve_uuid_like_node_handle,
)


class _FakeCacheClient:
    """Minimal client exposing the strict cache accessor for self-load tests."""

    def __init__(self, nodes):
        self._nodes = nodes

    def _get_nodes_export_cache_nodes(self):
        return list(self._nodes)


# --- fixtures ---------------------------------------------------------------

FULL_A = "a2e552e3-eb42-4438-ba75-4ab7f849eb4f"
FULL_B = "a2e552e3-ffff-4438-ba75-000000000000"  # shares first segment with A
FULL_C = "bc137d90-1111-2222-3333-444444444444"


def _nodes(*ids):
    return {i: {"id": i, "name": f"node {i[:8]}"} for i in ids}


# --- exact hit --------------------------------------------------------------

def test_exact_hit_returns_resolved_with_exact_kind():
    nodes = _nodes(FULL_A, FULL_C)
    res = resolve_uuid_like_node_handle(FULL_A, nodes)
    assert res["success"] is True
    assert res["resolved_uuid"] == FULL_A
    assert res["input_kind"] == "exact_uuid"
    # exact hit never carries a candidates list
    assert not res.get("candidates")


def test_exact_hit_does_not_invoke_prefix_backtrack():
    # Two nodes share the first segment; an EXACT hit on the full id must NOT
    # be re-interpreted as an ambiguous prefix.
    nodes = _nodes(FULL_A, FULL_B)
    res = resolve_uuid_like_node_handle(FULL_A, nodes)
    assert res["resolved_uuid"] == FULL_A
    assert res["input_kind"] == "exact_uuid"


# --- unique prefix ----------------------------------------------------------

def test_unique_prefix_adopts_silently():
    nodes = _nodes(FULL_A, FULL_C)
    # a hallucinated tail on A's real first segment; only A matches
    supplied = "a2e552e3-eb42-dead-beef-000000000000"
    res = resolve_uuid_like_node_handle(supplied, nodes)
    assert res["success"] is True
    assert res["resolved_uuid"] == FULL_A
    assert res["input_kind"] == "uuid_prefix"


def test_unique_prefix_matches_kernel_decision():
    nodes = _nodes(FULL_A, FULL_C)
    supplied = "a2e552e3-eb42-dead-beef-000000000000"
    kernel = _resolve_uuid_with_prefix_fallback(supplied, nodes)
    primitive = resolve_uuid_like_node_handle(supplied, nodes)
    # same resolved UUID as the raw kernel would have produced
    assert primitive["resolved_uuid"] == kernel["resolved_uuid"] == FULL_A


# --- ambiguous --------------------------------------------------------------

def test_ambiguous_prefix_never_auto_picks():
    nodes = _nodes(FULL_A, FULL_B, FULL_C)
    # collapses to the shared first segment "a2e552e3" -> A and B both match
    supplied = "a2e552e3-0000-0000-0000-000000000000"
    res = resolve_uuid_like_node_handle(supplied, nodes)
    assert res["success"] is False
    assert res.get("resolved_uuid") is None
    assert res["input_kind"] is None
    cands = {c["uuid"] for c in res["candidates"]}
    assert cands == {FULL_A, FULL_B}
    assert "matched_prefix_len" in res
    assert "truncated" in res


def test_ambiguous_surfaces_same_candidates_as_kernel():
    nodes = _nodes(FULL_A, FULL_B, FULL_C)
    supplied = "a2e552e3-0000-0000-0000-000000000000"
    kernel = _resolve_uuid_with_prefix_fallback(supplied, nodes)
    primitive = resolve_uuid_like_node_handle(supplied, nodes)
    assert {c["uuid"] for c in primitive["candidates"]} == {
        c["uuid"] for c in kernel["candidates"]
    }
    assert primitive["matched_prefix_len"] == kernel["matched_prefix_len"]
    assert primitive["truncated"] == kernel["truncated"]


# --- unresolved -------------------------------------------------------------

def test_no_match_returns_unresolved_no_candidates():
    nodes = _nodes(FULL_A, FULL_C)
    res = resolve_uuid_like_node_handle("ffffffff-0000-0000-0000-000000000000", nodes)
    assert res["success"] is False
    assert res.get("resolved_uuid") is None
    assert not res.get("candidates")
    assert res["input_kind"] is None


def test_empty_nodes_is_unresolved():
    res = resolve_uuid_like_node_handle(FULL_A, {})
    assert res["success"] is False
    assert res.get("resolved_uuid") is None


def test_too_short_candidate_is_unresolved():
    # shorter than min_prefix_len (8) and not an exact key -> unresolved
    nodes = _nodes(FULL_A)
    res = resolve_uuid_like_node_handle("a2e5", nodes)
    assert res["success"] is False
    assert res.get("resolved_uuid") is None


# --- key_prefix passthrough (slice-group parity, exclusion documented) ------

def test_key_prefix_exact_hit():
    nodes = {"grp-" + "a" * 32: {"id": "grp-" + "a" * 32, "name": "grp"}}
    key = "grp-" + "a" * 32
    res = resolve_uuid_like_node_handle(key, nodes, key_prefix="grp-")
    assert res["resolved_uuid"] == key
    assert res["input_kind"] == "exact_uuid"


# --- Commit 1.5: hybrid cache load ------------------------------------------

def test_self_load_returns_map_and_resolves():
    client = _FakeCacheClient([{"id": FULL_A, "name": "a"}, {"id": FULL_C, "name": "c"}])
    res = resolve_uuid_like_node_handle(FULL_A, client=client)
    assert res["resolved_uuid"] == FULL_A
    assert res["input_kind"] == "exact_uuid"
    # the map it loaded is handed back for downstream reuse (no double read)
    assert set(res["nodes_by_id"].keys()) == {FULL_A, FULL_C}


def test_passed_map_is_returned_verbatim():
    nodes = _nodes(FULL_A, FULL_C)
    res = resolve_uuid_like_node_handle(FULL_A, nodes)
    assert res["nodes_by_id"] is nodes  # same object, not a reload


def test_neither_map_nor_client_raises():
    with pytest.raises(ValueError):
        resolve_uuid_like_node_handle(FULL_A)


def test_both_map_and_client_raises():
    with pytest.raises(ValueError):
        resolve_uuid_like_node_handle(FULL_A, _nodes(FULL_A),
                                      client=_FakeCacheClient([]))


def test_load_nodes_by_id_from_cache_builds_map():
    client = _FakeCacheClient([{"id": FULL_A, "name": "a"}, {"no_id": 1}])
    m = load_nodes_by_id_from_cache(client)
    assert FULL_A in m and len(m) == 1  # entries without an id are skipped


def test_key_prefix_unique_prefix_matches_kernel():
    # Single group in the map; supplied id shares all but the final hex char,
    # so the kernel's backtrack finds a long UNIQUE prefix and resolves it.
    full = "grp-" + "a" * 32
    nodes = {full: {"id": full, "name": "grp"}}
    supplied = "grp-" + "a" * 31 + "b"  # differ only in the last char
    kernel = _resolve_uuid_with_prefix_fallback(supplied, nodes, key_prefix="grp-")
    primitive = resolve_uuid_like_node_handle(supplied, nodes, key_prefix="grp-")
    # The primitive's DECISION must equal the raw kernel's decision, whatever
    # it is (this is the no-behavior-change guard, not an assertion that a
    # match must occur).
    assert primitive.get("resolved_uuid") == kernel.get("resolved_uuid")
    assert kernel.get("resolved_uuid") == full  # fixture sanity: this one resolves


# --- 23db916f: matched_prefix_len on a UNIQUE kernel match -------------------

def test_kernel_unique_match_reports_matched_prefix_len():
    nodes = _nodes(FULL_A, FULL_C)
    supplied = "a2e552e3-eb42-dead-beef-000000000000"
    kernel = _resolve_uuid_with_prefix_fallback(supplied, nodes)
    assert kernel["resolved_uuid"] == FULL_A
    # The supplied shares 12 normalized chars with the true UUID before the
    # fabricated tail diverges, so 12 is the longest matching prefix (the
    # shave length the Layer-1 wrapper compares against the supplied length).
    assert kernel["matched_prefix_len"] == 12


def test_kernel_no_match_shape_unchanged():
    nodes = _nodes(FULL_A, FULL_C)
    kernel = _resolve_uuid_with_prefix_fallback(
        "ffffffff-0000-0000-0000-000000000000", nodes
    )
    assert kernel == {}


def test_kernel_reports_full_len_for_hyphenation_only_adoption():
    # An undashed 32-hex full UUID is not an exact cache key (the cache keys
    # are dashed), so the kernel adopts it at prefix_len == the full
    # normalized length -- a normalization, not a shave.
    nodes = _nodes(FULL_A, FULL_C)
    supplied = FULL_A.replace("-", "")
    kernel = _resolve_uuid_with_prefix_fallback(supplied, nodes)
    assert kernel["resolved_uuid"] == FULL_A
    assert kernel["matched_prefix_len"] == len(supplied)


# --- 23db916f: the wrapper records a correction ONLY on a shaved handle -----

def _drained():
    from workflowy_mcp.handle_corrections import drain_handle_corrections

    return drain_handle_corrections()


def test_wrapper_records_correction_on_shaved_uuid():
    from workflowy_mcp.handle_corrections import reset_handle_corrections

    reset_handle_corrections()
    nodes = _nodes(FULL_A, FULL_C)
    supplied = "a2e552e3-eb42-dead-beef-000000000000"
    res = resolve_uuid_like_node_handle(supplied, nodes)
    assert res["success"] is True and res["input_kind"] == "uuid_prefix"
    corrections = _drained()
    assert len(corrections) == 1
    entry = corrections[0]
    assert entry["supplied"] == supplied
    assert entry["resolved_uuid"] == FULL_A
    assert entry["name"] == "node a2e552e3"
    assert entry["short"] == "a2e552e3"
    # Drained means cleared: a second drain is empty.
    assert _drained() == []


def test_wrapper_silent_on_exact_hit():
    from workflowy_mcp.handle_corrections import reset_handle_corrections

    reset_handle_corrections()
    resolve_uuid_like_node_handle(FULL_A, _nodes(FULL_A, FULL_C))
    assert _drained() == []


def test_wrapper_silent_on_clean_short_prefix():
    # A clean 8-hex segment that matched at its own full length is NOT a
    # correction (matched_prefix_len == len(supplied)).
    from workflowy_mcp.handle_corrections import reset_handle_corrections

    reset_handle_corrections()
    res = resolve_uuid_like_node_handle("a2e552e3", _nodes(FULL_A, FULL_C))
    assert res["success"] is True and res["input_kind"] == "uuid_prefix"
    assert _drained() == []


def test_wrapper_silent_on_longer_clean_prefix():
    # A prefix longer than 8 that matches at its own full length (no node
    # shares a nearer prefix) is also clean.
    from workflowy_mcp.handle_corrections import reset_handle_corrections

    reset_handle_corrections()
    supplied = FULL_A[:12]
    res = resolve_uuid_like_node_handle(supplied, _nodes(FULL_A, FULL_C))
    assert res["success"] is True
    assert _drained() == []


def test_wrapper_records_when_long_prefix_is_shaved():
    # 12 supplied chars where the real node differs at char 9: the kernel
    # backtracks to 8 -- a shave -> one correction.
    from workflowy_mcp.handle_corrections import reset_handle_corrections

    reset_handle_corrections()
    supplied = "a2e552e3fff"
    res = resolve_uuid_like_node_handle(supplied, _nodes(FULL_A, FULL_C))
    assert res["resolved_uuid"] == FULL_A
    corrections = _drained()
    assert len(corrections) == 1
    assert corrections[0]["supplied"] == supplied
    assert corrections[0]["short"] == "a2e552e3"


def test_wrapper_silent_on_ambiguous_and_unresolved():
    from workflowy_mcp.handle_corrections import reset_handle_corrections

    reset_handle_corrections()
    resolve_uuid_like_node_handle(
        "a2e552e3-0000-0000-0000-000000000000", _nodes(FULL_A, FULL_B, FULL_C)
    )
    resolve_uuid_like_node_handle(
        "ffffffff-0000-0000-0000-000000000000", _nodes(FULL_A, FULL_C)
    )
    assert _drained() == []


def test_wrapper_key_prefix_exact_hit_records_nothing():
    from workflowy_mcp.handle_corrections import reset_handle_corrections

    reset_handle_corrections()
    key = "grp-" + "a" * 32
    resolve_uuid_like_node_handle(
        key, {key: {"id": key, "name": "grp"}}, key_prefix="grp-"
    )
    assert _drained() == []


def test_wrapper_key_prefix_shaved_group_id_records_correction():
    from workflowy_mcp.handle_corrections import reset_handle_corrections

    reset_handle_corrections()
    full = "grp-" + "a" * 32
    nodes = {full: {"id": full, "name": "grp"}}
    supplied = "grp-" + "a" * 31 + "b"
    res = resolve_uuid_like_node_handle(supplied, nodes, key_prefix="grp-")
    assert res["resolved_uuid"] == full
    corrections = _drained()
    assert len(corrections) == 1
    assert corrections[0]["supplied"] == supplied
    assert corrections[0]["resolved_uuid"] == full
