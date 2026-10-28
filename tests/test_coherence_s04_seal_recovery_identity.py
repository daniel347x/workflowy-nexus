"""S04 Remediation Chain 2 (work-order item 1) — conclusively committed
seals must never false-dirty.

The pre-fix recovery recorded ``pre_head`` only when the recovery began and
searched transaction ids only in ``pre_head+1..post_head``.  Two committed
shapes were missed and false-dirtied:

- our own manifest replacement BECAME VISIBLE and the head-advancement call
  still reported failure (the record is committed AT the attempted
  sequence, at or below the fresh ``pre_head``);
- ANOTHER process already adopted our orphan and advanced the head beyond
  it (again committed at or below the fresh ``pre_head``), including the
  ordering that continues through checkpoint incorporation + physical
  retirement of the record before our recovery resumes.

The fix: the append stamps the ATTEMPTED SEQUENCE onto every post-record
publication failure (exception type preserved), and the sealer's recovery
identifies the exact transaction by attempted identity — committed when its
immutable record is committed at the attempted sequence OR the attempted
sequence is already incorporated at/below the base watermark — never
restricted to records adopted by the current recovery invocation.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from coherence_harness import (
    ACCOUNT_A,
    InjectedFault,
    fault_ops,
    launch_subprocess,
    make_coherence_root,
    s02_node,
    s04_append,
    s04_install_base_manifest,
    s04_producer,
    s04_write_base_snapshot,
)

from workflowy_mcp.coherence import (
    JournalIntegrityError,
    JournalPublicationError,
    SemanticEffect,
    append_journal_transaction,
    journal_record_filename,
    read_manifest,
)
from workflowy_mcp.coherence.barrier import _seal_recovery_or_barrier

REPO = Path(__file__).resolve().parents[1]


def _append_with_fault(paths, *, transaction_id: str, fault: str):
    """Run the raw append under one injected fault; return the failure."""

    error: JournalPublicationError | None = None
    try:
        append_journal_transaction(
            paths,
            operation_kind="test",
            effects=[SemanticEffect.tombstone([f"{transaction_id}-ghost"])],
            producer=s04_producer(),
            transaction_id=transaction_id,
            fs_ops=fault_ops(fault),
        )
    except JournalPublicationError as exc:
        error = exc
    assert error is not None, f"append should have failed at {fault}"
    return error


# ---------------------------------------------------------------------------
# Attempted-sequence preservation on the append itself
# ---------------------------------------------------------------------------


def test_append_stamps_attempted_sequence_on_record_write_failure(tmp_path):
    """The record-write failure carries the attempted sequence (type kept)
    AND phase-aware durability evidence: the write failed BEFORE the atomic
    replace, so the stamp must report the record NOT durable (sequence
    allocation alone is not durable ownership — Round 2, item 1)."""

    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    with pytest.raises(InjectedFault) as captured:
        append_journal_transaction(
            paths,
            operation_kind="test",
            effects=[SemanticEffect.tombstone(["g"])],
            producer=s04_producer(),
            transaction_id="tx-stamp-record",
            fs_ops=fault_ops("before_record_replace"),
        )
    assert isinstance(captured.value, InjectedFault)  # S01 seam type preserved
    assert captured.value.sequence == 1
    assert captured.value.record_durable is False
    # No record ever occupied the attempted sequence (the work-order item-1
    # precondition: the attempted sequence was allocated, never written).
    assert not (paths.journal_dir / journal_record_filename(1)).exists()


def test_append_stamps_attempted_sequence_on_head_advance_failure(tmp_path):
    """The head-advancement failure carries the attempted sequence (type kept)."""

    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    with pytest.raises(InjectedFault) as captured:
        append_journal_transaction(
            paths,
            operation_kind="test",
            effects=[SemanticEffect.tombstone(["g"])],
            producer=s04_producer(),
            transaction_id="tx-stamp-manifest",
            fs_ops=fault_ops("after_record_replace_before_manifest"),
        )
    assert isinstance(captured.value, InjectedFault)  # S01 seam type preserved
    assert captured.value.sequence == 1
    # The record is durable above the unadvanced head (orphan evidence).
    assert (paths.journal_dir / journal_record_filename(1)).exists()
    assert read_manifest(paths, required=True).journal_head_seq == 0
    # Phase-aware (Round 2, item 1): the failure surfaced from the
    # record-write step, but the record's atomic replace HAD landed — the
    # stamp proves exact-record durability.  This is what keeps the valid
    # durable-orphan incorporation case (the work order's "do not weaken"
    # clause) intact.
    assert captured.value.record_durable is True


def test_append_stamps_record_durable_on_true_head_advance_failure(tmp_path):
    """A genuine head-advancement failure (the record write fully completed
    and the manifest replace never landed) stamps durable ownership of the
    attempted sequence (Round 2, item 1)."""

    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    with pytest.raises(InjectedFault) as captured:
        append_journal_transaction(
            paths,
            operation_kind="test",
            effects=[SemanticEffect.tombstone(["g"])],
            producer=s04_producer(),
            transaction_id="tx-stamp-head",
            fs_ops=fault_ops("before_manifest_replace"),
        )
    assert isinstance(captured.value, InjectedFault)  # S01 seam type preserved
    assert captured.value.sequence == 1
    assert captured.value.record_durable is True
    assert (paths.journal_dir / journal_record_filename(1)).exists()
    assert read_manifest(paths, required=True).journal_head_seq == 0


# ---------------------------------------------------------------------------
# (a) Manifest replacement becomes visible, then reports failure
# ---------------------------------------------------------------------------


def test_manifest_replacement_visible_then_failure_recognized_committed(tmp_path):
    """Work-order item 1(a): the manifest replace DID land (head 1 visible)
    but the head-advancement call still reported failure.  The transaction
    is committed at the attempted sequence AT the fresh pre-head — the
    pre-fix pre_head+1..head scan missed it and false-dirtied.  Now:
    recognized as committed; NO barrier."""

    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    result = s04_append(
        paths,
        transaction_id="tx-visible",
        fs_ops=fault_ops("after_manifest_replace"),
    )
    assert result.disposition == "recovered_orphan"
    assert result.transaction is not None
    assert result.transaction.transaction_id == "tx-visible"
    assert result.transaction.sequence == 1
    manifest = read_manifest(paths, required=True)
    assert manifest.journal_head_seq == 1
    assert manifest.dirty_status.active is False


# ---------------------------------------------------------------------------
# (b) Two REAL subprocesses: P1 orphan adopted by P2 before P1 resumes
# ---------------------------------------------------------------------------

_P1_ORPHAN_DRIVER = r"""
import json
import sys

sys.path.insert(0, sys.argv[2] + "/tests")
from pathlib import Path

from coherence_harness import (
    ACCOUNT_A,
    fault_ops,
    make_coherence_root,
    s04_producer,
    touch,
    wait_for_file,
)
from workflowy_mcp.coherence import (
    SemanticEffect,
    append_journal_transaction,
    read_manifest,
)
from workflowy_mcp.coherence.barrier import _seal_recovery_or_barrier

root = Path(sys.argv[1])
paths = make_coherence_root(root, ACCOUNT_A, bootstrap=False)
error = None
try:
    append_journal_transaction(
        paths,
        operation_kind="test",
        effects=[SemanticEffect.tombstone(["p1-ghost"])],
        producer=s04_producer(),
        transaction_id="tx-p1",
        fs_ops=fault_ops("after_record_replace_before_manifest"),
    )
except Exception as exc:  # noqa: BLE001
    error = exc
assert error is not None, "P1 append must fail after the durable record write"
assert getattr(error, "sequence", None) == 1, (type(error), getattr(error, "sequence", None))
touch(root / "p1_orphan_ready")
assert wait_for_file(root / "p2_done", timeout=60), "P2 never completed"
result = _seal_recovery_or_barrier(
    paths,
    publication_error=error,
    operation_kind="test",
    transaction_id="tx-p1",
    fs_ops=None,
    checkpoint_request=None,
)
manifest = read_manifest(paths, required=True)
print(
    json.dumps(
        {
            "disposition": result.disposition,
            "head": manifest.journal_head_seq,
            "barrier": manifest.dirty_status.active,
            "tx_sequence": result.transaction.sequence if result.transaction else None,
        }
    )
)
"""

_P2_ADOPTER_DRIVER = r"""
import json
import sys

sys.path.insert(0, sys.argv[2] + "/tests")
from pathlib import Path

from coherence_harness import ACCOUNT_A, make_coherence_root, s04_append, touch, wait_for_file

root = Path(sys.argv[1])
paths = make_coherence_root(root, ACCOUNT_A, bootstrap=False)
assert wait_for_file(root / "p1_orphan_ready", timeout=60), "P1 never wrote its orphan"
# P2's append first ADOPTS P1's orphan (seq 1) via the deterministic
# recovery, then commits its own record at seq 2.
result = s04_append(paths, transaction_id="tx-p2")
touch(root / "p2_done")
print(json.dumps({"disposition": result.disposition, "sequence": result.transaction.sequence}))
"""


def test_cross_process_orphan_adoption_recognized_committed(tmp_path):
    """Work-order item 1(b): P1 writes seq N and fails head advancement;
    P2 recovers N and appends N+1; P1 resumes recovery and recognizes its
    transaction — NO barrier (the pre-fix pre_head scan missed it)."""

    root = tmp_path / "share"
    make_coherence_root(root, ACCOUNT_A)  # the parent seeds the shared root
    p1 = launch_subprocess(_P1_ORPHAN_DRIVER, str(root), str(REPO))
    p2 = launch_subprocess(_P2_ADOPTER_DRIVER, str(root), str(REPO))
    out1, err1 = p1.communicate(timeout=120)
    out2, err2 = p2.communicate(timeout=120)
    assert p2.returncode == 0, f"P2 failed:\n{err2}"
    assert p1.returncode == 0, f"P1 failed:\n{err1}"
    r2 = json.loads(out2.strip().splitlines()[-1])
    assert r2 == {"disposition": "published", "sequence": 2}
    r1 = json.loads(out1.strip().splitlines()[-1])
    assert r1["disposition"] == "recovered_orphan"
    assert r1["tx_sequence"] == 1
    assert r1["head"] == 2
    assert r1["barrier"] is False


# ---------------------------------------------------------------------------
# (c) The ordering extended through checkpoint incorporation + retirement
# ---------------------------------------------------------------------------

_P1_INCORPORATED_DRIVER = r"""
import json
import sys

sys.path.insert(0, sys.argv[2] + "/tests")
from pathlib import Path

from coherence_harness import (
    ACCOUNT_A,
    fault_ops,
    make_coherence_root,
    s04_producer,
    touch,
    wait_for_file,
)
from workflowy_mcp.coherence import (
    SemanticEffect,
    append_journal_transaction,
    read_manifest,
)
from workflowy_mcp.coherence.barrier import _seal_recovery_or_barrier

root = Path(sys.argv[1])
paths = make_coherence_root(root, ACCOUNT_A, bootstrap=False)
error = None
try:
    append_journal_transaction(
        paths,
        operation_kind="test",
        effects=[SemanticEffect.tombstone(["p1-ghost"])],
        producer=s04_producer(),
        transaction_id="tx-p1",
        fs_ops=fault_ops("after_record_replace_before_manifest"),
    )
except Exception as exc:  # noqa: BLE001
    error = exc
assert error is not None, "P1 append must fail after the durable record write"
assert getattr(error, "sequence", None) == 1, (type(error), getattr(error, "sequence", None))
touch(root / "p1_orphan_ready")
assert wait_for_file(root / "p2_done", timeout=90), "P2 never completed"
result = _seal_recovery_or_barrier(
    paths,
    publication_error=error,
    operation_kind="test",
    transaction_id="tx-p1",
    fs_ops=None,
    checkpoint_request=None,
)
manifest = read_manifest(paths, required=True)
print(
    json.dumps(
        {
            "disposition": result.disposition,
            "transaction_is_none": result.transaction is None,
            "barrier": manifest.dirty_status.active,
            "epoch_number": manifest.epoch_number,
            "watermark": manifest.base_incorporated_through_seq,
            "head": manifest.journal_head_seq,
            "record1_exists": (paths.journal_dir / "00000000000000000001.json").exists(),
        }
    )
)
"""

_P2_CHECKPOINT_DRIVER = r"""
import asyncio
import json
import sys

sys.path.insert(0, sys.argv[2] + "/tests")
from pathlib import Path

from coherence_harness import (
    ACCOUNT_A,
    make_coherence_root,
    s04_append,
    s04_checkpoint_config,
    touch,
    wait_for_file,
)
from workflowy_mcp.coherence import run_checkpoint_rotation

root = Path(sys.argv[1])
paths = make_coherence_root(root, ACCOUNT_A, bootstrap=False)
assert wait_for_file(root / "p1_orphan_ready", timeout=60), "P1 never wrote its orphan"
result = s04_append(paths, transaction_id="tx-p2")
# Checkpoint incorporates BOTH records into the new epoch base; physical
# retirement then DELETES P1's record file before P1 resumes.
rotation = asyncio.run(
    run_checkpoint_rotation(
        paths,
        snapshot_dir=root / "nodes_export_cache",
        config=s04_checkpoint_config(record_threshold=2),
    )
)
touch(root / "p2_done")
print(
    json.dumps(
        {
            "disposition": result.disposition,
            "rotation": rotation.status,
            "retired": rotation.retired_record_count,
        }
    )
)
"""


def test_cross_process_incorporated_orphan_recognized_committed(tmp_path):
    """Work-order item 1(c): the (b) ordering extended through checkpoint
    incorporation AND physical retirement — P1's record disappears before
    P1 resumes.  The attempted sequence lies at/below the base watermark,
    which is conclusive committed proof (sequence ownership + contiguity):
    P1 recognizes its transaction as ``recovered_incorporated`` — NO
    barrier, no fabricated record."""

    root = tmp_path / "share"
    paths = make_coherence_root(root, ACCOUNT_A)
    snapshot_dir = root / "nodes_export_cache"
    base = s04_write_base_snapshot(
        paths, snapshot_dir, [s02_node("base")], epoch_number=0, epoch_id="b" * 32
    )
    s04_install_base_manifest(paths, base_snapshot_path=base, epoch_id="b" * 32)
    p1 = launch_subprocess(_P1_INCORPORATED_DRIVER, str(root), str(REPO))
    p2 = launch_subprocess(_P2_CHECKPOINT_DRIVER, str(root), str(REPO))
    out1, err1 = p1.communicate(timeout=180)
    out2, err2 = p2.communicate(timeout=180)
    assert p2.returncode == 0, f"P2 failed:\n{err2}"
    assert p1.returncode == 0, f"P1 failed:\n{err1}"
    r2 = json.loads(out2.strip().splitlines()[-1])
    assert r2["disposition"] == "published"
    assert r2["rotation"] == "published"
    assert r2["retired"] == 2  # BOTH records retired below the new floor
    r1 = json.loads(out1.strip().splitlines()[-1])
    assert r1["disposition"] == "recovered_incorporated"
    assert r1["transaction_is_none"] is True  # never fabricated
    assert r1["barrier"] is False
    assert r1["epoch_number"] == 1
    assert r1["watermark"] == 2
    assert r1["head"] == 2
    assert r1["record1_exists"] is False  # genuinely retired before resume


# ---------------------------------------------------------------------------
# Round 2 (item 2) — a recovery publication error re-reads durable state
# ---------------------------------------------------------------------------


def test_recovery_publication_error_rereads_durable_manifest(tmp_path):
    """Work-order Round-2 item 2: the initial append writes a durable
    orphan but fails its head replacement; the recovery's adoption write
    BECOMES VISIBLE and then reports failure.  The sealer must re-read and
    strictly validate the CURRENT durable manifest under the coherence
    mutex and recognize the exact transaction as committed
    (recovered_orphan, NO barrier) — the pre-fix continuation used the
    stale pre-recovery manifest object and false-dirtied it."""

    paths = make_coherence_root(tmp_path, ACCOUNT_A)

    class _RecoveryVisibleThenFails:
        """The FIRST manifest replace fails pre-replace (the append's head
        advance, leaving the durable orphan); the SECOND — the recovery's
        adoption write — LANDS and then reports failure."""

        def __init__(self) -> None:
            self._manifest_replaces = 0

        def ops(self):
            from workflowy_mcp.carto_job_governance import (
                _default_mkdir,
                _default_replace,
                _default_tmp_name,
                _default_unlink,
                _default_write_fsync,
            )
            from workflowy_mcp.coherence import DurableFileOps

            def replace(tmp, target) -> None:
                if target.name == "manifest.json":
                    self._manifest_replaces += 1
                    if self._manifest_replaces == 1:
                        raise InjectedFault("before_manifest_replace")
                    _default_replace(tmp, target)  # the adoption LANDS
                    if self._manifest_replaces == 2:
                        # ...then reports failure (a barrier's write, if the
                        # sealer wrongly reaches that phase, lands cleanly).
                        raise InjectedFault("after_manifest_replace")
                    return
                _default_replace(tmp, target)

            return DurableFileOps(
                write_fsync=_default_write_fsync,
                replace=replace,
                mkdir=_default_mkdir,
                unlink=_default_unlink,
                tmp_name=_default_tmp_name,
            )

    result = None
    error: JournalPublicationError | None = None
    try:
        result = s04_append(
            paths,
            transaction_id="tx-reread",
            fs_ops=_RecoveryVisibleThenFails().ops(),
        )
    except JournalPublicationError as exc:
        error = exc
    assert error is None, (
        "seal false-dirtied a conclusively committed orphan instead of "
        f"re-reading the durable manifest: {error}; "
        f"barrier={read_manifest(paths, required=True).dirty_status}"
    )
    assert result is not None
    assert result.disposition == "recovered_orphan"
    assert result.transaction is not None
    assert result.transaction.transaction_id == "tx-reread"
    assert result.transaction.sequence == 1
    manifest = read_manifest(paths, required=True)
    assert manifest.journal_head_seq == 1
    assert manifest.dirty_status.active is False


# ---------------------------------------------------------------------------
# Round 2 (item 1) — sequence reuse after a failed record write can never
# manufacture recovered_incorporated
# ---------------------------------------------------------------------------

_P1_REUSED_DRIVER = r"""
import json
import sys

sys.path.insert(0, sys.argv[2] + "/tests")
from pathlib import Path

from coherence_harness import (
    ACCOUNT_A,
    fault_ops,
    make_coherence_root,
    s04_producer,
    touch,
    wait_for_file,
)
from workflowy_mcp.coherence import (
    JournalPublicationError,
    SemanticEffect,
    append_journal_transaction,
    journal_record_filename,
    read_manifest,
)
from workflowy_mcp.coherence.barrier import _seal_recovery_or_barrier

root = Path(sys.argv[1])
paths = make_coherence_root(root, ACCOUNT_A, bootstrap=False)
error = None
try:
    append_journal_transaction(
        paths,
        operation_kind="test",
        effects=[SemanticEffect.tombstone(["p1-ghost"])],
        producer=s04_producer(),
        transaction_id="tx-p1",
        fs_ops=fault_ops("before_record_replace"),
    )
except Exception as exc:  # noqa: BLE001
    error = exc
assert error is not None, "P1 append must fail before the record replace"
assert getattr(error, "sequence", None) == 1, (type(error), getattr(error, "sequence", None))
# The work-order precondition: NO T1 record ever occupied the sequence.
assert not (paths.journal_dir / journal_record_filename(1)).exists()
touch(root / "p1_failed_ready")
assert wait_for_file(root / "p2_done", timeout=90), "P2 never completed"
disposition = None
resumed_raise = None
try:
    result = _seal_recovery_or_barrier(
        paths,
        publication_error=error,
        operation_kind="test",
        transaction_id="tx-p1",
        fs_ops=None,
        checkpoint_request=None,
    )
    disposition = result.disposition
except JournalPublicationError as exc:
    resumed_raise = type(exc).__name__
manifest = read_manifest(paths, required=True)
print(
    json.dumps(
        {
            "disposition": disposition,
            "resumed_raise": resumed_raise,
            "barrier": manifest.dirty_status.active,
            "barrier_tx": manifest.dirty_status.transaction_id,
            "barrier_reason": manifest.dirty_status.reason_code,
            "epoch_number": manifest.epoch_number,
            "watermark": manifest.base_incorporated_through_seq,
            "head": manifest.journal_head_seq,
            "record1_exists": (paths.journal_dir / "00000000000000000001.json").exists(),
        }
    )
)
"""

_P2_REUSE_DRIVER = r"""
import asyncio
import json
import sys

sys.path.insert(0, sys.argv[2] + "/tests")
from pathlib import Path

from coherence_harness import (
    ACCOUNT_A,
    make_coherence_root,
    s04_append,
    s04_checkpoint_config,
    touch,
    wait_for_file,
)
from workflowy_mcp.coherence import SemanticEffect, run_checkpoint_rotation

root = Path(sys.argv[1])
paths = make_coherence_root(root, ACCOUNT_A, bootstrap=False)
assert wait_for_file(root / "p1_failed_ready", timeout=60), "P1 never failed"
# P2 legitimately REUSES the sequence P1 allocated but never wrote: the
# head never advanced, so P2's append allocates seq 1 for its OWN distinct
# transaction T2 and commits it.
result = s04_append(
    paths,
    effects=[SemanticEffect.tombstone(["p2-ghost"])],
    transaction_id="tx-p2",
)
# The checkpoint incorporates T2's record and PHYSICALLY retires it.
rotation = asyncio.run(
    run_checkpoint_rotation(
        paths,
        snapshot_dir=root / "nodes_export_cache",
        config=s04_checkpoint_config(record_threshold=1),
    )
)
touch(root / "p2_done")
print(
    json.dumps(
        {
            "disposition": result.disposition,
            "sequence": result.transaction.sequence,
            "rotation": rotation.status,
            "retired": rotation.retired_record_count,
        }
    )
)
"""


def test_cross_process_sequence_reuse_after_failed_record_write_barriers(tmp_path):
    """Work-order Round-2 item 1: P1's append fails at before_record_replace
    for T1 at sequence N (NO T1 record ever exists); P2 legitimately reuses
    N for its distinct T2; the checkpoint incorporates and physically
    retires T2; P1 resumes recovery.  Sequence allocation is NOT durable
    ownership: P1 must NOT return recovered_incorporated/recovered_orphan
    — the original outcome is unprovable, so the hard barrier is active
    for T1.  Mutation-sensitivity: against a23f9f9 this test fails by
    observing the false recovered_incorporated result."""

    root = tmp_path / "share"
    paths = make_coherence_root(root, ACCOUNT_A)
    snapshot_dir = root / "nodes_export_cache"
    base = s04_write_base_snapshot(
        paths, snapshot_dir, [s02_node("base")], epoch_number=0, epoch_id="b" * 32
    )
    s04_install_base_manifest(paths, base_snapshot_path=base, epoch_id="b" * 32)
    p1 = launch_subprocess(_P1_REUSED_DRIVER, str(root), str(REPO))
    p2 = launch_subprocess(_P2_REUSE_DRIVER, str(root), str(REPO))
    out1, err1 = p1.communicate(timeout=180)
    out2, err2 = p2.communicate(timeout=180)
    assert p2.returncode == 0, f"P2 failed:\n{err2}"
    assert p1.returncode == 0, f"P1 failed:\n{err1}"
    r2 = json.loads(out2.strip().splitlines()[-1])
    assert r2["disposition"] == "published"
    assert r2["sequence"] == 1  # T2 legitimately reused the sequence
    assert r2["rotation"] == "published"
    assert r2["retired"] == 1  # T2's record physically retired
    r1 = json.loads(out1.strip().splitlines()[-1])
    # P1 does NOT return recovered_incorporated/recovered_orphan …
    assert r1["disposition"] not in ("recovered_incorporated", "recovered_orphan"), r1
    # … it re-raises the original publication failure …
    assert r1["resumed_raise"] is not None, (
        f"P1 false-cleaned instead of establishing the hard barrier: {r1}"
    )
    # … and the hard barrier is ACTIVE for T1.
    assert r1["barrier"] is True, r1
    assert r1["barrier_tx"] == "tx-p1"
    assert r1["barrier_reason"] == "journal_publication_failed"
    # T2's committed state is wholly intact (P1's barrier changes nothing).
    assert r1["epoch_number"] == 1
    assert r1["watermark"] == 1
    assert r1["head"] == 1
    assert r1["record1_exists"] is False


# ---------------------------------------------------------------------------
# Strict-integrity and defensive-fallback companions
# ---------------------------------------------------------------------------


def test_head_covering_missing_unincorporated_record_fails_closed(tmp_path):
    """The head covers the attempted sequence while its record is absent
    WITHOUT incorporation — impossible by construction (only retirement
    deletes, only below the floor).  Typed integrity failure, NEVER a
    barrier guess through corruption (strict checks unchanged)."""

    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    s04_append(paths, transaction_id="tx-1")
    error = _append_with_fault(
        paths,
        transaction_id="tx-2",
        fault="after_record_replace_before_manifest",
    )
    s04_append(paths, transaction_id="tx-3")  # adopts the orphan, head -> 3
    # External tampering: the committed record vanishes unincorporated.
    os.remove(paths.journal_dir / journal_record_filename(2))
    with pytest.raises(JournalIntegrityError):
        _seal_recovery_or_barrier(
            paths,
            publication_error=error,
            operation_kind="test",
            transaction_id="tx-2",
            fs_ops=None,
            checkpoint_request=None,
        )
    # No barrier guessing through the corrupt lineage (invariant 6).
    assert read_manifest(paths, required=True).dirty_status.active is False


def test_fallback_full_tail_scan_without_attempted_sequence(tmp_path):
    """Defensive fallback: a failure carrying NO attempted sequence still
    identifies by transaction id across the WHOLE active tail (never
    restricted to the current invocation's adopted records)."""

    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    s04_append(paths, transaction_id="tx-committed")
    error = JournalPublicationError("synthetic legacy-shaped failure")
    assert error.sequence is None
    result = _seal_recovery_or_barrier(
        paths,
        publication_error=error,
        operation_kind="test",
        transaction_id="tx-committed",
        fs_ops=None,
        checkpoint_request=None,
    )
    assert result.disposition == "recovered_orphan"
    assert result.transaction is not None
    assert result.transaction.transaction_id == "tx-committed"
    assert read_manifest(paths, required=True).dirty_status.active is False


def test_genuinely_unprovable_outcome_still_raises_hard_barrier(tmp_path):
    """The negative control: a durable orphan that could NOT be committed
    (the recovery's own adoption write failed too) still raises
    verified_effect_unpublishable — the fix narrows false-dirt, never the
    genuine hard-barrier path."""

    paths = make_coherence_root(tmp_path, ACCOUNT_A)

    class _ManifestReplaceFailsTwice:
        def __init__(self) -> None:
            self._remaining = 2

        def ops(self):
            from workflowy_mcp.carto_job_governance import (
                _default_mkdir,
                _default_replace,
                _default_tmp_name,
                _default_unlink,
                _default_write_fsync,
            )
            from workflowy_mcp.coherence import DurableFileOps

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

    with pytest.raises(JournalPublicationError):
        s04_append(
            paths,
            transaction_id="tx-unprovable",
            fs_ops=_ManifestReplaceFailsTwice().ops(),
        )
    manifest = read_manifest(paths, required=True)
    assert manifest.dirty_status.active is True
    assert manifest.dirty_status.reason_code == "verified_effect_unpublishable"
    assert manifest.dirty_status.transaction_id == "tx-unprovable"
