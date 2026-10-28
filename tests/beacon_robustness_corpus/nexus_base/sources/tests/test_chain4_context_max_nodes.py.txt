"""Audit E/WO-2: the public grep-tier boundaries admit the existing node cap."""
from __future__ import annotations

import asyncio

import pytest

from tests.test_render_search_sift_parity import (
    cache,
    _install_lens_snapshot,
    _SUBTREE_LENS,
)
from workflowy_mcp import server
from workflowy_mcp.mcp_api_registry import dispatch_operation


def _request(surface):
    if surface == "sift":
        # Deliberately omit tier: context is sift's default.
        return server.build_slice_group_registry(), "sift", {"lens": dict(_SUBTREE_LENS)}
    return server.build_lightning_rod_registry(), "render_search", {
        "root_handle": "R", "tier": "context",
    }


@pytest.mark.parametrize("surface", ["sift", "render_search"])
@pytest.mark.parametrize("structured", [False, True])
def test_context_max_nodes_dispatch_caps_matching_node_groups(cache, monkeypatch, surface, structured):
    # The shared producer always carries a root row. Make that row a genuine
    # match and give exactly one other node a hit, so both surfaces have two
    # matching documents without testing pre-existing connective-row policy.
    cache["R"]["note"] = "chain4hit root"
    cache["X"]["note"] = "chain4hit leaf"
    _install_lens_snapshot(monkeypatch, cache)
    registry, verb, params = _request(surface)
    result = asyncio.run(dispatch_operation(registry, verb, {
        **params, "query": "chain4hit", "max_nodes": 1,
        "context_lines": 0, "as_markdown": not structured,
    }))
    if structured:
        assert result["success"] is True, result
        assert result["returned_nodes"] == 1
        assert result["matching_nodes"] == 2
        assert result["nodes_hidden_by_max_nodes"] == 1
        report = result["text"]
    else:
        assert isinstance(result, str), result
        report = result
    assert report.count("Matched ") == 1
    assert "Matched 1 lines in R " in report
    assert "chain4hit root" in report
    assert "chain4hit leaf" not in report
    assert "nodes_hidden_by_max_nodes=1" in report


@pytest.mark.parametrize("surface", ["sift", "render_search"])
def test_context_max_nodes_push_stays_refused(cache, monkeypatch, surface):
    _install_lens_snapshot(monkeypatch, cache)
    registry, verb, params = _request(surface)
    result = asyncio.run(dispatch_operation(registry, verb, {
        **params, "query": "deploy", "max_nodes": 1, "push_to_modal": True,
    }))
    assert result["success"] is False
    # Preserve the existing render-family envelope (its shaper maps guard
    # codes to the declared family failure); WO-2 does not change that owner.
    expected = "invalid_parameter_combination" if surface == "sift" else "lightning_request_failed"
    assert result["error"]["code"] == expected
    assert "cannot push_to_modal" in result["error"]["message"]


@pytest.mark.parametrize("surface", ["sift", "render_search"])
def test_context_max_nodes_uses_existing_value_normalizer(cache, monkeypatch, surface):
    _install_lens_snapshot(monkeypatch, cache)
    registry, verb, params = _request(surface)
    result = asyncio.run(dispatch_operation(registry, verb, {
        **params, "query": "deploy", "max_nodes": 0,
    }))
    assert result["success"] is False
    expected = "invalid_parameter_value" if surface == "sift" else "lightning_request_failed"
    assert result["error"]["code"] == expected
    assert "max_nodes" in result["error"]["message"]
    assert "push_to_modal" not in result["error"]["message"]
