"""S04 §14.5 (rotation) — checkpoint-and-rotate compaction tests."""

from __future__ import annotations

import asyncio
import os
import pytest
from test_cut_2a_publication import _publication_records_cleanup

from coherence_harness import (
    ACCOUNT_A,
    RecordedSleep,
    make_coherence_root,
    s02_fetch,
    s02_make_export,
    s02_node,
    s04_append,
    s04_append_n,
    s04_checkpoint_config,
    s04_install_base_manifest,
    s04_write_base_snapshot,
    seed_publication_record,
)

from workflowy_mcp.coherence import (
    DurableFileOps,
    load_manifest_base_envelope,
    parse_journal_record_filename,
    read_manifest,
    run_checkpoint_rotation,
    run_full_refresh,
)
from workflowy_mcp.coherence import checkpoint as checkpoint_module
from workflowy_mcp.coherence.refresh_authority import _read_publication_record, _write_claim_status


def _snapshot_dir(tmp_path):
    return tmp_path / "nodes_export_cache"


def _based_manifest(paths, tmp_path, *, nodes=None):
    base = s04_write_base_snapshot(
        paths,
        _snapshot_dir(tmp_path),
        nodes if nodes is not None else [s02_node("a")],
        epoch_number=0,
        epoch_id="b" * 32,
    )
    s04_install_base_manifest(paths, base_snapshot_path=base, epoch_id="b" * 32)
    return base


def _journal_sequences(paths):
    if not paths.journal_dir.exists():
        return []
    out = []
    for entry in paths.journal_dir.iterdir():
        sequence = parse_journal_record_filename(entry.name)
        if sequence is not None:
            out.append(sequence)
    return sorted(out)


def test_rotation_publishes_e_plus_1_with_correct_watermarks_and_content(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    _based_manifest(paths, tmp_path)
    s04_append_n(paths, 3)
    before = read_manifest(paths, required=True)
    result = asyncio.run(
        run_checkpoint_rotation(
            paths,
            snapshot_dir=_snapshot_dir(tmp_path),
            config=s04_checkpoint_config(record_threshold=2),
        )
    )
    assert result.status == "published", result.warnings
    assert result.basis_epoch_id == before.epoch_id
    assert result.incorporated_through_seq == 3
    manifest = read_manifest(paths, required=True)
    assert manifest.epoch_number == before.epoch_number + 1
    assert manifest.epoch_id != before.epoch_id
    assert manifest.previous_epoch_id == before.epoch_id
    assert manifest.base_incorporated_through_seq == 3
    assert manifest.journal_floor_seq == 4
    assert manifest.journal_head_seq == 3  # head UNCHANGED
    # The new base validates and carries the journaled effects (the three
    # tombstones materialized into it).
    envelope = load_manifest_base_envelope(paths, manifest)
    assert envelope.publication_kind == "checkpoint"
    assert envelope.incorporated_through_seq == 3
    assert envelope.epoch_number == manifest.epoch_number
    assert envelope.epoch_id == manifest.epoch_id


def test_not_due(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    _based_manifest(paths, tmp_path)
    s04_append_n(paths, 1)
    result = asyncio.run(
        run_checkpoint_rotation(
            paths,
            snapshot_dir=_snapshot_dir(tmp_path),
            config=s04_checkpoint_config(record_threshold=500),
        )
    )
    assert result.status == "not_due"
    assert _read_publication_record(paths) is None


def test_append_during_materialization_is_caught_up(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    _based_manifest(paths, tmp_path)
    s04_append_n(paths, 3)

    real_write = checkpoint_module.write_snapshot_candidate
    appended = {"done": False}

    def _write_and_append(*args, **kwargs):
        if not appended["done"]:
            appended["done"] = True
            # A journal append lands while the checkpoint materializes OUT of lock.
            s04_append(paths, transaction_id="tx-mid-flight")
        return real_write(*args, **kwargs)

    checkpoint_module.write_snapshot_candidate = _write_and_append
    try:
        result = asyncio.run(
            run_checkpoint_rotation(
                paths,
                snapshot_dir=_snapshot_dir(tmp_path),
                config=s04_checkpoint_config(record_threshold=2),
            )
        )
    finally:
        checkpoint_module.write_snapshot_candidate = real_write
    assert result.status == "published", result.warnings
    manifest = read_manifest(paths, required=True)
    # The mid-flight append was incorporated through the bounded catch-up.
    assert manifest.journal_head_seq == 4
    assert manifest.base_incorporated_through_seq == 4
    assert manifest.journal_floor_seq == 5
    assert result.incorporated_through_seq == 4


def test_continuously_advancing_head_defers_with_lineage_untouched(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    _based_manifest(paths, tmp_path)
    s04_append_n(paths, 3)

    real_write = checkpoint_module.write_snapshot_candidate

    def _write_and_append_every_time(*args, **kwargs):
        # Every candidate rewrite is matched by ANOTHER append — the head
        # never stabilizes inside the bounded catch-up budget.
        s04_append(paths)
        return real_write(*args, **kwargs)

    checkpoint_module.write_snapshot_candidate = _write_and_append_every_time
    before = read_manifest(paths, required=True)
    try:
        result = asyncio.run(
            run_checkpoint_rotation(
                paths,
                snapshot_dir=_snapshot_dir(tmp_path),
                config=s04_checkpoint_config(record_threshold=2, max_catchup_passes=1),
            )
        )
    finally:
        checkpoint_module.write_snapshot_candidate = real_write
    assert result.status == "deferred_head_advancing"
    after = read_manifest(paths, required=True)
    # Old lineage wholly intact (only the appends advanced the head).
    assert after.epoch_id == before.epoch_id
    assert after.epoch_number == before.epoch_number
    assert after.base_incorporated_through_seq == before.base_incorporated_through_seq
    assert after.journal_floor_seq == before.journal_floor_seq
    # The checkpoint claim was terminalized (failed), not left active.
    claim = _read_publication_record(paths)
    assert claim is not None and claim.is_terminal and claim.status == "failed"


def test_full_refresh_claim_excludes_checkpoint(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    _based_manifest(paths, tmp_path)
    s04_append_n(paths, 3)

    claim = seed_publication_record(paths, status="downloading")
    result = asyncio.run(
        run_checkpoint_rotation(
            paths,
            snapshot_dir=_snapshot_dir(tmp_path),
            config=s04_checkpoint_config(record_threshold=2),
        )
    )
    assert result.status == "deferred_publisher_busy"
    # The checkpoint did NOT mint its own claim or queue behind the refresh.
    assert _read_publication_record(paths).claim_id == claim.claim_id


@pytest.mark.parametrize("basis_offset,covers", [(0, True), (1, True), (-1, False)])
def test_checkpoint_dedupes_onto_covering_checkpoint_claim(tmp_path, basis_offset, covers):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    _based_manifest(paths, tmp_path)
    s04_append_n(paths, 3)
    manifest = read_manifest(paths, required=True)

    claim = seed_publication_record(
        paths,
        refresh_kind="checkpoint",
        status="materializing",
        cut_token=None,
        basis_head_seq=manifest.journal_head_seq + basis_offset,
    )
    result = asyncio.run(
        run_checkpoint_rotation(
            paths,
            snapshot_dir=_snapshot_dir(tmp_path),
            config=s04_checkpoint_config(record_threshold=2),
        )
    )
    assert result.status == "deferred_publisher_busy"
    assert any("deduplicated" in warning for warning in result.warnings) is covers
    assert _read_publication_record(paths).claim_id == claim.claim_id


def test_stale_basis_epoch_rejection_discards_candidate(tmp_path, monkeypatch):
    """§11.4: an epoch cut racing the checkpoint defers it — never a stale
    publication, never hard dirt.

    The shared publication claim serializes the two epoch publishers, so a
    full refresh cannot win WHILE the checkpoint's claim is live (it would
    correctly wait). The fixture terminalizes the parked checkpoint's
    in-memory record underneath it; a successor refresh in this process wins
    and publishes E+1, and the checkpoint's next exact-token write fails —
    the checkpoint DEFERS (never publishes its stale candidate).
    """

    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    _based_manifest(paths, tmp_path)
    s04_append_n(paths, 3)

    # DI40KK: the later basis guard also defers a writer whose token store
    # was wrongly admitted. Observe that exact-token crossing directly.
    from workflowy_mcp.coherence import refresh_authority as authority
    foreign_stores = []
    real_store = authority._store_publication_record
    def observe_store(paths_arg, record, *, expected_claim_id=None):
        current = authority._read_publication_record(paths_arg)
        foreign = expected_claim_id is not None and (
            current is None or current.claim_id != expected_claim_id
        )
        if os.environ.get("CUT2B_NEUTRALIZE_PUBLICATION_TOKEN"):
            expected_claim_id = None
        result = real_store(paths_arg, record, expected_claim_id=expected_claim_id)
        if foreign:
            foreign_stores.append(expected_claim_id)
        return result
    monkeypatch.setattr(authority, "_store_publication_record", observe_store)

    real_phase = checkpoint_module._write_claim_status
    raced = {"done": False}

    async def _phase_and_publish_refresh(paths_arg, record, status, **changes):
        # Existing async phase boundary, outside the short lock and after the
        # candidate was written; no production seam or worker loop is added.
        if status == "materializing" and changes.get("candidate_snapshot_path") and not raced["done"]:
            raced["done"] = True
            payload = s02_make_export([s02_node("a")])
            current = _read_publication_record(paths)
            assert current is not None
            await real_phase(paths, current, "failed", result={"fixture_turnover": True})
            await run_full_refresh(
                paths=paths, fetch=s02_fetch(payload), refresh_kind="system",
                settle_seconds=0, snapshot_dir=_snapshot_dir(tmp_path), sleep=RecordedSleep(),
            )
        return await real_phase(paths_arg, record, status, **changes)

    checkpoint_module._write_claim_status = _phase_and_publish_refresh
    try:
        result = asyncio.run(
            run_checkpoint_rotation(
                paths,
                snapshot_dir=_snapshot_dir(tmp_path),
                config=s04_checkpoint_config(record_threshold=2),
            )
        )
    finally:
        checkpoint_module._write_claim_status = real_phase
    # The raced checkpoint defers (its exact-token guard rejects the replaced
    # claim); the refresh's epoch is authoritative; nothing is hard dirt.
    assert result.status == "deferred_publisher_busy", result.warnings
    manifest = read_manifest(paths, required=True)
    assert manifest.epoch_number == 1
    assert manifest.dirty_status.active is False
    claim = _read_publication_record(paths)
    assert claim is not None and claim.is_terminal
    assert foreign_stores == []


def test_candidate_write_failure_keeps_old_lineage(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    _based_manifest(paths, tmp_path)
    s04_append_n(paths, 3)

    real_write = checkpoint_module.write_snapshot_candidate

    def _exploding_write(*args, **kwargs):
        raise OSError("disk full (injected)")

    checkpoint_module.write_snapshot_candidate = _exploding_write
    before = read_manifest(paths, required=True)
    try:
        result = asyncio.run(
            run_checkpoint_rotation(
                paths,
                snapshot_dir=_snapshot_dir(tmp_path),
                config=s04_checkpoint_config(record_threshold=2),
            )
        )
    finally:
        checkpoint_module.write_snapshot_candidate = real_write
    assert result.status == "failed"
    after = read_manifest(paths, required=True)
    assert after.epoch_id == before.epoch_id
    assert after.base_incorporated_through_seq == before.base_incorporated_through_seq
    assert after.journal_head_seq == before.journal_head_seq
    # The failed checkpoint is NOT hard dirt.
    assert after.dirty_status.active is False


def test_incorporated_files_retire_only_after_publish(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    _based_manifest(paths, tmp_path)
    s04_append_n(paths, 3)
    assert _journal_sequences(paths) == [1, 2, 3]
    result = asyncio.run(
        run_checkpoint_rotation(
            paths,
            snapshot_dir=_snapshot_dir(tmp_path),
            config=s04_checkpoint_config(record_threshold=2),
        )
    )
    assert result.status == "published", result.warnings
    assert result.retired_record_count == 3
    # Below-floor records are physically retired; nothing active remains.
    assert _journal_sequences(paths) == []


def test_active_tail_survives_rotation(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    _based_manifest(paths, tmp_path)
    s04_append_n(paths, 3)

    real_publish = checkpoint_module.publish_epoch

    async def _publish_with_tail(*args, **kwargs):
        # One more record lands after the final catch-up revalidation, just
        # before publication — it must survive as the active tail.
        s04_append(paths, transaction_id="tx-tail")
        return await real_publish(*args, **kwargs)

    checkpoint_module.publish_epoch = _publish_with_tail
    try:
        result = asyncio.run(
            run_checkpoint_rotation(
                paths,
                snapshot_dir=_snapshot_dir(tmp_path),
                config=s04_checkpoint_config(record_threshold=2),
            )
        )
    finally:
        checkpoint_module.publish_epoch = real_publish
    assert result.status == "published", result.warnings
    assert result.incorporated_through_seq == 3
    assert result.surviving_tail_count == 1
    manifest = read_manifest(paths, required=True)
    assert manifest.base_incorporated_through_seq == 3
    assert manifest.journal_floor_seq == 4
    assert manifest.journal_head_seq == 4
    # The surviving tail record was NOT retired.
    assert _journal_sequences(paths) == [4]


def test_retirement_cleanup_failure_warns_but_epoch_is_coherent(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    _based_manifest(paths, tmp_path)
    s04_append_n(paths, 3)

    from workflowy_mcp.carto_job_governance import (
        _default_mkdir,
        _default_replace,
        _default_tmp_name,
        _default_write_fsync,
    )

    def _failing_unlink(path):
        raise PermissionError("injected sharing violation")

    fs_ops = DurableFileOps(
        write_fsync=_default_write_fsync,
        replace=_default_replace,
        mkdir=_default_mkdir,
        unlink=_failing_unlink,
        tmp_name=_default_tmp_name,
    )
    result = asyncio.run(
        run_checkpoint_rotation(
            paths,
            snapshot_dir=_snapshot_dir(tmp_path),
            config=s04_checkpoint_config(record_threshold=2),
            fs_ops=fs_ops,
        )
    )
    assert result.status == "published", result.warnings
    assert any("retire" in warning.lower() for warning in result.warnings)
    manifest = read_manifest(paths, required=True)
    assert manifest.epoch_number == 1  # the new epoch is wholly authoritative
    # Residue remains for the next maintenance pass.
    assert _journal_sequences(paths) == [1, 2, 3]

    # The next maintenance pass (even a not-due rotation) clears the residue.
    second = asyncio.run(
        run_checkpoint_rotation(
            paths,
            snapshot_dir=_snapshot_dir(tmp_path),
            config=s04_checkpoint_config(record_threshold=500),
        )
    )
    assert second.status == "not_due"
    assert _journal_sequences(paths) == []


def test_bootstrap_manifest_without_base_fails_cleanly(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    s04_append_n(paths, 3)  # journal records on a bootstrap manifest (no base)
    result = asyncio.run(
        run_checkpoint_rotation(
            paths,
            snapshot_dir=_snapshot_dir(tmp_path),
            config=s04_checkpoint_config(record_threshold=2),
        )
    )
    assert result.status == "failed"
    assert any("non-bootstrap base lineage" in warning for warning in result.warnings)


def test_due_state_revalidated_after_claim_acquisition_no_empty_checkpoint(tmp_path):
    """S04 Remediation Chain 1 (work-order item 3): requester A observes due
    state and is deterministically paused; requester B publishes and
    terminalizes; A resumes, wins its own claim, REVALIDATES the current
    tail, and terminalizes not_due instead of serializing/publishing an
    EMPTY checkpoint.  Exactly one epoch publication; the final
    epoch_number advances once.  Checkpoint/checkpoint dedupe and
    defer-not-queue are untouched.

    PRE-FIX: A has no revalidation — it proceeds to materialize over the
    incorporated tail and publishes a second (empty) epoch.
    """
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    _based_manifest(paths, tmp_path)
    s04_append_n(paths, 3)
    config = s04_checkpoint_config(record_threshold=2)

    observed = asyncio.Event()
    release = asyncio.Event()
    real_acquire = checkpoint_module._acquire_checkpoint_claim
    acquire_calls = {"n": 0}
    candidate_writes: list[int] = []
    real_write = checkpoint_module.write_snapshot_candidate

    async def _pausing_acquire(*args, **kwargs):
        acquire_calls["n"] += 1
        if acquire_calls["n"] == 1:
            # A observed due state; pause it BEFORE claim acquisition.
            observed.set()
            await release.wait()
        return await real_acquire(*args, **kwargs)

    def _spy_write(*args, **kwargs):
        candidate_writes.append(1)
        return real_write(*args, **kwargs)

    async def _scenario():
        checkpoint_module._acquire_checkpoint_claim = _pausing_acquire
        checkpoint_module.write_snapshot_candidate = _spy_write
        try:
            task_a = asyncio.create_task(
                run_checkpoint_rotation(
                    paths, snapshot_dir=_snapshot_dir(tmp_path), config=config
                )
            )
            await observed.wait()
            # B runs a full rotation (publishes + terminalizes) while A is
            # paused after observing due state.
            result_b = await run_checkpoint_rotation(
                paths, snapshot_dir=_snapshot_dir(tmp_path), config=config
            )
            b_writes = len(candidate_writes)
            release.set()
            result_a = await task_a
            return result_a, result_b, b_writes
        finally:
            checkpoint_module._acquire_checkpoint_claim = real_acquire
            checkpoint_module.write_snapshot_candidate = real_write

    result_a, result_b, b_writes = asyncio.run(_scenario())
    assert result_b.status == "published", result_b.warnings
    assert result_a.status == "not_due", result_a.warnings
    # Exactly one epoch publication (B's); A never serialized a candidate.
    assert b_writes == 1
    assert len(candidate_writes) == 1
    manifest = read_manifest(paths, required=True)
    assert manifest.epoch_number == 1
    # A's claim terminalized explicitly as a non-publication disposition.
    claim = _read_publication_record(paths)
    assert claim is not None and claim.is_terminal and claim.status == "completed"
    assert (claim.result or {}).get("not_due") is True
