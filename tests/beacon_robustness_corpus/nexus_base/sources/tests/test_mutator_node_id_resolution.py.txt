"""Lock for the mutator handle-resolution front door (_resolve_mutator_node_id).

Commit 3 routes the mutator MCP tools (workflowy_edit_node / delete_node /
complete_node / uncomplete_node / move_node) through the Layer-3 universal
resolver so they gain the SAME UUID-hallucination grace + symbol acceptance the
tag tools got in Commit 1 -- instead of posting a raw node_id to a
zero-tolerance REST endpoint.

_resolve_mutator_node_id is the shared helper (mirrors _resolve_tag_node_id).
Two modes:
  * required=True  (node_id): must resolve to a UNIQUE UUID; ambiguity/no-match
    RAISE (a mutator that can't pin its target must fail loudly, not no-op).
  * required=False (move_node parent_id): a unique match is adopted; a NO-MATCH
    passes the ORIGINAL value through unchanged (special keys like 'inbox' /
    None-for-root survive); AMBIGUITY still raises.

server.get_client is monkeypatched to a fake client exposing the cache accessor
+ the REAL bound resolve_node_handle, so the whole chain (cache load -> Layer 3
-> Layer 1 prefix grace -> symbol fallback) runs for real. _rate_limiter is None
in a fresh import, so that branch is inert.
"""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from workflowy_mcp import server  # noqa: E402
from workflowy_mcp.client.api_client_nexus import WorkFlowyClientNexus  # noqa: E402


FULL_A = "a2e552e3-eb42-4438-ba75-4ab7f849eb4f"
FULL_B = "a2e552e3-ffff-4438-ba75-000000000000"  # shares first segment with A
FULL_C = "bc137d90-1111-2222-3333-444444444444"


class _FakeClient:
    def __init__(self, node_ids, symbol_result=None):
        self._nodes = [{"id": i, "name": f"node {i[:8]}"} for i in node_ids]
        self._symbol_result = symbol_result or {
            "success": True, "match_count": 0, "candidates": [], "truncated": 0,
        }

    def _get_nodes_export_cache_nodes(self):
        return list(self._nodes)

    async def read_text_snippet_by_symbol(self, **kwargs):
        return self._symbol_result

    resolve_node_handle = WorkFlowyClientNexus.resolve_node_handle


def _resolve(node_id, fake, monkeypatch, **kw):
    monkeypatch.setattr(server, "get_client", lambda: fake)

    # [ap:7ORCPL] the mutator symbol leg now reads the global-suppression
    # list (lazily, via resolve_node_handle's provider); stub the config
    # read so these fake-client tests never need a connected tab.
    async def _suppress():
        return []

    monkeypatch.setattr(server, "_beacon_symbol_suppress_tags", _suppress)
    return asyncio.run(server._resolve_mutator_node_id(node_id, label="test", **kw))


# --- required=True (the primary node_id) ------------------------------------

def test_exact_uuid_resolves(monkeypatch):
    fake = _FakeClient([FULL_A, FULL_C])
    assert _resolve(FULL_A, fake, monkeypatch) == FULL_A


def test_hallucinated_tail_gets_prefix_grace(monkeypatch):
    fake = _FakeClient([FULL_A, FULL_C])
    supplied = "a2e552e3-eb42-dead-beef-000000000000"  # unique prefix of A
    assert _resolve(supplied, fake, monkeypatch) == FULL_A


def test_ambiguous_raises(monkeypatch):
    fake = _FakeClient([FULL_A, FULL_B, FULL_C])
    supplied = "a2e552e3-0000-0000-0000-000000000000"  # matches A and B
    with pytest.raises(RuntimeError) as ei:
        _resolve(supplied, fake, monkeypatch)
    assert "ambiguous" in str(ei.value)


def test_no_match_raises_when_required(monkeypatch):
    fake = _FakeClient([FULL_A])
    with pytest.raises(RuntimeError):
        _resolve("totally-unknown-thing-1234567890", fake, monkeypatch)


def test_empty_required_raises(monkeypatch):
    fake = _FakeClient([FULL_A])
    with pytest.raises(RuntimeError):
        _resolve("", fake, monkeypatch)


def test_symbol_fallback_resolves(monkeypatch):
    sym = {
        "success": True, "match_count": 1,
        "candidates": [{"uuid": FULL_C, "name": "n", "file_path": None,
                        "ast_qualname": None, "line": None, "via": ["name"]}],
        "truncated": 0,
    }
    fake = _FakeClient([FULL_A], symbol_result=sym)
    assert _resolve("my_function", fake, monkeypatch) == FULL_C


# --- required=False (move_node parent_id) -----------------------------------

def test_optional_none_passes_through(monkeypatch):
    fake = _FakeClient([FULL_A])
    assert _resolve(None, fake, monkeypatch, required=False) is None


def test_optional_special_key_passes_through(monkeypatch):
    # 'inbox' is not a UUID and won't resolve -> must pass through unchanged.
    fake = _FakeClient([FULL_A])
    assert _resolve("inbox", fake, monkeypatch, required=False) == "inbox"


def test_optional_real_parent_still_resolves(monkeypatch):
    fake = _FakeClient([FULL_A, FULL_C])
    supplied = "bc137d90-1111-dead-beef-000000000000"  # unique prefix of C
    assert _resolve(supplied, fake, monkeypatch, required=False) == FULL_C


def test_optional_ambiguous_still_raises(monkeypatch):
    # even for the optional handle, an ambiguous prefix is a real mistake.
    fake = _FakeClient([FULL_A, FULL_B])
    supplied = "a2e552e3-0000-0000-0000-000000000000"
    with pytest.raises(RuntimeError):
        _resolve(supplied, fake, monkeypatch, required=False)
