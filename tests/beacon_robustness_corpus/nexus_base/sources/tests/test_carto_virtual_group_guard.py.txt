"""Virtual-group guard locks (Manager-Session-088).

The beacon write planner and the legacy note-driven writer return an early
noop for Cartographer VIRTUAL GROUP nodes (synthetic ``📦`` wrappers built
from ``@carto-group`` markers). The guard used to be a plain substring test,
``"CARTOGRAPHER_VIRTUAL_GROUP:" in note``, so it also fired on any beacon node
whose note merely QUOTED the marker: the span beacon
``carto-virtual-groups@identity-and-grouping`` in ``nexus_map_codebase.py``
carries the group-parsing code itself in its SPAN TEXT, and ``tags.remove``
silently returned noop on it every time. The guard now requires the marker as
the note's first non-blank line, which is how ``group_outline_by_carto_markers``
writes a group note.

Every test works on a scratch file under pytest's ``tmp_path`` and never
touches a real repository source file or the ``beacon.tags.*`` MCP tools.
"""

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import beacon_mutation_contract as bmc  # noqa: E402
from workflowy_mcp.client import api_client_nexus as acn  # noqa: E402
from workflowy_mcp.lightning_rod_mutation_spec import BeaconMutationOperation  # noqa: E402

_ROOT = "11111111-1111-1111-1111-111111111111"
_FILE = "22222222-2222-2222-2222-222222222222"
_NODE = "33333333-3333-3333-3333-333333333333"

_GROUP_NOTE = "\n".join([
    "CARTOGRAPHER_VIRTUAL_GROUP: python",
    "CARTO_GROUP_ID: client-group-4",
    "CARTO_GROUP_LABEL: Client group four",
    "CARTO_GROUP_MARKER_LINE: 12",
    "---",
    "Synthetic Cartographer grouping node. Runtime source semantics unchanged.",
])

# A beacon node whose note QUOTES the marker inside its text (the real case:
# a span beacon's SPAN TEXT containing the group-parsing code).
_QUOTING_NOTE = "\n".join([
    "AST_QUALNAME: foo",
    "---",
    "BEACON (AST)",
    "id: quote-1",
    "role: foo",
    "slice_labels: keep-me,drop-me",
    "kind: ast",
    "comment: parses group notes",
    "---",
    "SPAN TEXT:",
    "def foo(note):",
    '    if s.startswith("CARTOGRAPHER_VIRTUAL_GROUP:"):',
    "        return True",
])


def test_predicate_true_for_a_real_group_note():
    assert acn._is_carto_virtual_group_note(_GROUP_NOTE)
    assert acn._is_carto_virtual_group_note("\n\n  " + _GROUP_NOTE)


def test_predicate_false_for_a_note_that_only_quotes_the_marker():
    assert not acn._is_carto_virtual_group_note(_QUOTING_NOTE)


def test_predicate_false_for_empty_or_none():
    assert not acn._is_carto_virtual_group_note("")
    assert not acn._is_carto_virtual_group_note(None)


def _py_source():
    return "\n".join([
        "# @beacon[",
        "#   id=quote-1,",
        "#   role=foo,",
        "#   slice_labels=keep-me,drop-me,",
        "#   kind=ast,",
        "#   comment=parses group notes,",
        "# ]",
        "def foo(note):",
        '    if note.startswith("CARTOGRAPHER_VIRTUAL_GROUP:"):',
        "        return True",
        "    return False",
        "",
    ])


def _cache(path, node_note):
    return {
        _ROOT: {"id": _ROOT, "name": "Root", "note": "", "parent_id": None, "priority": 0},
        _FILE: {
            "id": _FILE,
            "name": os.path.basename(str(path)),
            "note": f"Path: {path}",
            "parent_id": _ROOT,
            "priority": 0,
        },
        _NODE: {
            "id": _NODE,
            "name": "ƒ foo #keep-me #drop-me",
            "note": node_note,
            "parent_id": _FILE,
            "priority": 100,
        },
    }


def _remove_op():
    return BeaconMutationOperation(
        input_index=0,
        handle="foo",
        tags=("drop-me",),
        comment=bmc.BeaconCommentIntent.preserve(),
    )


def test_plan_is_update_not_noop_when_note_only_quotes_the_marker(tmp_path):
    p = tmp_path / "m.py"
    p.write_text(_py_source(), encoding="utf-8")
    plan = acn._compute_beacon_write_plan(
        _cache(p, _QUOTING_NOTE), _NODE, _remove_op(), "remove_beacon_tags",
        include_fingerprint=False,
    )
    assert plan.success, plan.error
    assert plan.planned_operation == "update"
    assert plan.beacon_id == "quote-1"
    assert plan.intended_slice_labels == ("keep-me",)


def test_plan_still_noops_for_a_real_virtual_group_node(tmp_path):
    p = tmp_path / "m.py"
    p.write_text(_py_source(), encoding="utf-8")
    plan = acn._compute_beacon_write_plan(
        _cache(p, _GROUP_NOTE), _NODE, _remove_op(), "remove_beacon_tags",
        include_fingerprint=False,
    )
    assert plan.success
    assert plan.planned_operation == "noop"
    assert plan.beacon_id is None
    assert plan.noop_reason == "virtual_group"


def test_noop_reason_labels_already_absent(tmp_path):
    p = tmp_path / "m.py"
    p.write_text(_py_source(), encoding="utf-8")
    op = BeaconMutationOperation(
        input_index=0,
        handle="foo",
        tags=("not-there",),
        comment=bmc.BeaconCommentIntent.preserve(),
    )
    plan = acn._compute_beacon_write_plan(
        _cache(p, _QUOTING_NOTE), _NODE, op, "remove_beacon_tags",
        include_fingerprint=False,
    )
    assert plan.success, plan.error
    assert plan.planned_operation == "noop"
    assert plan.noop_reason == "labels_already_absent"


def test_noop_reason_is_none_for_an_actionable_plan(tmp_path):
    p = tmp_path / "m.py"
    p.write_text(_py_source(), encoding="utf-8")
    plan = acn._compute_beacon_write_plan(
        _cache(p, _QUOTING_NOTE), _NODE, _remove_op(), "remove_beacon_tags",
        include_fingerprint=False,
    )
    assert plan.planned_operation == "update"
    assert plan.noop_reason is None
