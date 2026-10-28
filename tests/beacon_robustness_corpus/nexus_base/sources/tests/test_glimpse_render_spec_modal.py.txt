"""Regression lock: the glimpse `render_spec` argument (the modal-read surface).

Feature ② ([ap:GF9QX2] handoff): the older bare `focus` triple arg on the
``glimpse`` MCP tool was REPLACED by a single double-duty ``render_spec`` arg:

  * render_spec="modal"  -> the PRIMARY path. The server issues ONE Direction-A
    ``get_current_modal_render_spec`` WS query to the target tab; the tab
    answers with its OWN current dock scope + selected-row focus (from
    dockedSliceContext.render_spec). The server deserializes that blob into a
    RenderProjectionSpec, applies UUID prefix-grace, and re-projects via the
    SAME cache helpers an explicit-arg GLIMPSE uses. The agent types ZERO
    UUIDs (the (b)-variant architecture: no client-string round-trip; the
    Python composer stays the sole renderer).

  * render_spec=<blob | JSON string>  -> the FALLBACK paste path. Same
    deserialize -> grace -> dispatch, skipping the WS query.

These tests drive the REAL async ``glimpse`` tool + its helpers end-to-end for
the self-contained RAW-DUMP (raw_root) cell -- the cell that needs NO WebSocket
config read -- with a minimal monkeypatched client + an in-memory
/nodes-export cache. The WS modal-read path is exercised by monkeypatching the
transport primitives so the "tab" replies with a canned render_spec blob.

They lock:
  1. render_spec="modal" -> WS query fired -> returned blob re-projected.
  2. render_spec=<dict> and render_spec=<JSON string> -> deserialized + rendered.
  3. _grace_resolve_spec_uuids canonicalizes a prefix-drifted focus UUID while
     preserving occurrence-exact focus (the hardening Dan required).
  4. A malformed render_spec string (not "modal", not JSON) fails loudly.

All local: no network, no real Workflowy, no disk writes.
"""

import asyncio
import json
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from workflowy_mcp import server as srv  # noqa: E402
from workflowy_mcp.client.api_client_nexus import WorkFlowyClientNexus  # noqa: E402
from workflowy_mcp.cache_render_spec import RenderProjectionSpec  # noqa: E402


# --- a tiny in-memory /nodes-export cache (a 3-node subtree) ----------------
# The three UUIDs DIVERGE EARLY (at the 2nd hex segment) so a truncated prefix
# is UNIQUE per node -- letting the grace test simulate a plausible drift that
# resolve_uuid_like_node_handle can adopt via a unique-prefix match. (If they
# shared a long common prefix, a truncation would be AMBIGUOUS and grace would
# correctly leave it as-is -- a different, also-correct behavior we don't want
# to exercise here.)
_ROOT = "22222222-1111-bbbb-cccc-000000000001"
_CHILD_A = "22222222-2222-bbbb-cccc-000000000002"
_CHILD_B = "22222222-3333-bbbb-cccc-000000000003"

_CACHE_NODES = [
    {"id": _ROOT, "name": "Modal Anchor Root", "note": "", "parent_id": None, "priority": 0},
    {"id": _CHILD_A, "name": "Alpha point", "note": "body A", "parent_id": _ROOT, "priority": 0},
    {"id": _CHILD_B, "name": "Beta point", "note": "body B", "parent_id": _ROOT, "priority": 1},
]


def _make_client():
    """A real WorkFlowyClientNexus (render helpers intact) with a fake cache."""
    c = WorkFlowyClientNexus.__new__(WorkFlowyClientNexus)
    c._get_nodes_export_cache_nodes = lambda: [dict(n) for n in _CACHE_NODES]
    return c


def _patch_common(monkeypatch):
    """Point get_client at the fake cache client for every server caller."""
    monkeypatch.setattr(srv, "get_client", _make_client)


# ---------------------------------------------------------------------------
# 1. render_spec="modal" -> WS query fired -> returned blob re-projected.
# ---------------------------------------------------------------------------
def test_render_spec_modal_queries_tab_and_reprojects(monkeypatch):
    _patch_common(monkeypatch)

    # Fake the WS peer acquisition (a non-None tuple is enough; the fake client
    # method below ignores it).
    async def _fake_grace(*a, **k):
        return ("ws-conn-sentinel", "ws-queue-sentinel")

    monkeypatch.setattr(srv, "get_ws_connection_with_grace_wait", _fake_grace)

    # The "tab" answers get_current_modal_render_spec with a raw_root scope
    # (its current dock scope) + a selected-row focus on _CHILD_B.
    tab_blob = RenderProjectionSpec.normalize_from_glimpse_args(
        root_uuid=_ROOT,
        focus={"mode": "row", "uuid": _CHILD_B, "ancestor_path": [_ROOT], "occurrence": 0},
    ).to_render_spec_blob()

    captured = {}

    async def _fake_ws_request_response(self, action, *, ws_connection, ws_queue,
                                        target_tab=None, tab_optional=False,
                                        timeout_s=30.0, payload=None):
        captured["action"] = action
        captured["target_tab"] = target_tab
        return {"success": True, "render_spec": tab_blob}

    monkeypatch.setattr(
        WorkFlowyClientNexus, "_ws_request_response", _fake_ws_request_response
    )

    reply = asyncio.run(srv.glimpse(render_spec="modal"))

    # The WS leaf query was fired (tab-specific: NOT tab_optional).
    assert captured.get("action") == "get_current_modal_render_spec"
    # AGENT-FACING CONTRACT: a Markdown-mode GLIMPSE-family return is now the
    # RAW Markdown STRING (via _shape_glimpse_family_return), NOT a dict with a
    # preview_markdown field. The returned blob was re-projected: a focused
    # raw-dump render of _CHILD_B.
    assert isinstance(reply, str), reply
    assert "Beta point" in reply, reply
    assert "Alpha point" not in reply, reply


def test_render_spec_modal_reports_when_no_cache_render(monkeypatch):
    """A tab with no cache-sourced render replies success:false -> surfaced."""
    _patch_common(monkeypatch)

    async def _fake_grace(*a, **k):
        return ("ws-conn", "ws-queue")

    monkeypatch.setattr(srv, "get_ws_connection_with_grace_wait", _fake_grace)

    async def _fake_ws_request_response(self, action, **k):
        return {"success": False, "error": "not showing a cache-sourced render"}

    monkeypatch.setattr(
        WorkFlowyClientNexus, "_ws_request_response", _fake_ws_request_response
    )

    reply = asyncio.run(srv.glimpse(render_spec="modal"))
    assert reply.get("success") is False, reply
    assert "cache-sourced" in (reply.get("error") or ""), reply


# ---------------------------------------------------------------------------
# 2. render_spec=<dict> and render_spec=<JSON string> -> deserialized + rendered.
# ---------------------------------------------------------------------------
def test_render_spec_dict_blob_reprojects(monkeypatch):
    _patch_common(monkeypatch)
    blob = RenderProjectionSpec.normalize_from_glimpse_args(root_uuid=_ROOT).to_render_spec_blob()
    reply = asyncio.run(srv.glimpse(render_spec=blob))
    # AGENT-FACING CONTRACT: raw Markdown STRING (no envelope). The whole raw
    # dump renders root + both children, so all three names are present.
    assert isinstance(reply, str), reply
    assert "Modal Anchor Root" in reply, reply
    assert "Alpha point" in reply, reply
    assert "Beta point" in reply, reply


def test_render_spec_json_string_blob_reprojects(monkeypatch):
    _patch_common(monkeypatch)
    blob = RenderProjectionSpec.normalize_from_glimpse_args(
        root_uuid=_ROOT,
        focus={"mode": "row", "uuid": _CHILD_A, "ancestor_path": [_ROOT], "occurrence": 0},
    ).to_render_spec_blob()
    reply = asyncio.run(srv.glimpse(render_spec=json.dumps(blob)))
    # AGENT-FACING CONTRACT: raw Markdown STRING (no envelope).
    assert isinstance(reply, str), reply
    assert "Alpha point" in reply, reply
    assert "Beta point" not in reply, reply


# ---------------------------------------------------------------------------
# 3. UUID prefix-grace canonicalizes a drifted focus UUID (occurrence-exact
#    preserved). This is the hardening Dan required.
# ---------------------------------------------------------------------------
def test_grace_resolves_prefix_drifted_focus_uuid(monkeypatch):
    _patch_common(monkeypatch)
    cache_by_id = {n["id"]: n for n in _CACHE_NODES}

    # A focus whose uuid + ancestor entry are TRUNCATED to a UNIQUE prefix (a
    # plausible hallucinated drift). The UUIDs diverge at the 2nd segment, so
    # a 13-char prefix ('22222222-3333') uniquely identifies exactly one node
    # and resolve_uuid_like_node_handle adopts it silently.
    drifted = RenderProjectionSpec.normalize_from_glimpse_args(
        root_uuid=_ROOT[:13],  # unique prefix of _ROOT
        focus={
            "mode": "row",
            "uuid": _CHILD_B[:13],            # unique prefix of _CHILD_B
            "ancestor_path": [_ROOT[:13]],    # unique prefix of _ROOT
            "occurrence": 0,
        },
    )
    graced = srv._grace_resolve_spec_uuids(drifted, cache_by_id)

    # Every UUID canonicalized to its full cache UUID.
    assert graced.root_uuid == _ROOT, graced
    assert graced.focus is not None, graced
    assert graced.focus["uuid"] == _CHILD_B, graced
    assert graced.focus["ancestor_path"] == [_ROOT], graced
    # Occurrence is preserved (grace never touches it).
    assert graced.focus["occurrence"] == 0, graced

    # An UNRESOLVABLE uuid is left as-is (best-effort; fails safe downstream).
    unresolvable = RenderProjectionSpec.normalize_from_glimpse_args(
        root_uuid="ffffffff-dead-dead-dead-ffffffffffff",
    )
    graced2 = srv._grace_resolve_spec_uuids(unresolvable, cache_by_id)
    assert graced2.root_uuid == "ffffffff-dead-dead-dead-ffffffffffff", graced2


def test_render_spec_modal_grace_end_to_end(monkeypatch):
    """The modal path applies grace: a tab blob with a drifted focus still renders
    the right occurrence-exact row."""
    _patch_common(monkeypatch)

    async def _fake_grace(*a, **k):
        return ("ws-conn", "ws-queue")

    monkeypatch.setattr(srv, "get_ws_connection_with_grace_wait", _fake_grace)

    drifted_blob = RenderProjectionSpec.normalize_from_glimpse_args(
        root_uuid=_ROOT[:13],
        focus={"mode": "row", "uuid": _CHILD_A[:13], "ancestor_path": [_ROOT[:13]], "occurrence": 0},
    ).to_render_spec_blob()

    async def _fake_ws_request_response(self, action, **k):
        return {"success": True, "render_spec": drifted_blob}

    monkeypatch.setattr(
        WorkFlowyClientNexus, "_ws_request_response", _fake_ws_request_response
    )

    reply = asyncio.run(srv.glimpse(render_spec="modal"))
    # AGENT-FACING CONTRACT: raw Markdown STRING (no envelope).
    assert isinstance(reply, str), reply
    assert "Alpha point" in reply, reply
    assert "Beta point" not in reply, reply


# ---------------------------------------------------------------------------
# 4. A malformed render_spec string (not "modal", not JSON) fails loudly.
# ---------------------------------------------------------------------------
def test_render_spec_malformed_string_fails_loudly(monkeypatch):
    _patch_common(monkeypatch)
    reply = asyncio.run(srv.glimpse(render_spec="not-modal-not-json {{{"))
    assert reply.get("success") is False, reply
    assert "render_spec" in (reply.get("error") or ""), reply


def test_render_spec_none_is_live_dom_path(monkeypatch):
    """render_spec=None does NOT short-circuit: it falls through to the normal
    live-DOM path (which, with no WS peer, returns the standard unavailable
    signal). This proves the new branch is inert when the arg is absent."""
    _patch_common(monkeypatch)

    async def _no_peer(*a, **k):
        return (None, None)

    monkeypatch.setattr(srv, "get_ws_connection_with_grace_wait", _no_peer)
    # Not a render_spec call -> reaches the live-DOM branch -> WS unavailable.
    # We only assert it did NOT take the render_spec dispatch (no 'render_spec'
    # error wording, and it attempted the live path).
    try:
        reply = asyncio.run(srv.glimpse())
    except Exception:
        # A raised NetworkError from the live path is also acceptable evidence
        # that we did NOT short-circuit into render_spec dispatch.
        return
    # If it returned a dict, it must be the live-DOM unavailable shape, not a
    # render_spec parse error.
    assert "render_spec must be" not in (reply.get("error") or ""), reply
