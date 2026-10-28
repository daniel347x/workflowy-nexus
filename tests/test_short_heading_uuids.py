"""Short 8-hex heading ids in every rendered heading ([ap:0M6VP8] part a).

One meaning of "short" (Ledger CF-22 / O3): rendered headings and the resolve
verbs' ``short`` field both route through the ONE authority
``unique_short_uuid_prefix``; a colliding 8-hex prefix is lengthened just
enough to be unique across the same cache it will be pasted back into, never
silently ambiguous; ``full_uuids: true`` is the sole opt-in back to full
UUIDs; non-UUID-shaped ids are displayed verbatim.
"""
from __future__ import annotations

import copy

from workflowy_mcp.cache_render_spec import RenderPolicy, RenderProjectionSpec
from workflowy_mcp.client.api_client_nexus import (
    WorkFlowyClientNexus,
    build_uuid_prefix_collisions,
    node_identity,
    unique_short_uuid_prefix,
)

UUID_A = "11111111-aaaa-4111-8111-111111111111"
UUID_B = "11111111-bbbb-4111-8111-111111111111"
UUID_C = "22222222-2222-4222-8222-222222222222"


def _node(nid: str, name: str, children: list | None = None) -> dict:
    return {"id": nid, "name": name, "note": "", "children": children or []}


# --------------------------------------------------------------------------
# 1 · the pure helpers


def test_unambiguous_uuid_shortens_to_eight_hex():
    assert unique_short_uuid_prefix(UUID_C) == "22222222"
    assert unique_short_uuid_prefix(UUID_C, {}) == "22222222"
    assert unique_short_uuid_prefix(UUID_C, {UUID_C[:8]: [UUID_C]}) == "22222222"


def test_colliding_prefix_is_lengthened_just_enough_to_be_unique():
    collisions = build_uuid_prefix_collisions([UUID_A, UUID_B, UUID_C])
    assert set(collisions) == {UUID_A[:8]}
    short_a = unique_short_uuid_prefix(UUID_A, collisions)
    short_b = unique_short_uuid_prefix(UUID_B, collisions)
    assert short_a == "11111111a" and short_b == "11111111b"
    # paste-back property: neither short form matches the other node
    stripped = [u.replace("-", "") for u in (UUID_A, UUID_B, UUID_C)]
    assert sum(1 for s in stripped if s.startswith(short_a)) == 1
    assert sum(1 for s in stripped if s.startswith(short_b)) == 1


def test_non_uuid_shaped_ids_are_never_shortened():
    assert unique_short_uuid_prefix("sent-1") == "sent-1"
    assert unique_short_uuid_prefix("nested-md") == "nested-md"
    assert unique_short_uuid_prefix("") == ""
    assert unique_short_uuid_prefix(None) == ""
    # and the index builder ignores them entirely
    assert build_uuid_prefix_collisions(["sent-1", "nested-md", ""]) == {}


def test_node_identity_routes_short_through_the_one_helper():
    nodes = {UUID_A: {"id": UUID_A, "name": "A"}, UUID_B: {"id": UUID_B, "name": "B"}}
    ident = node_identity(UUID_A, nodes)
    assert ident["short"] == "11111111a"
    # a single-node (non-colliding) map keeps the plain 8-hex form
    solo = node_identity(UUID_A, {UUID_A: nodes[UUID_A]})
    assert solo["short"] == "11111111"
    # no map at all: plain 8-hex degrade
    assert node_identity(UUID_A)["short"] == "11111111"


# --------------------------------------------------------------------------
# 2 · the one composer emission site


def test_headings_only_prints_short_handles_by_default():
    tree = [_node(UUID_A, "Alpha", [_node(UUID_C, "Beta")])]
    text = WorkFlowyClientNexus._build_markdown_preview_from_tree(tree, headings_only=True)
    assert f"[{UUID_A[:8]}] Alpha" in text
    assert f"[{UUID_C[:8]}] Beta" in text
    assert UUID_A not in text and UUID_C not in text


def test_colliding_heading_ids_are_lengthened_via_the_policy_index():
    nodes = {
        UUID_A: {"id": UUID_A, "name": "A"},
        UUID_B: {"id": UUID_B, "name": "B"},
    }
    collisions = build_uuid_prefix_collisions(nodes)
    tree = [_node(UUID_A, "Alpha"), _node(UUID_B, "Beta")]
    policy = RenderPolicy(uuid_prefix_collisions=collisions)
    text = WorkFlowyClientNexus._build_markdown_preview_from_tree(
        tree, headings_only=True, render_policy=policy
    )
    assert "[11111111a] Alpha" in text and "[11111111b] Beta" in text
    # the exact UUIDs never appear
    assert UUID_A not in text and UUID_B not in text


def test_full_uuid_headings_opt_in_prints_full_uuids():
    tree = [_node(UUID_A, "Alpha")]
    policy = RenderPolicy(full_uuid_headings=True)
    text = WorkFlowyClientNexus._build_markdown_preview_from_tree(
        tree, headings_only=True, render_policy=policy
    )
    assert f"[{UUID_A}] Alpha" in text


def test_no_policy_still_prints_short_form():
    tree = [_node(UUID_A, "Alpha")]
    text = WorkFlowyClientNexus._build_markdown_preview_from_tree(tree, headings_only=True)
    assert f"[{UUID_A[:8]}] Alpha" in text


def test_body_render_and_inputs_untouched():
    tree = [_node(UUID_A, "Alpha", [_node(UUID_C, "Beta")])]
    before = copy.deepcopy(tree)
    full = WorkFlowyClientNexus._build_markdown_preview_from_tree(tree)
    # a non-headings-only markdown render prints NO ids at all (ids are a
    # headings-only emission; the structured full/UUID mode keeps full ids)
    assert "[" not in full and "# Alpha" in full
    assert tree == before


# --------------------------------------------------------------------------
# 3 · the spec knob: normalize, blob round-trip, override carry-through


def test_spec_full_uuids_defaults_false_and_normalizes():
    spec = RenderProjectionSpec.normalize_from_glimpse_args(root_uuid=UUID_A)
    assert spec.full_uuids is False
    spec_full = RenderProjectionSpec.normalize_from_glimpse_args(
        root_uuid=UUID_A, full_uuids=True
    )
    assert spec_full.full_uuids is True


def test_full_uuids_round_trips_through_the_wire_blob():
    spec = RenderProjectionSpec.normalize_from_glimpse_args(
        root_uuid=UUID_A, full_uuids=True
    )
    blob = spec.to_render_spec_blob()
    assert blob["full_uuids"] is True
    back = RenderProjectionSpec.from_render_spec_blob(blob)
    assert back.full_uuids is True
    # an OLD blob without the key deserializes to False (the default)
    old = {k: v for k, v in blob.items() if k != "full_uuids"}
    assert RenderProjectionSpec.from_render_spec_blob(old).full_uuids is False
    # and the scope echo stays untouched by the knob (byte-identical contract)
    plain = RenderProjectionSpec.normalize_from_glimpse_args(root_uuid=UUID_A)
    assert "full_uuids" not in plain.to_render_scope_fields()
    assert "full_uuids" not in spec.to_render_scope_fields()


def test_full_uuids_is_carried_through_view_overrides():
    spec = RenderProjectionSpec.normalize_from_glimpse_args(
        root_uuid=UUID_A, full_uuids=True
    )
    overridden = spec.with_view_overrides(headings_only=True)
    assert overridden.full_uuids is True and overridden.headings_only is True
