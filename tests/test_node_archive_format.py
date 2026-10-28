"""Tests for the workflowy-nexus-archive format authority (Unit 1, Gate 1a).

Every test here must fail against pre-unit code: the module did not exist,
so every import in this file is itself part of the evidence.
"""

from __future__ import annotations

import copy
import difflib
import json

import pytest

from workflowy_mcp.client.api_client_etch import WorkFlowyClientEtch
from workflowy_mcp.coherence.cache_view import (
    CacheView,
    is_test_deep_freeze_active,
    set_test_deep_freeze,
)
from workflowy_mcp.node_archive import (
    ARCHIVE_FORMAT,
    ARCHIVE_FORMAT_VERSION,
    TEXT_ENCODING,
    ArchiveError,
    ArchiveFormatError,
    archive_root_ids,
    build_archive_document,
    nodes_sha256,
    parse_archive,
    serialize_archive,
)

EXPORTED_AT = "2026-09-26T21:14:03Z"
SOURCE_CACHE = {"epoch_number": 57, "epoch_id": "epoch-57", "adopted_sequence": 1203}


def _nest(records):
    """The one shared flat-to-nested builder, bound without a client."""
    return WorkFlowyClientEtch.__new__(WorkFlowyClientEtch)._build_hierarchy(
        list(records)
    )


def _rec(node_id, name="n", parent_id=None, priority=0, **extra):
    record = {
        "id": node_id,
        "name": name,
        "note": "",
        "parent_id": parent_id,
        "priority": priority,
    }
    record.update(extra)
    return record


def _build(records, node_id=None, name=None, description=None):
    return build_archive_document(
        list(records),
        nest=_nest,
        node_id=node_id,
        name=name,
        description=description,
        exported_at=EXPORTED_AT,
        source_cache=dict(SOURCE_CACHE),
    )


def _roundtrip(records, node_id=None, name=None, description=None):
    document = _build(records, node_id=node_id, name=name, description=description)
    text = serialize_archive(document)
    parsed = parse_archive(text.encode("utf-8"), nest=_nest)
    return document, text, parsed


# ---------------------------------------------------------------------------
# T1 · Round trip
# ---------------------------------------------------------------------------


def test_t1_round_trip_preserves_every_record():
    records = [
        _rec("root", name="Root & <root>", note="line1\nline2", priority=3,
             data={"layoutMode": "bullets", "extra": [1, 2, {"k": "v"}]},
             createdAt=1758000000, modifiedAt=1758900000, completedAt=None,
             completed=False),
        _rec("kid", name="kid", parent_id="root", priority=1, completed=True),
        _rec("grand", name="grandkid", parent_id="kid", priority=0),
        _rec("sib", name="sibling", parent_id="root", priority=2),
    ]
    document, _text, parsed = _roundtrip(records, node_id="root")
    assert parsed == document
    by_id = {node["id"]: node for node in parsed["nodes"]}
    assert set(by_id) == {"root", "kid", "grand", "sib"}
    for source in records:
        assert by_id[source["id"]] == source
    # every canonical key of the source record survived
    assert by_id["root"]["data"] == records[0]["data"]
    assert by_id["root"]["createdAt"] == 1758000000
    assert by_id["kid"]["completed"] is True
    assert by_id["root"]["completedAt"] is None


# ---------------------------------------------------------------------------
# T2 · Determinism
# ---------------------------------------------------------------------------


def test_t2_determinism_over_shuffled_input_and_data_key_order():
    records = [
        _rec("a", name="a", data={"zz": 1, "aa": 2, "mm": {"y": 1, "b": 2}}),
        _rec("b", name="b", parent_id="a"),
        _rec("c", name="c", parent_id="a"),
        _rec("d", name="d"),
    ]
    shuffled_a = [records[2], records[0], records[3], records[1]]
    shuffled_b = [records[3], records[2], records[1], records[0]]
    reordered_data = [dict(records[0]), records[1], records[2], records[3]]
    reordered_data[0]["data"] = {"mm": {"b": 2, "y": 1}, "aa": 2, "zz": 1}

    texts = {
        serialize_archive(_build(order))
        for order in (records, shuffled_a, shuffled_b, reordered_data)
    }
    assert len(texts) == 1


# ---------------------------------------------------------------------------
# T3 · Small diffs
# ---------------------------------------------------------------------------


def _changed_line_count(before: str, after: str) -> int:
    """Changed content lines, excluding the nodes_sha256 line that every
    content change necessarily drags with it."""

    def _content(text_lines):
        return [
            line
            for line in text_lines
            if '"nodes_sha256"' not in line
        ]

    diff = list(
        difflib.ndiff(
            _content(before.splitlines(keepends=True)),
            _content(after.splitlines(keepends=True)),
        )
    )
    return sum(1 for line in diff if line.startswith(("+ ", "- ")))


def test_t3_one_field_edit_changes_exactly_one_line():
    records = [
        _rec("a", name="first", data={"k": "v", "n": 1}),
        _rec("b", name="second", parent_id="a"),
        _rec("c", name="third", parent_id="a"),
    ]
    base_text = serialize_archive(_build(records))

    renamed = [copy.deepcopy(records[0]), records[1], records[2]]
    renamed[0]["name"] = "first!"
    assert _changed_line_count(base_text, serialize_archive(_build(renamed))) == 2

    data_touched = [copy.deepcopy(records[0]), records[1], records[2]]
    data_touched[0]["data"]["n"] = 2
    assert (
        _changed_line_count(base_text, serialize_archive(_build(data_touched))) == 2
    )


# ---------------------------------------------------------------------------
# T4 · Layout golden
# ---------------------------------------------------------------------------


def test_t4_layout_golden_exact_key_order_and_indentation():
    records = [
        _rec("a", name="root", parent_id="p-outside", priority=2,
             data={"layoutMode": "bullets"}),
        _rec("b", name="kid", priority=1, parentId="a"),
        _rec("c", name="leaf", parent_id="b", priority=0, zeta_extra="x"),
    ]
    document = _build(records, node_id="a")
    expected = {
        "archive": {
            "format": ARCHIVE_FORMAT,
            "format_version": ARCHIVE_FORMAT_VERSION,
            "name": "root",
            "description": None,
            "exported_at": EXPORTED_AT,
            "root_ids": ["a"],
            "source_parent_id": "p-outside",
            "scope": "subtree",
            "node_count": 3,
            "nodes_sha256": nodes_sha256(document["nodes"]),
            "text_encoding": TEXT_ENCODING,
            "source_cache": {"epoch_number": 57, "epoch_id": "epoch-57",
                             "adopted_sequence": 1203},
        },
        "nodes": [
            {
                "id": "a", "name": "root", "note": "", "parent_id": "p-outside",
                "priority": 2, "data": {"layoutMode": "bullets"},
            },
            {
                "id": "b", "name": "kid", "note": "", "parent_id": None,
                "priority": 1, "parentId": "a",
            },
            {
                "id": "c", "name": "leaf", "note": "", "parent_id": "b",
                "priority": 0, "zeta_extra": "x",
            },
        ],
    }
    golden = json.dumps(expected, ensure_ascii=False, indent=2) + "\n"
    assert serialize_archive(document) == golden


# ---------------------------------------------------------------------------
# T5 · Roots
# ---------------------------------------------------------------------------


def test_t5a_single_root_subtree_carries_source_parent():
    records = [
        _rec("root", name="root", parent_id="the-parent"),
        _rec("kid", parent_id="root"),
    ]
    document, _text, _parsed = _roundtrip(records, node_id="root")
    assert document["archive"]["root_ids"] == ["root"]
    assert document["archive"]["source_parent_id"] == "the-parent"


def test_t5b_whole_vault_forest_roots_ordered_by_priority_then_id():
    records = [
        _rec("x", name="x", priority=2),
        _rec("y", name="y", priority=1),
        _rec("z", name="z", priority=1),
    ]
    document = _build(records)
    assert document["archive"]["root_ids"] == ["y", "z", "x"]
    assert document["archive"]["source_parent_id"] is None


def test_t5c_orphan_counts_as_a_root():
    records = [
        _rec("a", priority=2),
        _rec("b", parent_id="a", priority=1),
        _rec("orphan", parent_id="ghost", priority=1),
    ]
    document = _build(records)
    # b is a genuine child of a; the orphan's parent is outside the block
    assert document["archive"]["root_ids"] == ["orphan", "a"]
    assert [node["id"] for node in document["nodes"]] == ["orphan", "a", "b"]
    assert document["archive"]["root_ids"] == _root_ids_of(document)


def _root_ids_of(document):
    return archive_root_ids(document["nodes"])


# ---------------------------------------------------------------------------
# T6 · Emission order
# ---------------------------------------------------------------------------


def test_t6_emission_order_preorder_priority_defaults_and_no_children_key():
    records = [
        _rec("s-c", parent_id="root"),
        _rec("root", priority=5),
        _rec("s-b", parent_id="root"),
        _rec("s-a", parent_id="root"),
        _rec("s-b-1", parent_id="s-b"),
    ]
    document = _build(records)
    ids = [node["id"] for node in document["nodes"]]
    # equal (default 0) priorities fall back to id order; preorder walk;
    # the subtree under s-b is contiguous
    assert ids == ["root", "s-a", "s-b", "s-b-1", "s-c"]
    assert ids.index("root") == 0
    assert ids.index("s-b-1") == ids.index("s-b") + 1
    for node in document["nodes"]:
        assert "children" not in node


# ---------------------------------------------------------------------------
# T7 · Re-importability proof
# ---------------------------------------------------------------------------


def _canonical_walk(records):
    """Preorder walk with the format's own root order ((priority, id)); the
    builder sorts siblings but never roots, so both sides get the same
    canonical root order before comparison."""
    roots = _nest(list(records))
    roots = sorted(roots, key=lambda root: (root.get("priority", 0), root["id"]))
    return _preorder(roots)


def test_t7_nested_from_file_reproduces_source_topology_and_order():
    records = [
        _rec("r1", name="r1", priority=2),
        _rec("c1", parent_id="r1", priority=5),
        _rec("c2", parent_id="r1", priority=1),
        _rec("g1", parent_id="c1"),
        _rec("r2", name="r2", priority=1),
    ]
    document, text, _parsed = _roundtrip(records)
    parsed = parse_archive(text.encode("utf-8"), nest=_nest)

    source_walk = _canonical_walk(records)
    file_walk = _canonical_walk(parsed["nodes"])
    assert file_walk == source_walk
    assert len(file_walk) == len(records)
    assert set(file_walk) == {record["id"] for record in records}
    # every parent-child edge of the source survives into the file
    source_parents = {
        record["id"]: record.get("parent_id") or record.get("parentId")
        for record in records
    }
    file_parents = {
        record["id"]: record.get("parent_id") or record.get("parentId")
        for record in parsed["nodes"]
    }
    assert file_parents == source_parents


def _preorder(roots):
    walk = []
    stack = list(reversed(roots))
    while stack:
        node = stack.pop()
        walk.append(node["id"])
        stack.extend(reversed(list(node.get("children") or [])))
    return walk


# ---------------------------------------------------------------------------
# T8 · Text fidelity
# ---------------------------------------------------------------------------


def test_t8_text_fidelity_and_raw_utf8_emoji_no_raw_cr():
    tricky_name = "a & b < c > d &lt; literal 🌲\ttab"
    tricky_note = "lf\nline2\r\ncrlf &amp; tail"
    records = [_rec("a", name=tricky_name, note=tricky_note)]
    _document, text, parsed = _roundtrip(records)
    assert parsed["nodes"][0]["name"] == tricky_name
    assert parsed["nodes"][0]["note"] == tricky_note
    raw = text.encode("utf-8")
    assert "🌲".encode("utf-8") in raw  # emoji as raw UTF-8, not \u escapes
    assert b"\r" not in raw  # CRLF carried as escapes, never a raw byte


# ---------------------------------------------------------------------------
# T9 · Reader refusals
# ---------------------------------------------------------------------------


def _valid_bytes():
    records = [
        _rec("a", name="alpha"),
        _rec("b", name="beta", parent_id="a"),
    ]
    document = _build(records)
    return document, serialize_archive(document).encode("utf-8")


def _rehashed(document) -> bytes:
    """Re-serialize a mutated document with count+hash recomputed (roots
    kept as-is, so mutations reach the later reader rules)."""
    document["archive"]["node_count"] = len(document["nodes"])
    document["archive"]["nodes_sha256"] = nodes_sha256(document["nodes"])
    return serialize_archive(document).encode("utf-8")


def test_t9_reader_refusals():
    document, raw = _valid_bytes()

    with pytest.raises(ArchiveFormatError):
        _bytes = serialize_archive(
            {**document, "archive": {**document["archive"], "format": "other"}}
        ).encode("utf-8")
        parse_archive(_bytes, nest=_nest)
    with pytest.raises(ArchiveFormatError):
        _bytes = serialize_archive(
            {**document, "archive": {**document["archive"], "format_version": 2}}
        ).encode("utf-8")
        parse_archive(_bytes, nest=_nest)
    with pytest.raises(ArchiveFormatError):
        _bytes = serialize_archive(
            {**document, "archive": {**document["archive"], "format_version": True}}
        ).encode("utf-8")
        parse_archive(_bytes, nest=_nest)
    with pytest.raises(ArchiveFormatError):
        parse_archive(
            serialize_archive({**document, "extra": 1}).encode("utf-8"), nest=_nest
        )
    with pytest.raises(ArchiveFormatError):
        dropped = {k: v for k, v in document.items() if k != "archive"}
        parse_archive(serialize_archive(dropped).encode("utf-8"), nest=_nest)
    with pytest.raises(ArchiveFormatError):
        parse_archive(
            serialize_archive({**document, "nodes": {"id": "a"}}).encode("utf-8"),
            nest=_nest,
        )
    with pytest.raises(ArchiveFormatError):
        no_id = copy.deepcopy(document)
        del no_id["nodes"][1]["id"]
        parse_archive(_rehashed(no_id), nest=_nest)
    with pytest.raises(ArchiveFormatError):
        duped = copy.deepcopy(document)
        duped["nodes"][1]["id"] = "a"
        parse_archive(_rehashed(duped), nest=_nest)
    with pytest.raises(ArchiveFormatError):
        off_by_one = copy.deepcopy(document)
        off_by_one["archive"]["node_count"] += 1
        parse_archive(serialize_archive(off_by_one).encode("utf-8"), nest=_nest)
    with pytest.raises(ArchiveFormatError):
        renamed = copy.deepcopy(document)
        renamed["nodes"][0]["name"] = "alpha?"
        parse_archive(serialize_archive(renamed).encode("utf-8"), nest=_nest)
    with pytest.raises(ArchiveFormatError):
        two_roots = _build([_rec("a", priority=2), _rec("b", priority=1)])
        reordered = copy.deepcopy(two_roots)
        reordered["archive"]["root_ids"] = list(
            reversed(reordered["archive"]["root_ids"])
        )
        assert reordered["archive"]["root_ids"] == ["a", "b"]  # genuinely swapped
        parse_archive(serialize_archive(reordered).encode("utf-8"), nest=_nest)
    with pytest.raises(ArchiveFormatError):
        cycle = copy.deepcopy(document)
        cycle["nodes"][0]["parent_id"] = "b"
        cycle["nodes"][1]["parent_id"] = "a"
        cycle["archive"]["root_ids"] = archive_root_ids(cycle["nodes"])
        parse_archive(_rehashed(cycle), nest=_nest)
    with pytest.raises(ArchiveFormatError):
        parse_archive(b"\xff\xfe not utf-8", nest=_nest)
    with pytest.raises(ArchiveFormatError):
        parse_archive(b"\xef\xbb\xbf" + raw, nest=_nest)
    with pytest.raises(ArchiveFormatError):
        parse_archive(raw.replace(b'"priority": 0', b'"priority": NaN', 1), nest=_nest)

    # accepted: an extra unknown key inside archive (forward compatibility)
    forward = copy.deepcopy(document)
    forward["archive"]["future_key"] = {"anything": [1, 2]}
    parsed = parse_archive(serialize_archive(forward).encode("utf-8"), nest=_nest)
    assert parsed["archive"]["future_key"] == {"anything": [1, 2]}


# ---------------------------------------------------------------------------
# T10 · Hash recomputable from the file alone
# ---------------------------------------------------------------------------


def test_t10_hash_recomputable_from_file_alone():
    _document, raw = _valid_bytes()
    parsed = json.loads(raw.decode("utf-8"))
    independent = hashlib_sha256_of(parsed["nodes"])
    assert parsed["archive"]["nodes_sha256"] == independent
    assert nodes_sha256(parsed["nodes"]) == independent


def hashlib_sha256_of(nodes):
    import hashlib

    return hashlib.sha256(
        json.dumps(
            nodes, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    ).hexdigest()


# ---------------------------------------------------------------------------
# T11 · Serialization shape
# ---------------------------------------------------------------------------


def test_t11_serialization_shape_and_nan_refusal():
    _document, raw = _valid_bytes()
    assert raw.endswith(b"\n") and not raw.endswith(b"\n\n")
    assert b"\r\n" not in raw
    text = raw.decode("utf-8")
    lines = text.splitlines()
    assert lines[1].startswith("  ") and not lines[1].startswith("   ")

    with pytest.raises(ArchiveError):
        _build([_rec("a", name="a", data={"k": float("nan")})])
    nan_document = _build([_rec("a", name="a")])
    nan_document["nodes"][0]["data"] = {"k": float("nan")}
    with pytest.raises(ArchiveError):
        serialize_archive(nan_document)


# ---------------------------------------------------------------------------
# T12 · No aliasing, no mutation, deep-freeze safe
# ---------------------------------------------------------------------------


def _iter_containers(value):
    if isinstance(value, dict):
        yield id(value)
        for item in value.values():
            yield from _iter_containers(item)
    elif isinstance(value, list):
        yield id(value)
        for item in value:
            yield from _iter_containers(item)


def test_t12_no_aliasing_and_no_mutation():
    records = [
        _rec("a", name="a", data={"deep": {"deeper": [1, 2]}}),
        _rec("b", name="b", parent_id="a"),
    ]
    before = copy.deepcopy(records)
    document = _build(records)

    document_ids = set(_iter_containers(document))
    input_ids = set(_iter_containers(records))
    assert document_ids.isdisjoint(input_ids)
    assert records == before


def test_t12_build_and_serialize_succeed_under_deep_freeze():
    records = [
        _rec("a", name="a", data={"layoutMode": "bullets"}),
        _rec("b", name="b", parent_id="a"),
    ]
    previous = is_test_deep_freeze_active()
    set_test_deep_freeze(True)
    try:
        view = CacheView.build(
            epoch_number=1,
            epoch_id="e1",
            adopted_sequence=0,
            nodes_export={"nodes": list(records)},
        )
        frozen_records = list(view.nodes_export["nodes"])
        document = build_archive_document(
            frozen_records,
            nest=_nest,
            node_id="a",
            name=None,
            description=None,
            exported_at=EXPORTED_AT,
            source_cache=dict(SOURCE_CACHE),
        )
        text = serialize_archive(document)
        parsed = parse_archive(text.encode("utf-8"), nest=_nest)
        assert parsed == document
    finally:
        set_test_deep_freeze(previous)


# ---------------------------------------------------------------------------
# T13 · Build input checks
# ---------------------------------------------------------------------------


def test_t13_build_input_checks():
    with pytest.raises(ArchiveError):
        _build([_rec("a"), _rec("a", parent_id="a")])
    with pytest.raises(ArchiveError):
        _build([_rec(""), _rec("b", parent_id="")])
    with pytest.raises(ArchiveError):
        _build([_rec("a")], node_id="not-there")
    with pytest.raises(ArchiveError):
        build_archive_document(
            [_rec("a")], nest=_nest, node_id=None, name=None, description=None,
            exported_at="2026-09-26 21:14:03", source_cache=dict(SOURCE_CACHE),
        )
    with pytest.raises(ArchiveError):
        build_archive_document(
            [_rec("a")], nest=_nest, node_id=None, name=None, description=None,
            exported_at=EXPORTED_AT,
            source_cache={"epoch_number": 1, "epoch_id": "e"},
        )
    with pytest.raises(ArchiveError):
        build_archive_document(
            [_rec("a")], nest=_nest, node_id=None, name=None, description=None,
            exported_at=EXPORTED_AT,
            source_cache={"epoch_number": True, "epoch_id": "e",
                          "adopted_sequence": 0},
        )
    with pytest.raises(ArchiveError):
        # a subtree whose selection forms two roots
        _build([_rec("a", parent_id="outside"), _rec("b")], node_id="a")


# ---------------------------------------------------------------------------
# T14 · scope (Archive Reads U1, §4.1.2)
# ---------------------------------------------------------------------------


def test_t14a_builder_emits_scope_subtree_and_vault():
    subtree = _build([_rec("a"), _rec("b", parent_id="a")], node_id="a")
    vault = _build([_rec("a")], node_id=None)
    block = subtree["archive"]
    keys = list(block.keys())
    assert block["scope"] == "subtree"
    # scope sits immediately after source_parent_id in the archive block
    assert keys[keys.index("source_parent_id") + 1] == "scope"
    assert vault["archive"]["scope"] == "vault"
    # the new key round-trips (the reader ignores unknown archive keys, and
    # scope is now a known no-op key: presence only)
    text = serialize_archive(subtree)
    parsed = parse_archive(text.encode("utf-8"), nest=_nest)
    assert parsed["archive"]["scope"] == "subtree"


def test_t14b_archive_scope_matrix():
    from workflowy_mcp.node_archive import archive_scope

    # explicit valid values win
    assert archive_scope({"scope": "vault"}) == "vault"
    assert archive_scope({"scope": "subtree"}) == "subtree"
    with pytest.raises(ArchiveFormatError):
        archive_scope({"scope": "whole"})
    # absent -> inference
    assert archive_scope({"root_ids": ["a", "b"]}) == "vault"
    assert archive_scope(
        {"root_ids": ["a"], "source_parent_id": None, "name": "Workflowy vault"}
    ) == "vault"
    assert archive_scope(
        {"root_ids": ["a"], "source_parent_id": None, "name": "custom"}
    ) == "subtree"
    assert archive_scope(
        {"root_ids": ["a"], "source_parent_id": "p", "name": "anything"}
    ) == "subtree"


def test_t14c_archive_mount_parent_matrix():
    from workflowy_mcp.node_archive import archive_mount_parent

    assert archive_mount_parent({}) is None
    assert archive_mount_parent({"mount_parent_id": None}) is None
    assert archive_mount_parent({"mount_parent_id": "abc"}) == "abc"
    with pytest.raises(ArchiveFormatError):
        archive_mount_parent({"mount_parent_id": ""})
    with pytest.raises(ArchiveFormatError):
        archive_mount_parent({"mount_parent_id": 7})


def test_t14d_with_mount_parent_set_and_clear():
    from workflowy_mcp.node_archive import with_mount_parent

    records = [_rec("a", name="root"), _rec("b", parent_id="a")]
    document = _build(records, node_id="a")
    before = serialize_archive(document)
    nodes_object = document["nodes"]

    set_doc = with_mount_parent(document, "live-parent")
    # new document; input unchanged
    assert set_doc is not document
    assert serialize_archive(document) == before
    # nodes is the SAME list object, untouched
    assert set_doc["nodes"] is nodes_object
    assert set_doc["archive"]["mount_parent_id"] == "live-parent"
    # the key was appended at the end of the block when absent
    assert list(set_doc["archive"].keys())[-1] == "mount_parent_id"
    # nodes_sha256 unchanged and the result still parses
    assert set_doc["archive"]["nodes_sha256"] == document["archive"]["nodes_sha256"]
    parsed = parse_archive(serialize_archive(set_doc).encode("utf-8"), nest=_nest)
    assert parsed["archive"]["mount_parent_id"] == "live-parent"
    # nodes byte-identical between the documents
    assert json.dumps(set_doc["nodes"]) == json.dumps(document["nodes"])

    # clearing removes the key entirely
    cleared = with_mount_parent(set_doc, None)
    assert "mount_parent_id" not in cleared["archive"]
    assert serialize_archive(cleared) == before

    # setting over an existing mount preserves the key's position in the
    # block and replaces the value
    position = list(set_doc["archive"].keys()).index("mount_parent_id")
    again = with_mount_parent(set_doc, "second-parent")
    assert again["archive"]["mount_parent_id"] == "second-parent"
    assert list(again["archive"].keys()).index("mount_parent_id") == position

    with pytest.raises(ArchiveError):
        with_mount_parent(document, "")
    with pytest.raises(ArchiveError):
        with_mount_parent({"nodes": []}, "x")
