"""Tolerated near-miss key slips at the params-normalization seam.

A slipping key -- a model with the right intent typing the wrong spelling -- is
absorbed silently in the one dispatch funnel (``_apply_tolerated_key_slips`` in
``mcp_api_registry``), beside the JSON-string grace, and advertised nowhere.
The U01 unit locked the five ``node_*``/``note_*`` slips; U02 widens the one
table to every unambiguous near-miss the family surface admits (Brief
``[ap:MOBKFJ]``), including the approved ``node`` -> ``content`` on
``anchor.point.capture``.

Locked here, per slip:

(a) the mistaken spelling behaves byte-identically to the correct one --
    the handler receives identical kwargs and returns the identical result;
(b) the mistaken spelling appears nowhere in ``tools/list`` or ``describe``
    output (schema keys, legacy aliases, and serialized text);
(c) a correctly-spelled key is never overridden by the mistaken one;
(d) a genuine real key is untouched, a slip that is itself a legitimate key of
    a verb is never shadowed, and no slip is a published key/alias anywhere;
(e) both spellings present with different values is the loud error.

(b), (d)'s preservation halves and the table-consistency check pass on the
pre-fix tree by construction; they exist to keep the grace invisible and the
real keys safe.  (a), (c) and (e) are change-detecting: pre-fix every newly
added slip is a ``forbidden_parameter`` on the slip (the five U01 slips were
change-detecting at U01's base).
"""

from __future__ import annotations

import asyncio
import json
import re

import pytest

from workflowy_mcp import server
from workflowy_mcp.mcp_api_registry import (
    OperationRegistry,
    OperationSpec,
    dispatch_operation,
)


# The enumeration under test -- mirrors ``_TOLERATED_KEY_SLIPS`` exactly
# (asserted by ``test_table_matches_the_documented_enumeration``).  Each row is
# a slip spelling -> its ordered candidate intended keys.
SLIPS: dict[str, tuple[str, ...]] = {
    # note/node misspellings (U01); bare ``node`` also reaches point.capture's
    # body key ``content`` (U02 / approved Ledger L1).
    "node_edits": ("note_edits",),
    "node_file": ("note_file",),
    "node_search": ("note_search",),
    "node_replace": ("note_replace",),
    "node": ("note", "content"),
    # ``<thing>_id`` / ``<thing>_uuid`` typed for ``<thing>_handle``.
    "node_uuid": ("node_handle",),
    "root_id": ("root_handle",),
    "root_ids": ("root_handles",),
    "dock_id": ("dock_handle",),
    "dock_uuid": ("dock_handle",),
    "group_uuid": ("group_handle",),
    "source_id": ("source_handle",),
    "source_uuid": ("source_handle",),
    # bare noun typed for the qualified handle key.
    "parent": ("parent_handle",),
    "dock": ("dock_handle",),
    "root": ("root_handle", "root_handles"),
    # singular for plural.
    "range": ("ranges",),
    # distinctive stem or keyword typed for the qualified key.
    "direction": ("sort_direction",),
    "regex": ("is_regex",),
    "case": ("case_sensitive",),
    "filter": ("name_filter",),
    "output": ("output_file",),
    "push": ("push_to_modal",),
    "descend": ("descend_policy",),
    "headings": ("headings_only",),
    "markdown": ("as_markdown",),
    "path": ("file_path",),
    "recurse": ("recursive",),
    "operation": ("operations",),
    "max": ("max_matches", "max_name_chars", "max_chars"),
    "completed": ("include_completed",),
    "counts": ("show_counts",),
    "ids": ("show_ids",),
    "job": ("job_id",),
}

# The five slips U01 shipped, as slip -> intended (real node.edit handler tests).
NODE_EDIT_SLIPS = {
    "node_edits": "note_edits",
    "node_file": "note_file",
    "node_search": "note_search",
    "node_replace": "note_replace",
    "node": "note",
}

# Every (slip, candidate) pair -- the parametrization for the generic contract.
SLIP_PAIRS = sorted(
    (slip, intended) for slip, intendeds in SLIPS.items() for intended in intendeds
)

# ``node`` alone is a legitimate word everywhere (the family, ``node_handle``),
# so the text check covers the distinctive compound slips; every slip is
# covered by the schema-key and alias checks.
_COMPOUND_SLIP_RE = re.compile(r"\bnode_(?:edits|file|search|replace|uuid)\b")


def _run(coro):
    return asyncio.run(coro)


def _node_registry(monkeypatch):
    received: list[dict] = []

    async def edit(handle, **kwargs):
        received.append({"handle": handle, **kwargs})
        return {"id": handle, "note": kwargs.get("note")}

    monkeypatch.setattr(server, "_node_edit_impl", edit)
    return server.build_node_registry(), received


def _anchor_registry(monkeypatch):
    received: list[dict] = []

    async def capture(parent, **kwargs):
        received.append({"parent": parent, **kwargs})
        return {"success": True, "root_node_ids": ["P"]}

    monkeypatch.setattr(server, "_anchor_point_capture_impl", capture)
    return server.build_anchor_registry(), received


def _edit_value(intended: str, tmp_path):
    if intended == "note_edits":
        return [{"search": "old", "replace": "new"}]
    if intended == "note_file":
        path = tmp_path / "replacement.md"
        path.write_text("# Replaced\n\nbody\n", encoding="utf-8")
        return str(path)
    return f"value-for-{intended}"


async def _echo(params):
    return params


def _demo_spec(properties, **overrides):
    values = {
        "family": "demo",
        "verb": "run",
        "summary": "Run the demo operation.",
        "side_effects": ("pure_read",),
        "async_mode": "never",
        "params_schema": {
            "type": "object",
            "properties": properties,
            "additionalProperties": False,
        },
        "returns": {"type": "object"},
        "examples": (),
        "related": (),
        "handler": _echo,
        "cache_backed": False,
    }
    values.update(overrides)
    return OperationSpec(**values)


def _demo_registry(*properties_dicts) -> OperationRegistry:
    registry = OperationRegistry("demo", "Demo operations.")
    for properties in properties_dicts:
        registry.register(_demo_spec(properties))
    return registry


# ---------------------------------------------------------------------------
# (a) byte-identical behaviour -- generic over every slip, plus real handlers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slip,intended", SLIP_PAIRS)
def test_a_slip_maps_to_its_candidate_on_a_spec_exposing_it(slip, intended):
    registry = _demo_registry({intended: {"type": "string"}})
    result = _run(dispatch_operation(registry, "run", {slip: "v"}))
    assert result == {intended: "v"}
    assert slip not in result


@pytest.mark.parametrize("slip,intended", sorted(NODE_EDIT_SLIPS.items()))
def test_a_node_edit_slip_is_byte_identical_to_the_correct_key(
    monkeypatch, tmp_path, slip, intended
):
    registry, received = _node_registry(monkeypatch)
    value = _edit_value(intended, tmp_path)
    extra = {"note_replace": "r"} if intended == "note_search" else {}

    correct = _run(
        dispatch_operation(
            registry, "edit", {"node_handle": "N", intended: value, **extra}
        )
    )
    slipped = _run(
        dispatch_operation(
            registry, "edit", {"node_handle": "N", slip: value, **extra}
        )
    )

    assert len(received) == 2
    assert received[0] == received[1]
    assert slip not in received[1]
    assert correct == slipped
    assert correct["success"] is True and correct["operation"] == "node.edit"


def test_a_node_file_slip_is_byte_identical_on_point_capture(monkeypatch, tmp_path):
    registry, received = _anchor_registry(monkeypatch)
    path = tmp_path / "capture.md"
    path.write_text("# Captured\n", encoding="utf-8")

    correct = _run(
        dispatch_operation(
            registry, "point.capture", {"parent_handle": "P", "note_file": str(path)}
        )
    )
    slipped = _run(
        dispatch_operation(
            registry, "point.capture", {"parent_handle": "P", "node_file": str(path)}
        )
    )
    assert len(received) == 2
    assert received[0] == received[1]
    assert received[1]["note_file"] == str(path)
    assert "node_file" not in received[1]
    assert correct == slipped
    assert correct["changed"] is True


def test_a_bare_node_maps_to_content_on_point_capture(monkeypatch):
    """The approved L1: ``node`` on point.capture is the body key ``content``."""
    registry, received = _anchor_registry(monkeypatch)
    result = _run(
        dispatch_operation(registry, "point.capture", {"parent_handle": "P", "node": "body"})
    )
    assert result["changed"] is True
    assert received[0]["content"] == "body"
    assert "node" not in received[0] and "note" not in received[0]
    assert received[0]["note_file"] is None


def test_a_node_uuid_reaches_the_node_edit_handler_as_node_handle(monkeypatch):
    registry, received = _node_registry(monkeypatch)
    result = _run(
        dispatch_operation(registry, "edit", {"node_uuid": "N", "note": "x"})
    )
    assert result["success"] is True
    assert received[0]["handle"] == "N"


def test_check_paging_keeps_path_slip_shared_and_renamed_output_unaliased(monkeypatch):
    """U05: new paging contract does not publish any tolerated input slip."""
    received = []

    async def check(node_handle, file_path, min_severity, max_rows, offset, codes, finding_ids):
        received.append((node_handle, file_path, min_severity, max_rows, offset, codes, finding_ids))
        return {"success": True, "target": {"resolved_uuid": node_handle}}

    monkeypatch.setattr(server, "_beacon_check_impl", check)
    registry = server.build_beacon_registry()
    spec = registry.resolve("check")
    assert dict(spec.param_aliases) == {"node_id": "node_handle", "root_uuid": "node_handle"}
    assert not (set(spec.param_aliases) & set(SLIPS))
    description = registry.describe_full("check")
    assert "resolved_uuid" in str(description) and "node_uuid" not in str(description)
    filters = {"offset": 2, "max_rows": 3, "codes": ["empty_comment"], "finding_ids": ["bf1:x"]}
    correct = _run(dispatch_operation(registry, "check", {"file_path": "/repo/file.py", **filters}))
    slipped = _run(dispatch_operation(registry, "check", {"path": "/repo/file.py", **filters}))
    assert correct == slipped and correct["success"] is True
    assert received == [(None, "/repo/file.py", "warning", 3, 2, ["empty_comment"], ["bf1:x"])] * 2


def test_a_slip_survives_the_json_string_grace(monkeypatch):
    registry, received = _node_registry(monkeypatch)
    payload = json.dumps({"node_handle": "N", "node_search": "s", "node_replace": "r"})
    result = _run(dispatch_operation(registry, "edit", payload))
    assert result["success"] is True
    assert received[0]["note_search"] == "s"
    assert received[0]["note_replace"] == "r"


# ---------------------------------------------------------------------------
# (b) advertised nowhere: describe output, tools/list, and no published key
# ---------------------------------------------------------------------------


def _all_registries() -> dict[str, OperationRegistry]:
    return {
        name: value
        for name, value in vars(server).items()
        if name.endswith("_REGISTRY") and isinstance(value, OperationRegistry)
    }


def test_table_matches_the_documented_enumeration():
    from workflowy_mcp.mcp_api_registry import _TOLERATED_KEY_SLIPS

    assert {
        slip: tuple(intendeds) for slip, intendeds in _TOLERATED_KEY_SLIPS.items()
    } == SLIPS


def test_b_slips_absent_from_every_describe_surface():
    registries = _all_registries()
    assert len(registries) == 8
    for name, registry in registries.items():
        index = registry.describe_index()
        assert not _COMPOUND_SLIP_RE.search(json.dumps(index, default=str)), name
        for spec in registry.operations():
            full = registry.describe_full(spec.verb)
            properties = full["params_schema"].get("properties") or {}
            assert not set(properties).intersection(SLIPS), (name, spec.verb)
            assert not set(spec.param_aliases).intersection(SLIPS), (name, spec.verb)
            assert not set(spec.param_harmless_ignored).intersection(SLIPS), (
                name,
                spec.verb,
            )
            assert not _COMPOUND_SLIP_RE.search(json.dumps(full, default=str)), (
                name,
                spec.verb,
            )


def test_b_slips_absent_from_tools_list():
    tools = _run(server.mcp.list_tools())
    assert tools
    for tool in tools:
        blob = json.dumps(
            {"name": tool.name, "description": tool.description, "parameters": tool.parameters},
            default=str,
        )
        assert not _COMPOUND_SLIP_RE.search(blob), tool.name
        properties = (tool.parameters or {}).get("properties") or {}
        assert not set(properties).intersection(SLIPS), tool.name


def test_b_no_slip_is_a_published_key_or_alias_of_any_verb():
    """Rule 2: a tolerated slip spelling is never itself advertised anywhere.

    If a slip were a schema property or a legacy alias of any verb, tolerating
    it would put the slip spelling on that published surface -- so the table
    must not contain such a spelling (``node_id``, ``tag``, ``symbol``, ... are
    deliberately absent for exactly this reason).  The same holds for the
    per-verb harmless-ignored registry (Unit B): a slip spelling registered
    harmless would be silently dropped instead of mapped, so no slip may be a
    registered harmless argument of any verb either.
    """
    for name, registry in _all_registries().items():
        for spec in registry.operations():
            properties = set(spec.params_schema.get("properties") or {})
            assert not properties.intersection(SLIPS), (name, spec.verb)
            assert not set(spec.param_aliases).intersection(SLIPS), (name, spec.verb)
            assert not set(spec.param_harmless_ignored).intersection(SLIPS), (
                name,
                spec.verb,
            )


def test_harmless_ignored_registration_is_bounded_to_dock_peek_headings_only():
    """Unit B bound: the first and ONLY harmless-ignored registration is
    anchor.dock.peek -> headings_only, across every family registry; the key
    is not advertised in the published anchor tool schema (the fixed
    {verb, params} envelope cannot carry per-verb keys)."""
    registered = {
        (registry.family, spec.verb): tuple(spec.param_harmless_ignored)
        for registry in _all_registries().values()
        for spec in registry.operations()
        if spec.param_harmless_ignored
    }
    assert registered == {("anchor", "dock.peek"): ("headings_only",)}
    tools = _run(server.mcp.list_tools())
    anchor_tool = next(tool for tool in tools if tool.name == "anchor")
    assert "headings_only" not in json.dumps(anchor_tool.parameters, default=str)


# ---------------------------------------------------------------------------
# (c) the correct spelling always wins
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slip,intended", SLIP_PAIRS)
def test_c_correct_spelling_wins_when_both_carry_the_same_value(slip, intended):
    registry = _demo_registry({intended: {"type": "string"}})
    correct_only = _run(dispatch_operation(registry, "run", {intended: "v"}))
    both_same = _run(dispatch_operation(registry, "run", {intended: "v", slip: "v"}))
    assert both_same == correct_only == {intended: "v"}


@pytest.mark.parametrize("slip,intended", sorted(NODE_EDIT_SLIPS.items()))
def test_c_node_edit_correct_key_is_never_overridden(
    monkeypatch, tmp_path, slip, intended
):
    registry, received = _node_registry(monkeypatch)
    value = _edit_value(intended, tmp_path)
    extra = {"note_replace": "r"} if intended == "note_search" else {}

    correct_only = _run(
        dispatch_operation(
            registry, "edit", {"node_handle": "N", intended: value, **extra}
        )
    )
    both_same = _run(
        dispatch_operation(
            registry,
            "edit",
            {"node_handle": "N", intended: value, slip: value, **extra},
        )
    )
    assert len(received) == 2
    assert received[0] == received[1]
    assert slip not in received[1]
    assert correct_only == both_same


# ---------------------------------------------------------------------------
# (d) real keys pass through untouched; candidate priority is deterministic
# ---------------------------------------------------------------------------


def test_d_point_capture_nodes_payload_is_untouched(monkeypatch):
    registry, received = _anchor_registry(monkeypatch)
    nodes = [{"name": "Recap", "note": "# Body", "children": [{"name": "Item"}]}]
    result = _run(
        dispatch_operation(registry, "point.capture", {"parent_handle": "P", "nodes": nodes})
    )
    assert result["changed"] is True
    assert received[0]["nodes"] == nodes
    assert received[0]["note_file"] is None
    assert "note" not in received[0] and "node" not in received[0]


def test_d_node_etch_nodes_payload_is_untouched(monkeypatch):
    seen = []

    async def etch(parent, nodes=None, nodes_file=None, **kwargs):
        seen.append((parent, nodes, nodes_file, kwargs))
        return {"success": True, "root_node_ids": ["E"]}

    monkeypatch.setattr(server, "_node_etch_impl", etch)
    registry = server.build_node_registry()
    nodes = [{"name": "x", "note": "n"}]
    result = _run(
        dispatch_operation(registry, "etch", {"parent_handle": "P", "nodes": nodes})
    )
    assert result["success"] is True
    assert seen[0][0] == "P" and seen[0][1] == nodes


def test_d_slip_without_a_candidate_on_that_verb_falls_through_unchanged(monkeypatch):
    # node.etch has neither ``note`` nor ``content`` (its payload key is the
    # real ``nodes``), so ``node`` is not mapped there: the ordinary
    # forbidden_parameter names the slip and the handler never runs.
    seen = []

    async def etch(parent, nodes=None, nodes_file=None, **kwargs):
        seen.append(kwargs)
        return {"success": True, "root_node_ids": ["E"]}

    monkeypatch.setattr(server, "_node_etch_impl", etch)
    registry = server.build_node_registry()
    result = _run(
        dispatch_operation(registry, "etch", {"parent_handle": "P", "node": "body"})
    )
    assert result["error"]["code"] == "forbidden_parameter"
    assert "'node'" in result["error"]["message"]
    assert seen == []


def test_d_slip_that_is_a_legitimate_property_is_never_shadowed():
    registry = _demo_registry({"node": {"type": "string"}, "note": {"type": "string"}})
    assert _run(dispatch_operation(registry, "run", {"node": "N"})) == {"node": "N"}
    assert _run(dispatch_operation(registry, "run", {"node": "N", "note": "T"})) == {
        "node": "N",
        "note": "T",
    }


def test_d_slip_that_is_a_legacy_alias_is_never_shadowed():
    registry = OperationRegistry("demo", "Demo operations.")
    registry.register(
        _demo_spec(
            {"node_handle": {"type": "string"}, "note": {"type": "string"}},
            param_aliases={"node": "node_handle"},
        )
    )
    assert _run(dispatch_operation(registry, "run", {"node": "N"})) == {"node_handle": "N"}


def test_d_candidate_priority_picks_the_first_real_property():
    # ``node`` has two candidates; with both real on one spec, the earlier one wins.
    registry = _demo_registry(
        {"note": {"type": "string"}, "content": {"type": "string"}}
    )
    assert _run(dispatch_operation(registry, "run", {"node": "v"})) == {"note": "v"}
    # ``root`` has two candidates (handle, then handles).
    registry = _demo_registry(
        {"root_handle": {"type": "string"}, "root_handles": {"type": "string"}}
    )
    assert _run(dispatch_operation(registry, "run", {"root": "v"})) == {"root_handle": "v"}
    # ``max`` resolves by verb: only the exposed candidate maps.
    registry = _demo_registry({"max_name_chars": {"type": "string"}})
    assert _run(dispatch_operation(registry, "run", {"max": "v"})) == {
        "max_name_chars": "v"
    }


# ---------------------------------------------------------------------------
# (e) both spellings, different values -> loud error, handler never runs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slip,intended", SLIP_PAIRS)
def test_e_both_spellings_with_different_values_is_a_loud_error(slip, intended):
    registry = _demo_registry({intended: {"type": "string"}})
    result = _run(
        dispatch_operation(registry, "run", {intended: "kept", slip: "other"})
    )
    assert result["error"]["code"] == "forbidden_parameter"
    assert "different values" in result["error"]["message"]
    assert repr(slip) in result["error"]["message"]
    assert repr(intended) in result["error"]["message"]


@pytest.mark.parametrize("slip,intended", sorted(NODE_EDIT_SLIPS.items()))
def test_e_node_edit_both_spellings_with_different_values_is_a_loud_error(
    monkeypatch, tmp_path, slip, intended
):
    registry, received = _node_registry(monkeypatch)
    value = _edit_value(intended, tmp_path)
    other = [{"search": "x"}] if intended == "note_edits" else f"other-{intended}"
    result = _run(
        dispatch_operation(
            registry, "edit", {"node_handle": "N", intended: value, slip: other}
        )
    )
    assert result["error"]["code"] == "forbidden_parameter"
    assert "different values" in result["error"]["message"]
    assert repr(slip) in result["error"]["message"]
    assert repr(intended) in result["error"]["message"]
    assert received == []
