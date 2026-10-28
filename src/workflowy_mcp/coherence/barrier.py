"""Durable global hard dirty barrier, boundary status, and journal sealing (S04 §5/§6).

The hard barrier is the machine-wide, account-scoped durable signal that the
system can no longer prove shared cache lineage contains every committed
machine-local mutation.  It is **global-simple in V1** (invariant 2): one
active barrier blocks coherence-sensitive mutation across the account —
there is deliberately no regional barrier intersection math.

Contracts implemented here:

- **Publication** (:func:`raise_global_dirty_barrier`, §5.2): monotonic
  generation, idempotent same-fully-specified-event handling (A2: anonymous
  events with a blank/absent ``transaction_id`` NEVER deduplicate — a
  spurious extra generation is harmless, while a post-cut event deduped
  onto a pre-cut generation would be wrongly cleared by an in-flight
  refresh cut), exact bounded diagnostics, and durable-only return.
  :func:`raise_global_dirty_barrier_async` is the async lock facade over
  the same ONE locked authority, never a second writer.
- **Status** (:func:`read_coherence_boundary_status` /
  :func:`hard_stop_state`): the ONE boundary-status authority (H1).  The
  lock-free capture path serves operation-boundary advisory capture; the
  locked fresh path serves mutation/identity admission. The manifest
  barrier is the sole hard-stop source; external dirt remains advisory.
- **Sealing** (:func:`seal_verified_journal_transaction`, §6.1): the ONE
  production journal-publication door (H3).  Future S07+ mutation funnels
  and S15 accumulators call THIS wrapper, never the raw S01 appender.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from .durable_io import DurableFileOps
from .effects import SemanticEffect
from .errors import (
    CoherenceError,
    CoherenceLockError,
    CoherenceSchemaError,
    DirtyBarrierPublicationError,
    EffectValidationError,
    JournalIntegrityError,
    JournalPublicationError,
)
from .journal_store import (
    _read_record_file,
    _recover_orphans_locked,
    _validate_committed_range,
    _validate_record_against_manifest,
    append_journal_transaction,
    retire_incorporated_journal_records,
)
from .paths import (
    CoherencePaths,
    coherence_lock_async,
    journal_record_filename,
)
from .schema import (
    CoherenceManifest,
    DirtyBarrierState,
    JournalProducer,
    JournalTransaction,
    VerificationEvidence,
    manifest_with_updates,
    read_external_dirt,
    read_manifest,
    utc_now,
    write_manifest,
)

# Bounded diagnostics channel for post-seal checkpoint-scheduling failures
# (the same stdlib convention the governance modules use).
LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Barrier reason vocabulary (§5.1)
# ---------------------------------------------------------------------------

#: Reasons S04 may activate.  The schema's string field stays
#: forward-compatible; unknown reasons are rejected HERE, at the writer.
#: The historical ``lease_owner_died_after_send`` reason stays accepted for
#: compatibility; its on-disk plane retired in CUT-1 with no surviving producer.
BARRIER_REASONS_ACTIVE = frozenset(
    {
        "ambiguous_commit_outcome",
        "verified_effect_unpublishable",
        "journal_publication_failed",
        "lease_owner_died_after_send",  # historical compatibility vocabulary
    }
)

#: Documented future reasons — reserved, never activated by S04.
BARRIER_REASONS_RESERVED = frozenset(
    {
        "carto_unsealed_ambiguous",  # S15
    }
)

_DETAIL_LIMIT = 500


# ---------------------------------------------------------------------------
# Boundary status / result types (§4.1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
# @beacon[
#   id=auto-beacon@CoherenceBoundaryStatus-h3e0,
#   role=CoherenceBoundaryStatus,
#   slice_labels=mci-barrier-hard,
#   kind=ast,
# ]
class CoherenceBoundaryStatus:
    """One captured boundary-state snapshot: hard barrier + external dirt.

    Captured at an operation's adoption boundary for read advisories
    (lock-free, complete-or-old) or read fresh under the coherence mutex at
    a mutation/identity admission point.  Advisory shaping consumes the
    captured value; it never re-reads the manifest mid-operation (H4).

    """

    epoch_id: str
    journal_head_seq: int
    hard_dirty: bool
    hard_generation: int
    hard_reason_code: str | None
    hard_transaction_id: str | None
    external_dirty: bool
    external_generation: int
    external_mutation_kind: str | None


@dataclass(frozen=True)
# @beacon[
#   id=auto-beacon@BarrierPublicationResult-p4u1,
#   role=BarrierPublicationResult,
#   slice_labels=mci-barrier-hard,
#   kind=ast,
# ]
class BarrierPublicationResult:
    """The outcome of one :func:`raise_global_dirty_barrier` call.

    The two ``covered_by_*`` dispositions are historical compatibility
    vocabulary. They are no longer returned after CUT-1 retired the
    lease-token coverage guard.
    """

    state: DirtyBarrierState
    changed: bool
    disposition: Literal[
        "raised",
        "already_raised_same_event",
        "covered_by_completed_refresh",
        "covered_by_active_refresh",
    ]


@dataclass(frozen=True)
class JournalSealResult:
    """The outcome of one :func:`seal_verified_journal_transaction` call.

    ``transaction`` is None exactly when the disposition is
    ``recovered_incorporated``: the exact transaction was conclusively
    committed (its attempted sequence was adopted and then incorporated
    into the base by an epoch publisher) but its immutable record was
    physically retired before the recovery resumed, so it can no longer
    be returned.  Never fabricated.
    """

    transaction: JournalTransaction | None
    manifest: CoherenceManifest
    disposition: Literal["published", "recovered_orphan", "recovered_incorporated"]
    checkpoint_requested: bool


# ---------------------------------------------------------------------------
# Boundary status authority (H1)
# ---------------------------------------------------------------------------


def _compose_boundary_status(
    manifest: CoherenceManifest, dirt: Any
) -> CoherenceBoundaryStatus:
    barrier = manifest.dirty_status
    return CoherenceBoundaryStatus(
        epoch_id=manifest.epoch_id,
        journal_head_seq=manifest.journal_head_seq,
        hard_dirty=bool(barrier.active),
        hard_generation=barrier.generation,
        hard_reason_code=barrier.reason_code if barrier.active else None,
        hard_transaction_id=barrier.transaction_id if barrier.active else None,
        external_dirty=bool(dirt.active),
        external_generation=dirt.generation,
        external_mutation_kind=dirt.mutation_kind if dirt.active else None,
    )


# @beacon[
#   id=auto-beacon@read_coherence_boundary_status-m0g9,
#   role=read_coherence_boundary_status,
#   slice_labels=mci-barrier-hard,
#   kind=ast,
# ]
def read_coherence_boundary_status(
    paths: CoherencePaths, *, required: bool = False
) -> CoherenceBoundaryStatus | None:
    """The lock-free boundary-status capture authority.

    Complete-or-old by the S01/S02 durable-replacement doctrine (identical
    to the adoption engine's fast-path manifest read).  Used at operation
    adoption boundaries where a torn read is impossible and a slightly-old
    capture is a documented linearization (S04 §7.6).  Returns None only
    when no manifest exists and ``required`` is False. The status is
    composed from the manifest barrier and external dirt only.
    """

    manifest = read_manifest(paths, required=required)
    if manifest is None:
        return None
    dirt = read_external_dirt(paths)
    assert dirt is not None
    return _compose_boundary_status(manifest, dirt)


# @beacon[
#   id=auto-beacon@hard_stop_state-wa1u,
#   role=hard_stop_state,
#   slice_labels=mci-barrier-hard,
#   kind=ast,
# ]
async def hard_stop_state(paths: CoherencePaths) -> CoherenceBoundaryStatus:
    """THE sole hard-stop predicate (H1): a fresh, locked boundary read.

    Mutation and identity-proof admission consult THIS function and no
    other (§7.6 — the read must be fresh and locked at the admission
    point). It returns hard-stopped only when the manifest dirty barrier
    is active; external dirt remains a distinct indicator.
    """

    async with coherence_lock_async(paths):
        manifest = read_manifest(paths, required=True)
        assert manifest is not None
        dirt = read_external_dirt(paths)
        assert dirt is not None
        return _compose_boundary_status(manifest, dirt)


# ---------------------------------------------------------------------------
# Barrier publication (§5.2)
# ---------------------------------------------------------------------------


def _canonical_region_identity(affected_region: dict | None) -> str | None:
    """Canonical comparison form for the event-identity region component."""

    if affected_region is None:
        return None
    return json.dumps(affected_region, sort_keys=True, separators=(",", ":"))


def _normalize_barrier_event(
    reason_code: str,
    transaction_id: str | None,
    affected_region: dict | None,
    detail: str | None,
) -> tuple[str | None, dict | None, str | None]:
    """Pre-lock event validation/normalization shared by BOTH facades."""

    if reason_code not in BARRIER_REASONS_ACTIVE:
        raise CoherenceSchemaError(
            f"dirty barrier: reason_code must be one of {sorted(BARRIER_REASONS_ACTIVE)} "
            f"(reserved future reasons {sorted(BARRIER_REASONS_RESERVED)} are never "
            "activated by S04)"
        )
    # Normalize: a blank/whitespace transaction id is ANONYMOUS (A2).
    tx = str(transaction_id).strip() if transaction_id is not None else ""
    tx = tx or None
    if affected_region is not None and not isinstance(affected_region, dict):
        raise CoherenceSchemaError(
            "dirty barrier: affected_region must be an object or null"
        )
    bounded_detail = None
    if detail is not None:
        bounded_detail = str(detail)
        if len(bounded_detail) > _DETAIL_LIMIT:
            bounded_detail = bounded_detail[:_DETAIL_LIMIT]
    return tx, affected_region, bounded_detail


def _raise_global_dirty_barrier_locked(
    paths: CoherencePaths,
    *,
    reason_code: str,
    tx: str | None,
    affected_region: dict | None,
    bounded_detail: str | None,
    fs_ops: DurableFileOps | None,
) -> BarrierPublicationResult:
    """THE ONE locked barrier-publication body (§5.2), shared by the sync
    authority and the async adapter so there is exactly ONE implementation:

    1. read and strictly validate the manifest/account/journal integrity
       required to replace the manifest — a malformed or unclassifiable
       lineage keeps its S01 fail-closed error; the barrier NEVER guesses
       through corrupt lineage (invariant 6);
    2. compare the event identity ``(reason_code, transaction_id, canonical
       affected_region)`` against the active barrier — the identity is
       fully specified ONLY when ``transaction_id`` is nonblank (A2);
    3. the exact same fully specified event already active returns
       unchanged (``already_raised_same_event``) so retries do not
       manufacture generations; an anonymous event ALWAYS advances;
    4. otherwise increment ``generation`` exactly once, activate, and
       persist through the S01 durable manifest replacement with CAS
       guards — every non-dirt manifest field preserved byte-for-byte
       apart from ``updated_at``;
    5. return only after the new manifest is durable.
    """

    manifest = read_manifest(paths, required=True)
    assert manifest is not None
    _validate_committed_range(paths, manifest)

    active = manifest.dirty_status
    if (
        active.active
        and tx is not None
        and active.transaction_id == tx
        and active.reason_code == reason_code
        and _canonical_region_identity(active.affected_region)
        == _canonical_region_identity(affected_region)
    ):
        # The exact same FULLY SPECIFIED event is already active:
        # idempotent re-report, no manufactured generation (§5.2 step 4).
        return BarrierPublicationResult(
            state=active, changed=False, disposition="already_raised_same_event"
        )

    new_barrier = DirtyBarrierState(
        schema_version=1,
        account_scope_id=manifest.account_scope_id,
        active=True,
        generation=active.generation + 1,
        reason_code=reason_code,
        detail=bounded_detail,
        transaction_id=tx,
        affected_region=affected_region,
        raised_at=utc_now(),
    )
    new_manifest = write_manifest(
        paths,
        manifest_with_updates(manifest, dirty_status=new_barrier),
        expected_epoch_id=manifest.epoch_id,
        expected_head_seq=manifest.journal_head_seq,
        fs_ops=fs_ops,
    )
    return BarrierPublicationResult(
        state=new_manifest.dirty_status, changed=True, disposition="raised"
    )


# @beacon[
#   id=coherence@dirty-barrier,
#   slice_labels=ra-coherence,carto-governance,mci-barrier-hard,
#   kind=ast,
#   comment=One shared durable barrier body preserves monotonic generations and exact-event dedupe. The synchronous facade runs to completion; the async facade enters the in-process coherence lock. Only an authoritative full refresh clears the barrier.,
# ]
def raise_global_dirty_barrier(
    paths: CoherencePaths,
    *,
    reason_code: str,
    transaction_id: str | None = None,
    affected_region: dict | None = None,
    detail: str | None = None,
    fs_ops: DurableFileOps | None = None,
) -> BarrierPublicationResult:
    """Durably raise (or idempotently re-report) the global hard barrier.

    Normalize the event and run the one shared publication body to completion
    on the daemon loop, without a scheduling suspension.
    """

    tx, affected_region, bounded_detail = _normalize_barrier_event(
        reason_code, transaction_id, affected_region, detail
    )
    return _raise_global_dirty_barrier_locked(
        paths,
        reason_code=reason_code,
        tx=tx,
        affected_region=affected_region,
        bounded_detail=bounded_detail,
        fs_ops=fs_ops,
    )


# @beacon[
#   id=coherence@dirty-barrier-async,
#   slice_labels=ra-coherence,carto-governance,mci-barrier-hard,
#   kind=ast,
#   comment=One shared durable barrier body preserves monotonic generations and exact-event dedupe. The synchronous facade runs to completion; the async facade enters the in-process coherence lock. Only an authoritative full refresh clears the barrier.,
# ]
async def raise_global_dirty_barrier_async(
    paths: CoherencePaths,
    *,
    reason_code: str,
    transaction_id: str | None = None,
    affected_region: dict | None = None,
    detail: str | None = None,
    fs_ops: DurableFileOps | None = None,
    lock_timeout_seconds: float | None = None,
    cancel_check: Callable[[], Any] | None = None,
) -> BarrierPublicationResult:
    """Async-safe adapter around THE ONE S04 barrier authority.

    Identical semantics to :func:`raise_global_dirty_barrier` (the shared
    locked body runs verbatim), but the in-process coherence lock is acquired
    through :func:`coherence_lock_async` (bounded cancel-aware async
    polling). Budget exhaustion raises :class:`CoherenceLockError`; native
    cancellation and the existing cancellation probe propagate without a late
    transaction. The shared body remains the sole writer.
    """

    tx, affected_region, bounded_detail = _normalize_barrier_event(
        reason_code, transaction_id, affected_region, detail
    )
    lock_kwargs: dict[str, Any] = {}
    if lock_timeout_seconds is not None:
        lock_kwargs["timeout_seconds"] = float(lock_timeout_seconds)
    if cancel_check is not None:
        lock_kwargs["cancel_check"] = cancel_check
    async with coherence_lock_async(paths, **lock_kwargs):
        return _raise_global_dirty_barrier_locked(
            paths,
            reason_code=reason_code,
            tx=tx,
            affected_region=affected_region,
            bounded_detail=bounded_detail,
            fs_ops=fs_ops,
        )


# ---------------------------------------------------------------------------
# Verified journal sealing (§6.1) — the ONE production publication door
# ---------------------------------------------------------------------------


# @beacon[
#   id=coherence@journal-seal,
#   slice_labels=ra-coherence,ra-coherence-journal,carto-governance,mci-journal-commit,
#   kind=ast,
#   comment=S04 ONE production journal-sealing door (H3): wraps the S01 physical append; the record-durable/head-not-advanced fault reacquires through deterministic orphan recovery and recognizes the exact transaction by ATTEMPTED identity (pre-generated transaction id + attempted sequence stamped on every post-record failure + PHASE-AWARE record_durable evidence — sequence allocation alone never manufactures recovered_incorporated) — committed when its record is committed at the attempted sequence (own recovery, own visible manifest write, or another process's adoption) or durably occupied a sequence already incorporated at/below the base watermark (recovered_incorporated), never restricted to records adopted by the current invocation; a recovery publication error RE-READS the current durable manifest under the held mutex before identification (Remediation Chain 2 Rounds 1–2); otherwise raises the hard barrier BEFORE any coherent-failure report (DirtyBarrierPublicationError preserves both failures); a CALLER-SUPPLIED transaction failing publication-strict effect validation or verified-record construction (append-stamped) hard-dirties as verified_effect_unpublishable before the failure returns, while unstamped existing-lineage typed errors propagate untouched under invariant 6 (Remediation Chain 3); local cache application is callers' post-seal concern, never pre-publication; checkpoint evaluation happens only after a successful seal, outside every mutex, and checkpoint scheduling/maintenance failure NEVER propagates out of the sealer once record+head are durable (bounded logger diagnostics; Remediation Chain 1 item 2); DirtyBarrierPublicationError carries the full lineage coordinates required by S04 §5.3 — account scope, epoch, the ACTUAL pre-attempt/current manifest journal head (recovery-read preferred, append-stamped fallback), and the ATTEMPTED journal sequence as a distinct value, never mislabeled as the head (Remediation Chain 4); a CoherenceLockError from the initial append's lock acquisition or from the recovery's lock acquisition is journal publication failure, never a clean retry — the hard barrier (journal_publication_failed) is durable before the original failure returns, DirtyBarrierPublicationError preserves both failures on a double fault, the original publication failure stays the primary failed seal with the recovery lock failure as diagnostic context, no manifest lineage context is fabricated when the lock failure precedes any manifest read (the account is preserved from the validated paths; attempted_journal_sequence None), a recovery double fault keeps ALL THREE failures inspectable (original publication failure on the chain + recovery lock failure via recovery_error + barrier failure as underlying/cause), and the catch is never broadened beyond CoherenceLockError (Remediation Chain 6 Rounds 1–2).,
# ]
def seal_verified_journal_transaction(
    paths: CoherencePaths,
    *,
    operation_kind: str,
    effects: list[SemanticEffect] | tuple[SemanticEffect, ...],
    producer: JournalProducer,
    verification: tuple[VerificationEvidence, ...] | list[VerificationEvidence] = (),
    transaction_id: str | None = None,
    committed_at: str | None = None,
    cache_by_id: dict[str, dict[str, Any]] | None = None,
    fs_ops: DurableFileOps | None = None,
    checkpoint_request: Callable[[], Any] | None = None,
) -> JournalSealResult:
    """Seal one verified semantic transaction into the journal — or go dirty.

    Required flow (§6.1):

    1. validate the complete transaction in ``publication_strict`` mode
       against the producer's authoritative basis (inside the S01 append);
    2. call the S01 physical append protocol;
    3. on success return ``published`` — checkpoint thresholds are evaluated
       ONLY after every coherence mutex is released (the caller-supplied
       ``checkpoint_request`` hook runs here, never mid-append);
    4. on the known record-durable/head-not-advanced fault, reacquire
       through the S01 deterministic orphan recovery path;
    5. if the exact transaction is conclusively committed — its immutable
       record committed at the ATTEMPTED SEQUENCE (adopted by this
       recovery, made visible by our own failed manifest write, or adopted
       by ANOTHER process), or the attempted sequence already incorporated
       at/below the base watermark WITH phase-aware proof the exact record
       was durable there (``record_durable`` — sequence allocation alone
       never manufactures recovery; Remediation Chain 2 Round 2) — return
       ``recovered_orphan`` / ``recovered_incorporated`` with NO barrier
       (identification is never restricted to records adopted by the
       current recovery invocation — S04 Remediation Chain 2);
    6. otherwise raise the hard barrier with reason
       ``verified_effect_unpublishable`` (durable evidence of the verified
       effect exists but could not be committed) or
       ``journal_publication_failed`` (publication failed without durable
       committable evidence) BEFORE any coherent failure may be reported;
    7. if barrier publication ALSO fails, raise
       :class:`DirtyBarrierPublicationError` preserving both failures;
    8. the sealer NEVER touches the caller's local cache — local cache
       application is strictly a post-seal caller concern, so no effect is
       ever applied locally before journal publication is durable.

    Typed lineage/integrity/account/schema failures (a corrupt committed
    range, an account mismatch, an invalid orphan) propagate as their own
    S01 errors: the barrier is never a mechanism for guessing through an
    already-unclassifiable manifest (invariant 6).  A CALLER-SUPPLIED
    transaction that fails publication-strict effect validation or
    verified-record construction (stamped by the append) is instead
    ``verified_effect_unpublishable``: the barrier is raised durably BEFORE
    the validation failure returns (DirtyBarrierPublicationError preserves
    both failures if the barrier write also fails), with no local cache
    application (S04 Remediation Chain 3).

    A :class:`CoherenceLockError` from EITHER coherence-mutex acquisition
    point — the initial append's, or the recovery's after an existing
    :class:`JournalPublicationError` — is journal publication failure,
    never an ordinary clean retry (S04 Remediation Chain 6): the hard
    barrier (``journal_publication_failed``) must be durable BEFORE the
    original failure returns, or :class:`DirtyBarrierPublicationError`
    preserves both failures on a double fault.  The original publication
    failure stays the primary failed seal on the recovery path, with the
    recovery lock failure preserved as diagnostic context; on a recovery
    double fault ALL THREE failures stay separately inspectable (the
    original publication failure on the chain, the recovery lock failure
    via ``recovery_error``, the barrier failure as underlying/cause —
    Chain 6 Round 2).  No manifest lineage context is fabricated when the
    lock failure precedes any manifest read: the account is preserved from
    the validated paths (Chain 6 Round 2) while epoch/head stay unknown
    and the attempted journal sequence is None (allocation was never
    reached).  The catch is never broadened beyond CoherenceLockError.
    """

    tx = str(transaction_id).strip() if transaction_id is not None else ""
    # The sealer ALWAYS knows the transaction id (generated here when the
    # caller omits it): exact-transaction identification in the recovery
    # path is tx-based, never dependent on exception-carried sequences.
    if not tx:
        from .schema import new_transaction_id

        tx = new_transaction_id()
    try:
        record, new_manifest = append_journal_transaction(
            paths,
            operation_kind=operation_kind,
            effects=effects,
            producer=producer,
            verification=verification,
            transaction_id=tx,
            committed_at=committed_at,
            cache_by_id=cache_by_id,
            fs_ops=fs_ops,
        )
    except JournalPublicationError as exc:
        return _seal_recovery_or_barrier(
            paths,
            publication_error=exc,
            operation_kind=operation_kind,
            transaction_id=tx,
            fs_ops=fs_ops,
            checkpoint_request=checkpoint_request,
        )
    except (EffectValidationError, CoherenceSchemaError) as exc:
        # S04 Remediation Chain 3 (work-order item 1): a semantic
        # transaction that cannot pass publication-strict effect validation
        # or verified-record construction is verified_effect_unpublishable
        # — the global barrier MUST be durable before the failure returns.
        # ONLY append-stamped failures (arising from the caller's
        # new-transaction validation/construction region) take this path;
        # UNSTAMPED typed errors arise from EXISTING manifest/journal
        # lineage and propagate as their original typed integrity/schema
        # errors with no speculative barrier rewrite (invariant 6).
        if getattr(exc, "verified_record_construction_failed", None) is not True:
            raise
        return _seal_effect_validation_barrier(
            paths,
            validation_error=exc,
            operation_kind=operation_kind,
            transaction_id=tx,
            fs_ops=fs_ops,
        )
    except CoherenceLockError as exc:
        # S04 Remediation Chain 6: a coherence-mutex acquisition failure
        # while sealing an ALREADY-VERIFIED semantic effect is journal
        # publication failure, never an ordinary clean retry — the verified
        # mutation must not escape sealing with the global hard barrier
        # still clean merely because the coherence mutex could not be
        # acquired.  The initial append's acquisition failed BEFORE any
        # manifest read and BEFORE sequence allocation: no manifest lineage
        # context is knowable (the ACCOUNT is preserved from the validated
        # paths — Chain 6 Round 2) and the attempted journal sequence is
        # None.  The
        # catch is deliberately CoherenceLockError ONLY — never broadened
        # to every CoherenceError (invariant 6 untouched).
        return _seal_lock_failure_barrier(
            paths,
            lock_error=exc,
            operation_kind=operation_kind,
            transaction_id=tx,
            fs_ops=fs_ops,
        )

    # 3. Successful seal: checkpoint evaluation happens ONLY now, after the
    # append's coherence mutex has been released (§9.3).
    _quiet_retire_incorporated(paths, fs_ops=fs_ops)
    checkpoint_requested = _invoke_checkpoint_request_safely(checkpoint_request)
    return JournalSealResult(
        transaction=record,
        manifest=new_manifest,
        disposition="published",
        checkpoint_requested=checkpoint_requested,
    )


def _invoke_checkpoint_request_safely(
    checkpoint_request: Callable[[], Any] | None,
) -> bool:
    """Post-seal checkpoint scheduling is OPTIONAL maintenance (§9.3): once
    the journal record and manifest head are durable, a scheduling failure
    must NEVER propagate out of the sealer and fail a durably sealed
    mutation (S04 Remediation Chain 1, work-order item 2).  Bounded
    diagnostics through the module logger; the seal still reports
    published/recovered; optional compaction scheduling failure NEVER
    hard-dirties."""

    if checkpoint_request is None:
        return False
    try:
        return bool(checkpoint_request())
    except Exception as exc:  # noqa: BLE001 — maintenance failure must not propagate
        LOGGER.warning(
            "coherence checkpoint scheduling failed after a durable journal "
            "seal (the sealed mutation is unaffected; no barrier raised): "
            "%s: %.500s",
            type(exc).__name__,
            exc,
        )
        return False


def _quiet_retire_incorporated(
    paths: CoherencePaths, *, fs_ops: DurableFileOps | None
) -> None:
    """§10.7 'open' maintenance: every successful seal also retries
    below-floor retirement residue (incl. residue an S02 full-refresh or
    compat-patch cut deferred here).  Cleanup failure is deliberately
    silent here — residue is harmless, later maintenance retries, and the
    seal result has no warnings channel (CheckpointResult carries the
    warnings where the result type supports them)."""

    try:
        retire_incorporated_journal_records(paths, fs_ops=fs_ops)
    except Exception:  # noqa: BLE001 — quiet maintenance; never masks a seal
        pass


def _seal_recovery_or_barrier(
    paths: CoherencePaths,
    *,
    publication_error: JournalPublicationError,
    operation_kind: str,
    transaction_id: str | None,
    fs_ops: DurableFileOps | None,
    checkpoint_request: Callable[[], Any] | None,
) -> JournalSealResult:
    """§6.1 steps 4–7: recover conclusively, else raise the hard barrier.

    Identification (S04 Remediation Chain 2) is exact-transaction based and
    NEVER restricted to records adopted by the current recovery invocation:

    - the ATTEMPTED identity is reliable: the sealer pre-generates the
      transaction id, and the append stamps the ATTEMPTED SEQUENCE onto
      every post-record publication failure;
    - sequence allocation runs to completion on the daemon loop, so when
      this append's record DID become durable no other record can occupy
      the attempted sequence, and the manifest head / base watermark can
      advance past that sequence ONLY by adopting (and later
      incorporating) that exact record;
    - the exact transaction is therefore CONCLUSIVELY COMMITTED when
      (a) its immutable record exists at the attempted sequence and the
      current head covers it — whether the head advanced via THIS
      recovery, became visible from our own failed manifest write, or was
      advanced by a prior recovery of the orphan; or
      (b) the attempted sequence already lies at or below the base
      incorporation watermark AND the failure phase PROVES the exact
      record was durable there (``record_durable``) — the record was
      adopted, checkpointed into the new base, and physically retired
      before this recovery resumed (disposition
      ``recovered_incorporated``; no barrier).

    Round 2 (item 1) — sequence reuse can never manufacture recovery:
    sequence ALLOCATION alone is not durable ownership.  When the failure
    phase does not prove record durability (``record_durable`` falsy), the
    attempted record never existed; if the attempted sequence was reused
    by another transaction and incorporated, the original outcome is
    UNPROVABLE and the hard barrier is raised — never a false
    ``recovered_incorporated``.

    Round 2 (item 2) — a recovery publication error re-reads durable
    state: if the recovery's own manifest replacement became visible and
    then reported failure, the CURRENT durable manifest is re-read and
    strictly validated in the same synchronous transaction and identification runs
    against that state (the stale pre-recovery object never decides).

    A genuinely unprovable outcome still raises the hard barrier; strict
    account/epoch/sequence integrity checks are unchanged (a head covering
    the attempted sequence while its record is absent WITHOUT
    incorporation is impossible by construction and fails closed as
    :class:`JournalIntegrityError`).

    Recovery no longer acquires a synchronous lock. The existing narrow
    CoherenceLockError compatibility branch and diagnostic precedence remain
    unchanged: if such an error is supplied by a test/storage seam, the
    original publication failure stays primary and double-fault context is
    preserved. No broader exception catch or new recovery path is introduced.
    """

    attempted_sequence = publication_error.sequence
    # Phase-aware exact-record durability evidence (S04 Remediation Chain 2
    # Round 2, item 1): the append stamps ``record_durable`` onto every
    # post-record publication failure — True EXACTLY when the failure phase
    # proves the exact record became durable (a head-advancement failure,
    # or a record-step failure whose atomic replace had already landed).
    # Sequence ALLOCATION is never durable ownership: without this evidence
    # the attempted record never existed, and any record later occupying
    # the attempted sequence belongs to ANOTHER transaction.
    record_durable = getattr(publication_error, "record_durable", None) is True
    recovered: tuple[JournalTransaction, CoherenceManifest] | None = None
    incorporated_manifest: CoherenceManifest | None = None
    durable_evidence = record_durable
    # Lineage-context threading (S04 Remediation Chain 4, work-order item 4):
    # the CURRENT manifest read during this recovery (when one succeeds) is
    # the preferred source of the ACTUAL journal head/account/epoch for a
    # later DirtyBarrierPublicationError; the append-stamped pre-attempt
    # context on the publication error is the fallback.  No second manifest
    # read or speculative rewrite is performed for context alone.
    recovery_manifest: CoherenceManifest | None = None
    # S04 Remediation Chain 6: a coherence-mutex acquisition failure during
    # the RECOVERY is preserved as diagnostic context while the ORIGINAL
    # publication failure remains the primary failed seal.
    recovery_lock_error: CoherenceLockError | None = None
    try:
        manifest = read_manifest(paths, required=True)
        assert manifest is not None
        # The deterministic orphan-recovery authority: adopts a valid
        # contiguous above-head tail (including our record when durable),
        # quarantines+raises on anything unclassifiable. Typed
        # integrity/account errors propagate (invariant 6).
        try:
            manifest = _recover_orphans_locked(paths, manifest, fs_ops=fs_ops)
        except JournalPublicationError:
            # The recovery path's OWN durable write reported failure —
            # but its manifest replacement may have BECOME VISIBLE
            # before the failure surfaced (Remediation Chain 2 Round 2,
            # item 2). Re-read and strictly validate the CURRENT durable
            # manifest within this run-to-completion transaction and
            # re-run identification against that durable state: the
            # stale pre-recovery object must not decide (a visible
            # adoption makes the exact transaction committed — the
            # pre-fix continuation false-dirtied it). Typed lineage
            # errors from the re-read propagate (invariant 6): never
            # guess through malformed or incomplete journal lineage.
            manifest = read_manifest(paths, required=True)
            assert manifest is not None
        recovery_manifest = manifest
        head = manifest.journal_head_seq
        floor = manifest.journal_floor_seq
        watermark = manifest.base_incorporated_through_seq
        if attempted_sequence is not None:
            if attempted_sequence <= watermark:
                if record_durable:
                    # Incorporated: the failure phase PROVES our exact
                    # record was durable at the attempted sequence, and
                    # adoption is the ONLY path past it (sequence
                    # ownership + contiguity) — the exact transaction
                    # is conclusively committed even though its record
                    # was retired before we resumed.
                    incorporated_manifest = manifest
                # else: the attempted sequence was ALLOCATED to us but
                # our record never became durable — the record that was
                # adopted and incorporated at that sequence belongs to
                # ANOTHER transaction. The original outcome is
                # UNPROVABLE and falls through to the hard barrier
                # (never a false recovered_incorporated).
            else:
                record_path = paths.journal_dir / journal_record_filename(
                    attempted_sequence
                )
                if record_path.exists():
                    record: JournalTransaction | None
                    try:
                        record = _read_record_file(record_path, paths)
                    except JournalIntegrityError:
                        record = None
                    if (
                        record is not None
                        and record.transaction_id == transaction_id
                    ):
                        # Exact-record evidence of OUR transaction.
                        durable_evidence = True
                        if head >= attempted_sequence:
                            # Committed at the attempted sequence (strict
                            # account/epoch/sequence checks unchanged).
                            _validate_record_against_manifest(
                                record, manifest, path=record_path
                            )
                            recovered = (record, manifest)
                elif head >= attempted_sequence:
                    # Impossible by construction: the head covers the
                    # attempted sequence while its record is absent and
                    # the sequence was never incorporated (only
                    # retirement deletes, only below the floor).
                    raise JournalIntegrityError(
                        "journal head advanced past an attempted sequence "
                        "whose record is absent without incorporation",
                        path=record_path,
                        epoch_id=manifest.epoch_id,
                        sequence=attempted_sequence,
                    )
                # else: the record never became durable (or was
                # quarantined as corrupt) — no durable evidence.
        else:
            # Defensive fallback (the failure carried no attempted
            # sequence): scan the WHOLE active tail by transaction id —
            # identification is still never restricted to records adopted
            # by THIS recovery invocation.
            for sequence in range(floor, head + 1):
                record_path = paths.journal_dir / journal_record_filename(sequence)
                if not record_path.exists():
                    continue
                try:
                    record = _read_record_file(record_path, paths)
                except JournalIntegrityError:
                    continue
                if record.transaction_id == transaction_id:
                    recovered = (record, manifest)
                    break
            if recovered is None and transaction_id is not None:
                candidate = paths.journal_dir / journal_record_filename(head + 1)
                if candidate.exists():
                    try:
                        record = _read_record_file(candidate, paths)
                        durable_evidence = record.transaction_id == transaction_id
                    except JournalIntegrityError:
                        # Present but unreadable — durable evidence exists.
                        durable_evidence = True
    except JournalPublicationError:
        # Durable-write/manifest publication failures during recovery:
        # treat as unrecovered and let the barrier phase run (its own
        # failure is preserved).
        recovered = None
        incorporated_manifest = None
    except CoherenceLockError as exc:
        # S04 Remediation Chain 6: the recovery could not even acquire the
        # coherence mutex, so NO inspection occurred — the outcome is
        # UNPROVABLE.  Fall through to the barrier phase: a verified
        # mutation must not escape sealing with the global hard barrier
        # still clean merely because the recovery could not acquire the
        # mutex.  The original publication failure stays primary; this
        # lock failure rides as diagnostic context.
        recovered = None
        incorporated_manifest = None
        recovery_lock_error = exc
    if recovered is not None:
        record, manifest = recovered
        # 5. Conclusively committed: NO barrier.  Checkpoint evaluation
        # follows the same post-seal, outside-mutex rule.
        _quiet_retire_incorporated(paths, fs_ops=fs_ops)
        checkpoint_requested = _invoke_checkpoint_request_safely(checkpoint_request)
        return JournalSealResult(
            transaction=record,
            manifest=manifest,
            disposition="recovered_orphan",
            checkpoint_requested=checkpoint_requested,
        )
    if incorporated_manifest is not None:
        # 5b. Conclusively committed AND already incorporated into the base
        # by an epoch publisher (the record retired before we resumed): NO
        # barrier.  The exact committed record is no longer retrievable, so
        # the result carries the current manifest and no transaction.
        _quiet_retire_incorporated(paths, fs_ops=fs_ops)
        checkpoint_requested = _invoke_checkpoint_request_safely(checkpoint_request)
        return JournalSealResult(
            transaction=None,
            manifest=incorporated_manifest,
            disposition="recovered_incorporated",
            checkpoint_requested=checkpoint_requested,
        )

    # 6. Not conclusively committed: the hard barrier MUST be durable before
    # any coherent failure may be reported (invariant 5 — no false clean).
    # Remediation Chain 6: a recovery lock-acquisition failure forces the
    # uniform lock classification journal_publication_failed — no inspection
    # could establish durable committable evidence, so the sealer must not
    # claim verified_effect_unpublishable either.
    reason = (
        "journal_publication_failed"
        if recovery_lock_error is not None
        else (
            "verified_effect_unpublishable"
            if durable_evidence
            else "journal_publication_failed"
        )
    )
    detail = (
        f"journal seal for operation_kind={operation_kind!r} could not "
        f"conclusively publish: {publication_error}"
    )
    if recovery_lock_error is not None:
        detail += (
            f"; recovery coherence-mutex acquisition failed: "
            f"{type(recovery_lock_error).__name__}: {recovery_lock_error}"
        )
    try:
        raise_global_dirty_barrier(
            paths,
            reason_code=reason,
            transaction_id=transaction_id,
            affected_region=None,
            detail=detail,
            fs_ops=fs_ops,
        )
    except CoherenceError as barrier_error:
        # 7. BOTH failures are preserved; no coherent success may be reported.
        # Lineage context (S04 Remediation Chain 4, work-order item 4): the
        # ACTUAL pre-attempt/current manifest journal head (the recovery
        # read when available, else the append-stamped pre-attempt head),
        # the account scope and epoch, and the ATTEMPTED journal sequence as
        # a DISTINCT value — the attempted sequence is never mislabeled as
        # the manifest head.  Remediation Chain 6 Round 2: on a recovery
        # double fault ALL THREE failures stay separately inspectable — the
        # original publication failure rides the exception chain beneath
        # the barrier failure, the recovery lock failure is the explicit
        # recovery_error field, and the barrier-publication failure is the
        # underlying/cause.
        raise DirtyBarrierPublicationError(
            f"dirty barrier publication failed after journal publication failure "
            f"({reason}): {barrier_error}",
            original_reason_code=reason,
            original_transaction_id=transaction_id,
            journal_head_seq=(
                recovery_manifest.journal_head_seq
                if recovery_manifest is not None
                else getattr(publication_error, "manifest_journal_head_seq", None)
            ),
            attempted_journal_sequence=publication_error.sequence,
            account_scope_id=(
                recovery_manifest.account_scope_id
                if recovery_manifest is not None
                else getattr(publication_error, "manifest_account_scope_id", None)
            ),
            epoch_id=(
                recovery_manifest.epoch_id
                if recovery_manifest is not None
                else getattr(publication_error, "manifest_epoch_id", None)
            ),
            underlying=barrier_error,
            recovery_error=recovery_lock_error,
            path=paths.manifest_path,
        ) from barrier_error
    # The barrier is durable; surface the original publication failure (the
    # PRIMARY failed seal), preserving a recovery lock failure (when one
    # occurred) as diagnostic context on the exception chain (Chain 6).
    if recovery_lock_error is not None:
        raise publication_error from recovery_lock_error
    raise publication_error


def _seal_effect_validation_barrier(
    paths: CoherencePaths,
    *,
    validation_error: CoherenceError,
    operation_kind: str,
    transaction_id: str | None,
    fs_ops: DurableFileOps | None,
) -> JournalSealResult:
    """§6.1 pre-record validation/construction failure (S04 Remediation
    Chain 3, work-order item 1): the caller's verified transaction failed
    publication-strict effect validation or verified-record construction
    BEFORE any record write — nothing was committed and this transaction
    can never commit.  There is no orphan to recover and no ambiguous
    outcome: the unpublishable verified effect itself is the coherence
    event, so the global hard barrier (reason
    ``verified_effect_unpublishable``) must be durable BEFORE the failure
    returns.  If barrier publication also fails,
    :class:`DirtyBarrierPublicationError` preserves both failures.  The
    sealer NEVER applies any effect to the caller's local cache (here
    trivially: nothing validated, nothing to apply).  EXISTING
    manifest/journal lineage errors never take this path — they carry no
    append stamp and propagate as their original typed errors (invariant
    6, no speculative barrier rewrite).
    """

    try:
        raise_global_dirty_barrier(
            paths,
            reason_code="verified_effect_unpublishable",
            transaction_id=transaction_id,
            affected_region=None,
            detail=(
                f"journal seal for operation_kind={operation_kind!r} rejected "
                f"the verified transaction before any record write: "
                f"{type(validation_error).__name__}: {validation_error}"
            ),
            fs_ops=fs_ops,
        )
    except CoherenceError as barrier_error:
        # BOTH failures are preserved; no coherent success may be reported.
        # Lineage context (S04 Remediation Chain 4, work-order item 4):
        # threaded from the append's validation/construction stamping region
        # (the ACTUAL pre-attempt manifest head/account/epoch, plus the
        # ATTEMPTED sequence as a distinct value) — no second manifest read
        # or speculative rewrite.
        raise DirtyBarrierPublicationError(
            f"dirty barrier publication failed after verified-effect "
            f"validation/construction failure (verified_effect_unpublishable): "
            f"{barrier_error}",
            original_reason_code="verified_effect_unpublishable",
            original_transaction_id=transaction_id,
            journal_head_seq=getattr(validation_error, "manifest_journal_head_seq", None),
            attempted_journal_sequence=getattr(
                validation_error, "attempted_journal_sequence", None
            ),
            account_scope_id=getattr(
                validation_error, "manifest_account_scope_id", None
            ),
            epoch_id=getattr(validation_error, "manifest_epoch_id", None),
            underlying=barrier_error,
            path=paths.manifest_path,
        ) from barrier_error
    # The barrier is durable; surface the original validation failure.
    raise validation_error


def _seal_lock_failure_barrier(
    paths: CoherencePaths,
    *,
    lock_error: CoherenceLockError,
    operation_kind: str,
    transaction_id: str | None,
    fs_ops: DurableFileOps | None,
) -> JournalSealResult:
    """§6.1 coherence-mutex acquisition failure during sealing (S04
    Remediation Chain 6): the initial append could not acquire the
    coherence mutex, so an ALREADY-VERIFIED semantic effect is escaping
    unpublished with NOTHING inspected — no manifest read, no sequence
    allocation, no durable committable evidence.  That is journal
    publication failure, never an ordinary clean retry: the global hard
    barrier (reason ``journal_publication_failed``) must be durable BEFORE
    the failure returns, with the pre-generated transaction identity
    preserved.  If barrier publication also fails,
    :class:`DirtyBarrierPublicationError` preserves both failures — and NO
    manifest lineage context is fabricated: the lock failure preceded any
    manifest read, so epoch/actual head are genuinely unknown and the
    attempted journal sequence is None (allocation was never reached).
    The ACCOUNT is still preserved from the validated CoherencePaths
    (``paths.account_root.name`` — Chain 6 Round 2).  No effect is ever
    applied to the caller's local cache (strictly a post-seal caller
    concern).
    """

    try:
        raise_global_dirty_barrier(
            paths,
            reason_code="journal_publication_failed",
            transaction_id=transaction_id,
            affected_region=None,
            detail=(
                f"journal seal for operation_kind={operation_kind!r} could not "
                f"acquire the coherence mutex for the initial append: "
                f"{type(lock_error).__name__}: {lock_error}"
            ),
            fs_ops=fs_ops,
        )
    except CoherenceError as barrier_error:
        # BOTH failures are preserved; no coherent success may be reported.
        # The ACCOUNT is known from the validated paths even though no
        # manifest was ever read (Chain 6 Round 2); epoch/head/attempted
        # remain genuinely unknown.
        raise DirtyBarrierPublicationError(
            f"dirty barrier publication failed after coherence-mutex "
            f"acquisition failure during journal sealing "
            f"(journal_publication_failed): {barrier_error}",
            original_reason_code="journal_publication_failed",
            original_transaction_id=transaction_id,
            journal_head_seq=None,
            attempted_journal_sequence=None,
            account_scope_id=paths.account_root.name,
            underlying=barrier_error,
            path=paths.manifest_path,
        ) from barrier_error
    # The barrier is durable; surface the original lock failure.
    raise lock_error
