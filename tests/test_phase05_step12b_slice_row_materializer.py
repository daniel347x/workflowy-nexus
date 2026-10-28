"""Phase 0.5 Step 12b (ap:S12BREMAIN) — sub-commit 1 parity lock.

Locks that ``_render_slice_row_view`` — now MATERIALIZER-DRIVEN (routes through
``materialize_from_document_forest`` and feeds the composer ``render_forest`` +
``render_policy`` instead of the loose ``focus_path`` / ``focus_leaf`` /
``focus_index_path`` / ``body_suppressed_ids`` side channels) — is byte-for-byte
behavior-preserving on its own surface, AND still occurrence-exact on a
timestamp-sorted straddled-ancestor tree (the symptom-#5 guarantee this whole
phase exists to protect).

The existing golden-baseline locks (test_phase05_golden_baseline.py resolver #3
+ the straddle fixture) already ride along; these are the EXPLICIT
Step-12b-producer locks so the migration is pinned, not merely implied.
"""

from workflowy_mcp.client.api_client_nexus import WorkFlowyClientNexus as W


# ---------------------------------------------------------------------------
# Straddled-ancestor timestamp-sort tree (mirrors the golden-baseline fixture).
# createdAt A1=100, B1=500, A2=900 -> leaf sort A1, B1, A2 straddles Section A,
# so the trie-merge re-emits Section A as occurrence 0 (owning A1) and
# occurrence 1 (owning A2).
# ---------------------------------------------------------------------------
_ROOT = "22222222-0000-0000-0000-000000000001"
_A = "22222222-0000-0000-0000-000000000002"
_A1 = "22222222-0000-0000-0000-000000000003"
_A2 = "22222222-0000-0000-0000-000000000004"
_B = "22222222-0000-0000-0000-000000000005"
_B1 = "22222222-0000-0000-0000-000000000006"

_CACHE = {
    _ROOT: {"id": _ROOT, "name": "Root", "note": "", "parent_id": None, "priority": 0, "createdAt": 100, "modifiedAt": 100},
    _A: {"id": _A, "name": "Section A", "note": "body A", "parent_id": _ROOT, "priority": 0, "createdAt": 100, "modifiedAt": 100},
    _A1: {"id": _A1, "name": "Point A1", "note": "alpha one", "parent_id": _A, "priority": 0, "createdAt": 100, "modifiedAt": 100},
    _A2: {"id": _A2, "name": "Point A2", "note": "alpha two", "parent_id": _A, "priority": 1, "createdAt": 900, "modifiedAt": 900},
    _B: {"id": _B, "name": "Section B", "note": "body B", "parent_id": _ROOT, "priority": 1, "createdAt": 100, "modifiedAt": 100},
    _B1: {"id": _B1, "name": "Point B1", "note": "beta one", "parent_id": _B, "priority": 0, "createdAt": 500, "modifiedAt": 500},
}


def _node(uuid, name, note, children=None):
    return {"id": uuid, "name": name, "note": note, "children": children or []}


def _unsorted_tree():
    """The projected slice subtree in natural (document) order."""
    return _node(_ROOT, "Root", "", [
        _node(_A, "Section A", "body A", [
            _node(_A1, "Point A1", "alpha one"),
            _node(_A2, "Point A2", "alpha two"),
        ]),
        _node(_B, "Section B", "body B", [
            _node(_B1, "Point B1", "beta one"),
        ]),
    ])


def _sorted_tree():
    """The SAME subtree reordered by created/asc -> Section A straddled."""
    reordered = W._reorder_tree_by_timestamp(
        [_unsorted_tree()], sort_by="created", direction="asc", cache_by_id=_CACHE
    )
    assert reordered, "reorder should produce a forest"
    return reordered[0]


# ---------------------------------------------------------------------------
# 1. Whole-document parity: a root-row focus renders the WHOLE tree, identical
#    to a focus=None composer render (degrade-to-whole-document, unchanged).
# ---------------------------------------------------------------------------
def test_slice_row_root_focus_renders_whole_document():
    tree = _unsorted_tree()
    md = W._render_slice_row_view(tree, uuid=_ROOT, ancestor_path=[], occurrence=0)
    # Materializer whole-document render == direct composer whole-tree render.
    assert md == W._build_markdown_preview_from_tree([tree], suppress_metadata=True)
    # And it really is the whole thing.
    assert "body A" in md and "alpha one" in md and "beta one" in md


# ---------------------------------------------------------------------------
# 2. Plain singleton focus (unsorted, unique uuid): option-Y breadcrumb +
#    focused subtree, breadcrumb bodies suppressed via policy (not blanking).
# ---------------------------------------------------------------------------
def test_slice_row_focus_a1_option_y():
    tree = _unsorted_tree()
    md = W._render_slice_row_view(tree, uuid=_A1, ancestor_path=[_ROOT, _A], occurrence=0)
    # Root + Section A are breadcrumb (heading-only); A1 renders with its body.
    assert md == "# Root\n\n## Section A\n\n### Point A1\n\nalpha one"


# ---------------------------------------------------------------------------
# 3. OCCURRENCE-EXACT (the symptom-#5 guarantee) on the MIGRATED producer.
#    Under the created sort Section A is re-emitted twice; focusing Section A
#    at occurrence 0 must render the A1 subtree, at occurrence 1 the A2 subtree
#    — NEVER leak to the first occurrence.
# ---------------------------------------------------------------------------
def test_slice_row_straddled_occurrence0_renders_A1():
    tree = _sorted_tree()
    md = W._render_slice_row_view(tree, uuid=_A, ancestor_path=[_ROOT], occurrence=0)
    assert md == "# Root\n\n## Section A\n\nbody A\n\n### Point A1\n\nalpha one"


def test_slice_row_straddled_occurrence1_renders_A2():
    tree = _sorted_tree()
    md = W._render_slice_row_view(tree, uuid=_A, ancestor_path=[_ROOT], occurrence=1)
    assert md == "# Root\n\n## Section A\n\nbody A\n\n### Point A2\n\nalpha two"


# ---------------------------------------------------------------------------
# 4. DEPRECATED body suppression still flows through deprecated_ids on the
#    migrated producer (uuid-keyed): focusing A1 while A1 is deprecated renders
#    its heading but withholds its body.
# ---------------------------------------------------------------------------
def test_slice_row_deprecated_body_suppressed_via_policy():
    tree = _unsorted_tree()
    md = W._render_slice_row_view(
        tree, uuid=_A1, ancestor_path=[_ROOT, _A], occurrence=0,
        body_suppressed_ids={_A1},
    )
    # A1's heading renders; its body 'alpha one' is withheld (DEPBOD via policy).
    assert "### Point A1" in md
    assert "alpha one" not in md
