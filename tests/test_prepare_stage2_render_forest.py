"""Direct unit test for the Tier-1 shared Stage-2 forest-prep helper.

`prepare_stage2_render_forest` (ap:NXST2R1) is the ONE de-duplicated tail
that render_stage2_filter_sort (the GLIMPSE tail), _handle_render_slice_modal
(the Apply-slice modal), and _glimpse_dispatch_dom_capture (the frozen-DOM
render_spec="modal" sibling) all call to obtain the identical final
projected+sorted+since-pruned forest + node_count/depth + since echo.

These tests exercise the helper's OWN control flow directly (no WebSocket, no
config read): the no-since passthrough, item-count prune, empty-window
detection, and -- crucially -- that a `since` error is SURFACED (6th tuple
element) and NEVER swallowed. Sort is covered by the broader slice/GLIMPSE
suites (it needs cache timestamps + a live client); here we pin the pure
sequencing the extraction is responsible for.
"""

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from workflowy_mcp.server import prepare_stage2_render_forest  # noqa: E402
from workflowy_mcp.glimpse_since_policy import SinceSpec  # noqa: E402


def _leaf(uid: str) -> dict:
    return {"id": uid, "name": uid, "note": "", "children": []}


def _tree_with_n_leaves(n: int) -> dict:
    """A single root with N leaf children (N+1 nodes total, depth 2)."""
    return {
        "id": "root",
        "name": "root",
        "note": "",
        "children": [_leaf(f"c{i}") for i in range(n)],
    }


def test_no_since_passthrough_reports_counts():
    """No `since`, no sort: the forest is [final_subtree] unchanged, and the
    helper reports the whole-tree node_count / depth."""
    tree = _tree_with_n_leaves(3)  # root + 3 leaves = 4 nodes, depth 2
    forest, since_applied, empty_window, node_count, depth, since_err = (
        prepare_stage2_render_forest(
            final_subtree=tree,
            cache_by_id={},
            sort_by=None,
            sort_direction="asc",
            since_spec=None,
        )
    )
    assert since_err is None
    assert since_applied is None
    assert empty_window is False
    assert forest == [tree]
    assert node_count == 4
    assert depth == 2


def test_single_leaf_depth_one():
    """A lone root (no children) is depth 1, node_count 1."""
    tree = _leaf("solo")
    forest, _sa, empty_window, node_count, depth, since_err = (
        prepare_stage2_render_forest(
            final_subtree=tree,
            cache_by_id={},
            sort_by=None,
            sort_direction="asc",
            since_spec=None,
        )
    )
    assert since_err is None
    assert empty_window is False
    assert forest == [tree]
    assert node_count == 1
    assert depth == 1


def test_item_count_prune_keeps_last_n_leaves():
    """Form-C item-count ('last N') is sort-orthogonal and needs no cache
    timestamps; it trims the forest to the last N leaves of document order."""
    tree = _tree_with_n_leaves(5)  # 5 leaves
    forest, since_applied, empty_window, node_count, depth, since_err = (
        prepare_stage2_render_forest(
            final_subtree=tree,
            cache_by_id={},
            sort_by=None,
            sort_direction="asc",
            since_spec=SinceSpec(form="item_count", count=2),
        )
    )
    assert since_err is None
    assert empty_window is False
    assert since_applied is not None
    assert since_applied.get("form") == "item_count"
    # The prune kept only the last 2 leaves under the root -> root + 2 = 3.
    assert node_count == 3
    # Leaves retained are the LAST two in document order.
    kept = {c["id"] for c in forest[0]["children"]}
    assert kept == {"c3", "c4"}


def test_item_count_larger_than_leaves_is_noop():
    """N >= leaf count returns everything unchanged (a valid no-op), still
    reporting since_applied honestly."""
    tree = _tree_with_n_leaves(2)
    forest, since_applied, empty_window, node_count, depth, since_err = (
        prepare_stage2_render_forest(
            final_subtree=tree,
            cache_by_id={},
            sort_by=None,
            sort_direction="asc",
            since_spec=SinceSpec(form="item_count", count=99),
        )
    )
    assert since_err is None
    assert empty_window is False
    assert node_count == 3  # root + 2 leaves, unchanged
    assert since_applied is not None


def test_curated_without_pivot_and_no_slicetags_surfaces_error_not_swallowed():
    """A `curated` since needs a slice group to define 'curated'. When the
    helper is handed a curated SinceSpec with NO curated_pivot_ts (the
    non-slice callers pass None), apply_since_to_forest treats it as
    'nothing curated yet' -> whole forest, no error. This pins that a curated
    request does NOT raise here and does NOT silently drop content -- the
    error handling for the misuse case lives in the slice/raw-dump callers
    upstream, not in this pure helper."""
    tree = _tree_with_n_leaves(2)
    forest, since_applied, empty_window, node_count, depth, since_err = (
        prepare_stage2_render_forest(
            final_subtree=tree,
            cache_by_id={},
            sort_by=None,
            sort_direction="asc",
            since_spec=SinceSpec(form="curated"),
            curated_pivot_ts=None,
        )
    )
    # No pivot => nothing curated yet => whole forest returned (no prune),
    # surfaced honestly, never an empty window, never a swallowed drop.
    assert since_err is None
    assert empty_window is False
    assert node_count == 3
    assert forest == [tree]


def test_since_error_is_surfaced_as_sixth_element_not_swallowed(monkeypatch):
    """CONTRACT: a semantic `since` error from apply_since_to_forest is
    RETURNED as the 6th tuple element (since_err) with an empty forest + 0/0
    counts -- NEVER swallowed. Each caller shapes its own transport error from
    this. We force the error by monkeypatching the orchestrator the helper
    calls."""
    import workflowy_mcp.server as server_mod

    def _fake_apply(nodes, **kwargs):
        return (None, None, "forced since error")

    monkeypatch.setattr(server_mod, "_apply_since_to_forest", _fake_apply)

    tree = _tree_with_n_leaves(3)
    forest, since_applied, empty_window, node_count, depth, since_err = (
        prepare_stage2_render_forest(
            final_subtree=tree,
            cache_by_id={},
            sort_by=None,
            sort_direction="asc",
            since_spec=SinceSpec(form="item_count", count=1),
        )
    )
    assert since_err == "forced since error"
    assert forest == []
    assert since_applied is None
    assert empty_window is False
    assert node_count == 0
    assert depth == 0


def test_empty_window_detection(monkeypatch):
    """When apply_since_to_forest legitimately prunes to nothing (a real
    since_applied echo but an empty forest), the helper reports
    empty_window=True with 0/0 counts -- the valid 'nothing in the window'
    answer each caller renders as an explicit empty payload."""
    import workflowy_mcp.server as server_mod

    def _fake_apply(nodes, **kwargs):
        return ([], {"form": "window", "pivot_ts": 123}, None)

    monkeypatch.setattr(server_mod, "_apply_since_to_forest", _fake_apply)

    tree = _tree_with_n_leaves(3)
    forest, since_applied, empty_window, node_count, depth, since_err = (
        prepare_stage2_render_forest(
            final_subtree=tree,
            cache_by_id={},
            sort_by=None,
            sort_direction="asc",
            since_spec=SinceSpec(form="window", window_seconds=60),
        )
    )
    assert since_err is None
    assert empty_window is True
    assert forest == []
    assert since_applied == {"form": "window", "pivot_ts": 123}
    assert node_count == 0
    assert depth == 0
