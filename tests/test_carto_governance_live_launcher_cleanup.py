"""CUT-2B §9.7.7 — surviving admission-write and compatibility-format rows.

Ten spawn/claim-supervisor/token-artifact scenarios retire with the file plane
they prove.  The retained rows are the real replace-then-raise admission writer,
the inert `GovernanceCleanupError` J formatter passthrough (compatibility shape
only), the unrelated generic failure kinds, and the primary-error propagation
when the initial job JSON write fails after a memory reservation was taken.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path

import pytest

from workflowy_mcp import carto_job_governance as gov
from workflowy_mcp import server
from workflowy_mcp.mutation_locks import _governance_cooldowns, _governance_holds

ACCOUNT = "a" * 32


def _cache() -> dict[str, dict]:
    return {
        "vault": {"id": "vault"},
        "repo": {"id": "repo", "parent_id": "vault"},
        "file": {"id": "file", "parent_id": "repo"},
    }


def _footprint(tmp_path: Path, name: str = "a.py") -> dict:
    target = tmp_path / "repo" / name
    target.parent.mkdir(exist_ok=True)
    target.write_text("x = 1\n", encoding="utf-8")
    return gov.compute_footprint(target, "file", _cache(), kind="file")


def _cooldowns(jobs: Path) -> tuple[dict, ...]:
    return tuple(_governance_cooldowns(jobs, now_epoch=0.0, cooldown_seconds=10.0))


def test_admission_writer_replaced_job_then_raised_keeps_one_failed_job(
    tmp_path, monkeypatch
):
    async def scenario() -> None:
        jobs = tmp_path / "jobs"
        jobs.mkdir(parents=True, exist_ok=True)
        job_file = jobs / "new.json"

        def writer(path, payload):
            target = Path(path)
            if target == job_file:
                # The real writer replaced the job and only then raised.
                gov.atomic_write_json(target, payload)
                if os.environ.get("CUT2B_NEUTRALIZE_PRIMARY_ERROR_MASK"):
                    # Probe: the real primary write error is replaced.
                    raise RuntimeError("primary error masked")
                raise OSError("initial job JSON write failed")
            gov.atomic_write_json(target, payload)

        if os.environ.get("CUT2B_NEUTRALIZE_ADMISSION_LEAK"):
            from workflowy_mcp import mutation_locks as locks

            monkeypatch.setattr(locks, "_finalize_carto_hold", lambda *args, **kwargs: True)

        with pytest.raises(OSError, match="initial job JSON write failed"):
            await gov.admit_carto_job(
                job_file=job_file,
                job_payload={"id": "new", "type": "CARTO_REFRESH"},
                job_type="CARTO_REFRESH",
                dedupe_key="b" * 64,
                account_scope_id=ACCOUNT,
                footprint=_footprint(tmp_path),
                launcher="system",
                parent_job_id=None,
                prepared=False,
                jobs_root=jobs,
                atomic_writer=writer,
            )

        assert [path.name for path in jobs.glob("*.json")] == ["new.json"]
        job = json.loads(job_file.read_text(encoding="utf-8"))
        assert job["status"] == "failed"
        assert "Initial CARTO job JSON write failed" in job["error"]
        # The exact reservation is gone and its terminal cooldown remains.
        assert tuple(_governance_holds(jobs)) == ()
        assert _cooldowns(jobs)

    asyncio.run(scenario())


def _snapshot(path: Path) -> dict[str, str]:
    # S02: governed snapshots carry the v1 lineage wrapper (legacy wrappers
    # are bootstrap-only and no longer accepted at this seam).
    payload = {
        "snapshot_schema_version": 1,
        "snapshot_timestamp": gov.utc_now_iso(),
        "account_scope_id": ACCOUNT,
        "epoch_number": 0,
        "epoch_id": "e" * 32,
        "incorporated_through_seq": 0,
        "publication_kind": "full_refresh",
        "nodes_export": {"nodes": []},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return {"snapshot_path": str(path), "freshness_disposition": "performed"}


def _cleanup_failure() -> gov.GovernanceCleanupError:
    try:
        raise OSError("initial job JSON write failed")
    except OSError as primary:
        try:
            raise gov.GovernanceCleanupError(
                "claim cleanup remains blocked",
                artifact_paths=("A.lock.json", "B.lock.json"),
            ) from primary
        except gov.GovernanceCleanupError as cleanup:
            return cleanup


async def _unexpected_schedule(spec):
    raise AssertionError("admission failure unexpectedly reached task scheduling")


def test_public_refresh_launcher_preserves_cleanup_required_plain_and_prepared(
    tmp_path, monkeypatch
):
    """Compatibility-format only: the retained inert error keeps the J payload.

    No production cleanup producer or supervisor is revived; the typed error is
    injected through the existing admission seam and the unchanged J formatter
    must still reproduce its exact shape.
    """
    worker = tmp_path / "worker.py"
    worker.write_text("# worker\n", encoding="utf-8")
    snapshot = _snapshot(tmp_path / "snapshot.json")

    async def fail_admission(**_kwargs):
        raise _cleanup_failure()

    monkeypatch.setattr(gov, "admit_carto_job", fail_admission)

    async def launch(*, prepared: bool) -> dict:
        kwargs = {
            "root_uuid": "file",
            "mode": "file",
            "account_scope_id": ACCOUNT,
            "footprint_factory": lambda _selection: _footprint(tmp_path),
            "jobs_root": tmp_path / ("prepared-jobs" if prepared else "plain-jobs"),
            "schedule_job": _unexpected_schedule,
            "worker_script": worker,
            "env": {},
            "freshness_callback": lambda: snapshot,
        }
        if prepared:
            kwargs.update(
                preparation=lambda *_args: {"success": True},
                initial_footprint=_footprint(tmp_path, "prepared.py"),
            )
        return await gov.launch_carto_refresh_job(**kwargs)

    async def scenario() -> None:
        for prepared in (False, True):
            result = await launch(prepared=prepared)
            assert result == {
                "success": False,
                "error_kind": "governance_cleanup_required",
                "error": "claim cleanup remains blocked",
                "blocking_artifacts": ["A.lock.json", "B.lock.json"],
                "root_uuid": "file",
                "mode": "file",
                "primary_error": "initial job JSON write failed",
            }

    asyncio.run(scenario())


def test_public_refresh_launcher_keeps_unrelated_generic_failure_kinds(
    tmp_path, monkeypatch
):
    worker = tmp_path / "worker.py"
    worker.write_text("# worker\n", encoding="utf-8")
    snapshot = _snapshot(tmp_path / "snapshot.json")

    async def fail_admission(**_kwargs):
        raise RuntimeError("ordinary admission failure")

    monkeypatch.setattr(gov, "admit_carto_job", fail_admission)

    async def scenario() -> None:
        plain = await gov.launch_carto_refresh_job(
            root_uuid="file",
            mode="file",
            account_scope_id=ACCOUNT,
            footprint_factory=lambda _selection: _footprint(tmp_path),
            jobs_root=tmp_path / "plain-jobs",
            schedule_job=_unexpected_schedule,
            worker_script=worker,
            env={},
            freshness_callback=lambda: snapshot,
        )
        prepared = await gov.launch_carto_refresh_job(
            root_uuid="file",
            mode="file",
            account_scope_id=ACCOUNT,
            footprint_factory=lambda _selection: _footprint(tmp_path),
            jobs_root=tmp_path / "prepared-jobs",
            schedule_job=_unexpected_schedule,
            worker_script=worker,
            env={},
            freshness_callback=lambda: snapshot,
            preparation=lambda *_args: {"success": True},
            initial_footprint=_footprint(tmp_path, "prepared-generic.py"),
        )
        assert plain["error_kind"] == "launch_preflight_failed"
        assert prepared["error_kind"] == "admission_failed"

    asyncio.run(scenario())


def test_server_and_legacy_refresh_wrappers_preserve_cleanup_payload(
    tmp_path, monkeypatch
):
    payload = {
        "success": False,
        "error_kind": "governance_cleanup_required",
        "error": "claim cleanup remains blocked",
        "blocking_artifacts": ["claim.lock.json"],
        "primary_error": "initial job JSON write failed",
        "root_uuid": "file",
        "mode": "file",
    }

    async def fake_launch(**_kwargs):
        return dict(payload)

    monkeypatch.setattr(server, "launch_carto_refresh_job", fake_launch)
    monkeypatch.setattr(server, "_carto_account_scope", lambda: ACCOUNT)
    monkeypatch.setattr(server, "_governance_jobs_root", lambda: tmp_path / "jobs")

    async def _noop_gate(paths, *, operation):
        return None

    # S04: the hard-stop launch gate is covered by the S04 suites; this test
    # exercises the cleanup-payload wrapper preservation in isolation.
    monkeypatch.setattr(
        "workflowy_mcp.coherence.admission.require_coherence_mutation_admission",
        _noop_gate,
    )

    async def scenario() -> None:
        wrapped = await server._start_carto_refresh_job("file", "file")
        assert wrapped == payload
        monkeypatch.setattr(
            server,
            "_start_carto_refresh_job",
            lambda **_kwargs: asyncio.sleep(0, result=dict(payload)),
        )
        legacy = await server.cartographer_refresh_async("file", "file")
        assert legacy == payload

    asyncio.run(scenario())


def _admission_kwargs(tmp_path: Path, jobs: Path, job_file: Path, atomic_writer):
    return {
        "job_file": job_file,
        "job_payload": {"id": job_file.stem, "type": "CARTO_REFRESH"},
        "job_type": "CARTO_REFRESH",
        "dedupe_key": hashlib.sha256(job_file.stem.encode()).hexdigest(),
        "account_scope_id": ACCOUNT,
        "footprint": _footprint(tmp_path, f"{job_file.stem}.py"),
        "launcher": "system",
        "parent_job_id": None,
        "prepared": False,
        "jobs_root": jobs,
        "atomic_writer": atomic_writer,
    }


def test_admission_job_write_failure_with_already_absent_claim_keeps_primary(
    tmp_path, monkeypatch
):
    async def scenario() -> None:
        jobs = tmp_path / "jobs"
        jobs.mkdir(parents=True, exist_ok=True)
        job_file = jobs / "absent.json"

        def writer(path, payload):
            target = Path(path)
            if target == job_file:
                raise OSError("initial job JSON write failed")
            gov.atomic_write_json(target, payload)

        if os.environ.get("CUT2B_NEUTRALIZE_PRIMARY_ERROR_MASK"):
            def writer(path, payload):
                target = Path(path)
                if target == job_file:
                    raise RuntimeError("primary error masked")
                gov.atomic_write_json(target, payload)

        with pytest.raises(OSError, match="initial job JSON write failed"):
            await gov.admit_carto_job(**_admission_kwargs(tmp_path, jobs, job_file, writer))

        assert not job_file.exists()
        assert not list(jobs.glob("*.json"))
        assert tuple(_governance_holds(jobs)) == ()
        assert _cooldowns(jobs)

    asyncio.run(scenario())
