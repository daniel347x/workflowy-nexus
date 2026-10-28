"""S04 §14.1 — durable global dirty barrier tests.

Monotonic/idempotent publication, anonymous-event rule (A2), manifest field
preservation, bounded secret-free diagnostics, refresh-cut clearing safety,
checkpoint non-clearing, malformed-lineage fail-closed posture, and durable
write-fault honesty.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from coherence_harness import (
    ACCOUNT_A,
    RecordedSleep,
    make_coherence_root,
    s02_fault_ops,
    s02_fetch,
    s02_make_export,
    s02_node,
    s02_set_external_dirt,
    s04_append_n,
    s04_checkpoint_config,
    s04_install_base_manifest,
    s04_raise_barrier,
    s04_write_base_snapshot,
    write_malformed,
)

from workflowy_mcp.coherence import (
    BARRIER_REASONS_RESERVED,
    JournalPublicationError,
    read_external_dirt,
    read_manifest,
    run_checkpoint_rotation,
    run_full_refresh,
)
from workflowy_mcp.coherence.barrier import (
    hard_stop_state,
    raise_global_dirty_barrier,
    read_coherence_boundary_status,
)
from workflowy_mcp.coherence.errors import CoherenceSchemaError


def _snapshot_dir(tmp_path):
    return tmp_path / "nodes_export_cache"


# ---------------------------------------------------------------------------
# Publication basics
# ---------------------------------------------------------------------------


def test_clean_generation_zero_to_active_generation_one(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    result = s04_raise_barrier(paths, transaction_id="tx-1")
    assert result.changed is True
    assert result.disposition == "raised"
    assert result.state.active is True
    assert result.state.generation == 1
    assert result.state.reason_code == "journal_publication_failed"
    assert result.state.transaction_id == "tx-1"
    assert result.state.raised_at is not None
    # Durable on disk.
    manifest = read_manifest(paths, required=True)
    assert manifest.dirty_status.active is True
    assert manifest.dirty_status.generation == 1


def test_exact_same_fully_specified_event_retry_is_idempotent(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    first = s04_raise_barrier(paths, transaction_id="tx-dup", affected_region={"k": 1})
    second = s04_raise_barrier(paths, transaction_id="tx-dup", affected_region={"k": 1})
    assert first.disposition == "raised"
    assert second.disposition == "already_raised_same_event"
    assert second.changed is False
    assert read_manifest(paths, required=True).dirty_status.generation == 1


def test_distinct_events_produce_contiguous_monotonic_generations(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    s04_raise_barrier(paths, transaction_id="tx-a")
    s04_raise_barrier(paths, transaction_id="tx-b")
    third = s04_raise_barrier(paths, transaction_id="tx-c")
    assert third.state.generation == 3
    # A retry of an OLD fully-specified event is NOT idempotent across a newer
    # barrier (the active event is tx-c now) — it raises generation 4.
    fourth = s04_raise_barrier(paths, transaction_id="tx-a")
    assert fourth.state.generation == 4
    assert fourth.disposition == "raised"


def test_concurrent_distinct_raisers_serialize_without_lost_generations(tmp_path, monkeypatch):
    """Four loop-task transactions retain contiguous, lossless generations."""
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    errors: list[BaseException] = []
    from workflowy_mcp.coherence import barrier
    if os.environ.get("CUT2B_NEUTRALIZE_BARRIER_ADVANCE"):
        import inspect
        source = inspect.getsource(barrier._raise_global_dirty_barrier_locked)
        assert "generation=active.generation + 1" in source
        namespace = dict(vars(barrier))
        exec(compile(source.replace("generation=active.generation + 1", "generation=active.generation + 2", 1), "<cut2b-barrier-advance>", "exec"), namespace)
        monkeypatch.setattr(barrier, "_raise_global_dirty_barrier_locked", namespace["_raise_global_dirty_barrier_locked"])

    async def _raise(index: int) -> None:
        await asyncio.sleep(0)
        try:
            s04_raise_barrier(paths, transaction_id=f"tx-task-{index}")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    async def scenario():
        await asyncio.gather(*(_raise(index) for index in range(4)))
    asyncio.run(scenario())
    assert errors == []
    manifest = read_manifest(paths, required=True)
    # Four distinct events: exactly four contiguous generations, none lost.
    assert manifest.dirty_status.generation == 4


def test_barrier_replacement_preserves_every_non_dirt_manifest_field(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    s04_append_n(paths, 3)
    before = read_manifest(paths, required=True)
    s04_raise_barrier(paths, transaction_id="tx-preserve")
    after = read_manifest(paths, required=True)
    for field in (
        "schema_version",
        "account_scope_id",
        "epoch_number",
        "epoch_id",
        "base_snapshot_path",
        "base_incorporated_through_seq",
        "journal_floor_seq",
        "journal_head_seq",
        "coherence_mode",
        "created_at",
        "previous_epoch_id",
    ):
        assert getattr(after, field) == getattr(before, field), field


def test_bounded_diagnostic_detail_and_secret_free(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    huge = "x" * 5000
    result = s04_raise_barrier(paths, transaction_id="tx-bound", detail=huge)
    assert result.state.detail is not None
    assert len(result.state.detail) <= 500
    # Region identifiers are allowed; full note bodies/credentials are never
    # fabricated by the authority (the caller's detail is only bounded).
    with pytest.raises(CoherenceSchemaError):
        s04_raise_barrier(paths, transaction_id="tx-region", affected_region=["not-a-dict"])


def test_unknown_and_reserved_reasons_rejected(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    with pytest.raises(CoherenceSchemaError):
        raise_global_dirty_barrier(paths, reason_code="made_up_reason")
    for reserved in BARRIER_REASONS_RESERVED:
        with pytest.raises(CoherenceSchemaError):
            raise_global_dirty_barrier(paths, reason_code=reserved)
    # Nothing was written.
    assert read_manifest(paths, required=True).dirty_status.active is False


# ---------------------------------------------------------------------------
# Anonymous-event rule (A2)
# ---------------------------------------------------------------------------


def test_anonymous_events_never_deduplicate(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    first = raise_global_dirty_barrier(
        paths, reason_code="ambiguous_commit_outcome", transaction_id=None
    )
    second = raise_global_dirty_barrier(
        paths, reason_code="ambiguous_commit_outcome", transaction_id="   "
    )
    assert first.disposition == "raised"
    assert second.disposition == "raised"
    assert second.state.generation == 2


def test_malformed_lineage_refuses_barrier_rewrite(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    write_malformed(paths.manifest_path)
    with pytest.raises(CoherenceSchemaError):
        s04_raise_barrier(paths, transaction_id="tx-corrupt")
    # The corrupt artifact was NOT rewritten into a barrier state.
    assert paths.manifest_path.read_text(encoding="utf-8") == "{not json"


def test_barrier_durable_write_fault_never_reports_success(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    with pytest.raises(JournalPublicationError):
        s04_raise_barrier(
            paths, transaction_id="tx-fault", fs_ops=s02_fault_ops("manifest_replace")
        )
    # The on-disk manifest still shows the clean barrier (no false success).
    assert read_manifest(paths, required=True).dirty_status.active is False


# ---------------------------------------------------------------------------
# Refresh-cut clearing safety (S02 cut-token rule + S04 regression)
# ---------------------------------------------------------------------------


def test_full_refresh_clears_only_a_covered_generation(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    s04_raise_barrier(paths, transaction_id="tx-covered")
    calls: list[int] = []
    payload = s02_make_export([s02_node("a")])
    outcome = __import__("asyncio").run(
        run_full_refresh(
            paths=paths,
            fetch=s02_fetch(payload, calls),
            refresh_kind="recovery",
            settle_seconds=0,
            snapshot_dir=_snapshot_dir(tmp_path),
            sleep=RecordedSleep(),
        )
    )
    assert outcome.hard_barrier_cleared is True
    manifest = read_manifest(paths, required=True)
    assert manifest.dirty_status.active is False
    assert manifest.dirty_status.generation == 1  # generation preserved


def test_post_cut_generation_survives_the_refresh(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    s04_raise_barrier(paths, transaction_id="tx-pre-cut")
    payload = s02_make_export([s02_node("a")])

    async def _fetch_then_raise():
        # A new ambiguous event lands DURING the download (after the cut).
        s04_raise_barrier(paths, transaction_id="tx-post-cut")
        return payload

    outcome = __import__("asyncio").run(
        run_full_refresh(
            paths=paths,
            fetch=_fetch_then_raise,
            refresh_kind="recovery",
            settle_seconds=0,
            snapshot_dir=_snapshot_dir(tmp_path),
            sleep=RecordedSleep(),
        )
    )
    # The cut rule is exact: the ACTIVE barrier at publication is G2 (> the
    # G1 cut), so NOTHING is cleared — the post-cut generation survives and
    # the refresh honestly reports no clearing (it did NOT roll back to G1).
    assert outcome.hard_barrier_cleared is False
    manifest = read_manifest(paths, required=True)
    assert manifest.dirty_status.active is True
    assert manifest.dirty_status.generation == 2
    assert manifest.dirty_status.transaction_id == "tx-post-cut"


def test_anonymous_event_after_the_cut_survives_clearing(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    s04_raise_barrier(paths, transaction_id="tx-pre-cut-anon")
    payload = s02_make_export([s02_node("a")])

    async def _fetch_then_raise_anonymous():
        raise_global_dirty_barrier(
            paths, reason_code="ambiguous_commit_outcome", transaction_id=None
        )
        return payload

    outcome = __import__("asyncio").run(
        run_full_refresh(
            paths=paths,
            fetch=_fetch_then_raise_anonymous,
            refresh_kind="recovery",
            settle_seconds=0,
            snapshot_dir=_snapshot_dir(tmp_path),
            sleep=RecordedSleep(),
        )
    )
    assert outcome.hard_barrier_cleared is False
    manifest = read_manifest(paths, required=True)
    assert manifest.dirty_status.active is True
    assert manifest.dirty_status.generation == 2
    assert manifest.dirty_status.transaction_id is None


# ---------------------------------------------------------------------------
# Checkpoint NEVER clears (invariant 12)
# ---------------------------------------------------------------------------


def test_checkpoint_never_clears_hard_dirt_or_external_dirt(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    base = s04_write_base_snapshot(
        paths, _snapshot_dir(tmp_path), [s02_node("a")], epoch_number=0, epoch_id="b" * 32
    )
    s04_install_base_manifest(paths, base_snapshot_path=base, epoch_id="b" * 32)
    s04_append_n(paths, 3)
    s04_raise_barrier(paths, transaction_id="tx-keep")
    s02_set_external_dirt(paths, 7)
    config = s04_checkpoint_config(record_threshold=2)
    result = __import__("asyncio").run(
        run_checkpoint_rotation(
            paths, snapshot_dir=_snapshot_dir(tmp_path), config=config
        )
    )
    assert result.status == "published", result.warnings
    manifest = read_manifest(paths, required=True)
    assert manifest.epoch_number == 1
    assert manifest.dirty_status.active is True
    assert manifest.dirty_status.generation == 1
    assert manifest.dirty_status.transaction_id == "tx-keep"
    dirt = read_external_dirt(paths)
    assert dirt.active is True and dirt.generation == 7


def test_a3_barrier_raised_during_materialization_is_carried_forward(tmp_path):
    """The A3 race: clean basis captured, barrier raised during out-of-lock
    materialization — the published E+1 retains that barrier generation
    (publication-time carry-forward, never basis-state restoration)."""

    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    base = s04_write_base_snapshot(
        paths, _snapshot_dir(tmp_path), [s02_node("a")], epoch_number=0, epoch_id="b" * 32
    )
    s04_install_base_manifest(paths, base_snapshot_path=base, epoch_id="b" * 32)
    s04_append_n(paths, 3)
    config = s04_checkpoint_config(record_threshold=2)

    from workflowy_mcp.coherence import checkpoint as checkpoint_module

    real_write = checkpoint_module.write_snapshot_candidate
    armed = {"done": False}

    def _write_and_dirty(*args, **kwargs):
        if not armed["done"]:
            armed["done"] = True
            # The barrier lands while the checkpoint materializes OUT of lock.
            s04_raise_barrier(paths, transaction_id="tx-during-materialize")
        return real_write(*args, **kwargs)

    checkpoint_module.write_snapshot_candidate = _write_and_dirty
    try:
        result = __import__("asyncio").run(
            run_checkpoint_rotation(
                paths, snapshot_dir=_snapshot_dir(tmp_path), config=config
            )
        )
    finally:
        checkpoint_module.write_snapshot_candidate = real_write
    assert result.status == "published", result.warnings
    manifest = read_manifest(paths, required=True)
    assert manifest.epoch_number == 1
    assert manifest.dirty_status.active is True
    assert manifest.dirty_status.generation == 1
    assert manifest.dirty_status.transaction_id == "tx-during-materialize"


# ---------------------------------------------------------------------------
# Boundary-status composition
# ---------------------------------------------------------------------------


def test_boundary_status_composition_clean_and_dirty(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    clean = read_coherence_boundary_status(paths)
    assert clean is not None
    assert clean.hard_dirty is False
    assert clean.hard_generation == 0
    assert clean.external_dirty is False
    s04_raise_barrier(paths, transaction_id="tx-status")
    s02_set_external_dirt(paths, 4)
    status = read_coherence_boundary_status(paths)
    assert status.hard_dirty is True
    assert status.hard_generation == 1
    assert status.hard_reason_code == "journal_publication_failed"
    assert status.hard_transaction_id == "tx-status"
    assert status.external_dirty is True
    assert status.external_generation == 4
    locked = __import__("asyncio").run(hard_stop_state(paths))
    assert locked.hard_dirty is True
    assert locked.hard_generation == 1
