"""Serial N07 real-folder responsiveness witness; all budgets are test-only."""

from __future__ import annotations

import asyncio
import gc
import json
import logging
import math
import threading
import time
import traceback

import pytest

from n07_job_harness import scrubbed_runtime, real_job_environment, real_note_edit, reject_raw_job_cancel


ACCOUNT_NODES = 41_864
FILE_COUNT = 128
SYMBOLS_PER_FILE = 64
HEARTBEAT_SECONDS = 0.010
SLOW_CALLBACK_SECONDS = 0.100
WITNESS_HANG_GUARD_SECONDS = 600.0


def _percentile(values, quantile):
    assert values, "witness recorded no samples"
    return sorted(values)[max(0, math.ceil(len(values) * quantile) - 1)]


def _large_folder_data(repo, ids):
    """Fixture disk/graph only; seed cache notes with the REAL production parser.

    Parse the initial bytes, assign independent fixture UUIDs, then change one
    signature/body per file without changing identity or sibling order. The
    scheduled job must discover those real updates itself, at account scale.
    """
    from importlib.util import find_spec
    from n05_region_harness import node_uuid
    from workflowy_mcp.client import api_client_nexus as nexus
    for path in repo.iterdir():
        assert path.is_file(), "fixture must not remove an unexpected directory"
        path.unlink()
    coverage = {"python": 64, "markdown": 64, "javascript": 0, "typescript": 0}
    native = all(find_spec(name) is not None for name in (
        "tree_sitter", "tree_sitter_language_pack", "tree_sitter_javascript", "tree_sitter_typescript"))
    kinds = ["python", "markdown"] * 64
    if native:
        kinds[-2:] = ["javascript", "typescript"]
        coverage.update(python=63, markdown=63, javascript=1, typescript=1)
    coverage["optional_native"] = "installed: actual JS and TS fixtures" if native else "SKIP: existing optional JS/TS parser dependency unavailable"
    rows = [
        {"id": ids["vault"], "name": "Account", "parent_id": None},
        {"id": ids["folder"], "name": "📂 measured repo", "note": f"Root: {repo}", "parent_id": ids["vault"]},
        {"id": ids["outside"], "name": "Outside editable subtree", "note": "baseline", "parent_id": ids["vault"]},
        {"id": ids["outside-file"], "name": "Effects fixture", "note": f"Path: {repo.parent / 'peer.py'}", "parent_id": ids["vault"]},
    ]
    file_ids = []
    def flatten(children, parent, prefix):
        for index, child in enumerate(children):
            node_id = node_uuid(f"n07-scale-{prefix}-{index}")
            rows.append({"id": node_id, "parent_id": parent, "name": child.get("name", ""),
                         "note": child.get("note", ""), "priority": child.get("priority", index),
                         **({"data": child["data"]} if child.get("data") else {})})
            flatten(child.get("children") or (), node_id, f"{prefix}-{index}")
    for number, kind in enumerate(kinds):
        extension = {"python": ".py", "markdown": ".md", "javascript": ".js", "typescript": ".ts"}[kind]
        path = repo / f"fixture{number:03d}{extension}"
        if kind == "python":
            initial = "\n\n".join(f"def symbol{i:03d}():\n    return {i}" for i in range(SYMBOLS_PER_FILE)) + "\n"
            changed = initial.replace("def symbol063():", "def symbol063(value=1):")
        elif kind == "markdown":
            initial = "\n\n".join(f"# Heading{i:03d}\n\nbody {i}" for i in range(SYMBOLS_PER_FILE)) + "\n"
            changed = initial.replace("body 63", "updated body 63")
        else:
            annotation = ": number" if kind == "typescript" else ""
            initial = "\n".join(f"export function symbol{i:03d}(value{annotation}) {{ return value + {i}; }}" for i in range(SYMBOLS_PER_FILE)) + "\n"
            changed = initial.replace(f"symbol063(value{annotation})", f"symbol063(value{annotation} = 1)")
        path.write_text(initial, encoding="utf-8")
        parsed = nexus._prepare_carto_source_file(str(path), str(repo), dry_run=True, claimed=None)
        children = parsed["new_children"]
        assert len(children) == SYMBOLS_PER_FILE, (kind, path.name, [child.get("name") for child in children])
        assert all("parse error" not in str(child.get("name", "")).lower() for child in children)
        file_id = node_uuid(f"n07-scale-file-{number}")
        file_ids.append(file_id)
        rows.append({"id": file_id, "parent_id": ids["folder"], "name": path.name,
                     "note": f"Path: {path.name}\nLINE COUNT: {len(initial.splitlines())}", "priority": number})
        flatten(children, file_id, str(number))
        path.write_text(changed, encoding="utf-8")
    filler_root = node_uuid("n07-scale-unrelated-root")
    rows.append({"id": filler_root, "parent_id": ids["vault"], "name": "Unrelated account data"})
    filler_count = ACCOUNT_NODES - len(rows) - 6
    for index in range(filler_count):
        rows.append({"id": node_uuid(f"n07-scale-filler-{index}"), "parent_id": filler_root,
                     "name": f"Unrelated {index:05d}", "note": "ordinary account note", "priority": index})
    effect_ids = {key: node_uuid("n07-scale-effects-" + key) for key in ("left", "right", "moved", "keep", "obsolete", "other")}
    for key, parent in (("left", ids["outside-file"]), ("right", ids["outside-file"]),
                        ("moved", effect_ids["left"]), ("keep", ids["outside-file"]),
                        ("obsolete", ids["outside-file"]), ("other", effect_ids["right"])):
        rows.append({"id": effect_ids[key], "parent_id": parent, "name": key, "note": "original", "priority": len(rows)})
    assert len(rows) == ACCOUNT_NODES and len(file_ids) == FILE_COUNT
    return rows, file_ids, effect_ids, coverage


async def _outside_calls(env, count, destination, stage):
    from workflowy_mcp.coherence.operation_cache import cache_operation_boundary
    for number in range(count):
        started = time.monotonic()
        async with cache_operation_boundary(env.client, "N07-witness-outside-read"):
            value = await env.server._node_read_impl(env.ids["outside"])
        assert value
        note = f"{stage} outside edit {number}"
        await real_note_edit(env, "outside", note)
        assert env.probe.nodes[env.ids["outside"]]["note"] == note
        assert env.client._cache_view_publisher.capture().nodes_by_id[env.ids["outside"]]["note"] == note
        destination.append(time.monotonic() - started)


async def _after_existing_cooldown(launch, remaining):
    """Test driver honors the unchanged 60s lane; it never disables governance."""
    result = await launch()
    if result.get("error_kind") == "cooldown_active":
        advice = float(result["retry_after"])
        assert advice > 0 and advice + 0.050 < remaining()
        await asyncio.sleep(advice + 0.050)  # fixture scheduling margin only
        result = await launch()
    return result


async def _install_fixture_rows(env, rows):
    """Reseed the SAME authority by a real successor epoch, never equal markers."""
    from uuid import uuid4
    from workflowy_mcp.coherence.schema import read_manifest, manifest_with_updates, write_manifest
    from workflowy_mcp.coherence.snapshot_store import new_snapshot_candidate_path, write_snapshot_candidate
    manifest = read_manifest(env.paths, required=True)
    epoch_number, epoch_id = manifest.epoch_number + 1, uuid4().hex
    candidate = write_snapshot_candidate(new_snapshot_candidate_path(env.path / "snapshots"), {"nodes": rows},
        account_scope_id=manifest.account_scope_id, epoch_number=epoch_number, epoch_id=epoch_id,
        incorporated_through_seq=0, publication_kind="full_refresh")
    write_manifest(env.paths, manifest_with_updates(manifest, epoch_number=epoch_number, epoch_id=epoch_id,
        previous_epoch_id=manifest.epoch_id, base_snapshot_path=candidate), expected_epoch_id=manifest.epoch_id, expected_head_seq=0)
    assert env.client._load_nodes_export_cache_from_disk()
    env.probe.nodes = {row["id"]: {"createdAt": 100, "modifiedAt": 100, "note": "", "priority": 0, **row} for row in rows}


async def _effect_and_threshold_fixture(env, ids, remaining):
    """Real ordered driver and governed CRUD/COW under the public job grant."""
    from workflowy_mcp import carto_job_governance as gov, carto_job_runtime as runtime
    from workflowy_mcp.client.workflowy_move_reconcile import reconcile_tree
    from workflowy_mcp.coherence.operation_cache import cache_operation_boundary, current_operation_cache
    from workflowy_mcp.models import NodeCreateRequest, NodeListRequest, NodeUpdateRequest
    from copy import deepcopy
    root = env.ids["outside-file"]
    source = {"export_root_id": root, "export_root_children_status": "complete", "nodes": [
        {"id": ids["left"], "name": "left", "note": "original", "children": []},
        {"id": ids["right"], "name": "right", "note": "original", "children": [
            {"id": ids["moved"], "name": "moved", "note": "moved and updated", "children": []}]},
        {"id": ids["keep"], "name": "keep", "note": "updated", "children": []},
        {"name": "created fixture", "note": "new", "children": []}],
        "original_ids_seen": [root, *ids.values()], "explicitly_preserved_ids": []}
    async def listed(parent):
        nodes, _ = await env.client.list_nodes(NodeListRequest(parentId=parent))
        return [node.model_dump(exclude_none=True) for node in nodes]
    async def created(run):
        """One plain N04 create run; the real client performs the bare-v1 insert."""
        from workflowy_mcp.client.carto_reconcile_cpu import ReconcileCreateRun, ReconcileCreateResult
        assert isinstance(run, ReconcileCreateRun), "reconcile must hand one plain create run"
        position = "top" if run.placement == "top" else "bottom"
        created_ids = []
        for item in run.items:
            created_ids.append((await env.client.create_node(NodeCreateRequest(
                name=item.get("name"), note=item.get("note"), parent_id=run.parent_uuid,
                position=position), _internal_call=True, fetch_created_node=False)).id)
        return ReconcileCreateResult(tuple(created_ids), tuple(position for _ in created_ids),
                                     outcome="v1_boundary")
    async def updated(node, data):
        await env.client.update_node(node, NodeUpdateRequest(name=data.get("name"), note=data.get("note")))
    for force in (False, True):
        start = len(env.probe.sends)
        async def prepare(payload, selection):
            runtime.current_carto_job_context().job_control.may_have_workflowy_effects = True
            planned_source = await runtime.run_carto_cpu("fixture-effect-plan-copy", deepcopy, source)
            plan = await reconcile_tree(planned_source, root, listed, created, updated, env.client.delete_node,
                env.client.move_node, export_nodes=env.client.export_nodes, import_policy="strict", dry_run=True,
                max_delete_threshold=1, force=force, skip_delete_bulk_export_wait=True, allow_pure_reorder=True)
            assert plan["counts"]["deletes"] == 2 and plan["settings"]["option_b_enabled"]
            if not force:
                assert all(plan["counts"][key] > 0 for key in ("creates", "moves", "updates")), plan
            await reconcile_tree(source, root, listed, created, updated, env.client.delete_node,
                env.client.move_node, export_nodes=env.client.export_nodes, import_policy="strict",
                max_delete_threshold=1, force=force, skip_delete_bulk_export_wait=True, allow_pure_reorder=True)
            return {"success": True}
        async with cache_operation_boundary(env.client, "N07-effects-launch") as caller:
            async def launch():
                return await env.server._start_carto_refresh_job(root, "file", preparation=prepare,
                    frozen_payload={"force": force}, preparation_gate=lambda value: {"proceed": False, "status": "completed"},
                    preparation_input_digest=gov.derive_preparation_input_digest("ast_beacon_write", root, str(force), ""),
                    initial_footprint=gov.compute_footprint(str(env.path / "peer.py"), root, caller.nodes_by_id, kind="file"))
            ack = await _after_existing_cooldown(launch, remaining)
        assert ack["success"], ack
        await asyncio.wait_for(asyncio.shield(env.tasks[ack["job_id"]]), remaining())
        result = env.controls[ack["job_id"]].terminal_result
        if not force:
            # H's threshold is a safe early return, not a thrown error or a new
            # failed-job category. Preserve it; prove its actual send prefix.
            assert result["status"] == "completed", result
            assert ids["obsolete"] in env.probe.nodes and ids["other"] in env.probe.nodes
            assert not any(method == "DELETE" for method, *_ in env.probe.sends[start:])
        else:
            assert result["status"] == "completed", result
    methods = env.probe.sends[start:]
    # The first attempt exercises CREATE/MOVE/UPDATE before its delete guard;
    # the forced second attempt must also execute the original DELETE path.
    assert ids["obsolete"] not in env.probe.nodes and ids["other"] not in env.probe.nodes
    assert env.probe.nodes[ids["moved"]]["parent_id"] == ids["right"]
    assert env.probe.nodes[ids["keep"]]["note"] == "updated"
    assert any(row["name"] == "created fixture" for row in env.probe.nodes.values())
    assert any(method == "DELETE" for method, *_ in methods)


@pytest.mark.timing
@pytest.mark.asyncio
async def test_real_folder_refresh_keeps_reader_and_outside_edit_responsive(real_job_environment, monkeypatch, capsys):
    """128×64, 41,864 nodes, real parse/identity/reconcile/HTTP/COW, serial."""
    from workflowy_mcp import carto_job_runtime as runtime
    from workflowy_mcp.client import api_client_nexus as nexus
    env = real_job_environment
    deadline = time.monotonic() + WITNESS_HANG_GUARD_SECONDS
    def remaining():
        value = deadline - time.monotonic()
        assert value > 0, "N07 whole-witness 600s hang guard exceeded"
        return value
    rows, file_ids, effects, coverage = await runtime.run_carto_cpu("fixture-large-account", _large_folder_data, env.repo, env.ids)
    await _install_fixture_rows(env, rows)
    assert len(env.client._cache_view_publisher.capture().nodes_by_id) == ACCOUNT_NODES
    baseline, during, lateness, slow, publications, collections, windows = [], [], [], [], [], [], []
    task_construction, governor_waits = [], []
    await _outside_calls(env, 20, baseline, "baseline")
    loop = asyncio.get_running_loop()
    previous_debug, previous_slow = loop.get_debug(), loop.slow_callback_duration
    loop.set_debug(True)
    loop.slow_callback_duration = SLOW_CALLBACK_SECONDS
    cpu_entered, send_entered, between_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    cpu_release, send_release, between_release = threading.Event(), asyncio.Event(), asyncio.Event()
    stop_heartbeat = asyncio.Event()
    source = nexus._prepare_carto_source_file
    first_source = threading.Event()
    def gated_source(*args, **kwargs):
        assert threading.get_ident() != env.loop_thread and runtime.current_carto_job_context() is None
        if not first_source.is_set():
            first_source.set()
            loop.call_soon_threadsafe(cpu_entered.set)
            assert cpu_release.wait(WITNESS_HANG_GUARD_SECONDS), "fixture CPU gate not released"
        return source(*args, **kwargs)
    monkeypatch.setattr(nexus, "_prepare_carto_source_file", gated_source)
    async def send_gate(request):
        if runtime.current_carto_job_context() is not None and not send_entered.is_set():
            send_entered.set()
            await send_release.wait()
    env.send_gate = send_gate
    real_file = env.client.refresh_file_node_beacons
    completed = []
    async def file_boundary(*args, **kwargs):
        from workflowy_mcp.mutation_locks import held_mutation_lock_regions
        assert any(region.kind == "subtree" and region.key == env.ids["folder"] for region in held_mutation_lock_regions()), "measured FILE escaped the folder's whole-life grant"
        value = await real_file(*args, **kwargs)
        assert any(region.kind == "subtree" and region.key == env.ids["folder"] for region in held_mutation_lock_regions())
        completed.append(kwargs.get("file_node_id", args[0] if args else None))
        if len(completed) == 1:
            between_entered.set()
            await between_release.wait()
        return value
    monkeypatch.setattr(env.client, "refresh_file_node_beacons", file_boundary)
    original_publish = env.client.publish_local_cache_view
    def published(*args, **kwargs):
        start = time.monotonic()
        try:
            return original_publish(*args, **kwargs)
        finally:
            publications.append((start, time.monotonic(), kwargs.get("reason")))
    monkeypatch.setattr(env.client, "publish_local_cache_view", published)
    original_create_task = loop.create_task
    def create_task(*args, **kwargs):
        start, cpu = time.monotonic(), time.thread_time()
        try:
            return original_create_task(*args, **kwargs)
        finally:
            task_construction.append((start, time.monotonic(), time.thread_time() - cpu))
    monkeypatch.setattr(loop, "create_task", create_task)
    for name in ("acquire_write_slot", "recheck_before_send"):
        original = getattr(env.client._write_governor, name)
        async def governor_phase(*args, _original=original, _name=name, **kwargs):
            start, cpu = time.monotonic(), time.thread_time()
            try:
                return await _original(*args, **kwargs)
            finally:
                governor_waits.append((start, time.monotonic(), _name, time.thread_time() - cpu))
        monkeypatch.setattr(env.client._write_governor, name, governor_phase)
    gc_started = {}
    def gc_callback(phase, info):
        thread = threading.get_ident()
        if phase == "start":
            gc_started[thread] = time.monotonic()
        elif thread in gc_started:
            collections.append((gc_started.pop(thread), time.monotonic(), thread))
    gc.callbacks.append(gc_callback)
    class SlowCallbacks(logging.Handler):
        def emit(self, record):
            if not record.getMessage().startswith("Executing "):
                return
            ended = time.monotonic()
            duration = float(record.args[-1])
            handle = getattr(loop, "_current_handle", None)
            owner = getattr(getattr(handle, "_callback", None), "__self__", None)
            context = None
            for key, task in env.tasks.items():
                if task is owner:
                    context = env.controls[key]
                    break
            frames = []
            coroutine = owner.get_coro() if isinstance(owner, asyncio.Task) else None
            seen = set()
            while coroutine is not None and id(coroutine) not in seen:
                seen.add(id(coroutine))
                frame = getattr(coroutine, "cr_frame", None) or getattr(coroutine, "gi_frame", None)
                if frame is not None:
                    frames.append(frame)
                coroutine = getattr(coroutine, "cr_await", None) or getattr(coroutine, "gi_yieldfrom", None)
            slow.append({"started": ended - duration, "ended": ended, "duration_s": duration,
                         "phase": context.phase if context else "fixture/coordinator/loop callback",
                         "task": owner.get_name() if isinstance(owner, asyncio.Task) else None,
                         "message": record.getMessage(),
                         "stack": [line for frame in frames for line in traceback.format_stack(frame)]})
    logger, handler = logging.getLogger("asyncio"), SlowCallbacks()
    logger.addHandler(handler)
    async def heartbeat():
        expected = loop.time() + HEARTBEAT_SECONDS
        while not stop_heartbeat.is_set():
            await asyncio.sleep(max(0.0, expected - loop.time()))
            now = loop.time()
            lateness.append((time.monotonic(), max(0.0, now - expected)))
            expected = now + HEARTBEAT_SECONDS
    beat = asyncio.create_task(heartbeat(), name="N07-witness-heartbeat")
    task, elapsed, second_elapsed, error = None, None, None, None
    async def gate(event):
        waiter = asyncio.create_task(event.wait())
        try:
            done, _ = await asyncio.wait([waiter, task], timeout=remaining(), return_when=asyncio.FIRST_COMPLETED)
            assert waiter in done, env.controls[next(key for key, value in env.tasks.items() if value is task)].terminal_result
        finally:
            if not waiter.done():
                waiter.cancel()  # Fixture notification, never the actual job.
            await asyncio.gather(waiter, return_exceptions=True)
    try:
        with reject_raw_job_cancel(env):
            await asyncio.sleep(0)
            started = time.monotonic()
            ack = await env.server._start_carto_refresh_job(env.ids["folder"], "folder", extra_job_fields={"force": True})
            assert ack["success"], ack
            task = env.tasks[ack["job_id"]]
            for event, release, stage in ((cpu_entered, cpu_release, "cpu"), (send_entered, send_release, "send"), (between_entered, between_release, "between-files")):
                await gate(event)
                await _outside_calls(env, 7, during, stage)
                assert not task.done() and not release.is_set(), "outside call waited for the unrelated job gate"
                release.set()
            await asyncio.wait_for(asyncio.shield(task), remaining())
            elapsed = time.monotonic() - started
            windows.append((started, time.monotonic(), "primary-folder"))
            terminal = env.controls[ack["job_id"]].terminal_result
            assert terminal["status"] in {"completed", "completed_with_warnings"}, terminal
            assert terminal["result_summary"]["files_refreshed"] == FILE_COUNT and len(completed) == FILE_COUNT
            assert len(env.probe.sends) >= FILE_COUNT + 41, "scale fixture did not perform real mapped updates"
            writes = len(env.probe.sends)
            async def launch_second():
                return await env.server._start_carto_refresh_job(env.ids["folder"], "folder", extra_job_fields={"force": True})
            second = await _after_existing_cooldown(launch_second, remaining)
            assert second["success"], second
            second_started = time.monotonic()
            await asyncio.wait_for(asyncio.shield(env.tasks[second["job_id"]]), remaining())
            second_elapsed = time.monotonic() - second_started
            windows.append((second_started, time.monotonic(), "unchanged-folder"))
            assert env.controls[second["job_id"]].terminal_result["status"] in {"completed", "completed_with_warnings"}
            assert len(env.probe.sends) == writes, "unchanged second folder plan still mutated upstream"
            await _effect_and_threshold_fixture(env, effects, remaining)
    except BaseException as exc:
        error = exc
    finally:
        cpu_release.set()
        send_release.set()
        between_release.set()
        for key, job in env.tasks.items():
            if not job.done():
                env.controls[key].request_user_cancel()
        if env.tasks:
            await asyncio.gather(*env.tasks.values())  # Always join actual work, never Task.cancel.
        stop_heartbeat.set()
        await beat
        logger.removeHandler(handler)
        gc.callbacks.remove(gc_callback)
        loop.slow_callback_duration = previous_slow
        loop.set_debug(previous_debug)
    for record in slow:
        start, end, duration = record["started"], record["ended"], record["duration_s"]
        overlap = lambda span: max(0.0, min(end, span[1]) - max(start, span[0]))
        if any(overlap(span) >= duration * 0.5 for span in publications):
            record["attribution"] = "N01 synchronous COW publication"
        elif any(overlap(span) >= duration * 0.5 for span in collections):
            record["attribution"] = "measured GC pause"
        elif any(overlap(span) >= duration * 0.5 for span in task_construction):
            record["attribution"] = "fixture asyncio-debug Task construction"
        else:
            record["attribution"] = "UNEXPLAINED"
        record["overlapping_governor_phases"] = [{"phase": span[2], "wall_s": span[1] - span[0], "loop_thread_cpu_s": span[3]}
                                                for span in governor_waits if overlap(span) >= duration * 0.5]
        record["overlapping_task_construction"] = [{"wall_s": span[1] - span[0], "loop_thread_cpu_s": span[2]}
                                                  for span in task_construction if overlap(span) >= duration * 0.5]
    # Idle cooldown waits are NOT latency samples that could dilute a job's
    # percentile. The two actual folder lifetimes are measured independently.
    measured_lateness = [value for stamp, value in lateness if any(start <= stamp <= end for start, end, _ in windows)]
    if not measured_lateness:  # retain early-failure diagnostics too
        measured_lateness = [value for _, value in lateness]
    report = {"account_nodes": ACCOUNT_NODES, "files": FILE_COUNT, "symbols_per_file": SYMBOLS_PER_FILE,
              "governor_fixture_interval_s": 0.001, "native_parser_coverage": coverage,
              "job_elapsed_s": elapsed, "unchanged_plan_elapsed_s": second_elapsed,
              "heartbeat_samples": len(measured_lateness), "heartbeat_p99_s": _percentile(measured_lateness, 0.99), "heartbeat_max_s": max(measured_lateness),
              "job_windows": [{"kind": kind, "duration_s": end - start,
                  "p99_s": _percentile([value for stamp, value in lateness if start <= stamp <= end], 0.99),
                  "max_s": max(value for stamp, value in lateness if start <= stamp <= end)} for start, end, kind in windows],
              "outside_baseline_calls": len(baseline), "outside_in_job_calls": len(during),
              "outside_p95_baseline_s": _percentile(baseline, 0.95), "outside_p95_in_job_s": _percentile(during, 0.95) if during else None,
              "slow_callbacks": slow, "error": str(error) if error else None}
    with capsys.disabled():
        print("N07_RESPONSIVENESS " + json.dumps(report, ensure_ascii=False, default=str))
    (env.path / "n07-responsiveness.json").write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    if error is not None:
        raise error
    assert not env.probe.violations
    assert report["heartbeat_p99_s"] <= 0.100 and report["heartbeat_max_s"] <= 3.000, report
    assert all(window["p99_s"] <= 0.100 and window["max_s"] <= 3.000 for window in report["job_windows"]), report
    assert report["outside_p95_in_job_s"] <= max(1.000, 5 * report["outside_p95_baseline_s"]), report
    assert all(record["attribution"] != "UNEXPLAINED" for record in slow), report
