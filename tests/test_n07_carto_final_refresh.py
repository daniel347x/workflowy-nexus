"""N07 lifetime memory and actual coordinator receipts; no live endpoint."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from n07_job_harness import claimed_task_environment, task_core_environment, scrubbed_runtime


def receipt(**changes):
    return {"status": "published", "publication_kind": "full_refresh", "new_fetch": True,
            "adopted": True, "claim_id": "refresh-authority", "account_scope_id": "a" * 32,
            "epoch_id": "fixture-epoch", "epoch_number": 1, "adopted_sequence": 0,
            "base_snapshot_path": "fixture/snapshot.json", **changes}


def terminal_notification(env, job_id, *, relevant=True, claim_id=None, job_file=None, account_scope_id="a" * 32):
    """Explicit test task-exit lever, never a scanner-created obligation."""
    from workflowy_mcp.carto_job_runtime import CartoJobSpec, CartoJobControl, _freeze_job_data
    spec = CartoJobSpec(job_id, job_file or env.jobs / f"{job_id}.json", None, "CARTO_REFRESH", "file", "fixture-root",
                        claim_id or "claim-" + job_id, account_scope_id)
    control = CartoJobControl(may_have_workflowy_effects=relevant)
    status = _freeze_job_data({"id": job_id, "type": "CARTO_REFRESH", "execution_model": "daemon_task",
                              "status": "completed", "path": str(spec.job_file), "claim_id": spec.claim_id,
                              "cache_refresh_required": relevant, "pid": 123, "detached": False})
    env.server._record_carto_task_terminal(spec, control, status)
    return spec, control, status


async def settle(predicate):
    for _ in range(2000):
        if predicate():
            return
        await asyncio.sleep(0)
    pytest.fail("fixture coordinator did not reach its deterministic gate")


@pytest.fixture
def final_environment(claimed_task_environment, monkeypatch):
    env = claimed_task_environment
    srv = env.server
    monkeypatch.setattr(srv, "_refresh_carto_job_tracker_once", env.live_final_tracker)
    monkeypatch.setattr(srv, "_carto_jobs_lock", asyncio.Lock())
    monkeypatch.setattr(srv, "_carto_final_live_attempts", set())
    monkeypatch.setattr(srv, "_carto_final_delivery_task", None)
    monkeypatch.setattr(srv, "_carto_final_retry_event", asyncio.Event())
    monkeypatch.setattr(srv, "_carto_jobs_quiescent_event", asyncio.Event())
    monkeypatch.setattr(srv, "_nodes_export_refresh_lock", asyncio.Lock())
    monkeypatch.setattr(srv, "_nodes_export_refresh_task", None)
    monkeypatch.setattr(srv, "_nodes_export_refresh_state", "idle")
    monkeypatch.setattr(srv, "_nodes_export_refresh_pending_reasons", [])
    monkeypatch.setattr(srv, "_nodes_export_refresh_pending_kinds", [])
    monkeypatch.setattr(srv, "_nodes_export_refresh_pending_new_fetch", False)
    monkeypatch.setattr(srv, "_nodes_export_refresh_pending_final_batches", {})
    monkeypatch.setattr(srv, "_nodes_export_refresh_rerun_requested", False)
    monkeypatch.setattr(srv, "_nodes_export_refresh_idle_event", asyncio.Event())
    monkeypatch.setattr(srv, "_cache_refresh_should_skip", lambda: False)
    monkeypatch.setattr(srv, "_get_carto_file_refresh_dir", lambda: str(env.path / "f12-artifacts"))
    monkeypatch.setattr(srv, "get_server_config", lambda: SimpleNamespace(
        admission=SimpleNamespace(start_grace_seconds=30, spawn_grace_seconds=60,
                                  cooldown_seconds=60, orphan_grace_seconds=120),
        retention=SimpleNamespace(carto_job_max_age_seconds=3600),
        governance=SimpleNamespace(mutex_wait_seconds=10)))
    async def no_event(*args, **kwargs):
        pass
    monkeypatch.setattr(srv, "_send_ws_json_best_effort", no_event)
    monkeypatch.setattr(srv, "_emit_nodes_export_cache_status", no_event)
    return env


@pytest.mark.asyncio
async def test_terminal_task_creates_one_lifetime_obligation(final_environment, monkeypatch):
    from workflowy_mcp import carto_job_governance as gov
    from n05_region_harness import node_uuid
    env, calls = final_environment, []
    srv = env.server
    entered, release = asyncio.Event(), asyncio.Event()
    async def work(**kwargs):
        entered.set()
        await release.wait()
        return {"success": True, "nodes_updated": 1}
    monkeypatch.setattr(env.client, "refresh_file_node_beacons", work, raising=False)
    ack = await srv._start_carto_refresh_job(node_uuid("file"), "file")
    assert ack["success"], ack
    assert ack["job_id"] in srv._carto_active_cache_jobs and not srv._carto_jobs_quiescent_event.is_set()
    record = srv._carto_final_obligations[ack["job_id"]]
    task = srv._carto_inprocess_tasks[ack["job_id"]]
    await entered.wait()
    try:
        terminal_notification(env, ack["job_id"], claim_id=record.incarnation.removeprefix("claim:"), job_file=Path(record.path))
        assert record.state == "observing", "foreign control produced an exit proof for a live task"
    finally:
        release.set()
        await task
    assert record.state == "pending" and record.terminal_proof["kind"] == "task_terminal"
    original_read = gov._read_json
    marker_reads = []
    def read(path, *args, **kwargs):
        if ".refresh_claims" in str(path):
            marker_reads.append(str(path))
        assert ".refresh_claims" not in str(path), "tracker read a legacy dedupe marker"
        return original_read(path, *args, **kwargs)
    monkeypatch.setattr(gov, "_read_json", read)
    async def schedule(**kwargs):
        calls.append(kwargs)
        return {"success": True, "scheduled": True}
    monkeypatch.setattr(srv, "_request_nodes_export_cache_refresh_async", schedule)
    await srv._refresh_carto_job_tracker_once()
    assert len(calls) == 1 and calls[0]["source"] == "carto:inprocess-drain" and calls[0]["require_new_fetch"] is True
    await srv._finish_carto_final_batch(calls[0]["final_obligations"], calls[0]["attempt_token"], receipt=receipt())
    assert record.state == "published"
    assert srv._carto_jobs_quiescent_event.is_set()
    before = record.snapshot()
    spec, control, status = terminal_notification(env, ack["job_id"], claim_id=record.incarnation.removeprefix("claim:"), job_file=Path(record.path))
    assert str(spec.job_file) == record.path
    srv._record_carto_task_terminal(spec, control, status)
    srv._observe_carto_final_obligations({"observations": [{"job_id": ack["job_id"]}] * 2})
    Path(record.path).unlink()
    await srv._refresh_carto_job_tracker_once()
    assert record.snapshot() == before and len(calls) == 1
    assert marker_reads == []
    assert not srv._carto_inprocess_tasks and not srv._carto_job_controls
    status = await srv._system_job_status_impl(job_id=ack["job_id"])
    assert status["success"] is False, "lifetime dedupe identity resurrected deleted history"
    def forbidden(*args, **kwargs):
        raise AssertionError("terminal-memory cancellation entered a process controller")
    monkeypatch.setattr(srv, "cancel_carto_job", forbidden)
    assert (await srv._system_job_cancel_impl(ack["job_id"]))["outcome"] == "already_terminal"


@pytest.mark.asyncio
@pytest.mark.parametrize("qualifier", ["not-new", "not-adopted", "wrong-kind", "missing-epoch", "foreign-account", "stale-token", "wrong-incarnation"])
async def test_new_fetch_cutoff_account_and_receipt_are_required(final_environment, monkeypatch, qualifier):
    env, calls = final_environment, []
    srv = env.server
    terminal_notification(env, "receipt-job")
    async def request(**kwargs):
        calls.append(kwargs)
        return {"success": True, "scheduled": True}
    monkeypatch.setattr(srv, "_request_nodes_export_cache_refresh_async", request)
    await srv._refresh_carto_job_tracker_once()
    batch, token = calls[0]["final_obligations"], calls[0]["attempt_token"]
    bad = receipt(**{"not-new": {"new_fetch": False}, "not-adopted": {"adopted": False},
        "wrong-kind": {"publication_kind": "delta"}, "missing-epoch": {"epoch_id": None},
        "foreign-account": {"account_scope_id": "b" * 32}}.get(qualifier, {}))
    wrong_batch = [{**batch[0], "incarnation": "different"}] if qualifier == "wrong-incarnation" else batch
    await srv._finish_carto_final_batch(wrong_batch, "stale" if qualifier == "stale-token" else token, receipt=bad)
    record = srv._carto_final_obligations["receipt-job"]
    if qualifier in {"stale-token", "wrong-incarnation"}:
        assert record.state == "reserved" and token in srv._carto_final_live_attempts
    else:
        assert record.state == "pending" and token not in srv._carto_final_live_attempts
        await srv._refresh_carto_job_tracker_once()
        newer = calls[-1]
        await srv._finish_carto_final_batch(batch, token, receipt=receipt())
        assert record.state == "reserved" and newer["attempt_token"] in srv._carto_final_live_attempts
        batch, token = newer["final_obligations"], newer["attempt_token"]
    await srv._finish_carto_final_batch(batch, token, receipt=receipt())
    assert record.state == "published" and srv._carto_final_disposition_valid(record.snapshot())


@pytest.mark.asyncio
async def test_failed_attempt_retries_same_responsibility_on_watcher_tick(final_environment, monkeypatch):
    env, calls = final_environment, []
    srv = env.server
    terminal_notification(env, "retry-job")
    record = srv._carto_final_obligations["retry-job"]
    original_identity = record.incarnation
    async def refresh(**kwargs):
        calls.append(kwargs)
        return ({"success": False, "error": "fixture first attempt"} if len(calls) == 1 else
                {"success": True, "final_publication_receipt": receipt()})
    monkeypatch.setattr(env.client, "refresh_nodes_export_cache", refresh, raising=False)
    monkeypatch.setattr(srv, "_ensure_carto_final_delivery", env.live_final_delivery)
    srv._ensure_carto_final_delivery()
    delivery = srv._carto_final_delivery_task
    try:
        await settle(lambda: len(calls) == 1 and record.state == "pending")
        old_token = record.attempt_token
        assert not delivery.done() and record.incarnation == original_identity
        assert srv._daemon_drain_snapshot()["carto_final_delivery"] and env.controller._busy()
        for _ in range(5):
            await asyncio.sleep(0)
        assert len(calls) == 1, "delivery retried without an existing watcher tick"
        async def tick():
            class TickFinished(Exception):
                pass
            async def one_sleep(seconds):
                assert seconds == 1.0
                raise TickFinished
            phase = env.controller.phase
            env.controller.phase = "draining"
            try:
                with monkeypatch.context() as patch:
                    patch.setattr(srv.asyncio, "sleep", one_sleep)
                    with pytest.raises(TickFinished):
                        await srv._carto_jobs_watcher_loop()
            finally:
                env.controller.phase = phase
        await tick()
        await settle(lambda: record.state == "published")
        assert len(calls) == 2 and record.attempt_token != old_token and record.incarnation == original_identity
        await tick()
        await delivery
        await asyncio.sleep(0)
        assert not srv._daemon_drain_snapshot()["carto_final_delivery"]
    finally:
        # Complete any fixture-gated responsibility without cancelling a job.
        if not delivery.done():
            record.state = "irrelevant"
            srv._carto_final_retry_event.set()
            await delivery


@pytest.mark.asyncio
async def test_failed_terminal_status_is_visible_after_control_unregisters(final_environment, monkeypatch):
    from n05_region_harness import node_uuid
    env = final_environment
    srv, original = env.server, env.server.merge_write_carto_job_status
    async def work(**kwargs):
        raise RuntimeError("fixture body failure")
    def fail_terminal(path, payload):
        if payload.get("status") in {"completed", "failed", "cancelled"}:
            return False
        return original(path, payload)
    monkeypatch.setattr(env.client, "refresh_file_node_beacons", work, raising=False)
    monkeypatch.setattr(srv, "merge_write_carto_job_status", fail_terminal)
    ack = await srv._start_carto_refresh_job(node_uuid("file"), "file")
    assert ack["success"], ack
    await srv._carto_inprocess_tasks[ack["job_id"]]
    assert not srv._carto_job_controls and not srv._carto_inprocess_tasks
    status = await srv._system_job_status_impl(job_id=ack["job_id"])
    assert status["status"] == "failed" and status["error_kind"] == "carto_job_exception"
    assert "fixture body failure" in status["error"]
    assert status["error_extra"]["may_have_workflowy_effects"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("skip", [False, True])
async def test_policy_skip_and_gc_are_memory_scoped(final_environment, monkeypatch, skip):
    from workflowy_mcp import carto_job_governance as gov
    env, srv, calls = final_environment, final_environment.server, []
    spec, _, _ = terminal_notification(env, "gc-job")
    spec.job_file.write_text(json.dumps({"id": "gc-job", "type": "CARTO_REFRESH", "status": "completed",
        "claim_id": spec.claim_id, "updated_at": "2020-01-01", "cache_refresh_required": True}))
    await srv._run_carto_job_gc_pass()
    assert spec.job_file.exists()
    pending = await srv._clear_carto_job_history(str(env.jobs))
    assert pending["deleted_jobs"] == 0 and pending["skipped_active_jobs"] == 1
    async def request(**kwargs):
        calls.append(kwargs)
        return {"success": True, "skipped": True, "skip_reason": "cache_refresh_skip_toggle_active"} if skip else {"success": True, "scheduled": True}
    monkeypatch.setattr(srv, "_request_nodes_export_cache_refresh_async", request)
    await srv._refresh_carto_job_tracker_once()
    record = srv._carto_final_obligations["gc-job"]
    if not skip:
        await srv._finish_carto_final_batch(calls[0]["final_obligations"], calls[0]["attempt_token"], receipt=receipt())
    assert record.state == ("skipped_policy" if skip else "published")
    cleared = await srv._clear_carto_job_history(str(env.jobs))
    assert cleared["deleted_jobs"] == 1 and not spec.job_file.exists()
    assert "deleted_claims" not in cleared
    await srv._refresh_carto_job_tracker_once()
    assert len(calls) == 1 and srv._carto_final_obligations["gc-job"] is record


def test_lost_done_callback_recovers_from_actual_refresh_task(final_environment, monkeypatch):
    from test_carto_final_cache_publication import test_lost_done_callback_before_worker_entry_recovers_on_explicit_tracker_poll
    # The converted semantic witness uses the actual coordinator task/callback,
    # no file ownership lever; retaining it here pins F06's exact required name.
    test_lost_done_callback_before_worker_entry_recovers_on_explicit_tracker_poll((final_environment.jobs, []), monkeypatch)


@pytest.mark.parametrize("fault", ["late-batch", "drop-new-fetch"])
def test_retained_cutoff_and_new_fetch_witnesses_reject_specific_worker_neutralization(final_environment, monkeypatch, fault):
    """Mutate a test-local code object, not an ungranted coordinator source row.

    Its globals remain the ONE real server coordinator. Both witnesses retain
    their original assertions; no alternate scanner/receipt oracle is built.
    """
    import ast
    import inspect
    from types import CodeType, FunctionType
    import test_carto_final_cache_publication as retained
    srv = final_environment.server
    actual = srv._nodes_export_cache_refresh_worker
    tree = ast.parse(inspect.getsource(actual))
    changed = []
    class BreakCutoff(ast.NodeTransformer):
        def visit_For(self, node):
            node = self.generic_visit(node)
            if fault == "late-batch" and ast.unparse(node.iter) == "batches.items()":
                changed.append("late-batch")
                return ast.parse("batches.update(_nodes_export_refresh_pending_final_batches)").body + [node]
            return node
        def visit_Assign(self, node):
            if (fault == "drop-new-fetch" and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "require_new_fetch"):
                changed.append("drop-new-fetch")
                node.value = ast.Constant(False)
            return node
    tree = ast.fix_missing_locations(BreakCutoff().visit(tree))
    code = compile(tree, "<n07-test-only-coordinator-neutralization>", "exec")
    function_code = next(value for value in code.co_consts if isinstance(value, CodeType) and value.co_name == actual.__name__)
    broken = FunctionType(function_code, actual.__globals__, actual.__name__, actual.__defaults__, actual.__closure__)
    monkeypatch.setattr(srv, "_nodes_export_cache_refresh_worker", broken)
    assert changed == [fault]
    witness = (retained.test_coordinator_cutoff_cannot_receipt_later_final_batch if fault == "late-batch"
               else retained.test_final_with_widget_before_cutoff_ors_new_fetch)
    with pytest.raises(AssertionError) as caught:
        witness((final_environment.jobs, []), monkeypatch)
    assert ("1 == 2" if fault == "late-batch" else "require_new_fetch") in str(caught.value), (
        "neutralizer missed the specific cutoff/new-fetch assertion", str(caught.value))


@pytest.mark.asyncio
async def test_scan_and_gc_receive_immutable_loop_projections(final_environment, monkeypatch):
    import threading
    from n05_region_harness import node_uuid
    from workflowy_mcp.coherence.operation_cache import current_operation_cache
    from workflowy_mcp.mutation_locks import MUTATION_LOCK_STATE
    env, srv, loop_thread = final_environment, final_environment.server, threading.get_ident()
    entered, finish = asyncio.Event(), asyncio.Event()
    async def work(**kwargs):
        entered.set()
        await finish.wait()
        return {"success": True}
    monkeypatch.setattr(env.client, "refresh_file_node_beacons", work, raising=False)
    ack = await srv._start_carto_refresh_job(node_uuid("file"), "file")
    task = srv._carto_inprocess_tasks[ack["job_id"]]
    await entered.wait()
    record = srv._carto_final_obligations[ack["job_id"]]
    path = Path(record.path)
    path.write_text(json.dumps({"id": "forged-id", "type": "CARTO_REFRESH", "status": "completed",
                               "updated_at": "2020-01-01", "claim_id": record.incarnation.removeprefix("claim:")}))
    read, release = threading.Event(), threading.Event()
    original = srv._read_carto_history_candidates
    def inventory(base):
        result = original(base)
        read.set()
        assert release.wait(10), "fixture did not release the history inventory"
        return result
    monkeypatch.setattr(srv, "_read_carto_history_candidates", inventory)
    violations = []
    class LoopOnly(dict):
        def guard(self):
            if threading.get_ident() != loop_thread:
                violations.append("worker table access")
            assert threading.get_ident() == loop_thread, "data worker read a live task/obligation table"
        def __getitem__(self, key):
            self.guard()
            return super().__getitem__(key)
        def __contains__(self, key):
            self.guard()
            return super().__contains__(key)
        def __len__(self):
            self.guard()
            return super().__len__()
        def __iter__(self):
            self.guard()
            return super().__iter__()
        def values(self):
            self.guard()
            return super().values()
        def items(self):
            self.guard()
            return super().items()
        def get(self, *args):
            self.guard()
            return super().get(*args)
    monkeypatch.setattr(srv, "_carto_final_obligations", LoopOnly(srv._carto_final_obligations))
    monkeypatch.setattr(srv, "_carto_inprocess_tasks", LoopOnly(srv._carto_inprocess_tasks))
    original_gc = srv._gc_carto_jobs
    received = []
    def gc(*args, **kwargs):
        assert threading.get_ident() != loop_thread
        assert current_operation_cache(required=False) is None and MUTATION_LOCK_STATE.get() is None
        assert isinstance(kwargs["protected_ids"], frozenset)
        assert kwargs["protected_paths"] == frozenset({str(path)})
        received.append(kwargs["protected_ids"])
        return original_gc(*args, **kwargs)
    monkeypatch.setattr(srv, "_gc_carto_jobs", gc)
    gc_task = asyncio.create_task(srv._run_carto_job_gc_pass())
    try:
        assert await asyncio.to_thread(read.wait, 10)
        # It terminalizes while the data inventory is outstanding. Protection
        # is captured AFTER that joined read, so this transition cannot vanish.
        finish.set()
        await task
        assert record.state == "pending"
    finally:
        finish.set()
        release.set()
        await gc_task
        await task
    assert received == [frozenset({ack["job_id"]})] and path.exists()
    assert violations == [], "GC swallowed a forbidden live-table access"
    assert (await srv._system_job_status_impl(job_id=ack["job_id"]))["status"] == "completed"


@pytest.mark.asyncio
async def test_daemon_restart_drops_memory_debt_but_not_workflowy_writes(final_environment, monkeypatch):
    from pydantic import SecretStr
    from n07_job_harness import assert_endpoint, SUITE_ENDPOINT
    from workflowy_mcp.client.api_client_core import WorkFlowyClientCore
    from workflowy_mcp.models import APIConfiguration, WorkFlowyNode
    from coherence_harness import s02_node
    env, srv = final_environment, final_environment.server
    snapshots = env.path / "snapshots"
    snapshots.mkdir()
    def runtime_dir(*parts):
        return snapshots if parts == ("nodes_export_cache",) else env.path.joinpath(*parts)
    monkeypatch.setattr("workflowy_mcp.client.api_client_core.get_runtime_subdir", runtime_dir)
    monkeypatch.setattr("workflowy_mcp.carto_job_governance.get_cartographer_jobs_dir", lambda: env.jobs)
    monkeypatch.setattr(srv, "get_runtime_subdir", lambda *parts: env.path)
    assert_endpoint(SUITE_ENDPOINT)
    client = WorkFlowyClientCore(APIConfiguration(api_key=SecretStr("n07-fixture-only"), base_url=SUITE_ENDPOINT))
    monkeypatch.setattr(srv, "get_client", lambda: client)
    monkeypatch.setattr(client, "_refresh_settle_seconds", lambda _: 0)
    monkeypatch.setattr(client, "_gc_nodes_export_snapshots", lambda: None)
    upstream, fetches = {"nodes": [s02_node("root")]}, []
    async def fetch(client, max_retries):
        assert_endpoint(str(client.config.base_url))
        fetches.append(client)
        return upstream
    monkeypatch.setattr("workflowy_mcp.client.api_client_etch.fetch_nodes_export_uncached", fetch)
    result = await client.refresh_nodes_export_cache(refresh_kind="system", require_new_fetch=True)
    assert result["success"], result
    upstream = {"nodes": [s02_node("root"), s02_node("acknowledged", parent_id="root", name="retained write")]}
    # An acknowledged fake-upstream write also changed the OLD in-memory view,
    # but not its prior warm-start snapshot. Crash loses neither the upstream
    # write nor the existing files; it loses only process-local responsibilities.
    client._apply_create_to_nodes_export_cache(WorkFlowyNode(id="acknowledged", name="retained write", parent_id="root"), parent_id="root")
    assert "acknowledged" in client._cache_view_publisher.capture().nodes_by_id
    old_client, old_publisher = client, client._cache_view_publisher
    account = client._safe_account_scope_id()
    spec, _, status = terminal_notification(env, "old-terminal", account_scope_id=account)
    spec.job_file.write_text(json.dumps(dict(status)))
    abandoned_claim_id = "e" * 32
    (env.jobs / "abandoned.json").write_text(json.dumps({"id": "abandoned", "type": "CARTO_REFRESH",
        "execution_model": "daemon_task", "status": "running", "cache_refresh_required": True,
        "claim_id": abandoned_claim_id, "account_scope_id": account}))
    await old_client.close()  # simulated daemon end, never a job closing its shared client
    assert_endpoint(SUITE_ENDPOINT)
    client = WorkFlowyClientCore(APIConfiguration(api_key=SecretStr("n07-fixture-only"), base_url=SUITE_ENDPOINT))
    monkeypatch.setattr(client, "_refresh_settle_seconds", lambda _: 0)
    monkeypatch.setattr(client, "_gc_nodes_export_snapshots", lambda: None)
    assert client is not old_client and client._cache_view_publisher is not old_publisher
    assert client._safe_account_scope_id() == account
    monkeypatch.setattr(srv, "get_client", lambda: client)
    # A fresh incarnation has a fresh client/cache authority AND empty lifetime
    # tables, with the actual old job JSON bytes retained on disk.
    monkeypatch.setattr(srv, "_carto_final_obligations", {})
    monkeypatch.setattr(srv, "_carto_inprocess_tasks", {})
    monkeypatch.setattr(srv, "_carto_job_controls", {})
    monkeypatch.setattr(srv, "_carto_operation_jobs", {})
    monkeypatch.setattr(srv, "_carto_startup_failed_jobs", MappingProxyType({}))
    def no_resume(*args, **kwargs):
        raise AssertionError("fresh incarnation resumed an old job")
    monkeypatch.setattr(srv, "_schedule_carto_job", no_resume)
    await srv._mark_abandoned_carto_tasks_at_startup()
    assert json.loads((env.jobs / "abandoned.json").read_text())["error_kind"] == "daemon_restarted"
    await srv._refresh_carto_job_tracker_once()
    assert not srv._carto_final_obligations and len(fetches) == 1
    assert "acknowledged" not in client._cache_view_publisher.capture().nodes_by_id
    assert json.loads(spec.job_file.read_text())["status"] == "completed"
    result = await client.refresh_nodes_export_cache(refresh_kind="system", require_new_fetch=True)
    assert result["success"], result
    assert client._cache_view_publisher.capture().nodes_by_id["acknowledged"]["name"] == "retained write"
    assert len(fetches) == 2 and not srv._carto_final_obligations
    assert fetches == [old_client, client] and not srv._carto_inprocess_tasks
    await client.close()
