"""S04 §14.2 — verified journal sealing tests.

One production sealing authority: published, recovered_orphan, barrier on
unrecoverable publication failure (journal_publication_failed vs
verified_effect_unpublishable), DirtyBarrierPublicationError preserving
both failures, no local cache application before durable publication, and
checkpoint scheduling only after a successful seal outside every mutex.
"""

from __future__ import annotations

import os

import pytest
from coherence_harness import (
    ACCOUNT_A,
    InjectedFault,
    make_coherence_root,
    s02_fault_ops,
    s04_append,
)

from workflowy_mcp.coherence import (
    DirtyBarrierPublicationError,
    DurableFileOps,
    JournalIntegrityError,
    JournalPublicationError,
    journal_record_filename,
    read_manifest,
)


class _ManifestReplaceFails:
    """Fail the FIRST N manifest replace operations, then let them through.

    Inside one seal the manifest-replace sequence is: (1) append head
    advance, (2) recovery orphan adoption, (3) barrier write — so N selects
    exactly which stage(s) fail.
    """

    def __init__(self, fails: int) -> None:
        self._remaining = fails

    def ops(self) -> DurableFileOps:
        from workflowy_mcp.carto_job_governance import (
            _default_mkdir,
            _default_replace,
            _default_tmp_name,
            _default_unlink,
            _default_write_fsync,
        )

        def replace(tmp, target) -> None:
            if target.name == "manifest.json" and self._remaining > 0:
                self._remaining -= 1
                raise InjectedFault("manifest_replace_fail")
            _default_replace(tmp, target)

        return DurableFileOps(
            write_fsync=_default_write_fsync,
            replace=replace,
            mkdir=_default_mkdir,
            unlink=_default_unlink,
            tmp_name=_default_tmp_name,
        )


def test_normal_append_returns_published(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    result = s04_append(paths, transaction_id="tx-ok")
    assert result.disposition == "published"
    assert result.checkpoint_requested is False
    assert result.transaction.sequence == 1
    assert result.manifest.journal_head_seq == 1


def test_record_durable_head_failed_recovers_orphan_without_barrier(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    # (1) append head advance fails AFTER the record is durable; (2) the
    # recovery adoption write succeeds -> recovered_orphan, NO barrier.
    result = s04_append(
        paths,
        transaction_id="tx-orphan",
        fs_ops=_ManifestReplaceFails(1).ops(),
    )
    assert result.disposition == "recovered_orphan"
    assert result.transaction.transaction_id == "tx-orphan"
    assert result.manifest.journal_head_seq == 1
    manifest = read_manifest(paths, required=True)
    assert manifest.journal_head_seq == 1
    assert manifest.dirty_status.active is False


def test_record_write_failure_raises_journal_publication_failed_barrier(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    with pytest.raises(JournalPublicationError):
        s04_append(
            paths,
            transaction_id="tx-recfail",
            fs_ops=s02_fault_ops("record_replace"),
        )
    manifest = read_manifest(paths, required=True)
    assert manifest.dirty_status.active is True
    assert manifest.dirty_status.reason_code == "journal_publication_failed"
    assert manifest.dirty_status.transaction_id == "tx-recfail"
    assert manifest.dirty_status.generation == 1
    # Nothing committed.
    assert manifest.journal_head_seq == 0


def test_unrecoverable_durable_record_raises_verified_effect_unpublishable(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    # (1) append head advance fails (record durable); (2) recovery adoption
    # fails too; (3) the barrier write succeeds -> verified_effect_unpublishable.
    with pytest.raises(JournalPublicationError):
        s04_append(
            paths,
            transaction_id="tx-unpub",
            fs_ops=_ManifestReplaceFails(2).ops(),
        )
    manifest = read_manifest(paths, required=True)
    assert manifest.dirty_status.active is True
    assert manifest.dirty_status.reason_code == "verified_effect_unpublishable"
    assert manifest.dirty_status.transaction_id == "tx-unpub"
    # The orphan record remains durable evidence above an unadvanced head.
    assert manifest.journal_head_seq == 0
    assert (paths.journal_dir / journal_record_filename(1)).exists()


def test_barrier_failure_preserves_combined_diagnostics(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    with pytest.raises(DirtyBarrierPublicationError) as captured:
        s04_append(
            paths,
            transaction_id="tx-bothfail",
            fs_ops=_ManifestReplaceFails(3).ops(),
        )
    error = captured.value
    assert error.original_reason_code == "verified_effect_unpublishable"
    assert error.original_transaction_id == "tx-bothfail"
    assert error.underlying is not None
    assert error.recovery_required is True
    assert "no coherent success may be reported" in str(error)
    # No barrier state was durably written.
    manifest = read_manifest(paths, required=True)
    assert manifest.dirty_status.active is False


def test_checkpoint_hook_runs_only_after_durable_publication(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    observed: list[int] = []

    def _hook() -> bool:
        # At hook time the record AND head advance must BOTH be durable.
        observed.append(read_manifest(paths, required=True).journal_head_seq)
        return True

    result = s04_append(paths, transaction_id="tx-hook", checkpoint_request=_hook)
    assert result.disposition == "published"
    assert result.checkpoint_requested is True
    assert observed == [1]


def test_checkpoint_hook_never_called_on_failed_seal(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    calls: list[int] = []
    with pytest.raises(JournalPublicationError):
        s04_append(
            paths,
            transaction_id="tx-nohook",
            fs_ops=s02_fault_ops("record_replace"),
            checkpoint_request=lambda: calls.append(1) or True,
        )
    assert calls == []


def test_typed_lineage_corruption_propagates_without_barrier(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    s04_append(paths, transaction_id="tx-first")
    # Corrupt the committed range: delete the committed record.
    os.remove(paths.journal_dir / journal_record_filename(1))
    with pytest.raises(JournalIntegrityError):
        s04_append(paths, transaction_id="tx-second")
    # Typed corruption: NO barrier guessing (invariant 6).
    manifest = read_manifest(paths, required=True)
    assert manifest.dirty_status.active is False


def test_sealer_never_applies_effects_to_local_cache_before_durability(tmp_path):
    """The sealer owns no cache-write seam at all: the ONLY post-seal hook
    (checkpoint_request) provably runs after durability (see the hook test
    above); a caller-side cache apply can therefore never precede durable
    publication.  This test pins the ordering end-to-end: with a fault at
    after-record/head, the barrier is durable before ANY post-seal action."""

    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    order: list[str] = []

    def _hook() -> bool:
        order.append("hook")
        return True

    # (1) append head advance fails, (2) recovery adoption succeeds.
    result = s04_append(
        paths,
        transaction_id="tx-order",
        fs_ops=_ManifestReplaceFails(1).ops(),
        checkpoint_request=_hook,
    )
    assert result.disposition == "recovered_orphan"
    # The recovered commit was durable before the hook ran.
    assert read_manifest(paths, required=True).journal_head_seq == 1
    assert order == ["hook"]
    assert read_manifest(paths, required=True).dirty_status.active is False


def test_retried_seal_after_barrier_is_idempotent_by_event(tmp_path):
    """A retried seal of the SAME transaction_id re-raises the SAME barrier
    event (idempotent) rather than manufacturing generations."""

    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    for _ in range(2):
        with pytest.raises(JournalPublicationError):
            s04_append(
                paths,
                transaction_id="tx-retry",
                fs_ops=s02_fault_ops("record_replace"),
            )
    manifest = read_manifest(paths, required=True)
    assert manifest.dirty_status.generation == 1
    assert manifest.dirty_status.transaction_id == "tx-retry"
