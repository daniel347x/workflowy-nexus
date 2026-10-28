"""Regression lock for the SCOPED LIGHTNING ROD (ap:LRSCOPE / ap:LRSCOPE2 /
ap:LRHAND1 / ap:LRHAND2).

A Scoped Lightning Rod confines a Lightning Rod (gather / render_tags /
render_search) to the dock's CURRENT frozen artifact instead of walking the
whole /nodes-export cache root. It produces an EPHEMERAL ``kind='dom_capture'``
result (a one-way door: the durable slice/raw origin is discarded).

These tests lock the pieces that carry the real risk, per the ap:LRHAND2 build
checklist:

  (a) ``build_selected_subtree_from_forest`` admits by predicate and returns
      the minimal connective SINGLE-ROOT subtree (+ boundary predicate).
  (b) ``gather_tags_in_forest`` unions the forest's real tags.
  (c) the SINGLE-REAL-ROOT INVARIANT (CORRECTION #1): a single-root input
      yields a single-root-or-empty result and NEVER a synthetic empty-id
      container; the handler RAISES on a synthetically-forced multi-root and on
      an empty-id root.
  (d) the DOM-frozen Scoped handler renders ``kind='dom_capture'`` and is NOT
      laundered to a durable spec (the crack) -- asserted on the render_scope
      echo.
  (e) an empty match renders a valid empty ``dom_capture`` document (no error).
  (f) the OCCURRENCE-EXACT RE-ROOT: under a timestamp sort that re-emits a
      straddled ancestor as multiple occurrences, re-rooting at ONE occurrence
      keeps EXACTLY that occurrence's leaf-subset (not all mirrors reunited),
      via the exact primitive sequence the handler runs.
  (g) ``_find_node_by_uuid_in_forest`` locates the canonical (unsorted) node.
"""
import asyncio
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from workflowy_mcp.f8_slice_policy import (  # noqa: E402
    build_selected_subtree_from_forest,
    gather_tags_in_forest,
    build_tag_predicate,
    render_tags_outline,
)
from workflowy_mcp.glimpse_since_policy import collect_leaf_branches  # noqa: E402
from workflowy_mcp.client.api_client_nexus import (  # noqa: E402
    WorkFlowyClientNexus as W,
)


def _node(nid, name, children=None, note=""):
    return {"id": nid, "name": name, "note": note, "children": children or []}


def _leaf_ids(forest):
    """The set of leaf UUIDs of a forest (via the shared branch walk)."""
    out = set()
    for br in collect_leaf_branches(forest):
        if br:
            out.add(br[-1][0])
    return out


# ---------------------------------------------------------------------------
# (a) build_selected_subtree_from_forest -- admit + minimal connective root.
# ---------------------------------------------------------------------------
def test_forest_selection_admits_by_predicate_single_root():
    forest = [
        _node("root", "Root", [
            _node("a", "Alpha #keep", [
                _node("a1", "child keep #keep"),
                _node("a2", "child drop"),
            ]),
            _node("b", "Beta", [
                _node("b1", "deep keep #keep"),
            ]),
        ]),
    ]
    pred = build_tag_predicate(["keep"], match_scope="name_and_note")
    out = build_selected_subtree_from_forest(forest, pred)
    # Single connective root preserved.
    assert isinstance(out, list) and len(out) == 1
    assert out[0]["id"] == "root"
    kept = _leaf_ids(out)
    # a1 and b1 carry #keep; a2 does not; a carries #keep (a non-leaf member).
    assert "a1" in kept
    assert "b1" in kept
    assert "a2" not in kept


def test_forest_selection_empty_match_returns_empty_forest():
    forest = [_node("root", "Root", [_node("a", "Alpha"), _node("b", "Beta")])]
    pred = build_tag_predicate(["nope"], match_scope="name_and_note")
    out = build_selected_subtree_from_forest(forest, pred)
    assert out == []  # 0 roots -- a valid empty match (never an error).


def test_forest_selection_boundary_predicate_stops_recursion():
    forest = [
        _node("root", "Root", [
            _node("anchor", "Sub anchor #anch", [
                _node("hidden", "should not surface #keep"),
            ]),
        ]),
    ]
    # A boundary at 'anch' means the anchor is a leaf; its subtree is not walked.
    boundary = lambda n: "#anch" in str(n.get("name") or "")
    pred = lambda n: True  # admit everything the walk reaches
    out = build_selected_subtree_from_forest(forest, pred, boundary_predicate=boundary)
    assert len(out) == 1
    leaves = _leaf_ids(out)
    # 'hidden' is behind the boundary -> never surfaces.
    assert "hidden" not in leaves
    assert "anchor" in leaves  # the boundary node itself renders as a leaf


# ---------------------------------------------------------------------------
# (b) gather_tags_in_forest.
# ---------------------------------------------------------------------------
def test_gather_tags_in_forest_unions_real_tags():
    forest = [
        _node("root", "Root #alpha", [
            _node("a", "Alpha #beta", note="body #gamma"),
            _node("b", "Beta"),
        ]),
    ]
    tags = gather_tags_in_forest(forest, match_scope="name_and_note")
    assert "alpha" in tags
    assert "beta" in tags
    assert "gamma" in tags  # note-body tag under name_and_note
    # name-only scope drops the note-body tag.
    tags_name = gather_tags_in_forest(forest, match_scope="name")
    assert "gamma" not in tags_name
    assert "alpha" in tags_name


# ---------------------------------------------------------------------------
# (f) OCCURRENCE-EXACT RE-ROOT: only the clicked occurrence's leaves survive.
#
# Build the exact mirror scenario the timestamp-sort trie-merge produces, then
# run the SAME primitive sequence the handler's re-root pre-step runs and assert
# the re-rooted domain carries EXACTLY the clicked occurrence's leaves.
# ---------------------------------------------------------------------------
def _sorted_mirror_setup():
    """A canonical (unsorted) tree whose sort re-emits ancestor B as 2 mirrors.

    Root
      A
        B
          L1   (created early)
          L2   (created late)
    A 'created' sort interleaves L1 (early) and L2 (late) with an out-of-cluster
    leaf X (created between them) so the trie-merge re-emits A->B twice: once
    carrying L1, once carrying L2.
    """
    canonical = [
        _node("root", "Root", [
            _node("A", "A", [
                _node("B", "B", [
                    _node("L1", "Leaf One #t"),
                    _node("L2", "Leaf Two #t"),
                ]),
            ]),
            _node("X", "X mid #t"),
        ]),
    ]
    cache = {
        "root": {"createdAt": 1, "modifiedAt": 1},
        "A": {"createdAt": 1, "modifiedAt": 1},
        "B": {"createdAt": 1, "modifiedAt": 1},
        "L1": {"createdAt": 10, "modifiedAt": 10},
        "X":  {"createdAt": 20, "modifiedAt": 20},
        "L2": {"createdAt": 30, "modifiedAt": 30},
    }
    return canonical, cache


def test_reroot_keeps_only_clicked_occurrence_leaves_under_sort():
    canonical, cache = _sorted_mirror_setup()

    # (1) sort -> mirrors of A->B appear (one with L1, one with L2, X between).
    sorted_forest = W._reorder_tree_by_timestamp(
        canonical, sort_by="created", direction="asc", cache_by_id=cache
    )
    assert sorted_forest

    # Enumerate B's occurrences in the sorted forest.
    b_rows = [r for r in W._iter_markdown_focus_rows(sorted_forest) if r["uuid"] == "B"]
    assert len(b_rows) == 2, "the sort must re-emit B as two mirror occurrences"

    # For EACH occurrence, run the handler's re-root algorithm and assert the
    # re-rooted domain carries exactly that occurrence's single leaf.
    seen_leaves = []
    for occ_row in b_rows:
        occ = occ_row["occurrence"]
        # (2) resolve occurrence-exact index_path + round-trip.
        ip = W._resolve_focus_index_path(
            sorted_forest, uuid="B", ancestor_path=occ_row["ancestor_path"], occurrence=occ
        )
        assert ip is not None
        assert W._node_uuid_at_index_path(sorted_forest, ip) == "B"
        # (3) isolate that occurrence's subtree via the JOB-B primitive
        # (Phase 0.5 Step 10): clone_subtree_at_index_path returns ONLY N's
        # subtree (no breadcrumb), the exact-occurrence domain.
        occ_subtree = W.clone_subtree_at_index_path(sorted_forest, ip)
        assert isinstance(occ_subtree, dict)
        assert occ_subtree["id"] == "B"  # the landed node, NOT a breadcrumb root
        # (4) collect that occurrence's leaf UUIDs.
        occ_leaves = _leaf_ids([occ_subtree])
        assert len(occ_leaves) == 1, "each B mirror carries exactly one leaf"
        # (5) rebuild the CLEAN canonical subtree restricted to those leaves.
        from workflowy_mcp.server import _find_node_by_uuid_in_forest
        canon_b = _find_node_by_uuid_in_forest(canonical, "B")
        assert canon_b is not None
        leaf_pred = (lambda n, wanted=occ_leaves: str(n.get("id") or n.get("uuid") or "") in wanted)
        rebuilt = build_selected_subtree_from_forest([canon_b], leaf_pred)
        assert len(rebuilt) == 1
        rebuilt_leaves = _leaf_ids(rebuilt)
        # The re-rooted domain carries EXACTLY the clicked occurrence's leaf --
        # NOT both L1 and L2 reunited (that would be the mirror-reuniting bug).
        assert rebuilt_leaves == occ_leaves
        assert "X" not in rebuilt_leaves  # never the out-of-cluster leaf
        seen_leaves.append(tuple(sorted(occ_leaves)))

    # The two occurrences carried DIFFERENT leaves (L1 vs L2), never the same.
    assert set(seen_leaves) == {("L1",), ("L2",)}


# ---------------------------------------------------------------------------
# (g) _find_node_by_uuid_in_forest.
# ---------------------------------------------------------------------------
def test_find_node_by_uuid_in_forest():
    from workflowy_mcp.server import _find_node_by_uuid_in_forest
    forest = [_node("root", "Root", [_node("a", "A", [_node("deep", "Deep")])])]
    assert _find_node_by_uuid_in_forest(forest, "deep")["id"] == "deep"
    assert _find_node_by_uuid_in_forest(forest, "root")["id"] == "root"
    assert _find_node_by_uuid_in_forest(forest, "missing") is None
    assert _find_node_by_uuid_in_forest(forest, "") is None


# ---------------------------------------------------------------------------
# Async handler harness: drive _handle_render_lightning_dom_capture with a fake
# websocket capturing the single reply. Covers (c) invariant raise, (d) the
# crack (dom_capture echo, never a durable spec), (e) empty-match validity.
# ---------------------------------------------------------------------------
class _FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, s):
        import json
        self.sent.append(json.loads(s))


def _ensure_client(srv):
    """Ensure a module-global client exists (no network use in these tests).

    The handler's client methods it exercises (_normalize_modal_visible_tree,
    _reorder_tree_by_timestamp, _resolve_focus_index_path, _build_modal_rows,
    _build_markdown_preview_from_tree, _modal_cache_nodes_by_id) are pure /
    static; a real client constructed with a dummy key never makes a call here
    (we patch the one cache read). Set it once on the module global.
    """
    if getattr(srv, "_client", None) is None:
        from workflowy_mcp.client.api_client_nexus import WorkFlowyClientNexus
        from workflowy_mcp.models.config import APIConfiguration

        srv._client = WorkFlowyClientNexus(APIConfiguration(api_key="test-dummy-key"))
    # S03-B: the WS front wraps the handler in a cache_operation_boundary;
    # install the boundary surface (populated publisher + no-I/O adoption)
    # on the shared client.  Idempotent across tests.
    from coherence_harness import make_boundary_capable

    if not getattr(srv._client, "_s03b_boundary_double", False):
        make_boundary_capable(srv._client)
        srv._client._s03b_boundary_double = True
    return srv._client


def _run_handler(data, monkeypatch, cache=None):
    """Drive the handler once; return the single reply dict."""
    from workflowy_mcp import server as srv

    client = _ensure_client(srv)
    # Only the cache read needs to be deterministic (no /nodes-export I/O).
    monkeypatch.setattr(client, "_modal_cache_nodes_by_id", lambda: (cache or {}))

    ws = _FakeWS()
    asyncio.run(srv._handle_render_lightning_dom_capture(data, ws))
    assert len(ws.sent) == 1, f"expected exactly one reply, got {ws.sent}"
    return ws.sent[0]


def test_handler_dom_capture_echo_not_laundered(monkeypatch):
    """(d) A render_tags Scoped run echoes kind='dom_capture', never a durable
    scope kind -- the crack that must stay closed."""
    captured = {
        "root": {"id": "root", "name": "Root", "note": ""},
        "children": [
            {"id": "a", "name": "Alpha #keep", "note": "", "children": []},
            {"id": "b", "name": "Beta", "note": "", "children": []},
        ],
    }
    data = {
        "request_id": "t1",
        "captured_tree": captured,
        "root_uuid": "root",
        "lightning": {"mode": "render_tags", "tags": ["keep"], "match_scope": "name_and_note"},
        "as_markdown": True,
    }
    reply = _run_handler(data, monkeypatch)
    assert reply["success"] is True
    scope = reply["render_scope"]
    assert scope["kind"] == "dom_capture"
    assert scope["durable_scope"] is False
    assert scope["live_gui_sync_supported"] is False
    # The filtered forest is returned for re-pin (Lightning-within-Lightning).
    assert isinstance(reply.get("filtered_forest"), list)
    # It must NOT masquerade as a durable cache scope kind.
    assert scope["kind"] not in ("raw_root", "slice", "slice_override", "lightning_rod")
    # ★ TOP-HALF TREE-VIEW ROWS must be POPULATED (the disappearing-tree-view
    # bug: _glimpse_dispatch_dom_capture returned no 'rows', so the client
    # painted a blank top-half list while the bottom rendered fine). A
    # single-match filter over 'root'->'a #keep' yields at least the root row.
    rows = reply.get("rows")
    assert isinstance(rows, list) and len(rows) >= 1, (
        "a successful dom_capture render must return a non-empty top-half rows "
        "list (the disappearing-tree-view regression)"
    )
    assert any(r.get("uuid") == "a" for r in rows), "the matched node must appear as a row"


def test_handler_empty_match_is_valid_empty_doc(monkeypatch):
    """(e) A filter that matches nothing yields a valid empty dom_capture
    document, NOT an error."""
    captured = {
        "root": {"id": "root", "name": "Root", "note": ""},
        "children": [{"id": "a", "name": "Alpha", "note": "", "children": []}],
    }
    data = {
        "request_id": "t2",
        "captured_tree": captured,
        "root_uuid": "root",
        "lightning": {"mode": "render_tags", "tags": ["nomatch"], "match_scope": "name_and_note"},
        "as_markdown": True,
    }
    reply = _run_handler(data, monkeypatch)
    assert reply["success"] is True
    assert reply["markdown"] == ""
    assert reply["filtered_forest"] == []
    assert reply["render_scope"]["kind"] == "dom_capture"


def test_handler_render_symbols_rejected(monkeypatch):
    """render_symbols is a cache-resolution mode -- rejected over a frozen DOM
    view."""
    data = {
        "request_id": "t3",
        "captured_tree": {"root": {"id": "root", "name": "Root", "note": ""}, "children": []},
        "root_uuid": "root",
        "lightning": {"mode": "render_symbols", "symbols": ["foo"], "root_uuids": ["root"]},
        "as_markdown": True,
    }
    reply = _run_handler(data, monkeypatch)
    assert reply["success"] is False
    assert "render_symbols" in str(reply.get("error", "")).lower() or "not supported" in str(reply.get("error", "")).lower()


def test_handler_multi_root_raises_invariant(monkeypatch):
    """(c) CORRECTION #1: a (synthetically forced) multi-root filtered forest
    RAISES SliceProjectionInvariantError -- never a soft synthetic-container
    wrap. Real input can't produce this (single-root in -> single-root out),
    so we force it by patching the selection primitive as imported into the
    server module."""
    import pytest
    from workflowy_mcp import server as srv
    from workflowy_mcp.f8_slice_policy import SliceProjectionInvariantError

    _ensure_client(srv)
    client = srv._client
    monkeypatch.setattr(client, "_modal_cache_nodes_by_id", lambda: {})

    def _fake_multi(forest, predicate, **kw):
        return [
            {"id": "r1", "name": "R1", "note": "", "children": []},
            {"id": "r2", "name": "R2", "note": "", "children": []},
        ]
    monkeypatch.setattr(srv, "build_selected_subtree_from_forest", _fake_multi)

    data = {
        "request_id": "tm",
        "captured_tree": {"root": {"id": "root", "name": "Root", "note": ""}, "children": []},
        "root_uuid": "root",
        "lightning": {"mode": "render_tags", "tags": ["x"], "match_scope": "name_and_note"},
        "as_markdown": True,
    }
    ws = _FakeWS()
    # The handler's own try/except catches the raise and replies with the
    # error_type = the exception class name -- so the loud failure surfaces as
    # a structured error rather than a silent soft-wrap.
    asyncio.run(srv._handle_render_lightning_dom_capture(data, ws))
    assert len(ws.sent) == 1
    reply = ws.sent[0]
    assert reply["success"] is False
    assert reply.get("error_type") == "SliceProjectionInvariantError"
    assert "multi-root" in str(reply.get("error", "")).lower()


def test_handler_empty_id_root_raises_invariant(monkeypatch):
    """(c) CORRECTION #1: a single root with an EMPTY UUID (the phantom
    Workflowy 'home') RAISES -- never accepted as a render root."""
    from workflowy_mcp import server as srv

    _ensure_client(srv)
    client = srv._client
    monkeypatch.setattr(client, "_modal_cache_nodes_by_id", lambda: {})

    def _fake_emptyid(forest, predicate, **kw):
        return [{"id": "", "name": "", "note": "", "children": []}]
    monkeypatch.setattr(srv, "build_selected_subtree_from_forest", _fake_emptyid)

    data = {
        "request_id": "te",
        "captured_tree": {"root": {"id": "root", "name": "Root", "note": ""}, "children": []},
        "root_uuid": "root",
        "lightning": {"mode": "render_tags", "tags": ["x"], "match_scope": "name_and_note"},
        "as_markdown": True,
    }
    ws = _FakeWS()
    asyncio.run(srv._handle_render_lightning_dom_capture(data, ws))
    assert len(ws.sent) == 1
    reply = ws.sent[0]
    assert reply["success"] is False
    assert reply.get("error_type") == "SliceProjectionInvariantError"
    assert "empty uuid" in str(reply.get("error", "")).lower()


def test_handler_gather_returns_raw_text_outline(monkeypatch):
    """gather short-circuits to the tag outline (raw_text / root_only)."""
    captured = {
        "root": {"id": "root", "name": "Root #alpha", "note": ""},
        "children": [{"id": "a", "name": "Alpha #beta", "note": "", "children": []}],
    }
    data = {
        "request_id": "t4",
        "captured_tree": captured,
        "root_uuid": "root",
        "lightning": {"mode": "gather", "match_scope": "name_and_note"},
        "as_markdown": True,
    }
    reply = _run_handler(data, monkeypatch)
    assert reply["success"] is True
    assert reply["render_scope"]["content_format"] == "raw_text"
    assert reply["render_scope"]["tree_mode"] == "root_only"
    # gather unions the forest's real tags into a raw_text outline.
    assert "beta" in reply.get("tags", [])
    assert reply.get("tag_count", 0) >= 1
    assert isinstance(reply.get("markdown"), str) and "beta" in reply["markdown"]
    # ★ SYNTHETIC SCOPE-ROOT ROW: a gather emits ONE root row (mirroring the
    # Global gather cell) so the top-half list shows the gathered scope root
    # instead of a confusing BLANK pane. It is a single non-descending row.
    rows = reply.get("rows")
    assert isinstance(rows, list) and len(rows) == 1, (
        "a Scoped gather must emit exactly one synthetic scope-root row (not a "
        "blank tree view)"
    )
    assert rows[0]["uuid"] == "root"
    assert rows[0]["depth"] == 0


# ===========================================================================
# SLICE-BASE BRANCH (ap:LRHAND3 REMAINS 1 + 2) -- session 4.
#
# A SLICE-FROZEN Scoped Lightning Rod carries base_render_spec (no
# captured_tree). The handler calls _project_slice_base_tree_for_scoped_lightning
# to re-project the durable slice into a TREE, then filters that tree EXACTLY
# like a DOM captured_tree. Every reply threads was_slice=True so the client
# paints the 'you collapsed the slice' yellow bar.
#
# We patch _project_slice_base_tree_for_scoped_lightning (its own network/config
# path is exercised separately) to return a deterministic projected tree, and
# assert the handler (a) consumes it, (b) filters it, (c) threads was_slice, and
# (d) still produces an EPHEMERAL dom_capture (never a durable spec) -- the crack
# stays closed for the slice source too.
# ===========================================================================
def test_handler_slice_base_filters_and_flags_was_slice(monkeypatch):
    """(slice-base a+c+d) base_render_spec -> projected tree -> filtered
    dom_capture with was_slice=True and kind='dom_capture' (not laundered)."""
    from workflowy_mcp import server as srv

    _ensure_client(srv)
    client = srv._client
    monkeypatch.setattr(client, "_modal_cache_nodes_by_id", lambda: {})

    # The re-projected base slice tree (as if build_stage1_base_tree ran).
    projected = {
        "id": "sliceRoot", "name": "Slice Root", "note": "", "children": [
            {"id": "m1", "name": "Member one #keep", "note": "", "children": []},
            {"id": "m2", "name": "Member two", "note": "", "children": []},
        ],
    }

    async def _fake_project(*, base_render_spec, endpoint_id):
        # Prove the handler passed the client's durable spec through.
        assert isinstance(base_render_spec, dict)
        return projected, None
    monkeypatch.setattr(srv, "_project_slice_base_tree_for_scoped_lightning", _fake_project)

    data = {
        "request_id": "sb1",
        # NO captured_tree -> the slice-base branch fires.
        "base_render_spec": {"kind": "slice", "group_id": "g1", "root_uuid": None},
        "was_slice": True,
        "root_uuid": "sliceRoot",
        "lightning": {"mode": "render_tags", "tags": ["keep"], "match_scope": "name_and_note"},
        "as_markdown": True,
    }
    ws = _FakeWS()
    asyncio.run(srv._handle_render_lightning_dom_capture(data, ws))
    assert len(ws.sent) == 1
    reply = ws.sent[0]
    assert reply["success"] is True
    # (c) was_slice threaded so the client paints the yellow bar.
    assert reply.get("was_slice") is True
    # (d) still an ephemeral dom_capture -- the crack stays closed for slices.
    assert reply["render_scope"]["kind"] == "dom_capture"
    assert reply["render_scope"]["durable_scope"] is False
    # (b) the filter kept the #keep member and dropped the other.
    leaves = _leaf_ids(reply.get("filtered_forest") or [])
    assert "m1" in leaves
    assert "m2" not in leaves


def test_handler_slice_base_empty_match_still_flags_was_slice(monkeypatch):
    """(slice-base b) a slice-base filter that matches nothing STILL collapsed
    the durable slice -> was_slice=True on the empty-match reply too."""
    from workflowy_mcp import server as srv

    _ensure_client(srv)
    client = srv._client
    monkeypatch.setattr(client, "_modal_cache_nodes_by_id", lambda: {})

    projected = {
        "id": "sliceRoot", "name": "Slice Root", "note": "", "children": [
            {"id": "m1", "name": "Member one", "note": "", "children": []},
        ],
    }

    async def _fake_project(*, base_render_spec, endpoint_id):
        return projected, None
    monkeypatch.setattr(srv, "_project_slice_base_tree_for_scoped_lightning", _fake_project)

    data = {
        "request_id": "sb2",
        "base_render_spec": {"kind": "slice", "group_id": "g1"},
        "was_slice": True,
        "root_uuid": "sliceRoot",
        "lightning": {"mode": "render_tags", "tags": ["nomatch"], "match_scope": "name_and_note"},
        "as_markdown": True,
    }
    ws = _FakeWS()
    asyncio.run(srv._handle_render_lightning_dom_capture(data, ws))
    reply = ws.sent[0]
    assert reply["success"] is True
    assert reply["markdown"] == ""
    assert reply["filtered_forest"] == []
    assert reply.get("was_slice") is True  # empty match still flags the collapse
    assert reply["render_scope"]["kind"] == "dom_capture"


def test_handler_slice_base_projection_error_replies_verbatim(monkeypatch):
    """(slice-base error) if the base projection fails (e.g. stale cache), the
    handler replies the helper's structured error verbatim -- no crash, no
    silent wrong render."""
    from workflowy_mcp import server as srv

    _ensure_client(srv)
    client = srv._client
    monkeypatch.setattr(client, "_modal_cache_nodes_by_id", lambda: {})

    async def _fake_project(*, base_render_spec, endpoint_id):
        return None, {
            "success": False,
            "error": "the frozen slice's root is no longer in the nodes cache",
            "error_type": "scoped_lightning_focus_stale",
        }
    monkeypatch.setattr(srv, "_project_slice_base_tree_for_scoped_lightning", _fake_project)

    data = {
        "request_id": "sb3",
        "base_render_spec": {"kind": "slice", "group_id": "g1"},
        "was_slice": True,
        "root_uuid": "sliceRoot",
        "lightning": {"mode": "render_tags", "tags": ["keep"], "match_scope": "name_and_note"},
        "as_markdown": True,
    }
    ws = _FakeWS()
    asyncio.run(srv._handle_render_lightning_dom_capture(data, ws))
    reply = ws.sent[0]
    assert reply["success"] is False
    assert reply.get("error_type") == "scoped_lightning_focus_stale"


def test_handler_neither_source_errors(monkeypatch):
    """Neither captured_tree nor base_render_spec -> a loud, actionable error
    (not a crash)."""
    from workflowy_mcp import server as srv
    _ensure_client(srv)
    monkeypatch.setattr(srv._client, "_modal_cache_nodes_by_id", lambda: {})
    data = {
        "request_id": "sb4",
        "root_uuid": "root",
        "lightning": {"mode": "render_tags", "tags": ["keep"], "match_scope": "name_and_note"},
        "as_markdown": True,
    }
    ws = _FakeWS()
    asyncio.run(srv._handle_render_lightning_dom_capture(data, ws))
    reply = ws.sent[0]
    assert reply["success"] is False
    assert "frozen" in str(reply.get("error", "")).lower()


# ---------------------------------------------------------------------------
# render_tags_outline -- the gather serializer. Two decoupled concerns:
# family/nesting uses a NORMALIZED key (hyphen runs collapsed + trailing
# postfix dropped); DISPLAY is always the VERBATIM original.
# ---------------------------------------------------------------------------
def test_outline_displays_tags_verbatim_not_hyphen_collapsed():
    """A double-hyphen tag DISPLAYS verbatim (the old 'collapse for display'
    rule is gone) -- only the nesting KEY collapses hyphens."""
    out = render_tags_outline(["nexus--md-preview"])
    assert "#nexus--md-preview" in out, "the double hyphen must survive in the DISPLAY"


def test_outline_postfix_does_not_add_nesting_level():
    """A recognized trailing postfix (--deprecated) contributes NO nesting
    level: comm-jim--deprecated is the parent, comm-jim-teams nests one deeper,
    both displayed verbatim."""
    out = render_tags_outline(["comm-jim--deprecated", "comm-jim-teams"])
    lines = out.splitlines()
    # Parent line is the verbatim postfixed tag at depth 0.
    assert lines[0] == "#comm-jim--deprecated"
    # Child is exactly one indent (4 spaces) deeper, verbatim.
    assert "    #comm-jim-teams" in lines
    # The postfix word must NOT appear as its own nesting segment/label line.
    assert "#comm-jim-deprecated" not in out  # (single-hyphen collapsed form) never shown


def test_outline_plain_and_postfixed_render_as_siblings():
    out = render_tags_outline(["comm-jim", "comm-jim--deprecated", "comm-jim-teams"])
    assert "#comm-jim" in out
    assert "#comm-jim--deprecated" in out
    # both share key [comm,jim]; the deeper tag nests under them.
    assert "    #comm-jim-teams" in out


def test_outline_collapses_single_child_chain_no_synthetic_parents():
    """Only one deep tag with no siblings -> it roots at the top; no empty
    #comm / #comm-jim synthetic parents are manufactured."""
    out = render_tags_outline(["comm-jim-teams"])
    assert out == "#comm-jim-teams"


def test_outline_synthesizes_branch_label_only_at_real_branch():
    """A real branch point with no literal tag emits a synthetic collapsed-key
    label; a single-child chain above it is collapsed away."""
    out = render_tags_outline(["comm-jim-teams", "comm-rob"])
    lines = out.splitlines()
    assert lines[0] == "#comm"  # the branch (2 children) earns a synthetic header
    assert "    #comm-jim-teams" in lines
    assert "    #comm-rob" in lines


def test_scoped_gather_respects_selected_row_domain(monkeypatch):
    """CRACK FIX (the ORIGINAL use case): a Scoped gather with a selected row
    harvests ONLY that row's subtree tags -- NOT the whole artifact's -- exactly
    like render_tags / render_search narrow to the row domain. Before the fix,
    gather short-circuited over the raw captured_tree and ignored the row focus.
    """
    from workflowy_mcp import server as srv
    _ensure_client(srv)
    monkeypatch.setattr(srv._client, "_modal_cache_nodes_by_id", lambda: {})

    # Root has two subtrees carrying DIFFERENT tags. We click row 'branchA' and
    # gather -- only branchA's tags (#inA) must come back, never branchB's (#inB).
    captured = {
        "root": {"id": "root", "name": "Root #atroot", "note": ""},
        "children": [
            {"id": "branchA", "name": "Branch A", "note": "", "children": [
                {"id": "a1", "name": "a-one #inA", "note": "", "children": []},
            ]},
            {"id": "branchB", "name": "Branch B", "note": "", "children": [
                {"id": "b1", "name": "b-one #inB", "note": "", "children": []},
            ]},
        ],
    }
    data = {
        "request_id": "g-row",
        "captured_tree": captured,
        "root_uuid": "root",
        "lightning": {"mode": "gather", "match_scope": "name_and_note"},
        # Select branchA (occurrence 0, ancestor path = [root]).
        "focus": {"mode": "row", "uuid": "branchA", "ancestor_path": ["root"], "occurrence": 0},
        "as_markdown": True,
    }
    reply = _run_handler(data, monkeypatch)
    assert reply["success"] is True
    tags = reply.get("tags", [])
    assert "inA" in tags, "the selected row's subtree tag must be gathered"
    assert "inB" not in tags, "a sibling subtree's tag must NOT leak into a row-scoped gather"
    assert "atroot" not in tags, "the (re-rooted-away) root's tag must NOT appear"
    assert reply["render_scope"]["content_format"] == "raw_text"


def test_scoped_gather_whole_artifact_when_no_row(monkeypatch):
    """With NO row selected, a Scoped gather harvests the whole current artifact
    (byte-for-byte the prior whole-artifact behavior)."""
    from workflowy_mcp import server as srv
    _ensure_client(srv)
    monkeypatch.setattr(srv._client, "_modal_cache_nodes_by_id", lambda: {})
    captured = {
        "root": {"id": "root", "name": "Root #atroot", "note": ""},
        "children": [
            {"id": "branchA", "name": "Branch A #inA", "note": "", "children": []},
            {"id": "branchB", "name": "Branch B #inB", "note": "", "children": []},
        ],
    }
    data = {
        "request_id": "g-all",
        "captured_tree": captured,
        "root_uuid": "root",
        "lightning": {"mode": "gather", "match_scope": "name_and_note"},
        "as_markdown": True,
    }
    reply = _run_handler(data, monkeypatch)
    assert reply["success"] is True
    tags = reply.get("tags", [])
    # No row -> whole artifact: all three tags present.
    assert "atroot" in tags and "inA" in tags and "inB" in tags


def test_slice_base_helper_rejects_lightning_base(monkeypatch):
    """_project_slice_base_tree_for_scoped_lightning rejects a lightning_rod base
    (a prior Lightning result is ephemeral; it must arrive as captured_tree)."""
    from workflowy_mcp import server as srv
    _ensure_client(srv)
    tree, err = asyncio.run(
        srv._project_slice_base_tree_for_scoped_lightning(
            base_render_spec={"kind": "lightning_rod", "lightning": {"mode": "render_tags", "tags": ["x"]}, "root_uuid": "r"},
            endpoint_id=None,
        )
    )
    assert tree is None
    assert err is not None and err["success"] is False
    assert err.get("error_type") == "scoped_lightning_bad_base_spec"


# ===========================================================================
# (h) JOB A / JOB B SPLIT (Phase 0.5 Step 10, ap:EXECLDG section 6).
#
# clone_subtree_at_index_path is the JOB-B (domain re-root) primitive: it
# returns ONLY the landed occurrence's subtree -- notes intact, NO breadcrumb
# ancestors -- as opposed to the sibling _prune_forest_to_focus_index_path
# (JOB A: the render-focus projection that KEEPS the modal-root breadcrumb
# heading-only above N). These locks pin the separation: the two primitives
# must produce DIFFERENT shapes for the SAME index_path.
# ===========================================================================
def _breadcrumb_setup():
    """Root -> A -> B(note) -> {L1(note), L2}. Focus index_path lands on B."""
    forest = [
        _node("root", "Root", [
            _node("A", "A", [
                _node("B", "B", [
                    _node("L1", "Leaf One", note="leaf-one-body"),
                    _node("L2", "Leaf Two"),
                ], note="b-body-should-survive"),
            ]),
        ]),
    ]
    # index_path to B: root(0) -> A(child 0) -> B(child 0).
    ip = [0, 0, 0]
    assert W._node_uuid_at_index_path(forest, ip) == "B"
    return forest, ip


def test_clone_subtree_returns_only_occurrence_subtree_no_breadcrumb():
    """(h) JOB B: clone_subtree_at_index_path returns ONLY N's subtree -- the
    landed node is N itself (not a breadcrumb root), and NO ancestor of N
    (root / A) appears anywhere in the result."""
    forest, ip = _breadcrumb_setup()
    out = W.clone_subtree_at_index_path(forest, ip)
    assert isinstance(out, dict)
    # The RESULT ROOT is N (=B), never a breadcrumb ancestor.
    assert out["id"] == "B"
    # No breadcrumb ancestor uuid appears anywhere in the returned subtree.
    seen = set()
    _stack = [out]
    while _stack:
        n = _stack.pop()
        seen.add(n.get("id"))
        _stack.extend(c for c in (n.get("children") or []) if isinstance(c, dict))
    assert "root" not in seen and "A" not in seen, "NO breadcrumb ancestors"
    # N's full subtree is present (both leaves).
    assert "L1" in seen and "L2" in seen


def test_clone_subtree_preserves_all_notes_byte_for_byte():
    """(h) JOB B: every retained note is preserved intact -- never blanked (the
    contrast with Job A, which drops breadcrumb notes)."""
    forest, ip = _breadcrumb_setup()
    out = W.clone_subtree_at_index_path(forest, ip)
    # N's own note survives.
    assert out["note"] == "b-body-should-survive"
    # A descendant's note survives.
    l1 = next(c for c in out["children"] if c["id"] == "L1")
    assert l1["note"] == "leaf-one-body"


def test_clone_subtree_is_pure_no_input_mutation():
    """(h) JOB B: the input forest is never mutated (shadow dicts / fresh child
    lists)."""
    forest, ip = _breadcrumb_setup()
    import copy
    before = copy.deepcopy(forest)
    out = W.clone_subtree_at_index_path(forest, ip)
    # Mutating the clone must not touch the input.
    out["note"] = "MUTATED"
    out["children"].append(_node("injected", "X"))
    assert forest == before, "clone_subtree_at_index_path must not mutate its input"


def test_clone_subtree_none_on_bad_path():
    """(h) JOB B: a None/empty/out-of-range index_path returns None (the caller
    guards with isinstance(dict)) -- NOT the whole-document fail-safe Job A
    uses."""
    forest, _ = _breadcrumb_setup()
    assert W.clone_subtree_at_index_path(forest, None) is None
    assert W.clone_subtree_at_index_path(forest, []) is None
    assert W.clone_subtree_at_index_path(forest, [9]) is None          # top OOR
    assert W.clone_subtree_at_index_path(forest, [0, 0, 9]) is None    # child OOR
    assert W.clone_subtree_at_index_path(None, [0]) is None


def test_jobA_vs_jobB_deliberate_contrast_same_index_path():
    """(h) THE SPLIT: for the SAME index_path, JOB A (the composer's render-focus
    option-Y pruning) keeps the root->N breadcrumb (heading-only ancestors)
    while JOB B (clone_subtree_at_index_path) returns ONLY N's subtree with
    notes intact. This is the whole point of the split -- the two jobs are
    genuinely separated, not sharing one helper.

    NOTE (Phase 0.5 Step 12b sub-commit 3b): Job A is the RENDER-FOCUS
    option-Y projection. The standalone full/UUID Job-A helper
    _prune_forest_to_focus_index_path was deleted in Step 12a, and the
    composer's OWN in-walk focus pruning (focus_path/focus_leaf/
    focus_index_path) was deleted in 3b -- Job A now lives ONLY in the ONE
    shared materializer (materialize_from_document_forest ->
    _focused_render_forest_with_origin), which produces the note-preserving
    option-Y render_forest the (now focus-agnostic) composer renders. So this
    contrast asserts the materializer render-focus surface (Job A) vs the
    clone_subtree_at_index_path primitive (Job B)."""
    forest, ip = _breadcrumb_setup()

    # JOB A -- the render-focus projection via the materializer: the breadcrumb
    # ancestors (root, A) render HEADING-ONLY (no body) and N (=B) + its subtree
    # render fully. We assert the option-Y SHAPE via the rendered Markdown.
    # (ip = [0,0,0] == uuid B, ancestor_path [root, A], occurrence 0.)
    from workflowy_mcp.client.api_client_nexus import WorkFlowyClientNexus
    _mrd = WorkFlowyClientNexus.materialize_from_document_forest(
        forest,
        focus={
            "mode": "row",
            "uuid": "B",
            "ancestor_path": ["root", "A"],
            "occurrence": 0,
        },
    )
    md = WorkFlowyClientNexus._build_markdown_preview_from_tree(
        _mrd.render_forest,
        suppress_metadata=False,
        render_policy=_mrd.policy_by_occurrence,
    )
    # The focused leaf's body renders...
    assert "b-body-should-survive" in md
    # ...while the breadcrumb ancestors are heading-only (their names appear as
    # headings, but the breadcrumb chain does NOT emit an off-path sibling).
    # (Root/A carry no bodies in this fixture; the key Job-A property is that
    # the breadcrumb is present + N renders whole -- proven by the body above
    # and the headings below.)
    assert "# Root" in md or "Root" in md

    # JOB B -- domain re-root: result root IS N, breadcrumb absent, notes intact.
    cloned = W.clone_subtree_at_index_path(forest, ip)
    assert cloned["id"] == "B"                    # NO breadcrumb -- N is the root
    assert cloned["note"] == "b-body-should-survive"
    # Job B carries NONE of the breadcrumb ancestors (the genuine separation).
    _seen = set()
    _stack = [cloned]
    while _stack:
        _n = _stack.pop()
        _seen.add(_n.get("id"))
        _stack.extend(c for c in (_n.get("children") or []) if isinstance(c, dict))
    assert "root" not in _seen and "A" not in _seen


# ===========================================================================
# POST-SOL-AUDIT Findings 3+4: the Scoped base projector must thread the
# spec's Stage-1 policies (exclude_completed + the anchor boundary), and the
# handler must validate postfix_policy BEFORE any source-fork projection.
# ===========================================================================
def test_slice_base_helper_threads_exclude_completed(monkeypatch):
    """Finding 3: a frozen raw dump's Scoped re-projection must not resurface
    completed nodes the original render excluded."""
    from workflowy_mcp import server as srv
    from workflowy_mcp.cache_render_spec import RenderProjectionSpec

    client = _ensure_client(srv)
    nodes = {
        "R": {"id": "R", "name": "Root", "note": "", "parent_id": None, "priority": 0},
        "D": {"id": "D", "name": "Done", "note": "", "parent_id": "R", "priority": 0, "completed": True},
        "G": {"id": "G", "name": "Grand", "note": "", "parent_id": "D", "priority": 0},
        "L": {"id": "L", "name": "Live", "note": "", "parent_id": "R", "priority": 1},
    }
    monkeypatch.setattr(
        client, "_get_nodes_export_cache_nodes",
        lambda: [dict(n) for n in nodes.values()],
    )
    spec = RenderProjectionSpec.normalize_from_glimpse_args(root_uuid="R")
    assert spec.exclude_completed is True  # the server-side default
    tree, err = asyncio.run(srv._project_slice_base_tree_for_scoped_lightning(
        base_render_spec=spec.to_render_spec_blob(), endpoint_id=None))
    assert err is None, err
    assert set(_leaf_ids([tree])) == {"L"}  # completed D (and its child G) excluded


def test_slice_base_helper_threads_child_anchor_boundary(monkeypatch):
    """Finding 3: descend_policy='stop_at_child_anchors' must reach the base
    projection -- a child anchor hydrates as a leaf signpost, never descended."""
    from workflowy_mcp import server as srv
    from workflowy_mcp.cache_render_spec import RenderProjectionSpec

    client = _ensure_client(srv)
    nodes = {
        "R": {"id": "R", "name": "Root", "note": "", "parent_id": None, "priority": 0},
        "A": {"id": "A", "name": "Sub anchor #wfy-ns", "note": "", "parent_id": "R", "priority": 0},
        "K": {"id": "K", "name": "Kid under anchor", "note": "", "parent_id": "A", "priority": 0},
        "L": {"id": "L", "name": "Live", "note": "", "parent_id": "R", "priority": 1},
    }
    monkeypatch.setattr(
        client, "_get_nodes_export_cache_nodes",
        lambda: [dict(n) for n in nodes.values()],
    )

    async def _fake_cfg(_tab):
        return (["wfy-ns"], [], None, {})
    monkeypatch.setattr(srv, "_resolve_note_session_config", _fake_cfg)

    spec = RenderProjectionSpec.normalize_from_glimpse_args(
        root_uuid="R", descend_policy="stop_at_child_anchors")
    tree, err = asyncio.run(srv._project_slice_base_tree_for_scoped_lightning(
        base_render_spec=spec.to_render_spec_blob(), endpoint_id=None))
    assert err is None, err
    leaves = set(_leaf_ids([tree]))
    assert "A" in leaves      # the boundary node itself renders as a leaf
    assert "K" not in leaves  # its subtree is NOT descended into
    assert "L" in leaves


def test_handler_slice_policy_rejected_before_projection(monkeypatch):
    """Finding 4: Scoped + postfix_policy='slice' must be rejected BEFORE any
    source-fork projection runs (never masked, never wasted work)."""
    from workflowy_mcp import server as srv

    _ensure_client(srv)

    async def _no_project(*, base_render_spec, endpoint_id):
        raise AssertionError("projection must NOT run for a rejected policy")
    monkeypatch.setattr(srv, "_project_slice_base_tree_for_scoped_lightning", _no_project)

    ws = _FakeWS()
    asyncio.run(srv._handle_render_lightning_dom_capture({
        "request_id": "pp-first",
        "base_render_spec": {"kind": "slice", "group_id": "g1"},
        "was_slice": True,
        "lightning": {"mode": "render_tags", "tags": ["foo"], "postfix_policy": "slice"},
    }, ws))
    reply = ws.sent[-1]
    assert reply["success"] is False
    assert "Turn Scoped off" in (reply.get("error") or "")
