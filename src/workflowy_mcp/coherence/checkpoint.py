"""Checkpoint-and-rotate compaction (S04 §9/§10).

Checkpointing is storage compaction, never reconciliation: it replays the
existing semantic journal onto the manifest base (canonical after-states
only — no new delta format, no Workflowy I/O), atomically publishes the
result as epoch E+1 through S02's SOLE epoch-publication authority (the
same in-memory publication record, token validation, terminal bookkeeping, and
``publish_epoch`` — H2), rotates the journal floor, and physically retires
incorporated records.

Hard rules (§1/§10):

- checkpoint NEVER clears hard dirt or external dirt — it is not recovery
  and its result must never imply otherwise (invariant 12);
- the in-process coherence lock is never held through full-snapshot serialization or
  replay: basis capture under the lock, materialization outside it,
  revalidate-and-publish under the lock with bounded head catch-up
  (invariant 13, §10.4);
- ONE epoch-publication authority: checkpoint, full refresh, and compat
  patch are mutually exclusive through the one in-memory record; epoch
  identity (not merely a longer tail) is revalidated before publication;
- an expected basis-race/defer is NOT hard dirt (§11.4);
- active journal thresholds count the replay tail only (invariant 14).
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any, Literal

from .durable_io import DurableFileOps
from .epoch_publisher import publish_epoch
from .errors import (
    CheckpointError,
    CoherenceAccountMismatchError,
    CoherenceSchemaError,
    CoherenceStateMissingError,
    EffectApplicationError,
    EffectValidationError,
    JournalIntegrityError,
    ManifestConflictError,
    UnsupportedCoherenceSchemaError,
)
from .journal_store import (
    JournalTailStats,
    _recover_orphans_locked,
    _validate_committed_range,
    retire_incorporated_journal_records,
)
from .paths import (
    CoherencePaths,
    coherence_lock_async,
    journal_record_filename,
)
from .refresh_authority import (
    _PublicationRecord,
    _read_publication_record,
    _store_publication_record,
    _write_claim_status,
)
from .schema import (
    CoherenceManifest,
    new_claim_id,
    new_epoch_id,
    read_manifest,
    utc_now,
)
from .snapshot_store import (
    load_manifest_base_envelope,
    materialize_payload_with_records,
    new_snapshot_candidate_path,
    read_journal_records,
    write_snapshot_candidate,
)

# S01/S02 fail-closed typed lineage errors propagate as themselves (§11.5)
# — never converted into an operational failure result, never hard-dirtied
# merely because compaction failed.  Bug-class effect/view errors likewise.
_TYPED_PROPAGATE = (
    JournalIntegrityError,
    CoherenceAccountMismatchError,
    CoherenceSchemaError,
    UnsupportedCoherenceSchemaError,
    CoherenceStateMissingError,
    EffectApplicationError,
    EffectValidationError,
)


# ---------------------------------------------------------------------------
# Result + trigger (§4.1/§9.3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckpointResult:
    """The outcome of one checkpoint rotation attempt."""

    status: Literal[
        "not_due", "deferred_publisher_busy", "deferred_head_advancing",
        "published", "failed",
    ]
    basis_epoch_id: str | None = None
    new_epoch_id: str | None = None
    incorporated_through_seq: int | None = None
    surviving_tail_count: int = 0
    retired_record_count: int = 0
    warnings: tuple[str, ...] = ()


# @beacon[
#   id=auto-beacon@checkpoint_is_due-8zdq,
#   role=checkpoint_is_due,
#   slice_labels=mci-epoch-compaction,
#   kind=ast,
# ]
def checkpoint_is_due(stats: JournalTailStats, config: Any) -> bool:
    """§9.3: due when the record COUNT or the journal BYTES threshold trips."""

    return (
        stats.record_count >= config.checkpoint_record_threshold
        or stats.total_bytes >= config.checkpoint_byte_threshold
    )


def _light_tail_stats(paths: CoherencePaths, manifest: CoherenceManifest) -> JournalTailStats:
    """Trigger-grade tail stats: count from the manifest markers, bytes from
    file stats — no record parsing on the hot path.

    The journal invariant (the manifest never points past a missing record,
    re-verified at every append/adoption) makes the marker-derived count
    exact.  A stat failure on the active range is a loud CheckpointError —
    compaction never silently mis-evaluates.
    """

    floor = manifest.journal_floor_seq
    head = manifest.journal_head_seq
    if head < floor:
        return JournalTailStats(
            floor_seq=floor,
            head_seq=head,
            record_count=0,
            total_bytes=0,
            largest_record_bytes=0,
            largest_record_sequence=None,
        )
    total = 0
    largest = 0
    largest_seq: int | None = None
    for sequence in range(floor, head + 1):
        record_path = paths.journal_dir / journal_record_filename(sequence)
        try:
            size = record_path.stat().st_size
        except OSError as exc:
            raise CheckpointError(
                f"active journal record unreadable during trigger evaluation: {exc}",
                path=record_path,
                sequence=sequence,
            ) from exc
        total += size
        if size > largest:
            largest = size
            largest_seq = sequence
    return JournalTailStats(
        floor_seq=floor,
        head_seq=head,
        record_count=head - floor + 1,
        total_bytes=total,
        largest_record_bytes=largest,
        largest_record_sequence=largest_seq,
    )


# ---------------------------------------------------------------------------
# Claim acquisition (§10.1) — the ONE publication-claim lifecycle (H2)
# ---------------------------------------------------------------------------


async def _acquire_checkpoint_claim(
    paths: CoherencePaths,
    *,
    due_head: int,
    fs_ops: DurableFileOps | None,
) -> tuple[str, _PublicationRecord | None]:
    """Checkpoint publication-claim acquisition.

    Same in-memory record and exact-token discipline as the S02 full refresh.
    Decision vocabulary:

    - ``owner``   — this caller minted the active checkpoint claim;
    - ``deduped`` — an active checkpoint's captured head COVERS the
      requester's observed due head (§10.1 checkpoint↔checkpoint dedupe);
    - ``busy``    — another publisher (full refresh / compat patch / an
      earlier-head checkpoint) is active: the opportunistic checkpoint
      defers WITHOUT waiting — no hidden queue (§10.1).

    Checkpoint never dedupes with compat patch or full refresh.
    """

    while True:
        async with coherence_lock_async(paths):
            manifest = read_manifest(paths, required=True)
            assert manifest is not None
            claim = _read_publication_record(paths)
            if claim is None or claim.is_terminal:
                now = utc_now()
                new_claim = _PublicationRecord(
                    account_scope_id=manifest.account_scope_id,
                    claim_id=new_claim_id(),
                    refresh_kind="checkpoint",
                    status="claimed",
                    cut_token=None,
                    requested_hard_generation=0,
                    requested_external_generation=0,
                    target_epoch_number=manifest.epoch_number + 1,
                    target_epoch_id=new_epoch_id(),
                    started_at=now,
                    updated_at=now,
                    result=None,
                    candidate_snapshot_path=None,
                    basis_head_seq=manifest.journal_head_seq,
                )
                _store_publication_record(paths, new_claim)
                return ("owner", new_claim)
            if claim.refresh_kind == "checkpoint" and (claim.basis_head_seq or 0) >= due_head:
                return ("deduped", claim)
            return ("busy", claim)


async def _terminalize_checkpoint_quiet(
    paths: CoherencePaths,
    claim: _PublicationRecord,
    result: dict[str, Any],
    *,
    fs_ops: DurableFileOps | None,
) -> None:
    """Best-effort terminal failure write; never masks the primary path."""

    try:
        await _write_claim_status(paths, claim, "failed", result=result, fs_ops=fs_ops)
    except Exception:  # noqa: BLE001 — never mask the primary path
        pass


# ---------------------------------------------------------------------------
# The rotation (§10.2–§10.7)
# ---------------------------------------------------------------------------


# @beacon[
#   id=coherence@checkpoint-rotation,
#   slice_labels=ra-coherence,ra-coherence-journal,carto-governance,mci-epoch-compaction,
#   kind=ast,
#   comment=S04 checkpoint-and-rotate compaction: count/byte trigger; ONE in-memory publication-record lifecycle (checkpoint/checkpoint dedupe by captured-head coverage, defer-not-queue on other publishers); due state revalidated AFTER record acquisition before materialization (no empty checkpoints — Remediation Chain 1 item 3); basis capture under the in-process coherence lock, private replay outside it, bounded head catch-up, epoch-identity revalidation, sole publish_epoch authority with publication-time dirt carry-forward (A3); post-publish journal retirement; expected races defer, never hard-dirty; no Workflowy I/O.,
# ]
async def run_checkpoint_rotation(
    paths: CoherencePaths,
    *,
    snapshot_dir: str | os.PathLike[str],
    config: Any,
    fs_ops: DurableFileOps | None = None,
) -> CheckpointResult:
    """Run one opportunistic checkpoint-and-rotate compaction.

    Never raises for operational failure — a ``failed``/``deferred_*``
    result with warnings; typed lineage/integrity/account/schema corruption
    propagates as its own S01 error (invariant 6 / §11.5).
    """

    warnings: list[str] = []
    # Maintenance residue cleanup (§10.7): retry below-floor retirement —
    # including residue an S02 full-refresh or compat-patch cut deferred
    # here (A5).  Harmless and warning-only.
    _, residue_warnings = retire_incorporated_journal_records(paths, fs_ops=fs_ops)
    warnings.extend(residue_warnings)

    manifest = read_manifest(paths, required=True)
    assert manifest is not None
    stats = _light_tail_stats(paths, manifest)
    if not checkpoint_is_due(stats, config):
        return CheckpointResult(status="not_due", warnings=tuple(warnings))
    if stats.largest_record_bytes > config.journal_record_warning_bytes:
        # Bounded diagnostic naming sequence and bytes (§9.2) — never
        # payload content.
        warnings.append(
            f"journal record seq {stats.largest_record_sequence} is "
            f"{stats.largest_record_bytes} bytes (warning threshold "
            f"{config.journal_record_warning_bytes})"
        )
    due_head = manifest.journal_head_seq

    decision, claim = await _acquire_checkpoint_claim(
        paths, due_head=due_head, fs_ops=fs_ops
    )
    if decision == "deduped":
        return CheckpointResult(
            status="deferred_publisher_busy",
            warnings=(
                *warnings,
                "deduplicated onto the active checkpoint claim (its "
                "captured head covers the requested due head)",
            ),
        )
    if decision == "busy":
        return CheckpointResult(
            status="deferred_publisher_busy",
            warnings=(
                *warnings,
                "another epoch publisher is active; opportunistic "
                "checkpoint defers without queueing",
            ),
        )
    assert claim is not None

    basis_epoch_id: str | None = None
    try:
        # 2b. Post-acquisition due-state REVALIDATION (S04 Remediation
        # Chain 1, work-order item 3): another publisher may have
        # incorporated the active tail between this owner's trigger
        # observation and claim acquisition.  Re-read the current
        # manifest/tail state under ONE brief locked hold (no replay, no
        # serialization under the mutex); if the tail is no longer due,
        # terminalize the claim and return an explicit non-publication
        # disposition instead of serializing and publishing an EMPTY
        # checkpoint.  Checkpoint/checkpoint dedupe (above) and the
        # defer-not-queue posture against full refresh / compat patch are
        # untouched.
        async with coherence_lock_async(paths):
            current = read_manifest(paths, required=True)
            assert current is not None
            current_stats = _light_tail_stats(paths, current)
        if not checkpoint_is_due(current_stats, config):
            try:
                await _write_claim_status(
                    paths,
                    claim,
                    "completed",
                    result={
                        "checkpoint": False,
                        "not_due": True,
                        "reason": "active tail incorporated by another "
                        "publisher before materialization",
                    },
                    fs_ops=fs_ops,
                )
            except Exception as exc:  # noqa: BLE001 — warning-only terminal write
                warnings.append(f"not-due claim terminal write failed: {exc}")
            return CheckpointResult(
                status="not_due",
                basis_epoch_id=current.epoch_id,
                warnings=(
                    *warnings,
                    "checkpoint no longer due after claim acquisition; "
                    "claim terminalized without publication",
                ),
            )

        # 3. Basis capture (§10.2) — under the coherence mutex only.
        async with coherence_lock_async(paths):
            manifest = read_manifest(paths, required=True)
            assert manifest is not None
            if manifest.base_snapshot_path is None:
                raise CheckpointError(
                    "checkpoint requires a non-bootstrap base lineage "
                    "(bootstrap manifest carries no base snapshot)",
                    path=paths.manifest_path,
                )
            _validate_committed_range(paths, manifest)
            manifest = _recover_orphans_locked(paths, manifest, fs_ops=fs_ops)
            basis_epoch_id = manifest.epoch_id
            basis_epoch_number = manifest.epoch_number
            account = manifest.account_scope_id
            head = manifest.journal_head_seq
            floor = manifest.journal_floor_seq
            records = read_journal_records(paths, floor, head)

        # 4. Private materialization (§10.3) — OUTSIDE every filesystem
        # mutex; no Workflowy API, WebSocket, broker, or subprocess call.
        envelope = load_manifest_base_envelope(paths, manifest)
        payload = materialize_payload_with_records(
            envelope.nodes_export,
            records,
            start_sequence=floor,
            account_scope_id=account,
        )
        candidate_path = write_snapshot_candidate(
            new_snapshot_candidate_path(snapshot_dir),
            payload,
            account_scope_id=account,
            epoch_number=claim.target_epoch_number or (basis_epoch_number + 1),
            epoch_id=str(claim.target_epoch_id),
            incorporated_through_seq=head,
            publication_kind="checkpoint",
            fs_ops=fs_ops,
        )
        claim = await _write_claim_status(
            paths,
            claim,
            "materializing",
            candidate_snapshot_path=candidate_path,
            fs_ops=fs_ops,
        )

        # 5. Head catch-up (§10.4) — bounded; the mutex is NEVER held
        # through replay or candidate rewrite, and defer decisions are
        # terminalized only AFTER the mutex is released (the terminal claim
        # write itself acquires that mutex — never nest it).
        passes = 0
        pending_defer: tuple[str, str] | None = None  # (status, reason)
        stable = False
        while pending_defer is None and not stable:
            async with coherence_lock_async(paths):
                manifest = read_manifest(paths, required=True)
                assert manifest is not None
                _validate_committed_range(paths, manifest)
                manifest = _recover_orphans_locked(paths, manifest, fs_ops=fs_ops)
                if manifest.epoch_id != basis_epoch_id:
                    # Epoch changed during out-of-lock materialization: the
                    # whole checkpoint basis is invalid — discard the
                    # candidate and defer; a later trigger restarts (§10.4).
                    pending_defer = (
                        "deferred_publisher_busy",
                        "basis epoch changed during materialization; "
                        "candidate discarded",
                    )
                    continue
                new_head = manifest.journal_head_seq
                if new_head == head:
                    stable = True  # stable head: proceed to publication
                    continue
                if passes >= config.checkpoint_max_catchup_passes:
                    pending_defer = (
                        "deferred_head_advancing",
                        "journal head kept advancing past the bounded "
                        "catch-up budget; lineage untouched",
                    )
                    continue
                tail = read_journal_records(paths, head + 1, new_head)
            # Outside the mutex: fold the tail into the candidate and rewrite
            # it with the advanced watermark.
            payload = materialize_payload_with_records(
                payload,
                tail,
                start_sequence=head + 1,
                account_scope_id=account,
            )
            head = new_head
            candidate_path = write_snapshot_candidate(
                candidate_path,
                payload,
                account_scope_id=account,
                epoch_number=claim.target_epoch_number or (basis_epoch_number + 1),
                epoch_id=str(claim.target_epoch_id),
                incorporated_through_seq=head,
                publication_kind="checkpoint",
                fs_ops=fs_ops,
            )
            passes += 1

        if pending_defer is not None:
            defer_status, defer_reason = pending_defer
            await _terminalize_checkpoint_quiet(
                paths,
                claim,
                {"deferred": True, "error": defer_reason},
                fs_ops=fs_ops,
            )
            return CheckpointResult(
                status=defer_status,  # type: ignore[arg-type]
                basis_epoch_id=basis_epoch_id,
                warnings=(*warnings, defer_reason),
            )

        # 6. Final epoch publication (§10.5) through the SOLE authority.
        publication = await publish_epoch(
            paths=paths,
            claim_id=claim.claim_id,
            basis_epoch_id=basis_epoch_id,
            candidate_snapshot_path=candidate_path,
            incorporated_through_seq=head,
            publication_kind="checkpoint",
            refresh_cut=None,
            clear_hard_through_generation=None,
            clear_external_through_generation=None,
            fs_ops=fs_ops,
        )
    except ManifestConflictError as exc:
        # Expected basis-race / claim-token race (§11.4): an epoch publisher
        # won while we materialized.  Defer — never hard dirt.
        await _terminalize_checkpoint_quiet(
            paths,
            claim,
            {"deferred": True, "error": str(exc)[:500]},
            fs_ops=fs_ops,
        )
        return CheckpointResult(
            status="deferred_publisher_busy",
            basis_epoch_id=basis_epoch_id,
            warnings=(*warnings, f"publication race deferred: {exc}"),
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except _TYPED_PROPAGATE:
        # Typed lineage corruption fails closed as itself (§11.5) after a
        # best-effort terminal record update.
        await _terminalize_checkpoint_quiet(
            paths, claim, {"error": "typed lineage failure"}, fs_ops=fs_ops
        )
        raise
    except BaseException as exc:  # noqa: BLE001 — operational failure result
        await _terminalize_checkpoint_quiet(
            paths,
            claim,
            {"error": str(exc)[:500], "error_type": type(exc).__name__},
            fs_ops=fs_ops,
        )
        return CheckpointResult(
            status="failed",
            basis_epoch_id=basis_epoch_id,
            warnings=(
                *warnings,
                f"checkpoint failed before publication; previous lineage "
                f"remains authoritative: {type(exc).__name__}: {exc}",
            ),
        )

    # 7. Post-publish bookkeeping: terminal claim write + journal
    # retirement (§10.7).  Both are warning-only — the new epoch is already
    # wholly authoritative (§11.5).
    terminal_warnings: list[str] = []
    try:
        await _write_claim_status(
            paths,
            claim,
            "completed",
            result={
                "checkpoint": True,
                "new_epoch_id": publication.epoch_id,
                "incorporated_through_seq": publication.incorporated_through_seq,
            },
            fs_ops=fs_ops,
        )
    except Exception as exc:  # noqa: BLE001 — warning only
        terminal_warnings.append(f"claim terminal write failed: {exc}")
    retired = 0
    try:
        retired, retire_warnings = retire_incorporated_journal_records(
            paths, fs_ops=fs_ops
        )
        terminal_warnings.extend(retire_warnings)
    except Exception as exc:  # noqa: BLE001 — warning only
        terminal_warnings.append(f"journal retirement failed: {exc}")

    final_manifest = read_manifest(paths, required=True)
    assert final_manifest is not None
    return CheckpointResult(
        status="published",
        basis_epoch_id=basis_epoch_id,
        new_epoch_id=publication.epoch_id,
        incorporated_through_seq=publication.incorporated_through_seq,
        surviving_tail_count=max(
            0, final_manifest.journal_head_seq - publication.incorporated_through_seq
        ),
        retired_record_count=retired,
        warnings=(*warnings, *terminal_warnings),
    )


# ---------------------------------------------------------------------------
# Process-local one-flight scheduler (§9.3)
# ---------------------------------------------------------------------------

_CHECKPOINT_TASKS: dict[str, asyncio.Task] = {}


async def _scheduled_checkpoint(
    paths: CoherencePaths, *, snapshot_dir: str | os.PathLike[str], config: Any, fs_ops: Any
) -> CheckpointResult:
    """Fire-and-forget body: typed errors become a failed result (never an
    unretrieved task exception)."""

    try:
        return await run_checkpoint_rotation(
            paths, snapshot_dir=snapshot_dir, config=config, fs_ops=fs_ops
        )
    except BaseException as exc:  # noqa: BLE001 — scheduled path swallows
        return CheckpointResult(
            status="failed",
            warnings=(
                f"scheduled checkpoint raised {type(exc).__name__}: {exc}",
            ),
        )


# @beacon[
#   id=coherence@checkpoint-trigger,
#   slice_labels=ra-coherence,ra-coherence-journal,carto-governance,mci-epoch-compaction,
#   kind=ast,
#   comment=Unarmed one-flight checkpoint trigger: adjacent synchronous manifest and tail-stat observation cannot interleave with another loop task. Due evaluation and task scheduling remain maintenance outside publication; failures never invalidate a durable seal. N08a owns arming and local publication.,
# ]
def request_checkpoint_if_due(
    paths: CoherencePaths,
    *,
    snapshot_dir: str | os.PathLike[str],
    config: Any,
    fs_ops: DurableFileOps | None = None,
) -> asyncio.Task | None:
    """Unarmed one-flight maintenance trigger; N08a owns production arming.

    Adjacent synchronous manifest and tail-stat observation runs to completion
    on the loop. Returns the active/new task, or None when not due. The caller
    never waits for compaction or treats its failure as mutation failure.
    """

    account = paths.account_root.name
    existing = _CHECKPOINT_TASKS.get(account)
    if existing is not None and not existing.done():
        return existing  # one-flight: dedupe onto the in-flight rotation
    # No suspension separates these observations, so another loop task can
    # publish/retire only before or after the pair, never between them.
    manifest = read_manifest(paths, required=False)
    if manifest is None:
        return None
    stats = _light_tail_stats(paths, manifest)
    if not checkpoint_is_due(stats, config):
        return None
    from .operation_cache import create_isolated_context_task

    task = create_isolated_context_task(
        _scheduled_checkpoint(
            paths, snapshot_dir=snapshot_dir, config=config, fs_ops=fs_ops
        ),
        name=f"coherence-checkpoint:{account}",
    )
    _CHECKPOINT_TASKS[account] = task
    return task
