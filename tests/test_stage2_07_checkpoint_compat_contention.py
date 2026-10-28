"""Stage2-07: checkpoint/compat daemon tasks share one publication record.

Holder/waiter order, defer-not-queue, real patch content, epoch sequencing and
both exact-kind refusals survive the CUT-2A carrier conversion.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace

import pytest
from coherence_harness import (
    s02_node,
    s04_append_n,
    s04_checkpoint_config,
)
from test_coherence_s02_refresh_claim import async_test
from test_stage2_06_refresh_compat_contention import _based_root

from workflowy_mcp.coherence import (
    materialize_current_payload,
    read_manifest,
    run_checkpoint_rotation,
    run_compat_patch,
)
from workflowy_mcp.coherence import checkpoint as cp
from workflowy_mcp.coherence import refresh_authority as ra
from workflowy_mcp.coherence.epoch_publisher import publish_epoch
from workflowy_mcp.coherence.errors import CoherenceSchemaError
from workflowy_mcp.coherence.snapshot_store import new_snapshot_candidate_path, write_snapshot_candidate


@async_test
async def test_checkpoint_first_compat_waits_then_publishes(tmp_path, monkeypatch):
    """Checkpoint holds before publish; compat observes waiting and owns E+2."""
    paths, snapshot_dir = _based_root(tmp_path / "share", records=3)
    holding, contending, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    real_publish = cp.publish_epoch
    real_acquire = ra._acquire_or_join
    async def observe_acquisition(*args, **kwargs):
        result = await real_acquire(*args, **kwargs)
        # A wrongly admitted compat owner must also release the observation
        # rendezvous, so the original kind/order oracle catches it, not a timeout.
        contending.set()
        return result
    monkeypatch.setattr(ra, "_acquire_or_join", observe_acquisition)

    async def paused_publish(**kwargs):
        holding.set()
        await release.wait()
        return await real_publish(**kwargs)
    monkeypatch.setattr(cp, "publish_epoch", paused_publish)

    async def ack_sleep(seconds):
        if seconds > 0:
            contending.set()
        await asyncio.sleep(seconds)

    checkpoint = asyncio.create_task(run_checkpoint_rotation(
        paths, snapshot_dir=snapshot_dir, config=s04_checkpoint_config(record_threshold=2),
    ))
    compat = None
    try:
        await asyncio.wait_for(holding.wait(), 120)
        claim = ra._read_publication_record(paths)
        assert claim is not None and not claim.is_terminal
        assert claim.refresh_kind == "checkpoint"
        compat = asyncio.create_task(run_compat_patch(
            paths=paths, patches=[{"id": "base", "name": "patched-by-compat"}],
            snapshot_dir=snapshot_dir, sleep=ack_sleep, poll_seconds=0.05, max_wait_seconds=120,
        ))
        await asyncio.wait_for(contending.wait(), 120)
        release.set()
        r_ck, r_compat = await asyncio.gather(checkpoint, compat)
        assert r_ck.status == "published" and r_ck.incorporated_through_seq == 3
        assert r_compat.status == "published" and r_compat.epoch_number == 2
        manifest = read_manifest(paths, required=True)
        assert manifest.epoch_number == 2
        claim = ra._read_publication_record(paths)
        assert claim is not None and claim.refresh_kind == "compat_patch"
        by_id = {n["id"]: n for n in materialize_current_payload(paths, manifest).payload["nodes"]}
        assert by_id["base"]["name"] == "patched-by-compat"
    finally:
        release.set()
        await asyncio.gather(*(task for task in (checkpoint, compat) if task is not None), return_exceptions=True)
        ra._PUBLICATION_RECORDS.pop(ra._publication_key(paths), None)


@async_test
async def test_compat_first_checkpoint_defers_then_publishes(tmp_path, monkeypatch):
    """Checkpoint returns before the compat owner is released; later owns E+2."""
    paths, snapshot_dir = _based_root(tmp_path / "share", records=3)
    holding, release = asyncio.Event(), asyncio.Event()
    real_publish = ra.publish_epoch

    async def paused_publish(**kwargs):
        holding.set()
        await release.wait()
        return await real_publish(**kwargs)
    monkeypatch.setattr(ra, "publish_epoch", paused_publish)
    compat = asyncio.create_task(run_compat_patch(
        paths=paths, patches=[{"id": "base", "name": "patched-by-compat"}], snapshot_dir=snapshot_dir,
    ))
    try:
        await asyncio.wait_for(holding.wait(), 120)
        claim = ra._read_publication_record(paths)
        assert claim is not None and not claim.is_terminal
        assert claim.refresh_kind == "compat_patch"
        r_defer = await asyncio.wait_for(run_checkpoint_rotation(
            paths, snapshot_dir=snapshot_dir, config=s04_checkpoint_config(record_threshold=2),
        ), 120)
        assert r_defer.status == "deferred_publisher_busy"
        assert not release.is_set()
        release.set()
        r_compat = await compat
        assert r_compat.status == "published" and r_compat.epoch_number == 1
        s04_append_n(paths, 2)
        followup = await run_checkpoint_rotation(
            paths, snapshot_dir=snapshot_dir, config=s04_checkpoint_config(record_threshold=2),
        )
        assert followup.status == "published", followup.warnings
        assert read_manifest(paths, required=True).epoch_number == 2
    finally:
        release.set()
        await asyncio.gather(compat, return_exceptions=True)
        ra._PUBLICATION_RECORDS.pop(ra._publication_key(paths), None)


async def _run_kind_binding(root, mode, monkeypatch):
    paths, snapshot_dir = _based_root(root, records=3)
    manifest = read_manifest(paths, required=True)
    wrong_kind = "compat_patch" if mode == "checkpoint" else "checkpoint"
    if mode == "checkpoint":
        decision, claim = await cp._acquire_checkpoint_claim(
            paths, due_head=manifest.journal_head_seq, fs_ops=None,
        )
        assert decision == "owner"
    else:
        acquired = await ra._acquire_or_join(
            paths, refresh_kind="compat_patch", snapshot_dir=snapshot_dir, sleep=asyncio.sleep,
            poll_seconds=0.05, max_wait_seconds=30, cancel_check=None, fs_ops=None,
        )
        assert acquired.decision == "owner"
        claim = acquired.claim

    if os.environ.get("STAGE2_NEUTRALIZE_KIND_BINDING"):
        real_read = ra._read_publication_record
        def as_kind(current_paths):
            record = real_read(current_paths)
            if record is not None and current_paths == paths:
                return replace(
                    record, refresh_kind=wrong_kind, cut_token=None,
                    basis_head_seq=manifest.journal_head_seq if wrong_kind == "checkpoint" else None,
                )
            return record
        monkeypatch.setattr(ra, "_read_publication_record", as_kind)

    candidate = write_snapshot_candidate(
        new_snapshot_candidate_path(snapshot_dir), {"nodes": [s02_node("base")]},
        account_scope_id=manifest.account_scope_id,
        epoch_number=claim.target_epoch_number, epoch_id=claim.target_epoch_id,
        incorporated_through_seq=manifest.journal_head_seq, publication_kind=wrong_kind,
    )
    result = {"rejected": False, "error_type": None, "message": None, "published": False}
    try:
        await publish_epoch(
            paths=paths, claim_id=claim.claim_id, basis_epoch_id=manifest.epoch_id,
            candidate_snapshot_path=candidate, incorporated_through_seq=manifest.journal_head_seq,
            publication_kind=wrong_kind, refresh_cut=None,
            clear_hard_through_generation=None, clear_external_through_generation=None,
        )
        result["published"] = True
    except CoherenceSchemaError as exc:
        result.update(rejected=True, error_type="CoherenceSchemaError", message=str(exc))
    finally:
        ra._PUBLICATION_RECORDS.pop(ra._publication_key(paths), None)
    return paths, result


@pytest.mark.parametrize("mode,required", [
    ("checkpoint", "compat_patch publication requires a compat_patch"),
    ("compat", "checkpoint publication requires a checkpoint"),
])
@async_test
async def test_exact_claim_kind_verification_real_processes(tmp_path, monkeypatch, mode, required):
    """Historical name; both mismatch directions now run in the one process."""
    paths, result = await _run_kind_binding(tmp_path / mode, mode, monkeypatch)
    assert result["rejected"] is True, result
    assert result["error_type"] == "CoherenceSchemaError"
    assert required in result["message"]
    assert result["published"] is False
    assert read_manifest(paths, required=True).epoch_number == 0
