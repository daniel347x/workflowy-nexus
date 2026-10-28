"""Typed-region schema tests: round-trips, strict
validation, canonical ordering, the intersection truth table, and the A1
admission-time-rehydration containment authority.
"""

from __future__ import annotations

import pytest
from mutation_lease_harness import ACCOUNT_A, cache_of
from n05_region_harness import node_uuid as U

from workflowy_mcp.mutation_lease_governance import (
    MutationRegion,
    MutationRegionError,
    build_region_ap_core,
    build_region_child_set,
    build_region_exact_node,
    build_region_subtree,
    mutation_regions_intersect_carto_claim,
    normalize_mutation_regions,
    regions_intersect,
)

# ---------------------------------------------------------------------------
# Region construction + round-trips
# ---------------------------------------------------------------------------

_CHAIN_CACHE = cache_of(
    (U("vault"), None),
    (U("repo"), U("vault")),
    (U("file"), U("repo")),
    (U("f1"), U("file")),
    (U("f2"), U("file")),
    (U("g"), U("vault")),
    (U("p"), U("g")),
)


def test_exact_node_region_roundtrip():
    region = build_region_exact_node(U("f1"), cache_by_id=_CHAIN_CACHE)
    assert region.kind == "exact_node"
    assert region.node_uuid == U("f1")
    assert region.ancestor_chain == tuple(map(U, ("f1", "file", "repo", "vault")))
    decoded = MutationRegion.from_dict(region.to_dict())
    assert decoded == region


def test_child_set_region_roundtrip():
    region = build_region_child_set(U("file"), cache_by_id=_CHAIN_CACHE)
    assert region.kind == "child_set"
    assert region.parent_uuid == U("file")
    assert region.ancestor_chain == tuple(map(U, ("file", "repo", "vault")))
    assert MutationRegion.from_dict(region.to_dict()) == region


def test_subtree_region_roundtrip():
    region = build_region_subtree(U("repo"), cache_by_id=_CHAIN_CACHE)
    assert region.kind == "subtree"
    assert region.root_uuid == U("repo")
    assert region.ancestor_chain == tuple(map(U, ("repo", "vault")))
    assert MutationRegion.from_dict(region.to_dict()) == region


def test_ap_core_region_roundtrip_and_normalization():
    region = build_region_ap_core("[ap:k7m2qf]")
    assert region.kind == "ap_core"
    assert region.core == "K7M2QF"
    assert region.ancestor_chain == ()
    assert MutationRegion.from_dict(region.to_dict()) == region
    # Bare + bracket + ap: forms all normalize identically.
    assert build_region_ap_core("K7M2QF") == region
    assert build_region_ap_core("ap:K7M2QF") == region
    assert build_region_ap_core("[K7M2QF]") == region


def test_ap_core_invalid_input_fails_closed():
    with pytest.raises(MutationRegionError):
        build_region_ap_core("not a core!!")


def test_ancestor_cycle_fails_closed():
    cache = {U("a"): {"id": U("a"), "parent_id": U("b")}, U("b"): {"id": U("b"), "parent_id": U("a")}}
    with pytest.raises(MutationRegionError):
        build_region_exact_node(U("a"), cache_by_id=cache)


def test_missing_ancestor_fails_closed():
    cache = {U("a"): {"id": U("a"), "parent_id": U("ghost")}}
    with pytest.raises(MutationRegionError):
        build_region_exact_node(U("a"), cache_by_id=cache)


def test_missing_live_key_fails_closed_not_stored_chain_fallback():
    # A region whose stored chain SUGGESTS containment must never authorize a
    # decision when the live key is unresolvable (A1).
    stale = MutationRegion(
        kind="subtree", root_uuid=U("gone"), ancestor_chain=(U("gone"), U("vault"))
    )
    with pytest.raises(MutationRegionError):
        regions_intersect(
            stale,
            build_region_exact_node(U("f1"), cache_by_id=_CHAIN_CACHE),
            cache_by_id=_CHAIN_CACHE,
        )


def test_region_unknown_keys_and_kinds_fail_closed():
    with pytest.raises(MutationRegionError):
        MutationRegion.from_dict({"kind": "exact_node", "node_uuid": U("x"), "ancestor_chain": [U("x")], "extra": 1})
    with pytest.raises(MutationRegionError):
        MutationRegion.from_dict({"kind": "whole_vault", "node_uuid": U("x")})
    with pytest.raises(MutationRegionError):
        MutationRegion.from_dict({"kind": "exact_node"})
    with pytest.raises(MutationRegionError):
        MutationRegion(kind="exact_node", node_uuid=U("x"), ancestor_chain=(U("y"), U("vault")))


def test_canonical_sort_and_dedupe():
    regions = normalize_mutation_regions(
        [
            build_region_subtree(U("repo"), cache_by_id=_CHAIN_CACHE),
            build_region_ap_core("AAA111"),
            build_region_exact_node(U("f1"), cache_by_id=_CHAIN_CACHE),
            build_region_child_set(U("file"), cache_by_id=_CHAIN_CACHE),
            # byte-identical duplicate collapses:
            build_region_exact_node(U("f1"), cache_by_id=_CHAIN_CACHE),
        ]
    )
    kinds = [region.kind for region in regions]
    assert kinds == ["ap_core", "child_set", "exact_node", "subtree"]


def test_conflicting_duplicate_payloads_rejected():
    good = build_region_exact_node(U("f1"), cache_by_id=_CHAIN_CACHE)
    conflicting = MutationRegion(
        kind="exact_node", node_uuid=U("f1"), ancestor_chain=(U("f1"), U("WRONG"))
    )
    with pytest.raises(MutationRegionError):
        normalize_mutation_regions([good, conflicting])


def test_empty_region_set_rejected():
    with pytest.raises(MutationRegionError):
        normalize_mutation_regions([])


# ---------------------------------------------------------------------------
# The §5.3 intersection truth table (symmetric)
# ---------------------------------------------------------------------------


def _table(a, b, view):
    assert regions_intersect(a, b, cache_by_id=view) == regions_intersect(
        b, a, cache_by_id=view
    )
    return regions_intersect(a, b, cache_by_id=view)


def test_exact_vs_exact_equality_only():
    a = build_region_exact_node(U("f1"), cache_by_id=_CHAIN_CACHE)
    b = build_region_exact_node(U("f1"), cache_by_id=_CHAIN_CACHE)
    c = build_region_exact_node(U("f2"), cache_by_id=_CHAIN_CACHE)
    assert _table(a, b, _CHAIN_CACHE) is True
    assert _table(a, c, _CHAIN_CACHE) is False


def test_child_set_vs_child_set_equality_only():
    a = build_region_child_set(U("file"), cache_by_id=_CHAIN_CACHE)
    b = build_region_child_set(U("file"), cache_by_id=_CHAIN_CACHE)
    c = build_region_child_set(U("g"), cache_by_id=_CHAIN_CACHE)
    assert _table(a, b, _CHAIN_CACHE) is True
    assert _table(a, c, _CHAIN_CACHE) is False


def test_exact_node_vs_child_set_NEVER_intersects_permanent_lock():
    """The ownership counterpart to the S01/S04 reducer commutativity test:
    exact-node content UPDATE and sibling/parent child-order CREATE commute
    under Axiom 7 and MUST remain concurrently permissible."""
    exact = build_region_exact_node(U("f1"), cache_by_id=_CHAIN_CACHE)
    parent = build_region_child_set(U("file"), cache_by_id=_CHAIN_CACHE)
    assert _table(exact, parent, _CHAIN_CACHE) is False
    grandparent = build_region_child_set(U("repo"), cache_by_id=_CHAIN_CACHE)
    assert _table(exact, grandparent, _CHAIN_CACHE) is False


def test_subtree_containment_both_directions():
    subtree = build_region_subtree(U("file"), cache_by_id=_CHAIN_CACHE)
    inside = build_region_exact_node(U("f1"), cache_by_id=_CHAIN_CACHE)
    outside = build_region_exact_node(U("p"), cache_by_id=_CHAIN_CACHE)
    assert _table(subtree, inside, _CHAIN_CACHE) is True
    assert _table(subtree, outside, _CHAIN_CACHE) is False
    child = build_region_child_set(U("file"), cache_by_id=_CHAIN_CACHE)
    assert _table(subtree, child, _CHAIN_CACHE) is True
    wider = build_region_subtree(U("vault"), cache_by_id=_CHAIN_CACHE)
    narrower = build_region_subtree(U("repo"), cache_by_id=_CHAIN_CACHE)
    assert _table(wider, narrower, _CHAIN_CACHE) is True
    disjoint = build_region_subtree(U("g"), cache_by_id=_CHAIN_CACHE)
    assert _table(narrower, disjoint, _CHAIN_CACHE) is False


def test_ap_core_only_intersects_same_core():
    a = build_region_ap_core("AAA111")
    b = build_region_ap_core("AAA111")
    c = build_region_ap_core("BBB222")
    graph = build_region_exact_node(U("f1"), cache_by_id=_CHAIN_CACHE)
    assert _table(a, b, _CHAIN_CACHE) is True
    assert _table(a, c, _CHAIN_CACHE) is False
    assert _table(a, graph, _CHAIN_CACHE) is False


def test_containment_requires_current_view():
    # Stored chains exist on both regions; with no view the containment row
    # fails closed (A1 — never an allow from stale stored chains).
    subtree = build_region_subtree(U("file"), cache_by_id=_CHAIN_CACHE)
    inside = build_region_exact_node(U("f1"), cache_by_id=_CHAIN_CACHE)
    with pytest.raises(MutationRegionError):
        regions_intersect(subtree, inside, cache_by_id=None)


def test_stored_chain_never_authorizes_non_overlap():
    """A live key whose CURRENT chain moved OUT of a stored subtree must be
    rehydrated to non-overlap — and a key moved IN must be rehydrated to
    overlap; the persisted chain is acquisition-time evidence only (A1)."""
    stale_inside = MutationRegion(
        kind="exact_node", node_uuid=U("p"), ancestor_chain=tuple(map(U, ("p", "file", "repo", "vault")))
    )
    # Stored chain claims p is inside `file`; the CURRENT view has p under g.
    subtree_file = build_region_subtree(U("file"), cache_by_id=_CHAIN_CACHE)
    assert regions_intersect(stale_inside, subtree_file, cache_by_id=_CHAIN_CACHE) is False
    subtree_g = build_region_subtree(U("g"), cache_by_id=_CHAIN_CACHE)
    assert regions_intersect(stale_inside, subtree_g, cache_by_id=_CHAIN_CACHE) is True


def test_carto_claim_intersection():
    claim = {"root_uuid": U("repo"), "account_scope_id": ACCOUNT_A}
    inside = build_region_exact_node(U("f1"), cache_by_id=_CHAIN_CACHE)
    outside = build_region_exact_node(U("p"), cache_by_id=_CHAIN_CACHE)
    assert mutation_regions_intersect_carto_claim([inside], claim, cache_by_id=_CHAIN_CACHE) is inside
    assert mutation_regions_intersect_carto_claim([outside], claim, cache_by_id=_CHAIN_CACHE) is None
    child = build_region_child_set(U("file"), cache_by_id=_CHAIN_CACHE)
    assert mutation_regions_intersect_carto_claim([child], claim, cache_by_id=_CHAIN_CACHE) is child
    wider = build_region_subtree(U("vault"), cache_by_id=_CHAIN_CACHE)
    assert mutation_regions_intersect_carto_claim([wider], claim, cache_by_id=_CHAIN_CACHE) is wider
    narrower = build_region_subtree(U("g"), cache_by_id=_CHAIN_CACHE)
    assert mutation_regions_intersect_carto_claim([narrower], claim, cache_by_id=_CHAIN_CACHE) is None
    ap = build_region_ap_core("AAA111")
    assert mutation_regions_intersect_carto_claim([ap], claim, cache_by_id=_CHAIN_CACHE) is None


def test_ap_core_identity_in_single_account_plane():
    # One daemon serves one account (W7); there is no account filter on its regions.
    # Equal normalized AP cores intersect; graph regions never intersect an AP core.
    # This asserts region identity, not the retired lease caller's account gate.
    a = build_region_ap_core("AAA111")
    b = build_region_ap_core("AAA111")
    assert regions_intersect(a, b) is True


# ---------------------------------------------------------------------------
# A1 predicate rehydration; full A1b admission orderings and neutralizations
# live in tests/test_mutation_regions_n05_revalidation.py.
# ---------------------------------------------------------------------------


def test_topology_race_predicate_rehydration():
    """hold child_set(P) under old ancestor A; MOVE P beneath G; the
    rehydrated overlap predicate must discover P beneath G (never the stored
    acquisition-time chain)."""
    before = cache_of((U("vault"), None), (U("a"), U("vault")), (U("p"), U("a")), (U("g"), U("vault")))
    held = MutationRegion(
        kind="child_set", parent_uuid=U("p"), ancestor_chain=tuple(map(U, ("p", "a", "vault")))
    )
    candidate = build_region_subtree(U("g"), cache_by_id=before)
    assert regions_intersect(candidate, held, cache_by_id=before) is False
    after = cache_of((U("vault"), None), (U("a"), U("vault")), (U("p"), U("g")), (U("g"), U("vault")))
    assert regions_intersect(candidate, held, cache_by_id=after) is True
