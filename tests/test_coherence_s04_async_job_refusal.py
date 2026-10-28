"""S04 Remediation Chain 3 (work-order item 2) — generic async-job rechecks
retain the universal typed refusal contract.

``_start_background_job.runner`` correctly rechecks the hard barrier inside
the independently dispatched operation and performs zero mutations — but
pre-fix its generic exception branch stored only an error string, so
``system.job.status`` could not machine-read ``coherence_barrier_active`` or
the standard recovery payload.  The runner now catches
CoherenceBarrierActiveError distinctly and persists
``error_code="coherence_barrier_active"`` plus the standard
``refusal_extra()`` fields, retaining the human error text, with zero
mutations and the terminal status vocabulary unchanged.  Clean async-job
success shapes are byte/key compatible, and legacy optional-async fronts
converge on this runner (same behavior).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from coherence_harness import (
    ACCOUNT_A,
    make_boundary_capable,
    make_coherence_root,
    run_subprocess,
    s04_raise_barrier,
)

import workflowy_mcp.coherence.operation_cache as _opc
from workflowy_mcp import server

_SECOND_ACTOR_BARRIER_DRIVER = r"""
import sys

sys.path.insert(0, sys.argv[2] + "/tests")
from pathlib import Path

from coherence_harness import ACCOUNT_A, make_coherence_root, s04_raise_barrier


def main() -> None:
    root = Path(sys.argv[1])
    paths = make_coherence_root(root, ACCOUNT_A, bootstrap=False)
    s04_raise_barrier(paths, transaction_id="tx-second-actor")


main()
"""


def _double(nodes=(), status=None):
    client = object.__new__(server.WorkFlowyClient)
    return make_boundary_capable(client, nodes=nodes, boundary_status=status)


def _run_interrupted_job(monkeypatch, tmp_path, *, second_actor_subprocess: bool):
    """Enqueue one etch-style job while CLEAN, hold the dispatched runner,
    raise the barrier (from a REAL SUBPROCESS second actor when requested),
    then release the runner.  Returns (ack, factory_calls)."""
    root = tmp_path / "jobs"
    paths = make_coherence_root(root, ACCOUNT_A)
    monkeypatch.setattr(server, "_admission_coherence_paths", lambda: paths)
    monkeypatch.setattr(server, "get_client", lambda: _double(status=None))
    server._jobs.clear()
    factory_calls: list[str] = []

    async def _factory(_job_id):
        factory_calls.append("ran")
        return {"success": True}

    async def _scenario():
        release = asyncio.Event()
        real_spawn = _opc.create_isolated_context_task

        def _holding_spawn(coro, *, name=None):
            async def _held():
                await release.wait()
                return await coro

            return real_spawn(_held(), name=name)

        _opc.create_isolated_context_task = _holding_spawn
        try:
            ack = await server._start_background_job("etch", {}, _factory)
            if second_actor_subprocess:
                # A REAL second actor (another process) raises the barrier
                # against the shared coherence root before the runner's
                # internal recheck executes.
                completed = run_subprocess(
                    _SECOND_ACTOR_BARRIER_DRIVER,
                    str(root),
                    str(Path.cwd()),
                    timeout=60.0,
                )
                assert completed.returncode == 0
            else:
                s04_raise_barrier(paths, transaction_id="tx-second-actor")
            release.set()
            await server._jobs[ack["job_id"]]["_task"]
        finally:
            _opc.create_isolated_context_task = real_spawn
        return ack

    ack = asyncio.run(_scenario())
    return ack, factory_calls


def test_enqueue_clean_second_actor_barrier_zero_factory_calls(monkeypatch, tmp_path):
    """Required test (a): enqueue while clean, barrier raised by a SECOND
    ACTOR (real subprocess) before runner execution — zero factory calls,
    and the universal typed refusal contract on the job record."""
    ack, factory_calls = _run_interrupted_job(
        monkeypatch, tmp_path, second_actor_subprocess=True
    )
    assert factory_calls == []  # zero mutations
    job = server._jobs[ack["job_id"]]
    assert job["status"] == "failed"
    # The human error text is retained.
    assert "CoherenceBarrierActiveError" in job["error"]
    # The universal machine-readable contract.
    assert job["error_code"] == "coherence_barrier_active"
    extra = job["error_extra"]
    assert extra["refresh_required"] is True
    assert extra["hard_dirty"] is True
    assert extra["dirty_generation"] == 1
    assert extra["reason_code"] == "journal_publication_failed"
    assert extra["transaction_id"] == "tx-second-actor"
    assert extra["recovery"] == {
        "family": "system",
        "verb": "cache.refresh",
        "params": {},
    }


def test_job_status_returns_universal_code_and_recovery_payload(monkeypatch, tmp_path):
    """Required test (b): system.job.status(job_id=...) returns the
    universal code and the recovery payload."""
    ack, factory_calls = _run_interrupted_job(
        monkeypatch, tmp_path, second_actor_subprocess=False
    )
    assert factory_calls == []
    status = asyncio.run(server._system_job_status_impl(job_id=ack["job_id"]))
    assert status["success"] is True
    assert status["status"] == "failed"
    assert status["error_code"] == "coherence_barrier_active"
    assert "CoherenceBarrierActiveError" in status["error"]
    assert status["error_extra"]["recovery"] == {
        "family": "system",
        "verb": "cache.refresh",
        "params": {},
    }
    assert status["error_extra"]["hard_dirty"] is True
    assert status["error_extra"]["refresh_required"] is True
    assert status["error_extra"]["dirty_generation"] == 1


def test_clean_job_result_shape_unchanged(monkeypatch, tmp_path):
    """Required test (c): a clean async-job success keeps the pre-fix
    result/job shape byte-and-key compatible (no typed fields leak in)."""
    paths = make_coherence_root(tmp_path / "jobs", ACCOUNT_A)
    monkeypatch.setattr(server, "_admission_coherence_paths", lambda: paths)
    monkeypatch.setattr(server, "get_client", lambda: _double(status=None))
    server._jobs.clear()

    async def _factory(_job_id):
        return {"success": True, "echo": 1}

    async def _scenario():
        ack = await server._start_background_job("etch", {}, _factory)
        await server._jobs[ack["job_id"]]["_task"]
        return ack

    ack = asyncio.run(_scenario())
    job = server._jobs[ack["job_id"]]
    assert job["status"] == "completed"
    assert job["result"] == {"success": True, "echo": 1}
    assert job["error"] is None
    # The exact pre-fix key set — nothing added on the success path.
    assert set(job) == {
        "job_id",
        "kind",
        "status",
        "payload",
        "result",
        "error",
        "started_at",
        "finished_at",
        "_task",
    }
    status = asyncio.run(server._system_job_status_impl(job_id=ack["job_id"]))
    assert status["status"] == "completed"
    assert "error_code" not in status
    assert "error_extra" not in status
