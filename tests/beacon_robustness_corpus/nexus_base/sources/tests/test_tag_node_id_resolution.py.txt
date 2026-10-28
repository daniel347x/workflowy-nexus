"""Regression lock for the ORIGINATING tag-tool UUID-grace bug.

Before the handle-resolution unification, ``_resolve_tag_node_id`` (shared by
workflowy_add_tag / workflowy_remove_tag / workflowy_rename_tag) passed any
UUID-looking value STRAIGHT THROUGH verbatim, on the (false) belief that "the
downstream update_node path keeps its prefix tolerance." It does not --
``update_node`` POSTs the raw id with ZERO prefix tolerance -- so a hallucinated
tail (real first segment, fabricated rest, the dominant real failure mode) hit a
hard API error instead of resolving.

The fix routes ``_resolve_tag_node_id`` through the Layer-3 universal resolver
(prefer='uuid', both domains allowed), whose UUID leg runs the Layer-1 primitive
(exact-hit + shared prefix kernel). These tests pin the fixed behavior while
preserving the tool's ``(resolved_uuid|None, early_dict|None)`` contract.

The server module's ``get_client`` is monkeypatched to a fake client that
exposes the cache accessor and the REAL bound ``resolve_node_handle`` (so the
whole chain -- cache load -> Layer 3 -> Layer 1 prefix grace -> Layer 2 symbol
fallback -- executes for real). ``_rate_limiter`` is None in a fresh import, so
the rate-limit branch is inert.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from workflowy_mcp import server  # noqa: E402
from workflowy_mcp.client.api_client_nexus import WorkFlowyClientNexus  # noqa: E402


FULL_A = "a2e552e3-eb42-4438-ba75-4ab7f849eb4f"
FULL_B = "a2e552e3-ffff-4438-ba75-000000000000"  # shares first segment with A
FULL_C = "bc137d90-1111-2222-3333-444444444444"


class _FakeTagClient:
    """Fake client for the tag path: real cache accessor + real resolver, and a
    symbol engine that resolves nothing (so UUID behavior is isolated)."""

    def __init__(self, node_ids, symbol_result=None):
        self._nodes = [{"id": i, "name": f"node {i[:8]}"} for i in node_ids]
        self._symbol_result = symbol_result or {
            "success": True, "match_count": 0, "candidates": [], "truncated": 0,
        }
        self.symbol_calls = 0

    def _get_nodes_export_cache_nodes(self):
        return list(self._nodes)

    async def read_text_snippet_by_symbol(self, **kwargs):
        self.symbol_calls += 1
        return self._symbol_result

    resolve_node_handle = WorkFlowyClientNexus.resolve_node_handle


def _resolve(node_id, fake, monkeypatch):
    monkeypatch.setattr(server, "get_client", lambda: fake)

    # [ap:7ORCPL] the tag symbol leg now reads the global-suppression list
    # (lazily, via resolve_node_handle's provider); stub the config read so
    # these fake-client tests never need a connected tab.
    async def _suppress():
        return []

    monkeypatch.setattr(server, "_beacon_symbol_suppress_tags", _suppress)
    return asyncio.run(server._resolve_tag_node_id(node_id))


def test_exact_uuid_passes_through(monkeypatch):
    fake = _FakeTagClient([FULL_A, FULL_C])
    resolved, early = _resolve(FULL_A, fake, monkeypatch)
    assert resolved == FULL_A
    assert early is None
    assert fake.symbol_calls == 0


def test_hallucinated_tail_now_gets_prefix_grace(monkeypatch):
    # THE BUG FIX: real first segment, fabricated tail, unique in the cache.
    # OLD behavior: returned verbatim (would then 404 at update_node).
    # NEW behavior: Layer-1 prefix kernel resolves it to the real UUID.
    fake = _FakeTagClient([FULL_A, FULL_C])
    supplied = "a2e552e3-eb42-dead-beef-000000000000"
    resolved, early = _resolve(supplied, fake, monkeypatch)
    assert resolved == FULL_A
    assert early is None


def test_ambiguous_prefix_is_noop_with_candidates(monkeypatch):
    fake = _FakeTagClient([FULL_A, FULL_B, FULL_C])
    supplied = "a2e552e3-0000-0000-0000-000000000000"  # matches A and B
    resolved, early = _resolve(supplied, fake, monkeypatch)
    assert resolved is None
    assert early is not None
    assert early["no_op"] is True
    assert early["resolved"] is False
    assert {c["uuid"] for c in early["candidates"]} == {FULL_A, FULL_B}
    # ambiguity in the UUID leg STOPS -> symbol never consulted
    assert fake.symbol_calls == 0


def test_symbol_fallback_when_uuid_unresolved(monkeypatch):
    # A non-UUID-looking handle (a symbol name) still resolves via the symbol leg.
    sym = {
        "success": True, "match_count": 1,
        "candidates": [{"uuid": FULL_C, "name": "n", "file_path": None,
                        "ast_qualname": None, "line": None, "via": ["name"]}],
        "truncated": 0,
    }
    fake = _FakeTagClient([FULL_A], symbol_result=sym)
    resolved, early = _resolve("my_function", fake, monkeypatch)
    assert resolved == FULL_C
    assert early is None
    assert fake.symbol_calls == 1


def test_unresolved_is_noop_no_candidates(monkeypatch):
    fake = _FakeTagClient([FULL_A])
    resolved, early = _resolve("totally-unknown-thing", fake, monkeypatch)
    assert resolved is None
    assert early is not None
    assert early["no_op"] is True
    assert not early["candidates"]
