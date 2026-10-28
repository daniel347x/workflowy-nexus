"""Contract + behavior lock for the Layer-3 universal boundary resolver.

``WorkFlowyClientNexus.resolve_node_handle`` is the single arbitration point
for a node handle that could be EITHER a UUID or a source-code SYMBOL. It holds
no resolution logic itself: the UUID leg delegates to the Layer-1 primitive
``resolve_uuid_like_node_handle`` (a module-level free function), and the symbol
leg delegates to ``self.read_text_snippet_by_symbol(..., return_candidates_only
=True)`` (the Layer-2 engine).

These tests exercise the ARBITRATION -- the part with real branching risk:
  * contradictory flag combinations raise,
  * ``prefer`` chooses which domain runs first,
  * a SUCCESS or an AMBIGUITY in the preferred domain STOPS (no fall-through),
  * only an UNRESOLVED preferred domain falls through to the other allowed one.

The symbol leg is faked (a tiny object exposing an async
``read_text_snippet_by_symbol``) so the arbitration is tested in isolation from
the ~250-line real resolver; the UUID leg runs the REAL Layer-1 primitive over
an in-memory ``nodes_by_id`` (it is pure/read-only).

NOTE: written with plain ``asyncio.run(...)`` (no pytest-asyncio dependency, no
asyncio_mode config assumption) so the tests actually execute under the repo's
bare pytest.
"""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from workflowy_mcp.client.api_client_nexus import (  # noqa: E402
    WorkFlowyClientNexus,
)


FULL_A = "a2e552e3-eb42-4438-ba75-4ab7f849eb4f"
FULL_B = "a2e552e3-ffff-4438-ba75-000000000000"  # shares first segment with A
FULL_C = "bc137d90-1111-2222-3333-444444444444"


def _nodes(*ids):
    return {i: {"id": i, "name": f"node {i[:8]}"} for i in ids}


class _FakeSymbolClient:
    """Minimal stand-in exposing only the async symbol engine + the method
    under test (bound from the real class, so we test the REAL arbitration)."""

    def __init__(self, symbol_result):
        self._symbol_result = symbol_result
        self.symbol_calls = 0

    async def read_text_snippet_by_symbol(self, **kwargs):
        self.symbol_calls += 1
        return self._symbol_result

    # bind the real method so arbitration logic is exercised verbatim
    resolve_node_handle = WorkFlowyClientNexus.resolve_node_handle


def _sym(match_count, uuids=()):
    return {
        "success": True,
        "match_count": match_count,
        "candidates": [{"uuid": u, "name": "n", "file_path": None,
                        "ast_qualname": None, "line": None, "via": ["name"]}
                       for u in uuids],
        "truncated": 0,
        "resolved_symbol": "sym",
        "resolved_symbol_kind": "name",
    }


def _run(coro):
    return asyncio.run(coro)


# --- contradiction guards ---------------------------------------------------

def test_nothing_allowed_raises():
    c = _FakeSymbolClient(_sym(0))
    with pytest.raises(ValueError):
        _run(c.resolve_node_handle("x", {}, allow_uuid=False, allow_symbol=False, prefer="uuid"))


def test_prefer_disallowed_domain_raises():
    c = _FakeSymbolClient(_sym(0))
    with pytest.raises(ValueError):
        _run(c.resolve_node_handle("x", _nodes(FULL_A), allow_uuid=True,
                                   allow_symbol=False, prefer="symbol"))


def test_bad_prefer_value_raises():
    c = _FakeSymbolClient(_sym(0))
    with pytest.raises(ValueError):
        _run(c.resolve_node_handle("x", _nodes(FULL_A), allow_uuid=True,
                                   allow_symbol=True, prefer="banana"))


def test_allow_uuid_without_nodes_self_loads(monkeypatch):
    # Commit 1.5: omitting nodes_by_id is now VALID -- the UUID leg self-loads
    # via load_nodes_by_id_from_cache(self). We give the fake a cache accessor returning A,
    # and confirm an exact hit resolves through the self-load path (no map passed).
    c = _FakeSymbolClient(_sym(0))
    c._get_nodes_export_cache_nodes = lambda: [{"id": FULL_A, "name": "a"}]
    out = _run(c.resolve_node_handle(FULL_A, allow_uuid=True,
                                     allow_symbol=True, prefer="uuid"))
    assert out["success"] is True
    assert out["resolved_uuid"] == FULL_A
    assert out["resolved_via"] == "uuid"
    assert c.symbol_calls == 0


def test_self_load_failure_falls_through_to_symbol():
    # If the self-load raises (uninitialized cache), the UUID leg is treated as
    # unresolved and we fall through to the symbol leg -- preserving the tag
    # path's prior graceful degradation.
    def _boom():
        raise RuntimeError("nodes-export cache is not initialized")
    c = _FakeSymbolClient(_sym(1, [FULL_C]))
    c._get_nodes_export_cache_nodes = _boom
    out = _run(c.resolve_node_handle("my_function", allow_uuid=True,
                                     allow_symbol=True, prefer="uuid"))
    assert out["resolved_uuid"] == FULL_C
    assert out["resolved_via"] == "symbol"
    assert c.symbol_calls == 1


# --- prefer='uuid': UUID leg wins, no symbol call on success ----------------

def test_uuid_exact_hit_stops_before_symbol():
    c = _FakeSymbolClient(_sym(1, [FULL_C]))  # symbol WOULD resolve, but must not run
    out = _run(c.resolve_node_handle(FULL_A, _nodes(FULL_A), allow_uuid=True,
                                     allow_symbol=True, prefer="uuid"))
    assert out["success"] is True
    assert out["resolved_uuid"] == FULL_A
    assert out["resolved_via"] == "uuid"
    assert out["input_kind"] == "exact_uuid"
    assert c.symbol_calls == 0  # UUID success => symbol never consulted


def test_uuid_unique_prefix_resolves():
    c = _FakeSymbolClient(_sym(0))
    supplied = "a2e552e3-eb42-dead-beef-000000000000"  # unique prefix of A
    out = _run(c.resolve_node_handle(supplied, _nodes(FULL_A, FULL_C),
                                     allow_uuid=True, allow_symbol=True, prefer="uuid"))
    assert out["resolved_uuid"] == FULL_A
    assert out["resolved_via"] == "uuid"
    assert out["input_kind"] == "uuid_prefix"
    assert c.symbol_calls == 0


# --- prefer='uuid': ambiguity STOPS (never falls through to symbol) ---------

def test_uuid_ambiguous_stops_no_symbol_fallthrough():
    c = _FakeSymbolClient(_sym(1, [FULL_C]))  # symbol would resolve; must NOT be reached
    supplied = "a2e552e3-0000-0000-0000-000000000000"  # matches A and B
    out = _run(c.resolve_node_handle(supplied, _nodes(FULL_A, FULL_B, FULL_C),
                                     allow_uuid=True, allow_symbol=True, prefer="uuid"))
    assert out["success"] is False
    assert out["resolved_uuid"] is None
    assert out["candidates_via"] == "uuid"
    assert {x["uuid"] for x in out["candidates"]} == {FULL_A, FULL_B}
    assert c.symbol_calls == 0  # ambiguity is a decision -> STOP, do not fall through


# --- prefer='uuid': UUID unresolved -> fall through to symbol ----------------

def test_uuid_unresolved_falls_through_to_symbol():
    c = _FakeSymbolClient(_sym(1, [FULL_C]))
    out = _run(c.resolve_node_handle("my_function", _nodes(FULL_A),
                                     allow_uuid=True, allow_symbol=True, prefer="uuid"))
    assert out["success"] is True
    assert out["resolved_uuid"] == FULL_C
    assert out["resolved_via"] == "symbol"
    assert out["input_kind"] == "symbol"
    assert c.symbol_calls == 1  # UUID unresolved => symbol consulted


def test_symbol_ambiguous_after_uuid_miss():
    c = _FakeSymbolClient(_sym(3, [FULL_A, FULL_B, FULL_C]))
    out = _run(c.resolve_node_handle("overloaded", _nodes(FULL_A),
                                     allow_uuid=True, allow_symbol=True, prefer="uuid"))
    assert out["success"] is False
    assert out["resolved_uuid"] is None
    assert out["candidates_via"] == "symbol"
    assert out["match_count"] == 3


def test_all_unresolved():
    c = _FakeSymbolClient(_sym(0))
    out = _run(c.resolve_node_handle("nope", _nodes(FULL_A),
                                     allow_uuid=True, allow_symbol=True, prefer="uuid"))
    assert out["success"] is False
    assert out["resolved_uuid"] is None
    assert not out["candidates"]


# --- prefer='symbol': symbol leg runs first ---------------------------------

def test_prefer_symbol_runs_symbol_first():
    # symbol resolves uniquely; UUID leg (which would also exact-hit) must NOT
    # override the preferred symbol result.
    c = _FakeSymbolClient(_sym(1, [FULL_C]))
    out = _run(c.resolve_node_handle(FULL_A, _nodes(FULL_A), allow_uuid=True,
                                     allow_symbol=True, prefer="symbol"))
    assert out["resolved_uuid"] == FULL_C
    assert out["resolved_via"] == "symbol"
    assert c.symbol_calls == 1


def test_prefer_symbol_unresolved_falls_through_to_uuid():
    c = _FakeSymbolClient(_sym(0))
    out = _run(c.resolve_node_handle(FULL_A, _nodes(FULL_A), allow_uuid=True,
                                     allow_symbol=True, prefer="symbol"))
    assert out["resolved_uuid"] == FULL_A
    assert out["resolved_via"] == "uuid"
    assert c.symbol_calls == 1  # symbol tried first, missed, then UUID


# --- allow_symbol=False: pure UUID boundary ---------------------------------

def test_uuid_only_never_calls_symbol():
    c = _FakeSymbolClient(_sym(1, [FULL_C]))
    out = _run(c.resolve_node_handle("my_function", _nodes(FULL_A),
                                     allow_uuid=True, allow_symbol=False, prefer="uuid"))
    assert out["success"] is False        # symbol not allowed, UUID unresolved
    assert c.symbol_calls == 0
