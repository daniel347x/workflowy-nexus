"""Regression lock: glimpse render_spec BLOB + push_to_modal must PUSH.

Defect (traced + reproduced live in RECON [ap:6RHNGJ] §§1-3; fix unit R1
[ap:8473VD]): the render_spec block of ``_glimpse_run_impl`` returned the
server-rendered CONTENT (or an output_file summary) WITHOUT EVER consulting
``push_to_modal`` -- the flag was silently ignored and NO
``apply_slice_in_dock`` frame reached any tab, while the tool's documented
contract promised the push ("invalid only for a bare live-DOM call"). The
canonical push path (group_handle/root_handle + flag) was healthy throughout.

These tests lock the fixed behavior:

  1. blob + push_to_modal -> the EXISTING push funnel
     (``_glimpse_push_slice_to_modal``) is used: the reply is the funnel's ack
     (``_source='push_to_modal'``, ``pushed_to_modal``, ``rendering``) -- NOT
     content, NOT a file summary -- and the wire frame is
     ``apply_slice_in_dock`` carrying a re-normalized ``render_spec`` blob.
     Covered for the raw_root, slice (with a resolved-root echo + focus, the
     harvested-fixture shape) and lightning_rod cells of the spec truth table.
  2. the blob branch CANNOT reach the content dispatcher while the flag is set
     (tripwire on ``_glimpse_dispatch_spec``).
  3. ``render_spec='modal'`` + push_to_modal -> LOUD refusal BEFORE any wire
     frame (read-then-push cross-tab copy is a pending feature, never a
     silent content read).
  4. a degenerate blob whose resolved kind carries no pushable scope refuses
     loudly BEFORE any wire frame (the funnel's caller contract: at least one
     scope present), and the refusal names push_to_modal (not a render error).
  5. a pushed slice blob keeps its producer-resolved ``kind`` ('slice' is not
     promoted to 'slice_override' by the echoed stored root) and DROPS the
     blob's ``focus`` (a push paints the full document -- consistent with
     canonical pushes; the funnel's normalization has no focus input).

Stub patterns follow the repo's existing conventions
(test_glimpse_render_spec_modal.py / test_sgsort_default_sort.py): a fake
client cache, faked WS-peer acquisition, canned ``_ws_request_response``
acks, a faked slice-group config read; the REAL push funnel runs.

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


# --- a tiny in-memory /nodes-export cache ----------------------------------
_ROOT = "33333333-1111-bbbb-cccc-000000000001"
_CHILD = "33333333-2222-bbbb-cccc-000000000002"

_CACHE_NODES = [
    {"id": _ROOT, "name": "Push Blob Root", "note": "", "parent_id": None, "priority": 0},
    {"id": _CHILD, "name": "Child point", "note": "body", "parent_id": _ROOT, "priority": 0},
]

_GROUP_ID = "grp-blobpush-1"
_GROUP_NAME = "Blob|Push Group"


def _make_client():
    """A real WorkFlowyClientNexus (render helpers intact) with a fake cache."""
    c = WorkFlowyClientNexus.__new__(WorkFlowyClientNexus)
    c._get_nodes_export_cache_nodes = lambda: [dict(n) for n in _CACHE_NODES]
    return c


def _patch_push_transport(monkeypatch, frames, tab_blob=None):
    """Stub the WS transport seams the push funnel + modal read use.

    Every frame sent through ``_ws_request_response`` is recorded in
    ``frames``. ``apply_slice_in_dock`` gets the receipt-ack the real tab
    sends (``{success: True, rendering: True}``); a ``get_current_modal_
    render_spec`` query (only reachable pre-fix / in the modal test) answers
    with ``tab_blob`` so the pre-fix content path SUCCEEDS -- keeping the
    refusal test mutation-sensitive.
    """
    monkeypatch.setattr(srv, "get_client", _make_client)

    async def _fake_grace(*a, **k):
        return ("ws-conn-sentinel", "ws-queue-sentinel")

    async def _fake_ws_request_response(self, action, **kwargs):
        frames.append({
            "action": action,
            "payload": kwargs.get("payload"),
            "target_tab": kwargs.get("target_tab"),
        })
        if action == "get_current_modal_render_spec":
            return {"success": True, "render_spec": tab_blob or {}}
        return {"success": True, "rendering": True}

    monkeypatch.setattr(srv, "get_ws_connection_with_grace_wait", _fake_grace)
    monkeypatch.setattr(
        WorkFlowyClientNexus, "_ws_request_response", _fake_ws_request_response
    )


def _patch_slice_group_config(monkeypatch):
    """Canned live slice-group config (the funnel's loud-early resolution)."""
    async def _fake_resolve_slice_group_config(target_tab):
        return (
            [
                {
                    "id": _GROUP_ID,
                    "name": _GROUP_NAME,
                    "prefixes": ["bp"],
                    "root_uuid": _ROOT,
                }
            ],
            None,
            {},
            [],
            {},
        )

    monkeypatch.setattr(
        srv, "_resolve_slice_group_config", _fake_resolve_slice_group_config
    )


def _assert_push_ack(reply):
    """The funnel's ack shape -- and nothing content-render-shaped."""
    assert isinstance(reply, dict), reply
    assert reply.get("_source") == "push_to_modal", reply
    assert reply.get("pushed_to_modal") is True, reply
    assert reply.get("rendering") is True, reply
    assert reply.get("success") is True, reply
    # NOT content, NOT a file summary.
    assert "preview_markdown" not in reply, reply
    assert "preview_tree" not in reply, reply
    assert "bytes_written" not in reply, reply


# ---------------------------------------------------------------------------
# 1a. raw_root blob + push -> push ack + apply_slice_in_dock frame.
# ---------------------------------------------------------------------------
def test_blob_push_raw_root_returns_push_ack_not_content(monkeypatch):
    frames = []
    _patch_push_transport(monkeypatch, frames)

    blob = RenderProjectionSpec.normalize_from_glimpse_args(
        root_uuid=_ROOT,
    ).to_render_spec_blob()

    reply = asyncio.run(
        srv._glimpse_run_impl(
            render_spec=blob, push_to_modal=True, target_tab="SCRATCH"
        )
    )

    _assert_push_ack(reply)
    assert reply.get("root_uuid") == _ROOT
    # The wire frame is the canonical push command, aimed at the named tab,
    # carrying a re-normalized blob (the funnel emits its OWN spec).
    assert len(frames) == 1
    assert frames[0]["action"] == "apply_slice_in_dock"
    assert frames[0]["target_tab"] == "SCRATCH"
    pushed = frames[0]["payload"]["render_spec"]
    assert pushed["kind"] == "raw_root"
    assert pushed["root_uuid"] == _ROOT


# ---------------------------------------------------------------------------
# 1b. slice blob (harvested-fixture shape: kind 'slice' + resolved-root echo
#     + focus) + output_file + push -> push ack, NOT the incident's file
#     summary; kind preserved; focus dropped; group echoed.
# ---------------------------------------------------------------------------
def test_blob_push_slice_with_output_file_returns_ack_not_file_summary(
    monkeypatch, tmp_path
):
    frames = []
    _patch_push_transport(monkeypatch, frames)
    _patch_slice_group_config(monkeypatch)

    # The producer-resolved slice blob shape (the harvest fixtures): kind
    # 'slice', group_id, AND the group's stored root echoed into root_uuid,
    # plus the selected row folded in as `focus`.
    blob = RenderProjectionSpec.normalize_from_glimpse_args(
        slice_group=_GROUP_ID,
        group_name=_GROUP_NAME,
    ).to_render_spec_blob()
    blob["root_uuid"] = _ROOT
    blob["focus"] = {
        "mode": "row",
        "uuid": _CHILD,
        "ancestor_path": [_ROOT],
        "occurrence": 0,
    }

    reply = asyncio.run(
        srv._glimpse_run_impl(
            render_spec=blob,
            push_to_modal=True,
            target_tab="SCRATCH 002",
            output_file=str(tmp_path / "blob_push.md"),
        )
    )

    # The incident's false success was {_source: 'cache_slice_group',
    # bytes_written: ...}; the honest reply is the push ack.
    _assert_push_ack(reply)
    assert reply.get("group_id") == _GROUP_ID
    assert reply.get("group_name") == _GROUP_NAME

    assert len(frames) == 1
    assert frames[0]["action"] == "apply_slice_in_dock"
    pushed = frames[0]["payload"]["render_spec"]
    # kind-faithful routing: a plain 'slice' blob pushes as a plain slice at
    # the group's LIVE stored root -- the echoed root_uuid is display, not an
    # override (a blob push admits exactly what a blob render admits).
    assert pushed["kind"] == "slice"
    assert pushed["group_id"] == _GROUP_ID
    assert pushed["root_uuid"] is None
    # focus is DELIBERATELY dropped by the funnel's normalization (a push
    # paints the FULL document; consistent with canonical pushes).
    assert pushed["focus"] is None


# ---------------------------------------------------------------------------
# 2. the blob branch CANNOT return rendered content while the flag is set
#    (tripwire on the content dispatcher; JSON-string blob form).
# ---------------------------------------------------------------------------
def test_blob_push_never_routes_to_content_dispatcher(monkeypatch):
    frames = []
    _patch_push_transport(monkeypatch, frames)

    tripped = []

    async def _tripwire(spec, *, output_file, target_tab):
        tripped.append(spec)
        return {"success": True, "preview_markdown": "CONTENT-RENDERED"}

    monkeypatch.setattr(srv, "_glimpse_dispatch_spec", _tripwire)

    blob = RenderProjectionSpec.normalize_from_glimpse_args(
        root_uuid=_ROOT,
    ).to_render_spec_blob()

    reply = asyncio.run(
        srv._glimpse_run_impl(
            render_spec=json.dumps(blob),  # the agent-paste JSON-string form
            push_to_modal=True,
        )
    )

    assert tripped == [], "content dispatcher must be unreachable while pushing"
    assert not isinstance(reply, str), reply
    _assert_push_ack(reply)
    assert len(frames) == 1
    assert frames[0]["action"] == "apply_slice_in_dock"


# ---------------------------------------------------------------------------
# 3. render_spec='modal' + push -> loud refusal BEFORE any wire frame.
# ---------------------------------------------------------------------------
def test_modal_read_plus_push_refuses_loudly(monkeypatch):
    frames = []
    tab_blob = RenderProjectionSpec.normalize_from_glimpse_args(
        root_uuid=_ROOT,
    ).to_render_spec_blob()
    _patch_push_transport(monkeypatch, frames, tab_blob=tab_blob)

    reply = asyncio.run(
        srv._glimpse_run_impl(render_spec="modal", push_to_modal=True)
    )

    assert isinstance(reply, dict), reply
    assert reply.get("success") is False, reply
    err = str(reply.get("error") or "")
    assert "push_to_modal" in err, reply
    # Refused BEFORE any WS traffic: neither a modal-read query nor a push
    # frame may leave the server.
    assert frames == [], frames


# ---------------------------------------------------------------------------
# 4. degenerate blob (kind with no scope) + push -> loud refusal naming
#    push_to_modal, BEFORE any wire frame (the funnel's caller contract).
# ---------------------------------------------------------------------------
def test_degenerate_blob_push_refuses_before_any_frame(monkeypatch):
    frames = []
    _patch_push_transport(monkeypatch, frames)

    reply = asyncio.run(
        srv._glimpse_run_impl(render_spec={"kind": "raw_root"}, push_to_modal=True)
    )

    assert isinstance(reply, dict), reply
    assert reply.get("success") is False, reply
    err = str(reply.get("error") or "")
    # The refusal must come from the PUSH path, not a content-render error
    # (pre-fix, this blob produced dispatch_spec's no-root render error,
    # which never mentions push_to_modal).
    assert "push_to_modal" in err, reply
    assert frames == [], frames


# ---------------------------------------------------------------------------
# 5. lightning_rod blob + push -> the funnel's 4th scope cell.
# ---------------------------------------------------------------------------
def test_blob_push_lightning_selection_routes_through_funnel(monkeypatch):
    frames = []
    _patch_push_transport(monkeypatch, frames)

    blob = RenderProjectionSpec.normalize_from_glimpse_args(
        root_uuid=_ROOT,
        lightning={"mode": "render_tags", "tags": ["bp"]},
    ).to_render_spec_blob()

    reply = asyncio.run(
        srv._glimpse_run_impl(render_spec=blob, push_to_modal=True)
    )

    _assert_push_ack(reply)
    assert reply.get("root_uuid") == _ROOT
    assert (reply.get("lightning") or {}).get("mode") == "render_tags", reply
    assert len(frames) == 1
    assert frames[0]["action"] == "apply_slice_in_dock"
    pushed = frames[0]["payload"]["render_spec"]
    assert pushed["kind"] == "lightning_rod"
    assert pushed["root_uuid"] == _ROOT
    assert pushed["lightning"]["mode"] == "render_tags"
    assert pushed["lightning"]["tags"] == ["bp"]
