"""Regression lock: render_spec="modal" for a DOM-FROZEN docked modal view.

The audited DOM-frozen plan (Workflowy anchor [ap:RVK9TX], refining
[ap:MDT4RQ]). When the docked Markdown modal is showing a FROZEN DOM view
(not a cache-sourced slice/raw-dump), render_spec="modal" must re-project the
EPHEMERAL captured tree the tab pinned at freeze -- a DIFFERENT source axis
from the cache-projection RenderProjectionSpec path. The tab now answers the
``get_current_modal_render_spec`` WS query with an EXPLICIT tagged payload:

  * modal_payload_kind='cache_render_spec'  -> a durable RenderProjectionSpec
    blob (slice / raw dump) -> _glimpse_dispatch_spec (unchanged; covered by
    test_glimpse_render_spec_modal.py).
  * modal_payload_kind='dom_captured_tree'  -> {captured_tree, root_uuid,
    focus, sort_by, sort_direction, since, headings_only} -> the NEW sibling
    _glimpse_dispatch_dom_capture, which composes EXACTLY that captured tree
    through the SAME composer + focus/row-identity primitives.

These tests drive the REAL async ``glimpse`` tool end-to-end for the
dom_captured_tree cell with a monkeypatched WS transport (the "tab" replies
with a canned dom-capture payload) and a real WorkFlowyClientNexus (render
helpers intact) + a tiny in-memory /nodes-export cache.

They lock the audit's test matrix (the server/WS-observable cases):
  (1)  DOM capture -> whole-tree render (null focus).
  (3)  DOM capture + created sort -> reordered (sort applied to captured tree).
  (4a) full + headings_only -> preview_tree of exactly the focused rows.
  (4b) full WITHOUT headings_only -> rejected loudly (mod #5).
  (5)  focus EXACT-OR-ERROR -> an unresolvable focus triple -> loud error,
       NOT the whole document (mod #6).
  (7)  honest echo -> render_scope.kind='dom_capture', durable_scope False
       (mod #7).
  (8)  unfrozen DOM -> the tab replies success:false 'freeze first' -> surfaced
       (mod #8; the refusal text is authored client-side, so here we lock that
       the server faithfully surfaces a tab success:false).
  (9)  REGRESSION -> a cache_render_spec payload still routes to the spec path
       unchanged (no DOM-capture interference).
  focused-view -> a resolved focus renders ONLY that row's option-Y subtree.

All local: no network, no real Workflowy, no disk writes.
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
from workflowy_mcp.cache_render_spec import RenderProjectionSpec  # noqa: E402


# --- a tiny in-memory /nodes-export cache (mirrors the sibling test) --------
# The DOM-capture render is SELF-CONTAINED (it composes the sent captured
# tree), so the cache is only consulted for sort keys (createdAt/modifiedAt)
# and the zoom-proof root regime seed. We give the same UUIDs createdAt values
# so the created-sort test has a deterministic order.
_ROOT = "33333333-1111-bbbb-cccc-000000000001"
_CHILD_A = "33333333-2222-bbbb-cccc-000000000002"
_CHILD_B = "33333333-3333-bbbb-cccc-000000000003"

_CACHE_NODES = [
    {"id": _ROOT, "name": "Frozen Anchor Root", "note": "", "parent_id": None,
     "priority": 0, "createdAt": 1000, "modifiedAt": 1000},
    # Alpha created LATER than Beta so an ascending created sort puts Beta first.
    {"id": _CHILD_A, "name": "Alpha point", "note": "body A", "parent_id": _ROOT,
     "priority": 0, "createdAt": 2000, "modifiedAt": 2000},
    {"id": _CHILD_B, "name": "Beta point", "note": "body B", "parent_id": _ROOT,
     "priority": 1, "createdAt": 1500, "modifiedAt": 1500},
]


def _make_client():
    c = WorkFlowyClientNexus.__new__(WorkFlowyClientNexus)
    c._get_nodes_export_cache_nodes = lambda: [dict(n) for n in _CACHE_NODES]
    return c


def _patch_common(monkeypatch):
    monkeypatch.setattr(srv, "get_client", _make_client)

    async def _fake_grace(*a, **k):
        return ("ws-conn", "ws-queue")

    monkeypatch.setattr(srv, "get_ws_connection_with_grace_wait", _fake_grace)


def _captured_tree():
    """The {root, children} shape the client's serializeVisibleTreeForModal sends."""
    return {
        "root": {"id": _ROOT, "name": "Frozen Anchor Root", "note": ""},
        "children": [
            {"id": _CHILD_A, "name": "Alpha point", "note": "body A", "children": []},
            {"id": _CHILD_B, "name": "Beta point", "note": "body B", "children": []},
        ],
    }


def _patch_tab_reply(monkeypatch, reply_dict):
    async def _fake_ws_request_response(self, action, **k):
        return reply_dict
    monkeypatch.setattr(
        WorkFlowyClientNexus, "_ws_request_response", _fake_ws_request_response
    )


# ---------------------------------------------------------------------------
# (1) DOM capture -> whole-tree render (null focus).
# ---------------------------------------------------------------------------
def test_dom_capture_whole_tree(monkeypatch):
    _patch_common(monkeypatch)
    _patch_tab_reply(monkeypatch, {
        "success": True,
        "modal_payload_kind": "dom_captured_tree",
        "captured_tree": _captured_tree(),
        "root_uuid": _ROOT,
        "focus": None,
        "sort_by": "none",
        "sort_direction": "asc",
        "since": None,
        "headings_only": False,
    })
    reply = asyncio.run(srv.glimpse(render_spec="modal"))
    # AGENT-FACING CONTRACT: Markdown mode -> the RAW Markdown STRING (via
    # _shape_glimpse_family_return), NOT a dict. node_count / render_scope are
    # deliberately no longer surfaced to the agent (they remain on the
    # internal helper payload for the WS/modal handlers; the honest-echo lock
    # lives in test_dom_capture_render_scope_is_honest via the dispatch level).
    assert isinstance(reply, str), reply
    assert "Alpha point" in reply and "Beta point" in reply, reply


# ---------------------------------------------------------------------------
# focused-view -> a resolved focus renders ONLY that row's option-Y subtree.
# ---------------------------------------------------------------------------
def test_dom_capture_focus_renders_only_that_row(monkeypatch):
    _patch_common(monkeypatch)
    _patch_tab_reply(monkeypatch, {
        "success": True,
        "modal_payload_kind": "dom_captured_tree",
        "captured_tree": _captured_tree(),
        "root_uuid": _ROOT,
        "focus": {"mode": "row", "uuid": _CHILD_B, "ancestor_path": [_ROOT], "occurrence": 0},
        "sort_by": "none",
        "sort_direction": "asc",
        "since": None,
        "headings_only": False,
    })
    reply = asyncio.run(srv.glimpse(render_spec="modal"))
    # AGENT-FACING CONTRACT: raw Markdown STRING. Option-Y: the breadcrumb
    # (root heading) + Beta's full content; Alpha's sibling subtree is pruned.
    assert isinstance(reply, str), reply
    assert "Beta point" in reply, reply
    assert "Alpha point" not in reply, reply


# ---------------------------------------------------------------------------
# (3) DOM capture + created sort -> reordered (sort applied to captured tree).
# ---------------------------------------------------------------------------
def test_dom_capture_created_sort_reorders(monkeypatch):
    _patch_common(monkeypatch)
    _patch_tab_reply(monkeypatch, {
        "success": True,
        "modal_payload_kind": "dom_captured_tree",
        "captured_tree": _captured_tree(),
        "root_uuid": _ROOT,
        "focus": None,
        "sort_by": "created",
        "sort_direction": "asc",
        "since": None,
        "headings_only": False,
    })
    reply = asyncio.run(srv.glimpse(render_spec="modal"))
    # AGENT-FACING CONTRACT: raw Markdown STRING. Beta (createdAt 1500) sorts
    # before Alpha (createdAt 2000) ascending -- the sort is observable in the
    # rendered order (render_scope.sort_by is no longer in the agent return).
    assert isinstance(reply, str), reply
    assert reply.index("Beta point") < reply.index("Alpha point"), reply


# ---------------------------------------------------------------------------
# (4a) full + headings_only -> preview_tree of exactly the focused rows.
# ---------------------------------------------------------------------------
def test_dom_capture_full_headings_only_focus(monkeypatch):
    _patch_common(monkeypatch)
    _patch_tab_reply(monkeypatch, {
        "success": True,
        "modal_payload_kind": "dom_captured_tree",
        "captured_tree": _captured_tree(),
        "root_uuid": _ROOT,
        "focus": {"mode": "row", "uuid": _CHILD_A, "ancestor_path": [_ROOT], "occurrence": 0},
        "sort_by": "none",
        "sort_direction": "asc",
        "since": None,
        "headings_only": False,
    })
    # Agent override: full/UUID mode + headings_only (the {UUID<->title} harvest).
    reply = asyncio.run(srv.glimpse(
        render_spec="modal", as_markdown=False, headings_only=True
    ))
    # AGENT-FACING CONTRACT: full/UUID mode -> the structured dict with
    # preview_tree + useful metadata (node_count/depth/render_scope), minus
    # only the redundant `rows` content twin.
    assert isinstance(reply, dict), reply
    assert "rows" not in reply, reply
    tree = reply.get("preview_tree")
    assert isinstance(tree, list) and tree, reply
    flat = "\n".join(tree)
    # The focused row's UUID appears (a real handle to harvest); Beta pruned.
    assert _CHILD_A in flat, flat
    assert "Alpha point" in flat, flat
    assert "Beta point" not in flat, flat


# ---------------------------------------------------------------------------
# (4b) full WITHOUT headings_only -> rejected loudly (mod #5).
# ---------------------------------------------------------------------------
def test_dom_capture_full_without_headings_only_rejected(monkeypatch):
    _patch_common(monkeypatch)
    _patch_tab_reply(monkeypatch, {
        "success": True,
        "modal_payload_kind": "dom_captured_tree",
        "captured_tree": _captured_tree(),
        "root_uuid": _ROOT,
        "focus": None,
        "sort_by": "none",
        "sort_direction": "asc",
        "since": None,
        "headings_only": False,
    })
    reply = asyncio.run(srv.glimpse(render_spec="modal", as_markdown=False))
    assert reply.get("success") is False, reply
    assert "headings_only" in (reply.get("error") or ""), reply


# ---------------------------------------------------------------------------
# (5) focus EXACT-OR-ERROR -> unresolvable focus -> loud error (mod #6).
# ---------------------------------------------------------------------------
def test_dom_capture_unresolvable_focus_errors(monkeypatch):
    _patch_common(monkeypatch)
    _patch_tab_reply(monkeypatch, {
        "success": True,
        "modal_payload_kind": "dom_captured_tree",
        "captured_tree": _captured_tree(),
        "root_uuid": _ROOT,
        # A focus on a uuid that is NOT in the captured tree.
        "focus": {"mode": "row", "uuid": "deadbeef-0000-0000-0000-000000000000",
                  "ancestor_path": [_ROOT], "occurrence": 0},
        "sort_by": "none",
        "sort_direction": "asc",
        "since": None,
        "headings_only": False,
    })
    reply = asyncio.run(srv.glimpse(render_spec="modal"))
    assert reply.get("success") is False, reply
    # NOT the whole document -- a loud stale/focus error.
    assert "could not be resolved" in (reply.get("error") or "").lower(), reply


# ---------------------------------------------------------------------------
# (7) honest echo -> render_scope.kind='dom_capture', durable_scope False.
# ---------------------------------------------------------------------------
def test_dom_capture_render_scope_is_honest(monkeypatch):
    _patch_common(monkeypatch)
    _patch_tab_reply(monkeypatch, {
        "success": True,
        "modal_payload_kind": "dom_captured_tree",
        "captured_tree": _captured_tree(),
        "root_uuid": _ROOT,
        "focus": None,
        "sort_by": "none",
        "sort_direction": "asc",
        "since": None,
        "headings_only": False,
    })
    # The agent-facing return is now the RAW Markdown STRING, so render_scope
    # is intentionally NOT surfaced there. The honest-echo is a property of the
    # INTERNAL dispatch payload (which the WS/modal handlers consume), so we
    # lock it at that level -- calling _glimpse_dispatch_dom_capture directly.
    result = asyncio.run(srv._glimpse_dispatch_dom_capture(
        captured_tree=_captured_tree(),
        root_uuid_hint=_ROOT,
        focus=None,
        as_markdown=True,
        headings_only=False,
        sort_by=None,
        sort_direction="asc",
        since=None,
        output_file=None,
    ))
    rs = result.get("render_scope") or {}
    assert rs.get("kind") == "dom_capture", rs
    assert rs.get("durable_scope") is False, rs
    assert rs.get("content_reproducible_if_saved") is True, rs
    assert rs.get("live_gui_sync_supported") is False, rs
    # NEVER masquerade as a durable cache scope.
    assert rs.get("kind") not in ("raw_root", "slice", "slice_override"), rs


# ---------------------------------------------------------------------------
# (8) unfrozen DOM -> tab replies success:false 'freeze first' -> surfaced.
# ---------------------------------------------------------------------------
def test_dom_unfrozen_freeze_first_surfaced(monkeypatch):
    _patch_common(monkeypatch)
    _patch_tab_reply(monkeypatch, {
        "success": False,
        "error": "The docked Markdown modal is DOM-sourced but NOT frozen, so "
                 "its current tree is not reproducible. Freeze the dock first ...",
    })
    reply = asyncio.run(srv.glimpse(render_spec="modal"))
    assert reply.get("success") is False, reply
    assert "frozen" in (reply.get("error") or "").lower(), reply


# ---------------------------------------------------------------------------
# (5b) focused view forbids a SORT override (mod #6-adjacent guard).
# ---------------------------------------------------------------------------
def test_dom_capture_focus_plus_sort_override_rejected(monkeypatch):
    _patch_common(monkeypatch)
    _patch_tab_reply(monkeypatch, {
        "success": True,
        "modal_payload_kind": "dom_captured_tree",
        "captured_tree": _captured_tree(),
        "root_uuid": _ROOT,
        "focus": {"mode": "row", "uuid": _CHILD_A, "ancestor_path": [_ROOT], "occurrence": 0},
        "sort_by": "none",
        "sort_direction": "asc",
        "since": None,
        "headings_only": False,
    })
    reply = asyncio.run(srv.glimpse(render_spec="modal", sort_by="created"))
    assert reply.get("success") is False, reply
    assert "focused" in (reply.get("error") or "").lower(), reply


# ---------------------------------------------------------------------------
# (9) REGRESSION -> a cache_render_spec payload still routes to the spec path.
# ---------------------------------------------------------------------------
def test_cache_render_spec_payload_unchanged(monkeypatch):
    _patch_common(monkeypatch)
    blob = RenderProjectionSpec.normalize_from_glimpse_args(root_uuid=_ROOT).to_render_spec_blob()
    _patch_tab_reply(monkeypatch, {
        "success": True,
        "modal_payload_kind": "cache_render_spec",
        "render_spec": blob,
    })
    reply = asyncio.run(srv.glimpse(render_spec="modal"))
    # AGENT-FACING CONTRACT: the cache raw-dump path renders Markdown, so the
    # agent return is the RAW Markdown STRING (whole subtree: root + both
    # children). This still proves it routed through the cache spec path (a
    # dom_capture would ALSO be markdown, so the distinguishing check is that
    # the whole subtree rendered -- see test_dom_capture_render_scope_is_honest
    # for the kind='dom_capture' lock at the dispatch level).
    assert isinstance(reply, str), reply
    assert "Frozen Anchor Root" in reply, reply
    assert "Alpha point" in reply and "Beta point" in reply, reply


# ---------------------------------------------------------------------------
# unrecognized payload kind -> loud error.
# ---------------------------------------------------------------------------
def test_unrecognized_payload_kind_errors(monkeypatch):
    _patch_common(monkeypatch)
    _patch_tab_reply(monkeypatch, {
        "success": True,
        "modal_payload_kind": "some_future_kind",
    })
    reply = asyncio.run(srv.glimpse(render_spec="modal"))
    assert reply.get("success") is False, reply
    assert "unrecognized payload kind" in (reply.get("error") or "").lower(), reply
