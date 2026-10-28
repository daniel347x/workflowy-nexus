"""ap:MSFBP Step C — the materializer sets file_boundary_mode correctly.

Locks the SOLE decision point: ``materialize_from_document_forest`` stamps
``policy_by_occurrence.file_boundary_mode``:
  * 'connected_subtree'  ONLY for a focus mode == 'rows' (multi-select
    covering render);
  * 'standalone_blocks'  for whole-document (focus None), a single-row
    option-Y focus (mode 'row'), and a root-row focus.

The composer does NOT yet consume the mode at this step, so this is a
policy-object assertion, independent of rendered output.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from workflowy_mcp.client.api_client_nexus import (  # noqa: E402
    WorkFlowyClientNexus as W,
)


def _node(nid, name, note="", children=None):
    return {"id": nid, "name": name, "note": note, "children": children or []}


def _r(uuid, ancestor_path, occurrence=0):
    return {"uuid": uuid, "ancestor_path": list(ancestor_path), "occurrence": occurrence}


def _rows(*rows):
    return {"mode": "rows", "rows": [dict(r) for r in rows]}


def _row(uuid, ancestor_path, occurrence=0):
    return {
        "mode": "row",
        "uuid": uuid,
        "ancestor_path": list(ancestor_path),
        "occurrence": occurrence,
    }


def _forest():
    return [
        _node("root", "Root", "root body", [
            _node("a", "Alpha", "alpha body", [
                _node("a1", "Alpha-1", "a1 body"),
                _node("a2", "Alpha-2", "a2 body"),
            ]),
            _node("b", "Beta", "beta body", [
                _node("b1", "Beta-1", "b1 body"),
            ]),
        ]),
    ]


def test_rows_focus_sets_connected_subtree():
    mrd = W.materialize_from_document_forest(
        _forest(),
        focus=_rows(_r("a1", ["root", "a"], 0), _r("b1", ["root", "b"], 0)),
    )
    assert mrd.policy_by_occurrence.file_boundary_mode == "connected_subtree"


def test_one_element_rows_focus_still_connected_subtree():
    # A mode='rows' focus is a covering render REGARDLESS of element count.
    mrd = W.materialize_from_document_forest(
        _forest(), focus=_rows(_r("a1", ["root", "a"], 0))
    )
    assert mrd.policy_by_occurrence.file_boundary_mode == "connected_subtree"


def test_single_row_focus_stays_standalone():
    mrd = W.materialize_from_document_forest(
        _forest(), focus=_row("a1", ["root", "a"], 0)
    )
    assert mrd.policy_by_occurrence.file_boundary_mode == "standalone_blocks"


def test_whole_document_focus_none_stays_standalone():
    mrd = W.materialize_from_document_forest(_forest(), focus=None)
    assert mrd.policy_by_occurrence.file_boundary_mode == "standalone_blocks"


def test_root_row_focus_stays_standalone():
    # A root-row focus (top-level node, empty ancestor_path) is the whole
    # rooted document -> standalone.
    mrd = W.materialize_from_document_forest(
        _forest(), focus=_row("root", [], 0)
    )
    assert mrd.policy_by_occurrence.file_boundary_mode == "standalone_blocks"
