from __future__ import annotations

import asyncio

import pytest

from workflowy_mcp import server
from workflowy_mcp.mcp_api_registry import validate_params


@pytest.fixture(autouse=True)
def _boundary_client(monkeypatch):
    """S03-B: Lightning Rod verbs are cache_backed, so the family front wraps
    dispatch in a cache_operation_boundary; install a boundary-capable client
    double."""
    from types import SimpleNamespace

    from coherence_harness import make_boundary_capable

    monkeypatch.setattr(
        server,
        "get_client",
        lambda: make_boundary_capable(SimpleNamespace(), nodes=[]),
    )


def test_lightning_registry_inventory_and_describe_contract():
    registry = server.LIGHTNING_ROD_REGISTRY
    assert [spec.verb for spec in registry.operations()] == [
        "gather",
        "render_tags",
        "render_search",
        "render_symbols",
        "render_nodes",
    ]
    assert registry.resolve("gather").side_effects == ("pure_read", "modal_push")
    for verb in ("render_tags", "render_search", "render_symbols", "render_nodes"):
        spec = registry.resolve(verb)
        assert spec.side_effects == ("render", "modal_push", "artifact_write")
        assert spec.async_mode == "never"
        # [ap:TVIIBN] 4d(a): every render verb declares the loud
        # sort_direction rule. (gather has no sort_direction property, so its
        # schema can never carry one past validation and stays as it was.)
        assert spec.domain_error_codes == (
            "lightning_request_failed",
            server._SORT_DIRECTION_WITHOUT_SORT_BY_ERROR,
        )
    assert registry.resolve("gather").domain_error_codes == ("lightning_request_failed",)

    index = registry.describe_index()
    assert [row["verb"] for row in index["operations"]] == [
        "gather",
        "render_tags",
        "render_search",
        "render_symbols",
        "render_nodes",
        "describe",
    ]
    for retired in (
        "ensure_beacons",
        "remove_beacon_tags",
        "mutate_node_tags",
        "render_lens",
    ):
        assert registry.resolve(retired) is None
        assert retired not in str(index["operations"])

    for spec in registry.operations():
        full = registry.describe_full(spec.verb)
        assert not set(spec.param_aliases).intersection(
            full["params_schema"]["properties"]
        )
        for example in full["examples"]:
            validate_params(spec, example["call"].get("params") or {})

    assert "bare terms are ANDed" in registry.describe_full("render_search")[
        "returns"
    ]["description"]
    assert "full persisted-slice Stage-1 policy" in registry.describe_full(
        "render_tags"
    )["returns"]["description"]
    assert "{uuid, symbol}" in registry.describe_full("render_symbols")[
        "returns"
    ]["description"]


def test_lightning_four_verbs_translate_to_one_legacy_authority(monkeypatch):
    calls: list[dict] = []

    async def impl(**kwargs):
        calls.append(kwargs)
        return f"RAW {kwargs['mode']}"

    monkeypatch.setattr(server, "_lightning_rod_legacy_impl", impl)
    registry = server.build_lightning_rod_registry()

    assert asyncio.run(registry.resolve("gather").handler({"root_handle": "R"})) == "RAW gather"
    # Contract [ap:LQ8RDA]+[ap:DR0C0W]: the tag-search modes (gather /
    # render_tags) default match_scope to 'name' (trailing tag region); this
    # expectation was 'name_and_note' before the flip.
    assert calls[-1] == {
        "mode": "gather",
        "root_uuid": "R",
        "match_scope": "name",
        "descend_policy": "all",
        # [ap:EPR86W] 4b: the depth leg rides the impl kwargs (default True;
        # False is refused loudly at the impl boundary).
        "recursive": True,
        "push_to_modal": False,
        "target_tab": None,
    }

    tags = {
        "root_handle": "R",
        "tags": ["topic"],
        "postfix_policy": "slice",
        "tag_match": "exact",
        "match_scope": "name",
        "descend_policy": "stop_at_child_anchors",
        "as_markdown": False,
        "headings_only": True,
        "sort_by": "created",
        "sort_direction": "desc",
        "recent": "last 4",
        "output_file": "out.md",
        "push_to_modal": True,
        "target_tab": "spine",
    }
    assert asyncio.run(registry.resolve("render_tags").handler(tags)) == "RAW render_tags"
    assert calls[-1] == {
        "mode": "render_tags",
        "root_uuid": "R",
        "match_scope": "name",
        "descend_policy": "stop_at_child_anchors",
        # [ap:EPR86W] 4b: the depth leg rides the impl kwargs (default True;
        # False is refused loudly at the impl boundary).
        "recursive": True,
        "push_to_modal": True,
        "target_tab": "spine",
        "tags": ["topic"],
        "postfix_policy": "slice",
        "tag_match": "exact",
        "as_markdown": False,
        "headings_only": True,
        # [ap:0M6VP8]: the heading-id form rides the impl kwargs (default False).
        "full_uuids": False,
        "sort_by": "created",
        "sort_direction": "desc",
        "since": "last 4",
        # [ap:TVIIBN] 4d(a): the recency basis rides the impl kwargs.
        "recent_by": None,
        "output_file": "out.md",
    }

    search = {
        "root_handle": "R",
        "query": "invoice OR receipt -draft",
        "query_mode": "regex",
    }
    assert asyncio.run(registry.resolve("render_search").handler(search)) == "RAW render_search"
    assert calls[-1]["query"] == search["query"]
    assert calls[-1]["query_mode"] == "regex"
    # F5 guard: the free-text mode (render_search) keeps the name_and_note
    # default -- its match_scope selects a FIELD, not tag semantics.
    assert calls[-1]["match_scope"] == "name_and_note"

    symbols = {
        "symbols": ["handler", {"uuid": "[ap:K7M2QF]", "expand": True}],
        "root_handles": "ROOT",
        "favor": "symbol",
        "recent": "1w",
        "push_to_modal": True,
        "target_tab": "spine",
    }
    assert asyncio.run(registry.resolve("render_symbols").handler(symbols)) == "RAW render_symbols"
    assert calls[-1] == {
        "mode": "render_symbols",
        "push_to_modal": True,
        "target_tab": "spine",
        "symbols": symbols["symbols"],
        "root_uuids": "ROOT",
        "favor": "symbol",
        # 54e8356f(ii): the tier rides the impl kwargs (defaults = today).
        "tier": "slice",
        "context_lines": 5,
        "tail_lines": 0,
        "as_markdown": True,
        "headings_only": False,
        "full_uuids": False,
        "sort_by": None,
        "sort_direction": "asc",
        "since": "1w",
        # [ap:TVIIBN] 4d(a): the recency basis rides the impl kwargs.
        "recent_by": None,
        "output_file": None,
    }


def test_lightning_aliases_validation_and_retired_operation_teaching(monkeypatch):
    calls = []

    async def impl(**kwargs):
        calls.append(kwargs)
        return "OK"

    monkeypatch.setattr(server, "_lightning_rod_legacy_impl", impl)
    assert asyncio.run(
        server.lightning_rod_family_tool(
            "render_tags",
            {"root_uuid": "R", "tags": ["x"], "since": "last 2"},
        )
    ) == "OK"
    assert calls[-1]["root_uuid"] == "R"
    assert calls[-1]["since"] == "last 2"

    symbols = asyncio.run(
        server.lightning_rod_family_tool(
            "render_symbols",
            {"symbols": ["fn"], "root_uuids": ["R"], "since": "1w"},
        )
    )
    assert symbols == "OK"
    assert calls[-1]["root_uuids"] == ["R"]

    bare_tags = asyncio.run(
        server.lightning_rod_family_tool(
            "render_tags", {"root_handle": "R", "tags": "not-a-list"}
        )
    )
    assert bare_tags["error"]["code"] == "invalid_parameter_type"

    conflict = asyncio.run(
        server.lightning_rod_family_tool(
            "render_symbols",
            {"symbols": ["fn"], "root_handles": "R", "root_uuids": ["R"]},
        )
    )
    assert conflict["error"]["code"] == "forbidden_parameter"

    for verb in (
        None,
        "ensure_beacons",
        "remove_beacon_tags",
        "mutate_node_tags",
        "render_lens",
        "typo",
    ):
        result = asyncio.run(server.lightning_rod_family_tool(verb, {}))
        assert result["error"]["code"] in {"missing_verb", "unknown_operation"}
        message = result["error"]["message"]
        assert "tag.batch" in message
        assert "beacon.tags.ensure/remove" in message
        assert "render_tags" in message


def test_lightning_expected_failure_is_losslessly_standardized(monkeypatch):
    legacy = {"success": False, "error": "query failed", "mode": "render_search"}

    async def impl(**_kwargs):
        return legacy

    monkeypatch.setattr(server, "_lightning_rod_legacy_impl", impl)
    result = asyncio.run(
        server.lightning_rod_family_tool(
            "render_search", {"root_handle": "R", "query": "x"}
        )
    )
    assert result["operation"] == "lightning_rod.render_search"
    assert result["error"]["code"] == "lightning_request_failed"
    assert result["error"]["message"] == "query failed"
    assert result["error"]["extra"]["legacy_result"] == legacy


def test_legacy_lightning_rod_delegates_and_keeps_mutation_modes(monkeypatch):
    async def impl(**kwargs):
        return kwargs

    monkeypatch.setattr(server, "_lightning_rod_legacy_impl", impl)
    result = asyncio.run(server.lightning_rod(mode="mutate_node_tags", operations=[]))
    assert result["mode"] == "mutate_node_tags"
    assert result["operations"] == []
