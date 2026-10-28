"""The single epoch-publication authority (S02 §6).

``publish_epoch`` is the ONLY production code allowed to increment
``epoch_number``, mint/register ``epoch_id``, change ``base_snapshot_path``,
move ``base_incorporated_through_seq`` / ``journal_floor_seq``, or clear
covered barrier/external-dirt generations.  Full refresh and the
transitional compat patch INVOKE it; S04 checkpoint rotation will reuse it.
Neither caller reproduces its manifest-update body.

Physical ordering (§6.3):

- the candidate snapshot is fully serialized, fsynced, and atomically
  placed BEFORE the final publication mutex is acquired;
- orphan recovery runs under the OLD epoch before the boundary is crossed,
  so no recoverable above-head record is stranded;
- the manifest atomically names the candidate only after every lineage
  precondition holds; a crash after candidate write but before manifest
  replacement leaves an unreferenced candidate eligible for later GC;
- a crash after manifest replacement leaves a valid new epoch even when
  the process-local publication record is lost; the durable manifest remains
  the next process's lineage authority.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from .durable_io import DurableFileOps
from .errors import (
    CoherenceAccountMismatchError,
    CoherenceSchemaError,
    CoherenceStateMissingError,
    JournalPublicationError,
    ManifestConflictError,
)
from .journal_store import _recover_orphans_locked, _validate_committed_range
from .paths import CoherencePaths, coherence_lock_async
from .schema import (
    COMPAT_PATCH_KIND,
    FULL_REFRESH_KINDS,
    DirtyBarrierState,
    ExternalDirtState,
    RefreshCutToken,
    manifest_with_updates,
    read_external_dirt,
    read_manifest,
    write_external_dirt,
    write_manifest,
)
from .snapshot_store import (
    SnapshotEnvelope,
    normalize_snapshot_path,
    read_snapshot_envelope,
    validate_snapshot_lineage,
)

# Kinds publish_epoch accepts.  ``job_basis`` is NEVER a manifest base and
# never advances epoch/floor/head, so it is rejected here by construction.
PUBLISHABLE_KINDS = frozenset({"full_refresh", COMPAT_PATCH_KIND, "checkpoint"})

if TYPE_CHECKING:
    from .refresh_authority import _PublicationRecord


@dataclass(frozen=True)
class EpochPublicationResult:
    """The outcome of one successful epoch publication."""

    epoch_number: int
    epoch_id: str
    previous_epoch_id: str | None
    base_snapshot_path: str
    incorporated_through_seq: int
    journal_head_seq: int
    publication_kind: str
    hard_barrier_cleared: bool
    external_dirt_cleared: bool
    external_dirt_clear_failed: bool


def _cleared_barrier(barrier: DirtyBarrierState) -> DirtyBarrierState:
    """Inactive barrier with its generation PRESERVED (monotonic reference)."""

    return DirtyBarrierState(
        schema_version=1,
        account_scope_id=barrier.account_scope_id,
        active=False,
        generation=barrier.generation,
        reason_code=None,
        detail=None,
        transaction_id=None,
        affected_region=None,
        raised_at=None,
    )


def _cleared_external_dirt(state: ExternalDirtState) -> ExternalDirtState:
    """Inactive external dirt with its generation PRESERVED."""

    return ExternalDirtState(
        schema_version=1,
        account_scope_id=state.account_scope_id,
        active=False,
        generation=state.generation,
        observed_at=None,
        mutation_kind=None,
        node_uuid=None,
        parent_uuid=None,
        detail=None,
    )


# @beacon[
#   id=coherence@epoch-publisher,
#   slice_labels=carto-governance,ra-workflowy-cache,mci-epoch-authority,
#   kind=ast,
#   comment=Single epoch-publication authority: exact memory record/cut/basis checks, orphan recovery, contiguous range, exact-generation clearing and durable manifest CAS. Parse the candidate once outside the in-process coherence lock; defer its error until the existing step-5 site. No identity stat or TOCTOU proof.,
# ]
async def publish_epoch(
    *,
    paths: CoherencePaths,
    claim_id: str,
    basis_epoch_id: str,
    candidate_snapshot_path: str | os.PathLike[str],
    incorporated_through_seq: int,
    publication_kind: Literal["full_refresh", "compat_patch", "checkpoint"],
    refresh_cut: RefreshCutToken | None,
    clear_hard_through_generation: int | None,
    clear_external_through_generation: int | None,
    fs_ops: DurableFileOps | None = None,
) -> EpochPublicationResult:
    """Atomically publish epoch E+1 naming a durable candidate snapshot.

    Parse the candidate once before the in-process coherence lock. Record,
    manifest and cut guards precede any deferred candidate error; lineage
    validation and durable publication keep their existing order.
    """

    from .refresh_authority import _read_publication_record

    if publication_kind not in PUBLISHABLE_KINDS:
        raise CoherenceSchemaError(
            f"publish_epoch: publication_kind must be one of {sorted(PUBLISHABLE_KINDS)}"
        )
    if (
        isinstance(incorporated_through_seq, bool)
        or not isinstance(incorporated_through_seq, int)
        or incorporated_through_seq < 0
    ):
        raise CoherenceSchemaError(
            "publish_epoch: incorporated_through_seq must be a non-negative integer"
        )
    if publication_kind != "full_refresh":
        # Only an authoritative full refresh may clear covered generations.
        if clear_hard_through_generation is not None or (
            clear_external_through_generation is not None
        ):
            raise CoherenceSchemaError(
                "publish_epoch: only full_refresh publications may clear generations"
            )
    if refresh_cut is not None:
        for label, value, ceiling in (
            (
                "clear_hard_through_generation",
                clear_hard_through_generation,
                refresh_cut.hard_barrier_generation,
            ),
            (
                "clear_external_through_generation",
                clear_external_through_generation,
                refresh_cut.external_dirt_generation,
            ),
        ):
            if value is not None and value > ceiling:
                raise CoherenceSchemaError(
                    f"publish_epoch: {label} exceeds the refresh cut generation"
                )

    candidate = normalize_snapshot_path(candidate_snapshot_path)

    # Parse once outside short serialization. Defer read failure so steps
    # 1–4b retain precedence over missing or malformed candidate errors.
    envelope: SnapshotEnvelope | None = None
    candidate_error: Exception | None = None
    try:
        envelope = read_snapshot_envelope(candidate)
    except Exception as exc:  # noqa: BLE001 — deferred to the in-lock site
        candidate_error = exc

    async with coherence_lock_async(paths):
        # 1. Exact refresh-publication claim token and account.
        claim = _read_publication_record(paths)
        if claim is None:
            raise CoherenceStateMissingError(
                "publish_epoch: no active refresh-publication record",
            )
        _validate_claim_for_publication(claim, claim_id=claim_id, paths=paths)

        # 1b. SELF-DEFENSE (A2): the sole publication authority binds the
        # supplied kind/cut to the memory record — never to its caller.
        # Clearing ceilings derive from the record cut, and the
        # publication kind must match the claim kind.
        if publication_kind == "full_refresh":
            if claim.refresh_kind not in FULL_REFRESH_KINDS:
                raise CoherenceSchemaError(
                    "publish_epoch: full_refresh publication requires a full-refresh "
                    f"claim kind, not {claim.refresh_kind!r}",
                )
            if claim.cut_token is None:
                raise CoherenceSchemaError(
                    "publish_epoch: a full_refresh claim must carry an in-memory cut token",
                )
            if refresh_cut != claim.cut_token:
                raise ManifestConflictError(
                    "publish_epoch: supplied refresh cut does not equal the record cut",
                    account_scope_id=claim.account_scope_id,
                )
            effective_hard_ceiling = claim.cut_token.hard_barrier_generation
            effective_external_ceiling = claim.cut_token.external_dirt_generation
        elif publication_kind == COMPAT_PATCH_KIND:
            if claim.refresh_kind != COMPAT_PATCH_KIND:
                raise CoherenceSchemaError(
                    "publish_epoch: compat_patch publication requires a compat_patch "
                    f"claim, not {claim.refresh_kind!r}",
                )
            if refresh_cut is not None:
                raise CoherenceSchemaError(
                    "publish_epoch: compat_patch accepts no refresh cut",
                )
            effective_hard_ceiling = None
            effective_external_ceiling = None
        else:
            # checkpoint (A5.1, activated by S04): the claim kind must be a
            # genuine checkpoint claim — a full-refresh or compat-patch
            # claim can never be borrowed for a checkpoint publication.
            # Checkpoint accepts no refresh cut and clears NO generations
            # (only an authoritative full refresh may clear); the dirty
            # barrier and external dirt are carried forward from the
            # PUBLICATION-TIME manifest (A3 — read under this same mutex
            # below), never restored from a basis-captured state.
            if claim.refresh_kind != "checkpoint":
                raise CoherenceSchemaError(
                    "publish_epoch: checkpoint publication requires a checkpoint "
                    f"claim, not {claim.refresh_kind!r}",
                )
            if refresh_cut is not None:
                raise CoherenceSchemaError(
                    "publish_epoch: checkpoint accepts no refresh cut",
                )
            effective_hard_ceiling = None
            effective_external_ceiling = None

        # 2. Validate current manifest + active journal contiguity.
        manifest = read_manifest(paths, required=True)
        assert manifest is not None
        _validate_committed_range(paths, manifest)

        # 3. Orphan recovery BEFORE crossing the epoch boundary (§6.1.3).
        manifest = _recover_orphans_locked(paths, manifest, fs_ops=fs_ops)

        # 4. Stale-basis guard: never publish a candidate materialized from
        # another epoch.
        if manifest.epoch_id != basis_epoch_id:
            raise ManifestConflictError(
                "publish_epoch: basis epoch changed since materialization; discard "
                "the candidate and restart from a fresh basis",
                path=paths.manifest_path,
                account_scope_id=manifest.account_scope_id,
                epoch_id=manifest.epoch_id,
            )

        # 4b. A5.2 — the record cut must belong to the CURRENT publication
        # basis (post-orphan-recovery manifest identity). Argument↔record
        # equality (step 1b) is not enough: the record cut itself must be
        # consistent with the epoch being cut.  Any inconsistency is exactly
        # the detectable malformed-ownership state the sole authority must
        # fail closed on.
        if publication_kind == "full_refresh":
            cut = claim.cut_token
            assert cut is not None  # proven at step 1b
            if cut.account_scope_id != manifest.account_scope_id:
                raise CoherenceAccountMismatchError(
                    "publish_epoch: record cut account does not match the current "
                    "manifest account",
                    account_scope_id=cut.account_scope_id,
                    epoch_id=cut.epoch_id,
                )
            if cut.epoch_number != manifest.epoch_number or cut.epoch_id != manifest.epoch_id:
                raise ManifestConflictError(
                    "publish_epoch: record cut names a different epoch than the "
                    "current publication basis",
                    account_scope_id=manifest.account_scope_id,
                    epoch_id=manifest.epoch_id,
                )
            if cut.journal_head_seq > manifest.journal_head_seq:
                raise ManifestConflictError(
                    "publish_epoch: record cut journal head is ahead of the current "
                    "manifest head",
                    account_scope_id=manifest.account_scope_id,
                    epoch_id=manifest.epoch_id,
                    sequence=manifest.journal_head_seq,
                )

        # 5. Candidate metadata must match account, target epoch, kind, head.
        # The deferred outside-lock read failure (missing/malformed
        # candidate) re-raises HERE — after the claim/manifest guards —
        # exactly as when the read lived inside the lock.
        if candidate_error is not None:
            raise candidate_error
        assert envelope is not None
        expected_epoch_number = manifest.epoch_number + 1
        if claim.target_epoch_id is None or claim.target_epoch_number is None:
            raise CoherenceSchemaError(
                "publish_epoch: the active claim carries no target epoch identity",
            )
        if claim.target_epoch_number != expected_epoch_number:
            raise CoherenceSchemaError(
                f"publish_epoch: claim target epoch {claim.target_epoch_number} != "
                f"expected {expected_epoch_number}",
            )
        validate_snapshot_lineage(
            envelope,
            account_scope_id=manifest.account_scope_id,
            epoch_number=expected_epoch_number,
            epoch_id=claim.target_epoch_id,
            incorporated_through_seq=incorporated_through_seq,
            publication_kinds=frozenset({publication_kind}),
        )

        # 6. Incorporation watermark never beyond the current head.
        if incorporated_through_seq > manifest.journal_head_seq:
            raise JournalPublicationError(
                f"publish_epoch: incorporation head {incorporated_through_seq} exceeds "
                f"journal head {manifest.journal_head_seq}",
                path=paths.manifest_path,
                epoch_id=manifest.epoch_id,
                sequence=manifest.journal_head_seq,
            )

        # 7. Hard-barrier clearing decision (exact generation coverage).
        barrier = manifest.dirty_status
        hard_cleared = False
        new_barrier = barrier
        if (
            publication_kind == "full_refresh"
            and effective_hard_ceiling is not None
            and barrier.active
            and barrier.generation <= effective_hard_ceiling
        ):
            new_barrier = _cleared_barrier(barrier)
            hard_cleared = True

        # 8. Atomically replace the manifest with epoch E+1.  The journal
        # floor advances to incorporation+1; the head is UNCHANGED; every
        # record above the incorporation head survives as the active tail.
        new_mode = (
            "transition" if manifest.coherence_mode == "bootstrap" else manifest.coherence_mode
        )
        new_manifest = manifest_with_updates(
            manifest,
            epoch_number=expected_epoch_number,
            epoch_id=str(claim.target_epoch_id),
            previous_epoch_id=manifest.epoch_id,
            base_snapshot_path=candidate,
            base_incorporated_through_seq=incorporated_through_seq,
            journal_floor_seq=incorporated_through_seq + 1,
            journal_head_seq=manifest.journal_head_seq,
            coherence_mode=new_mode,
            dirty_status=new_barrier,
        )
        new_manifest = write_manifest(
            paths,
            new_manifest,
            expected_epoch_id=basis_epoch_id,
            expected_head_seq=manifest.journal_head_seq,
            fs_ops=fs_ops,
        )

        # 9. External-dirt clearing AFTER the new manifest is durable.  A
        # clearing failure retains the dirt and returns a successful
        # publication with the advisory flag — never a rollback.
        external_cleared = False
        external_clear_failed = False
        if publication_kind == "full_refresh" and effective_external_ceiling is not None:
            try:
                dirt = read_external_dirt(paths)
                assert dirt is not None
                if dirt.active and dirt.generation <= effective_external_ceiling:
                    write_external_dirt(paths, _cleared_external_dirt(dirt), fs_ops=fs_ops)
                    external_cleared = True
            except Exception:  # noqa: BLE001 — advisory; never roll back the epoch
                external_clear_failed = True

    return EpochPublicationResult(
        epoch_number=new_manifest.epoch_number,
        epoch_id=new_manifest.epoch_id,
        previous_epoch_id=new_manifest.previous_epoch_id,
        base_snapshot_path=new_manifest.base_snapshot_path or candidate,
        incorporated_through_seq=new_manifest.base_incorporated_through_seq,
        journal_head_seq=new_manifest.journal_head_seq,
        publication_kind=publication_kind,
        hard_barrier_cleared=hard_cleared,
        external_dirt_cleared=external_cleared,
        external_dirt_clear_failed=external_clear_failed,
    )


def _validate_claim_for_publication(
    claim: _PublicationRecord, *, claim_id: str, paths: CoherencePaths
) -> None:
    """Exact-token, exact-account, active-status publication guard."""

    if claim.claim_id != claim_id:
        raise ManifestConflictError(
            "publish_epoch: refresh-publication claim token mismatch",
            account_scope_id=claim.account_scope_id,
        )
    if claim.account_scope_id != paths.account_root.name:
        raise CoherenceAccountMismatchError(
            "publish_epoch: claim account does not match the coherence account root",
            account_scope_id=claim.account_scope_id,
        )
    if claim.is_terminal:
        raise ManifestConflictError(
            f"publish_epoch: claim is already terminal ({claim.status})",
            account_scope_id=claim.account_scope_id,
        )
