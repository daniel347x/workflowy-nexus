from __future__ import annotations

import asyncio
import inspect
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from workflowy_mcp import carto_job_governance as gov
from workflowy_mcp import config as carto_config
from workflowy_mcp import server
from workflowy_mcp.client.api_client_core import WorkFlowyClientCore, operation_cancel_scope
from workflowy_mcp.models.errors import OperationCancelledError

ACCOUNT = "d" * 32


def _cache() -> dict[str, dict]:
    return {
        "vault": {"id": "vault"},
        "repo": {"id": "repo", "parent_id": "vault"},
        "file": {"id": "file", "parent_id": "repo"},
    }


def _footprint(tmp_path: Path) -> dict:
    target = tmp_path / "repo" / "file.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.touch(exist_ok=True)
    return gov.compute_footprint(target, "file", _cache(), kind="file")


def _admit(
    tmp_path: Path,
    jobs: Path,
    *,
    job_id: str = "job",
    dedupe: str = "a" * 64,
    footprint: dict | None = None,
    job_type: str = "CARTO_REFRESH",
) -> dict:
    jobs.mkdir(parents=True, exist_ok=True)
    return asyncio.run(
        gov.admit_carto_job(
            job_file=jobs / f"{job_id}.json",
            job_payload={"id": job_id, "type": job_type},
            job_type=job_type,
            dedupe_key=dedupe,
            account_scope_id=ACCOUNT,
            footprint=footprint or _footprint(tmp_path),
            launcher="system",
            parent_job_id=None,
            prepared=False,
            jobs_root=jobs,
        )
    )


def _holds(jobs: Path) -> tuple[dict, ...]:
    from workflowy_mcp.mutation_locks import _governance_holds

    return tuple(_governance_holds(jobs))


def _cooldowns(jobs: Path) -> tuple[dict, ...]:
    from workflowy_mcp.mutation_locks import _governance_cooldowns

    return tuple(_governance_cooldowns(jobs, now_epoch=0.0, cooldown_seconds=10.0))


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_reap_status_write_failure_does_not_block_one_exit_door(tmp_path, monkeypatch):
    """C (EXIT/PRIMARY): a real task's terminal status write fails; the one
    finalizer still runs and releases its exact memory hold.  No reap and no
    cleanup-obligation supervisor survive."""
    from workflowy_mcp.carto_job_runtime import CartoJobControl, CartoJobSpec

    jobs = tmp_path / "jobs"
    admitted = _admit(tmp_path, jobs, job_id="job", dedupe="a" * 64)
    claim_id = admitted["claim"]["claim_id"]
    job_file = jobs / "job.json"
    control = CartoJobControl()
    control.may_have_workflowy_effects = True
    spec = CartoJobSpec(
        job_id="job",
        job_file=job_file,
        log_file=None,
        job_type="CARTO_REFRESH",
        mode="file",
        root_uuid="file",
        claim_id=claim_id,
        account_scope_id=ACCOUNT,
    )
    monkeypatch.setattr(server, "merge_write_carto_job_status", lambda *args: False)
    monkeypatch.setattr(server, "_ensure_carto_final_delivery", lambda: None)
    monkeypatch.setattr(server, "_carto_final_obligations", {})
    monkeypatch.setattr(server, "_carto_job_controls", {})
    monkeypatch.setattr(server, "_carto_job_states", {})
    monkeypatch.setattr(server, "_carto_active_cache_jobs", set())
    monkeypatch.setattr(server, "_carto_pending_terminal_cache_jobs", set())
    if os.environ.get("CUT2B_NEUTRALIZE_FINALIZE_RELEASE_ORDER"):
        async def no_finalize(*_args, **_kwargs):
            return True

        monkeypatch.setattr(server, "finalize_claim_async", no_finalize)
    if os.environ.get("CUT2B_NEUTRALIZE_PRIMARY_ERROR_MASK"):
        outcome = {"success": True}
    else:
        outcome = {"success": False, "error": "primary fixture error"}

    asyncio.run(server._finalize_carto_task(spec, control, outcome))

    terminal = control.terminal_result
    assert terminal["status"] == "failed"
    assert "status_write_error" in terminal
    assert "primary fixture error" in terminal["error"]
    assert tuple(_holds(jobs)) == ()
    assert _cooldowns(jobs)


def test_unknown_status_is_active_and_completed_with_warnings_is_terminal():
    assert carto_config.is_carto_job_active("")
    assert carto_config.is_carto_job_active("future_state")
    assert carto_config.is_carto_job_terminal("completed_with_warnings")


@pytest.mark.parametrize(
    "disk,incoming,expected",
    [
        ("cancelling", "running", "cancelling"),
        ("cancelling", "completed", "completed"),
        ("completed", "running", "completed"),
        ("completed", "completed_with_warnings", "completed_with_warnings"),
        ("completed_with_warnings", "completed", "completed_with_warnings"),
        ("completed_with_warnings", "failed", "failed"),
        ("failed", "cancelled", "failed"),
        ("cancelled", "failed", "cancelled"),
    ],
)
def test_status_merge_exact_precedence(tmp_path, disk, incoming, expected):
    path = tmp_path / "job.json"
    path.write_text(json.dumps({"id": "job", "status": disk}), encoding="utf-8")
    assert carto_config.merge_write_carto_job_status(path, {"id": "job", "status": incoming})
    assert _load(path)["status"] == expected


def test_cancel_ordering_already_terminal_and_parent_child_independence(tmp_path, monkeypatch):
    """C (CANCEL): the job control request precedes the joined cancelling-status
    write; native task completion is truth and a terminal record is a no-write
    no-op.  Sibling controls are untouched."""
    from workflowy_mcp.carto_job_runtime import CartoJobControl

    jobs = tmp_path / "jobs"
    jobs.mkdir()
    parent = CartoJobControl()
    child = CartoJobControl()
    parent_file = jobs / "parent.json"
    child_file = jobs / "child.json"
    payload = {"id": "parent", "type": "CARTO_REFRESH", "status": "running"}
    gov.atomic_write_json(parent_file, payload)
    gov.atomic_write_json(child_file, {**payload, "id": "child"})

    events: list[str] = []
    real_merge = carto_config.merge_write_carto_job_status

    def merge(path, data):
        if data.get("status") == "cancelling":
            events.append("cancelling")
        return real_merge(path, data)

    monkeypatch.setattr(carto_config, "merge_write_carto_job_status", merge)
    monkeypatch.setattr(gov, "merge_write_carto_job_status", merge)
    if os.environ.get("CUT2B_NEUTRALIZE_PENDING_MARKER"):
        # Probe: the cancelling reply drops the additive pending marker.
        real_cancel = gov.cancel_carto_job

        async def without_marker(*args, **kwargs):
            result = await real_cancel(*args, **kwargs)
            result.pop("cancellation_pending", None)
            return result

        monkeypatch.setattr(gov, "cancel_carto_job", without_marker)

    def task_cancel():
        events.append("task_cancel")
        parent.request_user_cancel()

    if os.environ.get("CUT2B_NEUTRALIZE_CANCEL_ORDER"):
        # Probe: deliver the control request after the joined status write.
        def task_cancel():
            events.append("late_task_cancel")

    def task_done() -> bool:
        return parent.user_observed

    result = asyncio.run(
        gov.cancel_carto_job(
            parent_file,
            requested_by="test",
            task_job=payload,
            task_done=task_done,
            task_cancel=task_cancel,
            cooperative_timeout_seconds=0,
        )
    )
    assert events[:2] == ["task_cancel", "cancelling"]
    assert parent.user_requested is True
    assert result["outcome"] == "kill_refused_in_process_job"
    assert result.get("cancellation_pending") is True
    assert _load(parent_file)["status"] == "cancelling"
    assert child.user_requested is False

    # Native completion is truth: a terminal record is not rewritten.
    payload["status"] = "failed"
    gov.atomic_write_json(parent_file, payload)
    before = parent_file.read_bytes()
    parent.user_observed = True
    result = asyncio.run(
        gov.cancel_carto_job(
            parent_file, requested_by="test", task_job=payload, task_done=task_done
        )
    )
    assert result["outcome"] == "already_terminal"
    assert parent_file.read_bytes() == before


from test_carto_final_cache_publication import final_env, write_job, record


def test_completed_with_warnings_transition_triggers_terminal_cache_refresh(final_env):
    jobs, requested = final_env
    path, payload = write_job(jobs, "warned")
    async def exercise():
        await server._refresh_carto_job_tracker_once()
        assert not requested
        assert gov.merge_write_carto_job_status(path, {
            **payload, "status": "completed_with_warnings", "warnings": [{"message": "phase warning"}]})
        from test_carto_final_cache_publication import notify_job_exit
        notify_job_exit(path, {**payload, "status": "completed_with_warnings", "warnings": [{"message": "phase warning"}]})
        await server._refresh_carto_job_tracker_once()
        assert requested and "warned" in requested[0]["reason"]
        assert requested[0]["require_new_fetch"] is True
        assert record(jobs, "warned")["state"] == "reserved"
        assert server._scan_carto_job_files(str(jobs))[0]["warnings"] == [{"message": "phase warning"}]
    asyncio.run(exercise())


def test_cooperative_release_and_verified_death(tmp_path):
    """M (EXIT): the cooperative-task half survives; the dead-owner kill path
    retires with the foreign-worker/escalation carrier."""
    from workflowy_mcp.carto_job_runtime import CartoJobControl

    jobs = tmp_path / "release"
    jobs.mkdir()
    job_file = jobs / "job.json"
    payload = {"id": "job", "type": "CARTO_REFRESH", "status": "running"}
    gov.atomic_write_json(job_file, payload)
    control = CartoJobControl()

    def task_cancel() -> None:
        control.request_user_cancel()

    if os.environ.get("CUT2B_NEUTRALIZE_FINALIZE_RELEASE_ORDER"):
        def task_cancel() -> None:
            return None

    async def scenario() -> None:
        task = asyncio.create_task(asyncio.sleep(0.05))

        def task_done() -> bool:
            return task.done()

        result = await gov.cancel_carto_job(
            job_file,
            requested_by="test",
            task_job=payload,
            task_done=task_done,
            task_cancel=task_cancel,
            cooperative_timeout_seconds=1.0,
            poll_interval_seconds=0.01,
        )
        await task
        assert result["outcome"] == "cooperative"
        assert control.user_requested is True

    asyncio.run(scenario())


def test_live_claim_pins_job_json_and_live_tombstone_survives_history_clear(tmp_path, monkeypatch):
    """C (GC/COOL): an admitted reservation and a real task id/path pin the job
    JSON; memory cooldown survives the existing history clear."""
    admission = SimpleNamespace(cooldown_seconds=60)
    monkeypatch.setattr(
        server,
        "get_server_config",
        lambda: SimpleNamespace(
            admission=admission,
            governance=SimpleNamespace(mutex_wait_seconds=10),
        ),
    )
    jobs = tmp_path / "jobs"
    admitted = _admit(tmp_path, jobs, job_id="job", dedupe="c" * 64)
    claim_id = admitted["claim"]["claim_id"]
    job_file = jobs / "job.json"

    protection = server._carto_gc_protection()
    protected_paths = server._carto_gc_paths(protection)
    assert "job" in protection
    assert os.path.normcase(os.path.abspath(str(job_file))) in protected_paths

    if os.environ.get("CUT2B_NEUTRALIZE_JOB_LEDGER_LOSS"):
        protection = frozenset()
        protected_paths = frozenset()

    # Terminal data, but the live reservation still pins it.
    payload = _load(job_file)
    payload["status"] = "completed"
    gov.atomic_write_json(job_file, payload)
    assert (
        server._delete_carto_job_json_if_unclaimed(
            jobs,
            job_file,
            _load(job_file),
            protected_ids=protection,
            protected_paths=protected_paths,
        )
        is False
    )
    assert job_file.exists()

    assert asyncio.run(gov.finalize_claim_async(claim_id, jobs)) is True
    cooldowns = _cooldowns(jobs)
    assert cooldowns

    asyncio.run(server._clear_carto_job_history(str(jobs)))
    if os.environ.get("CUT2B_NEUTRALIZE_COOLDOWN"):
        from workflowy_mcp import mutation_locks as locks

        locks._LOCKS.cooldowns.clear()
    assert _cooldowns(jobs) == cooldowns


def test_refresh_claim_and_q3_lease_lifecycles_remain_separate(tmp_path):
    """M: N07's final-refresh responsibility is not Q3/file ownership; a
    source-copy release of Q3 inside final delivery must be rejected."""
    source = inspect.getsource(server._gc_carto_jobs)
    server_source = inspect.getsource(server)
    for name in ("_carto_refresh_claims_dir", "_carto_final_record_path",
                 "_carto_final_cleanup_permitted_locked", "_delete_carto_final_marker_if_discharged"):
        assert name not in server_source
    assert ".refresh_claims" not in server_source
    assert "leases_dir" not in source
    adapters = (
        server._claim_carto_terminal_refresh_jobs,
        server._observe_carto_final_obligations,
        server._finish_carto_final_batch_locked,
    )
    final_delivery = "\n".join(inspect.getsource(adapter) for adapter in adapters)
    for adapter in adapters:
        authority = inspect.getsource(adapter)
        assert "_carto_final_obligations" in authority
        assert "_carto_final_record_path" not in authority and "os_lock(" not in authority
    if os.environ.get("CUT2B_NEUTRALIZE_REFRESH_Q3_COUPLING"):
        final_delivery += "\nrelease_q3_leases_async(leases, jobs_root=jobs)"
    assert "release_q3_leases_async" not in final_delivery


def _sweep_kill_offenders(sources: dict[str, str]) -> list[str]:
    markers = (
        "sweep_region_governance",
        "cleanup_carto_auxiliary_artifacts",
        "taskkill",
        ".terminate(",
        ".kill(",
    )
    return [
        f"{name}:{marker}"
        for name, source in sources.items()
        for marker in markers
        if marker in source
    ]


def test_shared_sweep_wiring_and_no_kill_in_watcher_or_startup():
    """C (KILL): the sweep and kill paths are physically absent from admission,
    watcher and startup; a source-copy reintroduction must be rejected."""
    sources = {
        "admission": inspect.getsource(gov.admit_carto_job_locked),
        "watcher": inspect.getsource(server._carto_jobs_watcher_loop),
        "startup": inspect.getsource(server.lifespan),
        "cancel": inspect.getsource(gov.cancel_carto_job),
    }
    assert _sweep_kill_offenders(sources) == []
    assert not hasattr(server, "_sweep_carto_governance")
    assert "async_os_lock(" not in sources["admission"]

    if os.environ.get("CUT2B_NEUTRALIZE_KILL_PATH"):
        sources["watcher"] += "\n    .kill()"
        sources["admission"] += "\n    sweep_region_governance_locked()"
    assert _sweep_kill_offenders(sources) == []


def test_both_public_cancel_fronts_share_the_controller():
    system_source = inspect.getsource(server._system_job_cancel_impl)
    # U4 (WS reader loop): the carto_cancel_job clause was lifted VERBATIM
    # into _handle_carto_cancel_job and is spawned, so the WS front's source
    # is the lifted handler; the reader loop must dispatch THAT front (and
    # never hand-roll the cancellation itself).
    websocket_source = inspect.getsource(server._handle_carto_cancel_job)
    pump_source = inspect.getsource(server.websocket_handler)
    dispatch_source = inspect.getsource(server._dispatch_ws_work)
    assert "cancel_carto_job(" in system_source
    assert "_system_job_cancel_impl(" in websocket_source
    assert 'carto_job["status"] = "cancelled"' not in websocket_source
    assert 'carto_job["status"] = "cancelled"' not in pump_source
    assert "_handle_carto_cancel_job(" in dispatch_source


def test_job_local_cancel_probe_does_not_cancel_sibling_task():
    async def scenario():
        client = WorkFlowyClientCore.__new__(WorkFlowyClientCore)
        client._operation_cancel_callback = None
        started = asyncio.Event()

        async def cancelled():
            with operation_cancel_scope(lambda: True):
                started.set()
                with pytest.raises(OperationCancelledError):
                    await client._sleep_with_operation_cancel(0.01, "cancelled")

        async def sibling():
            await started.wait()
            with operation_cancel_scope(lambda: False):
                await client._sleep_with_operation_cancel(0.01, "sibling")
                return "ok"

        assert await asyncio.gather(cancelled(), sibling()) == [None, "ok"]

    asyncio.run(scenario())
