"""G5's real bulk runner: flat batches, released evidence and exact stops."""
from __future__ import annotations

import asyncio
import gc
import json
from pathlib import Path
import weakref

import pytest

from n07_job_harness import real_job_environment, scrubbed_runtime  # noqa: F401
from test_beacon_integration_contract import _joined_complete_detail
from workflowy_mcp import server as srv, config, beacon_refresh_support as support
from workflowy_mcp.carto_job_record import CartoJobRecord, record_for
from workflowy_mcp.carto_job_runtime import current_carto_job_context
from workflowy_mcp.client import api_client_nexus as acn
from workflowy_mcp.coherence.operation_cache import cache_operation_boundary


class Evidence(dict):
    """JSON data with an observable lifetime, not an external retained owner."""


def _no_tail(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "in_flight":
                continue  # the specified small placeholder is not completed evidence
            assert not (key in {"files", "node_outcomes", "phase8", "findings", "events", "present_paths"}
                        and isinstance(item, list)), (key, item)
            _no_tail(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _no_tail(item)


async def _start(env, children, *, root=None):
    root = root or {"id": env.ids["file"], "name": "source.py", "note": "Path: source.py"}
    async with cache_operation_boundary(env.client, "jr3:bulk-writer-front"):
        ack = await srv._start_carto_bulk_visible_apply_job(
            root_uuid=env.ids["file"], mode="file", visible_tree_payload={"root": root, "children": children})
    assert ack["success"], ack
    return ack


async def _finish(env, ack, *, require_writer=True):
    await asyncio.wait_for(env.tasks[ack["job_id"]], 60)
    assert not env.probe.violations
    record = json.loads(Path(ack["carto_job_file"]).read_text())
    if require_writer:
        assert record["record_layout"] == "summary-head/2"
        assert not {"files", "node_outcomes", "phase8"}.intersection(record["result_summary"])
        _no_tail(env.controls[ack["job_id"]].terminal_result)
    return await _joined_complete_detail(ack["job_id"]), record


@pytest.mark.asyncio
async def test_533_nodes_append_only_their_own_batch_and_release_receipts(real_job_environment, monkeypatch):
    env = real_job_environment
    path = str((env.repo / "source.py").resolve())
    receipts, rows, batch_lengths, sizes = [], [], [], []
    original_nodes, original_row = CartoJobRecord.append_node_rows, CartoJobRecord.append_file_row
    original_merge, original_open = config.merge_write_carto_job_status, Path.open

    def append_nodes(self, values):
        batch_lengths.append(len(values))
        for entry in values:
            probe = Evidence()
            entry["row"]["lifetime_probe"] = probe
            receipts.append(weakref.ref(probe))
        return original_nodes(self, values)

    def append_row(self, row, **kwargs):
        probe = Evidence()
        row["lifetime_probe"] = probe
        rows.append(weakref.ref(probe))
        return original_row(self, row, **kwargs)

    def merged(filename, payload, **kwargs):
        if payload.get("type") == "CARTO_BULK_APPLY":
            _no_tail(payload)
            sizes.append(len(json.dumps(payload, separators=(",", ":")).encode()))
        return original_merge(filename, payload, **kwargs)

    def opened(self, mode="r", *args, **kwargs):
        assert not (self.name in {"files.jsonl", "nodes.jsonl"} and "r" in mode), "run read earlier evidence"
        return original_open(self, mode, *args, **kwargs)

    async def updated(self, **kwargs):
        assert all(ref() is None for ref in receipts), "completed receipt retained into next candidate"
        return {"success": True, "writer_success": True, "operation": "updated_beacon",
                "changed": True, "source_path": path, "receipt_number": len(batch_lengths)}

    async def refreshed(self, **kwargs):
        return {"file": path, "status": "refreshed", "success": True, "changed": False,
                "findings": [], "warnings": [], "reconcile_children": [],
                "projection": {"reconcile_children": []}, "nodes_updated": 0}

    def forbidden(*args, **kwargs):
        raise AssertionError("batch save re-aggregated run rows")

    with monkeypatch.context() as patches:
        patches.setattr(CartoJobRecord, "append_node_rows", append_nodes)
        patches.setattr(CartoJobRecord, "append_file_row", append_row)
        patches.setattr(config, "merge_write_carto_job_status", merged)
        patches.setattr(Path, "open", opened)
        patches.setattr(support, "aggregate_beacon_outcomes", forbidden)
        patches.setattr(support, "beacon_job_outcome", forbidden)
        patches.setattr(acn.WorkFlowyClientNexus, "update_beacon_from_node", updated)
        patches.setattr(acn.WorkFlowyClientNexus, "refresh_file_node_beacons", refreshed)
        candidate = {"id": env.ids["symbol"], "name": "existing #jr3", "note": "AST_QUALNAME: existing"}
        ack = await _start(env, [dict(candidate) for _ in range(533)])
        await asyncio.wait_for(env.tasks[ack["job_id"]], 60)
    gc.collect()
    assert batch_lengths == [1] * 533
    assert all(ref() is None for ref in receipts + rows)
    assert max(sizes) < 16_384
    assert max(sizes[-100:]) <= max(sizes[50:150]) + 1024
    detail, record = await _finish(env, ack)
    summary = detail["result_summary"]
    assert len(summary["node_outcomes"]) == len(summary["files"][0]["node_outcomes"]) == 533
    assert [row["receipt_number"] for row in summary["node_outcomes"]] == list(range(533))
    assert record["evidence"]["nodes"]["rows"] == 533
    row = summary["files"][0]
    assert row["reconcile_children"] == row["projection"]["reconcile_children"]
    assert row["reconcile_children"]["$reconcile_children"]["count"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("own_first", [False, True], ids=["rejection-before-own", "own-before-rejection"])
async def test_cf94_frozen_grouping_uses_every_receipt(real_job_environment, monkeypatch, own_first):
    env = real_job_environment
    path = str((env.repo / "source.py").resolve())
    original = acn.WorkFlowyClientNexus.update_beacon_from_node

    async def written(self, **kwargs):
        if "#reject" in kwargs["name"]:
            return {"success": False, "operation": "noop", "noop_reason": "beacon_id_not_found_on_disk",
                    "source_path": path, "error": "frozen-group rejection", "changed": False}
        return await original(self, **kwargs)

    monkeypatch.setattr(acn.WorkFlowyClientNexus, "update_beacon_from_node", written)
    own = {"id": env.ids["file"], "name": "A source.py" if own_first else "B source.py", "note": "Path: source.py"}
    other = {"id": env.ids["other-file"], "name": "B other.py" if own_first else "A other.py", "note": "Path: other.py"}
    ack = await _start(env, [
        {"id": env.ids["symbol"], "name": "existing #jr3", "note": "AST_QUALNAME: existing"},
        dict(other, children=[{"id": env.ids["symbol"], "name": "existing #reject", "note": "AST_QUALNAME: existing"}]),
    ], root=own)
    detail, record = await _finish(env, ack, require_writer=False)
    row = next(row for row in detail["result_summary"]["files"] if row["file"] == path)
    assert [receipt["success"] for receipt in row["node_outcomes"]] == ([True, False] if own_first else [False, True])
    assert row["status"] == "failed" and row["success"] is False
    assert row["error"] == "frozen-group rejection" and row["changed"] is True
    assert row["node_outcomes"] == detail["result_summary"]["node_outcomes"]
    assert record["record_layout"] == "summary-head/2"


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_after", [0, 1, 2, 3, 4])
async def test_stop_at_every_returned_batch_keeps_rows_and_receipts(real_job_environment, monkeypatch, stop_after):
    env = real_job_environment
    loop = srv._bulk_apply_per_file_inner_loop
    count = 0

    async def stopped(*args, **kwargs):
        nonlocal count
        flush = kwargs["flush_artifacts"]
        control = current_carto_job_context().job_control
        if stop_after == 0:
            control.request_user_cancel()

        async def flushed():
            nonlocal count
            await flush()
            count += 1
            if count == stop_after:
                control.request_user_cancel()
        kwargs["flush_artifacts"] = flushed
        return await loop(*args, **kwargs)

    monkeypatch.setattr(srv, "_bulk_apply_per_file_inner_loop", stopped)
    candidate = {"id": env.ids["symbol"], "name": "existing #jr3", "note": "AST_QUALNAME: existing"}
    ack = await _start(env, [dict(candidate) for _ in range(4)])
    detail, record = await _finish(env, ack)
    assert detail["status"] == "cancelled"
    row = detail["result_summary"]["files"][0]
    assert len(row["node_outcomes"]) == len(detail["result_summary"]["node_outcomes"]) == stop_after
    if stop_after == 0:
        assert row["status"] != "completed"  # CF-137 b6, not a rewrite of completed prefixes
    else:
        assert row["status"] == "completed"
    assert record["in_flight"] is None
    lines = [json.loads(line) for line in (Path(ack["carto_job_file"]).with_suffix("") / "files.jsonl").read_text().splitlines()]
    assert any(line["k"] == "row" for line in lines), "stop must append, not rely only on in_flight"


@pytest.mark.asyncio
async def test_cf137_empty_cancel_is_not_successful_completion(real_job_environment, monkeypatch):
    env = real_job_environment
    quiescent = srv._await_nodes_export_cache_quiescent
    calls = 0

    async def cancelled(*args, **kwargs):
        nonlocal calls
        await quiescent(*args, **kwargs)
        calls += 1
        if calls == 2:
            current_carto_job_context().job_control.request_user_cancel()

    monkeypatch.setattr(srv, "_await_nodes_export_cache_quiescent", cancelled)
    ack = await _start(env, [{"id": env.ids["symbol"], "name": "existing #jr3", "note": "AST_QUALNAME: existing"}])
    detail, record = await _finish(env, ack)
    assert detail["status"] == "cancelled"
    assert record["result_summary"]["success"] is False
    assert record["result_summary"]["cancelled"] is True
    assert detail["result_summary"]["files"] == []
    assert detail["result_summary"]["node_outcomes"] == []


@pytest.mark.asyncio
async def test_restart_at_next_group_head_keeps_only_completed_group(real_job_environment, monkeypatch):
    env = real_job_environment
    original = acn.WorkFlowyClientNexus.refresh_file_node_beacons

    async def refreshed(self, **kwargs):
        result = await original(self, **kwargs)
        current_carto_job_context().job_control.restart_state["restart_id"] = "jr3-boundary"
        return result

    monkeypatch.setattr(acn.WorkFlowyClientNexus, "refresh_file_node_beacons", refreshed)
    ack = await _start(env, [
        {"id": env.ids["symbol"], "name": "existing #jr3", "note": "AST_QUALNAME: existing"},
        {"id": env.ids["other-file"], "name": "Z other.py", "note": "Path: other.py", "children": [
            {"id": env.ids["other-symbol"], "name": "other #jr3", "note": "AST_QUALNAME: other"}]},
    ])
    detail, record = await _finish(env, ack)
    assert detail["status"] == "cancelled"
    assert [row["file"] for row in detail["result_summary"]["files"]] == [str((env.repo / "source.py").resolve())]
    assert record["in_flight"] is None


def test_cleared_bulk_batch_never_recreates_log_or_folder(tmp_path, monkeypatch):
    path = tmp_path / "cleared.json"
    monkeypatch.setattr(config, "_CARTO_CLEARED_JOB_PATHS", {str(path.resolve())})
    batch = [("log", "late bulk log"), ("nodes", [])]
    srv._write_bulk_job_artifacts(str(path), str(path.with_suffix(".log")), batch)
    assert batch == []
    assert not path.exists() and not path.with_suffix(".log").exists() and not path.with_suffix("").exists()


@pytest.mark.asyncio
async def test_empty_bulk_keeps_the_original_unstructured_ack(real_job_environment):
    env = real_job_environment
    ack = await _start(env, [])
    detail, record = await _finish(env, ack)
    expected = {"success": True, "message": "No eligible visible AST/beacon nodes found."}
    assert record["result_summary"] == detail["result_summary"] == expected


@pytest.mark.asyncio
async def test_later_failed_group_appends_warning_delta_without_losing_findings(real_job_environment, monkeypatch):
    env = real_job_environment
    source = str((env.repo / "source.py").resolve())
    root = {"id": env.ids["other-file"], "name": "A other.py", "note": "Path: other.py"}
    original = acn.WorkFlowyClientNexus.refresh_file_node_beacons

    async def refreshed(self, **kwargs):
        if kwargs["file_node_id"] == env.ids["file"]:
            raise ValueError("second group refresh failed")
        return await original(self, **kwargs)

    monkeypatch.setattr(acn.WorkFlowyClientNexus, "refresh_file_node_beacons", refreshed)
    ack = await _start(env, [
        {"id": env.ids["symbol"], "name": "existing #first", "note": "AST_QUALNAME: existing"},
        {"id": env.ids["file"], "name": "B source.py", "note": "Path: source.py", "children": [
            {"id": env.ids["symbol"], "name": "existing #second", "note": "AST_QUALNAME: existing"}]},
    ], root=root)
    detail, record = await _finish(env, ack)
    row = next(row for row in detail["result_summary"]["files"] if row["file"] == source)
    assert row["status"] == "failed" and row["changed"] and len(row["node_outcomes"]) == 2
    assert row["findings"] and row["findings"][0]["code"] == "empty_comment"
    assert row["warnings"][-1] == {"type": "file_refresh_failed", "error": "second group refresh failed"}
    lines = [json.loads(line) for line in (Path(ack["carto_job_file"]).with_suffix("") / "files.jsonl").read_text().splitlines()]
    last = [line for line in lines if line["k"] == "row" and line["row"]["file"] == source][-1]
    assert "findings" not in last["row"]
    assert last["delta"]["unset"] == ["error"]
    assert last["delta"]["extend"]["warnings"] == row["warnings"][-1:]
