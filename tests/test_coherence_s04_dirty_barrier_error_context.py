"""S04 Remediation Chain 4 (work-order item 4) — DirtyBarrierPublicationError
preserves the required lineage coordinates (locked S04 §5.3).

When journal/verified-record publication fails AND the barrier publication
also fails, the error now carries, where known: the account scope, the
epoch, the ACTUAL pre-attempt/current manifest journal head, and the
ATTEMPTED journal sequence as a DISTINCT value.  Pre-fix the
``journal_head_seq`` input was sometimes None (the validation/construction
path passed ``validation_error.sequence``, which is unset) and otherwise
the ATTEMPTED sequence mislabeled as the manifest head (the recovery path
passed ``publication_error.sequence``); account and epoch were never
carried.  Context is threaded from the append/construction region (in-lock
stamps) and the recovery phase's already-read manifest — never a second
speculative manifest rewrite, and never a new durable artifact.

Required tests:
(a) verified-effect validation failure + barrier-write failure exposes
    account, epoch, and actual head 0;
(b) physical journal publication failure + barrier-write failure exposes
    the actual head and the distinct attempted sequence;
(c) both underlying failures and the exception chain remain intact;
(d) existing invariant-6 tests remain green (existing files, unchanged);
(e) mutation-sensitivity against 6f5a8e2 — verified via the stash protocol
    (production reverted to the baseline): (a) FAILED with account/epoch/
    head all None, and (b) FAILED with journal_head_seq == 1 (the
    attempted sequence mislabeled as the head) and no
    attempted_journal_sequence attribute.  See the Chain-4 result anchor.
"""

from __future__ import annotations

import pytest
from coherence_harness import (
    ACCOUNT_A,
    InjectedFault,
    make_coherence_root,
    s02_fault_ops,
    s04_append,
    s04_producer,
)

from workflowy_mcp.coherence import (
    DirtyBarrierPublicationError,
    DurableFileOps,
    EffectValidationError,
    JournalPublicationError,
    SemanticEffect,
    journal_record_filename,
    read_manifest,
    seal_verified_journal_transaction,
)


class _ManifestReplaceFails:
    """Fail the FIRST N manifest replace operations, then let them through.

    Inside one seal the manifest-replace sequence is: (1) append head
    advance, (2) recovery orphan adoption, (3) barrier write — so N selects
    exactly which stage(s) fail.  (Mirror of the journal-sealing fixture.)
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


def test_validation_failure_barrier_failure_exposes_account_epoch_actual_head(tmp_path):
    """Required (a): a verified-effect validation failure whose barrier write
    also fails exposes the account scope, the epoch, the ACTUAL manifest
    journal head (0 — nothing ever committed), and the attempted sequence
    as a distinct value.  Both failures and the chain remain intact (c)."""
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    with pytest.raises(DirtyBarrierPublicationError) as excinfo:
        seal_verified_journal_transaction(
            paths,
            operation_kind="test",
            effects=[
                SemanticEffect.node_fields("n1", ["name"], {"name": "alpha"}),
                SemanticEffect.node_fields("n1", ["name"], {"name": "beta"}),
            ],
            producer=s04_producer(),
            transaction_id="tx-conflict",
            # The append performs no write for a validation-failing
            # transaction, so the first manifest write in the whole seal is
            # the barrier's — failed here deterministically.
            fs_ops=s02_fault_ops("manifest_temp_write"),
        )
    error = excinfo.value
    manifest = read_manifest(paths, required=True)
    # The required lineage coordinates (locked §5.3).
    assert error.account_scope_id == ACCOUNT_A
    assert error.epoch_id == manifest.epoch_id
    assert error.journal_head_seq == 0  # the ACTUAL manifest head
    assert error.attempted_journal_sequence == 1  # the DISTINCT attempted value
    # (c) original reason/transaction, underlying publication failure,
    # exception chaining, and recovery_required are all preserved.
    assert error.original_reason_code == "verified_effect_unpublishable"
    assert error.original_transaction_id == "tx-conflict"
    assert error.__context__ is error.underlying
    assert isinstance(error.underlying.__context__, EffectValidationError)
    assert "manifest_temp_write" in str(error.underlying)
    assert error.recovery_required is True
    # The barrier never landed: no speculative dirty state was written.
    assert manifest.dirty_status.active is False
    assert manifest.dirty_status.generation == 0


def test_publication_failure_barrier_failure_exposes_actual_head_and_attempt(tmp_path):
    """Required (b): a physical journal publication failure (record durable,
    head advance failed; recovery adoption failed) whose barrier write also
    fails exposes the ACTUAL manifest journal head (0) AND the DISTINCT
    attempted sequence (1).  Both failures and the chain remain intact (c).
    """
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    with pytest.raises(DirtyBarrierPublicationError) as excinfo:
        s04_append(
            paths,
            transaction_id="tx-bothfail",
            fs_ops=_ManifestReplaceFails(3).ops(),
        )
    error = excinfo.value
    manifest = read_manifest(paths, required=True)
    # The ACTUAL head and the DISTINCT attempted sequence.
    assert error.journal_head_seq == 0
    assert error.attempted_journal_sequence == 1
    assert error.journal_head_seq != error.attempted_journal_sequence
    assert error.account_scope_id == ACCOUNT_A
    assert error.epoch_id == manifest.epoch_id
    # (c) original reason/transaction, underlying failures, chaining,
    # recovery_required.
    assert error.original_reason_code == "verified_effect_unpublishable"
    assert error.original_transaction_id == "tx-bothfail"
    assert error.__context__ is error.underlying
    assert isinstance(error.underlying.__context__, JournalPublicationError)
    assert error.recovery_required is True
    # No barrier state was durably written; the orphan record remains
    # durable evidence above the unadvanced head.
    assert manifest.dirty_status.active is False
    assert manifest.journal_head_seq == 0
    assert (paths.journal_dir / journal_record_filename(1)).exists()


def test_malformed_lineage_still_propagates_without_barrier_context(tmp_path):
    """Invariant-6 companion pin (d): EXISTING committed-range corruption
    still propagates its original typed integrity error with NO barrier
    guess and no speculative lineage context — the Chain-4 context
    threading never touches existing-lineage failures."""
    from workflowy_mcp.coherence import JournalIntegrityError

    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    s04_append(paths, transaction_id="tx-good")
    record_path = paths.journal_dir / journal_record_filename(1)
    record_path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(JournalIntegrityError):
        s04_append(paths, transaction_id="tx-second")
    manifest = read_manifest(paths, required=True)
    assert manifest.dirty_status.active is False
    assert manifest.dirty_status.generation == 0
