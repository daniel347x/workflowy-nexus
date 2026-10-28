"""S04 §14.5 (stats & trigger) — active-tail statistics and checkpoint due state."""

from __future__ import annotations

import os

import pytest
from coherence_harness import (
    ACCOUNT_A,
    make_coherence_root,
    s02_node,
    s04_append_n,
    s04_checkpoint_config,
    s04_install_base_manifest,
    s04_write_base_snapshot,
)

from workflowy_mcp.coherence import (
    JournalIntegrityError,
    checkpoint_is_due,
    inspect_active_journal_tail,
    journal_record_filename,
    read_manifest,
    run_checkpoint_rotation,
)
from workflowy_mcp.coherence.checkpoint import _light_tail_stats


def _snapshot_dir(tmp_path):
    return tmp_path / "nodes_export_cache"


def _based_manifest(paths, tmp_path):
    base = s04_write_base_snapshot(
        paths, _snapshot_dir(tmp_path), [s02_node("a")], epoch_number=0, epoch_id="b" * 32
    )
    s04_install_base_manifest(paths, base_snapshot_path=base, epoch_id="b" * 32)


def test_due_by_count_only(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    _based_manifest(paths, tmp_path)
    s04_append_n(paths, 3)
    manifest = read_manifest(paths, required=True)
    config = s04_checkpoint_config(record_threshold=3, byte_threshold=10**12)
    stats = inspect_active_journal_tail(paths, manifest)
    assert stats.record_count == 3
    assert checkpoint_is_due(stats, config) is True


def test_due_by_bytes_only(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    _based_manifest(paths, tmp_path)
    s04_append_n(paths, 2)
    manifest = read_manifest(paths, required=True)
    stats = inspect_active_journal_tail(paths, manifest)
    assert stats.total_bytes > 0
    config = s04_checkpoint_config(record_threshold=10**6, byte_threshold=1)
    assert checkpoint_is_due(stats, config) is True


def test_not_due_below_both(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    _based_manifest(paths, tmp_path)
    s04_append_n(paths, 2)
    manifest = read_manifest(paths, required=True)
    stats = inspect_active_journal_tail(paths, manifest)
    config = s04_checkpoint_config(record_threshold=500, byte_threshold=67_108_864)
    assert checkpoint_is_due(stats, config) is False


def test_empty_tail_stats(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    manifest = read_manifest(paths, required=True)
    stats = inspect_active_journal_tail(paths, manifest)
    assert stats.record_count == 0
    assert stats.total_bytes == 0
    assert stats.largest_record_sequence is None
    assert checkpoint_is_due(stats, s04_checkpoint_config()) is False


def test_oversized_record_warns_without_rejection(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    _based_manifest(paths, tmp_path)
    s04_append_n(paths, 3)
    manifest = read_manifest(paths, required=True)
    stats = inspect_active_journal_tail(paths, manifest)
    assert stats.largest_record_bytes > 0
    assert stats.largest_record_sequence is not None
    # The oversized-but-valid record is never rejected; the rotation emits
    # ONE bounded diagnostic naming sequence and bytes.
    config = s04_checkpoint_config(record_threshold=2, warning_bytes=1)
    result = __import__("asyncio").run(
        run_checkpoint_rotation(paths, snapshot_dir=_snapshot_dir(tmp_path), config=config)
    )
    assert result.status == "published", result.warnings
    assert any(
        f"seq {stats.largest_record_sequence}" in warning
        and str(stats.largest_record_bytes) in warning
        for warning in result.warnings
    )


def test_incorporated_residue_excluded_from_stats(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    _based_manifest(paths, tmp_path)
    s04_append_n(paths, 3)
    config = s04_checkpoint_config(record_threshold=2)
    result = __import__("asyncio").run(
        run_checkpoint_rotation(paths, snapshot_dir=_snapshot_dir(tmp_path), config=config)
    )
    assert result.status == "published", result.warnings
    # Two new records after the cut: the active tail counts exactly those;
    # incorporated residue (if any physical files linger) is not replay debt.
    s04_append_n(paths, 2)
    manifest = read_manifest(paths, required=True)
    stats = inspect_active_journal_tail(paths, manifest)
    assert stats.record_count == 2
    assert stats.floor_seq == 4
    assert stats.head_seq == 5


def test_strict_validation_fails_closed_on_missing_record(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    _based_manifest(paths, tmp_path)
    s04_append_n(paths, 2)
    os.remove(paths.journal_dir / journal_record_filename(2))
    manifest = read_manifest(paths, required=True)
    with pytest.raises(JournalIntegrityError):
        inspect_active_journal_tail(paths, manifest)


def test_light_and_strict_stats_agree(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    _based_manifest(paths, tmp_path)
    s04_append_n(paths, 4)
    manifest = read_manifest(paths, required=True)
    strict = inspect_active_journal_tail(paths, manifest)
    light = _light_tail_stats(paths, manifest)
    assert strict.record_count == light.record_count
    assert strict.total_bytes == light.total_bytes
    assert strict.largest_record_bytes == light.largest_record_bytes
    assert strict.largest_record_sequence == light.largest_record_sequence


def test_one_flight_scheduler_spawns_dedupes_and_reports_not_due(tmp_path):
    """§9.3: the process-local one-flight scheduler spawns ONE rotation per
    account, dedupes concurrent requests onto it, and returns None when not
    due — the mutating caller never waits for compaction."""

    import asyncio

    from workflowy_mcp.coherence import request_checkpoint_if_due
    from workflowy_mcp.coherence.checkpoint import _CHECKPOINT_TASKS

    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    _based_manifest(paths, tmp_path)
    s04_append_n(paths, 3)
    config = s04_checkpoint_config(record_threshold=2)

    async def _scenario():
        # Not due yet (3 < 4).
        assert request_checkpoint_if_due(
            paths, snapshot_dir=_snapshot_dir(tmp_path), config=s04_checkpoint_config(record_threshold=4)
        ) is None
        # Due now: spawn once, dedupe the second request onto the same task.
        first = request_checkpoint_if_due(
            paths, snapshot_dir=_snapshot_dir(tmp_path), config=config
        )
        assert first is not None
        second = request_checkpoint_if_due(
            paths, snapshot_dir=_snapshot_dir(tmp_path), config=config
        )
        assert second is first
        result = await first
        assert result.status == "published", result.warnings
        return result

    asyncio.run(_scenario())
    manifest = read_manifest(paths, required=True)
    assert manifest.epoch_number == 1
    assert manifest.base_incorporated_through_seq == 3
    assert _CHECKPOINT_TASKS  # registry bookkeeping is account-keyed
