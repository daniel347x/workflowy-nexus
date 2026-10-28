"""Regression lock for OCCURRENCE-EXACT focus (symptom #5).

Background
----------
When a docked Markdown modal / Apply-slice view is rendered under a TIMESTAMP
SORT, the trie-merge in ``_reorder_tree_by_timestamp`` re-emits a straddled
ancestor as multiple FRESH node objects that share the same ``uuid`` AND the
same root-first ``ancestor_path``. Clicking the second such occurrence used to
render the FIRST one, because the composer's option-Y focus matched purely by
UUID-SET membership (``_focus_above_leaf`` / ``_focus_leaf``) -- which is
position-blind, so the first matching subtree won.

The fix introduces ONE shared row enumerator
(``WorkFlowyClientNexus._iter_markdown_focus_rows``) that is the single source
of truth for both the public identity triple ``(uuid, ancestor_path,
occurrence)`` AND a private list-rooted ``_index_path`` (the child-index route
from the forest to the node, INCLUDING hops through empty-id sentinel nodes).
``_build_modal_rows`` is a thin wrapper over it (strips the private field), and
the focused composer resolves a clicked triple to its ``_index_path``
(``_resolve_focus_index_path``), round-trip-verifies it
(``_node_uuid_at_index_path``), and prunes POSITIONALLY by that index path
(``focus_index_path``). Because the row list the client sees and the index path
the composer prunes by are produced by the SAME traversal, they cannot drift on
which occurrence a click means.

These tests assert:
  * the enumerator assigns distinct occurrence ordinals + index paths to two
    re-emissions of the same (uuid, ancestor_path);
  * resolve + round-trip land on the clicked node;
  * the composer focuses the EXACT occurrence (occ 1 -> the 2nd subtree, occ 0
    -> the 1st), never leaking to the wrong one;
  * ``focus_index_path=None`` renders the whole tree unchanged;
  * an empty-id SENTINEL above the target is routed through correctly (the case
    a uuid-only ancestor path cannot represent).
"""
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from workflowy_mcp.client.api_client_nexus import (  # noqa: E402
    WorkFlowyClientNexus as W,
)


def _node(nid, name, children=None):
    return {"id": nid, "name": name, "note": "", "children": children or []}


# Phase 0.5 Step 12b sub-commit 3b: the composer is now FOCUS-AGNOSTIC (its
# focus_path/focus_leaf/focus_index_path params + in-walk option-Y pruning were
# deleted). A focused render is produced by the ONE shared materializer
# (materialize_from_document_forest), which hands the composer the already-pruned
# note-preserving render_forest + a render-forest-keyed RenderPolicy. This helper
# reproduces exactly what the production producers (_render_slice_row_view,
# compose_markdown_modal_view, the cache tail) now do, so these occurrence-exact
# tests assert the SAME behavior through the surviving surface. Output is
# byte-identical to the old direct-composer focus call -- that IS the parity
# thesis of the migration.
def _focused_md(forest, uuid, ancestor_path, occurrence, *, suppress_metadata=True):
    mrd = W.materialize_from_document_forest(
        forest,
        focus={
            "mode": "row",
            "uuid": uuid,
            "ancestor_path": list(ancestor_path),
            "occurrence": occurrence,
        },
    )
    return W._build_markdown_preview_from_tree(
        mrd.render_forest,
        suppress_metadata=suppress_metadata,
        render_policy=mrd.policy_by_occurrence,
    )


# Two distinct subtrees both rooted at ROOT->A->B (same uuids + same
# ancestor_path), each B carrying its own distinct leaf -- exactly what the
# timestamp-sort trie-merge produces (fresh node objects, repeated uuid path).
def _repeated_ancestor_tree():
    return _node("ROOT", "Root", [
        _node("A", "A-first", [
            _node("B", "B-first", [_node("L1", "batch one")]),
        ]),
        _node("A", "A-second", [
            _node("B", "B-second", [_node("L3", "batch three")]),
        ]),
    ])


def test_enumerator_assigns_distinct_occurrence_and_index_path():
    rows = W._iter_markdown_focus_rows([_repeated_ancestor_tree()])
    b_rows = [r for r in rows if r["uuid"] == "B"]
    assert len(b_rows) == 2
    # Same identity prefix (uuid + ancestor_path) ...
    assert b_rows[0]["ancestor_path"] == ["ROOT", "A"]
    assert b_rows[1]["ancestor_path"] == ["ROOT", "A"]
    # ... distinguished only by occurrence + the structural index path.
    assert b_rows[0]["occurrence"] == 0
    assert b_rows[1]["occurrence"] == 1
    assert b_rows[0]["_index_path"] != b_rows[1]["_index_path"]


def test_resolve_and_roundtrip_land_on_clicked_node():
    tree = _repeated_ancestor_tree()
    ip1 = W._resolve_focus_index_path(
        [tree], uuid="B", ancestor_path=["ROOT", "A"], occurrence=1
    )
    assert ip1 is not None
    assert W._node_uuid_at_index_path([tree], ip1) == "B"
    ip0 = W._resolve_focus_index_path(
        [tree], uuid="B", ancestor_path=["ROOT", "A"], occurrence=0
    )
    assert ip0 is not None and ip0 != ip1
    assert W._node_uuid_at_index_path([tree], ip0) == "B"


def test_focus_renders_exact_occurrence_not_the_first():
    # Repointed (3b) to the materializer surface -- occurrence-exactness is now
    # owned by materialize_from_document_forest, and the composer renders the
    # already-pruned render_forest. Byte-identical to the old direct-composer
    # focus call.
    tree = _repeated_ancestor_tree()
    md1 = _focused_md([tree], "B", ["ROOT", "A"], 1)
    # The whole point of symptom #5: occurrence 1 must NOT leak to "batch one".
    assert "batch three" in md1
    assert "batch one" not in md1

    md0 = _focused_md([tree], "B", ["ROOT", "A"], 0)
    assert "batch one" in md0
    assert "batch three" not in md0


def test_focus_none_renders_whole_tree_unchanged():
    tree = _repeated_ancestor_tree()
    md = W._build_markdown_preview_from_tree([tree], suppress_metadata=True)
    # No focus -> the entire document, both occurrences present.
    assert "batch one" in md
    assert "batch three" in md


def test_sentinel_above_target_routes_correctly():
    # An empty-id "Home" sentinel above the real root: index_path must hop
    # through it (sentinel consumes an index slot, contributes NO uuid to
    # ancestor_path), so focus still lands on the exact occurrence -- the case
    # a uuid-only ancestor path cannot represent.
    stree = _node("", "Home", [
        _node("R", "R", [
            _node("X", "X-first", [_node("LX1", "sentinel batch one")]),
            _node("X", "X-second", [_node("LX3", "sentinel batch three")]),
        ]),
    ])
    rows = W._iter_markdown_focus_rows([stree])
    x_rows = [r for r in rows if r["uuid"] == "X"]
    assert len(x_rows) == 2
    # The empty-id sentinel must NOT appear in ancestor_path.
    assert x_rows[0]["ancestor_path"] == ["R"]
    assert x_rows[1]["ancestor_path"] == ["R"]

    sip = W._resolve_focus_index_path(
        [stree], uuid="X", ancestor_path=["R"], occurrence=1
    )
    assert sip is not None
    assert W._node_uuid_at_index_path([stree], sip) == "X"
    # Repointed (3b) to the materializer surface (see _focused_md): the sentinel
    # index-slot hop is handled inside the materializer's occurrence-exact
    # resolution, so focus still lands on the exact occurrence.
    smd = _focused_md([stree], "X", ["R"], 1)
    assert "sentinel batch three" in smd
    assert "sentinel batch one" not in smd


def test_build_modal_rows_strips_private_index_path():
    # The PUBLIC row list (what the client sees) must not carry _index_path.
    tree = _repeated_ancestor_tree()
    rows = W._build_modal_rows(tree)
    assert rows, "expected rows"
    for r in rows:
        assert "_index_path" not in r
        # Public identity triple is intact.
        assert set(("uuid", "depth", "name", "ancestor_path", "occurrence")) <= set(r)
