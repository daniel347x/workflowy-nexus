"""Lock: a FOCUSED GLIMPSE reports node_count/depth for the FOCUSED subtree.

The docked-modal-per-row fix. ``render_stage2_filter_sort`` computes
``node_count`` / ``depth`` over the whole projected scope, but when a row
``focus`` resolves BOTH render modes emit only the option-Y focused view
(markdown threads ``focus_*`` into the composer; full mode prunes the forest).
Previously the stats still described the WHOLE scope, so a per-row GLIMPSE (the
'GLIMPSE what the modal is showing right now' capability) reported the modal's
whole-scope node count instead of the returned row-subtree's count.

The fix recomputes ``node_count`` / ``depth`` from the SAME occurrence-exact
focused forest the renders use (the materializer core
``materialize_from_document_forest`` -- the single authority for 'what the
focused view contains'; historically the now-deleted
``_prune_forest_to_focus_index_path`` helper), so the stats can never disagree
with the rendered content. These tests lock that:

  * no focus            -> whole-scope stats (unchanged behavior);
  * focus on a section  -> stats = breadcrumb + that section's subtree;
  * focus on a leaf     -> stats = breadcrumb + the single leaf;
  * unresolvable focus  -> whole-scope stats (fail-safe, unchanged);
  * markdown & full/UUID mode agree on the focused node_count (one pruned
    forest feeds both).

Reuses the self-contained RAW-DUMP cell harness (root_uuid only -- NO
WebSocket) from test_glimpse_focus_feature2; all local, no network/disk.

The fixture tree (6 cache nodes):

    ROOT
    |-- A            (Section A)
    |   |-- A1       (Point A1, leaf)
    |   `-- A2       (Point A2, leaf)
    `-- B            (Section B)
        `-- B1       (Point B1, leaf)
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


def _run(monkeypatch, *, focus=None, as_markdown=True):
    client = _make_client()
    monkeypatch.setattr(srv, "get_client", lambda: client)
    return asyncio.run(
        srv._glimpse_root_uuid_from_cache(
            root_uuid=_ROOT,
            as_markdown=as_markdown,
            output_file=None,
            sort_by=None,
            sort_direction="asc",
            headings_only=False,
            since=None,
            focus=focus,
        )
    )


def test_no_focus_reports_whole_scope_count(monkeypatch):
    res = _run(monkeypatch, focus=None)
    assert res["success"] is True
    # ROOT + A + A1 + A2 + B + B1 = 6 nodes; depth ROOT->A->A1 = 3.
    assert res["node_count"] == 6
    assert res["depth"] == 3


def test_focus_on_section_counts_breadcrumb_plus_subtree(monkeypatch):
    # Focus Section A -> option-Y = ROOT (breadcrumb, heading-only) + A + A1 +
    # A2 = 4 nodes (NOT the whole-scope 6). Section B / B1 are pruned.
    focus = {"mode": "row", "uuid": _A, "ancestor_path": [_ROOT], "occurrence": 0}
    res = _run(monkeypatch, focus=focus)
    assert res["success"] is True
    assert res["node_count"] == 4
    assert res["depth"] == 3  # ROOT -> A -> A1/A2
    # Sanity: the render matches the count (B is gone).
    assert "Section B" not in res["preview_markdown"]


def test_focus_on_leaf_counts_breadcrumb_plus_leaf(monkeypatch):
    # Focus leaf A2 -> ROOT (breadcrumb) + A (breadcrumb) + A2 = 3 nodes.
    focus = {"mode": "row", "uuid": _A2, "ancestor_path": [_ROOT, _A], "occurrence": 0}
    res = _run(monkeypatch, focus=focus)
    assert res["success"] is True
    assert res["node_count"] == 3
    assert res["depth"] == 3  # ROOT -> A -> A2
    assert "Point A1" not in res["preview_markdown"]


def test_unresolvable_focus_fails_loud_zero_count(monkeypatch):
    # PHASE 0.5 STEP 12b sub-commit 3 (ap:S12BREMAIN) -- EXACT-OR-ERROR FLIP
    # (Dan-signed-off). A REQUESTED but unresolvable canonical row focus now
    # FAILS LOUD (focus_unresolved + empty preview) instead of degrading to the
    # whole document. The stats describe that empty loud reply (node_count 0),
    # never a whole-scope count for a render that did not happen.
    focus = {"mode": "row", "uuid": "no-such-uuid", "ancestor_path": [_ROOT], "occurrence": 0}
    res = _run(monkeypatch, focus=focus)
    assert res["success"] is True
    assert res.get("focus_unresolved") == "no-such-uuid"
    assert res["node_count"] == 0
    assert res["depth"] == 0


def test_markdown_and_full_mode_agree_on_focused_count(monkeypatch):
    # BOTH output modes emit the SAME focused node set (one pruned forest feeds
    # both), so node_count/depth must match across as_markdown True/False.
    focus = {"mode": "row", "uuid": _A, "ancestor_path": [_ROOT], "occurrence": 0}
    md_res = _run(monkeypatch, focus=focus, as_markdown=True)
    full_res = _run(monkeypatch, focus=focus, as_markdown=False)
    assert md_res["node_count"] == full_res["node_count"] == 4
    assert md_res["depth"] == full_res["depth"] == 3
