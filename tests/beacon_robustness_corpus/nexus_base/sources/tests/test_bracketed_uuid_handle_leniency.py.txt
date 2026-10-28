"""Contract lock: bracketed-UUID handle leniency (Dan, 2026).

An agent occasionally wraps a UUID -- or its first-segment prefix -- in
AP-ID-style square brackets ('[a2e552e3]'), conflating the two bracketed
handle grammars. The support is ONE pure edge normalizer
(``unwrap_bracketed_uuid_handle``) consumed at exactly TWO originators:

  1. ``resolve_uuid_like_node_handle`` -- the Layer-1 confluence point every
     funnel inherits (anchor dock verbs via ``_resolve_generic_node_handle`` /
     ``resolve_uuid_or_anchor_point_handle``, Layer-3 ``resolve_node_handle``,
     glimpse roots, scry, grace-resolve);
  2. the Lightning Rod ``render_symbols`` item ROUTER
     (``_resolve_lightning_rod_symbol_items_from_cache``), whose
     ``_uuid_shaped`` classification runs BEFORE Layer-1 (its routing tests
     live in test_lightning_rod_render_symbols_resolves.py).

The grammar is deliberately narrow so NO handle is stolen from the AP-ID
domain: unwrap only when the WHOLE (whitespace-stripped) candidate is
``[...]``-wrapped and the interior is pure hex (+ optional hyphens) with
>= 8 normalized chars (the full first segment, matching the kernel's
``min_prefix_len`` floor). So:

  * ``[ap:K7M2QF]`` -- colon => never unwrapped (AP leg, unchanged);
  * ``[K7M2QF]``    -- non-hex => never unwrapped (bracketed-bare AP core,
    already accepted by ``_normalize_ap_id_input``, unchanged);
  * ``[abc123]``    -- < 8 normalized chars => never unwrapped (AP domain);
  * ``[A1B2C3D4]``  -- pure hex >= 8: the UUID leg tries FIRST (documented
    precedence); on no UUID match it falls through to the AP leg, which
    still resolves a node named ``[ap:A1B2C3D4] ...`` -- locked below.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from workflowy_mcp.client.api_client_nexus import (  # noqa: E402
    resolve_uuid_like_node_handle,
    resolve_uuid_or_anchor_point_handle,
    unwrap_bracketed_uuid_handle,
)

# --- fixtures (same shape as test_uuid_handle_resolver.py) -------------------

FULL_A = "a2e552e3-eb42-4438-ba75-4ab7f849eb4f"
FULL_B = "a2e552e3-ffff-4438-ba75-000000000000"  # shares first segment with A
FULL_C = "bc137d90-1111-2222-3333-444444444444"


def _nodes(*entries):
    """{uuid: node} from uuid strings or (uuid, name) tuples."""
    out = {}
    for entry in entries:
        uid, name = entry if isinstance(entry, tuple) else (entry, None)
        out[uid] = {"id": uid, "name": name or f"node {uid[:8]}"}
    return out


# --- the pure unwrapper -------------------------------------------------------

def test_unwrap_full_dashed_uuid():
    assert unwrap_bracketed_uuid_handle(f"[{FULL_A}]") == FULL_A


def test_unwrap_full_undashed_uuid():
    undashed = FULL_A.replace("-", "")
    assert unwrap_bracketed_uuid_handle(f"[{undashed}]") == undashed


def test_unwrap_first_segment_prefix():
    assert unwrap_bracketed_uuid_handle("[bc137d90]") == "bc137d90"


def test_unwrap_partial_hex_with_hyphens():
    assert unwrap_bracketed_uuid_handle("[a2e552e3-eb42]") == "a2e552e3-eb42"


def test_unwrap_tolerates_surrounding_whitespace():
    assert unwrap_bracketed_uuid_handle("  [bc137d90]  ") == "bc137d90"


def test_unwrap_is_idempotent():
    once = unwrap_bracketed_uuid_handle("[bc137d90]")
    assert unwrap_bracketed_uuid_handle(once) == once


def test_ap_forms_never_unwrapped():
    assert unwrap_bracketed_uuid_handle("[ap:K7M2QF]") == "[ap:K7M2QF]"
    assert unwrap_bracketed_uuid_handle("ap:K7M2QF") == "ap:K7M2QF"


def test_bracketed_non_hex_core_never_unwrapped():
    # 'K' / 'Q' are not hex => bracketed-bare AP core stays AP-domain.
    assert unwrap_bracketed_uuid_handle("[K7M2QF]") == "[K7M2QF]"


def test_short_bracketed_hex_never_unwrapped():
    # < 8 normalized chars => AP-domain exclusive (kernel-floor parity).
    assert unwrap_bracketed_uuid_handle("[abc123]") == "[abc123]"


def test_non_hex_letter_bracketed_never_unwrapped():
    assert unwrap_bracketed_uuid_handle("[g2e552e3]") == "[g2e552e3]"


def test_interior_whitespace_never_unwrapped():
    assert unwrap_bracketed_uuid_handle("[ bc137d90 ]") == "[ bc137d90 ]"


def test_unbracketed_passes_through_unchanged():
    assert unwrap_bracketed_uuid_handle("bc137d90") == "bc137d90"
    assert unwrap_bracketed_uuid_handle("") == ""


def test_non_string_passes_through_verbatim():
    assert unwrap_bracketed_uuid_handle(None) is None
    assert unwrap_bracketed_uuid_handle(42) == 42


# --- Layer-1 confluence point --------------------------------------------------

def test_layer1_bracketed_exact_hit():
    nodes = _nodes(FULL_A, FULL_C)
    res = resolve_uuid_like_node_handle(f"[{FULL_A}]", nodes)
    assert res["success"] is True
    assert res["resolved_uuid"] == FULL_A
    assert res["input_kind"] == "exact_uuid"


def test_layer1_bracketed_unique_prefix():
    nodes = _nodes(FULL_A, FULL_C)
    res = resolve_uuid_like_node_handle(f"[{FULL_C[:8]}]", nodes)
    assert res["success"] is True
    assert res["resolved_uuid"] == FULL_C
    assert res["input_kind"] == "uuid_prefix"


def test_layer1_bracketed_ambiguous_prefix_stops_with_candidates():
    nodes = _nodes(FULL_A, FULL_B)
    res = resolve_uuid_like_node_handle(f"[{FULL_A[:8]}]", nodes)
    assert res["success"] is False
    assert res.get("resolved_uuid") is None
    assert len(res["candidates"]) == 2


def test_layer1_short_bracketed_hex_unresolved():
    # '[bc137]' is NOT unwrapped (< 8 hex chars), and the bracketed form is
    # not a cache key => unresolved, exactly like any other non-UUID input.
    nodes = _nodes(FULL_C)
    res = resolve_uuid_like_node_handle("[bc137]", nodes)
    assert res["success"] is False
    assert not res.get("candidates")


def test_layer1_unbracketed_behavior_unchanged():
    # NO-BEHAVIOR-CHANGE lock: unbracketed handles resolve exactly as before.
    nodes = _nodes(FULL_A, FULL_C)
    exact = resolve_uuid_like_node_handle(FULL_A, nodes)
    assert exact["success"] is True and exact["input_kind"] == "exact_uuid"
    prefix = resolve_uuid_like_node_handle(FULL_C[:8], nodes)
    assert prefix["success"] is True and prefix["input_kind"] == "uuid_prefix"


# --- narrow funnel (UUID-or-AP) precedence --------------------------------------

def test_funnel_bracketed_prefix_resolves_via_uuid():
    nodes = _nodes(FULL_C)
    res = resolve_uuid_or_anchor_point_handle("[bc137d90]", nodes)
    assert res["success"] is True
    assert res["resolved_uuid"] == FULL_C
    assert res["resolved_via"] == "uuid"
    assert res["input_kind"] == "uuid_prefix"


def test_funnel_ap_form_still_resolves_via_anchor_point():
    nodes = _nodes((FULL_A, "[ap:TESTAP1] Anchor node"))
    res = resolve_uuid_or_anchor_point_handle("[ap:TESTAP1]", nodes)
    assert res["success"] is True
    assert res["resolved_uuid"] == FULL_A
    assert res["resolved_via"] == "anchor_point"
    assert res["input_kind"] == "anchor_point"


def test_funnel_pure_hex_bracketed_core_falls_through_to_ap_leg():
    # THE NO-STOLEN-DOMAIN LOCK: '[A1B2C3D4]' is valid bracketed hex, so the
    # UUID leg unwraps and tries it FIRST -- no cache UUID matches -- and the
    # handle then falls through to the AP leg, which resolves the node named
    # '[ap:A1B2C3D4] ...'. Both domains are tried, in the documented order.
    nodes = _nodes(FULL_C, (FULL_A, "[ap:A1B2C3D4] Mnemonic AP node"))
    res = resolve_uuid_or_anchor_point_handle("[A1B2C3D4]", nodes)
    assert res["success"] is True
    assert res["resolved_uuid"] == FULL_A
    assert res["resolved_via"] == "anchor_point"


def test_funnel_bracketed_ambiguous_prefix_stops_never_tries_ap():
    # AMBIGUOUS-STOPS invariant survives the leniency: a bracketed prefix
    # matching TWO UUIDs returns uuid candidates and NEVER falls through to
    # the AP leg, even when a same-core AP node exists as a decoy.
    nodes = _nodes(FULL_A, FULL_B, (FULL_C, "[ap:A2E552E3] decoy"))
    res = resolve_uuid_or_anchor_point_handle("[a2e552e3]", nodes)
    assert res["success"] is False
    assert res.get("candidates_via") == "uuid"
    assert len(res["candidates"]) == 2
