"""Phase 1 multi-select (ap:MULTISEL) — _handle_render_slice_modal integration.

Drives the REAL async docked-modal render handler end-to-end for the
self-contained RAW-DUMP (root_uuid-only) cell — the cell with NO nested
WebSocket config read — with a mode='rows' focus in the render_spec blob, and
asserts:

  * a render_slice_modal frame carrying focus={mode:'rows', rows:[...]} renders
    the MINIMAL COVERING SUBTREE (both selected leaves' bodies present, off-path
    siblings pruned), NOT the per-row single-triple path;
  * is_row_mode is NOT triggered by a rows focus (the reply action is
    render_slice_modal_result, the full-document reply, with the full top-half
    `rows` list intact);
  * an unresolvable selected row makes the WHOLE render fail loud
    (slice_row_stale), never silently dropping a row.

Mirrors tests/test_render_slice_modal_spec_e3.py's fake-client + capture-ws
idiom. All local: no network, no real Workflowy, no disk.
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


_ROOT = "22222222-aaaa-bbbb-cccc-000000000001"
_A = "22222222-aaaa-bbbb-cccc-000000000002"
_A1 = "22222222-aaaa-bbbb-cccc-000000000003"
_A2 = "22222222-aaaa-bbbb-cccc-000000000004"
_B = "22222222-aaaa-bbbb-cccc-000000000005"
_B1 = "22222222-aaaa-bbbb-cccc-000000000006"

_CACHE_NODES = [
    {"id": _ROOT, "name": "Root", "note": "root body", "parent_id": None, "priority": 0},
    {"id": _A, "name": "Alpha", "note": "alpha body", "parent_id": _ROOT, "priority": 0},
    {"id": _A1, "name": "Alpha-1", "note": "a1 body", "parent_id": _A, "priority": 0},
    {"id": _A2, "name": "Alpha-2", "note": "a2 body", "parent_id": _A, "priority": 1},
    {"id": _B, "name": "Beta", "note": "beta body", "parent_id": _ROOT, "priority": 1},
    {"id": _B1, "name": "Beta-1", "note": "b1 body", "parent_id": _B, "priority": 0},
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


def _run_handler(frame, monkeypatch):
    fake_ws = _FakeCaptureWebsocket()
    monkeypatch.setattr(srv, "get_client", _make_client)
    asyncio.run(srv._handle_render_slice_modal(frame, fake_ws))
    assert fake_ws.sent, "handler sent no reply"
    return fake_ws.sent[-1]


def _rows_frame(rows, *, request_id="req-ms-1"):
    spec = RenderProjectionSpec.normalize_from_glimpse_args(
        root_uuid=_ROOT,
        focus={"mode": "rows", "rows": rows},
    )
    return {
        "action": "render_slice_modal",  # full-document action (NOT render_slice_row)
        "request_id": request_id,
        "endpoint_id": "tab-1",
        "render_spec": spec.to_render_spec_blob(),
    }


def test_rows_focus_renders_minimal_covering_subtree(monkeypatch):
    frame = _rows_frame([
        {"uuid": _A1, "ancestor_path": [_ROOT, _A], "occurrence": 0},
        {"uuid": _B1, "ancestor_path": [_ROOT, _B], "occurrence": 0},
    ])
    reply = _run_handler(frame, monkeypatch)
    assert reply.get("success") is True, reply
    # FULL-DOCUMENT reply (NOT the per-row result action) -- a rows focus does
    # NOT trigger is_row_mode.
    assert reply.get("action") == "render_slice_modal_result", reply
    md = reply.get("markdown") or ""
    # Both selected leaves' bodies present; off-path sibling a2 pruned.
    assert "a1 body" in md
    assert "b1 body" in md
    assert "a2 body" not in md
    assert "Alpha-2" not in md
    # Breadcrumb ancestors render heading-only (bodies withheld).
    assert "root body" not in md
    assert "alpha body" not in md
    assert "beta body" not in md
    # The top-half rows list is the FULL document (focus never shrinks it).
    rows = reply.get("rows") or []
    assert [r["uuid"] for r in rows] == [_ROOT, _A, _A1, _A2, _B, _B1]


def test_rows_focus_stale_row_fails_loud(monkeypatch):
    frame = _rows_frame([
        {"uuid": _A1, "ancestor_path": [_ROOT, _A], "occurrence": 0},
        {"uuid": "ghost", "ancestor_path": [_ROOT, "nope"], "occurrence": 0},
    ], request_id="req-ms-2")
    reply = _run_handler(frame, monkeypatch)
    # ALL-OR-NOTHING: one bad row -> the whole render is stale (not a silent
    # drop, not a partial cover).
    assert reply.get("slice_row_stale") is True, reply
    assert (reply.get("markdown") or "") == ""


def test_single_element_rows_matches_single_row_action(monkeypatch):
    # A 1-element rows focus on the full-document action renders the same
    # option-Y content a single-row focus would -- the regression anchor.
    rows_reply = _run_handler(
        _rows_frame(
            [{"uuid": _A1, "ancestor_path": [_ROOT, _A], "occurrence": 0}],
            request_id="req-ms-3",
        ),
        monkeypatch,
    )
    # Single-row via the SAME full-document action (focus mode='row').
    single_spec = RenderProjectionSpec.normalize_from_glimpse_args(
        root_uuid=_ROOT,
        focus={"mode": "row", "uuid": _A1, "ancestor_path": [_ROOT, _A], "occurrence": 0},
    )
    single_reply = _run_handler(
        {
            "action": "render_slice_modal",
            "request_id": "req-ms-4",
            "endpoint_id": "tab-1",
            "render_spec": single_spec.to_render_spec_blob(),
        },
        monkeypatch,
    )
    # Both focus exactly a1's option-Y view; the rendered markdown matches.
    assert (rows_reply.get("markdown") or "") == (single_reply.get("markdown") or "")


def test_render_spec_dispatch_rows_focus_covers(monkeypatch):
    # The render_spec="modal" agent read path: _glimpse_dispatch_spec routes a
    # raw_root spec carrying a mode='rows' focus to the cache tail, which
    # renders the covering subtree. Proves the dispatch passes a rows focus
    # through (not just single-row).
    from workflowy_mcp.cache_render_spec import RenderProjectionSpec
    monkeypatch.setattr(srv, "get_client", _make_client)
    spec = RenderProjectionSpec.normalize_from_glimpse_args(
        root_uuid=_ROOT,
        focus={"mode": "rows", "rows": [
            {"uuid": _A1, "ancestor_path": [_ROOT, _A], "occurrence": 0},
            {"uuid": _B1, "ancestor_path": [_ROOT, _B], "occurrence": 0},
        ]},
    ).with_overrides(kind="raw_root", root_uuid=_ROOT)
    res = asyncio.run(
        srv._glimpse_dispatch_spec(spec, output_file=None, target_tab=None)
    )
    md = res.get("preview_markdown") or ""
    assert "a1 body" in md
    assert "b1 body" in md
    assert "a2 body" not in md


def test_grace_resolve_preserves_rows_focus(monkeypatch):
    # ROOT-CAUSE REGRESSION LOCK (ap:MULTISEL live-smoke bug): the render_spec
    # UUID prefix-grace step (_grace_resolve_spec_uuids, used by BOTH
    # render_spec="modal" and the pasted-blob path) MUST preserve a mode='rows'
    # focus. The prior code rebuilt only a single-row {mode,uuid,ancestor_path}
    # shape and DROPPED the rows list, so a rows focus degraded to None and the
    # server silently rendered the WHOLE base scope. This asserts the rows
    # survive grace-resolution with their triples intact.
    cache_by_id = {n["id"]: dict(n) for n in _CACHE_NODES}
    spec = RenderProjectionSpec.normalize_from_glimpse_args(
        root_uuid=_ROOT,
        focus={"mode": "rows", "rows": [
            {"uuid": _A1, "ancestor_path": [_ROOT, _A], "occurrence": 0},
            {"uuid": _B1, "ancestor_path": [_ROOT, _B], "occurrence": 0},
        ]},
    ).with_overrides(kind="raw_root", root_uuid=_ROOT)
    graced = srv._grace_resolve_spec_uuids(spec, cache_by_id)
    assert isinstance(graced.focus, dict), "rows focus must survive (not degrade to None)"
    assert graced.focus.get("mode") == "rows"
    _rows = graced.focus.get("rows") or []
    assert len(_rows) == 2, "both selected rows must be preserved"
    _uuids = {r.get("uuid") for r in _rows}
    assert _uuids == {_A1, _B1}
    # ancestor_path preserved per row (canonicalized, same UUIDs here).
    for r in _rows:
        assert isinstance(r.get("ancestor_path"), list) and len(r["ancestor_path"]) == 2


def test_grace_resolve_full_modal_read_rows_covers_not_whole_tree(monkeypatch):
    # END-TO-END: a rows focus that flows through _grace_resolve_spec_uuids ->
    # _glimpse_dispatch_spec (the render_spec="modal" tail) renders the COVERING
    # SUBTREE, NOT the whole document. This is the exact path that produced the
    # live 'whole tree' fallback before the grace-resolve rows fix.
    monkeypatch.setattr(srv, "get_client", _make_client)
    cache_by_id = {n["id"]: dict(n) for n in _CACHE_NODES}
    spec = RenderProjectionSpec.normalize_from_glimpse_args(
        root_uuid=_ROOT,
        focus={"mode": "rows", "rows": [
            {"uuid": _A1, "ancestor_path": [_ROOT, _A], "occurrence": 0},
            {"uuid": _B1, "ancestor_path": [_ROOT, _B], "occurrence": 0},
        ]},
    ).with_overrides(kind="raw_root", root_uuid=_ROOT)
    graced = srv._grace_resolve_spec_uuids(spec, cache_by_id)
    res = asyncio.run(
        srv._glimpse_dispatch_spec(graced, output_file=None, target_tab=None)
    )
    md = res.get("preview_markdown") or ""
    assert "a1 body" in md
    assert "b1 body" in md
    # The whole-tree fallback WOULD have included a2 body (off-path sibling);
    # the covering subtree must NOT.
    assert "a2 body" not in md


def test_modal_read_focus_intent_loss_fails_loud(monkeypatch):
    # LOUD-FAIL GUARD (Dan's hard requirement): if the tab reports a NON-EMPTY
    # focus but it collapses to None server-side (here forced by monkeypatching
    # grace to void it), render_spec="modal" must ABORT with success:false /
    # focus_intent_lost -- NEVER silently render the whole (possibly
    # multi-megabyte) modal root.
    # A reply blob carrying an intended rows focus (two real uuids).
    spec_blob = RenderProjectionSpec.normalize_from_glimpse_args(
        root_uuid=_ROOT,
        focus={"mode": "rows", "rows": [
            {"uuid": _A1, "ancestor_path": [_ROOT, _A], "occurrence": 0},
            {"uuid": _B1, "ancestor_path": [_ROOT, _B], "occurrence": 0},
        ]},
    ).with_overrides(kind="raw_root", root_uuid=_ROOT).to_render_spec_blob()

    # A client whose WS query returns that reply blob, and whose grace-resolve
    # is forced to VOID the focus to None (the intent-loss condition).
    client = _make_client()

    async def _fake_ws_rr(*a, **k):
        return {
            "success": True,
            "modal_payload_kind": "cache_render_spec",
            "render_spec": spec_blob,
        }

    client._ws_request_response = _fake_ws_rr
    monkeypatch.setattr(srv, "get_client", lambda: client)

    async def _fake_grace_conn(*a, **k):
        return (object(), object())

    monkeypatch.setattr(srv, "get_ws_connection_with_grace_wait", _fake_grace_conn, raising=False)
    monkeypatch.setattr(
        srv, "_grace_resolve_spec_uuids",
        lambda spec, cache: spec.with_overrides(focus=None),
    )

    res = asyncio.run(srv._glimpse_read_modal(
        target_tab=None, output_file=None,
        ov_as_markdown=None, ov_headings_only=None,
        ov_sort_by=None, ov_sort_direction=None, ov_since=None,
    ))
    assert res.get("success") is False, res
    assert res.get("focus_intent_lost") is True, res
