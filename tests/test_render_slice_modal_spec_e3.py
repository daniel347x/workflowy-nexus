"""Phase E3 integration lock: _handle_render_slice_modal routes through the spec.

Phase E3 rewires the docked-modal render handler (``_handle_render_slice_modal``)
so it normalizes the incoming Apply/push frame into ONE RenderProjectionSpec at
the top, resolves the scope into a RESOLVED ``render_spec`` via ``with_overrides``,
and composes the server-authoritative ``render_scope`` echo FROM that spec via the
E1 bridge ``_compose_render_scope_from_spec`` -- instead of the old parallel
scattered-locals + hand-assembled ``_compose_render_scope(...)`` seam.

These tests drive the REAL async handler end-to-end for the self-contained
RAW-DUMP (``root_uuid`` only) cell -- the cell that needs NO WebSocket config
read -- with a minimal monkeypatched client + an in-memory /nodes-export cache,
and assert the returned ``render_scope`` is exactly what the spec-derived path
should produce. This is the behavior-preserving regression lock for the E3
handler rewrite (the unit-level bridge equivalence is locked separately in
``test_render_projection_spec_phaseE.py``; this proves the handler actually
builds + threads the spec correctly).

We exercise the raw-dump cell (and its per-row twin, and the descend_policy
unmoored signal indirectly via the raw_dump reason) because it is the one
cache-projected cell with no nested Direction-A config round-trip, so it runs
faithfully under a tiny fake client without a live Workflowy tab.

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
from workflowy_mcp.glimpse_since_policy import since_from_edge  # noqa: E402


# --- a tiny in-memory /nodes-export cache (a 3-node subtree) ----------------
_ROOT = "11111111-aaaa-bbbb-cccc-000000000001"
_CHILD_A = "11111111-aaaa-bbbb-cccc-000000000002"
_CHILD_B = "11111111-aaaa-bbbb-cccc-000000000003"

_CACHE_NODES = [
    {"id": _ROOT, "name": "Anchor Root", "note": "", "parent_id": None, "priority": 0},
    {"id": _CHILD_A, "name": "First point", "note": "body A", "parent_id": _ROOT, "priority": 0},
    {"id": _CHILD_B, "name": "Second point", "note": "body B", "parent_id": _ROOT, "priority": 1},
]


class _FakeCaptureWebsocket:
    """Captures the single JSON reply the handler sends."""

    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, payload: str):
        try:
            self.sent.append(json.loads(payload))
        except Exception:
            self.sent.append({"_raw": payload})


def _make_client():
    """A real WorkFlowyClientNexus (render methods intact) with a fake cache.

    Built via ``__new__`` so no live API/config is required; the raw-dump modal
    path only calls the pure render helpers + ``_get_nodes_export_cache_nodes``.
    S03-B: also carries the operation-boundary surface (the WS front wraps the
    handler in a cache_operation_boundary).
    """
    from coherence_harness import make_boundary_capable

    c = WorkFlowyClientNexus.__new__(WorkFlowyClientNexus)
    c._get_nodes_export_cache_nodes = lambda: [dict(n) for n in _CACHE_NODES]
    return make_boundary_capable(c, nodes=_CACHE_NODES)


# Transport-envelope keys a real Apply/push frame carries flat (NOT part of the
# render_spec scope/view contract).
#
# NOTE (row-as-render-primitive migration, Aug 2026): ``row`` was REMOVED from
# this set. The clicked per-row identity triple is no longer a flat envelope
# field -- it is the render PRIMITIVE ``render_spec.focus`` inside the ONE
# opaque blob (a {mode:'row', uuid, ancestor_path, occurrence} dict), exactly
# like every other scope/view knob. Per-row tests now build the frame with
# ``render_spec`` carrying ``focus`` (see test_raw_dump_per_row_*), and the
# blob-wins / focus-round-trip locks below exercise it directly.
_ENVELOPE_KEYS = frozenset(
    {"action", "request_id", "endpoint_id", "max_prerender_chars"}
)


def _to_wire_frame(frame):
    """Pack a test frame's scope/view keys into the ONE nested render_spec blob.

    The real wire carries the whole request as a single opaque ``render_spec``
    blob (the sole scope/view contract); only the envelope keys stay flat. This
    helper lets the test bodies keep readable flat scope literals while feeding
    the handler exactly what the client/push now send. A frame that already
    carries ``render_spec`` is passed through unchanged.
    """
    from workflowy_mcp.cache_render_spec import RenderProjectionSpec
    from workflowy_mcp.glimpse_since_policy import since_from_edge
    if "render_spec" in frame:
        return dict(frame)
    scope = {k: v for k, v in frame.items() if k not in _ENVELOPE_KEYS}
    envelope = {k: v for k, v in frame.items() if k in _ENVELOPE_KEYS}
    spec = RenderProjectionSpec.normalize_from_glimpse_args(
        group_id=(scope.get("group_id") or None),
        root_uuid=(scope.get("root_uuid") or None),
        lightning=(scope.get("lightning") if isinstance(scope.get("lightning"), dict) else None),
        match_scope=str(scope.get("match_scope") or "name_and_note"),
        descend_policy=str(scope.get("descend_policy") or "all"),
        sort_by=scope.get("sort_by"),
        sort_direction=str(scope.get("sort_direction") or "asc"),
        since=since_from_edge(scope.get("since")),
        headings_only=bool(scope.get("headings_only") or False),
    )
    return {**envelope, "render_spec": spec.to_render_spec_blob()}


def _run_handler(frame, monkeypatch):
    fake_ws = _FakeCaptureWebsocket()
    monkeypatch.setattr(srv, "get_client", _make_client)
    asyncio.run(srv._handle_render_slice_modal(_to_wire_frame(frame), fake_ws))
    assert fake_ws.sent, "handler sent no reply"
    return fake_ws.sent[-1]


# ---------------------------------------------------------------------------
# RAW-DUMP full-document render: render_scope must be the spec-derived echo.
# ---------------------------------------------------------------------------
def test_raw_dump_full_document_render_scope_is_spec_derived(monkeypatch):
    frame = {
        "action": "render_slice_modal",
        "request_id": "req-e3-1",
        "endpoint_id": "tab-1",
        "root_uuid": _ROOT,  # raw-dump cell: no group_id, no lightning
    }
    reply = _run_handler(frame, monkeypatch)

    assert reply.get("success") is True, reply
    assert reply.get("cache_sourced") is True
    scope = reply.get("render_scope")
    assert isinstance(scope, dict), reply

    # kind + identity come from the spec's raw_root cell.
    assert scope["kind"] == "raw_root"
    assert scope["group_id"] is None
    assert scope["group_name"] is None
    assert scope["root_uuid"] == _ROOT
    # default view knobs round-tripped through the spec.
    assert scope["sort_by"] == "none"
    assert scope["sort_direction"] == "asc"
    assert scope["since"] is None
    assert scope["headings_only"] is False
    assert scope["match_scope"] == "name_and_note"
    assert scope["descend_policy"] == "all"
    assert scope["content_format"] == "markdown"
    assert scope["tree_mode"] == "render_tree"
    # a raw dump is unmoored (no membership search to reproduce in the pane).
    assert scope["live_gui_sync_supported"] is False
    assert scope["live_gui_sync_skipped_reason"] == "raw_dump"

    # The full subtree rendered (root + 2 children); rows mirror the tree.
    assert reply.get("node_count") == 3
    rows = reply.get("rows") or []
    assert [r["uuid"] for r in rows] == [_ROOT, _CHILD_A, _CHILD_B]


def test_raw_dump_matches_bridge_output_exactly(monkeypatch):
    """The handler's echoed render_scope == _compose_render_scope_from_spec(spec)."""
    from workflowy_mcp.cache_render_spec import RenderProjectionSpec

    frame = {
        "action": "render_slice_modal",
        "request_id": "req-e3-2",
        "endpoint_id": "tab-1",
        "root_uuid": _ROOT,
        "headings_only": True,
    }
    reply = _run_handler(frame, monkeypatch)
    scope = reply["render_scope"]

    # The RESOLVED spec the handler should have built for this raw-dump frame.
    expected_spec = RenderProjectionSpec.normalize_from_glimpse_args(
        root_uuid=_ROOT, headings_only=True,
    ).with_overrides(
        kind="raw_root", group_id=None, group_name=None, root_uuid=_ROOT,
    )
    expected_scope = srv._compose_render_scope_from_spec(expected_spec)
    assert scope == expected_scope, f"handler scope != bridge scope\n{scope}\n{expected_scope}"


# ---------------------------------------------------------------------------
# RAW-DUMP per-row render: same spec-derived render_scope echoed (uniform shape).
# ---------------------------------------------------------------------------
def test_raw_dump_per_row_echoes_same_render_scope(monkeypatch):
    from workflowy_mcp.cache_render_spec import RenderProjectionSpec

    # ROW FOCUS IS A RENDER PRIMITIVE (Aug 2026 migration): the clicked row's
    # identity triple rides in ``render_spec.focus`` inside the ONE opaque blob,
    # NOT a flat frame ``row`` field. Build the frame the way the real client
    # now sends it -- focus in the blob -- and prove the handler derives row-mode
    # from it.
    spec = RenderProjectionSpec.normalize_from_glimpse_args(
        root_uuid=_ROOT,
        focus={"mode": "row", "uuid": _CHILD_A, "ancestor_path": [_ROOT], "occurrence": 0},
    )
    frame = {
        "action": "render_slice_row",
        "request_id": "req-e3-3",
        "endpoint_id": "tab-1",
        "render_spec": spec.to_render_spec_blob(),
    }
    reply = _run_handler(frame, monkeypatch)
    assert reply.get("success") is True, reply
    scope = reply.get("render_scope")
    assert isinstance(scope, dict)
    assert scope["kind"] == "raw_root"
    assert scope["root_uuid"] == _ROOT
    assert scope["live_gui_sync_supported"] is False
    # A focused render's render_scope honestly echoes the focus it rendered.
    assert isinstance(scope.get("focus"), dict), reply
    assert scope["focus"]["mode"] == "row"
    assert scope["focus"]["uuid"] == _CHILD_A
    assert scope["focus"]["ancestor_path"] == [_ROOT]
    assert scope["focus"]["occurrence"] == 0
    # The per-row markdown focuses the clicked child (contains its name).
    assert "First point" in (reply.get("markdown") or "")


# ---------------------------------------------------------------------------
# ROW-AS-RENDER-PRIMITIVE (Aug 2026): focus round-trips through the blob, and a
# contradictory flat ``row`` field is IGNORED (the blob's focus wins) -- the
# same opaque-wire robustness invariant the E6 locks assert for the scope/view
# knobs, now extended to the row focus primitive.
# ---------------------------------------------------------------------------
def test_focus_in_blob_drives_per_row_render(monkeypatch):
    from workflowy_mcp.cache_render_spec import RenderProjectionSpec

    # A render_slice_MODAL action (NOT render_slice_row) that nonetheless
    # carries a blob focus must STILL render per-row: is_row_mode is derived
    # from focus presence, not only the action name.
    spec = RenderProjectionSpec.normalize_from_glimpse_args(
        root_uuid=_ROOT,
        focus={"mode": "row", "uuid": _CHILD_B, "ancestor_path": [_ROOT], "occurrence": 0},
    )
    frame = {
        "action": "render_slice_modal",
        "request_id": "req-focus-1",
        "endpoint_id": "tab-1",
        "render_spec": spec.to_render_spec_blob(),
    }
    reply = _run_handler(frame, monkeypatch)
    assert reply.get("success") is True, reply
    # It focused _CHILD_B ("Second point"), NOT the full document.
    md = reply.get("markdown") or ""
    assert "Second point" in md, reply
    assert "First point" not in md, reply
    scope = reply.get("render_scope") or {}
    assert isinstance(scope.get("focus"), dict), reply
    assert scope["focus"]["uuid"] == _CHILD_B, reply


def test_contradictory_flat_row_is_ignored_blob_focus_wins(monkeypatch):
    from workflowy_mcp.cache_render_spec import RenderProjectionSpec

    # Blob focus targets _CHILD_A ("First point"). Adversarial flat ``row``
    # lies, targeting _CHILD_B. There is NO flat-row read in the handler (the
    # legacy fallback was fully REMOVED, not merely deprioritized), so the flat
    # ``row`` is completely inert: the render focuses _CHILD_A and the echoed
    # render_scope.focus is _CHILD_A. This is the row-field twin of the E6
    # contradictory-flat-field locks below (since / headings_only / root_uuid /
    # descend_policy): a reintroduced flat ``data.get("row")`` read would flip
    # this to _CHILD_B and fail here.
    spec = RenderProjectionSpec.normalize_from_glimpse_args(
        root_uuid=_ROOT,
        focus={"mode": "row", "uuid": _CHILD_A, "ancestor_path": [_ROOT], "occurrence": 0},
    )
    frame = {
        "action": "render_slice_row",
        "request_id": "req-focus-2",
        "endpoint_id": "tab-1",
        "render_spec": spec.to_render_spec_blob(),
        # adversarial contradictory flat legacy field (must be IGNORED):
        "row": {"uuid": _CHILD_B, "ancestor_path": [_ROOT], "occurrence": 0},
    }
    reply = _run_handler(frame, monkeypatch)
    assert reply.get("success") is True, reply
    md = reply.get("markdown") or ""
    assert "First point" in md, reply
    assert "Second point" not in md, reply
    scope = reply.get("render_scope") or {}
    assert isinstance(scope.get("focus"), dict), reply
    assert scope["focus"]["uuid"] == _CHILD_A, reply


# (The former ``test_legacy_flat_row_still_works_when_blob_has_no_focus`` was
# DELETED, Aug 2026: it locked the mid-deploy tolerance where a flat ``row`` +
# a focusless blob still rendered per-row via a fallback. That fallback was
# REMOVED entirely -- deploy is atomic (server+client+tests ship together) and
# the client always sends focus INSIDE render_spec, so a flat-``row`` frame
# cannot occur on the wire. The no-flat-row invariant is now locked by
# ``test_contradictory_flat_row_is_ignored_blob_focus_wins`` above (a flat
# ``row`` present alongside a blob focus is proven INERT).)


# ---------------------------------------------------------------------------
# RAW-DUMP + created sort: the sort flows through the spec into render_scope.
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# E4 -- CONTENT-RETURN raw-dump GLIMPSE (_glimpse_root_uuid_from_cache): its
# render_scope must be the spec-derived echo (server-as-originator), matching
# _compose_render_scope_from_spec of the resolved raw_root spec exactly.
# This path has NO WebSocket touch (pure cache), so it runs faithfully here.
# ---------------------------------------------------------------------------
def test_e4_root_uuid_glimpse_render_scope_is_spec_derived(monkeypatch):
    from workflowy_mcp.cache_render_spec import RenderProjectionSpec

    monkeypatch.setattr(srv, "get_client", _make_client)
    reply = asyncio.run(
        srv._glimpse_root_uuid_from_cache(
            root_uuid=_ROOT,
            as_markdown=True,
            output_file=None,
            sort_by=None,
            sort_direction="asc",
            headings_only=False,
            since=None,
            boundary_predicate=None,
        )
    )
    assert reply.get("success") is True, reply
    scope = reply.get("render_scope")
    assert isinstance(scope, dict), reply
    assert scope["kind"] == "raw_root"
    assert scope["root_uuid"] == _ROOT
    assert scope["live_gui_sync_supported"] is False
    assert scope["live_gui_sync_skipped_reason"] == "raw_dump"

    expected_spec = RenderProjectionSpec.normalize_from_glimpse_args(
        root_uuid=_ROOT, descend_policy="all", sort_by=None,
        sort_direction="asc", since=None, headings_only=False, as_markdown=True,
    ).with_overrides(root_uuid=_ROOT)
    expected_scope = srv._compose_render_scope_from_spec(expected_spec)
    assert scope == expected_scope, f"{scope}\n!=\n{expected_scope}"


# ---------------------------------------------------------------------------
# E2 -- OPAQUE WIRE at the modal handler: a frame carrying a nested render_spec
# blob (and NO top-level scope fields) renders correctly, proving the handler's
# normalize_from_modal_frame prefers the blob end-to-end.
# ---------------------------------------------------------------------------
def test_e2_modal_handler_honors_render_spec_blob(monkeypatch):
    from workflowy_mcp.cache_render_spec import RenderProjectionSpec

    spec = RenderProjectionSpec.normalize_from_glimpse_args(root_uuid=_ROOT)
    frame = {
        "action": "render_slice_modal",
        "request_id": "req-e2-1",
        "endpoint_id": "tab-1",
        # OPAQUE WIRE: the whole request rides in ONE nested render_spec object;
        # NO top-level group_id/root_uuid/sort_by/etc.
        "render_spec": spec.to_render_spec_blob(),
    }
    reply = _run_handler(frame, monkeypatch)
    assert reply.get("success") is True, reply
    scope = reply.get("render_scope")
    assert isinstance(scope, dict), reply
    assert scope["kind"] == "raw_root"
    assert scope["root_uuid"] == _ROOT
    assert reply.get("node_count") == 3


def test_raw_dump_sort_flows_through_spec(monkeypatch):
    # Give the cache nodes timestamps so the created-sort pre-pass can order them.
    def _cache_with_ts():
        nodes = [dict(n) for n in _CACHE_NODES]
        for i, n in enumerate(nodes):
            n["createdAt"] = 1000 + i
            n["modifiedAt"] = 1000 + i
        return nodes

    def _client_with_ts():
        from coherence_harness import make_boundary_capable

        c = WorkFlowyClientNexus.__new__(WorkFlowyClientNexus)
        c._get_nodes_export_cache_nodes = _cache_with_ts
        return make_boundary_capable(c, nodes=_cache_with_ts())

    fake_ws = _FakeCaptureWebsocket()
    monkeypatch.setattr(srv, "get_client", _client_with_ts)
    frame = {
        "action": "render_slice_modal",
        "request_id": "req-e3-4",
        "endpoint_id": "tab-1",
        "root_uuid": _ROOT,
        "sort_by": "created",
        "sort_direction": "desc",
    }
    asyncio.run(srv._handle_render_slice_modal(_to_wire_frame(frame), fake_ws))
    reply = fake_ws.sent[-1]
    assert reply.get("success") is True, reply
    scope = reply["render_scope"]
    assert scope["sort_by"] == "created"
    assert scope["sort_direction"] == "desc"
    assert scope["kind"] == "raw_root"


# ---------------------------------------------------------------------------
# `since` is a VIEW knob carried IN the render_spec blob (NOT a flat frame
# field). This locks that the handler sources `since` from incoming_spec, so a
# blob-only frame's item-count `since` actually prunes -- the regression that
# a leftover flat data.get("since") would have silently dropped once the wire
# went blob-only.
# ---------------------------------------------------------------------------
def test_since_item_count_flows_through_blob(monkeypatch):
    from workflowy_mcp.cache_render_spec import RenderProjectionSpec

    # A raw-dump over the 3-node subtree has 2 LEAVES (_CHILD_A, _CHILD_B).
    # since='last 1' (item-count; needs NO sort) keeps only the last leaf.
    spec = RenderProjectionSpec.normalize_from_glimpse_args(
        root_uuid=_ROOT, since=since_from_edge("last 1"),
    )
    frame = {
        "action": "render_slice_modal",
        "request_id": "req-since-1",
        "endpoint_id": "tab-1",
        "render_spec": spec.to_render_spec_blob(),
    }
    reply = _run_handler(frame, monkeypatch)
    assert reply.get("success") is True, reply
    # The item-count form fired (proving `since` was read from the blob, not a
    # now-absent flat field): since_applied echoes the item_count form, and the
    # render kept exactly the newest leaf. [ap:TVIIBN] 4d(a): 'last 1' now
    # SELECTS the newest leaf; this fixture's cache carries NO timestamps, so
    # every leaf is undated and undated leaves rank AFTER dated ones in
    # document order -- the top-1 of that ranking is the FIRST leaf (_CHILD_A),
    # the same rule dock.peek's missing-timestamps-sink-last policy implies.
    sa = reply.get("since_applied")
    assert isinstance(sa, dict) and sa.get("form") == "item_count", reply
    assert sa.get("selection") == "newest", reply
    rows = reply.get("rows") or []
    leaf_uuids = [r["uuid"] for r in rows if r["uuid"] in (_CHILD_A, _CHILD_B)]
    assert leaf_uuids == [_CHILD_A], reply


def test_raw_dump_modal_since_curated_is_rejected(monkeypatch):
    from workflowy_mcp.cache_render_spec import RenderProjectionSpec

    # since='curated' requires a slice group's tag vocabulary to define
    # "curated". A raw-root modal render has no group/prefixes, so it must fail
    # loudly instead of degrading to the misleading "nothing curated yet ->
    # render the whole subtree" no-op.
    spec = RenderProjectionSpec.normalize_from_glimpse_args(
        root_uuid=_ROOT,
        sort_by="created",
        since=since_from_edge("curated"),
    )
    frame = {
        "action": "render_slice_modal",
        "request_id": "req-since-curated-raw",
        "endpoint_id": "tab-1",
        "render_spec": spec.to_render_spec_blob(),
    }
    reply = _run_handler(frame, monkeypatch)
    assert reply.get("success") is False, reply
    assert reply.get("since_error") == "since_curated_requires_slice_group", reply
    assert "requires a slice group" in (reply.get("error") or ""), reply


# ---------------------------------------------------------------------------
# E6 VERIFICATION PASS -- CONTRADICTORY FLAT FRAME FIELDS ARE IGNORED.
#
# The E2/since tests above prove the blob IS READ. These prove the blob
# actively WINS when a frame ALSO carries contradictory flat top-level
# scope/view fields -- the precise robustness invariant the RenderProjectionSpec
# refactor exists to guarantee ("a new/leftover flat field must never silently
# override the one spec blob"). If a future edit reintroduced a flat
# ``data.get("since")`` / ``data.get("headings_only")`` /
# ``data.get("descend_policy")`` / ``data.get("root_uuid")`` read in
# _handle_render_slice_modal, exactly one of these would fail.
#
# We build each frame by hand (NOT via _to_wire_frame, which strips scope keys)
# so the WIRE genuinely carries BOTH a correct render_spec blob AND the
# adversarial flat fields side by side. All on the self-contained raw-dump cell
# (no WebSocket config read).
# ---------------------------------------------------------------------------
def test_contradictory_flat_since_is_ignored_blob_wins(monkeypatch):
    from workflowy_mcp.cache_render_spec import RenderProjectionSpec

    # Blob says: NO since (render both leaves). Flat field lies: since='last 1'.
    # If the handler read the flat field, only _CHILD_B would render.
    spec = RenderProjectionSpec.normalize_from_glimpse_args(root_uuid=_ROOT)
    frame = {
        "action": "render_slice_modal",
        "request_id": "req-e6-since",
        "endpoint_id": "tab-1",
        "render_spec": spec.to_render_spec_blob(),
        # adversarial contradictory flat field:
        "since": "last 1",
    }
    reply = _run_handler(frame, monkeypatch)
    assert reply.get("success") is True, reply
    # Blob wins: no since applied, full subtree rendered (root + both leaves).
    assert reply.get("since_applied") is None, reply
    assert reply.get("node_count") == 3, reply
    rows = reply.get("rows") or []
    leaf_uuids = [r["uuid"] for r in rows if r["uuid"] in (_CHILD_A, _CHILD_B)]
    assert leaf_uuids == [_CHILD_A, _CHILD_B], reply


def test_contradictory_flat_headings_only_is_ignored_blob_wins(monkeypatch):
    from workflowy_mcp.cache_render_spec import RenderProjectionSpec

    # Blob says: headings_only=False (bodies rendered). Flat field lies: True.
    # _CHILD_A's note is 'body A'; with headings_only the body would be dropped.
    spec = RenderProjectionSpec.normalize_from_glimpse_args(
        root_uuid=_ROOT, headings_only=False,
    )
    frame = {
        "action": "render_slice_modal",
        "request_id": "req-e6-headings",
        "endpoint_id": "tab-1",
        "render_spec": spec.to_render_spec_blob(),
        # adversarial contradictory flat field:
        "headings_only": True,
    }
    reply = _run_handler(frame, monkeypatch)
    assert reply.get("success") is True, reply
    scope = reply.get("render_scope") or {}
    # Blob wins in BOTH the echo and the rendered body.
    assert scope.get("headings_only") is False, reply
    assert "body A" in (reply.get("markdown") or ""), reply


def test_contradictory_flat_root_uuid_is_ignored_blob_wins(monkeypatch):
    from workflowy_mcp.cache_render_spec import RenderProjectionSpec

    # Blob scopes at _ROOT (3 nodes). Flat field lies: scope at _CHILD_A (a
    # leaf -> 1 node). If the handler read the flat root_uuid, node_count==1.
    spec = RenderProjectionSpec.normalize_from_glimpse_args(root_uuid=_ROOT)
    frame = {
        "action": "render_slice_modal",
        "request_id": "req-e6-root",
        "endpoint_id": "tab-1",
        "render_spec": spec.to_render_spec_blob(),
        # adversarial contradictory flat field:
        "root_uuid": _CHILD_A,
    }
    reply = _run_handler(frame, monkeypatch)
    assert reply.get("success") is True, reply
    scope = reply.get("render_scope") or {}
    assert scope.get("root_uuid") == _ROOT, reply
    assert reply.get("node_count") == 3, reply


def test_contradictory_flat_descend_policy_is_ignored_blob_wins(monkeypatch):
    from workflowy_mcp.cache_render_spec import RenderProjectionSpec

    # Blob says descend_policy='all' (the default). Flat field lies:
    # 'stop_at_child_anchors'. That non-default value ALSO drives the unmoored
    # signal, so if the handler read the flat field the echoed
    # live_gui_sync_skipped_reason would flip away from 'raw_dump'. With the
    # blob winning, the raw-dump reason stands and descend_policy stays 'all'.
    # (Bonus: 'stop_at_child_anchors' would trigger a live anchor-tag config
    # read that this fake client cannot serve -- so a flat-field read would
    # ALSO error out. The blob winning keeps the pure raw-dump path.)
    spec = RenderProjectionSpec.normalize_from_glimpse_args(
        root_uuid=_ROOT, descend_policy="all",
    )
    frame = {
        "action": "render_slice_modal",
        "request_id": "req-e6-descend",
        "endpoint_id": "tab-1",
        "render_spec": spec.to_render_spec_blob(),
        # adversarial contradictory flat field:
        "descend_policy": "stop_at_child_anchors",
    }
    reply = _run_handler(frame, monkeypatch)
    assert reply.get("success") is True, reply
    scope = reply.get("render_scope") or {}
    assert scope.get("descend_policy") == "all", reply
    assert scope.get("live_gui_sync_supported") is False, reply
    assert scope.get("live_gui_sync_skipped_reason") == "raw_dump", reply
