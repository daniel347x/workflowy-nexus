"""Stage2-03: same-loop INV-6 folded/tail cuts and bounded checkpoint catch-up.

Fetch waits are real async boundaries. Candidate-write ordering uses a test-only
re-entrant DurableFileOps callback, never a parked loop or a production await seam.
The three separately collected witnesses retain their original durable matrices.
"""
from __future__ import annotations

import asyncio
import os

from coherence_harness import (
    ACCOUNT_A, make_coherence_root, s02_node, s02_set_barrier, s04_append,
    s04_append_n, s04_checkpoint_config, s04_install_base_manifest,
    s04_raise_barrier, s04_write_base_snapshot,
)
from workflowy_mcp import carto_job_governance as gov
from workflowy_mcp.coherence import (
    DirtyBarrierState, DurableFileOps, SemanticEffect, manifest_with_updates,
    materialize_current_payload, read_manifest, run_checkpoint_rotation, run_full_refresh,
)
from workflowy_mcp.coherence.paths import journal_record_filename


def _based_root(root, *, records: int = 1):
    paths = make_coherence_root(root, ACCOUNT_A)
    snapshot_dir = root / "nodes_export_cache"
    base = s04_write_base_snapshot(
        paths,
        snapshot_dir,
        [s02_node("base")],
        epoch_number=0,
        epoch_id="b" * 32,
    )
    s04_install_base_manifest(paths, base_snapshot_path=base, epoch_id="b" * 32)
    if records:
        s04_append_n(paths, records)
    return paths, snapshot_dir


def _cut_counterfactual(monkeypatch):
    """Retain the literal old CUT probe against exact-generation clearing."""
    if not os.environ.get("STAGE2_NEUTRALIZE_CUT"):
        return
    from workflowy_mcp.coherence import epoch_publisher as ep
    real_read = ep.read_manifest
    def downgraded(paths, *, required=True):
        manifest = real_read(paths, required=required)
        if manifest is not None and manifest.dirty_status.active and manifest.dirty_status.generation > 1:
            barrier = manifest.dirty_status
            return manifest_with_updates(manifest, dirty_status=DirtyBarrierState(
                schema_version=barrier.schema_version, account_scope_id=barrier.account_scope_id,
                active=barrier.active, generation=1, reason_code=barrier.reason_code,
                detail=barrier.detail, transaction_id=barrier.transaction_id,
                affected_region=barrier.affected_region, raised_at=barrier.raised_at,
            ))
        return manifest
    monkeypatch.setattr(ep, "read_manifest", downgraded)


def test_refresh_appender_barrier_folded_record_and_surviving_barrier(tmp_path, monkeypatch):
    """PAIR-003/017: append before incorporation; only the captured barrier clears."""
    from workflowy_mcp.coherence import refresh_authority as ra
    paths, snapshot_dir = _based_root(tmp_path / "share", records=1)
    s02_set_barrier(paths, 1, active=True)
    _cut_counterfactual(monkeypatch)
    if os.environ.get("STAGE2_NEUTRALIZE_FOLD"):
        monkeypatch.setattr(ra, "read_journal_records", lambda *a, **k: [])
    async def scenario():
        cut_ready, release = asyncio.Event(), asyncio.Event()
        async def fetch():
            record = ra._read_publication_record(paths)
            assert record is not None and not record.is_terminal and record.cut_token is not None
            assert record.cut_token.hard_barrier_generation == 1
            cut_ready.set()
            await release.wait()
            return {"nodes": [s02_node("a")]}
        owner = asyncio.create_task(run_full_refresh(
            paths=paths, fetch=fetch, refresh_kind="system", settle_seconds=0, snapshot_dir=snapshot_dir,
        ))
        try:
            await cut_ready.wait()
            s04_append(paths, effects=[SemanticEffect.node_create(s02_node("inflight"))], transaction_id="a" * 32)
            s04_raise_barrier(paths, reason_code="ambiguous_commit_outcome", transaction_id="post-cut-barrier")
        finally:
            release.set()
        outcome = await owner
        assert outcome.status == "published"
        assert "inflight" in {n["id"] for n in outcome.nodes_export["nodes"]}
        manifest = read_manifest(paths, required=True)
        assert (manifest.epoch_number, manifest.journal_head_seq, manifest.base_incorporated_through_seq, manifest.journal_floor_seq) == (1, 2, 2, 3)
        assert manifest.dirty_status.active and manifest.dirty_status.generation == 2
        assert manifest.dirty_status.transaction_id == "post-cut-barrier"
        async def followup_fetch():
            record = ra._read_publication_record(paths)
            assert record.cut_token.hard_barrier_generation == 2
            return outcome.nodes_export
        followup = await run_full_refresh(paths=paths, fetch=followup_fetch, settle_seconds=0, snapshot_dir=snapshot_dir)
        assert followup.status == "published" and followup.hard_barrier_cleared
        assert not read_manifest(paths).dirty_status.active
    asyncio.run(scenario())


def test_refresh_appender_barrier_tail_record_preserved_and_barrier_survives(tmp_path, monkeypatch):
    """PAIR-003: watermark 1, floor/head 2, live seq-2 record and later barrier."""
    from workflowy_mcp.coherence import snapshot_store as store
    paths, snapshot_dir = _based_root(tmp_path / "share", records=1)
    s02_set_barrier(paths, 1, active=True)
    _cut_counterfactual(monkeypatch)
    if os.environ.get("CUT2B_NEUTRALIZE_TAIL_RECORD"):
        monkeypatch.setattr(store, "read_journal_records", lambda *a, **k: [])
    crossed = []
    def write_fsync(path, text):
        if "nodes_export_snapshot" in path.name and not crossed:
            crossed.append(True)
            s04_append(paths, effects=[SemanticEffect.node_create(s02_node("tailnode"))], transaction_id="a" * 32)
            s04_raise_barrier(paths, reason_code="ambiguous_commit_outcome", transaction_id="post-cut-barrier")
        gov._default_write_fsync(path, text)
    async def fetch():
        return {"nodes": [s02_node("a")]}
    outcome = asyncio.run(run_full_refresh(
        paths=paths, fetch=fetch, refresh_kind="system", settle_seconds=0,
        snapshot_dir=snapshot_dir, fs_ops=DurableFileOps(
            write_fsync=write_fsync, replace=gov._default_replace, mkdir=gov._default_mkdir,
            unlink=gov._default_unlink, tmp_name=gov._default_tmp_name,
        ),
    ))
    assert crossed == [True] and outcome.status == "published"
    assert "tailnode" in {n["id"] for n in outcome.nodes_export["nodes"]}
    manifest = read_manifest(paths, required=True)
    assert (manifest.epoch_number, manifest.base_incorporated_through_seq, manifest.journal_floor_seq, manifest.journal_head_seq) == (1, 1, 2, 2)
    assert (paths.journal_dir / journal_record_filename(2)).exists()
    replayed = materialize_current_payload(paths, manifest).payload
    assert "tailnode" in {n["id"] for n in replayed["nodes"]}
    assert manifest.dirty_status.active and manifest.dirty_status.generation == 2


def test_checkpoint_appender_barrier_bounded_catchup_defers_head_advancing(tmp_path):
    """PAIR-004: max_catchup_passes=2; no loop parked inside synchronous fsync."""
    paths, snapshot_dir = _based_root(tmp_path / "share", records=3)
    passes = []
    def write_fsync(path, text):
        if "nodes_export_snapshot" in path.name:
            passes.append(len(passes) + 1)
            n = passes[-1]
            if not os.environ.get("STAGE2_NEUTRALIZE_APPENDS") or n == 1:
                s04_append(paths, effects=[SemanticEffect.node_create(s02_node(f"app-{n}"))], transaction_id=f"{'abc'[n - 1]}" * 32)
            if n == 1:
                s04_raise_barrier(paths, reason_code="ambiguous_commit_outcome", transaction_id="ck-window-barrier")
        gov._default_write_fsync(path, text)
    config = s04_checkpoint_config(record_threshold=1, max_catchup_passes=2)
    result = asyncio.run(run_checkpoint_rotation(
        paths, snapshot_dir=snapshot_dir, config=config, fs_ops=DurableFileOps(
            write_fsync=write_fsync, replace=gov._default_replace, mkdir=gov._default_mkdir,
            unlink=gov._default_unlink, tmp_name=gov._default_tmp_name,
        ),
    ))
    assert result.status == "deferred_head_advancing", result.warnings
    assert passes == [1, 2, 3]
    manifest = read_manifest(paths, required=True)
    assert (manifest.epoch_number, manifest.base_incorporated_through_seq, manifest.journal_head_seq) == (0, 0, 6)
    for seq in range(1, 7):
        assert (paths.journal_dir / journal_record_filename(seq)).exists()
    assert manifest.dirty_status.active and manifest.dirty_status.generation == 1
    assert manifest.dirty_status.transaction_id == "ck-window-barrier"
    followup = asyncio.run(run_checkpoint_rotation(paths, snapshot_dir=snapshot_dir, config=config))
    assert followup.status == "published", followup.warnings
    manifest = read_manifest(paths, required=True)
    assert manifest.epoch_number == 1 and manifest.base_incorporated_through_seq == 6
    assert manifest.dirty_status.active and manifest.dirty_status.generation == 1
