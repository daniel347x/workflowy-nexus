"""S04 Remediation Chain 3 (work-order item 1) — verified effects rejected
BEFORE the record write must hard-dirty.

A semantic transaction that cannot pass publication-strict effect validation
or verified-record construction is ``verified_effect_unpublishable``: the
global barrier is raised durably BEFORE the failure returns (pre-fix, the
sealer caught only JournalPublicationError, so EffectValidationError and
equivalent caller-supplied verified-record construction failures escaped
with dirty_status still clean).  DirtyBarrierPublicationError preserves both
failures when the barrier write also fails.  Invariant 6 is preserved:
malformed EXISTING manifest/journal lineage, account mismatch, gaps, and
corrupt committed records propagate as their original typed integrity
errors with no speculative barrier rewrite (only append-STAMPED failures
take the barrier path).  No effect is ever applied to the local cache.
"""

from __future__ import annotations

import pytest
from coherence_harness import (
    ACCOUNT_A,
    make_coherence_root,
    s02_fault_ops,
    s04_append,
    s04_producer,
)

import workflowy_mcp.coherence.barrier as barrier_module
from workflowy_mcp.coherence import (
    CoherenceSchemaError,
    DirtyBarrierPublicationError,
    EffectValidationError,
    JournalIntegrityError,
    SemanticEffect,
    journal_record_filename,
    read_manifest,
    seal_verified_journal_transaction,
)


def _assert_unpublishable_barrier(paths, *, transaction_id: str, generation: int = 1):
    manifest = read_manifest(paths, required=True)
    assert manifest is not None
    assert manifest.dirty_status.active is True
    assert manifest.dirty_status.generation == generation
    assert manifest.dirty_status.reason_code == "verified_effect_unpublishable"
    assert manifest.dirty_status.transaction_id == transaction_id
    # Nothing was committed and no record file exists.
    assert manifest.journal_head_seq == 0
    journal_files = (
        list(paths.journal_dir.iterdir()) if paths.journal_dir.exists() else []
    )
    assert journal_files == []
    return manifest


def test_conflicting_node_fields_values_hard_dirty_verified_effect_unpublishable(tmp_path):
    """Required test (a): conflicting node_fields values through the sealer
    raise EffectValidationError AND leave an active generation-1
    verified_effect_unpublishable barrier."""
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    with pytest.raises(EffectValidationError):
        seal_verified_journal_transaction(
            paths,
            operation_kind="test",
            effects=[
                SemanticEffect.node_fields("n1", ["name"], {"name": "alpha"}),
                SemanticEffect.node_fields("n1", ["name"], {"name": "beta"}),
            ],
            producer=s04_producer(),
            transaction_id="tx-conflict",
        )
    _assert_unpublishable_barrier(paths, transaction_id="tx-conflict")


def test_child_order_parentage_disagreement_hard_dirty_same_outcome(tmp_path):
    """Required test (b): another whole-transaction conflict (child-order /
    parentage disagreement) receives the same outcome."""
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    with pytest.raises(EffectValidationError):
        seal_verified_journal_transaction(
            paths,
            operation_kind="test",
            effects=[
                SemanticEffect.child_order("p1", ["c1"]),
                SemanticEffect.node_parentage("c1", "p2"),
            ],
            producer=s04_producer(),
            transaction_id="tx-conflict-2",
        )
    _assert_unpublishable_barrier(paths, transaction_id="tx-conflict-2")


def test_verified_record_construction_failure_hard_dirty(tmp_path):
    """A caller-supplied verified-record CONSTRUCTION failure (effects valid,
    record envelope invalid — here a malformed committed_at) receives the
    same verified_effect_unpublishable outcome."""
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    with pytest.raises(CoherenceSchemaError):
        seal_verified_journal_transaction(
            paths,
            operation_kind="test",
            effects=[SemanticEffect.tombstone(["ghost"])],
            producer=s04_producer(),
            transaction_id="tx-construction",
            committed_at="not-a-timestamp",
        )
    _assert_unpublishable_barrier(paths, transaction_id="tx-construction")


def test_barrier_write_failure_preserves_both_failures(tmp_path):
    """Required test (c): when the barrier publication ALSO fails,
    DirtyBarrierPublicationError preserves both failures."""
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
            # transaction (validation precedes the record write), so the
            # first manifest write in the whole seal is the barrier's —
            # failed here deterministically.
            fs_ops=s02_fault_ops("manifest_temp_write"),
        )
    error = excinfo.value
    # The original (validation) failure is preserved: typed fields + the
    # exception chain (the barrier failure was raised while handling the
    # validation failure).
    assert error.original_reason_code == "verified_effect_unpublishable"
    assert error.original_transaction_id == "tx-conflict"
    assert error.__context__ is error.underlying
    assert isinstance(error.underlying.__context__, EffectValidationError)
    # The barrier (publication) failure is preserved.
    assert error.underlying is not None
    assert "manifest_temp_write" in str(error.underlying)
    assert error.recovery_required is True
    # The barrier never landed: no speculative dirty state was written.
    manifest = read_manifest(paths, required=True)
    assert manifest.dirty_status.active is False
    assert manifest.dirty_status.generation == 0


def test_committed_range_corruption_propagates_integrity_error_no_barrier(tmp_path):
    """Required test (d): EXISTING committed-range corruption still
    propagates JournalIntegrityError with NO barrier guess (invariant 6)."""
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    s04_append(paths, transaction_id="tx-good")
    # Corrupt the committed record at sequence 1.
    record_path = paths.journal_dir / journal_record_filename(1)
    record_path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(JournalIntegrityError):
        seal_verified_journal_transaction(
            paths,
            operation_kind="test",
            effects=[
                SemanticEffect.node_fields("n1", ["name"], {"name": "alpha"}),
                SemanticEffect.node_fields("n1", ["name"], {"name": "beta"}),
            ],
            producer=s04_producer(),
            transaction_id="tx-conflict",
        )
    manifest = read_manifest(paths, required=True)
    assert manifest.dirty_status.active is False
    assert manifest.dirty_status.generation == 0


def test_unstamped_schema_error_propagates_without_barrier(tmp_path, monkeypatch):
    """Invariant-6 discrimination pin: a CoherenceSchemaError arising from
    EXISTING lineage (no append stamp) propagates untouched — never
    barrier-ized.  Only append-STAMPED caller-supplied failures take the
    verified_effect_unpublishable path."""

    def _unstamped(*args, **kwargs):
        raise CoherenceSchemaError("existing-lineage schema failure (unstamped)")

    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    monkeypatch.setattr(
        barrier_module, "append_journal_transaction", _unstamped
    )
    with pytest.raises(CoherenceSchemaError, match="existing-lineage"):
        s04_append(paths, transaction_id="tx-lineage")
    manifest = read_manifest(paths, required=True)
    assert manifest.dirty_status.active is False
    assert manifest.dirty_status.generation == 0
