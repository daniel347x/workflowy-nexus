"""`lightning_rod.render_nodes` ([ap:OSQHRG]) -- render_symbols NAMED FOR NODES.

The verb is a discoverable sibling of ``render_symbols``: the same render
path, but ``root_handles`` is OPTIONAL. Without it, every listed item must
resolve AS A NODE (UUID / unique prefix / bracketed hex / [ap:CORE], via the
ONE shared ``resolve_uuid_or_anchor_point_handle``) and the resolved nodes
themselves become the scope -- so one node renders ALONE with its note (a
single root's LCA is itself; no ancestor chain rides along) and several nodes
connect through their LCA spine. A ``{symbol: ...}`` item or an unresolvable
string without ``root_handles`` is a LOUD teaching error (symbols need
``root_handles`` to scope their resolution). Everything else delegates to the
ONE ``render_symbols`` path, so the render stays ``mode: render_symbols``.

These tests drive the registry handler end-to-end against a minimal
in-memory cache (stubbing the ONE WebSocket touch, the global-suppress
read) and pin the delegation + refusal contracts. Note: the render_symbols
family return is Markdown-shaped by default (the shaper returns the raw
string), so success assertions read the Markdown and structural/delegation
behavior is pinned separately. All local: no network, no real Workflowy,
no disk.
"""
import asyncio
import os
import sys
from types import SimpleNamespace

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import pytest  # noqa: E402

from workflowy_mcp import server as srv  # noqa: E402
from workflowy_mcp.client.api_client_nexus import (  # noqa: E402
    WorkFlowyClientNexus,
)
from workflowy_mcp.mcp_api_registry import validate_params  # noqa: E402

_ROOT = "bbbbbbbb-0000-0000-0000-000000000001"
_F = "bbbbbbbb-0000-0000-0000-000000000002"
_G = "bbbbbbbb-0000-0000-0000-000000000003"
_P = "bbbbbbbb-0000-0000-0000-000000000004"  # parent, for the expand sweep
_C = "bbbbbbbb-0000-0000-0000-000000000005"  # its child
_ROOT2 = "cccccccc-0000-0000-0000-000000000001"  # a DISJOINT second tree
_H = "cccccccc-0000-0000-0000-000000000002"
_APNODE = _ROOT + "a"

# ROOT
# |-- F  ("def foo()", note "body of foo")
# |-- G  ("def bar()")
# |-- P
# |   `-- C
# `-- [ap:QQQ99Z] A point
# ROOT2
# `-- H
_CACHE_NODES = [
    {"id": _ROOT, "name": "Symbols Root", "note": "", "parent_id": None, "priority": 0},
    {"id": _F, "name": "def foo()", "note": "body of foo", "parent_id": _ROOT, "priority": 0},
    {"id": _G, "name": "def bar()", "note": "body of bar", "parent_id": _ROOT, "priority": 1},
    {"id": _P, "name": "def parent()", "note": "parent body", "parent_id": _ROOT, "priority": 2},
    {"id": _C, "name": "def child()", "note": "child body", "parent_id": _P, "priority": 0},
    {"id": _APNODE, "name": "[ap:QQQ99Z] A point", "note": "point body", "parent_id": _ROOT, "priority": 3},
    {"id": _ROOT2, "name": "Other Tree", "note": "", "parent_id": None, "priority": 1},
    {"id": _H, "name": "def elsewhere()", "note": "other tree", "parent_id": _ROOT2, "priority": 0},
]


def _make_client(nodes=None):
    c = WorkFlowyClientNexus.__new__(WorkFlowyClientNexus)
    c._get_nodes_export_cache_nodes = lambda: [dict(n) for n in (nodes or _CACHE_NODES)]

    # read_uuid_from_symbol is only consulted for NON-UUID-shaped items under
    # an explicit root; the fixture's only resolvable "symbol" is def bar().
    async def _ruf(*, file_path=None, symbol=None, symbol_kind="auto"):
        if symbol == "def bar()":
            return {"candidates": [{"uuid": _G, "name": "def bar()"}]}
        return {"candidates": []}

    c.read_uuid_from_symbol = _ruf
    return c


def _stub_suppress_config(monkeypatch, tags=None):
    """Stub the ONE WebSocket touch (global-suppress read) so the path is pure."""

    async def _cfg(_target_tab):
        # (groups, active_id, postfixes, suppress_tags, raw)
        return [], None, {}, list(tags or []), {}

    monkeypatch.setattr(srv, "_resolve_slice_group_config", _cfg)


def _run_registry(monkeypatch, params, *, impl=None, nodes=None):
    """Drive the render_nodes handler through a fresh registry. The render
    is Markdown-shaped by default (the family return is the raw string);
    failures come back as the standardized error envelope. The defaulting
    resolution reads the operation's pinned cache map through
    _current_cache_by_id (the sanctioned seam, loud outside a boundary), so
    the direct handler call pins it to the fixture map -- exactly what the
    in-boundary snapshot would carry."""
    monkeypatch.setattr(srv, "get_client", lambda: _make_client(nodes))
    monkeypatch.setattr(
        srv,
        "_current_cache_by_id",
        lambda: {str(n["id"]): dict(n) for n in (nodes if nodes is not None else _CACHE_NODES)},
    )
    _stub_suppress_config(monkeypatch)
    if impl is not None:
        monkeypatch.setattr(srv, "_lightning_rod_legacy_impl", impl)
    registry = srv.build_lightning_rod_registry()
    return asyncio.run(registry.resolve("render_nodes").handler(dict(params)))


# ---------------------------------------------------------------------------
# The happy paths -- defaulting, LCA spine, AP-ID form, expand, explicit roots
# ---------------------------------------------------------------------------
def test_render_nodes_single_node_renders_alone(monkeypatch):
    # THE contract: render_nodes(['<uuid>']) just works -- the node ITSELF is
    # the default scope, so its ancestor chain ("Symbols Root") does NOT ride
    # along. This is the lightning-bolt-hits-one-node property.
    md = _run_registry(monkeypatch, {"symbols": [_F]})
    assert isinstance(md, str), md
    assert "foo" in md, md
    assert "body of foo" in md, md
    assert "Symbols Root" not in md, md  # no ancestor chain


def test_render_nodes_multiple_nodes_connect_via_lca_spine(monkeypatch):
    md = _run_registry(monkeypatch, {"symbols": [_F, _G]})
    assert isinstance(md, str), md
    assert "foo" in md and "bar" in md, md
    # The shared parent appears as the connective LCA spine.
    assert "Symbols Root" in md, md


def test_render_nodes_ap_id_handle_resolves(monkeypatch):
    # '[ap:CORE]' items must resolve through the shared resolver's AP leg.
    md = _run_registry(monkeypatch, {"symbols": ["[ap:QQQ99Z]"]})
    assert isinstance(md, str), md
    assert "A point" in md and "point body" in md, md
    assert "Symbols Root" not in md, md  # the AP node itself is the scope


def test_render_nodes_expand_item_pulls_subtree(monkeypatch):
    md = _run_registry(monkeypatch, {"symbols": [{"uuid": _P, "expand": True}]})
    assert isinstance(md, str), md
    assert "parent" in md and "child" in md, md  # the swept descendant renders


def test_render_nodes_explicit_root_handles_overrides_defaulting(monkeypatch):
    # With root_handles the verb behaves exactly like render_symbols: a mixed
    # list (node UUID + code symbol) resolves under the given scope.
    res = _run_registry(
        monkeypatch, {"symbols": [_F, "def bar()"], "root_handles": [_ROOT]}
    )
    assert isinstance(res, str), res
    assert "foo" in res and "bar" in res, res


# ---------------------------------------------------------------------------
# The loud refusals -- the defaulting rule is all-or-nothing, standardized
# ---------------------------------------------------------------------------
def _assert_refusal(res, *, fragment=None):
    assert res["success"] is False, res
    assert res["operation"] == "lightning_rod.render_nodes", res
    assert res["error"]["code"] == "lightning_request_failed", res
    assert res["error"]["extra"]["legacy_result"]["success"] is False, res
    msg = res["error"]["message"]
    if fragment is not None:
        assert fragment in msg, msg
    return msg


def test_render_nodes_symbol_item_without_roots_refuses(monkeypatch):
    res = _run_registry(monkeypatch, {"symbols": [{"symbol": "foo_fn"}]})
    msg = _assert_refusal(res, fragment="root_handles")
    assert "foo_fn" in msg, msg


def test_render_nodes_unresolvable_string_refuses(monkeypatch):
    res = _run_registry(monkeypatch, {"symbols": ["zzz_no_such_symbol"]})
    msg = _assert_refusal(res, fragment="root_handles")
    assert "zzz_no_such_symbol" in msg, msg


def test_render_nodes_ambiguous_prefix_refuses_with_candidates(monkeypatch):
    # Every fixture id shares the 8-hex prefix 'bbbbbbbb' -> ambiguous; the
    # refusal must be a loud did-you-mean carrying the candidates.
    res = _run_registry(monkeypatch, {"symbols": ["bbbbbbbb"]})
    msg = _assert_refusal(res, fragment="prefix matches multiple nodes")
    legacy = res["error"]["extra"]["legacy_result"]
    assert legacy.get("candidates"), res


def test_render_nodes_disjoint_nodes_are_loud(monkeypatch):
    # Two nodes from disjoint top-level trees: the delegated render_symbols
    # projection refuses loudly (no connective LCA root) -- never an empty
    # render. The envelope still names render_nodes (the caller's verb).
    res = _run_registry(monkeypatch, {"symbols": [_F, _H]})
    _assert_refusal(res, fragment="disjoint")


def test_render_nodes_empty_symbols_refuses(monkeypatch):
    res = _run_registry(monkeypatch, {"symbols": []})
    _assert_refusal(res, fragment="non-empty")


# ---------------------------------------------------------------------------
# Delegation contract -- defaulting lowers to the ONE render_symbols path
# ---------------------------------------------------------------------------
def test_render_nodes_delegates_to_render_symbols_with_defaulted_roots(monkeypatch):
    calls = []

    async def impl(**kwargs):
        calls.append(kwargs)
        return "RAW render_symbols"

    res = _run_registry(monkeypatch, {"symbols": [_F]}, impl=impl)
    assert res == "RAW render_symbols"
    assert calls[-1] == {
        "mode": "render_symbols",
        "push_to_modal": False,
        "target_tab": None,
        "symbols": [_F],
        "root_uuids": [_F],
        "favor": "uuid",
        # 54e8356f(ii): the tier rides the impl kwargs (defaults = today).
        "tier": "slice",
        "context_lines": 5,
        "tail_lines": 0,
        "as_markdown": True,
        "headings_only": False,
        # [ap:0M6VP8]: the heading-id form rides the impl kwargs (default False).
        "full_uuids": False,
        "sort_by": None,
        "sort_direction": "asc",
        "since": None,
        # [ap:TVIIBN] 4d(a): the recency basis rides the impl kwargs.
        "recent_by": None,
        "output_file": None,
    }


def test_render_nodes_family_tool_failure_is_standardized(monkeypatch):
    from coherence_harness import make_boundary_capable

    monkeypatch.setattr(
        srv,
        "get_client",
        lambda: make_boundary_capable(_make_client([]), nodes=[]),
    )
    result = asyncio.run(
        srv.lightning_rod_family_tool("render_nodes", {"symbols": ["zzz_no_such"]})
    )
    assert result["operation"] == "lightning_rod.render_nodes", result
    assert result["error"]["code"] == "lightning_request_failed", result
    assert "root_handles" in result["error"]["message"], result
    legacy = result["error"]["extra"]["legacy_result"]
    assert legacy["success"] is False, result


# ---------------------------------------------------------------------------
# render_symbols is UNCHANGED, and both describes cross-reference
# ---------------------------------------------------------------------------
def test_render_symbols_still_requires_root_handles():
    registry = srv.build_lightning_rod_registry()
    sym_spec = registry.resolve("render_symbols")
    with pytest.raises(Exception):
        validate_params(sym_spec, {"symbols": [_F]})  # root_handles still required
    nodes_spec = registry.resolve("render_nodes")
    # ...while render_nodes requires only `symbols`.
    validate_params(nodes_spec, {"symbols": [_F]})
    with pytest.raises(Exception):
        validate_params(nodes_spec, {})


def test_describes_cross_reference_each_other():
    registry = srv.build_lightning_rod_registry()
    sym = registry.describe_full("render_symbols")
    nodes = registry.describe_full("render_nodes")
    assert "render_nodes" in sym["summary"], sym["summary"]
    assert "`render_nodes`: the same capability, named for nodes" in sym["returns"][
        "description"
    ]
    assert "render_symbols" in nodes["summary"], nodes["summary"]
    assert "the same capability as render_symbols, named for nodes" in nodes[
        "returns"
    ]["description"]
    assert "lightning_rod.render_nodes" in sym["related"]
    assert "lightning_rod.render_symbols" in nodes["related"]
