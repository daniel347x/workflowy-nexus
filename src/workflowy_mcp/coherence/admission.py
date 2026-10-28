"""Hard-stop admission gates and the pure external-dirt policy matrix (S04 §7/§8).

Two distinct states, never conflated (invariant 1):

- **Hard barrier** (``manifest.dirty_status.active``): coherence failure.
  Every coherence-sensitive mutation and every cache-derived identity proof
  REFUSES before any side effect through the ONE shared gates here
  (:func:`require_coherence_mutation_admission` /
  :func:`require_cache_identity_proof`), each a thin guard over the ONE
  hard-stop predicate (:func:`coherence.barrier.hard_stop_state`, H1/H4).
- **External dirt** (GUI edits observed out-of-band): stale-but-safe.  It
  changes only the few admission rows of the frozen pure matrix
  (:func:`admission_policy`, §8.1) — and S04 ships that matrix as a PURE
  authority only (A6): production consumer wiring is deferred to the named
  consumer sessions (S07/S10/S12–S14).
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from .barrier import CoherenceBoundaryStatus, hard_stop_state
from .errors import CoherenceBarrierActiveError
from .paths import CoherencePaths
from .schema import ExternalDirtState

# The shared gates accept either resolved CoherencePaths or a zero-argument
# resolver.  The lazy form is preferred at production call sites: path
# derivation touches client configuration, and it must only ever run when
# the gate itself actually executes (never merely because a call site was
# reached — e.g. inside test harnesses where the gate is stubbed).
_PathsOrResolver = "CoherencePaths | Callable[[], CoherencePaths]"


def _resolve_paths(paths: Any) -> CoherencePaths:
    return paths() if callable(paths) else paths

# ---------------------------------------------------------------------------
# The pure external-dirt admission matrix (§8.1) — ONE table, ONE body (H1(d))
# ---------------------------------------------------------------------------


# @beacon[
#   id=auto-beacon@AdmissionDecision-j53n,
#   role=AdmissionDecision,
#   slice_labels=mci-barrier-admission,
#   kind=ast,
# ]
class AdmissionDecision(str, Enum):
    """The closed decision vocabulary of the external-dirt matrix."""

    PROCEED_WITH_ADOPTED_BASELINE = "proceed_with_adopted_baseline"
    UPGRADE_TO_LIVE_PREFLIGHT = "upgrade_to_live_preflight"
    REFUSE_REFRESH_REQUIRED = "refuse_refresh_required"


#: The closed op-kind vocabulary of the external-dirt matrix (§8.1).
ADMISSION_OP_KINDS = frozenset(
    {
        "create",
        "update_complete",
        "move",
        "delete",
        "explicit_ap_id",
        "auto_ap_id",
        "cache_compiled_plan",
    }
)

#: The frozen table (§8.1): the decision each op kind receives WHEN
#: external dirt is ACTIVE.  When dirt is inactive every row is
#: PROCEED_WITH_ADOPTED_BASELINE (without canceling any intrinsic live
#: preflight the operation family already requires).
_EXTERNAL_DIRT_ACTIVE_TABLE: dict[str, AdmissionDecision] = {
    # Use a live parent child baseline instead of the adopted view (S07).
    "create": AdmissionDecision.UPGRADE_TO_LIVE_PREFLIGHT,
    # Mandatory live preflight already applies; dirt adds no rule (S12/S13/S14).
    "update_complete": AdmissionDecision.PROCEED_WITH_ADOPTED_BASELINE,
    "move": AdmissionDecision.PROCEED_WITH_ADOPTED_BASELINE,
    "delete": AdmissionDecision.PROCEED_WITH_ADOPTED_BASELINE,
    # The cache cannot prove mnemonic-core uniqueness while dirtied (S10/S12).
    "explicit_ap_id": AdmissionDecision.REFUSE_REFRESH_REQUIRED,
    # Random-core collision is negligible; the ordinary collision scan remains.
    "auto_ap_id": AdmissionDecision.PROCEED_WITH_ADOPTED_BASELINE,
    # The entire mutation target set came from a stale cache (S12:
    # merge.persist / purge.apply).
    "cache_compiled_plan": AdmissionDecision.REFUSE_REFRESH_REQUIRED,
}


# @beacon[
#   id=auto-beacon@admission_policy-0gtx,
#   role=admission_policy,
#   slice_labels=mci-barrier-admission,
#   kind=ast,
# ]
def admission_policy(
    op_kind: str, dirt_state: ExternalDirtState | None
) -> AdmissionDecision:
    """THE pure external-dirt decision table (§8.1).

    ``op_kind`` must be one of :data:`ADMISSION_OP_KINDS` (the table is
    closed and exhaustive; an unknown kind is a programmer error and fails
    closed loudly).  ``dirt_state`` is the shared external-dirt artifact
    (None = clean).  External dirt NEVER raises the hard barrier and never
    blocks a read/preview; "proceed" never removes an intrinsic live
    preflight.
    """

    if op_kind not in ADMISSION_OP_KINDS:
        raise ValueError(
            f"admission_policy: unknown op_kind {op_kind!r} "
            f"(closed table: {sorted(ADMISSION_OP_KINDS)})"
        )
    if dirt_state is None or not dirt_state.active:
        return AdmissionDecision.PROCEED_WITH_ADOPTED_BASELINE
    return _EXTERNAL_DIRT_ACTIVE_TABLE[op_kind]


def external_dirt_admission_policy(
    op_kind: str, dirt_state: ExternalDirtState | None
) -> AdmissionDecision:
    """Thin delegate of :func:`admission_policy` (H1(d): one table, one
    body — the §4.1 name aliases the §8.1 name)."""

    return admission_policy(op_kind, dirt_state)


# ---------------------------------------------------------------------------
# Hard-dirt operation classification (§7.2/§7.3) — the S04 frozen tables
# ---------------------------------------------------------------------------

# Admission class vocabulary used by the A4 boundary inventory.
ADMISSION_CLASS_REFUSE_HARD_DIRT = "refuse_under_hard_dirt"
ADMISSION_CLASS_PROCEED = "proceed"
ADMISSION_CLASS_PROCEED_EXCEPTION = "proceed_exception"

#: §7.3 frozen disk-side provenance table.  The policy is target/scope
#: PROVENANCE, never the side-effect label alone: a disk operation whose
#: target/scope derives from the dirty Workflowy cache refuses; pure disk
#: reads and fully disk-derived mutations proceed.  Unregistered future
#: disk mutations fail closed (refuse) until explicitly registered here.
DISK_OPERATION_HARD_DIRT_POLICY: dict[str, dict[str, str]] = {
    "beacon.snippet": {
        "class": ADMISSION_CLASS_PROCEED,
        "reason": "Pure read with a disk-derived address; read-only — stale "
        "mapping is disclosed via the standard read advisory when "
        "cache-backed resolution was used.",
    },
    "beacon.resolve": {
        "class": ADMISSION_CLASS_PROCEED,
        "reason": "Cache-derived candidate REPORT (0/1/N), not a mutation; "
        "carries the structured advisory, never a uniqueness guarantee.",
    },
    "beacon.tags.ensure": {
        "class": ADMISSION_CLASS_REFUSE_HARD_DIRT,
        "reason": "Symbol/root scope and target footprint derive from the cache.",
    },
    "beacon.tags.remove": {
        "class": ADMISSION_CLASS_REFUSE_HARD_DIRT,
        "reason": "Symbol/root scope and target footprint derive from the cache.",
    },
    "beacon.sync_node": {
        "class": ADMISSION_CLASS_REFUSE_HARD_DIRT,
        "reason": "The live node read does not prove the cached disk "
        "mapping/footprint.",
    },
    "beacon.map.refresh": {
        "class": ADMISSION_CLASS_REFUSE_HARD_DIRT,
        "reason": "Workflowy mutation plus cache-derived footprint (F12 launch).",
    },
    # Archive Reads U3 (§U3.2 D5): the archive mount write resolves its
    # parent from the cache (live-first), so a stale cache could plant an
    # archive under the wrong node — refuse under hard dirt, exactly like
    # the beacon disk mutations.
    "system.archives.mount": {
        "class": ADMISSION_CLASS_REFUSE_HARD_DIRT,
        "reason": "The parent handle resolves from the /nodes-export cache; "
        "a stale cache could plant the archive under the wrong parent.",
    },
}

#: §7.2 recovery/control exceptions — these proceed under hard dirt.
#: The classifier below returns ADMISSION_CLASS_PROCEED_EXCEPTION for these
#: named fronts (S04 Remediation Chain 4, work-order item 2): an HONEST
#: boundary-map distinction, never a runtime change — refuse branches
#: always win, so a hypothetical future exception-named operation carrying
#: Workflowy/disk/cache-dependent-config mutation effects still refuses
#: (fail closed), and proceed vs proceed_exception are both ungated.
HARD_DIRT_PROCEED_EXCEPTIONS: dict[str, str] = {
    "system.cache.refresh": "The recovery authority itself — must proceed.",
    "system.job.status": "Control/observability read; no nodes cache.",
    "system.job.cancel": "Unsafe/in-flight work must remain stoppable.",
}

#: Configuration mutations whose correctness or referential identity depends
#: on cached Workflowy nodes (§7.2).  None today: slice_group CRUD stores
#: root_handle verbatim and never resolves it against the cache (cache-free).
CACHE_DEPENDENT_CONFIG_MUTATIONS: frozenset[str] = frozenset()


# @beacon[
#   id=coherence@mutation-admission-class,
#   slice_labels=ra-coherence,carto-governance,mci-barrier-admission,
#   kind=ast,
#   comment=S04 A4 operation → hard-dirt admission classification authority mirrored by the admission_classes registry fence: workflowy_mutation refuses; disk_mutation follows the §7.3 provenance table (unregistered refuses); config_mutation refuses only when cache-dependent (none today); the named §7.2 recovery/control fronts (system.cache.refresh / job.status / job.cancel) classify proceed_exception, everything else proceeds — refuse branches always win so runtime refusal behavior is unchanged (Remediation Chain 4: proceed_exception is now a live runtime class recorded in the honest complete boundary map).,
# ]
def mutation_admission_class(
    canonical_name: str, side_effects: tuple[str, ...] | list[str], cache_backed: bool
) -> str:
    """THE operation → hard-dirt admission classification (A4).

    Fail-closed defaults with explicit registration:

    - ``workflowy_mutation`` present → refuse under hard dirt (every
      user-facing Workflowy mutation, cache-compiled plan, and CARTO
      launch/admission);
    - ``disk_mutation`` present → the §7.3 provenance table decides;
      unregistered disk mutations refuse (fail closed);
    - ``config_mutation`` alone → refuse only when the operation is
      registered as cache-dependent (none today);
    - a named §7.2 recovery/control exception (``system.cache.refresh`` /
      ``system.job.status`` / ``system.job.cancel``) → ``proceed_exception``
      (S04 Remediation Chain 4, work-order item 2 — an explicit boundary-map
      class; checked AFTER every refuse branch so fail-closed always wins
      and runtime refusal behavior is unchanged);
    - anything else (pure_read/render/modal_push/artifact_write/
      local_state_mutation) → proceed.
    """

    effects = set(side_effects)
    if "workflowy_mutation" in effects:
        return ADMISSION_CLASS_REFUSE_HARD_DIRT
    if "disk_mutation" in effects:
        entry = DISK_OPERATION_HARD_DIRT_POLICY.get(canonical_name)
        if entry is None:
            return ADMISSION_CLASS_REFUSE_HARD_DIRT
        return entry["class"]
    if "config_mutation" in effects:
        if canonical_name in CACHE_DEPENDENT_CONFIG_MUTATIONS:
            return ADMISSION_CLASS_REFUSE_HARD_DIRT
        return ADMISSION_CLASS_PROCEED
    if canonical_name in HARD_DIRT_PROCEED_EXCEPTIONS:
        return ADMISSION_CLASS_PROCEED_EXCEPTION
    return ADMISSION_CLASS_PROCEED


# ---------------------------------------------------------------------------
# The shared hard-stop gates (§7.2/§7.4) — thin guards over hard_stop_state
# ---------------------------------------------------------------------------


def barrier_refusal_extra(status: CoherenceBoundaryStatus) -> dict[str, Any]:
    """The §7.1 structured refusal ``error.extra`` payload, built from a
    boundary status (omits unavailable values; never fabricates)."""

    extra: dict[str, Any] = {
        "refresh_required": True,
        "hard_dirty": True,
        "dirty_generation": status.hard_generation,
        "recovery": {"family": "system", "verb": "cache.refresh", "params": {}},
    }
    if status.hard_reason_code is not None:
        extra["reason_code"] = status.hard_reason_code
    if status.hard_transaction_id is not None:
        extra["transaction_id"] = status.hard_transaction_id
    return extra


def _raise_barrier_refusal(status: CoherenceBoundaryStatus, operation: str) -> None:
    raise CoherenceBarrierActiveError(
        f"{operation}: refused before any side effect — the global coherence "
        f"dirty barrier is active (generation {status.hard_generation}"
        + (
            f", reason {status.hard_reason_code}"
            if status.hard_reason_code
            else ""
        )
        + "); run a full system.cache.refresh to recover",
        dirty_generation=status.hard_generation,
        reason_code=status.hard_reason_code,
        transaction_id=status.hard_transaction_id,
        operation=operation,
    )


# @beacon[
#   id=coherence@mutation-admission,
#   slice_labels=ra-coherence,carto-governance,mci-barrier-admission,
#   kind=ast,
#   comment=S04 shared hard-stop admission gates: mutation admission + cache-derived identity proofs refuse under the hard barrier BEFORE any side effect (job creation, governor acquisition, API send, config write, disk mutation, cache mutation), via one fresh LOCKED hard-stop read (§7.6) through the sole predicate hard_stop_state (H1); external dirt never hard-blocks in S04.,
# ]
async def require_coherence_mutation_admission(
    paths: Any, *, operation: str
) -> None:
    """Refuse a coherence-sensitive mutation under hard dirt — BEFORE any
    side effect.  A fresh locked hard-stop read at the admission point
    (§7.6); future typed-region/lifecycle code rechecks after acquisition
    and revalidation. ``paths`` may be resolved CoherencePaths or
    a zero-argument resolver (lazy — resolved only when the gate runs)."""

    status = await hard_stop_state(_resolve_paths(paths))
    if status.hard_dirty:
        _raise_barrier_refusal(status, operation)


# @beacon[
#   id=auto-beacon@require_cache_identity_proof-mug2,
#   role=require_cache_identity_proof,
#   slice_labels=mci-barrier-admission,
#   kind=ast,
# ]
async def require_cache_identity_proof(paths: Any, *, operation: str) -> None:
    """Refuse a cache-derived global uniqueness/absence proof under hard
    dirt (§7.4).  Ordinary 0/1/N resolution is a cache-backed READ and is
    NOT gated here — it proceeds with the standardized advisory.  ``paths``
    may be resolved CoherencePaths or a zero-argument resolver (lazy)."""

    status = await hard_stop_state(_resolve_paths(paths))
    if status.hard_dirty:
        _raise_barrier_refusal(status, operation)
