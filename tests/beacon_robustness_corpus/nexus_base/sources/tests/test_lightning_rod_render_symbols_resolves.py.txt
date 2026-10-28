"""Regression: `_lightning_rod_render_symbols` resolves its roots (no NameError).

THE BUG THIS LOCKS: ``_lightning_rod_render_symbols`` (the MCP-return
Lightning Rod ``render_symbols`` cell) resolved its passed ``root_uuids`` via
``resolve_uuid_or_anchor_point_handle`` but was MISSING the local
``from .client.api_client_nexus import resolve_uuid_or_anchor_point_handle``
that every other call site in server.py has. So ANY render_symbols call
NameError'd the instant it reached the root-resolution loop -- reproducible
regardless of arguments, while ``gather`` / ``render_tags`` / ``render_search``
worked (they never enter this function). The other consumer
(the docked-modal push handler in ``_handle_render_slice_modal``) imports it
correctly, so only the MCP-return path was broken.

These tests drive ``_lightning_rod_render_symbols`` end-to-end against a
minimal in-memory cache, stubbing the ONE WebSocket touch
(``_resolve_slice_group_config``, the global-suppress read) so the path is
pure. They assert:
  * a valid symbol + valid root RESOLVES and renders (would NameError before
    the fix -- the direct regression lock);
  * a root that cannot resolve returns a clean cache-miss error (proving the
    resolver is actually invoked, not bypassed);
  * an unresolved SYMBOL under a valid root is reported non-fatally (the
    render still succeeds) -- the resolver ran for the roots.

All local: no network, no real Workflowy, no disk.
"""
import asyncio
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from workflowy_mcp import server as srv  # noqa: E402
from workflowy_mcp.client.api_client_nexus import WorkFlowyClientNexus  # noqa: E402


_ROOT = "bbbbbbbb-0000-0000-0000-000000000001"
_F = "bbbbbbbb-0000-0000-0000-000000000002"
_G = "bbbbbbbb-0000-0000-0000-000000000003"

# ROOT
# |-- F  (a "function" node -- resolvable by UUID)
# `-- G  (another leaf)
_CACHE_NODES = [
    {"id": _ROOT, "name": "Symbols Root", "note": "", "parent_id": None, "priority": 0},
    {"id": _F, "name": "def foo()", "note": "body of foo", "parent_id": _ROOT, "priority": 0},
    {"id": _G, "name": "def bar()", "note": "body of bar", "parent_id": _ROOT, "priority": 1},
]


def _make_client():
    c = WorkFlowyClientNexus.__new__(WorkFlowyClientNexus)
    c._get_nodes_export_cache_nodes = lambda: [dict(n) for n in _CACHE_NODES]

    # read_uuid_from_symbol is only consulted for NON-UUID-shaped items; the
    # tests below pass UUID-shaped symbols (resolved via the UUID leg) or a
    # deliberately-unresolvable string. Provide a benign stub that resolves
    # nothing so a non-UUID item cleanly falls into the 'unresolved' bucket.
    async def _ruf(*, file_path=None, symbol=None, symbol_kind="auto"):
        return {"candidates": []}

    c.read_uuid_from_symbol = _ruf
    return c


def _stub_suppress_config(monkeypatch, tags=None):
    """Stub the ONE WebSocket touch (global-suppress read) so the path is pure."""
    async def _cfg(_target_tab):
        # (groups, active_id, postfixes, suppress_tags, raw)
        return [], None, {}, list(tags or []), {}
    monkeypatch.setattr(srv, "_resolve_slice_group_config", _cfg)


def _run(monkeypatch, *, symbols, root_uuids, suppress=None, focus=None):
    client = _make_client()
    monkeypatch.setattr(srv, "get_client", lambda: client)
    _stub_suppress_config(monkeypatch, tags=suppress)
    return asyncio.run(
        srv._lightning_rod_render_symbols(
            symbols=symbols,
            root_uuids=root_uuids,
            favor="uuid",
            as_markdown=True,
            output_file=None,
            sort_by=None,
            sort_direction="asc",
            headings_only=False,
            since=None,
            focus=focus,
        )
    )


def test_valid_symbol_and_root_resolves_and_renders(monkeypatch):
    # THE DIRECT REGRESSION: this reaches the root-resolution loop that used to
    # NameError. A UUID-shaped symbol (_F) under a valid root (_ROOT) resolves
    # cleanly and renders a Markdown slice.
    res = _run(monkeypatch, symbols=[_F], root_uuids=[_ROOT])
    assert res["success"] is True
    assert "preview_markdown" in res
    # foo (the resolved symbol) is in the render; its report shows no problems.
    assert "foo" in res["preview_markdown"]
    rep = res.get("resolution_report") or {}
    assert rep.get("resolved_count") == 1


def test_unresolvable_root_returns_cache_miss_error(monkeypatch):
    # Proves the root resolver is actually INVOKED (not bypassed): a root that
    # is neither an exact/prefix UUID nor an AP-ID in the cache returns a clean
    # error rather than NameError'ing or rendering an empty slice.
    res = _run(monkeypatch, symbols=[_F], root_uuids=["no-such-root-uuid"])
    assert res["success"] is False
    assert "error" in res


def test_unresolved_symbol_is_reported_nonfatally(monkeypatch):
    # A non-UUID symbol that resolves to nothing is reported non-fatally; the
    # render still succeeds. (The roots still resolved -- the loop ran.)
    res = _run(monkeypatch, symbols=["does_not_exist_fn"], root_uuids=[_ROOT])
    assert res["success"] is True
    rep = res.get("resolution_report") or {}
    assert rep.get("resolved_count") == 0
    assert len(rep.get("unresolved") or []) == 1


# ---------------------------------------------------------------------------
# ap:GLMPMODAL FOLLOW-UP -- the prior-agent verification question:
#   "Confirm the shared tail actually APPLIES focus for the lightning spec
#    (not just accepts the field) -- verify the render_symbols report still
#    appends on the FOCUSED path."
#
# This drives _lightning_rod_render_symbols WITH a row focus and asserts BOTH:
#   (1) focus is genuinely APPLIED -- the render is the option-Y view of the
#       focused row (the focused subtree is present); and
#   (2) the resolution report STILL APPENDS to preview_markdown on the focused
#       success path (the report append keys off preview_markdown being a
#       str, which the focused success path populates -- so it survives).
# Tree: ROOT -> foo (_F) + bar (_G). We select both symbols (slice = ROOT+F+G)
# and FOCUS on foo(_F); the option-Y view is the ROOT breadcrumb + foo's
# subtree. bar is a sibling OUTSIDE the focused row, so it must NOT appear in
# the focused render (proving focus actually pruned, not merely 'accepted').
# ---------------------------------------------------------------------------
def test_render_symbols_focus_applied_and_report_appends(monkeypatch):
    focus = {
        "mode": "row",
        "uuid": _F,
        "ancestor_path": [_ROOT],
        "occurrence": 0,
    }
    res = _run(monkeypatch, symbols=[_F, _G], root_uuids=[_ROOT], focus=focus)
    assert res["success"] is True, res
    md = res.get("preview_markdown")
    assert isinstance(md, str), res
    # (1) FOCUS APPLIED: the focused row (foo) is present; its non-focused
    #     sibling (bar) is pruned out of the option-Y view.
    assert "foo" in md, res
    assert "bar" not in md, res
    # (2) REPORT STILL APPENDS on the focused success path. Both symbols
    #     resolved cleanly (resolved_count == 2), so the in-band report has no
    #     '## Unresolved' section; assert the structured report is intact
    #     (the append machinery ran -- preview_markdown was a str) AND that the
    #     focused render did not lose it.
    rep = res.get("resolution_report") or {}
    assert rep.get("resolved_count") == 2, res


def test_render_symbols_focus_applied_report_with_unresolved_appends(monkeypatch):
    # Same as above, but with an UNRESOLVED extra symbol so the in-band
    # '## Unresolved / ambiguous symbols' report section is NON-EMPTY -- the
    # sharper form of the question: does that visible report text survive onto
    # the FOCUSED document? It must (append keys off preview_markdown:str,
    # which the focused success path sets).
    focus = {
        "mode": "row",
        "uuid": _F,
        "ancestor_path": [_ROOT],
        "occurrence": 0,
    }
    res = _run(
        monkeypatch,
        symbols=[_F, _G, "does_not_exist_fn"],
        root_uuids=[_ROOT],
        focus=focus,
    )
    assert res["success"] is True, res
    md = res.get("preview_markdown")
    assert isinstance(md, str), res
    # FOCUS APPLIED (foo present, bar pruned).
    assert "foo" in md, res
    assert "bar" not in md, res
    # REPORT APPENDED even on the focused render: the unresolved symbol shows
    # up in the in-band report section that was concatenated AFTER the tail
    # returned the focused markdown.
    assert "does_not_exist_fn" in md, res
    rep = res.get("resolution_report") or {}
    assert len(rep.get("unresolved") or []) == 1, res


# ---------------------------------------------------------------------------
# BRACKETED-UUID ITEM LENIENCY (Dan, 2026) -- the routing regression locks.
# The shared classifier unwrap (unwrap_bracketed_uuid_handle, the SAME pure
# normalizer Layer-1 consumes) must let an AP-ID-style bracketed UUID ROUTE
# to the UUID leg instead of falling to the symbol leg; every non-UUID-hex
# bracketed shape must route EXACTLY as before.
# ---------------------------------------------------------------------------
def test_bracketed_full_uuid_symbol_resolves(monkeypatch):
    # '[_F]' -- the full UUID wrapped in AP-ID-style brackets -- unwraps to
    # an exact UUID hit and renders foo. (The fixture ids share the bbbbbbbb
    # first segment and differ only in the final char, so the full-UUID form
    # is the unambiguous bracketed case here.)
    res = _run(monkeypatch, symbols=[f"[{_F}]"], root_uuids=[_ROOT])
    assert res["success"] is True, res
    rep = res.get("resolution_report") or {}
    assert rep.get("resolved_count") == 1, res
    assert "foo" in res["preview_markdown"], res


def test_bracketed_non_hex_item_still_symbol_routes(monkeypatch):
    # '[K7M2QF]' is NOT bracketed hex => never unwrapped, and it does not
    # match the render_symbols AP-ID grammar guard ('[ap:CORE]'/'ap:CORE'
    # only) => it routes to the symbol leg exactly as before and lands in
    # the unresolved bucket non-fatally.
    res = _run(monkeypatch, symbols=["[K7M2QF]"], root_uuids=[_ROOT])
    assert res["success"] is True, res
    rep = res.get("resolution_report") or {}
    assert rep.get("resolved_count") == 0, res
    assert len(rep.get("unresolved") or []) == 1, res
