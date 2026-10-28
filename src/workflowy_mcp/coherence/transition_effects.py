"""S03-CR-02 transition-COW sparse effects — per-field authority, ONE merge authority.

The 4fb06a1 transition overlay tracked only node IDs
(``_transition_cow_overlay_ids``) and replaced whole nodes on any content
difference; it also advanced a running operation's task snapshot forward to
the complete newer publisher generation.  The follow-up audit (CR-02A/B/C)
found three reachable defects in that design:

- historical epoch publication bypassed the overlay (the surviving local
  ``WorkFlowyClientCore._publish_lineage_view`` now uses the shared merge);
- ID-only whole-node replacement violates per-field authority (Axiom 7) and
  can mask a later legitimate server/GUI value on every future epoch
  publication while dirt is cleared;
- forward task-snapshot advancement imports unrelated newer-generation
  state into a running operation, violating the locked one-generation
  invariant (S03 §1.2/§6.3).

This module is the repair — one cohesive, shared authority consumed by
EVERY epoch/lineage publication route:

Refresh, compat and warm-start publication use
``WorkFlowyClientCore._publish_lineage_view``. Explicit warm rebuilds
materialize the retained base and journal through S2 before that same local
publisher; the retired journal-adoption engine is not another route.

Contracts implemented (work order A–F):

- A (one operation, one shared generation): a running operation's markers
  never move; its task snapshot gains ONLY its own sparse verified effect
  (``apply_transition_effects_to_nodes``), never the newer publisher
  generation (see ``coherence.operation_cache._advance_task_snapshot``).
- B (same-operation read-your-writes): the process publisher receives the
  newer generation + the operation's sparse effect; the operation's task
  snapshot receives its pinned generation + the same sparse effect.
- C (per-effect authority): transition effects separate creation/existence
  authority from every sparse post-create write; repeated writes retain their
  local predecessor chain so delayed intermediate offers are recognized;
  delete authority is ROOT-SCOPED — a root tombstone owns absence for exactly
  the nodes the OFFERED topology places beneath it (unknown added descendants
  included), never for a descendant that escaped the subtree before the
  delete committed.  Server-managed fields (createdAt/modifiedAt/data) and
  unknown/cache-only metadata are NEVER owned and always ride the newer
  offer.
- D (one authority): ``merge_offer_with_transition_effects`` is the single
  pure merge; the per-publisher ``TransitionEffectPolicy`` carries the
  effect store so the adoption engine (which never sees the client)
  applies the identical policy.
- E (no clean permanent staleness): the merge is a causal discriminator
  per owned field — offered == verified ⇒ reflected (retire); offered equal
  to ANY local predecessor (initial or intermediate) ⇒ delayed offer (carry
  verified, keep the effect, mark legacy dirty); anything else ⇒ genuine
  conflict: the OFFER wins, the field retires, and legacy-dirty evidence is
  marked.  Offered node presence reflects only creation, never later sparse
  writes.  A root tombstone remains dirty/live only while an offered member
  remains beneath its root; an escaped descendant's survival never pins a
  tombstone alive.
- F (publication atomicity): the merge is pure; the surviving-effect store
  and dirty bookkeeping commit under the SAME publisher critical section
  as the view swap; a failed publication leaves the last good view, the
  prior effect store, and dirty evidence untouched.

Text-value equivalence (documented, overlay-local): for the TEXT fields
``name``/``note``, ``None`` and ``""`` compare EQUAL.  The legacy patchers
write explicit ``None`` (model dumps) while exports omit or empty-string
text fields; without this equivalence a ``""``↔``None`` artifact pair would
carry forever and never retire.  The equivalence is confined to this
overlay — the S01 codec's explicit-null semantics are unchanged everywhere
else.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal

from .cache_view import CacheView, prepare_cow_payload
from .canonical_node import IngestionProfile, canonicalize_cache_node

# ---------------------------------------------------------------------------
# Owned-field vocabulary (Axiom 7 applied to the transition bridge)
# ---------------------------------------------------------------------------

# The canonical content fields a transition COW write may own.  Server-
# managed fields (createdAt / modifiedAt / data) and unknown/cache-only
# metadata are deliberately EXCLUDED: they always ride the newer lineage
# offer (Contract C — "unknown/cache-only metadata from a newer lineage
# offer must be preserved unless the local operation explicitly changed
# it", and no current patcher's semantic intent covers them).
TRANSITION_OWNED_FIELDS: frozenset[str] = frozenset(
    {"name", "note", "parent_id", "priority", "completed", "completedAt"}
)

_TEXT_FIELDS: frozenset[str] = frozenset({"name", "note"})


def _comparable(key: str, value: Any) -> Any:
    """Normalize one value for overlay comparison (None ≈ "" on text fields)."""

    if value is None and key in _TEXT_FIELDS:
        return ""
    return value


def _canonical(node: Mapping[str, Any]) -> dict[str, Any]:
    """Canonicalize one already-semantic cache node (zero additional decode)."""

    return canonicalize_cache_node(node, IngestionProfile.JOURNAL_SEMANTIC)


# ---------------------------------------------------------------------------
# The sparse effect representation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldWrite:
    """One owned field's causal local-write chain.

    ``prior`` is the value before the first local write, ``intermediate``
    retains every distinct locally-verified value superseded by a later
    local write, and ``verified`` is the latest value.  During merge an
    offered value equal to ANY predecessor is a delayed intermediate offer
    and must carry the latest verified value; only a value outside the local
    chain is a genuine later conflict.
    """

    prior: Any
    verified: Any
    intermediate: tuple[Any, ...] = ()


@dataclass(frozen=True)
# @beacon[
#   id=auto-beacon@TransitionEffect-0q2x,
#   role=TransitionEffect,
#   slice_labels=mci-cache-cow,
#   kind=ast,
# ]
class TransitionEffect:
    """One sparse, field-authoritative transition-COW effect for one node.

    - ``fields``: an exact causal before/after mask over the owned vocabulary.
    - ``create``: the canonical record at creation PLUS any sparse writes
      performed after creation (kept separate so an intermediate post-create
      offer cannot retire those later writes). ``absent_full_refreshes``
      counts consecutive authoritative full-refresh offers that omit the
      node; non-authoritative publication kinds never advance it, and any
      new local write resets it.
    - ``delete``: root-scoped subtree absence authority.  ``subtree_root``
      distinguishes the two delete shapes: a ROOT tombstone
      (``subtree_root`` is ``None`` or the node's own id) owns absence for
      exactly the nodes the OFFERED topology places beneath its root; a
      DESCENDANT marker (``subtree_root`` names another node) exists only so
      composition retires the descendant's earlier create/fields effects —
      it never removes an offered node on its own and never pins survival,
      because a descendant offered ALIVE outside every tombstoned root's
      offered subtree provably escaped before the delete committed (deleted
      UUIDs never reappear).
    """

    kind: Literal["fields", "create", "delete"]
    fields: Mapping[str, FieldWrite] = field(default_factory=dict)
    record: Mapping[str, Any] | None = None
    subtree_root: str | None = None
    absent_full_refreshes: int = 0


def _compose_field_write(existing: FieldWrite, new: FieldWrite) -> FieldWrite:
    """Preserve the complete causal predecessor chain for repeated writes."""

    predecessors: list[Any] = []
    for value in (
        existing.prior,
        *existing.intermediate,
        existing.verified,
        new.prior,
        *new.intermediate,
    ):
        if not any(value == prior for prior in predecessors):
            predecessors.append(value)
    return FieldWrite(
        prior=predecessors[0],
        verified=new.verified,
        intermediate=tuple(predecessors[1:]),
    )


def _compose_field_maps(
    existing: Mapping[str, FieldWrite], new: Mapping[str, FieldWrite]
) -> Mapping[str, FieldWrite]:
    merged: dict[str, FieldWrite] = dict(existing)
    for key, write in new.items():
        merged[key] = (
            _compose_field_write(merged[key], write) if key in merged else write
        )
    return MappingProxyType(merged)


def _materialize_created_record(effect: TransitionEffect) -> dict[str, Any]:
    record = dict(effect.record or {})
    for key, write in effect.fields.items():
        record[key] = write.verified
    return record


def _is_root_tombstone(node_id: str, effect: TransitionEffect) -> bool:
    """True when a delete effect carries root subtree-absence authority."""

    return effect.kind == "delete" and effect.subtree_root in (None, node_id)


def _local_delete_effects(
    before_by_id: Mapping[str, Mapping[str, Any]], removed_ids: Iterable[str]
) -> dict[str, TransitionEffect]:
    """Root-scoped tombstones for one attempt's locally-removed id set.

    A removed node whose local BEFORE-parent was not itself removed in the
    same derivation becomes a ROOT tombstone; every other removed node
    becomes a descendant marker pointing at the root whose before-topology
    subtree covers it.  For a removed cycle with no such root (every
    member's parent also removed), the FIRST uncovered member (before-list
    order) becomes the component's root tombstone and claims its removed-
    descendant chain, so the LATER ring members become descendant markers
    naming that root — the delete never silently loses authority, and the
    fallback deliberately grants root authority to exactly ONE member per
    ring component (never per-member self-roots, which would let a ring
    member's independent tombstone remove an offered-alive escapee).
    """

    removed = [str(node_id) for node_id in removed_ids]
    removed_set = set(removed)

    def _before_parent(node_id: str) -> str | None:
        node = before_by_id[node_id]
        parent_id = node.get("parent_id")
        if parent_id is None:
            parent_id = node.get("parentId")
        return str(parent_id) if parent_id is not None else None

    children_in_removed: dict[str, list[str]] = {}
    for node_id in removed:
        parent_id = _before_parent(node_id)
        if parent_id in removed_set:
            children_in_removed.setdefault(parent_id, []).append(node_id)

    roots = [
        node_id for node_id in removed if _before_parent(node_id) not in removed_set
    ]
    owner_by_id: dict[str, str] = {}

    def _claim(root: str) -> None:
        stack = [root]
        while stack:
            current = stack.pop()
            if current in owner_by_id:
                continue
            owner_by_id[current] = root
            stack.extend(children_in_removed.get(current, ()))

    for root in roots:
        _claim(root)
    for node_id in removed:
        # Cycle fallback: the FIRST uncovered member roots its whole ring
        # component (the _claim sweep marks the rest as its descendants).
        if node_id not in owner_by_id:
            _claim(node_id)

    return {
        node_id: TransitionEffect(kind="delete", subtree_root=owner_by_id[node_id])
        for node_id in removed
    }


def _subtree_ids_present(nodes: Any, roots: Iterable[str]) -> set[str]:
    """Return offered/pinned descendants of ``roots`` (cycle-safe).

    The root itself need not be present: an orphan whose ``parent_id`` names
    a deleted root is still structurally inside the tombstoned subtree and
    must be removed.
    """

    present: set[str] = set()
    children_by_parent: dict[str, list[str]] = {}
    for node in nodes:
        if not isinstance(node, Mapping) or node.get("id") is None:
            continue
        node_id = str(node["id"])
        present.add(node_id)
        parent_id = node.get("parent_id")
        if parent_id is None:
            parent_id = node.get("parentId")
        if parent_id is not None:
            children_by_parent.setdefault(str(parent_id), []).append(node_id)

    visited: set[str] = set()
    stack = [str(root) for root in roots]
    while stack:
        current = stack.pop()
        if current in visited:
            continue
        visited.add(current)
        stack.extend(children_by_parent.get(current, ()))
    return visited & present


# ---------------------------------------------------------------------------
# Derivation (the before/after field mask for one COW attempt)
# ---------------------------------------------------------------------------


# @beacon[
#   id=auto-beacon@derive_transition_effects-di3n,
#   role=derive_transition_effects,
#   slice_labels=mci-cache-cow,
#   kind=ast,
#   comment=Derive sparse owned-field effects with the unchanged identity shortcut and canonical authority; detach newly captured create records and field values from caller/publication aliases.,
# ]
def derive_transition_effects(
    before_nodes: Any, after_nodes: Any
) -> dict[str, TransitionEffect]:
    """Derive the sparse transition effects of ONE bridge COW attempt.

    ``before_nodes``/``after_nodes`` are the pre- and post-transform
    container lists (NOT views): transforms clone node dicts before
    mutating, so a node object that is not identity-equal to its
    predecessor was touched; create records and changed field values are
    captured as detached fragments, never aliases into the candidate.
    An id absent from the final list is a local
    deletion; an id present only after is a local creation.  Both sides are
    canonicalized under JOURNAL_SEMANTIC (zero additional decoding — the
    eager patchers already normalize into semantic space), and the diff is
    restricted to the OWNED vocabulary with text-field None ≈ ""
    equivalence, so model-dump noise (explicit ``None`` text fields,
    timestamp/alias churn) never manufactures a phantom effect.
    """

    before_by_id: dict[str, Mapping[str, Any]] = {}
    for node in before_nodes:
        if isinstance(node, Mapping) and node.get("id") is not None:
            before_by_id[str(node["id"])] = node
    effects: dict[str, TransitionEffect] = {}
    seen: set[str] = set()
    for node in after_nodes:
        if not isinstance(node, Mapping) or node.get("id") is None:
            continue
        node_id = str(node["id"])
        seen.add(node_id)
        before = before_by_id.get(node_id)
        if before is node:
            continue  # identity — untouched by this attempt
        if before is None:
            effects[node_id] = TransitionEffect(kind="create", record=copy.deepcopy(_canonical(node)))
            continue
        before_c = _canonical(before)
        after_c = _canonical(node)
        fields: dict[str, FieldWrite] = {}
        for key in TRANSITION_OWNED_FIELDS:
            prior = _comparable(key, before_c.get(key))
            verified = _comparable(key, after_c.get(key))
            if prior != verified:
                fields[key] = FieldWrite(prior=copy.deepcopy(prior), verified=copy.deepcopy(verified))
        if fields:
            effects[node_id] = TransitionEffect(
                kind="fields", fields=MappingProxyType(fields)
            )
    removed = [node_id for node_id in before_by_id if node_id not in seen]
    if removed:
        effects.update(_local_delete_effects(before_by_id, removed))
    return effects


# ---------------------------------------------------------------------------
# Composition (publication order; last local effect wins only on overlap)
# ---------------------------------------------------------------------------


# @beacon[
#   id=auto-beacon@compose_transition_effect-99ca,
#   role=compose_transition_effect,
#   slice_labels=mci-cache-cow,
#   kind=ast,
# ]
def compose_transition_effect(
    existing: TransitionEffect | None, new: TransitionEffect
) -> TransitionEffect:
    """Compose two chronological effects on one node into one.

    A create retains its original canonical record while later writes remain
    sparse in ``fields``.  Repeated field writes retain their intermediate
    locally-verified values so a delayed offer captured between two writes
    is recognized as stale rather than misclassified as a later conflict.
    """

    if new.kind == "delete":
        return new
    if existing is None or existing.kind == "delete":
        return new
    if new.kind == "create":
        return new  # defensive — same-ID recreation is impossible server-side
    merged_fields = _compose_field_maps(existing.fields, new.fields)
    if existing.kind == "create":
        # Any new local write resets absence evidence: a refresh already in
        # flight may legitimately omit the newly written state again.
        return TransitionEffect(
            kind="create",
            fields=merged_fields,
            record=existing.record,
            absent_full_refreshes=0,
        )
    return TransitionEffect(kind="fields", fields=merged_fields)


# ---------------------------------------------------------------------------
# The ONE sparse merge authority (Contract D)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TransitionMerge:
    """The pure result of rebasing one lineage offer through the effect store."""

    rebased: CacheView
    surviving_effects: Mapping[str, TransitionEffect]
    dirty_ids: frozenset[str]


# @beacon[
#   id=coherence@merge-transition-effects,
#   slice_labels=ra-coherence,ra-workflowy-cache,mci-cache-cow,
#   kind=ast,
#   comment=Sparse transition merge: canonicalize compared effect targets only, prepare changed fragments and transfer the successor while sharing untouched nodes. Root-scoped DELETE and per-field causal authority are unchanged; CREATE absence carries until two full-refresh observations, with non-authoritative offers not advancing evidence.,
# ]
def merge_offer_with_transition_effects(
    offer: CacheView,
    effects: Mapping[str, TransitionEffect],
    *,
    publication_kind: str | None = None,
) -> TransitionMerge:
    """Rebase one strict-newer lineage offer through transition effects.

    Canonicalize only compared effect targets. A changed result detaches its
    changed fragments and transfers its private containers; other nodes are
    shared read-only. A no-change merge returns the identical offer.

    Create/existence authority and post-create sparse writes are evaluated
    separately: offered presence reflects creation, while every later field
    write still uses its causal local-write chain.  Delete authority is
    ROOT-SCOPED: a root tombstone removes exactly the nodes the OFFERED
    topology places beneath its root (unknown added descendants and orphans
    naming an absent root included), while an offered-alive node whose
    offered parentage lies outside every tombstoned root's offered subtree
    is never removed — it provably escaped before the delete committed.

    An absent CREATE remains carried across every non-authoritative offer
    (``compat_patch``, ``checkpoint``, ``job_basis``, or unknown) and across
    the first ``full_refresh`` offer, which may have been requested before
    the create committed.  A second successive full-refresh absence is
    authoritative server-side deletion evidence: the offer wins, the effect
    retires, and one final dirty mark remains.  Any intervening local write
    resets the counter during composition.
    """

    offered_raw_by_id = offer.nodes_by_id
    offered_nodes = offer.nodes_export["nodes"]

    surviving: dict[str, TransitionEffect] = {}
    dirty: set[str] = set()
    removals: set[str] = set()
    replacements: dict[str, dict[str, Any]] = {}
    appends: list[dict[str, Any]] = []

    for node_id, effect in effects.items():
        offered_raw = offered_raw_by_id.get(node_id)
        if effect.kind == "delete":
            if not _is_root_tombstone(node_id, effect):
                # Descendant marker — compositional only (it retired the
                # descendant's earlier create/fields effects).  Absence
                # authority belongs to its root; an offered-alive node
                # outside every tombstoned root's offered subtree escaped
                # before the delete and must never be removed or pinned.
                continue
            scoped_ids = _subtree_ids_present(offered_nodes, (node_id,))
            if not scoped_ids:
                continue  # reflected — no offered member remains in the subtree
            removals.update(scoped_ids)
            surviving[node_id] = effect
            dirty.add(node_id)
            dirty.update(scoped_ids)
            continue

        # Canonicalize only present non-delete effect targets, not the account.
        offered_c = _canonical(offered_raw) if offered_raw is not None else None
        if effect.kind == "create" and offered_c is None:
            dirty.add(node_id)
            if publication_kind == "full_refresh":
                absent_full_refreshes = effect.absent_full_refreshes + 1
                if absent_full_refreshes >= 2:
                    # Two serialized full-refresh publications cannot both
                    # predate this local create.  The second absence is sound
                    # evidence that another Tier-1 actor deleted the node.
                    continue
                effect = TransitionEffect(
                    kind="create",
                    fields=effect.fields,
                    record=effect.record,
                    absent_full_refreshes=absent_full_refreshes,
                )
            appends.append(_materialize_created_record(effect))
            surviving[node_id] = effect
            continue

        # For an offered-present create, existence is reflected but its
        # post-create field writes remain causally live.  Any surviving mask
        # becomes an ordinary fields effect because creation itself no longer
        # needs carrying.  Ordinary fields effects use the same path.
        if offered_c is None:
            dirty.add(node_id)  # uncertain disappearance — offer wins, retire dirty
            continue

        kept: dict[str, FieldWrite] = {}
        patch: dict[str, Any] | None = None
        for key, write in effect.fields.items():
            offered_value = _comparable(key, offered_c.get(key))
            if offered_value == write.verified:
                continue  # reflected — the offer already carries the latest write
            predecessors = (write.prior, *write.intermediate)
            if any(offered_value == predecessor for predecessor in predecessors):
                # Delayed offer — it predates the latest local write (possibly
                # at an intermediate value in a repeated-write chain).
                if patch is None:
                    patch = dict(offered_raw) if offered_raw is not None else {}
                patch[key] = write.verified
                kept[key] = write
                dirty.add(node_id)
            else:
                # Genuine conflict — the offer wins; the field retires dirty.
                dirty.add(node_id)
        if kept:
            surviving[node_id] = TransitionEffect(
                kind="fields", fields=MappingProxyType(kept)
            )
        if patch is not None:
            replacements[node_id] = patch

    if not (removals or replacements or appends):
        return TransitionMerge(offer, MappingProxyType(surviving), frozenset(dirty))

    new_nodes: list[dict[str, Any]] = []
    for node in offered_nodes:
        node_id = (
            str(node.get("id"))
            if isinstance(node, dict) and node.get("id") is not None
            else None
        )
        if node_id is not None and node_id in removals:
            continue
        if node_id is not None and node_id in replacements:
            new_nodes.append(replacements[node_id])
        else:
            new_nodes.append(node)
    new_nodes.extend(appends)
    payload = dict(offer.nodes_export)
    payload["nodes"] = new_nodes
    payload = prepare_cow_payload(offer, payload)
    rebased = CacheView.build(
        epoch_number=offer.epoch_number,
        epoch_id=offer.epoch_id,
        adopted_sequence=offer.adopted_sequence,
        nodes_export=payload,
        account_scope_id=offer.account_scope_id,
        ownership="transferred",
    )
    return TransitionMerge(rebased, MappingProxyType(surviving), frozenset(dirty))


# ---------------------------------------------------------------------------
# Task-local overlay (Contract B — read-your-writes without marker movement)
# ---------------------------------------------------------------------------


def apply_transition_effects_to_nodes(
    nodes: Any, effects: Mapping[str, TransitionEffect]
) -> list[Any]:
    """Apply one operation's own effects to its pinned task view.

    Markers never move.  Create records receive their post-create sparse
    writes, and ROOT tombstones expand over every descendant visible in
    the pinned view using the same cycle-safe root-scoped authority as the
    epoch/recovery merge (descendant markers carry no expansion authority
    of their own).
    """

    if not effects:
        return list(nodes)

    node_list = list(nodes)
    by_id: dict[str, Any] = {}
    for node in node_list:
        if isinstance(node, dict) and node.get("id") is not None:
            by_id[str(node["id"])] = node

    delete_roots = {
        node_id
        for node_id, effect in effects.items()
        if _is_root_tombstone(node_id, effect)
    }
    deletes = _subtree_ids_present(node_list, delete_roots)

    result: list[Any] = []
    for node in node_list:
        node_id = (
            str(node.get("id"))
            if isinstance(node, dict) and node.get("id") is not None
            else None
        )
        if node_id is not None and node_id in deletes:
            continue
        effect = effects.get(node_id) if node_id is not None else None
        if effect is None or effect.kind != "fields":
            result.append(node)
            continue
        patched = dict(node)
        for key, write in effect.fields.items():
            patched[key] = write.verified
        result.append(patched)
    for node_id, effect in effects.items():
        if effect.kind == "create" and node_id not in by_id:
            result.append(_materialize_created_record(effect))
    return result


# ---------------------------------------------------------------------------
# The per-publisher policy (store + atomic merge commit)
# ---------------------------------------------------------------------------


# @beacon[
#   id=coherence@transition-effect-policy,
#   slice_labels=ra-coherence,ra-workflowy-cache,mci-cache-cow,
#   kind=ast,
#   comment=S03 transition-effect authority (Family-5 compound + F-3 + WO-S03-CLOSURE-F5-1 closure): one pure merge + one per-publisher policy consumed by EVERY epoch/lineage publication route, including CF-S03CR-10 recovery. Creation/existence is distinct from sparse post-create writes; absent creates carry across non-authoritative offers and one full-refresh race window, then retire dirty on a second successive full-refresh absence; repeated fields retain intermediate local values; delete authority is ROOT-SCOPED subtree absence over the OFFERED topology — escaped descendants (offered alive outside every tombstoned root) are never removed or pinned, while unknown added descendants beneath a root are removed. Non-clean resolution always leaves legacy-dirty evidence; store mutation remains atomic with the view swap.,
# ]
class TransitionEffectPolicy:
    """Per-process transition-effect store and merge-commit authority.

    One instance rides each client's ``CacheViewPublisher`` so client
    lineage publication, ordinary cross-process adoption, and clean-base
    recovery all apply the same compound-effect semantics.  Creation is
    tracked separately from later sparse writes, repeated writes retain
    intermediate predecessors, and delete authority is root-scoped subtree
    absence in the offered topology (escaped descendants are never removed
    or pinned).  Absent creates carry across non-authoritative publications
    and one full-refresh race window, then retire dirty on a second successive
    full-refresh absence.  Store changes commit only under the publisher
    mutex with the corresponding view swap.
    """

    def __init__(
        self, *, on_dirty: Callable[[Iterable[str]], None] | None = None
    ) -> None:
        self._effects: dict[str, TransitionEffect] = {}
        self._on_dirty = on_dirty

    def snapshot_effects(self) -> Mapping[str, TransitionEffect]:
        """One consistent read of the current store (COW-replaced, never torn)."""

        return self._effects

    def compose_into(self, new_effects: Mapping[str, TransitionEffect]) -> None:
        """Accumulate one winning bridge attempt's effects (publisher-mutex only)."""

        if not new_effects:
            return
        merged = dict(self._effects)
        for node_id, effect in new_effects.items():
            merged[node_id] = compose_transition_effect(merged.get(node_id), effect)
        self._effects = merged

    def commit_merge(
        self,
        surviving_effects: Mapping[str, TransitionEffect],
        dirty_ids: Iterable[str],
    ) -> None:
        """Commit one merge outcome atomically with its view swap.

        Runs INSIDE the publisher critical section and must never raise:
        replaces the store with the merge's surviving effects and marks the
        merge's legacy-dirty evidence (AFTER any legacy clear/discard the
        caller's own commit already ran, so post-cut dirt survives — the
        S02 cut-token rule at the process-local layer).
        """

        self._effects = dict(surviving_effects)
        dirty = tuple(dirty_ids)
        if dirty and self._on_dirty is not None:
            self._on_dirty(dirty)
