"""Typed-region ontology and the diagnostic create-funnel coverage warning.

The on-disk mutation-lease plane retired in the Wave A cut. This historical
module holds the closed UUID/special-parent/AP-core grammar shared by the
in-process ownership plane and both CARTO overlap directions, plus the
diagnostic create-funnel coverage warning. Containment always uses the
supplied current view, never diagnostic stored chains. Ownership itself
lives only in ``mutation_locks.py``; this module's filename is historical.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any
from uuid import UUID

from . import carto_job_governance as gov
from .carto_job_governance import GovernanceError

LOGGER = logging.getLogger(__name__)

_REGION_KINDS = frozenset({"exact_node", "child_set", "subtree", "ap_core"})
# Frozen canonical typed-region order (§5.2), then by region key.
_REGION_KIND_ORDER = {"ap_core": 0, "child_set": 1, "exact_node": 2, "subtree": 3}
_PERSISTED_CORE_RE = re.compile(r"^[A-Z0-9]{3,}$")
_UUID_TEXT_RE = re.compile(r"(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})\Z")


class ReservedMutationParent(Enum):
    """Virtual child-list identities, not strings or invented graph nodes."""

    ROOT = "root"
    INBOX = "inbox"


class MutationRegionError(GovernanceError):
    """Malformed/unresolvable typed region (including an unresolvable live key)."""

    code = "mutation_region_error"

    def __init__(
        self, message: str, *, reason: str = "malformed_region",
        retry_after: float | None = None, region: Any = None,
    ) -> None:
        self.reason = reason
        self.retry_after = retry_after
        self.region = region
        super().__init__(message)


def _validate_uuid_text(value: Any, label: str) -> str:
    raw = value.strip() if isinstance(value, str) else ""
    if not raw:
        raise MutationRegionError(f"mutation region: {label} must be nonempty")
    if not _UUID_TEXT_RE.fullmatch(raw):
        raise MutationRegionError(f"mutation region: {label} must be a full UUID")
    return str(UUID(raw))


def _lookup_region_node(
    key: str, cache_by_id: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    """Resolve supported UUID spellings with four map lookups, never a scan.

    Multiple spellings may alias the same record. Conflicting records cannot
    prove topology; normalize only UUID-bearing fields before comparing them.
    """

    canonical = _validate_uuid_text(key, "region key")
    compact = canonical.replace("-", "")
    found: Mapping[str, Any] | None = None

    def comparable(node: Mapping[str, Any]) -> dict[str, Any]:
        result = dict(node)
        for field in ("id", "uuid", "parent_id", "parentId"):
            value = result.get(field)
            if isinstance(value, str) and _UUID_TEXT_RE.fullmatch(value.strip()):
                result[field] = str(UUID(value.strip()))
        return result

    for spelling in (canonical, compact, canonical.upper(), compact.upper()):
        node = cache_by_id.get(spelling)
        if node is None:
            continue
        if not isinstance(node, Mapping) or (
            found is not None and node is not found and comparable(found) != comparable(node)
        ):
            raise MutationRegionError(
                f"mutation region: inconsistent cache aliases for {canonical!r}",
                reason="unresolvable_region", region=canonical,
            )
        found = node
    return found


class _RegionLookupView(Mapping[str, Mapping[str, Any]]):
    """Read-only spelling facade consumed by the existing footprint walk.

    The Inbox parent sentinel terminates the diagnostic walk but is recorded
    as incomplete ancestry. No fake root or topology index is constructed.
    """

    def __init__(self, source: Mapping[str, Mapping[str, Any]]) -> None:
        self.source = source
        self.saw_unresolved_inbox = False

    def __getitem__(self, key: str) -> Mapping[str, Any]:
        node = _lookup_region_node(key, self.source)
        if node is None:
            raise KeyError(key)
        parent = node.get("parent_id")
        if parent is None:
            parent = node.get("parentId")
        if isinstance(parent, str) and parent.strip().lower() == "inbox":
            self.saw_unresolved_inbox = True
            parent = None
        elif parent not in (None, ""):
            try:
                parent = _validate_uuid_text(parent, "parent_uuid")
            except MutationRegionError as exc:
                raise MutationRegionError(
                    str(exc), reason="unresolvable_region", region=key,
                ) from exc
        # Only this small node record is wrapped; the supplied cache is never
        # enumerated or copied. Both spellings agree even for Inbox terminals.
        return {**node, "parent_id": parent, "parentId": parent}

    def __iter__(self) -> Iterator[str]:
        return iter(self.source)

    def __len__(self) -> int:
        return len(self.source)


def normalize_ap_core(value: Any) -> str:
    """Normalize a supplied AP-ID handle to its uppercase base36 core.

    INPUT normalization delegates to the existing AP-ID normalizer (§5.1) —
    lazily imported so typed-region users do not eagerly import the client
    modules. Invalid input fails closed.
    """

    from .client.api_client_nexus import _normalize_ap_id_input

    core = _normalize_ap_id_input(str(value or ""))
    if core is None:
        raise MutationRegionError(f"mutation region: invalid AP core {value!r}")
    return core


def rehydrate_ancestor_chain(
    key_uuid: str, cache_by_id: Mapping[str, Mapping[str, Any]], *,
    allow_special_terminal: bool = False,
) -> tuple[str, ...]:
    """Rebuild the CURRENT region-key-first ancestor chain from one adopted view.

    The ONE audited fail-closed ancestor walk (``compute_footprint`` A1)
    remains the authority — cycle-guarded, complete-to-vault, fail closed on
    a missing ancestor.  A stored chain is never consulted here (A1).
    """

    key = _validate_uuid_text(key_uuid, "region key")
    lookup = _RegionLookupView(cache_by_id)
    try:
        footprint = gov.compute_footprint(None, key, lookup, kind="file")
    except gov.FootprintUnresolvableError as exc:
        raise MutationRegionError(
            f"mutation region: live key {key_uuid!r} is unresolvable in the "
            f"admitting operation's current adopted view: {exc}",
            reason="unresolvable_region", region=key,
        ) from exc
    if lookup.saw_unresolved_inbox and not allow_special_terminal:
        raise MutationRegionError(
            f"mutation region: live key {key!r} has unresolved Inbox ancestry",
            reason="unresolvable_region", region=key,
        )
    return tuple(_validate_uuid_text(item, "ancestor") for item in footprint["ancestor_chain"])


def mutation_parent_identity(
    node_uuid: str, *, cache_by_id: Mapping[str, Mapping[str, Any]],
) -> str | ReservedMutationParent:
    """Return known parentage; a failed node lookup is never outline ROOT."""

    key = _validate_uuid_text(node_uuid, "node_uuid")
    node = _lookup_region_node(key, cache_by_id)
    if node is None:
        raise MutationRegionError(
            f"mutation region: live key {key!r} is unresolvable in the current view",
            reason="unresolvable_region", region=key,
        )
    if "parent_id" not in node and "parentId" not in node:
        raise MutationRegionError(
            f"mutation region: parent of {key!r} is unknown",
            reason="unresolvable_region", region=key,
        )
    parent = node.get("parent_id")
    if parent is None:
        parent = node.get("parentId")
    if parent in (None, ""):
        return ReservedMutationParent.ROOT
    if isinstance(parent, str) and parent.strip().lower() == "inbox":
        return ReservedMutationParent.INBOX
    try:
        return _validate_uuid_text(parent, "parent_uuid")
    except MutationRegionError as exc:
        raise MutationRegionError(
            str(exc), reason="unresolvable_region", region=key,
        ) from exc


@dataclass(frozen=True)
# @beacon[
#   id=auto-beacon@MutationRegion-54et,
#   role=MutationRegion,
#   slice_labels=mci-lease-ontology,
#   kind=ast,
#   comment=Closed canonical UUID and AP-core regions with typed ROOT/INBOX child lists; stored chains are diagnostic only and never authorize containment.,
# ]
class MutationRegion:
    """One typed semantic region (§5.1 closed union, schema v1).

    ``ancestor_chain`` is region-key-first (H2e): the region's own
    node/parent/root UUID first, then successive parents through the vault
    root.  It records topology AT ACQUISITION for evidence/diagnostics; it
    is never the containment authority at a later admission (A1 — chains
    are rehydrated from the admitting operation's current adopted view).
    ``ap_core`` carries no graph chain.
    """

    kind: str
    node_uuid: str | None = None
    parent_uuid: str | ReservedMutationParent | None = None
    root_uuid: str | None = None
    core: str | None = None
    ancestor_chain: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or self.kind not in _REGION_KINDS:
            raise MutationRegionError(f"mutation region: unknown kind {self.kind!r}")
        if self.kind == "ap_core":
            core = normalize_ap_core(self.core)
            if not _PERSISTED_CORE_RE.fullmatch(core):
                raise MutationRegionError(
                    "mutation region: ap_core core must be uppercase base36 (>=3 chars)"
                )
            for absent in ("node_uuid", "parent_uuid", "root_uuid"):
                if getattr(self, absent) is not None:
                    raise MutationRegionError(f"mutation region: ap_core carries no {absent}")
            if self.ancestor_chain != ():
                raise MutationRegionError("mutation region: ap_core carries no ancestor chain")
            object.__setattr__(self, "core", core)
            return
        key_field = {"exact_node": "node_uuid", "child_set": "parent_uuid", "subtree": "root_uuid"}[
            self.kind
        ]
        key_value = getattr(self, key_field)
        for other in ("node_uuid", "parent_uuid", "root_uuid"):
            if other != key_field and getattr(self, other) is not None:
                raise MutationRegionError(
                    f"mutation region: {self.kind} carries no {other}"
                )
        if self.core is not None:
            raise MutationRegionError(f"mutation region: {self.kind} carries no core")
        if self.kind == "child_set" and isinstance(key_value, ReservedMutationParent):
            if self.ancestor_chain != ():
                raise MutationRegionError("mutation region: special child_set carries no ancestor chain")
            return
        key_value = _validate_uuid_text(key_value, key_field)
        object.__setattr__(self, key_field, key_value)
        if (
            not isinstance(self.ancestor_chain, tuple)
            or not self.ancestor_chain
            or not all(isinstance(item, str) and item for item in self.ancestor_chain)
        ):
            raise MutationRegionError(
                f"mutation region: {self.kind} requires a nonempty ancestor chain"
            )
        chain = tuple(_validate_uuid_text(item, "ancestor") for item in self.ancestor_chain)
        if len(set(chain)) != len(chain):
            raise MutationRegionError("mutation region: ancestor chain repeats a UUID")
        if chain[0] != key_value:
            raise MutationRegionError(
                "mutation region: ancestor chain must be region-key-first (H2e)"
            )
        object.__setattr__(self, "ancestor_chain", chain)

    @property
    def key(self) -> str | ReservedMutationParent:
        """The region's identity key (graph UUID or normalized AP core)."""

        if self.kind == "ap_core":
            return str(self.core)
        return getattr(self, {"exact_node": "node_uuid", "child_set": "parent_uuid", "subtree": "root_uuid"}[self.kind])

    def sort_key(self) -> tuple[int, str]:
        key = self.key
        if self.kind == "child_set":
            key = (
                "0:root" if key is ReservedMutationParent.ROOT else
                "1:inbox" if key is ReservedMutationParent.INBOX else f"2:{key}"
            )
        return (_REGION_KIND_ORDER[self.kind], key)

    def to_dict(self) -> dict[str, Any]:
        if self.kind == "ap_core":
            return {"kind": "ap_core", "core": self.core}
        if self.kind == "child_set" and isinstance(self.parent_uuid, ReservedMutationParent):
            return {"kind": "child_set", "parent_special": self.parent_uuid.value}
        key_field = {"exact_node": "node_uuid", "child_set": "parent_uuid", "subtree": "root_uuid"}[
            self.kind
        ]
        return {
            "kind": self.kind,
            key_field: getattr(self, key_field),
            "ancestor_chain": list(self.ancestor_chain),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> MutationRegion:
        if not isinstance(payload, Mapping):
            raise MutationRegionError("mutation region: payload must be an object")
        kind = payload.get("kind")
        if not isinstance(kind, str):
            raise MutationRegionError(f"mutation region: unknown kind {kind!r}")
        if kind == "ap_core":
            allowed = {"kind", "core"}
        elif kind == "child_set" and "parent_special" in payload:
            if set(payload) != {"kind", "parent_special"}:
                raise MutationRegionError("mutation region: special child_set accepts only parent_special")
            try:
                parent = ReservedMutationParent(payload["parent_special"])
            except (ValueError, TypeError) as exc:
                raise MutationRegionError("mutation region: unknown parent_special") from exc
            return cls(kind="child_set", parent_uuid=parent)
        elif kind in {"exact_node", "child_set", "subtree"}:
            allowed = {
                "kind",
                {"exact_node": "node_uuid", "child_set": "parent_uuid", "subtree": "root_uuid"}[kind],
                "ancestor_chain",
            }
        else:
            raise MutationRegionError(f"mutation region: unknown kind {kind!r}")
        extra = sorted(set(payload) - allowed)
        if extra:
            raise MutationRegionError(f"mutation region: unknown keys fail closed: {extra}")
        chain = payload.get("ancestor_chain", ())
        return cls(
            kind=str(kind),
            node_uuid=payload.get("node_uuid"),
            parent_uuid=payload.get("parent_uuid"),
            root_uuid=payload.get("root_uuid"),
            core=payload.get("core"),
            ancestor_chain=tuple(chain) if isinstance(chain, (list, tuple)) else (),
        )


def build_region_exact_node(
    node_uuid: str, *, cache_by_id: Mapping[str, Mapping[str, Any]]
) -> MutationRegion:
    key = _validate_uuid_text(node_uuid, "node_uuid")
    return MutationRegion(
        kind="exact_node",
        node_uuid=key,
        ancestor_chain=rehydrate_ancestor_chain(key, cache_by_id, allow_special_terminal=True),
    )


def build_region_child_set(
    parent_uuid: str | ReservedMutationParent, *, cache_by_id: Mapping[str, Mapping[str, Any]]
) -> MutationRegion:
    if isinstance(parent_uuid, ReservedMutationParent):
        return MutationRegion(kind="child_set", parent_uuid=parent_uuid)
    key = _validate_uuid_text(parent_uuid, "parent_uuid")
    return MutationRegion(
        kind="child_set",
        parent_uuid=key,
        ancestor_chain=rehydrate_ancestor_chain(key, cache_by_id, allow_special_terminal=True),
    )


def build_region_subtree(
    root_uuid: str, *, cache_by_id: Mapping[str, Mapping[str, Any]]
) -> MutationRegion:
    key = _validate_uuid_text(root_uuid, "root_uuid")
    return MutationRegion(
        kind="subtree",
        root_uuid=key,
        ancestor_chain=rehydrate_ancestor_chain(key, cache_by_id, allow_special_terminal=True),
    )


def build_region_ap_core(core: Any) -> MutationRegion:
    return MutationRegion(kind="ap_core", core=normalize_ap_core(core))


def region_display_key(region: MutationRegion) -> str:
    """Diagnostic display only; never an ownership identity."""

    if isinstance(region.key, ReservedMutationParent):
        return f"<{region.key.value}>"
    if region.kind == "ap_core":
        return f"ap:{region.core}"
    return region.key[:8]


# @beacon[
#   id=auto-beacon@normalize_mutation_regions-9iz6,
#   role=normalize_mutation_regions,
#   slice_labels=mci-lease-ontology,
#   kind=ast,
#   comment=Validates the whole four-kind request before registration; canonical typed identity order and duplicate collapse reject inconsistent diagnostic payloads within one request.,
# ]
def normalize_mutation_regions(
    regions: Iterable[MutationRegion | Mapping[str, Any]],
) -> tuple[MutationRegion, ...]:
    """Canonical all-or-none region normalization (§5.2).

    Validates every region, collapses byte-identical duplicates, rejects
    conflicting duplicate payloads, and returns the canonical sorted tuple
    (ap_core, child_set, exact_node, subtree; then by key).  The same order
    is used by typed-region consumers.
    """

    normalized: dict[tuple[int, str], MutationRegion] = {}
    for region in regions:
        candidate = (
            region
            if isinstance(region, MutationRegion)
            else MutationRegion.from_dict(region)
        )
        if not isinstance(candidate.kind, str) or candidate.kind not in _REGION_KINDS:
            raise MutationRegionError(f"mutation region: unknown kind {candidate.kind!r}")
        key = candidate.sort_key()
        existing = normalized.get(key)
        if existing is not None and existing != candidate:
            raise MutationRegionError(
                f"mutation regions: conflicting duplicate payloads for {candidate.kind}:{candidate.key}"
            )
        normalized[key] = candidate
    if not normalized:
        raise MutationRegionError("mutation regions: at least one region is required")
    return tuple(normalized[key] for key in sorted(normalized))


# ---------------------------------------------------------------------------
# Region intersection (§5.3 truth table) + CARTO claim intersection (§11.1)
# ---------------------------------------------------------------------------


def _chain_for(
    key_uuid: str, cache_by_id: Mapping[str, Mapping[str, Any]] | None, *,
    _chain_memo: dict[str, tuple[str, ...]] | None = None,
) -> tuple[str, ...]:
    """Current-view chain, optionally reused within ONE caller-owned scan.

    The scan creates/discards its memo; no state survives this decision or a
    view advance. Only successful complete chains are memoized. Callers which
    supply no decision-local memo retain the ordinary fresh-walk behavior.
    """
    if cache_by_id is None:
        raise MutationRegionError(
            "mutation region containment requires the admitting operation's "
            "current adopted view (cache_by_id); stored chains are never an "
            "allow-decision authority (A1)"
        )
    key = _validate_uuid_text(key_uuid, "region key")
    if _chain_memo is not None and key in _chain_memo:
        return _chain_memo[key]
    chain = rehydrate_ancestor_chain(key, cache_by_id)
    if _chain_memo is not None:
        _chain_memo[key] = chain
    return chain


# @beacon[
#   id=auto-beacon@regions_intersect-7sfh,
#   role=regions_intersect,
#   slice_labels=mci-lease-ontology,
#   kind=ast,
#   comment=Total symmetric four-kind intersection; exact-node and child-set commute permanently; every containment side uses the same proof view with optional one-scan chain reuse that never survives a decision or publication; unknown pairs raise.,
# ]
def regions_intersect(
    a: MutationRegion,
    b: MutationRegion,
    *,
    cache_by_id: Mapping[str, Mapping[str, Any]] | None = None,
    _chain_memo: dict[str, tuple[str, ...]] | None = None,
) -> bool:
    """The symmetric §5.3 region-intersection truth table.

    Equality rows are pure over region keys.  Containment rows rehydrate
    CURRENT ancestor chains from ``cache_by_id`` (A1) and fail closed when
    it is absent or a key is unresolvable.  ``exact_node`` vs ``child_set``
    NEVER intersects (content and child-order effects commute under Axiom
    7); ``ap_core`` never intersects a graph region. The acquiring scan may
    pass its decision-local chain memo; it is never retained here or shared
    across decisions/views, and unknown kinds still raise before any lookup.
    """

    if (not isinstance(a.kind, str) or a.kind not in _REGION_KINDS
            or not isinstance(b.kind, str) or b.kind not in _REGION_KINDS):
        raise MutationRegionError(f"mutation region: unknown kind pair {a.kind!r}/{b.kind!r}")
    if a.kind == "ap_core" or b.kind == "ap_core":
        return a.kind == b.kind == "ap_core" and a.core == b.core
    if a.kind == "exact_node" and b.kind == "exact_node":
        return a.node_uuid == b.node_uuid
    if a.kind == "child_set" and b.kind == "child_set":
        return a.parent_uuid == b.parent_uuid
    if {a.kind, b.kind} == {"exact_node", "child_set"}:
        return False
    if isinstance(a.key, ReservedMutationParent) or isinstance(b.key, ReservedMutationParent):
        return False
    # At least one subtree: containment rows (rehydrated, fail closed).
    # A1: EVERY participating live key's current chain is rehydrated — an
    # unresolvable key on EITHER side fails closed, never answers from the
    # stale stored chain.
    if a.kind == "subtree" and b.kind in {"exact_node", "child_set"}:
        _chain_for(str(a.root_uuid), cache_by_id, _chain_memo=_chain_memo)  # validates the subtree key
        return str(a.root_uuid) in _chain_for(b.key, cache_by_id, _chain_memo=_chain_memo)
    if b.kind == "subtree" and a.kind in {"exact_node", "child_set"}:
        _chain_for(str(b.root_uuid), cache_by_id, _chain_memo=_chain_memo)  # validates the subtree key
        return str(b.root_uuid) in _chain_for(a.key, cache_by_id, _chain_memo=_chain_memo)
    if a.kind == "subtree" and b.kind == "subtree":
        chain_a = _chain_for(a.key, cache_by_id, _chain_memo=_chain_memo)
        chain_b = _chain_for(b.key, cache_by_id, _chain_memo=_chain_memo)
        return a.root_uuid in chain_b or b.root_uuid in chain_a
    raise MutationRegionError(f"mutation region: unsupported kind pair {a.kind!r}/{b.kind!r}")


def regions_conflict_details(
    regions_a: Sequence[MutationRegion],
    regions_b: Sequence[MutationRegion],
    *,
    cache_by_id: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """First canonical conflicting pair across two region sets, or None."""

    for left in normalize_mutation_regions(regions_a):
        for right in normalize_mutation_regions(regions_b):
            if regions_intersect(left, right, cache_by_id=cache_by_id):
                return {
                    "region_a": left.to_dict(),
                    "region_b": right.to_dict(),
                    "conflicting_regions_a": [r.to_dict() for r in normalize_mutation_regions(regions_a)][:8],
                    "conflicting_regions_b": [r.to_dict() for r in normalize_mutation_regions(regions_b)][:8],
                }
    return None


# @beacon[
#   id=auto-beacon@mutation_regions_intersect_carto_claim-tz0u,
#   role=mutation_regions_intersect_carto_claim,
#   slice_labels=mci-lease-ontology,
#   kind=ast,
#   comment=Tests typed graph regions against CARTO with both sides rehydrated from the proof view; the acquiring scan may reuse complete chains only within that decision; AP cores and virtual parent lists never become fake claim roots.,
# ]
def mutation_regions_intersect_carto_claim(
    regions: Sequence[MutationRegion],
    claim: Mapping[str, Any],
    *,
    cache_by_id: Mapping[str, Mapping[str, Any]] | None = None,
    _chain_memo: dict[str, tuple[str, ...]] | None = None,
) -> MutationRegion | None:
    """The §11.1 mutation-region vs CARTO-claim footprint intersection.

    Returns the first conflicting region, or None.  ``claim`` is any mapping
    carrying ``root_uuid`` (a live claim artifact or a CARTO admission
    candidate footprint).  Every chain is rehydrated from ``cache_by_id``
    (A1 — for BOTH the typed region keys and the claim root key); AP-core
    alone never overlaps a CARTO claim. An optional acquiring-scan memo reuses
    complete chains only within that one decision/view; this function stores
    nothing and still validates every participating key before a positive.
    """

    normalized = normalize_mutation_regions(regions)
    graph = tuple(r for r in normalized if r.kind != "ap_core" and not isinstance(r.key, ReservedMutationParent))
    if not graph:
        return None
    claim_root = str(claim.get("root_uuid") or "")
    if not claim_root:
        raise MutationRegionError("carto claim overlap: claim lacks root_uuid")
    claim_root = _validate_uuid_text(claim_root, "claim root_uuid")
    # A1: the claim root is a live region key too — rehydrate its CURRENT
    # chain once (existence-validated, fail closed), and rehydrate every
    # typed region key's chain (reused only within this scan's proof view).
    claim_chain = _chain_for(claim_root, cache_by_id, _chain_memo=_chain_memo)
    # Validate every participating key before an early positive result.
    chains = tuple(_chain_for(region.key, cache_by_id, _chain_memo=_chain_memo) for region in graph)
    for region, region_chain in zip(graph, chains):
        if region.kind in {"exact_node", "child_set"}:
            # claim root is X or appears in X's (rehydrated) ancestor chain.
            if claim_root == region.key or claim_root in region_chain:
                return region
            continue
        # subtree S: claim root within S (S.root in claim's chain), or S
        # within claim root (claim root in S's chain).
        if claim_root in region_chain or str(region.root_uuid) in claim_chain:
            return region
    return None


# ---------------------------------------------------------------------------
# Transition-mode CREATE coverage warning (§14) — diagnostic, never authority
# ---------------------------------------------------------------------------

_COVERAGE_WARNED_KEYS: set[str] = set()


def _hint_covers_parent(coverage_hint: Any, account_scope_id: str | None, parent_uuid: str | None) -> bool:
    """Basic account/region ownership identity for a PASSED claim mapping.

    Inspects only the passed mapping — NEVER the governance directory
    (a directory scan could let an ungoverned caller borrow another
    operation's claim). Stored chains are sufficient here because this is a
    diagnostic suppressor, never an authorization decision.
    """

    if coverage_hint is None:
        return False
    hint_account: str | None = None
    covers = False
    if isinstance(coverage_hint, Mapping):
        # A CARTO claim artifact: account + footprint ancestor coverage.
        hint_account = str(coverage_hint.get("account_scope_id") or "") or None
        if parent_uuid:
            chain = coverage_hint.get("ancestor_chain") or []
            if coverage_hint.get("root_uuid") == parent_uuid or parent_uuid in chain:
                covers = True
    else:
        return False
    if account_scope_id and hint_account and account_scope_id != hint_account:
        return False
    return covers


# @beacon[
#   id=carto-governance@create-coverage-warning,
#   slice_labels=carto-governance,ra-carto-jobs,ra-workflowy-cache,mci-lease-seams,
#   kind=ast,
#   comment=S05 §14 transition-mode create-funnel coverage warning (diagnostic only, never authorization): invoked by WorkFlowyClientCore.create_node itself on every production entry before the send path; a passed claim mapping suppresses the structured warning ONLY on basic account/region ownership identity; no hint (or a mismatched hint) logs once per callsite per process; the helper NEVER scans the governance directory for a convenient overlapping owner — S07 replaces the hint with transaction-bound MutationContext and S11 flips to hard enforcement.,
# ]
def warn_create_governance_coverage(
    *,
    operation_label: str,
    caller_summary: str,
    account_scope_id: str | None = None,
    parent_uuid: str | None = None,
    coverage_hint: Any = None,
    logger: Any = None,
    dedupe_key: str | None = None,
) -> bool:
    """§14 transition-mode create-funnel coverage warning (diagnostic only).

    Returns True when a warning was emitted.  A passed hint suppresses the
    warning ONLY via :func:`_hint_covers_parent` identity — the hint confers
    NO authorization, and this helper performs NO governance-directory scan
    (permanent-fence locked).  Warning volume is bounded: one emission per
    dedupe key (default: operation + caller summary) per process.
    """

    if _hint_covers_parent(coverage_hint, account_scope_id, parent_uuid):
        return False
    key = dedupe_key or f"{operation_label}:{caller_summary}"
    if key in _COVERAGE_WARNED_KEYS:
        return False
    _COVERAGE_WARNED_KEYS.add(key)
    log = logger or LOGGER
    log.warning(
        "TRANSITION COVERAGE WARNING: %s called WITHOUT a governance coverage "
        "hint (account=%s parent=%s caller=%s). Ungoverned creates are "
        "unsupported at coherence_mode=enforced (S07 MutationContext / S11 "
        "hard enforcement). This is a transition-mode diagnostic only.",
        operation_label,
        account_scope_id or "unknown",
        parent_uuid or "unknown",
        caller_summary,
    )
    return True
