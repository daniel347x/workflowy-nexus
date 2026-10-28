"""Refresh authority (S02 §7/§9/§10/§12).

The in-process, account-scoped orchestration for every authoritative
``/nodes-export`` refresh and every transitional narrow-patch publication:

- ONE in-memory publication record per account serializes/dedupes
  all full refreshes in-process: a request whose required generations are
  covered by the ACTIVE record's cut JOINS it (one download, both callers
  succeed); a request requiring a newer generation waits for the active
  publisher and then acquires exactly one successor; ``compat_patch``
  publications never dedupe — they serialize fully.
- The refresh cut is captured atomically under the coherence mutex BEFORE
  the configurable settle delay; the export request is issued only after
  that delay.  Cut-first ordering is load-bearing: any signal arriving
  during the delay post-dates the cut and survives publication.
- The downloaded snapshot is never published directly: it is canonicalized
  through the S01 ``FULL_EXPORT_REPLACE`` codec and replayed through one
  captured contiguous journal head before becoming the new base.
- Local adoption is real: a successful outcome carries the fully
  materialized payload so the refreshing (or joining) process's ACTUAL
  production cache reflects the new lineage plus surviving tail.

The coherence package owns no HTTP client: the fetch callback is injected
by the caller (``WorkFlowyClientCore`` supplies credentials/transport).
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .canonical_node import IngestionProfile, _dewhiten_text, canonicalize_nodes_export
from .durable_io import DurableFileOps
from .effects import ApplicationMode, SemanticEffect, apply_semantic_effects
from .epoch_publisher import publish_epoch
from .errors import (
    CoherenceError,
    CoherenceAccountMismatchError,
    CoherenceSchemaError,
    CoherenceStateMissingError,
    ManifestConflictError,
    RefreshClaimError,
)
from .journal_store import (
    _recover_orphans_locked,
    _validate_committed_range,
    retire_incorporated_journal_records,
)
from .paths import (
    CoherencePaths,
    _resolve_physical_normalized,
    coherence_lock_async,
    validate_account_scope_id,
)
from .schema import (
    COMPAT_PATCH_KIND,
    FULL_REFRESH_KINDS,
    REFRESH_CLAIM_STATUSES,
    REFRESH_CLAIM_TERMINAL_STATUSES,
    REFRESH_KINDS,
    CoherenceManifest,
    DirtyBarrierState,
    ExternalDirtState,
    RefreshCutToken,
    _require_timestamp,
    initialize_manifest_if_absent,
    new_bootstrap_manifest,
    new_claim_id,
    new_epoch_id,
    read_external_dirt,
    read_manifest,
    utc_now,
)
from .snapshot_store import (
    load_manifest_base_envelope,
    materialize_current_payload,
    materialize_payload_with_records,
    new_snapshot_candidate_path,
    normalize_snapshot_path,
    read_journal_records,
    select_legacy_bootstrap_candidate,
    write_snapshot_candidate,
)

DEFAULT_SETTLE_SECONDS = 2.0
DEFAULT_POLL_SECONDS = 0.25
DEFAULT_MAX_WAIT_SECONDS = 600.0

PATCHABLE_FIELDS = frozenset({"name", "note"})


# ---------------------------------------------------------------------------
# Outcome
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RefreshOutcome:
    """One completed refresh-authority outcome (published or joined)."""

    status: str  # "published" | "joined"
    refresh_kind: str
    claim_id: str
    epoch_number: int
    epoch_id: str
    previous_epoch_id: str | None
    base_snapshot_path: str | None
    incorporated_through_seq: int
    journal_head_seq: int
    adopted_sequence: int
    hard_barrier_cleared: bool
    external_dirt_cleared: bool
    external_dirt_clear_failed: bool
    deduplicated: bool
    successor_refresh_performed: bool
    settle_seconds: float
    node_count: int
    snapshot_timestamp: str | None
    nodes_export: dict[str, Any]
    details: Mapping[str, Any] | None = None

    def to_result_dict(self) -> dict[str, Any]:
        """Additive coherence metadata atop the legacy success shape."""

        result: dict[str, Any] = {
            "success": True,
            "node_count": self.node_count,
            "timestamp": self.snapshot_timestamp or utc_now(),
            "epoch_number": self.epoch_number,
            "epoch_id": self.epoch_id,
            "base_snapshot_path": self.base_snapshot_path,
            "incorporated_through_seq": self.incorporated_through_seq,
            "journal_head_seq": self.journal_head_seq,
            "hard_barrier_cleared": self.hard_barrier_cleared,
            "external_dirt_cleared": self.external_dirt_cleared,
            "deduplicated": self.deduplicated,
            "successor_refresh_performed": self.successor_refresh_performed,
            "refresh_kind": self.refresh_kind,
            "settle_seconds": self.settle_seconds,
        }
        if self.external_dirt_clear_failed:
            result["external_dirt_clear_failed"] = True
        if self.details:
            result.update(dict(self.details))
        return result


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


# @beacon[
#   id=coherence@publication-record,
#   slice_labels=mci-epoch-authority,ra-workflowy-cache,
#   kind=ast,
#   comment=Private in-process publication record: exact-token serialization and initiation-cut coverage, with no owner identity or persistence.,
# ]
@dataclass(frozen=True)
class _PublicationRecord:
    """One account's current publication and its immutable initiation cut."""

    account_scope_id: str
    claim_id: str
    refresh_kind: str
    status: str
    cut_token: RefreshCutToken | None
    requested_hard_generation: int
    requested_external_generation: int
    target_epoch_number: int | None
    target_epoch_id: str | None
    started_at: str
    updated_at: str
    result: dict[str, Any] | None
    candidate_snapshot_path: str | None = None
    basis_head_seq: int | None = None

    def __post_init__(self) -> None:
        validate_account_scope_id(self.account_scope_id)
        if not isinstance(self.claim_id, str) or not self.claim_id:
            raise CoherenceSchemaError("refresh claim: claim_id must be a nonempty string")
        if self.refresh_kind not in REFRESH_KINDS:
            raise CoherenceSchemaError(
                f"refresh claim: refresh_kind must be one of {sorted(REFRESH_KINDS)}"
            )
        if self.status not in REFRESH_CLAIM_STATUSES:
            raise CoherenceSchemaError(
                f"refresh claim: status must be one of {sorted(REFRESH_CLAIM_STATUSES)}"
            )
        if self.cut_token is not None and not isinstance(self.cut_token, RefreshCutToken):
            raise CoherenceSchemaError("refresh claim: cut_token must be a RefreshCutToken or null")
        if self.cut_token is not None and self.cut_token.account_scope_id != self.account_scope_id:
            raise CoherenceAccountMismatchError(
                "refresh claim: cut_token account does not match claim account",
                account_scope_id=self.account_scope_id,
            )
        for key in ("requested_hard_generation", "requested_external_generation"):
            value = getattr(self, key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise CoherenceSchemaError(
                    f"refresh claim: {key} must be a non-negative integer"
                )
        if self.target_epoch_number is not None and (
            isinstance(self.target_epoch_number, bool)
            or not isinstance(self.target_epoch_number, int)
            or self.target_epoch_number < 0
        ):
            raise CoherenceSchemaError(
                "refresh claim: target_epoch_number must be a non-negative integer or null"
            )
        if self.target_epoch_id is not None and (
            not isinstance(self.target_epoch_id, str) or not self.target_epoch_id
        ):
            raise CoherenceSchemaError(
                "refresh claim: target_epoch_id must be a nonempty string or null"
            )
        _require_timestamp(self.started_at, "started_at", "refresh claim")
        _require_timestamp(self.updated_at, "updated_at", "refresh claim")
        if self.result is not None and not isinstance(self.result, dict):
            raise CoherenceSchemaError("refresh claim: result must be an object or null")
        if self.candidate_snapshot_path is not None and not isinstance(
            self.candidate_snapshot_path, str
        ):
            raise CoherenceSchemaError(
                "refresh claim: candidate_snapshot_path must be a string or null"
            )
        if self.basis_head_seq is not None and (
            isinstance(self.basis_head_seq, bool)
            or not isinstance(self.basis_head_seq, int)
            or self.basis_head_seq < 0
        ):
            raise CoherenceSchemaError(
                "refresh claim: basis_head_seq must be a non-negative integer or null"
            )
        if self.refresh_kind == "checkpoint" and self.basis_head_seq is None:
            raise CoherenceSchemaError(
                "refresh claim: a checkpoint claim must carry basis_head_seq "
                "(the journal head captured at acquisition)"
            )
        if self.refresh_kind != "checkpoint" and self.basis_head_seq is not None:
            raise CoherenceSchemaError(
                f"refresh claim: basis_head_seq is forbidden on a "
                f"{self.refresh_kind!r} claim (checkpoint-only field)"
            )

    @property
    def is_terminal(self) -> bool:
        return self.status in REFRESH_CLAIM_TERMINAL_STATUSES


_PUBLICATION_RECORDS: dict[str, _PublicationRecord] = {}


def _publication_key(paths: CoherencePaths) -> str:
    return os.path.normcase(os.fspath(paths.account_root))


def _read_publication_record(paths: CoherencePaths) -> _PublicationRecord | None:
    return _PUBLICATION_RECORDS.get(_publication_key(paths))


def _store_publication_record(
    paths: CoherencePaths,
    record: _PublicationRecord,
    *,
    expected_claim_id: str | None = None,
) -> _PublicationRecord:
    """Replace the account slot while the caller holds the coherence mutex."""

    if record.account_scope_id != paths.account_root.name:
        raise CoherenceAccountMismatchError(
            "refresh claim account does not match the coherence paths account root",
            account_scope_id=record.account_scope_id,
        )
    if expected_claim_id is not None:
        current = _read_publication_record(paths)
        if current is None or current.claim_id != expected_claim_id:
            raise ManifestConflictError(
                "refresh claim token changed underneath writer",
                account_scope_id=record.account_scope_id,
            )
    _PUBLICATION_RECORDS[_publication_key(paths)] = record
    return record


def _active_candidate_snapshot_paths(jobs_root: str | os.PathLike[str]) -> frozenset[str]:
    """Copy active candidate strings on-loop; the collector sees no registry."""

    coherence_root = os.path.normcase(
        os.fspath(_resolve_physical_normalized(Path(jobs_root) / ".coherence"))
    )
    return frozenset(
        normalize_snapshot_path(record.candidate_snapshot_path)
        for account_root, record in _PUBLICATION_RECORDS.items()
        if account_root.startswith(coherence_root + os.sep)
        and not record.is_terminal
        and record.candidate_snapshot_path
    )


def _check_cancel(cancel_check: Callable[[], Any] | None) -> None:
    if cancel_check is not None and cancel_check():
        raise asyncio.CancelledError("refresh authority wait cancelled")


def _required_generations(
    manifest: CoherenceManifest, dirt: ExternalDirtState
) -> tuple[int, int]:
    """The generations a refresh request must see cleared: the CURRENT
    active barrier/external-dirt generations (0 when clean)."""

    hard = manifest.dirty_status.generation if manifest.dirty_status.active else 0
    external = dirt.generation if dirt.active else 0
    return hard, external


def _cut_covers(
    cut: RefreshCutToken | None,
    req_hard: int,
    req_external: int,
) -> bool:
    if cut is None:
        return False
    if not (
        cut.hard_barrier_generation >= req_hard
        and cut.external_dirt_generation >= req_external
    ):
        return False
    return True


def _ensure_manifest(paths: CoherencePaths, *, snapshot_dir: str | os.PathLike[str]) -> CoherenceManifest:
    """Read the manifest, running the one-time legacy bootstrap when absent."""

    manifest = read_manifest(paths, required=False)
    if manifest is None:
        manifest = bootstrap_coherence_from_legacy(paths, snapshot_dir=snapshot_dir)
    return manifest


# ---------------------------------------------------------------------------
# One-time bootstrap (S02 §10.1) — sync; used by warm start and the authority
# ---------------------------------------------------------------------------


# @beacon[
#   id=coherence@legacy-bootstrap,
#   slice_labels=ra-workflowy-cache,carto-governance,mci-epoch-authority,
#   kind=ast,
#   comment=S02 one-time no-manifest bootstrap: newest usable legacy wrapper adopted as a wrapped lineage base (JOURNAL_SEMANTIC profile — never re-dewhitened), first-writer manifest convergence, unreferenced loser candidates left for GC.,
# ]
def bootstrap_coherence_from_legacy(
    paths: CoherencePaths,
    *,
    snapshot_dir: str | os.PathLike[str],
    fs_ops: DurableFileOps | None = None,
) -> CoherenceManifest:
    """One-time bootstrap when no manifest exists (S02 §10.1).

    Selects the lexicographically newest USABLE legacy snapshot, wraps it in
    a durable lineage copy (epoch 0, ``full_refresh`` kind, incorporated
    through sequence 0), and initializes a ``transition`` manifest
    referencing it.  With no usable snapshot, initializes a ``bootstrap``
    manifest with no base.  Concurrent first processes converge on ONE
    manifest: losers adopt the winner (their pre-serialized wrap is
    unreferenced residue for GC).  The legacy file is never mutated.

    The full-snapshot serialization deliberately happens BEFORE the
    coherence mutex is acquired (invariant 9).
    """

    account = paths.account_root.name
    existing = read_manifest(paths, required=False)
    if existing is not None:
        return existing

    # Unlocked pre-work: selection + canonicalization + durable wrap.  The
    # legacy payload is ALREADY semantic, so the bootstrap wrap uses the
    # JOURNAL_SEMANTIC profile (zero dewhitening) — re-running the cascading
    # full-export decode here would double-decode literal entities.
    wrapped_path: str | None = None
    epoch_id: str | None = None
    candidate = select_legacy_bootstrap_candidate(snapshot_dir)
    if candidate is not None:
        canonical = canonicalize_nodes_export(
            candidate.nodes_export, profile=IngestionProfile.JOURNAL_SEMANTIC
        )
        epoch_id = new_epoch_id()
        wrapped_path = write_snapshot_candidate(
            new_snapshot_candidate_path(snapshot_dir),
            canonical,
            account_scope_id=account,
            epoch_number=0,
            epoch_id=epoch_id,
            incorporated_through_seq=0,
            publication_kind="full_refresh",
            fs_ops=fs_ops,
            timestamp=candidate.snapshot_timestamp,
        )

    if wrapped_path is None:
        candidate_manifest: CoherenceManifest = new_bootstrap_manifest(account)
    else:
        assert epoch_id is not None
        candidate_manifest = CoherenceManifest(
            schema_version=1,
            account_scope_id=account,
            epoch_number=0,
            epoch_id=epoch_id,
            base_snapshot_path=wrapped_path,
            base_incorporated_through_seq=0,
            journal_floor_seq=1,
            journal_head_seq=0,
            coherence_mode="transition",
            dirty_status=DirtyBarrierState.clean(account),
            created_at=utc_now(),
            updated_at=utc_now(),
        )
    # First-writer initialization (seeds the mutex, creates the skeleton,
    # writes only when absent).  Losers adopt the winner's manifest.
    return initialize_manifest_if_absent(paths, candidate_manifest)


# ---------------------------------------------------------------------------
# Claim lifecycle helpers
# ---------------------------------------------------------------------------


async def _write_claim_status(
    paths: CoherencePaths,
    claim: _PublicationRecord,
    status: str,
    *,
    fs_ops: DurableFileOps | None = None,
    **changes: Any,
) -> _PublicationRecord:
    """Exact-token phase/result write under the coherence mutex."""

    async with coherence_lock_async(paths):
        if isinstance(changes.get("result"), dict):
            changes["result"] = dict(changes["result"])
        changes.setdefault("updated_at", utc_now())
        updated = replace(claim, status=status, **changes)
        return _store_publication_record(paths, updated, expected_claim_id=claim.claim_id)


@dataclass(frozen=True)
class _Acquisition:
    decision: str  # "owner" | "joined"
    claim: _PublicationRecord
    req_hard: int
    req_external: int
    successor: bool


async def _acquire_or_join(
    paths: CoherencePaths,
    *,
    refresh_kind: str,
    snapshot_dir: str | os.PathLike[str],
    sleep: Callable[[float], Awaitable[Any]],
    poll_seconds: float,
    max_wait_seconds: float,
    cancel_check: Callable[[], Any] | None,
    fs_ops: DurableFileOps | None,
    require_new_fetch: bool = False,
) -> _Acquisition:
    """The claim acquisition / join / successor-serialization loop (§7.1).

    No hidden unbounded queue: waiters poll with bounded cancellation-aware
    local sleeps OUTSIDE every mutex, re-reading manifest/claim state.
    """

    if refresh_kind not in REFRESH_KINDS:
        raise RefreshClaimError(f"unknown refresh_kind {refresh_kind!r}")
    deadline = time.monotonic() + max_wait_seconds
    successor = False
    while True:
        _check_cancel(cancel_check)
        if time.monotonic() > deadline:
            raise RefreshClaimError(
                "timed out waiting for the active refresh publication",
            )
        manifest = _ensure_manifest(paths, snapshot_dir=snapshot_dir)
        async with coherence_lock_async(paths):
            manifest = read_manifest(paths, required=True)
            assert manifest is not None
            dirt = read_external_dirt(paths)
            assert dirt is not None
            req_hard, req_external = _required_generations(manifest, dirt)
            claim = _read_publication_record(paths)

            if claim is None or claim.is_terminal:
                # Acquire: capture the initiation cut in ONE mutex-held store,
                # before settle/fetch. Compat/checkpoint clear no generations.
                cut = RefreshCutToken(
                    account_scope_id=manifest.account_scope_id,
                    epoch_number=manifest.epoch_number,
                    epoch_id=manifest.epoch_id,
                    journal_head_seq=manifest.journal_head_seq,
                    hard_barrier_generation=manifest.dirty_status.generation,
                    external_dirt_generation=dirt.generation,
                    captured_at=utc_now(),
                ) if refresh_kind in FULL_REFRESH_KINDS else None
                now = utc_now()
                new_claim = _PublicationRecord(
                    account_scope_id=manifest.account_scope_id,
                    claim_id=new_claim_id(),
                    refresh_kind=refresh_kind,
                    status="claimed",
                    cut_token=cut,
                    requested_hard_generation=req_hard,
                    requested_external_generation=req_external,
                    target_epoch_number=manifest.epoch_number + 1,
                    target_epoch_id=new_epoch_id(),
                    started_at=now,
                    updated_at=now,
                    result=None,
                    candidate_snapshot_path=None,
                )
                _store_publication_record(paths, new_claim)
                return _Acquisition("owner", new_claim, req_hard, req_external, successor)

            if (require_new_fetch or refresh_kind == COMPAT_PATCH_KIND
                    or claim.refresh_kind == COMPAT_PATCH_KIND):
                # A final-completion request never joins an older publisher,
                # whatever its cut covers. Wait outside locks, then own fetch.
                pass
            elif claim.refresh_kind in FULL_REFRESH_KINDS and _cut_covers(claim.cut_token, req_hard, req_external):
                return _Acquisition("joined", claim, req_hard, req_external, successor)
        # Outside the mutex: bounded local sleep, then re-evaluate.
        successor = True
        await sleep(poll_seconds)


# ---------------------------------------------------------------------------
# Full refresh (S02 §7)
# ---------------------------------------------------------------------------


# @beacon[
#   id=coherence@refresh-authority,
#   slice_labels=ra-workflowy-cache,carto-governance,mci-epoch-authority,
#   kind=ast,
#   comment=One in-process full-refresh authority: covered joins share one fetch; require_new_fetch waits for its own successor; initiation cuts and exact-token publication retain INV-6.,
# ]
async def run_full_refresh(
    *,
    paths: CoherencePaths,
    fetch: Callable[[], Awaitable[Mapping[str, Any]]],
    refresh_kind: str = "system",
    settle_seconds: float = DEFAULT_SETTLE_SECONDS,
    snapshot_dir: str | os.PathLike[str],
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    max_wait_seconds: float = DEFAULT_MAX_WAIT_SECONDS,
    cancel_check: Callable[[], Any] | None = None,
    fs_ops: DurableFileOps | None = None,
    require_new_fetch: bool = False,
) -> RefreshOutcome:
    """Run (or join) the in-process authoritative full refresh."""

    if refresh_kind == COMPAT_PATCH_KIND:
        raise RefreshClaimError("run_full_refresh does not accept compat_patch")
    acquisition = await _acquire_or_join(
        paths,
        refresh_kind=refresh_kind,
        snapshot_dir=snapshot_dir,
        sleep=sleep,
        poll_seconds=poll_seconds,
        max_wait_seconds=max_wait_seconds,
        cancel_check=cancel_check,
        fs_ops=fs_ops,
        require_new_fetch=require_new_fetch,
    )
    if acquisition.decision == "joined":
        return await _join_refresh(
            paths,
            joined=acquisition.claim,
            fetch=fetch,
            refresh_kind=refresh_kind,
            settle_seconds=settle_seconds,
            snapshot_dir=snapshot_dir,
            sleep=sleep,
            poll_seconds=poll_seconds,
            max_wait_seconds=max_wait_seconds,
            cancel_check=cancel_check,
            fs_ops=fs_ops,
        )
    return await _own_full_refresh(
        paths,
        claim=acquisition.claim,
        fetch=fetch,
        settle_seconds=settle_seconds,
        snapshot_dir=snapshot_dir,
        sleep=sleep,
        fs_ops=fs_ops,
        successor=acquisition.successor,
    )


async def _join_refresh(
    paths: CoherencePaths,
    *,
    joined: _PublicationRecord,
    fetch: Callable[[], Awaitable[Mapping[str, Any]]],
    refresh_kind: str,
    settle_seconds: float,
    snapshot_dir: str | os.PathLike[str],
    sleep: Callable[[float], Awaitable[Any]],
    poll_seconds: float,
    max_wait_seconds: float,
    cancel_check: Callable[[], Any] | None,
    fs_ops: DurableFileOps | None,
) -> RefreshOutcome:
    """Join the active covering refresh: poll to terminal, re-evaluate,
    adopt the winner's lineage locally, or become the successor owner."""

    deadline = time.monotonic() + max_wait_seconds
    claim_id = joined.claim_id
    while True:
        _check_cancel(cancel_check)
        if time.monotonic() > deadline:
            raise RefreshClaimError(
                "timed out joining the active refresh publication",
            )
        current = _read_publication_record(paths)
        terminal = current is not None and current.claim_id == claim_id and current.is_terminal
        if not terminal:
            if current is None or current.claim_id != claim_id:
                # Token replaced: re-evaluate coverage rather than assuming
                # failure (§5.3) — re-enter the acquisition loop.
                break
            await sleep(poll_seconds)
            continue
        assert current is not None
        if current.status == "completed":
            # Winner completed: adopt the new lineage locally (real adoption).
            manifest = read_manifest(paths, required=True)
            assert manifest is not None
            if manifest.epoch_id != current.target_epoch_id:
                # Terminal claim disagrees with the manifest: re-evaluate.
                break
            view = materialize_current_payload(paths, manifest)
            result = current.result or {}
            return RefreshOutcome(
                status="joined",
                refresh_kind=refresh_kind,
                claim_id=claim_id,
                epoch_number=view.epoch_number,
                epoch_id=view.epoch_id,
                previous_epoch_id=manifest.previous_epoch_id,
                base_snapshot_path=view.base_snapshot_path,
                incorporated_through_seq=view.incorporated_through_seq,
                journal_head_seq=view.adopted_sequence,
                adopted_sequence=view.adopted_sequence,
                hard_barrier_cleared=bool(result.get("hard_barrier_cleared")),
                external_dirt_cleared=bool(result.get("external_dirt_cleared")),
                external_dirt_clear_failed=bool(result.get("external_dirt_clear_failed")),
                deduplicated=True,
                successor_refresh_performed=False,
                settle_seconds=settle_seconds,
                node_count=len(view.payload.get("nodes", [])),
                snapshot_timestamp=view.snapshot_timestamp,
                nodes_export=view.payload,
            )
        # Winner failed: this caller becomes a successor owner.
        break

    # Re-enter the full acquisition/publish flow as a potential successor
    # owner, carrying the REAL fetch callback and remaining wait budget.
    return await run_full_refresh(
        paths=paths,
        fetch=fetch,
        refresh_kind=refresh_kind,
        settle_seconds=settle_seconds,
        snapshot_dir=snapshot_dir,
        sleep=sleep,
        poll_seconds=poll_seconds,
        max_wait_seconds=max(1.0, deadline - time.monotonic()),
        cancel_check=cancel_check,
        fs_ops=fs_ops,
    )


async def _own_full_refresh(
    paths: CoherencePaths,
    *,
    claim: _PublicationRecord,
    fetch: Callable[[], Awaitable[Mapping[str, Any]]],
    settle_seconds: float,
    snapshot_dir: str | os.PathLike[str],
    sleep: Callable[[float], Awaitable[Any]],
    fs_ops: DurableFileOps | None,
    successor: bool,
) -> RefreshOutcome:
    """The owning full refresh: settle → fetch → materialize → publish."""

    try:
        # 1. Cut is ALREADY captured in the memory record. Mark
        # settling, wait the configurable delay, and only then fetch — any
        # signal arriving during the delay post-dates the cut and survives.
        claim = await _write_claim_status(paths, claim, "settling", fs_ops=fs_ops)
        await sleep(max(0.0, float(settle_seconds)))

        # 2. Private fetch: never clears/mutates any published cache.
        claim = await _write_claim_status(paths, claim, "downloading", fs_ops=fs_ops)
        raw = await fetch()
        if not isinstance(raw, Mapping) or not isinstance(raw.get("nodes"), list):
            raise CoherenceError(
                "refresh fetch returned an invalid /nodes-export payload shape"
            )

        # 3. Canonicalize through the S01 FULL_EXPORT_REPLACE codec (the
        # named one-layer ingestion authority).
        canonical = canonicalize_nodes_export(raw, profile=IngestionProfile.FULL_EXPORT_REPLACE)

        # 4. Capture the incorporation head under the mutex: validate the
        # claim token, validate the journal, recover orphans, capture the
        # head and immutable copies of the active record range.
        claim = await _write_claim_status(paths, claim, "materializing", fs_ops=fs_ops)
        async with coherence_lock_async(paths):
            manifest = read_manifest(paths, required=True)
            assert manifest is not None
            if claim.cut_token is None or manifest.epoch_id != claim.cut_token.epoch_id:
                raise RefreshClaimError(
                    "refresh basis epoch changed before materialization",
                    path=paths.manifest_path,
                )
            _validate_committed_range(paths, manifest)
            manifest = _recover_orphans_locked(paths, manifest, fs_ops=fs_ops)
            incorporation_head = manifest.journal_head_seq
            floor = manifest.journal_floor_seq
            records = read_journal_records(paths, floor, incorporation_head)
            basis_epoch_id = manifest.epoch_id

        # 5. Replay the captured records onto the snapshot OUTSIDE the mutex
        # (safe whether or not the export already includes them).
        materialized = materialize_payload_with_records(
            canonical,
            records,
            start_sequence=floor,
            account_scope_id=manifest.account_scope_id,
        )

        # 6. Serialize the candidate fully (fsync, atomic place) BEFORE the
        # final publication mutex is acquired.
        candidate_path = write_snapshot_candidate(
            new_snapshot_candidate_path(snapshot_dir),
            materialized,
            account_scope_id=manifest.account_scope_id,
            epoch_number=claim.target_epoch_number or (manifest.epoch_number + 1),
            epoch_id=str(claim.target_epoch_id),
            incorporated_through_seq=incorporation_head,
            publication_kind="full_refresh",
            fs_ops=fs_ops,
        )
        claim = await _write_claim_status(
            paths,
            claim,
            "publishing",
            candidate_snapshot_path=candidate_path,
            fs_ops=fs_ops,
        )

        # 7. Publish through the single epoch authority (exact-generation
        # clearing of barrier/external dirt covered by the cut).
        cut = claim.cut_token
        publication = await publish_epoch(
            paths=paths,
            claim_id=claim.claim_id,
            basis_epoch_id=basis_epoch_id,
            candidate_snapshot_path=candidate_path,
            incorporated_through_seq=incorporation_head,
            publication_kind="full_refresh",
            refresh_cut=cut,
            clear_hard_through_generation=None if cut is None else cut.hard_barrier_generation,
            clear_external_through_generation=None if cut is None else cut.external_dirt_generation,
            fs_ops=fs_ops,
        )

        # 7b. S04 §10.7 (A5): physically retire the journal records this
        # cut just incorporated (their physical deletion was deferred to
        # S04).  Warning-only — the new epoch is already authoritative.
        retirement_warnings: list[str] = []
        try:
            _retired, retirement_warnings = retire_incorporated_journal_records(
                paths, fs_ops=fs_ops
            )
        except Exception as exc:  # noqa: BLE001 — warning only
            retirement_warnings = [f"journal retirement failed: {exc}"]

        # 8. Construct the refreshing process's current cache: new base +
        # any surviving tail through the final manifest head.
        final_manifest = read_manifest(paths, required=True)
        assert final_manifest is not None
        view = materialize_current_payload(paths, final_manifest)

        outcome = RefreshOutcome(
            status="published",
            refresh_kind=claim.refresh_kind,
            claim_id=claim.claim_id,
            epoch_number=publication.epoch_number,
            epoch_id=publication.epoch_id,
            previous_epoch_id=publication.previous_epoch_id,
            base_snapshot_path=publication.base_snapshot_path,
            incorporated_through_seq=publication.incorporated_through_seq,
            journal_head_seq=publication.journal_head_seq,
            adopted_sequence=view.adopted_sequence,
            hard_barrier_cleared=publication.hard_barrier_cleared,
            external_dirt_cleared=publication.external_dirt_cleared,
            external_dirt_clear_failed=publication.external_dirt_clear_failed,
            deduplicated=False,
            successor_refresh_performed=successor,
            settle_seconds=float(settle_seconds),
            node_count=len(view.payload.get("nodes", [])),
            snapshot_timestamp=view.snapshot_timestamp,
            nodes_export=view.payload,
            details=(
                {"journal_retirement_warnings": list(retirement_warnings)}
                if retirement_warnings
                else None
            ),
        )
        result_payload = outcome.to_result_dict()
        await _write_claim_status(
            paths, claim, "completed", result=result_payload, fs_ops=fs_ops
        )
        return outcome
    except BaseException as exc:
        await _terminalize_failed(paths, claim, exc, fs_ops=fs_ops)
        raise


async def _terminalize_failed(
    paths: CoherencePaths,
    claim: _PublicationRecord,
    exc: BaseException,
    *,
    fs_ops: DurableFileOps | None,
) -> None:
    """Best-effort terminal failure write; never masks the primary error."""

    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
        return
    try:
        await _write_claim_status(
            paths,
            claim,
            "failed",
            result={
                "error": str(exc)[:500],
                "error_type": type(exc).__name__,
            },
            fs_ops=fs_ops,
        )
    except Exception:  # noqa: BLE001 — never mask the primary error
        pass


# ---------------------------------------------------------------------------
# Transitional compat patch (S02 §12) — the mini epoch publication bridge
# ---------------------------------------------------------------------------


# @beacon[
#   id=coherence@compat-patch,
#   slice_labels=ra-workflowy-cache,carto-governance,mci-epoch-authority,
#   kind=ast,
#   comment=S02 transitional mini epoch publication: same in-memory publication authority as full refresh (never deduped), materialize active base + journal through the captured head, all-or-none narrow name/note patches via the S01 effect reducer, publish E+1 clearing NO barrier/dirt generations, fail closed with refresh_required.,
# ]
async def run_compat_patch(
    *,
    paths: CoherencePaths,
    patches: list[Mapping[str, Any]],
    snapshot_dir: str | os.PathLike[str],
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    max_wait_seconds: float = DEFAULT_MAX_WAIT_SECONDS,
    cancel_check: Callable[[], Any] | None = None,
    fs_ops: DurableFileOps | None = None,
) -> RefreshOutcome:
    """The transitional narrow-patch bridge: a fail-closed mini epoch
    publication (S02 §12), retired by S16 when these updates become ordinary
    journaled effects."""

    acquisition = await _acquire_or_join(
        paths,
        refresh_kind=COMPAT_PATCH_KIND,
        snapshot_dir=snapshot_dir,
        sleep=sleep,
        poll_seconds=poll_seconds,
        max_wait_seconds=max_wait_seconds,
        cancel_check=cancel_check,
        fs_ops=fs_ops,
    )
    # compat_patch never joins; acquisition always returns the owner path.
    assert acquisition.decision == "owner"
    claim = acquisition.claim
    try:
        # 1. Capture lineage under the mutex: manifest, orphan recovery,
        #    head, and the active record range.
        async with coherence_lock_async(paths):
            manifest = read_manifest(paths, required=True)
            assert manifest is not None
            if manifest.base_snapshot_path is None:
                raise CoherenceStateMissingError(
                    "compat patch requires a manifest base snapshot; run a full "
                    "refresh first (refresh_required)",
                    path=paths.manifest_path,
                )
            _validate_committed_range(paths, manifest)
            manifest = _recover_orphans_locked(paths, manifest, fs_ops=fs_ops)
            head = manifest.journal_head_seq
            floor = manifest.journal_floor_seq
            records = read_journal_records(paths, floor, head)
            basis_epoch_id = manifest.epoch_id

        # 2. Materialize the active base + journal through the captured head
        #    OUTSIDE the mutex.
        envelope = load_manifest_base_envelope(paths, manifest)
        materialized = materialize_payload_with_records(
            envelope.nodes_export,
            records,
            start_sequence=floor,
            account_scope_id=manifest.account_scope_id,
        )

        # 3. Validate + apply the narrow name/note patches all-or-none
        #    through the S01 canonical codec / effect reducer.
        effects: list[SemanticEffect] = []
        targets: list[str] = []
        for patch in patches:
            if not isinstance(patch, Mapping):
                raise CoherenceError("compat patch: every patch must be an object")
            node_id = patch.get("id")
            if not isinstance(node_id, str) or not node_id.strip():
                raise CoherenceError("compat patch: every patch requires a nonblank id")
            node_id = str(node_id)
            unknown = sorted(set(patch) - {"id", "name", "note"})
            if unknown:
                raise CoherenceError(
                    f"compat patch: only narrow name/note patches are permitted ({unknown})"
                )
            mask: list[str] = []
            values: dict[str, Any] = {}
            for field in ("name", "note"):
                if field in patch:
                    mask.append(field)
                    # Legacy caller-input normalization, byte-compatibly:
                    # the pinned cascading dewhiten exactly as the retired
                    # lineage-blind patcher applied it.
                    values[field] = _dewhiten_text(str(patch[field]))
            if not mask:
                raise CoherenceError("compat patch: a patch must change name or note")
            effects.append(SemanticEffect.node_fields(node_id, mask, values))
            targets.append(node_id)

        nodes_index = {
            str(node.get("id")): node
            for node in materialized.get("nodes", [])
            if isinstance(node, Mapping) and node.get("id")
        }
        missing = [node_id for node_id in targets if node_id not in nodes_index]
        if missing:
            raise CoherenceError(
                f"compat patch: targets missing from the materialized lineage: {missing}"
            )
        patched_payload = apply_semantic_effects(
            materialized, effects, mode=ApplicationMode.PUBLICATION_STRICT
        )

        # 4. Serialize a NEW full snapshot candidate for epoch E+1.
        claim = await _write_claim_status(paths, claim, "materializing", fs_ops=fs_ops)
        candidate_path = write_snapshot_candidate(
            new_snapshot_candidate_path(snapshot_dir),
            patched_payload,
            account_scope_id=manifest.account_scope_id,
            epoch_number=claim.target_epoch_number or (manifest.epoch_number + 1),
            epoch_id=str(claim.target_epoch_id),
            incorporated_through_seq=head,
            publication_kind=COMPAT_PATCH_KIND,
            fs_ops=fs_ops,
        )
        claim = await _write_claim_status(
            paths,
            claim,
            "publishing",
            candidate_snapshot_path=candidate_path,
            fs_ops=fs_ops,
        )

        # 5. Publish through the single epoch authority — clearing NO
        #    barrier or external-dirt generations under any circumstances.
        publication = await publish_epoch(
            paths=paths,
            claim_id=claim.claim_id,
            basis_epoch_id=basis_epoch_id,
            candidate_snapshot_path=candidate_path,
            incorporated_through_seq=head,
            publication_kind=COMPAT_PATCH_KIND,
            refresh_cut=None,
            clear_hard_through_generation=None,
            clear_external_through_generation=None,
            fs_ops=fs_ops,
        )

        # 5b. S04 §10.7 (A5): retire incorporated journal records after a
        # successful mini-epoch cut as well (warning-only).
        retirement_warnings: list[str] = []
        try:
            _retired, retirement_warnings = retire_incorporated_journal_records(
                paths, fs_ops=fs_ops
            )
        except Exception as exc:  # noqa: BLE001 — warning only
            retirement_warnings = [f"journal retirement failed: {exc}"]

        # 6. Adopt the fully materialized new lineage locally.
        final_manifest = read_manifest(paths, required=True)
        assert final_manifest is not None
        view = materialize_current_payload(paths, final_manifest)

        outcome_details: dict[str, Any] = {
            "patched": len(targets),
            "patched_ids": list(targets),
            "missing_ids": [],
            "locked": True,
            "loaded_latest": envelope.path,
            "snapshot_path": candidate_path,
        }
        if retirement_warnings:
            outcome_details["journal_retirement_warnings"] = list(retirement_warnings)

        outcome = RefreshOutcome(
            status="published",
            refresh_kind=COMPAT_PATCH_KIND,
            claim_id=claim.claim_id,
            epoch_number=publication.epoch_number,
            epoch_id=publication.epoch_id,
            previous_epoch_id=publication.previous_epoch_id,
            base_snapshot_path=publication.base_snapshot_path,
            incorporated_through_seq=publication.incorporated_through_seq,
            journal_head_seq=publication.journal_head_seq,
            adopted_sequence=view.adopted_sequence,
            hard_barrier_cleared=False,
            external_dirt_cleared=False,
            external_dirt_clear_failed=False,
            deduplicated=False,
            successor_refresh_performed=acquisition.successor,
            settle_seconds=0.0,
            node_count=len(view.payload.get("nodes", [])),
            snapshot_timestamp=view.snapshot_timestamp,
            nodes_export=view.payload,
            details=outcome_details,
        )
        await _write_claim_status(
            paths,
            claim,
            "completed",
            result={"patched": len(targets), "epoch_id": publication.epoch_id},
            fs_ops=fs_ops,
        )
        return outcome
    except BaseException as exc:
        await _terminalize_failed(paths, claim, exc, fs_ops=fs_ops)
        raise
