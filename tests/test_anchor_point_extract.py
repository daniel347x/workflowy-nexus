"""Extraction safety: real family boundaries/core composition, isolated I/O.

Unit faults target capture/update authorities, never real Workflowy. The
end-to-end case below goes through actual capture/ETCH/CRUD/governor/COW code
with only HTTP and browser configuration mocked, on an isolated lineage.
"""
from __future__ import annotations

import asyncio
import copy
import html
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from coherence_harness import make_boundary_capable, make_coherence_root, s04_raise_barrier
from n05_region_harness import isolate_claim_scope
from workflowy_mcp import server
from workflowy_mcp.coherence.admission import mutation_admission_class
from workflowy_mcp.coherence.errors import CacheViewError
from workflowy_mcp.coherence.operation_cache import current_operation_cache
from workflowy_mcp.mcp_api_registry import dispatch_operation, validate_params
from workflowy_mcp.models.node import WorkFlowyNode
from workflowy_mcp.note_lines import normalize_extract_ranges, plan_note_extraction

SOURCE = "11111111-1111-4111-8111-111111111111"
PARENT = "22222222-2222-4222-8222-222222222222"
NEW = "33333333-3333-4333-8333-333333333333"
NEW2 = "44444444-4444-4444-8444-444444444444"
NAME = "[ap:SOURCE] Source 📊 #keep #wfy-suppress"
NOTE = "one\ntwo\nthree\nfour\nfive\nsix\nseven\neight\nnine\nten"


async def dispatch_async(params):
    return await dispatch_operation(
        server.build_anchor_registry(), "point.extract", params,
        operation_boundary=server._mcp_operation_boundary,
        mutation_admission=server._mcp_mutation_admission,
        exception_mapper=server._mcp_exception_mapper,
        result_finalizer=server._mcp_operation_result_finalizer,
    )


def dispatch(**extra):
    return asyncio.run(dispatch_async({"node_handle": SOURCE, "parent_handle": PARENT, **extra}))


def setup(monkeypatch, tmp_path, note=NOTE):
    paths = make_coherence_root(tmp_path)
    monkeypatch.setattr(server, "_admission_coherence_paths", lambda: paths)
    client = make_boundary_capable(object.__new__(server.WorkFlowyClient), nodes=[
        {"id": SOURCE, "name": NAME, "note": "stale", "parent_id": None, "completed": True},
        {"id": PARENT, "name": "[ap:DEST] Destination", "parent_id": None},
    ])
    isolate_claim_scope(client, tmp_path)
    current = WorkFlowyNode(id=SOURCE, name=NAME, note=html.escape(note))
    client.get_node = AsyncMock(return_value=current)
    events = []
    async def capture(parent, **kwargs):
        index = len([event for event in events if event[0] == "capture"])
        events.append(("capture", parent, kwargs))
        return {"success": True, "root_node_ids": [NEW if index == 0 else NEW2], "ap_id": f"[ap:NEW{index}]"}
    async def update(node_id, request, *, basis=None):
        events.append(("update", node_id, request))
        return WorkFlowyNode(id=node_id, name=current.name, note=request.note)
    client.update_node = AsyncMock(side_effect=update)
    capture_mock = AsyncMock(side_effect=capture)
    monkeypatch.setattr(server, "_anchor_point_capture_impl", capture_mock)
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "_rate_limiter", None)
    return client, capture_mock, events, paths, current


@pytest.mark.parametrize("handle", [SOURCE, SOURCE[:8], f"[{SOURCE[:8]}]", "[ap:SOURCE]"])
def test_single_extract_real_read_edit_core_and_metadata_preservation(monkeypatch, tmp_path, handle):
    client, capture, events, _, current = setup(monkeypatch, tmp_path)
    core = AsyncMock(wraps=server._node_edit_core)
    monkeypatch.setattr(server, "_node_edit_core", core)
    result = dispatch(node_handle=handle, start_line=2, end_line=4, name="Section")
    assert result["success"] is True
    assert [event[0] for event in events] == ["capture", "update"]
    capture.assert_awaited_once_with(PARENT, content="two\nthree\nfour", name="Section", ap_id="auto", tags=None, target_tab=None)
    core.assert_awaited_once_with(SOURCE, note="one\nfive\nsix\nseven\neight\nnine\nten", _current_node=current)
    request = client.update_node.await_args.args[1]
    assert request.name is None and request.layoutMode is None
    assert current.name == NAME and current.note == html.escape(NOTE)
    assert result["source"] == {"uuid": SOURCE, "lines_before": 10, "lines_after": 7, "deleted_line_count": 3}
    assert result["extracted"]["line_count"] == 3 and result["extracted"]["uuid"] == NEW
    assert result["partial_failure"] is None
    client.get_node.assert_awaited_once_with(SOURCE)
    assert current_operation_cache(required=False) is None


def test_batch_original_coordinates_sorted_creates_one_deletion_and_no_input_mutation(monkeypatch, tmp_path):
    client, capture, events, _, _ = setup(monkeypatch, tmp_path)
    ranges = [{"start_line": 8, "end_line": 9, "name": "Later", "ap_id": None}, {"start_line": 2, "end_line": 3, "name": "Early"}]
    before = copy.deepcopy(ranges)
    result = dispatch(ranges=ranges, ap_id="[ap:POLICY]", tags=["x"], target_tab="tab")
    assert ranges == before
    assert result["success"] is True
    assert [event[0] for event in events] == ["capture", "capture", "update"]
    assert [call.kwargs["content"] for call in capture.await_args_list] == ["two\nthree", "eight\nnine"]
    assert [call.kwargs["ap_id"] for call in capture.await_args_list] == ["[ap:POLICY]", None]
    assert all(call.kwargs["tags"] == ["x"] and call.kwargs["target_tab"] == "tab" for call in capture.await_args_list)
    assert client.update_node.await_args.args[1].note == "one\nfour\nfive\nsix\nseven\nten"
    assert [row["name"] for row in result["extracted"]] == ["Early", "Later"]
    assert result["source"]["deleted_line_count"] == 4
    client.get_node.assert_awaited_once()
    client.update_node.assert_awaited_once()


@pytest.mark.parametrize("extra,code", [
    ({"start_line": 0, "end_line": 1, "name": "X"}, "invalid_parameter_value"),
    ({"start_line": True, "end_line": 1, "name": "X"}, "invalid_parameter_type"),
    ({"start_line": 3, "end_line": 2, "name": "X"}, "line_range_out_of_bounds"),
    ({"start_line": 1, "end_line": 11, "name": "X"}, "line_range_out_of_bounds"),
    ({"start_line": 1, "end_line": 1}, "invalid_parameter_type"),
    ({"start_line": 1, "end_line": 1, "name": "  "}, "invalid_parameter_type"),
    ({"ranges": []}, "invalid_parameter_combination"),
    ({"ranges": [{"start_line": 1, "end_line": 2, "name": "X"}], "name": "mixed"}, "invalid_parameter_combination"),
    ({"ranges": [{"start_line": 1, "end_line": 2, "name": "X"}], "start_line": 1}, "invalid_parameter_combination"),
    ({"ranges": [{"start_line": 1, "end_line": 2, "name": "X"}, {"start_line": 2, "end_line": 4, "name": "Y"}]}, "invalid_parameter_combination"),
    ({"ranges": [{"start_line": 1, "end_line": 2, "name": "X"}, {"start_line": 8, "end_line": 20, "name": "Y"}]}, "line_range_out_of_bounds"),
    ({"ranges": [{"start_line": 1, "end_line": 2, "name": "X"}, {"start_line": 3, "end_line": 4, "name": "Y", "parent_handle": PARENT}]}, "forbidden_parameter"),
    ({"ranges": [{"start_line": 1, "end_line": 2, "name": "X", "ap_id": False}]}, "invalid_parameter_type"),
    ({"ranges": ["bad"]}, "invalid_parameter_type"),
    ({"start_line": 1, "end_line": 1, "name": "X", "mode": "copy"}, "forbidden_parameter"),
])
def test_every_bad_batch_preflights_zero_writes(monkeypatch, tmp_path, extra, code):
    client, capture, _, _, _ = setup(monkeypatch, tmp_path)
    result = dispatch(**extra)
    assert result["success"] is False and result["error"]["code"] == code
    capture.assert_not_awaited()
    client.update_node.assert_not_awaited()


@pytest.mark.parametrize("note,start,end,expected", [
    ("one\ntwo", 1, 2, ""),
    ("a\n\nb\n\nc", 3, 3, "a\n\n\nc"),
    ("a\r\nb\rc", 2, 2, "a\nc"),
    ("a\nb\n\n", 1, 1, "b\n"),
    ("a\n\u00a0\t 📊\nc", 2, 2, "a\nc"),
])
def test_exact_deletion_no_seam_cleanup_and_actual_line_counts(monkeypatch, tmp_path, note, start, end, expected):
    client, _, _, _, _ = setup(monkeypatch, tmp_path, note)
    result = dispatch(start_line=start, end_line=end, name="X")
    assert result["success"] is True
    assert client.update_node.await_args.args[1].note == expected
    assert result["source"]["lines_after"] == len(expected.splitlines())


def test_adjacent_ranges_are_legal(monkeypatch, tmp_path):
    client, _, _, _, _ = setup(monkeypatch, tmp_path)
    result = dispatch(ranges=[{"start_line": 1, "end_line": 2, "name": "X"}, {"start_line": 3, "end_line": 4, "name": "Y"}])
    assert result["success"] and client.update_node.await_args.args[1].note == "five\nsix\nseven\neight\nnine\nten"


@pytest.mark.parametrize("note", ["", "\n"])
def test_empty_text_ranges_refuse_before_capture(monkeypatch, tmp_path, note):
    client, capture, _, _, _ = setup(monkeypatch, tmp_path, note)
    result = dispatch(start_line=1, end_line=1, name="X")
    assert result["error"]["code"] == "line_range_out_of_bounds"
    capture.assert_not_awaited()
    client.update_node.assert_not_awaited()


@pytest.mark.parametrize("bad_result", [
    {"success": False, "root_node_ids": [NEW], "errors": ["failed"]},
    {"success": True}, {"success": True, "uuid": NEW},
    {"success": True, "root_node_ids": []},
    {"success": "true", "root_node_ids": [NEW]},
    {"success": True, "root_node_ids": "not a list"},
    {"success": True, "root_node_ids": [""]},
    {"success": True, "root_node_ids": [SOURCE]},
    {"success": True, "root_node_ids": [NEW, NEW2]},
    {"success": True, "root_node_ids": [NEW], "uuid": NEW2},
    None, True,
])
def test_truthy_but_unverified_capture_never_deletes(monkeypatch, tmp_path, bad_result):
    client, capture, _, _, _ = setup(monkeypatch, tmp_path)
    capture.side_effect = None
    capture.return_value = bad_result
    result = dispatch(start_line=1, end_line=2, name="X")
    assert result["error"]["code"] == "partial_failure_source_unchanged"
    extra = result["error"]["extra"]
    assert extra["partial_failure"]["cause"] == bad_result
    assert extra["source"]["deleted_line_count"] == 0
    client.update_node.assert_not_awaited()


def test_batch_second_capture_failure_preserves_full_ledger_and_stops(monkeypatch, tmp_path):
    client, capture, _, _, _ = setup(monkeypatch, tmp_path)
    bad = {"success": False, "root_node_ids": [], "errors": ["verbatim capture failure"]}
    capture.side_effect = [{"success": True, "root_node_ids": [NEW], "ap_id": "[ap:NEW]"}, bad]
    result = dispatch(ranges=[{"start_line": n, "end_line": n, "name": f"Part {n}"} for n in (1, 3, 5)])
    extra = result["error"]["extra"]
    assert extra["source"]["note_state"] == "not_written_by_extract"
    assert [row["status"] for row in extra["partial_failure"]["ranges"]] == ["created_verified", "capture_unverified", "not_attempted"]
    assert extra["extracted"][0]["uuid"] == NEW
    assert extra["partial_failure"]["cause"] == bad
    assert capture.await_count == 2
    client.update_node.assert_not_awaited()


def test_capture_exception_retained_and_source_untouched(monkeypatch, tmp_path):
    client, capture, _, _, _ = setup(monkeypatch, tmp_path)
    capture.side_effect = server._AnchorOperationError("ap_id_collision", "exact collision", {"test": 1})
    result = dispatch(start_line=1, end_line=2, name="X")
    cause = result["error"]["extra"]["partial_failure"]["cause"]
    assert cause["message"] == "exact collision" and cause["code"] == "ap_id_collision" and cause["extra"] == {"test": 1}
    client.update_node.assert_not_awaited()


def test_update_failure_never_claims_source_unchanged(monkeypatch, tmp_path):
    client, _, _, _, _ = setup(monkeypatch, tmp_path)
    client.update_node.side_effect = RuntimeError("POST accepted but readback failed")
    result = dispatch(start_line=1, end_line=2, name="X")
    assert result["error"]["code"] == "partial_failure_source_unknown"
    extra = result["error"]["extra"]
    assert extra["source"]["note_state"] == "unknown" and extra["source"]["deleted_line_count"] is None
    assert extra["extracted"]["uuid"] == NEW
    assert "node.read" in extra["partial_failure"]["recovery"]
    assert extra["partial_failure"]["cause"]["message"] == "POST accepted but readback failed"
    client.update_node.assert_awaited_once()


def test_cancellation_during_capture_propagates_without_deletion(monkeypatch, tmp_path):
    client, capture, _, _, _ = setup(monkeypatch, tmp_path)
    capture.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        dispatch(start_line=1, end_line=2, name="X")
    client.update_node.assert_not_awaited()


def test_hard_barrier_refuses_before_source_read(monkeypatch, tmp_path):
    client, capture, _, paths, _ = setup(monkeypatch, tmp_path)
    s04_raise_barrier(paths)
    result = dispatch(start_line=1, end_line=2, name="X")
    assert result["error"]["code"] == "coherence_barrier_active"
    client.get_node.assert_not_awaited()
    capture.assert_not_awaited()
    client.update_node.assert_not_awaited()


def test_mid_capture_coherence_refusal_keeps_universal_envelope_and_partial_ledger(monkeypatch, tmp_path):
    from workflowy_mcp.coherence.admission import require_cache_identity_proof
    client, capture, _, paths, _ = setup(monkeypatch, tmp_path)
    calls = []
    async def capturing(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            return {"success": True, "root_node_ids": [NEW]}
        s04_raise_barrier(paths)
        await require_cache_identity_proof(lambda: paths, operation="test identity")
    capture.side_effect = capturing
    result = dispatch(ranges=[{"start_line": 1, "end_line": 1, "name": "X"}, {"start_line": 2, "end_line": 2, "name": "Y"}])
    assert result["error"]["code"] == "coherence_barrier_active"
    assert result["error"]["extra"]["refresh_required"] is True
    assert result["error"]["extra"]["extraction"]["extracted"][0]["uuid"] == NEW
    client.update_node.assert_not_awaited()


def test_no_operation_boundary_fails_closed(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path)
    with pytest.raises(CacheViewError):
        asyncio.run(server._anchor_point_extract_impl(SOURCE, PARENT, start_line=1, end_line=1, name="X"))


def test_display_numbers_120_through_180_are_61_lines_and_exact():
    batch, ranges = normalize_extract_ranges(start_line=120, end_line=180, name="Section")
    note = "\n".join(str(n) for n in range(1, 201))
    planned, replacement, counts = plan_note_extraction(note, ranges)
    assert batch is False and planned[0]["line_count"] == 61
    assert planned[0]["content"] == "\n".join(str(n) for n in range(120, 181))
    assert counts == {"lines_before": 200, "lines_after": 139, "deleted_line_count": 61}
    assert "119\n181" in replacement


def test_extract_registry_unpack_and_result_passthrough(monkeypatch):
    result = {"success": False, "error": {"code": "partial_failure_source_unknown", "message": "read first"}}
    impl = AsyncMock(return_value=result)
    monkeypatch.setattr(server, "_anchor_point_extract_impl", impl)
    params = {"node_handle": SOURCE, "parent_handle": PARENT, "start_line": 1, "end_line": 2, "name": "X"}
    returned = asyncio.run(dispatch_operation(server.build_anchor_registry(), "point.extract", params))
    assert returned == {
        **result,
        "operation_doc": server.build_anchor_registry().describe_full("point.extract"),
    }
    # Only response documentation is added: no truthy mutation wrapper,
    # no changed/detail fabrication, and the original failure stays untouched.
    assert returned["success"] is False and returned["error"] is result["error"]
    assert "operation_doc" not in result
    impl.assert_awaited_once_with(SOURCE, PARENT, start_line=1, end_line=2, name="X", ranges=None, ap_id="auto", tags=None, target_tab=None)


def test_registry_and_top_level_discovery():
    spec = server.ANCHOR_REGISTRY.resolve("point.extract")
    assert spec.side_effects == ("workflowy_mutation", "local_state_mutation")
    assert spec.cache_backed and spec.async_mode == "never"
    assert mutation_admission_class(spec.canonical_name, spec.side_effects, spec.cache_backed) == "refuse_under_hard_dirt"
    full = server.ANCHOR_REGISTRY.describe_full("point.extract")
    assert "UNKNOWN" in full["returns"]["description"]
    for example in full["examples"]:
        validate_params(spec, example["call"]["params"])
    tool = next(tool for tool in asyncio.run(server.mcp.list_tools()) if tool.name == "anchor")
    assert "point.extract —" in tool.description and "1-based inclusive" in tool.description


@pytest.mark.parametrize("batch", [False, True])
def test_real_capture_etch_crud_governor_and_cow_roundtrip(monkeypatch, tmp_path, batch):
    from test_coherence_s03_closure_repairs import _make_lineage_nexus_client
    note = "keep\n\t\u00a0📊 <literal> & text\nlast"
    nodes = [
        {"id": SOURCE, "name": NAME, "note": note, "parent_id": None},
        {"id": PARENT, "name": "[ap:DEST] Destination", "note": "", "parent_id": None},
    ]
    client, paths = _make_lineage_nexus_client(tmp_path, monkeypatch, nodes=nodes)
    old_view = client._cache_view_publisher.capture()
    old_export = copy.deepcopy(old_view.nodes_export)
    wire = {row["id"]: {**row, "note": html.escape(row["note"])} for row in nodes}
    requests = []
    def handler(request):
        requests.append((request.method, request.url.path))
        node_id = request.url.path.rstrip("/").rsplit("/", 1)[-1]
        if request.method == "GET" and node_id in wire:
            return httpx.Response(200, json={"node": wire[node_id]})
        if request.method == "POST" and request.url.path.endswith("/nodes/"):
            payload = json.loads(request.content)
            created_id = NEW if NEW not in wire else NEW2
            wire[created_id] = {"id": created_id, "name": payload["name"], "note": payload.get("note", ""), "parent_id": payload.get("parent_id", payload.get("parentId", PARENT))}
            return httpx.Response(200, json={"item_id": created_id})
        if request.method == "POST" and node_id in wire:
            wire[node_id].update(json.loads(request.content))
            return httpx.Response(200, json={"status": "ok"})
        raise AssertionError(f"Unexpected HTTP request (no cache refresh permitted): {request.method} {request.url}")
    client._client = httpx.AsyncClient(base_url="https://workflowy.com/api/v1", transport=httpx.MockTransport(handler))
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "_rate_limiter", None)
    monkeypatch.setattr(server, "_admission_coherence_paths", lambda: paths)
    monkeypatch.setattr(server, "_ANCHOR_AP_ID_RESERVED_CORES", set())
    monkeypatch.setattr(server, "_ANCHOR_AP_ID_ALLOCATED_CORES", set())
    monkeypatch.setattr(server, "_resolve_slice_group_config", AsyncMock(return_value=([], None, {"auto_expand": "expand"}, [], {})))
    acquire = AsyncMock(wraps=client._write_governor.acquire_write_slot)
    recheck = AsyncMock(wraps=client._write_governor.recheck_before_send)
    monkeypatch.setattr(client._write_governor, "acquire_write_slot", acquire)
    monkeypatch.setattr(client._write_governor, "recheck_before_send", recheck)
    params = {"node_handle": "[ap:SOURCE]", "parent_handle": "[ap:DEST]", "tags": ["topic-x"]}
    params.update(
        {"ranges": [{"start_line": 2, "end_line": 2, "name": "Second"}, {"start_line": 1, "end_line": 1, "name": "First"}]}
        if batch else {"start_line": 2, "end_line": 2, "name": "New section"}
    )
    async def run():
        try:
            return await dispatch_async(params)
        finally:
            await client.close()
    result = asyncio.run(run())
    assert result["success"] is True, result
    extracted = result["extracted"] if batch else [result["extracted"]]
    assert all(row["ap_id"].startswith("[ap:") for row in extracted)
    assert len({row["ap_id"] for row in extracted}) == len(extracted)
    assert server._note_wire_to_semantic(wire[NEW2 if batch else NEW]["note"]) == "\t\u00a0📊 <literal> & text"
    expected_source = "last" if batch else "keep\nlast"
    assert server._note_wire_to_semantic(wire[SOURCE]["note"]) == expected_source
    assert wire[SOURCE]["name"] == NAME
    # Existing capture policy: an extracted leaf has no --expand postfix.
    assert "#topic-x" in wire[NEW]["name"] and "#topic-x--expand" not in wire[NEW]["name"]
    expected_writes = ["create_node", "update_node"] * len(extracted) + ["update_node"]
    assert [call.args[0] for call in acquire.await_args_list] == expected_writes
    assert recheck.await_count == acquire.await_count == len(expected_writes)
    source_requests = [method for method, path in requests if path.endswith(SOURCE)]
    assert source_requests == ["GET", "GET", "POST", "GET"]  # original basis, final preflight, write, readback
    new_view = client._cache_view_publisher.capture()
    assert old_view.nodes_export == old_export  # old published generation never mutated
    assert new_view is not old_view and NEW in new_view.nodes_by_id
    assert new_view.nodes_by_id[SOURCE]["note"] == expected_source
    assert new_view.nodes_by_id[SOURCE]["name"] == NAME
