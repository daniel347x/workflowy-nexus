"""S01 §13.2 — canonical cache-node codec tests."""

from __future__ import annotations

import copy

import pytest

from workflowy_mcp.coherence import (
    IngestionProfile,
    canonicalize_cache_node,
    canonicalize_nodes_export,
    merge_canonical_cache_node,
)
from workflowy_mcp.coherence.errors import CoherenceSchemaError, EffectValidationError

FULL = IngestionProfile.FULL_EXPORT_REPLACE
LIVE = IngestionProfile.LIVE_NODE_MERGE
LISTING = IngestionProfile.CHILD_LISTING_MERGE
LLM = IngestionProfile.LLM_RECOVERY_MERGE
JOURNAL = IngestionProfile.JOURNAL_SEMANTIC
MERGE_PROFILES = (LIVE, LISTING, LLM, JOURNAL)


# ---------------------------------------------------------------------------
# Aliases and precedence
# ---------------------------------------------------------------------------


def test_all_known_aliases_normalize():
    raw = {
        "id": "n1",
        "nm": "the name",
        "no": "the note",
        "parentId": "p1",
        "created": 111,
        "modified": 222,
        "priority": 7,
    }
    node = canonicalize_cache_node(raw, FULL)
    assert node["name"] == "the name"
    assert node["note"] == "the note"
    assert node["parent_id"] == "p1"
    assert node["createdAt"] == 111
    assert node["modifiedAt"] == 222
    assert node["priority"] == 7
    # consumed aliases are not echoed
    for alias in ("nm", "no", "parentId", "created", "modified"):
        assert alias not in node


def test_canonical_key_wins_over_alias_by_presence():
    raw = {"id": "n1", "name": "canonical", "nm": "alias", "note": None, "no": "alias-note"}
    node = canonicalize_cache_node(raw, FULL)
    assert node["name"] == "canonical"
    assert node["note"] is None  # explicit null present on canonical key wins


def test_explicit_null_vs_absent():
    raw = {"id": "n1", "name": None}
    node = canonicalize_cache_node(raw, FULL)
    assert "name" in node and node["name"] is None
    assert "note" not in node  # absent stays absent (sparse canonical form)


def test_cp_completion_flag_semantics():
    """The explicit schema decision (S01 amendment): `completed` is a
    first-class canonical key; legacy `cp` is its alias; `completedAt` is
    never fabricated from the flag."""
    # completedAt present -> cp redundant, consumed
    node = canonicalize_cache_node({"id": "n", "completedAt": 42, "cp": True}, FULL)
    assert node["completedAt"] == 42 and "cp" not in node
    # canonical `completed` wins over cp by presence
    node = canonicalize_cache_node({"id": "n", "completed": True, "cp": False}, FULL)
    assert node["completed"] is True and "cp" not in node
    # cp falsy -> completed=False (consumed); completedAt untouched/absent
    node = canonicalize_cache_node({"id": "n", "cp": False}, FULL)
    assert node["completed"] is False and "cp" not in node and "completedAt" not in node
    # cp truthy -> completed=True (consumed); no fabricated timestamp
    node = canonicalize_cache_node({"id": "n", "cp": True}, FULL)
    assert node["completed"] is True and "cp" not in node and "completedAt" not in node


def test_unknown_keys_preserved_and_input_not_mutated():
    raw = {"id": "n1", "nm": "x", "_carto_meta": {"k": [1, 2]}, "completed": False}
    snapshot = copy.deepcopy(raw)
    node = canonicalize_cache_node(raw, FULL)
    assert node["_carto_meta"] == {"k": [1, 2]}
    assert node["completed"] is False
    assert raw == snapshot


def test_id_required_nonblank_and_normalized():
    with pytest.raises(CoherenceSchemaError):
        canonicalize_cache_node({"name": "x"}, FULL)
    with pytest.raises(CoherenceSchemaError):
        canonicalize_cache_node({"id": "  "}, FULL)
    node = canonicalize_cache_node({"id": 123}, FULL)
    assert node["id"] == "123"


# ---------------------------------------------------------------------------
# Per-profile pinned decoding (W1) — the nested-entity divergence lock
# ---------------------------------------------------------------------------


def test_nested_entity_divergence_lock():
    """INTENDED established behavior (W1): ``&amp;lt;`` decodes to ``<`` under
    FULL_EXPORT_REPLACE (cascading) and to ``&lt;`` under the merge profiles
    (non-cascading).  Both directions are locked as correct."""
    raw = {"id": "n", "name": "a &amp;lt; b &amp;gt; c &amp;amp; d"}
    full = canonicalize_cache_node(raw, FULL)
    assert full["name"] == "a < b > c &amp; d"  # cascading chained replaces
    for profile in (LIVE, LISTING, LLM):
        merged = canonicalize_cache_node(raw, profile)
        assert merged["name"] == "a &lt; b &gt; c &amp; d", profile  # single non-cascading pass


def test_full_export_dewhiten_leaves_other_entities_literal():
    node = canonicalize_cache_node({"id": "n", "note": "x &nbsp; y &amp; z"}, FULL)
    assert node["note"] == "x &nbsp; y & z"


def test_merge_profile_note_decoder_single_layer_html_unescape():
    node = canonicalize_cache_node({"id": "n", "note": "a &amp;lt; b"}, LISTING)
    assert node["note"] == "a &lt; b"  # html.unescape single pass, non-cascading


def test_merge_profile_note_autolink_unwrap():
    wire = {"id": "n", "note": '<a href="https://x.test">https://x.test</a> tail'}
    node = canonicalize_cache_node(wire, LIVE)
    assert node["note"] == "https://x.test tail"
    named = {"id": "n", "note": '<a href="https://x.test">label</a> tail'}
    node = canonicalize_cache_node(named, LIVE)
    assert "label" in node["note"] and "href" in node["note"]  # named links preserved


def test_journal_semantic_zero_dewhitening_and_literal_roundtrip():
    semantic = {"id": "n", "name": "literal & <b>bold</b> &lt;tag&gt;"}
    node = canonicalize_cache_node(semantic, JOURNAL)
    assert node["name"] == "literal & <b>bold</b> &lt;tag&gt;"
    # journal round-trip: semantic literal '&' survives byte-for-byte
    again = canonicalize_cache_node(node, JOURNAL)
    assert again == node


# ---------------------------------------------------------------------------
# Replace vs merge semantics
# ---------------------------------------------------------------------------


def test_full_replace_drops_old_only_fields():
    payload = {"nodes": [{"id": "n", "name": "new"}]}
    canonical = canonicalize_nodes_export(payload, profile=FULL)
    assert canonical["nodes"][0] == {"id": "n", "name": "new"}


def test_partial_merge_preserves_absent_fields_and_unknown_metadata():
    existing = {
        "id": "n",
        "name": "old",
        "note": "keep me",
        "priority": 5,
        "_carto_meta": {"anchor": True},
        "custom": "x",
    }
    incoming = {"id": "n", "name": "new &amp; improved"}
    merged = merge_canonical_cache_node(existing, incoming, profile=LIVE)
    assert merged["name"] == "new & improved"
    assert merged["note"] == "keep me"
    assert merged["priority"] == 5
    assert merged["_carto_meta"] == {"anchor": True}
    assert merged["custom"] == "x"


def test_partial_merge_explicit_null_clears_authoritative_field():
    existing = {"id": "n", "name": "x", "note": "y"}
    merged = merge_canonical_cache_node(existing, {"id": "n", "note": None}, profile=LIVE)
    assert merged["note"] is None
    assert merged["name"] == "x"


def test_partial_endpoint_record_never_erases_omitted_fields():
    existing = {"id": "n", "name": "x", "note": "y", "data": {"layoutMode": "h1"}}
    merged = merge_canonical_cache_node(existing, {"id": "n"}, profile=LISTING)
    assert merged == existing


def test_merge_rejects_replace_profile_and_id_mismatch():
    with pytest.raises(EffectValidationError):
        merge_canonical_cache_node({"id": "n"}, {"id": "n"}, profile=FULL)
    with pytest.raises(EffectValidationError):
        merge_canonical_cache_node({"id": "a"}, {"id": "b"}, profile=LIVE)


def test_merge_inputs_not_mutated():
    existing = {"id": "n", "name": "old"}
    incoming = {"id": "n", "name": "new"}
    existing_snapshot = copy.deepcopy(existing)
    incoming_snapshot = copy.deepcopy(incoming)
    merge_canonical_cache_node(existing, incoming, profile=LIVE)
    assert existing == existing_snapshot
    assert incoming == incoming_snapshot


def test_child_listing_authority_fields():
    # P6-probed listing fields merge in; a non-endpoint canonical field is
    # preserved from existing rather than overwritten.
    existing = {"id": "n", "name": "old", "priority": 5}
    incoming = {"id": "n", "name": "new", "priority": 9, "parent_id": "p"}
    merged = merge_canonical_cache_node(existing, incoming, profile=LISTING)
    assert merged["name"] == "new" and merged["priority"] == 9 and merged["parent_id"] == "p"


# ---------------------------------------------------------------------------
# Whole-export canonicalization
# ---------------------------------------------------------------------------


def test_nodes_export_duplicate_and_missing_ids_rejected():
    with pytest.raises(CoherenceSchemaError):
        canonicalize_nodes_export({"nodes": [{"id": "a"}, {"id": "a"}]}, profile=FULL)
    with pytest.raises(CoherenceSchemaError):
        canonicalize_nodes_export({"nodes": [{"name": "x"}]}, profile=FULL)
    with pytest.raises(CoherenceSchemaError):
        canonicalize_nodes_export({"no_nodes": []}, profile=FULL)


def test_nodes_export_preserves_payload_metadata_and_input():
    payload = {
        "nodes": [{"id": "a", "nm": "x &amp; y"}],
        "_total_fetched_from_api": 1,
    }
    snapshot = copy.deepcopy(payload)
    canonical = canonicalize_nodes_export(payload, profile=FULL)
    assert canonical["_total_fetched_from_api"] == 1
    assert canonical["nodes"][0]["name"] == "x & y"
    assert payload == snapshot


def test_cp_raw_copy_no_coercion_nonboolean():
    """F-3: the cp -> completed alias mapping is a RAW COPY with no boolean
    coercion; coercion policy is an S12 concern."""
    node = canonicalize_cache_node({"id": "n", "cp": 12345}, FULL)
    assert node["completed"] == 12345 and "cp" not in node


# ---------------------------------------------------------------------------
# F-1 fence — decoder single-source conformance (permanent)
# ---------------------------------------------------------------------------


def test_decoder_single_source_conformance():
    """Each pinned decoder's BODY exists at exactly ONE definition site
    (coherence.canonical_node); the production seams are one-line delegates.
    Two definition sites is a failure, not a safety achievement."""
    import inspect
    import pathlib

    from workflowy_mcp import server as _server
    from workflowy_mcp.client.api_client_core import (
        WorkFlowyClientCore,
        _semantic_name_for_tag_merge,
    )

    assert "coherence.canonical_node" in inspect.getsource(WorkFlowyClientCore._dewhiten_text)
    assert "_semantic_name_non_cascading" in inspect.getsource(_semantic_name_for_tag_merge)
    assert "coherence.canonical_node" in inspect.getsource(_server._note_wire_to_semantic)

    src_root = pathlib.Path(__file__).resolve().parents[1] / "src"
    # The pinned cache decoder's signature shape is the 3-entity chained
    # replace sequence on ONE line.  (f8_slice_policy.py carries a
    # deliberately DIFFERENT 4-entity multi-line tag-text dewhiten chain —
    # unrelated to the cache decoder and out of this fence's scope.)
    chain = '.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")'
    chain_sites: list[str] = []
    scan_sites: list[str] = []
    autolink_sites: list[str] = []
    for py in src_root.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        if chain in text:
            chain_sites.append(py.name)
        if 'raw_name.startswith("&amp;", i)' in text:
            scan_sites.append(py.name)
        if "_unwrap_autolink" in text:
            autolink_sites.append(py.name)
    # exactly one implementation site each (coherence/canonical_node.py)
    assert chain_sites == ["canonical_node.py"]
    assert scan_sites == ["canonical_node.py"]
    assert autolink_sites == ["canonical_node.py"]
