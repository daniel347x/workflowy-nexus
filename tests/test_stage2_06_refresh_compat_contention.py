"""Stage2-06: full/compat contention on the one in-process publication record.

CUT-2A converts only the publication carrier: holder/waiter acknowledgments,
fresh tokens, E+1/E+2, patch content and stale-publisher rejection survive.
The async publication seam parks after candidate storage, before its lock.
"""

from __future__ import annotations

import asyncio
import os

from coherence_harness import (
    ACCOUNT_A,
    make_coherence_root,
    s02_node,
    s04_append_n,
    s04_install_base_manifest,
    s04_write_base_snapshot,
)
from test_coherence_s02_refresh_claim import async_test

from workflowy_mcp.coherence import (
    materialize_current_payload,
    read_manifest,
    run_compat_patch,
    run_full_refresh,
)
from workflowy_mcp.coherence import refresh_authority as ra


def _based_root(root, *, records: int = 0):
    paths = make_coherence_root(root, ACCOUNT_A)
    snapshot_dir = root / "nodes_export_cache"
    base = s04_write_base_snapshot(
        paths, snapshot_dir, [s02_node("base")], epoch_number=0, epoch_id="b" * 32,
    )
    s04_install_base_manifest(paths, base_snapshot_path=base, epoch_id="b" * 32)
    if records:
        s04_append_n(paths, records)
    return paths, snapshot_dir


@async_test
async def test_refresh_first_compat_never_dedupes_or_borrows_then_publishes(tmp_path, monkeypatch):
    """Full owner holds at fetch; compat acknowledges waiting, then owns E+2."""
    paths, snapshot_dir = _based_root(tmp_path / "share")
    holding, contending, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    real_acquire = ra._acquire_or_join

    if os.environ.get("STAGE2_NEUTRALIZE_COMPAT_DEDUPE"):
        async def wrongly_join(paths, *, refresh_kind, **kwargs):
            record = ra._read_publication_record(paths)
            if refresh_kind == "compat_patch" and record is not None and not record.is_terminal:
                contending.set()
                return ra._Acquisition("joined", record, 0, 0, False)
            return await real_acquire(paths, refresh_kind=refresh_kind, **kwargs)
        monkeypatch.setattr(ra, "_acquire_or_join", wrongly_join)

    async def fetch():
        holding.set()
        await release.wait()
        return {"nodes": [s02_node("base")]}

    async def ack_sleep(seconds):
        if seconds > 0:
            contending.set()
        await asyncio.sleep(seconds)

    owner = asyncio.create_task(run_full_refresh(
        paths=paths, fetch=fetch, settle_seconds=0, snapshot_dir=snapshot_dir, poll_seconds=0.05,
    ))
    waiter = None
    try:
        await asyncio.wait_for(holding.wait(), 120)
        waiter = asyncio.create_task(run_compat_patch(
            paths=paths, patches=[{"id": "base", "name": "patched-by-compat"}],
            snapshot_dir=snapshot_dir, sleep=ack_sleep, poll_seconds=0.05, max_wait_seconds=120,
        ))
        await asyncio.wait_for(contending.wait(), 120)
        claim = ra._read_publication_record(paths)
        assert claim is not None and not claim.is_terminal
        assert claim.refresh_kind != "compat_patch"
        release.set()
        r_fr, r_compat = await asyncio.gather(owner, waiter)
        assert r_fr.status == "published" and r_fr.epoch_number == 1
        assert r_fr.deduplicated is False
        assert r_compat.status == "published" and r_compat.epoch_number == 2
        assert r_compat.claim_id != r_fr.claim_id
        manifest = read_manifest(paths, required=True)
        assert manifest.epoch_number == 2
        claim = ra._read_publication_record(paths)
        assert claim is not None and claim.refresh_kind == "compat_patch"
        payload = materialize_current_payload(paths, manifest).payload
        by_id = {n["id"]: n for n in payload["nodes"]}
        assert by_id["base"]["name"] == "patched-by-compat"
    finally:
        release.set()
        await asyncio.gather(*(task for task in (owner, waiter) if task is not None), return_exceptions=True)
        ra._PUBLICATION_RECORDS.pop(ra._publication_key(paths), None)


@async_test
async def test_compat_first_refresh_cannot_join_waits_then_publishes(tmp_path, monkeypatch):
    """Compat holds before publish; full requester must wait and fetch E+2."""
    paths, snapshot_dir = _based_root(tmp_path / "share")
    holding, contending, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    fetched = []
    real_publish = ra.publish_epoch

    async def paused_publish(**kwargs):
        if kwargs["publication_kind"] == "compat_patch":
            holding.set()
            await release.wait()
        return await real_publish(**kwargs)
    monkeypatch.setattr(ra, "publish_epoch", paused_publish)

    async def ack_sleep(seconds):
        if seconds > 0:
            contending.set()
        await asyncio.sleep(seconds)

    async def fetch():
        fetched.append("fr_fetched")
        return {"nodes": [s02_node("base")]}

    compat = asyncio.create_task(run_compat_patch(
        paths=paths, patches=[{"id": "base", "name": "patched-by-compat"}], snapshot_dir=snapshot_dir,
    ))
    refresh = None
    try:
        await asyncio.wait_for(holding.wait(), 120)
        refresh = asyncio.create_task(run_full_refresh(
            paths=paths, fetch=fetch, settle_seconds=0, snapshot_dir=snapshot_dir,
            sleep=ack_sleep, poll_seconds=0.05, max_wait_seconds=120,
        ))
        await asyncio.wait_for(contending.wait(), 120)
        claim = ra._read_publication_record(paths)
        assert claim is not None and not claim.is_terminal
        assert claim.refresh_kind == "compat_patch"
        assert fetched == []
        release.set()
        r_compat, r_fr = await asyncio.gather(compat, refresh)
        assert r_compat.status == "published" and r_compat.epoch_number == 1
        assert r_fr.status == "published" and r_fr.epoch_number == 2
        assert r_fr.deduplicated is False
        assert fetched == ["fr_fetched"]
        assert read_manifest(paths, required=True).epoch_number == 2
    finally:
        release.set()
        await asyncio.gather(*(task for task in (compat, refresh) if task is not None), return_exceptions=True)
        ra._PUBLICATION_RECORDS.pop(ra._publication_key(paths), None)


@async_test
async def test_stale_candidate_cannot_publish_after_claim_turnover(tmp_path, monkeypatch):
    """A terminalized/replaced parked compat owner cannot alter E+1's lineage."""
    paths, snapshot_dir = _based_root(tmp_path / "share")
    holding, release = asyncio.Event(), asyncio.Event()
    real_publish = ra.publish_epoch

    async def paused_publish(**kwargs):
        if kwargs["publication_kind"] == "compat_patch":
            holding.set()
            await release.wait()
        return await real_publish(**kwargs)
    monkeypatch.setattr(ra, "publish_epoch", paused_publish)
    stale = asyncio.create_task(run_compat_patch(
        paths=paths, patches=[{"id": "base", "name": "patched-by-compat"}], snapshot_dir=snapshot_dir,
    ))
    try:
        await asyncio.wait_for(holding.wait(), 120)
        claim = ra._read_publication_record(paths)
        assert claim is not None and not claim.is_terminal
        assert claim.refresh_kind == "compat_patch"
        await ra._write_claim_status(paths, claim, "failed", result={"error": "aborted owner (test-terminalized)"})

        async def fetch():
            return {"nodes": [s02_node("base")]}
        r_fr = await run_full_refresh(
            paths=paths, fetch=fetch, settle_seconds=0, snapshot_dir=snapshot_dir, poll_seconds=0.05,
        )
        assert r_fr.status == "published" and r_fr.epoch_number == 1
        # DI40KK: reusing the stale owner's otherwise lawful record is also
        # rejected by the retained basis guard. It must never cross token
        # validation into the following committed-range validation phase.
        from workflowy_mcp.coherence import epoch_publisher as authority
        post_token_steps = []
        real_validate = authority._validate_committed_range
        def observe_after_token(*args, **kwargs):
            post_token_steps.append("committed_range")
            return real_validate(*args, **kwargs)
        monkeypatch.setattr(authority, "_validate_committed_range", observe_after_token)
        release.set()
        result = (await asyncio.gather(stale, return_exceptions=True))[0]
        assert isinstance(result, Exception)
        assert type(result).__name__ == "ManifestConflictError"
        manifest = read_manifest(paths, required=True)
        assert manifest.epoch_number == 1
        payload = materialize_current_payload(paths, manifest).payload
        by_id = {n["id"]: n for n in payload["nodes"]}
        assert by_id["base"]["name"] == "node-base"
        assert post_token_steps == []
    finally:
        release.set()
        await asyncio.gather(stale, return_exceptions=True)
        ra._PUBLICATION_RECORDS.pop(ra._publication_key(paths), None)
