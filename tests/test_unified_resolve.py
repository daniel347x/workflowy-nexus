"""[ap:CZ95R6] + [ap:3UCTP0] — one shared resolve implementation, three
per-family verbs, one identifier-vocabulary triple.

Dan's REVISED 074 ruling: no single verb; ``node.resolve`` is new;
``anchor.point.resolve`` and ``beacon.resolve`` keep their verbs; all three
share ONE implementation (``resolve_node_identity_record`` over the ONE
prefix kernel — no second resolver), accept any identifier form
(``[ap:CORE]`` | 8-hex short UUID | full UUID, plus symbols for beacon),
return ``{short, uuid, ap_id | "no anchor point available"}``, and differ
ONLY in fallback order and wording:

* ``node.resolve``: UUID-first, AP-ID on fall-through;
* ``anchor.point.resolve``: AP-ID-first, UUID on fall-through;
* ``beacon.resolve``: symbol-first, with U1's landed gates (a UUID-shaped
  handle routes to the UUID leg first; an explicitly AP-shaped handle takes
  the AP leg first — neither needs a connected tab).
"""

from __future__ import annotations

import asyncio

import pytest

from workflowy_mcp import server
from workflowy_mcp.mcp_api_registry import dispatch_operation, validate_params

FULL_A = "1edcc8a4-aaaa-4444-8888-000000000001"
FULL_B = "1edcc8a4-bbbb-4444-8888-000000000002"
FULL_HEX = "654208c1-1111-4222-8333-444444444444"
FULL_AP = "aaaaaaaa-1111-4222-8333-444444444444"

TRIPLE_KEYS = ("short", "uuid", "ap_id")


def _cache(*nodes: dict) -> dict[str, dict]:
    return {str(n["id"]): n for n in nodes}


def _identity(record: dict) -> dict:
    return {key: record[key] for key in TRIPLE_KEYS}


# ---------------------------------------------------------------------------
# node.resolve — the new verb
# ---------------------------------------------------------------------------


def test_node_resolve_returns_the_triple_for_every_form(monkeypatch):
    cache = _cache({"id": FULL_A, "name": "[ap:K7M2QF] 📋 Widget", "note": ""})
    monkeypatch.setattr(server, "_current_cache_by_id", lambda: cache)
    registry = server.build_node_registry()

    records = {}
    for handle in (
        FULL_A,
        "1edcc8a4",
        "[ap:K7M2QF]",
        "ap:K7M2QF",
        "K7M2QF",  # bare AP core: the UUID leg cannot match non-hex
    ):
        records[handle] = asyncio.run(
            dispatch_operation(registry, "resolve", {"node_handle": handle})
        )

    first = records[FULL_A]
    assert first["success"] is True
    assert first["match_count"] == 1
    assert first["resolved_uuid"] == FULL_A
    assert _identity(first) == {
        "short": "1edcc8a4",
        "uuid": FULL_A,
        "ap_id": "[ap:K7M2QF]",
    }
    assert first["candidates"] == [
        {
            "uuid": FULL_A,
            "name": "[ap:K7M2QF] 📋 Widget",
            "short": "1edcc8a4",
            "ap_id": "[ap:K7M2QF]",
        }
    ]
    # The vocabulary is identical whatever form was passed in.
    for handle, record in records.items():
        assert _identity(record) == _identity(first), handle
        assert record["match_count"] == 1
    # Only the fallback order differs: UUID forms resolve via the UUID leg,
    # AP forms (including the bare core) via the AP-ID leg.
    assert records[FULL_A]["resolved_via"] == "uuid"
    assert records["1edcc8a4"]["resolved_via"] == "uuid"
    assert records["[ap:K7M2QF]"]["resolved_via"] == "anchor_point"
    assert records["K7M2QF"]["resolved_via"] == "anchor_point"
    assert records["[ap:K7M2QF]"]["resolved_ap_id"] == "K7M2QF"


def test_node_resolve_no_anchor_point_says_so_in_words(monkeypatch):
    cache = _cache({"id": FULL_A, "name": "Plain node", "note": ""})
    monkeypatch.setattr(server, "_current_cache_by_id", lambda: cache)
    registry = server.build_node_registry()
    record = asyncio.run(
        dispatch_operation(registry, "resolve", {"node_handle": "1edcc8a4"})
    )
    assert record["success"] is True
    assert record["ap_id"] == "no anchor point available"
    assert record["candidates"][0]["ap_id"] == "no anchor point available"
    assert record["short"] == "1edcc8a4" and record["uuid"] == FULL_A


def test_node_resolve_ambiguous_prefix_enriches_every_row_and_never_picks(monkeypatch):
    cache = _cache(
        {"id": FULL_A, "name": "[ap:AAAAAA] Alpha", "note": ""},
        {"id": FULL_B, "name": "Beta", "note": ""},
    )
    monkeypatch.setattr(server, "_current_cache_by_id", lambda: cache)
    registry = server.build_node_registry()
    record = asyncio.run(
        dispatch_operation(registry, "resolve", {"node_handle": "1edcc8a4"})
    )
    assert record["success"] is True
    assert record["match_count"] == 2
    assert "resolved_uuid" not in record  # never a silent pick
    # short is the SHORTEST UNIQUE handle (CF-22/O3): the two rows share the
    # 8-hex prefix 1edcc8a4, so each candidate's short is lengthened one
    # character -- the same short form rendered headings print.
    assert [row["short"] for row in record["candidates"]] == [
        "1edcc8a4a",
        "1edcc8a4b",
    ]
    assert record["candidates"][0]["ap_id"] == "[ap:AAAAAA]"
    assert record["candidates"][1]["ap_id"] == "no anchor point available"


def test_node_resolve_unresolved_is_a_nonraising_zero_record(monkeypatch):
    cache = _cache({"id": FULL_A, "name": "Plain", "note": ""})
    monkeypatch.setattr(server, "_current_cache_by_id", lambda: cache)
    registry = server.build_node_registry()
    record = asyncio.run(
        dispatch_operation(registry, "resolve", {"node_handle": "ffffffff"})
    )
    assert record["success"] is True
    assert record["match_count"] == 0
    assert record["candidates"] == []
    assert "short" not in record and "uuid" not in record and "ap_id" not in record


def test_node_resolve_blank_handle_is_a_typed_error(monkeypatch):
    monkeypatch.setattr(server, "_current_cache_by_id", lambda: {})
    registry = server.build_node_registry()
    record = asyncio.run(dispatch_operation(registry, "resolve", {"node_handle": "  "}))
    assert record["error"]["code"] == "invalid_parameter_combination"


# ---------------------------------------------------------------------------
# Per-family fallback ORDER — the bare-core-both-domains case
# ---------------------------------------------------------------------------


def test_per_family_order_on_a_bare_core_that_is_both_ap_and_hex(monkeypatch):
    cache = _cache(
        {"id": FULL_HEX, "name": "Plain node", "note": ""},
        {"id": FULL_AP, "name": "[ap:654208C1] AP carrier", "note": ""},
    )
    monkeypatch.setattr(server, "_current_cache_by_id", lambda: cache)
    monkeypatch.setattr(server, "_anchor_cache_by_id", lambda: cache)
    node_registry = server.build_node_registry()
    anchor_registry = server.build_anchor_registry()

    # node.resolve: UUID-first -> the hex prefix wins.
    via_node = asyncio.run(
        dispatch_operation(node_registry, "resolve", {"node_handle": "654208c1"})
    )
    assert via_node["resolved_via"] == "uuid"
    assert via_node["uuid"] == FULL_HEX
    assert via_node["ap_id"] == "no anchor point available"

    # anchor.point.resolve: AP-first -> the AP carrier wins.
    via_anchor = asyncio.run(
        dispatch_operation(anchor_registry, "point.resolve", {"ap_id": "654208c1"})
    )
    assert via_anchor["resolved_via"] == "anchor_point"
    assert via_anchor["uuid"] == FULL_AP
    assert via_anchor["ap_id"] == "[ap:654208C1]"
    assert via_anchor["resolved_ap_id"] == "654208C1"


def test_anchor_point_resolve_accepts_uuid_forms_and_preserves_legacy_keys(monkeypatch):
    cache = _cache({"id": FULL_A, "name": "[ap:K7M2QF] Widget", "note": ""})
    monkeypatch.setattr(server, "_anchor_cache_by_id", lambda: cache)
    registry = server.build_anchor_registry()

    by_uuid = asyncio.run(
        dispatch_operation(registry, "point.resolve", {"ap_id": FULL_A})
    )
    assert by_uuid["success"] is True
    assert by_uuid["match_count"] == 1
    assert by_uuid["resolved_uuid"] == FULL_A
    assert by_uuid["resolved_via"] == "uuid"
    assert by_uuid["resolved_ap_id"] is None  # no AP leg produced the match
    assert _identity(by_uuid) == {
        "short": "1edcc8a4",
        "uuid": FULL_A,
        "ap_id": "[ap:K7M2QF]",
    }
    # node_handle is accepted as the alias of ap_id (the shared handle name).
    by_alias = asyncio.run(
        dispatch_operation(registry, "point.resolve", {"node_handle": "1edcc8a4"})
    )
    assert by_alias["uuid"] == FULL_A

    # Established 0/N shape preserved: success stays True, candidates list.
    zero = asyncio.run(
        dispatch_operation(registry, "point.resolve", {"ap_id": "[ap:ZZZZZZ]"})
    )
    assert zero["success"] is True
    assert zero["match_count"] == 0
    assert zero["candidates"] == []
    assert "resolved_ap_id" in zero


# ---------------------------------------------------------------------------
# beacon.resolve — AP leg + the triple, without needing a tab
# ---------------------------------------------------------------------------


class _BeaconClient:
    def __init__(self, *nodes: dict):
        self._nodes = list(nodes)
        self.symbol_calls = 0

    def _get_nodes_export_cache_nodes(self):
        return list(self._nodes)

    async def read_uuid_from_symbol(self, **kwargs):
        self.symbol_calls += 1
        raise AssertionError("symbol leg must not run in this test")


def _beacon_registry(monkeypatch, client):
    async def _no_tab():
        raise AssertionError("suppress list must not be read in this test")

    monkeypatch.setattr(server, "_beacon_symbol_suppress_tags", _no_tab)
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "_rate_limiter", None)
    return server.build_beacon_registry()


def test_beacon_resolve_ap_id_resolves_without_a_tab(monkeypatch):
    client = _BeaconClient({"id": FULL_A, "name": "[ap:K7M2QF] Widget", "note": ""})
    registry = _beacon_registry(monkeypatch, client)
    record = asyncio.run(
        dispatch_operation(registry, "resolve", {"source_handle": "[ap:K7M2QF]"})
    )
    assert record["success"] is True
    assert record["match_count"] == 1
    assert record["resolved_via"] == "anchor_point"
    assert _identity(record) == {
        "short": "1edcc8a4",
        "uuid": FULL_A,
        "ap_id": "[ap:K7M2QF]",
    }
    assert client.symbol_calls == 0  # an explicit AP shape never scans symbols


def test_beacon_resolve_symbol_unique_carries_triple_and_descriptors(monkeypatch):
    class _SymbolClient:
        def _get_nodes_export_cache_nodes(self):
            return [{"id": FULL_A, "name": "alpha_fn", "note": ""}]

        async def read_uuid_from_symbol(self, **kwargs):
            return {
                "success": True,
                "match_count": 1,
                "resolved_symbol": "alpha_fn",
                "resolved_symbol_kind": "ast",
                "candidates": [
                    {
                        "uuid": FULL_A,
                        "name": "alpha_fn",
                        "file_path": "X.py",
                        "line": 12,
                        "via": "ast",
                    }
                ],
                "truncated": 0,
            }

    async def _empty():
        return []

    monkeypatch.setattr(server, "_beacon_symbol_suppress_tags", _empty)
    client = _SymbolClient()
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "_rate_limiter", None)
    registry = server.build_beacon_registry()
    record = asyncio.run(
        dispatch_operation(registry, "resolve", {"source_handle": "alpha_fn"})
    )
    assert record["success"] is True
    assert record["match_count"] == 1
    assert record["resolved_via"] == "symbol"
    assert _identity(record) == {
        "short": "1edcc8a4",
        "uuid": FULL_A,
        "ap_id": "no anchor point available",
    }
    # The symbol leg's richer descriptors ride along.
    assert record["resolved_symbol"] == "alpha_fn"
    assert record["resolved_symbol_kind"] == "ast"
    assert record["candidates"][0]["file_path"] == "X.py"
    assert record["candidates"][0]["short"] == "1edcc8a4"


# ---------------------------------------------------------------------------
# ONE shared implementation — the same triple from all three verbs
# ---------------------------------------------------------------------------


def test_the_three_verbs_return_the_same_triple(monkeypatch):
    cache = _cache({"id": FULL_A, "name": "[ap:K7M2QF] Widget", "note": ""})
    monkeypatch.setattr(server, "_current_cache_by_id", lambda: cache)
    monkeypatch.setattr(server, "_anchor_cache_by_id", lambda: cache)
    client = _BeaconClient({"id": FULL_A, "name": "[ap:K7M2QF] Widget", "note": ""})
    beacon_registry = _beacon_registry(monkeypatch, client)

    via_node = asyncio.run(
        dispatch_operation(server.build_node_registry(), "resolve", {"node_handle": "1edcc8a4"})
    )
    via_anchor = asyncio.run(
        dispatch_operation(server.build_anchor_registry(), "point.resolve", {"ap_id": "[ap:K7M2QF]"})
    )
    via_beacon = asyncio.run(
        dispatch_operation(beacon_registry, "resolve", {"source_handle": "[ap:K7M2QF]"})
    )
    expected = {"short": "1edcc8a4", "uuid": FULL_A, "ap_id": "[ap:K7M2QF]"}
    assert _identity(via_node) == expected
    assert _identity(via_anchor) == expected
    assert _identity(via_beacon) == expected


# ---------------------------------------------------------------------------
# Registry / describe surface ([ap:3UCTP0])
# ---------------------------------------------------------------------------


def test_node_resolve_spec_is_registered_and_described():
    registry = server.build_node_registry()
    spec = registry.resolve("resolve")
    assert spec.verb == "resolve"
    assert spec.side_effects == ("pure_read",)
    assert list(spec.params_schema["required"]) == ["node_handle"]
    assert spec.param_aliases == {"node_id": "node_handle"}
    full = registry.describe_full("resolve")
    assert "no anchor point available" in full["returns"]["description"]
    assert "8-hex" in full["summary"]
    assert "never a prerequisite" in full["summary"]
    for example in full["examples"]:
        validate_params(spec, example["call"].get("params") or {})
    # The verb list of the family carries it (SH1F1F item 11, automatic).
    assert "resolve" in [spec.verb for spec in registry.operations()]


def test_point_and_beacon_resolve_specs_teach_their_order_and_the_never_rule():
    anchor_registry = server.build_anchor_registry()
    point_spec = anchor_registry.resolve("point.resolve")
    assert point_spec.param_aliases == {"node_handle": "ap_id"}
    assert "Never needed before an ordinary" in point_spec.summary
    assert "AP-ID-first" in point_spec.summary

    beacon_registry = server.build_beacon_registry()
    resolve_spec = beacon_registry.resolve("resolve")
    assert "[ap:CORE]" in resolve_spec.params_schema["properties"]["source_handle"]["description"]
    assert "Symbol-first" in resolve_spec.summary
    assert "no anchor point available" in resolve_spec.returns["description"]


def test_handle_grammar_names_the_8hex_segment_everywhere():
    for build in (
        server.build_node_registry,
        server.build_anchor_registry,
        server.build_tag_registry,
        server.build_slice_group_registry,
        server.build_beacon_registry,
        server.build_glimpse_registry,
        server.build_lightning_rod_registry,
    ):
        registry = build()
        grammar = registry.describe_index().get("handle_grammar") or ""
        assert "8-hex" in grammar, registry.family


def test_server_instructions_teach_the_never_resolve_rule():
    instructions = server._SERVER_INSTRUCTIONS
    assert "8-hex first segment" in instructions
    assert "never point.resolve an AP-ID just to get a UUID" in instructions


def test_all_handle_fronts_share_layer3_arbitration(monkeypatch):
    """WO-1: patch THE arbiter, not primitives common to two private loops."""
    from workflowy_mcp.client.api_client_nexus import WorkFlowyClientNexus

    original = WorkFlowyClientNexus.resolve_node_handle
    calls = []

    async def tracking(self, candidate, *args, **kwargs):
        calls.append((candidate, kwargs.get("domain_order"), kwargs.get("_with_leg_details", False)))
        return await original(self, candidate, *args, **kwargs)

    monkeypatch.setattr(WorkFlowyClientNexus, "resolve_node_handle", tracking)

    class Client(_BeaconClient):
        resolve_node_handle = tracking

    cache = _cache({"id": FULL_A, "name": "[ap:K7M2QF] Widget", "note": ""})
    client = Client(*cache.values())
    monkeypatch.setattr(server, "_current_cache_by_id", lambda: cache)
    monkeypatch.setattr(server, "_anchor_cache_by_id", lambda: cache)
    registry = _beacon_registry(monkeypatch, client)

    async def check():
        for name, operation in (
            ("node.resolve", lambda: dispatch_operation(server.build_node_registry(), "resolve", {"node_handle": FULL_A})),
            ("anchor.point.resolve", lambda: dispatch_operation(server.build_anchor_registry(), "point.resolve", {"ap_id": "[ap:K7M2QF]"})),
            ("beacon.resolve UUID", lambda: dispatch_operation(registry, "resolve", {"source_handle": FULL_A})),
            ("beacon.resolve AP", lambda: dispatch_operation(registry, "resolve", {"source_handle": "[ap:K7M2QF]"})),
            ("tag front", lambda: server._resolve_tag_node_id(FULL_A)),
            ("mutator front", lambda: server._resolve_mutator_node_id(FULL_A, label="structural test")),
        ):
            before = len(calls)
            result = await operation()
            assert len(calls) == before + 1, name
            if isinstance(result, dict):
                assert result["resolved_uuid"] == FULL_A, name
            elif isinstance(result, tuple):
                assert result == (FULL_A, None), name
            else:
                assert result == FULL_A, name

    asyncio.run(check())
    assert [c[1] for c in calls] == [
        None, None, ("uuid", "symbol", "anchor_point"),
        ("anchor_point", "symbol", "uuid"), None, None,
    ]
    assert [c[2] for c in calls] == [True, True, True, True, False, False]
