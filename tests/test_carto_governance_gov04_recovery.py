"""CUT-2B §9.7.6 — the one retained finalizer witness.

All fourteen artifact-release retry/hand-repair tests prove a file/lock cleanup
failure or its supervisor and retire with that mechanism (§6.2.4).  The one
converted row drives the real in-memory finalizer: exact-token idempotence,
absence as success, and no cooldown extension on the second or absent call.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from workflowy_mcp import carto_job_governance as gov
from workflowy_mcp.mutation_locks import _governance_cooldowns, _governance_holds

ACCOUNT = "a" * 32


def _cache() -> dict[str, dict]:
    return {
        "vault": {"id": "vault"},
        "repo": {"id": "repo", "parent_id": "vault"},
        "file": {"id": "file", "parent_id": "repo"},
    }


def _footprint(tmp_path: Path) -> dict:
    target = tmp_path / "repo" / "a.py"
    target.parent.mkdir(exist_ok=True)
    target.write_text("x = 1\n", encoding="utf-8")
    return gov.compute_footprint(target, "file", _cache(), kind="file")


def _admit(jobs: Path, tmp_path: Path, *, job_id: str = "gov04-recovery") -> dict:
    jobs.mkdir(parents=True, exist_ok=True)
    return asyncio.run(
        gov.admit_carto_job(
            job_file=jobs / f"{job_id}.json",
            job_payload={"id": job_id, "type": "CARTO_REFRESH"},
            job_type="CARTO_REFRESH",
            dedupe_key="a" * 64,
            account_scope_id=ACCOUNT,
            footprint=_footprint(tmp_path),
            launcher="system",
            parent_job_id=None,
            prepared=False,
            jobs_root=jobs,
        )
    )


def _cooldown_snapshot(jobs: Path, now_epoch: float) -> tuple[dict, ...]:
    return tuple(
        _governance_cooldowns(jobs, now_epoch=now_epoch, cooldown_seconds=1000.0)
    )


def test_finalize_claim_async_is_idempotent_after_release_and_when_initially_absent(
    tmp_path,
):
    jobs = tmp_path / "jobs"
    admitted = _admit(jobs, tmp_path)
    claim_id = admitted["claim"]["claim_id"]
    assert admitted["claim_path"] is None
    assert [hold["claim_id"] for hold in _governance_holds(jobs)] == [claim_id]

    async def scenario() -> None:
        assert await gov.finalize_claim_async(claim_id, jobs, now_epoch=200) is True
        assert tuple(_governance_holds(jobs)) == ()
        first = _cooldown_snapshot(jobs, 200)
        assert len(first) == 1
        assert first[0]["account_scope_id"] == ACCOUNT
        assert gov._parse_time(first[0]["last_terminal_at"]) == 200

        # A second exact finalize is success and cannot extend the terminal
        # timestamp; an absent id is an idempotent no-op, never a new record.
        assert await gov.finalize_claim_async(claim_id, jobs, now_epoch=210) is True
        assert _cooldown_snapshot(jobs, 210) == first
        assert await gov.finalize_claim_async("f" * 32, jobs, now_epoch=220) is True
        assert _cooldown_snapshot(jobs, 220) == first
        assert tuple(_governance_holds(jobs)) == ()

        if os.environ.get("CUT2B_NEUTRALIZE_FINALIZER_IDEMPOTENCE"):
            from workflowy_mcp import mutation_locks as locks

            locks._LOCKS.cooldowns.clear()
        assert _cooldown_snapshot(jobs, 220) == first

    asyncio.run(scenario())
