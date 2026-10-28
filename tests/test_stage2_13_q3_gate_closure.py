"""Stage2-13 (Locked Work Order §6.13) — Q3/CARTO overlap closure, CUT-2B MEM.

The former acceptance-gate inventory proved Q3↔Q3 and CARTO↔Q3 refusals through
real OS subprocesses and on-disk lease/claim artifacts.  CUT-2B's one in-process
registry carries those refusals; these three separately collected tests drive
the real async admission/Q3 APIs with same-loop tasks.  The retained oracles are
the refusal text/kind, exact release, and success after release; the kept
``STAGE2_NEUTRALIZE_OVERLAP`` sensitivity neutralizes the overlap predicate.
``CUT2B_NEUTRALIZE_ADMISSION_GAP`` omits the pre-grant reservation; the
fourth, dead-owner-sweep witness retires with the sweep (§6.2.6).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from workflowy_mcp import carto_job_governance as gov

_ACCOUNT = "a" * 32
_ACCOUNT_B = "b" * 32


def _footprint(root: Path) -> dict:
    return {
        "kind": "file",
        "path": str(root / "target.py"),
        "root_uuid": "file-a",
        "ancestor_chain": ["file-a"],
    }


def _snapshot(tmp_path: Path) -> dict[str, str]:
    path = tmp_path / "snapshot.json"
    gov.atomic_write_json(
        path,
        {
            "snapshot_schema_version": 1,
            "snapshot_timestamp": gov.utc_now_iso(),
            "account_scope_id": _ACCOUNT,
            "epoch_number": 0,
            "epoch_id": "e" * 32,
            "incorporated_through_seq": 0,
            "publication_kind": "full_refresh",
            "nodes_export": {"nodes": []},
        },
    )
    return {"snapshot_path": str(path), "freshness_disposition": "performed"}


def _neutralize_overlap(monkeypatch) -> None:
    if os.environ.get("STAGE2_NEUTRALIZE_OVERLAP") or os.environ.get(
        "CUT2B_NEUTRALIZE_Q3_SYMMETRY"
    ):
        monkeypatch.setattr(gov, "footprints_overlap", lambda _left, _right: False)


def test_q3_vs_q3_real_process_refusal_then_acquire_after_release(
    tmp_path, monkeypatch
):
    """PAIR-026: a live Q3 hold blocks a second Q3 acquisition; the same
    acquisition succeeds after the holder's exact release."""
    root = tmp_path / "share"
    jobs = root / "cartographer_jobs"
    jobs.mkdir(parents=True)
    (root / "target.py").write_text("# target\n", encoding="utf-8")
    footprint = _footprint(root)

    async def scenario() -> None:
        holding = asyncio.Event()
        release = asyncio.Event()
        holder_leases: list[dict] = []

        async def holder() -> None:
            holder_leases.extend(
                await gov.acquire_q3_leases_async(
                    [footprint], account_scope_id=_ACCOUNT, jobs_root=jobs
                )
            )
            holding.set()
            await release.wait()
            assert await gov.release_q3_leases_async(holder_leases, jobs_root=jobs) == []

        holder_task = asyncio.create_task(holder())
        await holding.wait()
        _neutralize_overlap(monkeypatch)
        with pytest.raises(gov.GovernanceConflictError) as caught:
            await gov.acquire_q3_leases_async(
                [footprint], account_scope_id=_ACCOUNT, jobs_root=jobs
            )
        assert "overlaps active owner" in str(caught.value)

        release.set()
        await holder_task
        leases = await gov.acquire_q3_leases_async(
            [footprint], account_scope_id=_ACCOUNT, jobs_root=jobs
        )
        assert len(leases) == 1
        assert await gov.release_q3_leases_async(leases, jobs_root=jobs) == []

    asyncio.run(scenario())


def test_q3_holder_blocks_carto_admission_real_process(tmp_path, monkeypatch):
    """PAIR-027 (Q3 → CARTO): a live Q3 hold blocks a real CARTO admission
    over the overlapping footprint (overlap_conflict)."""
    root = tmp_path / "share"
    jobs = root / "cartographer_jobs"
    jobs.mkdir(parents=True)
    (root / "target.py").write_text("# target\n", encoding="utf-8")
    footprint = _footprint(root)

    async def scenario() -> None:
        leases = await gov.acquire_q3_leases_async(
            [footprint], account_scope_id=_ACCOUNT, jobs_root=jobs
        )
        try:
            _neutralize_overlap(monkeypatch)
            with pytest.raises(gov.AdmissionRefusal) as caught:
                await gov.admit_carto_job(
                    job_file=jobs / "contend-job.json",
                    job_payload={"id": "contend-job"},
                    job_type="CARTO_REFRESH",
                    dedupe_key="e" * 64,
                    account_scope_id=_ACCOUNT,
                    footprint=footprint,
                    launcher="agent",
                    parent_job_id=None,
                    prepared=False,
                    jobs_root=jobs,
                )
            assert caught.value.kind == "overlap_conflict"
            assert "overlaps" in str(caught.value)
        finally:
            assert await gov.release_q3_leases_async(leases, jobs_root=jobs) == []

        admitted = await gov.admit_carto_job(
            job_file=jobs / "after-release.json",
            job_payload={"id": "after-release"},
            job_type="CARTO_REFRESH",
            dedupe_key="f" * 64,
            account_scope_id=_ACCOUNT,
            footprint=footprint,
            launcher="agent",
            parent_job_id=None,
            prepared=False,
            jobs_root=jobs,
        )
        assert admitted["admitted"] is True
        assert (
            await gov.finalize_claim_async(admitted["claim"]["claim_id"], jobs) is True
        )

    asyncio.run(scenario())


def test_carto_claim_holder_blocks_q3_acquisition_real_process(
    tmp_path, monkeypatch
):
    """PAIR-027 (CARTO → Q3): both the admitted reservation and the running
    task grant block Q3; after the task's exact release, Q3 succeeds."""
    root = tmp_path / "share"
    jobs = root / "cartographer_jobs"
    jobs.mkdir(parents=True)
    (root / "target.py").write_text("# target\n", encoding="utf-8")
    reservation_footprint = _footprint(root)
    running_footprint = {
        "kind": "file",
        "path": str(root / "target2.py"),
        "root_uuid": "file-b",
        "ancestor_chain": ["file-b"],
    }
    (root / "target2.py").write_text("# target two\n", encoding="utf-8")

    async def scenario() -> None:
        admitted = await gov.admit_carto_job(
            job_file=jobs / "reservation.json",
            job_payload={"id": "reservation"},
            job_type="CARTO_REFRESH",
            dedupe_key="d" * 64,
            account_scope_id=_ACCOUNT,
            footprint=reservation_footprint,
            launcher="agent",
            parent_job_id=None,
            prepared=False,
            jobs_root=jobs,
        )
        claim_id = admitted["claim"]["claim_id"]
        _neutralize_overlap(monkeypatch)
        if os.environ.get("CUT2B_NEUTRALIZE_ADMISSION_GAP"):
            assert await gov.finalize_claim_async(claim_id, jobs) is True
        with pytest.raises(gov.GovernanceConflictError) as caught:
            await gov.acquire_q3_leases_async(
                [reservation_footprint], account_scope_id=_ACCOUNT, jobs_root=jobs
            )
        assert "overlaps active owner" in str(caught.value)
        assert await gov.finalize_claim_async(claim_id, jobs) is True

        # Running-grant phase: the real N07 task holds its typed grant.
        from n07_job_harness import install_legacy_task_fixture

        fixture = install_legacy_task_fixture(monkeypatch, jobs=jobs, account=_ACCOUNT)
        started = asyncio.Event()
        release = asyncio.Event()

        async def work(**_kwargs):
            from workflowy_mcp.mutation_locks import held_mutation_lock_regions

            assert held_mutation_lock_regions(), "the running job lost its typed grant"
            started.set()
            await release.wait()
            return {"success": True}

        monkeypatch.setattr(fixture.client, "refresh_file_node_beacons", work, raising=False)
        worker = tmp_path / "worker.py"
        worker.write_text("# worker\n", encoding="utf-8")
        result = await gov.launch_carto_refresh_job(
            root_uuid="file-b",
            mode="file",
            account_scope_id=_ACCOUNT,
            footprint_factory=lambda _selection: running_footprint,
            jobs_root=jobs,
            schedule_job=fixture.schedule,
            worker_script=worker,
            env={},
            freshness_callback=lambda: _snapshot(tmp_path),
        )
        assert result["success"] is True
        await started.wait()
        with pytest.raises(gov.GovernanceConflictError):
            await gov.acquire_q3_leases_async(
                [running_footprint], account_scope_id=_ACCOUNT, jobs_root=jobs
            )
        release.set()
        await fixture.tasks[0]
        await asyncio.sleep(0)
        leases = await gov.acquire_q3_leases_async(
            [running_footprint], account_scope_id=_ACCOUNT, jobs_root=jobs
        )
        assert len(leases) == 1
        # Incoming Q3 has no cooldown: a released CARTO job permits it at once.
        assert await gov.release_q3_leases_async(leases, jobs_root=jobs) == []

    asyncio.run(scenario())
