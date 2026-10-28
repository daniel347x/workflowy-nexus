"""S1 targeted protocol: ``include_rows`` on glimpse.run / anchor.dock.render.

The contract (UnitSpec [ap:OAPE16] §5.2, §6.1, §9.1):

  * Boolean ``include_rows`` is registered ONLY on ``glimpse.run`` and
    ``anchor.dock.render``.
  * A successful, in-memory **Markdown** render with the switch true returns
    ``{markdown: <the exact legacy final string>, rows: <the existing row
    authority's list>, render_spec: <the SAME resolved spec blob>}``.
  * Every other shape -- absent/false, full/UUID, headings-only full, file
    summary, push ack, error, estate, gather -- is byte-identical to the
    pre-S1 base.

The legacy side is an IMMUTABLE recording made from a ``git archive`` of the
S1 base commit ``0c56c96`` (``legacy_outputs.json``, captured by
``capture_legacy.py`` in the same fixture directory). It imports pre-change
bytes independently of the edited checkout; comparing the two new paths alone would be
the forbidden "two identically drifted paths" check.

The real public dispatch is exercised end-to-end against a tiny in-memory
``/nodes-export`` cache (``scenario.py``): no network, no live Workflowy, no
real writes.  Rows are compared with the Apply/push modal handler's
authoritative output for the SAME scope, captured into the same recording.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from unittest import mock

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

_FIXTURE_DIR = os.path.join(
    _REPO_ROOT, "tests", "fixtures", "glimpse-include-rows"
)
if _FIXTURE_DIR not in sys.path:
    sys.path.insert(0, _FIXTURE_DIR)

from workflowy_mcp import server as srv  # noqa: E402
from workflowy_mcp.cache_render_spec import RenderProjectionSpec  # noqa: E402
from workflowy_mcp.mcp_api_registry import dispatch_operation  # noqa: E402

import scenario  # noqa: E402


with open(os.path.join(_FIXTURE_DIR, "legacy_outputs.json"), "r", encoding="utf-8") as _fh:
    LEGACY = json.load(_fh)


# ---------------------------------------------------------------------------
# Fixture/harness helpers
# ---------------------------------------------------------------------------
def _plain(name, tmp_dir=None):
    return scenario.run_scenario(srv, name, tmp_dir=tmp_dir)


def _with_rows(name, *, include_rows=True, tmp_dir=None):
    return scenario.run_scenario(
        srv, name, include_rows=include_rows, tmp_dir=tmp_dir
    )


def _legacy_value(name):
    record = LEGACY["results"][name]
    return record["value"] if record["type"] == "str" else record["value"]


def _strip_operation_doc(value):
    """Errors carry dispatch-attached ``operation_doc``.

    That document is generated from the live registry, so it necessarily
    gains the new property's description; the error PAYLOAD (code, message,
    legacy_result, ...) is what must stay unchanged. Byte equality is kept for
    every result that is not an error envelope.
    """
    if isinstance(value, dict) and value.get("success") is False:
        return {k: v for k, v in value.items() if k != "operation_doc"}
    return value


def _normalize(value, tmp_dir):
    """Same normalization the capture script applied to file summaries."""
    if isinstance(value, str):
        return value.replace(str(tmp_dir), "<TMP>")
    if isinstance(value, list):
        return [_normalize(v, tmp_dir) for v in value]
    if isinstance(value, dict):
        return {k: _normalize(v, tmp_dir) for k, v in value.items()}
    return value


def _walk_keys(value):
    if isinstance(value, dict):
        for k, v in value.items():
            yield k
            yield from _walk_keys(v)
    elif isinstance(value, list):
        for v in value:
            yield from _walk_keys(v)


POSITIVE_MARKDOWN_SCOPES = [
    "raw_root",
    "raw_root_headings",
    "raw_root_created_asc",
    "raw_root_modified_desc",
    "raw_root_recent",
    "raw_root_empty_window",
    "slice_group",
    "slice_override",
    "simple_dock",
    "node_parcel",
    "subtree_parcel",
    "tags_parcel",
    "lens_parcel",
    "raw_root_advisory",
]

AUTHORITY_SCOPES = [
    "raw_root",
    "slice_group",
    "slice_override",
    "simple_dock",
    "raw_root_created_asc",
    "raw_root_modified_desc",
]


# ---------------------------------------------------------------------------
# 1 · Legacy byte identity WITHOUT the switch (passes on pre-S1 bytes by
#     design; its job is to prove the recording and the live calls agree).
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", list(scenario.SCENARIOS))
def test_plain_output_still_matches_prechange_recording(name):
    with tempfile.TemporaryDirectory() as d:
        got = _plain(name, tmp_dir=d)
        assert _strip_operation_doc(_normalize(got, d)) == _strip_operation_doc(
            _legacy_value(name)
        )


@pytest.mark.parametrize("name", list(scenario.SCENARIOS))
def test_include_rows_false_matches_prechange_recording(name):
    # Fails on pre-S1 bytes: the option is not registered yet.
    with tempfile.TemporaryDirectory() as d:
        got = _with_rows(name, include_rows=False, tmp_dir=d)
        assert _strip_operation_doc(_normalize(got, d)) == _strip_operation_doc(
            _legacy_value(name)
        )


# ---------------------------------------------------------------------------
# 2 · Positive Markdown envelope = exact legacy string + row authority + spec
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", POSITIVE_MARKDOWN_SCOPES)
def test_markdown_envelope_carries_exact_legacy_string(name):
    plain = _plain(name)
    envelope = _with_rows(name)

    assert isinstance(plain, str), plain
    assert isinstance(envelope, dict), envelope
    assert set(envelope) == {"markdown", "rows", "render_spec"}, envelope
    assert envelope["markdown"] == plain
    # ... and the plain call is still the RECORDED pre-S1 bytes.
    assert envelope["markdown"] == _legacy_value(name)
    assert isinstance(envelope["rows"], list), envelope["rows"]
    assert isinstance(envelope["render_spec"], dict), envelope["render_spec"]
    assert "_resolved_render_spec" not in envelope


def test_anchor_dock_render_entry_returns_the_same_envelope():
    # The SECOND registered operation. Exercises the anchor registry's own
    # `dock.render` handler (not glimpse.run) end-to-end.
    plain = _plain("anchor_dock")
    envelope = _with_rows("anchor_dock")
    assert isinstance(envelope, dict), envelope
    assert set(envelope) == {"markdown", "rows", "render_spec"}, envelope
    assert envelope["markdown"] == plain == _legacy_value("anchor_dock")
    assert envelope["rows"] == LEGACY["modal_authority"]["simple_dock"]["rows"]
    assert envelope["render_spec"]["root_uuid"] == scenario.DOCK
    assert envelope["render_spec"]["descend_policy"] == "stop_at_child_anchors"
    # A child Dock is a visible leaf signpost; its descendants never render.
    assert scenario.CHILD_DOCK in [r["uuid"] for r in envelope["rows"]]
    assert scenario.INSIDE_CHILD_DOCK not in envelope["markdown"]
    assert scenario.INSIDE_CHILD_DOCK not in [r["uuid"] for r in envelope["rows"]]


def test_empty_markdown_keeps_the_literal_empty_stub_inside_the_envelope():
    # A PUBLIC live-DOM empty result (no resolved cache spec) is the empty
    # window/empty-document shape: the literal '(empty)' plus rows []. The
    # resolved-spec echo is genuinely unavailable here, so it stays absent.
    async def fake_live_glimpse(**_kwargs):
        return {"success": True, "node_count": 0, "preview_markdown": "", "rows": []}

    async def fake_grace(**_kwargs):
        return (None, None)

    with scenario.patched_environment(srv) as client:
        client.workflowy_glimpse = fake_live_glimpse
        with mock.patch.object(
            srv, "get_ws_connection_with_grace_wait", fake_grace
        ):
            reply = __import__("asyncio").run(dispatch_operation(
                srv.GLIMPSE_REGISTRY,
                "run",
                {"include_rows": True},
                error_logger=lambda *_a: None,
                operation_boundary=srv._mcp_operation_boundary,
                mutation_admission=srv._mcp_mutation_admission,
                exception_mapper=srv._mcp_exception_mapper,
                result_finalizer=srv._mcp_operation_result_finalizer,
            ))
    assert reply == {"markdown": "(empty)", "rows": []}, reply


def test_shaper_empty_markdown_stub_with_resolved_spec():
    # The same stub case where the render DID have a spec: the spec echo rides
    # beside it.
    result = {
        "success": True,
        "preview_markdown": "",
        "rows": [],
        "_resolved_render_spec": {
            "kind": "raw_root",
            "root_uuid": scenario.R,
            "as_markdown": True,
        },
    }
    out = srv._shape_glimpse_family_return(
        result, output_file=None, include_rows=True
    )
    assert out == {
        "markdown": "(empty)",
        "rows": [],
        "render_spec": {
            "kind": "raw_root",
            "root_uuid": scenario.R,
            "as_markdown": True,
        },
    }


# ---------------------------------------------------------------------------
# 3 · Canonical rows equal the modal authority (with cache_by_id timestamps)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", AUTHORITY_SCOPES)
def test_rows_match_modal_authority_including_occurrences_and_timestamps(name):
    envelope = _with_rows(name)
    expected = LEGACY["modal_authority"][name]["rows"]
    assert envelope["rows"] == expected, (
        f"row authority mismatch for {name}\n"
        f"got={envelope['rows']!r}\nexpected={expected!r}"
    )


def test_timestamp_sorts_expose_repeated_ancestor_occurrences():
    # The corpus interleaves the A-branch leaves with the B-branch leaf, so
    # the shared ancestor is re-emitted and its SECOND row carries
    # occurrence=1 with the same identity + own timestamps.
    for name in ("raw_root_created_asc", "raw_root_modified_desc"):
        rows = _with_rows(name)["rows"]
        arch = [r for r in rows if r["uuid"] == scenario.A]
        assert [r["occurrence"] for r in arch] == [0, 1], rows
        assert arch[1]["ancestor_path"] == [scenario.R]
        assert arch[1]["depth"] == 1
        assert arch[1]["created"] == 10 and arch[1]["modified"] == 40


def test_every_row_timestamp_comes_from_the_cache_by_id_join():
    rows = _with_rows("raw_root")["rows"]
    by_id = {n["id"]: n for n in scenario.CACHE_NODES}
    for row in rows:
        assert row["created"] == by_id[row["uuid"]]["createdAt"], row
        assert row["modified"] == by_id[row["uuid"]]["modifiedAt"], row


# ---------------------------------------------------------------------------
# 4 · The echoed spec round-trips and drives a focused glimpse.run
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", POSITIVE_MARKDOWN_SCOPES)
def test_render_spec_echo_round_trips_through_the_authority_serializer(name):
    blob = _with_rows(name)["render_spec"]
    spec = RenderProjectionSpec.from_render_spec_blob(blob)
    assert spec.to_render_spec_blob() == blob


def test_dock_echo_keeps_stop_at_child_docks_and_recursive_semantics():
    for name in ("simple_dock", "anchor_dock"):
        blob = _with_rows(name)["render_spec"]
        assert blob["kind"] == "raw_root", blob
        assert blob["root_uuid"] == scenario.DOCK, blob
        assert blob["descend_policy"] == "stop_at_child_anchors", blob
        assert blob["recursive"] is True, blob


def test_focused_tap_uses_the_real_triple_and_keeps_the_whole_row_list():
    baseline = _with_rows("raw_root")
    rows = baseline["rows"]
    resolver = next(r for r in rows if r["uuid"] == scenario.A1)

    # A timestamp-sorted scope proves the repeated-occurrence triple too.
    sorted_baseline = _with_rows("raw_root_created_asc")
    sorted_rows = sorted_baseline["rows"]
    repeated_arch = next(
        r for r in sorted_rows
        if r["uuid"] == scenario.A and r["occurrence"] == 1
    )

    cases = [
        (resolver, baseline),
        (repeated_arch, sorted_baseline),
    ]
    for row, base in cases:
        focused = dict(base["render_spec"])
        focused["focus"] = {
            "mode": "row",
            "uuid": row["uuid"],
            "ancestor_path": row["ancestor_path"],
            "occurrence": row["occurrence"],
        }
        # A real tap sends the focused formal blob through glimpse.run.
        reply = _dispatch_focused(focused)
        assert isinstance(reply, dict), reply
        assert set(reply) == {"markdown", "rows", "render_spec"}, reply
        assert reply["markdown"] and isinstance(reply["markdown"], str)
        # ROW FOCUS RETAINS THE WHOLE LIST: Stage-2 loops the whole final
        # forest, not the focused render forest.
        assert reply["rows"] == base["rows"]
        assert reply["render_spec"]["focus"] == focused["focus"]


def _dispatch_focused(blob):
    with scenario.patched_environment(srv):
        return __import__("asyncio").run(dispatch_operation(
            srv.GLIMPSE_REGISTRY,
            "run",
            {"render_spec": blob, "include_rows": True},
            error_logger=lambda *_a: None,
            operation_boundary=srv._mcp_operation_boundary,
            mutation_admission=srv._mcp_mutation_admission,
            exception_mapper=srv._mcp_exception_mapper,
            result_finalizer=srv._mcp_operation_result_finalizer,
        ))


def test_stale_focus_with_include_rows_stays_a_loud_error():
    reply = _with_rows("stale_focus")
    assert isinstance(reply, dict), reply
    assert reply.get("success") is False, reply
    err = reply.get("error") or {}
    assert err.get("code") == "glimpse_request_failed", reply
    assert "could not be resolved" in str(err.get("message")), reply
    for forbidden in ("markdown", "rows", "render_spec"):
        assert forbidden not in reply, reply


# ---------------------------------------------------------------------------
# 5 · The switch never creates a new mode for non-Markdown shapes
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name",
    ["raw_root_full", "raw_root_full_headings", "raw_root_output_file", "bad_group"],
)
def test_non_markdown_shapes_are_unchanged_with_the_switch(name):
    with tempfile.TemporaryDirectory() as d:
        got = _with_rows(name, tmp_dir=d)
        assert _strip_operation_doc(_normalize(got, d)) == _strip_operation_doc(
            _legacy_value(name)
        )
        if isinstance(got, dict):
            assert "rows" not in got, got
            assert "_resolved_render_spec" not in got
            assert "markdown" not in got


def test_push_ack_is_not_converted_into_an_envelope(monkeypatch):
    # push_to_modal never returns content; the switch cannot turn its ack into
    # a Markdown envelope. Drive the impl directly with an ack-shaped result
    # through the shaper (the push path is out of scope for network here).
    ack = {"success": True, "pushed_to_modal": True, "root_uuid": scenario.R}
    assert srv._shape_glimpse_family_return(
        ack, output_file=None, include_rows=True
    ) is ack


# ---------------------------------------------------------------------------
# 6 · Registration: Boolean, only on the two operations, never required
# ---------------------------------------------------------------------------
def test_include_rows_is_registered_as_boolean_only_on_run_and_dock_render():
    allowed = {("glimpse", "run"), ("anchor", "dock.render")}
    seen = set()
    for registry in (
        srv.GLIMPSE_REGISTRY,
        srv.ANCHOR_REGISTRY,
        srv.LIGHTNING_ROD_REGISTRY,
        srv.SLICE_GROUP_REGISTRY,
    ):
        for spec in registry.operations():
            props = (spec.params_schema or {}).get("properties") or {}
            if "include_rows" in props:
                seen.add((spec.family, spec.verb))
                prop = props["include_rows"]
                assert prop.get("type") == "boolean", prop
                assert prop.get("default") is False, prop
                assert "include_rows" not in str(prop.get("enum")), prop
                # Described: the trailing caveat is part of the contract.
                assert "Markdown" in str(prop.get("description") or ""), prop
                assert "include_rows" not in (
                    spec.params_schema.get("required") or []
                ), spec.params_schema
    assert seen == allowed, seen
    # The describe surface carries the same registration (the agent-facing
    # contract, not only the in-process schema).
    for registry, verb in (
        (srv.GLIMPSE_REGISTRY, "run"),
        (srv.ANCHOR_REGISTRY, "dock.render"),
    ):
        doc = registry.describe_full(verb)
        assert "include_rows" in doc["params_schema"]["properties"], doc
        assert "include_rows" not in (
            doc["params_schema"].get("required") or []
        ), doc
        assert "include_rows" in str(doc.get("returns") or ""), doc


def test_include_rows_is_not_a_required_property_on_either_operation():
    for registry, verb in (
        (srv.GLIMPSE_REGISTRY, "run"),
        (srv.ANCHOR_REGISTRY, "dock.render"),
    ):
        schema = registry.resolve(verb).params_schema
        assert "include_rows" not in (schema.get("required") or [])
        assert schema.get("additionalProperties") is False


def test_estate_does_not_register_the_option_and_stays_row_less():
    estate = srv.GLIMPSE_REGISTRY.resolve("estate")
    assert "include_rows" not in (estate.params_schema or {}).get("properties", {})
    reply = __import__("asyncio").run(dispatch_operation(
        srv.GLIMPSE_REGISTRY,
        "estate",
        {"handle": "anything", "include_rows": True},
        error_logger=lambda *_a: None,
        operation_boundary=srv._mcp_operation_boundary,
        mutation_admission=srv._mcp_mutation_admission,
        exception_mapper=srv._mcp_exception_mapper,
        result_finalizer=srv._mcp_operation_result_finalizer,
    ))
    assert reply.get("success") is False, reply
    assert reply["error"]["code"] == "forbidden_parameter", reply


# ---------------------------------------------------------------------------
# 7 · No opt-in metadata leaks anywhere
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", list(scenario.SCENARIOS))
def test_private_resolved_spec_never_leaks(name):
    for kwargs in ({}, {"include_rows": False}, {"include_rows": True}):
        with tempfile.TemporaryDirectory() as d:
            value = _with_rows(name, tmp_dir=d, **kwargs) if kwargs else _plain(name, tmp_dir=d)
            assert "_resolved_render_spec" not in list(_walk_keys(value)), value


# Supplemental coverage: the public schemas/transports, every focusable scope,
# and the two successful empty exits (rather than only a narrow nonempty window).
import asyncio
import copy
import edge_scenarios

with open(os.path.join(_FIXTURE_DIR, "edge_outputs.json"), encoding="utf-8") as _fh:
    EDGES = json.load(_fh)["values"]


@pytest.mark.parametrize("name", ["empty_window", "empty_lens"])
def test_real_empty_exits_preserve_prechange_markdown_and_echo_the_same_spec(name):
    plain = edge_scenarios.run(srv, name)
    assert plain == EDGES[name]
    assert plain.endswith("(empty)")
    assert edge_scenarios.run(srv, name, False) == plain
    envelope = edge_scenarios.run(srv, name, True)
    assert envelope["markdown"] == plain
    assert envelope["rows"] == []
    blob = envelope["render_spec"]
    assert RenderProjectionSpec.from_render_spec_blob(blob).to_render_spec_blob() == blob
    if name == "empty_window":
        assert blob["since"]["form"] == "curated"
        assert blob["group_id"] == "grp-arch"
    else:
        assert blob["lightning"]["lens"]["operation"] == "difference"


@pytest.mark.parametrize("name", ["estate_node", "estate_empty_lens"])
def test_estate_rendering_stays_byte_identical_and_has_no_private_spec(name):
    result = edge_scenarios.run(srv, name)
    assert result == EDGES[name]
    assert isinstance(result, str)
    assert "_resolved_render_spec" not in result


@pytest.mark.parametrize("name", [
    "raw_root", "slice_group", "slice_override", "simple_dock", "anchor_dock",
    "node_parcel", "subtree_parcel", "tags_parcel", "lens_parcel",
    "raw_root_headings", "raw_root_recent", "raw_root_created_asc",
])
def test_each_cache_scope_echo_is_a_working_occurrence_exact_tap_request(name):
    original = _with_rows(name)
    row = original["rows"][-1]
    blob = copy.deepcopy(original["render_spec"])
    focus = {"mode": "row", **{key: row[key] for key in (
        "uuid", "ancestor_path", "occurrence")}}
    blob["focus"] = focus
    focused = _dispatch_focused(blob)
    assert focused["rows"] == original["rows"]
    assert focused["render_spec"] == blob
    assert focused["markdown"]
    if name == "raw_root":
        assert "Resolver body" not in focused["markdown"]
        assert "Deploy step body" in focused["markdown"]


def test_captured_focused_document_remains_byte_identical():
    focused = _dispatch_focused(LEGACY["focused"]["spec_blob"])
    assert focused["markdown"] == LEGACY["focused"]["result"]["value"]


@pytest.mark.parametrize("name", ["raw_root_created_asc", "slice_group", "anchor_dock"])
def test_rows_are_verbatim_authority_results_over_the_actual_final_scope_forest(monkeypatch, name):
    from workflowy_mcp.client.api_client_nexus import WorkFlowyClientNexus
    original = WorkFlowyClientNexus._build_modal_rows
    calls = []

    def observe(cls, root, *, cache_by_id=None):
        rows = original(root, cache_by_id=cache_by_id)
        calls.append((root, cache_by_id, rows))
        return rows

    monkeypatch.setattr(WorkFlowyClientNexus, "_build_modal_rows", classmethod(observe))
    result = _with_rows(name)
    assert calls
    assert result["rows"] == [row for _, _, rows in calls for row in rows]
    for root, cache, rows in calls:
        assert cache and root
        assert rows == original(root, cache_by_id=cache)


def test_dock_shallow_echo_and_focus_do_not_reexpand_plain_or_child_dock_descendants():
    descendant = "20000000-0000-4000-8000-000000000099"
    nodes = list(scenario.CACHE_NODES) + [{
        "id": descendant, "name": "Plain grandchild", "note": "hidden by depth",
        "parent_id": scenario.DA, "priority": 0, "createdAt": 20, "modifiedAt": 20,
    }]
    with mock.patch.object(scenario, "CACHE_NODES", nodes), scenario.patched_environment(srv):
        shallow = asyncio.run(scenario._dispatch(srv, "ANCHOR_REGISTRY", "dock.render", {
            "dock_handle": scenario.DOCK, "recursive": False, "include_rows": True,
        }))
    assert shallow["render_spec"]["recursive"] is False
    assert shallow["render_spec"]["descend_policy"] == "stop_at_child_anchors"
    assert descendant not in [row["uuid"] for row in shallow["rows"]]
    row = next(row for row in shallow["rows"] if row["uuid"] == scenario.DA)
    blob = copy.deepcopy(shallow["render_spec"])
    blob["focus"] = {"mode": "row", **{key: row[key] for key in (
        "uuid", "ancestor_path", "occurrence")}}
    with mock.patch.object(scenario, "CACHE_NODES", nodes):
        focused = _dispatch_focused(blob)
    assert focused["rows"] == shallow["rows"]
    assert "Plain grandchild" not in focused["markdown"]
    assert focused["render_spec"]["recursive"] is False


def test_text_only_gather_stays_a_raw_string_and_public_push_stays_an_ack(monkeypatch):
    text = "#alpha\n#beta"
    assert srv._shape_glimpse_family_return({
        "success": True, "preview_text": text, "rows": [1],
    }, output_file=None, include_rows=True) == text

    ack = {"success": True, "pushed_to_modal": True}

    async def fake_push(**kwargs):
        assert "include_rows" not in kwargs
        return ack

    monkeypatch.setattr(srv, "_glimpse_push_slice_to_modal", fake_push)
    with scenario.patched_environment(srv):
        assert asyncio.run(srv._glimpse_run_impl(
            root_uuid=scenario.R, push_to_modal=True, include_rows=True,
        )) is ack


def test_generated_public_family_params_schema_stays_open_and_unenumerated():
    tools = asyncio.run(srv.mcp.list_tools())
    selected = [tool for tool in tools if tool.name in ("glimpse", "anchor")]
    assert len(selected) == 2
    for tool in selected:
        params = tool.parameters["properties"]["params"]
        alternatives = params.get("anyOf", [params])
        object_params = next(value for value in alternatives if value.get("type") == "object")
        assert object_params["additionalProperties"] is True
        assert "include_rows" not in json.dumps(tool.parameters)


@pytest.mark.parametrize("family,verb,params", [
    ("glimpse", "run", {"render_spec": {"node": scenario.A1}}),
    ("anchor", "dock.render", {"dock_handle": scenario.DOCK}),
])
def test_mcp_success_envelope_has_equal_structured_content_and_json_text_twin(family, verb, params):
    with scenario.patched_environment(srv):
        result = asyncio.run(srv.mcp.call_tool(family, {
            "verb": verb, "params": {**params, "include_rows": True},
        }))
    assert not result.is_error
    envelope = result.structured_content
    assert set(envelope) == {"markdown", "rows", "render_spec"}
    assert json.loads(result.content[0].text) == envelope
    assert envelope["rows"]
