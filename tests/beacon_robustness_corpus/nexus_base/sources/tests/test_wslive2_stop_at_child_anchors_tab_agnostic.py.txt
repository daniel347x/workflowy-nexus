"""WSLIVE2 [ap:WSLIV4] amendment lock — the 6524 tab-AGNOSTIC anchor-config read.

The descend_policy='stop_at_child_anchors' branch of ``_handle_render_slice_modal``
reads the live anchor-tag config to build its boundary predicate. WSLIV4 Option 1
changed that read from ``_resolve_note_session_config(str(endpoint_id) ...)`` (which
laundered the ORIGINATING tab's endpoint_id through target_tab -> unknown_target_tab
-> OFF-RAIL on a ghost -> an EMPTY anchor-tag list -> an always-False boundary ->
the slice SILENTLY DESCENDS through child anchors it should stop at) to
``_resolve_note_session_config(None)``.

That read is TAB-AGNOSTIC (anchor config lives in shared chrome.storage.sync,
mirrored identically into every tab), so passing ``None`` lets the broker's
deterministic any-tab fallback ANSWER even when the originating tab has ghosted --
so the boundary predicate is built with REAL anchor tags and descent stops.

This test drives the REAL handler end-to-end over a tiny in-memory cache and a
fake ``_resolve_note_session_config`` that (1) CAPTURES the target_tab it is
called with and (2) returns real anchor tags. It asserts:
  * the read was made with target_tab=None (NOT the originating endpoint_id) --
    the exact WSLIV4 code change; a regression to str(endpoint_id) captures the
    ghosted 'tab-GHOST' here and fails; and
  * the resulting boundary predicate is NON-empty: the slice INCLUDES a child
    anchor (as a leaf signpost) but does NOT descend into its grandchild.

All local: no network, no real Workflowy, no disk.
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
from workflowy_mcp.cache_render_spec import RenderProjectionSpec  # noqa: E402
from workflowy_mcp.client.api_client_nexus import WorkFlowyClientNexus  # noqa: E402


# --- in-memory /nodes-export cache: ROOT -> {ANCHOR_CHILD -> GRANDCHILD, PLAIN}
_ROOT = "22222222-aaaa-bbbb-cccc-000000000001"
_ANCHOR_CHILD = "22222222-aaaa-bbbb-cccc-000000000002"
_GRANDCHILD = "22222222-aaaa-bbbb-cccc-000000000003"
_PLAIN = "22222222-aaaa-bbbb-cccc-000000000004"

_CACHE_NODES = [
    {"id": _ROOT, "name": "Root", "note": "", "parent_id": None, "priority": 0},
    # A CHILD ANCHOR: its NAME trailing-tag region carries the anchor tag.
    {"id": _ANCHOR_CHILD, "name": "Child Anchor #wfy-anchor", "note": "",
     "parent_id": _ROOT, "priority": 0},
    # The anchor's descendant: MUST be excluded when descent stops at the anchor.
    {"id": _GRANDCHILD, "name": "Buried grandchild", "note": "",
     "parent_id": _ANCHOR_CHILD, "priority": 0},
    # A plain (non-anchor) sibling: descent continues normally here.
    {"id": _PLAIN, "name": "Plain point", "note": "", "parent_id": _ROOT,
     "priority": 1},
]


class _FakeCaptureWebsocket:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        try:
            self.sent.append(json.loads(payload))
        except Exception:
            self.sent.append({"_raw": payload})


def _make_client():
    from coherence_harness import make_boundary_capable

    c = WorkFlowyClientNexus.__new__(WorkFlowyClientNexus)
    c._get_nodes_export_cache_nodes = lambda: [dict(n) for n in _CACHE_NODES]
    return make_boundary_capable(c, nodes=_CACHE_NODES)


def test_stop_at_child_anchors_reads_anchor_config_tab_agnostically(monkeypatch):
    captured = {}

    async def _fake_resolve_note_session_config(target_tab):
        # (1) record HOW the read was targeted; (2) serve REAL anchor tags so
        # the boundary predicate is non-empty (as the any-tab fallback would).
        captured["target_tab"] = target_tab
        return (["wfy-anchor"], [], None, {})

    monkeypatch.setattr(srv, "get_client", _make_client)
    monkeypatch.setattr(
        srv, "_resolve_note_session_config", _fake_resolve_note_session_config
    )

    spec = RenderProjectionSpec.normalize_from_glimpse_args(
        root_uuid=_ROOT, descend_policy="stop_at_child_anchors",
    )
    frame = {
        "action": "render_slice_modal",
        "request_id": "req-wsliv4-1",
        # A GHOSTED originating endpoint: if the read laundered this through
        # target_tab it would ghost off-rail and yield an empty predicate.
        "endpoint_id": "tab-GHOST",
        "render_spec": spec.to_render_spec_blob(),
    }

    fake_ws = _FakeCaptureWebsocket()
    asyncio.run(srv._handle_render_slice_modal(frame, fake_ws))
    assert fake_ws.sent, "handler sent no reply"
    reply = fake_ws.sent[-1]

    assert reply.get("success") is True, reply

    # THE WSLIV4 LOCK: the anchor-config read was tab-AGNOSTIC (None), NOT the
    # originating tab's (ghosted) endpoint_id. A revert to
    # _resolve_note_session_config(str(endpoint_id) ...) captures "tab-GHOST".
    assert "target_tab" in captured, "the descend_policy branch was not reached"
    assert captured["target_tab"] is None, captured

    # The boundary predicate is NON-EMPTY: the child anchor is emitted as a
    # leaf signpost, but the slice does NOT descend into its grandchild.
    row_uuids = {r["uuid"] for r in (reply.get("rows") or [])}
    assert _ROOT in row_uuids, reply
    assert _ANCHOR_CHILD in row_uuids, reply
    assert _PLAIN in row_uuids, reply
    assert _GRANDCHILD not in row_uuids, (
        "descent should STOP at the child anchor; the grandchild leaked in "
        "(an empty/off-rail anchor-tag read would cause exactly this)"
    )


if __name__ == "__main__":  # pragma: no cover
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
