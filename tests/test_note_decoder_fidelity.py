"""CF-101 / Unit N02′: bare ampersands survive reads and unrelated writes."""

import asyncio
import json

import pytest

from test_mutation_regions_n05_fences import real_region_probe
from workflowy_mcp import server
from workflowy_mcp.client.api_client_core import WorkFlowyClientCore
from workflowy_mcp.coherence.canonical_node import (
    IngestionProfile,
    _note_wire_to_semantic,
    canonicalize_cache_node,
)
from workflowy_mcp.coherence.operation_cache import cache_operation_boundary


NODE = "f3889711-3f62-411d-af5c-6010f068789b"


PROBE_LINE_3 = "https://example.test/a?x=1&param=2&section=3&copy=4&times=5"
PROBE_LINE_4 = "plain & text, &amp;amp; literal, &amp;lt;tag&amp;gt; literal"
PROBE_LINE_6 = "https://example.com/b?x=1&param=2"
BARE_WIRE = [
    PROBE_LINE_3,
    PROBE_LINE_4,
    PROBE_LINE_6,
    *[
        f"https://example.test/?x=1&{key}{suffix}=2"
        for key in ("section", "param", "copy", "times", "reg", "not", "para")
        for suffix in ("", "more")
    ],
    "trailing &",
    "&&",
    "&#38",
    "&#",
    "literal &amp;amp; beside &param=2",
]


@pytest.mark.parametrize("wire", BARE_WIRE)
def test_bare_wire_round_trip(wire):
    semantic = _note_wire_to_semantic(wire)
    assert WorkFlowyClientCore.encode_for_workflowy_api(semantic, "note") == wire


@pytest.mark.parametrize("wire, semantic", [
    ("&amp; &param=2", "& &param=2"),
    ("&lt;", "<"),
    ("&#38;", "&"),
    ("&#x26;", "&"),
    ("&#X26;", "&"),
    ("&copy;", "©"),
    (PROBE_LINE_4, "plain & text, &amp; literal, &lt;tag&gt; literal"),
])
def test_real_entities_keep_existing_decode(wire, semantic):
    assert _note_wire_to_semantic(wire) == semantic


@pytest.mark.parametrize("url", [
    "https://www.npmjs.com/package/example",
    "https://www.linkedin.com/in/example/",
    PROBE_LINE_6,
])
def test_equal_label_href_auto_links_keep_unwrapping(url):
    # Fixtures by shape for 34444e2b / 43eb9547; no live account reads.
    assert _note_wire_to_semantic(f'<a href="{url}">{url}</a>') == url


def test_auto_link_fingerprint_does_not_collapse_bare_legacy_names():
    wire = '<a href="https://example.test/?x=1&param=2">https://example.test/?x=1¶m=2</a>'
    assert _note_wire_to_semantic(wire) == wire


@pytest.mark.parametrize("profile", [
    IngestionProfile.LIVE_NODE_MERGE,
    IngestionProfile.CHILD_LISTING_MERGE,
    IngestionProfile.LLM_RECOVERY_MERGE,
])
def test_live_ingestion_preserves_bare_query(profile):
    node = canonicalize_cache_node({"id": NODE, "note": PROBE_LINE_3}, profile)
    assert node["note"] == PROBE_LINE_3


@pytest.mark.parametrize("verb", ["append_note", "prepend_note", "search_replace", "note_edits"])
def test_unrelated_line_write_preserves_posted_query(monkeypatch, tmp_path, verb):
    """Exercise the real encoder and POST, not just a captured semantic request."""
    original = PROBE_LINE_6 + "\nother line"
    client, probe, ids = real_region_probe(tmp_path, monkeypatch)
    node_id = ids["source"]
    probe.nodes[node_id]["note"] = original
    posts = []

    async def capture(request, held, required):
        posts.append(json.loads(request.content))

    probe.before_send = capture

    async def resolve(node_id, *, label, required=True, role=None):
        return node_id

    monkeypatch.setattr(server, "_resolve_mutator_node_id", resolve)
    monkeypatch.setattr(server, "_resolve_generic_node_handle", resolve)

    async def run():
        if verb == "append_note":
            result = await server._node_append_note_impl(node_id, "new line")
            assert result["success"] is True
            expected = original + "\nnew line"
        elif verb == "prepend_note":
            result = await server._node_prepend_note_impl(node_id, "new line")
            assert result["success"] is True
            expected = "new line\n" + original
        elif verb == "search_replace":
            await server.edit_node(node_id, note_search="other line", note_replace="changed line")
            expected = PROBE_LINE_6 + "\nchanged line"
        else:
            await server.edit_node(node_id, note_edits=[{"search": "other line", "replace": "changed line"}])
            expected = PROBE_LINE_6 + "\nchanged line"
        assert len(posts) == 1
        assert posts[0]["note"] == expected
        assert PROBE_LINE_6.encode() in posts[0]["note"].encode()
        assert not probe.violations

    async def within_boundary():
        try:
            async with cache_operation_boundary(client, "cf101-fidelity-witness"):
                await run()
        finally:
            await client._client.aclose()

    asyncio.run(within_boundary())
