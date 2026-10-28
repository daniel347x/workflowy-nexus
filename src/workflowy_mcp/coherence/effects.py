"""Semantic effect schema and pure reducers (S01 §8).

A closed tagged union of five effect types with explicit per-effect field
authority (Axiom 7), two application modes (audit amendment F1), and an
all-or-none pure application contract.

Field authority summary:

- ``node_create``    — full canonical record for a transaction-created node.
- ``node_fields``    — declared content/state fields only; NEVER
  ``id`` / ``parent_id`` / ``priority``.
- ``node_parentage`` — only ``parent_id`` for one node (explicit null/root).
- ``child_order``    — complete direct-child membership, listed children's
  ``parent_id``, and sibling priority ranks; NEVER a pre-existing sibling's
  content fields.
- ``tombstone``      — absence for an explicitly enumerated set of node IDs.

Application modes:

- ``publication_strict`` — the PRODUCING transaction validating/applying its
  own effects before journal publication.  Any anomaly is a genuine bug or
  race: fail closed before anything is applied or published.
- ``replay_authoritative`` — a COMMITTED journal record applied to a
  process's cache.  The record is settled truth; the cache may legitimately
  contain NEWER out-of-band GUI state (external dirt is stale-but-safe).
  Ordinary cache-state divergence yields structured diagnostics, never hard
  failure.  Schema violations, field-authority violations, account
  mismatches, and sequence gaps/reversals remain FAIL-CLOSED in BOTH modes.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Iterable, Mapping, Sequence
from enum import Enum
from typing import Any, Literal

from .errors import (
    CoherenceAccountMismatchError,
    CoherenceSchemaError,
    EffectApplicationError,
    EffectValidationError,
    JournalIntegrityError,
    UnsupportedCoherenceSchemaError,
)

EFFECT_VERSION = 1

NODE_CREATE = "node_create"
NODE_FIELDS = "node_fields"
NODE_PARENTAGE = "node_parentage"
CHILD_ORDER = "child_order"
TOMBSTONE = "tombstone"

EFFECT_TYPES = frozenset({NODE_CREATE, NODE_FIELDS, NODE_PARENTAGE, CHILD_ORDER, TOMBSTONE})

# Fields a node_fields mask may own (plus explicitly approved future
# nonstructural fields).  NEVER id / parent_id / priority.
CONTENT_FIELDS = frozenset({"name", "note", "data", "createdAt", "modifiedAt", "completedAt"})
_NON_MASKABLE = frozenset({"id", "parent_id", "priority"})

# Deterministic synthetic sibling ranks (frozen from P0's observed sort
# contract: ascending integer priorities, origin 100, step 100).
SYNTHETIC_PRIORITY_BASE = 100
SYNTHETIC_PRIORITY_STEP = 100


class ApplicationMode(Enum):
    """Effect application mode — an explicit argument, never inferred."""

    PUBLICATION_STRICT = "publication_strict"
    REPLAY_AUTHORITATIVE = "replay_authoritative"


def synthetic_priority_ranks(count: int) -> list[int]:
    """Deterministic consecutive synthetic ranks for ``count`` siblings."""

    return [SYNTHETIC_PRIORITY_BASE + i * SYNTHETIC_PRIORITY_STEP for i in range(count)]


def _require_nonblank_str(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EffectValidationError(f"{what} must be a nonblank string")
    return value


def _unique_id_list(value: Any, what: str) -> list[str]:
    if not isinstance(value, (list, tuple)):
        raise EffectValidationError(f"{what} must be an array of node ids")
    ids: list[str] = []
    seen: set[str] = set()
    for item in value:
        node_id = _require_nonblank_str(item, f"{what} entry")
        if node_id in seen:
            raise EffectValidationError(f"{what} contains duplicate id {node_id!r}")
        seen.add(node_id)
        ids.append(node_id)
    return ids


# ---------------------------------------------------------------------------
# SemanticEffect — the closed tagged union
# ---------------------------------------------------------------------------


# @beacon[
#   id=coherence@semantic-effects,
#   slice_labels=carto-governance,mci-journal-effects,
#   kind=ast,
#   comment=Closed semantic-effect union with strict envelope validation and isolated effect fragments; one all-or-none engine preserves per-effect authority in both application modes, with pure COW input or explicitly private working-copy mutation.,
# ]
class SemanticEffect:
    """One immutable semantic effect (``effect_type`` + validated payload).

    Payload node records are canonical plain dicts; unknown node/cache
    metadata keys inside them are preserved (the documented extensibility
    surface).  Effect ENVELOPE keys are exact and fail closed on extras.
    The payload is deeply immutable by ownership: it never escapes except
    as a deep copy (audit amendment A9).
    """

    __slots__ = ("effect_type", "_payload")

    _PAYLOAD_KEYS: dict[str, frozenset[str]] = {
        NODE_CREATE: frozenset({"node"}),
        NODE_FIELDS: frozenset({"node_id", "field_mask", "values"}),
        NODE_PARENTAGE: frozenset({"node_id", "parent_id"}),
        CHILD_ORDER: frozenset(
            {"parent_uuid", "ordered_child_ids", "priority_by_id", "canonical_records"}
        ),
        TOMBSTONE: frozenset({"node_ids"}),
    }

    def __init__(self, effect_type: str, payload: Mapping[str, Any]) -> None:
        if effect_type not in EFFECT_TYPES:
            raise EffectValidationError(f"unknown effect_type {effect_type!r}")
        object.__setattr__(self, "effect_type", effect_type)
        object.__setattr__(self, "_payload", self._validate(effect_type, payload))

    def __setattr__(self, key: str, value: Any) -> None:  # immutability
        raise AttributeError("SemanticEffect is immutable")

    @property
    def payload(self) -> dict[str, Any]:
        """Deeply immutable by ownership: every read returns a deep copy."""

        return copy.deepcopy(self._payload)

    def __eq__(self, other: Any) -> bool:
        return (
            isinstance(other, SemanticEffect)
            and self.effect_type == other.effect_type
            and self.payload == other.payload
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"SemanticEffect({self.effect_type!r}, {self.payload!r})"

    # -- constructors -----------------------------------------------------

    @classmethod
    def node_create(cls, node: Mapping[str, Any]) -> SemanticEffect:
        return cls(NODE_CREATE, {"node": node})

    @classmethod
    def node_fields(
        cls, node_id: str, field_mask: Iterable[str], values: Mapping[str, Any]
    ) -> SemanticEffect:
        return cls(
            NODE_FIELDS,
            {"node_id": node_id, "field_mask": list(field_mask), "values": dict(values)},
        )

    @classmethod
    def node_parentage(cls, node_id: str, parent_id: str | None) -> SemanticEffect:
        return cls(NODE_PARENTAGE, {"node_id": node_id, "parent_id": parent_id})

    @classmethod
    def child_order(
        cls,
        parent_uuid: str,
        ordered_child_ids: Sequence[str],
        *,
        priority_by_id: Mapping[str, int] | None = None,
        canonical_records: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> SemanticEffect:
        payload: dict[str, Any] = {
            "parent_uuid": parent_uuid,
            "ordered_child_ids": list(ordered_child_ids),
            "priority_by_id": None if priority_by_id is None else dict(priority_by_id),
            "canonical_records": (
                None if canonical_records is None else dict(canonical_records)
            ),
        }
        return cls(CHILD_ORDER, payload)

    @classmethod
    def tombstone(cls, node_ids: Sequence[str]) -> SemanticEffect:
        return cls(TOMBSTONE, {"node_ids": list(node_ids)})

    # -- validation -------------------------------------------------------

    @classmethod
    def _validate(cls, effect_type: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise EffectValidationError(f"{effect_type}: payload must be an object")
        allowed = cls._PAYLOAD_KEYS[effect_type]
        extra = sorted(set(payload) - allowed)
        if extra:
            raise EffectValidationError(f"{effect_type}: unknown payload keys {extra}")
        missing = sorted(k for k in _required_payload_keys(effect_type) if k not in payload)
        if missing:
            raise EffectValidationError(f"{effect_type}: missing payload keys {missing}")

        if effect_type == NODE_CREATE:
            node = payload["node"]
            if not isinstance(node, Mapping):
                raise EffectValidationError("node_create: node must be an object")
            node = copy.deepcopy(dict(node))
            node_id = node.get("id")
            if isinstance(node_id, bool) or node_id is None or not str(node_id).strip():
                raise EffectValidationError("node_create: node.id required and nonblank")
            node["id"] = str(node_id)
            _require_json_safe_payload(node, "node_create.node")
            return {"node": node}

        if effect_type == NODE_FIELDS:
            node_id = _require_nonblank_str(payload["node_id"], "node_fields.node_id")
            mask = payload["field_mask"]
            if not isinstance(mask, (list, tuple)) or not mask:
                raise EffectValidationError("node_fields: field_mask must be a nonempty array")
            mask_list: list[str] = []
            for field in mask:
                if not isinstance(field, str) or not field:
                    raise EffectValidationError("node_fields: mask entries must be strings")
                if field in _NON_MASKABLE:
                    raise EffectValidationError(
                        f"node_fields: field {field!r} is structural and can never be masked"
                    )
                if field not in CONTENT_FIELDS:
                    raise EffectValidationError(
                        f"node_fields: field {field!r} is not an approved content/state field"
                    )
                mask_list.append(field)
            if len(set(mask_list)) != len(mask_list):
                raise EffectValidationError("node_fields: duplicate mask entries")
            values = payload["values"]
            if not isinstance(values, Mapping):
                raise EffectValidationError("node_fields: values must be an object")
            if set(values) != set(mask_list):
                raise EffectValidationError(
                    "node_fields: values keys must equal the field mask exactly"
                )
            _require_json_safe_payload(dict(values), "node_fields.values")
            return {
                "node_id": node_id,
                "field_mask": mask_list,
                "values": copy.deepcopy(dict(values)),
            }

        if effect_type == NODE_PARENTAGE:
            node_id = _require_nonblank_str(payload["node_id"], "node_parentage.node_id")
            parent_id = payload["parent_id"]
            if parent_id is not None:
                parent_id = _require_nonblank_str(parent_id, "node_parentage.parent_id")
            return {"node_id": node_id, "parent_id": parent_id}

        if effect_type == CHILD_ORDER:
            parent_uuid = _require_nonblank_str(
                payload["parent_uuid"], "child_order.parent_uuid"
            )
            ordered = _unique_id_list(
                payload["ordered_child_ids"], "child_order.ordered_child_ids"
            )
            priority_by_id = payload.get("priority_by_id")
            if priority_by_id is not None:
                if not isinstance(priority_by_id, Mapping):
                    raise EffectValidationError("child_order: priority_by_id must be an object")
                extra_ids = sorted(set(priority_by_id) - set(ordered))
                if extra_ids:
                    raise EffectValidationError(
                        f"child_order: priority_by_id has unlisted ids {extra_ids}"
                    )
                priority_by_id = dict(priority_by_id)
            canonical_records = payload.get("canonical_records")
            if canonical_records is not None:
                if not isinstance(canonical_records, Mapping):
                    raise EffectValidationError(
                        "child_order: canonical_records must be an object"
                    )
                records: dict[str, Any] = {}
                for record_id, record in canonical_records.items():
                    if not isinstance(record, Mapping):
                        raise EffectValidationError(
                            "child_order: canonical_records entries must be objects"
                        )
                    record = copy.deepcopy(dict(record))
                    rid = record.get("id")
                    if rid != record_id:
                        raise EffectValidationError(
                            f"child_order: canonical_records key {record_id!r} != record id {rid!r}"
                        )
                    if record_id not in set(ordered):
                        raise EffectValidationError(
                            f"child_order: canonical record {record_id!r} is not a listed child"
                        )
                    _require_json_safe_payload(record, "child_order.canonical_records entry")
                    records[record_id] = record
                canonical_records = records
            return {
                "parent_uuid": parent_uuid,
                "ordered_child_ids": ordered,
                "priority_by_id": priority_by_id,
                "canonical_records": canonical_records,
            }

        # TOMBSTONE
        return {"node_ids": _unique_id_list(payload["node_ids"], "tombstone.node_ids")}

    # -- serialization ----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "effect_type": self.effect_type,
            "effect_version": EFFECT_VERSION,
            "payload": copy.deepcopy(self.payload),
        }

    def __hash__(self) -> int:
        import json

        return hash(
            json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), default=str)
        )


def _required_payload_keys(effect_type: str) -> frozenset[str]:
    if effect_type == CHILD_ORDER:
        return frozenset({"parent_uuid", "ordered_child_ids"})
    return SemanticEffect._PAYLOAD_KEYS[effect_type]


def _require_json_safe_payload(value: Any, what: str) -> None:
    import json as _json

    try:
        _json.dumps(value)
    except (TypeError, ValueError) as exc:
        raise EffectValidationError(f"{what} must be JSON-safe: {exc}") from exc


def effect_from_dict(payload: Mapping[str, Any]) -> SemanticEffect:
    """Strict effect-envelope decode.  Unknown types/versions fail closed."""

    if not isinstance(payload, Mapping):
        raise CoherenceSchemaError("effect envelope must be an object")
    extra = sorted(set(payload) - {"effect_type", "effect_version", "payload"})
    if extra:
        raise CoherenceSchemaError(f"effect envelope: unknown keys {extra}")
    version = payload.get("effect_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise CoherenceSchemaError("effect envelope: effect_version must be an integer")
    if version != EFFECT_VERSION:
        raise UnsupportedCoherenceSchemaError(
            f"effect envelope: unsupported effect_version {version}"
        )
    effect_type = payload.get("effect_type")
    if not isinstance(effect_type, str) or effect_type not in EFFECT_TYPES:
        raise CoherenceSchemaError(f"effect envelope: unknown effect_type {effect_type!r}")
    return SemanticEffect(effect_type, payload.get("payload"))


# ---------------------------------------------------------------------------
# Transaction validation — §8.2 / §8.4
# ---------------------------------------------------------------------------


def _transaction_authority(
    effects: Sequence[SemanticEffect],
) -> tuple[set[str], dict[str, set[str]], dict[str, dict[str, Any]], set[str], set[str]]:
    """Transaction-level authority analysis (A3).

    Returns (created, content_masks, content_values, tombstoned, reparented):
    - created: ids installed by same-transaction node_create effects;
    - content_masks: id -> union of content fields owned by node_fields;
    - content_values: id -> field -> after-value (conflicting same-field
      values inside one transaction are a validation error);
    - tombstoned / reparented: ids owned by tombstone / node_parentage.
    """

    created: set[str] = set()
    content_masks: dict[str, set[str]] = {}
    content_values: dict[str, dict[str, Any]] = {}
    tombstoned: set[str] = set()
    reparented: set[str] = set()
    for effect in effects:
        if effect.effect_type == NODE_CREATE:
            node_id = effect.payload["node"]["id"]
            if node_id in created:
                raise EffectValidationError(
                    f"transaction creates node {node_id!r} more than once"
                )
            created.add(node_id)
        elif effect.effect_type == NODE_FIELDS:
            node_id = effect.payload["node_id"]
            mask = content_masks.setdefault(node_id, set())
            values = content_values.setdefault(node_id, {})
            for field_name in effect.payload["field_mask"]:
                mask.add(field_name)
                new_value = effect.payload["values"][field_name]
                if field_name in values and values[field_name] != new_value:
                    raise EffectValidationError(
                        f"transaction carries conflicting node_fields values for "
                        f"{node_id!r}.{field_name}"
                    )
                values[field_name] = new_value
        elif effect.effect_type == TOMBSTONE:
            tombstoned.update(effect.payload["node_ids"])
        elif effect.effect_type == NODE_PARENTAGE:
            reparented.add(effect.payload["node_id"])
    return created, content_masks, content_values, tombstoned, reparented


# @beacon[
#   id=auto-beacon@validate_transaction_effects-znku,
#   role=validate_transaction_effects,
#   slice_labels=mci-journal-effects,
#   kind=ast,
#   comment=Public mapping-scan accessor over the one whole-transaction validation body; indexed workspaces supply the same ordered pre-transaction membership without scanning unrelated nodes.,
# ]
def validate_transaction_effects(
    effects: Sequence[SemanticEffect],
    cache_by_id: Mapping[str, Mapping[str, Any]] | None = None,
    *,
    mode: ApplicationMode,
) -> tuple[SemanticEffect, ...]:
    """Validate one transaction's effects as a whole before application.

    Returns the validated effect tuple.  In ``publication_strict`` mode with
    a supplied cache, membership anomalies fail closed; in
    ``replay_authoritative`` mode they are deferred to apply-time
    diagnostics.
    """

    return _validate_transaction_effects(
        effects, cache_by_id, mode=mode,
        children_for_parent=lambda parent: (
            node for node in cache_by_id.values() if node.get("parent_id") == parent
        ) if cache_by_id is not None else (),
    )


def _validate_transaction_effects(
    effects: Sequence[SemanticEffect],
    cache_by_id: Mapping[str, Mapping[str, Any]] | None,
    *,
    mode: ApplicationMode,
    children_for_parent: Callable[[str], Iterable[Mapping[str, Any]]],
) -> tuple[SemanticEffect, ...]:
    """The single validation body; accessors select scan or indexed membership."""
    if not isinstance(mode, ApplicationMode):
        raise EffectValidationError("application mode must be an explicit ApplicationMode")
    if not effects:
        raise EffectValidationError("transaction effects must be nonempty")
    for effect in effects:
        if not isinstance(effect, SemanticEffect):
            raise EffectValidationError("transaction effects must be SemanticEffect objects")
    created, content_masks, content_values, tombstoned, reparented = _transaction_authority(effects)
    verified = set(content_masks)

    # Whole-transaction conflict/order discipline (audit residual R1).
    contradictions = sorted(created & tombstoned)
    if contradictions:
        raise EffectValidationError(
            f"transaction both creates and tombstones: {contradictions}"
        )
    tombstone_positions: dict[str, int] = {}
    for index, effect in enumerate(effects):
        if effect.effect_type == TOMBSTONE:
            for tombstoned_id in effect.payload["node_ids"]:
                tombstone_positions.setdefault(tombstoned_id, index)
    create_positions = {
        effect.payload["node"]["id"]: index
        for index, effect in enumerate(effects)
        if effect.effect_type == NODE_CREATE
    }
    # Structural owner maps (audit residual: inter-effect conflict matrix).
    # Two effects owning the same final structural state must AGREE.
    parentage_owner: dict[str, Any] = {}
    order_owner: dict[str, tuple] = {}
    listed_owner: dict[str, str] = {}
    for effect in effects:
        if effect.effect_type == NODE_PARENTAGE:
            target = effect.payload["node_id"]
            value = effect.payload["parent_id"]
            if target in parentage_owner and parentage_owner[target] != value:
                raise EffectValidationError(
                    f"conflicting node_parentage values for {target!r}: "
                    f"{parentage_owner[target]!r} vs {value!r}"
                )
            parentage_owner[target] = value
        elif effect.effect_type == CHILD_ORDER:
            parent_uuid = effect.payload["parent_uuid"]
            declaration = (
                tuple(effect.payload["ordered_child_ids"]),
                None
                if effect.payload.get("priority_by_id") is None
                else tuple(sorted(effect.payload["priority_by_id"].items())),
                None
                if effect.payload.get("canonical_records") is None
                else tuple(sorted(effect.payload["canonical_records"].keys())),
            )
            if parent_uuid in order_owner and order_owner[parent_uuid] != declaration:
                raise EffectValidationError(
                    f"conflicting child_order declarations for parent {parent_uuid!r}"
                )
            order_owner[parent_uuid] = declaration
            for child_id in effect.payload["ordered_child_ids"]:
                if child_id in listed_owner and listed_owner[child_id] != parent_uuid:
                    raise EffectValidationError(
                        f"child {child_id!r} listed under two parents: "
                        f"{listed_owner[child_id]!r} and {parent_uuid!r}"
                    )
                listed_owner[child_id] = parent_uuid
    # tombstone absence authority vs child_order membership authority.
    listed_and_tombstoned = sorted(set(listed_owner) & tombstoned)
    if listed_and_tombstoned:
        raise EffectValidationError(
            f"tombstoned ids listed in a child_order effect: {listed_and_tombstoned}"
        )
    # node_parentage vs child_order agreement (both own final parentage).
    for node_id, parent_value in parentage_owner.items():
        listing_parent = listed_owner.get(node_id)
        if listing_parent is not None and listing_parent != parent_value:
            raise EffectValidationError(
                f"node_parentage({node_id!r} -> {parent_value!r}) disagrees with "
                f"child_order listing under {listing_parent!r}"
            )
        if (
            listing_parent is None
            and parent_value in order_owner
        ):
            raise EffectValidationError(
                f"node_parentage({node_id!r} -> {parent_value!r}) but the "
                f"child_order for {parent_value!r} does not list it"
            )
    for index, effect in enumerate(effects):
        if effect.effect_type in (NODE_FIELDS, NODE_PARENTAGE):
            # ORDER-AWARE: update-then-delete is coherent; writing to an id
            # AFTER its tombstone in the same transaction is contradictory.
            target = effect.payload["node_id"]
            tombstone_index = tombstone_positions.get(target)
            if tombstone_index is not None and index > tombstone_index:
                raise EffectValidationError(
                    f"{effect.effect_type} targets id {target!r} after its tombstone "
                    "in the same transaction"
                )
        if effect.effect_type == NODE_CREATE:
            continue
        if effect.effect_type == CHILD_ORDER:
            referenced = list(effect.payload["ordered_child_ids"])
        elif effect.effect_type in (NODE_FIELDS, NODE_PARENTAGE):
            referenced = [effect.payload["node_id"]]
        else:
            referenced = []
        for referenced_id in referenced:
            create_index = create_positions.get(referenced_id)
            if create_index is not None and index < create_index:
                raise EffectValidationError(
                    f"effect references transaction-created id {referenced_id!r} "
                    "before its node_create (noncanonical ordering)"
                )
    creates_by_id = {
        effect.payload["node"]["id"]: effect.payload["node"]
        for effect in effects
        if effect.effect_type == NODE_CREATE
    }
    for effect in effects:
        if effect.effect_type != CHILD_ORDER:
            continue
        records = effect.payload.get("canonical_records") or {}
        unauthorized = sorted(set(records) - created - verified)
        if unauthorized:
            raise EffectValidationError(
                "child_order canonical records are accepted only for ids the same "
                f"transaction created or content-verified: {unauthorized}"
            )
        for record_id, record in records.items():
            if record_id in created:
                # A canonical record for a TRANSACTION-CREATED id must AGREE
                # with the node_create record on every non-structural key
                # (R1): structure (parent_id/priority) belongs to child_order;
                # everything else has exactly one owner.
                created_node = creates_by_id[record_id]
                structural = {"parent_id", "priority"}
                record_keys = set(record) - structural
                create_keys = set(created_node) - structural
                if record_keys != create_keys or any(
                    record[key] != created_node[key] for key in record_keys
                ):
                    raise EffectValidationError(
                        f"child_order canonical record for transaction-created id "
                        f"{record_id!r} does not agree with its node_create record"
                    )
                continue
            mask = content_masks[record_id]
            values = content_values[record_id]
            # A canonical record for a PRE-EXISTING id is verification cargo:
            # every content field it carries must be explicitly owned by a
            # same-transaction node_fields mask (never an unverified overwrite),
            # and any owned value must agree with the content effect (the
            # transaction is order-independent).
            unverified = sorted(
                key for key in record if key in CONTENT_FIELDS and key not in mask
            )
            if unverified:
                raise EffectValidationError(
                    f"child_order canonical record for {record_id!r} carries content "
                    f"fields not owned by any same-transaction node_fields mask: "
                    f"{unverified}"
                )
            conflicts = sorted(
                field_name
                for field_name in mask
                if field_name in record and record[field_name] != values[field_name]
            )
            if conflicts:
                raise EffectValidationError(
                    f"child_order canonical record for {record_id!r} disagrees with "
                    f"same-transaction node_fields values on: {conflicts}"
                )
        if mode is ApplicationMode.PUBLICATION_STRICT and cache_by_id is not None:
            parent_uuid = effect.payload["parent_uuid"]
            ordered = effect.payload["ordered_child_ids"]
            listed = set(ordered)
            missing = [cid for cid in ordered if cid not in cache_by_id and cid not in created]
            if missing:
                raise EffectValidationError(
                    f"child_order lists children absent from cache and not created "
                    f"by this transaction: {missing}"
                )
            unexplained = [
                node["id"]
                for node in children_for_parent(parent_uuid)
                if node["id"] not in listed
                and node["id"] not in tombstoned
                and node["id"] not in reparented
            ]
            if unexplained:
                raise EffectValidationError(
                    f"child_order omits cached children of {parent_uuid!r} without a "
                    f"same-transaction tombstone/parentage explanation: {unexplained}"
                )
    return tuple(effects)


# ---------------------------------------------------------------------------
# Pure application — §8.3 / §8.4
# ---------------------------------------------------------------------------


def _resolve_ranks(
    ordered: list[str], priority_by_id: Mapping[str, Any] | None
) -> list[int]:
    """Preserve complete/consistent server priorities, else all-synthetic.

    The whole supplied map is discarded whenever it cannot reproduce the
    declared order — stale server values and synthetic values never mix.
    """

    if priority_by_id is not None and set(priority_by_id) == set(ordered):
        values = [priority_by_id[cid] for cid in ordered]
        if all(isinstance(v, int) and not isinstance(v, bool) for v in values):
            # The map must impose an UNAMBIGUOUS strict ordering that
            # reproduces the declared order exactly; duplicate values would
            # let cache storage order break the tie (A4).
            if all(values[i] < values[i + 1] for i in range(len(values) - 1)):
                return list(values)
    return synthetic_priority_ranks(len(ordered))


def _assert_ranks_reproduce(ordered: list[str], ranks: list[int]) -> None:
    """Priority-sort must reproduce ``ordered_child_ids`` exactly."""

    indices = sorted(range(len(ordered)), key=lambda i: ranks[i])
    if [ordered[i] for i in indices] != ordered:
        raise EffectApplicationError(
            "priority ranks do not reproduce the declared child order"
        )


def _diagnostic(
    diagnostics: list[dict[str, Any]] | None, kind: str, detail: str, **context: Any
) -> None:
    if diagnostics is not None:
        diagnostics.append({"kind": kind, "detail": detail, **context})


class _EffectState:
    """One ordered slot table, last-ID lookup and canonical-parent index.

    Deleted slots remain holes until finish; record commits update only the
    affected ID groups and parent buckets. Slots retain raw/duplicate IDs.
    """

    def __init__(self, payload: dict[str, Any]):
        self.payload = payload
        self.slots = list(payload["nodes"])
        # Only duplicate IDs need a group allocation. Canonical accounts
        # have one slot per ID; rebuilding singleton sets on S1's default
        # COW calls would add a large, unnecessary shallow allocation tax.
        self.groups: dict[str, set[int]] = {}
        self.lookup: dict[str, int] = {}
        self.parents: dict[str, set[int]] = {}
        self.consumed = False
        for slot, node in enumerate(self.slots):
            if not isinstance(node, dict):
                node = dict(node)
                self.slots[slot] = node
            node_id = node.get("id")
            if node_id is not None:
                key = str(node_id)
                prior = self.lookup.get(key)
                if prior is not None:
                    self.groups.setdefault(key, {prior}).add(slot)
                self.lookup[key] = slot
            parent = node.get("parent_id")
            if isinstance(parent, str):
                bucket = self.parents.get(parent)
                if bucket is None:
                    self.parents[parent] = {slot}
                else:
                    bucket.add(slot)

    def active(self):
        if self.consumed:
            raise EffectApplicationError("EffectWorkingCopy has already been finished")

    def _add(self, slot, node, *, ids=True, parents=True):
        node_id = node.get("id")
        if ids and node_id is not None:
            key = str(node_id)
            prior = self.lookup.get(key)
            if prior is None:
                self.lookup[key] = slot
            else:
                self.groups.setdefault(key, {prior}).add(slot)
                self.lookup[key] = max(self.groups[key])
        parent = node.get("parent_id")
        if parents and isinstance(parent, str):
            self.parents.setdefault(parent, set()).add(slot)

    def _remove(self, slot, node, *, ids=True, parents=True):
        node_id = node.get("id")
        if ids and node_id is not None:
            key = str(node_id)
            group = self.groups.get(key)
            if group is None:
                del self.lookup[key]
            else:
                group.remove(slot)
                self.lookup[key] = max(group)
                if len(group) == 1:
                    del self.groups[key]
        parent = node.get("parent_id")
        if parents and isinstance(parent, str):
            bucket = self.parents[parent]
            bucket.remove(slot)
            if not bucket:
                del self.parents[parent]

    def positions(self, key):
        group = self.groups.get(key)
        if group is not None:
            return group
        slot = self.lookup.get(key)
        return () if slot is None else (slot,)

    def first_position(self, key):
        group = self.groups.get(key)
        return self.lookup[key] if group is None else min(group)

    def validation_children(self, parent):
        selected = []
        for slot in self.parents.get(parent, ()):
            node = self.slots[slot]
            node_id = node.get("id")
            if node_id is not None:
                key = str(node_id)
                if self.lookup.get(key) == slot:
                    selected.append((self.first_position(key), node))
        return [node for _, node in sorted(selected, key=lambda pair: pair[0])]

    def finish(self):
        self.active()
        self.payload["nodes"] = [node for node in self.slots if node is not None]
        self.consumed = True
        return self.payload


class _EffectLookup(Mapping):
    def __init__(self, state):
        self.state = state

    def __getitem__(self, key):
        return self.state.slots[self.state.lookup[key]]

    def __len__(self):
        return len(self.state.lookup)

    def __iter__(self):
        return iter(sorted(self.state.lookup, key=self.state.first_position))


class _EffectTransaction:
    """Sparse rollback boundary: no copies of the account's index or slots."""

    def __init__(self, state):
        self.state = state
        self.replacements = {}
        self.removals = set()
        self.overrides = {}
        self.next_slot = len(state.slots)

    def node(self, slot):
        if slot in self.replacements:
            return self.replacements[slot]
        return self.state.slots[slot]

    def slot_for(self, key):
        return self.overrides.get(key, self.state.lookup.get(key))

    def get(self, key):
        slot = self.slot_for(key)
        return None if slot is None else self.node(slot)

    def mutable_slot(self, slot):
        if slot not in self.replacements:
            self.replacements[slot] = copy.deepcopy(dict(self.node(slot)))
        return self.replacements[slot]

    def mutable(self, key):
        return self.mutable_slot(self.slot_for(key))

    def install(self, node):
        key = node["id"]
        slot = self.slot_for(key)
        if slot is None:
            slot = self.next_slot
            self.next_slot += 1
        self.replacements[slot] = copy.deepcopy(node)
        self.overrides[key] = slot

    def remove(self, key):
        target = self.get(key)
        if target is None:
            return False
        # list.remove removes the FIRST equal entry, not necessarily the
        # last-ID lookup entry. Canonical string IDs bound equality to one
        # ID group; odd raw numeric/bool IDs can compare across groups.
        if isinstance(target.get("id"), str):
            candidates = set(self.state.positions(str(target["id"])))
            candidates.update(slot for slot, node in self.replacements.items()
                              if node.get("id") == target["id"])
        else:
            candidates = range(self.next_slot)
        for slot in sorted(candidates):
            if slot not in self.removals and self.node(slot) == target:
                self.removals.add(slot)
                break
        self.overrides[key] = None
        return True

    def children(self, parent):
        candidates = set(self.state.parents.get(parent, ()))
        candidates.update(slot for slot, node in self.replacements.items()
                          if node.get("parent_id") == parent)
        return [(slot, self.node(slot)) for slot in sorted(candidates)
                if slot not in self.removals and self.node(slot).get("parent_id") == parent]

    def commit(self):
        touched = set(self.replacements) | self.removals
        for slot in sorted(touched):
            new = None if slot in self.removals else self.replacements[slot]
            if slot < len(self.state.slots):
                old = self.state.slots[slot]
            else:
                old = None
                self.state.slots.extend([None] * (slot + 1 - len(self.state.slots)))
            old_id = old.get("id") if old is not None else None
            new_id = new.get("id") if new is not None else None
            same_id = old is not None and new is not None and (
                (old_id is None and new_id is None) or
                (old_id is not None and new_id is not None and str(old_id) == str(new_id)))
            old_parent = old.get("parent_id") if old is not None else None
            new_parent = new.get("parent_id") if new is not None else None
            old_parent = old_parent if isinstance(old_parent, str) else None
            new_parent = new_parent if isinstance(new_parent, str) else None
            same_parent = old is not None and new is not None and old_parent == new_parent
            if old is not None:
                self.state._remove(slot, old, ids=not same_id, parents=not same_parent)
            self.state.slots[slot] = new
            if new is not None:
                self.state._add(slot, new, ids=not same_id, parents=not same_parent)


# @beacon[
#   id=coherence@effect-working-copy,
#   slice_labels=mci-journal-effects,
#   kind=ast,
#   comment=Acquire one explicitly borrowed or transferred indexed private replay workspace; transactions stage touched slots atomically, and finish emits one ordered list and consumes mutation authority.,
# ]
class EffectWorkingCopy:
    """Explicitly owned replay state, never directly publishable as a payload."""

    def __init__(self, state: _EffectState):
        self._state = state

    @classmethod
    def from_payload(cls, payload, *, ownership: Literal["borrowed", "transferred"]):
        """Acquire once: borrowed deep-owns the payload; transferred consumes it.

        Both contracts build the same private indexes. Neither publishes,
        persists, consults a ContextVar or infers mutation authority.
        """
        if ownership not in ("borrowed", "transferred"):
            raise EffectApplicationError("workspace ownership must be borrowed or transferred")
        if not isinstance(payload, Mapping):
            raise EffectApplicationError("nodes_export must be a mapping with a 'nodes' list")
        if not isinstance(payload.get("nodes"), list):
            raise EffectApplicationError("nodes_export is missing its 'nodes' list")
        if ownership == "borrowed":
            payload = copy.deepcopy(dict(payload))
        elif type(payload) is not dict or type(payload["nodes"]) is not list:
            raise EffectApplicationError("transferred workspace requires ordinary dict/list containers")
        return cls(_EffectState(payload))

    def finish(self) -> dict[str, Any]:
        """Transfer the owned payload, compact live slots once, and consume self."""
        return self._state.finish()


# @beacon[
#   id=auto-beacon@apply_semantic_effects-9p6f,
#   role=apply_semantic_effects,
#   slice_labels=mci-journal-effects,
#   kind=ast,
#   comment=One indexed transactional engine: default COW shares untouched nodes read-only, explicit working_copy updates private replay state; only touched records/values are cloned, validation and late failures remain all-or-none.,
# ]
def apply_semantic_effects(
    nodes_export: Mapping[str, Any] | EffectWorkingCopy,
    effects: Sequence[SemanticEffect],
    *,
    mode: ApplicationMode,
    diagnostics: list[dict[str, Any]] | None = None,
    input_mode: Literal["copy_on_write", "working_copy"] = "copy_on_write",
) -> dict[str, Any] | EffectWorkingCopy:
    """Apply atomically through one engine with an explicit input contract.

    COW never mutates its input and shares unchanged nodes read-only.
    Working-copy input privately commits touched slots only after success.
    """

    if not isinstance(mode, ApplicationMode):
        raise EffectValidationError("application mode must be an explicit ApplicationMode")
    if input_mode == "working_copy" and isinstance(nodes_export, EffectWorkingCopy):
        state = nodes_export._state
        state.active()
        _apply_effect_transaction(state, effects, mode=mode, diagnostics=diagnostics)
        return nodes_export
    if input_mode != "copy_on_write" or not isinstance(nodes_export, Mapping):
        raise EffectApplicationError("nodes_export must be a mapping with a 'nodes' list")
    source_nodes = nodes_export.get("nodes")
    if not isinstance(source_nodes, list):
        raise EffectApplicationError("nodes_export is missing its 'nodes' list")

    # Rare dict-convertible raw entries retain the old validation-before-
    # conversion behavior. Canonical/Mapping exports take the indexed route.
    validated = False
    if any(not isinstance(node, dict) for node in source_nodes):
        input_index = {str(node["id"]): node for node in source_nodes
                       if isinstance(node, Mapping) and node.get("id") is not None}
        validate_transaction_effects(effects, input_index, mode=mode)
        validated = True
    state = _EffectState(dict(nodes_export))
    _apply_effect_transaction(state, effects, mode=mode, diagnostics=diagnostics,
                              validated=validated)
    return state.finish()


def _apply_effect_transaction(state, effects, *, mode, diagnostics, validated=False):
    """The sole application body, with a sparse transaction-local overlay."""
    state.active()
    if not validated:
        _validate_transaction_effects(effects, _EffectLookup(state), mode=mode,
                                      children_for_parent=state.validation_children)
    created, _masks, _values, tombstoned, reparented = _transaction_authority(effects)
    transaction = _EffectTransaction(state)

    strict = mode is ApplicationMode.PUBLICATION_STRICT

    for effect in effects:
        payload = effect.payload

        if effect.effect_type == NODE_CREATE:
            node = payload["node"]
            node_id = node["id"]
            if transaction.get(node_id) is not None:
                if strict:
                    raise EffectApplicationError(
                        f"node_create targets already-existing id {node_id!r}",
                        sequence=None,
                    )
                _diagnostic(
                    diagnostics,
                    "create_upsert",
                    f"node_create upserts existing id {node_id!r} (idempotent replay)",
                    node_id=node_id,
                )
            transaction.install(node)
            continue

        if effect.effect_type == NODE_FIELDS:
            node_id = payload["node_id"]
            node = transaction.get(node_id)
            if node is None:
                if strict:
                    raise EffectApplicationError(
                        f"node_fields targets absent node {node_id!r}"
                    )
                _diagnostic(
                    diagnostics,
                    "absent_target",
                    f"node_fields on absent node {node_id!r}: diagnostic no-op",
                    node_id=node_id,
                )
                continue
            node = transaction.mutable(node_id)
            for field_name in payload["field_mask"]:
                node[field_name] = copy.deepcopy(payload["values"][field_name])
            continue

        if effect.effect_type == NODE_PARENTAGE:
            node_id = payload["node_id"]
            node = transaction.get(node_id)
            if node is None:
                if strict:
                    raise EffectApplicationError(
                        f"node_parentage targets absent node {node_id!r}"
                    )
                _diagnostic(
                    diagnostics,
                    "absent_target",
                    f"node_parentage on absent node {node_id!r}: diagnostic no-op",
                    node_id=node_id,
                )
                continue
            transaction.mutable(node_id)["parent_id"] = payload["parent_id"]
            continue

        if effect.effect_type == TOMBSTONE:
            for node_id in payload["node_ids"]:
                if not transaction.remove(node_id):
                    _diagnostic(
                        diagnostics,
                        "absent_target",
                        f"tombstone on absent node {node_id!r}: absence already satisfied",
                        node_id=node_id,
                    )
                    continue
            continue

        # CHILD_ORDER
        parent_uuid = payload["parent_uuid"]
        ordered = payload["ordered_child_ids"]
        listed = set(ordered)
        records = payload.get("canonical_records") or {}

        # Install authorized canonical records (they may themselves be
        # listed children; only ids this transaction created or
        # content-verified can appear here — validated above).
        for record_id, record in records.items():
            if record_id in created:
                # Full installation is allowed for transaction-created ids
                # (A3): install fresh or replace the same-transaction node.
                transaction.install(record)
                continue
            # A canonical record for a PRE-EXISTING (content-verified) id is
            # verification cargo ONLY (A3): node_fields effects own content,
            # child_order owns parentage/order.  Never a wholesale replace.
            if transaction.get(record_id) is None:
                _diagnostic(
                    diagnostics,
                    "canonical_record_absent_preexisting",
                    f"child_order canonical record for pre-existing id {record_id!r} "
                    "absent from cache: skipped (refresh heals)",
                    node_id=record_id,
                )

        resolved: list[tuple[str, dict[str, Any]]] = []
        missing: list[str] = []
        for child_id in ordered:
            node = transaction.get(child_id)
            if node is None:
                missing.append(child_id)
            else:
                resolved.append((child_id, node))
        if missing:
            if strict:
                raise EffectApplicationError(
                    f"child_order lists children absent from cache: {missing}"
                )
            _diagnostic(
                diagnostics,
                "listed_sibling_absent",
                f"child_order skips listed siblings absent from cache: {missing}",
                parent_uuid=parent_uuid,
            )

        resolved_ids = [cid for cid, _node in resolved]
        ranks = _resolve_ranks(resolved_ids, payload.get("priority_by_id"))
        _assert_ranks_reproduce(resolved_ids, ranks)
        for (_child_id, node), rank in zip(resolved, ranks, strict=True):
            node = transaction.mutable(_child_id)
            # Structural fields ONLY — never a pre-existing sibling's
            # name/note/data/timestamps/completion.
            node["parent_id"] = parent_uuid
            node["priority"] = rank

        for slot, node in transaction.children(parent_uuid):
            node_id = node.get("id")
            if (
                node.get("parent_id") == parent_uuid
                and node_id not in listed
                and node_id not in created
            ):
                explained = node_id in tombstoned or node_id in reparented
                if strict and not explained:
                    raise EffectApplicationError(
                        f"child_order of {parent_uuid!r} omits cached child {node_id!r} "
                        "without a same-transaction tombstone/parentage explanation"
                    )
                # replay_authoritative: the record's declared membership
                # authority is applied as-is (the documented tolerated-tier
                # brief cache regression, healed by the next refresh).
                transaction.mutable_slot(slot)["parent_id"] = None
                _diagnostic(
                    diagnostics,
                    "membership_removed",
                    f"child_order removes unlisted child {node_id!r} from cached "
                    f"membership of {parent_uuid!r}",
                    node_id=node_id,
                    parent_uuid=parent_uuid,
                )

    transaction.commit()


# @beacon[
#   id=auto-beacon@apply_journal_transaction-lsld,
#   role=apply_journal_transaction,
#   slice_labels=mci-journal-effects,
#   kind=ast,
#   comment=Apply one journal record with unchanged account/sequence discipline through explicit COW or working-copy input; only complete success advances markers, and new CacheViews explicitly consume transferred reducer output.,
# ]
def apply_journal_transaction(
    cache_view_or_payload: Any,
    transaction: Any,
    *,
    expected_sequence: int,
    mode: ApplicationMode = ApplicationMode.REPLAY_AUTHORITATIVE,
    expected_account: str | None = None,
    diagnostics: list[dict[str, Any]] | None = None,
    input_mode: Literal["copy_on_write", "working_copy"] = "copy_on_write",
) -> Any:
    """Apply one committed journal transaction to a cache view or payload.

    Sequence discipline (audit amendment A2): for a CacheView, legal sequence
    movement is DERIVED from ``view.adopted_sequence`` — normal replay is
    exactly ``adopted_sequence + 1``; a duplicate at the already-adopted
    sequence is a harmless no-op under the immutable-journal identity rule;
    any older sequence or gap fails REGARDLESS of the caller-supplied
    ``expected_sequence`` (which must still equal the transaction's own
    sequence as a caller-consistency check).  For a raw mapping payload (no
    adopted marker), the ``expected_sequence`` contract is the authority.
    Adopted sequence advances only after the complete private apply
    succeeds.  Account mismatches fail closed in BOTH modes when the view
    or caller binds an expected account (A6).
    ``working_copy`` explicitly selects the same indexed engine over an
    owned workspace and returns that same workspace; COW view output is
    explicitly transferred to the builder only after successful application.
    """

    if transaction.sequence != expected_sequence:
        raise JournalIntegrityError(
            f"journal sequence {transaction.sequence} does not match expected "
            f"{expected_sequence} (caller-consistency check)",
            sequence=transaction.sequence,
        )
    from .cache_view import CacheView  # lazy: avoids an import cycle

    if input_mode not in ("copy_on_write", "working_copy") or (
        isinstance(cache_view_or_payload, EffectWorkingCopy) != (input_mode == "working_copy")
    ):
        raise EffectApplicationError("journal input does not match its input_mode")
    if isinstance(cache_view_or_payload, CacheView):
        view = cache_view_or_payload
        if (
            view.account_scope_id is not None
            and view.account_scope_id != transaction.account_scope_id
        ):
            raise CoherenceAccountMismatchError(
                "journal transaction account does not match the cache view account",
                account_scope_id=view.account_scope_id,
                epoch_id=view.epoch_id,
                sequence=transaction.sequence,
            )
        adopted = view.adopted_sequence
        if transaction.sequence == adopted:
            # Immutable-journal identity rule: the same sequence IS the same
            # already-applied transaction — a duplicate is a harmless no-op.
            return view
        if transaction.sequence != adopted + 1:
            raise JournalIntegrityError(
                f"journal sequence {transaction.sequence} is not the contiguous "
                f"next record for adopted sequence {adopted} (gap, reversal, or "
                "older-after-newer)",
                epoch_id=view.epoch_id,
                sequence=transaction.sequence,
            )
        new_payload = apply_semantic_effects(
            view.nodes_export, transaction.effects, mode=mode, diagnostics=diagnostics
        )
        return CacheView.build(
            epoch_number=view.epoch_number,
            epoch_id=view.epoch_id,
            adopted_sequence=transaction.sequence,
            nodes_export=new_payload,
            account_scope_id=view.account_scope_id,
            ownership="transferred",
        )
    if isinstance(cache_view_or_payload, (Mapping, EffectWorkingCopy)):
        if expected_account is not None and transaction.account_scope_id != expected_account:
            raise CoherenceAccountMismatchError(
                "journal transaction account does not match the expected account",
                account_scope_id=expected_account,
                sequence=transaction.sequence,
            )
        return apply_semantic_effects(
            cache_view_or_payload, transaction.effects, mode=mode, diagnostics=diagnostics,
            input_mode=input_mode,
        )
    raise EffectApplicationError(
        "apply_journal_transaction requires a CacheView or a nodes-export mapping"
    )
