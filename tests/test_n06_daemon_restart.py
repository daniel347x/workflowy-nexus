"""N06 drain/control and real old→new process tests on isolated fake services.

Clock jumps advance the attempt deadline, never lower the production floor.
The process overlays keep the real package entry, guard, lifecycle and runner.
Every process/event witness here is event-driven in a private Scenario root and
port, and its wait is a hang guard, not an idle-machine speed threshold.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
from contextlib import AsyncExitStack, asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from fastmcp import Client, FastMCP
from mcp.shared.exceptions import MCPError
from mcp_types import Implementation, InitializeResult

from n06_harness import WAIT, SETUP_WAIT, Scenario, collect, cleanup, drain_output, launch_subprocess, wait_for_file, touch
from workflowy_mcp import daemon_lifecycle as lifecycle
from workflowy_mcp.daemon_identity import DAEMON_META_KEY, InstalledIdentity


def fixture_identity(commit="a", source_time=10, platform="linux"):
    return InstalledIdentity("1.0.1", commit * 40, source_time, "git", platform, "test-host", commit * 32)


def restart_body(controller, *, source_time=20, commit="b", bound=3600):
    return {
        "protocol": 1, "expected_instance_id": controller.identity.instance_id,
        "target_identity": {"package_version": "1.0.1", "commit": commit * 40, "commit_time": source_time},
        "requester": {"platform": controller.identity.platform, "hostname": "TEST-HOST", "is_wsl": False},
        "drain_timeout_s": bound,
    }


@pytest.fixture(autouse=True)
def isolated_controller_environment(monkeypatch):
    scenario = Scenario.create()
    scenario.install_environment(monkeypatch)
    lifecycle.set_active_lifecycle(None)
    mutation = os.environ.get("N06_TEST_MUTATION", "")
    if mutation == "restart_early_exit":
        original = lifecycle.DaemonLifecycle.request_restart
        def request(self, body):
            status, result = original(self, body)
            if result.get("accepted"):
                self._exit_callback()
            return status, result
        monkeypatch.setattr(lifecycle.DaemonLifecycle, "request_restart", request)
    elif mutation == "restart_reset_deadline":
        original = lifecycle.DaemonLifecycle.request_restart
        def request(self, body):
            status, result = original(self, body)
            if status == 202 and not result["accepted"]:
                self._attempt.deadline = self._clock() + self._attempt.bound
            return status, result
        monkeypatch.setattr(lifecycle.DaemonLifecycle, "request_restart", request)
    elif mutation == "restart_cancel_at_deadline":
        original = lifecycle.DaemonLifecycle._fail_attempt
        def fail(self, attempt, reason):
            for token in self._tokens:
                if token.task is not None:
                    token.task.cancel()
            original(self, attempt, reason)
        monkeypatch.setattr(lifecycle.DaemonLifecycle, "_fail_attempt", fail)
    elif mutation == "restart_drop_on_disconnect":
        original = lifecycle.DaemonLifecycle.run_admitted
        async def run(self, token, coro):
            try:
                return await original(self, token, coro)
            except asyncio.CancelledError:
                self._tokens.discard(token)
                self._notify()
                raise
        monkeypatch.setattr(lifecycle.DaemonLifecycle, "run_admitted", run)
    elif mutation == "restart_ignore_probe":
        monkeypatch.setattr(lifecycle.DaemonLifecycle, "_busy", lambda self, attempt=None: bool(self._tokens) or any(not task.done() for task in self._background))
    elif mutation == "restart_ignore_work_tokens":
        monkeypatch.setattr(lifecycle.DaemonLifecycle, "_busy", lambda self, attempt=None: any(bool(probe()) for probe in self._probes.values()))
    elif mutation == "restart_bare_error":
        from fastmcp.tools.base import ToolResult
        monkeypatch.setattr(lifecycle.RestartingError, "tool_result", lambda self: ToolResult(is_error=True, content="restarting"))
    elif mutation == "restart_grace_cut":
        import uvicorn
        original = uvicorn.Config
        def config(*args, **kwargs):
            kwargs["timeout_graceful_shutdown"] = 2
            return original(*args, **kwargs)
        monkeypatch.setattr(uvicorn, "Config", config)
    elif mutation == "restart_spawn_once":
        from workflowy_mcp import daemon
        original = daemon.ensure_daemon_running
        def ensure(**kwargs):
            popen = daemon.subprocess.Popen
            def child(*args, **kw):
                actual = popen(*args, **kw)
                def poll():
                    code = actual.poll()
                    return 1 if code == 75 else code
                return SimpleNamespace(poll=poll)
            with monkeypatch.context() as local:
                local.setattr(daemon.subprocess, "Popen", child)
                return original(**kwargs)
        monkeypatch.setattr(daemon, "ensure_daemon_running", ensure)
    elif mutation == "restart_short_bound":
        monkeypatch.setattr(lifecycle, "DEFAULT_DRAIN_TIMEOUT_S", 30)
    elif mutation == "restart_drop_child":
        from workflowy_mcp.coherence.operation_cache import create_isolated_context_task
        monkeypatch.setattr(lifecycle.DaemonLifecycle, "spawn_admitted_child", lambda self, label, coro: create_isolated_context_task(coro))
    elif mutation == "restart_drop_background":
        monkeypatch.setattr(lifecycle.DaemonLifecycle, "track_background_task", lambda *args, **kwargs: None)
    elif mutation == "restart_no_auth":
        monkeypatch.setattr(lifecycle.hmac, "compare_digest", lambda *args: True)
    elif mutation == "restart_ignore_instance":
        original = lifecycle.DaemonLifecycle.request_restart
        def request(self, body):
            if isinstance(body, dict):
                body = {**body, "expected_instance_id": self.identity.instance_id}
            return original(self, body)
        monkeypatch.setattr(lifecycle.DaemonLifecycle, "request_restart", request)
    elif mutation == "restart_drop_recovery":
        from workflowy_mcp import server
        from workflowy_mcp.coherence.operation_cache import create_isolated_context_task
        def untracked(task):
            if server._nodes_export_refresh_task is task:
                create_isolated_context_task(server._recover_nodes_export_refresh_task(task))
        monkeypatch.setattr(server, "_nodes_export_refresh_task_done", untracked)
    elif mutation == "restart_bulk_mid_group":
        from workflowy_mcp import server
        original = server._carto_job_cancel_requested
        monkeypatch.setattr(server, "_carto_job_cancel_requested", lambda path: original(path) or any(state["restart_id"] for state in server._inprocess_restart_stops.values()))
    elif mutation == "restart_ignore_boundary_input":
        from workflowy_mcp import carto_job_governance as gov, server
        original = gov.finalize_cancelled_job_claim_async
        async def old_semantics(*args, **kwargs):
            kwargs.pop("restart_boundary_stop", None)
            return await original(*args, **kwargs)
        monkeypatch.setattr(gov, "finalize_cancelled_job_claim_async", old_semantics)
        monkeypatch.setattr(server, "finalize_cancelled_job_claim_async", old_semantics)
    elif mutation == "restart_drop_poll":
        from workflowy_mcp import server
        from workflowy_mcp.coherence.operation_cache import create_isolated_context_task
        original = server._carto_list_jobs_poll
        def poll(websocket):
            ctl = lifecycle.active_lifecycle()
            with monkeypatch.context() as local:
                local.setattr(ctl, "spawn_admitted_child", lambda label, coro: create_isolated_context_task(coro))
                return original(websocket)
        monkeypatch.setattr(server, "_carto_list_jobs_poll", poll)
    elif mutation == "restart_drop_ws_body":
        original = lifecycle.DaemonLifecycle.run_admitted
        async def run(self, token, coro):
            if token.label.startswith("ws:"):
                self._tokens.discard(token)
                return await coro
            return await original(self, token, coro)
        monkeypatch.setattr(lifecycle.DaemonLifecycle, "run_admitted", run)
    elif mutation == "restart_omit_registration":
        from workflowy_mcp import server
        monkeypatch.setattr(server.mcp, "middleware", [mw for mw in server.mcp.middleware if not isinstance(mw, lifecycle.DaemonLifecycleMiddleware)])
    yield scenario
    lifecycle.set_active_lifecycle(None)


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


async def bounded(awaitable):
    return await asyncio.wait_for(awaitable, WAIT)


def controller(**kwargs):
    value = lifecycle.DaemonLifecycle(identity=fixture_identity(), **kwargs)
    value.start()
    return value


@pytest.mark.parametrize(("drain", "call", "requested", "expected", "warning_count"), [
    (None, None, None, 3600, 0),
    ("3600", "0", None, 3600, 0),
    ("3600", "0e0", None, 3600, 0),
    ("7200", "900", None, 7200, 1),
    ("7200", "10800", 14400, 14400, 0),
    ("1e20", "0", 3600, 1e20, 0),
    ("30", "0", None, 3600, 1),
    ("nan", "inf", -1, 3600, 3),
    ("-1", "-10", False, 3600, 3),
    ("bad", "0", 1, 3600, 2),
])
def test_drain_bound_is_lengthen_only_with_disabled_call_timeout(drain, call, requested, expected, warning_count):
    env = {}
    if drain is not None:
        env[lifecycle.DRAIN_TIMEOUT_ENV] = drain
    if call is not None:
        env["WORKFLOWY_MCP_SHIM_CALL_TIMEOUT_S"] = call
    bound, warnings = lifecycle.drain_bound(requested, environ=env)
    assert bound == expected and bound >= 3600
    assert len(warnings) == warning_count
    for warning in warnings:
        assert warning.code == "drain_setting_adjusted"
        assert "Active work will never be cancelled by this bound." in warning.message


def test_drain_reserves_before_await_retains_cancelled_waiter_and_rejects_new_body():
    async def run():
        exits, effects = [], []
        started, release, published = asyncio.Event(), asyncio.Event(), asyncio.Event()
        ctl = controller(exit_callback=lambda: exits.append("exit"))
        async def body():
            async with ctl.operation_scope("adopt-release"):
                effects.append("dispatch")
                started.set()
                await release.wait()
                effects.append("publish")
            published.set()
            return {"result": "original"}
        token = ctl.admit_work("mutation")  # No await before reserving.
        waiter = asyncio.create_task(ctl.run_admitted(token, body()))
        await bounded(started.wait())
        status, accepted = ctl.request_restart(restart_body(ctl))
        assert status == 202 and accepted["accepted"] is True
        assert ctl.snapshot()["active_work"] == ctl.snapshot()["active_operations"] == 1
        with pytest.raises(lifecycle.RestartingError) as refused:
            ctl.admit_work("must-not-run")
        error = refused.value.error
        assert error["outcome"] == "not_sent" and error["retryable"] and error["retry_after"] == 30
        waiter.cancel()  # Simulates a transport waiter, never the admitted body.
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert effects == ["dispatch"] and not token.task.cancelled() and not exits
        assert ctl.snapshot()["active_work"] == 1
        release.set()
        assert await bounded(asyncio.shield(token.task)) == {"result": "original"}
        await bounded(published.wait())
        result = await bounded(ctl.wait_restart(accepted["restart_id"]))
        assert result["phase"] == "exiting" and result["retry_after"] == 1
        assert effects == ["dispatch", "publish"] and exits == ["exit"]
        assert ctl.snapshot()["active_work"] == ctl.snapshot()["active_operations"] == 0
    asyncio.run(run())


def test_actual_sdk_peer_cancellation_cannot_cancel_admitted_body():
    async def run():
        effects, exits, startups = [], [], []
        started, release, published = asyncio.Event(), asyncio.Event(), asyncio.Event()
        ctl = lifecycle.DaemonLifecycle(identity=fixture_identity(), exit_callback=lambda: exits.append("exit"))
        @asynccontextmanager
        async def lifespan(app):
            startups.append("one-client")
            lifecycle.set_active_lifecycle(ctl)
            ctl.start()
            try:
                yield
            finally:
                await ctl.join_work()
                lifecycle.set_active_lifecycle(None)
        app = FastMCP("sdk-cancellation-fixture", lifespan=lifespan, middleware=[lifecycle.DaemonLifecycleMiddleware()])
        @app.tool
        async def mutation(value: str) -> dict:
            effects.append(("dispatch", value))
            started.set()
            await release.wait()
            effects.append(("publish", value))
            published.set()
            return {"value": value}
        async with Client(app, mode="legacy") as client:
            assert client.initialize_result.meta[DAEMON_META_KEY] == ctl.metadata()
            call = asyncio.create_task(client.call_tool("mutation", {"value": "one"}))
            await bounded(started.wait())
            _, attempt = ctl.request_restart(restart_body(ctl))
            call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await call
            assert ctl.snapshot()["active_work"] == 1 and effects == [("dispatch", "one")] and not exits
            release.set()
            await bounded(published.wait())
            await bounded(ctl.wait_restart(attempt["restart_id"]))
            assert effects == [("dispatch", "one"), ("publish", "one")] and exits == ["exit"]
        assert startups == ["one-client"]
    asyncio.run(run())


def test_actual_governor_wait_send_and_publication_stay_in_the_same_admitted_operation(monkeypatch, isolated_controller_environment):
    """A real ticket wait with no key yet cannot fall between drain counters."""
    from coherence_harness import make_boundary_capable
    from workflowy_mcp import mutation_locks, server
    from workflowy_mcp.carto_write_governor import CartoWriteGovernor

    class FakeClient:
        pass
    client = FakeClient()
    lock_uuid = "06060606-0000-4000-8000-000000000001"
    make_boundary_capable(client, nodes=[{"id": lock_uuid, "nm": "fake", "parent_id": None}])
    monkeypatch.setattr(server, "_client", client)

    async def run():
        effects, exits = [], []
        ticket_wait, release_ticket = asyncio.Event(), asyncio.Event()
        sent, publish = asyncio.Event(), asyncio.Event()
        clock = Clock()
        governor = CartoWriteGovernor(account_scope_id="e" * 32, jobs_root=isolated_controller_environment.root / "governor", clock=clock)
        client._write_governor = governor
        client.api_key = "n06-fake-key-never-connect"
        ctl = controller(exit_callback=lambda: exits.append("exit"))
        lifecycle.set_active_lifecycle(ctl)
        ctl.register_drain_probe("node_locks", server._node_locks_drain_busy)
        async def first_sleep(seconds, operation):
            pytest.fail("first ticket unexpectedly waited")
        await governor.acquire_write_slot("warm-ticket", sleep_fn=first_sleep)
        async def ticket_sleep(seconds, operation):
            assert seconds == 1 and operation == "n06-governed"
            ticket_wait.set()
            await release_ticket.wait()
            clock.now += seconds
        async def body():
            async with server._mcp_operation_boundary("n06-governed"):
                ticket = await governor.acquire_write_slot("n06-governed", sleep_fn=ticket_sleep)
                assert ticket.slot_at == clock.now
                async with mutation_locks.mutation_node_locks(lock_uuid, client=client):
                    effects.append("sent")
                    sent.set()
                    await publish.wait()
                    effects.append("published")
                effects.append("region-released")
            effects.append("operation-released")
            return "original-result"
        token = ctl.admit_work("real-governor-owner")
        waiter = asyncio.create_task(ctl.run_admitted(token, body()))
        try:
            await bounded(ticket_wait.wait())
            assert not server._node_locks_drain_busy() and effects == []
            _, attempt = ctl.request_restart(restart_body(ctl))
            await asyncio.sleep(0)  # Let the real independent coordinator inspect.
            assert not exits and ctl.snapshot()["active_operations"] == 1
            waiter.cancel()  # Caller loss does not cancel the real ticket owner.
            with pytest.raises(asyncio.CancelledError):
                await waiter
            assert not token.task.done() and not exits
            release_ticket.set()
            await bounded(sent.wait())
            assert server._node_locks_drain_busy() and effects == ["sent"]
            assert not exits and ctl.snapshot()["active_operations"] == 1
            publish.set()
            assert await bounded(asyncio.shield(token.task)) == "original-result"
            await bounded(ctl.wait_restart(attempt["restart_id"]))
            assert effects == ["sent", "published", "region-released", "operation-released"]
            assert exits == ["exit"] and not server._node_locks_drain_busy()
        finally:
            release_ticket.set()
            publish.set()
            if not token.task.done():
                await bounded(asyncio.shield(token.task))
    asyncio.run(run())


def test_registered_prepared_task_enters_its_own_boundary_after_drain_begins():
    async def run():
        exits = []
        ctl = controller(exit_callback=lambda: exits.append("exit"))
        enter, entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def prepared():
            await enter.wait()
            async with ctl.operation_scope("prepared-callback") as token:
                assert token.owner is asyncio.current_task()
                entered.set()
                await release.wait()
        task = asyncio.create_task(prepared())
        ctl.track_background_task(task, "prepared")
        _, attempt = ctl.request_restart(restart_body(ctl))
        enter.set()
        await bounded(entered.wait())
        assert ctl.snapshot()["active_operations"] == 1 and not exits
        release.set()
        await bounded(task)
        await bounded(ctl.wait_restart(attempt["restart_id"]))
        assert exits == ["exit"] and ctl.snapshot()["active_work"] == 0
    asyncio.run(run())


def test_cancel_before_waiter_first_instruction_still_keeps_reserved_body():
    async def run():
        exits = []
        ctl = controller(exit_callback=lambda: exits.append("exit"))
        started, release, published = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def body():
            started.set()
            await release.wait()
            published.set()
            return "actual-body"
        token = ctl.admit_work("pre-first-await")
        waiter = asyncio.create_task(ctl.run_admitted(token, body()))
        waiter.cancel()
        _, response = ctl.request_restart(restart_body(ctl))
        with pytest.raises(asyncio.CancelledError):
            await waiter
        await bounded(started.wait())
        assert ctl.snapshot()["active_work"] == 1 and not exits
        release.set()
        assert await bounded(asyncio.shield(token.task)) == "actual-body"
        await bounded(ctl.wait_restart(response["restart_id"]))
        assert published.is_set() and exits == ["exit"]
    asyncio.run(run())


def test_failed_coordinator_schedule_restores_service_without_leaking_coroutine(monkeypatch):
    from workflowy_mcp.coherence import operation_cache
    async def run():
        ctl = controller(exit_callback=lambda: pytest.fail("failed scheduling requested exit"))
        def unavailable(coro, **kwargs):
            raise RuntimeError("test-only scheduling failure")
        monkeypatch.setattr(operation_cache, "create_isolated_context_task", unavailable)
        status, result = ctl.request_restart(restart_body(ctl))
        assert status == 200 and result["error"]["reason"] == "control_failed"
        assert ctl.phase == "running" and ctl.snapshot()["active_work"] == 0
        token = ctl.admit_work("still-serving")
        async def ordinary():
            return "ok"
        assert await ctl.run_admitted(token, ordinary()) == "ok"
    asyncio.run(run())


def test_job_registered_before_coordinator_first_step_gets_one_stop_and_join_waits():
    async def run():
        requested, exits = [], []
        waiting, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def observe_wait(event, seconds):
            waiting.set()
            await lifecycle._wait_event(event, seconds)
        ctl = controller(exit_callback=lambda: exits.append("exit"), waiter=observe_wait)
        async def job():
            try:
                await release.wait()
            finally:
                finished.set()
        task = asyncio.create_task(job())
        ctl.track_background_task(task)
        _, result = ctl.request_restart(restart_body(ctl))
        # Synchronous registration races ahead of the independent coordinator.
        ctl.register_job_boundary_stop("late-register", requested.append, task)
        join = asyncio.create_task(ctl.join_work())
        await bounded(waiting.wait())
        assert requested == [result["restart_id"]] and not join.done() and not exits
        release.set()
        await bounded(task)
        await bounded(join)
        await bounded(ctl.wait_restart(result["restart_id"]))
        assert finished.is_set() and exits == ["exit"] and not ctl._job_stops
    asyncio.run(run())


def test_registered_refresh_recovery_handoff_remains_counted_after_pointer_clears(monkeypatch):
    from workflowy_mcp import server

    async def run():
        finalizing, finish = asyncio.Event(), asyncio.Event()
        effects, exits = [], []
        ctl = controller(exit_callback=lambda: exits.append("exit"))
        lifecycle.set_active_lifecycle(ctl)
        monkeypatch.setattr(server, "_nodes_export_refresh_task", None)
        monkeypatch.setattr(server, "_nodes_export_refresh_lock", asyncio.Lock())
        monkeypatch.setattr(server, "_nodes_export_refresh_idle_event", asyncio.Event())
        monkeypatch.setattr(server, "_nodes_export_refresh_pending_reasons", [])
        monkeypatch.setattr(server, "_nodes_export_refresh_pending_kinds", [])
        monkeypatch.setattr(server, "_nodes_export_refresh_pending_new_fetch", False)
        monkeypatch.setattr(server, "_nodes_export_refresh_pending_final_batches", {})
        monkeypatch.setattr(server, "_nodes_export_refresh_rerun_requested", False)
        monkeypatch.setattr(server, "_carto_final_live_attempts", set())
        async def external_status(*args, **kwargs):
            pass
        async def worker():
            pytest.fail("fixture must cancel this worker before its first instruction")
        async def finalizer(obligations, token, **kwargs):
            effects.append(("finalizing", token))
            finalizing.set()
            await finish.wait()
            effects.append(("terminalized", token))
        monkeypatch.setattr(server, "_emit_nodes_export_cache_status", external_status)
        monkeypatch.setattr(server, "_nodes_export_cache_refresh_worker", worker)
        monkeypatch.setattr(server, "_finish_carto_final_batch", finalizer)
        result = await server._request_nodes_export_cache_refresh_async(
            "test-only refresh", source="test", require_new_fetch=True,
            final_obligations=[{"job_id": "test-job", "incarnation": "test-incarnation"}],
            attempt_token="test-attempt",
        )
        assert result["scheduled"]
        original = server._nodes_export_refresh_task
        original.cancel()  # Existing user/transport failure; never restart-origin.
        _, response = ctl.request_restart(restart_body(ctl))
        await bounded(finalizing.wait())
        assert original.done() and server._nodes_export_refresh_task is None
        assert ctl.snapshot()["active_work"] == 1 and not exits
        assert effects == [("finalizing", "test-attempt")]
        finish.set()
        await bounded(ctl.wait_restart(response["restart_id"]))
        assert effects[-1] == ("terminalized", "test-attempt") and exits == ["exit"]
    asyncio.run(run())


def test_restart_stop_hook_never_clears_an_already_observed_boundary(monkeypatch):
    from workflowy_mcp import server

    async def run():
        release = asyncio.Event()
        task = asyncio.create_task(release.wait())
        state = {"restart_id": None, "observed": False}
        monkeypatch.setattr(server, "_inprocess_restart_stops", {task: state})
        server._request_inprocess_restart_stop("a" * 32, task=task)
        assert state == {"restart_id": "a" * 32, "observed": False}
        server._request_inprocess_restart_stop(None, task=task)
        assert state == {"restart_id": None, "observed": False}
        server._request_inprocess_restart_stop("b" * 32, task=task)
        state["observed"] = True
        server._request_inprocess_restart_stop(None, task=task)
        assert state["restart_id"] == "b" * 32 and state["observed"] is True
        release.set()
        await bounded(task)
    asyncio.run(run())


@pytest.mark.parametrize("groups", [1, 2])
def test_real_bulk_task_finishes_current_group_then_finalizes_before_claim_release(monkeypatch, isolated_controller_environment, groups):
    from pathlib import Path

    from coherence_harness import make_base_snapshot, make_boundary_capable
    from n06_harness import FAKE_KEY, FAKE_URL
    from workflowy_mcp import carto_job_governance as gov, mutation_locks, server

    scenario = isolated_controller_environment
    jobs = scenario.root / "bulk-jobs"
    target = scenario.root / "fixture.py"
    target.write_text("x = 1\n", encoding="utf-8")
    nodes = [{"id": "root"}, {"id": "file", "parent_id": "root"}]
    # Bulk file evidence resolves each frozen group through the pinned cache.
    # Give the synthetic group IDs real FILE paths, not a resolver bypass.
    for i in range(groups):
        group_target = scenario.root / f"fixture-{i}.py"
        group_target.write_text("x = 1\n", encoding="utf-8")
        nodes.append({"id": f"file-{i}", "parent_id": "root",
                      "note": f"Path: {group_target}"})
    snapshot = make_base_snapshot(scenario.root, nodes)
    scope = gov.derive_account_scope_id(FAKE_URL, FAKE_KEY)
    footprint = gov.compute_footprint(target, "file", {node["id"]: node for node in nodes}, kind="file")
    monkeypatch.setattr(server, "_get_carto_jobs_base_dir", lambda: str(jobs))
    monkeypatch.setattr(server, "_governance_jobs_root", lambda: jobs)
    monkeypatch.setattr(server, "_carto_account_scope", lambda: scope)
    monkeypatch.setattr(server, "_build_bulk_visible_root_from_payload", lambda payload: {"id": "file"})
    monkeypatch.setattr(server, "_carto_footprint_from_selection", lambda **kwargs: footprint)
    monkeypatch.setattr(server, "_carto_inprocess_tasks", {})
    monkeypatch.setattr(server, "_inprocess_restart_stops", {})
    monkeypatch.setattr(server, "_carto_final_live_attempts", set())
    # This retained group-boundary oracle fakes final refresh transport. D09
    # separately proves the real final-delivery handoff through drain.
    monkeypatch.setattr(server, "_carto_final_obligations", {})
    monkeypatch.setattr(server, "_ensure_carto_final_delivery", lambda: None)
    file_groups = {f"file-{i}": {"file_node": {"id": f"file-{i}", "name": f"file-{i}"}, "nodes": [{"id": f"node-{i}"}]} for i in range(groups)}
    monkeypatch.setattr(server, "_collect_bulk_visible_apply_groups", lambda *args, **kwargs: (file_groups, [], 0))

    async def run():
        started, finish_write, finalizing, finish_finalization = [asyncio.Event() for _ in range(4)]
        checked, cancel_checks = asyncio.Event(), []
        order, exits = [], []
        ctl = controller(exit_callback=lambda: (order.append("exit"), exits.append(True)))
        lifecycle.set_active_lifecycle(ctl)
        ctl.register_drain_probe("real-node-locks", lambda: bool(mutation_locks.registry_held_keys()))
        async def no_external_refresh(*args, **kwargs):
            pass
        async def no_coherence_barrier(*args, **kwargs):
            pass  # Coherence admission has independent S04/N05 tests, not this drain oracle.
        monkeypatch.setattr(server, "_await_nodes_export_cache_quiescent", no_external_refresh)
        monkeypatch.setattr(server, "_refresh_carto_job_tracker_once", no_external_refresh)
        monkeypatch.setattr("workflowy_mcp.coherence.admission.require_coherence_mutation_admission", no_coherence_barrier)
        class FakeClient:
            def _get_nodes_export_cache_nodes(self):
                return nodes
            async def refresh_file_node_beacons(self, **kwargs):
                order.append("refresh:" + kwargs["file_node_id"])
                return {"success": True, "nodes_updated": 1}
        lock_uuid = "06060606-0000-4000-8000-000000000002"
        client = make_boundary_capable(FakeClient(), nodes=[
            *nodes, {"id": lock_uuid, "name": "drain-probe", "parent_id": None},
        ])
        from types import SimpleNamespace
        client.api_key = FAKE_KEY
        client._write_governor = SimpleNamespace(paths=SimpleNamespace(
            jobs_root=jobs, account_scope_id=scope,
        ))
        monkeypatch.setattr(server, "get_client", lambda: client)
        # N07 common task grant covers the actual fake write funnel's UUID;
        # legacy group labels remain display-only fixture identities.
        from workflowy_mcp.mutation_lease_governance import build_region_subtree
        monkeypatch.setattr(server, "_carto_job_regions", lambda spec, proof: (
            build_region_subtree(lock_uuid, cache_by_id=proof.nodes_by_id),
        ))
        async def effects(*, summary, file_name, cancel_check, **kwargs):
            async with mutation_locks.mutation_node_locks(lock_uuid, client=client):
                order.append("write:" + file_name)
                started.set()
                await finish_write.wait()
                cancel_checks.append(cancel_check())
                checked.set()
                assert cancel_check() is False, "restart flag leaked into the group's send/user-cancel check"
                summary["node_updates"] += 1
                return 1, False
        monkeypatch.setattr(server, "_bulk_apply_per_file_inner_loop", effects)
        actual_finalizer = server._finalize_carto_task
        async def finalizer(*args, **kwargs):
            if not args[1].restart_state["observed"]:
                return await actual_finalizer(*args, **kwargs)
            order.append("finalizer:start")
            finalizing.set()
            try:
                await finish_finalization.wait()
            except asyncio.CancelledError:
                # Test-only cleanup: the existing runner may re-enter this
                # finalizer after its waiter is cancelled. Never strand that
                # second cleanup wait when a fault probe's assertion fails.
                finish_finalization.set()
                raise
            result = await actual_finalizer(*args, **kwargs)
            order.append("finalizer:done")
            return result
        monkeypatch.setattr(server, "_finalize_carto_task", finalizer)
        launched = await server._start_carto_bulk_visible_apply_job(
            root_uuid="file", mode="file", visible_tree_payload={}, selected_snapshot={"snapshot_path": str(snapshot)},
        )
        assert launched["success"], launched
        task = server._carto_inprocess_tasks[launched["job_id"]]
        await bounded(started.wait())
        assert mutation_locks.registry_held_keys()
        _, attempt = ctl.request_restart(restart_body(ctl))
        await asyncio.sleep(0)
        assert order == ["write:file-0"] and not exits
        finish_write.set()
        await bounded(checked.wait())
        if cancel_checks != [False]:
            finish_finalization.set()
        assert cancel_checks == [False], "restart leaked into the current group's cancellation callback"
        if groups > 1:
            await bounded(finalizing.wait())
            assert order == ["write:file-0", "refresh:file-0", "finalizer:start"]
            assert not exits and not task.done()
            claim_files = [hold for hold in mutation_locks._governance_holds(jobs)
                           if hold["job_id"] == launched["job_id"]]
            assert len(claim_files) == 1 and claim_files[0]["finalized"] is False  # Region ownership survives terminalization wait.
            payload = json.loads(Path(launched["carto_job_file"]).read_text())
            assert payload["status"] == "cancelling" and payload["result_summary"]["node_updates"] == 1
            finish_finalization.set()
        await bounded(task)
        await bounded(ctl.wait_restart(attempt["restart_id"]))
        payload = json.loads(Path(launched["carto_job_file"]).read_text())
        assert payload["status"] == ("cancelled" if groups > 1 else "completed")
        assert payload["result_summary"]["files_refreshed"] == 1 and payload["result_summary"]["node_updates"] == 1
        assert not [hold for hold in mutation_locks._governance_holds(jobs)
                    if hold["job_id"] == launched["job_id"]]
        assert order[-1] == "exit" and exits == [True]
        assert not server._carto_inprocess_tasks and not server._inprocess_restart_stops
        if groups == 1:
            assert order == ["write:file-0", "refresh:file-0", "exit"]  # Last group wins normally.
    asyncio.run(run())


@pytest.mark.parametrize("restart_stop", [False, True])
def test_finalizer_default_and_explicit_boundary_persist_before_release(monkeypatch, isolated_controller_environment, restart_stop):
    from workflowy_mcp import carto_job_governance as gov

    jobs = isolated_controller_environment.root / "finalizer-proof"
    jobs.mkdir()
    path = jobs / "n06-finalizer.json"
    path.write_text(json.dumps({
        "id": "n06-finalizer", "type": "CARTO_BULK_APPLY", "status": "cancelled", "error": None,
        "result_summary": {"node_updates": 7}, "cache_refresh_required": True,
    }), encoding="utf-8")
    observed = []
    async def release(claim_id, root, **kwargs):
        record = json.loads(path.read_text(encoding="utf-8"))
        observed.append(record["status"])
        assert record["status"] == "cancelled", "terminal status must be persisted BEFORE claim release"
    monkeypatch.setattr(gov, "finalize_claim_async", release)
    async def run():
        # Default really omits the new argument; the observed user/restart stop
        # is memory finalization only, with no sentinel-derived outcome.
        proof = {"restart_boundary_stop": True} if restart_stop else {}
        result = await gov.finalize_cancelled_job_claim_async(path, "test-claim", jobs_root=jobs, **proof)
        assert result == "cancelled" and observed == ["cancelled"]
    asyncio.run(run())
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["result_summary"] == {"node_updates": 7} and record["cache_refresh_required"] is True
    assert record.get("error") is None
    assert not hasattr(gov, "cancellation_sentinel_path")
    assert not hasattr(gov, "allow_legacy_cancelled")


def test_probe_alone_blocks_exit_until_authority_reports_clear():
    async def run():
        held, exits = [True], []
        observed = asyncio.Event()
        ctl = controller(exit_callback=lambda: exits.append("exit"))
        def authority():
            observed.set()
            return held[0]
        ctl.register_drain_probe("actual-held-state", authority)
        _, response = ctl.request_restart(restart_body(ctl))
        # A bounded fixture guard is not a replacement for the authority oracle.
        await asyncio.sleep(0)
        assert not exits and ctl.phase == "draining" and observed.is_set()
        held[0] = False
        ctl._notify()
        await bounded(ctl.wait_restart(response["restart_id"]))
        assert exits == ["exit"]
    asyncio.run(run())


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_own_busy_child_retries_same_argv_env_flags_within_existing_budget(monkeypatch, platform):
    from workflowy_mcp import daemon, broker_protocol

    clock, starts, observations = Clock(), [], []
    monkeypatch.setattr(daemon, "time", SimpleNamespace(monotonic=clock, sleep=lambda seconds: setattr(clock, "now", clock.now + seconds)))
    monkeypatch.setattr(daemon, "sys", SimpleNamespace(platform=platform, executable=sys.executable, stderr=sys.stderr))
    monkeypatch.setattr(broker_protocol, "ensure_log_dir", lambda: None)
    monkeypatch.setattr(daemon, "is_daemon_listening", lambda: len(starts) >= 2 and clock.now >= 101)
    class Child:
        def __init__(self, index):
            self.index = index
        def poll(self):
            observations.append((self.index, clock.now))
            return 75 if self.index == 0 else None
    def popen(argv, **kwargs):
        starts.append((list(argv), kwargs, clock.now))
        return Child(len(starts) - 1)
    monkeypatch.setattr(daemon.subprocess, "Popen", popen)
    assert daemon.ensure_daemon_running(timeout_s=10)
    assert len(starts) == 2 and starts[1][2] - starts[0][2] == 0.5
    for argv, kwargs, _ in starts:
        assert argv == [sys.executable, "-m", "workflowy_mcp", "--transport", "http"]
        assert kwargs["close_fds"] is True and kwargs["env"]["WORKFLOWY_MCP_TRANSPORT"] == "http"
        assert kwargs["stdin"] == daemon.subprocess.DEVNULL
        if platform == "win32":
            assert "creationflags" in kwargs and "start_new_session" not in kwargs and "stdout" not in kwargs
        else:
            assert kwargs["start_new_session"] is True and kwargs["stdout"] == daemon.subprocess.DEVNULL
    assert observations[0] == (0, 100.0)


@pytest.mark.parametrize(("code", "expected_spawns"), [(1, 1), (74, 1), (2, 1), (None, 1), (75, 5)])
def test_only_own_busy_exit_is_relaunched_never_a_live_child(monkeypatch, code, expected_spawns):
    from workflowy_mcp import daemon, broker_protocol

    clock, starts = Clock(), []
    monkeypatch.setattr(daemon, "time", SimpleNamespace(monotonic=clock, sleep=lambda seconds: setattr(clock, "now", clock.now + seconds)))
    monkeypatch.setattr(broker_protocol, "ensure_log_dir", lambda: None)
    monkeypatch.setattr(daemon, "is_daemon_listening", lambda: False)
    def popen(*args, **kwargs):
        starts.append(clock.now)
        return SimpleNamespace(poll=lambda: code)
    monkeypatch.setattr(daemon.subprocess, "Popen", popen)
    assert daemon.ensure_daemon_running(timeout_s=10) is False
    assert len(starts) == expected_spawns
    assert clock.now <= 110
    if code == 75:
        assert starts == [100, 100.5, 102, 104.5, 109]


@pytest.mark.parametrize("host", ["0.0.0.0", "127.0.0.1"])
def test_owned_runner_uses_real_host_policy_lifespan_and_no_grace_cut(monkeypatch, host, isolated_controller_environment):
    import fastmcp
    import uvicorn
    from workflowy_mcp import daemon

    observed = {}
    monkeypatch.setattr(fastmcp.settings, "http_host_origin_protection", "auto")
    monkeypatch.setattr(fastmcp.settings, "http_allowed_hosts", None)
    class App:
        def custom_route(self, path, methods):
            observed["control_path"] = path
            return lambda handler: handler
        def http_app(self, **kwargs):
            observed["app"] = kwargs
            return "same-app"
        @asynccontextmanager
        async def _lifespan_manager(self):
            observed["startup"] = observed.get("startup", 0) + 1
            ctl = controller(restart_path=observed["control_path"])
            lifecycle.set_active_lifecycle(ctl)
            yield
            lifecycle.set_active_lifecycle(None)
            observed["teardown"] = True
    class Server:
        def __init__(self, config):
            assert observed["startup"] == 1
            observed["config"] = config
            self.should_exit = False
        async def serve(self):
            assert self.should_exit is False
            ctl = lifecycle.active_lifecycle()
            held = ctl.admit_work("held-during-runner")
            _, result = ctl.request_restart(restart_body(ctl))
            assert not self.should_exit
            async def finish():
                return True
            await ctl.run_admitted(held, finish())
            await bounded(ctl.wait_restart(result["restart_id"]))
            assert self.should_exit is True
    monkeypatch.setattr(uvicorn, "Server", Server)
    port = int(isolated_controller_environment.env["WORKFLOWY_MCP_HTTP_PORT"])
    asyncio.run(daemon.run_http_daemon_async(App(), token="test-only-control-token", host=host, port=port, path="/mcp", stateless_http=True))
    config = observed["config"]
    assert config.app == "same-app" and config.port == port and config.host == host
    assert config.lifespan == "on" and config.ws == "websockets-sansio" and config.timeout_graceful_shutdown is None
    assert observed["app"]["allowed_hosts"] == (None if host == "0.0.0.0" else ["127.0.0.1"])
    assert observed["app"]["stateless_http"] is True and observed["teardown"]


def test_expiry_restores_running_keeps_work_and_one_deadline_for_twenty_joiners(monkeypatch):
    async def run():
        clock, exits = Clock(), []
        ctl = controller(clock=clock, exit_callback=lambda: exits.append("exit"))
        started, release = asyncio.Event(), asyncio.Event()
        stop_requests = []
        async def work():
            started.set()
            await release.wait()
            return "still-alive"
        token = ctl.admit_work("held-write")
        task = asyncio.create_task(ctl.run_admitted(token, work()))
        await bounded(started.wait())
        ctl.register_job_boundary_stop("future-job", stop_requests.append, token.task)
        status, first = ctl.request_restart(restart_body(ctl))
        initial = ctl._attempt
        deadline = initial.deadline
        assert status == 202 and deadline == 3700
        clock.now += 100
        joins = [ctl.request_restart(restart_body(ctl, bound=99999, source_time=30, commit="c")) for _ in range(20)]
        assert all(code == 202 and not item["accepted"] and item["restart_id"] == first["restart_id"] for code, item in joins)
        assert ctl._attempt is initial and initial.deadline == deadline
        assert initial.target["commit"] == "c" * 40 and initial.bound == 3600
        # Let the real coordinator issue its non-cancelling job request.
        await asyncio.sleep(0)
        assert stop_requests == [first["restart_id"]]
        clock.now = deadline
        ctl._notify()
        result = await bounded(ctl.wait_restart(first["restart_id"]))
        assert result["status"] == "restart_failed" and result["error"]["reason"] == "drain_timeout"
        assert result["error"]["outcome"] == "not_sent" and result["error"]["retry_after"] == 30
        assert ctl.phase == "running" and not exits and not token.task.done()
        assert stop_requests == [first["restart_id"], None]
        next_token = ctl.admit_work("service-resumed")
        async def ordinary():
            return "ok"
        assert await ctl.run_admitted(next_token, ordinary()) == "ok"
        release.set()
        assert await bounded(task) == "still-alive"
        _, later = ctl.request_restart(restart_body(ctl))
        assert later["restart_id"] != first["restart_id"]
        assert ctl.snapshot(first["restart_id"])["status"] == "restart_superseded"
        await bounded(ctl.wait_restart(later["restart_id"]))
        assert exits == ["exit"]
    asyncio.run(run())


def test_independent_child_reserved_during_drain_survives_parent_and_finally():
    async def run():
        ctl = controller(exit_callback=lambda: exits.append("exit"))
        exits, order = [], []
        parent_started, handoff, child_started, finish_send, cleanup_done = [asyncio.Event() for _ in range(5)]
        async def child():
            try:
                async with ctl.operation_scope("child-view"):
                    child_started.set()
                    await finish_send.wait()
                    order.append("send")
            finally:
                order.append("cleanup")
                cleanup_done.set()
        async def parent():
            async with ctl.operation_scope("parent-view") as outer:
                async with ctl.operation_scope("nested") as nested:
                    assert nested is outer
                    assert ctl.snapshot()["active_operations"] == 1
                parent_started.set()
                await handoff.wait()
                ctl.spawn_admitted_child("independent", child())
                order.append("parent-return")
        token = ctl.admit_work("parent")
        parent_waiter = asyncio.create_task(ctl.run_admitted(token, parent()))
        await bounded(parent_started.wait())
        _, response = ctl.request_restart(restart_body(ctl))
        handoff.set()
        await bounded(parent_waiter)
        assert ctl.snapshot()["active_work"] == 1
        await bounded(child_started.wait())
        assert ctl.snapshot()["active_work"] == ctl.snapshot()["active_operations"] == 1
        assert not exits and order == ["parent-return"]
        finish_send.set()
        await bounded(cleanup_done.wait())
        await bounded(ctl.wait_restart(response["restart_id"]))
        assert order == ["parent-return", "send", "cleanup"] and exits == ["exit"]
    asyncio.run(run())


def test_an_inherited_token_is_not_authority_for_an_independent_task():
    async def run():
        ctl = controller(exit_callback=lambda: None)
        parent_started, finish = asyncio.Event(), asyncio.Event()
        async def parent():
            parent_started.set()
            await finish.wait()
            async def unrelated():
                async with ctl.operation_scope("illegal-borrow"):
                    pytest.fail("inherited foreign-owner token bypassed drain admission")
            other = asyncio.create_task(unrelated())  # Deliberately copied ContextVars.
            with pytest.raises(lifecycle.RestartingError):
                await other
        token = ctl.admit_work("parent")
        waiter = asyncio.create_task(ctl.run_admitted(token, parent()))
        await bounded(parent_started.wait())
        _, response = ctl.request_restart(restart_body(ctl))
        finish.set()
        await bounded(waiter)
        await bounded(ctl.wait_restart(response["restart_id"]))
    asyncio.run(run())


def test_probe_and_background_finalization_are_not_replaced_by_status_strings(capsys):
    async def run():
        clock, exits, states = Clock(), [], {"held": True}
        ctl = controller(clock=clock, exit_callback=lambda: exits.append("exit"))
        started, finish, finalizing, final_release = [asyncio.Event() for _ in range(4)]
        async def job():
            try:
                started.set()
                await finish.wait()
            finally:
                finalizing.set()
                await final_release.wait()
        task = asyncio.create_task(job())
        ctl.track_background_task(task, "prepared-or-etch")
        ctl.register_drain_probe("held-regions", lambda: states["held"])
        await bounded(started.wait())
        _, response = ctl.request_restart(restart_body(ctl))
        finish.set()
        await bounded(finalizing.wait())
        assert ctl.snapshot()["active_background"] == 1 and not exits
        final_release.set()
        await bounded(task)
        assert states["held"] and not exits
        states["held"] = False
        ctl._notify()
        await bounded(ctl.wait_restart(response["restart_id"]))
        assert exits == ["exit"]
        # A broken future lifecycle probe cannot declare false quiescence.
        broken = controller(clock=clock, exit_callback=lambda: exits.append("unsafe"))
        def unavailable():
            raise ValueError("not a public diagnostic")
        broken.register_drain_probe("future-shield", unavailable)
        _, attempt = broken.request_restart(restart_body(broken))
        await asyncio.sleep(0)
        broken.register_drain_probe("future-shield", lambda: False)
        clock.now += 3600
        broken._notify()
        result = await bounded(broken.wait_restart(attempt["restart_id"]))
        assert result["error"]["reason"] == "drain_timeout" and exits == ["exit"]
    asyncio.run(run())
    assert "treating it as busy for this restart attempt" in capsys.readouterr().err


@pytest.mark.parametrize("method", ["on_call_tool", "on_read_resource", "on_get_prompt"])
def test_middleware_plain_pass_through_without_controller(method):
    async def run():
        result = object()
        async def handler(context):
            return result
        assert await getattr(lifecycle.DaemonLifecycleMiddleware(), method)(SimpleNamespace(method="test"), handler) is result
    asyncio.run(run())


def test_middleware_preserves_metadata_and_gates_every_tool_resource_and_prompt():
    async def run():
        ctl = controller(exit_callback=lambda: None)
        lifecycle.set_active_lifecycle(ctl)
        middleware = lifecycle.DaemonLifecycleMiddleware()
        original = InitializeResult(protocolVersion="2025-03-26", capabilities={}, serverInfo=Implementation(name="original", version="version"), instructions="unchanged", _meta={"other": 42})
        async def initialize(context):
            return original
        result = await middleware.on_initialize(SimpleNamespace(), initialize)
        assert result.meta["other"] == 42 and result.meta[DAEMON_META_KEY] == ctl.metadata()
        assert result.serverInfo == original.serverInfo and result.instructions == "unchanged"
        assert original.meta == {"other": 42}
        discover = {"serverInfo": {"name": "original"}, "_meta": {"other": 42}}
        async def discovery(context):
            return discover
        projected = await middleware.on_discover(None, discovery)
        assert projected["_meta"][DAEMON_META_KEY] == ctl.metadata() and discover["_meta"] == {"other": 42}
        # Hold a reserved token without beginning a handler, so draining persists.
        held = ctl.admit_work("pre-schedule")
        _, response = ctl.request_restart(restart_body(ctl))
        draining = await middleware.on_initialize(None, initialize)
        assert draining.meta[DAEMON_META_KEY]["phase"] == "draining"
        assert draining.meta[DAEMON_META_KEY]["restart_id"] == response["restart_id"]
        effects = []
        async def forbidden(context):
            effects.append(context.method)
            return lifecycle.ToolResult(content=[])
        for method in ("system.ping", "system.job.cancel", "system.cache.refresh", "slice_group.list", "mutation"):
            refused = await middleware.on_call_tool(SimpleNamespace(method=method), forbidden)
            assert refused.is_error and refused.structured_content["error"]["retry_after"] == 30
            assert json.loads(refused.content[0].text) == refused.structured_content
        for method in ("on_read_resource", "on_get_prompt"):
            with pytest.raises(MCPError) as raised:
                await getattr(middleware, method)(SimpleNamespace(method=method), forbidden)
            assert raised.value.error.code == -32070 and raised.value.error.data["outcome"] == "not_sent"
        assert effects == []
        async def release():
            return "admitted-before-drain"
        assert await ctl.run_admitted(held, release()) == "admitted-before-drain"
        await bounded(ctl.wait_restart(response["restart_id"]))
        exiting = await middleware.on_discover(None, discovery)
        assert exiting["_meta"][DAEMON_META_KEY]["phase"] == "exiting"
        refused = await middleware.on_call_tool(SimpleNamespace(method="mutation"), forbidden)
        assert refused.is_error and refused.structured_content["error"]["retry_after"] == 1
        assert not effects
    asyncio.run(run())


@pytest.mark.parametrize("fault", ["not_dict", "protocol", "bool_protocol", "instance", "target_type", "version_type", "bad_commit", "bad_time", "requester_type", "requester_bool", "bound_type"])
def test_invalid_restart_request_never_changes_phase(fault):
    async def run():
        ctl = controller(exit_callback=lambda: pytest.fail("invalid request stopped service"))
        body = restart_body(ctl)
        if fault == "not_dict": body = []
        elif fault == "protocol": body["protocol"] = 2
        elif fault == "bool_protocol": body["protocol"] = True
        elif fault == "instance": body["expected_instance_id"] = "not-an-instance"
        elif fault == "target_type": body["target_identity"] = []
        elif fault == "version_type": body["target_identity"]["package_version"] = None
        elif fault == "bad_commit": body["target_identity"]["commit"] = "abbreviated"
        elif fault == "bad_time": body["target_identity"]["commit_time"] = True
        elif fault == "requester_type": body["requester"] = None
        elif fault == "requester_bool": body["requester"]["is_wsl"] = "false"
        elif fault == "bound_type": body["drain_timeout_s"] = {"not": "a numeric bound"}
        status, payload = ctl.request_restart(body)
        assert status == 400 and payload["status"] == "invalid_restart_request"
        assert ctl.phase == "running" and ctl._attempt is None
    asyncio.run(run())


@pytest.mark.parametrize(("change", "expected"), [("equal", "not_required"), ("older", "not_required"), ("unknown", "not_required"), ("wsl", "attach_only"), ("host", "attach_only"), ("platform", "attach_only")])
def test_control_rechecks_source_order_and_affinity_without_stopping(change, expected):
    async def run():
        ctl = controller(exit_callback=lambda: pytest.fail("ineligible stop"))
        body = restart_body(ctl)
        if change == "equal": body["target_identity"] = ctl.identity.source_payload()
        elif change == "older": body["target_identity"]["commit_time"] = 1
        elif change == "unknown": body["target_identity"]["commit"] = None
        elif change == "wsl": body["requester"]["is_wsl"] = True
        elif change == "host": body["requester"]["hostname"] = "elsewhere"
        elif change == "platform": body["requester"]["platform"] = "win32"
        status, payload = ctl.request_restart(body)
        assert status == 200 and payload["status"] == expected and ctl._attempt is None
        assert ctl.phase == "running"
        if change == "equal":
            assert not payload["warnings"]
        else:
            assert payload["warnings"]
    asyncio.run(run())


@pytest.mark.parametrize("reason", ["drain_timeout", "control_failed", "exit_timeout", "spawn_failed"])
def test_restart_failure_envelope_names_only_its_safe_remedy(reason):
    error = lifecycle.RestartFailedError(reason, drain_timeout_s=7200, url="http://127.0.0.1:12345/mcp")
    payload = error.payload()
    assert not payload["success"]
    assert error.error["code"] == "restart_failed" and error.error["reason"] == reason
    assert error.error["retryable"] is True and error.error["retry_after"] == 30 and error.error["outcome"] == "not_sent"
    assert json.loads(error.tool_result().content[0].text) == payload
    if reason == "drain_timeout":
        assert "7200 seconds" in str(error) and "admitted work was not cancelled" in str(error)
        assert error.error["recovery"] == "retry_attach_after_active_work"
    elif reason == "control_failed":
        assert "existing daemon is still serving" in str(error) and "No process was killed" in str(error)
    else:
        assert "Start this same upgraded installation" in str(error) and "Do not delete writer.lock or clear the uv cache." in str(error)


def test_unknown_forwarded_outcome_never_authorizes_a_blind_retry():
    error = lifecycle.UpstreamOutcomeUnknownError()
    assert error.error == {
        "code": "upstream_outcome_unknown",
        "message": "The connection ended after this call was forwarded; its outcome is unknown and the shim did not resend it. Read back the affected state before deciding whether to issue a new call.",
        "retryable": False, "outcome": "unknown", "forwarded": True, "recovery": "read_back_before_retry",
    }
    assert error.mcp_error().code == -32071
    unavailable = lifecycle.DaemonUnavailableError("http://127.0.0.1:12345/mcp")
    assert unavailable.error["outcome"] == "not_sent" and unavailable.error["retry_after"] == 30
    assert "Start the native-host shim with workflowy-mcp-shim" in str(unavailable)


def test_control_auth_stale_instance_and_long_poll_do_not_own_coordinator(isolated_controller_environment):
    async def run():
        ctl = controller(exit_callback=lambda: exits.append("exit"))
        lifecycle.set_active_lifecycle(ctl)
        app_server, exits = FastMCP("test-control"), []
        lifecycle.install_daemon_control_routes(app_server, "test-only-control-token", ctl.restart_path)
        app = app_server.http_app(path="/mcp", stateless_http=True, host_origin_protection=True, allowed_hosts=["127.0.0.1"])
        base_url = f"http://127.0.0.1:{isolated_controller_environment.env['WORKFLOWY_MCP_HTTP_PORT']}"
        isolated_controller_environment.assert_endpoint()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base_url, follow_redirects=False) as client:
            path = ctl.restart_path
            body = restart_body(ctl)
            assert (await client.post(path, json=body)).status_code == 401
            assert ctl._attempt is None
            headers = {"Authorization": "Bearer test-only-control-token"}
            invalid = await client.post(path, content="invalid json", headers=headers)
            assert invalid.status_code == 400
            stale = copy.deepcopy(body)
            stale["expected_instance_id"] = "d" * 32
            mismatch = await client.post(path, json=stale, headers=headers)
            assert mismatch.status_code == 409 and mismatch.json()["daemon"] == ctl.metadata()
            assert ctl._attempt is None
            held = ctl.admit_work("publication")
            body["future_addition"] = "ignored"
            accepted = await client.post(path, json=body, headers=headers)
            assert accepted.status_code == 202 and accepted.json()["accepted"]
            assert accepted.json()["warnings"][0]["code"] == "identity_unknown"
            restart_id = accepted.json()["restart_id"]
            joined = await client.post(path, json=body, headers=headers)
            assert joined.status_code == 202 and not joined.json()["accepted"]
            status = await client.get(path, headers=headers)
            assert status.status_code == 200 and status.json()["phase"] == "draining"
            poll = asyncio.create_task(client.get(path, params={"restart_id": restart_id, "wait": "1"}, headers=headers))
            await asyncio.sleep(0)
            poll.cancel()  # Only the HTTP waiter; not the daemon coordinator.
            with pytest.raises(asyncio.CancelledError):
                await poll
            assert ctl._coordinator is not None and not ctl._coordinator.done() and not exits
            async def finish():
                return "sent"
            assert await ctl.run_admitted(held, finish()) == "sent"
            complete = await bounded(client.get(path, params={"restart_id": restart_id, "wait": "1"}, headers=headers))
            assert complete.status_code == 200 and complete.json()["phase"] == "exiting" and exits == ["exit"]
            wrong_host = await client.get(path, headers={**headers, "Host": "untrusted.invalid"})
            assert wrong_host.status_code in {400, 403, 421}
    asyncio.run(run())


WS_ACTIONS = (
    "resolve_uuid_path", "list_anchors_for_widget", "refresh_nodes_export_cache",
    "set_cache_refresh_skip", "save_nodes_export_cache", "open_node_in_windsurf",
    "refresh_file_node", "refresh_folder_node", "bulk_apply_visible_nodes",
    "generate_markdown_file", "materialize_markdown_file", "classify_bulk_materialize",
    "bulk_materialize_markdown", "classify_bulk_f12p3", "bulk_generate_markdown",
    "render_markdown_modal", "compile_slice_search", "f8_expansion_plan",
    "render_slice_modal", "render_slice_row", "render_lightning_dom_capture",
    "mutate_node_tags", "merge_slice_groups", "anchor_dock_update",
    "carto_list_jobs", "carto_clear_job_history", "carto_cancel_job",
)


@pytest.mark.parametrize("action", WS_ACTIONS + ("render_slice_row:rows",))
def test_every_ws_action_refuses_before_effects_with_exact_correlation(monkeypatch, action):
    from workflowy_mcp import server

    async def run():
        ctl = controller(exit_callback=lambda: None)
        lifecycle.set_active_lifecycle(ctl)
        held = ctl.admit_work("keeps-drain-open")
        _, attempt = ctl.request_restart(restart_body(ctl))
        effects, replies = [], []
        def forbidden(*args, **kwargs):
            effects.append("handler")
            raise AssertionError("a refused WS handler ran")
        for name in dir(server):
            if name.startswith("_handle_") or name in {"get_client", "_set_cache_refresh_skip", "_carto_list_jobs_poll", "_request_nodes_export_cache_refresh_async", "_resolve_uuid_path_and_respond"}:
                monkeypatch.setattr(server, name, forbidden)
        class WS:
            async def send(self, frame):
                replies.append(json.loads(frame))
        actual = action.split(":")[0]
        frame = {"action": actual, "request_id": "request", "endpoint_id": "endpoint", "node_id": "node", "target_uuid": "target"}
        if action.endswith(":rows"):
            frame["render_spec"] = {"focus": {"mode": "rows", "rows": [{"uuid": "target"}]}}
        await server._dispatch_ws_work(frame, WS())
        expected_action = "uuid_path_result" if actual == "resolve_uuid_path" else f"{actual}_result"
        if action.endswith(":rows"):
            expected_action = "render_slice_modal_result"
        assert replies == [{
            "action": expected_action, "success": False,
            "error": "Workflowy daemon is restarting; this call was not admitted. Retry after 30 seconds.",
            "error_code": "restarting", "retryable": True, "retry_after": 30,
            "outcome": "not_sent", "restart_id": attempt["restart_id"],
            **{key: frame[key] for key in ("request_id", "endpoint_id", "node_id", "target_uuid")},
        }]
        assert effects == [] and ctl.snapshot()["active_work"] == 1
        async def finish():
            return None
        await ctl.run_admitted(held, finish())
        await bounded(ctl.wait_restart(attempt["restart_id"]))
    asyncio.run(run())


def test_real_poll_child_stays_counted_through_send_and_finally(monkeypatch):
    from workflowy_mcp import server

    monkeypatch.setattr(server, "_carto_list_jobs_in_flight", False)
    monkeypatch.setattr(server, "_carto_list_jobs_last_started", float("-inf"))
    # A real scanner row's key (job_id): the widget reply keeps only widget fields.
    monkeypatch.setattr(server, "_scan_carto_job_files", lambda **_view: [{"job_id": "visible", "status": "running"}])
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        exits, replies = [], []
        ctl = controller(exit_callback=lambda: exits.append("exit"))
        lifecycle.set_active_lifecycle(ctl)
        class WS:
            async def send(self, frame):
                started.set()
                await release.wait()
                replies.append(json.loads(frame))
        await server._dispatch_ws_work({"action": "carto_list_jobs"}, WS())
        await bounded(started.wait())
        assert server._carto_list_jobs_in_flight
        assert ctl.snapshot()["active_work"] == 1
        _, attempt = ctl.request_restart(restart_body(ctl))
        await asyncio.sleep(0)
        assert not exits and not replies and server._carto_list_jobs_in_flight
        release.set()
        await bounded(ctl.wait_restart(attempt["restart_id"]))
        assert not server._carto_list_jobs_in_flight and exits == ["exit"]
        assert replies == [{"action": "carto_list_jobs_result", "success": True, "jobs": [{"job_id": "visible", "status": "running"}]}]
    asyncio.run(run())


def test_ws_inline_body_is_shielded_after_pump_waiter_disconnect(monkeypatch):
    from workflowy_mcp import server

    async def run():
        started, release, published = asyncio.Event(), asyncio.Event(), asyncio.Event()
        exits, effects = [], []
        ctl = controller(exit_callback=lambda: exits.append("exit"))
        lifecycle.set_active_lifecycle(ctl)
        async def classify(data, websocket):
            effects.append("entered")
            started.set()
            await release.wait()
            effects.append("published")
            published.set()
        monkeypatch.setattr(server, "_handle_classify_bulk_materialize", classify)
        waiter = asyncio.create_task(server._dispatch_ws_work({"action": "classify_bulk_materialize"}, None))
        await bounded(started.wait())
        _, attempt = ctl.request_restart(restart_body(ctl))
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert ctl.snapshot()["active_work"] == 1 and effects == ["entered"] and not exits
        release.set()
        await bounded(published.wait())
        await bounded(ctl.wait_restart(attempt["restart_id"]))
        assert effects == ["entered", "published"] and exits == ["exit"]
    asyncio.run(run())


def test_ws_pump_routes_direction_a_and_ping_while_child_drains(monkeypatch):
    from workflowy_mcp import server

    async def run():
        started, got_reply, release, pong, refused = [asyncio.Event() for _ in range(5)]
        exits, messages = [], []
        ctl = controller(exit_callback=lambda: exits.append("exit"))
        lifecycle.set_active_lifecycle(ctl)
        class WS:
            remote_address = "fixture"
            def __init__(self):
                self.frames = asyncio.Queue()
            def __aiter__(self):
                return self
            async def __anext__(self):
                frame = await self.frames.get()
                if frame is None:
                    raise StopAsyncIteration
                return json.dumps(frame)
            async def send(self, raw):
                frame = json.loads(raw)
                messages.append(frame)
                if frame.get("action") == "pong":
                    pong.set()
                if frame.get("error_code") == "restarting":
                    refused.set()
        async def handler(data, websocket):
            started.set()
            reply = await server._ws_message_queue.get()
            assert reply == {"action": "nested_reply", "request_id": "one", "payload": "actual"}
            got_reply.set()
            await release.wait()
            await websocket.send(json.dumps({"action": "compile_slice_search_result", "request_id": "one"}))
        monkeypatch.setattr(server, "_handle_compile_slice_search", handler)
        ws = WS()
        await ws.frames.put({"action": "compile_slice_search", "request_id": "one"})
        pump = asyncio.create_task(server.websocket_handler(ws))
        await bounded(started.wait())
        _, attempt = ctl.request_restart(restart_body(ctl))
        for frame in ({"action": "hello"}, {"action": "ping"}, {"action": "carto_cancel_job", "request_id": "two"}, {"action": "nested_reply", "request_id": "one", "payload": "actual"}):
            await ws.frames.put(frame)
        await bounded(pong.wait())
        await bounded(refused.wait())
        await bounded(got_reply.wait())
        assert not exits and ctl.snapshot()["active_work"] == 1
        release.set()
        await bounded(ctl.wait_restart(attempt["restart_id"]))
        await ws.frames.put(None)
        await bounded(pump)
        assert exits == ["exit"] and messages[-1]["action"] == "compile_slice_search_result"
    asyncio.run(run())


def test_real_server_lifespan_registers_one_controller_and_identity_without_second_bootstrap(monkeypatch):
    from workflowy_mcp import daemon_identity, server

    events = []
    identity = fixture_identity()
    monkeypatch.setattr(daemon_identity, "_snapshot", identity)
    monkeypatch.setattr(server.server_bootstrap, "acquire_single_writer_guard", lambda config: events.append("guard"))
    monkeypatch.setattr(server, "setup_logging", lambda config: None)
    class FakeClient:
        def __init__(self, *args, **kwargs):
            events.append("client")
        async def close(self):
            events.append("close")
    monkeypatch.setattr(server, "WorkFlowyClient", FakeClient)
    async def idle():
        await asyncio.Event().wait()
    assert not hasattr(server, "_sweep_carto_governance"), "retired startup sweep is still importable"
    monkeypatch.setattr(server, "connect_to_broker", idle)
    monkeypatch.setattr(server, "_carto_jobs_watcher_loop", idle)
    async def run():
        async with Client(server.mcp, mode="legacy") as client:
            ctl = lifecycle.active_lifecycle()
            assert ctl is not None and ctl.phase == "running"
            assert set(ctl._probes) == {"node_locks", "daemon_tasks"}
            assert ctl.identity is identity
            initialized = client.initialize_result
            ping = await server._system_ping_impl()
            assert ping["daemon"] == initialized.meta[DAEMON_META_KEY] == ctl.metadata()
            assert ping["package_commit"] == "a" * 40 and ping["package_commit_time"] == 10
            assert ping["identity_warnings"] == [] and ping["package_version"] == "1.0.1"
            assert ping["package_path"] and ping["pid"] == os.getpid() and ping["registered_tool_count"] > 0
            held = ctl.admit_work("before-drain")
            _, attempt = ctl.request_restart(restart_body(ctl))
            result = await client.call_tool_mcp("system", {"verb": "ping", "params": {}})
            assert result.is_error and result.structured_content["error"]["outcome"] == "not_sent"
            async def release():
                return None
            await ctl.run_admitted(held, release())
            # stdio has no HTTP runner exit callback: it must not exit itself.
            failed = await bounded(ctl.wait_restart(attempt["restart_id"]))
            assert failed["error"]["reason"] == "control_failed" and ctl.phase == "running"
        assert lifecycle.active_lifecycle() is None
    asyncio.run(run())
    assert events == ["guard", "client", "close"]


@pytest.mark.parametrize(("count", "hold_exit", "crash"), [(1, False, False), (2, False, False), (20, False, False), (1, True, False), (1, False, True)])
def test_real_old_daemon_to_new_shim_overlay_drains_write_exits_and_respawns(monkeypatch, count, hold_exit, crash):
    """T06 real package/guard/runner/stdio-shim; only external work is fake."""
    from fastmcp.client.transports import StdioTransport
    from workflowy_mcp.daemon import daemon_url

    scenario = Scenario.create()
    scenario.install_environment(monkeypatch)
    lane = scenario.lane("upgrade")
    old = scenario.installation_overlay("old", commit="a" * 40, source_time=10)
    new = scenario.installation_overlay("new", commit="b" * 40, source_time=20)
    old_env, new_env = scenario.overlay_environment(old), scenario.overlay_environment(new)
    if hold_exit:
        old_env["_N06_HOLD_EXIT"] = "1"
    process = scenario.start_overlay(old_env)

    async def marker(name):
        deadline = asyncio.get_running_loop().time() + SETUP_WAIT
        while not (lane / name).exists():
            if name == "ready-a" and process.poll() is not None:
                result = collect(process)
                raise AssertionError(f"test old overlay exited {result.returncode}: {result.stderr}")
            assert asyncio.get_running_loop().time() < deadline, name
            await asyncio.sleep(0.02)

    async def run(new_hosts):
        await marker("ready-a")
        scenario.assert_endpoint()
        url = daemon_url()
        async def use_new(index):
            transport = StdioTransport(
                command=sys.executable, args=["-m", "workflowy_mcp.shim"],
                env=new_env, keep_alive=False, log_file=lane / f"shim-{index}-stderr.log",
            )
            try:
                upgraded = await new_hosts.enter_async_context(Client(transport, mode="legacy", timeout=0, init_timeout=SETUP_WAIT))
                public = upgraded.initialize_result.meta[DAEMON_META_KEY]
                assert public["commit"] == "b" * 40 and public["identity_kind"] == "git"
                result = await upgraded.call_tool_mcp("n06_test_write", {"value": f"next-{index}"})
                assert not result.is_error and result.structured_content == {"value": f"next-{index}", "commit": "b" * 40}
                return public
            except Exception:
                print("N06_SHIM_FAILURE", index, (lane / f"shim-{index}-stderr.log").read_text(encoding="utf-8")[-8000:])
                raise
        old_transport = StdioTransport(command=sys.executable, args=["-m", "workflowy_mcp.shim"], env=old_env, keep_alive=False, log_file=lane / "old-shim-stderr.log")
        async with Client(old_transport, mode="legacy", timeout=0, init_timeout=SETUP_WAIT) as old_host:
            public = old_host.initialize_result.meta[DAEMON_META_KEY]
            assert public["commit"] == "a" * 40 and public["identity_kind"] == "git"
            # Normal host discovery precedes calls. Otherwise the SDK validates
            # the held response by listing through a NEW old-shim attachment,
            # which waits for this test's deliberately held exit barrier.
            await old_host.list_tools()
            held = asyncio.create_task(old_host.call_tool_mcp("n06_test_write", {"value": "held"}))
            await marker("effect-held")
            attach = asyncio.gather(*(use_new(index) for index in range(count)))
            try:
                await marker("draining")
                assert process.poll() is None and not (lane / "ready-b").exists()
                assert not (lane / "exit-request-a").exists(), "owned runner was told to exit before the admitted write published"
                async with Client(url, auth="n06-fake-broker-token", mode="legacy", timeout=0, init_timeout=SETUP_WAIT) as before:
                    refusal = await before.call_tool_mcp("n06_test_write", {"value": "refused"})
                    assert refusal.is_error
                    assert refusal.structured_content["error"]["code"] == "restarting"
                    assert refusal.structured_content["error"]["outcome"] == "not_sent"
                    assert refusal.structured_content["error"]["retry_after"] == 30
                touch(lane / ("crash" if crash else "release-write"))
                result = await asyncio.wait_for(held, SETUP_WAIT)
                if crash:
                    assert result.is_error and result.structured_content["error"]["code"] == "upstream_outcome_unknown"
                    assert result.structured_content["error"]["retryable"] is False
                    assert result.structured_content["error"]["recovery"] == "read_back_before_retry"
                else:
                    assert not result.is_error and result.structured_content == {"value": "held", "commit": "a" * 40}
                if hold_exit:
                    await marker("port-closed")
                    await marker("guard-busy")
                    assert process.poll() is None and not (lane / "ready-b").exists()
                    touch(lane / "release-exit")
                after = await asyncio.wait_for(attach, SETUP_WAIT)
                assert all(item["instance_id"] != public["instance_id"] for item in after)
                assert len({item["instance_id"] for item in after}) == 1
            finally:
                touch(lane / "release-write")
                touch(lane / "release-exit")
                if not held.done():
                    await asyncio.wait_for(held, SETUP_WAIT)
                if not attach.done():
                    # Let pre-forward orchestration finish; never cancel it as
                    # a test shortcut to a claimed graceful upgrade.
                    await asyncio.wait_for(attach, SETUP_WAIT)
        assert await asyncio.to_thread(process.wait, SETUP_WAIT) == (73 if crash else 0)  # real process death
        def events(suffix):
            return [json.loads(line) for line in (lane / ("events-" + suffix)).read_text(encoding="utf-8").splitlines()]
        a, b = events("a"), events("b")
        assert [event["value"] for event in a if event["kind"] == "effect"] == ["held"]
        assert sorted(event["value"] for event in b if event["kind"] == "effect") == sorted(f"next-{index}" for index in range(count))
        assert sum(event["kind"] == "bootstrap" for event in a) == sum(event["kind"] == "bootstrap" for event in b) == 1
        assert sum(event["kind"] == "accepted" for event in a) == 1
        assert next(event["path"] for event in a if event["kind"] == "guard") == next(event["path"] for event in b if event["kind"] == "guard")
        assert next(event["interpreter"] for event in b if event["kind"] == "service") == sys.executable

    async def own_hosts():
        # The Windows SDK owns a kill-on-close Job Object for each stdio host.
        # Keep hosts alive until the fixture daemon exits by its quit barrier;
        # SDK host teardown is an external process-tree kill, not N06's upgrade.
        async with AsyncExitStack() as new_hosts:
            try:
                await run(new_hosts)
            finally:
                touch(lane / "release-write")
                touch(lane / "release-exit")
                touch(lane / "quit")
                if (lane / "ready-b").exists():
                    assert await asyncio.to_thread(wait_for_file, lane / "exited-b", SETUP_WAIT)

    try:
        asyncio.run(own_hosts())
    except BaseException:
        # Keep the per-incarnation evidence, not just the last traceback tail.
        for diagnostic in sorted(lane.glob("events-*")) + sorted(lane.glob("*stderr.log")) + sorted(lane.glob("negotiations-*.jsonl")):
            print("N06_PROCESS_DIAGNOSTIC", diagnostic.name, diagnostic.read_text(encoding="utf-8"))
        raise
    finally:
        failed = sys.exc_info()[0] is not None
        touch(lane / "release-write")
        touch(lane / "release-exit")
        touch(lane / "quit")
        if (lane / "ready-b").exists():
            assert wait_for_file(lane / "exited-b", timeout=SETUP_WAIT)
        cleanup(process)
        if failed:
            # Observe the actual ASGI/SDK failure, not just our deliberately
            # sanitized shim envelope. These pipes belong to this fake-account
            # process and are read only after harness cleanup/reaping.
            outcome = collect(process)
            print("N06_DAEMON_STDOUT", outcome.stdout)
            print("N06_DAEMON_STDERR", outcome.stderr)


def test_real_wsl_shim_to_windows_peer_warns_forwards_and_never_stops_or_spawns(monkeypatch):
    """Real HTTP/stdio forwarding; only OS-affinity inputs are modeled on WSL."""
    from fastmcp.client.transports import StdioTransport

    scenario = Scenario.create()
    scenario.install_environment(monkeypatch)
    lane = scenario.lane("upgrade")
    old = scenario.installation_overlay("old", commit="a" * 40, source_time=10)
    new = scenario.installation_overlay("new", commit="b" * 40, source_time=20)
    old_env, new_env = scenario.overlay_environment(old), scenario.overlay_environment(new)
    old_env["_N06_DAEMON_PLATFORM"] = "win32"
    new_env.update(_N06_SHIM_IS_WSL="1", _N06_SHIM_PLATFORM="linux")
    process = scenario.start_overlay(old_env)
    async def run():
        assert await asyncio.to_thread(wait_for_file, lane / "ready-a", SETUP_WAIT)
        scenario.assert_endpoint()
        log = lane / "wsl-shim-stderr.log"
        transport = StdioTransport(command=sys.executable, args=["-m", "workflowy_mcp.shim"], env=new_env, keep_alive=False, log_file=log)
        async with Client(transport, mode="legacy", timeout=0, init_timeout=SETUP_WAIT) as host:
            info = host.initialize_result.meta["workflowy-nexus/shim"]
            assert info["relation"] == "local_newer" and info["replacement_capable"] is False
            assert [warning["code"] for warning in info["warnings"]] == ["drain_setting_adjusted", "shim_newer_attach_only"]
            assert "Windows-side" in info["warnings"][-1]["message"]
            for value in ("forward-one", "forward-two"):
                result = await host.call_tool_mcp("n06_test_write", {"value": value})
                assert not result.is_error and result.structured_content == {"value": value, "commit": "a" * 40}
        assert process.poll() is None and not (lane / "events-b").exists()
        events = [json.loads(line) for line in (lane / "events-a").read_text(encoding="utf-8").splitlines()]
        assert not any(event["kind"] == "accepted" for event in events)
        assert [event["value"] for event in events if event["kind"] == "effect"] == ["forward-one", "forward-two"]
        assert log.read_text(encoding="utf-8").count("this shim is newer") == 1
    try:
        asyncio.run(run())
    finally:
        touch(lane / "quit")
        if process.poll() is None:
            process.wait(timeout=SETUP_WAIT)
        cleanup(process)


@pytest.mark.parametrize("wsl", [False, True])
def test_real_legacy_peer_disappearance_native_cold_recovers_but_wsl_does_not(monkeypatch, wsl):
    """ZOV1IB: an existing proxy survives legacy → absent without a stale veto."""
    from fastmcp.client.transports import StdioTransport

    scenario = Scenario.create()
    scenario.install_environment(monkeypatch)
    lane = scenario.lane("upgrade")
    old = scenario.installation_overlay("legacy", commit="a" * 40, source_time=10)
    new = scenario.installation_overlay("new", commit="b" * 40, source_time=20)
    old_env, new_env = scenario.overlay_environment(old), scenario.overlay_environment(new)
    old_env["_N06_HIDE_RESTART_METADATA"] = "1"
    if wsl:
        old_env["_N06_DAEMON_PLATFORM"] = "win32"
        new_env.update(_N06_SHIM_IS_WSL="1", _N06_SHIM_PLATFORM="linux")
    process = scenario.start_overlay(old_env)

    async def run():
        assert await asyncio.to_thread(wait_for_file, lane / "ready-a", SETUP_WAIT)
        scenario.assert_endpoint()
        transport = StdioTransport(command=sys.executable, args=["-m", "workflowy_mcp.shim"], env=new_env, keep_alive=False, log_file=lane / "legacy-transition-stderr.log")
        async with Client(transport, mode="legacy", timeout=0, init_timeout=SETUP_WAIT) as host:
            public = host.initialize_result.meta
            assert DAEMON_META_KEY not in public
            assert public["workflowy-nexus/shim"]["replacement_capable"] is False
            assert "legacy_daemon" in {entry["code"] for entry in public["workflowy-nexus/shim"]["warnings"]}
            await host.list_tools()
            before = await host.call_tool_mcp("n06_test_write", {"value": "legacy-forward"})
            assert before.structured_content == {"value": "legacy-forward", "commit": "a" * 40}
            assert not (lane / "draining").exists()  # No unsupported stop request.
            touch(lane / "retire-old")
            assert await asyncio.to_thread(process.wait, SETUP_WAIT) == 0
            result = await host.call_tool_mcp("n06_test_write", {"value": "cold-recovered"})
            if wsl:
                assert result.is_error and result.structured_content["error"]["code"] == "daemon_unavailable"
                assert result.structured_content["error"]["outcome"] == "not_sent"
                assert result.structured_content["error"]["retryable"] is True
                assert not (lane / "events-b").exists()
            else:
                assert not result.is_error and result.structured_content == {"value": "cold-recovered", "commit": "b" * 40}
                events = [json.loads(line) for line in (lane / "events-b").read_text(encoding="utf-8").splitlines()]
                assert sum(event["kind"] == "bootstrap" for event in events) == 1
                assert len({event["instance_id"] for event in events}) == 1
                assert [event["value"] for event in events if event["kind"] == "effect"] == ["cold-recovered"]
                previous = [json.loads(line) for line in (lane / "events-a").read_text(encoding="utf-8").splitlines()]
                assert sum(event["kind"] == "bootstrap" for event in previous) == 1
                assert next(event["path"] for event in events if event["kind"] == "guard") == next(event["path"] for event in previous if event["kind"] == "guard")
                # Finish fixture cleanup before the Windows host's SDK closes
                # its child Job Object; external host death is not our drain.
                touch(lane / "quit")
                assert await asyncio.to_thread(wait_for_file, lane / "exited-b", SETUP_WAIT)
        assert (lane / "legacy-transition-stderr.log").read_text(encoding="utf-8").count("does not expose the N06 restart handshake") == 1

    try:
        asyncio.run(run())
    except BaseException:
        for diagnostic in sorted(lane.glob("events-*")) + sorted(lane.glob("*stderr.log")) + sorted(lane.glob("negotiations-*.jsonl")):
            print("N06_LEGACY_TRANSITION_DIAGNOSTIC", diagnostic.name, diagnostic.read_text(encoding="utf-8"))
        raise
    finally:
        touch(lane / "retire-old")
        touch(lane / "quit")
        if (lane / "ready-b").exists():
            assert wait_for_file(lane / "exited-b", timeout=SETUP_WAIT)
        cleanup(process)
