from __future__ import annotations

import json

import pytest

from workflowy_mcp.lens_spec import (
    GroupAtom,
    LensSpecError,
    MAX_LENS_ATOMS,
    MergeNode,
    SubtreeAtom,
    TagsAtom,
    iter_lens_atoms,
    lens_spec_to_wire,
    parse_lens_spec,
)


def _group(name: str = "G") -> dict:
    return {"kind": "group", "group_handle": name}


def test_parse_all_four_kinds_and_json_string_grace():
    group = parse_lens_spec(_group())
    tags = parse_lens_spec(
        {"kind": "tags", "root_handle": "R", "tags": ["#a"], "match_scope": "name"}
    )
    subtree = parse_lens_spec(
        {"kind": "subtree", "root_handle": "R", "descend": "stop_at_child_docks"}
    )
    merge = parse_lens_spec(
        json.dumps(
            {
                "kind": "merge",
                "operation": "union",
                "inputs": [_group("A"), _group("B")],
            }
        )
    )
    assert group == GroupAtom("G")
    assert tags == TagsAtom("R", ("#a",), "name")
    assert subtree == SubtreeAtom("R", "stop_at_child_docks")
    assert isinstance(merge, MergeNode)


def test_difference_is_accepted_and_round_trips_in_input_order():
    raw = {
        "kind": "merge",
        "operation": "difference",
        "inputs": [_group("Base"), _group("Subtract-1"), _group("Subtract-2")],
    }
    parsed = parse_lens_spec(raw)
    assert parsed == MergeNode(
        "difference",
        (GroupAtom("Base"), GroupAtom("Subtract-1"), GroupAtom("Subtract-2")),
    )
    assert lens_spec_to_wire(parsed) == raw


def test_closed_keys_and_nested_paths_are_loud():
    raw = {
        "kind": "merge",
        "operation": "union",
        "inputs": [
            _group("A"),
            {
                "kind": "merge",
                "operation": "intersection",
                "inputs": [_group("B"), {"kind": "subtree", "root_handle": "R", "oops": 1}],
            },
        ],
    }
    with pytest.raises(LensSpecError) as raised:
        parse_lens_spec(raw)
    assert raised.value.code == "invalid_lens_shape"
    assert raised.value.path == "inputs[1].inputs[1]"
    assert "oops" in raised.value.message


def test_depth_five_and_seventeen_atoms_are_rejected():
    node = _group("leaf")
    for depth in range(4):
        node = {
            "kind": "merge",
            "operation": "union",
            "inputs": [node, _group(f"sibling-{depth}")],
        }
    with pytest.raises(LensSpecError) as depth_error:
        parse_lens_spec(node)
    assert depth_error.value.code == "lens_depth_exceeded"

    too_many = {
        "kind": "merge",
        "operation": "union",
        "inputs": [_group(str(index)) for index in range(MAX_LENS_ATOMS + 1)],
    }
    with pytest.raises(LensSpecError) as budget_error:
        parse_lens_spec(too_many)
    assert budget_error.value.code == "lens_atom_budget_exceeded"


def test_one_input_merge_and_enum_failures_have_domain_codes():
    with pytest.raises(LensSpecError) as one:
        parse_lens_spec({"kind": "merge", "operation": "union", "inputs": [_group()]})
    assert one.value.code == "empty_merge_inputs"

    cases = [
        ({"kind": "merge", "operation": "xor", "inputs": [_group(), _group("B")]}, "invalid_merge_operation"),
        ({"kind": "subtree", "root_handle": "R", "descend": "sometimes"}, "invalid_descend_policy"),
        ({"kind": "tags", "root_handle": "R", "tags": ["x"], "match_scope": "note"}, "invalid_match_scope"),
        ({"kind": "wat"}, "invalid_lens_kind"),
    ]
    for raw, code in cases:
        with pytest.raises(LensSpecError) as raised:
            parse_lens_spec(raw)
        assert raised.value.code == code


def test_round_trip_and_atom_paths_are_exact():
    raw = {
        "kind": "merge",
        "operation": "intersection",
        "inputs": [
            {
                "kind": "merge",
                "operation": "union",
                "inputs": [
                    {"kind": "group", "group_handle": "Architecture", "match_scope": "name"},
                    {"kind": "tags", "root_handle": "R", "tags": ["build-v1"]},
                ],
            },
            {"kind": "subtree", "root_handle": "A", "descend": "all"},
        ],
    }
    parsed = parse_lens_spec(raw)
    assert parse_lens_spec(lens_spec_to_wire(parsed)) == parsed
    assert [path for path, _atom in iter_lens_atoms(parsed)] == [
        "inputs[0].inputs[0]",
        "inputs[0].inputs[1]",
        "inputs[1]",
    ]


def test_error_structure_is_machine_readable():
    error = LensSpecError("root_resolution_failed", "missing", "inputs[2]")
    assert error.to_dict() == {
        "code": "root_resolution_failed",
        "message": "missing",
        "path": "inputs[2]",
    }
