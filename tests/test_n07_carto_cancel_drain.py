"""N07 real-job cancellation, failure isolation, token cleanup and drain witnesses."""

from __future__ import annotations

import asyncio
import inspect
import os
import textwrap

import pytest

from n07_job_harness import ThreadGate, job_context, scrubbed_runtime, task_core_environment, claimed_task_environment, real_job_environment, launch_real_job, real_note_edit, wait_job_gate, reject_raw_job_cancel
from workflowy_mcp import carto_job_runtime as runtime


def _probe(name: str) -> bool:
    """Named, env-only test-local neutralization (off in every normal run)."""
    return bool(os.environ.get(name))


def _source_copy(module, name: str, replacements):
    """Compile an in-memory mutant of one production function; disk is untouched.

    The copy carries the live module globals (including the test's monkeypatches)
    and applies exactly the named seams, so a failing test is rejecting the
    specified rule rather than an unrelated import/shim error.
    """
    source = textwrap.dedent(inspect.getsource(getattr(module, name)))
    for old, new in replacements:
        assert old in source, f"{module.__name__}.{name} no longer contains the mutation seam"
        source = source.replace(old, new, 1)
    namespace = dict(vars(module))
    exec(compile(source, getattr(module, "__file__", name), "exec"), namespace)
    return namespace[name]


def _release_current_carto_grant():
    """Mutant: release the actual outer grant before the exit door finalizes."""
    from workflowy_mcp import mutation_locks as locks
    context = runtime.current_carto_job_context()
    if context is None:
        return None
    for token, hold in tuple(locks._LOCKS.holders.items()):
        metadata = hold.governance
        if metadata is not None and metadata.claim_id == context.claim_id:
            locks._release_region_hold(token, hold.state)
            return metadata
    return None


@pytest.mark.asyncio
@pytest.mark.parametrize("point", ["parse", "planning", "prepare", "send", "phase8", "system-exit", "keyboard-interrupt"])
async def test_phase_exception_terminalizes_only_its_job(real_job_environment, monkeypatch, point):
    """D05: fail actual phases, not a replacement scheduler/work/finalizer.

    Keep the failing phase outstanding while a disjoint real FILE job and an
    interactive read/edit complete on the same client. Then join its exact
    future, inspect the terminal row/proof and observe the actual exit order.
    The two BaseException cases are job-origin fake phase faults, not signals.
    """
    import json
    import threading
    from workflowy_mcp import carto_job_governance as gov, markdown_generate_pipeline as pipeline
    from workflowy_mcp.client import api_client_nexus as nexus, workflowy_move_reconcile as driver
    from workflowy_mcp.coherence.operation_cache import cache_operation_boundary, current_operation_cache
    from workflowy_mcp.mutation_locks import _governance_holds, registry_held_regions
    env = real_job_environment
    entered, release = asyncio.Event(), asyncio.Event()
    disk_release, injected = threading.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    unhandled, faults = [], []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
    marker = f"D05 {point} job-only failure"
    exception_type = SystemExit if point == "system-exit" else KeyboardInterrupt if point == "keyboard-interrupt" else RuntimeError

    def bad_data(function):
        def wrapped(*args, **kwargs):
            if not injected.is_set():
                injected.set()
                loop.call_soon_threadsafe(entered.set)
                assert disk_release.wait(10), "fixture did not release failing data phase"
                raise exception_type(marker)
            return function(*args, **kwargs)
        return wrapped
    if point == "parse":
        monkeypatch.setattr(nexus, "_prepare_carto_source_file", bad_data(nexus._prepare_carto_source_file))
    elif point == "planning":
        monkeypatch.setattr(nexus, "_advance_carto_file_plan", bad_data(nexus._advance_carto_file_plan))
    elif point == "prepare":
        monkeypatch.setattr(pipeline, "_write_generated_markdown", bad_data(pipeline._write_generated_markdown))
    elif point == "phase8":
        monkeypatch.setattr(driver, "_advance_reconcile_steps", bad_data(driver._advance_reconcile_steps))
    elif point == "send":
        async def failing_send(request):
            if not injected.is_set():
                injected.set()
                env.trace("phase-fault", point)
                faults.append(runtime.current_carto_job_context().job_control.phase)
                entered.set()
                await release.wait()
                raise exception_type(marker)
        env.send_gate = failing_send
    else:
        original_phase8 = pipeline._run_phase8_refresh_inline
        async def failing_phase8(*args, **kwargs):
            value = await original_phase8(*args, **kwargs)
            assert value["status"] == "completed" and env.probe.sends, "phase8 did not first apply real work"
            injected.set()
            env.trace("phase-fault", point)
            faults.append(runtime.current_carto_job_context().job_control.phase)
            entered.set()
            await release.wait()
            raise exception_type(marker)
        monkeypatch.setattr(pipeline, "_run_phase8_refresh_inline", failing_phase8)

    try:
        with reject_raw_job_cancel(env):
            ack = await launch_real_job(env, "persistent_new" if point == "prepare" else
                                        "markdown" if point in {"phase8", "system-exit", "keyboard-interrupt"} else "file")
            assert ack["success"], ack
            job_id, task = ack["job_id"], env.tasks[ack["job_id"]]
            control = env.controls[job_id]
            try:
                await wait_job_gate(env, entered, job_id=job_id)
                exact_future = control.cpu_future
                if point in {"parse", "planning", "prepare", "phase8"}:
                    assert exact_future is not None and not exact_future.done()
                if point == "phase8":
                    assert any(event["job"] == job_id and event["kind"] == "cache-after" for event in env.events)
                assert not task.done() and registry_held_regions().regions
                peer = await launch_real_job(env, "peer")
                assert peer["success"], peer
                await env.tasks[peer["job_id"]]
                assert env.controls[peer["job_id"]].terminal_result["status"] == "completed"
                async with cache_operation_boundary(env.client, "D05-unrelated-read"):
                    read = await env.server._node_read_impl(env.ids["outside"])
                assert read and not task.done()
                edit = await real_note_edit(env, "outside", marker)
                edit = json.loads(edit)  # return_diff=false is the header-only report
                assert edit["node_id"] == env.ids["outside"] and env.probe.nodes[env.ids["outside"]]["note"] == marker
                assert not task.done(), "peer progress depended on releasing the failed job's phase"
            finally:
                disk_release.set()
                release.set()
                await task
                await asyncio.sleep(0)
        terminal = control.terminal_result
        assert terminal["status"] == "failed", terminal
        assert terminal["error_kind"] == "carto_job_exception" and terminal["error_extra"]["job_id"] == job_id
        extra = terminal["error_extra"]
        expected_phase = {"parse": "file:source", "planning": "file:plan", "prepare": "materialize-write",
                          "phase8": "reconcile:advance"}.get(point)
        assert extra["phase"] == (expected_phase or faults[0]), terminal
        # FILE's established public primitive wraps ordinary failures in a
        # NetworkError. Preserve that actual escaping type, while separately
        # checking the original CPU failure latch and its precise phase.
        expected_type = "NetworkError" if point in {"parse", "planning", "send"} else exception_type.__name__
        assert extra["exception_type"] == expected_type, terminal
        assert extra["may_have_workflowy_effects"] is (point != "prepare")
        assert terminal["cache_refresh_required"] is (point != "prepare")
        assert terminal["error"].startswith(f"Cartographer job {job_id} failed during {extra['phase']}: {expected_type}: ")
        assert terminal["error"].endswith(". The daemon is still serving; inspect the job result before retrying.")
        if expected_type != "NetworkError":
            assert marker in terminal["error"]
        if expected_phase:
            assert control.cpu_failure[0] == expected_phase and type(control.cpu_failure[1]) is RuntimeError
            assert marker in str(control.cpu_failure[1])
        assert control.cpu_future is None and (exact_future is None or exact_future.done())
        persisted = json.loads(next(path for path in env.jobs.glob("*.json") if json.loads(path.read_text()).get("id") == job_id).read_text())
        assert persisted["status"] == "failed" and persisted["error_extra"] == extra
        events = [event for event in env.events if event["job"] == job_id]
        assert [event["kind"] for event in events[-3:]] == ["finalizer-enter", "finalizer-exit", "grant-before-release"]
        assert len([event for event in events if event["kind"] == "finalizer-enter"]) == 1
        assert all(event["held"] and event["task"] is task for event in events if event["kind"].startswith("finalizer"))
        assert not [hold for hold in _governance_holds(env.jobs) if hold["job_id"] == job_id]
        assert not registry_held_regions().regions and not env.probe.violations
        assert not env.server._carto_operation_jobs and not env.controller._busy()
        assert runtime.current_carto_job_context() is None and current_operation_cache(required=False) is None
        assert not unhandled, "job exception escaped into the daemon loop"
        assert task.exception() is None, "job exception escaped the runner's BaseException boundary"
    finally:
        disk_release.set()
        release.set()
        loop.set_exception_handler(previous_handler)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["status-false", "status-raise", "normalization", "projection"])
async def test_finalization_failures_preserve_primary_and_cleanup_authority(real_job_environment, monkeypatch, failure):
    """D06: one exit door keeps the primary failure and finalizes memory exactly once.

    Status/normalization/projection failures run under the real task's grant; the
    retired tombstone/unlink/supervisor arms have no subject, so only failures
    that can still occur are exercised. Finalization is memory-only and happens
    before the outer grant release.
    """
    import json
    import threading
    from pathlib import Path
    from workflowy_mcp import carto_job_governance as gov, markdown_generate_pipeline as pipeline, config
    from workflowy_mcp import carto_job_runtime as job_runtime
    from workflowy_mcp import mutation_locks as locks
    from workflowy_mcp.coherence.operation_cache import current_operation_cache
    from workflowy_mcp.mutation_locks import _governance_holds, held_mutation_lock_regions, registry_held_regions
    env = real_job_environment
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    disk_release = threading.Event()
    unhandled, normalizations, finalize_calls = [], [], []
    old_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
    marker = "D06 original preparation failure"
    def failing_disk(*args, **kwargs):
        loop.call_soon_threadsafe(entered.set)
        assert disk_release.wait(10), "fixture did not release primary preparation failure"
        raise RuntimeError(marker)
    monkeypatch.setattr(pipeline, "_write_generated_markdown", failing_disk)
    normalize = env.server._carto_task_terminal_payload
    async def normalized(*args, **kwargs):
        if failure == "normalization":
            raise RuntimeError("fixture terminal normalization failure")
        value = await normalize(*args, **kwargs)
        normalizations.append(dict(value))
        return value
    monkeypatch.setattr(env.server, "_carto_task_terminal_payload", normalized)
    if failure == "projection":
        cpu = env.server.run_carto_cpu
        def frozen(*args, **kwargs):
            raise RuntimeError("fixture terminal projection failure")
        async def projection_failure(phase, function, *args, **kwargs):
            return await cpu(phase, frozen if phase == "terminal-projection" else function,
                             *args, **kwargs)
        monkeypatch.setattr(env.server, "run_carto_cpu", projection_failure)
    finalize = env.server.finalize_claim_async
    terminal_status_failed = []
    async def observed_finalize(*args, **kwargs):
        if _probe("CUT2B_NEUTRALIZE_STATUS_RELEASE") and terminal_status_failed:
            # Treat the failed terminal presentation write as release authority:
            # no memory finalization is attempted.
            return True
        finalize_calls.append((args, kwargs))
        assert held_mutation_lock_regions(), "memory finalization left the whole-life grant"
        if _probe("CUT2B_NEUTRALIZE_FINALIZE_RELEASE_ORDER"):
            # Release the actual outer grant before finalizing the same entry,
            # so the next assertion observes the retired release-before-finalize
            # order rather than memory finalization under the held grant.
            _release_current_carto_grant()
        if _probe("CUT2B_NEUTRALIZE_COOLDOWN"):
            # Keep finalization but skip the max-merged memory cooldown record.
            kwargs["write_tombstone"] = False
        return await finalize(*args, **kwargs)
    monkeypatch.setattr(env.server, "finalize_claim_async", observed_finalize)
    original_writer = config.merge_write_carto_job_status
    job_file = None
    def terminal_writer(path, payload, **kwargs):
        if job_file is not None and str(path) == job_file and payload.get("status") == "failed":
            if failure == "status-false":
                terminal_status_failed.append(True)
                return False
            if failure == "status-raise":
                terminal_status_failed.append(True)
                raise OSError("D06 terminal row is read-only")
        return original_writer(path, payload, **kwargs)
    # G4's registered writer imports the same funnel from config; legacy kinds
    # retain the server alias. Inject at both references, never bypass the test.
    monkeypatch.setattr(config, "merge_write_carto_job_status", terminal_writer)
    monkeypatch.setattr(env.server, "merge_write_carto_job_status", terminal_writer)
    if _probe("CUT2B_NEUTRALIZE_PRIMARY_ERROR_MASK") and failure == "normalization":
        # A secondary normalization failure replaces the primary error text.
        monkeypatch.setattr(env.server, "_finalize_carto_task", _source_copy(
            env.server, "_finalize_carto_task", [(
                '"error": f"Cartographer job {spec.job_id} failed during {control.phase}: '
                '{type(normalization_error).__name__}: {normalization_error}. '
                'The daemon is still serving; inspect the job result before retrying.",',
                '"error": "coherence_barrier_active: masked secondary failure",',
            )]))
    if _probe("CUT2B_NEUTRALIZE_PRIMARY_ERROR_MASK") and failure == "projection":
        # The projection fallback drops the primary work error in favour of the
        # secondary projection error.
        monkeypatch.setattr(env.server, "_finalize_carto_task", _source_copy(
            env.server, "_finalize_carto_task", [(
                '"error": str(payload.get("error") or projection_error),',
                '"error": str(projection_error),',
            )]))
    task = None
    try:
        with reject_raw_job_cancel(env):
            ack = await launch_real_job(env, "persistent_new")
            assert ack["success"], ack
            job_id, task = ack["job_id"], env.tasks[ack["job_id"]]
            control = env.controls[job_id]
            await wait_job_gate(env, entered, job_id=job_id)
            job_file = control.presentation["path"]
            exact_future = control.cpu_future
            assert exact_future is not None and not exact_future.done()
            disk_release.set()
            await task
            await asyncio.sleep(0)
        terminal = control.terminal_result
        assert terminal["status"] == "failed" and terminal["error_kind"] == "carto_job_exception"
        if failure == "normalization":
            assert "fixture terminal normalization failure" in terminal["error"]
            assert not normalizations and terminal["error_extra"]["job_id"] == job_id
        else:
            assert marker in terminal["error"], terminal
            assert len(normalizations) == 1
            assert terminal["error_extra"] == normalizations[0]["error_extra"]
        assert len(finalize_calls) == 1, "the one exit door must finalize memory exactly once"
        assert control.cpu_future is None and exact_future.done() and not exact_future.cancelled()
        events = [event for event in env.events if event["job"] == job_id]
        assert len([event for event in events if event["kind"] == "finalizer-enter"]) == 1
        assert [event["kind"] for event in events][-2:] == ["finalizer-exit", "grant-before-release"]
        assert all(event["held"] and event["task"] is task for event in events if event["kind"].startswith("finalizer"))
        assert not registry_held_regions().regions and not env.server._carto_operation_jobs
        assert _governance_holds(env.jobs) == (), "finalization left a registry reservation"
        cooldowns = [record for record in locks._LOCKS.cooldowns.values() if record.get("job_id") == job_id]
        assert len(cooldowns) == 1 and cooldowns[0]["last_terminal_at"], "finalization published no memory cooldown"
        # Terminal memory overrides a failed durable write after control removal.
        status = await env.server._system_job_status_impl(job_id=job_id)
        assert status["status"] == "failed"
        if failure == "normalization":
            assert "fixture terminal normalization failure" in status["error"]
        else:
            assert marker in status["error"], status
        if failure.startswith("status-"):
            assert terminal["status_write_error"]
            assert json.loads(Path(job_file).read_text())["status"] != "failed"
        else:
            assert "status_write_error" not in terminal
            assert json.loads(Path(job_file).read_text())["status"] == "failed"
        await real_note_edit(env, "persistent", "graph editable after failed finalization")
        await real_note_edit(env, "outside", "daemon still serves after finalization failure")
        assert env.probe.nodes[env.ids["persistent"]]["note"] == "graph editable after failed finalization"
        assert not env.probe.violations and not unhandled and task.exception() is None
        assert not env.controller._busy()
    finally:
        disk_release.set()
        if task is not None:
            await task
        loop.set_exception_handler(old_handler)


_D01_POINTS = (
    "before-freshness", "after-freshness", "preparation", "after-preparation", "body-complete",
    "parse", "planning", *(f"reconcile-{phase}" for phase in range(6)),
    "folder-between", "bulk-between", "phase-2-resolve-path", "phase-5-re-export",
    "phase-6-write-markdown", "phase-7-reapply-beacons",
    "phase8-refresh", "send", "publication",
)


@pytest.mark.asyncio
@pytest.mark.parametrize("point", _D01_POINTS)
async def test_user_cancel_at_safe_points_never_interrupts_send(real_job_environment, monkeypatch, point):
    """D01: real jobs, named phase/decision heads and an actual HTTP/COW await.

    Inject the actual registered request delivery (with its real sentinel
    prerequisite) at each point. D02/D07 separately exercise the public cancel
    call. Nothing replaces parser/planner/reconcile/CRUD work with a sleep.
    """
    import json
    from workflowy_mcp import carto_job_governance as gov, markdown_generate_pipeline as pipeline
    from workflowy_mcp.client import api_client_nexus as nexus, workflowy_move_reconcile as driver
    from workflowy_mcp.mutation_locks import registry_held_regions
    env = real_job_environment
    entered, release = asyncio.Event(), asyncio.Event()
    requests, checkpoints = [], []
    checkpoint = runtime.CartoJobControl.checkpoint
    original_cpu = runtime.run_carto_cpu
    def request():
        if requests:
            return
        context = runtime.current_carto_job_context()
        job_id = context.job_control.presentation["id"]
        env.trace("user-request", point)
        requests.append(job_id)
        context.job_control.request_user_cancel()
        entered.set()
    def observed_checkpoint(control, phase, **kwargs):
        checkpoints.append(phase)
        selected = phase == point
        if point == "folder-between":
            selected = phase == "folder:file-head" and checkpoints.count(phase) == 2
        if point == "bulk-between":
            # Bulk also checks once during preflight, before its first group.
            selected = phase == "bulk-file-group" and checkpoints.count(phase) == 3
        if selected and not control.finalizing:
            request()
        return checkpoint(control, phase, **kwargs)
    monkeypatch.setattr(runtime.CartoJobControl, "checkpoint", observed_checkpoint)
    async def observed_cpu(phase, function, *args, **kwargs):
        if (point == "parse" and phase == "file:source") or (point == "planning" and phase == "file:plan"):
            request()
        value = await original_cpu(phase, function, *args, **kwargs)
        if phase == "reconcile:advance" and point.startswith("reconcile-") and not requests:
            marker = f"[PHASE {point.rsplit('-', 1)[1]}]"
            if any(kind == "log" and marker in str(text) for kind, text, _ in value.diagnostics):
                request()
        return value
    for module in (env.server, nexus, pipeline, driver):
        monkeypatch.setattr(module, "run_carto_cpu", observed_cpu)
    async def send_gate(http_request):
        if point == "send" and not requests:
            request()
            await release.wait()
    env.send_gate = send_gate
    if point == "publication":
        env.patch_gate = lambda *args: request()
    family = ("folder" if point == "folder-between" else "bulk-groups" if point == "bulk-between" else
              "markdown" if point.startswith("phase") else
              "ast" if point in {"before-freshness", "after-freshness", "preparation", "after-preparation"} else "file")
    with reject_raw_job_cancel(env):
        ack = await launch_real_job(env, family)
        assert ack["success"], ack
        job_id, task = ack["job_id"], env.tasks[ack["job_id"]]
        control = env.controls[job_id]
        try:
            await wait_job_gate(env, entered, job_id=job_id)
            if point == "send":
                assert control.user_requested and not control.user_observed and not task.done()
                assert not [event for event in env.events if event["job"] == job_id and event["kind"] in {"cache-after", "finalizer-enter"}]
                await real_note_edit(env, "outside", "edit completed before cancelled send released")
                assert not task.done()
        finally:
            release.set()
            await task
    assert requests == [job_id], f"required safe point was not reached: {point}"
    assert control.user_observed and control.terminal_result["status"] == "cancelled", json.dumps(control.terminal_result, default=str)
    assert json.loads(next(env.jobs.glob("*.json")).read_text())["status"] == "cancelled"
    assert control.cpu_future is None and not registry_held_regions().regions
    assert not env.probe.violations
    events = [event for event in env.events if event["job"] == job_id]
    stop = next(index for index, event in enumerate(events) if event["kind"] == "user-request")
    assert not any(event["kind"] == "send" for event in events[stop + 1:]), "a send followed observed stop delivery"
    assert len([event for event in events if event["kind"] == "finalizer-enter"]) == 1
    if point == "before-freshness":
        assert not any(event["kind"] == "freshness" for event in events), "B0 ran after its safe stop point"
    if point in {"send", "publication"}:
        assert any(event["kind"] == "cache-after" for event in events)
        assert "LINE COUNT:" in env.client._cache_view_publisher.capture().nodes_by_id[env.ids["file"]]["note"]
    if point in {"folder-between", "bulk-between"}:
        assert control.terminal_result["result_summary"]["files_refreshed"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("family", ["file", "folder", "bulk-groups", "markdown", "phase8"])
@pytest.mark.parametrize("abandon", [False, True])
async def test_restart_stops_each_job_kind_at_registered_boundary(real_job_environment, monkeypatch, family, abandon):
    """D03: real N06 registration, complete real work, no user sentinel/probe."""
    from j_counterfactual import install_j_counterfactual
    install_j_counterfactual(monkeypatch)
    import json
    import threading
    from workflowy_mcp import markdown_generate_pipeline as pipeline
    env = real_job_environment
    entered, release = asyncio.Event(), asyncio.Event()
    disk_release = threading.Event()
    loop = asyncio.get_running_loop()
    if family == "markdown":
        original = pipeline._write_generated_markdown
        def write(path, text=None, **kwargs):
            if kwargs.get("operation", "write") in {"write", "publish"}:
                loop.call_soon_threadsafe(entered.set)
                assert disk_release.wait(10), "fixture did not release the Markdown publication"
            return original(path, text, **kwargs)
        monkeypatch.setattr(pipeline, "_write_generated_markdown", write)
    else:
        async def send_gate(request):
            if not entered.is_set():
                entered.set()
                await release.wait()
        env.send_gate = send_gate
    with reject_raw_job_cancel(env):
        ack = await launch_real_job(env, "markdown" if family == "phase8" else family)
        assert ack["success"], ack
        job_id, task = ack["job_id"], env.tasks[ack["job_id"]]
        control = env.controls[job_id]
        stop, registered = env.controller._job_stops[job_id]
        assert registered is task
        try:
            await wait_job_gate(env, entered, job_id=job_id)
            stop("D03-restart")
            assert control.restart_state["restart_id"] == "D03-restart" and not control.restart_state["observed"]
            assert control.user_cancel_requested() is False and not control.user_requested
            assert not list(env.jobs.glob(".cancel_requests/*.cancel")), "restart created a user sentinel"
            if abandon:
                stop(None)
                assert control.restart_state["restart_id"] is None
        finally:
            release.set()
            disk_release.set()
            await task
    terminal = control.terminal_result
    expected = ({"completed_with_warnings"} if family == "folder" else {"completed", "completed_with_warnings"}) if abandon else {"cancelled"}
    assert terminal["status"] in expected, json.dumps(terminal, default=str)
    assert control.restart_state["observed"] is (not abandon)
    assert not control.user_requested and not control.user_observed and control.cpu_future is None
    assert not list(env.jobs.glob(".cancel_requests/*.cancel")) and not env.probe.violations
    if family == "markdown":
        assert not env.probe.sends if not abandon else bool(env.probe.sends)
    else:
        assert env.probe.sends and any(event["kind"] == "cache-after" for event in env.events)
    if family in {"folder", "bulk-groups"}:
        assert terminal["result_summary"]["files_refreshed"] == (1 if not abandon else (2 if family == "bulk-groups" else 3))
    events = [event for event in env.events if event["job"] == job_id]
    assert events[-1]["kind"] == "grant-before-release" and events[-2]["kind"] == "finalizer-exit"
    assert all(event["held"] for event in events if event["kind"].startswith("finalizer"))
    if not abandon:
        assert terminal["result_summary"]["message"] == "Stopped at a safe job boundary for daemon restart; completed work is retained. Re-run the job if needed."
        stop(None)
        assert control.restart_state["observed"] and task.done(), "abandonment resumed an observed stop"


@pytest.mark.asyncio
async def test_cancel_during_cpu_joins_before_releasing_grant(real_job_environment, monkeypatch):
    """D02: stop the real parser future, not a simulated job body."""
    import threading
    from workflowy_mcp.client import api_client_nexus as nexus
    from workflowy_mcp.mutation_locks import registry_held_regions
    env = real_job_environment
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    original = nexus._prepare_carto_source_file
    def gated_source(path, *args, **kwargs):
        if str(path).endswith("/source.py"):
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(10), "fixture did not release real parser"
        return original(path, *args, **kwargs)
    monkeypatch.setattr(nexus, "_prepare_carto_source_file", gated_source)
    cancel = None
    with reject_raw_job_cancel(env):
        ack = await launch_real_job(env, "file")
        assert ack["success"], ack
        job_id, task = ack["job_id"], env.tasks[ack["job_id"]]
        control = env.controls[job_id]
        try:
            await wait_job_gate(env, entered, job_id=job_id)
            future = control.cpu_future
            assert future is not None and not future.done()
            cancel = asyncio.create_task(env.server._system_job_cancel_impl(job_id))
            for _ in range(1000):
                if control.user_requested:
                    break
                await asyncio.sleep(0.001)
            assert control.user_requested and not task.done() and control.cpu_future is future
            assert env.ids["file"] in {region.key for region in registry_held_regions().regions}
            assert not any(event["kind"] == "finalizer-enter" for event in env.events)
            await real_note_edit(env, "outside", "CPU remains outstanding")
            peer = await launch_real_job(env, "peer")
            assert peer["success"], peer
            await env.tasks[peer["job_id"]]
            assert not task.done() and control.cpu_future is future
        finally:
            release.set()
            await task
            if cancel:
                result = await cancel
        assert result["outcome"] == "cooperative" and task.done()
        assert control.terminal_result["status"] == "cancelled" and control.cpu_future is None
    assert not env.probe.violations
    assert not registry_held_regions().regions
    assert env.probe.nodes[env.ids["outside"]]["note"] == "CPU remains outstanding"


@pytest.mark.asyncio
@pytest.mark.parametrize("distortion", ["terminal-json", "missing-reservation", "past-window"])
async def test_cancel_ack_uses_task_completion_not_status_or_pid(real_job_environment, monkeypatch, distortion):
    """D07: public cancellation while an actual governed HTTP await is live."""
    import json
    from workflowy_mcp import mutation_locks
    env = real_job_environment
    entered, release = asyncio.Event(), asyncio.Event()
    async def gated_send(request):
        if not entered.is_set():
            entered.set()
            await release.wait()
    env.send_gate = gated_send
    def forbidden(*args, **kwargs):
        raise AssertionError("daemon task cancellation reached process authority")
    if _probe("CUT2B_NEUTRALIZE_CANCEL_ORDER"):
        # Decide the acknowledgement from the persisted row/registry instead of
        # actual task completion: a terminal-looking row or a missing reservation
        # answers already_terminal; anything else answers cooperative without
        # waiting for the task.
        from pathlib import Path
        original_cancel = env.server.cancel_carto_job
        async def records_ack(*args, **kwargs):
            kwargs["cooperative_timeout_seconds"] = 0
            result = await original_cancel(*args, **kwargs)
            path = Path(args[0]) if args else Path(kwargs["job_file"])
            try:
                persisted_status = json.loads(path.read_text()).get("status")
            except Exception:
                persisted_status = None
            claim_id = control.presentation["claim_id"]
            registered = any(
                hold.governance is not None and hold.governance.claim_id == claim_id
                for hold in mutation_locks._LOCKS.holders.values())
            return {**result, "outcome": (
                "already_terminal" if persisted_status == "completed" or not registered
                else "cooperative")}
        monkeypatch.setattr(env.server, "cancel_carto_job", records_ack)
    if _probe("CUT2B_NEUTRALIZE_PENDING_MARKER"):
        original_cancel = env.server.cancel_carto_job
        async def without_pending_marker(*args, **kwargs):
            result = await original_cancel(*args, **kwargs)
            result.pop("cancellation_pending", None)
            return result
        monkeypatch.setattr(env.server, "cancel_carto_job", without_pending_marker)
    with reject_raw_job_cancel(env):
        ack = await launch_real_job(env, "file")
        assert ack["success"], ack
        job_id, task = ack["job_id"], env.tasks[ack["job_id"]]
        control = env.controls[job_id]
        try:
            await wait_job_gate(env, entered, job_id=job_id)
            path = next(env.jobs.glob("*.json"))
            if distortion == "terminal-json":
                row = json.loads(path.read_text())
                row["status"] = "completed"
                path.write_text(json.dumps(row))
            elif distortion == "missing-reservation":
                # Test-only registry distortion: actual task completion stays
                # cancellation truth even if the reservation entry is gone.
                claim_id = control.presentation["claim_id"]
                for token, hold in tuple(mutation_locks._LOCKS.holders.items()):
                    metadata = hold.governance
                    if metadata is not None and metadata.claim_id == claim_id:
                        del mutation_locks._LOCKS.holders[token]
            with monkeypatch.context() as traps:
                traps.setattr("psutil.Process", forbidden)
                traps.setattr("os.kill", forbidden)
                pending = await env.server._system_job_cancel_impl(job_id)
                assert pending["outcome"] == "kill_refused_in_process_job", pending
                assert pending["status"] == "cancelling" and pending["cancellation_requested"] is True
                assert pending.get("cancellation_pending") is True
                assert pending["message"] == "Cancellation requested; the daemon task will stop at its next safe point. No process was killed. Poll system.job.status."
                assert control.user_requested and not control.user_observed and not task.done()
                assert not any(event["kind"] in {"cache-after", "finalizer-enter"} for event in env.events)
                repeated = asyncio.create_task(env.server._system_job_cancel_impl(job_id))
                release.set()
                await task
                cooperative = await repeated
                assert cooperative["outcome"] in {"cooperative", "already_terminal"} and task.done()
        finally:
            release.set()
            await task
    assert control.terminal_result["status"] == "cancelled", json.dumps(control.terminal_result, default=str)
    events = [event["kind"] for event in env.events if event["job"] == job_id]
    assert events.index("response") < events.index("cache-after") < events.index("finalizer-enter")
    assert events.count("send") == 1 and not env.probe.violations


@pytest.mark.asyncio
async def test_repeated_cancel_does_not_race_terminal_artifact_replace(real_job_environment, monkeypatch):
    """Deterministically order two real writes at their shared atomic temp path.

    No failure is injected: the terminal writer has completed its JSON temp;
    a repeated public cancel completes its ordinary status write; then the
    first writer proceeds to its original replace. Both use the actual writer.
    """
    import json
    import threading
    from workflowy_mcp import carto_job_governance as gov, config
    env = real_job_environment
    send_entered, send_release = asyncio.Event(), asyncio.Event()
    terminal_entered, repeat_entered, repeat_written = asyncio.Event(), asyncio.Event(), asyncio.Event()
    terminal_release = threading.Event()
    loop = asyncio.get_running_loop()
    local = threading.local()
    original_replace = config.os.replace
    original_writer = config.merge_write_carto_job_status
    original_request = gov._write_task_cancellation_request
    requests, replacements = [], []

    async def held_send(request):
        if not send_entered.is_set():
            send_entered.set()
            await send_release.wait()
    env.send_gate = held_send
    def terminal_writer(path, payload, **kwargs):
        local.terminal = payload.get("status") == "cancelled"
        try:
            return original_writer(path, payload, **kwargs)
        finally:
            local.terminal = False
    def replace(source, destination):
        if getattr(local, "terminal", False) and not terminal_entered.is_set():
            replacements.append((str(source), str(destination), "terminal"))
            loop.call_soon_threadsafe(terminal_entered.set)
            assert terminal_release.wait(10), "fixture did not release terminal replace"
        elif getattr(local, "repeated", False) and str(destination).endswith(".json"):
            replacements.append((str(source), str(destination), "request"))
        return original_replace(source, destination)
    def cancellation_request(*args, **kwargs):
        requests.append(True)
        local.repeated = len(requests) == 2
        if local.repeated:
            loop.call_soon_threadsafe(repeat_entered.set)
        try:
            value = original_request(*args, **kwargs)
        finally:
            local.repeated = False
        if len(requests) == 2:
            loop.call_soon_threadsafe(repeat_written.set)
        return value
    monkeypatch.setattr(config, "merge_write_carto_job_status", terminal_writer)
    monkeypatch.setattr(env.server, "merge_write_carto_job_status", terminal_writer)
    monkeypatch.setattr(config.os, "replace", replace)
    monkeypatch.setattr(gov, "_write_task_cancellation_request", cancellation_request)
    first = repeated = None
    with reject_raw_job_cancel(env):
        ack = await launch_real_job(env, "file")
        assert ack["success"], ack
        job_id, task = ack["job_id"], env.tasks[ack["job_id"]]
        control = env.controls[job_id]
        try:
            await wait_job_gate(env, send_entered, job_id=job_id)
            first = asyncio.create_task(env.server._system_job_cancel_impl(job_id))
            for _ in range(1000):
                if control.user_requested:
                    break
                await asyncio.sleep(0.001)
            assert control.user_requested
            send_release.set()
            await wait_job_gate(env, terminal_entered, job_id=job_id)
            repeated = asyncio.create_task(env.server._system_job_cancel_impl(job_id))
            await wait_job_gate(env, repeat_entered, job_id=job_id)
            assert not repeat_written.is_set(), "second write passed the held writer transaction"
        finally:
            send_release.set()
            terminal_release.set()
            await task
            for request in (first, repeated):
                if request is not None:
                    result = await request
                    assert result["outcome"] == "cooperative", result
    assert len(requests) == 2
    assert repeat_written.is_set() and len({source for source, _, _ in replacements}) == 2, replacements
    assert control.terminal_result["status"] == "cancelled", json.dumps({
        "terminal": control.terminal_result, "replacements": replacements}, default=str)


@pytest.mark.asyncio
@pytest.mark.parametrize("order", ["cancel-first", "terminal-first"])
async def test_terminal_status_survives_both_cancel_replace_orders(real_job_environment, monkeypatch, order):
    """28QK3G: second writer blocks BEFORE its merge read, in both orders.

    Obtain a real task's terminal payload, then order the ONE actual writer's
    two transactions over its pre-terminal fixture row. No live job remains to
    heal a stale overwrite after either pair. Only explicit paths/value data
    enter these workers; no error/return/status is injected into the writer.
    """
    import json
    import threading
    from workflowy_mcp import carto_job_governance as gov, config
    env = real_job_environment
    loop, local = asyncio.get_running_loop(), threading.local()
    send_entered, send_release = asyncio.Event(), asyncio.Event()
    async def held_send(request):
        if not send_entered.is_set():
            send_entered.set()
            await send_release.wait()
    env.send_gate = held_send
    cancellation = None
    with reject_raw_job_cancel(env):
        ack = await launch_real_job(env, "file")
        assert ack["success"], ack
        job_id, task = ack["job_id"], env.tasks[ack["job_id"]]
        control = env.controls[job_id]
        try:
            await wait_job_gate(env, send_entered, job_id=job_id)
            cancellation = asyncio.create_task(env.server._system_job_cancel_impl(job_id))
            for _ in range(1000):
                if control.user_requested:
                    break
                await asyncio.sleep(0.001)
            assert control.user_requested
        finally:
            send_release.set()
            await task
            if cancellation is not None:
                assert (await cancellation)["outcome"] == "cooperative"
    assert control.terminal_result["status"] == "cancelled", control.terminal_result
    path = next(env.jobs.glob("*.json"))
    terminal = json.loads(json.dumps(control.terminal_result))
    request = {**terminal, "status": "cancelling"}
    path.write_text(json.dumps(request))  # The row BOTH orders start from.
    original_open, original_replace = config.Path.open, config.os.replace
    entered = {label: asyncio.Event() for label in ("terminal", "request")}
    released = {label: threading.Event() for label in entered}
    second_started, second_read = asyncio.Event(), threading.Event()
    first_label, second_label = (("terminal", "request") if order == "terminal-first" else ("request", "terminal"))
    trace = []
    def opened(target, mode="r", *args, **kwargs):
        label = getattr(local, "label", None)
        if target == path and mode == "r" and label is not None:
            trace.append({"writer": label, "event": "read"})
            if label == second_label:
                second_read.set()
        return original_open(target, mode, *args, **kwargs)
    def replace(source, destination):
        label = getattr(local, "label", None)
        if label is None or destination != path:
            return original_replace(source, destination)
        trace.append({"writer": label, "event": "temp-ready", "source": str(source), "destination": str(destination)})
        loop.call_soon_threadsafe(entered[label].set)
        assert released[label].wait(10), f"fixture did not release {label} replace"
        value = original_replace(source, destination)
        trace.append({"writer": label, "event": "replaced"})
        return value
    def write(label, payload):
        local.label = label
        if label == second_label:
            loop.call_soon_threadsafe(second_started.set)
        try:
            return config.merge_write_carto_job_status(path, payload)
        finally:
            local.label = None
    monkeypatch.setattr(config.Path, "open", opened)
    monkeypatch.setattr(config.os, "replace", replace)
    payloads = {"terminal": terminal, "request": request}
    first = asyncio.create_task(runtime.run_carto_cpu("fixture-first-status", write, first_label, payloads[first_label]))
    second = None
    try:
        await asyncio.wait_for(entered[first_label].wait(), 10)
        second = asyncio.create_task(runtime.run_carto_cpu("fixture-second-status", write, second_label, payloads[second_label]))
        await asyncio.wait_for(second_started.wait(), 10)
        await asyncio.sleep(0.050)  # Test-only contention observation, never a product bound.
        blocked_before_read = not second_read.is_set()
        released[first_label].set()
        assert await first is True
        await asyncio.wait_for(entered[second_label].wait(), 10)
        released[second_label].set()
        assert await second is True
    finally:
        for gate in released.values():
            gate.set()
        await asyncio.gather(*(future for future in (first, second) if future is not None))
    persisted = json.loads(path.read_text())
    assert control.terminal_result["status"] == persisted["status"] == "cancelled", json.dumps({
        "order": order, "actual_terminal": control.terminal_result,
        "disk": persisted, "trace": trace}, default=str)
    assert blocked_before_read, f"second writer read before the first transaction released: {trace}"
    ready = [event for event in trace if event["event"] == "temp-ready" and not event["source"].endswith(".cancel-corrective.tmp")]
    assert len({event["source"] for event in ready}) == 2 and len({event["destination"] for event in ready}) == 1, trace


def test_status_temp_paths_are_unique_for_each_guarded_invocation(scrubbed_runtime, monkeypatch):
    """JFIE0S: check BOTH ordinary and corrective identities, even serialized."""
    import json
    from pathlib import Path
    from workflowy_mcp import config
    path = scrubbed_runtime / "status-fixture.json"
    # The landed cancelling write is the retained precedence authority; no
    # durable sentinel exists any more.
    path.write_text(json.dumps({"id": "status-fixture", "status": "cancelling"}))
    original_replace, paths = config.os.replace, []
    def replace(source, destination):
        if destination == path:
            paths.append(Path(source))
        return original_replace(source, destination)
    monkeypatch.setattr(config.os, "replace", replace)
    for count in (1, 2):
        assert config.merge_write_carto_job_status(path, {
            "id": "status-fixture", "status": "running", "progress": {"nodes_updated": count},
        }) is True
        assert json.loads(path.read_text())["status"] == "cancelling"
    assert len(paths) == len(set(paths)) == 2, paths
    assert not any(item.name.endswith(".cancel-corrective.tmp") for item in paths)
    assert all(item.parent == path.parent and item.name.endswith(".tmp") for item in paths)
    assert not list(scrubbed_runtime.glob("*.tmp"))


@pytest.mark.asyncio
async def test_runtime_requests_observe_only_safe_owner_checkpoint(scrubbed_runtime):
    restart = {"restart_id": "restart-test", "observed": False}
    control = runtime.CartoJobControl(task=asyncio.current_task(), restart_state=restart)
    assert control.restart_state is restart
    assert not control.user_cancel_requested()
    control.checkpoint("active-send")
    assert not restart["observed"]
    restart["restart_id"] = None  # N06 abandons before observation.
    control.checkpoint("file-head", restart_boundary=True)
    assert not restart["observed"]
    async def foreign():
        with pytest.raises(RuntimeError, match="owning task"):
            control.checkpoint("foreign")
    await asyncio.create_task(foreign())
    assert control.phase == "file-head"
    restart["restart_id"] = "restart-next"
    with pytest.raises(asyncio.CancelledError, match="restart boundary"):
        control.checkpoint("file-head", restart_boundary=True)
    assert restart["observed"] and not control.user_cancel_requested()
    restart["restart_id"] = None  # An observed stop never resumes.
    with pytest.raises(asyncio.CancelledError, match="restart boundary"):
        control.checkpoint("file-head", restart_boundary=True)
    user = runtime.CartoJobControl(task=asyncio.current_task())
    user.request_user_cancel()
    user.request_user_cancel()
    assert user.user_cancel_requested() and not user.user_observed
    with pytest.raises(asyncio.CancelledError, match="user cancellation"):
        user.checkpoint("after-patch")
    assert user.user_observed and user.phase == "after-patch"


@pytest.mark.asyncio
async def test_runtime_cpu_cooperative_cancel_joins_exact_disk_future(scrubbed_runtime):
    gate = ThreadGate(scrubbed_runtime / "joined.txt")
    control = runtime.CartoJobControl()
    exited = []
    async def run():
        control.task = asyncio.current_task()
        with runtime.carto_job_scope(job_context(scrubbed_runtime, control)):
            try:
                await runtime.run_carto_cpu("disk", gate.write)
            finally:
                exited.append(gate.finished.is_set())
        pytest.fail("cooperative stop was not observed at the CPU return boundary")
    task = asyncio.create_task(run())
    try:
        await gate.wait_entered()
        exact = control.cpu_future
        assert exact is not None and not exact.done()
        control.request_user_cancel()
        outside = asyncio.create_task(asyncio.sleep(0, result="outside progressed"))
        assert await outside == "outside progressed"
        assert not task.done() and control.cpu_future is exact
        assert not control.user_observed and not exited and not gate.path.exists()
    finally:
        gate.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert exited == [True] and gate.path.read_text() == "joined disk work"
    assert gate.calls == 1 and exact.done() and not exact.cancelled()
    assert control.cpu_future is None and control.user_observed
    assert runtime.current_carto_job_context() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["ap_core", "root_parent", "inbox_parent"])
async def test_typed_only_hold_blocks_drain_without_work_token(scrubbed_runtime, monkeypatch, kind):
    """D04: real typed holds alone keep the real N06 drain probe busy."""
    from types import SimpleNamespace
    from n05_region_harness import region_client
    from workflowy_mcp import daemon_lifecycle as lifecycle, mutation_locks as locks, server
    from workflowy_mcp import mutation_lease_governance as regions
    from workflowy_mcp.coherence.operation_cache import cache_operation_boundary

    monkeypatch.setenv(lifecycle.DRAIN_TIMEOUT_ENV, "3600")
    monkeypatch.setenv(lifecycle._CALL_TIMEOUT_ENV, "0")
    monkeypatch.setattr(locks, "_LOCKS", locks._RegionRegistry())
    controller = lifecycle.DaemonLifecycle(identity=SimpleNamespace(warnings=()))
    controller.register_drain_probe("node_locks", server._node_locks_drain_busy)
    assert not controller._tokens and not controller._background and not controller._busy()
    client = region_client(scrubbed_runtime)
    def factory(snapshot):
        if kind == "ap_core":
            return (regions.build_region_ap_core("N07D04"),)
        parent = (regions.ReservedMutationParent.ROOT if kind == "root_parent"
                  else regions.ReservedMutationParent.INBOX)
        return (regions.build_region_child_set(parent, cache_by_id=snapshot.nodes_by_id),)
    async with cache_operation_boundary(client, "n07-typed-only-drain"):
        async with locks.mutation_region_locks(
            client=client, operation_label="n07-typed-only-drain", region_factory=factory,
        ):
            assert locks.registry_held_keys() == frozenset(), "fixture accidentally held an ordinary UUID"
            assert not controller._tokens and not controller._background, "ordinary work masked the drain probe"
            assert server._node_locks_drain_busy(), "typed-only hold disappeared from drain"
            assert controller._busy(), "drain ignored its registered typed observer"
        assert not server._node_locks_drain_busy() and not controller._busy()
    assert not locks._LOCKS.holders and not locks._LOCKS.waiters


@pytest.mark.asyncio
async def test_runtime_repeated_waiter_cancellation_joins_original_future(scrubbed_runtime):
    gate = ThreadGate(scrubbed_runtime / "joined-after-cancel.txt")
    control = runtime.CartoJobControl()
    exited = []
    async def run():
        control.task = asyncio.current_task()
        with runtime.carto_job_scope(job_context(scrubbed_runtime, control)):
            try:
                return await runtime.run_carto_cpu("disk", gate.write)
            finally:
                exited.append(gate.finished.is_set())
    task = asyncio.create_task(run())
    try:
        await gate.wait_entered()
        exact = control.cpu_future
        # Test injection only: production user/restart fronts never call this.
        for _ in range(2):
            task.cancel()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not task.done(), "waiter cancellation detached the disk worker"
            assert control.cpu_future is exact and not exact.cancelled()
        assert not exited and gate.calls == 1
    finally:
        gate.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert exited == [True] and gate.finished.is_set() and gate.path.exists()
    assert exact.done() and not exact.cancelled() and control.cpu_future is None
    assert runtime.current_carto_job_context() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["pending", "cooperative", "already-done"])
async def test_registered_cancel_branch_ignores_terminal_json_and_pid(scrubbed_runtime, monkeypatch, case):
    """D07 primitive: actual task completion, never presentation or claim absence."""
    import json
    from workflowy_mcp import carto_job_governance as gov
    job_file = scrubbed_runtime / "job.json"
    job_file.write_text(json.dumps({"id": "task-proof", "type": "CARTO_REFRESH",
                                  "execution_model": "daemon_task", "status": "completed"}))
    release = asyncio.Event()
    task = asyncio.create_task(release.wait())
    requested = []
    def request_stop():
        requested.append(True)
        if case == "cooperative":
            release.set()
    for name in ("_claim_candidates_for_job", "carto_cancel_requested", "create_cancellation_sentinel"):
        assert not hasattr(gov, name), f"retired file/process cancellation authority {name} is importable"
    if _probe("CUT2B_NEUTRALIZE_CANCEL_ORDER"):
        # The persisted terminal row is trusted as the acknowledgement instead of
        # the live task's completion.
        original_cancel = gov.cancel_carto_job
        async def json_ack(*args, **kwargs):
            if kwargs.get("task_cancel") is not None:
                kwargs["task_cancel"]()
            try:
                row = json.loads(job_file.read_text())
            except Exception:
                row = {}
            return {"success": True, "job_id": row.get("id") or job_file.stem,
                    "outcome": "already_terminal"}
        monkeypatch.setattr(gov, "cancel_carto_job", json_ack)
    async def yield_to_task(seconds):
        await asyncio.sleep(0)
    if case == "already-done":
        release.set()
        await task
    try:
        result = await gov.cancel_carto_job(
            job_file, requested_by="n07-fixture", jobs_root=scrubbed_runtime,
            task_cancel=request_stop, task_done=task.done,
            cooperative_timeout_seconds=0 if case == "pending" else 5,
            sleep_fn=yield_to_task,
        )
        if case == "pending":
            assert not task.done() and requested == [True]
            assert result["outcome"] == "kill_refused_in_process_job"
            assert result["status"] == "cancelling" and result["cancellation_requested"] is True
            assert result.get("cancellation_pending") is True
            assert result["message"] == "Cancellation requested; the daemon task will stop at its next safe point. No process was killed. Poll system.job.status."
        else:
            assert task.done()
            assert result["outcome"] == ("cooperative" if case == "cooperative" else "already_terminal")
            assert requested == ([True] if case == "cooperative" else [])
        assert json.loads(job_file.read_text())["status"] == "completed", (
            "cancellation presentation overwrote the terminal row or read it as stop truth")
    finally:
        release.set()
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "false", "raise"])
async def test_startup_marks_prior_daemon_tasks_without_resuming_or_blocking_serving(scrubbed_runtime, monkeypatch, failure):
    """W12 marks all old CARTO models/statuses, preserving terminal/non-CARTO data."""
    import json
    import os
    import threading
    from pathlib import Path
    from types import MappingProxyType
    from workflowy_mcp import server, carto_job_governance as gov
    from workflowy_mcp.coherence.operation_cache import current_operation_cache
    from workflowy_mcp.mutation_locks import MUTATION_LOCK_STATE
    jobs, loop_thread = scrubbed_runtime, threading.get_ident()
    interrupted = {"id": "interrupted", "type": "CARTO_REFRESH", "status": "running",
                   "execution_model": "daemon_task", "execution_owner": "launcher", "pid": 999999,
                   "cache_refresh_required": True, "claim_id": "old-claim", "progress": {"nodes_updated": 3}}
    terminal = {**interrupted, "id": "terminal", "status": "completed"}
    legacy = {"id": "legacy-adopted", "type": "CARTO_REFRESH", "status": "running",
              "execution_owner": "worker", "detached": True, "pid": 12345}
    old_rows = [interrupted, legacy]
    for model in (None, "", "foreign-worker"):
        for status in (None, "", "future-status", "preparing", "cancelling"):
            row = {**legacy, "id": f"old-{len(old_rows)}", "status": status}
            if model is not None:
                row["execution_model"] = model
            old_rows.append(row)
    unrelated = {**interrupted, "id": "other-kind", "type": "ETCH"}
    missing_type = {"id": "no-type", "execution_model": "daemon_task", "status": "running"}
    terminal_rows = [{**legacy, "id": f"terminal-{status}", "status": status}
                     for status in ("completed", "completed_with_warnings", "failed", "cancelled")]
    for value in (*old_rows, terminal, *terminal_rows, unrelated, missing_type):
        (jobs / f"{value['id']}.json").write_text(json.dumps(value))
    (jobs / "broken.json").write_text("not JSON")
    original = server.merge_write_carto_job_status
    writes, warnings = [], []
    def writer(path, value):
        assert threading.get_ident() != loop_thread, "startup mark ran on loop"
        assert runtime.current_carto_job_context() is None and current_operation_cache(required=False) is None
        assert MUTATION_LOCK_STATE.get() is None
        writes.append(value["id"])
        if failure == "false":
            return False
        if failure == "raise":
            raise OSError("fixture read-only directory")
        return original(path, value)
    def forbidden(*args, **kwargs):
        raise AssertionError("startup resumed or PID-classified an old daemon task")
    monkeypatch.setattr(server, "merge_write_carto_job_status", writer)
    monkeypatch.setattr(server, "_get_carto_jobs_base_dir", lambda: str(jobs))
    monkeypatch.setattr(server, "_carto_startup_failed_jobs", MappingProxyType({}))
    monkeypatch.setattr(server, "log_event", lambda message, category: warnings.append(message))
    monkeypatch.setattr(server, "_schedule_carto_job", forbidden)
    if os.getenv("CUT2B_NEUTRALIZE_W12_LEGACY_EXCLUSION"):
        # Test-only old-exclusion carrier: hide exactly the non-daemon rows
        # from the real disk pass; no mutated product source is executed.
        original_pass, original_iterdir = server._read_and_mark_abandoned_carto_tasks, Path.iterdir
        def excluded_pass(base):
            def visible(directory):
                rows = original_iterdir(directory)
                if directory != jobs:
                    return rows
                return (path for path in rows if path.name not in {
                    f"{row['id']}.json" for row in old_rows
                    if row.get("execution_model") != "daemon_task"})
            with monkeypatch.context() as traps:
                traps.setattr(Path, "iterdir", visible)
                return original_pass(base)
        monkeypatch.setattr(server, "_read_and_mark_abandoned_carto_tasks", excluded_pass)
    for name in ("inspect_carto_job_liveness", "carto_cancel_requested", "create_cancellation_sentinel"):
        assert not hasattr(server if name == "inspect_carto_job_liveness" else gov, name)
    await server._mark_abandoned_carto_tasks_at_startup()  # Failure MUST NOT escape this call.
    assert set(writes) == {row["id"] for row in old_rows}, "W12 excluded legacy/unknown CARTO history"
    assert len(writes) == len(old_rows)
    for row in old_rows:
        marked = server._carto_startup_failed_jobs[row["id"]]
        assert marked["status"] == "failed" and marked["error_kind"] == "daemon_restarted"
        persisted_row = json.loads((jobs / f"{row['id']}.json").read_text())
        assert persisted_row["status"] == (row["status"] if failure else "failed")
    for row in (terminal, *terminal_rows, unrelated, missing_type):
        assert json.loads((jobs / f"{row['id']}.json").read_text()) == row
    memory = server._carto_startup_failed_jobs["interrupted"]
    assert memory["status"] == "failed" and memory["error_kind"] == "daemon_restarted"
    assert memory["error"] == "The previous daemon ended before this job completed. The job was not resumed; refresh the cache and re-run if needed."
    assert memory["progress"]["nodes_updated"] == 3 and memory["claim_id"] == "old-claim"
    assert "cancel" not in memory["status"]
    with pytest.raises(TypeError):
        memory["progress"]["nodes_updated"] = 4
    persisted = json.loads((jobs / "interrupted.json").read_text())
    assert persisted["status"] == ("running" if failure else "failed")
    assert json.loads((jobs / "terminal.json").read_text()) == terminal
    assert len(warnings) == (1 + len(old_rows) if failure else 1) and all("WARNING" in message for message in warnings)
    assert any("broken.json" in message and "unknown" in message for message in warnings)
    if failure:
        assert any("interrupted" in message and str(jobs / "interrupted.json") in message for message in warnings)
    # Status and cancel honor startup memory even when disk remains nonterminal.
    (jobs / "legacy-adopted.json").unlink()
    (jobs / "terminal.json").unlink()
    result = await server._system_job_status_impl(job_id="interrupted")
    assert result["status"] == "failed" and result["error_kind"] == "daemon_restarted" and result["heartbeat"] is None
    monkeypatch.setattr(server, "cancel_carto_job", forbidden)
    cancel = await server._system_job_cancel_impl("interrupted")
    assert cancel["outcome"] == "already_terminal" and cancel["job_id"] == "interrupted"
    assert not server._carto_inprocess_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("first_write_failure", [False, True])
async def test_startup_marks_abandoned_tasks_failed_without_resuming(scrubbed_runtime, monkeypatch, first_write_failure):
    """D08: include a failed first mark, then a distinct startup-memory table."""
    import json
    from types import MappingProxyType
    from workflowy_mcp import server
    original_writer = server.merge_write_carto_job_status
    await test_startup_marks_prior_daemon_tasks_without_resuming_or_blocking_serving(
        scrubbed_runtime, monkeypatch, "false" if first_write_failure else None)
    path = scrubbed_runtime / "interrupted.json"
    before = json.loads(path.read_text())
    assert before["status"] == ("running" if first_write_failure else "failed")
    old_memory = server._carto_startup_failed_jobs
    monkeypatch.setattr(server, "_carto_startup_failed_jobs", MappingProxyType({}))
    monkeypatch.setattr(server, "merge_write_carto_job_status", original_writer)
    await server._mark_abandoned_carto_tasks_at_startup()
    persisted = json.loads(path.read_text())
    assert persisted["status"] == "failed" and persisted["error_kind"] == "daemon_restarted"
    assert persisted["progress"] == before["progress"] and persisted["claim_id"] == before["claim_id"]
    assert server._carto_startup_failed_jobs is not old_memory
    if first_write_failure:
        assert server._carto_startup_failed_jobs["interrupted"]["error_kind"] == "daemon_restarted"
    else:
        assert persisted == before and not server._carto_startup_failed_jobs, "second start overwrote terminal history"
    assert not server._carto_inprocess_tasks


@pytest.mark.asyncio
async def test_startup_directory_read_failure_warns_and_lifespan_orders_mark_before_jobs(scrubbed_runtime, monkeypatch):
    """CF-45 resolver and directory failures each warn once and keep serving."""
    import ast
    import os
    from pathlib import Path
    from types import MappingProxyType
    from workflowy_mcp import server
    warnings = []
    broken = scrubbed_runtime / "not-a-directory"
    broken.write_text("fixture")
    monkeypatch.setattr(server, "_get_carto_jobs_base_dir", lambda: str(broken))
    monkeypatch.setattr(server, "_carto_startup_failed_jobs", MappingProxyType({}))
    monkeypatch.setattr(server, "log_event", lambda text, category: warnings.append(text))
    await server._mark_abandoned_carto_tasks_at_startup()
    assert not server._carto_startup_failed_jobs and len(warnings) == 1
    assert "WARNING" in warnings[0] and "unknown" in warnings[0] and str(broken) in warnings[0]
    warnings.clear()
    def unavailable_root():
        raise OSError("fixture jobs root resolution failed")
    monkeypatch.setattr(server, "_get_carto_jobs_base_dir", unavailable_root)
    mark_startup = server._mark_abandoned_carto_tasks_at_startup
    if os.getenv("CUT2B_NEUTRALIZE_STARTUP_RESOLVER_TRY"):
        async def outside_try():
            server._get_carto_jobs_base_dir()
            await mark_startup()
        invoke = outside_try
    else:
        invoke = mark_startup
    escaped = None
    try:
        await invoke()
    except OSError as exc:
        escaped = exc
    assert escaped is None, "startup resolver escaped warn-and-serve boundary"
    assert not server._carto_startup_failed_jobs and len(warnings) == 1
    assert "WARNING" in warnings[0] and "<jobs root unavailable>" in warnings[0]
    assert "fixture jobs root resolution failed" in warnings[0]
    tree = ast.parse(Path(server.__file__).read_text())
    lifespan = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan")
    sweep = [node.lineno for node in ast.walk(lifespan) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_sweep_carto_governance"]
    mark = [node.lineno for node in ast.walk(lifespan) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_mark_abandoned_carto_tasks_at_startup"]
    controller = [node.lineno for node in ast.walk(lifespan) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "DaemonLifecycle"]
    assert not sweep and len(mark) == 1 and mark[0] < controller[0], "startup can schedule jobs before W12 marking"


@pytest.mark.asyncio
@pytest.mark.parametrize("disk", ["missing", "malformed", "terminal", "forged-id", "malformed-progress"])
async def test_status_and_cancel_use_live_task_overlay_even_when_json_cannot_prove_it(claimed_task_environment, monkeypatch, disk):
    import json
    import os
    import threading
    from workflowy_mcp import server, carto_job_governance as gov
    from workflowy_mcp.coherence.operation_cache import current_operation_cache
    from workflowy_mcp.mutation_locks import MUTATION_LOCK_STATE
    from n05_region_harness import node_uuid
    env, loop_thread = claimed_task_environment, threading.get_ident()
    entered, release = asyncio.Event(), asyncio.Event()
    async def work(**kwargs):
        entered.set()
        await release.wait()
        return {"success": True}
    monkeypatch.setattr(env.client, "refresh_file_node_beacons", work, raising=False)
    ack = await server._start_carto_refresh_job(node_uuid("file"), "file")
    assert ack.get("success") is True, repr(ack)
    task, control = server._carto_inprocess_tasks[ack["job_id"]], server._carto_job_controls[ack["job_id"]]
    assert server._carto_live_job_projection()[ack["job_id"]]["status"] == "preparing"
    await asyncio.wait_for(entered.wait(), 10)
    from pathlib import Path
    path = Path(control.presentation["path"])
    control.progress["nodes_updated"] = 3
    before = server._carto_live_job_projection()
    control.progress["nodes_updated"] = 7
    assert before[ack["job_id"]]["progress"]["nodes_updated"] == 3
    if disk == "missing":
        path.unlink()
    elif disk == "malformed":
        path.write_text("invalid JSON")
    else:
        path.write_text(json.dumps({"id": "spoof" if disk == "forged-id" else ack["job_id"],
                                   "type": "CARTO_REFRESH", "status": "completed", "pid": -1,
                                   "detached": True, "cache_refresh_required": False,
                                   "progress": "invalid" if disk == "malformed-progress" else {}}))
    scans, replies = [], []
    original_scan, original_cancel = server._scan_carto_job_inventory_locked, gov.cancel_carto_job
    def scan(*args, **kwargs):
        assert threading.get_ident() != loop_thread, "job inventory scanned on loop"
        assert current_operation_cache(required=False) is None and runtime.current_carto_job_context() is None
        assert MUTATION_LOCK_STATE.get() is None
        scans.append(kwargs["live_projection"])
        with pytest.raises(TypeError):
            kwargs["live_projection"]["forged"] = {}
        return original_scan(*args, **kwargs)
    def forbidden(*args, **kwargs):
        raise AssertionError("daemon task status/cancel used PID heartbeat or healer authority")
    monkeypatch.setattr(server, "_scan_carto_job_inventory_locked", scan)
    for name in ("inspect_carto_job_liveness", "_claim_candidates_for_job", "carto_cancel_requested",
                 "create_cancellation_sentinel"):
        assert not hasattr(server if name == "inspect_carto_job_liveness" else gov, name)
    assert not hasattr(server, "_read_carto_heartbeat_diagnostic")
    async def cancel(*args, **kwargs):
        assert kwargs["task_done"]() is False and kwargs["task_job"]["id"] == ack["job_id"]
        result = await original_cancel(*args, **kwargs, cooperative_timeout_seconds=0)
        if _probe("CUT2B_NEUTRALIZE_CANCEL_ORDER"):
            # Decide the acknowledgement from the persisted row instead of the
            # actual task: a terminal row is already_terminal, and a row that
            # cannot prove the live task is treated as a completed cooperative
            # stop without waiting for native task completion.
            try:
                persisted_status = json.loads(path.read_text()).get("status")
            except Exception:
                persisted_status = None
            result = {**result, "outcome": (
                "already_terminal" if persisted_status == "completed" else "cooperative")}
        if _probe("CUT2B_NEUTRALIZE_PENDING_MARKER"):
            result = {key: value for key, value in result.items()
                      if key != "cancellation_pending"}
        return result
    monkeypatch.setattr(server, "cancel_carto_job", cancel)
    class Socket:
        async def send(self, text):
            assert threading.get_ident() == loop_thread
            replies.append(json.loads(text))
    try:
        status = await server._system_job_status_impl(job_id=ack["job_id"])
        assert status["success"] and status["status"] == "running" and status["pid"] == os.getpid()
        assert status["heartbeat"] is None and status["detached"] is False and status["progress"]["nodes_updated"] == 7
        await server._handle_carto_list_jobs(Socket())
        assert [row["job_id"] for row in replies[0]["jobs"]] == [ack["job_id"]]
        result = await server._system_job_cancel_impl(ack["job_id"])
        assert result["outcome"] == "kill_refused_in_process_job" and result["status"] == "cancelling"
        assert result.get("cancellation_pending") is True
        assert control.user_requested and not task.done()
        persisted = json.loads(path.read_text())
        assert persisted["progress"]["nodes_updated"] == 7
        # The landed terminal row keeps precedence over a later cancelling
        # presentation write; a missing/malformed row accepts the merge.
        assert persisted["status"] == ("completed" if disk in {"terminal", "forged-id", "malformed-progress"} else "cancelling")
        # G2's one-job status is filename-first and never scans the inventory;
        # only the widget poll consumes the immutable live projection here.
        assert len(scans) == 1
    finally:
        release.set()
        await task
    assert control.user_observed and not server._carto_inprocess_tasks


@pytest.mark.asyncio
async def test_unregistered_daemon_task_cancel_never_reaches_pid_escalation(scrubbed_runtime, monkeypatch):
    import json
    from types import MappingProxyType
    from workflowy_mcp import server
    (scrubbed_runtime / "stale.json").write_text(json.dumps({"id": "stale", "type": "CARTO_REFRESH",
        "execution_model": "daemon_task", "status": "running", "pid": 123456}))
    monkeypatch.setattr(server, "_carto_inprocess_tasks", {})
    monkeypatch.setattr(server, "_carto_startup_failed_jobs", MappingProxyType({}))
    monkeypatch.setattr(server, "_get_carto_jobs_base_dir", lambda: str(scrubbed_runtime))
    def forbidden(*args, **kwargs):
        raise AssertionError("unregistered daemon_task reached legacy process controller")
    monkeypatch.setattr(server, "cancel_carto_job", forbidden)
    result = await server._system_job_cancel_impl("stale")
    assert result["outcome"] == "already_terminal" and result["job_id"] == "stale"


from test_n07_carto_final_refresh import final_environment


@pytest.mark.asyncio
async def test_final_refresh_handoff_remains_accounted_during_drain(final_environment, monkeypatch):
    from n05_region_harness import node_uuid
    from workflowy_mcp import daemon_lifecycle as lifecycle
    from workflowy_mcp.daemon_identity import InstalledIdentity
    from workflowy_mcp.mutation_locks import held_mutation_lock_regions
    from test_n07_carto_final_refresh import receipt, settle
    env, srv, exits, fetches = final_environment, final_environment.server, [], []
    ctl = lifecycle.DaemonLifecycle(
        identity=InstalledIdentity("1.0.1", "a" * 40, 10, "git", "linux", "test-host", "a" * 32),
        exit_callback=lambda: exits.append("exit"))
    ctl.start()  # Local controller only: no daemon, transport or server starts.
    monkeypatch.setattr(lifecycle, "active_lifecycle", lambda: ctl)
    ctl.register_drain_probe("node_locks", srv._node_locks_drain_busy)
    ctl.register_drain_probe("daemon_tasks", lambda: any(srv._daemon_drain_snapshot().values()))
    entered, finish_work, second, publish = (asyncio.Event() for _ in range(4))
    async def work(**kwargs):
        assert held_mutation_lock_regions()
        entered.set()
        await finish_work.wait()
        return {"success": True, "nodes_updated": 1}
    async def refresh(**kwargs):
        assert not held_mutation_lock_regions(), "delivery borrowed the job's graph grant"
        fetches.append(kwargs)
        if len(fetches) == 1:
            return {"success": False, "error": "fixture first terminal fetch failed"}
        second.set()
        await publish.wait()
        return {"success": True, "final_publication_receipt": receipt()}
    monkeypatch.setattr(env.client, "refresh_file_node_beacons", work, raising=False)
    monkeypatch.setattr(env.client, "refresh_nodes_export_cache", refresh, raising=False)
    monkeypatch.setattr(srv, "_ensure_carto_final_delivery", env.live_final_delivery)
    ack = await srv._start_carto_refresh_job(node_uuid("file"), "file")
    assert ack["success"], ack
    task = srv._carto_inprocess_tasks[ack["job_id"]]
    await entered.wait()
    record = srv._carto_final_obligations[ack["job_id"]]
    busy_samples, actual_busy = [], ctl._busy
    def busy(attempt=None):
        result = actual_busy(attempt)
        finished = record.state == "published" and task.done() and not srv._daemon_drain_snapshot()["carto_final_delivery"]
        busy_samples.append((result, finished))
        assert result or finished, "zero-count drain gap before terminal delivery finished"
        return result
    monkeypatch.setattr(ctl, "_busy", busy)
    status, attempt = ctl.request_restart({"protocol": 1, "expected_instance_id": ctl.identity.instance_id,
        "target_identity": {"package_version": "1.0.1", "commit": "b" * 40, "commit_time": 20},
        "requester": {"platform": "linux", "hostname": "TEST-HOST", "is_wsl": False}, "drain_timeout_s": 3600})
    assert status == 202 and attempt["accepted"]
    async def watcher_tick():
        class TickFinished(Exception):
            pass
        async def one_sleep(seconds):
            assert seconds == 1.0
            raise TickFinished
        with monkeypatch.context() as patch:
            patch.setattr(srv.asyncio, "sleep", one_sleep)
            with pytest.raises(TickFinished):
                await srv._carto_jobs_watcher_loop()
    try:
        finish_work.set()
        await task
        await settle(lambda: record.state == "pending" and len(fetches) == 1)
        delivery = srv._carto_final_delivery_task
        assert delivery is not None and delivery in ctl._background and not exits
        assert ctl.phase == "draining" and not srv._node_locks_drain_busy()
        assert srv._daemon_drain_snapshot()["carto_final_delivery"]
        await watcher_tick()  # Real existing tick in drain; no sweep or GC.
        await asyncio.wait_for(second.wait(), 10)
        assert not exits and ctl._busy() and record.state == "reserved"
        publish.set()
        await settle(lambda: record.state == "published")
        assert not exits, "drain forgot the still-registered delivery task"
        await watcher_tick()
        await delivery
        await asyncio.wait_for(ctl.wait_restart(attempt["restart_id"]), 10)
        assert exits == ["exit"] and ctl.phase == "exiting"
        assert len(fetches) == 2 and all(call == {"refresh_kind": "system", "require_new_fetch": True} for call in fetches)
        assert busy_samples and all(result or done for result, done in busy_samples)
    finally:
        monkeypatch.setattr(ctl, "_busy", actual_busy)
        finish_work.set()
        publish.set()
        await task
        coordinator = srv._nodes_export_refresh_task
        if coordinator is not None:
            await asyncio.gather(coordinator, return_exceptions=True)
        if record.state != "published":
            # Fixture cleanup after a planted fault, not a production transition.
            record.state = "irrelevant"
            srv._carto_final_live_attempts.clear()
        srv._carto_final_retry_event.set()
        delivery = srv._carto_final_delivery_task
        if delivery is not None:
            await delivery


@pytest.mark.asyncio
@pytest.mark.parametrize("disturbance", ["user-cancel", "hard-barrier"])
async def test_region_wait_preserves_ticket_deadline_and_rechecks_before_effect(real_job_environment, monkeypatch, disturbance):
    """§7.2 rows3/5: a real short write races after claim admission.

    Observe the actual N05 waiter, not a sleeping replacement job or a new
    cancellation waiter. A request cannot shorten/reset its original bound;
    after the short write publishes, user stop or the fresh barrier gate wins
    before the Cartographer task's first effect.
    """
    from pathlib import Path
    import json
    from workflowy_mcp import carto_job_governance as gov, mutation_locks as locks
    from workflowy_mcp.coherence.barrier import raise_global_dirty_barrier_async
    env = real_job_environment
    short_entered, release_short, waiting, requested = (asyncio.Event() for _ in range(4))
    original_schedule, original_wait = env.server._schedule_carto_job, locks._wait_for_region_change
    short = cancel_call = None
    observations = []

    async def send_gate(request):
        if runtime.current_carto_job_context() is None and not short_entered.is_set():
            short_entered.set()
            await release_short.wait()
    env.send_gate = send_gate

    async def race_after_admission(spec):
        nonlocal short
        short = asyncio.create_task(real_note_edit(env, "symbol", "AST_QUALNAME: existing\nshort-before-job"))
        await asyncio.wait_for(short_entered.wait(), 10)
        return await original_schedule(spec)

    async def observed_wait(waiter, *, deadline, timeout_seconds):
        if runtime.current_carto_job_context() is not None:
            observations.append((waiter, waiter.ticket, waiter.event, deadline, timeout_seconds))
            waiting.set()
        return await original_wait(waiter, deadline=deadline, timeout_seconds=timeout_seconds)

    monkeypatch.setattr(env.server, "_schedule_carto_job", race_after_admission)
    monkeypatch.setattr(locks, "_wait_for_region_change", observed_wait)
    with reject_raw_job_cancel(env):
        try:
            ack = await launch_real_job(env, "file")
            assert ack["success"], ack
            job_id, task = ack["job_id"], env.tasks[ack["job_id"]]
            await asyncio.wait_for(waiting.wait(), 10)
            control = env.controls[job_id]
            raw_attempts, raw_cancel = [], task.cancel
            def counted_raw_cancel(*args, **kwargs):
                raw_attempts.append((args, kwargs))
                return raw_cancel(*args, **kwargs)
            monkeypatch.setattr(task, "cancel", counted_raw_cancel)
            waiter, ticket, event, deadline, timeout_seconds = observations[0]
            assert waiter in locks._LOCKS.waiters and len(locks._LOCKS.waiters) == 1
            assert not task.done() and not control.user_requested and not control.user_observed
            await real_note_edit(env, "outside", "outside edit while actual job waiter is blocked")
            assert env.probe.nodes[env.ids["outside"]]["note"] == "outside edit while actual job waiter is blocked"
            if disturbance == "user-cancel":
                from workflowy_mcp import carto_job_governance as governance
                written = asyncio.Event()
                loop = asyncio.get_running_loop()
                write_request = governance._write_task_cancellation_request
                def observe_write(*args, **kwargs):
                    result = write_request(*args, **kwargs)
                    loop.call_soon_threadsafe(written.set)
                    return result
                monkeypatch.setattr(governance, "_write_task_cancellation_request", observe_write)
                request = runtime.CartoJobControl.request_user_cancel
                def requested_stop(owner):
                    if _probe("CUT2B_NEUTRALIZE_CANCEL_ORDER"):
                        # Presentation is treated as the stop request: publish the
                        # cancelling row but never ask the loop-owned control.
                        governance._write_task_cancellation_request(
                            Path(ack["job_file"]), {"status": "cancelling"})
                        if owner is control:
                            requested.set()
                        return
                    request(owner)
                    if owner is control:
                        requested.set()
                monkeypatch.setattr(runtime.CartoJobControl, "request_user_cancel", requested_stop)
                cancel_call = asyncio.create_task(env.server._system_job_cancel_impl(job_id))
                await asyncio.wait_for(requested.wait(), 10)
                assert control.user_requested and not control.user_observed
                await asyncio.wait_for(written.wait(), 10)
                assert json.loads(Path(ack["job_file"]).read_text())["status"] == "cancelling"
            else:
                await raise_global_dirty_barrier_async(env.paths, reason_code="verified_effect_unpublishable",
                    transaction_id="fixture-wait-barrier", detail="suite-local barrier after admission")
            # Neither front is allowed to raw-cancel the waiter, replace its
            # event/ticket/deadline, or run a finalizer while it still waits.
            assert not task.done() and not short.done()
            assert waiter in locks._LOCKS.waiters and len(locks._LOCKS.waiters) == 1
            assert (waiter.ticket, waiter.event, waiter.deadline) == (ticket, event, deadline)
            assert all(row == (waiter, ticket, event, deadline, timeout_seconds) for row in observations)
            assert not [e for e in env.events if e["job"] == job_id and e["kind"] in {"send", "cache-after", "finalizer-enter"}]
            release_short.set()
            await short
            await task
            assert not raw_attempts, "raw job cancellation was attempted even if the controller contained its error"
            result = control.terminal_result
            if disturbance == "user-cancel":
                assert control.user_observed and result["status"] == "cancelled", result
                assert (await cancel_call)["outcome"] == "cooperative"
            else:
                assert result["status"] == "failed" and result["error_kind"] == "coherence_barrier_active", result
            own = [e for e in env.events if e["job"] == job_id]
            assert not [e for e in own if e["kind"] in {"send", "cache-after"}], "waiting job made a first effect after stop/refusal"
            assert [e["kind"] for e in own if e["kind"].startswith("finalizer-")] == ["finalizer-enter", "finalizer-exit"]
            assert not locks._LOCKS.waiters and not locks.registry_held_regions().regions
            assert not env.probe.violations
        finally:
            release_short.set()
            if short is not None:
                await short
            if env.tasks:
                await asyncio.gather(*env.tasks.values())
            if cancel_call is not None:
                await asyncio.gather(cancel_call, return_exceptions=True)
