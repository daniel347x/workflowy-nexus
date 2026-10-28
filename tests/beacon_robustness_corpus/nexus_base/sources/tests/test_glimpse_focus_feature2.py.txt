"""Feature 2 lock: GLIMPSE a FOCUSED row's option-Y view ('what the modal shows').

``RenderProjectionSpec.focus`` (a ``{mode:'row', uuid, ancestor_path,
occurrence}`` dict) is threaded through the content-return GLIMPSE cache paths
(``_glimpse_root_uuid_from_cache`` / ``_glimpse_slice_group_from_cache``) into
the shared render tail ``render_stage2_filter_sort``. When present, the tail
resolves the triple occurrence-exactly against the FINAL (sorted + since-pruned)
forest and renders that ONE row's option-Y view -- the modal-root->row
breadcrumb (heading-only) followed by the row's full subtree -- instead of the
whole document. This is byte-identical to what the dock's per-row view shows,
so an agent can GLIMPSE exactly what the modal is currently displaying.

These tests drive the self-contained RAW-DUMP cell (``root_uuid`` only -- NO
WebSocket) with a minimal client + in-memory cache and assert:
  * no focus -> the whole subtree renders;
  * a focus on an interior row -> that row's subtree renders and its
    NON-descendant siblings are pruned (option-Y);
  * the focused GLIMPSE output equals _render_slice_row_view on the same tree
    (the dock's per-row view) -- the 'what the modal shows' identity;
  * an unresolvable focus degrades safely to the whole document.

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


_ROOT = "aaaaaaaa-0000-0000-0000-000000000001"
_A = "aaaaaaaa-0000-0000-0000-000000000002"
_A1 = "aaaaaaaa-0000-0000-0000-000000000003"
_A2 = "aaaaaaaa-0000-0000-0000-000000000004"
_B = "aaaaaaaa-0000-0000-0000-000000000005"
_B1 = "aaaaaaaa-0000-0000-0000-000000000006"

# ROOT
# ├── A
# │   ├── A1   (leaf)
# │   └── A2   (leaf)
# └── B
#     └── B1   (leaf)
_CACHE_NODES = [
    {"id": _ROOT, "name": "Anchor Root", "note": "", "parent_id": None, "priority": 0},
    {"id": _A, "name": "Section A", "note": "body A", "parent_id": _ROOT, "priority": 0},
    {"id": _A1, "name": "Point A1", "note": "alpha one", "parent_id": _A, "priority": 0},
    {"id": _A2, "name": "Point A2", "note": "alpha two", "parent_id": _A, "priority": 1},
    {"id": _B, "name": "Section B", "note": "body B", "parent_id": _ROOT, "priority": 1},
    {"id": _B1, "name": "Point B1", "note": "beta one", "parent_id": _B, "priority": 0},
]


def _make_client():
    c = WorkFlowyClientNexus.__new__(WorkFlowyClientNexus)
    c._get_nodes_export_cache_nodes = lambda: [dict(n) for n in _CACHE_NODES]
    return c


def _run_root_glimpse(monkeypatch, *, focus=None):
    client = _make_client()
    monkeypatch.setattr(srv, "get_client", lambda: client)
    return asyncio.run(
        srv._glimpse_root_uuid_from_cache(
            root_uuid=_ROOT,
            as_markdown=True,
            output_file=None,
            sort_by=None,
            sort_direction="asc",
            headings_only=False,
            since=None,
            focus=focus,
        )
    )


def test_no_focus_renders_whole_subtree(monkeypatch):
    res = _run_root_glimpse(monkeypatch, focus=None)
    assert res["success"] is True
    md = res["preview_markdown"]
    # Every section + point is present in the whole-document render.
    for token in ("Section A", "Point A1", "Point A2", "Section B", "Point B1"):
        assert token in md


def test_focus_on_interior_row_prunes_non_descendant_siblings(monkeypatch):
    # Focus Section A: option-Y view = ROOT breadcrumb (heading only) + A's full
    # subtree (A1, A2). Section B (a sibling of A, NOT under it) is pruned.
    focus = {"mode": "row", "uuid": _A, "ancestor_path": [_ROOT], "occurrence": 0}
    res = _run_root_glimpse(monkeypatch, focus=focus)
    assert res["success"] is True
    md = res["preview_markdown"]
    assert "Section A" in md
    assert "Point A1" in md
    assert "Point A2" in md
    # The breadcrumb shows the root heading, but B's subtree must be gone.
    assert "Section B" not in md
    assert "Point B1" not in md
    # A's own body is present (full subtree render below the focus leaf).
    assert "alpha one" in md


def test_focused_glimpse_equals_dock_per_row_view(monkeypatch):
    # The 'GLIMPSE what the modal shows' identity: the focused GLIMPSE markdown
    # must equal what the dock's per-row renderer (_render_slice_row_view)
    # produces for the SAME row on the SAME projected tree.
    client = _make_client()
    monkeypatch.setattr(srv, "get_client", lambda: client)

    # Build the SAME raw-dump projected tree the GLIMPSE path renders.
    from workflowy_mcp.f8_slice_policy import build_slice_subtree_from_cache
    cache_by_id = {n["id"]: dict(n) for n in _CACHE_NODES}
    subtree = build_slice_subtree_from_cache(cache_by_id, _ROOT, [], member_all=True)
    assert subtree is not None

    focus = {"mode": "row", "uuid": _A, "ancestor_path": [_ROOT], "occurrence": 0}
    res = _run_root_glimpse(monkeypatch, focus=focus)
    dock_view = WorkFlowyClientNexus._render_slice_row_view(
        subtree, _A, [_ROOT], occurrence=0, root_in_non_md_file=False,
    )
    assert res["preview_markdown"] == dock_view


def test_focus_on_leaf_row(monkeypatch):
    # Focus a LEAF (A2): breadcrumb ROOT + A (heading-only), then A2 itself.
    # Its sibling A1 and the whole B subtree are pruned.
    focus = {"mode": "row", "uuid": _A2, "ancestor_path": [_ROOT, _A], "occurrence": 0}
    res = _run_root_glimpse(monkeypatch, focus=focus)
    md = res["preview_markdown"]
    assert "Point A2" in md
    assert "alpha two" in md
    assert "Point A1" not in md
    assert "alpha one" not in md
    assert "Section B" not in md


def test_unresolvable_focus_fails_loud(monkeypatch):
    # PHASE 0.5 STEP 12b sub-commit 3 (ap:S12BREMAIN) -- EXACT-OR-ERROR FLIP
    # (Dan-signed-off). A REQUESTED canonical row focus whose uuid is not in the
    # tree can NOT resolve occurrence-exactly. Pre-flip this lenient-degraded to
    # the whole document (showing the agent content it did NOT intend,
    # invisibly); post-flip it FAILS LOUD: focus_unresolved + empty preview,
    # NEVER a silent whole-document render.
    focus = {"mode": "row", "uuid": "no-such-uuid", "ancestor_path": [_ROOT], "occurrence": 0}
    res = _run_root_glimpse(monkeypatch, focus=focus)
    assert res["success"] is True
    assert res.get("focus_unresolved") == "no-such-uuid"
    assert res.get("preview_markdown", "") == ""
    # The whole document is NOT rendered.
    md = res.get("preview_markdown", "")
    for token in ("Section A", "Point A1", "Section B", "Point B1"):
        assert token not in md


def test_shaped_stale_focus_markdown_is_loud_not_empty(monkeypatch):
    # [ap:XDVDRG] The AGENT-facing shape: the same stale payload through the
    # ONE family finalizer must surface the loud structured error, never the
    # silent '' string (Markdown mode).
    focus = {"mode": "row", "uuid": "no-such-uuid", "ancestor_path": [_ROOT], "occurrence": 0}
    res = _run_root_glimpse(monkeypatch, focus=focus)
    shaped = srv._shape_glimpse_family_return(res, output_file=None)
    assert isinstance(shaped, dict), shaped
    assert shaped.get("success") is False
    assert shaped.get("focus_unresolved") == "no-such-uuid"
    assert "could not be resolved" in str(shaped.get("error"))
    assert "preview_markdown" not in shaped and "rows" not in shaped


def test_non_row_focus_mode_ignored(monkeypatch):
    # A focus with an unknown mode is ignored -> whole document.
    focus = {"mode": "not-a-row", "uuid": _A, "ancestor_path": [_ROOT], "occurrence": 0}
    res = _run_root_glimpse(monkeypatch, focus=focus)
    md = res["preview_markdown"]
    assert "Section B" in md  # not pruned -> whole doc
