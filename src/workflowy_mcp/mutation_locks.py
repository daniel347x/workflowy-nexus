"""One process-local typed-region ownership plane (N05).

Complete canonical sets are checked and installed synchronously. Ownership
may span network/governor waits, but registry bookkeeping never awaits or
performs I/O. Short contention waits on registered Events with conflict-FIFO
precedence and ONE lengthenable acquisition deadline; long-lived holders and
actual CARTO graph grants refuse with advice, never expire or kill ownership.

The sole production door is mutation_region_locks: ADOPT is the existing
operation boundary, then LOCK and view-identity/full-holder REVALIDATE. One
replacement grant is permitted under the original budget. First admission
may finalize execution's view; later/nested admissions use proof-only views.
Nested callers are the closed covered-or-fresh inventory in N05 §7.3 and its
HG2R5B/VA5ZZO extensions, not arbitrary upgrades of unrelated existing nodes.

mutation_node_locks is only the exact-node compatibility delegate. Typed
readers report actual held regions, never waiters; old UUID-key readers are
diagnostics, not ownership proofs. MUTATION_LOCK_STATE shares one logical
operation's hold references and successful wait accounting. New-operation
spawns clear it alongside the cache snapshot. No lock file, PID, TTL, task
spawn, polling loop, per-key lock, second registry or durable phase exists.
CARTO admission reserves a file footprint on the same entry later bound to
the job's graph grant. Q3 owns file footprints only; cooldown is non-owning
memory history, evaluated only when admission demands it.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import math
import os
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal
from uuid import uuid4

from .coherence.cache_view import CacheView, CacheViewPublisher
from .coherence.errors import CacheViewError
from .coherence.operation_cache import (
    OperationCacheSnapshot, _authority_identity, current_operation_cache, establish_admission_snapshot,
    rebase_during_admission,
)
from .models.config import ServerConfig
from .mutation_lease_governance import (
    MutationRegion, MutationRegionError, ReservedMutationParent,
    build_region_exact_node,
    normalize_mutation_regions, region_display_key, regions_intersect,
)

LOGGER = logging.getLogger(__name__)
__all__ = [
    "MUTATION_LOCK_STATE", "MutationRegionGrant", "RegistryMutationRegions",
    "MutationSerializationTimeoutError", "MutationSerializationConflictError",
    "mutation_region_locks", "mutation_node_locks", "held_mutation_lock_regions",
    "registry_held_regions", "held_mutation_lock_keys", "registry_held_keys",
]
_DEFAULT_ACQUIRE_TIMEOUT_SECONDS = float(
    ServerConfig.model_fields["mutation_lock_acquire_timeout_seconds"].default
)


# @beacon[
#   id=mutation-locks@timeout-error,
#   slice_labels=mutation-serialization,mci-lock-plane,
#   kind=ast,
#   comment=Unchanged typed timeout envelope and message for the one total region-acquisition budget; retry_after remains twice the governor interval and contended keys are diagnostic displays; ownership is never expired.,
# ]
class MutationSerializationTimeoutError(RuntimeError):
    """The existing typed bounded-wait refusal, never a hold-duration limit."""

    code = "mutation_serialization_timeout"

    def __init__(self, *, retry_after: float, contended_keys: list[str], timeout_seconds: float) -> None:
        self.retry_after = float(retry_after)
        self.contended_keys = [str(key) for key in contended_keys]
        self.timeout_seconds = float(timeout_seconds)
        super().__init__(
            f"mutation serialization: timed out after {self.timeout_seconds:g}s "
            f"waiting for the node mutation lock on {', '.join(self.contended_keys)} "
            "(another operation is mid read-modify-write on the same node); "
            f"refusing rather than queueing — retry after {self.retry_after:g}s."
        )


class MutationSerializationConflictError(RuntimeError):
    """Immediate pre-send refusal against declared long ownership/claims."""

    code = "mutation_serialization_conflict"

    def __init__(
        self, message: str, *, retry_after: float, contended_keys: list[str],
        holder_kind: str, holder_id: str, reason: str,
    ) -> None:
        self.retry_after = float(retry_after)
        self.contended_keys = sorted(set(contended_keys))
        self.holder_kind, self.holder_id, self.reason = holder_kind, holder_id, reason
        super().__init__(message)


@dataclass(eq=False)
class _TaskLockState:
    """References to this logical operation's actual entries, not a second set."""

    operation_id: str
    owner: Any
    client: Any
    holds: list[_RegionHold] = field(default_factory=list)
    waits: list[tuple[str, float]] = field(default_factory=list)
    execution_finalized: bool = False
    depth: int = 0

    @property
    def held(self) -> frozenset[str]:
        return frozenset(r.key for h in self.holds if not h.released for r in h.regions if _real_graph_region(r))

    def serialization(self, since: int = 0) -> dict[str, Any] | None:
        events = self.waits[since:]
        if not events:
            return None
        return {
            "waited_seconds": round(sum(seconds for _, seconds in events), 3),
            "waited_on": sorted({key for key, _ in events}),
        }


# @beacon[
#   id=mutation-locks@state-contextvar,
#   slice_labels=mutation-serialization,mci-lock-plane,
#   kind=ast,
#   comment=Logical-operation state references actual typed holds and successful wait events; operation-id/cache authority prevent borrowing a foreign owner and isolated new-operation spawns clear this ContextVar beside the cache snapshot.,
# ]
MUTATION_LOCK_STATE: contextvars.ContextVar[_TaskLockState | None] = contextvars.ContextVar(
    "MUTATION_LOCK_STATE", default=None,
)


@dataclass(eq=False)
class _RegionHold:
    token: object
    state: _TaskLockState | None
    regions: tuple[MutationRegion, ...]
    client: Any
    hold_mode: Literal["short", "long_lived"]
    expected_release_at: float | None
    released: bool = False
    governance: _GovernanceHold | None = None


@dataclass
class _GovernanceHold:
    """File/admission metadata on an actual hold, never a second owner."""

    record_kind: Literal["carto", "q3"]
    jobs_root: str
    account_scope_id: str
    footprint: Mapping[str, Any]
    claim_id: str | None = None
    lease_id: str | None = None
    job_id: str | None = None
    job_file: str | None = None
    job_type: str | None = None
    mode: str | None = None
    dedupe_key: str | None = None
    launcher: str | None = None
    parent_job_id: str | None = None
    log_file: str | None = None
    freshness_disposition: str | None = None
    finalized: bool = False


@dataclass(eq=False)
class _RegionWaiter:
    ticket: int
    state: _TaskLockState
    regions: tuple[MutationRegion, ...]
    snapshot: OperationCacheSnapshot
    deadline: float
    event: asyncio.Event = field(default_factory=asyncio.Event)
    waits: list[tuple[str, float]] = field(default_factory=list)
    blockers: tuple[str, ...] = ()


# @beacon[
#   id=mutation-locks@unified-file-and-graph-registry,
#   slice_labels=mutation-serialization,mci-lock-plane,carto-governance,
#   kind=ast,
#   comment=The one active hold collection: CARTO admission file metadata becomes the same job graph grant, Q3 entries own disk footprints only, and cooldown is non-owning memory history. No process owner, expiry or reaper.,
# ]
@dataclass
class _RegionRegistry:
    holders: dict[object, _RegionHold] = field(default_factory=dict)
    waiters: list[_RegionWaiter] = field(default_factory=list)
    next_ticket: int = 0
    cooldowns: dict[tuple[str, str, str], Mapping[str, Any]] = field(default_factory=dict)


_LOCKS = _RegionRegistry()


def _normalized_governance_root(path) -> str:
    """Normalize an existing root/path value without touching the filesystem."""
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _immutable_footprint(footprint) -> Mapping[str, Any]:
    """Validate the retained geometry and detach its only nested sequence."""
    from .carto_job_governance import validate_footprint
    value = validate_footprint(footprint)
    return MappingProxyType({**value, "ancestor_chain": tuple(value["ancestor_chain"])})


def _project_governance_hold(metadata) -> Mapping[str, Any]:
    """Return immutable diagnostic values, never the actual registry entry."""
    value = {"record_kind": metadata.record_kind, "jobs_root": metadata.jobs_root,
             "account_scope_id": metadata.account_scope_id, **metadata.footprint}
    if metadata.record_kind == "q3":
        value["lease_id"] = metadata.lease_id
    else:
        value.update({key: getattr(metadata, key) for key in (
            "claim_id", "job_id", "job_file", "job_type", "mode", "dedupe_key",
            "launcher", "parent_job_id", "log_file", "freshness_disposition", "finalized",
        )})
    return MappingProxyType(value)


def _governance_holds(jobs_root=None) -> tuple[Mapping[str, Any], ...]:
    """Snapshot actual file holds, filtering root only, never account scope."""
    root = _normalized_governance_root(jobs_root) if jobs_root is not None else None
    return tuple(_project_governance_hold(hold.governance)
                 for hold in _LOCKS.holders.values()
                 if not hold.released and hold.governance is not None
                 and (root is None or hold.governance.jobs_root == root))


def _reserve_carto_hold(*, jobs_root, account_scope_id, footprint, job_id, job_file,
                        job_type, mode, dedupe_key, launcher, parent_job_id=None,
                        log_file=None, freshness_disposition=None, claim_id=None):
    """Install the empty-graph reservation before initial job publication."""
    from .carto_job_governance import _validate_hex
    metadata = _GovernanceHold(
        "carto", _normalized_governance_root(jobs_root),
        _validate_hex(account_scope_id, 32, "account_scope_id"),
        _immutable_footprint(footprint),
        claim_id=_validate_hex(claim_id or uuid4().hex, 32, "claim_id"),
        job_id=str(job_id), job_file=_normalized_governance_root(job_file),
        job_type=str(job_type), mode=mode, dedupe_key=dedupe_key, launcher=launcher,
        parent_job_id=parent_job_id, log_file=str(log_file) if log_file is not None else None,
        freshness_disposition=freshness_disposition,
    )
    hold = _RegionHold(object(), None, (), None, "long_lived", None, governance=metadata)
    _LOCKS.holders[hold.token] = hold
    return _project_governance_hold(metadata)


def _acquire_q3_holds(targets, *, account_scope_id, jobs_root, lease_id=None):
    """Validate the entire group and all blockers before inserting file holds."""
    from .carto_job_governance import (
        _validate_hex, derive_region_key, footprints_overlap,
        GovernanceConflictError, Q3LeaseAcquisitionError,
    )
    account = _validate_hex(account_scope_id, 32, "account_scope_id")
    footprints = tuple(_immutable_footprint(target) for target in targets)
    if not footprints:
        return []
    label = _validate_hex(lease_id or uuid4().hex, 32, "lease_id")
    root = _normalized_governance_root(jobs_root)
    keys = [derive_region_key(fp["path"], fp["root_uuid"]) for fp in footprints]
    if len(keys) != len(set(keys)):
        raise Q3LeaseAcquisitionError(
            "Q3 lease acquisition contains duplicate actionable targets; zero leases were created.")
    active_holds = _governance_holds(root)
    for footprint in footprints:
        candidate = {"account_scope_id": account, **footprint}
        for active in active_holds:
            if footprints_overlap(
                {**candidate, "ancestor_chain": list(candidate["ancestor_chain"])},
                {**active, "ancestor_chain": list(active["ancestor_chain"])},
            ):
                owner = active.get("job_id") or active.get("lease_id")
                raise GovernanceConflictError(
                    f"Q3 target '{footprint.get('path') or footprint['root_uuid']}' overlaps active owner '{owner}'.",
                    conflict=dict(active),
                )
    receipts = []
    for footprint in footprints:
        metadata = _GovernanceHold("q3", root, account, footprint, lease_id=label)
        hold = _RegionHold(object(), None, (), None, "short", None, governance=metadata)
        _LOCKS.holders[hold.token] = hold
        receipts.append({"lease_id": label, "account_scope_id": account,
                         **footprint, "ancestor_chain": list(footprint["ancestor_chain"])})
    return receipts


def _release_q3_holds(leases, *, jobs_root) -> None:
    """Remove only the exact acquisition/footprint; stale receipts are inert."""
    root = _normalized_governance_root(jobs_root)
    for receipt in leases:
        footprint = _immutable_footprint(receipt)
        for token, hold in tuple(_LOCKS.holders.items()):
            metadata = hold.governance
            if (metadata is not None and metadata.record_kind == "q3"
                    and metadata.jobs_root == root
                    and metadata.account_scope_id == receipt.get("account_scope_id")
                    and metadata.lease_id == receipt.get("lease_id")
                    and metadata.footprint == footprint):
                _release_region_hold(token, None)


def _finalize_carto_hold(claim_id, jobs_root, *, write_tombstone=True, now_epoch=None) -> bool:
    """Finalize once; publish non-owning cooldown before the grant's release."""
    from .carto_job_governance import build_tombstone_from_claim, derive_region_key, _parse_time
    root = _normalized_governance_root(jobs_root)
    for hold in tuple(_LOCKS.holders.values()):
        metadata = hold.governance
        if (metadata is None or metadata.record_kind != "carto"
                or metadata.jobs_root != root or metadata.claim_id != claim_id):
            continue
        if metadata.finalized:
            return True
        if write_tombstone:
            projected = _project_governance_hold(metadata)
            record = build_tombstone_from_claim(
                {**projected, "ancestor_chain": list(projected["ancestor_chain"])}, now_epoch=now_epoch)
            record = MappingProxyType({**record, "ancestor_chain": tuple(record["ancestor_chain"])})
            key = (root, metadata.account_scope_id,
                   derive_region_key(metadata.footprint["path"], metadata.footprint["root_uuid"]))
            previous = _LOCKS.cooldowns.get(key)
            if previous is None or _parse_time(previous["last_terminal_at"]) <= _parse_time(record["last_terminal_at"]):
                _LOCKS.cooldowns[key] = record
        metadata.finalized = True
        if hold.state is None:
            _release_region_hold(hold.token, None)
        return True
    return True


def _governance_cooldowns(jobs_root, *, now_epoch, cooldown_seconds):
    """Q9: prune elapsed history only at an admission demand; no live expiry."""
    from .carto_job_governance import _parse_time
    root = _normalized_governance_root(jobs_root)
    for key, record in tuple(_LOCKS.cooldowns.items()):
        if key[0] == root and max(0.0, float(now_epoch) - _parse_time(record["last_terminal_at"])) >= cooldown_seconds:
            del _LOCKS.cooldowns[key]
    return tuple(value for key, value in sorted(_LOCKS.cooldowns.items()) if key[0] == root)


def _carto_hold_protection(jobs_root=None):
    """Immutable history-GC ids/paths include pending and still-bound jobs."""
    holds = tuple(value for value in _governance_holds(jobs_root) if value["record_kind"] == "carto")
    return frozenset(value["job_id"] for value in holds), frozenset(value["job_file"] for value in holds)


def _require_carto_hold(job_file, claim_id):
    """Resolve the actual exact reservation; validation creates no authority."""
    from .carto_job_governance import GovernanceConflictError
    path = _normalized_governance_root(job_file)
    for hold in _LOCKS.holders.values():
        metadata = hold.governance
        if (metadata is not None and metadata.record_kind == "carto"
                and metadata.job_file == path and metadata.claim_id == claim_id
                and not metadata.finalized and not hold.released):
            return hold
    raise GovernanceConflictError("Admitted daemon task no longer owns its exact claim token.")


def _reset_carto_region_binding(hold, state) -> None:
    """Undo a retry's graph binding without interrupting its file reservation."""
    if _LOCKS.holders.get(hold.token) is not hold or hold.state is not state:
        return
    if state is not None:
        state.holds.remove(hold)
    hold.state, hold.client, hold.regions = None, None, ()
    hold.expected_release_at = None
    _wake_region_waiters()


@dataclass(frozen=True)
class RegistryMutationRegions:
    regions: tuple[MutationRegion, ...]
    view: CacheView | None
    authority_consistent: bool


@dataclass(frozen=True)
class _RegionAcquireResult:
    hold: _RegionHold | None
    regions: tuple[MutationRegion, ...]
    snapshot: OperationCacheSnapshot
    waits: tuple[tuple[str, float], ...] = ()


class MutationRegionGrant:
    """Read-only receipt; the orchestrator's context finally owns release.

    No public owner/token injection or release-by-key API. A covered grant
    has no token and cannot remove an outer hold. N07/N08 retain this context
    through their own job or SENT→JOURNALED lifetime.
    """

    __slots__ = ("_hold", "_state", "_regions", "_snapshot", "_mode", "_waits", "_active")

    def __init__(self) -> None:
        raise TypeError("MutationRegionGrant is issued only by mutation_region_locks")

    @property
    def token(self) -> object | None:
        return self._hold.token if self._hold is not None else None

    @property
    def regions(self) -> tuple[MutationRegion, ...]:
        return self._regions

    @property
    def snapshot(self) -> OperationCacheSnapshot:
        return self._snapshot

    @property
    def hold_mode(self) -> str:
        return self._mode

    @property
    def waited_seconds(self) -> float:
        return round(sum(seconds for _, seconds in self._waits), 3)

    @property
    def waited_on(self) -> list[str]:
        return sorted({key for key, _ in self._waits})

    @property
    def is_active(self) -> bool:
        return self._active and (self._hold is None or not self._hold.released)


def _acquire_timeout_seconds() -> float:
    try:
        from .config import get_server_config
        return float(get_server_config().mutation_lock_acquire_timeout_seconds)
    except Exception:
        return _DEFAULT_ACQUIRE_TIMEOUT_SECONDS


def _governor_interval_seconds() -> float:
    try:
        from .config import get_server_config
        return float(get_server_config().write_governor.interval_seconds)
    except Exception:
        from .carto_write_governor import DEFAULT_WRITE_INTERVAL_SECONDS
        return float(DEFAULT_WRITE_INTERVAL_SECONDS)


def _long_lived_retry_after_seconds() -> float:
    default = float(ServerConfig.model_fields["mutation_lock_long_lived_retry_after_seconds"].default)
    try:
        from .config import get_server_config
        advice = float(get_server_config().mutation_lock_long_lived_retry_after_seconds)
        return advice if math.isfinite(advice) and advice > 0 else default
    except Exception:
        return default


def _real_graph_region(region: MutationRegion) -> bool:
    return region.kind != "ap_core" and not isinstance(region.key, ReservedMutationParent)


def _identity(regions: Iterable[MutationRegion]) -> tuple:
    return tuple((r.kind, r.key) for r in regions)


def _regions_cover_request(held, requested, *, cache_by_id) -> bool:
    """Identical identity or real subtree coverage, never mere intersection."""
    for requested_region in requested:
        if any(h.kind == requested_region.kind and h.key == requested_region.key for h in held):
            continue
        if _real_graph_region(requested_region) and any(
            h.kind == "subtree" and regions_intersect(h, requested_region, cache_by_id=cache_by_id)
            # A requested subtree is covered only in the containing direction.
            and (requested_region.kind != "subtree" or h.root_uuid in _current_chain(requested_region, cache_by_id))
            for h in held
        ):
            continue
        return False
    return True


def _current_chain(region, view):
    from .mutation_lease_governance import rehydrate_ancestor_chain
    return rehydrate_ancestor_chain(region.key, view)


def _scan_region_blockers(regions, *, snapshot, state, exclude_token=None, waiter=None) -> tuple[str, ...]:
    """Scan every actual other holder/claim on this proof view.

    Complete current chains are reused only in this call's local memo. Every
    decision and every advanced-view rescan starts empty; no topology state
    survives a scan or authorizes a later decision (75JCT1 / N05 §6.1).
    """
    chain_memo: dict[str, tuple[str, ...]] = {}
    blockers: set[str] = set()
    for token, hold in _LOCKS.holders.items():
        if token is exclude_token or hold.state is state:
            continue
        overlaps = [r for r in regions if any(
            regions_intersect(r, other, cache_by_id=snapshot.nodes_by_id, _chain_memo=chain_memo) for other in hold.regions
        )]
        if not overlaps:
            continue
        keys = sorted({region_display_key(r) for r in overlaps})
        if hold.hold_mode == "long_lived":
            remaining = (hold.expected_release_at or 0) - asyncio.get_running_loop().time()
            advice = remaining if remaining > 0 else _long_lived_retry_after_seconds()
            holder_id = hold.state.operation_id
            raise MutationSerializationConflictError(
                f"Mutation region is held by long-lived operation '{holder_id}'; no write was attempted. "
                f"Retry after about {advice:g}s; the holder may still be running.",
                retry_after=advice, contended_keys=keys, holder_kind="long_lived",
                holder_id=holder_id, reason="long_lived_holder",
            )
        blockers.update(keys)
    return tuple(sorted(blockers))


def _wake_region_waiters() -> None:
    for waiter in _LOCKS.waiters:
        waiter.event.set()


def _release_region_hold(token, state) -> None:
    hold = _LOCKS.holders.get(token)
    if hold is None or hold.token is not token or hold.state is not state:
        return
    hold.released = True
    del _LOCKS.holders[token]
    if state is not None:
        state.holds.remove(hold)
    _wake_region_waiters()


async def _wait_for_region_change(waiter, *, deadline, timeout_seconds) -> None:
    remaining = deadline - asyncio.get_running_loop().time()
    try:
        await asyncio.wait_for(waiter.event.wait(), timeout=max(remaining, 0.0))
    except (asyncio.TimeoutError, TimeoutError):
        raise MutationSerializationTimeoutError(
            retry_after=2 * _governor_interval_seconds(), contended_keys=list(waiter.blockers),
            timeout_seconds=timeout_seconds,
        ) from None


# @beacon[
#   id=mutation-locks@acquire-region-set,
#   role=_acquire_region_set: sole raw all-or-none acquisition primitive,
#   slice_labels=mutation-serialization,mci-lock-plane,
#   kind=ast,
#   comment=Current proof before every blocker decision and wake; all-or-none graph additions use the existing ticket/Event/deadline. A job binds its existing file reservation rather than creating duplicate ownership; nested coverage and conflict-FIFO stay unchanged.,
# ]
async def _acquire_region_set(
    regions, *, snapshot, state, deadline, timeout_seconds, hold_mode, expected_release_at,
    refresh_proof, reservation=None,
) -> _RegionAcquireResult:
    waiter = None
    try:
        while True:
            regions, snapshot = await refresh_proof(regions, snapshot)
            if waiter is not None:
                waiter.regions, waiter.snapshot = regions, snapshot
            held = tuple(r for h in state.holds for r in h.regions)
            additions = tuple(r for r in regions if not _regions_cover_request(held, (r,), cache_by_id=snapshot.nodes_by_id))
            blockers = set(_scan_region_blockers(regions, snapshot=snapshot, state=state, waiter=waiter))
            # The caller inventory authorizes only covered or fresh nested
            # additions. Never wait behind tickets which may depend on us.
            if additions and not state.holds:
                for earlier in _LOCKS.waiters:
                    if earlier is waiter:
                        break
                    try:
                        blockers.update(region_display_key(r) for r in regions if any(
                            regions_intersect(r, pending, cache_by_id=snapshot.nodes_by_id)
                            for pending in earlier.regions
                        ))
                    except MutationRegionError:
                        # A pending request is not ownership. Wake it to prove
                        # its own topology, never refuse unrelated work for it.
                        earlier.event.set()
            if not blockers:
                hold = None
                if additions:
                    if reservation is not None:
                        hold = _require_carto_hold(
                            reservation.governance.job_file, reservation.governance.claim_id)
                        hold.state, hold.client, hold.regions = state, state.client, additions
                        hold.hold_mode, hold.expected_release_at = hold_mode, expected_release_at
                    else:
                        hold = _RegionHold(object(), state, additions, state.client, hold_mode, expected_release_at)
                        _LOCKS.holders[hold.token] = hold
                    state.holds.append(hold)
                return _RegionAcquireResult(hold, regions, snapshot, tuple(waiter.waits) if waiter else ())
            if waiter is None:
                waiter = _RegionWaiter(_LOCKS.next_ticket, state, regions, snapshot, deadline)
                _LOCKS.next_ticket += 1
                _LOCKS.waiters.append(waiter)
            waiter.blockers = tuple(sorted(blockers))
            waiter.event.clear()
            started = asyncio.get_running_loop().time()
            await _wait_for_region_change(waiter, deadline=deadline, timeout_seconds=timeout_seconds)
            elapsed = max(asyncio.get_running_loop().time() - started, 0.0)
            waiter.waits.extend((key, elapsed if index == 0 else 0.0) for index, key in enumerate(waiter.blockers))
    finally:
        if waiter is not None:
            _LOCKS.waiters.remove(waiter)
            _wake_region_waiters()


def _public_region_error(exc: MutationRegionError) -> MutationRegionError:
    advice = 2 * _governor_interval_seconds()
    if exc.reason == "unresolvable_region":
        region = exc.region
        display = region_display_key(region) if isinstance(region, MutationRegion) else str(region or "unknown")[:8]
        message = (
            f"Cannot place mutation region '{display}' in the current cache view; no write was attempted. "
            f"Run system.cache.refresh, then retry after {advice:g}s. Refusing unproved containment "
            "prevents one MCP writer silently overwriting another."
        )
    elif exc.reason == "unstable_topology":
        message = (
            "Mutation topology changed again during admission; no write was attempted. "
            f"Re-read the target and retry after {advice:g}s. Refusing a stale region prevents "
            "one MCP writer silently overwriting another."
        )
    else:
        message = (
            f"Cannot acquire a typed mutation region ({exc}); no write was attempted. Resolve "
            "non-special handles to full UUIDs and retry. Refusing an unproved region prevents "
            "one MCP writer silently overwriting another."
        )
    return MutationRegionError(message, reason=exc.reason, retry_after=advice, region=exc.region)


# @beacon[
#   id=mutation-locks@mutation-region-locks,
#   role=mutation_region_locks: sole production ADOPT LOCK REVALIDATE door,
#   slice_labels=mutation-serialization,mci-lock-plane,
#   kind=ast,
#   comment=Derives pure typed factories on current proof views before initial derivation and every raw blocker decision or wake; post-grant view-identity advance rescans actual typed holders with at most one replacement under the original budget. A job's admitted file reservation becomes this same graph-hold entry; retries preserve its file footprint and finalization precedes exact release.,
# ]
@asynccontextmanager
async def mutation_region_locks(
    *, client, operation_label: str,
    region_factory: Callable[[OperationCacheSnapshot], Iterable[MutationRegion]],
    hold_mode: Literal["short", "long_lived"] = "short",
    expected_duration_seconds: float | None = None,
) -> AsyncIterator[MutationRegionGrant]:
    boundary = current_operation_cache(required=True)
    publisher = _authority_identity(client)
    if boundary.owner is not publisher or not isinstance(publisher, CacheViewPublisher):
        raise CacheViewError("typed-region acquisition requires this client's existing operation boundary")
    state = MUTATION_LOCK_STATE.get()
    if state is None or state.operation_id != boundary.operation_id or state.owner is not boundary.owner:
        state = _TaskLockState(boundary.operation_id, boundary.owner, client)
        MUTATION_LOCK_STATE.set(state)
    token = None
    reservation = None
    events = []
    try:
        if not state.depth and not state.execution_finalized:
            from .carto_job_runtime import current_carto_job_context
            context = current_carto_job_context()
            if context is not None:
                reservation = _require_carto_hold(context.job_file, context.claim_id)
        if hold_mode not in ("short", "long_lived"):
            raise MutationRegionError("mutation region: hold_mode must be short or long_lived")
        expected_release_at = None
        if hold_mode == "long_lived" and expected_duration_seconds is None:
            LOGGER.warning("Long-lived mutation hold has no duration estimate; using configured retry advice, never expiring ownership.")
        if expected_duration_seconds is not None:
            try:
                estimate = float(expected_duration_seconds)
                if not math.isfinite(estimate) or estimate <= 0 or isinstance(expected_duration_seconds, bool):
                    raise ValueError("invalid estimate")
                expected_release_at = asyncio.get_running_loop().time() + estimate
            except (ValueError, TypeError, OverflowError):
                LOGGER.warning("Invalid mutation hold-duration estimate; using configured retry advice, never expiring ownership.")
        async def refresh_proof(regions, proof):
            # Proof-only capture, not execution adoption/repinning. In the
            # unchanged case this coroutine neither suspends nor performs I/O.
            if publisher.capture() is proof.view:
                return regions, proof
            fresh = await rebase_during_admission(client, operation_label, install=False)
            return normalize_mutation_regions(region_factory(fresh)), fresh

        initial = await establish_admission_snapshot(client, operation_label)
        if state.depth or state.execution_finalized:
            initial = await rebase_during_admission(client, operation_label, install=False)
        regions, initial = await refresh_proof(None, initial)
        if regions is None:
            regions = normalize_mutation_regions(region_factory(initial))
        timeout_seconds = _acquire_timeout_seconds()
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        proof = initial
        for attempt in range(2):
            result = await _acquire_region_set(
                regions, snapshot=proof, state=state, deadline=deadline,
                timeout_seconds=timeout_seconds, hold_mode=hold_mode,
                expected_release_at=expected_release_at,
                refresh_proof=refresh_proof,
                reservation=reservation,
            )
            regions, proof = result.regions, result.snapshot
            token = result.hold.token if result.hold is not None else None
            events.extend(result.waits)
            if publisher.capture() is proof.view:
                break
            fresh = await rebase_during_admission(client, operation_label, install=False)
            requested = normalize_mutation_regions(region_factory(fresh))
            try:
                blockers = _scan_region_blockers(requested, snapshot=fresh, state=state, exclude_token=token)
                conflict = bool(blockers)
            except MutationSerializationConflictError:
                conflict = True
            changed = _identity(requested) != _identity(regions)
            if not changed and not conflict:
                proof, regions = fresh, requested
                if result.hold is not None:
                    # Preserve exact additions, not the whole covered request.
                    keys = set(_identity(result.hold.regions))
                    result.hold.regions = tuple(r for r in requested if (r.kind, r.key) in keys)
                break
            if token is not None:
                if reservation is not None and token is reservation.token:
                    _reset_carto_region_binding(reservation, state)
                else:
                    _release_region_hold(token, state)
                token = None
            if attempt:
                raise MutationRegionError("second relevant topology advance", reason="unstable_topology")
            proof, regions = fresh, requested
        if not state.depth and not state.execution_finalized:
            if current_operation_cache().view is not proof.view:
                await rebase_during_admission(client, operation_label, snapshot=proof, install=True)
            state.execution_finalized = True
        state.waits.extend(events)
        grant = object.__new__(MutationRegionGrant)
        grant._state = state
        grant._hold, grant._regions, grant._snapshot = result.hold, regions, proof
        grant._mode, grant._waits, grant._active = hold_mode, tuple(events), True
    except BaseException as exc:
        if token is not None:
            if reservation is not None and token is reservation.token:
                _reset_carto_region_binding(reservation, state)
            else:
                _release_region_hold(token, state)
        if isinstance(exc, MutationRegionError):
            raise _public_region_error(exc) from exc
        raise
    state.depth += 1
    try:
        yield grant
    finally:
        grant._active = False
        state.depth -= 1
        if token is not None:
            _release_region_hold(token, state)


# @beacon[
#   id=mutation-locks@acquire,
#   slice_labels=mutation-serialization,mci-lock-plane,
#   kind=ast,
#   comment=Exact-node compatibility delegate only: resolves no handles and owns no registry or wait logic; requires the same existing client boundary and enters the single typed-region orchestrator.,
# ]
@asynccontextmanager
async def mutation_node_locks(*uuids: str, client, operation_label: str = "mutation_node_locks") -> AsyncIterator[MutationRegionGrant]:
    async with mutation_region_locks(
        client=client, operation_label=operation_label,
        region_factory=lambda snapshot: tuple(build_region_exact_node(key, cache_by_id=snapshot.nodes_by_id) for key in uuids),
    ) as grant:
        yield grant


def held_mutation_lock_regions() -> tuple[MutationRegion, ...]:
    state = MUTATION_LOCK_STATE.get()
    if state is None:
        return ()
    unique = {(r.kind, r.key): r for hold in state.holds for r in hold.regions}
    return tuple(sorted(unique.values(), key=lambda r: r.sort_key()))


# @beacon[
#   id=mutation-locks@registry-held-regions,
#   role=registry_held_regions: immutable actual-holder observation,
#   slice_labels=mutation-serialization,mci-lock-plane,
#   kind=ast,
#   comment=Read-only actual graph-region projection; empty pending/Q3 file entries contribute no graph owner. Real graph holds share one publisher-current view; pending graph requests never appear.,
# ]
def registry_held_regions() -> RegistryMutationRegions:
    unique = {(r.kind, r.key): r for hold in _LOCKS.holders.values() for r in hold.regions}
    regions = tuple(sorted(unique.values(), key=lambda r: r.sort_key()))
    authorities = {hold.state.owner for hold in _LOCKS.holders.values() if any(_real_graph_region(r) for r in hold.regions)}
    if not authorities:
        return RegistryMutationRegions(regions, None, True)
    if len(authorities) != 1 or not all(isinstance(owner, CacheViewPublisher) for owner in authorities):
        return RegistryMutationRegions(regions, None, False)
    return RegistryMutationRegions(regions, next(iter(authorities)).capture(), True)


# @beacon[
#   id=mutation-locks@held-keys,
#   slice_labels=mutation-serialization,mci-lock-plane,
#   kind=ast,
#   comment=Read-only diagnostic UUID projection of this logical owner's actual holds; omits AP/special identities and never proves ownership or reports pending requests.,
# ]
def held_mutation_lock_keys() -> frozenset[str]:
    state = MUTATION_LOCK_STATE.get()
    return state.held if state is not None else frozenset()


# @beacon[
#   id=mutation-locks@registry-held-keys,
#   slice_labels=mutation-serialization,mci-lock-plane,
#   kind=ast,
#   comment=Direct diagnostic UUID projection of actual typed registry entries without a view capture; neither production drain nor CARTO admission uses it and AP/special-only holds are deliberately absent.,
# ]
def registry_held_keys() -> frozenset[str]:
    return frozenset(r.key for hold in _LOCKS.holders.values() for r in hold.regions if _real_graph_region(r))
